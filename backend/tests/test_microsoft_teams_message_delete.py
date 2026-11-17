"""Deleting Teams channel messages, thread replies and chat messages."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.integrations.http import format_http_error
from app.integrations.providers import microsoft as ms
from app.integrations.providers.microsoft import build_tools
from app.integrations.providers.microsoft.teams_scope import (
    DROPPED_WHEN_SCOPED,
    ScopedChannel,
)
from app.services.integration_read_only import withhold_write_tools
from app.services.tool_classification import infer_tool_type
from app.tools.base import CallableToolExecutor, ToolCall

TEAM = "5b3c2a1d-6e7f-4a8b-9c0d-1e2f3a4b5c6d"
CHANNEL = "19:0a1b2c3d4e5f60718293a4b5c6d7e8f9@thread.tacv2"
CHAT = "19:1f2e3d4c5b6a79880716253443526170@thread.v2"
ME = "a1b2c3d4-0000-4000-8000-000000000001"
DELETED_AT = "2026-10-07T11:12:13.456Z"
FORBIDDEN = {
    "error": {
        "code": "Forbidden",
        "message": "User is not authorized to delete this message.",
    }
}


class _Response:
    def __init__(self, payload: Any):
        self._payload = payload

    def json(self) -> Any:
        return self._payload


def _graph(monkeypatch, answer):
    """Route every Graph request through *answer*; return the requests sent."""
    sent: list[tuple[str, str]] = []

    async def fake_safe_request(_client, method, url, _provider, op, **_kwargs):
        sent.append((method, url))
        return answer(method, url, op)

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)
    return sent


def _deleting_graph(method: str, url: str, _op: str):
    if method == "get" and url.endswith("/me"):
        return _Response({"id": ME}), None
    if method == "post" and url.endswith("/softDelete"):
        return _Response(None), None
    if method == "get":
        return _Response({"id": url.rsplit("/", 1)[-1], "deletedDateTime": DELETED_AT}), None
    raise AssertionError(f"unexpected {method} {url}")


async def _call(name: str, **arguments: Any):
    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    return await executor.execute_tool(ToolCall(id="call-1", name=name, arguments=arguments))


@pytest.mark.asyncio
async def test_a_channel_post_is_soft_deleted_and_read_back(monkeypatch):
    sent = _graph(monkeypatch, _deleting_graph)

    result = await _call(
        "delete_microsoft_channel_message",
        team_id=TEAM,
        channel_id=CHANNEL,
        message_id="1700000000100",
    )

    message = f"https://graph.microsoft.com/v1.0/teams/{TEAM}/channels/{CHANNEL}/messages/1700000000100"
    assert sent == [("post", f"{message}/softDelete"), ("get", message)]
    assert result.is_error is False
    assert result.content == f"Channel message deleted. Message ID: 1700000000100. Deleted at: {DELETED_AT}."


@pytest.mark.asyncio
async def test_a_thread_reply_is_deleted_through_its_root(monkeypatch):
    sent = _graph(monkeypatch, _deleting_graph)

    result = await _call(
        "delete_microsoft_channel_message",
        team_id=TEAM,
        channel_id=CHANNEL,
        message_id="1700000000101",
        parent_message_id="1700000000100",
    )

    reply = (
        f"https://graph.microsoft.com/v1.0/teams/{TEAM}/channels/{CHANNEL}"
        "/messages/1700000000100/replies/1700000000101"
    )
    assert sent == [("post", f"{reply}/softDelete"), ("get", reply)]
    assert result.content.startswith("Channel reply deleted. Message ID: 1700000000101.")
    assert DELETED_AT in result.content


@pytest.mark.asyncio
async def test_a_chat_message_is_deleted_under_the_signed_in_user(monkeypatch):
    sent = _graph(monkeypatch, _deleting_graph)

    result = await _call("delete_microsoft_chat_message", chat_id=CHAT, message_id="1746034567890")

    message = f"https://graph.microsoft.com/v1.0/users/{ME}/chats/{CHAT}/messages/1746034567890"
    assert sent == [
        ("get", "https://graph.microsoft.com/v1.0/me"),
        ("post", f"{message}/softDelete"),
        ("get", message),
    ]
    assert result.content == f"Chat message deleted. Message ID: 1746034567890. Deleted at: {DELETED_AT}."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("delete_microsoft_channel_message", {"team_id": TEAM, "channel_id": CHANNEL, "message_id": "1"}),
        ("delete_microsoft_chat_message", {"chat_id": CHAT, "message_id": "1"}),
    ],
    ids=["channel", "chat"],
)
async def test_graph_refusing_someone_elses_message_is_returned_as_is(monkeypatch, tool, arguments):
    def refusing(method: str, url: str, op: str):
        if method == "get" and url.endswith("/me"):
            return _Response({"id": ME}), None
        assert method == "post", "nothing is read back after a refusal"
        return None, format_http_error(httpx.Response(403, json=FORBIDDEN), "Microsoft", op)

    sent = _graph(monkeypatch, refusing)

    result = await _call(tool, **arguments)

    assert sent[-1][1].endswith("/softDelete")
    assert result.content.startswith("Error: Microsoft access denied")
    assert "User is not authorized to delete this message." in result.content
    assert "deleted." not in result.content


@pytest.mark.asyncio
async def test_a_deletion_the_read_back_does_not_confirm_says_so(monkeypatch):
    def unconfirmed(method: str, url: str, _op: str):
        if method == "post":
            return _Response(None), None
        return _Response({"id": "1", "deletedDateTime": None}), None

    _graph(monkeypatch, unconfirmed)

    result = await _call("delete_microsoft_channel_message", team_id=TEAM, channel_id=CHANNEL, message_id="1")

    assert result.content.startswith("Channel message deletion accepted by Microsoft. Message ID: 1. Requested at: ")
    assert result.content.endswith("Reading the message back did not show it deleted yet.")
    requested_at = result.content.split("Requested at: ")[1].split(".")[0]
    assert datetime.strptime(requested_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) <= datetime.now(UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("delete_microsoft_channel_message", {"team_id": TEAM, "channel_id": CHANNEL, "message_id": "n/a"}),
        ("delete_microsoft_channel_message", {"team_id": "na", "channel_id": CHANNEL, "message_id": "1"}),
        (
            "delete_microsoft_channel_message",
            {"team_id": TEAM, "channel_id": CHANNEL, "message_id": "1", "parent_message_id": "{{root}}"},
        ),
        ("delete_microsoft_chat_message", {"chat_id": "chat_id", "message_id": "1"}),
        ("delete_microsoft_chat_message", {"chat_id": CHAT, "message_id": ""}),
    ],
    ids=["channel-message", "channel-team", "channel-parent", "chat-chat", "chat-message"],
)
async def test_placeholder_ids_send_nothing(monkeypatch, tool, arguments):
    sent = _graph(monkeypatch, _deleting_graph)

    result = await _call(tool, **arguments)

    assert sent == []
    assert result.content.startswith("Error:")


@pytest.mark.asyncio
async def test_the_channel_listing_shows_a_deleted_message_as_deleted(monkeypatch):
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def listing(method: str, url: str, _op: str):
        if url.endswith("/messages"):
            return _Response(
                {
                    "value": [
                        {
                            "id": "1700000000100",
                            "createdDateTime": now,
                            "deletedDateTime": now,
                            "from": {"user": {"displayName": "Executive Assistant"}},
                            "body": {"content": ""},
                        }
                    ]
                }
            ), None
        return _Response({"value": []}), None

    _graph(monkeypatch, listing)

    result = await _call("list_microsoft_channel_messages", team_id=TEAM, channel_id=CHANNEL)

    assert "[message deleted]" in result.content
    assert "(Message ID: 1700000000100)" in result.content


@pytest.mark.parametrize("tool", ["delete_microsoft_channel_message", "delete_microsoft_chat_message"])
def test_both_tools_are_writes_a_read_only_binding_withholds(tool):
    assert infer_tool_type(tool) == "write"
    executor = CallableToolExecutor()
    defs = build_tools(token="dummy", executor=executor)
    kept = {d.name for d in withhold_write_tools(defs, executor, integration_label="Microsoft")}
    assert tool not in kept


@pytest.mark.asyncio
async def test_a_channel_scope_limits_the_channel_delete_and_withdraws_the_chat_delete(monkeypatch):
    sent = _graph(monkeypatch, _deleting_graph)
    executor = CallableToolExecutor()
    scope = (ScopedChannel(team_id=TEAM, channel_id=CHANNEL, team_name="Filigran", channel_name="Testing XTM One"),)
    names = {d.name for d in build_tools(token="dummy", executor=executor, teams_channel_scope=scope)}

    assert "delete_microsoft_chat_message" in DROPPED_WHEN_SCOPED
    assert "delete_microsoft_chat_message" not in names
    assert "delete_microsoft_channel_message" in names

    refused = await executor.execute_tool(
        ToolCall(
            id="call-out",
            name="delete_microsoft_channel_message",
            arguments={"team_id": TEAM, "channel_id": "19:other@thread.tacv2", "message_id": "1"},
        )
    )
    assert refused.is_error is True
    assert refused.content.startswith("Refused:")
    assert sent == []

    allowed = await executor.execute_tool(
        ToolCall(
            id="call-in",
            name="delete_microsoft_channel_message",
            arguments={"team_id": TEAM, "channel_id": CHANNEL, "message_id": "1"},
        )
    )
    assert allowed.content.startswith("Channel message deleted.")
