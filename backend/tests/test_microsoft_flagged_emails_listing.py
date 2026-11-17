"""The flagged-email listing carries each thread's conversation id and has a compact mode.

``list_microsoft_flagged_emails`` promised conversation IDs it never printed,
and a daily sweep of about 150 flags with previews ran past 20k tokens. Each
entry now carries ``conversationId``; ``include_preview=false`` returns one
line per flag. ``list_microsoft_emails`` carries the conversation id too, so a
Sent Items read can be matched on it.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import microsoft as ms
from app.integrations.providers.microsoft import build_tools
from app.integrations.providers.microsoft.mail import (
    format_email_line,
    format_flagged_email,
    format_flagged_email_line,
)
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall


def _message(index: int) -> dict[str, Any]:
    return {
        "id": f"AAMkAGI2TG93AAA{index:04d}" + "x" * 133,
        "subject": f"Re: Partnership follow-up | quarter {index}",
        "from": {"emailAddress": {"name": f"Sender {index}", "address": f"sender{index}@example.com"}},
        "receivedDateTime": f"2026-10-{1 + index % 28:02d}T08:{index % 60:02d}:00Z",
        "bodyPreview": "Hello, " + "a long preview of the message body " * 20,
        "flag": {"flagStatus": "flagged"},
        "conversationId": f"AAQkAGI2conv{index:04d}" + "y" * 60,
    }


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture
def graph(monkeypatch):
    """Serve the Graph pages of 150 flagged messages, 50 per page, and record every request."""
    messages = [_message(i) for i in range(150)]
    sent: list[tuple[str, dict]] = []

    async def fake_safe_request(_client, _method, url, _provider, _op, **kwargs):
        sent.append((url, kwargs))
        if "/mailFolders/" in url:
            return _Response({"value": messages[: int(kwargs["params"]["$top"])]}), None
        page = sum(1 for u, _ in sent if "/mailFolders/" not in u) - 1
        chunk = messages[page * 50 : (page + 1) * 50]
        next_link = f"https://graph.microsoft.com/v1.0/me/messages?page={page + 1}" if page < 2 else None
        payload: dict[str, Any] = {"value": chunk}
        if next_link:
            payload["@odata.nextLink"] = next_link
        return _Response(payload), None

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)
    return sent


async def _call(tool: str, **arguments: Any):
    executor = _SafeIntegrationExecutor()
    build_tools("token", executor)
    return await executor.execute_tool(ToolCall(id="c1", name=tool, arguments=arguments))


def test_the_full_entry_carries_the_conversation_id():
    entry = format_flagged_email(_message(7))
    assert entry.startswith("[FLAGGED]\nID: AAMkAGI2TG93AAA0007")
    assert "From: Sender 7 <sender7@example.com>" in entry
    assert f"Conversation ID: {_message(7)['conversationId']}" in entry
    assert entry.rsplit("Preview: ", 1)[1] == _message(7)["bodyPreview"][:300]


def test_the_compact_line_is_one_line_with_five_fields():
    message = _message(3)
    message["subject"] = "Re: offer\n  | terms"
    line = format_flagged_email_line(message)
    assert "\n" not in line
    fields = line.split(" | ")
    assert fields == [
        message["id"],
        "sender3@example.com",
        "Re: offer / terms",
        message["receivedDateTime"],
        message["conversationId"],
    ]


def test_a_message_with_missing_fields_still_formats():
    assert format_flagged_email_line({}) == " |  |  |  | "
    assert "Conversation ID: \n" in format_flagged_email({"bodyPreview": None})


@pytest.mark.asyncio
async def test_the_compact_listing_is_one_line_per_flag_and_a_fraction_of_the_full_one(graph):
    compact = await _call("list_microsoft_flagged_emails", count=150, include_preview=False)
    graph.clear()
    full = await _call("list_microsoft_flagged_emails", count=150)
    assert compact.is_error is False and full.is_error is False
    lines = compact.content.split("\n")
    assert lines[0] == "Found 150 flagged emails: (id | from | subject | received | conversation id)"
    assert [line.rsplit(" | ", 1)[1] for line in lines[1:]] == sorted_conversations()
    assert "Preview:" not in compact.content
    # Graph ids are long (about 150 characters for a message, 76 for a
    # conversation): they set the floor. Everything else is cut.
    assert len(compact.content) < 0.5 * len(full.content)


@pytest.mark.asyncio
async def test_the_default_listing_keeps_its_previews_and_adds_the_conversation_id(graph):
    result = await _call("list_microsoft_flagged_emails", count=150)
    assert result.is_error is False
    assert result.content.startswith("Found 150 flagged emails:\n[FLAGGED]\nID: ")
    assert result.content.count("Preview: Hello, ") == 150
    assert result.content.count("Conversation ID: AAQkAGI2conv") == 150


@pytest.mark.asyncio
async def test_the_folder_listing_carries_the_conversation_id(graph):
    result = await _call("list_microsoft_emails", folder="sentitems", count=3)
    assert result.is_error is False
    assert "conversationId" in graph[0][1]["params"]["$select"].split(",")
    for index in range(3):
        assert f"Conversation ID: {_message(index)['conversationId']}\n" in result.content


def test_the_folder_compact_line_is_one_line_with_six_fields():
    message = _message(4)
    message.update({"subject": "Re: offer\n | terms", "isRead": False, "importance": "high"})
    line = format_email_line(message)
    assert "\n" not in line
    assert line.split(" | ") == [
        message["id"],
        "Sender 4 <sender4@example.com>",
        "Re: offer / terms",
        message["receivedDateTime"],
        message["conversationId"],
        "unread,flagged,high",
    ]
    assert format_email_line({"isRead": True}) == " |  |  |  |  | read"


@pytest.mark.asyncio
async def test_the_folder_compact_listing_keeps_the_order_and_cuts_the_size(graph):
    compact = await _call("list_microsoft_emails", count=150, include_preview=False)
    full = await _call("list_microsoft_emails", count=150)
    assert compact.is_error is False and full.is_error is False
    # Same request: paging and ordering are unchanged.
    assert graph[0][1]["params"] == graph[1][1]["params"]
    lines = compact.content.split("\n")
    assert lines[0] == (
        "150 emails in 'inbox': (id | from | subject | received | conversation id | state;"
        " platform-sent = composed by this platform; meetingRequest, meetingCancelled,"
        " meetingResponse = a meeting message)"
    )
    assert [line.split(" | ", 1)[0] for line in lines[1:]] == [_message(i)["id"] for i in range(150)]
    assert "Preview:" not in compact.content
    assert full.content.count("Preview: Hello, ") == 150
    assert lines[1:] == [format_email_line(_message(i)) for i in range(150)]
    # The ids (148 + 72 characters a message) set the floor; the 200-character
    # preview and the labelled lines of every message are what goes.
    assert len(full.content) - len(compact.content) > 150 * 200


def sorted_conversations() -> list[str]:
    """The conversation ids in the order the tool lists them: oldest received first."""
    messages = sorted((_message(i) for i in range(150)), key=lambda m: m["receivedDateTime"])
    return [m["conversationId"] for m in messages]
