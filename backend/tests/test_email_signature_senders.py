"""Every email-sending provider signs what it writes through one hook.

``integration_tools`` hands each provider registered as a sender
(``email_signatures.register_email_sender``) the same two kwargs:
``signature_fn`` (the person's signature, read from the signature
integration) and ``signature_plan`` (the integration's Email signature setting
as resolved). Each places the signature where its mail client does: Outlook's
``<div id="Signature">`` block, Gmail's ``gmail_signature`` block in the HTML
part and the text rendering in the plain part, SES's HTML and text bodies.
With the setting off nothing is signed, not even from the sent mail.
"""

from __future__ import annotations

import base64
import uuid
from email import message_from_bytes
from email import policy as email_policy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import app.integrations.providers.microsoft as ms
from app.integrations.providers import google, ses
from app.integrations.providers.microsoft import mail
from app.services.email_signatures import (
    OFF_BY_INTEGRATION,
    EmailSignature,
    SignaturePlan,
    email_sender_providers,
)
from app.services.integration_tools import BindingBasis, ToolBindings
from app.tools.base import CallableToolExecutor, ToolCall

SIGNED = SignaturePlan(integration_id=str(uuid.uuid4()), integration_name="Signatures")
OFF = SignaturePlan(enabled=False, off_by=OFF_BY_INTEGRATION)
NONE_AVAILABLE = SignaturePlan(missing="no signature integration is available to this integration's user.")


async def _signature_of(address: str):
    assert address == "sam@example.com"
    return EmailSignature(
        html="<html><head><style>.n{color:#f60}</style></head><body><p>Sam - new email</p></body></html>",
        reply_html="<p>Sam - reply</p>",
        name="Main",
    )


async def _run(executor: CallableToolExecutor, name: str, **arguments: Any) -> str:
    result = await executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments))
    return result.content


def test_microsoft_google_and_ses_are_email_senders():
    assert {"microsoft", "google", "ses"} <= email_sender_providers()


# -- Outlook ------------------------------------------------------------------

MESSAGE_ID = "AAMkAGI2NGVhZTVlLTI1OGMtNDI4My1iZmVmLTI1ZmM5OWM5MTIwNgBGAAAAAADUuTJK1K9aTpCdqXop_4NaBwCd9nJ-tVys"
REPLY_BODY = (
    '<html><body><div>Thanks</div><div id="appendonsend"></div><hr style="display:inline-block">'
    '<div id="divRplyFwdMsg">From: Alice</div><div id="x_Signature"><p>Alice quoted</p></div></body></html>'
)
SENT_SIGNATURE = '<div id="Signature"><p>Sam - copied from sent mail</p></div>'


class _Ok:
    def __init__(self, payload: dict | None = None):
        self._payload = payload or {}
        self.status_code = 200

    def json(self):
        return self._payload


