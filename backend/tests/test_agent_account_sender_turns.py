"""One turn at a time per sender of an agent, across its accounts.

A person who writes to the same agent on a chat account and, seconds later, on
a mailbox account used to start two turns side by side: one of them acted
before the other had read the correction. The pipeline now holds the sender:
the second message is steered into the turn in flight, its own turn runs after
it, and every turn reads what the sender wrote on the agent's other channels.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.services.agent_accounts import sender_turns, state
from app.services.agent_accounts.events import AccountEvent
from app.tools.agent_loop import SteeringMessage, drain_steering_queue

AGENT = "agent-1"
CHAT_ACCOUNT = "chat-account"
MAIL_ACCOUNT = "mail-account"
NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)


class FakeRedis:
    """The calls ``sender_turns`` makes, in memory."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}

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

    async def expire(self, key, seconds):
        return True

    async def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    async def ltrim(self, key, start, end):
        items = self.lists.get(key, [])
        self.lists[key] = items[start:] if end == -1 else items[start : end + 1]

    async def lrange(self, key, start, end):
        items = self.lists.get(key, [])
        return items[start:] if end == -1 else items[start : end + 1]

    async def lpop(self, key):
        items = self.lists.get(key)
        if not items:
            return None
        return items.pop(0)


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()

    async def _redis():
        return fake

    monkeypatch.setattr(state, "_redis", _redis)
    monkeypatch.setattr(sender_turns, "POLL_SECONDS", 0.01)
    return fake


def _event(
    message_id: str,
    text: str,
    *,
    provider: str,
    conversation_id: str,
    email: str | None = "person@example.com",
    at: datetime = NOW,
) -> AccountEvent:
    return AccountEvent(
        provider=provider,
        conversation_type="direct",
        conversation_id=conversation_id,
        thread_id=conversation_id,
        message_id=message_id,
        sender_id="person",
        sender_name="Person",
        text=text,
        created_at=at,
        sender_email=email,
    )


def _chat(message_id: str = "c1", text: str = "find a partner for the pilot") -> AccountEvent:
    return _event(message_id, text, provider="microsoft_teams", conversation_id="chat-1")


def _mail(message_id: str = "e1", text: str = "ask for an intro, do not write to them cold") -> AccountEvent:
    # A forwarded email: the same person, by the same address in another case.
    return _event(
        message_id, text, provider="microsoft_outlook", conversation_id="mail-thread-9", email="Person@Example.com"
    )


def test_the_sender_is_the_primary_address_whatever_its_case():
    assert sender_turns.sender_key(_chat()) == sender_turns.sender_key(_mail()) == "person@example.com"
    assert sender_turns.sender_key(_event("x", "t", provider="p", conversation_id="c", email=None)) is None


async def test_a_message_on_another_channel_steers_the_turn_in_flight_then_runs_after_it(redis):
    """The chat + mailbox pair: one turn reads both, the second turn never runs beside it."""
    order: list[str] = []
    chat_started = asyncio.Event()
    release_chat = asyncio.Event()
    steered: list[str] = []

    async def chat_turn():
        async with sender_turns.hold(AGENT, CHAT_ACCOUNT, _chat(), label="Teams") as queue:
            assert queue is not None and sender_turns.current_queue() is queue
            order.append("chat:start")
            chat_started.set()
            await release_chat.wait()
            # The loop's next iteration boundary: what was steered meanwhile.
            await asyncio.sleep(0.05)
            for item in drain_steering_queue(queue):
                steered.append(item.text if isinstance(item, SteeringMessage) else item)
            order.append("chat:send")

    async def mail_turn():
        await chat_started.wait()
        waited: list[bool] = []

        async def before_wait():
            waited.append(True)

        async with sender_turns.hold(AGENT, MAIL_ACCOUNT, _mail(), label="Outlook", before_wait=before_wait) as queue:
            assert queue is not None
            order.append("mail:start")
        assert waited == [True]

    chat = asyncio.create_task(chat_turn())
    mail = asyncio.create_task(mail_turn())
    await chat_started.wait()
    await asyncio.sleep(0.05)
    assert "mail:start" not in order
    release_chat.set()
    await asyncio.wait_for(asyncio.gather(chat, mail), timeout=5)

    # The chat turn's send happens after it read the email, and the mail turn after the send.
    assert order == ["chat:start", "chat:send", "mail:start"]
    assert len(steered) == 1
    assert "on Outlook" in steered[0] and "do not write to them cold" in steered[0]
    assert "before any further action" in steered[0]
    # Released: nothing held, no inbox left behind.
    assert not [k for k in redis.values if "sender_turn" in k]
    assert not [k for k, v in redis.lists.items() if "sender_turn_inbox" in k and v]


