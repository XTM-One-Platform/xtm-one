"""A mail an agent sent or drafted never re-triggers the Microsoft ``new_email`` poller.

``/me/messages`` spans every folder, so the agent's own draft (Drafts) and its sent
copy (Sent Items) read as new mail. Drafts are recorded by their Graph id, sends by
the mailbox, subject, To / Cc and send time (``sendMail`` returns no id), each for the
assignment whose run made it, and the poller skips a recorded message for that
assignment only. A mail a person wrote, a mail sent outside an
assignment run, the same mail arriving in another mailbox, and the agent's mail read
by another assignment all still trigger. Redis and Graph are replaced by fakes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import agent_posts, event_poller, mail_send_guard
from app.tools.base import CallableToolExecutor

MAILBOX, OTHER_MAILBOX = str(uuid.uuid4()), str(uuid.uuid4())
POSTING, OTHER = str(uuid.uuid4()), str(uuid.uuid4())
NOW = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)
DRAFT_ID = "AAMkAGI2" + "A" * 140


class _Redis:
    def __init__(self, *, fail: bool = False):
        self.values: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}
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
        return self.values.get(key)

    async def delete(self, *keys):
        self._check()
        return sum(1 for key in keys if self.values.pop(key, None) is not None)

    async def exists(self, key):
        self._check()
        return int(key in self.values)

    async def zadd(self, key, mapping):
        self._check()
        self.zsets.setdefault(key, {}).update(mapping)

    async def zremrangebyscore(self, key, low, high):
        low = float("-inf") if low == "-inf" else low
        members = self.zsets.get(key, {})
        for member, score in list(members.items()):
            if low <= score <= high:
                del members[member]

    async def expire(self, key, seconds):
        return True

    async def zcount(self, key, low, high):
        self._check()
        return sum(1 for score in self.zsets.get(key, {}).values() if low <= score <= high)


@pytest.fixture
def store(monkeypatch):
    redis = _Redis()
    monkeypatch.setattr(agent_posts, "redis_client", AsyncMock(return_value=redis))
    # The send guard shares the fake; the tools below are built
    # without an agent, so it guards nothing and no request is added.
    monkeypatch.setattr(mail_send_guard, "redis_client", AsyncMock(return_value=redis))
    token = agent_posts.set_posting_assignment(POSTING)
    yield redis
    agent_posts.reset_posting_assignment(token)


def _message(msg_id, subject, to, *, cc=(), sent=NOW, sender="me@corp.example"):
    return {
        "id": msg_id,
        "subject": subject,
        "from": {"emailAddress": {"address": sender, "name": "Me"}},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        "ccRecipients": [{"emailAddress": {"address": a}} for a in cc],
        "sentDateTime": sent.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "receivedDateTime": sent.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bodyPreview": "",
    }


# ---------------------------------------------------------------------------
# The lookup
# ---------------------------------------------------------------------------


async def test_a_recorded_draft_is_the_agents(store):
    await agent_posts.record_mail_draft("draft-1")
    assert await agent_posts.is_agent_mail(store, MAILBOX, _message("draft-1", "Re: hi", ["a@x.example"]), POSTING)
    assert not await agent_posts.is_agent_mail(store, MAILBOX, _message("draft-2", "Re: hi", ["a@x.example"]), POSTING)


async def test_a_recorded_send_is_the_agents_in_the_mailbox_that_sent_it(store):
    await agent_posts.record_mail_send(MAILBOX, "Weekly  Digest", ["B@X.example", "a@x.example"], now=NOW.timestamp())
    sent_copy = _message("sent-1", "weekly digest", ["a@x.example"], cc=["b@x.example"], sent=NOW + timedelta(seconds=40))
    assert await agent_posts.is_agent_mail(store, MAILBOX, sent_copy, POSTING)


@pytest.mark.parametrize(
    ("mailbox", "message"),
    [
        pytest.param(OTHER_MAILBOX, _message("in-1", "Weekly digest", ["a@x.example"]), id="another-mailbox-receiving-it"),
        pytest.param(MAILBOX, _message("m-2", "Weekly digest", ["a@x.example"], sent=NOW + timedelta(hours=2)), id="later-mail"),
        pytest.param(MAILBOX, _message("m-3", "Re: Weekly digest", ["a@x.example"]), id="the-reply"),
        pytest.param(MAILBOX, _message("m-4", "Weekly digest", ["a@x.example", "c@x.example"]), id="other-recipients"),
        pytest.param(None, _message("m-5", "Weekly digest", ["a@x.example"]), id="mailbox-unknown"),
    ],
)
async def test_a_mail_that_is_not_the_recorded_send_still_triggers(store, mailbox, message):
    await agent_posts.record_mail_send(MAILBOX, "Weekly digest", ["a@x.example"], now=NOW.timestamp())
    assert not await agent_posts.is_agent_mail(store, mailbox, message, POSTING)


async def test_another_assignment_on_the_same_mailbox_still_triggers(store):
    await agent_posts.record_mail_draft("draft-1")
    await agent_posts.record_mail_send(MAILBOX, "Digest", ["a@x.example"], now=NOW.timestamp())
    assert not await agent_posts.is_agent_mail(store, MAILBOX, _message("draft-1", "Re: hi", ["a@x.example"]), OTHER)
    assert not await agent_posts.is_agent_mail(store, MAILBOX, _message("sent-1", "Digest", ["a@x.example"]), OTHER)


async def test_mail_sent_outside_an_assignment_run_is_not_recorded(store):
    token = agent_posts.set_posting_assignment(None)
    try:
        await agent_posts.record_mail_send(MAILBOX, "Digest", ["a@x.example"], now=NOW.timestamp())
        await agent_posts.record_mail_draft("draft-chat")
    finally:
        agent_posts.reset_posting_assignment(token)
    assert store.zsets == {}
    assert store.values == {}


async def test_nothing_is_recorded_without_the_mailbox_or_recipients(store):
    await agent_posts.record_mail_send(None, "s", ["a@x.example"])
    await agent_posts.record_mail_send(MAILBOX, "s", [])
    assert store.zsets == {}


async def test_the_store_is_fail_open_both_ways(monkeypatch):
    redis = _Redis(fail=True)
    monkeypatch.setattr(agent_posts, "redis_client", AsyncMock(return_value=redis))
    token = agent_posts.set_posting_assignment(POSTING)
    try:
        await agent_posts.record_mail_send(MAILBOX, "s", ["a@x.example"])
        await agent_posts.record_mail_draft("d")
    finally:
        agent_posts.reset_posting_assignment(token)
    assert not await agent_posts.is_agent_mail(redis, MAILBOX, _message("d", "s", ["a@x.example"]), POSTING)


# ---------------------------------------------------------------------------
# The poller
# ---------------------------------------------------------------------------


class _Http:
    def __init__(self, data: dict):
        self.data = data
        self.urls: list[str] = []

    async def get(self, url, **_k):
        self.urls.append(url)
        return SimpleNamespace(status_code=200, json=lambda: self.data, text="", headers={})


async def test_the_poller_skips_the_agents_mail_and_keeps_the_rest(store):
    await agent_posts.record_mail_draft("draft-agent")
    await agent_posts.record_mail_send(MAILBOX, "Digest", ["boss@x.example"], now=NOW.timestamp())
    listing = {
        "value": [
            _message("draft-agent", "Re: question", ["client@x.example"]),
            _message("sent-agent", "Digest", ["boss@x.example"]),
            _message("typed", "Re: question", ["client@x.example"]),
            _message("inbound", "question", ["me@corp.example"], sender="client@x.example"),
        ]
    }
    http = _Http(listing)
    wf = SimpleNamespace(id=POSTING)
    events = await event_poller._poll_microsoft_emails("tok", wf, store, http, integration_id=MAILBOX)
    # Graph lists the latest first; the poller returns them oldest first.
    assert [e["message_id"] for e in events] == ["inbound", "typed"]
    # sentDateTime read for the check stays out of the payload; to / cc are the
    # visible recipients the prompt renders (#9222);
    # conversation_id is the thread, the mail trigger's ordering key;
    # internet_message_id the mail's Message-ID, its dedup key across mailboxes.
    assert set(events[0]) == {
        "from",
        "from_name",
        "to",
        "cc",
        "subject",
        "body",
        "received_at",
        "is_flagged",
        "message_id",
        "conversation_id",
        "internet_message_id",
        "message_type",
        "trigger_type",
        "event",
        "provider",
    }, "the payload the agent reads carries no field of the check"


async def test_the_poller_advances_its_cursor_past_a_skipped_mail(store):
    await agent_posts.record_mail_draft("draft-agent")
    wf = SimpleNamespace(id=POSTING)
    listing = {"value": [_message("draft-agent", "Re: q", ["c@x.example"], sent=NOW + timedelta(minutes=3))]}
    assert await event_poller._poll_microsoft_emails("tok", wf, store, _Http(listing), integration_id=MAILBOX) == []
    assert store.values[f"event_cursor:{wf.id}"] == "2026-09-27T10:03:00Z"


# ---------------------------------------------------------------------------
# The tools record what they post
# ---------------------------------------------------------------------------


def _tools(monkeypatch, answers: list[tuple[dict | None, str | None]], **kwargs):
    from app.integrations.providers import microsoft

    calls: list[tuple[str, str]] = []

    async def _safe_request(_client, method, url, *_a, **_k):
        calls.append((method, url))
        data, err = answers.pop(0) if answers else ({}, None)
        return (SimpleNamespace(json=lambda: data, status_code=200) if data is not None else None), err

    monkeypatch.setattr(microsoft._common, "safe_request", _safe_request)
    executor = CallableToolExecutor()
    microsoft.build_tools("graph-token", executor, **kwargs)
    return executor._handlers, calls


async def test_a_sent_mail_is_recorded_for_its_mailbox(store, monkeypatch):
    handlers, _ = _tools(monkeypatch, [({}, None)], integration_id=MAILBOX)
    await handlers["send_microsoft_email"](to="a@x.example", subject="Digest", body="hi", cc="b@x.example")
    copy = _message("sent", "Digest", ["a@x.example"], cc=["b@x.example"], sent=datetime.now(UTC))
    assert await agent_posts.is_agent_mail(store, MAILBOX, copy, POSTING)


async def test_a_failed_send_records_nothing(store, monkeypatch):
    handlers, _ = _tools(monkeypatch, [(None, "Error: 403")], integration_id=MAILBOX)
    await handlers["send_microsoft_email"](to="a@x.example", subject="Digest", body="hi")
    assert store.zsets == {}


async def test_a_new_draft_is_recorded(store, monkeypatch):
    handlers, _ = _tools(monkeypatch, [({"id": "draft-9"}, None)], integration_id=MAILBOX)
    await handlers["create_microsoft_draft_email"](to="a@x.example", subject="s", body="b")
    assert agent_posts.agent_post_key("microsoft_mail_draft", "draft-9") in store.values


async def test_a_sent_draft_is_recorded_from_what_it_says(store, monkeypatch):
    draft = {"subject": "Proposal", "toRecipients": [{"emailAddress": {"address": "a@x.example"}}]}
    handlers, calls = _tools(monkeypatch, [(draft, None), ({}, None)], integration_id=MAILBOX)
    result = await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    assert "has been sent" in result
    assert [m for m, _ in calls] == ["get", "post"]
    copy = _message("sent", "Proposal", ["a@x.example"], sent=datetime.now(UTC))
    assert await agent_posts.is_agent_mail(store, MAILBOX, copy, POSTING)


async def test_without_the_mailbox_a_sent_draft_records_nothing(store, monkeypatch):
    # The draft is read for its signature images (signatures on), then sent; no mailbox, no record.
    handlers, calls = _tools(monkeypatch, [({}, None), ({}, None)])
    await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    assert [m for m, _ in calls] == ["get", "post"]
    assert store.zsets == {}


async def test_a_draft_that_cannot_be_read_is_still_sent(store, monkeypatch):
    handlers, calls = _tools(monkeypatch, [(None, "Error: 404"), ({}, None)], integration_id=MAILBOX)
    result = await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    assert "has been sent" in result
    assert [m for m, _ in calls] == ["get", "post"]
    assert store.zsets == {}


async def test_a_draft_that_cannot_be_read_is_logged_as_such_not_as_an_empty_mail(store, monkeypatch):
    """The agent accounts' log of the messages sent says the content is unknown, not empty."""
    from app.integrations.providers.microsoft._common import MicrosoftContext

    logged: list[dict] = []

    async def _log(self, account_provider, tool, **kwargs):
        logged.append({"provider": account_provider, "tool": tool, **kwargs})

    monkeypatch.setattr(MicrosoftContext, "log_account_send", _log)
    handlers, _ = _tools(monkeypatch, [(None, "Error: 503"), ({}, None)], integration_id=MAILBOX)
    assert "has been sent" in await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    (entry,) = logged
    assert entry["tool"] == "send_microsoft_draft" and entry["message_id"] == DRAFT_ID
    assert "content unavailable" in entry["text"] and "Error: 503" in entry["text"]
    assert entry.get("error") is None


async def test_a_reply_draft_is_recorded_as_soon_as_it_exists(store, monkeypatch):
    created = {"id": "reply-draft-1", "body": {"content": "<html><body><hr>quoted</body></html>"}}
    handlers, _ = _tools(monkeypatch, [(created, None)], integration_id=MAILBOX)
    # The answers are served in order: no conversation read before createReply.
    await handlers["create_microsoft_draft_reply"](message_id=DRAFT_ID, body="Thanks", reply_to_exact_message=True)
    assert agent_posts.agent_post_key("microsoft_mail_draft", "reply-draft-1") in store.values


def test_the_integration_tools_hand_the_mailbox_to_the_microsoft_tools():
    import inspect

    from app.services import integration_tools

    source = inspect.getsource(integration_tools)
    # WhatsApp joined the tuple for its read tools.
    assert 'if provider in ("notion", "microsoft", "whatsapp"):' in source
