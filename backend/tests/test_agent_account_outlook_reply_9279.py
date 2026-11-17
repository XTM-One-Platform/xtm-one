"""An Outlook agent account's reply goes out as the mail tools' sends do (#9279).

One signature pipeline (images inline under matching content ids, no bottom
margin under the sign-off), the mail tools' mailbox-scoped send guard (a tool
send then an account reply in one conversation within the window sends one
message; a newer message from someone else releases it) and the recipient
roles of a reply-all to an email the account's owner wrote. Graph and Redis
are fakes; nothing here reaches the network.
"""

from __future__ import annotations

import re
import time
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.integrations.file_delivery import FetchedFile
from app.integrations.providers.microsoft import _signature_images as images
from app.services import mail_send_guard
from app.services.agent_accounts import pipeline
from app.services.agent_accounts.providers import (
    AccountSignature,
    DuplicateReplyError,
    ProviderError,
    ProviderSession,
)
from app.services.agent_accounts.providers import microsoft_outlook as outlook
from app.services.email_signatures import EmailSignature

GRAPH = outlook.GRAPH
ME_MAIL = "assistant@example.com"
OWNER = "owner@example.com"
COUNTERPART = "guest@partner.example"
MAILBOX = str(uuid.uuid4())
MESSAGE_ID = "AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0Aanswered" + "x" * 30
DRAFT_ID = "AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0Adraft" + "y" * 30
CONVERSATION = "AAQkADAwATM0MDAAMS0xMDJhLTAwAhAAconversation="
READ_URL = f"{GRAPH}/me/messages/{MESSAGE_ID}"
DRAFT_URL = f"{GRAPH}/me/messages/{DRAFT_ID}"
SENT_ITEMS = ("GET", f"{GRAPH}/me/mailFolders/sentitems/messages")
MESSAGES = ("GET", f"{GRAPH}/me/messages")
DRAFT_HTML = '<html><body><div id="appendonsend"></div><hr style="display:inline-block"><div id="divRplyFwdMsg">quoted</div></body></html>'
LOGO = "https://storage.example.com/sig/logo.png"
ICONS = [f"https://storage.example.com/sig/icon-{n}.png" for n in range(5)]
SIGNATURE_HTML = (
    "<table><tr><td>"
    + "".join(f'<a href="https://x.example/{n}"><img src="{u}"></a>' for n, u in enumerate([LOGO, *ICONS]))
    + "</td></tr></table><p>Assistant</p>"
)


def _person(address: str) -> dict:
    return {"emailAddress": {"address": address, "name": address.split("@")[0]}}


def _iso(epoch: float) -> str:
    return mail_send_guard.iso_instant(epoch)


def _sent(at: float) -> dict:
    """A Sent Items page holding one reply in the conversation, sent at *at*."""
    message = {
        "id": "s1",
        "conversationId": CONVERSATION,
        "subject": "RE: Invitation",
        "sentDateTime": _iso(at),
        "toRecipients": [_person(COUNTERPART)],
    }
    return {"value": [message]}


class _Redis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, *keys):
        return sum(1 for key in keys if self.values.pop(key, None) is not None)


class FakeGraph:
    def __init__(self, routes: dict[tuple[str, str], Any]):
        self.routes = routes
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, session, method, url, *, params=None, payload=None, headers=None, allow_missing=False):
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


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(outlook, "_addresses_of_accounts", outlook._TTLCache())
    monkeypatch.setattr(outlook, "_signatures", outlook._TTLCache())
    store = _Redis()
    monkeypatch.setattr(mail_send_guard, "redis_client", AsyncMock(return_value=store))

    async def _fetch(url: str, **_kwargs):
        return FetchedFile(data=b"png", content_type="image/png", filename="")

    monkeypatch.setattr("app.integrations.providers.social_media.fetch_public_media", _fetch)
    return store


def _answered(*, sender: str = OWNER, to: tuple[str, ...] = (COUNTERPART,), cc: tuple[str, ...] = (ME_MAIL,)) -> dict:
    return {
        "id": MESSAGE_ID,
        "conversationId": CONVERSATION,
        "subject": "Invitation",
        "from": _person(sender),
        "toRecipients": [_person(a) for a in to],
        "ccRecipients": [_person(a) for a in cc],
    }


