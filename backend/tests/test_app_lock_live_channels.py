"""The PIN lock holds on the channels that stay open, not only at their handshake.

A web chat stream or an activity stream opened while the companion was
unlocked must not outlive the lock: once the session relocks (the unlock
window ran out, ``POST /app-lock/lock``) or wrong PINs end it, the stream
ends at its next session check (``chat_web_transport.session_lock_state``), as
every REST route answers 423 / 401 (``services/app_lock.py``). A send, a
steering message and a reviewer's verdict are each a request of their own,
refused by that dependency.

The lock's own rules are pinned against PostgreSQL in
``test_app_lock_postgres.py``. Here ``app_lock.enforce`` is replaced by a
switch, to pin where the open channels read it.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, status

from app.core.security import create_access_token
from tests.test_session_timeout import (  # noqa: F401 - ``redis`` is a fixture
    _account,
    _PubSubClient,
    _UserLookup,
    redis,
)

# The in-memory Redis of the session checks, for every test here.
pytestmark = pytest.mark.usefixtures("redis")

LOCKED_CLOSE = 4023


class _Lock:
    """What ``app_lock.enforce`` answers: open, locked (423), ended by the PIN (401), or broken (unreadable)."""

    def __init__(self) -> None:
        self.state = "open"
        self.calls = 0

    async def enforce(self, _db, _user, *, now=None) -> None:
        self.calls += 1
        if self.state == "locked":
            raise HTTPException(status_code=status.HTTP_423_LOCKED, detail={"code": "app_locked"})
        if self.state == "ended":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "app_lock_ended"})
        if self.state == "broken":
            raise RuntimeError("the lock's table cannot be read")


@pytest.fixture
def lock(monkeypatch) -> _Lock:
    switch = _Lock()
    monkeypatch.setattr("app.services.app_lock.enforce", switch.enforce)
    return switch


# ---------------------------------------------------------------------------
# Web chat streams
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _no_db():
    yield None


@pytest.mark.parametrize(("state", "told"), [("open", "open"), ("locked", "locked"), ("ended", "locked"), ("broken", "unreadable")])
async def test_a_chat_stream_tells_a_lock_from_one_it_cannot_read(monkeypatch, lock, state, told):
    from app.services.chat_web_transport import session_lock_state

    monkeypatch.setattr("app.core.database.async_session", _no_db)
    lock.state = state
    assert await session_lock_state("00000000-0000-4000-8000-000000000001", None) == told
    assert lock.calls == 1


async def test_a_lock_that_cannot_be_read_ends_a_chat_stream(monkeypatch, lock):
    from app.services.chat_web_transport import session_lock_state

    @asynccontextmanager
    async def _down():
        raise ConnectionError("database down")
        yield  # pragma: no cover

    monkeypatch.setattr("app.core.database.async_session", _down)
    assert await session_lock_state("00000000-0000-4000-8000-000000000002", None) == "unreadable"


async def _chat_events(monkeypatch, relay) -> list[bytes]:
    """The frames of a conversation's event stream, opened while unlocked, up to its end."""
    import asyncio
    import uuid
    from types import SimpleNamespace

    from app.api.v1 import chat_web_stream
    from app.services import chat_turn_stream

    monkeypatch.setattr(chat_turn_stream, "relay_turn_frames", relay)
    monkeypatch.setattr(chat_web_stream, "release_db_connection_before_loop", AsyncMock())
    monkeypatch.setattr(chat_web_stream.chat_turn_guard, "mark_client_seen", AsyncMock())
    # Short, not zero: the loop then waits on its queue, so the relay task runs.
    monkeypatch.setattr(chat_web_stream, "SESSION_RECHECK_SECONDS", 0.01)
    monkeypatch.setattr(chat_web_stream, "recheck_session", AsyncMock(side_effect=lambda session: session))
    monkeypatch.setattr("app.core.database.async_session", _no_db)
    account = _account()
    conversation = SimpleNamespace(id=uuid.uuid4(), user_id=account.id)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: conversation)))
    response = await chat_web_stream.stream_web_chat_events(
        str(conversation.id),
        SimpleNamespace(headers={}),
        client_id="tab-1",
        db=db,
        user=SimpleNamespace(id=account.id, web_session=SimpleNamespace()),
    )
    frames: list[bytes] = []

    async def _read() -> None:
        async for frame in response.body_iterator:
            frames.append(frame)

    await asyncio.wait_for(_read(), 5)
    return frames


