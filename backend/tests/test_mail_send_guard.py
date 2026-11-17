"""One answer per conversation from a mailbox an agent sends through (layer 2).

Before a Microsoft mail tool sends, it takes a lock on the conversation for the
sending agent and looks for a message the agent already sent in it within the
window - its own record, then the mailbox's Sent Items - and refuses the send
naming that message unless ``force_duplicate=true`` is passed, or a message
from someone else arrived in the conversation after that earlier send (then
the new send answers it and goes out). Redis and Graph are replaced by fakes;
nothing here reaches the network.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import agent_posts, mail_send_guard
from app.tools.base import CallableToolExecutor

AGENT, OTHER_AGENT = str(uuid.uuid4()), str(uuid.uuid4())
MAILBOX = str(uuid.uuid4())
RUN_A, RUN_B = str(uuid.uuid4()), str(uuid.uuid4())
DRAFT_ID = "AAMkAGI2" + "A" * 140
CONVERSATION = "AAQkAGI2" + "Q" * 100
SENT_ITEMS_URL = "https://graph.microsoft.com/v1.0/me/mailFolders/sentitems/messages"
MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
ME_URL = "https://graph.microsoft.com/v1.0/me"
OWN_ADDRESS = "agent@mailbox.example"


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
        self._check()
        return self.values.get(key)

    async def delete(self, *keys):
        self._check()
        return sum(1 for key in keys if self.values.pop(key, None) is not None)

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    async def zremrangebyscore(self, key, low, high):
        return 0

    async def expire(self, key, seconds):
        return True

    def locks(self) -> list[str]:
        return [k for k in self.values if k.startswith("mail_send_guard:lock:")]

    def records(self) -> list[str]:
        return [k for k in self.values if k.startswith("mail_send_guard:sent:")]


@pytest.fixture
def redis(monkeypatch):
    store = _Redis()
    monkeypatch.setattr(mail_send_guard, "redis_client", AsyncMock(return_value=store))
    monkeypatch.setattr(agent_posts, "redis_client", AsyncMock(return_value=store))
    token = agent_posts.set_posting_assignment(RUN_A)
    yield store
    agent_posts.reset_posting_assignment(token)


def _sent_copy(conversation_id, subject, to, *, sent_at, msg_id="sent-1"):
    return {
        "id": msg_id,
        "subject": subject,
        "conversationId": conversation_id,
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        "ccRecipients": [],
        "sentDateTime": sent_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "webLink": "https://outlook.example/owa/sent-1",
    }


def _inbound(sender, *, received_at, draft=False, subject="RE: Question", headers=None):
    """A message of the conversation as the guard's listing selects it."""
    message = {
        "from": {"emailAddress": {"address": sender}},
        "receivedDateTime": received_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "isDraft": draft,
        "subject": subject,
    }
    if headers is not None:
        message["internetMessageHeaders"] = [{"name": n, "value": v} for n, v in headers]
    return message


class _Graph:
    """A Graph that answers by URL: Sent Items, the conversation listing, ``/me``, a draft's metadata, 202 for the rest."""

    def __init__(
        self,
        *,
        sent_items: list[dict] | None = None,
        draft: dict | None = None,
        conversation: list[dict] | None = None,
        conversation_error: str | None = None,
    ):
        self.sent_items = sent_items or []
        self.draft = draft
        self.conversation = conversation or []
        self.conversation_error = conversation_error
        self.calls: list[tuple[str, str]] = []
        self.params: list[dict] = []

    async def __call__(self, _client, method, url, *_a, **_k):
        self.calls.append((method, url))
        self.params.append(_k.get("params") or {})
        if url == SENT_ITEMS_URL:
            return SimpleNamespace(json=lambda: {"value": self.sent_items}, status_code=200), None
        if method == "get" and url == MESSAGES_URL:
            if self.conversation_error:
                return None, self.conversation_error
            return SimpleNamespace(json=lambda: {"value": self.conversation}, status_code=200), None
        if method == "get" and url == ME_URL:
            return SimpleNamespace(json=lambda: {"mail": OWN_ADDRESS}, status_code=200), None
        reads_draft = method == "get" or url.endswith(("/createReply", "/createReplyAll"))
        if reads_draft and self.draft is not None:
            return SimpleNamespace(json=lambda: self.draft, status_code=200), None
        return SimpleNamespace(json=lambda: {}, status_code=200), None

    def sends(self) -> list[str]:
        return [u for m, u in self.calls if m == "post" and (u.endswith("/send") or u.endswith("/sendMail"))]

    def sent_items_reads(self) -> int:
        return sum(1 for m, u in self.calls if m == "get" and u == SENT_ITEMS_URL)

    def conversation_reads(self) -> list[dict]:
        return [p for (m, u), p in zip(self.calls, self.params, strict=True) if m == "get" and u == MESSAGES_URL]


