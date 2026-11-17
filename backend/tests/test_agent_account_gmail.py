"""Gmail as an agent-account provider, with the Gmail API answered by a fake."""

from __future__ import annotations

import base64
import email
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.services.agent_accounts.providers import (
    AccountSignature,
    MessageMark,
    ProviderError,
    ProviderSession,
    get_provider,
)
from app.services.agent_accounts.providers import google_gmail as gmail
from app.services.agent_accounts.rules import ListeningPlan

GMAIL = gmail.GMAIL
ME = "sam.personal@example.com"


class FakeGmail:
    """Answers ``_call`` from a table of ``(method, url) -> body``; records every call."""

    def __init__(self, routes: dict[tuple[str, str], Any]):
        self.routes = routes
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, session, method, url, *, params=None, payload=None, allow_missing=False):
        self.calls.append({"method": method, "url": url, "params": params, "payload": payload})
        key = (method, url)
        if key not in self.routes:
            if allow_missing:
                return None
            raise ProviderError(f"unexpected {method} {url}")
        body = self.routes[key]
        return body(params, payload) if callable(body) else body

    def made(self, method: str, url: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] == method and c["url"] == url]


SEND_AS = {"sendAs": [{"sendAsEmail": ME, "isPrimary": True, "signature": "<div>Sam</div>"}]}


def _provider(monkeypatch, routes) -> tuple[gmail.GmailProvider, FakeGmail]:
    provider = gmail.GmailProvider()
    fake = FakeGmail({("GET", f"{GMAIL}/settings/sendAs"): SEND_AS, **routes})
    monkeypatch.setattr(provider, "_call", fake)
    return provider, fake


def _session(**kwargs) -> ProviderSession:
    return ProviderSession(token="token", account_external_id=ME, **kwargs)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _msg(
    msg_id: str = "m1",
    *,
    sender: str = "Alice <alice@partner.example>",
    to: str = ME,
    cc: str = "",
    subject: str = "Lunch",
    body: str = "Are you free on Tuesday?",
    labels: tuple[str, ...] = ("INBOX", "UNREAD", "CATEGORY_PERSONAL"),
    thread: str = "t1",
    received: datetime | None = None,
    extra: dict[str, str] | None = None,
) -> dict:
    headers = {"From": sender, "To": to, "Subject": subject, "Message-ID": f"<{msg_id}@partner.example>"}
    if cc:
        headers["Cc"] = cc
    headers.update(extra or {})
    when = received or datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
    return {
        "id": msg_id,
        "threadId": thread,
        "labelIds": list(labels),
        "internalDate": str(int(when.timestamp() * 1000)),
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": k, "value": v} for k, v in headers.items()],
            "parts": [{"mimeType": "text/plain", "body": {"data": _b64(body)}}],
        },
    }


PLAN = ListeningPlan(chats=True)


def test_registered_as_a_mailbox_on_a_google_integration():
    provider = get_provider("google_gmail")
    assert provider is not None
    assert provider.integration_provider == "google"
    assert provider.kind == "mailbox"
    assert provider.capabilities.triage and provider.capabilities.signature and provider.capabilities.poll


async def test_identify_reads_the_profile(monkeypatch):
    provider, _ = _provider(monkeypatch, {("GET", f"{GMAIL}/profile"): {"emailAddress": "Sam.Personal@Example.com"}})
    identity = await provider.identify(ProviderSession(token="t"))
    assert identity.external_id == ME and identity.email == ME


async def test_direct_email_to_the_account(monkeypatch):
    provider, _ = _provider(monkeypatch, {})
    event = await provider.mail_event(_session(), _msg())
    assert event is not None
    assert event.conversation_type == "direct" and event.mentions_account
    assert event.thread_id == "t1" and event.sender_email == "alice@partner.example"
    assert event.text.startswith("Subject: Lunch\n\nAre you free")
    assert event.route == {"kind": "mail", "message_id": "m1", "thread_id": "t1", "reply_all": False}
    assert not event.from_application and not event.from_account


