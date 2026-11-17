"""Gmail tools: Markdown bodies sent as HTML, reply recipients, and the read-back of a message.

The tools run for real against Gmail answered in memory (``safe_request``
replaced, no network, no token).
"""

from __future__ import annotations

import base64
from email import message_from_bytes
from email import policy as email_policy
from types import SimpleNamespace
from typing import Any

import pytest

from app.integrations.providers import google
from app.tools.base import CallableToolExecutor, ToolCall

pytestmark = pytest.mark.asyncio


class _Ok:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


ORIGINAL_HEADERS = [
    {"name": "Subject", "value": "Plan"},
    {"name": "From", "value": "alice@example.com"},
    {"name": "To", "value": "sam@example.com"},
    {"name": "Cc", "value": "bob@example.com"},
    {"name": "Message-ID", "value": "<m0@example.com>"},
    {"name": "References", "value": "<root@example.com>"},
]


@pytest.fixture
def gmail(monkeypatch):
    state = SimpleNamespace(calls=[], message=None)

    async def _fake(_client, method, url, _provider, _action, **kwargs):
        state.calls.append((method, url, kwargs))
        if method == "get" and "/users/me/messages/" in url:
            return _Ok(state.message or {"threadId": "t1", "payload": {"headers": ORIGINAL_HEADERS}}), None
        return _Ok({"id": "m1", "message": {"id": "m1"}}), None

    monkeypatch.setattr(google, "safe_request", _fake)
    return state


async def _run(name: str, **arguments: Any) -> str:
    executor = CallableToolExecutor()
    google.build_tools("dummy", executor)
    result = await executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments))
    return result.content


def _sent(gmail, suffix: str) -> tuple[Any, dict]:
    (payload,) = [kw["json"] for method, url, kw in gmail.calls if method == "post" and url.endswith(suffix)]
    inner = payload.get("message", payload)
    raw = base64.urlsafe_b64decode(inner["raw"] + "=" * (-len(inner["raw"]) % 4))
    return message_from_bytes(raw, policy=email_policy.default), payload


def _part(message, kind: str) -> str:
    return next(p for p in message.walk() if p.get_content_type() == kind).get_content()


# -- G-A: a Markdown body is sent as HTML ------------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments", "suffix"),
    [
        ("send_gmail", {"to": "a@example.com", "subject": "Hi"}, "/users/me/messages/send"),
        ("create_gmail_draft", {"to": "a@example.com", "subject": "Hi"}, "/users/me/drafts"),
        ("reply_gmail", {"message_id": "m0"}, "/users/me/messages/send"),
    ],
)
async def test_markdown_body_is_sent_as_html_with_a_text_alternative(gmail, tool, arguments, suffix):
    body = "Points:\n\n- **first** item\n- second, see [the doc](https://example.com/doc)"
    await _run(tool, body=body, **arguments)
    message, _payload = _sent(gmail, suffix)
    html, plain = _part(message, "text/html"), _part(message, "text/plain")
    assert "<ul" in html and html.count("<li") == 2
    assert "<strong>first</strong>" in html
    assert 'href="https://example.com/doc"' in html
    assert "Arial" in html and "14px" in html
    assert plain.startswith("Points:\n\n- **first** item")


async def test_a_tag_in_a_text_body_shows_as_written(gmail):
    await _run("send_gmail", to="a@example.com", subject="Hi", body="Use <b> for bold")
    message, _payload = _sent(gmail, "/users/me/messages/send")
    html = _part(message, "text/html")
    assert "&lt;b&gt;" in html and "<b>" not in html


async def test_an_explicit_html_body_is_sent_as_given(gmail):
    await _run("send_gmail", to="a@example.com", subject="Hi", body="B", body_html="<p>Exact</p>")
    message, _payload = _sent(gmail, "/users/me/messages/send")
    assert _part(message, "text/html").strip() == "<p>Exact</p>"


# -- G-B: reply_gmail recipients ----------------------------------------------


async def test_reply_keeps_the_thread_and_takes_the_given_recipients(gmail):
    answer = await _run(
        "reply_gmail",
        message_id="m0",
        body="Adding Carol.",
        cc="carol@example.com, bob@example.com",
        bcc="d@example.com",
    )
    message, payload = _sent(gmail, "/users/me/messages/send")
    assert payload["threadId"] == "t1"
    assert message["To"] == "alice@example.com"
    assert message["Cc"] == "carol@example.com, bob@example.com"
    assert message["Bcc"] == "d@example.com"
    assert message["Subject"] == "Re: Plan"
    assert message["In-Reply-To"] == "<m0@example.com>"
    assert message["References"] == "<root@example.com> <m0@example.com>"
    assert "Thread ID: t1" in answer


async def test_reply_to_replaces_the_sender(gmail):
    await _run("reply_gmail", message_id="m0", body="Over to you.", to="eve@example.com", reply_all=True)
    message, _payload = _sent(gmail, "/users/me/messages/send")
    assert message["To"] == "eve@example.com"
    # reply_all with no cc keeps the original recipients in copy.
    assert "bob@example.com" in message["Cc"]


async def test_reply_without_overrides_answers_the_sender(gmail):
    await _run("reply_gmail", message_id="m0", body="Thanks")
    message, _payload = _sent(gmail, "/users/me/messages/send")
    assert message["To"] == "alice@example.com"
    assert message["Cc"] is None and message["Bcc"] is None


# -- G-C: get_gmail_message returns the text once -----------------------------


async def test_read_back_returns_the_text_part_only(gmail):
    gmail.message = {
        "id": "m9",
        "threadId": "t9",
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "From", "value": "Samuel"}],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64url(b"Test reply\n- one")}},
                {"mimeType": "text/html", "body": {"data": _b64url(b"<p>Test reply</p><ul><li>one</li></ul>")}},
            ],
        },
    }
    answer = await _run("get_gmail_message", message_id="m9")
    body = answer.split("Body:\n", 1)[1]
    assert body.strip() == "Test reply\n- one"


async def test_read_back_of_an_html_only_message_keeps_words_apart(gmail):
    gmail.message = {
        "id": "m9",
        "threadId": "t9",
        "labelIds": [],
        "payload": {
            "mimeType": "text/html",
            "headers": [],
            "body": {"data": _b64url(b"<p>Samuel</p><p>Test reply</p>")},
        },
    }
    answer = await _run("get_gmail_message", message_id="m9")
    assert "Samuel Test reply" in answer