def _tools(monkeypatch, graph: _Graph, **kwargs):
    from app.integrations.providers import microsoft

    monkeypatch.setattr(microsoft._common, "safe_request", graph)
    executor = CallableToolExecutor()
    definitions = microsoft.build_tools("graph-token", executor, **kwargs)
    return executor._handlers, {d.name: d for d in definitions}


def _reply_draft(conversation_id=CONVERSATION, subject="Re: Question", to=("a@x.example",)):
    return {
        "subject": subject,
        "conversationId": conversation_id,
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        "ccRecipients": [],
    }


# ---------------------------------------------------------------------------
# The pure parts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (300, 300),
        ("120", 120),
        (0, 0),
        (-5, 0),
        (10**9, mail_send_guard.MAX_WINDOW_SECONDS),
        (True, mail_send_guard.DEFAULT_WINDOW_SECONDS),
        ("soon", mail_send_guard.DEFAULT_WINDOW_SECONDS),
    ],
)
def test_the_window_is_a_clamped_whole_number(raw, expected):
    assert mail_send_guard.window_seconds(raw) == expected


def test_the_window_reads_the_setting(monkeypatch):
    monkeypatch.setattr(mail_send_guard.settings, "mail_send_guard_window_seconds", 120)
    assert mail_send_guard.window_seconds() == 120


def test_the_default_window_is_five_minutes():
    assert mail_send_guard.settings.mail_send_guard_window_seconds == 300


def test_a_thread_is_its_conversation_else_its_subject_and_recipients():
    by_conversation = mail_send_guard.thread_key(CONVERSATION, subject="x", recipients=["a@x.example"])
    assert by_conversation == mail_send_guard.thread_key(CONVERSATION)
    assert by_conversation.startswith("conversation:")
    by_subject = mail_send_guard.thread_key(None, subject="Re: Weekly  digest", recipients=["B@x.example", "a@x.example"])
    assert by_subject == mail_send_guard.thread_fingerprint("weekly digest", ["a@x.example", "b@x.example"])
    assert by_subject == mail_send_guard.thread_fingerprint("FWD: Weekly digest", ["b@x.example", "A@x.example"])
    assert by_subject != mail_send_guard.thread_fingerprint("Weekly digest", ["a@x.example"])
    assert by_subject != mail_send_guard.thread_fingerprint("Monthly digest", ["a@x.example", "b@x.example"])


def test_a_sent_items_copy_counts_only_within_the_window():
    now = datetime(2026, 10, 30, 20, 0, tzinfo=UTC).timestamp()
    inside = mail_send_guard.recent_send(
        sent_at="2026-10-30T19:58:30Z", subject="Re: Q", recipients=["a@x.example"], message_id="m", now=now, window=300
    )
    assert inside is not None and inside.source == "sent_items" and inside.message_id == "m"
    assert mail_send_guard.recent_send(sent_at="2026-10-30T19:50:00Z", subject="Re: Q", recipients=[], now=now, window=300) is None
    assert mail_send_guard.recent_send(sent_at="not a date", subject="Re: Q", recipients=[], now=now, window=300) is None
    assert mail_send_guard.recent_send(sent_at="2026-10-30T19:58:30Z", subject="Re: Q", recipients=[], now=now, window=0) is None