async def test_another_sender_is_not_held(redis):
    entered = asyncio.Event()
    leave = asyncio.Event()

    async def first():
        async with sender_turns.hold(AGENT, CHAT_ACCOUNT, _chat(), label="Teams"):
            entered.set()
            await leave.wait()

    task = asyncio.create_task(first())
    await entered.wait()
    other = _event("o1", "hello", provider="microsoft_outlook", conversation_id="t2", email="other@example.com")
    async with sender_turns.hold(AGENT, MAIL_ACCOUNT, other, label="Outlook") as queue:
        assert queue is not None
    # Another agent of the same sender is not held either.
    async with sender_turns.hold("agent-2", MAIL_ACCOUNT, _mail(), label="Outlook") as queue:
        assert queue is not None
    leave.set()
    await task


async def test_without_an_address_or_redis_the_turn_runs_unserialized(redis, monkeypatch):
    anonymous = _event("a1", "hi", provider="whatsapp", conversation_id="c", email=None)
    async with sender_turns.hold(AGENT, CHAT_ACCOUNT, anonymous, label="WhatsApp") as queue:
        assert queue is None

    async def _none():
        return None

    monkeypatch.setattr(state, "_redis", _none)
    async with sender_turns.hold(AGENT, CHAT_ACCOUNT, _chat(), label="Teams") as queue:
        assert queue is None
    await sender_turns.note(AGENT, CHAT_ACCOUNT, _chat(), label="Teams")
    assert await sender_turns.recent_elsewhere(AGENT, MAIL_ACCOUNT, _mail()) == []


async def test_a_turn_in_flight_past_the_wait_bound_lets_the_message_run(redis, monkeypatch):
    monkeypatch.setattr(sender_turns, "WAIT_SECONDS", 0.05)
    entered = asyncio.Event()
    leave = asyncio.Event()

    async def first():
        async with sender_turns.hold(AGENT, CHAT_ACCOUNT, _chat(), label="Teams"):
            entered.set()
            await leave.wait()

    task = asyncio.create_task(first())
    await entered.wait()
    async with sender_turns.hold(AGENT, MAIL_ACCOUNT, _mail(), label="Outlook") as queue:
        assert queue is None
    leave.set()
    await task


async def test_each_turn_reads_the_sender_on_the_other_channels(redis):
    await sender_turns.note(AGENT, CHAT_ACCOUNT, _chat(), label="Teams", now=NOW)
    await sender_turns.note(
        AGENT, CHAT_ACCOUNT, _chat(), label="Teams", direction="out", text="On it.", status="sent", now=NOW
    )
    await sender_turns.note(AGENT, MAIL_ACCOUNT, _mail(), label="Outlook", now=NOW + timedelta(seconds=30))

    seen_by_mail = await sender_turns.recent_elsewhere(AGENT, MAIL_ACCOUNT, _mail(), now=NOW + timedelta(minutes=1))
    assert [(e["label"], e["direction"]) for e in seen_by_mail] == [("Teams", "in"), ("Teams", "out")]
    seen_by_chat = await sender_turns.recent_elsewhere(AGENT, CHAT_ACCOUNT, _chat("c2"), now=NOW + timedelta(minutes=1))
    assert [e["message_id"] for e in seen_by_chat] == ["e1"]

    text = sender_turns.with_recent("Person: ask for an intro", seen_by_mail, "Person")
    assert text.startswith("Person: ask for an intro")
    assert "[2026-11-03 09:00Z, Teams] Person: find a partner for the pilot" in text
    assert "you answered (sent): On it." in text
    assert sender_turns.with_recent("unchanged", [], "Person") == "unchanged"

    # Past the window, a resumed turn no longer reads them.
    later = NOW + timedelta(seconds=sender_turns.RECENT_WINDOW_SECONDS + 60)
    assert await sender_turns.recent_elsewhere(AGENT, MAIL_ACCOUNT, _mail(), now=later) == []
