"""An assignment never runs on its own post; everything else triggers as before.

Every Slack and Teams write made inside an assignment run records the message in
``services/agent_posts`` (Redis, TTL) with that assignment; the Slack poller, the Slack
events endpoint (``test_slack_events_dispatch``) and the Teams channel poller skip a
recorded message for THAT assignment only. A post made outside an assignment run (a chat
reply, a channel bot answer) is not recorded, and another assignment still runs on the
post. The store is fail-open both ways.
Redis, Slack, Graph and the channel adapter's HTTP client are replaced by fakes.
"""

from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import agent_posts, event_poller
from app.tools.base import CallableToolExecutor

CHANNEL, AGENT_TS, TYPED_TS = "C123", "1700000000.000100", "1700000000.000200"
TEAM, TEAMS_CHANNEL = "11111111-2222-3333-4444-555555555555", "19:abc@thread.tacv2"


class _Redis:
    def __init__(self, *, lookups_fail: bool = False):
        self.values: dict[str, str] = {}
        self.lookups_fail = lookups_fail

    async def set(self, key, value, ex=None):
        self.values[key] = value

    async def get(self, key):
        if self.lookups_fail and key.startswith("agent_post:"):
            raise ConnectionError("redis down")
        return self.values.get(key)


POSTER = str(uuid.uuid4())


@pytest.fixture
def store(monkeypatch):
    redis = _Redis()
    monkeypatch.setattr(agent_posts, "redis_client", AsyncMock(return_value=redis))
    token = agent_posts.set_posting_assignment(POSTER)
    yield redis
    agent_posts.reset_posting_assignment(token)


class _Response:
    def __init__(self, data: dict, status_code: int = 200):
        self._data = data
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._data


def _handlers(module, token: str | None = None, **kwargs):
    executor = CallableToolExecutor()
    module.build_tools(token or "user-token", executor, **kwargs)
    return executor._handlers


def _slack_answer(monkeypatch, module, data):
    calls: list[str] = []

    async def _safe_request(_client, _method, url, *_a, **_k):
        calls.append(url)
        return _Response(data), None

    monkeypatch.setattr(module, "safe_request", _safe_request)
    return calls


def _recorded(store, provider, *parts) -> bool:
    return store.values.get(agent_posts.agent_post_key(provider, *parts)) == POSTER


# ---------------------------------------------------------------------------
# Every write that posts a message records it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        pytest.param("send_slack_message", {"channel": "#general", "text": "hi"}, id="post"),
        pytest.param("reply_slack_thread", {"channel": CHANNEL, "thread_ts": "1.0", "text": "hi"}, id="thread-reply"),
    ],
)
async def test_a_slack_integration_post_is_recorded(store, monkeypatch, tool, args):
    from app.integrations.providers import slack

    _slack_answer(monkeypatch, slack, {"ok": True, "channel": CHANNEL, "ts": AGENT_TS})
    result = await _handlers(slack)[tool](**args)
    assert AGENT_TS in result
    assert _recorded(store, "slack", CHANNEL, AGENT_TS), "recorded under the resolved channel id"


def _slack_upload(monkeypatch, completed: dict) -> None:
    """``files.getUploadURLExternal``, the byte upload, ``files.completeUploadExternal``."""
    from app.integrations.providers import slack_files

    async def _safe_request(_client, _method, url, *_a, **_k):
        if url.endswith("/files.getUploadURLExternal"):
            return _Response({"ok": True, "upload_url": "https://files.slack.com/upload/v1/ABC", "file_id": "F1"}), None
        if url.endswith("/files.completeUploadExternal"):
            return _Response({"ok": True, "files": [completed]}), None
        return _Response({}), None

    monkeypatch.setattr(slack_files, "safe_request", _safe_request)


@pytest.mark.parametrize(
    ("module", "tool", "args"),
    [
        pytest.param("slack", "upload_slack_file", {"channels": CHANNEL, "content": "x"}, id="integration"),
        pytest.param("slack_bot", "upload_slack_bot_file", {"channel": CHANNEL, "content": "x"}, id="channel-bot"),
    ],
)
async def test_a_file_the_agent_uploads_records_the_message_slack_names(store, monkeypatch, module, tool, args):
    import importlib

    provider = importlib.import_module(f"app.integrations.providers.{module}")
    _slack_upload(monkeypatch, {"id": "F1", "title": "a.txt", "shares": {"public": {CHANNEL: [{"ts": AGENT_TS}]}}})
    result = await _handlers(provider, token="xoxb-token")[tool](**args)
    assert str(result).startswith("Uploaded the content as a file to Slack (ID: F1), shared in ")
    assert _recorded(store, "slack", CHANNEL, AGENT_TS)