@pytest.fixture
def graph(monkeypatch):
    """Graph answered in memory; ``drafts`` holds the body each draft id reads back."""
    calls: list[tuple[str, str, dict]] = []
    drafts: dict[str, dict] = {}

    async def _fake(_client, method, url, _provider, _action, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/me"):
            return _Ok({"mail": "sam@example.com"}), None
        if "sentitems" in url:
            return _Ok({"value": [{"body": {"content": f"<html><body>{SENT_SIGNATURE}</body></html>"}}]}), None
        if url.endswith("/createReply"):
            return _Ok({"id": "draft-1", "body": {"content": REPLY_BODY}, "toRecipients": []}), None
        for draft_id, body in drafts.items():
            if method == "get" and url.endswith(f"/me/messages/{draft_id}"):
                return _Ok({"subject": "S", "toRecipients": [], "body": body}), None
        return _Ok({"id": "draft-2"}), None

    async def _quiet(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ms._common, "safe_request", _fake)
    monkeypatch.setattr(mail, "record_mail_draft", _quiet)
    monkeypatch.setattr(mail, "record_mail_send", _quiet)
    return SimpleNamespace(calls=calls, drafts=drafts)


def _outlook(signature_fn=None, plan=None):
    executor = CallableToolExecutor()
    definitions = ms.build_tools(token="dummy", executor=executor, signature_fn=signature_fn, signature_plan=plan)
    return executor, {d.name: d for d in definitions}


def _patches(graph) -> list[str]:
    return [kwargs["json"]["body"]["content"] for method, _url, kwargs in graph.calls if method == "patch"]


async def test_outlook_with_signatures_off_signs_nothing_not_even_from_the_sent_mail(graph):
    executor, tools = _outlook(_signature_of, OFF)
    await _run(executor, "create_microsoft_draft_reply", message_id=MESSAGE_ID, body="Hello")
    (patched,) = _patches(graph)
    assert "Sam -" not in patched
    await _run(executor, "send_microsoft_email", to="a@example.com", subject="Hi", body="Hello")
    (send,) = [kwargs for _m, url, kwargs in graph.calls if url.endswith("/sendMail")]
    assert "Signature" not in send["json"]["message"]["body"]["content"]
    assert not any("sentitems" in url for _m, url, _k in graph.calls)
    assert "off for this integration" in tools["create_microsoft_draft_reply"].description
    assert "Do NOT include the email signature" not in tools["create_microsoft_draft_reply"].description
    assert "integration's settings" in await _run(executor, "get_microsoft_email_signature")


async def test_outlook_sends_a_draft_signed_once(graph):
    executor, tools = _outlook(_signature_of, SIGNED)
    new_id, reply_id, signed_id = (f"{MESSAGE_ID}{suffix}" for suffix in ("NEW", "REPLY", "SIGNED"))
    graph.drafts[new_id] = {"contentType": "html", "content": "<html><body><p>Draft</p></body></html>"}
    graph.drafts[reply_id] = {"contentType": "html", "content": REPLY_BODY}
    graph.drafts[signed_id] = {
        "contentType": "html",
        "content": '<html><body><p>Draft</p><div id="Signature"><p>Already</p></div></body></html>',
    }
    assert "added before it was sent" in await _run(executor, "send_microsoft_draft", draft_id=new_id)
    new, = _patches(graph)
    # The sign-off right above the signature keeps no bottom margin (#8522).
    assert new.index('<p style="margin-bottom:0">Draft</p>') < new.index(
        '<div id="Signature"><style>.n{color:#f60}</style><p>Sam - new email</p></div>'
    )

    graph.calls.clear()
    await _run(executor, "send_microsoft_draft", draft_id=reply_id)
    reply, = _patches(graph)
    assert reply.index("Thanks") < reply.index("Sam - reply") < reply.index('<hr style="display:inline-block">')

    graph.calls.clear()
    assert await _run(executor, "send_microsoft_draft", draft_id=signed_id) == f"Draft {signed_id} has been sent."
    assert _patches(graph) == []
    assert "before it is sent" in tools["send_microsoft_draft"].description


async def test_outlook_says_where_its_signature_comes_from(graph):
    executor, tools = _outlook(_signature_of, SIGNED)
    found = await _run(executor, "get_microsoft_email_signature")
    assert found.startswith("User's email signature (from the signature integration 'Signatures')")
    assert "Sam - reply" in found
    assert "'Signatures'" in tools["send_microsoft_email"].description

    executor, tools = _outlook(None, NONE_AVAILABLE)
    copied = await _run(executor, "get_microsoft_email_signature")
    assert "copied from recent sent emails" in copied and "no signature integration is available" in copied
    assert "Sam - copied from sent mail" in copied
    assert "before it is sent" not in tools["send_microsoft_draft"].description


# -- Gmail --------------------------------------------------------------------


def _raw(payload: dict) -> bytes:
    return base64.urlsafe_b64decode(payload["raw"] + "=" * (-len(payload["raw"]) % 4))


def _parts(raw: bytes) -> dict[str, str]:
    message = message_from_bytes(raw, policy=email_policy.default)
    return {
        part.get_content_type(): part.get_content().rstrip("\r\n")
        for part in message.walk()
        if not part.is_multipart()
    }


@pytest.fixture
def gmail(monkeypatch):
    """Gmail answered in memory; ``draft`` is what the drafts read returns."""
    state = SimpleNamespace(calls=[], draft=None)

    async def _fake(_client, method, url, _provider, _action, **kwargs):
        state.calls.append((method, url, kwargs))
        if url.endswith("/users/me/profile"):
            return _Ok({"emailAddress": "sam@example.com"}), None
        if method == "get" and "/users/me/drafts/" in url:
            return _Ok({"id": "d1", "message": {"raw": state.draft, "threadId": "t1"}}), None
        if method == "get" and "/users/me/messages/" in url:
            headers = [
                {"name": "Subject", "value": "Plan"},
                {"name": "From", "value": "alice@example.com"},
                {"name": "Message-ID", "value": "<m0@example.com>"},
            ]
            return _Ok({"threadId": "t1", "payload": {"headers": headers}}), None
        return _Ok({"id": "m1", "message": {"id": "m1"}}), None

    monkeypatch.setattr(google, "safe_request", _fake)
    return state


def _gmail(signature_fn=None, plan=None):
    executor = CallableToolExecutor()
    definitions = google.build_tools("dummy", executor, signature_fn=signature_fn, signature_plan=plan)
    return executor, {d.name: d for d in definitions}


def _sent_raw(gmail, suffix: str) -> bytes:
    (payload,) = [kwargs["json"] for method, url, kwargs in gmail.calls if method == "post" and url.endswith(suffix)]
    return _raw(payload.get("message", payload))


async def test_gmail_signs_the_html_part_and_the_plain_part(gmail):
    executor, tools = _gmail(_signature_of, SIGNED)
    await _run(executor, "send_gmail", to="a@example.com", subject="Hi", body="Hello <team>\nSee you")
    parts = _parts(_sent_raw(gmail, "/users/me/messages/send"))
    html, plain = parts["text/html"], parts["text/plain"]
    assert html.index("Hello &lt;team&gt;<br") < html.index(
        '<div dir="ltr" class="gmail_signature" data-smartmail="gmail_signature"><style>.n{color:#f60}</style><p>Sam - new email</p></div>'
    )
    assert plain.startswith("Hello <team>\nSee you\n\nSam - new email")
    assert "do NOT write a signature" in tools["send_gmail"].description
    assert "'Signatures'" in tools["create_gmail_draft"].description

    gmail.calls.clear()
    await _run(executor, "create_gmail_draft", to="a@example.com", subject="Hi", body="B", body_html="<p>B</p>")
    draft = _parts(_sent_raw(gmail, "/users/me/drafts"))
    assert draft["text/html"].count('class="gmail_signature"') == 1 and draft["text/html"].startswith("<p>B</p>")

    gmail.calls.clear()
    await _run(executor, "reply_gmail", message_id="m0", body="Sure")
    reply = _parts(_sent_raw(gmail, "/users/me/messages/send"))
    assert "Sam - reply" in reply["text/html"] and "Sam - new email" not in reply["text/html"]
    assert reply["text/plain"].endswith("Sam - reply")


async def test_gmail_without_a_signature_integration_sends_as_before(gmail):
    executor, tools = _gmail(None, NONE_AVAILABLE)
    await _run(executor, "send_gmail", to="a@example.com", subject="Hi", body="Hello")
    parts = _parts(_sent_raw(gmail, "/users/me/messages/send"))
    assert parts["text/plain"] == "Hello"
    assert ">Hello</span></p>" in parts["text/html"] and "gmail_signature" not in parts["text/html"]
    assert not any(url.endswith("/profile") for _m, url, _k in gmail.calls)
    assert "signature" not in tools["send_gmail"].description.lower()
    answer = await _run(executor, "get_gmail_email_signature")
    assert answer.startswith("No email signature: no signature integration is available")


async def test_gmail_signs_a_draft_once_before_sending_it(gmail):
    executor, tools = _gmail(_signature_of, SIGNED)
    unsigned = message_from_bytes(b"To: a@example.com\r\nSubject: S\r\n\r\nPlain draft\r\n", policy=email_policy.default)
    gmail.draft = base64.urlsafe_b64encode(unsigned.as_bytes()).decode()
    assert "added before it was sent" in await _run(executor, "send_gmail_draft", draft_id="d1")
    (put,) = [kwargs for method, url, kwargs in gmail.calls if method == "put"]
    body = put["content"]
    raw = body[body.index(b"message/rfc822\r\n\r\n") + len(b"message/rfc822\r\n\r\n") : body.rindex(b"\r\n--xtm-")]
    parts = _parts(raw)
    assert parts["text/plain"].startswith("Plain draft") and "Sam - new email" in parts["text/plain"]
    assert parts["text/html"].count('class="gmail_signature"') == 1
    assert b'"threadId": "t1"' in body
    assert [url for method, url, _k in gmail.calls if method == "post"][-1].endswith("/drafts/send")

    gmail.calls.clear()
    signed = google._alternative("Plain draft", "", "<p>Sam - new email</p>")
    signed["To"] = "a@example.com"
    gmail.draft = base64.urlsafe_b64encode(signed.as_bytes()).decode()
    assert await _run(executor, "send_gmail_draft", draft_id="d1") == "Draft sent. Message ID: m1."
    assert not any(method == "put" for method, _url, _k in gmail.calls)
    assert "before it is sent" in tools["send_gmail_draft"].description


async def test_gmail_with_signatures_off_says_how_to_turn_them_on(gmail):
    executor, _tools = _gmail(_signature_of, OFF)
    await _run(executor, "send_gmail", to="a@example.com", subject="Hi", body="Hello")
    assert "gmail_signature" not in _parts(_sent_raw(gmail, "/users/me/messages/send"))["text/html"]
    assert "Email signature setting" in await _run(executor, "get_gmail_email_signature")


# -- SES ----------------------------------------------------------------------


@pytest.fixture
def ses_api(monkeypatch):
    sent: list[dict] = []

    async def _signed_request(**kwargs):
        sent.append(kwargs["body"])
        return _Ok({"MessageId": "ses-1"}), None

    monkeypatch.setattr(ses, "_signed_request", _signed_request)
    return sent


def _ses(signature_fn=None, plan=None):
    executor = CallableToolExecutor()
    definitions = ses.build_tools(
        "us-east-1||AKIA||secret||", executor, signature_fn=signature_fn, signature_plan=plan
    )
    return executor, {d.name: d for d in definitions}


async def test_ses_signs_the_html_and_text_bodies_of_the_from_address(ses_api):
    executor, tools = _ses(_signature_of, SIGNED)
    await _run(
        executor, "send_ses_email", from_address="Sam <Sam@Example.com>", to="a@example.com", subject="Hi", text_body="Hello"
    )
    body = ses_api[0]["Content"]["Simple"]["Body"]
    assert body["Text"]["Data"] == "Hello\n\nSam - new email"
    assert body["Html"]["Data"].startswith('<div dir="ltr">Hello</div>')
    assert '<div class="signature"><style>.n{color:#f60}</style><p>Sam - new email</p></div>' in body["Html"]["Data"]
    assert "do NOT write a signature" in tools["send_ses_email"].description
    assert "no signature is added" in tools["send_ses_templated_email"].description
    found = await _run(executor, "get_ses_email_signature", from_address="sam@example.com")
    assert found.startswith("User's email signature (from the signature integration 'Signatures')")


async def test_ses_without_a_signature_integration_sends_as_before(ses_api):
    executor, tools = _ses(None, NONE_AVAILABLE)
    await _run(executor, "send_ses_email", from_address="sam@example.com", to="a@example.com", subject="Hi", html_body="<p>Hi</p>")
    assert ses_api[0]["Content"]["Simple"]["Body"] == {"Html": {"Data": "<p>Hi</p>", "Charset": "UTF-8"}}
    assert "signature" not in tools["send_ses_email"].description.lower()
    assert "no signature integration is available" in await _run(
        executor, "get_ses_email_signature", from_address="sam@example.com"
    )


# -- the hook -----------------------------------------------------------------


async def test_each_sender_gets_its_own_plan_through_the_same_kwargs(monkeypatch):
    """Two bound mailboxes resolve on their own; a provider that sends no email gets neither kwarg."""
    from app.services import integration_tools as it
    from app.services.email_signature_settings import SIGNATURE_CONTEXT_KEY

    def _row(provider: str, name: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=uuid.uuid4(), provider=provider, name=name, description=None, auth_type="oauth",
            credential_mode="shared", relay_group_id=None, user_id=uuid.uuid4(),
            signature_enabled=True, signature_integration_id=None,
        )

    first, second = _row("google", "First"), _row("google", "Second")
    signatures, chat = _row("fake_signatures", "Signatures"), _row("slack", "Chat")
    rows = [first, second, signatures, chat]

    class _Rows:
        def scalars(self):
            return self

        def all(self):
            return rows

    class _Db:
        async def execute(self, *_args, **_kwargs):
            return _Rows()

    by_provider: dict[str, list[dict]] = {}

    def _builder(name):
        def _build(token, executor, **kwargs):
            by_provider.setdefault(name, []).append(kwargs)
            return []

        return _build

    from app.services import email_signatures

    monkeypatch.setitem(email_signatures._READERS, "fake_signatures", AsyncMock(return_value=None))
    for provider in ("google", "fake_signatures", "slack"):
        monkeypatch.setitem(it.PROVIDER_TOOLS, provider, _builder(provider))
    monkeypatch.setattr(it, "_parallel_refresh_expiring_tokens", AsyncMock(return_value=({}, set())))
    monkeypatch.setattr(it, "_build_a2a_delegation_tool", AsyncMock(return_value=None))
    monkeypatch.setattr(it, "_get_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(it, "_make_refresh_fn", lambda _db, _iid: None)

    await it.build_integration_tools(
        _Db(),
        ToolBindings({
            "google": [str(first.id), str(second.id)],
            "fake_signatures": [str(signatures.id)],
            "slack": [str(chat.id)],
        }, basis=BindingBasis.CALLER_CHECKED, reason="test"),
        context={SIGNATURE_CONTEXT_KEY: {str(second.id): {"signature_mode": "off"}}},
    )
    google_plans = [kwargs["signature_plan"] for kwargs in by_provider["google"]]
    assert sorted((p.enabled, p.integration_id) for p in google_plans) == [
        (False, None),
        (True, str(signatures.id)),
    ]
    assert sum(callable(kwargs["signature_fn"]) for kwargs in by_provider["google"]) == 1
    for provider in ("fake_signatures", "slack"):
        assert all("signature_plan" not in kwargs and "signature_fn" not in kwargs for kwargs in by_provider[provider])