@pytest.mark.parametrize("state", ["locked", "ended"])
async def test_the_web_chat_events_stream_ends_when_its_session_locks(monkeypatch, lock, state):
    import asyncio

    async def _follows(*_args, **_kwargs):
        await asyncio.Event().wait()

    lock.state = state
    frames = await _chat_events(monkeypatch, _follows)
    # It ends on an app_locked frame, the lock screen's cue, never a
    # session_expired one, which would sign the app out: the app's next request
    # reads the lock (423) or the PIN's end (401).
    assert not any(b"session_expired" in frame for frame in frames)
    assert frames[-1] == b'data: {"type": "app_locked"}\n\n'
    assert lock.calls >= 1


async def test_a_web_chat_events_stream_whose_lock_cannot_be_read_just_ends(monkeypatch, lock):
    import asyncio

    async def _follows(*_args, **_kwargs):
        await asyncio.Event().wait()

    lock.state = "broken"
    frames = await _chat_events(monkeypatch, _follows)
    # No app_locked frame: a person with no PIN is never shown the lock screen
    # for a database blip. The app's next request reads the lock, if any.
    assert not any(b"app_locked" in frame for frame in frames)
    assert lock.calls >= 1


async def test_an_unlocked_session_keeps_its_web_chat_events_stream(monkeypatch, lock):
    async def _one_frame(_conversation_id, *, send, **_kwargs):
        await send({"type": "user_message", "message": {"id": "m1"}})

    frames = await _chat_events(monkeypatch, _one_frame)
    assert frames == [b'data: {"type": "user_message", "message": {"id": "m1"}}\n\n']


# ---------------------------------------------------------------------------
# Activity stream
# ---------------------------------------------------------------------------


async def _stream(monkeypatch, *, session_factory) -> list[str]:
    from app.api.v1 import activity as activity_module

    account = _account()
    token = create_access_token(str(account.id))

    async def _pubsub_client():
        return _PubSubClient()

    monkeypatch.setattr(activity_module, "redis_pubsub_client", _pubsub_client)
    monkeypatch.setattr(activity_module, "release_db_connection_before_loop", AsyncMock())
    monkeypatch.setattr(activity_module, "SESSION_RECHECK_SECONDS", 0)
    monkeypatch.setattr(activity_module.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr("app.core.database.async_session", session_factory)
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)
    response = await activity_module.activity_stream(request, token=token, db=_UserLookup(account))
    return [frame async for frame in response.body_iterator]


@asynccontextmanager
async def _fresh_session():
    yield object()


@pytest.mark.parametrize("state", ["locked", "ended"])
async def test_the_activity_stream_ends_when_its_session_locks(monkeypatch, lock, state):
    async def _lock_after_connect(*_args, **_kwargs):
        lock.state = state

    # Locked right after the connect check passed: the next recheck ends the stream.
    monkeypatch.setattr("app.api.v1.activity.release_db_connection_before_loop", _lock_after_connect)
    from app.api.v1 import activity as activity_module

    account = _account()
    token = create_access_token(str(account.id))

    async def _pubsub_client():
        return _PubSubClient()

    monkeypatch.setattr(activity_module, "redis_pubsub_client", _pubsub_client)
    monkeypatch.setattr(activity_module, "SESSION_RECHECK_SECONDS", 0)
    monkeypatch.setattr(activity_module.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr("app.core.database.async_session", _fresh_session)
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)
    response = await activity_module.activity_stream(request, token=token, db=_UserLookup(account))
    frames = [frame async for frame in response.body_iterator]
    # The stream just ends: the app's next request reads the 423 or the 401.
    assert frames == []
    assert lock.calls == 2  # at connect, then at the first recheck


async def test_the_activity_stream_ends_when_the_lock_cannot_be_read(monkeypatch, lock):
    @asynccontextmanager
    async def _unreachable():
        raise ConnectionError("database down")
        yield  # pragma: no cover

    frames = await _stream(monkeypatch, session_factory=_unreachable)
    assert frames == []
    assert lock.calls == 1  # at connect only
