"""One turn per public widget session.

A message sent while a turn answers the session is stored and answered with
``queued``; the turn's holder answers every message stored meanwhile as ONE
next turn on the latest, then gives the turn back.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.api.v1 import public_chat

pytestmark = pytest.mark.asyncio


class _Turns:
    """The session's turn lock and its stored visitor messages, in memory."""

    def __init__(self, held: bool = False):
        self.holder: str | None = "other" if held else None
        self.messages: list[SimpleNamespace] = []
        self.answered: list[int] = []
        self.log: list[str] = []

    def store(self, sequence: int) -> SimpleNamespace:
        message = SimpleNamespace(id=uuid.uuid4(), sequence=sequence, content=f"m{sequence}")
        self.messages.append(message)
        return message


@pytest.fixture
def turns(monkeypatch):
    state = _Turns()

    async def _take(_conversation_id):
        if state.holder is not None:
            state.log.append("queued")
            return False, None
        state.holder = uuid.uuid4().hex
        state.log.append("take")
        return True, state.holder

    async def _release(_conversation_id, token):
        if state.holder == token:
            state.holder = None
            state.log.append("release")

    async def _next(_conversation_id, after_sequence):
        later = [m for m in state.messages if m.sequence > after_sequence]
        return max(later, key=lambda m: m.sequence) if later else None

    async def _answer(_agent, _conversation_id, message, **_turn):
        assert state.holder is not None, "a queued message is answered while the turn is held"
        state.answered.append(message.sequence)

    async def _refresh(*_args):
        return None

    monkeypatch.setattr(public_chat, "_take_widget_turn", _take)
    monkeypatch.setattr(public_chat, "_release_widget_turn", _release)
    monkeypatch.setattr(public_chat, "_next_unanswered", _next)
    monkeypatch.setattr(public_chat, "_answer_queued_message", _answer)
    monkeypatch.setattr("app.services.chat_turn_guard.refresh_turn", _refresh)
    return state


async def _settle():
    await asyncio.gather(*list(public_chat._WIDGET_FOLLOW_UPS))


_TURN = {"session_id": "s", "context": None, "visitor_id": None}


async def test_a_turn_with_nothing_queued_gives_the_turn_back(turns):
    _, token = await public_chat._take_widget_turn(uuid.uuid4())
    turns.store(1)
    await public_chat._finish_widget_turn(SimpleNamespace(), uuid.uuid4(), token, 1, **_TURN)
    await _settle()
    assert turns.holder is None
    assert turns.answered == []


async def test_messages_queued_mid_turn_are_answered_once_on_the_latest(turns):
    _, token = await public_chat._take_widget_turn(uuid.uuid4())
    turns.store(1)
    # Two more messages arrive while the turn runs: both find it held.
    for sequence in (3, 5):
        answer_now, _ = await public_chat._take_widget_turn(uuid.uuid4())
        assert answer_now is False
        turns.store(sequence)
    await public_chat._finish_widget_turn(SimpleNamespace(), uuid.uuid4(), token, 1, **_TURN)
    await _settle()
    # One next turn, on the latest message: its history holds the other one.
    assert turns.answered == [5]
    assert turns.holder is None


async def test_a_message_queued_during_the_follow_up_turn_gets_its_own(turns, monkeypatch):
    _, token = await public_chat._take_widget_turn(uuid.uuid4())
    turns.store(1)
    turns.store(3)
    answer = public_chat._answer_queued_message

    async def _answer_and_receive(agent, conversation_id, message, **turn):
        await answer(agent, conversation_id, message, **turn)
        if message.sequence == 3:
            turns.store(5)

    monkeypatch.setattr(public_chat, "_answer_queued_message", _answer_and_receive)
    await public_chat._finish_widget_turn(SimpleNamespace(), uuid.uuid4(), token, 1, **_TURN)
    await _settle()
    assert turns.answered == [3, 5]
    assert turns.holder is None


async def test_a_message_stored_between_the_check_and_the_release_is_answered(turns, monkeypatch):
    _, token = await public_chat._take_widget_turn(uuid.uuid4())
    turns.store(1)
    release = public_chat._release_widget_turn
    raced = []

    async def _release_after_a_send(conversation_id, held):
        # Its sender found the turn held and stored it: nobody else answers it.
        if not raced:
            raced.append(1)
            turns.store(3)
        await release(conversation_id, held)

    monkeypatch.setattr(public_chat, "_release_widget_turn", _release_after_a_send)
    await public_chat._finish_widget_turn(SimpleNamespace(), uuid.uuid4(), token, 1, **_TURN)
    await _settle()
    assert turns.answered == [3]
    assert turns.holder is None


async def test_a_failed_follow_up_gives_the_turn_back(turns, monkeypatch):
    _, token = await public_chat._take_widget_turn(uuid.uuid4())
    turns.store(1)
    turns.store(3)

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("the store went away")

    monkeypatch.setattr(public_chat, "_answer_queued_message", _boom)
    await public_chat._finish_widget_turn(SimpleNamespace(), uuid.uuid4(), token, 1, **_TURN)
    await _settle()
    assert turns.holder is None


async def test_without_redis_every_message_is_its_own_turn(monkeypatch):
    async def _no_redis(*_args, **_kwargs):
        raise ConnectionError("refused")

    monkeypatch.setattr("app.services.chat_turn_guard.acquire_turn", _no_redis)
    assert await public_chat._take_widget_turn(uuid.uuid4()) == (True, None)
    # No token: nothing to hand on, nothing released.
    await public_chat._finish_widget_turn(SimpleNamespace(), uuid.uuid4(), None, 1, **_TURN)
    assert not public_chat._WIDGET_FOLLOW_UPS


async def test_a_held_session_answers_the_send_with_queued(monkeypatch):
    async def _held(_conversation_id, **_kwargs):
        return None

    monkeypatch.setattr("app.services.chat_turn_guard.acquire_turn", _held)
    assert await public_chat._take_widget_turn(uuid.uuid4()) == (False, None)
