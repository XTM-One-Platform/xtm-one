"""A Teams channel bot's own top-level post never re-triggers the Teams poller.

``send_teams_bot_channel_message`` / ``send_teams_bot_channel_card`` without a
``reply_to_id`` create a top-level channel message through Bot Framework; its
``activityId`` is the Graph message id the ``new_teams_message`` poller lists it by.
Both record it in ``services/agent_posts`` for the assignment whose
run posted it, and the poller skips it for that assignment only. Replies are not
polled and are not recorded; a message a person typed, and the bot's post read by
another assignment, still trigger. Redis, Bot Framework and Graph are replaced by fakes.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.integrations.providers import teams_bot
from app.services import agent_posts, event_poller
from app.tools.base import CallableToolExecutor

TEAM, CHANNEL = "11111111-2222-3333-4444-555555555555", "19:abc@thread.tacv2"
POSTING, OTHER = str(uuid.uuid4()), str(uuid.uuid4())
CARD = {"type": "AdaptiveCard", "version": "1.5", "body": [{"type": "TextBlock", "text": "hi"}]}


class _Redis:
    def __init__(self):
        self.values: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.values[key] = value

    async def get(self, key):
        return self.values.get(key)

    async def exists(self, key):
        return int(key in self.values)


@pytest.fixture
def store(monkeypatch):
    redis = _Redis()
    monkeypatch.setattr(agent_posts, "redis_client", AsyncMock(return_value=redis))
    token = agent_posts.set_posting_assignment(POSTING)
    yield redis
    agent_posts.reset_posting_assignment(token)


def _send(answer: dict | None, err: str | None = None):
    calls: list[str] = []

    async def _safe_request(_client, _method, url, *_a, **_k):
        calls.append(url)
        return (SimpleNamespace(json=lambda: answer) if answer is not None else None), err

    executor = CallableToolExecutor()
    teams_bot.build_tools("tenant-123||client-abc||secret-xyz", executor)
    patches = (
        patch.object(teams_bot, "_acquire_token", new=AsyncMock(return_value="bf-token")),
        patch.object(teams_bot, "_get_service_url", new=AsyncMock(return_value="https://smba.example")),
        patch.object(teams_bot, "safe_request", new=_safe_request),
    )
    return executor._handlers, patches, calls


def _recorded(store, message_id: str) -> bool:
    return agent_posts.agent_post_key("teams", TEAM, CHANNEL, message_id) in store.values


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        pytest.param("send_teams_bot_channel_message", {"message": "hello"}, id="message"),
        pytest.param("send_teams_bot_channel_card", {"card": CARD, "fallback_text": "hi"}, id="card"),
    ],
)
async def test_a_top_level_bot_post_is_recorded(store, tool, args):
    handlers, patches, _ = _send({"activityId": "1700000000000", "id": "a:conv"})
    with patches[0], patches[1], patches[2]:
        result = await handlers[tool](team_id=TEAM, channel_id=CHANNEL, **args)
    assert "1700000000000" in result
    assert _recorded(store, "1700000000000")


@pytest.mark.parametrize("tool", ["send_teams_bot_channel_message", "send_teams_bot_channel_card"])
async def test_a_failed_bot_post_records_nothing(store, tool):
    handlers, patches, _ = _send(None, "Error: 403")
    args = {"message": "hello"} if tool.endswith("message") else {"card": CARD}
    with patches[0], patches[1], patches[2]:
        await handlers[tool](team_id=TEAM, channel_id=CHANNEL, **args)
    assert store.values == {}


async def test_a_reply_is_not_recorded_the_poller_never_lists_it(store):
    handlers, patches, _ = _send({"id": "reply-1"})
    with patches[0], patches[1], patches[2]:
        await handlers["send_teams_bot_channel_message"](
            team_id=TEAM, channel_id=CHANNEL, message="hi", reply_to_id="1700000000000"
        )
    assert store.values == {}


async def test_a_store_failure_never_blocks_the_bot_post(monkeypatch):
    monkeypatch.setattr(agent_posts, "redis_client", AsyncMock(side_effect=ConnectionError("down")))
    handlers, patches, _ = _send({"activityId": "1700000000000"})
    with patches[0], patches[1], patches[2]:
        result = await handlers["send_teams_bot_channel_message"](team_id=TEAM, channel_id=CHANNEL, message="hi")
    assert "1700000000000" in result


class _Http:
    def __init__(self, data: dict):
        self.data = data

    async def get(self, *_a, **_k):
        return SimpleNamespace(status_code=200, json=lambda: self.data, text="", headers={})


async def test_the_poller_skips_the_bot_post_and_keeps_the_typed_message(store):
    handlers, patches, _ = _send({"activityId": "1700000000000"})
    with patches[0], patches[1], patches[2]:
        await handlers["send_teams_bot_channel_message"](team_id=TEAM, channel_id=CHANNEL, message="digest")
    listing = {
        "value": [
            {"id": "1700000000000", "messageType": "message", "createdDateTime": "2026-09-27T10:00:00Z",
             "from": {"application": {"id": "client-abc"}}, "body": {"content": "digest"}},
            {"id": "1700000000999", "messageType": "message", "createdDateTime": "2026-09-27T10:01:00Z",
             "from": {"user": {"id": "u-1"}}, "body": {"content": "typed"}},
        ]
    }
    wf = SimpleNamespace(id=POSTING)
    events = await event_poller._poll_microsoft_teams("tok", [f"{TEAM}:{CHANNEL}"], wf, store, _Http(listing))
    assert [e["message_id"] for e in events] == ["1700000000999"]
    other = SimpleNamespace(id=OTHER)
    events = await event_poller._poll_microsoft_teams("tok", [f"{TEAM}:{CHANNEL}"], other, store, _Http(listing))
    assert [e["message_id"] for e in events] == ["1700000000000", "1700000000999"], "another assignment still runs on it"


async def test_a_bot_post_outside_an_assignment_run_is_not_recorded(store):
    token = agent_posts.set_posting_assignment(None)
    try:
        handlers, patches, _ = _send({"activityId": "1700000000000"})
        with patches[0], patches[1], patches[2]:
            await handlers["send_teams_bot_channel_message"](team_id=TEAM, channel_id=CHANNEL, message="chat answer")
    finally:
        agent_posts.reset_posting_assignment(token)
    assert store.values == {}
