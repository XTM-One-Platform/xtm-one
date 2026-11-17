"""Behavioral tests for the channel routing resolution helpers.

Covers the three two-tier resolvers in ``app.channels.router``:

* ``_resolve_allow_anonymous`` — bot ``False`` is enforced (cannot be
  overridden), bot ``True`` lets the agent opt in/out, no bot -> ``False``.
* ``_resolve_allow_private`` — bot ``False`` is enforced, bot ``True`` lets
  the agent opt in/out, no bot -> ``True``.
* ``_resolve_only_mention`` — bot ``True`` is enforced (cannot be disabled),
  bot ``False`` lets the agent opt in/out, no bot -> ``False``.

And the two per-platform detectors:

* ``_is_private_conversation`` — Slack ``channel_type == "im"``, Teams
  ``conversation_type == "personal"`` (or non-``19:`` thread id), Telegram /
  WhatsApp ``chat_type == "private"`` (WhatsApp groups stay public so channel
  restrictions apply), Discord ``is_dm``, Google Chat ``space_type == "DM"``,
  Web always False.
* ``_is_bot_mentioned`` — Teams / Slack / Telegram / Discord / Google Chat
  use the adapter-set ``bot_was_mentioned`` flag; WhatsApp and Web are
  always False (the Cloud API provides no structured mention entities).

Assessment finding: OBS-8 (critical untested modules).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.channels.base import ChannelType, InboundMessage
from app.channels.router import (
    _is_bot_mentioned,
    _is_private_conversation,
    _resolve_allow_anonymous,
    _resolve_allow_private,
    _resolve_only_mention,
)

# ---------------------------------------------------------------------------
# Stub builders — the resolvers only read plain attributes, so lightweight
# SimpleNamespace stand-ins for ChannelBot / Agent rows are sufficient.
# ---------------------------------------------------------------------------


def _bot(
    *,
    allow_anonymous: bool = False,
    allow_private: bool = True,
    only_mention: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        allow_anonymous=allow_anonymous,
        allow_private=allow_private,
        only_mention=only_mention,
    )


def _agent(channel_config: dict[str, Any] | None) -> SimpleNamespace:
    return SimpleNamespace(channel_config=channel_config)


def _agent_with(channel: ChannelType, key: str, value: Any) -> SimpleNamespace:
    """Agent whose channel_config sets a single override key on *channel*."""
    return _agent({channel.value: {key: value}})


def _inbound(
    channel: ChannelType,
    *,
    metadata: dict[str, Any] | None = None,
    channel_thread_id: str | None = None,
) -> InboundMessage:
    return InboundMessage(
        channel=channel,
        text="hello",
        channel_thread_id=channel_thread_id,
        metadata=metadata or {},
    )


# ---------------------------------------------------------------------------
# _resolve_allow_anonymous — agent override x bot default matrix
# ---------------------------------------------------------------------------


class TestResolveAllowAnonymous:
    CH = ChannelType.SLACK

    @pytest.mark.parametrize(
        ("agent_override", "bot_value", "expected"),
        [
            # Bot allows anonymous: agent may opt in/out, None inherits.
            (True, True, True),
            (False, True, False),
            (None, True, True),
            # Bot forbids anonymous: enforced, agent cannot override.
            (True, False, False),
            (False, False, False),
            (None, False, False),
        ],
    )
    def test_agent_override_with_bot(self, agent_override, bot_value, expected):
        agent = _agent_with(self.CH, "allow_anonymous", agent_override)
        bot = _bot(allow_anonymous=bot_value)
        assert _resolve_allow_anonymous(bot, agent, self.CH) is expected

    @pytest.mark.parametrize(
        ("agent_override", "expected"),
        [(True, True), (False, False), (None, False)],
    )
    def test_no_bot_defaults_to_false(self, agent_override, expected):
        agent = _agent_with(self.CH, "allow_anonymous", agent_override)
        assert _resolve_allow_anonymous(None, agent, self.CH) is expected

    def test_no_bot_no_agent_is_false(self):
        assert _resolve_allow_anonymous(None, None, self.CH) is False

    def test_agent_without_channel_config_inherits_bot(self):
        assert (
            _resolve_allow_anonymous(_bot(allow_anonymous=True), _agent(None), self.CH)
            is True
        )
        assert (
            _resolve_allow_anonymous(_bot(allow_anonymous=False), _agent(None), self.CH)
            is False
        )

    def test_agent_config_for_other_channel_is_ignored(self):
        # Override set on Teams must not affect Slack resolution.
        agent = _agent_with(ChannelType.TEAMS, "allow_anonymous", True)
        assert _resolve_allow_anonymous(None, agent, self.CH) is False


# ---------------------------------------------------------------------------
# _resolve_allow_private — agent override x bot default matrix
# ---------------------------------------------------------------------------


class TestResolveAllowPrivate:
    CH = ChannelType.TEAMS

    @pytest.mark.parametrize(
        ("agent_override", "bot_value", "expected"),
        [
            # Bot allows private: agent may opt in/out, None inherits.
            (True, True, True),
            (False, True, False),
            (None, True, True),
            # Bot forbids private: enforced, agent cannot override.
            (True, False, False),
            (False, False, False),
            (None, False, False),
        ],
    )
    def test_agent_override_with_bot(self, agent_override, bot_value, expected):
        agent = _agent_with(self.CH, "allow_private", agent_override)
        bot = _bot(allow_private=bot_value)
        assert _resolve_allow_private(bot, agent, self.CH) is expected

    @pytest.mark.parametrize(
        ("agent_override", "expected"),
        [(True, True), (False, False), (None, True)],
    )
    def test_no_bot_defaults_to_true(self, agent_override, expected):
        agent = _agent_with(self.CH, "allow_private", agent_override)
        assert _resolve_allow_private(None, agent, self.CH) is expected

    def test_no_bot_no_agent_is_true(self):
        assert _resolve_allow_private(None, None, self.CH) is True

    def test_agent_without_channel_config_inherits_bot(self):
        assert (
            _resolve_allow_private(_bot(allow_private=True), _agent(None), self.CH)
            is True
        )
        assert (
            _resolve_allow_private(_bot(allow_private=False), _agent(None), self.CH)
            is False
        )


# ---------------------------------------------------------------------------
# _resolve_only_mention — agent override x bot default matrix
# ---------------------------------------------------------------------------


class TestResolveOnlyMention:
    CH = ChannelType.TELEGRAM

    @pytest.mark.parametrize(
        ("agent_override", "bot_value", "expected"),
        [
            # Bot enforces only-mention: cannot be disabled by the agent.
            (True, True, True),
            (False, True, True),
            (None, True, True),
            # Bot relaxed: agent may opt in/out, None inherits False.
            (True, False, True),
            (False, False, False),
            (None, False, False),
        ],
    )
    def test_agent_override_with_bot(self, agent_override, bot_value, expected):
        agent = _agent_with(self.CH, "only_mention", agent_override)
        bot = _bot(only_mention=bot_value)
        assert _resolve_only_mention(bot, agent, self.CH) is expected

    @pytest.mark.parametrize(
        ("agent_override", "expected"),
        [(True, True), (False, False), (None, False)],
    )
    def test_no_bot_defaults_to_false(self, agent_override, expected):
        agent = _agent_with(self.CH, "only_mention", agent_override)
        assert _resolve_only_mention(None, agent, self.CH) is expected

    def test_no_bot_no_agent_is_false(self):
        assert _resolve_only_mention(None, None, self.CH) is False

    def test_agent_without_channel_config_inherits_bot(self):
        assert (
            _resolve_only_mention(_bot(only_mention=True), _agent(None), self.CH)
            is True
        )
        assert (
            _resolve_only_mention(_bot(only_mention=False), _agent(None), self.CH)
            is False
        )


# ---------------------------------------------------------------------------
# _is_private_conversation — per-platform detection table
# ---------------------------------------------------------------------------


class TestIsPrivateConversation:
    # Slack: DMs report channel_type == "im".
    def test_slack_im_is_private(self):
        msg = _inbound(ChannelType.SLACK, metadata={"channel_type": "im"})
        assert _is_private_conversation(msg) is True

    @pytest.mark.parametrize("channel_type", ["channel", "group", "mpim", None])
    def test_slack_non_im_is_not_private(self, channel_type):
        metadata = {"channel_type": channel_type} if channel_type else {}
        msg = _inbound(ChannelType.SLACK, metadata=metadata)
        assert _is_private_conversation(msg) is False

    # Teams: conversation_type == "personal" wins; otherwise a thread id
    # NOT starting with "19:" (channel ids) indicates a personal chat.
    def test_teams_personal_conversation_type_is_private(self):
        msg = _inbound(
            ChannelType.TEAMS, metadata={"conversation_type": "personal"}
        )
        assert _is_private_conversation(msg) is True

    def test_teams_group_chat_is_not_private(self):
        msg = _inbound(
            ChannelType.TEAMS,
            metadata={"conversation_type": "groupChat"},
            channel_thread_id="a:1personalthread",
        )
        assert _is_private_conversation(msg) is False

    def test_teams_missing_type_with_personal_thread_prefix_is_private(self):
        # Personal chat ids use the "a:" prefix — no "19:" channel marker.
        msg = _inbound(ChannelType.TEAMS, channel_thread_id="a:1abcdef")
        assert _is_private_conversation(msg) is True

    def test_teams_missing_type_with_channel_thread_prefix_is_not_private(self):
        msg = _inbound(
            ChannelType.TEAMS, channel_thread_id="19:meeting_thread@thread.v2"
        )
        assert _is_private_conversation(msg) is False

    def test_teams_missing_type_and_thread_is_not_private(self):
        # Note: the Teams branch short-circuits on the empty thread id and
        # returns a falsy value (not the literal ``False``) — assert on
        # truthiness, which is what every call site relies on.
        assert not _is_private_conversation(_inbound(ChannelType.TEAMS))

    # WhatsApp: chat_type == "private" is a DM; groups must stay public so
    # channel receive whitelists keep applying to them.
    def test_whatsapp_private_chat_is_private(self):
        msg = _inbound(ChannelType.WHATSAPP, metadata={"chat_type": "private"})
        assert _is_private_conversation(msg) is True

    def test_whatsapp_group_chat_is_not_private(self):
        msg = _inbound(ChannelType.WHATSAPP, metadata={"chat_type": "group"})
        assert _is_private_conversation(msg) is False

    # Telegram: chat_type == "private".
    def test_telegram_private_chat_is_private(self):
        msg = _inbound(ChannelType.TELEGRAM, metadata={"chat_type": "private"})
        assert _is_private_conversation(msg) is True

    @pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
    def test_telegram_group_chat_is_not_private(self, chat_type):
        msg = _inbound(ChannelType.TELEGRAM, metadata={"chat_type": chat_type})
        assert _is_private_conversation(msg) is False

    # Discord: metadata.is_dm flag (truthiness-coerced).
    def test_discord_dm_is_private(self):
        msg = _inbound(ChannelType.DISCORD, metadata={"is_dm": True})
        assert _is_private_conversation(msg) is True

    @pytest.mark.parametrize("is_dm", [False, None])
    def test_discord_guild_channel_is_not_private(self, is_dm):
        metadata = {"is_dm": is_dm} if is_dm is not None else {}
        msg = _inbound(ChannelType.DISCORD, metadata=metadata)
        assert _is_private_conversation(msg) is False

    # Google Chat: space_type == "DM".
    def test_google_chat_dm_space_is_private(self):
        msg = _inbound(ChannelType.GOOGLE_CHAT, metadata={"space_type": "DM"})
        assert _is_private_conversation(msg) is True

    @pytest.mark.parametrize("space_type", ["ROOM", "SPACE", None])
    def test_google_chat_room_space_is_not_private(self, space_type):
        metadata = {"space_type": space_type} if space_type else {}
        msg = _inbound(ChannelType.GOOGLE_CHAT, metadata=metadata)
        assert _is_private_conversation(msg) is False

    # Web: never private (web chat has its own routing).
    def test_web_is_never_private(self):
        msg = _inbound(ChannelType.WEB, metadata={"chat_type": "private"})
        assert _is_private_conversation(msg) is False

    def test_empty_metadata_is_not_private(self):
        for channel in (
            ChannelType.SLACK,
            ChannelType.TEAMS,
            ChannelType.WHATSAPP,
            ChannelType.TELEGRAM,
            ChannelType.DISCORD,
            ChannelType.GOOGLE_CHAT,
        ):
            # Truthiness assertion: the Teams branch returns a falsy ``""``
            # (not the literal ``False``) for empty metadata + thread id.
            assert not _is_private_conversation(_inbound(channel))


# ---------------------------------------------------------------------------
# _is_bot_mentioned — per-platform detection table
# ---------------------------------------------------------------------------


class TestIsBotMentioned:
    MENTION_CHANNELS = [
        ChannelType.TEAMS,
        ChannelType.SLACK,
        ChannelType.TELEGRAM,
        ChannelType.DISCORD,
        ChannelType.GOOGLE_CHAT,
    ]

    @pytest.mark.parametrize("channel", MENTION_CHANNELS)
    def test_mention_flag_true_detected(self, channel):
        msg = _inbound(channel, metadata={"bot_was_mentioned": True})
        assert _is_bot_mentioned(msg) is True

    @pytest.mark.parametrize("channel", MENTION_CHANNELS)
    def test_mention_flag_false_or_missing_not_detected(self, channel):
        assert (
            _is_bot_mentioned(_inbound(channel, metadata={"bot_was_mentioned": False}))
            is False
        )
        assert _is_bot_mentioned(_inbound(channel)) is False

    def test_whatsapp_is_always_false(self):
        # The WhatsApp Cloud API provides no structured mention entities —
        # even an explicit flag must not flip the detection.
        msg = _inbound(ChannelType.WHATSAPP, metadata={"bot_was_mentioned": True})
        assert _is_bot_mentioned(msg) is False

    def test_web_is_always_false(self):
        msg = _inbound(ChannelType.WEB, metadata={"bot_was_mentioned": True})
        assert _is_bot_mentioned(msg) is False

    @pytest.mark.parametrize("channel", MENTION_CHANNELS)
    def test_truthy_non_bool_flag_is_coerced(self, channel):
        # Adapters should set a bool, but the detector coerces defensively.
        msg = _inbound(channel, metadata={"bot_was_mentioned": 1})
        assert _is_bot_mentioned(msg) is True
