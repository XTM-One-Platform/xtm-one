"""One turn at a time per conversation of a chat agent account.

Two messages a few seconds apart in one chat used to start two turns that both
answered. The pipeline now holds the conversation: a message that finds a turn
in flight is queued, and the queued messages are answered as ONE next turn, on
the latest of them. A queue a dead process left is handed to a new turn.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.agent_accounts import activity, listening, pipeline, state, turns
from app.services.agent_accounts.events import AccountEvent
from app.services.agent_accounts.rules import Decision


class FakeRedis:
    """The calls ``turns`` makes, in memory."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, key):
        self.values.pop(key, None)
        self.lists.pop(key, None)

    async def exists(self, key):
        return int(key in self.values or bool(self.lists.get(key)))

    async def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    async def ltrim(self, key, start, end):
        items = self.lists.get(key, [])
        self.lists[key] = items[start:] if end == -1 else items[start : end + 1]

    async def expire(self, key, seconds):
        return True

    async def lpop(self, key):
        items = self.lists.get(key)
        if not items:
            return None
        value = items.pop(0)
        if not items:
            self.lists.pop(key, None)
        return value

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    async def zrem(self, key, member):
        return int(self.zsets.get(key, {}).pop(member, None) is not None)

    async def zrangebyscore(self, key, low, high, start=0, num=None):
        members = sorted((m for m, s in self.zsets.get(key, {}).items() if low <= s <= high), key=lambda m: self.zsets[key][m])
        return members[start : start + num if num else None]


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()

    async def _redis():
        return fake

    monkeypatch.setattr(state, "_redis", _redis)
    return fake


def _event(
    message_id: str,
    text: str = "hi",
    conversation_id: str = "chat-1",
    *,
    sender: str = "Alice",
    at: datetime = datetime(2026, 11, 3, 9, 0, tzinfo=UTC),
) -> AccountEvent:
    return AccountEvent(
        provider="whatsapp",
        conversation_type="direct",
        conversation_id=conversation_id,
        thread_id=conversation_id,
        message_id=message_id,
        sender_id=sender.lower(),
        sender_name=sender,
        text=text,
        created_at=at,
    )


def _decision(respond: bool = True) -> Decision:
    return SimpleNamespace(respond=respond, reason="addressed" if respond else "not_addressed", grace_period_seconds=0)


async def test_one_holder_at_a_time_and_only_the_holder_releases(redis):
    assert await turns.take("a1", "chat-1", "h1") is True
    assert await turns.take("a1", "chat-1", "h2") is False
    await turns.release("a1", "chat-1", "h2")
    assert await turns.take("a1", "chat-1", "h3") is False
    await turns.release("a1", "chat-1", "h1")
    assert await turns.take("a1", "chat-1", "h3") is True
    # Another conversation of the same account is not held.
    assert await turns.take("a1", "chat-2", "h4") is True


async def test_queue_keeps_order_and_drains_once(redis):
    for message_id in ("m1", "m2", "m3"):
        assert await turns.enqueue("a1", "chat-1", _event(message_id))
    assert await turns.has_queued("a1", "chat-1")
    assert [e.message_id for e in await turns.drain("a1", "chat-1")] == ["m1", "m2", "m3"]
    assert await turns.drain("a1", "chat-1") == []
    assert not await turns.has_queued("a1", "chat-1")
    assert redis.zsets[turns.QUEUES_KEY] == {}


async def test_a_flood_keeps_the_latest_messages(redis):
    for index in range(turns.QUEUE_MAX_MESSAGES + 5):
        await turns.enqueue("a1", "chat-1", _event(f"m{index}"))
    drained = await turns.drain("a1", "chat-1")
    assert len(drained) == turns.QUEUE_MAX_MESSAGES and drained[-1].message_id == f"m{turns.QUEUE_MAX_MESSAGES + 4}"


async def test_without_redis_every_call_fails_open(monkeypatch):
    async def _none():
        return None

    monkeypatch.setattr(state, "_redis", _none)
    assert await turns.take("a1", "chat-1", "h1") is None
    assert await turns.enqueue("a1", "chat-1", _event("m1")) is False
    assert await turns.drain("a1", "chat-1") == []
    assert await turns.orphaned() == []


async def test_an_orphaned_queue_is_found_only_when_nobody_holds_it(redis):
    await turns.enqueue("a1", "chat-1", _event("m1"))
    member = next(iter(redis.zsets[turns.QUEUES_KEY]))
    # Just queued: a live turn may still drain it.
    assert await turns.orphaned() == []
    redis.zsets[turns.QUEUES_KEY][member] = time.time() - turns.ORPHAN_AFTER_SECONDS - 1
    await turns.take("a1", "chat-1", "live")
    assert await turns.orphaned() == []
    await turns.release("a1", "chat-1", "live")
    assert await turns.orphaned() == [("a1", "chat-1")]