def test_the_refusal_names_the_earlier_message_and_the_escape():
    earlier = mail_send_guard.EarlierSend(
        sent_at=1000.0, subject="Re: Q", recipients=("a@x.example",), message_id="m-1", web_link="https://o/1", source="sent_items"
    )
    text = mail_send_guard.refusal(earlier, window=300, tool="send_microsoft_draft", now=1003.0)
    assert text.startswith("Not sent:")
    for part in ("300 s", "sent 3 s ago", "Re: Q", "a@x.example", "m-1", "https://o/1", "Sent Items", "force_duplicate=true"):
        assert part in text
    assert "send_microsoft_draft" in text


def test_an_inbound_message_counts_only_after_the_send_and_from_someone_else():
    sent = datetime(2026, 10, 30, 20, 0, tzinfo=UTC)
    after = sent.timestamp()
    own = "agent@mailbox.example"
    later, earlier = sent + timedelta(seconds=40), sent - timedelta(seconds=40)
    inbound_after = mail_send_guard.inbound_after
    assert inbound_after([_inbound("b@x.example", received_at=later)], after=after, own_address=own) is True
    assert inbound_after([_inbound("b@x.example", received_at=earlier)], after=after, own_address=own) is False
    assert inbound_after([_inbound("Agent@Mailbox.example", received_at=later)], after=after, own_address=own) is False
    assert inbound_after([_inbound("b@x.example", received_at=later, draft=True)], after=after, own_address=own) is False
    assert inbound_after([], after=after, own_address=own) is False
    # A later message whose origin cannot be told keeps the refusal.
    assert inbound_after([_inbound("b@x.example", received_at=later)], after=after, own_address="") is None
    assert inbound_after([{"receivedDateTime": later.strftime("%Y-%m-%dT%H:%M:%SZ")}], after=after, own_address=own) is None


@pytest.mark.parametrize(
    ("subject", "headers", "expected"),
    [
        ("Automatic reply: RE: Question", None, True),
        ("Réponse automatique : RE: Question", None, True),
        ("Réponse automatique : RE: Question", None, True),
        ("Out of Office: Question", None, True),
        ("Abwesenheitsnotiz: Question", None, True),
        ("RE: Question", [("Auto-Submitted", "auto-replied")], True),
        ("RE: Question", [("Auto-Submitted", "auto-generated")], True),
        ("RE: Question", [("X-Autoreply", "yes")], True),
        ("RE: Question", [("Precedence", "auto_reply")], True),
        ("RE: Question", [("Auto-Submitted", "no")], False),
        ("RE: Question", [("Precedence", "bulk")], False),
        ("RE: Automatic reply settings", None, False),
        ("RE: Question", None, False),
        ("", None, False),
    ],
)
def test_an_automatic_reply_is_told_by_its_headers_or_its_subject(subject, headers, expected):
    message = _inbound("b@x.example", received_at=datetime(2026, 11, 7, tzinfo=UTC), subject=subject, headers=headers)
    assert mail_send_guard.is_automatic_reply(message) is expected


def test_an_automatic_reply_after_the_send_is_nothing_new_to_answer():
    sent = datetime(2026, 11, 7, 0, 38, 23, tzinfo=UTC)
    later = sent + timedelta(seconds=16)
    out_of_office = [
        _inbound("cedric@x.example", received_at=later, subject="Réponse automatique : RE: Interview"),
        _inbound("cindy@x.example", received_at=later, headers=[("Auto-Submitted", "auto-replied")]),
    ]
    own = "agent@mailbox.example"
    assert mail_send_guard.inbound_after(out_of_office, after=sent.timestamp(), own_address=own) is False
    # An unknown own address does not matter for a message that does not count.
    assert mail_send_guard.inbound_after(out_of_office, after=sent.timestamp(), own_address="") is False
    person = [*out_of_office, _inbound("cedric@x.example", received_at=later, subject="RE: Interview")]
    assert mail_send_guard.inbound_after(person, after=sent.timestamp(), own_address=own) is True


def test_a_record_survives_the_round_trip():
    earlier = mail_send_guard.EarlierSend(
        sent_at=1000.5, subject="Q", recipients=("a@x.example",), message_id="d-1", conversation_id="conv-1"
    )
    again = mail_send_guard.EarlierSend.from_json(earlier.to_json().encode("utf-8"))
    assert again == earlier
    assert mail_send_guard.EarlierSend.from_json("not json") is None
    assert mail_send_guard.EarlierSend.from_json(None) is None


