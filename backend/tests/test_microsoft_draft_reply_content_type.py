"""The Outlook draft reply renders its body by ``content_type``.

``create_microsoft_draft_reply`` (reply, and reply-all through ``reply_all``)
escaped every line of its body, so an HTML body reached the recipient as raw
tags and Markdown as asterisks and pipes. ``content_type`` now says how the
body is rendered: ``text`` (the default, unchanged byte for byte), ``html`` and
``markdown``, the last two through the normalizer ``send_microsoft_email`` uses.
Either way only the body of the ``createReply`` draft is patched, so the
conversation and the quoted original stay.

The thread test runs the tools against a fake Microsoft Graph mailbox served
through ``httpx.MockTransport`` (``safe_request`` and ``AutoRefreshClient`` are
the production paths), starting from ``data/outlook_reply/create_reply.json``:
the documented shape of a ``createReply`` draft and of the message it answers,
with made-up ids and addresses, trimmed to the fields the tool reads or must
leave alone. It is not a recording of a live mailbox.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import app.integrations.providers.microsoft as ms
from app.integrations.providers.microsoft import build_tools, mail
from app.integrations.providers.microsoft._platform_sent import platform_sent_properties
from app.services.email_html import (
    DEFAULT_COMPOSE_STYLE,
    build_graph_email_body,
    close_before_signature,
    markdown_to_email_html,
    starts_as_html_markup,
)
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

MESSAGE_ID = "AAMkAGI2NGVhZTVlLTI1OGMtNDI4My1iZmVmLTI1ZmM5OWM5MTIwNgBGAAAAAADUuTJK1K9aTpCdqXop_4NaBwCd9nJ-tVys"
QUOTED = '<hr style="display:inline-block"><div id="divRplyFwdMsg">From: Alice</div><div>Original question</div>'
DRAFT_BODY = f'<html><body><div id="appendonsend"></div>{QUOTED}</body></html>'
SIGNATURE = '<div id="Signature"><p>Sam - copied from sent mail</p></div>'
HTML_BODY = (
    "<h3>Q3 patch status</h3>"
    "<table><tr><th>Fleet</th><th>State</th></tr><tr><td>Servers</td><td>Done</td></tr></table>"
    "<p>Sam</p>"
)
WARNING = "body looks like HTML but content_type=text"
#: The heading of HTML_BODY as sent: its own size kept, Outlook's compose font and colour added.
HEADING = (
    '<h3 style="margin-top:0;margin-bottom:12pt">'
    '<span style="font-family:Aptos,Calibri,Arial,sans-serif;color:rgb(0,0,0)">Q3 patch status</span></h3>'
)


class _Ok:
    def __init__(self, payload: dict | None = None):
        self._payload = payload or {}
        self.status_code = 200

    def json(self):
        return self._payload


@pytest.fixture
def graph(monkeypatch):
    """Graph answered in memory through ``safe_request``; every request recorded."""
    calls: list[tuple[str, str, dict]] = []

    async def _fake(_client, method, url, _provider, _action, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith(("/createReply", "/createReplyAll")):
            recipients = [{"emailAddress": {"address": "alice@example.com"}}]
            return _Ok({"id": "draft-1", "body": {"content": DRAFT_BODY}, "toRecipients": recipients}), None
        if "sentitems" in url:
            return _Ok({"value": [{"body": {"content": f"<html><body><p>Old</p>{SIGNATURE}</body></html>"}}]}), None
        return _Ok({"id": "draft-1"}), None

    async def _quiet(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ms._common, "safe_request", _fake)
    monkeypatch.setattr(mail, "record_mail_draft", _quiet)
    monkeypatch.setattr(mail, "record_mail_send", _quiet)
    return calls


def _executor() -> tuple[CallableToolExecutor, dict]:
    executor = CallableToolExecutor()
    definitions = build_tools(token="dummy", executor=executor)
    return executor, {d.name: d for d in definitions}


async def _call(executor: CallableToolExecutor, name: str, **arguments: Any) -> ToolResult:
    return await executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments))


async def _reply(executor: CallableToolExecutor, **arguments: Any) -> ToolResult:
    return await _call(executor, "create_microsoft_draft_reply", message_id=MESSAGE_ID, **arguments)


def _patch(calls) -> dict:
    (patch,) = [kwargs["json"] for method, _url, kwargs in calls if method == "patch"]
    return patch


def _patched_body(calls) -> str:
    return _patch(calls)["body"]["content"]


def _composed(block: str) -> str:
    """The draft as patched: *block*, its sign-off with no bottom margin (#8522), and the signature above Outlook's ``<hr>``."""
    at = DRAFT_BODY.index("<hr")
    return DRAFT_BODY[:at] + close_before_signature(block) + SIGNATURE + "\r\n" + DRAFT_BODY[at:]


REPLY_KINDS = [pytest.param(False, id="reply"), pytest.param(True, id="reply_all")]


# -- text: Markdown rendered, its tags literal ----------------------


@pytest.mark.parametrize("reply_all", REPLY_KINDS)
async def test_without_content_type_the_reply_renders_paragraphs_and_keeps_tags_literal(graph, reply_all):
    executor, _ = _executor()
    result = await _reply(executor, body="Hi Alice,\n\nWe use <b>bold</b> & more\nSam", reply_all=reply_all)

    assert not result.is_error and WARNING not in result.content
    content = _patched_body(graph)
    head = content[: content.index("<hr")]
    # Outlook's own compose style: dark mode inverts it as it does the quoted thread, never grey.
    # Each paragraph carries its own bottom margin, inline, so Outlook's head p {margin:0} cannot glue them.
    span = f'<span style="{DEFAULT_COMPOSE_STYLE}">'
    spaced = '<p class="elementToProof" style="margin-top:0;margin-bottom:12pt">'
    assert f"<div>{spaced}{span}Hi Alice,</span></p>" in head
    assert "<font" not in head
    # The last paragraph, right above the signature, keeps no bottom margin (#8522).
    signoff = '<p class="elementToProof" style="margin-top:0;margin-bottom:0">'
    assert f"{signoff}{span}We use &lt;b&gt;bold&lt;/b&gt; &amp; more<br/>" in head and "Sam</span></p>" in head
    assert "<b>" not in head
    assert content == _composed(head[content.index(f"<div>{spaced}{span}Hi") :].split(SIGNATURE)[0])
    # The body, and the mark of a message this platform composed (_platform_sent).
    assert set(_patch(graph)) == {"body", "singleValueExtendedProperties"}
    assert _patch(graph)["body"]["contentType"] == "HTML"
    (create,) = [(url, kwargs) for method, url, kwargs in graph if method == "post"]
    assert create[0].endswith("/createReplyAll" if reply_all else "/createReply")
    assert create[1]["json"] == {}


@pytest.mark.parametrize("content_type", ["text", " Text ", None, ""], ids=["text", "padded", "null", "empty"])
async def test_explicit_text_patches_exactly_what_the_default_does(graph, content_type):
    executor, _ = _executor()
    body = "Thanks,\nwill do <today>"
    await _reply(executor, body=body)
    default = _patch(graph)
    graph.clear()

    result = await _reply(executor, body=body, content_type=content_type)

    assert not result.is_error
    assert _patch(graph) == default


# -- html and markdown: rendered like a new email ---------------------------


@pytest.mark.parametrize("reply_all", REPLY_KINDS)
async def test_an_html_reply_renders_its_heading_and_table_above_the_quoted_thread(graph, reply_all):
    executor, _ = _executor()
    result = await _reply(executor, body=HTML_BODY, content_type="html", reply_all=reply_all)

    assert not result.is_error and WARNING not in result.content
    rendered = markdown_to_email_html(HTML_BODY, assume_html=True)
    assert _patch(graph) == {
        "body": {"contentType": "HTML", "content": _composed(f"<div>{rendered}</div>\r\n")},
        "singleValueExtendedProperties": platform_sent_properties(),
    }
    content = _patched_body(graph)
    head = content[: content.index("<hr")]
    assert HEADING in head
    assert "<table style=" in head and "<th style=" in head
    assert "&lt;" not in head
    assert content.endswith(f"{QUOTED}</body></html>")


@pytest.mark.parametrize("reply_all", REPLY_KINDS)
async def test_a_markdown_reply_is_converted_by_the_shared_converter(graph, reply_all):
    executor, _ = _executor()
    body = "**Status**\n\n| Fleet | State |\n|---|---|\n| Servers | Done |\n\n[Runbook](https://example.com/runbook)"

    result = await _reply(executor, body=body, content_type="markdown", reply_all=reply_all)

    assert not result.is_error
    content = _patched_body(graph)
    rendered = markdown_to_email_html(body, assume_html=False)
    assert content == _composed(f"<div>{rendered}</div>\r\n")
    assert "<strong>Status</strong>" in rendered and "<table style=" in rendered
    assert '<a href="https://example.com/runbook"' in rendered
    assert "**" not in content and "|---|" not in content


async def test_html_is_sanitized_exactly_as_send_microsoft_email_sanitizes_it(graph):
    executor, _ = _executor()
    body = (
        '<p onclick="steal()">Hi</p><script>alert(1)</script>'
        '<table><tr><td>x</td></tr></table><a href="javascript:alert(1)">bad</a>'
    )
    await _call(executor, "send_microsoft_email", to="alice@example.com", subject="S", body=body, content_type="html")
    (send,) = [kwargs for _m, url, kwargs in graph if url.endswith("/sendMail")]
    new_email = send["json"]["message"]["body"]["content"]
    graph.clear()

    await _reply(executor, body=body, content_type="html")

    content = _patched_body(graph)
    assert f"<div>{new_email}</div>" in content
    assert "script" not in content and "onclick" not in content and "javascript:" not in content


@pytest.mark.parametrize("content_type", ["rtf", "HTML5", "md", 1], ids=["rtf", "html5", "md", "number"])
async def test_an_unknown_content_type_is_refused_before_any_request(graph, content_type):
    executor, _ = _executor()
    result = await _reply(executor, body="Hi", content_type=content_type)

    assert result.is_error
    assert "content_type must be 'text', 'html' or 'markdown'" in result.content
    assert "No draft was created" in result.content
    assert graph == []


# -- the guard: HTML sent as text is said, never switched -------------------


@pytest.mark.parametrize(
    ("body", "content_type", "warned"),
    [
        pytest.param("<p>Hi Alice</p><table><tr><td>x</td></tr></table>", None, True, id="default"),
        pytest.param("  <h3>Status</h3>\nDone", "text", True, id="text-heading"),
        pytest.param("<p>Hi Alice</p>", "html", False, id="html"),
        pytest.param("<p>**Hi**</p>", "markdown", False, id="markdown"),
        pytest.param("Use the <table> element, then </table>", "text", False, id="text-mentions-a-tag"),
        pytest.param("<team> please check", "text", False, id="text-opens-no-element"),
    ],
)
async def test_html_sent_as_text_gets_a_warning_and_its_tags_stay_literal(graph, body, content_type, warned):
    executor, _ = _executor()
    arguments: dict[str, Any] = {"body": body}
    if content_type is not None:
        arguments["content_type"] = content_type

    result = await _reply(executor, **arguments)

    assert not result.is_error and "Draft reply created" in result.content
    assert (WARNING in result.content) is warned
    if warned:
        assert "Pass content_type='html'" in result.content
        assert "&lt;" in _patched_body(graph).split("<hr")[0]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("<p>Hi</p>", True),
        ("\n  <div><b>Hi</b></div>", True),
        ("<!DOCTYPE html><html><body>Hi</body></html>", True),
        ("<p>Hi", False),
        ("Hi <p>there</p>", False),
        ("<team> and </team", False),
        ("", False),
    ],
)
def test_starts_as_html_markup(body, expected):
    assert starts_as_html_markup(body) is expected