async def test_an_upload_whose_share_slack_has_not_named_yet_records_nothing(store, monkeypatch):
    """Slack shares an uploaded file asynchronously: the completion usually names no message yet.
    Nothing is recorded, and the upload still succeeds (a file share carries a subtype, which
    both the poller and the events endpoint skip anyway)."""
    from app.integrations.providers import slack

    _slack_upload(monkeypatch, {"id": "F1", "title": "a.txt"})
    result = await _handlers(slack)["upload_slack_file"](channels=CHANNEL, content="x")
    assert str(result).startswith("Uploaded the content as a file to Slack (ID: F1), shared in ")
    assert store.values == {}


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        pytest.param("send_slack_bot_message", {"channel": CHANNEL, "text": "hi"}, id="post"),
        pytest.param("reply_slack_bot_thread", {"channel": CHANNEL, "thread_ts": "1.0", "text": "hi"}, id="thread-reply"),
    ],
)
async def test_a_channel_bot_post_is_recorded(store, monkeypatch, tool, args):
    from app.integrations.providers import slack_bot

    _slack_answer(monkeypatch, slack_bot, {"ok": True, "channel": CHANNEL, "ts": AGENT_TS})
    await _handlers(slack_bot, token="xoxb-token")[tool](**args)
    assert _recorded(store, "slack", CHANNEL, AGENT_TS)


async def test_the_channel_adapter_reply_is_recorded(store, monkeypatch):
    from app.channels import slack as slack_channel
    from app.channels.base import OutboundMessage

    class _Client:
        def __init__(self, *_a, **_k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, *_a, **_k):
            return _Response({"ok": True, "channel": CHANNEL, "ts": AGENT_TS})

    monkeypatch.setattr(slack_channel.httpx, "AsyncClient", _Client)
    sent = await slack_channel.SlackChannel("xoxb-token").send_message(
        OutboundMessage(text="hello", channel_user_id=CHANNEL)
    )
    assert sent == AGENT_TS
    assert _recorded(store, "slack", CHANNEL, AGENT_TS)


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        pytest.param(
            "send_microsoft_channel_message",
            {"team_id": TEAM, "channel_id": TEAMS_CHANNEL, "message": "hi"},
            id="channel-post",
        ),
    ],
)
async def test_a_teams_channel_post_is_recorded(store, monkeypatch, tool, args):
    from app.integrations.providers import microsoft

    _slack_answer(monkeypatch, microsoft._common, {"id": "1700000000000", "createdDateTime": "2026-09-27T10:00:00Z"})
    result = await _handlers(microsoft, token="graph-token")[tool](**args)
    assert "1700000000000" in result
    assert _recorded(store, "teams", TEAM, TEAMS_CHANNEL, "1700000000000")


async def test_a_store_failure_never_blocks_the_post(monkeypatch):
    from app.integrations.providers import slack

    monkeypatch.setattr(agent_posts, "redis_client", AsyncMock(side_effect=ConnectionError("redis down")))
    _slack_answer(monkeypatch, slack, {"ok": True, "channel": CHANNEL, "ts": AGENT_TS})
    assert "Message sent" in await _handlers(slack)["send_slack_message"](channel=CHANNEL, text="hi")


async def test_a_failed_post_records_nothing(store, monkeypatch):
    from app.integrations.providers import slack

    _slack_answer(monkeypatch, slack, {"ok": False, "error": "channel_not_found"})
    await _handlers(slack)["send_slack_message"](channel=CHANNEL, text="hi")
    assert store.values == {}


# ---------------------------------------------------------------------------
# The pollers skip a recorded message, never a typed one
# ---------------------------------------------------------------------------


class _Http:
    def __init__(self, data: dict):
        self.data = data

    async def get(self, *_a, **_k):
        return _Response(self.data)


async def test_the_slack_poller_skips_its_own_post_and_keeps_the_typed_message(store):
    await agent_posts.record_agent_post("slack", CHANNEL, AGENT_TS)
    history = {
        "ok": True,
        "messages": [
            {"ts": AGENT_TS, "user": "U-a", "text": "posted by the agent as U-a"},
            {"ts": TYPED_TS, "user": "U-a", "text": "typed by U-a"},
        ],
    }
    wf = SimpleNamespace(id=POSTER)
    events = await event_poller._poll_slack_messages("xoxp", [CHANNEL], wf, store, _Http(history))
    assert [e["message_id"] for e in events] == [f"{CHANNEL}:{TYPED_TS}"]


