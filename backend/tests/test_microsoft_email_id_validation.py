"""Regression tests for get_microsoft_email message-ID validation.

The ``get_microsoft_email`` tool (and the other email tools that accept a
message ID) must short-circuit with an actionable error message when the
agent passes an obviously invalid ID — a placeholder word, an
unsubstituted template token, or a value that is far too short to be a
real Microsoft Graph message ID — instead of forwarding the bad value to
the Graph API and receiving a ``400 ErrorInvalidIdMalformed`` response.

Covers:
  1. Known placeholder strings (``placeholder``, ``none``, ``id``, …)
  2. Short hex strings that are not Graph IDs (e.g. 24-char MongoDB-style IDs)
  3. Empty / whitespace-only inputs
  4. Unsubstituted template tokens (``{{message_id}}``, ``<email_id>`` …)
  5. A well-formed Graph message ID passes through to the Graph API call
  6. All six affected tools share the same guard:
     get_microsoft_email, flag_microsoft_email, unflag_microsoft_email,
     mark_microsoft_email_read, create_microsoft_draft_reply, send_microsoft_draft
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers.microsoft import build_tools
from app.integrations.providers.microsoft._outlook import _MIN_MESSAGE_ID_LENGTH, _validate_message_id
from app.tools.base import CallableToolExecutor, ToolCall

# ---------------------------------------------------------------------------
# Unit tests for _validate_message_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "   ",
        "placeholder",
        "PLACEHOLDER",
        "none",
        "null",
        "undefined",
        "id",
        "message_id",
        "email_id",
        "n/a",
        "na",
        "unknown",
        "-",
        # Unsubstituted template tokens
        "{{message_id}}",
        "{{email_id}}",
        "<message_id>",
        "[message_id]",
        "&lt;message_id&gt;",
        # Short hex strings (observed in production — 24-char MongoDB-style IDs)
        "6a478516a3959653be9e43af",
        "6a4ccad5ef9d034c4774938b",
        # Any string shorter than _MIN_MESSAGE_ID_LENGTH
        "x" * (_MIN_MESSAGE_ID_LENGTH - 1),
    ],
)
def test_validate_message_id_rejects_bad_ids(bad_id: str) -> None:
    """_validate_message_id must return an error string for every invalid input."""
    result = _validate_message_id(bad_id, "get_microsoft_email")
    assert result is not None, f"Expected error for bad_id={bad_id!r}"
    assert "message_id" in result.lower() or "error" in result.lower()


def test_validate_message_id_accepts_valid_graph_id() -> None:
    """A realistic Graph message ID (base64url, 150+ chars) must pass through."""
    valid_id = (
        "AAMkAGI2NGVhZTVlLTI1OGMtNDI4My1iZmVmLTI1ZmM5OWM5MTIwNgBGAAAAAADUuTJK"
        "1K9aTpCdqXop_4NaBwCd9nJ-tVysQos2hTfspaWSAAAAAAEMAAA="
    )
    assert len(valid_id) > _MIN_MESSAGE_ID_LENGTH
    result = _validate_message_id(valid_id, "get_microsoft_email")
    assert result is None, f"Expected None for valid_id, got {result!r}"


def test_validate_message_id_error_includes_tool_name() -> None:
    """The error message must mention the tool name so agents can self-correct."""
    result = _validate_message_id("placeholder", "get_microsoft_email")
    assert result is not None
    assert "get_microsoft_email" in result


def test_validate_message_id_error_mentions_list_or_search() -> None:
    """The error must suggest using list/search tools to retrieve a valid ID."""
    result = _validate_message_id("placeholder", "get_microsoft_email")
    assert result is not None
    assert "list_microsoft_emails" in result or "search_microsoft_emails" in result


def test_validate_message_id_supports_custom_field_and_recovery_hint() -> None:
    """Callers with a different parameter name (send_microsoft_draft's
    ``draft_id``) must be able to keep the error message accurate — naming
    the real field and pointing at the draft-creation tools instead of the
    email list/search tools."""
    result = _validate_message_id(
        "placeholder",
        "send_microsoft_draft",
        field_name="draft_id",
        recovery_hint="Use create_microsoft_draft_email and pass the Draft ID it returns",
    )
    assert result is not None
    assert "draft_id" in result
    assert "create_microsoft_draft_email" in result
    assert "list_microsoft_emails" not in result


# ---------------------------------------------------------------------------
# Integration-style tests via the tool executor
# ---------------------------------------------------------------------------


class _RequestRecorder:
    """Captures all outgoing Graph API calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method: str, url: str, provider: str, op: str, **kwargs: Any):
        self.calls.append({"method": method, "url": url, "op": op})
        return None, "should not be called"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,arguments",
    [
        ("get_microsoft_email", {"message_id": "placeholder"}),
        ("get_microsoft_email", {"message_id": "6a478516a3959653be9e43af"}),
        ("get_microsoft_email", {"message_id": "{{message_id}}"}),
        ("get_microsoft_email", {"message_id": ""}),
        ("flag_microsoft_email", {"message_id": "placeholder"}),
        ("flag_microsoft_email", {"message_id": "6a4ccad5ef9d034c4774938b"}),
        ("unflag_microsoft_email", {"message_id": "placeholder"}),
        ("mark_microsoft_email_read", {"message_id": "placeholder"}),
        ("create_microsoft_draft_reply", {"message_id": "placeholder", "body": "Hi"}),
        ("send_microsoft_draft", {"draft_id": "placeholder"}),
    ],
)
async def test_email_tools_reject_invalid_ids_without_calling_graph(
    monkeypatch, tool_name: str, arguments: dict
) -> None:
    """All email tools must short-circuit before calling Graph for invalid IDs."""
    recorder = _RequestRecorder()

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", recorder)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)

    result = await executor.execute_tool(ToolCall(id="c1", name=tool_name, arguments=arguments))

    # The guard must fire before any HTTP call
    assert recorder.calls == [], (
        f"Graph API was called despite invalid message_id for {tool_name}: {recorder.calls}"
    )
    # Result must be an error-like string (not a crash)
    assert result.is_error, f"expected a guarded rejection for {tool_name}: {result.content}"
    content = str(result.content)
    assert "error" in content.lower(), (
        f"Expected error message for {tool_name} with bad ID, got: {content!r}"
    )


