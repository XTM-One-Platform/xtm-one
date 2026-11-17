"""Tests for the create_microsoft_chat tool member serialization.

Microsoft Graph ``POST /chats`` requires every ``conversationMember`` to
carry at least one role (``owner`` or ``guest``); an empty ``roles`` list is
rejected with ``400 BadRequest`` ("Each chat member must have at least one
role"). These tests pin a fixed regression: one-on-one chat
members were serialized with ``roles: []``.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers.microsoft import build_tools
from app.tools.base import CallableToolExecutor, ToolCall

_GRAPH = "https://graph.microsoft.com/v1.0"


class _StubResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


def _make_fake_safe_request(chat_payload: dict[str, Any]):
    async def fake_safe_request(_client, method, url, _provider, _op, **kwargs):
        if method == "get" and url.endswith("/me"):
            return _StubResponse({"id": "me-123"}), None
        if method == "post" and url.endswith("/chats"):
            chat_payload.update(kwargs.get("json", {}))
            return _StubResponse({"id": "chat-123"}), None
        return _StubResponse({}), None

    return fake_safe_request


@pytest.mark.asyncio
async def test_create_microsoft_chat_sets_roles_for_one_on_one_members(monkeypatch):
    chat_payload: dict[str, Any] = {}

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", _make_fake_safe_request(chat_payload))

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)

    result = await executor.execute_tool(
        ToolCall(
            id="call-1",
            name="create_microsoft_chat",
            arguments={"members": "alice@example.com"},
        )
    )

    assert not result.is_error, result.content
    assert chat_payload["chatType"] == "oneOnOne"
    members = chat_payload["members"]
    assert len(members) == 2
    # Every member (the authenticated user AND the invitee) must have a
    # non-empty role — Graph rejects empty ``roles`` with 400 BadRequest.
    assert all(m["roles"] == ["owner"] for m in members)
    # Member references must bind to the resolved /me id and the provided
    # email, in that order (authenticated user is added first).
    assert members[0]["user@odata.bind"] == f"{_GRAPH}/users('me-123')"
    assert members[1]["user@odata.bind"] == f"{_GRAPH}/users('alice@example.com')"


@pytest.mark.asyncio
async def test_create_microsoft_chat_sets_roles_and_topic_for_group_chat(monkeypatch):
    chat_payload: dict[str, Any] = {}

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", _make_fake_safe_request(chat_payload))

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)

    result = await executor.execute_tool(
        ToolCall(
            id="call-2",
            name="create_microsoft_chat",
            arguments={
                "members": "alice@example.com, bob@example.com",
                "topic": "Deal Review",
            },
        )
    )

    assert not result.is_error, result.content
    assert chat_payload["chatType"] == "group"
    assert chat_payload["topic"] == "Deal Review"
    members = chat_payload["members"]
    assert len(members) == 3
    assert all(m["roles"] == ["owner"] for m in members)
    assert members[0]["user@odata.bind"] == f"{_GRAPH}/users('me-123')"
    assert members[1]["user@odata.bind"] == f"{_GRAPH}/users('alice@example.com')"
    assert members[2]["user@odata.bind"] == f"{_GRAPH}/users('bob@example.com')"