async def test_group_email_and_reply_recipients(monkeypatch):
    provider, _ = _provider(monkeypatch, {})
    event = await provider.mail_event(_session(), _msg(cc="bob@partner.example, carol@partner.example"))
    assert event.conversation_type == "group" and event.reply_recipients == 2 and event.route["reply_all"]


@pytest.mark.parametrize(
    "labels, extra",
    [
        (("INBOX", "CATEGORY_PROMOTIONS"), None),
        (("INBOX", "CATEGORY_SOCIAL"), None),
        (("INBOX", "CATEGORY_UPDATES"), None),
        (("INBOX",), {"List-Unsubscribe": "<mailto:u@example.com>"}),
        (("INBOX",), {"Auto-Submitted": "auto-replied"}),
    ],
)
async def test_application_mail(monkeypatch, labels, extra):
    provider, _ = _provider(monkeypatch, {})
    event = await provider.mail_event(_session(), _msg(labels=labels, extra=extra))
    assert event is not None and event.from_application


@pytest.mark.parametrize("label", ["DRAFT", "SPAM", "TRASH"])
async def test_never_heard(monkeypatch, label):
    provider, _ = _provider(monkeypatch, {})
    assert await provider.mail_event(_session(), _msg(labels=("INBOX", label))) is None


async def test_own_and_agent_sent_mail(monkeypatch):
    provider, _ = _provider(monkeypatch, {})
    own = await provider.mail_event(_session(), _msg(sender=f"Sam <{ME}>", to="alice@partner.example"))
    assert own.from_account
    agent = await provider.mail_event(_session(), _msg(extra={gmail.AGENT_HEADER: "reply"}))
    assert agent.from_agent_account


async def test_first_poll_starts_at_the_current_history_id(monkeypatch):
    provider, fake = _provider(monkeypatch, {("GET", f"{GMAIL}/profile"): {"emailAddress": ME, "historyId": "500"}})
    events, cursor = await provider.poll(_session(), PLAN, None)
    assert events == [] and cursor == "500"
    assert not fake.made("GET", f"{GMAIL}/history")


async def test_poll_reads_the_history_deltas_once_each(monkeypatch):
    history = {
        "historyId": "520",
        "history": [
            {"messagesAdded": [{"message": {"id": "m2"}}, {"message": {"id": "m1"}}]},
            {"messagesAdded": [{"message": {"id": "m2"}}]},
        ],
    }
    early = datetime(2026, 11, 7, 8, 0, tzinfo=UTC)
    provider, fake = _provider(
        monkeypatch,
        {
            ("GET", f"{GMAIL}/history"): history,
            ("GET", f"{GMAIL}/messages/m1"): _msg("m1", received=early),
            ("GET", f"{GMAIL}/messages/m2"): _msg("m2"),
        },
    )
    events, cursor = await provider.poll(_session(), PLAN, "510")
    assert [e.message_id for e in events] == ["m1", "m2"]  # oldest first, m2 once
    assert cursor == "520"
    call = fake.made("GET", f"{GMAIL}/history")[0]
    assert call["params"]["startHistoryId"] == "510" and "labelId" not in call["params"]
    assert len(fake.made("GET", f"{GMAIL}/messages/m2")) == 1


async def test_poll_skips_a_message_neither_in_the_inbox_nor_sent(monkeypatch):
    provider, _ = _provider(
        monkeypatch,
        {
            ("GET", f"{GMAIL}/history"): {
                "historyId": "9",
                "history": [{"messagesAdded": [{"message": {"id": "m1"}}]}],
            },
            ("GET", f"{GMAIL}/messages/m1"): _msg("m1", labels=("CATEGORY_PERSONAL",)),
        },
    )
    events, cursor = await provider.poll(_session(), PLAN, "8")
    assert events == [] and cursor == "9"


