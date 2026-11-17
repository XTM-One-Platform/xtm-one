"""Microsoft Outlook as an agent-account provider, with Graph answered by a fake."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.services.agent_accounts.events import AccountEvent
from app.services.agent_accounts.providers import (
    AccountSignature,
    InboundNotification,
    MessageMark,
    ProviderError,
    ProviderSession,
    get_provider,
)
from app.services.agent_accounts.providers import microsoft_outlook as outlook
from app.services.agent_accounts.rules import ListeningPlan

GRAPH = outlook.GRAPH
ME = "me-id"
ME_MAIL = "sam@example.com"
MESSAGE_ID = "AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0AsamplemessageidxxxxxxxxxxxxxxxxxxxxxxAAA="
DRAFT_ID = "AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0AsampledraftidxxxxxxxxxxxxxxxxxxxxxxxxAAB="
CONVERSATION = "AAQkADAwATM0MDAAMS0xMDJhLTAwAhAAsampleconversation="
ROUTE = {"kind": "mail", "message_id": MESSAGE_ID, "reply_all": False}


class FakeGraph:
    """Answers ``_call`` from a table of ``(method, url) -> body``; records every call with its headers."""

    def __init__(self, routes: dict[tuple[str, str], Any]):
        self.routes = routes
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, session, method, url, *, params=None, payload=None, headers=None, allow_missing=False):
        self.calls.append({"method": method, "url": url, "params": params, "payload": payload, "headers": headers})
        key = (method, url)
        if key not in self.routes:
            if allow_missing:
                return None
            raise ProviderError(f"unexpected {method} {url}")
        body = self.routes[key]
        return body(params, payload) if callable(body) else body

    def made(self, method: str, url: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] == method and c["url"] == url]


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch):
    monkeypatch.setattr(outlook, "_addresses_of_accounts", outlook._TTLCache())
    monkeypatch.setattr(outlook, "_signatures", outlook._TTLCache())


ME_ROUTE = ("GET", f"{GRAPH}/me")
ME_BODY = {"mail": ME_MAIL, "userPrincipalName": "sam.example@corp.example", "proxyAddresses": ["SMTP:sam@example.com", "smtp:s.example@example.com", "x500:/o=x"]}


def _provider(monkeypatch, routes) -> tuple[outlook.MicrosoftOutlookProvider, FakeGraph]:
    provider = outlook.MicrosoftOutlookProvider()
    fake = FakeGraph({ME_ROUTE: ME_BODY, **routes})
    monkeypatch.setattr(provider, "_call", fake)
    return provider, fake


def _session() -> ProviderSession:
    return ProviderSession(token="token", account_external_id=ME)


def _person(address: str, name: str | None = None) -> dict:
    return {"emailAddress": {"address": address, "name": name or address.split("@")[0].title()}}


def _email(
    msg_id: str = MESSAGE_ID,
    *,
    sender: str = "alice@partner.example",
    to: tuple[str, ...] = (ME_MAIL,),
    cc: tuple[str, ...] = (),
    subject: str = "Q4 budget",
    body: str = "Can you send me the figures?",
    headers: dict[str, str] | None = None,
    minutes: int = 0,
    **extra,
) -> dict:
    when = (datetime(2026, 10, 12, 9, 0, tzinfo=UTC) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
    return {
        "id": msg_id,
        "conversationId": CONVERSATION,
        "subject": subject,
        "from": _person(sender) if sender else {},
        "toRecipients": [_person(a) for a in to],
        "ccRecipients": [_person(a) for a in cc],
        "receivedDateTime": when,
        "sentDateTime": when,
        "uniqueBody": {"contentType": "text", "content": body},
        "bodyPreview": body[:50],
        "hasAttachments": False,
        "isDraft": False,
        "internetMessageHeaders": [{"name": k, "value": v} for k, v in (headers or {}).items()],
        **extra,
    }


def _notified(msg_id: str = MESSAGE_ID) -> InboundNotification:
    return InboundNotification("s1", "c1", None, {"resource": f"Users/{ME}/Messages/{msg_id}", "changeType": "created"})


READ_URL = f"{GRAPH}/me/messages/{MESSAGE_ID}"


def test_the_provider_is_registered_as_a_mailbox_on_the_microsoft_integration():
    provider = get_provider("microsoft_outlook")
    assert provider.integration_provider == "microsoft" and provider.kind == "mailbox"
    assert provider.capabilities.triage and not provider.capabilities.channel
    assert not provider.capabilities.conversations and provider.chats_covers == "inbox"


async def test_an_email_to_the_account_alone_is_a_direct_message_read_back_with_immutable_ids(monkeypatch):
    provider, fake = _provider(monkeypatch, {("GET", READ_URL): _email()})
    event = await provider.event_from_notification(_session(), _notified())
    assert event is not None
    assert (event.conversation_type, event.conversation_id, event.thread_id) == ("direct", CONVERSATION, CONVERSATION)
    assert event.mentions_account and not event.from_account and not event.from_application
    assert event.sender_email == "alice@partner.example" and event.sender_id == "alice@partner.example"
    assert event.conversation_name == "Q4 budget"
    assert event.text == "Subject: Q4 budget\n\nCan you send me the figures?"
    assert event.route == ROUTE
    (read,) = fake.made("GET", READ_URL)
    assert 'IdType="ImmutableId"' in read["headers"]["Prefer"] and "body-content-type" in read["headers"]["Prefer"]


@pytest.mark.parametrize(
    ("to", "cc", "kind", "addressed", "reply_all"),
    [
        ((ME_MAIL, "bob@example.com"), (), "group", True, True),
        ((ME_MAIL,), ("boss@example.com",), "group", True, True),
        (("bob@example.com",), (ME_MAIL,), "group", False, True),
        # The account's alias is the account: still an email to it alone.
        (("S.Example@example.com",), (), "direct", True, False),
        # Received through a list or in Bcc: not addressed, nobody else to answer.
        (("all@example.com",), (), "group", False, True),
    ],
)
async def test_the_recipients_say_the_kind_of_conversation_and_whether_the_account_is_addressed(
    monkeypatch, to, cc, kind, addressed, reply_all
):
    provider, _ = _provider(monkeypatch, {("GET", READ_URL): _email(to=to, cc=cc)})
    event = await provider.event_from_notification(_session(), _notified())
    assert (event.conversation_type, event.mentions_account, event.route["reply_all"]) == (kind, addressed, reply_all)


@pytest.mark.parametrize(
    ("sender", "to", "cc", "recipients"),
    [
        ("alice@partner.example", (ME_MAIL,), (), 0),
        # The sender chose fifty people in Cc: a reply-all reaches every one of them.
        ("alice@partner.example", (ME_MAIL,), tuple(f"p{i}@elsewhere.example" for i in range(50)), 50),
        # Each address once, whatever its case; the account and its aliases are not recipients.
        ("alice@partner.example", (ME_MAIL, "Bob@example.com"), ("bob@example.com", "s.example@example.com"), 1),
        # The sender copying themselves is answered as the sender.
        ("alice@partner.example", (ME_MAIL,), ("Alice@Partner.example",), 0),
    ],
)
async def test_an_email_says_how_many_people_a_reply_reaches_besides_its_sender(monkeypatch, sender, to, cc, recipients):
    """What the account bounds before a reply-all goes out."""
    provider, _ = _provider(monkeypatch, {("GET", READ_URL): _email(sender=sender, to=to, cc=cc)})
    event = await provider.event_from_notification(_session(), _notified())
    assert event.reply_recipients == recipients


@pytest.mark.parametrize(
    ("headers", "automated"),
    [
        ({"Auto-Submitted": "auto-replied"}, True),
        ({"Auto-Submitted": "auto-generated"}, True),
        ({"auto-submitted": "No"}, False),
        ({"Precedence": "bulk"}, True),
        ({"Precedence": "list"}, True),
        ({"List-Unsubscribe": "<mailto:leave@news.example>"}, True),
        ({"List-Id": "<team.lists.example>"}, True),
        ({"X-Mailer": "Outlook"}, False),
    ],
)
async def test_automated_mail_is_an_applications_as_rfc_3834_asks(monkeypatch, headers, automated):
    provider, _ = _provider(monkeypatch, {("GET", READ_URL): _email(headers=headers)})
    event = await provider.event_from_notification(_session(), _notified())
    assert event.from_application is automated


async def test_an_email_with_no_sender_is_an_applications(monkeypatch):
    provider, _ = _provider(monkeypatch, {("GET", READ_URL): _email(sender="")})
    event = await provider.event_from_notification(_session(), _notified())
    assert event.from_application


async def test_an_email_another_agent_account_sent_says_so(monkeypatch):
    provider, _ = _provider(monkeypatch, {("GET", READ_URL): _email(headers={"x-xtm-one-agent-account": "reply"})})
    event = await provider.event_from_notification(_session(), _notified())
    assert event.from_agent_account and not event.from_application


async def test_the_accounts_own_email_from_an_alias_is_its_own(monkeypatch):
    provider, _ = _provider(monkeypatch, {("GET", READ_URL): _email(sender="s.example@example.com", to=("bob@example.com",))})
    event = await provider.event_from_notification(_session(), _notified())
    assert event.from_account


@pytest.mark.parametrize(
    "extra", [{"isDraft": True}, {"@odata.type": "#microsoft.graph.eventMessageRequest"}], ids=["draft", "invitation"]
)
async def test_a_draft_or_a_calendar_invitation_is_not_heard(monkeypatch, extra):
    provider, _ = _provider(monkeypatch, {("GET", READ_URL): _email(**extra)})
    assert await provider.event_from_notification(_session(), _notified()) is None


RESPONSE = "#microsoft.graph.eventMessageResponse"
CANCELLATION = "#microsoft.graph.eventMessage"
COUNTER = "I cannot make it. Could we do Thursday 10:00 or Friday 14:00 instead?"


@pytest.mark.parametrize(
    ("odata_type", "subject", "extra"),
    [
        (RESPONSE, "Accepted: Weekly sync", {"responseType": "accepted"}),
        (RESPONSE, "Tentative: Weekly sync", {"responseType": "tentativelyAccepted"}),
        (RESPONSE, "Declined: Weekly sync", {"responseType": "declined"}),
        (CANCELLATION, "Canceled: Weekly sync", {"meetingMessageType": "meetingCancelled"}),
    ],
    ids=["accepted", "tentative", "declined", "cancelled"],
)
async def test_a_meeting_response_or_cancellation_with_a_written_body_is_heard(monkeypatch, odata_type, subject, extra):
    """A counterpart's comment on a response (a counter-proposal) reaches the rules like any email."""
    msg = _email(subject=subject, body=COUNTER, **{"@odata.type": odata_type, **extra})
    provider, _ = _provider(monkeypatch, {("GET", READ_URL): msg})
    event = await provider.event_from_notification(_session(), _notified())
    assert event is not None
    assert event.text == f"Subject: {subject}\n\n{COUNTER}"
    assert event.sender_email == "alice@partner.example" and not event.from_application


