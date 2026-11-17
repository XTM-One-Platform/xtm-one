"""A pub/sub subscription gives its connection back to the shared pool when its holder is cancelled.

``redis_pubsub_client()`` draws on the shared pool, and only closing the
subscription returns its connection to it. Starlette ends a streaming response
whose client left by cancelling it in an anyio cancel scope, which cancels every
later await too: the activity stream's ``finally`` stopped at its first await,
and each closed browser tab kept a connection checked out for the life of the
process, until every Redis command failed with "Too many connections".
"""

import asyncio
import logging
import re
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
import redis.asyncio as redis_asyncio

from app.api.v1 import activity as activity_module
from app.core import cancellation
from app.core import redis as core_redis

APP_DIR = Path(__file__).resolve().parents[1] / "app"


def _subscription(**overrides) -> tuple[MagicMock, MagicMock]:
    """A pub/sub and its client whose every call yields to the loop, as the real ones do."""

    async def _yield(*_args, **_kwargs):
        await asyncio.sleep(0)

    pubsub = MagicMock()
    pubsub.subscribe = AsyncMock(side_effect=overrides.get("subscribe", _yield))
    pubsub.get_message = AsyncMock(side_effect=overrides.get("get_message", _yield))
    # The subscription's connection, checked out of the pool until it is released.
    pubsub.connection = MagicMock()
    pubsub.connection.disconnect = AsyncMock(side_effect=overrides.get("disconnect", _yield))
    pubsub.connection_pool.release = AsyncMock(side_effect=_yield)
    client = MagicMock()
    client.pubsub = MagicMock(return_value=pubsub)
    client.aclose = AsyncMock(side_effect=_yield)
    return pubsub, client


