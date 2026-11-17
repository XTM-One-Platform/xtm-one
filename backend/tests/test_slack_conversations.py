"""Slack messages join the shared chat turn model.

A Slack delivery carries the ``conversation_key`` of its DM, channel or
thread, and the Slack send tools claim the turn's inbound ids through
``chat_reply_guard`` before posting. Redis is a fake; nothing here reaches
the network.
"""

from __future__ import annotations

import uuid

import pytest

from app.services import chat_reply_guard, chat_turn_coalescing, slack_conversations
from app.services.assignment_dispatch import CONVERSATION_KEY_FIELD

ASSIGNMENT = str(uuid.uuid4())
RUN_A, RUN_B = str(uuid.uuid4()), str(uuid.uuid4())
CHANNEL = "C0123"


class _Redis:
    def __init__(self):
        self.values: dict[str, str] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def expire(self, key, seconds):
        return key in self.values

    async def delete(self, *keys):
        return sum(1 for key in keys if self.values.pop(key, None) is not None)


@pytest.fixture
def redis(monkeypatch):
    store = _Redis()

    async def _client():
        return store

    monkeypatch.setattr(chat_reply_guard, "redis_client", _client)
    return store


def test_a_top_level_message_belongs_to_its_channel_and_a_thread_reply_to_its_thread():
    assert slack_conversations.conversation_key(CHANNEL, "100.1") == "slack-channel:C0123"
    # A thread root carries thread_ts == ts: still the channel's conversation.
    assert slack_conversations.conversation_key(CHANNEL, "100.1", "100.1") == "slack-channel:C0123"
    assert slack_conversations.conversation_key(CHANNEL, "100.2", "100.1") == "slack-thread:C0123:100.1"
    assert slack_conversations.conversation_key("", "100.1") == ""
    assert slack_conversations.thread_conversation_key(CHANNEL, "") == ""


def test_the_payload_is_a_conversation_event_for_the_shared_dispatcher():
    payload = {"message_id": "100.1", CONVERSATION_KEY_FIELD: slack_conversations.conversation_key(CHANNEL, "100.1")}
    assert chat_turn_coalescing.is_conversation_event({"type": "integration_event"}, payload)


def test_a_send_answers_the_channel_and_a_thread_reply_its_thread_or_its_channel():
    assert slack_conversations.reply_keys(CHANNEL) == ["slack-channel:C0123"]
    assert slack_conversations.reply_keys(CHANNEL, "100.1") == ["slack-thread:C0123:100.1", "slack-channel:C0123"]


async def _claim_as(run_id: str, payload: dict, channel: str, thread_ts: str | None = None):
    token = chat_reply_guard.bind_turn(payload, scope=ASSIGNMENT, run_id=run_id)
    try:
        return await slack_conversations.claim_reply(channel, thread_ts)
    finally:
        chat_reply_guard.reset_turn(token)


@pytest.mark.asyncio
async def test_a_second_run_replying_to_an_answered_slack_message_is_refused(redis):
    payload = {"message_id": "100.1", CONVERSATION_KEY_FIELD: "slack-channel:C0123"}
    first = await _claim_as(RUN_A, payload, CHANNEL)
    assert first.claimed == ["100.1"] and not first.refused
    await first.confirm()
    # Answering the top-level message in its thread lands in the same conversation.
    second = await _claim_as(RUN_B, payload, CHANNEL, "100.1")
    assert second.refused
    assert "already answered" in second.refusal()
    assert "edit it with" not in second.refusal()


@pytest.mark.asyncio
async def test_a_thread_turn_claims_on_a_thread_reply_and_never_on_another_channel(redis):
    payload = {"message_id": "100.2", CONVERSATION_KEY_FIELD: "slack-thread:C0123:100.1"}
    elsewhere = await _claim_as(RUN_A, payload, "C9999")
    assert elsewhere.turn is None and not elsewhere.claimed
    in_thread = await _claim_as(RUN_A, payload, CHANNEL, "100.1")
    assert in_thread.claimed == ["100.2"]


@pytest.mark.asyncio
async def test_a_send_outside_a_conversation_run_is_never_guarded(redis):
    result = await slack_conversations.claim_reply(CHANNEL)
    assert result.turn is None and not result.refused
    assert redis.values == {}