@pytest.mark.parametrize(
    ("odata_type", "subject"),
    [
        (RESPONSE, "Accepted: Weekly sync"),
        (RESPONSE, "Tentative: Weekly sync"),
        (RESPONSE, "Declined: Weekly sync"),
        (CANCELLATION, "Canceled: Weekly sync"),
    ],
    ids=["accepted", "tentative", "declined", "cancelled"],
)
async def test_a_bare_meeting_response_or_cancellation_is_not_heard(monkeypatch, odata_type, subject):
    """With no word written by its sender, a response is the calendar's bookkeeping."""
    msg = _email(subject=subject, body="", **{"@odata.type": odata_type, "bodyPreview": ""})
    provider, _ = _provider(monkeypatch, {("GET", READ_URL): msg})
    assert await provider.event_from_notification(_session(), _notified()) is None


BANNER = (
    "[You don't often get email from tim@partner.example. Learn why this is important at "
    "https://aka.ms/LearnAboutSenderIdentification ]"
)
BANNER_HTML = (
    '<html><body><table><tr><td><div>Some people who received this message don’t often get email from '
    'tim@partner.example. <a href="https://aka.ms/LearnAboutSenderIdentification">Learn why this is important</a>'
    "</div></td></tr></table>{comment}</body></html>"
)