def _provider(monkeypatch, *, answered: dict | None = None, draft: dict | None = None, **routes):
    provider = outlook.MicrosoftOutlookProvider()
    table: dict[tuple[str, str], Any] = {
        ("GET", f"{GRAPH}/me"): {"mail": ME_MAIL, "userPrincipalName": ME_MAIL},
        ("GET", READ_URL): answered or _answered(),
        ("POST", f"{READ_URL}/createReplyAll"): draft
        or {
            "id": DRAFT_ID,
            "body": {"contentType": "html", "content": DRAFT_HTML},
            "toRecipients": [_person(OWNER), _person(COUNTERPART)],
            "ccRecipients": [],
        },
        ("POST", f"{READ_URL}/createReply"): {"id": DRAFT_ID, "body": {"contentType": "html", "content": DRAFT_HTML}},
        SENT_ITEMS: {"value": []},
        MESSAGES: {"value": []},
        ("POST", f"{DRAFT_URL}/attachments"): {"id": "att"},
        ("PATCH", DRAFT_URL): {},
        ("POST", f"{DRAFT_URL}/send"): {},
        ("DELETE", DRAFT_URL): {},
    }
    table.update(routes.get("extra", {}))
    fake = FakeGraph(table)
    monkeypatch.setattr(provider, "_call", fake)
    return provider, fake


def _session(*, signature: AccountSignature | None = None, owner: str | None = OWNER) -> ProviderSession:
    session = ProviderSession(token="t", account_external_id="me-id", integration_id=MAILBOX, owner_email=owner)
    session.signature = signature or AccountSignature("given", SIGNATURE_HTML)
    return session


ROUTE_ALL = {"kind": "mail", "message_id": MESSAGE_ID, "reply_all": True}
BODIES = {
    "markdown": "Thank you for the invitation.\n\nKind regards,",
    "html": "<p>Thank you for the invitation.</p><p>Kind regards,</p>",
    "list": "Points:\n\n- one\n- two",
    "french": "Merci pour l'invitation.\n\nBien cordialement,",
}


def _own_part(body: str) -> str:
    return body[: body.index('<div id="Signature">')]


@pytest.mark.parametrize("kind", list(BODIES))
async def test_a_reply_is_signed_once_with_every_image_inline_and_no_margin_above_it(monkeypatch, kind):
    provider, fake = _provider(monkeypatch)
    await provider.send_reply(_session(), ROUTE_ALL, BODIES[kind])
    body = fake.made("PATCH", DRAFT_URL)[0]["payload"]["body"]["content"]
    # One signature block, above the quoted thread.
    assert body.count('<div id="Signature">') == 1
    assert body.index('<div id="Signature">') < body.index("<hr") < body.index("quoted")
    # Six images, each a cid: whose inline attachment carries the same content id.
    cids = re.findall(r'src="cid:([^"]+)"', body)
    attached = [c["payload"]["contentId"] for c in fake.made("POST", f"{DRAFT_URL}/attachments")]
    assert len(cids) == 6 and sorted(cids) == sorted(attached) and len(set(attached)) == 6
    assert all(c["payload"]["isInline"] for c in fake.made("POST", f"{DRAFT_URL}/attachments"))
    assert LOGO not in body
    # The last own block keeps no bottom margin; the signature follows it directly.
    own = _own_part(body)
    last_open = list(re.finditer(r"<(p|ul|ol)\b[^>]*>", own))[-1].group(0)
    assert "margin-bottom:0" in last_open and "12pt" not in last_open.split("margin-top:0")[-1]
    assert re.search(r"</(?:p|ul|ol)>\s*</div>\s*$", own)


