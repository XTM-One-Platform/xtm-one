"""One reply per inbound chat message, at the send.

A run started by a chat message is bound to the inbound ids it answers; its
reply in that conversation claims them, and a second run's reply to ids
already answered is refused, logged and counted. Redis is a fake; nothing
here reaches the network.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.services import chat_reply_guard
from app.services.teams_chat_posts import chat_conversation_key, thread_conversation_key

ASSIGNMENT = str(uuid.uuid4())
RUN_A, RUN_B = str(uuid.uuid4()), str(uuid.uuid4())
CHAT = "19:abc@thread.v2"
KEY = chat_conversation_key(CHAT)


class _Redis:
    def __init__(self, *, fail: bool = False):
        self.values: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.fail = fail

    def _check(self):
        if self.fail:
            raise ConnectionError("redis down")

    async def set(self, key, value, ex=None, nx=False):
        self._check()
        if nx and key in self.values:
            return None
        self.values[key] = value
        self.ttl[key] = ex
        return True

    async def get(self, key):
        self._check()
        return self.values.get(key)

    async def expire(self, key, seconds):
        self._check()
        if key in self.values:
            self.ttl[key] = seconds
            return True
        return False

    async def delete(self, *keys):
        self._check()
        return sum(1 for key in keys if self.values.pop(key, None) is not None)


@pytest.fixture
def redis(monkeypatch):
    store = _Redis()

    async def _client():
        return store

    monkeypatch.setattr(chat_reply_guard, "redis_client", _client)
    return store


def _payload(*ids: str, conversation: str = KEY) -> dict:
    payload: dict = {"message_id": ids[0], "conversation_key": conversation, "text": "hi"}
    if len(ids) > 1:
        payload["message_ids"] = list(ids)
    return payload


def _refused_count() -> float:
    return chat_reply_guard.duplicate_replies_refused.labels(channel="teams")._value.get()


async def _claim_as(run_id: str, payload: dict, conversation: str = KEY) -> chat_reply_guard.Claim:
    token = chat_reply_guard.bind_turn(payload, scope=ASSIGNMENT, run_id=run_id)
    try:
        return await chat_reply_guard.claim("teams", conversation)
    finally:
        chat_reply_guard.reset_turn(token)


def test_turn_of_reads_the_merged_ids_in_order_and_skips_non_conversation_payloads():
    turn = chat_reply_guard.turn_of(_payload("m1", "m2", "m1"), scope=ASSIGNMENT, run_id=RUN_A)
    assert turn is not None and turn.inbound_ids == ("m1", "m2")
    assert turn.conversation_key == KEY
    assert chat_reply_guard.turn_of({"message_id": "m1"}, scope=ASSIGNMENT, run_id=RUN_A) is None
    assert chat_reply_guard.turn_of({"conversation_key": KEY}, scope=ASSIGNMENT, run_id=RUN_A) is None
    assert chat_reply_guard.turn_of(_payload("m1"), scope=None, run_id=RUN_A) is None
    assert chat_reply_guard.turn_of("not a dict", scope=ASSIGNMENT, run_id=RUN_A) is None


@pytest.mark.asyncio
async def test_a_second_run_answering_the_same_inbound_message_is_refused_logged_and_counted(redis, caplog):
    first = await _claim_as(RUN_A, _payload("m1"))
    assert first.claimed == ["m1"] and not first.refused
    await first.confirm()
    assert redis.ttl[chat_reply_guard.claim_key(ASSIGNMENT, "m1")] == chat_reply_guard.CLAIM_TTL_SECONDS

    before = _refused_count()
    second = await _claim_as(RUN_B, _payload("m1"))
    assert second.refused
    assert second.answered_elsewhere["m1"]["run"] == RUN_A
    assert _refused_count() == before + 1
    assert "m1" in caplog.text and RUN_A in caplog.text
    refusal = second.refusal(edit_tool="update_microsoft_chat_message")
    assert "m1" in refusal and RUN_A in refusal and "update_microsoft_chat_message" in refusal


@pytest.mark.asyncio
async def test_the_same_run_may_post_again_in_its_conversation(redis):
    first = await _claim_as(RUN_A, _payload("m1"))
    await first.confirm()
    again = await _claim_as(RUN_A, _payload("m1"))
    assert not again.refused and again.claimed == [] and again.answered_elsewhere == {}


@pytest.mark.asyncio
async def test_a_turn_with_one_new_message_goes_out_and_claims_only_the_new_one(redis):
    await (await _claim_as(RUN_A, _payload("m1"))).confirm()
    merged = await _claim_as(RUN_B, _payload("m1", "m2"))
    assert not merged.refused
    assert merged.claimed == ["m2"]
    assert set(merged.answered_elsewhere) == {"m1"}
    assert chat_reply_guard.answered_note(merged) == " Answers inbound message(s): m2."


@pytest.mark.asyncio
async def test_a_send_into_another_conversation_or_outside_a_run_is_never_claimed(redis):
    await (await _claim_as(RUN_A, _payload("m1"))).confirm()
    elsewhere = await _claim_as(RUN_B, _payload("m1"), conversation=chat_conversation_key("19:other@thread.v2"))
    assert not elsewhere.refused and elsewhere.turn is None
    unbound = await chat_reply_guard.claim("teams", KEY)
    assert not unbound.refused and unbound.turn is None


@pytest.mark.asyncio
async def test_a_failed_send_gives_its_claim_back(redis):
    first = await _claim_as(RUN_A, _payload("m1"))
    assert redis.ttl[chat_reply_guard.claim_key(ASSIGNMENT, "m1")] == chat_reply_guard.PENDING_TTL_SECONDS
    await first.release()
    assert chat_reply_guard.claim_key(ASSIGNMENT, "m1") not in redis.values
    retry = await _claim_as(RUN_B, _payload("m1"))
    assert retry.claimed == ["m1"] and not retry.refused


@pytest.mark.asyncio
async def test_an_unreachable_store_never_blocks_a_reply(monkeypatch):
    store = _Redis(fail=True)

    async def _client():
        return store

    monkeypatch.setattr(chat_reply_guard, "redis_client", _client)
    result = await _claim_as(RUN_B, _payload("m1"))
    assert not result.refused and result.claimed == []


@pytest.mark.asyncio
async def test_an_unreadable_holder_record_is_not_a_refusal(redis):
    redis.values[chat_reply_guard.claim_key(ASSIGNMENT, "m1")] = "not json"
    result = await _claim_as(RUN_B, _payload("m1"))
    assert not result.refused


@pytest.mark.asyncio
async def test_claims_are_scoped_to_the_assignment(redis):
    await (await _claim_as(RUN_A, _payload("m1"))).confirm()
    token = chat_reply_guard.bind_turn(_payload("m1"), scope=str(uuid.uuid4()), run_id=RUN_B)
    try:
        other = await chat_reply_guard.claim("teams", KEY)
    finally:
        chat_reply_guard.reset_turn(token)
    assert other.claimed == ["m1"] and not other.refused


def test_teams_conversation_keys_match_the_payload_builders():
    from app.services.event_poller import teams_chat_payload, teams_reply_payload

    chat = teams_chat_payload({"id": "m1", "body": {"content": "x"}}, chat_id=CHAT)
    assert chat["conversation_key"] == KEY
    reply = teams_reply_payload(
        {"id": "r1", "body": {"content": "x"}}, team_id="t", channel_id="c", channel_name="n", parent_id="root"
    )
    assert reply["conversation_key"] == thread_conversation_key("t", "c", "root")
    assert chat_conversation_key("") == "" and thread_conversation_key("t", "c", "") == ""


def test_the_holder_record_names_the_run_and_the_conversation():
    turn = chat_reply_guard.turn_of(_payload("m1"), scope=ASSIGNMENT, run_id=RUN_A)
    assert turn is not None
    record = json.loads(chat_reply_guard._record(turn, 12.0))
    assert record == {"run": RUN_A, "conversation": KEY, "at": 12.0}