@pytest.mark.parametrize("html", [False, True], ids=["text", "html"])
async def test_a_meeting_response_holding_only_the_sender_identification_tip_is_not_heard(monkeypatch, html):
    """Exchange's first-contact safety tip is not a word of the sender."""
    msg = _email(subject="Accepted: Weekly sync", body=BANNER, **{"@odata.type": RESPONSE, "bodyPreview": BANNER})
    if html:
        msg["uniqueBody"] = {"contentType": "html", "content": BANNER_HTML.format(comment="")}
    provider, _ = _provider(monkeypatch, {("GET", READ_URL): msg})
    assert await provider.event_from_notification(_session(), _notified()) is None


@pytest.mark.parametrize("html", [False, True], ids=["text", "html"])
async def test_a_meeting_response_with_the_tip_and_a_comment_is_heard_with_the_comment(monkeypatch, html):
    msg = _email(subject="Declined: Weekly sync", body=f"{BANNER}\n\n{COUNTER}", **{"@odata.type": RESPONSE})
    if html:
        msg["uniqueBody"] = {"contentType": "html", "content": BANNER_HTML.format(comment=f"<p>{COUNTER}</p>")}
    provider, _ = _provider(monkeypatch, {("GET", READ_URL): msg})
    event = await provider.event_from_notification(_session(), _notified())
    assert event is not None
    assert event.text == f"Subject: Declined: Weekly sync\n\n{COUNTER}"