async def test_the_owner_who_copied_the_account_stays_in_cc_on_a_reply_all(monkeypatch):
    provider, fake = _provider(monkeypatch)
    await provider.send_reply(_session(), ROUTE_ALL, "Noted.")
    payload = fake.made("PATCH", DRAFT_URL)[0]["payload"]
    assert [r["emailAddress"]["address"] for r in payload["toRecipients"]] == [COUNTERPART]
    assert [r["emailAddress"]["address"] for r in payload["ccRecipients"]] == [OWNER]


@pytest.mark.parametrize(
    ("answered", "owner"),
    [
        (_answered(sender=COUNTERPART, to=(ME_MAIL,), cc=(OWNER,)), OWNER),  # another sender: Graph's roles
        (_answered(sender=OWNER, to=(ME_MAIL,), cc=()), OWNER),  # the owner wrote to the account alone
        (_answered(), None),  # no owner known
    ],
)
async def test_recipient_roles_are_left_to_graph_otherwise(monkeypatch, answered, owner):
    provider, fake = _provider(monkeypatch, answered=answered)
    await provider.send_reply(_session(owner=owner), ROUTE_ALL, "Noted.")
    payload = fake.made("PATCH", DRAFT_URL)[0]["payload"]
    assert "toRecipients" not in payload and "ccRecipients" not in payload


async def test_a_tool_send_then_an_account_reply_in_the_window_sends_one_message(monkeypatch):
    # The mail tools of the same mailbox recorded a send in the conversation.
    tool_guard = mail_send_guard.SendGuard(f"mailbox:{MAILBOX}", mail_send_guard.thread_key(CONVERSATION), window=300)
    assert await tool_guard.acquire()
    await tool_guard.sent(subject="RE: Invitation", recipients=[COUNTERPART, OWNER], conversation_id=CONVERSATION)
    provider, fake = _provider(monkeypatch)
    with pytest.raises(DuplicateReplyError) as refused:
        await provider.send_reply(_session(), ROUTE_ALL, "Thanks.")
    assert "Not sent" in str(refused.value) and "force_duplicate" not in str(refused.value)
    assert not fake.made("POST", f"{READ_URL}/createReplyAll")
    assert not fake.made("POST", f"{DRAFT_URL}/send")


async def test_a_send_found_in_sent_items_refuses_the_reply_when_no_record_exists(monkeypatch):
    now = time.time()
    sent = _sent(now - 7)
    provider, fake = _provider(monkeypatch, extra={SENT_ITEMS: sent})
    with pytest.raises(DuplicateReplyError):
        await provider.send_reply(_session(), ROUTE_ALL, "Thanks.")
    assert not fake.made("POST", f"{DRAFT_URL}/send")


async def test_a_newer_message_from_someone_else_releases_the_guard(monkeypatch, _isolated):
    now = time.time()
    sent = _sent(now - 60)
    newer = {"value": [{"from": _person(COUNTERPART), "receivedDateTime": _iso(now - 5), "isDraft": False}]}
    provider, fake = _provider(monkeypatch, extra={SENT_ITEMS: sent, MESSAGES: newer})
    assert await provider.send_reply(_session(), ROUTE_ALL, "Thanks.") == DRAFT_ID
    assert fake.made("POST", f"{DRAFT_URL}/send")
    # The reply is recorded: a tool send right after it is caught.
    assert any(k.startswith(f"mail_send_guard:sent:mailbox:{MAILBOX}") for k in _isolated.values)
    assert not any(k.startswith("mail_send_guard:lock:") for k in _isolated.values)


async def test_an_out_of_office_answer_since_the_earlier_send_keeps_the_refusal(monkeypatch):
    now = time.time()
    sent = _sent(now - 60)
    away = {
        "value": [
            {
                "from": _person(COUNTERPART),
                "receivedDateTime": _iso(now - 5),
                "isDraft": False,
                "subject": "Automatic reply: RE: Invitation",
                "internetMessageHeaders": [{"name": "Auto-Submitted", "value": "auto-replied"}],
            }
        ]
    }
    provider, fake = _provider(monkeypatch, extra={SENT_ITEMS: sent, MESSAGES: away})
    with pytest.raises(DuplicateReplyError):
        await provider.send_reply(_session(), ROUTE_ALL, "Thanks.")
    assert not fake.made("POST", f"{DRAFT_URL}/send")


