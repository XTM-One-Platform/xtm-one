"""forward_microsoft_email and reply_microsoft_email_in_thread against Graph in memory.

A person forwards a thread to an assistant's mailbox; the assistant replies
from her mailbox to the ORIGINAL correspondents, in the original thread
(In-Reply-To / References), the thread quoted under her text and signature.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import pytest

import app.integrations.providers.microsoft as ms
from app.integrations.providers.microsoft import mail
from app.integrations.providers.microsoft._thread_reply import (
    THREAD_IN_REPLY_TO_HEADER,
    THREAD_REFERENCES_HEADER,
)
from app.tools.base import CallableToolExecutor, ToolCall

ORIGINAL = "AAMkAGI2NGVhZTVlLTI1OGMtNDI4My1iZmVmLTI1ZmM5OWM5MTIwNgBGAAAAAADUuTJK1K9aTpCdqXop_4NaBwCdORIGINAL"
FORWARDED = "AAMkAGI2NGVhZTVlLTI1OGMtNDI4My1iZmVmLTI1ZmM5OWM5MTIwNgBGAAAAAADUuTJK1K9aTpCdqXop_4NaBwCdFORWARD"
DRAFT = "AAMkAGI2NGVhZTVlLTI1OGMtNDI4My1iZmVmLTI1ZmM5OWM5MTIwNgBGAAAAAADUuTJK1K9aTpCdqXop_4NaBwCd00DRAFT"
OWN = "assistant@example.com"
FORWARD_BODY = (
    '<html><body><div>Please answer them</div><hr style="display:inline-block">'
    '<div id="divRplyFwdMsg"><b>From:</b> Sam &lt;sam@example.com&gt;<br><b>Sent:</b> Sunday<br>'
    f"<b>To:</b> {OWN}<br><b>Subject:</b> FW: Introduction</div><div>Please answer them</div>"
    '<hr style="display:inline-block"><div id="divRplyFwdMsg"><b>From:</b> Alice &lt;alice@example.org&gt;<br>'
    "<b>Sent:</b> Sunday<br><b>To:</b> sam@example.com<br><b>Cc:</b> bob@example.org<br>"
    "<b>Subject:</b> Introduction</div><div>Hello Sam</div></body></html>"
)
REPLY_DRAFT_BODY = '<html><body><div id="appendonsend"></div>' + FORWARD_BODY[len("<html><body>") :]


class _Resp:
    def __init__(self, payload: Any = None, content: bytes = b""):
        self._payload = payload if payload is not None else {}
        self.content = content
        self.status_code = 200

    def json(self):
        return self._payload


@pytest.fixture
def graph(monkeypatch):
    state = SimpleNamespace(
        calls=[],
        held_headers=[],
        refuse_forward=False,
        refuse_delete=False,
        forwarder="sam@example.com",
        cc=[],
    )

    def _call(method: str, suffix: str) -> list[dict]:
        return [k for m, u, k in state.calls if m == method and u.endswith(suffix)]

    state.call = _call

    async def _fake(_client, method, url, _provider, _action, **kwargs):
        state.calls.append((method, url, kwargs))
        if url.endswith("/me"):
            return _Resp({"mail": OWN}), None
        if url.endswith(f"/{ORIGINAL}") and method == "get":
            return _Resp(
                {
                    "subject": "Introduction",
                    "internetMessageId": "<orig@mail.example.org>",
                    "internetMessageHeaders": [{"name": "References", "value": "<root@mail.example.org>"}],
                }
            ), None
        if url.endswith(f"/{FORWARDED}") and method == "get":
            return _Resp(
                {
                    "subject": "FW: Introduction",
                    "body": {"contentType": "html", "content": FORWARD_BODY},
                    "from": {"emailAddress": {"address": state.forwarder}},
                    "toRecipients": [{"emailAddress": {"address": OWN}}],
                    "ccRecipients": [{"emailAddress": {"address": a}} for a in state.cc],
                    "internetMessageHeaders": state.held_headers,
                }
            ), None
        if url.endswith("/createForward"):
            if state.refuse_forward:
                refused = _Resp({"error": {"code": "ErrorInvalidRecipients", "message": "Invalid request."}})
                refused.status_code = 400
                return refused, "Error: Microsoft returned HTTP 400 while trying to create forward."
            body = '<html><body><hr><div id="divRplyFwdMsg">From: Alice</div></body></html>'
            return _Resp({"id": DRAFT, "body": {"content": body}}), None
        if url.endswith("/createReply"):
            return _Resp(
                {
                    "id": DRAFT,
                    "body": {"content": REPLY_DRAFT_BODY},
                    "toRecipients": [{"emailAddress": {"address": "sam@example.com"}}],
                    "conversationId": "conv",
                }
            ), None
        if url.endswith(f"/{DRAFT}") and method == "delete" and state.refuse_delete:
            return None, "Error: Microsoft returned HTTP 503 while trying to delete sent draft."
        if url.endswith(f"/{DRAFT}/$value"):
            mime = b"Subject: RE: Introduction\r\nIn-Reply-To: <fwd@x>\r\nReferences: <fwd@x>\r\n\r\nbody"
            return _Resp(content=mime), None
        if url.endswith(f"/{DRAFT}") and method == "get":
            return _Resp({"subject": "RE: Introduction", "toRecipients": [], "conversationId": "conv"}), None
        return _Resp({}), None

    async def _quiet(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ms._common, "safe_request", _fake)
    monkeypatch.setattr(mail, "record_mail_draft", _quiet)
    monkeypatch.setattr(mail, "record_mail_send", _quiet)
    return state


async def _run(name: str, **arguments: Any) -> str:
    executor = CallableToolExecutor()
    ms.build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments))
    return result.content


async def test_a_forward_carries_the_comment_above_the_original_and_its_thread(graph):
    answer = await _run("forward_microsoft_email", message_id=ORIGINAL, to=OWN, comment="Can you answer them?")
    assert answer == f"Email {ORIGINAL} forwarded to {OWN}."
    (patch,) = graph.call("patch", f"/{DRAFT}")
    content = patch["json"]["body"]["content"]
    assert content.index("Can you answer them?") < content.index("<hr>") < content.index("From: Alice")
    assert patch["json"]["toRecipients"] == [{"emailAddress": {"address": OWN}}]
    # createForward takes no internetMessageHeaders and a PATCH refuses them:
    # the thread headers are written into the forward's MIME at the send.
    assert "internetMessageHeaders" not in patch["json"]
    (created,) = graph.call("post", "/createForward")
    assert created["json"] == {}
    (sent,) = graph.call("post", "/sendMail")
    mime = base64.b64decode(sent["content"])
    assert f"{THREAD_IN_REPLY_TO_HEADER}: <orig@mail.example.org>".encode() in mime
    assert f"{THREAD_REFERENCES_HEADER}: <root@mail.example.org> <orig@mail.example.org>".encode() in mime
    # The forward's own threading is kept: only the custom headers are added.
    assert b"In-Reply-To: <fwd@x>" in mime
    assert mime.endswith(b"\r\n\r\nbody")
    assert sent["headers"]["Content-Type"] == "text/plain"
    assert graph.call("post", f"/{DRAFT}/send") == []
    assert graph.call("delete", f"/{DRAFT}")


async def test_a_sent_forward_whose_draft_cannot_be_deleted_says_not_to_send_it_again(graph):
    graph.refuse_delete = True
    answer = await _run("forward_microsoft_email", message_id=ORIGINAL, to=OWN, comment="Can you answer them?")
    assert answer.startswith(f"Email {ORIGINAL} forwarded to {OWN}.")
    assert f"its draft {DRAFT} could not be deleted and stays in Drafts" in answer
    assert "do not send that draft again" in answer
    assert len(graph.call("post", "/sendMail")) == 1
    # The delete is tried twice before the draft is reported as left behind.
    assert len(graph.call("delete", f"/{DRAFT}")) == 2


async def test_a_forward_of_a_message_without_an_id_is_sent_natively(graph, monkeypatch):
    async def _no_id(_client, method, url, *_a, **kwargs):
        graph.calls.append((method, url, kwargs))
        if url.endswith(f"/{ORIGINAL}") and method == "get":
            return _Resp({"subject": "Introduction"}), None
        if url.endswith("/createForward"):
            return _Resp({"id": DRAFT, "body": {"content": "<html><body><hr>From: Alice</body></html>"}}), None
        return _Resp({}), None

    monkeypatch.setattr(ms._common, "safe_request", _no_id)
    answer = await _run("forward_microsoft_email", message_id=ORIGINAL, to=OWN)
    assert answer == f"Email {ORIGINAL} forwarded to {OWN}."
    assert graph.call("post", f"/{DRAFT}/send")
    assert graph.call("post", "/sendMail") == []


async def test_a_forward_refused_for_another_reason_fails_without_a_retry(graph):
    graph.refuse_forward = True
    answer = await _run("forward_microsoft_email", message_id=ORIGINAL, to=OWN, comment="Can you answer them?")
    assert answer.startswith("Error:") and "Nothing was forwarded" in answer
    assert len(graph.call("post", "/createForward")) == 1
    assert graph.call("post", f"/{DRAFT}/send") == []


async def test_a_reply_in_thread_draft_goes_to_the_original_correspondents(graph):
    answer = await _run("reply_microsoft_email_in_thread", message_id=FORWARDED, body="Kind regards,")
    assert "NOT sent" in answer
    (patch,) = graph.call("patch", f"/{DRAFT}")
    sent = patch["json"]
    assert sent["toRecipients"] == [{"emailAddress": {"address": "alice@example.org"}}]
    assert sent["ccRecipients"] == [
        {"emailAddress": {"address": "sam@example.com"}},
        {"emailAddress": {"address": "bob@example.org"}},
    ]
    assert sent["subject"] == "RE: Introduction"
    # The quote opens on the original message: the internal forward note is never quoted.
    content = sent["body"]["content"]
    assert "From:</b> Sam" not in content and "Please answer them</div><hr" not in content
    assert content.index("Kind regards,") < content.index("From:</b> Alice") < content.index("Hello Sam")
    assert graph.call("post", "/sendMail") == []


async def test_reply_in_thread_takes_cc_and_can_keep_the_forward_header(graph):
    await _run(
        "reply_microsoft_email_in_thread",
        message_id=FORWARDED,
        body="Kind regards,",
        cc="sam@example.com, julien@example.com",
        keep_forward_header=True,
    )
    (patch,) = graph.call("patch", f"/{DRAFT}")
    assert [r["emailAddress"]["address"] for r in patch["json"]["ccRecipients"]] == [
        "sam@example.com",
        "julien@example.com",
    ]
    content = patch["json"]["body"]["content"]
    assert "From:</b> Sam" in content and "From:</b> Alice" in content
    # Nothing was cut from the quote: its inline images are left alone.
    assert not [u for m, u, _k in graph.calls if "/attachments" in u]


async def test_reply_in_thread_drops_the_inline_images_of_the_forward_note_it_left_out(graph, monkeypatch):
    """createReply copies the forwarder's signature images: the note is cut, they go with it."""
    original_image = '<img src="cid:image001.png@01DB">'
    body = REPLY_DRAFT_BODY.replace("<div>Hello Sam</div>", f"<div>Hello Sam{original_image}</div>").replace(
        "FW: Introduction</div><div>Please answer them</div>",
        'FW: Introduction</div><div>Please answer them<img src="cid:sig-note@xtm-one"></div>',
    )
    assert "cid:sig-note@xtm-one" in body
    attachments = {
        "a1": {"name": "sig-note.png", "isInline": True, "contentId": "sig-note@xtm-one"},
        "a2": {"name": "image001.png", "isInline": True, "contentId": "image001.png@01DB"},
        "a3": {"name": "Outlook-logo.png", "isInline": True, "contentId": "Outlook-logo@x"},
        "a4": {"name": "agenda.pdf", "isInline": False},
    }
    fake = ms._common.safe_request

    async def _with_images(client, method, url, provider, action, **kwargs):
        if url.endswith("/createReply"):
            graph.calls.append((method, url, kwargs))
            return _Resp({"id": DRAFT, "body": {"contentType": "html", "content": body}, "toRecipients": []}), None
        if url.endswith(f"/{DRAFT}/attachments") and method == "get":
            graph.calls.append((method, url, kwargs))
            listed = [{"id": k, "name": v["name"], "isInline": v["isInline"]} for k, v in attachments.items()]
            return _Resp({"value": listed}), None
        for key, value in attachments.items():
            if url.endswith(f"/{DRAFT}/attachments/{key}"):
                graph.calls.append((method, url, kwargs))
                return _Resp(value), None
        return await fake(client, method, url, provider, action, **kwargs)

    monkeypatch.setattr(ms._common, "safe_request", _with_images)
    await _run("reply_microsoft_email_in_thread", message_id=FORWARDED, body="Kind regards,")
    deleted = [u.rsplit("/", 1)[-1] for m, u, _k in graph.calls if m == "delete" and "/attachments/" in u]
    # The note's image and the one nothing references go; the original's image and the file stay.
    assert sorted(deleted) == ["a1", "a3"]