# -- the schema the model reads ---------------------------------------------


def test_the_reply_tool_declares_content_type_and_the_new_email_tools_keep_theirs():
    _, tools = _executor()
    reply = tools["create_microsoft_draft_reply"]
    prop = reply.input_schema["properties"]["content_type"]
    assert prop["enum"] == ["text", "html", "markdown"] and prop["default"] == "text"
    assert "content_type" not in reply.input_schema["required"]
    assert "rendered according to content_type" in reply.description
    assert "shows its raw tags" in reply.description
    assert "content_type='html', body='<h3>" in reply.description
    for name in ("send_microsoft_email", "create_microsoft_draft_email"):
        new_email = tools[name].input_schema["properties"]["content_type"]
        assert new_email["enum"] == ["text", "html"] and new_email["default"] == "text"


@pytest.mark.parametrize("content_type", ["text", "html"])
async def test_a_new_email_body_is_still_the_shared_normalizer_output(graph, content_type):
    executor, _ = _executor()
    body = "**Hi**\n\n| a | b |\n|---|---|\n| 1 | 2 |" if content_type == "text" else HTML_BODY

    await _call(executor, "send_microsoft_email", to="a@example.com", subject="S", body=body, content_type=content_type)
    await _call(
        executor, "create_microsoft_draft_email", to="a@example.com", subject="S", body=body, content_type=content_type
    )

    (send,) = [kwargs for _m, url, kwargs in graph if url.endswith("/sendMail")]
    (draft,) = [kwargs for m, url, kwargs in graph if m == "post" and url.endswith("/me/messages")]
    expected = build_graph_email_body(body, content_type)
    assert send["json"]["message"]["body"] == expected
    assert draft["json"]["body"] == expected


