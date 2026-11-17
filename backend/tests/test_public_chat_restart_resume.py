"""Widget messages a platform restart left unanswered are answered once.

A worker stopped or killed mid-turn takes the widget turn with it: the
visitor's message is stored, its answer never is. The scheduler tick's
``resume_unanswered_widget_sessions`` takes such a session's turn, reads it
again under the turn, claims the message once and answers it in the
background; a live turn keeps its lock alive so it is never taken.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.api.v1 import public_chat

pytestmark = pytest.mark.asyncio


class _World:
    """The sessions, their turn locks, the resume claims and the answers, in memory."""

    def __init__(self) -> None:
        self.sessions: list[tuple[uuid.UUID, uuid.UUID, str | None]] = []
        self.unanswered: dict[uuid.UUID, SimpleNamespace | None] = {}
        self.held: dict[uuid.UUID, str] = {}
        self.claimed: set[object] = set()
        self.answered: list[tuple[uuid.UUID, int]] = []
        self.agent = SimpleNamespace(id=uuid.uuid4(), enabled=True, web_enabled=True)
        self.chat_enabled = True

    def add(self, sequence: int = 1, *, held: bool = False) -> uuid.UUID:
        cid = uuid.uuid4()
        self.sessions.append((cid, self.agent.id, "visitor"))
        self.unanswered[cid] = SimpleNamespace(id=uuid.uuid4(), sequence=sequence, content=f"m{sequence}")
        if held:
            self.held[cid] = "live-turn"
        return cid


@pytest.fixture
def world(monkeypatch):
    state = _World()

    async def _sessions(_now):
        return list(state.sessions)

    async def _take(cid):
        if cid in state.held:
            return False, None
        token = uuid.uuid4().hex
        state.held[cid] = token
        return True, token

    async def _release(cid, token):
        if state.held.get(cid) == token:
            del state.held[cid]

    async def _latest(cid):
        return state.unanswered.get(cid)

    async def _agent(_agent_id):
        agent = state.agent
        return agent if agent.enabled and agent.web_enabled else None

    async def _claim(message_id):
        if message_id in state.claimed:
            return False
        state.claimed.add(message_id)
        return True

    async def _enabled():
        return state.chat_enabled

    async def _answer(_agent, cid, message, **_turn):
        assert cid in state.held, "the resumed message is answered while the turn is held"
        state.answered.append((cid, message.sequence))
        state.unanswered[cid] = None

    async def _next(_cid, _after):
        return None

    async def _refresh(*_args):
        return None

    monkeypatch.setattr(public_chat, "_sessions_left_unanswered", _sessions)
    monkeypatch.setattr(public_chat, "_take_widget_turn", _take)
    monkeypatch.setattr(public_chat, "_release_widget_turn", _release)
    monkeypatch.setattr(public_chat, "_latest_unanswered", _latest)
    monkeypatch.setattr(public_chat, "_resumable_agent", _agent)
    monkeypatch.setattr(public_chat, "_claim_widget_resume", _claim)
    monkeypatch.setattr(public_chat, "_widget_chat_enabled", _enabled)
    monkeypatch.setattr(public_chat, "_answer_queued_message", _answer)
    monkeypatch.setattr(public_chat, "_next_unanswered", _next)
    monkeypatch.setattr("app.services.chat_turn_guard.refresh_turn", _refresh)
    monkeypatch.setattr("app.core.worker_shutdown.shutdown_request", lambda: None)
    return state


async def _settle():
    await asyncio.gather(*list(public_chat._WIDGET_FOLLOW_UPS))


async def test_a_message_a_restart_left_unanswered_is_answered_once(world):
    cid = world.add(sequence=3)
    assert await public_chat.resume_unanswered_widget_sessions() == 1
    await _settle()
    assert world.answered == [(cid, 3)]
    assert cid not in world.held
    # The next ticks find it answered, and the claim holds even if they did not.
    world.unanswered[cid] = SimpleNamespace(id=next(iter(world.claimed)), sequence=3, content="m3")
    assert await public_chat.resume_unanswered_widget_sessions() == 0
    await _settle()
    assert world.answered == [(cid, 3)]
    assert cid not in world.held


async def test_a_session_whose_turn_is_held_is_left_to_it(world):
    cid = world.add(held=True)
    assert await public_chat.resume_unanswered_widget_sessions() == 0
    assert world.answered == []
    assert world.held[cid] == "live-turn"


async def test_a_session_answered_meanwhile_gives_the_turn_back(world):
    cid = world.add()
    # A send answered it between the listing and the turn.
    world.unanswered[cid] = None
    assert await public_chat.resume_unanswered_widget_sessions() == 0
    assert world.answered == []
    assert cid not in world.held


async def test_two_sweeps_resume_a_message_once(world):
    cid = world.add()
    first, second = await asyncio.gather(
        public_chat.resume_unanswered_widget_sessions(),
        public_chat.resume_unanswered_widget_sessions(),
    )
    await _settle()
    assert first + second == 1
    assert world.answered == [(cid, 1)]


async def test_a_disabled_agent_is_not_resumed(world):
    cid = world.add()
    world.agent.web_enabled = False
    assert await public_chat.resume_unanswered_widget_sessions() == 0
    assert world.answered == []
    assert cid not in world.held
    assert world.claimed == set()


async def test_nothing_is_resumed_when_chat_is_off(world):
    world.add()
    world.chat_enabled = False
    assert await public_chat.resume_unanswered_widget_sessions() == 0
    assert world.answered == []


async def test_a_stopping_process_takes_nothing(world, monkeypatch):
    world.add()
    monkeypatch.setattr("app.core.worker_shutdown.shutdown_request", lambda: SimpleNamespace(reason="stop"))
    assert await public_chat.resume_unanswered_widget_sessions() == 0
    assert world.answered == []
    assert world.held == {}


async def test_a_failed_listing_never_raises(world, monkeypatch):
    async def _boom(_now):
        raise RuntimeError("database gone")

    monkeypatch.setattr(public_chat, "_sessions_left_unanswered", _boom)
    assert await public_chat.resume_unanswered_widget_sessions() == 0


async def test_a_queued_turn_cut_by_a_stop_gives_the_session_back(world, monkeypatch):
    cid = world.add()
    _, token = await public_chat._take_widget_turn(cid)

    async def _cut(*_args, **_kwargs):
        raise asyncio.CancelledError("timeout graceful shutdown exceeded")

    monkeypatch.setattr(public_chat, "_answer_queued_message", _cut)
    with pytest.raises(asyncio.CancelledError):
        await public_chat._answer_queued(
            world.agent, cid, token, world.unanswered[cid], session_id="s", context=None, visitor_id=None
        )
    # Released at once: the next process's sweep takes it, not a lock left to lapse.
    assert cid not in world.held


async def test_a_live_turn_keeps_its_lock_alive(monkeypatch):
    beats: list[tuple[str, str]] = []

    async def _refresh(cid, token):
        beats.append((cid, token))

    monkeypatch.setattr("app.services.chat_turn_guard.refresh_turn", _refresh)
    monkeypatch.setattr(public_chat, "_WIDGET_TURN_REFRESH_SECONDS", 0.01)
    cid = uuid.uuid4()
    async with public_chat._widget_turn_keepalive(cid, "tok"):
        await asyncio.sleep(0.05)
    assert beats and set(beats) == {(str(cid), "tok")}
    count = len(beats)
    await asyncio.sleep(0.03)
    assert len(beats) == count, "the beat stops with the turn"


async def test_no_token_no_keepalive(monkeypatch):
    async def _refresh(*_args):
        raise AssertionError("no lock to renew")

    monkeypatch.setattr("app.services.chat_turn_guard.refresh_turn", _refresh)
    monkeypatch.setattr(public_chat, "_WIDGET_TURN_REFRESH_SECONDS", 0.01)
    async with public_chat._widget_turn_keepalive(uuid.uuid4(), None):
        await asyncio.sleep(0.03)


async def test_the_claim_key_names_the_message():
    assert public_chat.widget_resume_claim_key("m1") == "public_chat:widget_resume:m1"