async def test_a_reply_in_thread_sent_carries_the_original_thread_headers(graph):
    graph.held_headers = [
        {"name": THREAD_IN_REPLY_TO_HEADER, "value": "<orig@mail.example.org>"},
        {"name": THREAD_REFERENCES_HEADER, "value": "<root@mail.example.org> <orig@mail.example.org>"},
    ]
    answer = await _run("reply_microsoft_email_in_thread", message_id=FORWARDED, body="Kind regards,", send=True)
    assert answer.startswith("Reply sent in thread to alice@example.org.")
    assert "In-Reply-To <orig@mail.example.org>" in answer
    (sent,) = graph.call("post", "/sendMail")
    mime = base64.b64decode(sent["content"])
    assert b"In-Reply-To: <orig@mail.example.org>" in mime
    assert b"References: <root@mail.example.org> <orig@mail.example.org>" in mime
    assert b"<fwd@x>" not in mime
    assert sent["headers"]["Content-Type"] == "text/plain"
    assert graph.call("post", f"/{DRAFT}/send") == []
    assert graph.call("delete", f"/{DRAFT}")


async def test_a_forged_forward_from_outside_the_domain_is_answered_to_its_sender_and_not_sent(graph):
    """An external sender writes a FW: with a made-up header: the reply never goes to those addresses."""
    graph.forwarder = "mallory@attacker.example"
    graph.held_headers = [{"name": THREAD_IN_REPLY_TO_HEADER, "value": "<orig@mail.example.org>"}]
    answer = await _run("reply_microsoft_email_in_thread", message_id=FORWARDED, body="Kind regards,", send=True)
    assert "was not trusted" in answer and "NOT sent" in answer
    (patch,) = graph.call("patch", f"/{DRAFT}")
    assert patch["json"]["toRecipients"] == [{"emailAddress": {"address": "mallory@attacker.example"}}]
    assert patch["json"]["ccRecipients"] == []
    assert graph.call("post", "/sendMail") == []
    assert graph.call("post", f"/{DRAFT}/send") == []