@pytest.mark.asyncio
async def test_send_microsoft_draft_error_names_draft_id_and_creation_tools(
    monkeypatch,
) -> None:
    """send_microsoft_draft's guard must talk about draft_id (its actual
    parameter) and point the agent at the draft-creation tools, not at the
    email list/search tools which cannot produce a draft ID."""
    recorder = _RequestRecorder()

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", recorder)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)

    result = await executor.execute_tool(
        ToolCall(id="c1", name="send_microsoft_draft", arguments={"draft_id": "placeholder"})
    )

    assert recorder.calls == []
    content = str(result.content)
    assert "draft_id" in content
    assert "create_microsoft_draft_email" in content
    assert "create_microsoft_draft_reply" in content


@pytest.mark.asyncio
async def test_get_microsoft_email_calls_graph_for_valid_id(monkeypatch) -> None:
    """With a well-formed Graph message ID the tool must reach the Graph API."""
    valid_id = (
        "AAMkAGI2NGVhZTVlLTI1OGMtNDI4My1iZmVmLTI1ZmM5OWM5MTIwNgBGAAAAAADUuTJK"
        "1K9aTpCdqXop_4NaBwCd9nJ-tVysQos2hTfspaWSAAAAAAEMAAA="
    )

    class _OkResponse:
        def json(self) -> dict:
            return {
                "id": valid_id,
                "subject": "Test",
                "from": {"emailAddress": {"name": "Alice", "address": "alice@example.com"}},
                "toRecipients": [],
                "ccRecipients": [],
                "receivedDateTime": "2026-07-01T09:00:00Z",
                "body": {"content": "Hello"},
                "bodyPreview": "Hello",
                "isRead": True,
                "importance": "normal",
                "flag": {"flagStatus": "notFlagged"},
                "conversationId": "conv1",
                "hasAttachments": False,
            }

    calls: list[str] = []

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kw):
        calls.append(url)
        return _OkResponse(), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)

    result = await executor.execute_tool(
        ToolCall(id="c1", name="get_microsoft_email", arguments={"message_id": valid_id})
    )

    assert calls, "Graph API was NOT called for a valid message ID"
    assert not result.is_error
    assert "Alice" in str(result.content)