# ---------------------------------------------------------------------------
# The guard around the tools
# ---------------------------------------------------------------------------


async def test_a_second_reply_in_the_conversation_is_refused_from_the_agents_record(redis, monkeypatch):
    graph = _Graph(draft=_reply_draft())
    handlers, _ = _tools(monkeypatch, graph, integration_id=MAILBOX, agent_id=AGENT)
    first = await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    assert "has been sent" in first
    assert len(graph.sends()) == 1
    assert redis.records() and not redis.locks(), "the send is recorded and the lock given back"

    # Another run of the same agent answers the same thread.
    token = agent_posts.set_posting_assignment(RUN_B)
    try:
        second = await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    finally:
        agent_posts.reset_posting_assignment(token)
    assert second.startswith("Not sent:")
    assert DRAFT_ID in second and "force_duplicate=true" in second
    assert "no message from anyone else arrived in the conversation since" in second
    assert len(graph.sends()) == 1, "nothing else was sent"
    assert not redis.locks()
    # The conversation was read once, bounded to what arrived after the earlier send.
    (read,) = graph.conversation_reads()
    assert f"conversationId eq '{CONVERSATION}'" in read["$filter"] and "receivedDateTime gt " in read["$filter"]


async def test_a_message_already_in_sent_items_refuses_the_send_without_a_record(redis, monkeypatch):
    now = datetime.now(UTC)
    copy = _sent_copy(CONVERSATION, "Re: Question", ["a@x.example"], sent_at=now - timedelta(seconds=3))
    graph = _Graph(draft=_reply_draft(), sent_items=[copy])
    handlers, _ = _tools(monkeypatch, graph, integration_id=MAILBOX, agent_id=AGENT)
    result = await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    assert result.startswith("Not sent:")
    assert "sent-1" in result and "Sent Items" in result and "https://outlook.example/owa/sent-1" in result
    assert graph.sends() == []
    assert graph.sent_items_reads() == 1
    assert not redis.locks()


async def test_a_newer_inbound_message_lets_the_second_reply_go_out(redis, monkeypatch):
    graph = _Graph(draft=_reply_draft())
    handlers, _ = _tools(monkeypatch, graph, integration_id=MAILBOX, agent_id=AGENT)
    assert "has been sent" in await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    # The counterpart answered after the agent's reply: the next reply answers that.
    later = datetime.now(UTC) + timedelta(seconds=5)
    graph.conversation = [
        _inbound(OWN_ADDRESS, received_at=later),
        _inbound("Counterpart@x.example", received_at=later),
    ]
    second = await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    assert "has been sent" in second, "no force_duplicate needed"
    assert len(graph.sends()) == 2
    assert len(graph.conversation_reads()) == 1
    assert redis.records() and not redis.locks(), "the second send is recorded in turn"


async def test_out_of_office_answers_since_the_first_reply_keep_the_refusal(redis, monkeypatch):
    graph = _Graph(draft=_reply_draft())
    handlers, _ = _tools(monkeypatch, graph, integration_id=MAILBOX, agent_id=AGENT)
    assert "has been sent" in await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    # Two recipients of the first reply are away: their servers answered it.
    later = datetime.now(UTC) + timedelta(seconds=5)
    graph.conversation = [
        _inbound("counterpart@x.example", received_at=later, subject="Automatic reply: RE: Question"),
        _inbound("colleague@x.example", received_at=later, headers=[("Auto-Submitted", "auto-replied")]),
    ]
    token = agent_posts.set_posting_assignment(RUN_B)
    try:
        second = await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    finally:
        agent_posts.reset_posting_assignment(token)
    assert second.startswith("Not sent:")
    assert "no message from anyone else arrived in the conversation since" in second
    assert len(graph.sends()) == 1, "the second run's reply did not go out"
    (read,) = graph.conversation_reads()
    assert "internetMessageHeaders" in read["$select"] and "subject" in read["$select"]