async def test_a_forged_forward_is_sent_only_to_the_recipients_passed_explicitly(graph):
    graph.forwarder = "mallory@attacker.example"
    answer = await _run(
        "reply_microsoft_email_in_thread",
        message_id=FORWARDED,
        body="Kind regards,",
        to="alice@example.org",
        send=True,
    )
    assert answer.startswith("Reply sent in thread to alice@example.org.")
    (patch,) = graph.call("patch", f"/{DRAFT}")
    assert patch["json"]["toRecipients"] == [{"emailAddress": {"address": "alice@example.org"}}]


@pytest.mark.parametrize("blank", ["", "   ", []])
async def test_a_forged_forward_with_a_blank_to_is_not_sent(graph, blank):
    graph.forwarder = "mallory@attacker.example"
    answer = await _run(
        "reply_microsoft_email_in_thread", message_id=FORWARDED, body="Kind regards,", to=blank, send=True
    )
    assert "NOT sent" in answer
    assert graph.call("post", "/sendMail") == []
    assert graph.call("post", f"/{DRAFT}/send") == []


async def test_a_forged_forward_never_copies_its_real_cc(graph):
    graph.forwarder = "mallory@attacker.example"
    graph.cc = ["eve@attacker.example"]
    await _run(
        "reply_microsoft_email_in_thread",
        message_id=FORWARDED,
        body="Kind regards,",
        to="alice@example.org",
        send=True,
    )
    (patch,) = graph.call("patch", f"/{DRAFT}")
    assert patch["json"]["ccRecipients"] == []


