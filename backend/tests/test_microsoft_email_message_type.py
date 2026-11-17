"""The mail reads and the new_email trigger say whether a message is a meeting message.

A meeting request reads like any email when its body holds no Teams join
block (an in-person invite): only its Graph type tells it apart. The reads
name it from the ``@odata.type`` the request already returns; the trigger
payload carries it as ``message_type`` and ``exclude_meeting_messages`` drops it.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import microsoft as ms
from app.integrations.providers.microsoft import build_tools
from app.integrations.providers.microsoft._message_type import message_type
from app.integrations.providers.microsoft.mail import format_email_line
from app.services.event_poller import microsoft_mail_payload
from app.services.integration_event_filters import event_filter_reason
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall

_PAD = "x" * 133
_REQUEST = "#microsoft.graph.eventMessageRequest"
_RESPONSE = "#microsoft.graph.eventMessageResponse"
_CANCEL = "#microsoft.graph.eventMessage"


def _id(index: int) -> str:
    return f"AAMkAGI2msg{index:04d}{_PAD}"


def _message(index: int, odata_type: str | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "id": _id(index),
        "subject": f"Subject {index}",
        "from": {"emailAddress": {"name": "Me", "address": "me@example.com"}},
        "receivedDateTime": "2026-11-09T08:00:00Z",
        "bodyPreview": "Hello",
        "isRead": True,
        "conversationId": f"AAQkconv{index:04d}",
        "body": {"contentType": "html", "content": "<p>Hello</p>"},
    }
    if odata_type:
        message["@odata.type"] = odata_type
    return message


def test_the_type_is_read_from_the_graph_type():
    assert message_type({}) == "message"
    assert message_type({"@odata.type": "#microsoft.graph.message"}) == "message"
    assert message_type({"@odata.type": _REQUEST}) == "meetingRequest"
    assert message_type({"@odata.type": _CANCEL}) == "meetingCancelled"
    assert message_type({"@odata.type": _RESPONSE}) == "meetingResponse"
    assert message_type({"@odata.type": _RESPONSE, "meetingMessageType": "meetingAccepted"}) == "meetingAccepted"
    assert message_type({"@odata.type": _RESPONSE, "meetingMessageType": "meetingDeclined"}) == "meetingDeclined"
    assert (
        message_type({"@odata.type": _RESPONSE, "meetingMessageType": "meetingTenativelyAccepted"})
        == "meetingTentativelyAccepted"
    )
    # A plain message never takes a meeting kind, whatever else it carries.
    assert message_type({"meetingMessageType": "meetingRequest"}) == "message"


def test_the_compact_line_names_a_meeting_message_in_its_state():
    assert format_email_line(_message(0, _REQUEST)).endswith(" | meetingRequest")
    assert format_email_line(_message(1)).endswith(" | read")


class _Response:
    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.status_code = 200
        self.content = b""

    def json(self) -> Any:
        return self._payload


@pytest.fixture
def graph(monkeypatch):
    """A mailbox with a plain email, an invitation and an acceptance; every request recorded."""
    requests: list[tuple[str, str, dict]] = []
    stored = {0: _message(0), 1: _message(1, _REQUEST), 2: _message(2, _RESPONSE)}

    async def fake_safe_request(_client, method, url, _provider, _op, **kwargs):
        requests.append((method, url, kwargs))
        if "/mailFolders/" in url or (method == "get" and url.endswith("/me/messages")):
            return _Response({"value": list(stored.values())}), None
        if method == "get" and "/me/messages/" in url:
            index = int(url.split("msg", 1)[1][:4])
            message = dict(stored[index])
            if "$select=" not in url and index == 2:
                message["meetingMessageType"] = "meetingAccepted"
            return _Response(message), None
        return _Response({}), None

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)
    return requests


async def _call(tool: str, **arguments: Any):
    executor = _SafeIntegrationExecutor()
    build_tools("token", executor)
    return await executor.execute_tool(ToolCall(id="c1", name=tool, arguments=arguments))


@pytest.mark.asyncio
async def test_get_email_names_the_type_with_no_extra_read_for_a_plain_email_or_a_request(graph):
    plain = await _call("get_microsoft_email", message_id=_id(0))
    assert "Message type: message\n" in plain.content
    invite = await _call("get_microsoft_email", message_id=_id(1))
    assert "Message type: meetingRequest\n" in invite.content
    assert len(graph) == 2


@pytest.mark.asyncio
async def test_get_email_reads_a_response_once_more_for_its_verdict(graph):
    answer = await _call("get_microsoft_email", message_id=_id(2))
    assert "Message type: meetingAccepted\n" in answer.content
    assert len(graph) == 2


@pytest.mark.asyncio
async def test_the_listings_carry_the_type_in_the_same_call(graph):
    compact = await _call("list_microsoft_emails", folder="sentitems", count=3, include_preview=False)
    lines = compact.content.split("\n")
    assert lines[1].endswith(" | read")
    assert lines[2].endswith(" | meetingRequest")
    assert lines[3].endswith(" | meetingResponse")
    full = await _call("list_microsoft_emails", folder="sentitems", count=3)
    assert full.content.count("Message type: meetingRequest\n") == 1
    assert full.content.count("Message type: message\n") == 1
    found = await _call("search_microsoft_emails", query="Subject")
    assert found.content.count("Message type: meetingRequest\n") == 1
    assert len(graph) == 3


def test_the_trigger_payload_carries_the_type_and_the_filter_drops_meetings():
    invite = microsoft_mail_payload(_message(1, _REQUEST))
    plain = microsoft_mail_payload(_message(0))
    assert invite["message_type"] == "meetingRequest"
    assert plain["message_type"] == "message"
    assert event_filter_reason("new_email", invite, {"exclude_meeting_messages": True}) == (
        "meeting message (meetingRequest)"
    )
    assert event_filter_reason("new_email", plain, {"exclude_meeting_messages": True}) is None
    # A mail payload without the field (Gmail) passes.
    assert event_filter_reason("new_email", {"from": "a@example.com"}, {"exclude_meeting_messages": True}) is None
