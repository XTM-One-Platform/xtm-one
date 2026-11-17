"""Every message the mail tools compose is stamped, and the listings say which messages carry the stamp.

Telling a platform send from a message typed in Outlook took a raw read of
each Sent Items message (20k to 40k tokens for one boolean). The tools now
stamp what they compose with the ``X-Xtm-One-Sent`` internet header named
property, and ``list_microsoft_emails`` / ``get_microsoft_email`` read it with
one ``$expand`` in the request they already make.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from app.integrations.providers import microsoft as ms
from app.integrations.providers.microsoft import build_tools
from app.integrations.providers.microsoft._platform_sent import (
    PLATFORM_SENT_EXPAND,
    PLATFORM_SENT_HEADER,
    PLATFORM_SENT_PROPERTY_ID,
    is_platform_sent,
    platform_sent_properties,
    stamp_platform_sent,
)
from app.integrations.providers.microsoft.mail import format_email_line
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall

_STAMP = {"id": PLATFORM_SENT_PROPERTY_ID, "value": "xtm-one"}
_PAD = "x" * 133


def _id(index: int) -> str:
    """A message id of a Graph id's length (the tools refuse a short one)."""
    return f"AAMkAGI2msg{index:04d}{_PAD}"


def _message(index: int, *, stamped: bool) -> dict[str, Any]:
    message: dict[str, Any] = {
        "id": _id(index),
        "subject": f"Subject {index}",
        "from": {"emailAddress": {"name": "Me", "address": "me@example.com"}},
        "receivedDateTime": f"2026-11-0{1 + index}T08:00:00Z",
        "bodyPreview": "Hello",
        "isRead": True,
        "conversationId": f"AAQkconv{index:04d}",
        "internetMessageId": f"<msg{index}@example.com>",
        "body": {"contentType": "html", "content": "<p>Hello</p>"},
    }
    if stamped:
        message["singleValueExtendedProperties"] = [dict(_STAMP)]
    return message


class _Response:
    def __init__(self, payload: Any = None, content: bytes = b"") -> None:
        self._payload = payload
        self.content = content
        self.status_code = 200

    def json(self) -> Any:
        return self._payload


@pytest.fixture
def graph(monkeypatch):
    """A mailbox holding one platform-sent and one Outlook-typed message; every request recorded."""
    requests: list[tuple[str, str, dict]] = []
    listed = [_message(0, stamped=True), _message(1, stamped=False)]

    async def fake_safe_request(_client, method, url, _provider, _op, **kwargs):
        requests.append((method, url, kwargs))
        if "/mailFolders/" in url:
            return _Response({"value": listed}), None
        if url.endswith("/$value"):
            return _Response(content=b"Subject: Hi\r\nTo: a@example.com\r\n\r\nBody"), None
        if method == "post" and (url.endswith("/me/messages") or url.endswith("/createForward")):
            return _Response({"id": f"AAMkAGI2draft{_PAD}", "conversationId": "conv-1"}), None
        if method == "get" and "/me/messages/" in url:
            index = 0 if "msg0000" in url else 1
            return _Response(_message(index, stamped=index == 0)), None
        return _Response({}), None

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)
    return requests


async def _call(tool: str, **arguments: Any):
    executor = _SafeIntegrationExecutor()
    build_tools("token", executor)
    return await executor.execute_tool(ToolCall(id="c1", name=tool, arguments=arguments))


def test_the_stamp_is_added_once_and_read_back():
    message: dict[str, Any] = {"singleValueExtendedProperties": [{"id": "String {x} Name other", "value": "1"}]}
    stamp_platform_sent(stamp_platform_sent(message))
    assert message["singleValueExtendedProperties"] == [{"id": "String {x} Name other", "value": "1"}, _STAMP]
    assert is_platform_sent(message)
    assert not is_platform_sent({})
    assert not is_platform_sent({"singleValueExtendedProperties": [{"id": "String {x} Name other", "value": "1"}]})
    assert not is_platform_sent({"singleValueExtendedProperties": [{"id": PLATFORM_SENT_PROPERTY_ID, "value": ""}]})
    assert platform_sent_properties() == [_STAMP]


def test_the_compact_line_names_a_platform_send_in_its_state():
    assert format_email_line(_message(0, stamped=True)).endswith(" | platform-sent")
    assert format_email_line(_message(1, stamped=False)).endswith(" | read")


@pytest.mark.asyncio
async def test_the_sent_items_listing_flags_each_message_in_the_same_call(graph):
    result = await _call("list_microsoft_emails", folder="sentitems", count=2, include_preview=False)
    assert result.is_error is False
    assert len(graph) == 1
    assert graph[0][2]["params"]["$expand"] == PLATFORM_SENT_EXPAND
    lines = result.content.split("\n")
    assert "platform-sent = composed by this platform" in lines[0]
    assert lines[1].endswith(" | platform-sent")
    assert lines[2].endswith(" | read")


@pytest.mark.asyncio
async def test_the_full_listing_and_the_single_read_say_sent_by_platform(graph):
    full = await _call("list_microsoft_emails", folder="sentitems", count=2)
    assert full.content.count("Sent by platform: Yes\n") == 1
    assert full.content.count("Sent by platform: No\n") == 1
    one = await _call("get_microsoft_email", message_id=_id(0))
    assert "Sent by platform: Yes\n" in one.content
    assert f"$expand={PLATFORM_SENT_EXPAND}" in graph[-1][1]
    other = await _call("get_microsoft_email", message_id=_id(1))
    assert "Sent by platform: No\n" in other.content


@pytest.mark.asyncio
async def test_a_new_email_and_a_new_draft_are_stamped(graph):
    await _call("send_microsoft_email", to="a@example.com", subject="Hi", body="Hello")
    sent = next(kwargs["json"] for method, url, kwargs in graph if url.endswith("/me/sendMail"))
    assert _STAMP in sent["message"]["singleValueExtendedProperties"]
    graph.clear()
    await _call("create_microsoft_draft_email", to="a@example.com", subject="Hi", body="Hello")
    created = next(kwargs["json"] for method, url, kwargs in graph if method == "post" and url.endswith("/me/messages"))
    assert _STAMP in created["singleValueExtendedProperties"]


@pytest.mark.asyncio
async def test_a_draft_sent_as_its_mime_carries_the_header(graph):
    executor = _SafeIntegrationExecutor()
    build_tools("token", executor)
    # A forward goes out as its MIME (the thread headers ride it): the stamp too.
    await executor.execute_tool(
        ToolCall(id="c1", name="forward_microsoft_email", arguments={"message_id": _id(1), "to": "a@example.com"})
    )
    patches = [kwargs.get("json") or {} for method, url, kwargs in graph if method == "patch"]
    assert any(_STAMP in (p.get("singleValueExtendedProperties") or []) for p in patches)
    mime_sends = [kwargs for method, url, kwargs in graph if url.endswith("/me/sendMail") and "content" in kwargs]
    assert mime_sends
    assert f"{PLATFORM_SENT_HEADER}: xtm-one".encode() in base64.b64decode(mime_sends[0]["content"])
