"""Tests for list_all_microsoft_chat_messages compound tool.

Validates that the tool fetches messages from all chats in O(1) LLM iterations
(a single tool call) by making parallel Graph API requests — fixing the
assignment timeout bug where agents called list_microsoft_chat_messages once
per chat, consuming 1 iteration per chat and exhausting the 20-iteration budget.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.integrations.providers.microsoft import build_tools
from app.tools.base import CallableToolExecutor, ToolCall


class _StubResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._payload


def _make_chat(chat_id: str, topic: str, chat_type: str = "group") -> dict:
    return {
        "id": chat_id,
        "chatType": chat_type,
        "topic": topic,
        "lastUpdatedDateTime": "2026-07-08T09:00:00Z",
        "members": [
            {"displayName": "Alice"},
            {"displayName": "Bob"},
        ],
    }


def _make_message(msg_id: str, sender: str, text: str, ts: str | None = None) -> dict:
    return {
        "id": msg_id,
        "messageType": "message",
        "createdDateTime": ts or "2026-07-08T09:00:00Z",
        "from": {"user": {"displayName": sender}},
        "body": {"content": text},
    }


# ---------------------------------------------------------------------------
# Happy path: two chats, each with messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_all_microsoft_chat_messages_returns_all_chats(monkeypatch):
    """Tool returns messages from every chat in one call."""
    calls: list[str] = []
    chat_list_params: list[dict] = []

    async def fake_safe_request(_client, _method, url, _provider, _op, **kwargs):
        calls.append(url)
        if url.endswith("/me/chats"):
            chat_list_params.append(kwargs.get("params", {}))
            return (
                _StubResponse(
                    {
                        "value": [
                            _make_chat("chat-1", "MUFG Deal Q&A"),
                            _make_chat("chat-2", "Another Chat"),
                        ]
                    }
                ),
                None,
            )
        if "chat-1/messages" in url:
            return (
                _StubResponse(
                    {
                        "value": [
                            _make_message("msg-1", "Alice", "What is the deal status?"),
                            _make_message("msg-2", "Bob", "Closing next week."),
                        ]
                    }
                ),
                None,
            )
        if "chat-2/messages" in url:
            return (
                _StubResponse(
                    {
                        "value": [
                            _make_message("msg-3", "Carol", "Hello from another chat"),
                        ]
                    }
                ),
                None,
            )
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-1",
            name="list_all_microsoft_chat_messages",
            arguments={},
        )
    )

    assert not result.is_error
    assert "MUFG Deal Q&A" in result.content
    assert "What is the deal status?" in result.content
    assert "Closing next week." in result.content
    assert "Another Chat" in result.content
    assert "Hello from another chat" in result.content
    # Participants come from the expanded members and identify 1:1 chats
    # that have no topic — they must survive in the output.
    assert "Alice" in result.content
    assert "Bob" in result.content

    # One /me/chats call + one per chat = 3 total Graph API requests
    chats_calls = [u for u in calls if u.endswith("/me/chats")]
    msg_calls = [u for u in calls if "/messages" in u and "chats/" in u]
    assert len(chats_calls) == 1, "Must fetch the chat list exactly once"
    assert len(msg_calls) == 2, "Must fetch messages for each chat concurrently"

    # The chat listing must pin Graph to most-recent-message-first ordering —
    # the endpoint's default "last user action" order could otherwise drop the
    # most recently active chats for accounts with more chats than $top.
    assert chat_list_params[0].get("$orderby") == (
        "lastMessagePreview/createdDateTime desc"
    )
    # Graph /me/chats rejects a nested select inside $expand
    # ("members($select=displayName)" -> 400 Unsupported query parameters)
    # and does not support $select at all on this endpoint — the
    # request must use the PLAIN $expand=members form only.
    assert chat_list_params[0].get("$expand") == "members", (
        "Chat listing must use the plain $expand=members form — a nested "
        "$select inside $expand is rejected by Graph with 400 BadRequest."
    )
    assert "$select" not in chat_list_params[0], (
        "$select is not a supported OData parameter on GET /me/chats."
    )


@pytest.mark.asyncio
async def test_list_all_microsoft_chat_messages_single_tool_call_o1_iterations(
    monkeypatch,
):
    """The tool replaces N per-chat calls with a single tool invocation.

    A 5-chat scenario that previously required 6 LLM iterations
    (1 list_chats + 5 list_microsoft_chat_messages) now finishes in 1.
    """
    num_chats = 5
    msg_fetch_urls: list[str] = []

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        if url.endswith("/me/chats"):
            return (
                _StubResponse(
                    {
                        "value": [
                            _make_chat(f"chat-{i}", f"Chat {i}")
                            for i in range(num_chats)
                        ]
                    }
                ),
                None,
            )
        if "/messages" in url and "chats/" in url:
            msg_fetch_urls.append(url)
            return (
                _StubResponse(
                    {
                        "value": [
                            _make_message(
                                f"msg-{url}",
                                "User",
                                f"Message in {url}",
                            )
                        ]
                    }
                ),
                None,
            )
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-o1",
            name="list_all_microsoft_chat_messages",
            arguments={"count": num_chats},
        )
    )

    assert not result.is_error
    # All 5 chats worth of messages should appear in the single response
    assert len(msg_fetch_urls) == num_chats


# ---------------------------------------------------------------------------
# Empty states
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_all_microsoft_chat_messages_no_chats(monkeypatch):
    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(id="call-empty", name="list_all_microsoft_chat_messages", arguments={})
    )

    assert not result.is_error
    assert "No Microsoft Teams chats found" in result.content


@pytest.mark.asyncio
async def test_list_all_microsoft_chat_messages_chat_with_no_messages(monkeypatch):
    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        if url.endswith("/me/chats"):
            return _StubResponse({"value": [_make_chat("chat-1", "Empty Chat")]}), None
        # Messages endpoint returns empty
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(id="call-no-msg", name="list_all_microsoft_chat_messages", arguments={})
    )

    assert not result.is_error
    assert "Empty Chat" in result.content
    assert "No messages" in result.content


# ---------------------------------------------------------------------------
# Partial failure resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_all_microsoft_chat_messages_tolerates_per_chat_error(monkeypatch):
    """A failure fetching messages for one chat should not break other chats."""

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        if url.endswith("/me/chats"):
            return (
                _StubResponse(
                    {
                        "value": [
                            _make_chat("chat-ok", "Working Chat"),
                            _make_chat("chat-fail", "Broken Chat"),
                        ]
                    }
                ),
                None,
            )
        if "chat-ok/messages" in url:
            return (
                _StubResponse(
                    {"value": [_make_message("msg-1", "Alice", "This works")]}
                ),
                None,
            )
        if "chat-fail/messages" in url:
            return None, "Graph API error: 403 Forbidden"
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-partial",
            name="list_all_microsoft_chat_messages",
            arguments={},
        )
    )

    assert not result.is_error
    # Working chat's message should still appear
    assert "This works" in result.content
    # The broken chat must keep its header AND surface the actual error text —
    # asserting either alone would let the error string silently disappear.
    assert "Broken Chat" in result.content
    assert "Could not fetch messages" in result.content
    assert "403 Forbidden" in result.content


@pytest.mark.asyncio
async def test_list_all_microsoft_chat_messages_surfaces_task_exception(monkeypatch):
    """An unexpected exception in a fetch task is surfaced in the output.

    ``asyncio.gather(..., return_exceptions=True)`` hands raw exceptions back
    to the result loop; tool callers never see server logs, so the chat must
    appear with an error placeholder instead of silently vanishing — and the
    summary denominator must still count it as attempted.
    """

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        if url.endswith("/me/chats"):
            return (
                _StubResponse(
                    {
                        "value": [
                            _make_chat("chat-ok", "Working Chat"),
                            _make_chat("chat-boom", "Exploding Chat"),
                        ]
                    }
                ),
                None,
            )
        if "chat-ok/messages" in url:
            return (
                _StubResponse(
                    {"value": [_make_message("msg-1", "Alice", "This works")]}
                ),
                None,
            )
        if "chat-boom/messages" in url:
            raise RuntimeError("connection exploded")
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-exc",
            name="list_all_microsoft_chat_messages",
            arguments={},
        )
    )

    assert not result.is_error
    assert "This works" in result.content
    # The exploding chat keeps its header, surfaces the error text, and is
    # still counted in the summary denominator.
    assert "Exploding Chat" in result.content
    assert "Could not fetch messages" in result.content
    assert "connection exploded" in result.content
    assert "1 of 2 chat(s)" in result.content


# ---------------------------------------------------------------------------
# Time filter (hours > 0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_all_microsoft_chat_messages_hours_filter(monkeypatch):
    """Messages older than the ``hours`` threshold are excluded.

    Timestamps use Graph's fractional-seconds format (``.153Z``), which does
    NOT sort lexicographically against whole-second cutoffs — the filter must
    compare parsed datetimes, and this test would catch a regression to raw
    string comparison for same-second boundaries.
    """
    now = datetime.now(UTC)
    recent_ts = now.strftime("%Y-%m-%dT%H:%M:%S") + ".153Z"
    old_ts = (now - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S") + ".153Z"

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        if url.endswith("/me/chats"):
            return _StubResponse({"value": [_make_chat("chat-1", "Deal Chat")]}), None
        if "chat-1/messages" in url:
            return (
                _StubResponse(
                    {
                        "value": [
                            _make_message("msg-new", "Alice", "Recent message", recent_ts),
                            _make_message("msg-old", "Bob", "Old message", old_ts),
                        ]
                    }
                ),
                None,
            )
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-hours",
            name="list_all_microsoft_chat_messages",
            arguments={"hours": 24},
        )
    )

    assert not result.is_error
    assert "Recent message" in result.content
    assert "Old message" not in result.content


@pytest.mark.asyncio
async def test_list_all_microsoft_chat_messages_hours_filter_graph_timestamp_variants(
    monkeypatch,
):
    """Graph's 7-digit fractional seconds and missing-Z timestamps both survive.

    ``datetime.fromisoformat`` on Python 3.11+ parses ``.0000000Z`` directly
    (truncating to microseconds), and ``_parse_graph_timestamp`` assumes UTC
    for values without a timezone designator — a naive datetime compared with
    the aware cutoff would otherwise raise ``TypeError`` inside the gathered
    fetch task and silently drop the whole chat from the output.
    """
    now = datetime.now(UTC)
    seven_digit_ts = now.strftime("%Y-%m-%dT%H:%M:%S") + ".0000000Z"
    naive_ts = now.strftime("%Y-%m-%dT%H:%M:%S")  # no Z / offset

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        if url.endswith("/me/chats"):
            return _StubResponse({"value": [_make_chat("chat-1", "Deal Chat")]}), None
        if "chat-1/messages" in url:
            return (
                _StubResponse(
                    {
                        "value": [
                            _make_message(
                                "msg-7dig", "Alice", "Seven digit message", seven_digit_ts
                            ),
                            _make_message("msg-naive", "Bob", "Naive ts message", naive_ts),
                        ]
                    }
                ),
                None,
            )
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-ts-variants",
            name="list_all_microsoft_chat_messages",
            arguments={"hours": 24},
        )
    )

    assert not result.is_error
    assert "Seven digit message" in result.content
    assert "Naive ts message" in result.content


@pytest.mark.asyncio
async def test_list_all_microsoft_chat_messages_hours_filter_drops_unparseable(
    monkeypatch,
):
    """Messages without a parseable createdDateTime are excluded when hours > 0."""

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        if url.endswith("/me/chats"):
            return _StubResponse({"value": [_make_chat("chat-1", "Deal Chat")]}), None
        if "chat-1/messages" in url:
            return (
                _StubResponse(
                    {
                        "value": [
                            _make_message(
                                "msg-good",
                                "Alice",
                                "Parseable message",
                                datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            ),
                            _make_message("msg-bad", "Bob", "Broken timestamp", "not-a-date"),
                        ]
                    }
                ),
                None,
            )
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-badts",
            name="list_all_microsoft_chat_messages",
            arguments={"hours": 24},
        )
    )

    assert not result.is_error
    assert "Parseable message" in result.content
    assert "Broken timestamp" not in result.content


# ---------------------------------------------------------------------------
# Chat-list paging and expand fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_all_microsoft_chat_messages_follows_chat_paging(monkeypatch):
    """Chat listing follows @odata.nextLink past Graph's 25-chat expand cap.

    With ``$expand=members`` Graph returns at most 25 chats per page even
    when ``$top`` is larger (documented known issue) — a ``count`` of 30
    must transparently collect the second page instead of silently
    truncating the digest to 25 chats.
    """
    page_urls: list[str] = []
    next_link = "https://graph.microsoft.com/v1.0/me/chats?$skiptoken=page2"

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        if "/me/chats" in url and "/messages" not in url:
            page_urls.append(url)
            if url == next_link:
                return (
                    _StubResponse(
                        {
                            "value": [
                                _make_chat(f"chat-{i}", f"Chat {i}")
                                for i in range(25, 30)
                            ]
                        }
                    ),
                    None,
                )
            return (
                _StubResponse(
                    {
                        "value": [
                            _make_chat(f"chat-{i}", f"Chat {i}") for i in range(25)
                        ],
                        "@odata.nextLink": next_link,
                    }
                ),
                None,
            )
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-paging",
            name="list_all_microsoft_chat_messages",
            arguments={"count": 30},
        )
    )

    assert not result.is_error
    assert len(page_urls) == 2, f"Expected 2 chat-list pages, got {page_urls}"
    assert page_urls[1] == next_link
    assert "30 chat(s)" in result.content
    assert "Chat 29" in result.content


@pytest.mark.asyncio
async def test_list_all_microsoft_chat_messages_falls_back_without_expand(
    monkeypatch,
):
    """If Graph rejects the expanded listing, retry once without ``$expand``.

    This is the deterministic safety net for that failure class: a
    member-less digest (blank participants) beats the tool being 100%
    broken again if Graph tightens this endpoint's parameter validation.
    """
    chat_list_params: list[dict] = []

    async def fake_safe_request(_client, _method, url, _provider, _op, **kwargs):
        if url.endswith("/me/chats"):
            params = kwargs.get("params") or {}
            chat_list_params.append(params)
            if "$expand" in params:
                return None, (
                    "Error: Microsoft rejected the request to list chats "
                    "(all messages) - bad request."
                )
            return _StubResponse({"value": [_make_chat("chat-1", "Deal Chat")]}), None
        if "chat-1/messages" in url:
            return (
                _StubResponse(
                    {"value": [_make_message("msg-1", "Alice", "Still works")]}
                ),
                None,
            )
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-fallback",
            name="list_all_microsoft_chat_messages",
            arguments={},
        )
    )

    assert not result.is_error, result.content
    assert "Still works" in result.content
    assert len(chat_list_params) == 2
    assert "$expand" in chat_list_params[0]
    assert "$expand" not in chat_list_params[1]


# ---------------------------------------------------------------------------
# Count clamping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_all_microsoft_chat_messages_clamps_count_to_max(monkeypatch):
    """count > 50 should be clamped to 50."""
    top_values: list[str] = []

    async def fake_safe_request(_client, _method, url, _provider, _op, **kwargs):
        params = kwargs.get("params", {})
        if url.endswith("/me/chats"):
            top_values.append(params.get("$top", ""))
            return _StubResponse({"value": []}), None
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    await executor.execute_tool(
        ToolCall(
            id="call-clamp",
            name="list_all_microsoft_chat_messages",
            arguments={"count": 999},
        )
    )

    assert top_values == ["50"], f"Expected $top=50, got {top_values}"


@pytest.mark.asyncio
async def test_list_all_microsoft_chat_messages_coerces_string_arguments(monkeypatch):
    """String-typed tool arguments are coerced instead of raising TypeError.

    ``ToolCall.arguments`` are not type-coerced by ``CallableToolExecutor``,
    and LLMs occasionally emit integers as strings — the tool must not burn
    an iteration on ``max()``/``min()`` TypeErrors.
    """
    top_values: list[str] = []
    now = datetime.now(UTC)
    recent_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    old_ts = (now - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def fake_safe_request(_client, _method, url, _provider, _op, **kwargs):
        params = kwargs.get("params", {})
        if url.endswith("/me/chats"):
            top_values.append(params.get("$top", ""))
            return _StubResponse({"value": [_make_chat("chat-1", "Deal Chat")]}), None
        if "chat-1/messages" in url:
            return (
                _StubResponse(
                    {
                        "value": [
                            _make_message("msg-new", "Alice", "Recent message", recent_ts),
                            _make_message("msg-old", "Bob", "Old message", old_ts),
                        ]
                    }
                ),
                None,
            )
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-str-args",
            name="list_all_microsoft_chat_messages",
            arguments={"count": "5", "messages_per_chat": "10", "hours": "24"},
        )
    )

    assert not result.is_error, result.content
    assert top_values == ["5"], f"Expected $top=5, got {top_values}"
    # The string "24" hours filter must be applied, not ignored
    assert "Recent message" in result.content
    assert "Old message" not in result.content


# ---------------------------------------------------------------------------
# system event messages are skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_all_microsoft_chat_messages_skips_system_events(monkeypatch):
    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        if url.endswith("/me/chats"):
            return _StubResponse({"value": [_make_chat("chat-1", "Deal Chat")]}), None
        if "chat-1/messages" in url:
            return (
                _StubResponse(
                    {
                        "value": [
                            {
                                "id": "sys-1",
                                "messageType": "systemEventMessage",
                                "createdDateTime": "2026-07-08T09:00:00Z",
                                "from": {},
                                "body": {"content": "User joined the chat"},
                            },
                            _make_message("msg-1", "Alice", "Real message"),
                        ]
                    }
                ),
                None,
            )
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-sys",
            name="list_all_microsoft_chat_messages",
            arguments={},
        )
    )

    assert not result.is_error
    assert "Real message" in result.content
    assert "User joined the chat" not in result.content