async def test_an_ordinary_email_keeps_the_sender_identification_tip(monkeypatch):
    msg = _email(subject="Intro", body=f"{BANNER}\n\nHello")
    provider, _ = _provider(monkeypatch, {("GET", READ_URL): msg})
    event = await provider.event_from_notification(_session(), _notified())
    assert event is not None and event.text == f"Subject: Intro\n\n{BANNER}\n\nHello"


async def test_a_meeting_invitation_with_a_body_is_still_not_heard(monkeypatch):
    msg = _email(subject="Weekly sync", body="Agenda attached", **{"@odata.type": "#microsoft.graph.eventMessageRequest"})
    provider, _ = _provider(monkeypatch, {("GET", READ_URL): msg})
    assert await provider.event_from_notification(_session(), _notified()) is None


async def test_a_response_sent_on_behalf_of_someone_names_the_delegate_first(monkeypatch):
    """"Charles on behalf of Helen": Charles (Graph ``sender``) wrote it; Helen (``from``) still matches a rule."""
    msg = _email(sender="helen@partner.example", subject="Declined: Weekly sync", body=COUNTER, **{"@odata.type": RESPONSE})
    msg["sender"] = _person("charles@partner.example", "Charles")
    provider, _ = _provider(monkeypatch, {("GET", READ_URL): msg})
    event = await provider.event_from_notification(_session(), _notified())
    assert (event.sender_email, event.sender_id, event.sender_name) == (
        "charles@partner.example", "charles@partner.example", "Charles"
    )
    assert event.sender_addresses == ("charles@partner.example", "helen@partner.example")


async def test_an_ordinary_email_on_behalf_of_someone_keeps_the_principal_first(monkeypatch):
    msg = _email(sender="helen@partner.example")
    msg["sender"] = _person("charles@partner.example")
    provider, _ = _provider(monkeypatch, {("GET", READ_URL): msg})
    event = await provider.event_from_notification(_session(), _notified())
    assert event.sender_email == "helen@partner.example"


async def test_a_polled_meeting_response_with_a_body_is_heard(monkeypatch):
    msg = _email(subject="Declined: Weekly sync", body=COUNTER, minutes=1, **{"@odata.type": RESPONSE})
    provider, _ = _provider(monkeypatch, {("GET", f"{GRAPH}/me/{outlook._INBOX}"): {"value": [msg]}})
    events, _cursor = await provider.poll(_session(), ListeningPlan(chats=True), "2026-10-12T09:00:00+00:00")
    assert [e.text for e in events] == [f"Subject: Declined: Weekly sync\n\n{COUNTER}"]


