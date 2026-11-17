"""Regression tests: bot @mention detection in Teams group chats under RSC.

Production symptom: users @mention the bot in a Teams group chat
(conversation id ``19:...@thread.v2``) and get no answer.  The agent's
``only_mention=true`` gate rejects every group-chat message with
"only_mention=true but bot was not @mentioned".

Root cause: the shipped Teams manifest grants receive-all RSC permissions
(``ChatMessage.Read.Chat`` / ``ChannelMessage.Read.Group``).  Under RSC,
Teams can deliver an @mention activity WITHOUT the mention entity — the
``entities`` array carries only ``clientInfo`` and the bot name is stripped
from the leading text (MicrosoftDocs/msteams-docs#14422).  The webhook's
entities-based detection (``mentioned.id == recipient.id``) then never
fires, ``bot_was_mentioned`` stays False, and the only_mention gate
silently drops genuine @mentions.

Fix: ``_process_teams_message`` re-reads the Graph copy of the message
(already fetched for the file-attachment fallback) and recovers the flag
from ``mentions[].mentioned.application.id == <bot app id>``.
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from app.channels.base import ChannelType, InboundMessage, OutboundMessage
from app.channels.router import (
    _agent_allows_channel_reason,
    _is_bot_mentioned,
    _is_private_conversation,
)
from app.channels.teams import (
    _graph_message_mentions_bot,
    _process_teams_message,
)

BOT_APP_ID = "8b67cf9c-bbcb-4104-b821-a95a5235d7ee"
GROUP_CHAT_ID = "19:3b4c5d6e7f8091a2b3c4d5e6f7081920@thread.v2"


def _graph_message(mentions: list | None = None) -> dict:
    return {
        "id": "1751234567890",
        "body": {"contentType": "html", "content": "<p>hello bot</p>"},
        "mentions": mentions or [],
    }


def _bot_mention_entry(app_id: str = BOT_APP_ID) -> dict:
    return {
        "id": 0,
        "mentionText": "Filigran Copilot",
        "mentioned": {"application": {"id": app_id, "displayName": "Filigran Copilot"}},
    }


def _user_mention_entry() -> dict:
    return {
        "id": 0,
        "mentionText": "John Doe",
        "mentioned": {"user": {"id": "11111111-2222-3333-4444-555555555555"}},
    }


# ---------------------------------------------------------------------------
# _graph_message_mentions_bot — Graph mentions shape
# ---------------------------------------------------------------------------


class TestGraphMessageMentionsBot:
    def test_bot_application_mention_detected(self):
        msg = _graph_message([_bot_mention_entry()])
        assert _graph_message_mentions_bot(msg, BOT_APP_ID) is True

    def test_app_id_comparison_is_case_insensitive(self):
        msg = _graph_message([_bot_mention_entry(BOT_APP_ID.upper())])
        assert _graph_message_mentions_bot(msg, BOT_APP_ID) is True

    def test_user_mention_is_not_a_bot_mention(self):
        msg = _graph_message([_user_mention_entry()])
        assert _graph_message_mentions_bot(msg, BOT_APP_ID) is False

    def test_other_application_mention_is_not_a_bot_mention(self):
        msg = _graph_message(
            [_bot_mention_entry("99999999-8888-7777-6666-555555555555")]
        )
        assert _graph_message_mentions_bot(msg, BOT_APP_ID) is False

    def test_no_mentions_returns_false(self):
        assert _graph_message_mentions_bot(_graph_message(), BOT_APP_ID) is False

    def test_missing_mentions_key_returns_false(self):
        assert _graph_message_mentions_bot({"id": "x"}, BOT_APP_ID) is False

    def test_none_mentions_returns_false(self):
        assert _graph_message_mentions_bot({"mentions": None}, BOT_APP_ID) is False

    def test_empty_app_id_returns_false(self):
        msg = _graph_message([_bot_mention_entry()])
        assert _graph_message_mentions_bot(msg, "") is False

    def test_mixed_mentions_detects_bot_among_users(self):
        msg = _graph_message([_user_mention_entry(), _bot_mention_entry()])
        assert _graph_message_mentions_bot(msg, BOT_APP_ID) is True


# ---------------------------------------------------------------------------
# _process_teams_message — end-to-end mention recovery from the Graph copy
# ---------------------------------------------------------------------------


def _groupchat_inbound(*, bot_was_mentioned: bool = False) -> InboundMessage:
    """A groupChat activity whose mention entity was stripped by RSC delivery."""
    return InboundMessage(
        channel=ChannelType.TEAMS,
        text="can you summarize the incident?",
        user_email="user@example.com",
        channel_user_id="29:user-id",
        channel_thread_id=GROUP_CHAT_ID,
        channel_message_id="1751234567890",
        metadata={
            "conversation_type": "groupChat",
            "bot_was_mentioned": bot_was_mentioned,
            "contact_name": "John Doe",
        },
    )


def _process_patches(graph_msg: dict | None, handle_mock: AsyncMock):
    """Common patch set for driving _process_teams_message in isolation."""
    return (
        patch(
            "app.channels.teams._get_teams_creds",
            new=AsyncMock(return_value=("bf-token", BOT_APP_ID)),
        ),
        patch(
            "app.channels.teams._get_teams_graph_creds",
            new=AsyncMock(return_value=("graph-token", BOT_APP_ID, "tenant-id")),
        ),
        patch(
            "app.channels.teams._fetch_graph_inbound_message",
            new=AsyncMock(return_value=graph_msg),
        ),
        patch(
            "app.channels.teams._store_conversation_reference",
            new=AsyncMock(),
        ),
        patch("app.channels.teams.handle_message", new=handle_mock),
        patch(
            "app.channels.teams.TeamsChannel.send_message",
            new=AsyncMock(return_value="reply-id"),
        ),
        patch(
            "app.channels.teams.TeamsChannel.send_typing_indicator",
            new=AsyncMock(),
        ),
    )


@pytest.mark.asyncio
async def test_groupchat_mention_recovered_from_graph_copy():
    """An RSC-stripped groupChat @mention must reach the router with
    bot_was_mentioned=True, recovered from the Graph message copy."""
    inbound = _groupchat_inbound(bot_was_mentioned=False)
    handle_mock = AsyncMock(return_value=OutboundMessage(text="Here is the summary"))
    graph_msg = _graph_message([_bot_mention_entry()])

    with ExitStack() as stack:
        for p in _process_patches(graph_msg, handle_mock):
            stack.enter_context(p)
        await _process_teams_message(
            inbound, "https://smba.trafficmanager.net/emea/", GROUP_CHAT_ID
        )

    handle_mock.assert_awaited_once()
    routed = handle_mock.await_args.args[0]
    assert routed.metadata["bot_was_mentioned"] is True
    # The recovered flag is what the router-side detector reads.
    assert _is_bot_mentioned(routed) is True


@pytest.mark.asyncio
async def test_groupchat_without_mention_stays_unmentioned():
    """RSC receive-all chatter (no bot mention on the Graph copy) must NOT
    be promoted to a mention — only_mention keeps suppressing it."""
    inbound = _groupchat_inbound(bot_was_mentioned=False)
    handle_mock = AsyncMock(
        return_value=OutboundMessage(text="", suppress=True)
    )
    graph_msg = _graph_message([_user_mention_entry()])

    with ExitStack() as stack:
        for p in _process_patches(graph_msg, handle_mock):
            stack.enter_context(p)
        await _process_teams_message(
            inbound, "https://smba.trafficmanager.net/emea/", GROUP_CHAT_ID
        )

    handle_mock.assert_awaited_once()
    routed = handle_mock.await_args.args[0]
    assert routed.metadata["bot_was_mentioned"] is False
    assert _is_bot_mentioned(routed) is False


@pytest.mark.asyncio
async def test_groupchat_entities_mention_skips_graph_lookup_for_flag():
    """When the webhook already detected the mention from the activity
    entities, the flag must survive untouched (Graph copy unavailable)."""
    inbound = _groupchat_inbound(bot_was_mentioned=True)
    handle_mock = AsyncMock(return_value=OutboundMessage(text="ok"))

    with ExitStack() as stack:
        for p in _process_patches(None, handle_mock):
            stack.enter_context(p)
        await _process_teams_message(
            inbound, "https://smba.trafficmanager.net/emea/", GROUP_CHAT_ID
        )

    handle_mock.assert_awaited_once()
    routed = handle_mock.await_args.args[0]
    assert routed.metadata["bot_was_mentioned"] is True


@pytest.mark.asyncio
async def test_groupchat_graph_fetch_failure_degrades_gracefully():
    """Graph creds present but the message fetch fails (returns None) ->
    no recovery, no crash, message still flows through routing."""
    inbound = _groupchat_inbound(bot_was_mentioned=False)
    handle_mock = AsyncMock(return_value=OutboundMessage(text="", suppress=True))

    with ExitStack() as stack:
        for p in _process_patches(None, handle_mock):
            stack.enter_context(p)
        await _process_teams_message(
            inbound, "https://smba.trafficmanager.net/emea/", GROUP_CHAT_ID
        )

    handle_mock.assert_awaited_once()
    routed = handle_mock.await_args.args[0]
    assert routed.metadata["bot_was_mentioned"] is False


@pytest.mark.asyncio
async def test_groupchat_mention_without_graph_creds_degrades_gracefully():
    """No Graph credentials -> no recovery possible, but the message must
    still flow through routing without raising."""
    inbound = _groupchat_inbound(bot_was_mentioned=False)
    handle_mock = AsyncMock(return_value=OutboundMessage(text="", suppress=True))

    with ExitStack() as stack:
        for p in _process_patches(None, handle_mock):
            stack.enter_context(p)
        # Layered AFTER the base patches so it wins regardless of the
        # ordering inside _process_patches (order-independent override).
        stack.enter_context(
            patch(
                "app.channels.teams._get_teams_graph_creds",
                new=AsyncMock(return_value=None),
            )
        )
        await _process_teams_message(
            inbound, "https://smba.trafficmanager.net/emea/", GROUP_CHAT_ID
        )

    handle_mock.assert_awaited_once()
    routed = handle_mock.await_args.args[0]
    assert routed.metadata["bot_was_mentioned"] is False


@pytest.mark.asyncio
async def test_graph_creds_acquired_at_most_once_per_message():
    """A message that needs BOTH the mention fallback and /shares/ auth
    headers must acquire Graph credentials exactly once (shared between
    the two blocks), not once per block."""
    from app.channels.base import InboundFileAttachment
    from app.channels.teams import GRAPH_API

    inbound = _groupchat_inbound(bot_was_mentioned=False)
    inbound.file_attachments = [
        InboundFileAttachment(
            url=f"{GRAPH_API}/shares/u!abc/driveItem/content",
            filename="shared-doc.docx",
        )
    ]
    handle_mock = AsyncMock(return_value=OutboundMessage(text="ok"))
    creds_mock = AsyncMock(return_value=("graph-token", BOT_APP_ID, "tenant-id"))

    with ExitStack() as stack:
        for p in _process_patches(_graph_message([_bot_mention_entry()]), handle_mock):
            stack.enter_context(p)
        stack.enter_context(
            patch("app.channels.teams._get_teams_graph_creds", new=creds_mock)
        )
        await _process_teams_message(
            inbound, "https://smba.trafficmanager.net/emea/", GROUP_CHAT_ID
        )

    assert creds_mock.await_count == 1
    handle_mock.assert_awaited_once()
    routed = handle_mock.await_args.args[0]
    assert routed.metadata["bot_was_mentioned"] is True
    # The shared credentials were also applied to the /shares/ attachment.
    assert routed.file_attachments[0].auth_headers == {
        "Authorization": "Bearer graph-token"
    }


# ---------------------------------------------------------------------------
# Router gate — the recovered flag passes only_mention for group chats
# ---------------------------------------------------------------------------


def _agent_only_mention() -> object:
    from types import SimpleNamespace

    return SimpleNamespace(
        channel_config={
            "teams": {
                "receive_enabled": True,
                "publish_enabled": True,
                "receive_channels": [],
                "publish_channels": [],
                "only_mention": True,
                "bypass_whitelist": True,
            }
        },
        available_channel_types=None,
    )


def _routed_inbound(*, bot_was_mentioned: bool) -> InboundMessage:
    return InboundMessage(
        channel=ChannelType.TEAMS,
        text="can you summarize the incident?",
        channel_thread_id=GROUP_CHAT_ID,
        metadata={
            "conversation_type": "groupChat",
            "bot_was_mentioned": bot_was_mentioned,
        },
    )


class TestOnlyMentionGateForGroupChats:
    def test_groupchat_is_not_private(self):
        # groupChat must stay non-private so only_mention applies to it.
        assert _is_private_conversation(_routed_inbound(bot_was_mentioned=True)) is False

    def test_recovered_mention_passes_only_mention_gate(self):
        allowed, reason = _agent_allows_channel_reason(
            _agent_only_mention(),
            ChannelType.TEAMS,
            _routed_inbound(bot_was_mentioned=True),
            None,
        )
        assert allowed is True, reason

    def test_unmentioned_groupchat_message_is_rejected(self):
        allowed, reason = _agent_allows_channel_reason(
            _agent_only_mention(),
            ChannelType.TEAMS,
            _routed_inbound(bot_was_mentioned=False),
            None,
        )
        assert allowed is False
        assert "only_mention" in reason

    def test_thread_continuation_still_bypasses_gate(self):
        inbound = _routed_inbound(bot_was_mentioned=False)
        inbound.metadata["is_thread_continuation"] = True
        allowed, reason = _agent_allows_channel_reason(
            _agent_only_mention(),
            ChannelType.TEAMS,
            inbound,
            None,
        )
        assert allowed is True, reason
