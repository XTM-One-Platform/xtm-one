"""Regression tests for the Teams / channel-bot assessment fixes.

Covers:
- Teams webhook Bot Framework JWT gate (401 without a valid token)
- HTTP 202 treated as success by ``TeamsChannel.send_message``
- Embedded images delivered as SEPARATE activities (never text+attachments)
- Outbound chunking against the Teams activity size cap
- Teams receive-whitelist matching (Graph GUID vs Bot Framework thread ids)
- Teams groupChat gating (whitelist bypass + thread key)
- Google Chat thread keys
- Bot-level publish whitelist merging
"""

from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request

from app.channels.base import CHANNEL_MAX_CHARS, ChannelType, InboundMessage, OutboundMessage
from app.channels.router import (
    _channel_id_matches,
    _extract_channel_id,
    _get_thread_key,
    _teams_channel_component,
)
from app.channels.teams import TeamsChannel, teams_messages
from app.services.integration_tools import _effective_publish_channels

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _inbound_teams(
    *,
    channel_thread_id: str,
    conversation_type: str | None = None,
    team_id: str | None = None,
    channel_id: str | None = None,
) -> InboundMessage:
    metadata: dict[str, str] = {}
    if conversation_type is not None:
        metadata["conversation_type"] = conversation_type
    if team_id is not None:
        metadata["team_id"] = team_id
    if channel_id is not None:
        metadata["channel_id"] = channel_id
    return InboundMessage(
        channel=ChannelType.TEAMS,
        text="hello",
        channel_thread_id=channel_thread_id,
        metadata=metadata,
    )


class _CapturingClient:
    """Fake httpx.AsyncClient that records every posted activity."""

    posted: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        _CapturingClient.posted.append(json)

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"id": f"act-{len(_CapturingClient.posted)}"}

        return _Resp()


@pytest.fixture()
def capture_posts():
    _CapturingClient.posted = []
    with patch(
        "app.channels.teams.httpx.AsyncClient",
        lambda *a, **k: _CapturingClient(),
    ):
        yield _CapturingClient.posted


# ---------------------------------------------------------------------------
# Webhook JWT gate
# ---------------------------------------------------------------------------


def _make_request(headers: dict[str, str] | None = None, body: bytes = b"{}") -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/channels/teams/messages",
        "query_string": b"",
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