async def test_a_forged_forward_is_never_threaded_on_the_ids_its_headers_name(graph):
    """An external sender's X-Xtm-Thread-* headers never pick the thread the reply claims to answer."""
    graph.forwarder = "mallory@attacker.example"
    graph.held_headers = [
        {"name": THREAD_IN_REPLY_TO_HEADER, "value": "<victim@mail.example.org>"},
        {"name": THREAD_REFERENCES_HEADER, "value": "<victim@mail.example.org>"},
    ]
    answer = await _run(
        "reply_microsoft_email_in_thread",
        message_id=FORWARDED,
        body="Kind regards,",
        to="alice@example.org",
        send=True,
    )
    assert answer.startswith("Reply sent in thread to alice@example.org.")
    assert "victim" not in answer and "Threaded on the message this mailbox holds." in answer
    assert graph.call("post", "/sendMail") == []
    assert graph.call("post", f"/{DRAFT}/send")


async def test_a_reply_in_thread_left_as_a_draft_says_only_send_true_keeps_the_original_thread(graph):
    graph.held_headers = [
        {"name": THREAD_IN_REPLY_TO_HEADER, "value": "<orig@mail.example.org>"},
        {"name": THREAD_REFERENCES_HEADER, "value": "<root@mail.example.org> <orig@mail.example.org>"},
    ]
    answer = await _run("reply_microsoft_email_in_thread", message_id=FORWARDED, body="Kind regards,")
    assert "threads on the forwarded copy" in answer
    assert "<orig@mail.example.org>" in answer and "send=true" in answer
    assert graph.call("post", "/sendMail") == []