async def test_only_this_mailboxs_own_messages_since_keep_the_refusal(redis, monkeypatch):
    graph = _Graph(draft=_reply_draft())
    handlers, _ = _tools(monkeypatch, graph, integration_id=MAILBOX, agent_id=AGENT)
    assert "has been sent" in await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    later = datetime.now(UTC) + timedelta(seconds=5)
    graph.conversation = [
        _inbound(OWN_ADDRESS.upper(), received_at=later),
        _inbound("counterpart@x.example", received_at=later, draft=True),
    ]
    second = await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    assert second.startswith("Not sent:")
    assert len(graph.sends()) == 1


async def test_a_failed_conversation_read_keeps_the_refusal(redis, monkeypatch):
    graph = _Graph(draft=_reply_draft(), conversation_error="Error: 503")
    handlers, _ = _tools(monkeypatch, graph, integration_id=MAILBOX, agent_id=AGENT)
    assert "has been sent" in await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    second = await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    assert second.startswith("Not sent:") and DRAFT_ID in second
    assert "could not be checked" in second
    assert len(graph.sends()) == 1
    assert not redis.locks()


async def test_a_newer_inbound_after_a_sent_items_copy_lets_a_new_email_go_out(redis, monkeypatch):
    now = datetime.now(UTC)
    copy = _sent_copy(CONVERSATION, "Digest", ["a@x.example"], sent_at=now - timedelta(seconds=30))
    graph = _Graph(sent_items=[copy], conversation=[_inbound("a@x.example", received_at=now - timedelta(seconds=10))])
    handlers, _ = _tools(monkeypatch, graph, integration_id=MAILBOX, agent_id=AGENT)
    result = await handlers["send_microsoft_email"](to="a@x.example", subject="Re: Digest", body="answer")
    assert "Email sent" in result
    (read,) = graph.conversation_reads()
    assert f"conversationId eq '{CONVERSATION}'" in read["$filter"]


async def test_force_duplicate_sends_and_is_still_recorded(redis, monkeypatch):
    now = datetime.now(UTC)
    copy = _sent_copy(CONVERSATION, "Re: Question", ["a@x.example"], sent_at=now - timedelta(seconds=3))
    graph = _Graph(draft=_reply_draft(), sent_items=[copy])
    handlers, _ = _tools(monkeypatch, graph, integration_id=MAILBOX, agent_id=AGENT)
    result = await handlers["send_microsoft_draft"](draft_id=DRAFT_ID, force_duplicate=True)
    assert "has been sent" in result
    assert len(graph.sends()) == 1
    assert graph.sent_items_reads() == 0, "a forced send checks nothing"
    assert redis.records(), "but it is recorded, so the one after it is caught"


async def test_another_conversation_is_untouched(redis, monkeypatch):
    graph = _Graph(draft=_reply_draft())
    handlers, _ = _tools(monkeypatch, graph, integration_id=MAILBOX, agent_id=AGENT)
    assert "has been sent" in await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    graph.draft = _reply_draft(conversation_id="AAQkAGI2" + "Z" * 100, subject="Re: Other")
    assert "has been sent" in await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    assert len(graph.sends()) == 2
    assert len(redis.records()) == 2


async def test_another_agent_sending_from_the_same_mailbox_is_refused(redis, monkeypatch):
    # The record and the lock are the mailbox's: an event run and a chat
    # run answering one thread from the same mailbox send one reply.
    graph = _Graph(draft=_reply_draft())
    handlers, _ = _tools(monkeypatch, graph, integration_id=MAILBOX, agent_id=AGENT)
    assert "has been sent" in await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    other_handlers, _ = _tools(monkeypatch, graph, integration_id=MAILBOX, agent_id=OTHER_AGENT)
    second = await other_handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    assert second.startswith("Not sent:") and DRAFT_ID in second
    assert len(graph.sends()) == 1
    assert "has been sent" in await other_handlers["send_microsoft_draft"](draft_id=DRAFT_ID, force_duplicate=True)
    assert len(graph.sends()) == 2