async def test_poll_hears_what_the_owner_sends(monkeypatch):
    history = {
        "historyId": "30",
        "history": [
            {"messagesAdded": [{"message": {"id": "s1", "labelIds": ["SENT"]}}]},
            {"messagesAdded": [{"message": {"id": "d1", "labelIds": ["DRAFT"]}}]},
        ],
    }
    provider, fake = _provider(
        monkeypatch,
        {
            ("GET", f"{GMAIL}/history"): history,
            ("GET", f"{GMAIL}/messages/s1"): _msg(
                "s1", sender=f"Sam <{ME}>", to="alice@partner.example", labels=("SENT",)
            ),
        },
    )
    events, cursor = await provider.poll(_session(), PLAN, "20")
    assert [e.message_id for e in events] == ["s1"] and events[0].from_account
    assert cursor == "30"
    # A draft is dropped from the history entry: never read.
    assert not fake.made("GET", f"{GMAIL}/messages/d1")


async def test_expired_history_cursor_resyncs_seven_days(monkeypatch):
    now = datetime.now(UTC)
    old = now - timedelta(days=9)
    provider, fake = _provider(
        monkeypatch,
        {
            # No ("GET", history) route: allow_missing answers None, Gmail's 404.
            ("GET", f"{GMAIL}/messages"): {"messages": [{"id": "new"}, {"id": "old"}]},
            ("GET", f"{GMAIL}/messages/new"): _msg("new", received=now - timedelta(hours=1)),
            ("GET", f"{GMAIL}/messages/old"): _msg("old", received=old),
            ("GET", f"{GMAIL}/profile"): {"emailAddress": ME, "historyId": "900"},
        },
    )
    events, cursor = await provider.poll(_session(), PLAN, "1")
    assert [e.message_id for e in events] == ["new"]
    assert cursor == "900"
    listed = fake.made("GET", f"{GMAIL}/messages")[0]
    assert listed["params"]["q"] == f"newer_than:{gmail.RESYNC_DAYS}d"


async def test_no_poll_without_chats(monkeypatch):
    provider, fake = _provider(monkeypatch, {})
    assert await provider.poll(_session(), ListeningPlan(), "5") == ([], "5")
    assert fake.calls == []


async def test_needs_owner_stars_and_never_moves(monkeypatch):
    provider, fake = _provider(monkeypatch, {("POST", f"{GMAIL}/messages/m1/modify"): {}})
    await provider.mark_message(_session(), {"kind": "mail", "message_id": "m1"}, MessageMark(flag=True))
    payload = fake.made("POST", f"{GMAIL}/messages/m1/modify")[0]["payload"]
    assert payload == {"addLabelIds": ["STARRED"], "removeLabelIds": []}


async def test_label_created_once_when_missing_and_read_on_request(monkeypatch):
    created: list[dict] = []

    def create(_params, payload):
        created.append(payload)
        return {"id": "Label_7", "name": payload["name"]}

    provider, fake = _provider(
        monkeypatch,
        {
            ("GET", f"{GMAIL}/labels"): {"labels": [{"id": "INBOX", "name": "INBOX"}]},
            ("POST", f"{GMAIL}/labels"): create,
            ("POST", f"{GMAIL}/messages/m1/modify"): {},
            ("POST", f"{GMAIL}/messages/m2/modify"): {},
        },
    )
    await provider.mark_message(
        _session(), {"kind": "mail", "message_id": "m1"}, MessageMark(read=True, category="Needs Sam")
    )
    await provider.mark_message(_session(), {"kind": "mail", "message_id": "m2"}, MessageMark(category="needs sam"))
    assert len(created) == 1 and created[0]["name"] == "Needs Sam"
    first = fake.made("POST", f"{GMAIL}/messages/m1/modify")[0]["payload"]
    assert first == {"addLabelIds": ["Label_7"], "removeLabelIds": ["UNREAD"]}
    second = fake.made("POST", f"{GMAIL}/messages/m2/modify")[0]["payload"]
    assert "INBOX" not in second["removeLabelIds"]


async def test_empty_mark_writes_nothing(monkeypatch):
    provider, fake = _provider(monkeypatch, {})
    await provider.mark_message(_session(), {"kind": "mail", "message_id": "m1"}, MessageMark())
    assert fake.calls == []