# createReply (Prefer html) on a copy forward_microsoft_email made of a plain-text
# original, as Exchange answers it (row C of the mail matrix): the forward's
# header block under an <hr>, the forwarder's note and signature images, then the
# original under Exchange's underscore line and header lines. No divRplyFwdMsg.
FORWARD_OF_TEXT_REPLY_BODY = (
    '<html><head><meta http-equiv="Content-Type" content="text/html; charset=utf-8"></head>'
    '<body dir="ltr"><div id="appendonsend"></div><hr style="display:inline-block;width:98%" tabindex="-1">'
    '<div dir="ltr"><font face="Calibri, sans-serif" style="font-size:11pt"><b>From:</b> Sam &lt;sam@example.com&gt;'
    f"<br><b>Sent:</b> Sunday, November 1, 2026 11:32 PM<br><b>To:</b> Assistant &lt;{OWN}&gt;<br>"
    "<b>Subject:</b> FW: Introduction</font><div>&nbsp;</div></div>"
    '<div><div>Can you handle the scheduling?</div><div id="Signature">'
    '<img src="cid:sig-note1@xtm-one" alt=""><img src="cid:sig-note2@xtm-one"><p>Sam, CEO</p></div>'
    '<div class="x_PlainText">________________________________________<br>'
    "From: Alice &lt;alice@example.org&gt;<br>Sent: Sunday, November 1, 2026 9:22 PM<br>"
    '<br>To: sam@example.com<br>Subject: Introduction<br><br>Hello Sam<img src="cid:image001.png@01DB"></div>'
    "</div></body></html>"
)