@pytest.fixture
def answers(monkeypatch, redis):
    """The pipeline's turn loop with a recorded ``_answer`` and decision log."""
    answered: list[str] = []
    outcomes: list[tuple[str, str]] = []
    absorbed_by: dict[str, list[str]] = {}
    traces: list[tuple[list[str], str]] = []
    gate = asyncio.Event()

    class Session:
        async def __aenter__(self):
            return SimpleNamespace(get=self.get)

        async def __aexit__(self, *exc):
            return False

        async def get(self, model, key):
            return SimpleNamespace(id=key)

    async def _answer(db, account, event, decision, *, after_grace=False, absorbed=None, trace_out=None):
        answered.append(event.message_id)
        absorbed_by[event.message_id] = [m.message_id for m in absorbed or []]
        if trace_out is not None:
            trace_out["trace_id"] = f"trace-{event.message_id}"
        if event.message_id == "m1":
            # While the first turn runs, two more messages arrive.
            gate.set()
            await asyncio.sleep(0.05)
        return True

    async def _turn_trace(account_id, conversation_id, message_ids, trace_id):
        traces.append((message_ids, trace_id))

    async def _record(account_id, event, decision, outcome, **kwargs):
        outcomes.append((event.message_id, outcome))

    async def _decide(account, event, db):
        return _decision()

    async def _allowed(db):
        return None

    from app.services import agentic_gate

    monkeypatch.setattr(pipeline, "async_session", Session)
    monkeypatch.setattr(pipeline, "_answer", _answer)
    monkeypatch.setattr(pipeline, "decide", _decide)
    monkeypatch.setattr(activity, "record_decision", _record)
    monkeypatch.setattr(activity, "record_turn_trace", _turn_trace)
    monkeypatch.setattr(agentic_gate, "agentic_refusal", _allowed)
    monkeypatch.setattr(pipeline.settings, "chat_event_coalesce_window_seconds", 0)
    return SimpleNamespace(
        answered=answered, outcomes=outcomes, gate=gate, absorbed_by=absorbed_by, traces=traces
    )


ACCOUNT = "00000000-0000-0000-0000-000000000001"


async def test_messages_arriving_mid_turn_are_one_next_turn_on_the_latest(answers, redis):
    answered, outcomes, gate = answers.answered, answers.outcomes, answers.gate
    assert await turns.take(ACCOUNT, "chat-1", "h1")
    holder = asyncio.create_task(pipeline.hold_turns(ACCOUNT, "chat-1", "h1", first=_event("m1"), decision=_decision()))
    await gate.wait()
    for message_id in ("m2", "m3"):
        assert await turns.take(ACCOUNT, "chat-1", "late") is False
        await turns.enqueue(ACCOUNT, "chat-1", _event(message_id))
    await holder
    # Two answers, never three: the second turn answers m3 and reads m2 with it.
    assert answered == ["m1", "m3"]
    assert ("m2", pipeline.COALESCED) in outcomes
    # The conversation is free again.
    assert await turns.take(ACCOUNT, "chat-1", "next") is True


async def test_a_queue_left_by_a_dead_process_is_answered_by_the_sweep(answers, redis):
    answered, outcomes = answers.answered, answers.outcomes
    await turns.enqueue(ACCOUNT, "chat-1", _event("m7"))
    await turns.enqueue(ACCOUNT, "chat-1", _event("m8"))
    assert await listening.resume_orphaned_turn(ACCOUNT, "chat-1") is True
    assert answered == ["m8"] and ("m7", pipeline.COALESCED) in outcomes
    assert not await turns.has_queued(ACCOUNT, "chat-1")


async def test_a_queued_message_the_rules_now_leave_is_not_answered(answers, redis, monkeypatch):
    answered, outcomes = answers.answered, answers.outcomes

    async def _decide(account, event, db):
        return _decision(respond=False)

    monkeypatch.setattr(pipeline, "decide", _decide)
    await turns.enqueue(ACCOUNT, "chat-1", _event("m9"))
    assert await listening.resume_orphaned_turn(ACCOUNT, "chat-1") is True
    assert answered == [] and outcomes == [("m9", "not_addressed")]


async def test_a_burst_from_two_senders_is_one_turn_on_the_newest_message_with_both(answers, redis):
    """Two messages 5 s apart from two senders, delivered newest first, are one turn.

    The turn answers the message written last and carries the other one; both
    entries of the activity log name the turn's trace.
    """
    t0 = datetime(2026, 11, 3, 9, 47, 25, tzinfo=UTC)
    first = _event("s1", "could we use xtm-docs?", sender="Stephane", at=t0)
    second = _event("j1", "https://github.com/XTM-Flow/xtm-docs", sender="JP", at=t0 + timedelta(seconds=5))
    # The newer message reaches the queue first (a grace timer, a poll batch).
    await turns.enqueue(ACCOUNT, "chat-1", second)
    await turns.enqueue(ACCOUNT, "chat-1", first)
    assert await listening.resume_orphaned_turn(ACCOUNT, "chat-1") is True
    assert answers.answered == ["j1"]
    assert answers.absorbed_by == {"j1": ["s1"]}
    assert ("s1", pipeline.COALESCED) in answers.outcomes
    assert answers.traces == [(["s1", "j1"], "trace-j1")]


def test_queued_and_coalesced_have_an_activity_group():
    assert activity.group_of("queued") == "waiting"
    assert activity.group_of("coalesced") == "answered"
