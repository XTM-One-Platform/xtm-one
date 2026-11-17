"""A native reply or forward draft keeps Outlook's quote as HTML, with its line breaks.

``createReply`` / ``createReplyAll`` / ``createForward`` answer a draft in the
body type of the message answered. A plain-text message gave a ``text`` draft
whose quote, patched under an HTML body, collapsed into one line. The draft is
now asked for in HTML, re-read in HTML when Graph still answers text, and as a
last resort kept as Graph's own text: no separator, header block or marker of
the tools' own is ever written.
"""

from __future__ import annotations

from typing import Any

import pytest

import app.integrations.providers.microsoft as ms
from app.integrations.providers.microsoft import mail
from app.integrations.providers.microsoft._draft_body import (
    HTML_BODY_PREFERENCE,
    html_draft_body,
    text_body_as_html,
)
from app.integrations.providers.microsoft._thread_reply import drop_forward_header
from app.services.email_html import DEFAULT_COMPOSE_STYLE
from app.tools.base import CallableToolExecutor, ToolCall

MESSAGE_ID = "AAMkAGI2NGVhZTVlLTI1OGMtNDI4My1iZmVmLTI1ZmM5OWM5MTIwNgBGAAAAAADUuTJK1K9aTpCdqXop_4NaBwCd9nJ-tVys"
DRAFT = f"{MESSAGE_ID}DRAFT"
TEXT_QUOTE = (
    "\r\n\r\n________________________________\r\n"
    "From: Test sender <sender@example.org>\r\n"
    "Sent: Sunday, 01 November 2026 21:22:23\r\n"
    "To: Sam <sam@example.com>\r\n"
    "Subject: Test thread\r\n\r\n"
    "Hello Samuel,\r\n\r\nThis is a test message.\r\n\r\nBest,\r\nTest sender\r\n"
)
HTML_QUOTE = (
    '<html><body><div id="appendonsend"></div><hr style="display:inline-block;width:98%">'
    '<div id="divRplyFwdMsg"><b>From:</b> Test sender<br><b>Sent:</b> Sunday<br><b>To:</b> Sam<br>'
    "<b>Subject:</b> Test thread</div><div>Hello Samuel,<br><br>This is a test message.</div></body></html>"
)


class _Ok:
    def __init__(self, payload: dict | None = None):
        self._payload = payload or {}
        self.status_code = 200

    def json(self):
        return self._payload


def test_a_text_body_is_kept_as_graphs_text_with_nothing_of_our_own():
    """Sam's rule: no <hr>, no header block, no divRplyFwdMsg written by the tools."""
    html = text_body_as_html(TEXT_QUOTE + "<b>not a tag</b>")
    assert "<hr" not in html and "divRplyFwdMsg" not in html and "<b>" not in html
    inner = html[html.index('<div style="white-space:pre-wrap">') + len('<div style="white-space:pre-wrap">') :]
    inner = inner[: inner.rindex("</div>")]
    # Graph's own text, its line breaks kept, only escaped.
    assert inner == (TEXT_QUOTE + "&lt;b&gt;not a tag&lt;/b&gt;").replace("\r\n", "\n").replace(
        "<sender@example.org>", "&lt;sender@example.org&gt;"
    ).replace("<sam@example.com>", "&lt;sam@example.com&gt;")


def test_a_text_body_of_a_forward_can_still_be_cut_on_the_original_message():
    """The provider's own underscore separator and header lines are the boundaries: the note is dropped."""
    forward = (
        "\r\n________________________________\r\nFrom: Sam <sam@example.com>\r\nSent: Sunday\r\n"
        "Subject: FW: Test thread\r\n\r\nCan you handle the scheduling?\r\n____________________\r\n"
        "Sam, CEO\r\n" + TEXT_QUOTE
    )
    cut = drop_forward_header(text_body_as_html(forward))
    assert "Can you handle" not in cut and "Sam, CEO" not in cut and "FW: Test thread" not in cut
    assert cut.index("________________________________\nFrom: Test sender") < cut.index("Hello Samuel,")


async def test_an_html_draft_is_used_as_it_is_without_a_second_read():
    calls: list[Any] = []

    async def _request(*args, **kwargs):
        calls.append(args)
        return _Ok(), None

    draft = {"body": {"contentType": "html", "content": HTML_QUOTE}}
    assert await html_draft_body(_request, None, "https://graph/me/messages/d", draft) == HTML_QUOTE
    assert calls == []