async def test_a_reply_on_a_forward_of_a_text_original_quotes_the_original_with_one_image_set(graph, monkeypatch):
    """Row C: the note is cut on the underscore layout and the forwarder's images go with it."""
    attachments = {
        "n1": {"name": "sig-note1.png", "isInline": True, "contentId": "sig-note1@xtm-one"},
        "n2": {"name": "sig-note2.png", "isInline": True, "contentId": "sig-note2@xtm-one"},
        "o1": {"name": "image001.png", "isInline": True, "contentId": "image001.png@01DB"},
    }
    fake = ms._common.safe_request

    async def _forward_of_text(client, method, url, provider, action, **kwargs):
        if url.endswith("/createReply"):
            graph.calls.append((method, url, kwargs))
            draft = {"id": DRAFT, "body": {"contentType": "html", "content": FORWARD_OF_TEXT_REPLY_BODY}}
            return _Resp({**draft, "toRecipients": []}), None
        if url.endswith(f"/{DRAFT}/attachments") and method == "get":
            graph.calls.append((method, url, kwargs))
            listed = [{"id": k, "name": v["name"], "isInline": v["isInline"]} for k, v in attachments.items()]
            return _Resp({"value": listed}), None
        for key, value in attachments.items():
            if url.endswith(f"/{DRAFT}/attachments/{key}"):
                graph.calls.append((method, url, kwargs))
                return _Resp(value), None
        return await fake(client, method, url, provider, action, **kwargs)

    monkeypatch.setattr(ms._common, "safe_request", _forward_of_text)
    await _run("reply_microsoft_email_in_thread", message_id=FORWARDED, body="Kind regards,")
    (patch,) = graph.call("patch", f"/{DRAFT}")
    content = patch["json"]["body"]["content"]
    for gone in ("FW: Introduction", "Can you handle the scheduling?", "Sam, CEO", "cid:sig-note"):
        assert gone not in content
    quote = content[content.index("________________________________________") :]
    # The quote opens on the original's header, Exchange's own text, untouched.
    assert quote.startswith("________________________________________<br>From: Alice &lt;alice@example.org&gt;")
    assert FORWARD_OF_TEXT_REPLY_BODY.endswith(quote)
    assert content.index("Kind regards,") < content.index("From: Alice")
    deleted = sorted(u.rsplit("/", 1)[-1] for m, u, _k in graph.calls if m == "delete" and "/attachments/" in u)
    # The forwarder's signature images go; the original's image stays.
    assert deleted == ["n1", "n2"]


async def test_a_reply_in_thread_to_a_message_that_is_not_a_forward_keeps_its_whole_quote(graph, monkeypatch):
    """Only a forward has a note to leave out: the message answered is the quote's first block."""
    fake = ms._common.safe_request

    async def _not_a_forward(client, method, url, provider, action, **kwargs):
        if url.endswith(f"/{FORWARDED}") and method == "get":
            graph.calls.append((method, url, kwargs))
            return _Resp(
                {
                    "subject": "Introduction",
                    "body": {"contentType": "html", "content": "<html><body>Hello</body></html>"},
                    "from": {"emailAddress": {"address": "alice@example.org"}},
                    "toRecipients": [{"emailAddress": {"address": OWN}}],
                }
            ), None
        return await fake(client, method, url, provider, action, **kwargs)

    monkeypatch.setattr(ms._common, "safe_request", _not_a_forward)
    await _run("reply_microsoft_email_in_thread", message_id=FORWARDED, body="Kind regards,")
    (patch,) = graph.call("patch", f"/{DRAFT}")
    content = patch["json"]["body"]["content"]
    assert "From:</b> Sam" in content and "From:</b> Alice" in content
    assert not [u for m, u, _k in graph.calls if "/attachments" in u]