# -- the thread: the sent reply is the createReply draft --------------------

FIXTURE = json.loads(
    (Path(__file__).parent / "data" / "outlook_reply" / "create_reply.json").read_text(encoding="utf-8")
)
ORIGINAL = FIXTURE["original"]
DRAFT = FIXTURE["draft"]


class MailboxGraph:
    """A Graph mailbox in memory: ``createReply`` makes the fixture draft, PATCH merges, ``send`` sends."""

    def __init__(self) -> None:
        self.messages: dict[str, dict] = {ORIGINAL["id"]: copy.deepcopy(ORIGINAL)}
        self.sent: list[dict] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/v1.0")
        if request.method == "GET" and path == "/me/mailFolders/sentitems/messages":
            return httpx.Response(200, json={"value": []})
        if request.method == "POST" and path in (
            f"/me/messages/{ORIGINAL['id']}/createReply",
            f"/me/messages/{ORIGINAL['id']}/createReplyAll",
        ):
            draft = copy.deepcopy(DRAFT)
            if path.endswith("/createReplyAll"):
                draft["ccRecipients"] = copy.deepcopy(ORIGINAL["ccRecipients"])
            self.messages[draft["id"]] = draft
            return httpx.Response(201, json=draft)
        message_id, _, action = path.removeprefix("/me/messages/").partition("/")
        message = self.messages.get(message_id)
        if message is not None and request.method == "GET" and not action:
            # send_microsoft_draft reads the draft (with its body whenever
            # signatures are on) before it sends it.
            return httpx.Response(200, json=message)
        if message is not None and request.method == "PATCH" and not action:
            message.update(json.loads(request.content))
            return httpx.Response(200, json=message)
        if message is not None and request.method == "POST" and action == "send":
            message["isDraft"] = False
            self.sent.append(message)
            return httpx.Response(202)
        raise AssertionError(f"no fake route for {request.method} {request.url}")