@pytest.mark.asyncio
async def test_teams_webhook_rejects_missing_token():
    """With a bot configured, an unauthenticated POST is rejected with 401."""
    with patch(
        "app.channels.teams._get_teams_app_id",
        new=AsyncMock(return_value="app-123"),
    ):
        response = await teams_messages(_make_request())
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_teams_webhook_rejects_invalid_token():
    with (
        patch(
            "app.channels.teams._get_teams_app_id",
            new=AsyncMock(return_value="app-123"),
        ),
        patch(
            "app.channels.teams._verify_botframework_jwt",
            return_value=False,
        ),
    ):
        response = await teams_messages(
            _make_request({"Authorization": "Bearer bad-token"})
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_teams_webhook_accepts_valid_token():
    with (
        patch(
            "app.channels.teams._get_teams_app_id",
            new=AsyncMock(return_value="app-123"),
        ),
        patch(
            "app.channels.teams._verify_botframework_jwt",
            return_value=True,
        ),
    ):
        # Empty JSON body → non-message activity → 200 without dispatch.
        response = await teams_messages(
            _make_request({"Authorization": "Bearer token"})
        )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_teams_webhook_unauthenticated_when_no_bot_configured():
    """No teams_bot client_id → the webhook warns once but does not 401
    (fail-open with a loud warning, not a hard block)."""
    with (
        patch(
            "app.channels.teams._get_teams_app_id",
            new=AsyncMock(return_value=""),
        ),
        patch("app.channels.teams.warn_unauthenticated_once") as warn,
    ):
        response = await teams_messages(_make_request())
    assert response.status_code == 200
    warn.assert_called_once()


def test_verify_botframework_jwt_fails_closed_on_garbage():
    """Malformed tokens are rejected (and never reach the network)."""
    from app.channels.teams import _verify_botframework_jwt

    assert not _verify_botframework_jwt("not-a-jwt", "app-123")
    assert not _verify_botframework_jwt("x.y.z", "app-123")


# ---------------------------------------------------------------------------
# 202 handling + multi-activity sends
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_202_is_success():
    class _Client202:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            class _Resp:
                status_code = 202
                text = ""

                @staticmethod
                def json():
                    raise ValueError("empty body")

            return _Resp()

    channel = TeamsChannel(
        "https://smba.trafficmanager.net/amer/",
        "19:private@thread.tacv2;messageid=1",
        "bf-token",
        bot_id="bot-app-id",
        conversation_type="channel",
    )
    with patch(
        "app.channels.teams.httpx.AsyncClient", lambda *a, **k: _Client202()
    ):
        result = await channel.send_message(OutboundMessage(text="hi"))
    # "" (not None) — callers gate monitoring setup on ``is not None``.
    assert result == ""


@pytest.mark.asyncio
async def test_send_message_embedded_images_are_separate_activities(capture_posts):
    """Text + hero-card attachments must never share one activity (HTTP 400)."""
    channel = TeamsChannel(
        "https://smba.trafficmanager.net/amer/",
        "19:conversation-id",
        "bf-token",
        bot_id="bot-app-id",
        conversation_type="personal",
    )
    msg = OutboundMessage(
        text="Here is your chart ![chart](https://example.com/x.png)",
        metadata={
            "embedded_images": [
                {"alt_text": "chart", "public_url": "https://example.com/x.png"}
            ]
        },
    )
    result = await channel.send_message(msg)

    assert result == "act-1"
    assert len(capture_posts) == 2
    text_activity, image_activity = capture_posts
    assert "attachments" not in text_activity
    assert "text" not in image_activity
    assert (
        image_activity["attachments"][0]["contentType"]
        == "application/vnd.microsoft.card.hero"
    )


@pytest.mark.asyncio
async def test_send_message_chunks_long_text(capture_posts):
    """Replies above the Teams cap are split instead of failing with 400."""
    channel = TeamsChannel(
        "https://smba.trafficmanager.net/amer/",
        "19:conversation-id",
        "bf-token",
        bot_id="bot-app-id",
        conversation_type="personal",
    )
    long_text = "word " * 12_000  # ~60k chars > 28k cap
    await channel.send_message(OutboundMessage(text=long_text))

    assert len(capture_posts) >= 2
    for activity in capture_posts:
        assert len(activity["text"]) <= CHANNEL_MAX_CHARS["teams"]


# ---------------------------------------------------------------------------
# Teams receive-whitelist matching
# ---------------------------------------------------------------------------


def test_teams_channel_component_normalization():
    guid_ref = "3f2504e0-4f89-11d3-9a0c-0305e82c3301:19:chan@thread.tacv2"
    bf_ref = "19:teamGeneral@thread.tacv2:19:chan@thread.tacv2"
    bare = "19:chan@thread.tacv2"
    threaded = "19:chan@thread.tacv2;messageid=12345"
    assert _teams_channel_component(guid_ref) == bare
    assert _teams_channel_component(bf_ref) == bare
    assert _teams_channel_component(bare) == bare
    assert _teams_channel_component(threaded) == bare


def test_channel_id_matches_teams_guid_vs_thread_style():
    """Whitelist entries (Graph GUID team ids) must match Bot Framework ids."""
    allowed = {"3f2504e0-4f89-11d3-9a0c-0305e82c3301:19:chan@thread.tacv2"}
    incoming = "19:teamGeneral@thread.tacv2:19:chan@thread.tacv2"
    assert _channel_id_matches(ChannelType.TEAMS, incoming, allowed)
    # A different channel still does not match.
    other = "19:teamGeneral@thread.tacv2:19:other@thread.tacv2"
    assert not _channel_id_matches(ChannelType.TEAMS, other, allowed)
    # Non-Teams channels keep exact matching only.
    assert not _channel_id_matches(ChannelType.SLACK, "C123", {"C999"})
    assert _channel_id_matches(ChannelType.SLACK, "C123", {"C123"})


def test_extract_channel_id_teams_group_chat_bypasses_whitelist():
    """Group chats can never appear in a Teams whitelist — must return None."""
    inbound = _inbound_teams(
        channel_thread_id="19:group-chat@thread.v2",
        conversation_type="groupChat",
    )
    assert _extract_channel_id(inbound) is None


def test_extract_channel_id_teams_channel_still_scoped():
    inbound = _inbound_teams(
        channel_thread_id="19:chan@thread.tacv2;messageid=1",
        conversation_type="channel",
        team_id="team-guid",
        channel_id="19:chan@thread.tacv2",
    )
    assert _extract_channel_id(inbound) == "team-guid:19:chan@thread.tacv2"


def test_extract_channel_id_discord_and_google_chat():
    discord = InboundMessage(
        channel=ChannelType.DISCORD,
        text="hi",
        channel_thread_id="1234567890",
        metadata={"is_dm": False},
    )
    assert _extract_channel_id(discord) == "1234567890"
    gchat = InboundMessage(
        channel=ChannelType.GOOGLE_CHAT,
        text="hi",
        channel_thread_id="spaces/AAA",
        metadata={"space_type": "ROOM"},
    )
    assert _extract_channel_id(gchat) == "spaces/AAA"


# ---------------------------------------------------------------------------
# Thread keys (continuation + /stop mute)
# ---------------------------------------------------------------------------


def test_thread_key_teams_group_chat():
    inbound = _inbound_teams(
        channel_thread_id="19:group-chat@thread.v2",
        conversation_type="groupChat",
    )
    assert _get_thread_key(inbound) == "19:group-chat@thread.v2"


def test_thread_key_teams_bare_channel_still_none():
    inbound = _inbound_teams(
        channel_thread_id="19:chan@thread.tacv2",
        conversation_type="channel",
    )
    assert _get_thread_key(inbound) is None


def test_thread_key_google_chat_thread():
    inbound = InboundMessage(
        channel=ChannelType.GOOGLE_CHAT,
        text="hi",
        channel_thread_id="spaces/AAA",
        metadata={"thread_name": "spaces/AAA/threads/BBB"},
    )
    assert _get_thread_key(inbound) == "spaces/AAA/threads/BBB"


# ---------------------------------------------------------------------------
# Bot-level publish whitelist merging
# ---------------------------------------------------------------------------


def test_effective_publish_channels_merging():
    a = [{"id": "1", "name": "one"}, {"id": "2", "name": "two"}]
    b = [{"id": "2", "name": "two"}, {"id": "3", "name": "three"}]

    # No bot whitelist → agent list as-is (empty = unrestricted).
    assert _effective_publish_channels(a, None) == (a, False)
    assert _effective_publish_channels([], None) == ([], False)

    # Bot whitelist without agent selection → bot boundary applies.
    assert _effective_publish_channels(None, b) == (b, False)

    # Both set → intersection.
    merged, blocked = _effective_publish_channels(a, b)
    assert [c["id"] for c in merged] == ["2"]
    assert not blocked

    # Disjoint → blocked (publish tools disabled by the caller).
    merged, blocked = _effective_publish_channels(
        [{"id": "1"}], [{"id": "9"}]
    )
    assert merged == []
    assert blocked


def test_effective_publish_channels_teams_id_normalization():
    """Mixed Teams ID formats (Graph GUID vs Bot Framework thread-style)
    must intersect via the normalized channel component — exact-match-only
    used to produce a false-empty intersection."""
    agent = [
        {
            "id": "3f2504e0-4f89-11d3-9a0c-0305e82c3301:19:chan@thread.tacv2",
            "name": "Licensing (Collateral)",
        }
    ]
    bot = [
        {
            "id": "19:teamGeneral@thread.tacv2:19:chan@thread.tacv2",
            "name": "Licensing (Collateral)",
        }
    ]

    merged, blocked = _effective_publish_channels(agent, bot, "teams")
    assert not blocked
    assert [c["id"] for c in merged] == [agent[0]["id"]]

    # A genuinely different channel still blocks.
    other_bot = [{"id": "19:teamGeneral@thread.tacv2:19:other@thread.tacv2"}]
    merged, blocked = _effective_publish_channels(agent, other_bot, "teams")
    assert merged == []
    assert blocked

    # Non-Teams channels keep exact matching only.
    merged, blocked = _effective_publish_channels(
        [{"id": "C123"}], [{"id": "C999"}], "slack"
    )
    assert blocked

    # Per-entry matching: an exact match and a normalized-only match in the
    # same agent selection are BOTH kept (the fallback is not all-or-nothing).
    mixed_agent = [
        {"id": "guid-a:19:chanA@thread.tacv2", "name": "A"},
        {"id": "guid-b:19:chanB@thread.tacv2", "name": "B"},
    ]
    mixed_bot = [
        {"id": "guid-a:19:chanA@thread.tacv2"},  # exact
        {"id": "19:teamX@thread.tacv2:19:chanB@thread.tacv2"},  # normalized
    ]
    merged, blocked = _effective_publish_channels(mixed_agent, mixed_bot, "teams")
    assert not blocked
    assert [c["id"] for c in merged] == [c["id"] for c in mixed_agent]


# ---------------------------------------------------------------------------
# build_channel_tools — blocked intersection + bypass_whitelist
# ---------------------------------------------------------------------------


def _fake_bot(allowed_publish: list[dict] | None):
    from types import SimpleNamespace

    return SimpleNamespace(
        name="Microsoft Teams",
        provider="teams_bot",
        allowed_publish_channels=allowed_publish,
    )


async def _run_build_channel_tools(ch_cfg: dict, bot) -> tuple[list, dict]:
    """Run build_channel_tools with a stubbed bot + tool builder.

    Returns ``(definitions, captured_builder_kwargs)``.
    """
    from app.services import integration_tools
    from app.tools.base import ToolDefinition

    captured: dict = {}

    def fake_builder(token, executor, **kwargs):
        captured.update(kwargs)
        return [
            ToolDefinition(
                name="list_teams_bot_teams_and_channels",
                description="read tool",
                input_schema={"type": "object", "properties": {}},
                source="integration:teams_bot",
            ),
            ToolDefinition(
                name="send_teams_bot_channel_message",
                description="publish tool",
                input_schema={"type": "object", "properties": {}},
                source="integration:teams_bot",
            ),
        ]

    with (
        patch.object(
            integration_tools,
            "_get_channel_bot_token",
            new=AsyncMock(return_value=("tenant||client||secret", bot)),
        ),
        patch.dict(
            integration_tools.CHANNEL_BOT_TOOLS,
            {"teams_bot": fake_builder},
        ),
    ):
        defs, _executor = await integration_tools.build_channel_tools(
            None,  # db unused — _get_channel_bot_token is stubbed
            {"teams": ch_cfg},
            context={"source": "test"},
        )
    return defs, captured


@pytest.mark.asyncio
async def test_blocked_publish_intersection_keeps_read_tools():
    """A disjoint agent/bot publish whitelist must keep the channel tools
    loaded (the whole tool group used to be silently skipped) with
    publish failing closed: the builder receives a never-matching sentinel
    ``publish_channels``, ``publish_blocked=True`` for channel-less writes,
    and ``read_channels`` (the agent's own selection) so read tools stay
    genuinely usable."""
    agent_selection = [{"id": "guid-a:19:chanA@thread.tacv2", "name": "A"}]
    ch_cfg = {
        "channel_bot_id": "232c198c-78cf-42db-9b58-91b232ad9091",
        "receive_enabled": True,
        "publish_enabled": True,
        "publish_channels": agent_selection,
    }
    bot = _fake_bot([{"id": "guid-b:19:chanB@thread.tacv2", "name": "B"}])

    defs, captured = await _run_build_channel_tools(ch_cfg, bot)

    assert [d.name for d in defs] == [
        "list_teams_bot_teams_and_channels",
        "send_teams_bot_channel_message",
    ]
    sentinel_ids = {c["id"] for c in captured["publish_channels"]}
    assert sentinel_ids == {"__no_publishable_channels__"}
    assert captured["read_channels"] == agent_selection
    assert captured["publish_blocked"] is True


@pytest.mark.asyncio
async def test_bypass_whitelist_skips_bot_publish_boundary():
    """An admin-set ``bypass_whitelist`` must ignore the bot-level publish
    whitelist at runtime — previously only receive routing honored it."""
    agent_selection = [{"id": "guid-a:19:chanA@thread.tacv2", "name": "A"}]
    ch_cfg = {
        "channel_bot_id": "232c198c-78cf-42db-9b58-91b232ad9091",
        "receive_enabled": True,
        "publish_enabled": True,
        "bypass_whitelist": True,
        "publish_channels": agent_selection,
    }
    bot = _fake_bot([{"id": "guid-b:19:chanB@thread.tacv2", "name": "B"}])

    defs, captured = await _run_build_channel_tools(ch_cfg, bot)

    assert len(defs) == 2
    assert captured["publish_channels"] == agent_selection


# ---------------------------------------------------------------------------
# Provider-level read/write scoping under a blocked publish intersection
# ---------------------------------------------------------------------------


_SENTINEL = [{"id": "__no_publishable_channels__", "name": "(blocked)"}]


def _build_provider_tools(module_path: str, token: str, **kwargs):
    """Build a provider's tools and return the executor handler map."""
    import importlib

    from app.tools.base import CallableToolExecutor

    module = importlib.import_module(module_path)
    executor = CallableToolExecutor()
    module.build_tools(token, executor, **kwargs)
    return executor._handlers


@pytest.mark.asyncio
async def test_teams_list_channels_uses_read_scope_when_publish_blocked():
    """The Teams config-shortcut listing must show the agent's readable
    channels — the blocked-publish sentinel must never leak into it."""
    read_channels = [
        {"id": "guid-a:19:chanA@thread.tacv2", "name": "Licensing"},
    ]
    handlers = _build_provider_tools(
        "app.integrations.providers.teams_bot",
        "tenant||client||secret",
        publish_channels=list(_SENTINEL),
        read_channels=read_channels,
        publish_blocked=True,
    )

    listing = await handlers["list_teams_bot_teams_and_channels"]()
    assert "Licensing" in listing
    assert "__no_publishable_channels__" not in listing


@pytest.mark.asyncio
async def test_slack_reads_allowed_writes_denied_when_publish_blocked():
    """With the sentinel publish whitelist + agent read scope, Slack reads
    succeed for readable channels while sends fail closed."""
    from app.integrations.providers import slack_bot as slack_module

    handlers = _build_provider_tools(
        "app.integrations.providers.slack_bot",
        "xoxb-token",
        publish_channels=list(_SENTINEL),
        read_channels=[{"id": "C123", "name": "general"}],
        publish_blocked=True,
    )

    class _Resp:
        @staticmethod
        def json():
            return {"ok": True, "messages": [{"user": "U1", "ts": "1.0", "text": "hi"}]}

    with patch.object(
        slack_module,
        "safe_request",
        new=AsyncMock(return_value=(_Resp(), None)),
    ):
        allowed = await handlers["read_slack_bot_messages"]("C123")
        denied_read = await handlers["read_slack_bot_messages"]("C999")
        denied_send = await handlers["send_slack_bot_message"]("C123", "hello")

    assert "hi" in allowed
    assert denied_read.startswith("Access denied")
    assert denied_send.startswith("Access denied")


@pytest.mark.asyncio
async def test_whatsapp_publish_blocked_gates_channel_less_writes():
    """WhatsApp mark-read / react have no recipient argument to whitelist —
    they must fail closed via ``publish_blocked``."""
    handlers = _build_provider_tools(
        "app.integrations.providers.whatsapp_bot",
        "pn-123||token-abc",
        publish_channels=list(_SENTINEL),
        publish_blocked=True,
    )

    assert (await handlers["mark_whatsapp_bot_read"]("wamid.1")).startswith(
        "Access denied"
    )
    assert (
        await handlers["react_whatsapp_bot_message"]("wamid.1", "👍")
    ).startswith("Access denied")


@pytest.mark.asyncio
async def test_googlechat_delete_respects_publish_whitelist():
    """Deleting a message is a write to the space — it must honor the same
    publish whitelist as sends (it used to bypass it entirely)."""
    handlers = _build_provider_tools(
        "app.integrations.providers.googlechat_bot",
        '{"type": "service_account"}',
        publish_channels=[{"id": "spaces/ALLOWED", "name": "ok"}],
    )

    denied = await handlers["delete_googlechat_bot_message"](
        "spaces/OTHER/messages/MSG1"
    )
    assert denied.startswith("Access denied")


# ---------------------------------------------------------------------------
# Thread-context formatting — Adaptive Card content
# ---------------------------------------------------------------------------


_TICKET_CARD = {
    "type": "AdaptiveCard",
    "version": "1.5",
    "body": [
        {"type": "TextBlock", "text": "Test: Ticket-linked Adaptive Card"},
        {
            "type": "Container",
            "items": [
                {
                    "type": "FactSet",
                    "facts": [
                        {"title": "Ticket", "value": "#423072371954"},
                        {"title": "Requester", "value": "Daniel Barker"},
                        {"title": "Status", "value": "ESCALATED (New)"},
                    ],
                }
            ],
        },
    ],
    "actions": [
        {
            "type": "Action.OpenUrl",
            "title": "View Ticket",
            "url": "https://app.hubspot.com/tickets/423072371954",
        }
    ],
}


def _graph_card_message() -> dict:
    """A Graph channel message as returned for a bot-posted Adaptive Card."""
    import json as _json

    return {
        "messageType": "message",
        "createdDateTime": "2026-07-14T10:44:00Z",
        "from": {"application": {"displayName": "Filigran Copilot"}},
        "body": {
            "contentType": "html",
            "content": '<attachment id="card-1"></attachment>',
        },
        "attachments": [
            {
                "id": "card-1",
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": _json.dumps(_TICKET_CARD),
            }
        ],
    }


def test_format_channel_message_renders_adaptive_card():
    """A card-only root message must render its card content — it used to be
    dropped entirely (empty body after HTML stripping), so the agent lost
    the thread topic."""
    from app.channels.teams import _format_channel_message

    line = _format_channel_message(_graph_card_message())
    assert line is not None
    assert "Filigran Copilot" in line  # application sender, not "Unknown"
    assert "Ticket #423072371954" in line
    assert "Requester Daniel Barker" in line
    assert "ESCALATED" in line
    assert "View Ticket" in line
    assert "<attachment" not in line


def test_format_channel_message_plain_text_unchanged():
    from app.channels.teams import _format_channel_message

    msg = {
        "messageType": "message",
        "createdDateTime": "2026-07-14T10:45:00Z",
        "from": {"user": {"displayName": "Samuel Hassine"}},
        "body": {"contentType": "html", "content": "<p>grant this one</p>"},
    }
    line = _format_channel_message(msg)
    assert line == "[2026-07-14T10:45] Samuel Hassine: grant this one"


def test_format_channel_message_still_drops_empty_and_system_messages():
    from app.channels.teams import _format_channel_message

    assert _format_channel_message({"messageType": "systemEventMessage"}) is None
    assert (
        _format_channel_message(
            {"messageType": "message", "body": {"content": ""}}
        )
        is None
    )


def test_format_channel_message_renders_hero_card_and_file_attachment():
    from app.channels.teams import _format_channel_message

    msg = {
        "messageType": "message",
        "createdDateTime": "2026-07-14T10:46:00Z",
        "from": {"application": {"displayName": "Filigran Copilot"}},
        "body": {"contentType": "html", "content": '<attachment id="h1"></attachment>'},
        "attachments": [
            {
                "id": "h1",
                "contentType": "application/vnd.microsoft.card.hero",
                "content": {"title": "Weekly digest", "text": "3 new escalations"},
            },
            {
                "id": "f1",
                "contentType": "reference",
                "name": "report.pdf",
            },
        ],
    }
    line = _format_channel_message(msg)
    assert line is not None
    assert "Weekly digest" in line
    assert "3 new escalations" in line
    assert "[Attachment: report.pdf]" in line


def test_adaptive_card_text_handles_columnsets_and_malformed_entries():
    from app.channels.teams import _adaptive_card_text

    card = {
        "body": [
            {
                "type": "ColumnSet",
                "columns": [
                    {"items": [{"type": "TextBlock", "text": "left"}]},
                    {"items": [{"type": "TextBlock", "text": "right"}]},
                ],
            },
            "not-a-dict",
            {"type": "FactSet", "facts": [{"title": "K", "value": "V"}, None]},
        ],
        "actions": [{"type": "Action.Submit", "title": "Approve"}],
    }
    text = _adaptive_card_text(card)
    assert "left" in text
    assert "right" in text
    assert "K V" in text
    assert "[Action: Approve]" in text


# ---------------------------------------------------------------------------
# WhatsApp error notice (DM processing failures)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whatsapp_error_notice_sent_on_processing_failure():
    """A DM processing exception must produce a user-visible error notice.

    Regression: the error path unpacked the 4-tuple from
    ``_get_whatsapp_creds`` into 3 variables, raising ``ValueError`` inside
    the handler and silently swallowing the notice.
    """
    from app.channels import whatsapp as whatsapp_module

    inbound = InboundMessage(
        channel=ChannelType.WHATSAPP,
        text="hello",
        channel_user_id="33612345678",
        channel_thread_id="33612345678",
        metadata={"chat_type": "private", "contact_name": "Sam", "group_id": None},
    )

    sent: list[OutboundMessage] = []

    class _FakeWhatsAppChannel:
        def __init__(self, phone_number_id, access_token):
            assert phone_number_id == "pn-123"
            assert access_token == "token-abc"

        async def send_message(self, message):
            sent.append(message)
            return "wamid.1"

    with (
        patch.object(
            whatsapp_module,
            "_get_whatsapp_creds",
            new=AsyncMock(return_value=("token-abc", "pn-123", "verify", "secret")),
        ),
        patch.object(
            whatsapp_module,
            "handle_message",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch.object(whatsapp_module, "WhatsAppChannel", _FakeWhatsAppChannel),
    ):
        await whatsapp_module._process_whatsapp_message(inbound)

    assert len(sent) == 1
    assert "error" in sent[0].text.lower()
    assert sent[0].channel_user_id == "33612345678"
