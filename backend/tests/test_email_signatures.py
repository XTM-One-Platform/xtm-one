"""Signatures read from a signature integration, and where Outlook puts them.

``services/email_signatures`` is the one reader (cache, timeout, failures in
the owner's words); ``signature_block`` turns a service's HTML into the
``<div id="Signature">`` block Outlook writes - the block the February draft
work settled on, which a signature add-in refreshes in place when the person
opens the draft; the Outlook mail tools sign drafts and new emails with it
when a signature integration is bound, and change nothing without one.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict
from types import SimpleNamespace
from typing import Any

import pytest

import app.integrations.providers.microsoft as ms
from app.core.security import encrypt_value
from app.integrations.providers.microsoft import build_tools, mail
from app.integrations.providers.microsoft._outlook import _extract_signature_block, signature_block
from app.services import email_signatures
from app.services.email_signatures import EmailSignature, SignatureUnavailableError, read_signature
from app.tools.base import CallableToolExecutor, ToolCall

INTEGRATION_ID = uuid.uuid4()


class _Result:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _Db:
    """A session that answers every SELECT with one integration row, and counts them."""

    def __init__(self, row):
        self.row = row
        self.reads = 0

    async def execute(self, _statement):
        self.reads += 1
        return _Result(self.row)


def _integration(provider: str = "fake_signatures", key: str | None = "key") -> SimpleNamespace:
    return SimpleNamespace(
        id=INTEGRATION_ID, provider=provider, name="Signatures", relay_group_id=None,
        api_key_enc=encrypt_value(key) if key else None,
    )


@pytest.fixture
def reader(monkeypatch):
    """A registered signature source answering what the test says; it records the addresses asked."""
    asked: list[tuple[str, str]] = []
    answer: dict[str, Any] = {"value": EmailSignature(html="<p>New</p>", reply_html="<p>Reply</p>", name="Main")}

    async def _read(credential: str, address: str):
        asked.append((credential, address))
        value = answer["value"]
        if isinstance(value, BaseException):
            raise value
        if value == "slow":
            await asyncio.sleep(5)
        return value

    monkeypatch.setattr(email_signatures, "_READERS", {"fake_signatures": _read})
    monkeypatch.setattr(email_signatures, "_cache", OrderedDict())
    return asked, answer


async def test_a_signature_is_read_once_per_address_then_kept(reader):
    asked, _ = reader
    db = _Db(_integration())
    first = await read_signature(db, INTEGRATION_ID, " Sam@Example.com ")
    again = await read_signature(db, str(INTEGRATION_ID), "sam@example.com")
    assert first == again == EmailSignature(html="<p>New</p>", reply_html="<p>Reply</p>", name="Main")
    assert asked == [("key", "sam@example.com")] and db.reads == 1
    assert first.for_reply(True) == "<p>Reply</p>" and first.for_reply(False) == "<p>New</p>"


async def test_no_signature_for_an_address_is_kept_too(reader):
    asked, answer = reader
    answer["value"] = None
    db = _Db(_integration())
    assert await read_signature(db, INTEGRATION_ID, "nobody@example.com") is None
    assert await read_signature(db, INTEGRATION_ID, "nobody@example.com") is None
    assert len(asked) == 1


@pytest.mark.parametrize(
    ("row", "address", "words"),
    [
        (None, "sam@example.com", "is gone"),
        (_integration(provider="slack"), "sam@example.com", "does not hold email signatures"),
        (_integration(key=None), "sam@example.com", "has no API key"),
        (_integration(), "  ", "email address is not known"),
    ],
)
async def test_what_cannot_be_read_says_why(reader, row, address, words):
    with pytest.raises(SignatureUnavailableError, match=words):
        await read_signature(_Db(row), INTEGRATION_ID, address)


async def test_a_failure_of_the_service_is_not_kept(reader):
    asked, answer = reader
    answer["value"] = SignatureUnavailableError("The service refused the key.")
    db = _Db(_integration())
    for _ in range(2):
        with pytest.raises(SignatureUnavailableError, match="refused the key"):
            await read_signature(db, INTEGRATION_ID, "sam@example.com")
    assert len(asked) == 2


async def test_a_slow_service_never_holds_a_reply_back(reader, monkeypatch):
    _, answer = reader
    answer["value"] = "slow"
    monkeypatch.setattr(email_signatures, "READ_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(SignatureUnavailableError, match="did not answer in time"):
        await read_signature(_Db(_integration()), INTEGRATION_ID, "sam@example.com")


def test_an_oversized_signature_is_dropped():
    huge = "x" * (email_signatures.MAX_SIGNATURE_CHARS + 1)
    assert email_signatures.bounded(EmailSignature(html=huge)) is None
    kept = email_signatures.bounded(EmailSignature(html=huge, reply_html="<p>ok</p>"))
    assert kept == EmailSignature(html="<p>ok</p>", reply_html="<p>ok</p>")


# -- the Outlook block --------------------------------------------------------


def test_a_whole_document_becomes_the_outlook_signature_block():
    html = (
        "<html><head><title>x</title><style>.n{color:#f60}</style></head>"
        '<body><table onclick="steal()"><tr><td class="n">Sam Example</td>'
        '<td><img src="https://cdn/banner.png"></td></tr></table><script>alert(1)</script></body></html>'
    )
    block = signature_block(html)
    assert block.startswith('<div id="Signature"><style>.n{color:#f60}</style><table>')
    assert "Sam Example" in block and "banner.png" in block
    assert "script" not in block and "onclick" not in block and "<title>" not in block and "<body" not in block
    # The block Outlook writes, read back from sent mail by the same rule.
    assert _extract_signature_block(f"<html><body><p>Hi</p>{block}</body></html>") == block


def test_a_sent_reply_with_a_flattened_quote_never_lends_that_quote_to_the_signature():
    """The Lightrock leak: a sent reply whose quote was text, its signature read as signature + quote."""
    from app.integrations.providers.microsoft._outlook import sent_mail_signature

    sent = (
        '<html><body><p>Thanks</p><div id="Signature"><table><tr><td>Sam</td>'
        '<td><img src="cid:image001.png@01DB"><img src="https://cdn.example.com/logo.png"></td></tr></table>'
        "<p>________________</p><p>Book a meeting</p></div>"
        "<div>________________________________________ From: Samuel Hassine Sent: Sunday, 01 November 2026 "
        "21:22:23 To: Samuel Hassine Subject: Test thread Hello Samuel, This is a test message</div></body></html>"
    )
    block = sent_mail_signature(sent)
    assert "Book a meeting" in block and "https://cdn.example.com/logo.png" in block
    assert "Test thread" not in block and "Hello Samuel" not in block and "From:" not in block
    # Its cid: image belongs to that sent email: broken anywhere else, so left out.
    assert "cid:" not in block
    assert sent_mail_signature("<html><body><p>No signature</p></body></html>") == ""
    # From / To lines of the signature itself, with no separator above them, are no quote.
    own = '<html><body><div id="Signature"><p>Sam</p><p>From: Paris office</p><p>To book: example.com</p></div></body></html>'
    assert "To book: example.com" in sent_mail_signature(own)


def test_a_signature_already_in_a_signature_block_keeps_one_block():
    block = signature_block('<div id="x_Signature"><p>Sam</p></div>')
    assert block == '<div id="Signature"><p>Sam</p></div>'
    assert signature_block("") == signature_block("<p>  </p>") == ""
    assert signature_block('<img src="https://cdn/logo.png">') == '<div id="Signature"><img src="https://cdn/logo.png"/></div>'


# -- the Outlook mail tools ---------------------------------------------------

MESSAGE_ID = "AAMkAGI2NGVhZTVlLTI1OGMtNDI4My1iZmVmLTI1ZmM5OWM5MTIwNgBGAAAAAADUuTJK1K9aTpCdqXop_4NaBwCd9nJ-tVys"
DRAFT_BODY = '<html><body><div id="appendonsend"></div><hr style="display:inline-block"><div id="divRplyFwdMsg">From: Alice</div></body></html>'
SENT_SIGNATURE = '<div id="Signature"><p>Sam - copied from sent mail</p></div>'


class _Ok:
    def __init__(self, payload: dict | None = None):
        self._payload = payload or {}
        self.status_code = 200

    def json(self):
        return self._payload


@pytest.fixture
def graph(monkeypatch):
    """Graph answered in memory; every request recorded."""
    calls: list[tuple[str, str, dict]] = []

    async def _fake(_client, method, url, _provider, _action, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/createReply"):
            return _Ok({"id": "draft-1", "body": {"content": DRAFT_BODY}, "toRecipients": []}), None
        if url.endswith("/me"):
            return _Ok({"mail": "sam@example.com"}), None
        if "sentitems" in url:
            return _Ok({"value": [{"body": {"content": f"<html><body><p>Old</p>{SENT_SIGNATURE}</body></html>"}}]}), None
        return _Ok({"id": "draft-2"}), None

    async def _quiet(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ms._common, "safe_request", _fake)
    monkeypatch.setattr(mail, "record_mail_draft", _quiet)
    monkeypatch.setattr(mail, "record_mail_send", _quiet)
    return calls


def _executor(signature_fn=None):
    executor = CallableToolExecutor()
    definitions = build_tools(token="dummy", executor=executor, signature_fn=signature_fn)
    return executor, {d.name: d for d in definitions}


async def _signature_of(address: str):
    assert address == "sam@example.com"
    return EmailSignature(html="<p>Sam - new email</p>", reply_html="<p>Sam - reply</p>", name="Main")


def _patched_body(calls) -> str:
    (patch,) = [kwargs for method, _url, kwargs in calls if method == "patch"]
    return patch["json"]["body"]["content"]


async def test_a_draft_reply_carries_the_live_reply_signature_where_outlook_puts_it(graph):
    executor, _ = _executor(_signature_of)
    await executor.execute_tool(
        ToolCall(id="c1", name="create_microsoft_draft_reply", arguments={"message_id": MESSAGE_ID, "body": "Hello"})
    )
    content = _patched_body(graph)
    signature = '<div id="Signature"><p>Sam - reply</p></div>'
    assert signature in content and "copied from sent mail" not in content
    assert content.index("Hello") < content.index(signature) < content.index("<hr")
    assert not any("sentitems" in url for _m, url, _k in graph)


async def test_without_a_signature_integration_a_draft_reply_still_copies_the_sent_mail_one(graph):
    executor, tools = _executor()
    await executor.execute_tool(
        ToolCall(id="c1", name="create_microsoft_draft_reply", arguments={"message_id": MESSAGE_ID, "body": "Hello"})
    )
    content = _patched_body(graph)
    assert content.index("Hello") < content.index(SENT_SIGNATURE) < content.index("<hr")
    assert "signature integration" not in tools["create_microsoft_draft_reply"].description
    assert "signature" not in tools["send_microsoft_email"].description.lower()


async def test_a_new_email_is_signed_only_with_a_signature_integration(graph):
    executor, tools = _executor(_signature_of)
    await executor.execute_tool(
        ToolCall(id="c1", name="send_microsoft_email", arguments={"to": "alice@example.com", "subject": "Hi", "body": "**Hello**"})
    )
    (send,) = [kwargs for _m, url, kwargs in graph if url.endswith("/sendMail")]
    content = send["json"]["message"]["body"]["content"]
    assert content.index("<strong>Hello</strong>") < content.index('<div id="Signature"><p>Sam - new email</p></div>')
    assert "do NOT write a signature" in tools["send_microsoft_email"].description
    assert "do NOT write a signature" in tools["create_microsoft_draft_email"].description

    graph.clear()
    executor, _ = _executor()
    await executor.execute_tool(
        ToolCall(id="c2", name="send_microsoft_email", arguments={"to": "alice@example.com", "subject": "Hi", "body": "Hello"})
    )
    (send,) = [kwargs for _m, url, kwargs in graph if url.endswith("/sendMail")]
    assert "Signature" not in send["json"]["message"]["body"]["content"]


async def test_a_signature_integration_holding_none_falls_back_for_replies_only(graph):
    async def _none(_address):
        return None

    executor, _ = _executor(_none)
    await executor.execute_tool(
        ToolCall(id="c1", name="create_microsoft_draft_reply", arguments={"message_id": MESSAGE_ID, "body": "Hello"})
    )
    assert SENT_SIGNATURE in _patched_body(graph)
    await executor.execute_tool(
        ToolCall(id="c2", name="create_microsoft_draft_email", arguments={"to": "a@example.com", "subject": "S", "body": "B"})
    )
    (draft,) = [kwargs for m, url, kwargs in graph if m == "post" and url.endswith("/me/messages")]
    assert "Signature" not in draft["json"]["body"]["content"]
