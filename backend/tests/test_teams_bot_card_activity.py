"""Regression tests for Teams Bot Adaptive Card activity payloads.

The Teams/Skype connector rejects a Bot Framework activity that carries BOTH
renderable ``text`` and ``attachments`` with HTTP 400
``BadSyntax: "Activity resulted into multiple skype activities"`` — it turns
such an activity into two separate Skype-level messages.

These tests pin the fix: the Adaptive Card fallback is carried on the
activity's ``summary`` field (notification-preview metadata) instead of
``text``, so the outgoing wire payload is always a single message.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.integrations.providers import teams_bot
from app.integrations.providers.teams_bot import (
    _build_card_activity,
    _card_is_interactive,
    _card_must_stay_card,
    _card_to_markdown,
    _ensure_full_width_card,
    _is_chat_conversation,
    _is_team_channel_conversation,
    build_tools,
)
from app.tools.base import CallableToolExecutor

_CARD = {
    "type": "AdaptiveCard",
    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
    "version": "1.5",
    "body": [{"type": "TextBlock", "text": "Daily digest", "weight": "Bolder"}],
}


class TestEnsureFullWidthCard:
    def test_injects_full_width(self):
        result = _ensure_full_width_card(_CARD)
        assert result["msteams"] == {"width": "Full"}

    def test_does_not_mutate_input(self):
        card = {"type": "AdaptiveCard", "body": []}
        _ensure_full_width_card(card)
        assert "msteams" not in card, "the caller's card must not be mutated"

    def test_preserves_explicit_width(self):
        card = {"type": "AdaptiveCard", "body": [], "msteams": {"width": "Auto"}}
        result = _ensure_full_width_card(card)
        assert result["msteams"]["width"] == "Auto"

    def test_merges_with_other_msteams_keys(self):
        card = {"type": "AdaptiveCard", "body": [], "msteams": {"entities": []}}
        result = _ensure_full_width_card(card)
        assert result["msteams"]["width"] == "Full"
        assert result["msteams"]["entities"] == []

    def test_non_dict_returned_unchanged(self):
        assert _ensure_full_width_card(None) is None

    def test_returns_shallow_copy_when_width_already_set(self):
        card = {"type": "AdaptiveCard", "body": [], "msteams": {"width": "Full"}}
        result = _ensure_full_width_card(card)
        assert result is not card, "must return a fresh shallow copy, not the original"
        assert result == card


class TestBuildCardActivity:
    def test_fallback_goes_to_summary_not_text(self):
        activity = _build_card_activity(_CARD, "Daily digest summary")
        assert activity["type"] == "message"
        assert "text" not in activity, (
            "`text` alongside `attachments` triggers the multi-skype-activity 400"
        )
        assert activity["summary"] == "Daily digest summary"
        assert len(activity["attachments"]) == 1
        assert (
            activity["attachments"][0]["contentType"]
            == "application/vnd.microsoft.card.adaptive"
        )
        content = activity["attachments"][0]["content"]
        assert content["body"] == _CARD["body"]
        assert content["msteams"]["width"] == "Full"

    def test_card_is_opted_into_full_width(self):
        activity = _build_card_activity(_CARD)
        assert activity["attachments"][0]["content"]["msteams"]["width"] == "Full"

    def test_no_fallback_omits_both_text_and_summary(self):
        activity = _build_card_activity(_CARD)
        assert "text" not in activity
        assert "summary" not in activity
        assert len(activity["attachments"]) == 1

    def test_empty_fallback_omits_summary(self):
        activity = _build_card_activity(_CARD, "")
        assert "text" not in activity
        assert "summary" not in activity


_INTERACTIVE_CARD = {
    "type": "AdaptiveCard",
    "version": "1.5",
    "body": [
        {"type": "TextBlock", "text": "Approve the request?"},
        {"type": "Input.Text", "id": "reason", "label": "Reason"},
    ],
    "actions": [{"type": "Action.Submit", "title": "Approve", "data": {"ok": True}}],
}

_DIGEST_CARD = {
    "type": "AdaptiveCard",
    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
    "version": "1.5",
    "body": [
        {"type": "TextBlock", "text": "Daily Digest", "size": "Large", "weight": "Bolder"},
        {"type": "TextBlock", "text": "Highlights", "weight": "Bolder"},
        {"type": "TextBlock", "text": "- giranih763 still blocked on Docker"},
        {
            "type": "FactSet",
            "facts": [{"title": "Open threads:", "value": "3"}],
        },
    ],
    "actions": [
        {"type": "Action.OpenUrl", "title": "View thread", "url": "https://example.com/t"}
    ],
}


class TestIsTeamChannelConversation:
    def test_team_channel_suffix_is_channel(self):
        assert _is_team_channel_conversation("19:abc@thread.tacv2")
        assert _is_team_channel_conversation("19:abc@thread.skype")

    def test_group_chat_is_not_channel(self):
        assert not _is_team_channel_conversation("19:abc@thread.v2")

    def test_one_on_one_is_not_channel(self):
        assert not _is_team_channel_conversation("19:a_b@unq.gbl.spaces")

    def test_unknown_or_empty_is_not_channel(self):
        # Conservative: anything not provably a channel is treated as a chat,
        # so the worst case is a channel card degrading to full-width Markdown.
        assert not _is_team_channel_conversation("")
        assert not _is_team_channel_conversation("19:something-else")

    def test_channel_thread_with_messageid_suffix_is_channel(self):
        # Channel thread ids carry a trailing ";messageid=<root>" (see the
        # thread_key usage in channels/teams.py). The suffix must be stripped
        # before matching, otherwise a real channel thread is misclassified as
        # a chat and its display card is needlessly downgraded to Markdown.
        assert _is_team_channel_conversation("19:abc@thread.tacv2;messageid=1700000000000")
        assert _is_team_channel_conversation("19:abc@thread.skype;messageid=42")


_CHART_CARD = {
    "type": "AdaptiveCard",
    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
    "version": "1.5",
    "body": [
        {"type": "TextBlock", "text": "Object type breakdown", "weight": "Bolder"},
        {
            "type": "Chart.Donut",
            "data": [
                {"legend": "Indicators", "value": 27},
                {"legend": "Countries", "value": 17},
            ],
        },
    ],
}


class TestCardMustStayCard:
    def test_chart_card_must_stay_card(self):
        # Charts are native Teams elements Markdown cannot represent —
        # downgrading would silently drop the chart.
        assert _card_must_stay_card(_CHART_CARD)

    def test_all_chart_types_must_stay_card(self):
        for chart_type in (
            "Chart.Donut",
            "Chart.Pie",
            "Chart.VerticalBar",
            "Chart.VerticalBar.Grouped",
            "Chart.HorizontalBar",
            "Chart.HorizontalBar.Stacked",
            "Chart.Line",
            "Chart.Gauge",
        ):
            card = {"type": "AdaptiveCard", "body": [{"type": chart_type}]}
            assert _card_must_stay_card(card), chart_type

    def test_media_table_codeblock_must_stay_card(self):
        for etype in ("Media", "Table", "CodeBlock", "Carousel"):
            card = {"type": "AdaptiveCard", "body": [{"type": etype}]}
            assert _card_must_stay_card(card), etype

    def test_interactive_card_must_stay_card(self):
        assert _card_must_stay_card(_INTERACTIVE_CARD)

    def test_plain_display_card_can_downgrade(self):
        assert not _card_must_stay_card(_DIGEST_CARD)
        assert not _card_must_stay_card(_CARD)

    def test_non_dict_is_false(self):
        assert not _card_must_stay_card(None)


class TestIsChatConversation:
    def test_group_chat_is_chat(self):
        assert _is_chat_conversation("19:abc@thread.v2")

    def test_meeting_chat_is_chat(self):
        assert _is_chat_conversation("19:meeting_YzJjYjUz@thread.v2")

    def test_one_on_one_is_chat(self):
        assert _is_chat_conversation("19:a_b@unq.gbl.spaces")

    def test_team_channel_is_not_chat(self):
        assert not _is_chat_conversation("19:abc@thread.tacv2")
        assert not _is_chat_conversation("19:abc@thread.skype")

    def test_empty_is_not_chat(self):
        assert not _is_chat_conversation("")


class TestCardIsInteractive:
    def test_submit_action_is_interactive(self):
        assert _card_is_interactive(_INTERACTIVE_CARD)

    def test_input_element_is_interactive(self):
        assert _card_is_interactive(
            {"type": "AdaptiveCard", "body": [{"type": "Input.Toggle", "id": "x"}]}
        )

    def test_open_url_is_not_interactive(self):
        # A hyperlink renders fine as Markdown — not "interactive".
        assert not _card_is_interactive(_DIGEST_CARD)

    def test_plain_display_card_is_not_interactive(self):
        assert not _card_is_interactive(_CARD)

    def test_non_dict_is_not_interactive(self):
        assert not _card_is_interactive(None)


class TestCardToMarkdown:
    def test_renders_headings_bold_facts_and_links(self):
        md = _card_to_markdown(_DIGEST_CARD)
        assert "## Daily Digest" in md
        assert "**Highlights**" in md
        assert "- giranih763 still blocked on Docker" in md
        assert "- **Open threads:** 3" in md
        assert "[View thread](https://example.com/t)" in md

    def test_empty_card_returns_empty_string(self):
        assert _card_to_markdown({"type": "AdaptiveCard", "body": []}) == ""

    def test_non_dict_returns_empty_string(self):
        assert _card_to_markdown(None) == ""

    def test_nested_containers_are_flattened(self):
        card = {
            "type": "AdaptiveCard",
            "body": [
                {
                    "type": "Container",
                    "items": [{"type": "TextBlock", "text": "Inside container"}],
                }
            ],
        }
        assert "Inside container" in _card_to_markdown(card)

    def test_non_string_text_never_renders_literal_none(self):
        # Best-effort renderer: malformed cards with a non-string text (None)
        # must not leak the literal "None" into the user-visible message.
        card = {
            "type": "AdaptiveCard",
            "body": [
                {"type": "TextBlock", "text": None},
                {
                    "type": "RichTextBlock",
                    "inlines": [{"type": "TextRun", "text": None}, {"text": "real"}],
                },
            ],
        }
        md = _card_to_markdown(card)
        assert "None" not in md
        assert "real" in md

    def test_factset_omits_bold_wrapper_when_title_empty(self):
        # A value-only fact must not emit a malformed empty bold marker.
        card = {
            "type": "AdaptiveCard",
            "body": [
                {
                    "type": "FactSet",
                    "facts": [
                        {"title": "Status:", "value": "OK"},
                        {"value": "value only"},
                        {"title": "Title only"},
                    ],
                }
            ],
        }
        md = _card_to_markdown(card)
        assert "- **Status:** OK" in md
        assert "- value only" in md
        assert "- **Title only**" in md
        assert "****" not in md

    def test_factset_non_string_title_value_never_renders_literal_none(self):
        # A malformed fact with an explicit None title/value must not stringify
        # into the literal "None" (e.g. "- **None** None") in the downgrade.
        card = {
            "type": "AdaptiveCard",
            "body": [
                {
                    "type": "FactSet",
                    "facts": [
                        {"title": None, "value": "real value"},
                        {"title": "Real title", "value": None},
                        {"title": None, "value": None},
                    ],
                }
            ],
        }
        md = _card_to_markdown(card)
        assert "None" not in md
        assert "- real value" in md
        assert "- **Real title**" in md

    def test_image_non_string_alt_or_url_never_renders_literal_none(self):
        # A malformed Image with a non-string altText must not leak "None",
        # and a non-string url must be skipped entirely.
        card = {
            "type": "AdaptiveCard",
            "body": [
                {"type": "Image", "url": "https://x/y.png", "altText": None},
                {"type": "Image", "url": None, "altText": "ignored"},
            ],
        }
        md = _card_to_markdown(card)
        assert "None" not in md
        # Valid url renders with the fallback alt text ...
        assert "![image](https://x/y.png)" in md
        # ... and the non-string url is dropped (no broken/`None` link).
        assert "ignored" not in md

    def test_open_url_non_string_title_never_renders_literal_none(self):
        # A malformed Action.OpenUrl with title=None must fall back to the url
        # as link text, never the literal "None"; a non-string url is skipped.
        card = {
            "type": "AdaptiveCard",
            "actions": [
                {"type": "Action.OpenUrl", "title": None, "url": "https://x/z"},
                {"type": "Action.OpenUrl", "title": "skip", "url": None},
            ],
        }
        md = _card_to_markdown(card)
        assert "None" not in md
        assert "[https://x/z](https://x/z)" in md
        assert "skip" not in md

    def test_non_list_inlines_and_facts_are_ignored(self):
        # A malformed card may provide a dict (not a list) for inlines/facts;
        # iterating it would leak the dict's keys into the downgraded Markdown.
        card = {
            "type": "AdaptiveCard",
            "body": [
                {"type": "RichTextBlock", "inlines": {"leak_inline_key": "x"}},
                {"type": "FactSet", "facts": {"leak_fact_key": "y"}},
                {"type": "TextBlock", "text": "real content"},
            ],
        }
        md = _card_to_markdown(card)
        assert "leak_inline_key" not in md
        assert "leak_fact_key" not in md
        # The well-formed element still renders.
        assert "real content" in md


def _make_executor() -> CallableToolExecutor:
    """Build the Teams Bot tools and return the populated executor.

    The credential triple (``tenant||client||secret``) is dummy; ``safe_request``
    and the token/service-url helpers are patched per-test, so callers inspect
    the captured wire payload via the local ``captured`` dict rather than a
    returned mock.
    """
    executor = CallableToolExecutor()
    build_tools("tenant-123||client-abc||secret-xyz", executor)
    return executor


def _fake_response(payload: dict):
    resp = AsyncMock()
    resp.json = lambda: payload
    return resp


@pytest.mark.asyncio
async def test_send_channel_card_payload_has_no_text():
    executor = _make_executor()
    captured: dict = {}

    async def fake_safe_request(client, method, url, provider, action, **kwargs):
        captured["json"] = kwargs.get("json")
        captured["url"] = url
        return _fake_response({"activityId": "act-1"}), None

    with (
        patch.object(
            teams_bot, "_acquire_token", new=AsyncMock(return_value="bf-token")
        ),
        patch.object(
            teams_bot,
            "_get_service_url",
            new=AsyncMock(return_value="https://smba.trafficmanager.net/amer"),
        ),
        patch.object(teams_bot, "safe_request", new=fake_safe_request),
    ):
        result = await executor._handlers["send_teams_bot_channel_card"](
            team_id="team-1",
            channel_id="19:channel-1@thread.tacv2",
            card=_CARD,
            fallback_text="Daily digest summary",
        )

    assert "activity ID" in result
    activity = captured["json"]["activity"]
    assert "text" not in activity
    assert activity["summary"] == "Daily digest summary"
    assert captured["json"]["isGroup"] is True
    assert captured["url"].endswith("/v3/conversations")


@pytest.mark.asyncio
async def test_reply_channel_card_payload_has_no_text():
    executor = _make_executor()
    captured: dict = {}

    async def fake_safe_request(client, method, url, provider, action, **kwargs):
        captured["json"] = kwargs.get("json")
        captured["url"] = url
        return _fake_response({"id": "reply-1"}), None

    with (
        patch.object(
            teams_bot, "_acquire_token", new=AsyncMock(return_value="bf-token")
        ),
        patch.object(
            teams_bot,
            "_get_service_url",
            new=AsyncMock(return_value="https://smba.trafficmanager.net/amer"),
        ),
        patch.object(teams_bot, "safe_request", new=fake_safe_request),
    ):
        result = await executor._handlers["send_teams_bot_channel_card"](
            team_id="team-1",
            channel_id="19:channel-1@thread.tacv2",
            card=_CARD,
            fallback_text="Daily digest summary",
            reply_to_id="msg-root",
        )

    assert "reply sent" in result.lower()
    activity = captured["json"]
    assert "text" not in activity
    assert activity["summary"] == "Daily digest summary"
    assert activity["replyToId"] == "msg-root"
    # Teams ignores ``replyToId`` for threading (botframework-sdk#6626) —
    # the reply must target the THREADED conversation id.
    assert captured["url"].endswith(
        "/v3/conversations/19:channel-1@thread.tacv2;messageid=msg-root/activities"
    )


@pytest.mark.asyncio
async def test_reply_channel_message_posts_to_threaded_conversation_id():
    """A text reply with reply_to_id must post to the threaded conversation id
    (channel id + ``;messageid=<root>``) — Teams ignores ``replyToId``."""
    executor = _make_executor()
    captured: dict = {}

    async def fake_safe_request(client, method, url, provider, action, **kwargs):
        captured["json"] = kwargs.get("json")
        captured["url"] = url
        return _fake_response({"id": "reply-1"}), None

    with (
        patch.object(
            teams_bot, "_acquire_token", new=AsyncMock(return_value="bf-token")
        ),
        patch.object(
            teams_bot,
            "_get_service_url",
            new=AsyncMock(return_value="https://smba.trafficmanager.net/amer"),
        ),
        patch.object(teams_bot, "safe_request", new=fake_safe_request),
    ):
        result = await executor._handlers["send_teams_bot_channel_message"](
            team_id="team-1",
            channel_id="19:channel-1@thread.tacv2",
            message="Hello thread",
            reply_to_id="1234567890",
        )

    assert "reply sent" in result.lower()
    assert captured["json"]["replyToId"] == "1234567890"
    assert captured["url"].endswith(
        "/v3/conversations/19:channel-1@thread.tacv2;messageid=1234567890/activities"
    )


@pytest.mark.asyncio
async def test_thread_suffixed_channel_id_auto_threads_and_passes_whitelist():
    """Passing the message-context ``channel_thread_id``
    (``19:...@thread.tacv2;messageid=<root>``) as channel_id must thread the
    send AND still pass the publish whitelist (which stores bare channel ids).
    """
    executor = CallableToolExecutor()
    build_tools(
        "tenant-123||client-abc||secret-xyz",
        executor,
        publish_channels=[{"id": "team-1:19:channel-1@thread.tacv2"}],
    )
    captured: dict = {}

    async def fake_safe_request(client, method, url, provider, action, **kwargs):
        captured["json"] = kwargs.get("json")
        captured["url"] = url
        return _fake_response({"id": "reply-1"}), None

    with (
        patch.object(
            teams_bot, "_acquire_token", new=AsyncMock(return_value="bf-token")
        ),
        patch.object(
            teams_bot,
            "_get_service_url",
            new=AsyncMock(return_value="https://smba.trafficmanager.net/amer"),
        ),
        patch.object(teams_bot, "safe_request", new=fake_safe_request),
    ):
        result = await executor._handlers["send_teams_bot_channel_card"](
            team_id="team-1",
            channel_id="19:channel-1@thread.tacv2;messageid=987654321",
            card=_CARD,
        )

    assert "access denied" not in result.lower(), (
        "the whitelist check must run on the bare channel id"
    )
    assert "reply sent" in result.lower()
    assert captured["json"]["replyToId"] == "987654321"
    assert captured["url"].endswith(
        "/v3/conversations/19:channel-1@thread.tacv2;messageid=987654321/activities"
    )


@pytest.mark.asyncio
async def test_explicit_reply_to_id_wins_over_embedded_root():
    """An explicit reply_to_id takes precedence over the id embedded in a
    thread-suffixed channel_id."""
    executor = _make_executor()
    captured: dict = {}

    async def fake_safe_request(client, method, url, provider, action, **kwargs):
        captured["url"] = url
        return _fake_response({"id": "reply-1"}), None

    with (
        patch.object(
            teams_bot, "_acquire_token", new=AsyncMock(return_value="bf-token")
        ),
        patch.object(
            teams_bot,
            "_get_service_url",
            new=AsyncMock(return_value="https://smba.trafficmanager.net/amer"),
        ),
        patch.object(teams_bot, "safe_request", new=fake_safe_request),
    ):
        await executor._handlers["send_teams_bot_channel_message"](
            team_id="team-1",
            channel_id="19:channel-1@thread.tacv2;messageid=111",
            message="Hello",
            reply_to_id="222",
        )

    assert captured["url"].endswith(
        "/v3/conversations/19:channel-1@thread.tacv2;messageid=222/activities"
    )


@pytest.mark.asyncio
async def test_reply_to_id_with_semicolon_is_rejected():
    """An explicit reply_to_id containing ';' must be rejected — it is
    interpolated into the threaded conversation id and could inject extra
    conversation-id suffix segments."""
    executor = _make_executor()
    called: dict = {}

    async def fake_safe_request(client, method, url, provider, action, **kwargs):
        called["url"] = url
        return _fake_response({"id": "reply-1"}), None

    with (
        patch.object(
            teams_bot, "_acquire_token", new=AsyncMock(return_value="bf-token")
        ),
        patch.object(
            teams_bot,
            "_get_service_url",
            new=AsyncMock(return_value="https://smba.trafficmanager.net/amer"),
        ),
        patch.object(teams_bot, "safe_request", new=fake_safe_request),
    ):
        msg_result = await executor._handlers["send_teams_bot_channel_message"](
            team_id="team-1",
            channel_id="19:channel-1@thread.tacv2",
            message="Hello",
            reply_to_id="123;messageid=456",
        )
        card_result = await executor._handlers["send_teams_bot_channel_card"](
            team_id="team-1",
            channel_id="19:channel-1@thread.tacv2",
            card=_INTERACTIVE_CARD,
            reply_to_id="123;messageid=456",
        )

    assert msg_result.startswith("Error: Invalid reply_to_id")
    assert card_result.startswith("Error: Invalid reply_to_id")
    assert "url" not in called, "no request may be sent for an invalid reply_to_id"


class TestSplitChannelThreadId:
    def test_splits_messageid_suffix(self):
        base, root = teams_bot._split_channel_thread_id(
            "19:abc@thread.tacv2;messageid=1700000000000"
        )
        assert base == "19:abc@thread.tacv2"
        assert root == "1700000000000"

    def test_bare_channel_id_unchanged(self):
        base, root = teams_bot._split_channel_thread_id("19:abc@thread.tacv2")
        assert base == "19:abc@thread.tacv2"
        assert root == ""

    def test_non_messageid_suffix_left_intact(self):
        base, root = teams_bot._split_channel_thread_id("19:abc@thread.tacv2;other=1")
        assert base == "19:abc@thread.tacv2;other=1"
        assert root == ""

    def test_root_stops_at_next_semicolon(self):
        # The extracted root is interpolated back into a threaded
        # conversation id — trailing ";"-delimited segments must not leak.
        base, root = teams_bot._split_channel_thread_id(
            "19:abc@thread.tacv2;messageid=123;other=1"
        )
        assert base == "19:abc@thread.tacv2"
        assert root == "123"

    def test_empty_input(self):
        assert teams_bot._split_channel_thread_id("") == ("", "")


@pytest.mark.asyncio
async def test_channel_card_to_group_chat_downgrades_to_full_width_text():
    """A display-only card sent to a CHAT conversation id (group chat) is
    delivered as a full-width Markdown text message, not a constrained card.

    Adaptive Cards cap to the message-bubble width in chats; Markdown text
    renders edge-to-edge. The card path detects the chat conversation id,
    routes through the group-chat path, and the text-only card downgrades
    to Markdown.
    """
    executor = _make_executor()
    captured: dict = {}

    async def fake_safe_request(client, method, url, provider, action, **kwargs):
        captured["json"] = kwargs.get("json")
        captured["url"] = url
        return _fake_response({"id": "act-1"}), None

    with (
        patch.object(
            teams_bot, "_acquire_token", new=AsyncMock(return_value="bf-token")
        ),
        patch.object(
            teams_bot,
            "_get_service_url",
            new=AsyncMock(return_value="https://smba.trafficmanager.net/amer"),
        ),
        patch.object(teams_bot, "safe_request", new=fake_safe_request),
    ):
        result = await executor._handlers["send_teams_bot_channel_card"](
            team_id="team-1",
            channel_id="19:group-chat@thread.v2",
            card=_DIGEST_CARD,
            fallback_text="Daily digest summary",
        )

    assert "sent to group chat" in result.lower()
    # Chat conversation ids route via the direct conversation send — the
    # payload is the activity itself, not a create-conversation body.
    activity = captured["json"]
    assert "attachments" not in activity, (
        "a chat conversation id must downgrade the display card to text"
    )
    assert activity["type"] == "message"
    assert "Daily Digest" in activity["text"]
    assert captured["url"].endswith(
        "/v3/conversations/19:group-chat@thread.v2/activities"
    )


@pytest.mark.asyncio
async def test_channel_message_html_content_type_is_sanitized():
    """Agent-supplied HTML (content_type='html') must be sanitized before it is
    delivered with Bot Framework ``textFormat="xml"``.

    ``content_type`` and ``message`` are agent-controlled tool input, so the
    raw-HTML send surface must strip scripts / event handlers / unsafe URL
    schemes (same nh3 allow-list as the Graph path), while keeping legitimate
    structure such as a table.
    """
    executor = _make_executor()
    captured: dict = {}

    async def fake_safe_request(client, method, url, provider, action, **kwargs):
        captured["json"] = kwargs.get("json")
        return _fake_response({"id": "msg-1"}), None

    agent_html = (
        "<table><tr><td>ok</td></tr></table>"
        "<script>alert('xss')</script>"
        '<img src=x onerror="alert(1)">'
        '<a href="javascript:alert(2)">click</a>'
    )

    with (
        patch.object(
            teams_bot, "_acquire_token", new=AsyncMock(return_value="bf-token")
        ),
        patch.object(
            teams_bot,
            "_get_service_url",
            new=AsyncMock(return_value="https://smba.trafficmanager.net/amer"),
        ),
        patch.object(teams_bot, "safe_request", new=fake_safe_request),
    ):
        await executor._handlers["send_teams_bot_channel_message"](
            team_id="team-1",
            channel_id="19:channel-1@thread.tacv2",
            message=agent_html,
            content_type="html",
        )

    activity = captured["json"]["activity"]
    assert activity["textFormat"] == "xml"
    sent = activity["text"]
    # Legitimate structure survives ...
    assert "<table" in sent
    # ... but dangerous markup is stripped.
    assert "<script" not in sent.lower()
    assert "alert('xss')" not in sent
    assert "onerror" not in sent.lower()
    assert "javascript:" not in sent.lower()


@pytest.mark.asyncio
async def test_channel_card_to_meeting_chat_routes_via_group_chat_path():
    """A meeting-chat conversation id passed as channel_id must be routed to
    the group-chat card path (direct conversation send), bypassing the
    team/channel publish whitelist — chats are not whitelist-governed."""
    executor = CallableToolExecutor()
    build_tools(
        "tenant-123||client-abc||secret-xyz",
        executor,
        publish_channels=[{"id": "team-1:19:channel-1@thread.tacv2"}],
    )
    captured: dict = {}

    async def fake_safe_request(client, method, url, provider, action, **kwargs):
        captured["json"] = kwargs.get("json")
        captured["url"] = url
        captured["action"] = action
        return _fake_response({"id": "act-9"}), None

    with (
        patch.object(
            teams_bot, "_acquire_token", new=AsyncMock(return_value="bf-token")
        ),
        patch.object(
            teams_bot,
            "_get_service_url",
            new=AsyncMock(return_value="https://smba.trafficmanager.net/amer"),
        ),
        patch.object(teams_bot, "safe_request", new=fake_safe_request),
    ):
        result = await executor._handlers["send_teams_bot_channel_card"](
            team_id="",
            channel_id="19:meeting_YzJjYjUz@thread.v2",
            card=_CHART_CARD,
            fallback_text="Report breakdown",
        )

    assert "access denied" not in result.lower(), (
        "chat conversations must not be gated by the channel publish whitelist"
    )
    assert "group chat" in result.lower()
    # Direct conversation send, not the create-conversation channel path.
    assert captured["url"].endswith(
        "/v3/conversations/19:meeting_YzJjYjUz@thread.v2/activities"
    )
    # The chart card must stay a native card (Markdown cannot render charts).
    assert (
        captured["json"]["attachments"][0]["contentType"]
        == "application/vnd.microsoft.card.adaptive"
    )


@pytest.mark.asyncio
async def test_group_chat_chart_card_stays_card():
    """A display-only card containing a Chart.* element must NOT be
    downgraded to Markdown in a group chat — Markdown drops the chart."""
    executor = _make_executor()
    captured: dict = {}

    async def fake_safe_request(client, method, url, provider, action, **kwargs):
        captured["json"] = kwargs.get("json")
        captured["url"] = url
        return _fake_response({"id": "act-10"}), None

    with (
        patch.object(
            teams_bot, "_acquire_token", new=AsyncMock(return_value="bf-token")
        ),
        patch.object(
            teams_bot,
            "_get_service_url",
            new=AsyncMock(return_value="https://smba.trafficmanager.net/amer"),
        ),
        patch.object(teams_bot, "safe_request", new=fake_safe_request),
    ):
        result = await executor._handlers["send_teams_bot_group_chat_card"](
            chat_id="19:group-chat@thread.v2",
            card=_CHART_CARD,
            fallback_text="Breakdown",
        )

    assert "adaptive card sent" in result.lower()
    assert (
        captured["json"]["attachments"][0]["contentType"]
        == "application/vnd.microsoft.card.adaptive"
    )


@pytest.mark.asyncio
async def test_channel_message_to_meeting_chat_routes_via_group_chat_path():
    """A meeting-chat conversation id passed to the channel MESSAGE tool is
    routed to the group-chat send path as well."""
    executor = _make_executor()
    captured: dict = {}

    async def fake_safe_request(client, method, url, provider, action, **kwargs):
        captured["json"] = kwargs.get("json")
        captured["url"] = url
        return _fake_response({"id": "act-11"}), None

    with (
        patch.object(
            teams_bot, "_acquire_token", new=AsyncMock(return_value="bf-token")
        ),
        patch.object(
            teams_bot,
            "_get_service_url",
            new=AsyncMock(return_value="https://smba.trafficmanager.net/amer"),
        ),
        patch.object(teams_bot, "safe_request", new=fake_safe_request),
    ):
        result = await executor._handlers["send_teams_bot_channel_message"](
            team_id="",
            channel_id="19:meeting_YzJjYjUz@thread.v2",
            message="Hello meeting",
        )

    assert "group chat" in result.lower()
    assert captured["url"].endswith(
        "/v3/conversations/19:meeting_YzJjYjUz@thread.v2/activities"
    )


@pytest.mark.asyncio
async def test_channel_message_html_to_chat_keeps_html_format():
    """content_type='html' must survive the chat-surface routing — the
    forwarded send keeps the sanitized-HTML path (textFormat='xml') instead
    of pushing raw HTML through the Markdown normalizer."""
    executor = _make_executor()
    captured: dict = {}

    async def fake_safe_request(client, method, url, provider, action, **kwargs):
        captured["json"] = kwargs.get("json")
        captured["url"] = url
        return _fake_response({"id": "act-12"}), None

    with (
        patch.object(
            teams_bot, "_acquire_token", new=AsyncMock(return_value="bf-token")
        ),
        patch.object(
            teams_bot,
            "_get_service_url",
            new=AsyncMock(return_value="https://smba.trafficmanager.net/amer"),
        ),
        patch.object(teams_bot, "safe_request", new=fake_safe_request),
    ):
        result = await executor._handlers["send_teams_bot_channel_message"](
            team_id="",
            channel_id="19:group-chat@thread.v2",
            message="<b>Hello</b> chat",
            content_type="html",
        )

    assert "group chat" in result.lower()
    assert captured["json"]["textFormat"] == "xml"
    assert "<b>Hello</b>" in captured["json"]["text"]


@pytest.mark.asyncio
async def test_channel_card_interactive_to_group_chat_stays_card():
    """An interactive card sent to a chat conversation id stays a card —
    Markdown cannot represent a submit button / input."""
    executor = _make_executor()
    captured: dict = {}

    async def fake_safe_request(client, method, url, provider, action, **kwargs):
        captured["json"] = kwargs.get("json")
        captured["url"] = url
        return _fake_response({"id": "act-1"}), None

    with (
        patch.object(
            teams_bot, "_acquire_token", new=AsyncMock(return_value="bf-token")
        ),
        patch.object(
            teams_bot,
            "_get_service_url",
            new=AsyncMock(return_value="https://smba.trafficmanager.net/amer"),
        ),
        patch.object(teams_bot, "safe_request", new=fake_safe_request),
    ):
        await executor._handlers["send_teams_bot_channel_card"](
            team_id="team-1",
            channel_id="19:group-chat@thread.v2",
            card=_INTERACTIVE_CARD,
            fallback_text="Approve?",
        )

    # Chat conversation ids route via the direct conversation send — the
    # payload is the activity itself, not a create-conversation body.
    activity = captured["json"]
    assert (
        activity["attachments"][0]["contentType"]
        == "application/vnd.microsoft.card.adaptive"
    )
    assert captured["url"].endswith(
        "/v3/conversations/19:group-chat@thread.v2/activities"
    )