async def test_a_reply_that_fails_gives_the_lock_back_and_records_nothing(monkeypatch, _isolated):
    def _refuse(_params, _payload):
        raise ProviderError("Microsoft Graph answered 403", kind="refused")

    provider, _fake = _provider(monkeypatch, extra={("POST", f"{DRAFT_URL}/send"): _refuse})
    with pytest.raises(ProviderError):
        await provider.send_reply(_session(), ROUTE_ALL, "Thanks.")
    assert not _isolated.values


async def test_a_session_without_its_integration_is_not_guarded(monkeypatch):
    now = time.time()
    sent = {"value": [{"id": "s1", "conversationId": CONVERSATION, "sentDateTime": _iso(now - 7)}]}
    provider, fake = _provider(monkeypatch, extra={SENT_ITEMS: sent})
    session = _session()
    session.integration_id = None
    await provider.send_reply(session, ROUTE_ALL, "Thanks.")
    assert fake.made("POST", f"{DRAFT_URL}/send")


async def test_the_own_signature_skips_a_sent_copy_whose_images_were_inline(monkeypatch):
    """A sent email signed with cid: images loses them when copied: one that kept its links wins."""
    stripped = '<p>x</p><div id="Signature"><a href="https://x.example"><img src="cid:sig-1@xtm-one"></a><p>Assistant</p></div></body>'
    linked = f'<p>y</p><div id="Signature">{SIGNATURE_HTML}</div></body>'
    provider, fake = _provider(
        monkeypatch, extra={SENT_ITEMS: {"value": [{"body": {"content": stripped}}, {"body": {"content": linked}}]}}
    )
    session = _session(signature=AccountSignature("own"))
    session.integration_id = None
    await provider.send_reply(session, ROUTE_ALL, "Thanks.")
    body = fake.made("PATCH", DRAFT_URL)[0]["payload"]["body"]["content"]
    assert len(re.findall(r'src="cid:sig-[0-9a-f]+@xtm-one"', body)) == 6
    assert images.content_id(LOGO) in body


def _own_account(integration: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        integration_id=integration.id,
        account_email=ME_MAIL,
        user_id=uuid.uuid4(),
        signature_source="own",
    )


async def test_an_account_on_its_own_signature_signs_with_its_mailboxs_signature_integration(monkeypatch):
    from app.services import email_signature_settings, email_signatures

    integration = SimpleNamespace(id=uuid.uuid4(), provider="microsoft")
    account = _own_account(integration)
    db = SimpleNamespace(get=AsyncMock(return_value=integration))
    plan = email_signatures.SignaturePlan(integration_id=str(uuid.uuid4()), integration_name="Signatures")
    plans = AsyncMock(return_value={str(integration.id): plan})
    monkeypatch.setattr(email_signature_settings, "plan_signatures", plans)
    read = AsyncMock(return_value=EmailSignature(html="<p>New</p>", reply_html="<p>Reply banner</p>"))
    monkeypatch.setattr(email_signatures, "read_signature", read)
    provider = SimpleNamespace(capabilities=SimpleNamespace(signature=True))
    session = ProviderSession(token="t")
    assert await pipeline._sign(db, provider, account, session) is None
    assert session.signature == AccountSignature("given", "<p>Reply banner</p>")
    read.assert_awaited_once_with(db, plan.integration_id, ME_MAIL)


async def test_without_a_signature_integration_the_account_keeps_its_own(monkeypatch):
    from app.services import email_signature_settings, email_signatures

    integration = SimpleNamespace(id=uuid.uuid4(), provider="microsoft")
    account = _own_account(integration)
    db = SimpleNamespace(get=AsyncMock(return_value=integration))
    monkeypatch.setattr(
        email_signature_settings,
        "plan_signatures",
        AsyncMock(return_value={str(integration.id): email_signatures.SignaturePlan(missing="none")}),
    )
    session = ProviderSession(token="t")
    await pipeline._sign(db, SimpleNamespace(capabilities=SimpleNamespace(signature=True)), account, session)
    assert session.signature == AccountSignature("own")