@pytest.mark.parametrize(
    ("reread", "expected"),
    [
        pytest.param({"body": {"contentType": "html", "content": HTML_QUOTE}}, HTML_QUOTE, id="reread-html"),
        pytest.param({"body": {"contentType": "text", "content": TEXT_QUOTE}}, None, id="still-text"),
        pytest.param(None, None, id="read-failed"),
    ],
)
async def test_a_text_draft_is_read_again_as_html_else_kept_as_text(reread, expected):
    seen: list[dict] = []

    async def _request(_client, method, url, _provider, _action, **kwargs):
        seen.append({"method": method, "url": url, **kwargs})
        if reread is None:
            return None, "Error: HTTP 503"
        return _Ok(reread), None

    draft = {"body": {"contentType": "text", "content": TEXT_QUOTE}}
    html = await html_draft_body(_request, None, "https://graph/me/messages/d", draft)
    (read,) = seen
    assert read["method"] == "get" and read["headers"] == {"Prefer": HTML_BODY_PREFERENCE}
    assert html == (expected if expected is not None else text_body_as_html(TEXT_QUOTE))


@pytest.fixture
def text_mailbox(monkeypatch):
    """Graph that answers createReply with a TEXT draft, as for a plain-text message, and HTML once asked."""
    calls: list[tuple[str, str, dict]] = []

    async def _fake(_client, method, url, _provider, _action, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith(("/createReply", "/createReplyAll", "/createForward")):
            return _Ok({"id": DRAFT, "body": {"contentType": "text", "content": TEXT_QUOTE}, "toRecipients": []}), None
        if url.endswith(f"/{DRAFT}") and method == "get":
            if (kwargs.get("headers") or {}).get("Prefer") == HTML_BODY_PREFERENCE:
                return _Ok({"body": {"contentType": "html", "content": HTML_QUOTE}}), None
            return _Ok({"subject": "S", "toRecipients": []}), None
        return _Ok({"id": "x"}), None

    async def _quiet(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ms._common, "safe_request", _fake)
    monkeypatch.setattr(mail, "record_mail_draft", _quiet)
    monkeypatch.setattr(mail, "record_mail_send", _quiet)
    return calls


async def _run(name: str, **arguments: Any) -> str:
    executor = CallableToolExecutor()
    ms.build_tools(token="dummy", executor=executor)
    return (await executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments))).content


@pytest.mark.parametrize("reply_all", [False, True], ids=["reply", "reply_all"])
async def test_a_reply_draft_to_a_text_message_carries_outlooks_html_quote(text_mailbox, reply_all):
    body = "Hello,\n\nHere are the slots:\n- Monday 10:00\n- Tuesday 14:00\n\nBest,"
    await _run("create_microsoft_draft_reply", message_id=MESSAGE_ID, body=body, reply_all=reply_all)
    (create,) = [k for m, u, k in text_mailbox if m == "post" and "/createReply" in u]
    assert create["headers"]["Prefer"] == HTML_BODY_PREFERENCE and create["headers"]["Authorization"]
    (patch,) = [k["json"] for m, u, k in text_mailbox if m == "patch" and u.endswith(f"/{DRAFT}")]
    content = patch["body"]["content"]
    assert patch["body"]["contentType"] == "HTML"
    # The quote is the one Outlook built in HTML, whole, under the agent's text.
    assert content.endswith(HTML_QUOTE[HTML_QUOTE.index("<hr") :])
    head = content[: content.index("<hr")]
    assert '<ul style="margin-top:0;margin-bottom:12pt">' in head
    assert f'<li><span style="{DEFAULT_COMPOSE_STYLE}">Monday 10:00</span></li>' in head and "- Monday" not in head
    assert "________" not in content


async def test_a_forward_of_a_text_message_carries_outlooks_html_quote(text_mailbox):
    await _run("forward_microsoft_email", message_id=MESSAGE_ID, to="alice@example.org", comment="FYI")
    (create,) = [k for m, u, k in text_mailbox if m == "post" and u.endswith("/createForward")]
    assert create["headers"]["Prefer"] == HTML_BODY_PREFERENCE
    (patch,) = [
        k["json"] for m, u, k in text_mailbox if m == "patch" and u.endswith(f"/{DRAFT}") and "body" in k["json"]
    ]
    assert patch["body"]["content"].endswith(HTML_QUOTE[HTML_QUOTE.index("<hr") :])