@pytest.fixture
def mailbox(monkeypatch) -> MailboxGraph:
    fake = MailboxGraph()
    real_client = httpx.AsyncClient

    class _Client(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(fake)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return fake


@pytest.mark.parametrize("reply_all", REPLY_KINDS)
async def test_an_html_reply_is_sent_in_the_thread_with_the_quoted_original(mailbox, reply_all):
    executor = CallableToolExecutor()
    ms.build_tools("tok", executor)

    created = await _call(
        executor,
        "create_microsoft_draft_reply",
        message_id=ORIGINAL["id"],
        body=HTML_BODY,
        content_type="html",
        reply_all=reply_all,
    )
    sent = await _call(executor, "send_microsoft_draft", draft_id=DRAFT["id"])

    assert not created.is_error and f"Draft ID: {DRAFT['id']}" in created.content
    assert not sent.is_error
    (message,) = mailbox.sent
    assert message["id"] == DRAFT["id"]
    assert message["conversationId"] == ORIGINAL["conversationId"]
    assert message["subject"] == "RE: Q3 patch status"
    assert [r["emailAddress"]["address"] for r in message["ccRecipients"]] == (["bob@contoso.com"] if reply_all else [])

    content = message["body"]["content"]
    assert message["body"]["contentType"] == "HTML"
    quoted = DRAFT["body"]["content"]
    assert content.endswith(quoted[quoted.index("<hr") :])
    assert "<b>From:</b> Alice Martin" in content and "Where are we on the Q3 patch rollout?" in content
    head = content[: content.index("<hr")]
    assert HEADING in head
    assert "<table style=" in head and "&lt;" not in head

    # The reply never became a new message (a new thread) and its body never went through `comment`.
    (create,) = [r for r in mailbox.requests if r.method == "POST" and "/createReply" in r.url.path]
    assert create.url.path.endswith("/createReplyAll" if reply_all else "/createReply")
    assert json.loads(create.content) == {}
    assert [set(json.loads(r.content)) for r in mailbox.requests if r.method == "PATCH"] == [
        {"body", "singleValueExtendedProperties"}
    ]
    # A GET of /me/messages is the conversation read of the reply-on-latest rule (#11069).
    assert not [
        r for r in mailbox.requests if r.method != "GET" and r.url.path in ("/v1.0/me/messages", "/v1.0/me/sendMail")
    ]


# -- the integration's Compose style setting -------------------------------


@pytest.mark.parametrize("reply_all", REPLY_KINDS)
async def test_the_integrations_compose_style_is_the_style_of_the_reply(graph, reply_all):
    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor, compose_style="font-family:Arial;color:#222")
    await _reply(executor, body="Hi Alice,", reply_all=reply_all)
    head = _patched_body(graph).split("<hr")[0]
    assert '<span style="font-family:Arial;color:#222">Hi Alice,</span>' in head
    assert "Aptos" not in head


def test_the_compose_style_is_read_from_the_integration_row(monkeypatch):
    import app.services.integration_tools as integration_tools

    monkeypatch.setattr(integration_tools, "_decrypt_dict", lambda enc: {"compose_style": f" {enc} "})
    assert integration_tools._compose_style_of(SimpleNamespace(extra_config_enc="none")) == "none"
    assert integration_tools._compose_style_of(SimpleNamespace(extra_config_enc=None)) is None
    assert integration_tools._compose_style_of(None) is None
    monkeypatch.setattr(integration_tools, "_decrypt_dict", lambda enc: {"compose_style": "  "})
    assert integration_tools._compose_style_of(SimpleNamespace(extra_config_enc="x")) is None

    def _broken(_enc):
        raise ValueError("rotated key")

    monkeypatch.setattr(integration_tools, "_decrypt_dict", _broken)
    assert integration_tools._compose_style_of(SimpleNamespace(id="i", extra_config_enc="x")) is None