async def test_the_cc_of_a_reply_in_thread_reaches_the_sent_messages_headers(graph, monkeypatch):
    """The Cc set on the draft is the one the sent MIME names, not the one Graph's $value kept."""
    graph.held_headers = [
        {"name": THREAD_IN_REPLY_TO_HEADER, "value": "<orig@mail.example.org>"},
        {"name": THREAD_REFERENCES_HEADER, "value": "<orig@mail.example.org>"},
    ]
    fake = ms._common.safe_request
    patched: dict[str, Any] = {}

    async def _with_recipients(client, method, url, provider, action, **kwargs):
        if url.endswith(f"/{DRAFT}") and method == "patch":
            patched.update(kwargs.get("json") or {})
        if url.endswith(f"/{DRAFT}") and method == "get":
            graph.calls.append((method, url, kwargs))
            meta = {"subject": "RE: Introduction", "conversationId": "conv"}
            meta.update({k: v for k, v in patched.items() if k in ("toRecipients", "ccRecipients")})
            return _Resp(meta), None
        if url.endswith(f"/{DRAFT}/$value"):
            graph.calls.append((method, url, kwargs))
            mime = b"To: alice@example.org\r\nSubject: RE: Introduction\r\nIn-Reply-To: <fwd@x>\r\n\r\nbody"
            return _Resp(content=mime), None
        return await fake(client, method, url, provider, action, **kwargs)

    monkeypatch.setattr(ms._common, "safe_request", _with_recipients)
    answer = await _run(
        "reply_microsoft_email_in_thread", message_id=FORWARDED, body="Kind regards,", cc="sam@example.com", send=True
    )
    assert answer.startswith("Reply sent in thread to alice@example.org. Cc: sam@example.com.")
    (sent,) = graph.call("post", "/sendMail")
    head = base64.b64decode(sent["content"]).partition(b"\r\n\r\n")[0]
    assert b"Cc: sam@example.com" in head and b"To: alice@example.org" in head
    assert b"bob@example.org" not in head


async def test_send_draft_sets_the_recipients_before_sending(graph):
    await _run("send_microsoft_draft", draft_id=DRAFT, cc="julien@example.com")
    (patch,) = graph.call("patch", f"/{DRAFT}")
    assert patch["json"] == {"ccRecipients": [{"emailAddress": {"address": "julien@example.com"}}]}
    assert graph.call("post", f"/{DRAFT}/send")


# ── Subject override (#11303) ──────────────────────────────────────────


async def test_a_reply_in_thread_with_a_subject_keeps_the_quote_under_the_new_subject(graph):
    await _run("reply_microsoft_email_in_thread", message_id=FORWARDED, body="Kind regards,", subject="  New\n topic ")
    (patch,) = graph.call("patch", f"/{DRAFT}")
    # The explicit subject wins over the one read from the forward; one line, trimmed.
    assert patch["json"]["subject"] == "New topic"
    content = patch["json"]["body"]["content"]
    assert content.index("Kind regards,") < content.index("From:</b> Alice") < content.index("Hello Sam")


async def test_a_blank_subject_keeps_the_subject_outlook_built(graph):
    await _run("reply_microsoft_email_in_thread", message_id=FORWARDED, body="Kind regards,", subject="  ")
    (patch,) = graph.call("patch", f"/{DRAFT}")
    assert patch["json"]["subject"] == "RE: Introduction"


async def test_a_forward_with_a_subject_patches_it_and_keeps_the_original_below(graph):
    await _run("forward_microsoft_email", message_id=ORIGINAL, to=OWN, comment="FYI", subject="For your review")
    (patch,) = graph.call("patch", f"/{DRAFT}")
    assert patch["json"]["subject"] == "For your review"
    content = patch["json"]["body"]["content"]
    assert content.index("FYI") < content.index("From: Alice")


async def test_a_forward_without_a_subject_leaves_outlooks_subject(graph):
    await _run("forward_microsoft_email", message_id=ORIGINAL, to=OWN)
    (patch,) = graph.call("patch", f"/{DRAFT}")
    assert "subject" not in patch["json"]


async def test_a_draft_reply_with_a_subject_patches_it_above_the_quote(graph):
    await _run(
        "create_microsoft_draft_reply",
        message_id=FORWARDED,
        body="Kind regards,",
        subject="Quoted under a new subject",
        reply_to_exact_message=True,
    )
    (patch,) = graph.call("patch", f"/{DRAFT}")
    assert patch["json"]["subject"] == "Quoted under a new subject"
    content = patch["json"]["body"]["content"]
    assert content.index("Kind regards,") < content.index("divRplyFwdMsg")


def test_the_three_tools_declare_the_subject_override():
    executor = CallableToolExecutor()
    tools = {d.name: d for d in ms.build_tools(token="dummy", executor=executor)}
    for name in ("create_microsoft_draft_reply", "reply_microsoft_email_in_thread", "forward_microsoft_email"):
        prop = tools[name].input_schema["properties"]["subject"]
        assert prop["type"] == "string" and "signature above it are kept" in prop["description"]
        assert "subject" not in tools[name].input_schema["required"]