async def test_attachments_are_named_for_the_agent(monkeypatch):
    provider, _ = _provider(
        monkeypatch,
        {
            ("GET", READ_URL): _email(hasAttachments=True),
            ("GET", f"{READ_URL}/attachments"): {"value": [
                {"name": "figures.xlsx", "isInline": False}, {"name": "logo.png", "isInline": True},
            ]},
        },
    )
    event = await provider.event_from_notification(_session(), _notified())
    assert event.text.endswith("\n\nAttachments: figures.xlsx")


async def test_a_notification_of_a_change_or_of_something_else_is_not_read(monkeypatch):
    provider, fake = _provider(monkeypatch, {})
    changed = InboundNotification("s", "c", None, {"resource": f"Users/{ME}/Messages/{MESSAGE_ID}", "changeType": "updated"})
    other = InboundNotification("s", "c", None, {"resource": f"Users/{ME}/Events/x", "changeType": "created"})
    assert await provider.event_from_notification(_session(), changed) is None
    assert await provider.event_from_notification(_session(), other) is None
    assert fake.calls == []


async def test_an_unsafe_id_never_reaches_a_graph_url(monkeypatch):
    provider, fake = _provider(monkeypatch, {})
    forged = InboundNotification("s", "c", None, {"resource": "Users/x/Messages('..%2Fme')", "changeType": "created"})
    with pytest.raises(ProviderError):
        await provider.event_from_notification(_session(), forged)
    assert fake.calls == []


async def test_the_inbox_is_subscribed_to_when_the_rules_reach_emails(monkeypatch):
    provider, _ = _provider(monkeypatch, {})
    (desired,) = await provider.desired_subscriptions(_session(), ListeningPlan(chats=True))
    assert desired.resource == f"/users/{ME}/mailFolders('inbox')/messages"
    assert (desired.change_type, desired.covers, desired.permission) == ("created", "inbox", "Mail.Read")
    assert await provider.desired_subscriptions(_session(), ListeningPlan(all_channels=True)) == []


async def test_a_subscription_asks_for_immutable_ids_and_the_outlook_lifetime(monkeypatch):
    provider, fake = _provider(
        monkeypatch, {("POST", f"{GRAPH}/subscriptions"): {"id": "sub-1", "expirationDateTime": "2026-10-19T09:00:00Z"}}
    )
    await provider.create_subscription(
        _session(), f"/users/{ME}/mailFolders('inbox')/messages",
        notification_url="https://p/x", lifecycle_url="https://p/x?lifecycle=1", client_state="secret",
    )
    (call,) = fake.made("POST", f"{GRAPH}/subscriptions")
    assert call["headers"] == {"Prefer": 'IdType="ImmutableId"'}
    expires = datetime.fromisoformat(call["payload"]["expirationDateTime"].replace("Z", "+00:00"))
    assert timedelta(days=6) < expires - datetime.now(UTC) <= timedelta(minutes=outlook.SUBSCRIPTION_MINUTES)


def _thread(params, _payload):
    assert params["$filter"] == "conversationId eq 'AAQkADAwATM0MDAAMS0xMDJhLTAwAhAAsampleconversation='"
    return {"value": [
        _email("later", body="after", minutes=30),
        _email("mine", sender=ME_MAIL, to=("alice@partner.example",), body="Here you go", minutes=-10),
        _email("first", body="Hi Sam", minutes=-20),
        _email("draft", sender=ME_MAIL, body="unsent", minutes=-5, isDraft=True),
        _email(MESSAGE_ID),
    ]}


def _event(**overrides) -> AccountEvent:
    values = {
        "provider": "microsoft_outlook", "conversation_type": "direct", "conversation_id": CONVERSATION,
        "thread_id": CONVERSATION, "message_id": MESSAGE_ID, "sender_id": "alice@partner.example",
        "sender_name": "Alice", "text": "Can you send me the figures?",
        "created_at": datetime(2026, 10, 12, 9, 0, tzinfo=UTC), "route": ROUTE,
    }
    values.update(overrides)
    return AccountEvent(**values)