async def test_another_assignment_still_runs_on_the_post(store):
    """A digest posted into a channel another assignment watches keeps triggering it, as on main."""
    await agent_posts.record_agent_post("slack", CHANNEL, AGENT_TS)
    history = {"ok": True, "messages": [{"ts": AGENT_TS, "user": "U-a", "text": "the weekly digest"}]}
    watcher = SimpleNamespace(id=uuid.uuid4())
    events = await event_poller._poll_slack_messages("xoxp", [CHANNEL], watcher, store, _Http(history))
    assert [e["message_id"] for e in events] == [f"{CHANNEL}:{AGENT_TS}"]


async def test_a_post_made_outside_an_assignment_run_is_not_recorded(monkeypatch):
    """A chat reply or a channel bot answer triggers every assignment, as on main."""
    from app.integrations.providers import slack

    redis = _Redis()
    monkeypatch.setattr(agent_posts, "redis_client", AsyncMock(return_value=redis))
    token = agent_posts.set_posting_assignment(None)
    try:
        _slack_answer(monkeypatch, slack, {"ok": True, "channel": CHANNEL, "ts": AGENT_TS})
        await _handlers(slack)["send_slack_message"](channel=CHANNEL, text="hi")
    finally:
        agent_posts.reset_posting_assignment(token)
    assert redis.values == {}


async def test_the_teams_poller_skips_its_own_post_only(store):
    await agent_posts.record_agent_post("teams", TEAM, TEAMS_CHANNEL, "m-agent")
    listing = {
        "value": [
            {"id": "m-agent", "messageType": "message", "createdDateTime": "2026-09-27T10:00:00Z", "body": {"content": "a"}},
            {"id": "m-typed", "messageType": "message", "createdDateTime": "2026-09-27T10:01:00Z", "body": {"content": "b"}},
        ]
    }
    channels = [f"{TEAM}:{TEAMS_CHANNEL}"]
    own = await event_poller._poll_microsoft_teams("tok", channels, SimpleNamespace(id=POSTER), store, _Http(listing))
    assert [e["message_id"] for e in own] == ["m-typed"]
    other = await event_poller._poll_microsoft_teams(
        "tok", channels, SimpleNamespace(id=uuid.uuid4()), store, _Http(listing)
    )
    assert [e["message_id"] for e in other] == ["m-agent", "m-typed"]


async def test_a_record_from_before_the_poster_was_kept_triggers_as_before(store):
    """A value that names no assignment (the earlier "1") never skips anything."""
    store.values[agent_posts.agent_post_key("slack", CHANNEL, AGENT_TS)] = "1"
    assert await agent_posts.is_agent_slack_post(store, CHANNEL, AGENT_TS, POSTER) is False


async def test_an_unreadable_store_lets_every_message_through():
    redis = _Redis(lookups_fail=True)
    redis.values[agent_posts.agent_post_key("slack", CHANNEL, AGENT_TS)] = POSTER
    history = {"ok": True, "messages": [{"ts": AGENT_TS, "user": "U-a", "text": "x"}]}
    wf = SimpleNamespace(id=POSTER)
    events = await event_poller._poll_slack_messages("xoxp", [CHANNEL], wf, redis, _Http(history))
    assert len(events) == 1
    assert await agent_posts.is_agent_slack_post(None, CHANNEL, AGENT_TS, POSTER) is False


async def test_the_orchestration_adapter_leaves_no_poster_bound_after_a_run(monkeypatch):
    """A worker running assignments back to back never attributes a later post to an earlier run."""
    from app.services import assignment_engine
    from app.services.orchestration_adapters import assignment as adapter

    seen: list[str | None] = []

    async def _run(run_id, **_kwargs):
        agent_posts.set_posting_assignment(POSTER)
        seen.append(agent_posts.posting_assignment())
        return None

    async def _nothing(*_a, **_k):
        return None

    monkeypatch.setattr(assignment_engine, "execute_assignment_run", _run)
    monkeypatch.setattr(adapter, "_bind_memory_chain", _nothing)
    monkeypatch.setattr("app.services.ai_hold.close_hold_if_ai_is_back", _nothing)
    execution = SimpleNamespace(payload={"run_id": str(uuid.uuid4())})
    # What the adapter does with the outcome afterwards needs a database: only the binding matters here.
    with contextlib.suppress(Exception):
        # The real AdapterContext always carries cancel_event (None by default),
        # which the adapter forwards to the engine.
        await adapter.AssignmentAdapter().execute(SimpleNamespace(db=None, cancel_event=None), execution)
    assert seen == [POSTER]
    assert agent_posts.posting_assignment() is None


def test_both_slack_paths_use_one_lookup():
    from app.services import slack_events

    assert slack_events.is_agent_slack_post is agent_posts.is_agent_slack_post
    assert event_poller.is_agent_slack_post is agent_posts.is_agent_slack_post