def _sent(fake: FakeGmail) -> tuple[dict, email.message.Message]:
    payload = fake.made("POST", f"{GMAIL}/messages/send")[0]["payload"]
    raw = base64.urlsafe_b64decode(payload["raw"])
    return payload, email.message_from_bytes(raw)


def _metadata(msg: dict) -> dict:
    return {**msg, "payload": {"headers": msg["payload"]["headers"]}}


@pytest.mark.parametrize(
    "signature, expected",
    [
        (AccountSignature("own"), "<div>Sam</div>"),
        (AccountSignature("none"), None),
        (AccountSignature("given", "<p>From the signature integration</p>"), "<p>From the signature integration</p>"),
    ],
)
async def test_reply_is_threaded_marked_and_signed_as_chosen(monkeypatch, signature, expected):
    original = _msg(extra={"References": "<root@partner.example>"})
    provider, fake = _provider(
        monkeypatch,
        {
            ("GET", f"{GMAIL}/messages/m1"): _metadata(original),
            ("POST", f"{GMAIL}/messages/send"): {"id": "sent-1", "threadId": "t1"},
        },
    )
    sent_id = await provider.send_reply(
        _session(signature=signature), {"kind": "mail", "message_id": "m1"}, "Yes, Tuesday works."
    )
    assert sent_id == "sent-1"
    payload, mime = _sent(fake)
    assert payload["threadId"] == "t1"
    assert mime["In-Reply-To"] == "<m1@partner.example>"
    assert mime["References"] == "<root@partner.example> <m1@partner.example>"
    assert mime["Subject"] == "Re: Lunch"
    assert mime["To"] == "Alice <alice@partner.example>"
    assert mime[gmail.AGENT_HEADER] == "reply"
    html = next(p for p in mime.walk() if p.get_content_type() == "text/html").get_payload(decode=True).decode()
    if expected is None:
        assert "gmail_signature" not in html
    else:
        assert expected in html


async def test_reply_all_leaves_the_account_out_and_keeps_the_owner(monkeypatch):
    original = _msg(cc=f"bob@partner.example, {ME}")
    provider, fake = _provider(
        monkeypatch,
        {
            ("GET", f"{GMAIL}/messages/m1"): _metadata(original),
            ("POST", f"{GMAIL}/messages/send"): {"id": "sent-2"},
        },
    )
    session = _session(signature=AccountSignature("none"), owner_email="owner@corp.example")
    await provider.send_reply(session, {"kind": "mail", "message_id": "m1", "reply_all": True}, "Noted.")
    _payload, mime = _sent(fake)
    assert mime["Cc"] == "bob@partner.example, owner@corp.example"


async def test_context_is_the_thread_before_the_message(monkeypatch):
    first = _msg(
        "m0",
        sender=f"Sam <{ME}>",
        to="alice@partner.example",
        body="Lunch next week?",
        received=datetime(2026, 11, 6, 9, 0, tzinfo=UTC),
    )
    second = _msg("m1")
    later = _msg("m2", sender=f"Sam <{ME}>", received=datetime(2026, 11, 7, 10, 0, tzinfo=UTC))
    provider, _ = _provider(monkeypatch, {("GET", f"{GMAIL}/threads/t1"): {"messages": [second, later, first]}})
    event = await provider.mail_event(_session(), second)
    context = await provider.fetch_context(_session(), event, 10)
    assert [(c.message_id, c.from_account) for c in context] == [("m0", True)]
    assert await provider.account_messages_since(_session(), event) == ["m2"]


@pytest.mark.parametrize("bad", ["../profile", "a/b", "", "x" * 300])
async def test_ids_are_checked_before_any_call(monkeypatch, bad):
    provider, fake = _provider(monkeypatch, {})
    with pytest.raises(ProviderError):
        await provider.mark_message(_session(), {"kind": "mail", "message_id": bad}, MessageMark(flag=True))
    assert fake.calls == []