async def test_the_context_is_the_thread_before_the_email_oldest_first(monkeypatch):
    provider, _ = _provider(monkeypatch, {("GET", f"{GRAPH}/me/messages"): _thread})
    context = await provider.fetch_context(_session(), _event(), 10)
    assert [(m.message_id, m.from_account) for m in context] == [("first", False), ("mine", True)]
    assert context[1].text == "Subject: Q4 budget\n\nHere you go"
    assert await provider.account_messages_since(_session(), _event()) == []
    later = _event(created_at=datetime(2026, 10, 12, 8, 0, tzinfo=UTC))
    assert await provider.account_messages_since(_session(), later) == ["mine"]


def _long_thread(count: int, newest_from: str):
    """A thread of *count* emails over Graph pages of 25, newest on the last page, its last one by *newest_from*."""
    emails = [_email(f"m{i:03d}", body=f"email {i}", minutes=i - count) for i in range(count - 1)]
    emails.append(_email("newest", sender=newest_from, to=("alice@partner.example",), body="Mine", minutes=5))
    pages = [emails[i: i + 25] for i in range(0, count, 25)]
    links = [f"{GRAPH}/me/messages?$skiptoken=page{n}" for n in range(1, len(pages))]

    def page(n):
        def body(_params, _payload):
            out = {"value": pages[n]}
            if n < len(links):
                out["@odata.nextLink"] = links[n]
            return out
        return body

    routes = {("GET", f"{GRAPH}/me/messages"): page(0)}
    routes.update({("GET", link): page(n + 1) for n, link in enumerate(links)})
    return routes


async def test_a_long_thread_is_read_to_its_last_page_and_its_newest_emails_kept(monkeypatch):
    provider, fake = _provider(monkeypatch, _long_thread(60, ME_MAIL))
    event = _event(message_id="m058", created_at=datetime(2026, 10, 12, 9, 10, tzinfo=UTC))
    context = await provider.fetch_context(_session(), event, 3)
    assert [m.message_id for m in context] == ["m056", "m057", "newest"]
    assert context[-1].from_account is True
    (first,) = fake.made("GET", f"{GRAPH}/me/messages")
    assert first["params"]["$select"] == outlook._CONTEXT_SELECT
    assert first["params"]["$top"] == str(outlook.CONTEXT_PAGE)
    assert len([c for c in fake.calls if c["url"].startswith(f"{GRAPH}/me/messages")]) == 3


async def test_the_owner_takeover_check_reads_the_whole_thread_headers_only(monkeypatch):
    provider, fake = _provider(monkeypatch, _long_thread(60, ME_MAIL))
    since = _event(created_at=datetime(2026, 10, 12, 9, 0, tzinfo=UTC))
    assert await provider.account_messages_since(_session(), since) == ["newest"]
    (first,) = fake.made("GET", f"{GRAPH}/me/messages")
    assert first["params"]["$select"] == "id,from,sender,sentDateTime,isDraft"
    assert first["params"]["$top"] == str(outlook.HEADER_PAGE)
    assert len([c for c in fake.calls if c["url"].startswith(f"{GRAPH}/me/messages")]) == 3


async def test_a_thread_read_stops_at_its_bound(monkeypatch):
    monkeypatch.setattr(outlook, "THREAD_READ", 30)
    provider, fake = _provider(monkeypatch, _long_thread(60, ME_MAIL))
    await provider.account_messages_since(_session(), _event())
    assert len([c for c in fake.calls if c["url"].startswith(f"{GRAPH}/me/messages")]) == 2


async def test_a_quote_in_a_conversation_id_stays_inside_the_filter(monkeypatch):
    provider, fake = _provider(monkeypatch, {("GET", f"{GRAPH}/me/messages"): {"value": []}})
    await provider.fetch_context(_session(), _event(conversation_id="a' or 1 eq 1 or '"), 5)
    assert fake.calls[-1]["params"]["$filter"] == "conversationId eq 'a'' or 1 eq 1 or '''"


DRAFT_HTML = '<html><body><div id="appendonsend"></div><hr style="display:inline-block"><div id="divRplyFwdMsg">quoted</div></body></html>'
SIGNATURE = '<div id="Signature"><p>Sam Example - CEO</p></div>'
SENT_ITEMS = ("GET", f"{GRAPH}/me/mailFolders/sentitems/messages")