async def test_another_mailbox_answering_the_same_conversation_is_not_refused(redis, monkeypatch):
    graph = _Graph(draft=_reply_draft())
    handlers, _ = _tools(monkeypatch, graph, integration_id=MAILBOX, agent_id=AGENT)
    assert "has been sent" in await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    other_handlers, _ = _tools(monkeypatch, graph, integration_id=str(uuid.uuid4()), agent_id=AGENT)
    assert "has been sent" in await other_handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    assert len(graph.sends()) == 2


async def test_a_reply_in_thread_after_a_draft_send_is_refused_across_entry_points(redis, monkeypatch):
    graph = _Graph(draft=_reply_draft())
    handlers, _ = _tools(monkeypatch, graph, integration_id=MAILBOX, agent_id=AGENT)
    assert "has been sent" in await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    graph.draft = {**_reply_draft(), "id": DRAFT_ID, "body": {"content": "<html><body><hr>q</body></html>"}}
    other_handlers, _ = _tools(monkeypatch, graph, integration_id=MAILBOX, agent_id=OTHER_AGENT)
    second = await other_handlers["reply_microsoft_email_in_thread"](
        message_id=DRAFT_ID, body="Thanks", to="a@x.example", send=True
    )
    assert "Not sent:" in second and "force_duplicate=true" in second
    assert len(graph.sends()) == 1


async def test_a_send_outside_an_agents_tools_is_not_guarded(redis, monkeypatch):
    # No agent: a person repeating a mail on purpose, not two runs answering one inbound mail.
    now = datetime.now(UTC)
    copy = _sent_copy(CONVERSATION, "Re: Question", ["a@x.example"], sent_at=now - timedelta(seconds=3))
    graph = _Graph(draft=_reply_draft(), sent_items=[copy])
    handlers, _ = _tools(monkeypatch, graph, integration_id=MAILBOX)
    assert "has been sent" in await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    assert "has been sent" in await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    assert len(graph.sends()) == 2
    assert graph.sent_items_reads() == 0
    assert not redis.records() and not redis.locks()


async def test_lock_contention_refuses_the_send_and_keeps_the_other_lock(redis, monkeypatch):
    graph = _Graph(draft=_reply_draft())
    handlers, _ = _tools(monkeypatch, graph, integration_id=MAILBOX, agent_id=AGENT)
    # Another attempt of this agent holds the thread's lock right now.
    other = mail_send_guard.SendGuard(f"mailbox:{MAILBOX}", mail_send_guard.thread_key(CONVERSATION), window=300)
    assert await other.acquire()
    result = await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    assert result.startswith("Not sent:") and "in progress" in result and "force_duplicate=true" in result
    assert graph.sends() == []
    assert graph.sent_items_reads() == 0
    assert redis.locks() == [other.lock_key], "the holder's lock is never released by the loser"
    await other.release()
    assert not redis.locks()
    assert "has been sent" in await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)


async def test_window_zero_turns_the_guard_off(redis, monkeypatch):
    monkeypatch.setattr(mail_send_guard.settings, "mail_send_guard_window_seconds", 0)
    now = datetime.now(UTC)
    copy = _sent_copy(CONVERSATION, "Re: Question", ["a@x.example"], sent_at=now - timedelta(seconds=3))
    graph = _Graph(draft=_reply_draft(), sent_items=[copy])
    handlers, definitions = _tools(monkeypatch, graph, integration_id=MAILBOX, agent_id=AGENT)
    assert "has been sent" in await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    assert "has been sent" in await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    assert len(graph.sends()) == 2
    assert graph.sent_items_reads() == 0
    assert not redis.records() and not redis.locks()
    assert "force_duplicate" not in definitions["send_microsoft_draft"].description


async def test_redis_down_is_fail_open_and_sent_items_still_guard(monkeypatch):
    store = _Redis(fail=True)
    monkeypatch.setattr(mail_send_guard, "redis_client", AsyncMock(return_value=store))
    monkeypatch.setattr(agent_posts, "redis_client", AsyncMock(return_value=store))
    graph = _Graph(draft=_reply_draft())
    handlers, _ = _tools(monkeypatch, graph, integration_id=MAILBOX, agent_id=AGENT)
    assert "has been sent" in await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    now = datetime.now(UTC)
    graph.sent_items = [_sent_copy(CONVERSATION, "Re: Question", ["a@x.example"], sent_at=now - timedelta(seconds=2))]
    assert (await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)).startswith("Not sent:")
    assert len(graph.sends()) == 1


