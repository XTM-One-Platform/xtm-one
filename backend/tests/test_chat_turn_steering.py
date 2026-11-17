"""A chat message arriving mid-run steers the running turn.

The running turn of a conversation event opens a steering session; a later
message of the same conversation and sender is offered to it, injected at the
loop's next iteration as a user steer, claimed by the turn's reply, and handed
back as the next turn when the loop never took it. Redis is a fake; nothing
here reaches the network.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from app.services import chat_reply_guard, chat_turn_steering
from app.tools.agent_loop import SteeringMessage, drain_steering_queue

ASSIGNMENT = str(uuid.uuid4())
RUN = str(uuid.uuid4())
KEY = "teams:chat:19:abc@thread.v2"


class _Redis:
    def __init__(self, *, fail: bool = False):
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.fail = fail

    def _check(self):
        if self.fail:
            raise ConnectionError("redis down")

    async def set(self, key, value, ex=None, nx=False):
        self._check()
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def get(self, key):
        self._check()
        return self.values.get(key)

    async def delete(self, *keys):
        self._check()
        return sum(1 for key in keys if self.values.pop(key, None) is not None or self.lists.pop(key, None) is not None)

    async def expire(self, key, seconds):
        self._check()
        return True

    async def rpush(self, key, value):
        self._check()
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    async def lpop(self, key):
        self._check()
        items = self.lists.get(key) or []
        return items.pop(0) if items else None

    async def lrem(self, key, count, value):
        self._check()
        items = self.lists.get(key) or []
        if value in items:
            items.remove(value)
            return 1
        return 0


@pytest.fixture
def redis(monkeypatch):
    store = _Redis()

    async def _client():
        return store

    monkeypatch.setattr(chat_turn_steering, "redis_client", _client)
    monkeypatch.setattr(chat_reply_guard, "redis_client", _client)
    monkeypatch.setattr(chat_turn_steering, "POLL_SECONDS", 0.01)
    return store


def _message(message_id: str, *, sender: str = "u1", text: str = "hi") -> dict:
    return {
        "message_id": message_id,
        "conversation_key": KEY,
        "from_id": sender,
        "from_name": "Ada",
        "text": text,
    }


class _Leftovers:
    def __init__(self):
        self.entries: list[dict] = []

    async def __call__(self, entries):
        self.entries.extend(entries)


def test_steering_is_on_by_default_and_off_only_when_the_assignment_says_false():
    assert chat_turn_steering.enabled_for(None)
    assert chat_turn_steering.enabled_for({})
    assert chat_turn_steering.enabled_for({"steer_mid_run": True})
    assert not chat_turn_steering.enabled_for({"steer_mid_run": False})


@pytest.mark.asyncio
async def test_no_live_run_offers_nothing(redis):
    assert await chat_turn_steering.offer(ASSIGNMENT, KEY, _message("m2"), {}) is None
    assert not redis.lists


@pytest.mark.asyncio
async def test_a_message_of_the_same_sender_mid_run_is_injected_and_claimed_by_the_turn_reply(redis):
    token = chat_reply_guard.bind_turn(_message("m1"), scope=ASSIGNMENT, run_id=RUN)
    leftovers = _Leftovers()
    try:
        async with chat_turn_steering.session(_message("m1"), {}, leftovers) as queue:
            assert queue is not None
            assert await chat_turn_steering.offer(ASSIGNMENT, KEY, _message("m2", text="also this"), {"type": "integration_event"}) == RUN
            for _ in range(100):
                if not queue.empty():
                    break
                await asyncio.sleep(0.01)
            items = drain_steering_queue(queue)
            items = await queue.admit(items)
            assert len(items) == 1 and isinstance(items[0], SteeringMessage)
            assert "also this" in items[0].text and "Ada" in items[0].text
            assert chat_reply_guard.current_turn().inbound_ids == ("m1", "m2")
            claim = await chat_reply_guard.claim("teams", KEY)
            assert claim.claimed == ["m1", "m2"]
        assert leftovers.entries == []
        assert chat_turn_steering.live_key(ASSIGNMENT, KEY) not in redis.values
    finally:
        chat_reply_guard.reset_turn(token)


@pytest.mark.asyncio
async def test_another_sender_is_not_steered(redis):
    token = chat_reply_guard.bind_turn(_message("m1"), scope=ASSIGNMENT, run_id=RUN)
    try:
        async with chat_turn_steering.session(_message("m1"), {}, _Leftovers()) as queue:
            assert queue is not None
            assert await chat_turn_steering.offer(ASSIGNMENT, KEY, _message("m2", sender="u2"), {}) is None
    finally:
        chat_reply_guard.reset_turn(token)


@pytest.mark.asyncio
async def test_a_message_the_loop_never_took_is_handed_back_as_the_next_turn(redis):
    token = chat_reply_guard.bind_turn(_message("m1"), scope=ASSIGNMENT, run_id=RUN)
    leftovers = _Leftovers()
    try:
        async with chat_turn_steering.session(_message("m1"), {}, leftovers) as queue:
            assert queue is not None
            assert await chat_turn_steering.offer(ASSIGNMENT, KEY, _message("m3"), {"type": "integration_event"}) == RUN
            # The loop ends before its next iteration boundary: m3 is never taken.
        assert [e["payload"]["message_id"] for e in leftovers.entries] == ["m3"]
        assert leftovers.entries[0]["trigger_context"] == {"type": "integration_event"}
        assert chat_reply_guard.current_turn().inbound_ids == ("m1",)
    finally:
        chat_reply_guard.reset_turn(token)


@pytest.mark.asyncio
async def test_an_offer_racing_the_close_is_taken_back(redis):
    live = chat_turn_steering.live_key(ASSIGNMENT, KEY)
    redis.values[live] = json.dumps({"run": RUN, "sender": "u1"})
    original_rpush = redis.rpush

    async def _rpush_then_close(key, value):
        result = await original_rpush(key, value)
        redis.values.pop(live, None)
        return result

    redis.rpush = _rpush_then_close
    assert await chat_turn_steering.offer(ASSIGNMENT, KEY, _message("m2"), {}) is None
    assert redis.lists[chat_turn_steering.inbox_key(ASSIGNMENT, KEY)] == []


@pytest.mark.asyncio
async def test_a_duplicate_delivery_of_a_message_the_turn_carries_is_not_injected(redis):
    token = chat_reply_guard.bind_turn(_message("m1"), scope=ASSIGNMENT, run_id=RUN)
    leftovers = _Leftovers()
    try:
        async with chat_turn_steering.session(_message("m1"), {}, leftovers) as queue:
            assert await chat_turn_steering.offer(ASSIGNMENT, KEY, _message("m1"), {}) == RUN
            await asyncio.sleep(0.05)
            assert drain_steering_queue(queue) == []
        assert leftovers.entries == []
    finally:
        chat_reply_guard.reset_turn(token)


@pytest.mark.asyncio
async def test_no_session_outside_a_conversation_turn_when_disabled_or_when_the_store_is_down(redis, monkeypatch):
    async with chat_turn_steering.session({"text": "x"}, {}, _Leftovers()) as queue:
        assert queue is None
    token = chat_reply_guard.bind_turn(_message("m1"), scope=ASSIGNMENT, run_id=RUN)
    try:
        async with chat_turn_steering.session(_message("m1"), {"steer_mid_run": False}, _Leftovers()) as queue:
            assert queue is None
        redis.fail = True
        async with chat_turn_steering.session(_message("m1"), {}, _Leftovers()) as queue:
            assert queue is None
        assert await chat_turn_steering.offer(ASSIGNMENT, KEY, _message("m2"), {}) is None
    finally:
        chat_reply_guard.reset_turn(token)


def test_extend_turn_adds_the_steered_ids_once_and_only_to_a_bound_turn():
    assert chat_reply_guard.extend_turn(["m2"]) is None
    token = chat_reply_guard.bind_turn(_message("m1"), scope=ASSIGNMENT, run_id=RUN)
    try:
        chat_reply_guard.extend_turn(["m2", "m1", "m2"])
        assert chat_reply_guard.current_turn().inbound_ids == ("m1", "m2")
    finally:
        chat_reply_guard.reset_turn(token)