async def test_a_reply_keeps_the_thread_carries_the_signature_and_the_agent_header(monkeypatch):
    draft_url = f"{GRAPH}/me/messages/{DRAFT_ID}"
    provider, fake = _provider(
        monkeypatch,
        {
            ("POST", f"{READ_URL}/createReplyAll"): {"id": DRAFT_ID, "body": {"contentType": "html", "content": DRAFT_HTML}},
            SENT_ITEMS: {"value": [{"body": {"content": "<p>no signature</p>"}}, {"body": {"content": f"<p>x</p>{SIGNATURE}</body>"}}]},
            ("PATCH", draft_url): {},
            ("POST", f"{draft_url}/send"): {},
        },
    )
    sent = await provider.send_reply(_session(), {**ROUTE, "reply_all": True}, "**Sure**, attached.")
    assert sent == DRAFT_ID
    (created,) = fake.made("POST", f"{READ_URL}/createReplyAll")
    assert created["payload"] == {"message": {"internetMessageHeaders": [{"name": "X-XTM-One-Agent-Account", "value": "reply"}]}}
    (patched,) = fake.made("PATCH", draft_url)
    body = patched["payload"]["body"]["content"]
    assert patched["payload"]["body"]["contentType"] == "HTML"
    assert body.index("<strong>Sure</strong>") < body.index("Sam Example - CEO") < body.index("<hr") < body.index("quoted")
    assert fake.made("POST", f"{draft_url}/send")


@pytest.mark.parametrize(
    ("chosen", "shown", "hidden", "reads_sent_mail"),
    [
        (AccountSignature("given", "<html><body><p>Sam - live banner</p></body></html>"), "Sam - live banner", "CEO", False),
        (AccountSignature("none"), None, "CEO", False),
        (AccountSignature("own"), "Sam Example - CEO", "live banner", True),
        # A signature service answering something empty: the account's own, never none.
        (AccountSignature("given", "<p> </p>"), "Sam Example - CEO", "live banner", True),
    ],
)
async def test_a_reply_carries_the_signature_its_owner_chose(monkeypatch, chosen, shown, hidden, reads_sent_mail):
    """A signature integration's (above the thread, in Outlook's block), none, or the sent mail's."""
    draft_url = f"{GRAPH}/me/messages/{DRAFT_ID}"
    provider, fake = _provider(
        monkeypatch,
        {
            ("POST", f"{READ_URL}/createReply"): {"id": DRAFT_ID, "body": {"content": DRAFT_HTML}},
            SENT_ITEMS: {"value": [{"body": {"content": f"<p>x</p>{SIGNATURE}</body>"}}]},
            ("PATCH", draft_url): {},
            ("POST", f"{draft_url}/send"): {},
        },
    )
    session = _session()
    session.signature = chosen
    await provider.send_reply(session, ROUTE, "Hello")
    body = fake.made("PATCH", draft_url)[0]["payload"]["body"]["content"]
    if shown is None:
        assert "Signature" not in body
    else:
        assert body.index("Hello") < body.index('<div id="Signature">') < body.index(shown) < body.index("<hr")
    assert hidden not in body
    assert bool(fake.made(*SENT_ITEMS)) is reads_sent_mail


async def test_the_signature_images_of_a_reply_are_attached_inline_once(monkeypatch):
    """A signature service's image links, which Outlook blocks, go out as inline images."""
    from app.integrations.file_delivery import FetchedFile
    from app.integrations.providers.microsoft import _signature_images as images

    logo = "https://storage.example.com/sig/logo.png"
    fetched: list[str] = []

    async def _fetch(url: str, **_kwargs):
        fetched.append(url)
        return FetchedFile(data=b"png", content_type="image/png", filename="")

    monkeypatch.setattr("app.integrations.providers.social_media.fetch_public_media", _fetch)
    draft_url = f"{GRAPH}/me/messages/{DRAFT_ID}"
    provider, fake = _provider(
        monkeypatch,
        {
            ("POST", f"{READ_URL}/createReply"): {"id": DRAFT_ID, "body": {"content": DRAFT_HTML}},
            ("POST", f"{draft_url}/attachments"): {"id": "a"},
            ("PATCH", draft_url): {},
            ("POST", f"{draft_url}/send"): {},
        },
    )
    session = _session()
    session.signature = AccountSignature("given", f'<p><img src="{logo}"><img src="{logo}"> Sam</p>')
    await provider.send_reply(session, ROUTE, "Hello")
    (attached,) = fake.made("POST", f"{draft_url}/attachments")
    assert attached["payload"]["isInline"] is True
    assert attached["payload"]["contentId"] == images.content_id(logo)
    body = fake.made("PATCH", draft_url)[0]["payload"]["body"]["content"]
    assert logo not in body and body.count(f"cid:{images.content_id(logo)}") == 2
    assert fetched == [logo]


