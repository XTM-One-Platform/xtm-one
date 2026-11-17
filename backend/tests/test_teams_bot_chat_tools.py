"""Tests for the Teams Bot chat tools (Graph reads + Bot Framework writes).

Microsoft Graph supports NO application permission for sending chat messages
(only the ``Teamwork.Migrate.All`` import mode) and the bare ``GET /chats``
endpoint is delegated-only, so:

- chat WRITES must go through Bot Framework (proactive 1:1 conversation
  creation, or posting into a group chat the bot was added to), and
- chat READS use the app-only-supported Graph endpoints
  (``/users/{email}/chats``, ``/chats/{id}/messages``).

These tests pin that split — the previous implementation created chats and
sent messages via Graph with the bot's app-only token, which always failed
with ``Authorization_RequestDenied`` regardless of granted permissions.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.integrations.providers import teams_bot
from app.tools.base import CallableToolExecutor

_DISPLAY_CARD = {
    "type": "AdaptiveCard",
    "version": "1.5",
    "body": [
        {"type": "TextBlock", "text": "Fruit sales", "size": "Large", "weight": "Bolder"},
        {"type": "TextBlock", "text": "Bananas are winning."},
    ],
}

_INTERACTIVE_CARD = {
    "type": "AdaptiveCard",
    "version": "1.5",
    "body": [{"type": "TextBlock", "text": "Approve?"}],
    "actions": [{"type": "Action.Submit", "title": "Approve", "data": {"ok": True}}],
}


def _make_executor() -> CallableToolExecutor:
    executor = CallableToolExecutor()
    teams_bot.build_tools("tenant-123||client-abc||secret-xyz", executor)
    return executor


class _Recorder:
    """Fake ``safe_request`` that records calls and pops queued responses."""

    def __init__(self, responses: list[dict]):
        self.calls: list[dict] = []
        self._responses = responses

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append(
            {"method": method, "url": url, "json": kwargs.get("json")}
        )
        payload = self._responses.pop(0) if self._responses else {}
        resp = AsyncMock()
        resp.json = lambda p=payload: p
        return resp, None


def _patched(recorder: _Recorder):
    return (
        patch.object(
            teams_bot, "_acquire_token", new=AsyncMock(return_value="tok")
        ),
        patch.object(
            teams_bot,
            "_get_service_url",
            new=AsyncMock(return_value="https://smba.trafficmanager.net/amer"),
        ),
        patch.object(teams_bot, "safe_request", new=recorder),
    )


@pytest.mark.asyncio
async def test_send_chat_message_uses_bot_framework_not_graph():
    """1:1 sends go Graph (user resolution) → Bot Framework — never a Graph
    chat create / message send (impossible app-only)."""
    executor = _make_executor()
    recorder = _Recorder(
        [
            {"id": "user-aad-1"},  # GET /users/{email}
            {"id": "conv-1", "activityId": "act-9"},  # POST /v3/conversations
        ]
    )
    p1, p2, p3 = _patched(recorder)
    with p1, p2, p3:
        result = await executor._handlers["send_teams_bot_chat_message"](
            user_email="romain@filigran.io", message="**hello**"
        )

    assert "act-9" in result
    urls = [c["url"] for c in recorder.calls]
    assert urls[0].startswith(f"{teams_bot.GRAPH_API}/users/romain@filigran.io")
    assert urls[1].endswith("/v3/conversations")
    assert not any("/chats" in u for u in urls), (
        "Graph chat creation / sending must never be attempted (app-only "
        "tokens cannot send chat messages)"
    )
    bf_body = recorder.calls[1]["json"]
    assert bf_body["bot"] == {"id": "client-abc"}
    assert bf_body["members"] == [{"id": "user-aad-1"}]
    assert bf_body["tenantId"] == "tenant-123"
    assert bf_body["activity"]["textFormat"] == "markdown"


@pytest.mark.asyncio
async def test_send_chat_card_no_longer_needs_service_principal_lookup():
    """The card tool resolves only the user — no servicePrincipals or Graph
    chat calls (which required Application.Read.All and could never send)."""
    executor = _make_executor()
    recorder = _Recorder(
        [
            {"id": "user-aad-1"},
            {"id": "conv-1", "activityId": "act-5"},
        ]
    )
    p1, p2, p3 = _patched(recorder)
    with p1, p2, p3:
        result = await executor._handlers["send_teams_bot_chat_card"](
            user_email="romain@filigran.io",
            card=dict(_INTERACTIVE_CARD),
            fallback_text="Approve?",
        )

    assert "Adaptive Card sent" in result
    urls = [c["url"] for c in recorder.calls]
    assert len(urls) == 2
    assert not any("servicePrincipals" in u for u in urls)
    assert not any("/chats" in u for u in urls)
    activity = recorder.calls[1]["json"]["activity"]
    assert "text" not in activity  # summary-not-text card contract
    assert activity["summary"] == "Approve?"


@pytest.mark.asyncio
async def test_send_group_chat_message_posts_to_conversation_activities():
    executor = _make_executor()
    recorder = _Recorder([{"id": "act-1"}])
    p1, p2, p3 = _patched(recorder)
    with p1, p2, p3:
        result = await executor._handlers["send_teams_bot_group_chat_message"](
            chat_id="19:groupchat@thread.v2", message="## Update\nhello"
        )

    assert "act-1" in result
    call = recorder.calls[0]
    assert call["url"].endswith(
        "/v3/conversations/19:groupchat@thread.v2/activities"
    )
    activity = call["json"]
    assert activity["from"] == {"id": "client-abc"}
    assert activity["textFormat"] == "markdown"
    # markdown_to_teams_text downgrades headings (Bot Framework text surface)
    assert "## Update" not in activity["text"]
    assert "**Update**" in activity["text"]


@pytest.mark.asyncio
async def test_group_chat_send_without_activity_id_is_success_with_note():
    """A 2xx Bot Framework response with no ``id`` (202 asynchronous
    acceptance / empty body) is a success — the confirmation must say the
    id is unavailable instead of rendering a misleading ``(activity ID: )``
    or failing the send."""
    executor = _make_executor()
    recorder = _Recorder([{}])  # 2xx, body without an id
    p1, p2, p3 = _patched(recorder)
    with p1, p2, p3:
        result = await executor._handlers["send_teams_bot_group_chat_message"](
            chat_id="19:groupchat@thread.v2", message="hello"
        )

    assert "Message sent to group chat" in result
    assert "(activity ID: )" not in result
    assert "no activity ID" in result


@pytest.mark.asyncio
async def test_group_chat_display_card_downgrades_to_text():
    """Group chats are a chat surface — display-only cards become full-width
    Markdown text instead of a bubble-constrained card."""
    executor = _make_executor()
    recorder = _Recorder([{"id": "act-2"}])
    p1, p2, p3 = _patched(recorder)
    with p1, p2, p3:
        await executor._handlers["send_teams_bot_group_chat_card"](
            chat_id="19:groupchat@thread.v2", card=dict(_DISPLAY_CARD)
        )

    activity = recorder.calls[0]["json"]
    assert "attachments" not in activity
    assert "Fruit sales" in activity["text"]


@pytest.mark.asyncio
async def test_group_chat_interactive_card_stays_card():
    executor = _make_executor()
    recorder = _Recorder([{"id": "act-3"}])
    p1, p2, p3 = _patched(recorder)
    with p1, p2, p3:
        await executor._handlers["send_teams_bot_group_chat_card"](
            chat_id="19:groupchat@thread.v2",
            card=dict(_INTERACTIVE_CARD),
            fallback_text="Approve?",
        )

    activity = recorder.calls[0]["json"]
    assert (
        activity["attachments"][0]["contentType"]
        == "application/vnd.microsoft.card.adaptive"
    )
    assert "text" not in activity
    assert activity["summary"] == "Approve?"


@pytest.mark.asyncio
async def test_list_chats_uses_app_only_user_scoped_endpoint():
    """``GET /chats`` is delegated-only; the tool must use /users/{email}/chats."""
    executor = _make_executor()
    recorder = _Recorder(
        [
            {
                "value": [
                    {
                        "id": "19:groupchat@thread.v2",
                        "chatType": "group",
                        "topic": None,
                        "lastUpdatedDateTime": "2026-07-03T04:00:00Z",
                        "members": [
                            {"displayName": "Romain Delelis"},
                            {"displayName": "Samuel Hassine"},
                        ],
                    }
                ]
            }
        ]
    )
    p1, p2, p3 = _patched(recorder)
    with p1, p2, p3:
        result = await executor._handlers["list_teams_bot_chats"](
            user_email="samuel@filigran.io"
        )

    url = recorder.calls[0]["url"]
    assert "/users/samuel@filigran.io/chats" in url
    assert not url.startswith(f"{teams_bot.GRAPH_API}/chats")
    # Members must be expanded (the tool renders displayName), but with a
    # plain expand only: Graph rejects a nested $select inside $expand for
    # this endpoint (HTTP 400).
    assert "$expand=members" in url
    assert "members($select=" not in url, (
        "$expand=members($select=...) is unsupported by Graph /users/{id}/chats"
    )
    assert "19:groupchat@thread.v2" in result
    assert "Romain Delelis" in result


@pytest.mark.asyncio
async def test_path_breaking_tool_inputs_are_rejected_before_any_request():
    """LLM-controlled emails / chat ids are embedded in URL paths — values
    with path-breaking characters must be rejected without issuing a
    request (prompt-injection hardening)."""
    executor = _make_executor()
    recorder = _Recorder([])
    p1, p2, p3 = _patched(recorder)
    with p1, p2, p3:
        results = [
            await executor._handlers["send_teams_bot_chat_message"](
                user_email="evil/../admin", message="hi"
            ),
            await executor._handlers["send_teams_bot_group_chat_message"](
                chat_id="19:x@thread.v2/../other", message="hi"
            ),
            await executor._handlers["read_teams_bot_chat_messages"](
                chat_id="19:x@thread.v2#frag"
            ),
            await executor._handlers["list_teams_bot_chats"](
                user_email="user@x.io?$select=id"
            ),
        ]

    assert all(r.startswith("Error: Invalid") for r in results)
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_read_chat_messages_clamps_count():
    """Graph caps $top at 50 — oversized LLM-supplied counts are clamped."""
    executor = _make_executor()
    recorder = _Recorder([{"value": []}])
    p1, p2, p3 = _patched(recorder)
    with p1, p2, p3:
        await executor._handlers["read_teams_bot_chat_messages"](
            chat_id="19:groupchat@thread.v2", count=10_000
        )

    assert "$top=50" in recorder.calls[0]["url"]


@pytest.mark.asyncio
async def test_read_chat_messages_strips_html():
    executor = _make_executor()
    recorder = _Recorder(
        [
            {
                "value": [
                    {
                        "id": "m1",
                        "messageType": "message",
                        "from": {"user": {"displayName": "Romain"}},
                        "createdDateTime": "2026-07-03T04:00:00Z",
                        "body": {
                            "contentType": "html",
                            "content": "<p>hello <b>world</b></p>",
                        },
                    }
                ]
            }
        ]
    )
    p1, p2, p3 = _patched(recorder)
    with p1, p2, p3:
        result = await executor._handlers["read_teams_bot_chat_messages"](
            chat_id="19:groupchat@thread.v2"
        )

    assert "/chats/19:groupchat@thread.v2/messages" in recorder.calls[0]["url"]
    assert "hello world" in result
    assert "<p>" not in result
    assert "Romain" in result


@pytest.mark.asyncio
async def test_edit_group_chat_message_puts_activity():
    executor = _make_executor()
    recorder = _Recorder([{"id": "act-1"}])
    p1, p2, p3 = _patched(recorder)
    with p1, p2, p3:
        result = await executor._handlers["edit_teams_bot_group_chat_message"](
            chat_id="19:groupchat@thread.v2",
            activity_id="act-1",
            message="See `https://example.com`",
        )

    assert "Group chat message edited" in result
    assert "act-1" in result
    call = recorder.calls[0]
    assert call["method"] == "put"
    assert call["url"].endswith(
        "/v3/conversations/19:groupchat@thread.v2/activities/act-1"
    )
    activity = call["json"]
    assert activity["textFormat"] == "markdown"
    assert "[https://example.com](https://example.com)" in activity["text"]
    assert "`https://example.com`" not in activity["text"]


@pytest.mark.asyncio
async def test_edit_channel_message_puts_activity():
    executor = _make_executor()
    recorder = _Recorder([{"id": "act-9"}])
    p1, p2, p3 = _patched(recorder)
    with p1, p2, p3:
        result = await executor._handlers["edit_teams_bot_channel_message"](
            team_id="team-1",
            channel_id="19:channel@thread.tacv2",
            activity_id="act-9",
            message="updated body",
        )

    assert "Channel message edited" in result
    call = recorder.calls[0]
    assert call["method"] == "put"
    assert call["url"].endswith(
        "/v3/conversations/19:channel@thread.tacv2/activities/act-9"
    )


@pytest.mark.asyncio
async def test_edit_channel_message_routes_chat_conversation():
    executor = _make_executor()
    recorder = _Recorder([{"id": "act-3"}])
    p1, p2, p3 = _patched(recorder)
    with p1, p2, p3:
        result = await executor._handlers["edit_teams_bot_channel_message"](
            team_id="unused",
            channel_id="19:groupchat@thread.v2",
            activity_id="act-3",
            message="from channel tool",
        )

    assert "Group chat message edited" in result
    assert recorder.calls[0]["url"].endswith(
        "/v3/conversations/19:groupchat@thread.v2/activities/act-3"
    )


@pytest.mark.asyncio
async def test_edit_channel_thread_reply_keeps_messageid_suffix():
    executor = _make_executor()
    recorder = _Recorder([{"id": "act-4"}])
    p1, p2, p3 = _patched(recorder)
    with p1, p2, p3:
        await executor._handlers["edit_teams_bot_channel_message"](
            team_id="team-1",
            channel_id="19:channel@thread.tacv2;messageid=root-1",
            activity_id="act-4",
            message="thread edit",
        )

    assert recorder.calls[0]["url"].endswith(
        "/v3/conversations/19:channel@thread.tacv2;messageid=root-1"
        "/activities/act-4"
    )