async def test_a_new_email_is_guarded_by_its_subject_and_recipients(redis, monkeypatch):
    graph = _Graph()
    handlers, _ = _tools(monkeypatch, graph, integration_id=MAILBOX, agent_id=AGENT)
    assert "Email sent" in await handlers["send_microsoft_email"](to="a@x.example", subject="Digest", body="hi")
    second = await handlers["send_microsoft_email"](to="A@x.example", subject="Re: Digest", body="hi again")
    assert second.startswith("Not sent:") and "Digest" in second
    third = await handlers["send_microsoft_email"](to="a@x.example, c@x.example", subject="Digest", body="hi")
    assert "Email sent" in third, "other recipients: another thread"
    assert "Email sent" in await handlers["send_microsoft_email"](
        to="a@x.example", subject="Digest", body="hi", force_duplicate=True
    )
    assert len(graph.sends()) == 3


async def test_a_new_email_matching_a_sent_items_copy_is_refused(redis, monkeypatch):
    now = datetime.now(UTC)
    copy = _sent_copy(CONVERSATION, "Digest", ["a@x.example"], sent_at=now - timedelta(seconds=10))
    graph = _Graph(sent_items=[copy])
    handlers, _ = _tools(monkeypatch, graph, integration_id=MAILBOX, agent_id=AGENT)
    result = await handlers["send_microsoft_email"](to="a@x.example", subject="Digest", body="hi")
    assert result.startswith("Not sent:") and "sent-1" in result
    assert graph.sends() == []


async def test_a_failed_send_records_nothing_and_releases_the_lock(redis, monkeypatch):
    graph = _Graph(draft=_reply_draft())

    async def _failing(_client, method, url, *_a, **_k):
        graph.calls.append((method, url))
        if method == "post":
            return None, "Error: 503"
        return await graph(_client, method, url)

    from app.integrations.providers import microsoft

    monkeypatch.setattr(microsoft._common, "safe_request", _failing)
    executor = CallableToolExecutor()
    microsoft.build_tools("graph-token", executor, integration_id=MAILBOX, agent_id=AGENT)
    assert "Error: 503" in await executor._handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    assert not redis.records() and not redis.locks()


async def test_a_reply_draft_warns_when_the_conversation_was_already_answered(redis, monkeypatch):
    graph = _Graph(draft=_reply_draft())
    handlers, _ = _tools(monkeypatch, graph, integration_id=MAILBOX, agent_id=AGENT)
    assert "has been sent" in await handlers["send_microsoft_draft"](draft_id=DRAFT_ID)
    created = {"id": "reply-draft-2", "conversationId": CONVERSATION, "body": {"content": "<html><body><hr>q</body></html>"}}
    graph.draft = created
    result = await handlers["create_microsoft_draft_reply"](message_id=DRAFT_ID, body="Thanks")
    assert "Draft reply created" in result
    assert "Warning: this mailbox already sent a message in this conversation" in result
    assert "force_duplicate=true" in result


def test_the_tools_offer_the_escape_and_say_what_the_guard_does(monkeypatch):
    _, definitions = _tools(monkeypatch, _Graph(), integration_id=MAILBOX, agent_id=AGENT)
    for name in ("send_microsoft_email", "send_microsoft_draft"):
        assert definitions[name].input_schema["properties"]["force_duplicate"]["type"] == "boolean"
        assert "force_duplicate=true" in definitions[name].description
        assert "300 s" in definitions[name].description
    _, bare = _tools(monkeypatch, _Graph())
    assert "force_duplicate=true" not in bare["send_microsoft_draft"].description, "no scope: no guard, no note"


def test_the_integration_tools_hand_the_agent_to_the_microsoft_tools():
    import inspect

    from app.services import integration_tools

    source = inspect.getsource(integration_tools)
    assert 'if provider == "microsoft" and context.get("agent_id"):' in source
    assert '_builder_kwargs["agent_id"] = str(context["agent_id"])' in source