async def test_a_mailbox_refusing_the_header_gets_the_reply_without_it(monkeypatch):
    attempts = []

    def _create(_params, payload):
        attempts.append(payload)
        if payload:
            raise ProviderError("Microsoft Graph answered 400", kind="error")
        return {"id": DRAFT_ID, "body": {"content": DRAFT_HTML}}

    draft_url = f"{GRAPH}/me/messages/{DRAFT_ID}"
    provider, _fake = _provider(
        monkeypatch,
        {("POST", f"{READ_URL}/createReply"): _create, SENT_ITEMS: {"value": []}, ("PATCH", draft_url): {}, ("POST", f"{draft_url}/send"): {}},
    )
    assert await provider.send_reply(_session(), ROUTE, "Hi") == DRAFT_ID
    assert len(attempts) == 2 and attempts[1] == {}


async def test_a_reply_that_cannot_be_sent_leaves_no_draft_behind(monkeypatch):
    draft_url = f"{GRAPH}/me/messages/{DRAFT_ID}"

    def _refuse(_params, _payload):
        raise ProviderError("Microsoft Graph answered 403", kind="refused")

    provider, fake = _provider(
        monkeypatch,
        {
            ("POST", f"{READ_URL}/createReply"): {"id": DRAFT_ID, "body": {"content": DRAFT_HTML}},
            SENT_ITEMS: {"value": []},
            ("PATCH", draft_url): {},
            ("POST", f"{draft_url}/send"): _refuse,
            ("DELETE", draft_url): {},
        },
    )
    with pytest.raises(ProviderError):
        await provider.send_reply(_session(), ROUTE, "Hi")
    assert fake.made("DELETE", draft_url)


async def test_the_mailbox_files_a_flag_read_and_a_category_once(monkeypatch):
    provider, fake = _provider(
        monkeypatch, {("GET", READ_URL): {"categories": ["Red", "needs your attention"]}, ("PATCH", READ_URL): {}}
    )
    await provider.mark_message(_session(), ROUTE, MessageMark(flag=True, category="Needs your attention"))
    await provider.mark_message(_session(), ROUTE, MessageMark(read=True, category="Answered by AI"))
    first, second = fake.made("PATCH", READ_URL)
    assert first["payload"] == {"flag": {"flagStatus": "flagged"}}
    assert second["payload"] == {"isRead": True, "categories": ["Red", "needs your attention", "Answered by AI"]}
    await provider.mark_message(_session(), ROUTE, MessageMark())
    await provider.mark_message(_session(), {"kind": "chat"}, MessageMark(read=True))
    assert len(fake.made("PATCH", READ_URL)) == 2


async def test_a_poll_starts_now_then_reads_the_inbox_oldest_first(monkeypatch):
    def _inbox(params, _payload):
        assert params["$filter"] == "receivedDateTime ge 2026-10-12T08:59:00Z"
        assert params["$orderby"] == "receivedDateTime asc"
        return {"value": [_email("b", minutes=5), _email("a", minutes=1)]}

    provider, _ = _provider(monkeypatch, {("GET", f"{GRAPH}/me/mailFolders('inbox')/messages"): _inbox})
    plan = ListeningPlan(chats=True)
    events, cursor = await provider.poll(_session(), plan, None)
    assert events == [] and cursor
    events, cursor = await provider.poll(_session(), plan, "2026-10-12T08:59:00+00:00")
    assert [e.message_id for e in events] == ["a", "b"] and cursor.startswith("2026-10-12T09:05")
    assert await provider.poll(_session(), ListeningPlan(), "x") == ([], "x")