def _count_shield_waits(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """How many times the helpers re-enter their wait: once per cancellation delivered to them."""
    waits = [0]
    shield = asyncio.shield

    def _counting(awaitable):
        waits[0] += 1
        return shield(awaitable)

    monkeypatch.setattr(cancellation.asyncio, "shield", _counting)
    return waits


@pytest.mark.asyncio
async def test_a_cancelled_holder_still_closes_its_subscription():
    pubsub, client = _subscription()
    with anyio.CancelScope() as scope:
        try:
            scope.cancel()
            await asyncio.sleep(1)
        finally:
            await core_redis.close_pubsub(pubsub, client)
    _assert_handed_back(pubsub)
    client.aclose.assert_awaited_once()


def _assert_handed_back(pubsub: MagicMock) -> None:
    """The subscription's connection went back to the pool, once."""
    connection = pubsub.connection_pool.release.await_args.args[0]
    pubsub.connection_pool.release.assert_awaited_once_with(connection)
    connection.disconnect.assert_awaited()
    assert pubsub.connection is None


@pytest.mark.asyncio
async def test_a_close_waits_under_a_cancelled_anyio_scope_without_spinning(monkeypatch: pytest.MonkeyPatch):
    async def _slow_close(**_kwargs):
        await asyncio.sleep(0.2)

    pubsub, client = _subscription(disconnect=_slow_close)
    waits = _count_shield_waits(monkeypatch)
    with anyio.CancelScope() as scope:
        try:
            scope.cancel()
            await asyncio.sleep(1)
        finally:
            await core_redis.close_pubsub(pubsub, client)
    _assert_handed_back(pubsub)
    # Unshielded, the cancelled scope re-cancels the task on every loop tick: thousands of waits.
    assert waits[0] <= 2


@pytest.mark.asyncio
async def test_a_cleanup_waits_under_a_cancelled_anyio_scope_without_spinning(monkeypatch: pytest.MonkeyPatch):
    waits = _count_shield_waits(monkeypatch)
    cleaned = asyncio.Event()

    async def _cleanup():
        await asyncio.sleep(0.2)
        cleaned.set()

    with anyio.CancelScope() as scope:
        try:
            scope.cancel()
            await asyncio.sleep(1)
        finally:
            await cancellation.run_cleanup_to_completion(_cleanup())
    assert cleaned.is_set()
    assert waits[0] <= 2


@pytest.mark.asyncio
async def test_a_cancellation_during_the_close_is_let_through_once_it_is_done():
    closed = asyncio.Event()

    async def _slow_close(**_kwargs):
        await asyncio.sleep(0.1)
        closed.set()

    pubsub, client = _subscription(disconnect=_slow_close)
    closing = asyncio.ensure_future(core_redis.close_pubsub(pubsub, client))
    await asyncio.sleep(0.01)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert closed.is_set(), "the close was abandoned half-done"
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_socket_that_never_closes_is_dropped_and_its_connection_handed_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setattr(core_redis, "PUBSUB_SOCKET_CLOSE_SECONDS", 0.05)

    async def _never_answers(nowait: bool = False):
        if not nowait:
            await asyncio.Event().wait()

    pubsub, client = _subscription(disconnect=_never_answers)
    connection = pubsub.connection
    pubsub.channels = {b"activity:a": None}
    pubsub.patterns = {b"wake:*": None}
    subscribed_at_release: list[tuple] = []

    async def _release(_connection):
        # The next borrower reconnects this connection: by then the pubsub
        # must not subscribe again through its connect callback.
        subscribed_at_release.append((dict(pubsub.channels), dict(pubsub.patterns)))

    pubsub.connection_pool.release = AsyncMock(side_effect=_release)

    with caplog.at_level(logging.WARNING, logger=core_redis.__name__):
        await asyncio.wait_for(core_redis.close_pubsub(pubsub, client), 2)

    connection.deregister_connect_callback.assert_called_once_with(pubsub.on_connect)
    connection.disconnect.assert_awaited_with(nowait=True)
    pubsub.connection_pool.release.assert_awaited_once_with(connection)
    assert subscribed_at_release == [({}, {})]
    assert pubsub.connection is None
    client.aclose.assert_awaited_once()
    assert any("did not finish (TimeoutError)" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_the_connection_is_handed_back_once_after_its_socket_is_closed():
    """Never from inside a close cut short in ``ConnectionPool.release()``: it could hand back a
    connection another borrower already holds, or lose its slot."""
    events: list[str] = []

    async def _disconnect(**_kwargs):
        await asyncio.sleep(0)
        events.append("socket closed")

    pubsub, client = _subscription(disconnect=_disconnect)

    async def _release(_connection):
        events.append("handed back")

    pubsub.connection_pool.release = AsyncMock(side_effect=_release)
    await core_redis.close_pubsub(pubsub, client)
    assert events == ["socket closed", "handed back"]


def _record_deadlines(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The deadlines the close applies, in order: asserted instead of a wall-clock duration."""
    deadlines: list[float] = []
    timeout_at = asyncio.timeout_at

    def _recording(when):
        deadlines.append(when)
        return timeout_at(when)

    monkeypatch.setattr(core_redis.asyncio, "timeout_at", _recording)
    return deadlines


@pytest.mark.asyncio
async def test_the_whole_close_has_one_bound(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    monkeypatch.setattr(core_redis, "PUBSUB_CLOSE_SECONDS", 0.2)
    monkeypatch.setattr(core_redis, "PUBSUB_SOCKET_CLOSE_SECONDS", 0.1)
    deadlines = _record_deadlines(monkeypatch)

    async def _never_answers(nowait: bool = False):
        if not nowait:
            await asyncio.Event().wait()

    pubsub, client = _subscription(disconnect=_never_answers)
    client.aclose = AsyncMock(side_effect=_never_answers)
    loop = asyncio.get_running_loop()
    started = loop.time()
    with caplog.at_level(logging.WARNING, logger=core_redis.__name__):
        await core_redis.close_pubsub(pubsub, client)
    # A stalled socket close, then a stalled client close: both bounded by one
    # 0.2 s deadline read once at the start, not one bound per step.
    socket_deadline, close_deadline = deadlines
    assert started + 0.2 <= close_deadline <= loop.time()
    assert close_deadline - socket_deadline == pytest.approx(0.1)
    pubsub.connection_pool.release.assert_awaited_once()
    assert any("took over" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_a_stalled_loop_past_every_deadline_still_hands_the_connection_back(
    monkeypatch: pytest.MonkeyPatch,
):
    """A worker that blocks the loop past both deadlines (CI under load, a GC pause).

    Two nested timers then expire in the same loop iteration and cancel the close
    twice: the socket timeout no longer becomes a TimeoutError and the hand-back
    to the pool was skipped, leaking the connection.
    """
    monkeypatch.setattr(core_redis, "PUBSUB_CLOSE_SECONDS", 0.02)
    monkeypatch.setattr(core_redis, "PUBSUB_SOCKET_CLOSE_SECONDS", 0.01)

    async def _stalls_then_never_answers(nowait: bool = False):
        if not nowait:
            time.sleep(0.05)  # blocks the loop past both deadlines, on purpose
            await asyncio.Event().wait()

    pubsub, client = _subscription(disconnect=_stalls_then_never_answers)
    connection = pubsub.connection
    await asyncio.wait_for(core_redis.close_pubsub(pubsub, client), 2)
    connection.disconnect.assert_awaited_with(nowait=True)
    pubsub.connection_pool.release.assert_awaited_once_with(connection)
    assert pubsub.connection is None


@pytest.mark.asyncio
async def test_a_subscription_without_a_connection_hands_nothing_back(caplog: pytest.LogCaptureFixture):
    pubsub, client = _subscription()
    pubsub.connection = None
    with caplog.at_level(logging.WARNING, logger=core_redis.__name__):
        await core_redis.close_pubsub(pubsub, client)
    pubsub.connection_pool.release.assert_not_awaited()
    client.aclose.assert_awaited_once()
    assert not caplog.records


@pytest.mark.asyncio
async def test_a_failed_wake_subscription_of_the_reconciler_is_closed(monkeypatch: pytest.MonkeyPatch):
    """``subscribe()`` checked a connection out of the pool before it failed: the reconciler never held it."""
    from app.services import orchestration_reconciler

    async def _refused(*_args, **_kwargs):
        raise ConnectionError("refused")

    pubsub, client = _subscription(subscribe=_refused)
    monkeypatch.setattr(core_redis, "redis_pubsub_client", AsyncMock(return_value=client))
    assert await orchestration_reconciler._subscribe_wake() == (None, None)
    _assert_handed_back(pubsub)
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failed_work_item_dispatch_wake_subscription_is_closed(monkeypatch: pytest.MonkeyPatch):
    from app.services import work_item_dispatch_wake

    async def _refused(*_args, **_kwargs):
        raise ConnectionError("refused")

    pubsub, client = _subscription(subscribe=_refused)
    monkeypatch.setattr(core_redis, "redis_pubsub_client", AsyncMock(return_value=client))
    with pytest.raises(ConnectionError):
        await work_item_dispatch_wake._subscribe()
    _assert_handed_back(pubsub)
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_activity_stream_closes_its_subscription_when_the_browser_leaves(
    monkeypatch: pytest.MonkeyPatch,
):
    user_id = uuid.uuid4()
    monkeypatch.setattr("app.api.deps.decode_access_token", lambda _t: {"sub": str(user_id)})
    monkeypatch.setattr(activity_module, "release_db_connection_before_loop", AsyncMock())

    async def _quiet(*_args, **_kwargs):
        await asyncio.sleep(0.01)

    subscribed = asyncio.Event()

    async def _subscribe(*_args, **_kwargs):
        subscribed.set()

    pubsub, client = _subscription(get_message=_quiet, subscribe=_subscribe)
    monkeypatch.setattr(activity_module, "redis_pubsub_client", AsyncMock(return_value=client))
    db = AsyncMock()
    db_result = MagicMock()
    db_result.scalar_one_or_none.return_value = SimpleNamespace(id=user_id, is_active=True, password_changed_at=None)
    db.execute = AsyncMock(return_value=db_result)
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)

    response = await activity_module.activity_stream(request, token="tok", db=db)

    # The ASGI spec uvicorn declares (2.3): Starlette races the stream against the
    # client's disconnect in a task group, and cancels the stream when it comes.
    left = asyncio.Event()

    async def receive():
        await left.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}}
    serving = asyncio.ensure_future(response(scope, receive, send))
    await asyncio.wait_for(subscribed.wait(), 5)
    left.set()
    await asyncio.wait_for(serving, 5)

    _assert_handed_back(pubsub)
    client.aclose.assert_awaited_once()


def test_every_subscription_in_the_app_is_opened_and_closed_through_core_redis():
    """A holder that opens or closes its own subscription leaks it when cancelled.

    No ``.pubsub()`` and no ``pubsub.aclose()`` outside ``app/core/redis.py``,
    and each ``open_pubsub()`` of a module has its own ``close_pubsub()``.
    """
    holders, own_opens, own_closes = [], [], []
    for path in sorted(APP_DIR.rglob("*.py")):
        if path == APP_DIR / "core" / "redis.py":
            continue
        source = path.read_text(encoding="utf-8")
        relative = path.relative_to(APP_DIR.parent).as_posix()
        opened = len(re.findall(r"\bopen_pubsub\(", source))
        closed = len(re.findall(r"\bclose_pubsub\(", source))
        if opened > closed:
            holders.append(f"{relative} ({opened} opened, {closed} closed)")
        if re.search(r"\.pubsub\(\)", source):
            own_opens.append(relative)
        if re.search(r"pubsub\.aclose\(", source):
            own_closes.append(relative)
    assert own_opens == [], f".pubsub() outside app/core/redis.py: {own_opens}"
    assert holders == [], f"subscriptions not closed with close_pubsub(): {holders}"
    assert own_closes == [], f"pubsub.aclose() outside app/core/redis.py: {own_closes}"


@pytest.mark.asyncio
async def test_a_subscribe_that_fails_closes_the_subscription_and_its_client():
    async def _refused(*_args, **_kwargs):
        raise ConnectionError("refused")

    pubsub, client = _subscription(subscribe=_refused)
    with pytest.raises(ConnectionError):
        await core_redis.open_pubsub(client, "activity:a")
    _assert_handed_back(pubsub)
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_cancelled_subscribe_closes_the_subscription_and_its_client():
    started = asyncio.Event()

    async def _hangs(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    pubsub, client = _subscription(subscribe=_hangs)
    opening = asyncio.ensure_future(core_redis.open_pubsub(client, "activity:a"))
    await started.wait()
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    _assert_handed_back(pubsub)
    client.aclose.assert_awaited_once()


class _SilentConnection(redis_asyncio.Connection):
    """A real pooled connection that talks to no server: what ``close_pubsub`` does to the pool is real."""

    def __init__(self, *args, hang_on_close: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.hang_on_close = hang_on_close

    async def connect(self):
        return None

    async def can_read_destructive(self):
        return False

    async def send_packed_command(self, command, check_health=True):
        return None

    async def disconnect(self, nowait: bool = False, **kwargs):
        if self.hang_on_close and not nowait:
            await asyncio.Event().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("hang_on_close", [False, True], ids=["socket closes", "socket never closes"])
async def test_a_real_pool_gets_the_subscription_connection_back(monkeypatch: pytest.MonkeyPatch, hang_on_close: bool):
    """Against redis-py's own ``ConnectionPool`` and ``PubSub``: no server, nothing faked but the socket."""
    monkeypatch.setattr(core_redis, "PUBSUB_SOCKET_CLOSE_SECONDS", 0.05)
    pool = redis_asyncio.ConnectionPool(
        connection_class=_SilentConnection, max_connections=2, hang_on_close=hang_on_close
    )
    client = redis_asyncio.Redis(connection_pool=pool)
    pubsub = await core_redis.open_pubsub(client, "activity:a")
    assert len(pool._in_use_connections) == 1

    with anyio.CancelScope() as scope:
        try:
            scope.cancel()
            await asyncio.sleep(1)
        finally:
            await core_redis.close_pubsub(pubsub, client)

    assert pool._in_use_connections == set()
    assert len(pool._available_connections) == 1
    assert pubsub.connection is None
