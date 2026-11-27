# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Message router – the core of the multi-channel architecture.

Receives an ``InboundMessage`` from any channel adapter, resolves the
Copilot user, finds or creates a conversation, runs the agent, stores
messages, and returns the response to be sent back.

Flow (web):
    InboundMessage → resolve_user → find_or_create_conversation →
    store_user_message → build_context → run_agent → store_assistant →
    OutboundMessage

Flow (external channels — Slack, Teams, WhatsApp, Telegram):
    InboundMessage → resolve_user → find_existing_conversation →
    build_context → run_agent → **if agent responds** →
    create_conversation (if new) → store_user_message →
    store_assistant → OutboundMessage

    For external channels, conversation creation and message storage are
    deferred until after the agent decides to respond.  Messages the
    agent ignores (join events, off-topic, ``[NO_RESPONSE]``, publish
    disabled) never create or pollute conversations.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import json
import logging
import time
import uuid
import zlib
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.helpers import get_user_group_ids, hidden_rows_excluded, user_visible_filter
from app.channels.base import (
    CHANNEL_TO_BOT_PROVIDER,
    FILE_ONLY_PLACEHOLDER,
    ChannelType,
    InboundFileAttachment,
    InboundMessage,
    OutboundMessage,
    log_id,
    teams_channel_component,
)
from app.core.database import async_session
from app.core.redaction import bind_known_secrets, known_secrets_scoped, redact_known_secrets_for_display
from app.core.redis import redis_client
from app.core.utils import format_exception_message, is_graceful_shutdown_cancellation, safe_uuid
from app.llm.base import ChatMessage
from app.models.activity import Activity
from app.models.agent import Agent
from app.models.channel_bot import ChannelBot
from app.models.chat import Conversation, Message
from app.models.user import User
from app.services.branding import get_cached_branding
from app.services.channel_notices import (
    chat_unavailable_notice,
    localized_reply,
    notice,
    notice_language,
)
from app.services.chat_messages import message_cursor_filter, message_order_desc, slice_message_page
from app.services.output_format import CONTENT_FORMAT_META_KEY, override_content_format
from app.services.prompt_guard import ScanResult, scan_user_input
from app.tools.base import files_for_reply

if TYPE_CHECKING:
    from app.services.channel_continuation import ChannelTurn
    from app.services.mention_dispatch import TurnMetering
    from app.tools.base import FileContext

logger = logging.getLogger(__name__)


NO_RESPONSE_MARKER = "[NO_RESPONSE]"

# Commands that trigger a conversation reset (case-insensitive).
_RESET_COMMANDS = frozenset({"/new", "/reset"})

# Commands that mute the bot in the current thread (case-insensitive).
_STOP_COMMANDS = frozenset({"/stop"})

# ---------------------------------------------------------------------------
# Message context helpers
# ---------------------------------------------------------------------------

# Maps raw platform-specific channel types to a normalised label.
_SLACK_CHANNEL_TYPES: dict[str, tuple[str, bool]] = {
    "im": ("direct_message", True),
    "channel": ("public_channel", False),
    "group": ("private_channel", False),
    "mpim": ("group_direct_message", False),
}

_TEAMS_CONVERSATION_TYPES: dict[str, tuple[str, bool]] = {
    "personal": ("direct_message", True),
    "groupChat": ("group_chat", False),
    "channel": ("channel", False),
}

_TELEGRAM_CHAT_TYPES: dict[str, tuple[str, bool]] = {
    "private": ("direct_message", True),
    "group": ("group_chat", False),
    "supergroup": ("group_chat", False),
    "channel": ("channel", False),
}

_WHATSAPP_CHAT_TYPES: dict[str, tuple[str, bool]] = {
    "private": ("direct_message", True),
    "group": ("group_chat", False),
}


def _build_message_context(inbound: InboundMessage) -> dict[str, object] | None:
    """Build a structured context dict from an inbound message.

    The dict is forwarded as-is to ``run_agent_chat(context=…)`` so
    the agent can inspect platform, channel type, DM vs group, and
    any adapter-specific metadata.

    Returns ``None`` for web messages (they don't go through the
    channel router).
    """
    if inbound.channel == ChannelType.WEB:
        return None

    platform = inbound.channel.value  # "slack", "teams", …
    channel_type = "unknown"
    is_dm = False
    platform_channel_type = ""

    if inbound.channel == ChannelType.SLACK:
        platform_channel_type = inbound.metadata.get(
            "channel_type",
            (inbound.raw or {}).get("channel_type", ""),
        )
        channel_type, is_dm = _SLACK_CHANNEL_TYPES.get(
            platform_channel_type,
            (f"unknown ({platform_channel_type})", False),
        )
    elif inbound.channel == ChannelType.TEAMS:
        conv = (inbound.raw or {}).get("conversation", {})
        platform_channel_type = inbound.metadata.get("conversation_type", "") or (
            conv.get("conversationType", "") if isinstance(conv, dict) else ""
        )
        channel_type, is_dm = _TEAMS_CONVERSATION_TYPES.get(
            platform_channel_type,
            (f"unknown ({platform_channel_type})", False),
        )
    elif inbound.channel == ChannelType.WHATSAPP:
        platform_channel_type = inbound.metadata.get("chat_type", "private")
        channel_type, is_dm = _WHATSAPP_CHAT_TYPES.get(
            platform_channel_type,
            ("direct_message", True),
        )
    elif inbound.channel == ChannelType.TELEGRAM:
        platform_channel_type = inbound.metadata.get("chat_type", "")
        channel_type, is_dm = _TELEGRAM_CHAT_TYPES.get(
            platform_channel_type,
            (f"unknown ({platform_channel_type})", False),
        )

    ctx: dict[str, object] = {
        "platform": platform,
        "channel_type": channel_type,
        "is_direct_message": is_dm,
    }
    if platform_channel_type:
        ctx["platform_channel_type"] = platform_channel_type
    # Platform-native conversation/thread id — lets the agent target the
    # CURRENT conversation with proactive channel tools (e.g. Teams
    # group-chat sends take the chat's conversation id as chat_id).
    if inbound.channel_thread_id:
        ctx["channel_thread_id"] = inbound.channel_thread_id

    # Merge adapter-supplied metadata (e.g. event_type, chat_title)
    # Avoid overwriting the keys we already set.
    _reserved = {
        "platform",
        "channel_type",
        "is_direct_message",
        "platform_channel_type",
        "channel_thread_id",
    }
    for key, val in (inbound.metadata or {}).items():
        if key not in _reserved and val not in (None, ""):
            ctx[key] = val

    return ctx


async def _load_channel_backfill(inbound: InboundMessage) -> str | None:
    """Load prior conversation context for an external channel.

    When the bot joins an existing conversation (e.g. Teams group chat or
    channel thread), the prior messages are fetched via the platform API
    so the agent understands what was being discussed.

    Supports:

    - **Teams groupChat**: Multi-party DMs.  Cached at bot-add time via
      ``conversationUpdate``; falls back to on-the-fly Graph API fetch.
    - **Teams channel**: Public/private Teams channel threads.  Fetches
      thread replies (or recent channel messages) via Graph API using
      ``ChannelMessage.Read.All``.
    - **Slack threads**: Public/private channel threads.  Fetches thread
      replies via the ``conversations.replies`` API.

    The result is injected into ``message_context["prior_conversation"]``.
    """
    if inbound.channel == ChannelType.TEAMS:
        conv_type = (inbound.metadata or {}).get("conversation_type", "")
        from app.channels.teams import (
            fetch_teams_channel_thread_context,
            fetch_teams_chat_context,
            load_teams_chat_backfill,
        )

        # --- groupChat: try cached backfill, then on-the-fly fetch ---
        if conv_type == "groupChat":
            backfill = await load_teams_chat_backfill(inbound.channel_thread_id or "")
            if backfill:
                logger.info(
                    "Loaded cached Teams chat backfill for %s (%d chars)",
                    _log_thread_id(inbound)[:60],
                    len(backfill),
                )
                return backfill

            if inbound.channel_thread_id:
                logger.info(
                    "No cached backfill — fetching Teams chat context "
                    "on-the-fly for groupChat %s",
                    _log_thread_id(inbound)[:60],
                )
                backfill = await fetch_teams_chat_context(inbound.channel_thread_id)
                if backfill:
                    logger.info(
                        "Fetched Teams chat context on-the-fly (%d chars)",
                        len(backfill),
                    )
                return backfill

        # --- channel: fetch thread replies or recent channel messages ---
        if conv_type == "channel":
            meta = inbound.metadata or {}
            team_id = meta.get("team_id", "")
            channel_id = meta.get("channel_id", "")
            reply_to_id = meta.get("reply_to_id") or None

            # If reply_to_id is missing, try to extract the thread root
            # from the Bot Framework conversation.id which includes
            # ";messageid=<root>" for thread replies.
            if not reply_to_id:
                thread_id = inbound.channel_thread_id or ""
                if ";messageid=" in thread_id:
                    reply_to_id = thread_id.split(";messageid=", 1)[1]
                    logger.info(
                        "Extracted reply_to_id=%s from channel_thread_id",
                        reply_to_id[:30],
                    )

            if team_id and channel_id:
                logger.info(
                    "Fetching Teams channel thread context — "
                    "team=%s, channel=%s, reply_to=%s",
                    team_id[:20],
                    channel_id[:20],
                    (reply_to_id or "(root)")[:20],
                )
                backfill = await fetch_teams_channel_thread_context(
                    team_id,
                    channel_id,
                    reply_to_id,
                )
                if backfill:
                    logger.info(
                        "Fetched Teams channel thread context (%d chars)",
                        len(backfill),
                    )
                return backfill

    if inbound.channel == ChannelType.SLACK:
        from app.channels.slack import fetch_slack_thread_context

        is_threaded = bool((inbound.metadata or {}).get("is_threaded_reply"))
        channel_type = (inbound.metadata or {}).get("channel_type", "")
        is_channel = channel_type in ("channel", "group")

        if is_threaded or is_channel:
            thread_id = inbound.channel_thread_id or ""
            parts = thread_id.split(":", 1) if ":" in thread_id else (thread_id, "")
            slack_channel_id = parts[0]
            thread_ts = parts[1] if len(parts) > 1 else ""

            if slack_channel_id and thread_ts:
                logger.info(
                    "Fetching Slack thread context — channel=%s, thread_ts=%s",
                    slack_channel_id[:20],
                    thread_ts[:20],
                )
                backfill = await fetch_slack_thread_context(
                    slack_channel_id,
                    thread_ts,
                )
                if backfill:
                    logger.info(
                        "Fetched Slack thread context (%d chars)",
                        len(backfill),
                    )
                return backfill

    return None


def _is_channel_thread_message(inbound: InboundMessage) -> bool:
    """Return True if the inbound message is part of a channel thread.

    Channel threads (Teams public/private channels, Slack public/group
    channel threads) contain messages from multiple participants that are
    NOT stored in the Copilot DB.  For these, we always refresh the
    thread context from the platform API so the agent sees the full
    conversation — not just its own prior messages.
    """
    if inbound.channel == ChannelType.TEAMS:
        conv_type = (inbound.metadata or {}).get("conversation_type", "")
        return conv_type == "channel"

    if inbound.channel == ChannelType.SLACK:
        channel_type = (inbound.metadata or {}).get("channel_type", "")
        is_threaded = bool((inbound.metadata or {}).get("is_threaded_reply"))
        return channel_type in ("channel", "group") or is_threaded

    return False


def _is_bare_channel_id(inbound: InboundMessage) -> bool:
    """Return True if the channel_thread_id represents the whole channel.

    For Teams, channel thread replies include ``;messageid=<root>`` in
    ``conversation.id``, making them thread-specific.  Root-level channel
    messages use the bare channel ID (no suffix), which is shared by ALL
    root-level messages in the channel.  Reusing a DB conversation for
    the bare channel ID causes stale history from unrelated interactions
    to pollute new conversations.

    For Slack, ``channel_thread_id`` normally includes a timestamp
    (``channel_id:thread_ts``), so each message gets a unique key.
    Falls back to ``True`` only if the timestamp part is missing.
    """
    if not _is_channel_thread_message(inbound):
        return False

    thread_id = inbound.channel_thread_id or ""

    if inbound.channel == ChannelType.TEAMS:
        return ";messageid=" not in thread_id

    if inbound.channel == ChannelType.SLACK:
        return ":" not in thread_id

    return False


def _log_thread_id(inbound: InboundMessage) -> str:
    """The thread id a log line may carry: see :func:`log_id`.

    A WhatsApp direct message is threaded by the sender's phone number.
    """
    return log_id(inbound.channel, inbound.channel_thread_id)


def _log_user_id(inbound: InboundMessage) -> str:
    """The sender id a log line may carry: see :func:`log_id`.

    A WhatsApp sender is identified by their phone number.
    """
    return log_id(inbound.channel, inbound.channel_user_id)


def _is_direct_message(inbound: InboundMessage) -> bool:
    """Return True if the inbound message is a direct/private message (DM).

    Used to determine whether a user's personal channel agent override
    should take priority over the default routed agent.

    Delegates to ``_is_private_conversation()`` which handles all
    platforms including WhatsApp (DMs vs groups).
    """
    return _is_private_conversation(inbound)


def _agent_can_publish(agent: Agent, channel: ChannelType) -> bool:
    """Return True if the agent is allowed to *publish* (reply) on *channel*.

    When ``publish_enabled`` is False for the channel in the agent's
    ``channel_config``, the agent should only monitor (receive + store)
    messages — not reply.
    """
    if channel == ChannelType.WEB:
        return True  # Web replies are always allowed

    ch_cfg = (agent.channel_config or {}).get(channel.value)
    if not ch_cfg:
        return True  # No config → legacy behaviour, allow replies
    return bool(ch_cfg.get("publish_enabled", True))


def _is_no_response(text: str) -> bool:
    """Return True if the LLM output is the no-response sentinel."""
    return text.strip() == NO_RESPONSE_MARKER


# ---------------------------------------------------------------------------
# Prompt injection scanning (external channels)
# ---------------------------------------------------------------------------

def _scan_inbound_message(
    inbound: InboundMessage, *, is_anonymous: bool
) -> ScanResult:
    """Scan an inbound external-channel message for prompt injection.

    Mirrors the authenticated-chat behaviour (``agent_chat`` /
    ``general_chat``): detections are logged as warnings. The caller decides
    whether to block (anonymous senders with high severity are blocked).
    """
    result = scan_user_input(inbound.text or "")
    if result.flagged:
        logger.warning(
            "Prompt injection detected on %s channel message "
            "(severity=%s, anonymous=%s, channel_thread_id=%s): %s",
            inbound.channel.value,
            result.severity,
            is_anonymous,
            _log_thread_id(inbound),
            result.reasons,
        )
    return result


#: Same preview budget the chat paths use. The OpenAEV AI red-team injector
#: matches its ``oaev<hash>`` canary marker inside this preview, so truncating
#: it shorter would silently break detection validation.
_INJECTION_PREVIEW_LENGTH = 500


async def _log_channel_injection_event(
    db: AsyncSession,
    *,
    inbound: InboundMessage,
    user: User | None,
    scan: ScanResult,
    blocked: bool,
) -> None:
    """Audit a prompt-injection detection on an external channel.

    Mirrors the ``security_alert`` events emitted by ``agent_chat`` /
    ``general_chat`` so every injection detection lands in one queryable place.

    ``log_security_event`` resolves the user itself and returns ``None`` for a
    non-identifiable sender, so anonymous-channel detections stay log-only -
    that is the existing platform contract (audit rows carry a real
    ``user_id``), not a gap introduced here. Because suppression only applies to
    anonymous senders, a persisted row therefore always carries
    ``blocked: False``; the field is recorded for parity with the chat paths and
    to carry the decision if enforcement is ever widened to known senders.

    The entry is committed here rather than left to the caller: ``log_audit``
    only flushes, and the remainder of ``handle_message`` runs a full agentic
    turn that can raise long before ``_finalize_message`` commits. Security
    evidence must not depend on the request succeeding.
    """
    if user is None:
        return
    from app.services.audit import log_security_event

    text = inbound.text or ""
    preview = (
        (text[:_INJECTION_PREVIEW_LENGTH] + "...")
        if len(text) > _INJECTION_PREVIEW_LENGTH
        else text
    )
    try:
        await log_security_event(
            db,
            user_id=str(user.id),
            action="security_alert",
            entity_type="security",
            entity_name=f"channel:{inbound.channel.value}",
            summary="Prompt injection detected",
            details={
                "source": "channel",
                "channel": inbound.channel.value,
                "severity": scan.severity,
                "reasons": scan.reasons,
                "channel_thread_id": inbound.channel_thread_id,
                "message_preview": preview,
                # Real enforcement decision: external channels suppress only
                # anonymous high-severity messages.
                "blocked": blocked,
                "action": "blocked" if blocked else "allowed",
            },
        )
        await db.commit()
    except Exception as exc:  # pragma: no cover - auditing must never break routing
        logger.warning(
            "Failed to audit channel prompt-injection detection: %s",
            format_exception_message(exc),
        )
        # A failed flush/commit leaves the session needing a rollback, which
        # would turn every later query in this turn into a PendingRollbackError.
        # Roll back so a broken audit write really cannot break routing.
        try:
            await db.rollback()
        except Exception:
            logger.warning("Rollback after failed injection audit also failed")


# ---------------------------------------------------------------------------
# Anonymous access helpers
# ---------------------------------------------------------------------------


async def _get_channel_bot(db: AsyncSession, channel: ChannelType) -> ChannelBot | None:
    """Return the connected ChannelBot for *channel*, or None."""
    provider = CHANNEL_TO_BOT_PROVIDER.get(channel.value)
    if not provider:
        return None
    result = await db.execute(
        select(ChannelBot)
        .where(
            ChannelBot.provider == provider,
            ChannelBot.status == "connected",
        )
        .order_by(ChannelBot.is_company_managed.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _is_private_conversation(inbound: InboundMessage) -> bool:
    """Return True if the inbound message is a private/DM conversation.

    Platform detection:
      - **Slack**: ``channel_type == "im"`` (direct message with the bot)
      - **Teams**: ``conversation_type == "personal"`` or conversation ID
        doesn't start with ``"19:"`` (personal chat IDs use ``"a:"`` prefix)
      - **Telegram**: ``chat_type == "private"``
      - **WhatsApp**: ``chat_type == "private"`` (DM).  Group messages
        have ``chat_type == "group"`` and return False so that channel
        restrictions (receive whitelists) apply to groups.
      - **Web**: always False (web chat has its own routing)
    """
    meta = inbound.metadata or {}

    if inbound.channel == ChannelType.SLACK:
        return meta.get("channel_type") == "im"

    if inbound.channel == ChannelType.TEAMS:
        conv_type = meta.get("conversation_type", "")
        thread_id = inbound.channel_thread_id or ""
        return conv_type == "personal" or (
            not conv_type and thread_id and not thread_id.startswith("19:")
        )

    if inbound.channel == ChannelType.WHATSAPP:
        return meta.get("chat_type") == "private"

    if inbound.channel == ChannelType.TELEGRAM:
        return meta.get("chat_type") == "private"

    if inbound.channel == ChannelType.DISCORD:
        return bool(meta.get("is_dm"))

    if inbound.channel == ChannelType.GOOGLE_CHAT:
        return meta.get("space_type") == "DM"

    # Web: not applicable
    return False


def _resolve_allow_private(
    channel_bot: ChannelBot | None,
    agent: Agent | None,
    channel: ChannelType,
) -> bool:
    """Resolve allow_private with bot-level enforcement.

    Rules:
      - Bot ``allow_private`` = False → always False (cannot be overridden).
      - Bot ``allow_private`` = True → agent may set True or False.
      - No bot → True (default).
    """
    if channel_bot and not channel_bot.allow_private:
        return False

    if agent and agent.channel_config:
        ch_cfg = agent.channel_config.get(channel.value)
        if ch_cfg:
            agent_priv = ch_cfg.get("allow_private")
            if agent_priv is not None:
                return bool(agent_priv)

    if channel_bot:
        return channel_bot.allow_private

    return True


def _resolve_only_mention(
    channel_bot: ChannelBot | None,
    agent: Agent | None,
    channel: ChannelType,
) -> bool:
    """Resolve only_mention with bot-level enforcement.

    Rules:
      - Bot ``only_mention`` = True → always True (cannot be disabled).
      - Bot ``only_mention`` = False → agent may set True or False.
      - No bot → False (default).
    """
    if channel_bot and channel_bot.only_mention:
        return True

    if agent and agent.channel_config:
        ch_cfg = agent.channel_config.get(channel.value)
        if ch_cfg:
            agent_val = ch_cfg.get("only_mention")
            if agent_val is not None:
                return bool(agent_val)

    if channel_bot:
        return channel_bot.only_mention

    return False


def _is_bot_mentioned(inbound: InboundMessage) -> bool:
    """Return True if the bot was explicitly @mentioned in the message.

    Platform detection:
      - **Teams**: ``metadata.bot_was_mentioned`` flag set by the adapter
        (from Bot Framework ``entities`` array with type ``mention``).
      - **Slack**: ``metadata.bot_was_mentioned`` flag set by the adapter
        (detects ``<@BOT_ID>`` in message text or ``app_mention`` event
        type).  Supports both ``app_mention`` event subscriptions and
        ``message.channels`` subscriptions where the bot is @mentioned.
      - **Telegram**: ``metadata.bot_was_mentioned`` flag set by the adapter
        (from Telegram message entities with type ``mention``).
      - **WhatsApp**: always False — WhatsApp groups support @mentions in
        the message text, but the Cloud API does not provide structured
        mention entities.  If ``only_mention`` is needed for WhatsApp
        groups, disable it per-channel or per-agent.
      - **Web**: always False (not applicable).
    """
    meta = inbound.metadata or {}

    if inbound.channel == ChannelType.TEAMS:
        return bool(meta.get("bot_was_mentioned", False))

    if inbound.channel == ChannelType.SLACK:
        # The Slack adapter sets bot_was_mentioned=True for both
        # app_mention events AND <@BOT_ID> detected in message text.
        return bool(meta.get("bot_was_mentioned", False))

    if inbound.channel == ChannelType.TELEGRAM:
        return bool(meta.get("bot_was_mentioned", False))

    if inbound.channel == ChannelType.DISCORD:
        return bool(meta.get("bot_was_mentioned", False))

    if inbound.channel == ChannelType.GOOGLE_CHAT:
        return bool(meta.get("bot_was_mentioned", False))

    # WhatsApp and Web: no structured mention mechanism
    return False


# ---------------------------------------------------------------------------
# Thread continuation tracking (only-mention with smart follow-up)
# ---------------------------------------------------------------------------
# When only_mention is enabled, the bot normally ignores messages without an
# explicit @mention.  Thread continuation extends this: once the bot has been
# mentioned and responded in a specific thread, it keeps monitoring that
# thread so it can follow up when relevant — even without further @mentions.
#
# Supported platforms (those with true threading):
#   Slack    — channel_thread_id (already thread-specific via thread_ts)
#   Teams    — channel_thread_id with ;messageid= suffix (thread-specific)
#   WhatsApp — group_id for group chats (all group messages share the
#              same thread key, enabling continuation after first response)
#
# NOT supported:
#   Telegram — channel_thread_id is just the chat_id (entire group), so
#              marking it "active" would monitor ALL messages. Too broad.
# ---------------------------------------------------------------------------

_THREAD_ACTIVE_TTL = 86_400  # 24 hours, refreshed on each bot response


def _get_thread_key(inbound: InboundMessage) -> str | None:
    """Derive a thread-level key from the inbound message.

    Returns a string that uniquely identifies the thread within its channel,
    or ``None`` if thread tracking is not applicable.

    For **Slack**, ``channel_thread_id`` already encodes
    ``channel_id:thread_ts``, which is thread-specific — all messages in
    a Slack thread share the same ``thread_ts``.

    For **Teams**, ``channel_thread_id`` is the Bot Framework
    ``conversation.id``.  In channel threads this already includes a
    ``;messageid=<root>`` suffix that is shared by every message in the
    same thread, so it serves as a reliable thread key.  We only
    activate tracking when the suffix is present to avoid marking the
    entire channel as active for rare root-level messages.

    For **WhatsApp** groups, the ``group_id`` (stored in metadata) serves
    as the thread key — all messages in a group share it.  DMs return
    ``None`` (no thread tracking needed for 1:1 chats).

    For **Telegram**, returns ``None`` — ``channel_thread_id`` is just the
    chat_id (entire group), which is too broad for continuation tracking.
    """
    if inbound.channel == ChannelType.SLACK:
        return inbound.channel_thread_id

    if inbound.channel == ChannelType.TEAMS:
        thread_id = inbound.channel_thread_id or ""
        # Channel threads include ";messageid=<root>" — use the full ID.
        # Plain channel IDs (no ";messageid=") represent the whole channel,
        # which is too broad for thread tracking — return None for those.
        if ";messageid=" in thread_id:
            return thread_id
        # Group chats (multi-party private chats) have a stable, chat-scoped
        # conversation ID and no thread structure — the whole chat is the
        # "thread", mirroring WhatsApp groups keyed on group_id.  Enables
        # thread continuation and /stop mute in Teams group chats.
        if (inbound.metadata or {}).get("conversation_type") == "groupChat":
            return thread_id or None
        return None

    if inbound.channel == ChannelType.WHATSAPP:
        # Group messages: use group_id as thread key for continuation.
        # DMs: no thread tracking needed (all messages are 1:1).
        group_id = (inbound.metadata or {}).get("group_id")
        return group_id if group_id else None

    if inbound.channel == ChannelType.GOOGLE_CHAT:
        # Google Chat has real threads — the adapter forwards the thread
        # resource name ("spaces/AAA/threads/BBB") in metadata.  Any event
        # carrying a thread name (DMs included) is tracked by it; None only
        # when the event has no thread.
        return (inbound.metadata or {}).get("thread_name") or None

    # Telegram, Discord, Web — no thread-level tracking (their thread IDs
    # would cover the entire chat/channel, which is too broad).
    return None


async def _mark_thread_active(channel: ChannelType, thread_key: str) -> None:
    """Mark a thread as active (the bot has responded here).

    Sets a Redis key with a TTL that is refreshed every time the bot
    sends a response in the thread.
    """
    try:
        redis = await redis_client()
        key = f"active_thread:{channel.value}:{thread_key}"
        await redis.set(key, "1", ex=_THREAD_ACTIVE_TTL)
        logger.info(
            "Thread marked active: %s (key=%s, ttl=%ds)",
            channel.value,
            f"active_thread:{channel.value}:{log_id(channel, thread_key)}",
            _THREAD_ACTIVE_TTL,
        )
    except Exception as exc:
        logger.warning(
            "Failed to mark thread active (channel=%s, thread=%s): %s",
            channel.value,
            log_id(channel, thread_key),
            format_exception_message(exc),
        )


async def _is_thread_active(channel: ChannelType, thread_key: str) -> bool:
    """Return True if the thread was previously activated by a bot response."""
    try:
        redis = await redis_client()
        key = f"active_thread:{channel.value}:{thread_key}"
        active = await redis.exists(key) > 0
        logger.info(
            "Thread active check: %s (key=%s, active=%s)",
            channel.value,
            f"active_thread:{channel.value}:{log_id(channel, thread_key)}",
            active,
        )
        return active
    except Exception as exc:
        logger.warning(
            "Failed to check thread active status (channel=%s, thread=%s): %s",
            channel.value,
            log_id(channel, thread_key),
            format_exception_message(exc),
        )
        return False


_MUTED_THREAD_TTL = 7 * 86_400  # 7 days — mute persists until re-mention


async def _mute_thread(channel: ChannelType, thread_key: str) -> None:
    """Mute a thread so the bot stops responding to follow-up messages.

    Deletes the ``active_thread`` key and sets a ``muted_thread`` key.
    The mute can be lifted by explicitly @mentioning the bot again.
    """
    try:
        redis = await redis_client()
        active_key = f"active_thread:{channel.value}:{thread_key}"
        muted_key = f"muted_thread:{channel.value}:{thread_key}"
        await redis.delete(active_key)
        await redis.set(muted_key, "1", ex=_MUTED_THREAD_TTL)
        logger.info(
            "Thread muted: %s (key=%s, ttl=%ds)",
            channel.value,
            f"muted_thread:{channel.value}:{log_id(channel, thread_key)}",
            _MUTED_THREAD_TTL,
        )
    except Exception as exc:
        logger.warning(
            "Failed to mute thread (channel=%s, thread=%s): %s",
            channel.value,
            log_id(channel, thread_key),
            format_exception_message(exc),
        )


async def _is_thread_muted(channel: ChannelType, thread_key: str) -> bool:
    """Return True if the thread has been muted via /stop."""
    try:
        redis = await redis_client()
        key = f"muted_thread:{channel.value}:{thread_key}"
        return await redis.exists(key) > 0
    except Exception as exc:
        logger.warning(
            "Failed to check muted thread status (channel=%s, thread=%s): %s",
            channel.value,
            log_id(channel, thread_key),
            format_exception_message(exc),
        )
        return False


async def _unmute_thread(channel: ChannelType, thread_key: str) -> None:
    """Remove the muted flag so the bot resumes responding in the thread."""
    try:
        redis = await redis_client()
        key = f"muted_thread:{channel.value}:{thread_key}"
        await redis.delete(key)
        logger.info("Thread unmuted: %s (thread_key=%s)", channel.value, log_id(channel, thread_key))
    except Exception as exc:
        logger.warning(
            "Failed to unmute thread (channel=%s, thread=%s): %s",
            channel.value,
            log_id(channel, thread_key),
            format_exception_message(exc),
        )


def _is_stop_command(text: str) -> bool:
    """Return True if *text* is a thread-mute command."""
    return text.strip().lower() in _STOP_COMMANDS


def _bot_allows_receive(
    channel_bot: ChannelBot | None,
    inbound: InboundMessage,
) -> bool:
    """Check if the channel bot's receive whitelist allows this message.

    Returns True (allow) when:
      - No channel bot found (legacy / unconfigured)
      - Bot has no receive whitelist (NULL or empty → all channels allowed)
      - The message is a private/DM conversation and bot's allow_private is True
      - The incoming channel ID is present in the bot's receive whitelist

    Returns False (reject) when the bot has a non-empty whitelist and the
    incoming channel ID is not in it (and the message is not a private
    conversation that's allowed through).

    Note: even when this returns False, the message may still be processed
    if an agent has ``bypass_whitelist`` enabled for this channel type.
    See ``_any_agent_bypasses_whitelist()``.
    """
    if not channel_bot:
        return True
    if not channel_bot.allowed_receive_channels:
        return True  # NULL or empty → all channels allowed

    # Private conversations bypass channel restrictions when allow_private is True
    if _is_private_conversation(inbound) and channel_bot.allow_private:
        return True

    incoming_id = _extract_channel_id(inbound)
    if not incoming_id:
        return True  # Cannot determine channel ID → allow (safe fallback)

    allowed_ids = {
        ch.get("id") for ch in channel_bot.allowed_receive_channels if ch.get("id")
    }
    return _channel_id_matches(inbound.channel, incoming_id, allowed_ids)


async def _any_agent_bypasses_whitelist(
    db: AsyncSession,
    channel: ChannelType,
) -> bool:
    """Return True if any enabled agent has ``bypass_whitelist`` for *channel*.

    Used to override the bot-level receive whitelist gate: if at least one
    agent is configured to bypass, the message is allowed through and
    per-agent filtering in ``_agent_allows_channel`` takes over.
    """
    ch_value = channel.value
    result = await db.execute(
        select(Agent.channel_config).where(
            Agent.enabled.is_(True),
            hidden_rows_excluded(Agent),
            Agent.channel_config.isnot(None),
            Agent.channel_config[ch_value].isnot(None),
        )
    )
    for (config,) in result.all():
        ch_cfg = (config or {}).get(ch_value, {})
        if ch_cfg.get("bypass_whitelist") and ch_cfg.get("receive_enabled"):
            return True
    return False


async def _bot_receives(
    db: AsyncSession, channel_bot: ChannelBot | None, inbound: InboundMessage
) -> bool | None:
    """Step 0 of :func:`handle_message`, the bot's receive whitelist.

    ``True``: the message passes. ``False``: it is off the whitelist, let
    through only because an agent bypasses it. ``None``: it is dropped.
    """
    if _bot_allows_receive(channel_bot, inbound):
        return True
    if await _any_agent_bypasses_whitelist(db, inbound.channel):
        return False
    return None


def _only_mention_gates(channel_bot: ChannelBot | None, inbound: InboundMessage) -> bool:
    """Step 0b: the bot answers *inbound* only if it continues a thread it is in.

    Never a direct message: a private conversation is always for the bot.
    """
    return bool(
        channel_bot
        and channel_bot.only_mention
        and not _is_private_conversation(inbound)
        and not _is_bot_mentioned(inbound)
    )


def _resolve_allow_anonymous(
    channel_bot: ChannelBot | None,
    agent: Agent | None,
    channel: ChannelType,
) -> bool:
    """Resolve allow_anonymous with bot-level enforcement.

    Rules:
      - Bot ``allow_anonymous`` = False → always False (cannot be overridden).
      - Bot ``allow_anonymous`` = True → agent may set True or False.
      - No bot → False.
    """
    if channel_bot and not channel_bot.allow_anonymous:
        return False

    if agent and agent.channel_config:
        ch_cfg = agent.channel_config.get(channel.value)
        if ch_cfg:
            agent_anon = ch_cfg.get("allow_anonymous")
            if agent_anon is not None:
                return bool(agent_anon)

    if channel_bot:
        return channel_bot.allow_anonymous

    return False


async def _resolve_channel_agent_anonymous(
    db: AsyncSession,
    channel: ChannelType,
    inbound: InboundMessage,
    channel_bot: ChannelBot | None = None,
) -> Agent | None:
    """Find an agent for an anonymous (unauthenticated) channel message.

    Checks all enabled agents with ``receive_enabled`` for the channel,
    filtering by the two-tier ``allow_anonymous`` resolution (agents that
    explicitly set ``allow_anonymous: false`` are skipped).

    Prefers company-managed agents (sorted first in the query) since they
    are visible to everyone by design, but considers all enabled agents.
    The admin explicitly enabling ``allow_anonymous`` on the channel bot
    is treated as sufficient authorisation to route to any matching agent.
    """
    ch_value = channel.value

    # Single query: all enabled agents with this channel configured,
    # ordered by company-managed first (preferred for anonymous routing).
    result = await db.execute(
        select(Agent)
        .where(
            Agent.enabled.is_(True),
            hidden_rows_excluded(Agent),
            Agent.channel_config.isnot(None),
            Agent.channel_config[ch_value].isnot(None),
        )
        .order_by(Agent.is_company_managed.desc())
    )
    agents = result.scalars().all()

    declined: list[str] = []
    for agent in agents:
        ch_cfg = (agent.channel_config or {}).get(ch_value)
        if not ch_cfg or not ch_cfg.get("receive_enabled"):
            continue
        # Skip agents that don't allow anonymous for this channel
        if not _resolve_allow_anonymous(channel_bot, agent, channel):
            declined.append(f"'{agent.name}' ({agent.id}): allow_anonymous=false")
            continue
        allowed, reason = _agent_allows_channel_reason(
            agent, channel, inbound, channel_bot
        )
        if allowed:
            if not agent.is_company_managed:
                logger.info(
                    "Routed anonymous %s message to non-company-managed agent '%s' (%s)",
                    channel.value,
                    agent.name,
                    agent.id,
                )
            return agent
        declined.append(f"'{agent.name}' ({agent.id}): {reason}")

    logger.debug(
        "No agent took the anonymous %s message: %s (channel_thread_id=%s)",
        channel.value,
        "; ".join(declined) if declined else f"no agent receives {ch_value} messages",
        _log_thread_id(inbound),
    )
    return None


@asynccontextmanager
async def _conversation_create_lock(
    channel: ChannelType,
    channel_thread_id: str | None,
    user_id: uuid.UUID | None,
):
    """Serialize conversation find-or-create per (channel, thread, user).

    Two concurrent messages in a brand-new thread each run their own
    check-then-insert (in separate ``spawn_channel_task`` sessions) and there
    is no unique constraint on ``(user_id, channel, channel_thread_id)`` —
    without serialization both create a conversation and the thread is split
    (or worse, a later strict lookup trips on the duplicates). A short Redis
    ``SET NX`` lock closes the race; on any Redis error the lock degrades to
    a no-op (same behaviour as before this guard).
    """
    if not channel_thread_id:
        yield
        return
    key = f"conv_create_lock:{channel.value}:{channel_thread_id}:{user_id or 'anon'}"
    # Random holder token: if this holder outlives the 10 s TTL and another
    # worker acquires the expired key, an unconditional DELETE on release
    # would remove the NEW holder's lock and reopen the race the lock exists
    # to close. Release only deletes the key when it still holds it.
    token = uuid.uuid4().hex
    redis = None
    acquired = False
    try:
        redis = await redis_client()
        for _ in range(50):  # up to ~5s
            acquired = bool(await redis.set(key, token, nx=True, ex=10))
            if acquired:
                break
            await asyncio.sleep(0.1)
        if not acquired:
            logger.warning(
                "Conversation-create lock not acquired after 5s for %s — "
                "proceeding without serialization",
                f"conv_create_lock:{channel.value}:{log_id(channel, channel_thread_id)}:{user_id or 'anon'}",
            )
    except Exception:
        logger.debug("Conversation-create lock unavailable", exc_info=True)
    try:
        yield
    finally:
        if redis is not None and acquired:
            try:
                # Compare-and-delete so an expired lock never deletes the
                # next holder's key (GET+DEL race window is acceptable here:
                # the tolerant duplicate lookup bounds the worst case).
                if await redis.get(key) == token:
                    await redis.delete(key)
            except Exception:
                logger.debug("Conversation-create lock release failed", exc_info=True)


async def _find_or_create_anonymous_conversation(
    db: AsyncSession,
    channel: ChannelType,
    channel_thread_id: str | None = None,
    agent_id: uuid.UUID | None = None,
) -> Conversation:
    """Find or create a conversation for an anonymous user (no Copilot account).

    Matches by channel + channel_thread_id with NULL user_id.
    """
    async with _conversation_create_lock(channel, channel_thread_id, None):
        if channel_thread_id:
            result = await db.execute(
                select(Conversation)
                .where(
                    Conversation.user_id.is_(None),
                    Conversation.channel == channel.value,
                    Conversation.channel_thread_id == channel_thread_id,
                    Conversation.is_archived.is_(False),
                )
                # Deterministic pick if historical duplicates exist — a strict
                # scalar_one_or_none() would raise MultipleResultsFound and
                # permanently break routing for the thread.
                .order_by(Conversation.created_at)
                .limit(1)
            )
            existing = result.scalars().first()
            if existing:
                return existing

        conv = Conversation(
            user_id=None,
            channel=channel.value,
            channel_thread_id=channel_thread_id,
            agent_id=agent_id,
        )
        db.add(conv)
        await db.flush()
        return conv


async def resolve_user_by_email(db: AsyncSession, email: str) -> User | None:
    """Look up a Copilot user by email, whatever its case.

    A platform reports the address as its profile holds it; accounts are stored
    lowercased and ``ix_users_email_lower`` allows one row per address.
    """
    result = await db.execute(
        select(User).where(
            func.lower(User.email) == email.strip().lower(),
            User.is_active.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def resolve_user_by_channel_identity(
    db: AsyncSession,
    channel_type: str,
    channel_user_id: str,
) -> User | None:
    """Look up a Copilot user by their linked channel identity.

    Searches the ``channel_identities`` JSON column for a matching
    ``{channel_type}:{channel_user_id}`` key.  This enables cross-channel
    user recognition when email-based lookup isn't available (e.g. WhatsApp
    phone number, Telegram user ID, Discord user ID).
    """
    if not channel_user_id:
        return None
    identity_key = f"{channel_type}:{channel_user_id}"
    result = await db.execute(
        select(User)
        .where(
            User.is_active.is_(True),
            User.channel_identities.op("?")(identity_key),
        )
        .order_by(User.created_at)
    )
    rows = result.scalars().all()
    if len(rows) > 1:
        logger.warning(
            "Multiple users (%d) found for channel identity %s — using earliest account",
            len(rows),
            f"{channel_type}:{log_id(channel_type, channel_user_id)}",
        )
    return rows[0] if rows else None


async def link_channel_identity(
    db: AsyncSession,
    user: User,
    channel_type: str,
    channel_user_id: str,
) -> None:
    """Link a channel identity to a user for future cross-channel resolution.

    Called automatically when a user is first resolved by email on an
    external channel, so subsequent messages can be matched even without
    email.  Idempotent — linking an already-linked identity is a no-op.
    """
    if not channel_user_id:
        return
    identity_key = f"{channel_type}:{channel_user_id}"
    current = user.channel_identities or {}
    if identity_key not in current:
        # Create a new dict so SQLAlchemy detects the JSON column change
        # (in-place mutation of the same object is not tracked by the JSON type)
        user.channel_identities = {**current, identity_key: True}
        await db.flush()


async def find_or_create_conversation(
    db: AsyncSession,
    user_id: uuid.UUID,
    channel: ChannelType,
    channel_thread_id: str | None = None,
    agent_id: uuid.UUID | None = None,
) -> Conversation:
    """Find an existing conversation or create a new one.

    For platform channels (slack, teams, whatsapp), we match on channel_thread_id
    to continue existing threads. For web, each call can create a new one or
    supply a conversation_id to continue.
    """
    async with _conversation_create_lock(channel, channel_thread_id, user_id):
        if channel_thread_id:
            result = await db.execute(
                select(Conversation)
                .where(
                    Conversation.user_id == user_id,
                    Conversation.channel == channel.value,
                    Conversation.channel_thread_id == channel_thread_id,
                    Conversation.is_archived.is_(False),
                )
                # Deterministic pick if historical duplicates exist — a strict
                # scalar_one_or_none() would raise MultipleResultsFound and
                # permanently break routing for the thread.
                .order_by(Conversation.created_at)
                .limit(1)
            )
            existing = result.scalars().first()
            if existing:
                return existing

        conv = Conversation(
            user_id=user_id,
            channel=channel.value,
            channel_thread_id=channel_thread_id,
            agent_id=agent_id,
        )
        db.add(conv)
        await db.flush()
        return conv


async def get_conversation_history(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    limit: int = 50,
    before_sequence: int | None = None,
) -> list[ChatMessage]:
    """Load the *most recent* messages from a conversation as ChatMessage objects.

    Uses a DESC-then-reverse pattern so the caller always receives the
    newest ``limit`` messages in chronological (ascending) order. With
    *before_sequence*, only messages stored before that sequence count: the
    window a turn reads when it loads its history after its own message was
    committed, so what later sends stored never takes a place in it.

    **File attachment rehydration**: when a past user message had files
    attached (tracked via ``metadata_json.attachments`` + ``file_attachments``
    rows), the corresponding :class:`FileRef` objects are rebuilt and
    attached to the returned ``ChatMessage.files`` field.  This ensures
    that LLM providers can re-inline the file contents on follow-up turns
    (e.g. "did you read the CSV I sent earlier?") instead of the model
    losing track of the attachment.

    **Compaction rehydration**: when the conversation has a
    persisted compaction state (``conversations.compaction_state_json``),
    only messages NEWER than the compaction's sequence watermark are
    loaded and the rendered compaction system message (Pinned Directives +
    Rolling Summary) is prepended.  This is what makes conversation
    compaction incremental across turns: the summarizer's output survives
    the request instead of being recomputed from scratch on every message
    past the threshold.  Each returned message carries its DB sequence in
    ``ChatMessage.db_sequence`` so the compactor can persist the next
    watermark.
    """
    from app.services.conversation_memory import (
        clear_compaction_state,
        compaction_state_is_stale,
        parse_compaction_state,
        render_stored_compaction,
    )

    compaction_state = None
    # Who answers now, and the agent a deletion took from it: the
    # earlier turns another agent wrote are read back as that agent's.
    answering_agent_id: uuid.UUID | None = None
    deleted_agent_id: uuid.UUID | None = None
    try:
        _conv_row = (
            await db.execute(
                select(
                    Conversation.compaction_state_json,
                    Conversation.agent_id,
                    Conversation.deleted_agent_id,
                ).where(Conversation.id == conversation_id)
            )
        ).first()
        _state_raw = None
        if _conv_row is not None:
            _state_raw, answering_agent_id, deleted_agent_id = _conv_row
        compaction_state = parse_compaction_state(_state_raw)
    except Exception:
        logger.warning(
            "Failed to load compaction state for conversation %s — "
            "falling back to full history load",
            conversation_id,
            exc_info=True,
        )

    # ── Compaction-state hygiene (P4.3) ──────────────
    # When messages covered by (or up to) the watermark were deleted
    # (message purge / thread trim), the ``sequence > watermark`` filter
    # below would hide every message forever (store_message restarts the
    # sequence from the new max).  Detect the stale state, clear it
    # (best-effort, own session) and fall back to the full-history load.
    if compaction_state is not None:
        try:
            _max_seq = await db.scalar(
                select(func.max(Message.sequence)).where(
                    Message.conversation_id == conversation_id
                )
            )
        except Exception:
            logger.warning(
                "Failed to validate compaction watermark for conversation "
                "%s — keeping the stored state",
                conversation_id,
                exc_info=True,
            )
        else:
            if compaction_state_is_stale(compaction_state, _max_seq):
                logger.warning(
                    "Compaction state for conversation %s is stale "
                    "(watermark %s > max message sequence %s — messages "
                    "were purged) — clearing it and reloading the full "
                    "history",
                    conversation_id,
                    compaction_state.get("through_sequence"),
                    _max_seq,
                )
                await clear_compaction_state(
                    str(conversation_id),
                    observed_through_sequence=compaction_state.get("through_sequence"),
                )
                compaction_state = None

    message_filters = [Message.conversation_id == conversation_id]
    if compaction_state is not None:
        message_filters.append(
            Message.sequence > compaction_state["through_sequence"]
        )
    if before_sequence is not None:
        message_filters.append(Message.sequence < before_sequence)

    # The web chat's history page: newest first on
    # ``(sequence, created_at, id)`` - legacy rows share ``sequence=0`` and
    # a sequence may repeat, so ``sequence`` alone would leave the window
    # and its order to heap order - then reversed to chronological. The one
    # sentinel row past ``limit`` only says whether older history remains.
    from app.core.hot_imports import ensure_hot_path_imports

    # Importing the web chat registers how a channel turn's user message is
    # relayed to its web viewers (``chat_turn_stream.set_user_message_serializer``),
    # which a process without the web routes (the worker) has no other way to get.
    await ensure_hot_path_imports("app.api.v1.chat")

    result = await db.execute(
        select(Message)
        .where(*message_filters)
        .order_by(*message_order_desc())
        .limit(limit + 1)
    )
    messages, has_more = slice_message_page(list(result.scalars().all()), limit)

    # Stale-watermark guard: if compaction persistence failed for
    # many turns (or a burst of channel messages landed), MORE than ``limit``
    # messages can sit after the watermark.  The newest-``limit`` window is
    # then identical with or without the sequence filter (legacy behaviour
    # also dropped the overflow), and the prepended summary still only adds
    # information -- but messages between the watermark and the window start
    # are neither summarised nor loaded verbatim.  Surface the gap loudly
    # instead of letting the context silently diverge; the next successful
    # compaction cycle advances the watermark past the gap.
    if compaction_state is not None and has_more and messages:
        try:
            _gap_count = await db.scalar(
                select(func.count())
                .select_from(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.sequence > compaction_state["through_sequence"],
                    # Older than the window's first row on the same
                    # (sequence, created_at, id) order, ties included.
                    message_cursor_filter(messages[0]),
                )
            )
        except Exception:
            _gap_count = None
        if _gap_count:
            logger.warning(
                "Conversation %s has a stale compaction watermark: %d "
                "message(s) between sequence %d and the loaded window "
                "(starting at %d) are neither covered by the stored summary "
                "nor loaded verbatim -- the LLM context has a gap until the "
                "next compaction cycle advances the watermark",
                conversation_id,
                _gap_count,
                compaction_state["through_sequence"],
                messages[0].sequence,
            )

    # ── Batch-load FileAttachment rows referenced by past messages ────
    # Collect all file_ids from metadata_json.attachments[*].file_id and
    # resolve them in one query.  Falls back silently on malformed JSON.
    file_ids_by_message: dict[uuid.UUID, list[str]] = {}
    all_file_ids: set[str] = set()
    # Replies of a multi-agent turn carry the agent that wrote them:
    # they are read back labelled, so no agent takes another's words as its
    # own, and a note written in place of an agent reads as a note.
    from app.services.mention_dispatch import history_label

    history_labels: dict[uuid.UUID, str] = {}
    metas: dict[uuid.UUID, dict] = {}
    for msg in messages:
        if not msg.metadata_json:
            continue
        try:
            meta = json.loads(msg.metadata_json)
        except (TypeError, ValueError):
            logger.debug("Skipping message %s with invalid metadata JSON", msg.id)
            continue
        if not isinstance(meta, dict):
            continue
        metas[msg.id] = meta
        if msg.role == "assistant":
            _labelled = history_label(meta, msg.content or "")
            if _labelled is not None:
                history_labels[msg.id] = _labelled
        attachments = meta.get("attachments")
        if not isinstance(attachments, list):
            continue
        for att in attachments:
            if not isinstance(att, dict):
                continue
            fid = att.get("file_id")
            if isinstance(fid, str) and fid:
                file_ids_by_message.setdefault(msg.id, []).append(fid)
                all_file_ids.add(fid)

    from app.services.conversation_continuity import history_labels as _other_agents

    history_labels.update(
        await _other_agents(
            db,
            messages,
            metas,
            answering_agent_id=answering_agent_id,
            deleted_agent_id=deleted_agent_id,
        )
    )

    file_refs_by_id: dict[str, object] = {}
    if all_file_ids:
        from app.llm.base import FileRef
        from app.models.file_attachment import FileAttachment

        try:
            uuid_set = [uuid.UUID(fid) for fid in all_file_ids]
        except Exception:
            uuid_set = []
        if uuid_set:
            fa_result = await db.execute(
                select(FileAttachment).where(FileAttachment.id.in_(uuid_set))
            )
            for fa in fa_result.scalars().all():
                file_refs_by_id[str(fa.id)] = FileRef.from_attachment(fa)

    chat_messages: list[ChatMessage] = []
    for msg in messages:
        tool_calls = None
        if msg.tool_calls_json:
            try:
                from app.tools.base import ToolCall

                raw = json.loads(msg.tool_calls_json)
                tool_calls = [ToolCall(**tc) for tc in raw]
            except Exception as exc:
                logger.warning("Failed to parse tool_calls_json: %s", format_exception_message(exc))

        msg_files: list | None = None
        rehydrated_ids = file_ids_by_message.get(msg.id)
        if rehydrated_ids:
            refs = [file_refs_by_id[fid] for fid in rehydrated_ids if fid in file_refs_by_id]
            if refs:
                msg_files = refs

        chat_messages.append(
            ChatMessage(
                role=msg.role,
                content=history_labels.get(msg.id, msg.content),
                tool_calls=tool_calls,
                tool_call_id=msg.tool_call_id,
                name=msg.tool_name,
                files=msg_files,
                # Persistence watermark for conversation compaction.
                db_sequence=msg.sequence,
            )
        )

    if compaction_state is not None:
        # Prepend the persisted Pinned Directives + Rolling Summary so the
        # LLM keeps the compacted context and the compactor's roll-forward
        # detection (compaction message at index 0) fires on later cycles.
        return [render_stored_compaction(compaction_state), *chat_messages]
    return chat_messages


def without_current_turn(history: list[ChatMessage], text: str) -> list[ChatMessage]:
    """*history* without the user message of the turn being answered.

    A send path that stores its user message before it loads the history
    reads that message back as the newest entry, and ``run_agent_chat``
    appends the turn's text itself: without this the model receives the turn
    twice, and a large document in it is detached from one copy only. The
    newest entry is dropped when it is a user message holding *text* as
    stored (null bytes removed); any other history is returned as it is.
    """
    if text and history and history[-1].role == "user" and history[-1].content == text.replace("\x00", ""):
        return history[:-1]
    return history


async def _load_llm_trace_id(conversation_id: str) -> uuid.UUID | None:
    """Load the latest ``llm_trace_id`` cached in Redis for a conversation."""
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        raw = await redis.get(f"langfuse:chat:{conversation_id}:latest")
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return safe_uuid(parsed.get("llm_trace_id"))
    except Exception:
        logger.debug("Unable to load llm_trace_id from Redis for %s", conversation_id)
    return None


async def store_message(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    role: str,
    content: str,
    channel: str = "web",
    channel_message_id: str | None = None,
    tool_calls_json: str | None = None,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
    metadata_json: str | None = None,
    llm_trace_id: uuid.UUID | None = None,
) -> Message:
    """Store a message in the database."""
    # PostgreSQL text columns reject null bytes (0x00)
    if content:
        content = content.replace("\x00", "")
    # Touch conversation.updated_at FIRST (conversations sort by most recent
    # message) — the UPDATE takes the conversation row lock, which doubles as
    # the per-conversation sequence-allocation lock: concurrent writers on
    # separate sessions (the steering listener persisting a user message
    # while the web chat's mid-turn segment persist runs, #3293) block here and read
    # ``max(sequence)`` only after the winner committed, instead of both
    # reading the same max and committing duplicate sequence numbers that
    # reload in nondeterministic order.  Every caller already took this lock
    # at the tail of this function; moving it before the sequence read
    # changes when the lock is taken, not what is locked.
    touched = await db.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(updated_at=datetime.now(UTC))
        .returning(Conversation.agent_id)
    )
    if role == "assistant":
        # Who wrote it, read under the lock: a later agent of this
        # conversation reads it as that agent's words, not its own.
        from app.services.conversation_continuity import stamp_author

        metadata_json = stamp_author(metadata_json, touched.scalar_one_or_none())
    # Get next sequence number (serialized behind the row lock above)
    seq_result = await db.scalar(
        select(func.coalesce(func.max(Message.sequence), 0)).where(
            Message.conversation_id == conversation_id
        )
    )
    next_seq = (seq_result or 0) + 1

    if llm_trace_id is None and metadata_json:
        with contextlib.suppress(Exception):
            parsed = json.loads(metadata_json)
            if isinstance(parsed, dict):
                llm_trace_id = safe_uuid(parsed.get("llm_trace_id"))

    msg = Message(
        conversation_id=conversation_id,
        role=role,
        content=content,
        channel=channel,
        channel_message_id=channel_message_id,
        tool_calls_json=tool_calls_json,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        metadata_json=metadata_json,
        llm_trace_id=llm_trace_id,
        sequence=next_seq,
    )
    db.add(msg)
    await db.flush()

    return msg


async def move_messages_to_end(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    message_ids: list[str],
) -> int:
    """Give *message_ids* the next sequences of the conversation, in their order.

    A web steer is stored when it is sent. When the turn's loop never took it
    (it arrived while the final answer streamed) a follow-up pass answers it,
    and the answer that ignored it is stored after it: moved here, the steer
    reads after that answer and before its own. Taken under the conversation
    row lock :func:`store_message` allocates sequences behind. Ids of another
    conversation, or unknown, are left alone. Returns how many rows moved.
    """
    wanted = [u for u in (safe_uuid(m) for m in message_ids) if u is not None]
    if not wanted:
        return 0
    await db.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(updated_at=datetime.now(UTC))
    )
    rows = {
        m.id: m
        for m in (
            await db.execute(
                select(Message).where(
                    Message.conversation_id == conversation_id,
                    Message.id.in_(wanted),
                )
            )
        ).scalars()
    }
    if not rows:
        return 0
    top = await db.scalar(
        select(func.coalesce(func.max(Message.sequence), 0)).where(
            Message.conversation_id == conversation_id
        )
    )
    moved = 0
    for message_uuid in wanted:
        row = rows.get(message_uuid)
        if row is None:
            continue
        moved += 1
        row.sequence = (top or 0) + moved
    await db.flush()
    return moved


async def _resolve_channel_agent(
    db: AsyncSession,
    user_id: uuid.UUID,
    channel: ChannelType,
    inbound: InboundMessage,
    channel_bot: ChannelBot | None = None,
    *,
    bot_whitelist_passed: bool = True,
    declined_out: list[str] | None = None,
) -> Agent | None:
    """Find an agent configured to receive messages on *channel*.

    ``declined_out`` collects, for a group message no agent takes, one line
    per agent that receives on *channel* and declined it, naming the setting
    that decided; it stays empty when no agent receives on *channel* at all.

    Resolution order:
      0. **User DM override** — if the message is a direct/private
         conversation and the user has a ``channel_agent_config`` entry
         for this channel type, that agent is used unconditionally.
         If the message is a DM but no override is configured, return
         ``None`` immediately so the General Assistant handles it with
         the user's own profile configuration.
      1. Agents visible to the user (own + group-shared + company-managed)
         with ``receive_enabled`` for this channel.  Only applies to
         group/channel messages (not DMs).
      2. All enabled agents with ``receive_enabled`` (channel routing
         override) — allows private agents with ``bypass_whitelist`` to
         handle messages in their configured channels.  Only applies to
         group/channel messages (not DMs).
    """
    if channel == ChannelType.WEB:
        return None  # Web chat uses General Assistant or explicit agent

    ch_value = channel.value

    # --- Pass 0: user DM override (private conversations only) ---------
    if _is_direct_message(inbound):
        user_result = await db.execute(select(User).where(User.id == user_id))
        user_obj = user_result.scalar_one_or_none()
        if user_obj and user_obj.channel_agent_config:
            override_agent_id = user_obj.channel_agent_config.get(ch_value)
            if override_agent_id:
                try:
                    agent_result = await db.execute(
                        select(Agent).where(
                            Agent.id == uuid.UUID(override_agent_id),
                            Agent.enabled.is_(True),
                            hidden_rows_excluded(Agent),
                        )
                    )
                    override_agent = agent_result.scalar_one_or_none()
                    if override_agent:
                        logger.info(
                            "Routed %s DM to user-override agent '%s' (%s) for user %s",
                            ch_value,
                            override_agent.name,
                            override_agent.id,
                            user_id,
                        )
                        return override_agent
                    logger.warning(
                        "User %s channel_agent_config references agent %s "
                        "for %s but agent not found or disabled — falling "
                        "back to normal routing",
                        user_id,
                        override_agent_id,
                        ch_value,
                    )
                except (ValueError, AttributeError):
                    logger.warning(
                        "Invalid agent ID '%s' in channel_agent_config for user %s channel %s",
                        override_agent_id,
                        user_id,
                        ch_value,
                    )

        # DM with no profile override — fall through to the General
        # Assistant so the user's own GA configuration (integrations,
        # tools, KBs) is used.  Without this guard, Pass 1 could bind
        # the DM to another user's agent that happens to have
        # receive_enabled for this channel.
        return None

    # --- Pass 1: agents visible to the user (preferred) ----------------
    #     Only fetch agents whose channel_config contains the requested
    #     channel key to avoid scanning unrelated agents.
    user_group_ids = await get_user_group_ids(db, user_id)

    result = await db.execute(
        select(Agent).where(
            user_visible_filter(Agent, user_id, user_group_ids),
            Agent.enabled.is_(True),
            Agent.channel_config.isnot(None),
            Agent.channel_config[ch_value].isnot(None),
        )
    )
    agents = result.scalars().all()

    rejected = declined_out if declined_out is not None else []
    for agent in agents:
        ch_cfg = (agent.channel_config or {}).get(ch_value)
        if not ch_cfg or not ch_cfg.get("receive_enabled"):
            continue
        allowed, reason = _agent_allows_channel_reason(
            agent,
            channel,
            inbound,
            channel_bot,
            bot_whitelist_passed=bot_whitelist_passed,
        )
        if allowed:
            return agent
        rejected.append(f"'{agent.name}' ({agent.id}): {reason}")

    # --- Pass 2: ALL enabled agents (channel routing override) ---------
    # Allows private agents with bypass_whitelist / receive_enabled to
    # handle group/channel messages even when not in the user's visibility
    # scope.  DMs never reach this point (returned in Pass 0 above).
    result = await db.execute(
        select(Agent).where(
            Agent.enabled.is_(True),
            hidden_rows_excluded(Agent),
            Agent.channel_config.isnot(None),
            Agent.channel_config[ch_value].isnot(None),
        )
    )
    all_agents = result.scalars().all()

    seen_ids = {a.id for a in agents}
    for agent in all_agents:
        if agent.id in seen_ids:
            continue
        ch_cfg = (agent.channel_config or {}).get(ch_value)
        if not ch_cfg or not ch_cfg.get("receive_enabled"):
            continue
        allowed, reason = _agent_allows_channel_reason(
            agent,
            channel,
            inbound,
            channel_bot,
            bot_whitelist_passed=bot_whitelist_passed,
        )
        if allowed:
            logger.info(
                "Routed %s message to agent '%s' (%s) via channel-config "
                "fallback (agent not in user %s visibility scope)",
                ch_value,
                agent.name,
                agent.id,
                user_id,
            )
            return agent
        rejected.append(f"'{agent.name}' ({agent.id}): {reason} [not visible to user]")

    logger.debug(
        "No agent took the %s message: %s (user=%s, channel_thread_id=%s)",
        ch_value,
        "; ".join(rejected) if rejected else f"no agent receives {ch_value} messages",
        user_id,
        _log_thread_id(inbound),
    )
    return None


# ---------------------------------------------------------------------------
# Reset command handling
# ---------------------------------------------------------------------------


def _is_reset_command(text: str) -> bool:
    """Return True if *text* is a conversation-reset command."""
    return text.strip().lower() in _RESET_COMMANDS


async def _handle_reset(
    db: AsyncSession,
    user: User | None,
    inbound: InboundMessage,
) -> OutboundMessage:
    """Archive the current conversation so the next message starts fresh.

    For external channels, the ``channel_thread_id`` is permanent (especially
    in Teams personal chats and WhatsApp).  Archiving the conversation forces
    ``find_or_create_conversation`` to create a new one on the next message.
    """
    archived = False

    if inbound.channel_thread_id:
        if user:
            q = await db.execute(
                select(Conversation).where(
                    Conversation.user_id == user.id,
                    Conversation.channel == inbound.channel.value,
                    Conversation.channel_thread_id == inbound.channel_thread_id,
                    Conversation.is_archived.is_(False),
                )
            )
        else:
            # Anonymous conversation
            q = await db.execute(
                select(Conversation).where(
                    Conversation.user_id.is_(None),
                    Conversation.channel == inbound.channel.value,
                    Conversation.channel_thread_id == inbound.channel_thread_id,
                    Conversation.is_archived.is_(False),
                )
            )
        conv = q.scalar_one_or_none()
        if conv:
            conv.is_archived = True
            archived = True
            logger.info(
                "Reset command: archived conversation %s for %s (channel=%s, channel_thread_id=%s)",
                conv.id,
                user.id if user else "anonymous",
                inbound.channel.value,
                _log_thread_id(inbound),
            )
        if user and await _release_bound_thread(db, inbound, user):
            # A thread a web conversation was continued in is
            # released, never archived: the conversation stays the user's.
            archived = True
            logger.info(
                "Reset command: released the %s thread a conversation was "
                "continued in for %s (channel_thread_id=%s)",
                inbound.channel.value,
                user.id,
                _log_thread_id(inbound),
            )

    await db.commit()

    reply = notice("reset_done" if archived else "reset_nothing", notice_language(user))
    return OutboundMessage(
        text=reply,
        channel_thread_id=inbound.channel_thread_id,
        channel_user_id=inbound.channel_user_id,
    )


async def _handle_stop(
    inbound: InboundMessage,
) -> OutboundMessage:
    """Mute the bot in the current thread.

    Deletes the ``active_thread`` Redis key and sets a ``muted_thread`` key
    so the bot silently ignores all follow-up messages in this thread.
    The mute is lifted when someone explicitly @mentions the bot again.
    Answered before the sender is looked up: the platform default language.
    """
    _thread_key = _get_thread_key(inbound)
    if _thread_key:
        await _mute_thread(inbound.channel, _thread_key)
        reply = notice("stop_done", notice_language())
    else:
        reply = notice("stop_threads_only", notice_language())
    return OutboundMessage(
        text=reply,
        channel_thread_id=inbound.channel_thread_id,
        channel_user_id=inbound.channel_user_id,
    )


# ---------------------------------------------------------------------------
# Conversation memory compaction for external channels
# ---------------------------------------------------------------------------


async def _compact_history(
    db: AsyncSession,
    history: list[ChatMessage],
    *,
    effective_user_id: str,
    agent_id: uuid.UUID | None,
    conversation_id: uuid.UUID | None = None,
) -> list[ChatMessage]:
    """Apply sliding-window summarisation to long conversation histories.

    This mirrors the compaction applied in the web REST chat path
    (``chat.py``) so that external channels (Slack, Teams, WhatsApp,
    Telegram) also benefit from context-window management.

    Returns the (possibly shortened) history list.  On any error the
    original history is returned unchanged.
    """
    try:
        from app.llm.router import get_llm_for_user
        from app.services.conversation_memory import manage_conversation_memory

        llm, resolved_model = await get_llm_for_user(
            db, effective_user_id if effective_user_id != "anonymous" else None
        )

        is_company_managed = False
        memory_persistent_enabled = True
        if agent_id:
            res = await db.execute(
                select(Agent.is_company_managed, Agent.memory_persistent_enabled).where(
                    Agent.id == agent_id
                )
            )
            row = res.one_or_none()
            if row is not None:
                is_company_managed = bool(row[0])
                memory_persistent_enabled = bool(row[1])

        # Commit-release before compaction: on compaction-cycle turns
        # this awaits a full LLM summarization on the inbound channel
        # message while the reads above hold an open transaction.
        from app.core.database import release_db_connection_before_loop

        await release_db_connection_before_loop(db)
        return await manage_conversation_memory(
            llm,
            history,
            agent_id=str(agent_id) if agent_id else None,
            user_id=effective_user_id if effective_user_id != "anonymous" else None,
            is_company_managed=is_company_managed,
            memory_persistent_enabled=memory_persistent_enabled,
            model=resolved_model,
            conversation_id=str(conversation_id) if conversation_id else None,
        )
    except Exception as exc:
        logger.debug(
            "Conversation memory compaction failed for channel message (non-critical): %s",
            format_exception_message(exc),
        )
        return history


async def _log_channel_activity(
    db: AsyncSession,
    user: User,
    inbound: InboundMessage,
    conv: Conversation,
    target_agent_id: uuid.UUID | None,
    assistant_text: str,
) -> None:
    """Create an Activity entry when an agent responds on an external channel.

    This makes channel conversations visible in the Activity feed so admins
    and users can see when the bot responds on Slack, Teams, WhatsApp, etc.
    Skipped for anonymous users (Activity requires ``user_id``).
    """
    try:
        agent_name: str | None = None
        if target_agent_id:
            result = await db.execute(
                select(Agent.name).where(Agent.id == target_agent_id)
            )
            agent_name = result.scalar_one_or_none()

        channel_label = inbound.channel.value.title()
        is_private = _is_private_conversation(inbound)
        convo_type = "DM" if is_private else "message"
        title = f"{channel_label} {convo_type} — {agent_name or 'General Assistant'}"

        activity = Activity(
            user_id=user.id,
            activity_type="channel_message",
            agent_id=target_agent_id,
            agent_name=agent_name or "General Assistant",
            title=title,
            summary=assistant_text[:500] if assistant_text else None,
            extra_data={
                "conversation_id": str(conv.id),
                "channel": inbound.channel.value,
                "is_private": is_private,
            },
        )
        db.add(activity)

        # Push via SSE for real-time updates
        redis = await redis_client()
        payload = json.dumps(
            {
                "type": "channel_message",
                "agent_name": agent_name or "General Assistant",
                "channel": inbound.channel.value,
                "is_private": is_private,
                "summary": assistant_text[:200] if assistant_text else "",
            }
        )
        await redis.publish(f"activity:{user.id}", payload)
    except Exception as exc:
        logger.warning("Failed to log channel activity: %s", format_exception_message(exc))


async def _shared_thread_fallback(
    db: AsyncSession,
    channel: ChannelType,
    channel_thread_id: str,
    agent_id: uuid.UUID | None = None,
) -> Conversation | None:
    """Return the oldest non-archived conversation for a given channel thread.

    Used as a fallback when no user-scoped match is found and
    ``allow_shared_thread=True``.  The optional *agent_id* narrows the search
    to the same agent so that a shared ``channel_thread_id`` in one agent's
    conversation cannot surface another agent's context (cross-agent
    isolation).
    """
    filters = [
        Conversation.channel == channel.value,
        Conversation.channel_thread_id == channel_thread_id,
        Conversation.is_archived.is_(False),
    ]
    if agent_id is not None:
        filters.append(Conversation.agent_id == agent_id)
    result = await db.execute(
        select(Conversation)
        .where(*filters)
        .order_by(Conversation.created_at.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _should_allow_shared_thread(inbound: InboundMessage) -> bool:
    """Return ``True`` when inbound is a non-private external-channel message.

    Group chats and public channels share a single conversation per
    ``channel_thread_id`` so secondary participants continue an existing thread
    instead of creating parallel conversations that miss the prior context.
    Private / DM conversations always return ``False`` so personal
    contexts never leak across users.
    """
    if inbound.channel in (ChannelType.WEB, ChannelType.WEB_EMBED):
        return False
    return not _is_private_conversation(inbound)


async def _find_bound_conversation(
    db: AsyncSession, inbound: InboundMessage, user: User
) -> Conversation | None:
    """The conversation *user* continues in this direct-message thread.

    Only for the sender the binding was made by, who still owns the
    conversation: an unresolved sender never gets here, another user finds
    nothing.
    """
    from app.services.channel_continuation import find_bound_conversation

    return await find_bound_conversation(
        db, inbound.channel.value, inbound.channel_thread_id, user_id=user.id
    )


async def _release_bound_thread(
    db: AsyncSession, inbound: InboundMessage, user: User
) -> int:
    """Release the direct-message thread *user* reset (``/new``), audited."""
    from app.services.channel_continuation import audit_released_threads, unbind_thread

    released = await unbind_thread(
        db, inbound.channel.value, inbound.channel_thread_id, user_id=user.id
    )
    await audit_released_threads(
        db,
        user=user,
        conversation_ids=released,
        channel=inbound.channel.value,
        how="its thread was reset with /new",
    )
    return len(released)


async def _find_existing_external_conversation(
    db: AsyncSession,
    user_id: uuid.UUID | None,
    channel: ChannelType,
    channel_thread_id: str | None,
    *,
    allow_shared_thread: bool = False,
    agent_id: uuid.UUID | None = None,
) -> Conversation | None:
    """Find an existing conversation for an external channel (no creation).

    Used by the deferred-creation pattern: external channels only look up
    existing conversations initially, deferring creation until the agent
    decides to respond.

    When *user_id* is ``None``, matches conversations with
    ``user_id IS NULL`` (anonymous).

    When *allow_shared_thread* is ``True`` (set for non-private, non-DM
    Teams/Slack/Telegram/WhatsApp/Discord/Google Chat conversations, i.e.
    group chats and public channels), the lookup falls back to any
    conversation matching the same `channel_thread_id` regardless of
    `user_id`. This is required so secondary participants in a group
    chat continue an existing thread instead of creating their own
    parallel conversation that misses the prior context.

    The optional *agent_id* is forwarded to :func:`_shared_thread_fallback`
    so the cross-user lookup is scoped to the same agent, preventing a
    shared ``channel_thread_id`` from surfacing a different agent's
    conversation history.
    """
    if not channel_thread_id:
        return None

    if user_id is None:
        result = await db.execute(
            select(Conversation).where(
                Conversation.user_id.is_(None),
                Conversation.channel == channel.value,
                Conversation.channel_thread_id == channel_thread_id,
                Conversation.is_archived.is_(False),
            )
        )
        existing = result.scalar_one_or_none()
        if existing or not allow_shared_thread:
            return existing
        # Anonymous lookup fell through; the thread might have been started
        # by an authenticated participant before this one.
        return await _shared_thread_fallback(db, channel, channel_thread_id, agent_id)

    result = await db.execute(
        select(Conversation).where(
            Conversation.user_id == user_id,
            Conversation.channel == channel.value,
            Conversation.channel_thread_id == channel_thread_id,
            Conversation.is_archived.is_(False),
        )
    )
    existing = result.scalar_one_or_none()
    if existing or not allow_shared_thread:
        return existing

    # Group-chat / public-channel fallback: another participant in the
    # same `channel_thread_id` may already own the conversation. Continue
    # that thread so secondary participants see and contribute to the
    # same shared context.
    return await _shared_thread_fallback(db, channel, channel_thread_id, agent_id)


async def _attach_files_to_message(
    db: AsyncSession,
    conv: Conversation,
    message: Message,
    refs: list | None,
) -> list:
    """Show the stored files behind *refs* on *message*.

    Binds each ``FileAttachment`` row to the conversation and the message and
    writes the message's ``attachments`` block, the shared bind: the
    file then has a chip, is rehydrated with the message on later turns
    (``get_conversation_history``), is loaded into the next turn's FileContext
    and is in the conversation scope a channel link is signed for - an
    anonymous sender's file included. A row another message already shows is
    left there. Never raises.
    """
    ids = [str(ref.file_id) for ref in refs or [] if getattr(ref, "file_id", None)]
    return await _attach_file_ids_to_message(db, conv, message, ids)


async def _attach_file_ids_to_message(
    db: AsyncSession,
    conv: Conversation,
    message: Message,
    ids: list[str],
) -> list:
    """:func:`_attach_files_to_message` for the files named by id."""
    if not ids:
        return []
    from app.services.sub_agent_files import attach_rows_to_message, load_attachment_rows

    try:
        rows = await load_attachment_rows(ids, db=db, conversation_id=conv.id)
    except Exception as exc:
        logger.warning(
            "Could not load the files of a %s message: %s",
            message.channel,
            format_exception_message(exc),
        )
        return []
    return await attach_rows_to_message(db, message, rows, conversation_id=conv.id)


def _with_produced_file_markers(text: str, refs: list | None) -> str:
    """*text* with a ``[[FILE:id]]`` marker for each produced download file it does not name.

    The web chat shows such a file as a chip under the reply; a channel reply
    has no chip, so the marker becomes its signed link
    (:func:`~app.channels.base.resolve_channel_file_markers`). Working files
    (``agent_working``: a rendered figure's source, a document read's copy;
    ``agent_check``: a check's picture) stay the agent's own unless the reply
    names them.
    """
    from app.channels.base import _INTERNAL_IMAGE_RE
    from app.llm.base import is_working_file_source

    lowered = (text or "").lower()
    # The image tags ``resolve_channel_images`` signs; any other internal URL
    # stays relative in a channel and links nowhere.
    signed_images = {m.group(4).lower() for m in _INTERNAL_IMAGE_RE.finditer(text or "")}
    markers: list[str] = []
    for ref in refs or []:
        file_id = str(getattr(ref, "file_id", "") or "").removeprefix("file-").lower()
        if not safe_uuid(file_id) or is_working_file_source(getattr(ref, "source", None)):
            continue
        if not getattr(ref, "s3_object_name", None):
            continue
        if f"[[file:{file_id}]]" in lowered or file_id in signed_images:
            continue
        marker = f"[[FILE:{file_id}]]"
        if marker not in markers:
            markers.append(marker)
    if not markers:
        return text
    return f"{text}\n\n" + "\n".join(markers) if text else "\n".join(markers)


async def _store_user_message(
    db: AsyncSession,
    conv: Conversation,
    inbound: InboundMessage,
    files: list | None = None,
    stored_out: dict | None = None,
) -> bool:
    """Store a user message and update the conversation title if still default.

    *files* are the stored copies of the message's attachments
    (:func:`_resolve_channel_files`), shown on the message it stores.
    *stored_out*, when given, receives the stored message under
    ``user_message``.

    Returns ``True`` when the title was a placeholder and needs AI generation
    after the assistant responds.
    """
    message = await store_message(
        db,
        conv.id,
        role="user",
        content=inbound.text,
        channel=inbound.channel.value,
        channel_message_id=inbound.channel_message_id,
    )
    if stored_out is not None:
        stored_out["user_message"] = message
    if files:
        await _attach_files_to_message(db, conv, message, files)
    needs_ai_title = conv.title == "New conversation"
    if needs_ai_title:
        conv.title = inbound.text[:200] if len(inbound.text) > 200 else inbound.text
    return needs_ai_title


async def _finalize_message(
    db: AsyncSession,
    inbound: InboundMessage,
    *,
    user: User | None,
    conv: Conversation | None,
    target_agent_id: uuid.UUID | None,
    is_anonymous: bool,
    is_external_channel: bool,
    suppress: bool,
    assistant_text: str,
    answering_agent_id: uuid.UUID | None = None,
    needs_ai_title: bool = False,
    ai_title: bool = True,
    llm_trace_id: uuid.UUID | None = None,
    user_files: list | None = None,
    turn_files: list | None = None,
    assistant_metadata: dict | None = None,
    mention_replies: list | None = None,
    store_reply: bool = True,
    stored_out: dict | None = None,
) -> Conversation | None:
    """Persist messages, log activity, and commit after LLM execution.

    *stored_out*, when given, receives the stored user message under
    ``user_message`` and the stored reply's id under ``assistant_message_id``.
    *store_reply* False: the routed agent chose silence and the @mentioned
    agents answered - only their replies are stored, and the last
    one's id is the reply's.

    Post-processing for ``handle_message``.  Handles:

    - Deferred conversation creation (external channels), bound to
      *target_agent_id*; *answering_agent_id* is the agent the activity entry
      names when it differs (the General Assistant's stand-in; the
      @mentioned agent whose reply is the answer when the routed agent chose
      silence), and defaults to *target_agent_id*
    - User message storage (external channels, deferred from pre-processing),
      with the message's attachments (*user_files*)
    - Assistant response storage, with the files the turn produced
      (*turn_files*), then the @mentioned agents' replies, each its
      own attributed message
    - AI-powered conversation title generation (first exchange), unless
      *ai_title* is False: a turn refused before any model ran gets none
    - Channel activity logging
    - Thread continuation marking
    - Database commit

    Returns the (possibly newly created) conversation.
    """
    # Deferred conversation creation + user message (external channels only)
    _deferred_needs_title = False
    if is_external_channel and not suppress:
        if not conv:
            if is_anonymous:
                conv = await _find_or_create_anonymous_conversation(
                    db,
                    inbound.channel,
                    inbound.channel_thread_id,
                    agent_id=target_agent_id,
                )
            elif user is not None:
                conv = await find_or_create_conversation(
                    db,
                    user.id,
                    inbound.channel,
                    inbound.channel_thread_id,
                )
                if target_agent_id:
                    conv.agent_id = target_agent_id
            else:
                logger.error(
                    "Cannot create conversation — user is None and not anonymous"
                )
        if conv:
            # Created only now for a new thread: what this turn stores holds this
            # conversation's markers, which its next turn restores.
            bind_known_secrets(conv.id)
            _deferred_needs_title = await _store_user_message(
                db, conv, inbound, files=user_files, stored_out=stored_out
            )

    # Store assistant response
    if not suppress and conv is not None:
        if store_reply:
            assistant_message = await store_message(
                db,
                conv.id,
                role="assistant",
                content=assistant_text,
                channel=inbound.channel.value,
                metadata_json=json.dumps(assistant_metadata) if assistant_metadata else None,
                llm_trace_id=llm_trace_id,
            )
            if stored_out is not None:
                stored_out["assistant_message_id"] = str(assistant_message.id)
            if turn_files:
                await _attach_files_to_message(db, conv, assistant_message, turn_files)
        for _reply in mention_replies or []:
            _reply_meta = _reply.metadata()
            _reply_message = await store_message(
                db,
                conv.id,
                role="assistant",
                content=_reply.text,
                channel=inbound.channel.value,
                metadata_json=json.dumps(_reply_meta) if _reply_meta else None,
                llm_trace_id=safe_uuid(_reply.trace.get("llm_trace_id")),
            )
            # The files its own run produced.
            if _reply.file_ids:
                await _attach_file_ids_to_message(db, conv, _reply_message, _reply.file_ids)
            if not store_reply and stored_out is not None:
                stored_out["assistant_message_id"] = str(_reply_message.id)

    # Generate AI-powered conversation title on the first exchange
    _should_gen_title = ai_title and (needs_ai_title or _deferred_needs_title)
    if _should_gen_title and not suppress and conv is not None and assistant_text:
        try:
            from app.llm.router import get_llm_for_user as _get_llm_ch_title
            from app.services.conversation_memory import (
                generate_conversation_title as _gen_title_ch,
            )

            _user_id_for_title = str(user.id) if user else None
            _llm_ch, _ch_title_model = await _get_llm_ch_title(db, _user_id_for_title)
            _ch_title = await _gen_title_ch(
                _llm_ch, inbound.text, assistant_text, model=_ch_title_model
            )
            if _ch_title:
                conv.title = _ch_title
        except Exception as _ch_title_exc:
            logger.debug(
                "Channel AI title generation failed (non-critical): %s", format_exception_message(_ch_title_exc)
            )

    # Log channel activity (external channels only, when agent responds)
    if is_external_channel and not suppress and conv is not None and user is not None:
        await _log_channel_activity(
            db,
            user,
            inbound,
            conv,
            answering_agent_id if answering_agent_id is not None else target_agent_id,
            assistant_text,
        )

    # Mark thread as active for thread continuation tracking
    if is_external_channel and not suppress:
        _thread_key = _get_thread_key(inbound)
        if _thread_key:
            await _mark_thread_active(inbound.channel, _thread_key)

    await db.commit()
    return conv


def _answers_channel_mention(
    agent: Agent,
    inbound: InboundMessage,
    channel_bot: ChannelBot | None,
    *,
    bot_whitelist_passed: bool,
) -> bool:
    """Whether an agent a channel message names can answer it there.

    Only through what the agent already configures for this channel: enabled,
    open to chat, ``receive_enabled`` in its ``channel_config``, accepted by
    ``_agent_allows_channel`` exactly as the routing would accept it (bot
    whitelist, ``receive_channels``, bot-enforced ``allow_private`` and
    ``only_mention``) and allowed to reply there.
    """
    ch_cfg = (agent.channel_config or {}).get(inbound.channel.value) or {}
    return bool(
        agent.enabled
        and not agent.disable_chat
        and ch_cfg.get("receive_enabled")
        and _agent_allows_channel(
            agent,
            inbound.channel,
            inbound,
            channel_bot,
            bot_whitelist_passed=bot_whitelist_passed,
        )
        and _agent_can_publish(agent, inbound.channel)
    )


async def _read_channel_mentions(
    db: AsyncSession,
    inbound: InboundMessage,
    user: User | None,
    *,
    channel_bot: ChannelBot | None,
    bot_whitelist_passed: bool,
    is_external_channel: bool,
    is_anonymous: bool,
) -> list[str]:
    """The agents an identified user's channel message names as ``@key`` and
    that can answer it there (``_answers_channel_mention``).

    Empty unless ``CHANNEL_AGENT_MENTIONS_ENABLED`` is on: the Receive
    and Publish settings of the agents a bot routes to were set for routing,
    not as consent to answer each other's mentions, so off, the ``@`` tokens
    are never read and the routed agent answers alone. Any other ``@`` token
    is plain text: no reply, no note, and the routed agent's run is metered
    exactly as for a message without mentions. An anonymous sender's tokens
    are never read: its message is answered by the one agent the anonymous
    routing picks.
    """
    from app.core.config import settings

    if not (
        settings.channel_agent_mentions_enabled
        and is_external_channel
        and user is not None
        and not is_anonymous
    ):
        return []
    try:
        from app.services.mention_dispatch import mentioned_agent_ids_from_text

        return await mentioned_agent_ids_from_text(
            db,
            user_id=user.id,
            text=inbound.text,
            admit=lambda agent: _answers_channel_mention(
                agent, inbound, channel_bot, bot_whitelist_passed=bot_whitelist_passed
            ),
        )
    except Exception:
        logger.warning(
            "Could not read the @mentions of a %s message; only the routed agent answers",
            inbound.channel.value,
            exc_info=True,
        )
        return []


async def _channel_mention_targets(
    db: AsyncSession,
    user: User | None,
    mentioned: list[str],
    answering_agent_id: uuid.UUID | None,
    language: str,
) -> list:
    """The agents the routed agent reads as answering after it.

    ``mentioned`` already holds only the agents that can answer the message
    here (:func:`_read_channel_mentions`); they are resolved the way
    :func:`_answer_channel_mentions` resolves them - which does it again once
    the routed agent has run, so an agent disabled meanwhile, or the one a
    transfer handed the message to, is not asked. A lookup error leaves the
    routed agent's run as it is without them.
    """
    if user is None or not mentioned:
        return []
    from app.services.mention_dispatch import resolve_mention_targets

    try:
        return await resolve_mention_targets(
            db,
            user_id=user.id,
            raw_ids=mentioned,
            answering_agent_id=str(answering_agent_id) if answering_agent_id else None,
            language=language,
        )
    except Exception:
        logger.warning(
            "Could not resolve the agents a channel message mentions; the routed agent "
            "answers without their line-up",
            exc_info=True,
        )
        return []


async def _answer_channel_mentions(
    db: AsyncSession,
    inbound: InboundMessage,
    *,
    user: User,
    channel_bot: ChannelBot | None,
    answering_agent_id: uuid.UUID | None,
    first_reply: str,
    history: list[ChatMessage],
    message_context: dict | None,
    channel_files: list | None,
    conversation_id: str | None,
    mentioned: list[str],
    metering: TurnMetering,
    bot_whitelist_passed: bool = True,
    first_failed: bool = False,
    first_silent: bool = False,
    file_context: FileContext | None = None,
) -> tuple[list, dict | None, str | None]:
    """Have the agents a channel message @mentions answer in turn.

    ``first_silent``: the routed agent chose silence (``[NO_RESPONSE]``) - it
    knew who answers after it, so having nothing to add is its part.
    The agents the user named still answer; its reply is neither read by them
    nor part of the outbound message.

    Returns ``(replies, metadata of the first reply, outbound text)`` - all
    empty when the message mentions no other agent. Same contract as the web
    chat (``services/mention_dispatch``): mention order, earlier replies read
    labelled, one agentic action per agent that runs, nobody past a quota
    refusal (``metering`` holds the routed agent's outcome). On a channel,
    only an agent that can answer there under its own channel settings
    answers (``_answers_channel_mention``); any other one is plain text,
    never a note. The replies are stored separately and delivered in one
    message, a labelled section per agent, since every adapter sends one
    reply per inbound message. Each run works in the turn's ``file_context``
    and the files it produced are its reply's (``MentionReply.file_ids``).
    """
    from app.services.agent_chat import run_agent_chat
    from app.services.mention_dispatch import (
        MENTIONED_AGENT_HANDOVER_TYPES,
        MentionReply,
        TurnRefusedError,
        compose_channel_reply,
        conversation_agent_target,
        dispatch_mentions,
        quota_refused,
        reply_metadata,
        resolve_mention_targets,
        run_on_own_session,
    )

    if not mentioned or metering.refused:
        return [], None, None
    language = notice_language(user)
    targets = await resolve_mention_targets(
        db,
        user_id=user.id,
        raw_ids=mentioned,
        answering_agent_id=str(answering_agent_id) if answering_agent_id else None,
        language=language,
    )
    # Read again now the routed agent has run: an agent that can no longer
    # answer here is dropped like any plain-text token, never noted.
    # Only the per-message limit still leaves its note.
    targets = [
        t
        for t in targets
        if t.over_limit
        or (
            t.agent is not None
            and _answers_channel_mention(
                t.agent, inbound, channel_bot, bot_whitelist_passed=bot_whitelist_passed
            )
        )
    ]
    if not targets:
        return [], None, None
    channel_name = inbound.channel.value
    first = MentionReply(
        await conversation_agent_target(db, answering_agent_id), first_reply, failed=first_failed
    )
    # The user addressed each mentioned agent by name: read as a mention, so a
    # group-channel agent does not default to staying silent.
    mention_context = {**(message_context or {}), "bot_was_mentioned": True}
    traces: dict[str, dict[str, str]] = {}
    files_by_agent: dict[str, list[str]] = {}

    def _file_ids() -> list[str]:
        return [ref.file_id for ref in file_context.list_files()] if file_context is not None else []

    async def _run(
        agent, mention_history: list[ChatMessage], cancel: asyncio.Event, agents_after: list
    ) -> str:
        async def _run_on(own_db, own_agent) -> str:
            # Its own agentic action, charged before it runs.
            quota: dict = {}
            before = set(_file_ids())
            try:
                text, _ = await run_agent_chat(
                    own_db,
                    str(user.id),
                    own_agent,
                    mention_history,
                    inbound.text,
                    context=mention_context,
                    files=channel_files or None,
                    conversation_id=conversation_id,
                    file_context=file_context,
                    cancel_event=cancel,
                    quota_out=quota,
                    agents_after=agents_after,
                    allowed_handover_types=MENTIONED_AGENT_HANDOVER_TYPES,
                )
            finally:
                # The files this run wrote are shown by its own reply.
                files_by_agent[str(own_agent.id)] = [i for i in _file_ids() if i not in before]
            if quota_refused(quota):
                raise TurnRefusedError(text)
            trace_id = await _load_llm_trace_id(conversation_id) if conversation_id else None
            traces[str(own_agent.id)] = {"llm_trace_id": str(trace_id)} if trace_id else {}
            return text

        return await run_on_own_session(agent, _run_on)

    replies = await dispatch_mentions(
        targets,
        history=history,
        user_message=inbound.text,
        earlier=[first] if first_reply.strip() and not first_failed and not first_silent else [],
        run_agent=_run,
        cancel_event=None,
        describe_error=lambda _exc: notice("mention_error_reason", language),
        language=language,
    )
    reply_format = override_content_format(mention_context.get("platform"))
    for reply in replies:
        reply.trace = traces.get(reply.target.agent_id, {})
        reply.file_ids = files_for_reply(
            file_context, files_by_agent.get(reply.target.agent_id, []), reply.text
        )
        if not reply.failed:
            reply.content_format = reply_format
    # An agent that chose silence adds nothing to the thread.
    replies = [r for r in replies if not _is_no_response(r.text)]
    if not replies:
        return [], None, None
    # Each reply is stored as written; the delivered message is the reader's.
    shown = [dataclasses.replace(r, text=localized_reply(r.text, language)) for r in replies]
    return (
        replies,
        reply_metadata(first.target, failed=first_failed),
        compose_channel_reply(channel_name, shown if first_silent else [first, *shown]),
    )


# ---------------------------------------------------------------------------
# Channel file attachment resolution
# ---------------------------------------------------------------------------


async def _store_queued_files(db: AsyncSession, inbound: InboundMessage, user: User) -> list[str]:
    """Store the files of a message about to be queued: their ids.

    A platform's file URL may not outlive the wait (a Teams personal chat's
    lasts minutes), so the files are stored now, marked queued, committed,
    and claimed when the message runs (:func:`_stored_channel_files`); those
    of a message that never runs are deleted
    (``channel_continuation.discard_queued_files``, the upload purge).
    """
    from app.services.channel_continuation import queued_file_source
    from app.services.file_cleanup import purge_storage_objects

    refs = await _resolve_channel_files(
        db, inbound, str(user.id), source=queued_file_source(inbound.channel.value)
    )
    try:
        await db.commit()
    except Exception:
        # No row names the objects any more.
        with contextlib.suppress(Exception):
            await db.rollback()
        await purge_storage_objects(ref.s3_object_name for ref in refs)
        raise
    return [str(ref.file_id) for ref in refs]


async def _stored_channel_files(
    db: AsyncSession, file_ids: list[str], user_id: uuid.UUID, channel: str
) -> list:
    """The ``FileRef`` of each of *user_id*'s queued files this turn claimed, in *file_ids* order."""
    from app.llm.base import FileRef
    from app.services.channel_continuation import claim_queued_files

    rows = await claim_queued_files(db, file_ids, channel=channel, user_id=user_id)
    return [FileRef.from_attachment(row) for row in rows]


async def _resolve_channel_files(
    db: AsyncSession,
    inbound: InboundMessage,
    user_id: str | None,
    *,
    source: str | None = None,
    missed_out: list[InboundFileAttachment] | None = None,
) -> list:
    """Download channel file attachments, upload to S3, and return FileRef list.

    Each :class:`InboundFileAttachment` on the inbound message is:
    1. Downloaded from the platform URL (using the attachment's ``auth_headers``).
    2. Uploaded to S3 via :func:`file_storage.upload`.
    3. Persisted as a :class:`FileAttachment` record (``source`` *source*,
       ``channel:<channel>`` by default).
    4. Converted to a :class:`FileRef`.

    Skips files that fail to download or exceed size limits; *missed_out*,
    when given, receives each of them (a voice note that could not be
    downloaded is then stated in the turn, :func:`_hear_voice_notes`).
    Returns an empty list when there are no attachments.
    """
    if not inbound.file_attachments:
        return []

    import httpx

    from app.llm.base import FileRef
    from app.models.file_attachment import FileAttachment
    from app.services.file_cleanup import purge_storage_objects
    from app.services.file_storage import MAX_UPLOAD_SIZE_BYTES, file_storage

    refs: list = []
    for att in inbound.file_attachments:
        # Stored, and named by no row yet: removed if the row is not written.
        s3_key: str | None = None
        resolved_before = len(refs)
        try:
            downloaded = await _download_channel_file(att, MAX_UPLOAD_SIZE_BYTES)
            if downloaded is None:
                if missed_out is not None:
                    missed_out.append(att)
                continue
            data, resp_ct = downloaded

            # Determine content type from response or attachment metadata:
            # the response's when the attachment reports none.
            content_type = att.content_type
            if content_type == "application/octet-stream" and resp_ct and resp_ct != "application/octet-stream":
                content_type = resp_ct.split(";")[0].strip()

            # Upload to S3
            s3_key = await file_storage.upload(
                data=data,
                filename=att.filename,
                content_type=content_type,
                prefix=f"channel/{inbound.channel.value}",
            )

            # Create FileAttachment record
            user_id_uuid = None
            if user_id and user_id != "anonymous":
                import contextlib
                with contextlib.suppress(ValueError, AttributeError):
                    user_id_uuid = uuid.UUID(user_id)

            fa = FileAttachment(
                id=uuid.uuid4(),
                user_id=user_id_uuid,
                filename=att.filename,
                content_type=content_type,
                size=len(data),
                s3_object_name=s3_key,
                source=source or f"channel:{inbound.channel.value}",
            )
            db.add(fa)
            await db.flush()
            s3_key = None

            refs.append(FileRef.from_attachment(fa))
            logger.info(
                "Channel file resolved: %s (%s, %d bytes)",
                fa.id,
                content_type,
                len(data),
            )
        except httpx.HTTPStatusError as exc:
            if missed_out is not None:
                missed_out.append(att)
            # No traceback: the error's message is the URL.
            logger.warning(
                "Failed to resolve channel file attachment: %s (%s, HTTP %d)",
                att.content_type,
                _log_url_host(att.url),
                exc.response.status_code,
            )
        except Exception:
            if missed_out is not None and len(refs) == resolved_before:
                missed_out.append(att)
            logger.warning(
                "Failed to resolve channel file attachment: %s (%s)",
                att.content_type,
                _log_url_host(att.url),
                exc_info=True,
            )
        if s3_key:
            await purge_storage_objects([s3_key])
    return refs


async def _hear_voice_notes(
    db: AsyncSession,
    inbound: InboundMessage,
    files: list,
    missed: list[InboundFileAttachment],
    user: User | None,
) -> None:
    """Put the transcript of every voice note of the message in its text.

    A voice clip or an audio file sent to a channel bot (a Teams chat, a
    channel or a channel thread the bot is addressed in, and any other
    adapter whose files reach the router) reaches the turn as
    ``[voice note, 0:42, transcribed] ...`` after the message's own text,
    in place of the file-only placeholder; an audio that could not be
    downloaded or transcribed is stated (``voice_notes.failed_line``), so the
    agent never sees an empty message. Transcribed for the sender's account
    with the platform's one transcription; an anonymous sender's audio is
    stated, not transcribed. The audio stays the stored attachment it already
    is; the transcript is not logged.
    """
    from app.services.agent_accounts import voice_notes
    from app.services.file_storage import file_storage

    audio_files = [ref for ref in files if voice_notes.is_audio(ref.content_type, ref.filename)]
    audio_missed = [att for att in missed if voice_notes.is_audio(att.content_type, att.filename)]
    if not audio_files and not audio_missed:
        return
    where = f"{inbound.channel.value} message"
    lines: list[str] = []
    for ref in audio_files:
        if user is None:
            lines.append(voice_notes.failed_line("voice notes are transcribed for a known sender only"))
            continue
        try:
            audio = await file_storage.download(ref.s3_object_name)
        except Exception as exc:
            logger.info("%s: a stored voice note could not be read: %s", where, type(exc).__name__)
            lines.append(voice_notes.failed_line("the audio could not be read"))
            continue
        lines.append(await voice_notes.heard_line(db, user.id, audio, ref.filename, where=where))
    lines.extend(voice_notes.failed_line("the audio could not be downloaded") for _att in audio_missed)
    text = inbound.text or ""
    only_audio = len(audio_files) + len(audio_missed) == len(files) + len(missed)
    if only_audio and text.strip() == FILE_ONLY_PLACEHOLDER:
        text = ""
    inbound.text = voice_notes.with_voice_notes(text, lines)


async def _bot_governing_turn(
    db, agent, channel, fallback: ChannelBot | None
) -> ChannelBot | None:
    """The bot whose approval switch governs this turn.

    ``_get_channel_bot`` picks *a* connected bot of the provider, ordered by
    ``is_company_managed`` — which every channel bot is, so the ordering decides
    nothing and the pick is arbitrary among several. The agent's tools come from
    somewhere else entirely: ``channel_config[<channel>].channel_bot_id``, which
    is what ``load_channel_bot_tools`` reads.

    With two connected Slack bots that difference is not cosmetic. This override
    disables approval for the *whole turn*, so reading the arbitrary one meant
    switching off bot A could run bot B's conversations unattended, and
    switching off the bot somebody meant could do nothing at all.

    Falls back to the provider pick when the agent names no bot for this
    channel, which is the pre-existing behaviour for a single-bot deployment.
    """
    config = getattr(agent, "channel_config", None) or {}
    configured = (config.get(channel.value) or {}).get("channel_bot_id")
    if not configured:
        return fallback
    if fallback is not None and str(fallback.id) == str(configured):
        return fallback
    bot_id = safe_uuid(str(configured))
    if bot_id is None:
        return fallback
    return await db.scalar(select(ChannelBot).where(ChannelBot.id == bot_id))


#: The most digits a ``Content-Length`` may have before ``int()`` reads it: an
#: absurd value is left to the streamed count.
_MAX_CONTENT_LENGTH_DIGITS = 20

#: The content codings a channel file is inflated from, under the size cap,
#: with the zlib windows each is tried with, in order. The download asks for
#: ``identity``; a server that encodes the body anyway with another coding is
#: refused rather than decoded without a bound. "deflate" is
#: a zlib stream by the RFC and sometimes a bare one in practice (the same
#: table as the MCP configuration fetch's ``_CONTENT_CODING_WBITS``).
_CODING_WBITS: dict[str, tuple[int, ...]] = {
    "gzip": (zlib.MAX_WBITS | 16,),
    "x-gzip": (zlib.MAX_WBITS | 16,),
    "deflate": (zlib.MAX_WBITS, -zlib.MAX_WBITS),
}

#: How much of a body is kept to retry it with the next window: a zlib header
#: fails within its first bytes.
_INFLATE_RETRY_HEAD_BYTES = 64 * 1024


#: The content endpoints a channel file download follows a redirect from. The
#: Chat one is the prefix of every ``google.chat_media_url()`` (pinned by a test).
_REDIRECTING_CONTENT_ENDPOINTS = ("https://graph.microsoft.com/", "https://chat.googleapis.com/v1/media/")


class ChannelFileEncodingError(Exception):
    """A channel file arrived in a content coding the bounded read does not inflate."""


async def _download_channel_file(
    att: InboundFileAttachment, max_bytes: int
) -> tuple[bytes, str] | None:
    """The bytes of *att* and its response's ``Content-Type``; ``None`` when it is over *max_bytes*.

    The read never holds more than *max_bytes* + 1 bytes of the file: a size
    the platform declared or a ``Content-Length`` over the limit stops it
    before the body, and the body is read raw and counted as it is stored - a
    gzip or deflate body inflated a bounded piece at a time, since one small
    compressed chunk can inflate to hundreds of megabytes - so the count stops
    it as soon as it passes the limit (a Teams attachment declares no size,
    and httpx's timeout is per read, not a total). Raises what the request
    raises, an HTTP error status included, and ``ChannelFileEncodingError``.
    """
    import httpx

    if isinstance(att.size, int) and att.size > max_bytes:
        _log_channel_file_too_large(att.size, max_bytes, att.content_type)
        return None
    # follow_redirects only for Microsoft Graph content endpoints (e.g. Teams
    # /shares/{token}/driveItem/content), which reply 302 to a
    # pre-authenticated download location — without following it the
    # "download" silently captured an empty redirect body — and Google's
    # media endpoint (a Google Chat upload), which may answer the same way.
    # httpx drops the Authorization header on the cross-origin hop, so the
    # bearer token never leaks to the target.  All other attachment URLs keep
    # redirects disabled (platform CDN links serve bytes directly) to avoid
    # widening the SSRF/redirect surface.
    follow = att.url.startswith(_REDIRECTING_CONTENT_ENDPOINTS)
    headers = {"Accept-Encoding": "identity", **(att.auth_headers or {})}
    async with (
        httpx.AsyncClient(timeout=30, follow_redirects=follow) as client,
        client.stream("GET", att.url, headers=headers) as resp,
    ):
        resp.raise_for_status()
        coding = (resp.headers.get("content-encoding") or "identity").strip().lower()
        if coding != "identity" and coding not in _CODING_WBITS:
            raise ChannelFileEncodingError(f"unsupported content encoding {coding[:40]!r}")
        # A Content-Length counts the encoded body: it is the stored size only
        # when the body is not content-encoded.
        declared = resp.headers.get("content-length") or ""
        if (
            coding == "identity"
            and declared.isascii()
            and declared.isdigit()
            and len(declared) <= _MAX_CONTENT_LENGTH_DIGITS
            and int(declared) > max_bytes
        ):
            _log_channel_file_too_large(int(declared), max_bytes, att.content_type)
            return None
        windows = list(_CODING_WBITS.get(coding, ()))
        inflater = zlib.decompressobj(windows.pop(0)) if windows else None
        # The body read so far while nothing is inflated yet, to retry it with
        # the next window (a bare deflate stream fails the zlib header).
        head = bytearray()
        # A compressed body of a file within the limit is barely larger than it
        # (deflate adds 5 bytes per 16 KB stored block): the raw read is capped
        # too, or a stream of empty blocks would be read forever.
        max_raw = max_bytes + max_bytes // 100 + 1024
        data = bytearray()
        raw_total = 0
        async for raw in resp.aiter_raw():
            raw_total += len(raw)
            if raw_total > max_raw:
                _log_channel_file_too_large_stream(max_bytes, att.content_type)
                return None
            pending = raw
            while pending:
                if inflater is None:
                    piece, pending = pending, b""
                else:
                    try:
                        piece = inflater.decompress(pending, max_bytes - len(data) + 1)
                    except zlib.error:
                        if not windows or data or len(head) > _INFLATE_RETRY_HEAD_BYTES:
                            raise
                        inflater = zlib.decompressobj(windows.pop(0))
                        pending, head = bytes(head) + pending, bytearray()
                        continue
                    if not data and not piece and windows:
                        head += pending[: len(pending) - len(inflater.unconsumed_tail)]
                    pending = inflater.unconsumed_tail
                if piece:
                    data += piece
                    if len(data) > max_bytes:
                        _log_channel_file_too_large_stream(max_bytes, att.content_type)
                        return None
        if inflater is not None:
            data += inflater.flush()
            if len(data) > max_bytes:
                _log_channel_file_too_large_stream(max_bytes, att.content_type)
                return None
        return bytes(data), resp.headers.get("content-type", "")


def _log_channel_file_too_large(size: int, max_bytes: int, content_type: str) -> None:
    logger.warning("Channel file too large (%d bytes > %d): %s", size, max_bytes, content_type)


def _log_channel_file_too_large_stream(max_bytes: int, content_type: str) -> None:
    logger.warning("Channel file too large (over %d bytes): %s", max_bytes, content_type)


def _log_url_host(url: str) -> str:
    """What a log line may say of an attachment URL: its host.

    The URL itself carries the file name (Slack) or the bot token (Telegram),
    and a log line never carries the name the sender gave a file either, nor
    the object key, which keeps whatever follows the name's last dot.
    """
    try:
        return urlsplit(url).hostname or "?"
    except ValueError:
        return "?"


@dataclass(frozen=True, slots=True)
class _TurnRoute:
    """Who answers a message that passed the router's gates.

    ``agent``: that agent's model answers. ``general_assistant``: the General
    Assistant's model answers. Neither: the router answers with ``text``, or
    stays silent when ``suppress`` is set.
    """

    agent: Agent | None = None
    general_assistant: bool = False
    text: str = ""
    suppress: bool = False

    @property
    def runs_model(self) -> bool:
        return self.agent is not None or self.general_assistant


class _ProbeLog(logging.LoggerAdapter):
    """The router's log while it only probes a route (:func:`direct_message_refusal`): DEBUG."""

    def log(self, level: int, msg: object, *args: object, **kwargs: object) -> None:
        self.logger.log(logging.DEBUG, msg, *args, **kwargs)


_PROBE_LOG = _ProbeLog(logger, {})


async def _route_turn(
    db: AsyncSession,
    inbound: InboundMessage,
    *,
    target_agent_id: uuid.UUID | None,
    channel_bot: ChannelBot | None,
    is_profile_override: bool,
    is_anonymous: bool,
    is_dm: bool,
    user: User | None,
    no_agent_receives: bool = False,
    probe: bool = False,
    bot_whitelist_passed: bool = True,
    conversation_channel: str | None = None,
) -> _TurnRoute:
    """Step 6's choice, made before any model runs.

    *conversation_channel*: where the conversation started, ``None`` for one
    this message starts on its channel.

    An agent that no longer exists, is disabled, does not take this channel or
    may not publish on it, and a General Assistant not enabled for the channel,
    are answered by the router itself - said in a private conversation, kept
    silent in a group channel. ``handle_message`` asks it once, right after
    routing: the licence refusal and step 6 read the same answer. A
    message off the bot's receive whitelist (``bot_whitelist_passed`` false,
    let in because some agent bypasses it) is for an agent with
    ``bypass_whitelist`` only: the bound agent without it declines, and the
    General Assistant (or the agent standing in for it) stays silent, as the
    whitelist would have.

    Logged at DEBUG when a group message was not for the bot, at INFO when the
    router answers a person itself, at WARNING only for a configuration that
    cannot work: a conversation bound to an agent that does not receive on its
    channel, or an @mention nobody can answer (``no_agent_receives``: routing
    found no agent receiving on the channel at all, not one that declined).
    A *probe* decides the same way and logs at DEBUG: no message was sent.
    """
    log: logging.Logger | logging.LoggerAdapter = _PROBE_LOG if probe else logger
    in_group = inbound.channel != ChannelType.WEB and not _is_private_conversation(
        inbound
    )
    not_for_the_bot = logging.DEBUG if in_group else logging.INFO
    if target_agent_id:
        agent = (
            await db.execute(select(Agent).where(Agent.id == target_agent_id))
        ).scalar_one_or_none()
        if not agent:
            log.warning(
                "Bound agent %s no longer exists for %s conversation (channel_thread_id=%s)",
                target_agent_id,
                inbound.channel.value,
                _log_thread_id(inbound),
            )
            # For non-DM external channel messages, suppress silently —
            # avoid posting error messages for every ambient group message.
            if in_group:
                return _TurnRoute(suppress=True)
            return _TurnRoute(text=notice("agent_unavailable", notice_language(user)))
        if not agent.enabled:
            log.warning(
                "Bound agent '%s' (%s) is disabled for %s message (channel_thread_id=%s)",
                agent.name,
                agent.id,
                inbound.channel.value,
                _log_thread_id(inbound),
            )
            if in_group:
                return _TurnRoute(suppress=True)
            return _TurnRoute(text=notice("agent_disabled", notice_language(user)))
        if not is_profile_override and not _agent_allows_channel(
            agent, inbound.channel, inbound, channel_bot, bot_whitelist_passed=bot_whitelist_passed
        ):
            # Profile-override agents bypass the channel_config
            # check — the user explicitly chose this agent for
            # their DMs, so requiring channel routing config on
            # the agent would be confusing and unnecessary.
            _, reason = _agent_allows_channel_reason(
                agent, inbound.channel, inbound, channel_bot, bot_whitelist_passed=bot_whitelist_passed
            )
            # Asked without the message, the check reads only the agent's
            # channel settings: does it take this channel at all.
            if _agent_allows_channel(agent, inbound.channel):
                log.log(
                    not_for_the_bot,
                    "Bound agent '%s' (%s) declined the %s message: %s (channel_thread_id=%s)",
                    agent.name,
                    agent.id,
                    inbound.channel.value,
                    reason,
                    _log_thread_id(inbound),
                )
            else:
                log.warning(
                    "Conversation bound to agent '%s' (%s), which does not receive "
                    "%s messages: %s (channel_thread_id=%s)",
                    agent.name,
                    agent.id,
                    inbound.channel.value,
                    reason,
                    _log_thread_id(inbound),
                )
            if in_group:
                return _TurnRoute(suppress=True)
            return _TurnRoute(
                text=notice(
                    "agent_not_on_channel", notice_language(user), channel=inbound.channel.value
                )
            )
        if not is_profile_override and not _agent_can_publish(agent, inbound.channel):
            # Same bypass for profile-override: publish check is
            # irrelevant for personal DM routing.
            log.info(
                "Agent '%s' has publish disabled for %s — monitoring only",
                agent.name,
                inbound.channel.value,
            )
            return _TurnRoute(suppress=True)
        return _TurnRoute(agent=agent)

    # No agent matched — fall back to General Assistant
    if not bot_whitelist_passed:
        log.log(
            not_for_the_bot,
            "The %s message is off the bot's receive whitelist and no agent that "
            "bypasses it took it: nobody answers (channel_thread_id=%s)",
            inbound.channel.value,
            _log_thread_id(inbound),
        )
        return _TurnRoute(suppress=True)
    if is_anonymous:
        log.warning(
            "Anonymous %s message reached GA fallback — no agent bound (channel_thread_id=%s)",
            inbound.channel.value,
            _log_thread_id(inbound),
        )
        # Anonymous users cannot use the General Assistant.
        # For non-DM external channel messages, suppress silently.
        if in_group:
            return _TurnRoute(suppress=True)
        return _TurnRoute(text=notice("no_suitable_agent", notice_language(user)))
    if not is_dm and not await _ga_allows_channel(db, inbound.channel):
        if in_group and no_agent_receives and _is_bot_mentioned(inbound):
            log.warning(
                "A %s @mention went unanswered: no agent receives %s messages and "
                "the General Assistant is not enabled for them (channel_thread_id=%s)",
                inbound.channel.value,
                inbound.channel.value,
                _log_thread_id(inbound),
            )
        else:
            log.log(
                not_for_the_bot,
                "The General Assistant is not enabled for %s: nobody answers the "
                "message from user %s (channel_thread_id=%s)",
                inbound.channel.value,
                user.id if user else "?",
                _log_thread_id(inbound),
            )
        # Non-DM external channel messages: suppress silently —
        # the bot should not send error messages into group channels.
        if in_group:
            return _TurnRoute(suppress=True)
        return _TurnRoute(
            text=notice(
                "assistant_not_on_channel", notice_language(user), channel=inbound.channel.value
            )
        )
    if user is None:
        log.error("Non-anonymous GA fallback but user is None")
        return _TurnRoute(text=notice("account_unidentified", notice_language()))
    # The agent linked to the General Assistant answers in its place,
    # on the channels the General Assistant's settings allow: the conversation
    # stays unbound, so the next message is routed here again. A web
    # conversation continued here keeps the General Assistant, as on the web.
    from app.services.general_assistant_link import turn_stand_in

    stand_in = await turn_stand_in(
        db,
        agent_id=None,
        conversation_channel=conversation_channel or inbound.channel.value,
        user_id=user.id,
    )
    if stand_in is not None:
        return _TurnRoute(agent=stand_in)
    return _TurnRoute(general_assistant=True)


def _direct_message_probe(channel: ChannelType) -> InboundMessage:
    """A direct message to the bot of *channel*, as its adapter hands one over.

    The thread id is a placeholder of the right kind: a receive whitelist
    lists channels, never a person's direct-message channel.
    """
    if channel == ChannelType.TEAMS:
        return InboundMessage(
            channel=channel,
            text="",
            channel_thread_id="a:availability-probe",
            metadata={"conversation_type": "personal"},
        )
    return InboundMessage(
        channel=channel,
        text="",
        channel_user_id="D0PROBE",
        channel_thread_id="D0PROBE:0",
        metadata={"channel_type": "im", "is_threaded_reply": True},
    )


async def direct_message_refusal(
    db: AsyncSession,
    channel: ChannelType,
    *,
    agent_id: uuid.UUID | None,
    user: User,
) -> str | None:
    """Why a direct message to the bot would not be answered, or ``None``.

    The checks :func:`handle_message` runs on a message of a thread a
    conversation is continued in, not a copy of them: the bot's receive
    whitelist (``allow_private``, an agent's ``bypass_whitelist``),
    ``only_mention``, then the route the conversation's agent - the General
    Assistant when *agent_id* is ``None`` - takes (:func:`_route_turn`), with
    whether the message got past the whitelist or only in for the agents
    that bypass it. ``"bot"``: the bot drops the message, or lets it in for
    other agents only. ``"agent"``: nobody answers it.
    """
    probe = _direct_message_probe(channel)
    channel_bot = await _get_channel_bot(db, channel)
    received = await _bot_receives(db, channel_bot, probe)
    if received is None or _only_mention_gates(channel_bot, probe):
        return "bot"
    route = functools.partial(
        _route_turn,
        db,
        probe,
        target_agent_id=agent_id,
        channel_bot=channel_bot,
        is_profile_override=False,
        is_anonymous=False,
        is_dm=_is_direct_message(probe),
        user=user,
        probe=True,
    )
    if (await route(bot_whitelist_passed=received)).runs_model:
        return None
    if not received and (await route(bot_whitelist_passed=True)).runs_model:
        return "bot"
    return "agent"


def _licence_refusal_outbound(
    inbound: InboundMessage, language: str | None = None
) -> OutboundMessage:
    """The reply to a message a model would have answered while agentic AI is
    not licensed: the chat gate's own sentence, in *language* (the
    platform default when the sender is not known yet)."""
    logger.info(
        "AI is not licensed: refusing inbound %s message from %s",
        inbound.channel,
        _log_user_id(inbound),
    )
    return OutboundMessage(
        text=chat_unavailable_notice(language or notice_language()),
        channel_thread_id=inbound.channel_thread_id,
        channel_user_id=inbound.channel_user_id,
    )


#: At most one warning per window while the gate cannot be read: an
#: outage would otherwise log once per inbound message.
_UNREADABLE_STATE_LOG_INTERVAL_SECONDS = 60.0
_unreadable_state_logged_at: float | None = None
_unreadable_state_unlogged = 0


def _log_unreadable_state(inbound: InboundMessage, *, private: bool) -> None:
    """Warn that inbound messages go unprocessed, rate-limited, content-free."""
    global _unreadable_state_logged_at, _unreadable_state_unlogged
    now = time.monotonic()
    if (
        _unreadable_state_logged_at is not None
        and now - _unreadable_state_logged_at < _UNREADABLE_STATE_LOG_INTERVAL_SECONDS
    ):
        _unreadable_state_unlogged += 1
        return
    logger.warning(
        "Could not read whether agentic AI may run: inbound %s %s not processed "
        "(%d more since the last notice); direct messages are told the AI "
        "service is temporarily unavailable, group channels get no reply",
        inbound.channel.value,
        "direct message" if private else "group message",
        _unreadable_state_unlogged,
    )
    _unreadable_state_logged_at = now
    _unreadable_state_unlogged = 0


def _unreadable_state_outbound(inbound: InboundMessage) -> OutboundMessage:
    """The reply when whether agentic AI may run could not be read.

    Not a refusal, and nothing is processed: a direct message is told the AI
    service is temporarily unavailable, a group channel gets silence. The web
    channel is always addressed to the bot, like a direct message.
    """
    private = inbound.channel == ChannelType.WEB or _is_private_conversation(inbound)
    _log_unreadable_state(inbound, private=private)
    if not private:
        return OutboundMessage(
            text="",
            channel_thread_id=inbound.channel_thread_id,
            channel_user_id=inbound.channel_user_id,
            suppress=True,
        )
    return OutboundMessage(
        text=notice("ai_temporarily_unavailable", notice_language()),
        channel_thread_id=inbound.channel_thread_id,
        channel_user_id=inbound.channel_user_id,
    )


def _reply_redacted_for_display(
    fn: Callable[..., Awaitable[OutboundMessage]],
) -> Callable[..., Awaitable[OutboundMessage]]:
    """The reply with each secret variable value the model echoed replaced by the display text.

    The channel adapter sends it once the turn returns, to readers who cannot
    read the variables: never the value, never a restorable marker.
    """

    @functools.wraps(fn)
    async def redacted(*args: Any, **kwargs: Any) -> OutboundMessage:
        outbound = await fn(*args, **kwargs)
        return replace(
            outbound,
            text=redact_known_secrets_for_display(outbound.text),
            metadata=redact_known_secrets_for_display(outbound.metadata),
        )

    return redacted


async def _hand_on_cut_turn(turn: ChannelTurn, inbound: InboundMessage) -> None:
    """Flush the cut turn's checkpoint and put its message back, before its lock goes.

    A shutdown finalizer: the drain cancelling the turn again cannot stop it,
    and the lifespan shutdown waits for it while Redis and the database are
    up. Bounded by ``CUT_HANDOFF_SECONDS``; past it, the drain still waits.
    """
    from app.core.shutdown_finalizers import start_shutdown_finalizer, wait_for_shutdown_finalizer
    from app.services.channel_continuation import CUT_HANDOFF_SECONDS, hand_on_cut_turn

    async def _hand_on() -> None:
        if turn.checkpoint is not None:
            await turn.checkpoint.aclose(flush=True)
        await hand_on_cut_turn(turn, inbound)

    task = start_shutdown_finalizer(_hand_on(), name=f"channel-turn-cut:{turn.conversation_id}")
    with contextlib.suppress(asyncio.CancelledError):
        await wait_for_shutdown_finalizer(task, CUT_HANDOFF_SECONDS)


def _holds_channel_turn(
    handler: Callable[..., Awaitable[OutboundMessage]],
) -> Callable[..., Awaitable[OutboundMessage]]:
    """Hand *handler* the turn lock of its message, released once it returned.

    The lock a message takes on a conversation the web also reaches
    is given back here, whichever way the handling returned: after the reply
    was stored, before the adapter delivers it. A message the queue drain
    runs arrives with the lock already taken for it, and adopts it here.
    """

    @functools.wraps(handler)
    async def _with_turn(
        inbound: InboundMessage, conversation_id: uuid.UUID | None = None
    ) -> OutboundMessage:
        from app.services.channel_continuation import ChannelTurn

        turn = ChannelTurn(inbound.channel.value)
        turn.adopt_queued()
        hand_over = True
        try:
            return await handler(inbound, conversation_id, turn=turn)
        except asyncio.CancelledError as cut:
            # Cut by a platform update (a rolling deploy, a restart): its
            # message goes back to its queue for the next process, and the
            # sender is told, never left in silence.
            if turn.held and is_graceful_shutdown_cancellation(cut):
                hand_over = False
                await _hand_on_cut_turn(turn, inbound)
            raise
        finally:
            await turn.release(hand_over=hand_over)

    return _with_turn


@_holds_channel_turn
@known_secrets_scoped()
@_reply_redacted_for_display
async def handle_message(
    inbound: InboundMessage,
    conversation_id: uuid.UUID | None = None,
    *,
    turn: ChannelTurn,
) -> OutboundMessage:
    """Handle an inbound message end-to-end.

    This is the main entry point for all channels. It:
      1. Resolves the Copilot user.
      2. Finds or creates a conversation.
      3. Stores the user message.
      4. Loads conversation history.
      5. Builds LLM context.
      6. Runs the agentic loop.
      7. Stores the assistant response.
      8. Returns an OutboundMessage for the channel to deliver.

    Refuses outright when AI is disabled or the deployment is not
    licensed for it (Community Edition). This is the single choke point
    for every external channel — Slack, Teams, Telegram, Discord, WhatsApp and
    Google Chat all arrive here — and FR-010 names this case: the refusal covers
    "integration-driven calls", and "no entrypoint may bypass it". Gating here
    rather than in each adapter is why one check covers six of them, and gating
    per adapter is how this surface was missed in the first place.

    Answered rather than dropped: the sender is a person waiting on a bot, and
    silence is indistinguishable from the bot being broken (SC-011). The reply
    goes out before the conversation or the inbound message is stored, so a
    refused message leaves no half-written state. The licence refusal answers
    with the chat gate's own sentence, the one the REST chat routes return,
    and only where a model would have answered: every routing rule runs first
    (receive whitelist, only-mention, muted thread, anonymous access, the
    routed agent's own channel rules, the General Assistant's channels), so a
    deployment that dropped to Community Edition stays silent wherever the bot
    stays silent while it is licensed. A state that could not be read (the
    switch or the licence) is not a refusal and nothing is processed: a direct
    message is told the AI service is temporarily unavailable, a group channel
    gets silence.
    """
    from redis.exceptions import RedisError
    from sqlalchemy.exc import SQLAlchemyError

    from app.services.agentic_gate import AI_DISABLED_CODE, agentic_verdict

    try:
        async with async_session() as _gate_db:
            verdict = await agentic_verdict(_gate_db)
    except (SQLAlchemyError, RedisError, OSError):
        # A store failure the gate did not absorb (TimeoutError is an OSError)
        # is a state that could not be read too; anything else still surfaces.
        return _unreadable_state_outbound(inbound)
    if verdict.transient:
        return _unreadable_state_outbound(inbound)
    if verdict.refusal == AI_DISABLED_CODE:
        logger.info(
            "AI is disabled: refusing inbound %s message from %s",
            inbound.channel,
            _log_user_id(inbound),
        )
        return OutboundMessage(
            text=notice("ai_disabled", notice_language()),
            channel_thread_id=inbound.channel_thread_id,
            channel_user_id=inbound.channel_user_id,
        )
    # Not licensed: decided once routing has said whether a model
    # would answer this message (below).
    licence_refused = verdict.refusal is not None

    async with async_session() as db:
        # 0. Bot-level receive whitelist (external channels only).
        #    If the channel bot restricts which platform channels it
        #    processes, silently drop messages from non-whitelisted ones —
        #    UNLESS at least one agent has bypass_whitelist for this channel.
        channel_bot: ChannelBot | None = None
        bot_whitelist_passed = True
        if inbound.channel != ChannelType.WEB:
            channel_bot = await _get_channel_bot(db, inbound.channel)
            _received = await _bot_receives(db, channel_bot, inbound)
            if _received is not True:
                # Not on the whitelist: through only when an agent bypasses it.
                if _received is False:
                    bot_whitelist_passed = False  # allow through, but tag it
                    logger.info(
                        "Bot whitelist would block %s message but an agent has bypass_whitelist — allowing through",
                        inbound.channel.value,
                    )
                else:
                    logger.info(
                        "Dropping %s message — channel ID not in bot receive whitelist",
                        inbound.channel.value,
                    )
                    return OutboundMessage(
                        text="",
                        channel_thread_id=inbound.channel_thread_id,
                        channel_user_id=inbound.channel_user_id,
                        suppress=True,
                    )

        # 0b. Only-mention gate: if the bot requires explicit @mention
        #     and this is a group channel message without a mention, drop it.
        #     Private/DM conversations always bypass this check.
        #     Thread continuations (threads where the bot previously responded)
        #     also bypass this check — the bot keeps monitoring those threads.
        if _only_mention_gates(channel_bot, inbound):
            # Check for thread continuation before rejecting
            _thread_key = _get_thread_key(inbound)
            if _thread_key and await _is_thread_active(inbound.channel, _thread_key):
                logger.info(
                    "Allowing %s message in active thread (only_mention "
                    "enabled but thread previously activated, thread_key=%s)",
                    inbound.channel.value,
                    log_id(inbound.channel, _thread_key),
                )
                if inbound.metadata is None:
                    inbound.metadata = {}
                inbound.metadata["is_thread_continuation"] = True
            else:
                logger.info(
                    "Dropping %s message — only_mention enabled on bot and "
                    "bot was not @mentioned (channel_thread_id=%s)",
                    inbound.channel.value,
                    _log_thread_id(inbound),
                )
                return OutboundMessage(
                    text="",
                    channel_thread_id=inbound.channel_thread_id,
                    channel_user_id=inbound.channel_user_id,
                    suppress=True,
                )

        # 0b'. Thread continuation detection: even if the bot-level
        #      only_mention didn't fire, pre-compute the thread continuation
        #      flag for agent-level only_mention checks downstream.
        if (
            inbound.channel != ChannelType.WEB
            and not _is_private_conversation(inbound)
            and not _is_bot_mentioned(inbound)
            and not (inbound.metadata or {}).get("is_thread_continuation")
        ):
            _thread_key = _get_thread_key(inbound)
            if _thread_key and await _is_thread_active(inbound.channel, _thread_key):
                if inbound.metadata is None:
                    inbound.metadata = {}
                inbound.metadata["is_thread_continuation"] = True
                logger.info(
                    "Detected %s thread continuation (thread_key=%s)",
                    inbound.channel.value,
                    log_id(inbound.channel, _thread_key),
                )

        # 0c. Reset command: /new or /reset archives the current
        #     conversation so the next message starts a fresh session.
        if inbound.channel != ChannelType.WEB and _is_reset_command(inbound.text):
            reset_user: User | None = None
            if inbound.user_email:
                reset_user = await resolve_user_by_email(db, inbound.user_email)
            # Fallback: channel identity resolution (e.g. WhatsApp phone, Telegram ID)
            if reset_user is None and inbound.channel_user_id:
                reset_user = await resolve_user_by_channel_identity(
                    db,
                    inbound.channel.value,
                    inbound.channel_user_id,
                )
            return await _handle_reset(db, reset_user, inbound)

        # 0d. Stop command: /stop mutes the bot in the current thread.
        if inbound.channel != ChannelType.WEB and _is_stop_command(inbound.text):
            return await _handle_stop(inbound)

        # 0e. Muted-thread gate: if the thread was previously muted via
        #     /stop, silently drop the message — unless the bot is
        #     explicitly @mentioned, which lifts the mute.
        if inbound.channel != ChannelType.WEB:
            _mute_thread_key = _get_thread_key(inbound)
            if _mute_thread_key and await _is_thread_muted(
                inbound.channel, _mute_thread_key
            ):
                if _is_bot_mentioned(inbound):
                    await _unmute_thread(inbound.channel, _mute_thread_key)
                    logger.info(
                        "Thread unmuted via @mention: %s (thread_key=%s)",
                        inbound.channel.value,
                        log_id(inbound.channel, _mute_thread_key),
                    )
                else:
                    logger.info(
                        "Dropping %s message — thread is muted " "(thread_key=%s)",
                        inbound.channel.value,
                        log_id(inbound.channel, _mute_thread_key),
                    )
                    return OutboundMessage(
                        text="",
                        channel_thread_id=inbound.channel_thread_id,
                        channel_user_id=inbound.channel_user_id,
                        suppress=True,
                    )

        # Not licensed, web: always addressed to the bot, and the web
        # path stores the conversation and the message before routing, so it
        # is answered here. External channels are answered after routing.
        if licence_refused and inbound.channel == ChannelType.WEB:
            return _licence_refusal_outbound(inbound)

        # 1. Resolve user — try email first, then channel identity linkage
        _is_ext = inbound.channel != ChannelType.WEB
        user: User | None = None
        if inbound.user_email:
            user = await resolve_user_by_email(db, inbound.user_email)

        # Fallback: cross-channel identity resolution
        if user is None and _is_ext and inbound.channel_user_id:
            user = await resolve_user_by_channel_identity(
                db,
                inbound.channel.value,
                inbound.channel_user_id,
            )

        # Auto-link: if user was found by email but has a channel_user_id,
        # persist the link for future cross-channel recognition.
        if user is not None and _is_ext and inbound.channel_user_id:
            await link_channel_identity(
                db,
                user,
                inbound.channel.value,
                inbound.channel_user_id,
            )

        is_anonymous = user is None

        # Prompt injection scan (defense-in-depth). Authenticated web chat
        # scans in agent_chat/general_chat; external channels are scanned
        # here. Known senders are logged only (matching chat behaviour);
        # anonymous senders with high-severity detections are blocked.
        if _is_ext:
            _injection_scan = _scan_inbound_message(
                inbound, is_anonymous=is_anonymous
            )
            _injection_blocked = is_anonymous and _injection_scan.severity == "high"
            if _injection_scan.flagged:
                # Record the detection in the audit trail, not just the log.
                # External-channel injections were previously log-only, so an AI
                # red-team run over Slack / Teams left no queryable evidence -
                # and the OpenAEV xtm-one collector, which reads the audit log
                # to fill DETECTION expectations, saw nothing. This closes the
                # DETECTION gap; PREVENTION cannot be scored from here, since
                # suppression only ever applies to anonymous senders and those
                # have no audit identity (see _log_channel_injection_event).
                await _log_channel_injection_event(
                    db,
                    inbound=inbound,
                    user=user,
                    scan=_injection_scan,
                    blocked=_injection_blocked,
                )
            if _injection_blocked:
                logger.warning(
                    "Blocked anonymous %s message — high-severity prompt "
                    "injection (channel_thread_id=%s)",
                    inbound.channel.value,
                    _log_thread_id(inbound),
                )
                # Suppress silently in group channels (same convention as
                # the anonymous-access rejections below).
                if not _is_private_conversation(inbound):
                    return OutboundMessage(
                        text="",
                        channel_thread_id=inbound.channel_thread_id,
                        channel_user_id=inbound.channel_user_id,
                        suppress=True,
                    )
                return OutboundMessage(
                    text=notice("injection_refused", notice_language()),
                    channel_thread_id=inbound.channel_thread_id,
                    channel_user_id=inbound.channel_user_id,
                )

        candidate_agent: Agent | None = None
        if is_anonymous:
            # Bot-level anonymous check first (hard constraint — if the
            # bot doesn't allow anonymous, no agent can override it).
            if not channel_bot or not channel_bot.allow_anonymous:
                logger.log(
                    logging.INFO if _is_private_conversation(inbound) else logging.DEBUG,
                    "Anonymous %s message not processed: anonymous access is not "
                    "enabled on the channel bot (channel_bot=%s, channel_thread_id=%s)",
                    inbound.channel.value,
                    channel_bot.id if channel_bot else None,
                    _log_thread_id(inbound),
                )
                # For non-DM external channel messages, suppress silently —
                # the bot should not send error messages into group channels
                # where the message was not directed at the bot.
                if not _is_private_conversation(inbound):
                    return OutboundMessage(
                        text="",
                        channel_thread_id=inbound.channel_thread_id,
                        channel_user_id=inbound.channel_user_id,
                        suppress=True,
                    )
                return OutboundMessage(
                    text=notice(
                        "account_unregistered",
                        notice_language(),
                        product=get_cached_branding().name,
                    ),
                    channel_thread_id=inbound.channel_thread_id,
                    channel_user_id=inbound.channel_user_id,
                )
            # Bot allows anonymous — find a suitable agent (checks
            # agent-level allow_anonymous + channel routing internally).
            candidate_agent = await _resolve_channel_agent_anonymous(
                db,
                inbound.channel,
                inbound,
                channel_bot=channel_bot,
            )
            if candidate_agent is None:
                logger.log(
                    logging.INFO if _is_private_conversation(inbound) else logging.DEBUG,
                    "Anonymous %s message not processed: no agent takes it "
                    "(channel_thread_id=%s)",
                    inbound.channel.value,
                    _log_thread_id(inbound),
                )
                # For non-DM external channel messages, suppress silently.
                if not _is_private_conversation(inbound):
                    return OutboundMessage(
                        text="",
                        channel_thread_id=inbound.channel_thread_id,
                        channel_user_id=inbound.channel_user_id,
                        suppress=True,
                    )
                return OutboundMessage(
                    text=notice("no_suitable_agent", notice_language()),
                    channel_thread_id=inbound.channel_thread_id,
                    channel_user_id=inbound.channel_user_id,
                )

        # 2. Find or create conversation
        #    For external channels we *defer* creation until we know the
        #    agent will actually respond.  This avoids polluting the chat
        #    list with conversations for channel noise the agent ignores
        #    (join events, off-topic messages, etc.).
        is_external_channel = inbound.channel != ChannelType.WEB
        conv: Conversation | None = None
        # A direct-message thread a conversation was continued in:
        # it keeps its agent and session tools whatever the DM routing says.
        _bound_conv = False

        if is_external_channel:
            # For bare channel IDs (Teams root-level channel messages
            # without ;messageid= suffix), skip the conversation lookup
            # entirely.  The bare channel ID is shared by ALL root-level
            # messages in the channel, so reusing the conversation would
            # load stale history from unrelated prior interactions.
            _skip_conv_lookup = _is_bare_channel_id(inbound)
            if _skip_conv_lookup:
                logger.info(
                    "Skipping conversation lookup for bare channel ID "
                    "(channel_thread_id=%s) — will create fresh conversation",
                    _log_thread_id(inbound)[:60],
                )

            _allow_shared_thread = _should_allow_shared_thread(inbound)

            # Try to find an existing conversation only (no creation yet).
            if _skip_conv_lookup:
                conv = None
            elif is_anonymous:
                conv = await _find_existing_external_conversation(
                    db,
                    None,
                    inbound.channel,
                    inbound.channel_thread_id,
                    allow_shared_thread=_allow_shared_thread,
                    agent_id=candidate_agent.id if candidate_agent else None,
                )
            elif user is not None:
                if _is_private_conversation(inbound):
                    conv = await _find_bound_conversation(db, inbound, user)
                    _bound_conv = conv is not None
                if conv is None:
                    conv = await _find_existing_external_conversation(
                        db,
                        user.id,
                        inbound.channel,
                        inbound.channel_thread_id,
                        allow_shared_thread=_allow_shared_thread,
                    )
            else:
                logger.error("Non-anonymous external channel message but user is None")
                return OutboundMessage(
                    text=notice("account_unidentified", notice_language()),
                    channel_thread_id=inbound.channel_thread_id,
                    channel_user_id=inbound.channel_user_id,
                )
        else:
            # Web channels: create immediately (unchanged behaviour).
            if is_anonymous:
                conv = await _find_or_create_anonymous_conversation(
                    db,
                    inbound.channel,
                    inbound.channel_thread_id,
                    agent_id=candidate_agent.id if candidate_agent else None,
                )
            elif user is not None:
                if conversation_id:
                    result = await db.execute(
                        select(Conversation).where(
                            Conversation.id == conversation_id,
                            Conversation.user_id == user.id,
                        )
                    )
                    conv = result.scalar_one_or_none()
                    if not conv:
                        conv = await find_or_create_conversation(
                            db, user.id, inbound.channel, inbound.channel_thread_id
                        )
                else:
                    conv = await find_or_create_conversation(
                        db,
                        user.id,
                        inbound.channel,
                        inbound.channel_thread_id,
                    )
            else:
                logger.error("Non-anonymous web message but user is None")
                return OutboundMessage(
                    text=notice("account_unidentified", notice_language()),
                    channel_thread_id=inbound.channel_thread_id,
                    channel_user_id=inbound.channel_user_id,
                )

        if conv is not None:
            # Before anything of this turn is stored: its secret markers are this conversation's.
            bind_known_secrets(conv.id)

        # 3. Store user message (web only; external channels deferred to post-processing)
        _needs_ai_title = False
        if not is_external_channel:
            if conv is None:
                logger.error(
                    "Web conversation is None after creation — this should not happen"
                )
                return OutboundMessage(
                    text=notice("conversation_failed", notice_language(user)),
                    channel_thread_id=inbound.channel_thread_id,
                    channel_user_id=inbound.channel_user_id,
                )
            _needs_ai_title = await _store_user_message(db, conv, inbound)

        # 3b. A conversation the web also reaches: a Slack or Teams
        #     direct message of its owner, or a thread it was continued in.
        #     One turn at a time on it whatever the channel: a message that
        #     finds the turn taken is queued and answered as the next turn,
        #     and a tool call left waiting for approval is retired as a web
        #     send retires it. The other channels are not continued across
        #     surfaces and keep running each message as it comes.
        from app.services.channel_continuation import CONTINUABLE_CHANNELS

        _cross_surface = (
            inbound.channel.value in CONTINUABLE_CHANNELS
            and conv is not None
            and user is not None
            and getattr(conv, "user_id", None) == user.id
            and (_bound_conv or _is_private_conversation(inbound))
        )
        _approval_retired = False
        if turn.held and (not _cross_surface or turn.conversation_id != str(conv.id)):
            # A queued message whose thread stopped continuing the conversation
            # it was queued for (/new, Stop, a Teams chat re-pointed): never
            # answered in another one. That conversation's next message, if
            # any, gets the turn.
            await turn.release()
            logger.info(
                "Queued %s message not answered: its thread no longer continues "
                "conversation %s",
                inbound.channel.value,
                turn.conversation_id,
            )
            return OutboundMessage(
                text=notice("channel_queued_elsewhere", notice_language(user)),
                channel_thread_id=inbound.channel_thread_id,
                channel_user_id=inbound.channel_user_id,
            )
        if _cross_surface:
            from app.services.channel_continuation import (
                claim_channel_message,
                retire_pending_approval,
                take_turn_or_queue,
            )

            if not turn.held:
                if not turn.queued and not await claim_channel_message(inbound):
                    logger.info(
                        "%s message already received for conversation %s: not answered twice",
                        inbound.channel.value,
                        conv.id,
                    )
                    return OutboundMessage(
                        text="",
                        channel_thread_id=inbound.channel_thread_id,
                        channel_user_id=inbound.channel_user_id,
                        suppress=True,
                    )
                _queue_notice = await take_turn_or_queue(
                    turn,
                    conv.id,
                    inbound,
                    # Not licensed: refused when it runs, so no file
                    # of it is stored.
                    store_files=None
                    if licence_refused
                    else functools.partial(_store_queued_files, db, inbound, user),
                )
                if _queue_notice is not None:
                    return OutboundMessage(
                        text=localized_reply(_queue_notice, notice_language(user)),
                        channel_thread_id=inbound.channel_thread_id,
                        channel_user_id=inbound.channel_user_id,
                    )
            _approval_retired = await retire_pending_approval(db, conv, inbound.channel.value)
            turn.start()

        # 4. Load history
        history = await get_conversation_history(db, conv.id) if conv else []
        if not is_external_channel:
            # Stored at step 3: run_agent_chat appends the turn itself.
            history = without_current_turn(history, inbound.text)
        _ch_replay: list[ChatMessage] = []
        if turn.held and conv is not None:
            # The tool work of a turn a shutdown cut goes right before this
            # message (appended after compaction, 5b), and this turn checkpoints
            # its own, so a cut resumes from its last completed tool call, as on the web.
            from app.api.v1.chat import turn_offers_code_interpreter
            from app.services import turn_checkpoint

            # The resume note names attachments/ only when this turn can call the code interpreter (#2677).
            _ch_user_id = getattr(user, "id", None)
            _ch_resumed, _ch_replay = await turn_checkpoint.load_replay(
                db,
                conv.id,
                user_id=_ch_user_id,
                code_interpreter=await turn_offers_code_interpreter(db, conv, _ch_user_id),
            )
            turn.checkpoint = turn_checkpoint.TurnCheckpoint(
                conv.id,
                resumed=_ch_resumed,
                user_id=str(user.id) if user is not None else None,
            )

        # 5. Route to an agent.
        #    For non-web channels, check which agent has receive_enabled
        #    for this channel type in its channel_config before falling
        #    back to the General Assistant.
        #    We track target_agent_id separately so routing works even
        #    when conversation creation is deferred (conv may be None).
        #
        #    IMPORTANT: For DMs on external channels, we ALWAYS re-resolve
        #    routing even if the conversation already has an agent binding.
        #    This ensures user profile overrides (channel_agent_config)
        #    take effect immediately — without it, the override is only
        #    evaluated on the first message in a conversation, and
        #    long-lived channels like Teams 1:1 would stay bound to a
        #    stale agent forever.
        target_agent_id: uuid.UUID | None = conv.agent_id if conv else None
        _is_dm = _is_direct_message(inbound)
        # Track whether the agent was resolved via a user profile
        # override.  Profile-override agents bypass the channel_config
        # check — the user explicitly chose this agent for their DMs,
        # so it should work without configuring channel routing on the
        # agent itself.
        _is_profile_override = False
        _no_agent_receives = False
        # A conversation continued from another surface keeps its agent - the
        # General Assistant included.
        _should_resolve = not _bound_conv and (
            not target_agent_id  # no binding yet
            or (_is_dm and not is_anonymous)  # DM: always re-check profile override
        )
        if _should_resolve and inbound.channel != ChannelType.WEB:
            if is_anonymous:
                # For anonymous users, use the candidate agent already resolved
                routed_agent = candidate_agent
            elif user is not None:
                _agent_declines: list[str] = []
                routed_agent = await _resolve_channel_agent(
                    db,
                    user.id,
                    inbound.channel,
                    inbound,
                    channel_bot=channel_bot,
                    bot_whitelist_passed=bot_whitelist_passed,
                    declined_out=_agent_declines,
                )
                _no_agent_receives = routed_agent is None and not _agent_declines
                # Check if this was resolved via the user's profile
                # override (Pass 0 in _resolve_channel_agent).
                if (
                    routed_agent
                    and _is_dm
                    and user.channel_agent_config
                    and user.channel_agent_config.get(inbound.channel.value)
                    and str(routed_agent.id)
                    == user.channel_agent_config.get(inbound.channel.value)
                ):
                    _is_profile_override = True
            else:
                routed_agent = None
            if routed_agent:
                # Re-bind conversation if the resolved agent differs
                if target_agent_id and routed_agent.id != target_agent_id:
                    logger.info(
                        "Re-binding %s conversation from agent %s to '%s' (%s) — user profile override changed",
                        inbound.channel.value,
                        target_agent_id,
                        routed_agent.name,
                        routed_agent.id,
                    )
                target_agent_id = routed_agent.id
                if conv:
                    conv.agent_id = routed_agent.id
                logger.info(
                    "Routed %s message to agent '%s' (%s)%s%s",
                    inbound.channel.value,
                    routed_agent.name,
                    routed_agent.id,
                    " (anonymous)" if is_anonymous else "",
                    " (profile override)" if _is_profile_override else "",
                )
            elif _is_dm and target_agent_id and user is not None:
                # DM re-resolution returned None (no profile override).
                # Keep the binding only if the bound agent is visible to
                # the user (legitimate GA handover).  Clear it otherwise
                # so the GA handles the conversation with the user's own
                # config — prevents stale cross-user agent bindings.
                _bound_visible = await db.execute(
                    select(Agent.id).where(
                        Agent.id == target_agent_id,
                        Agent.enabled.is_(True),
                        user_visible_filter(
                            Agent,
                            user.id,
                            await get_user_group_ids(db, user.id),
                        ),
                    )
                )
                if not _bound_visible.scalar_one_or_none():
                    logger.info(
                        "Clearing stale %s DM agent binding %s — agent not "
                        "visible to user %s, falling back to General Assistant",
                        inbound.channel.value,
                        target_agent_id,
                        user.id,
                    )
                    target_agent_id = None
                    if conv:
                        conv.agent_id = None

        # Who answers (step 6's choice), decided once, before files, backfill
        # or compaction run or anything is stored.
        _channel_errored = False
        try:
            route = await _route_turn(
                db,
                inbound,
                target_agent_id=target_agent_id,
                channel_bot=channel_bot,
                is_profile_override=_is_profile_override,
                is_anonymous=is_anonymous,
                is_dm=_is_dm,
                user=user,
                no_agent_receives=_no_agent_receives,
                bot_whitelist_passed=bot_whitelist_passed,
                conversation_channel=getattr(conv, "channel", None),
            )
        except Exception as e:
            logger.error("Error handling message: %s", format_exception_message(e), exc_info=True)
            route = _TurnRoute(text=notice("processing_error", notice_language(user)))
            _channel_errored = True
        # The agent whose turn this is: the conversation's, or the one
        # standing in for the General Assistant, which leaves the
        # conversation unbound. Compaction, the @mentions and the activity
        # entry are read for it.
        answering_agent_id = route.agent.id if route.agent is not None else target_agent_id

        # Not licensed, external channels: answered only where a
        # model would have answered - past every routing rule above and the
        # routed agent's (or General Assistant's) own channel rules - and
        # before files, backfill or compaction run or anything is stored.
        # Where no model would have run, the router's own answer (or silence)
        # is exactly what it is on a licensed deployment.
        if licence_refused:
            if route.runs_model:
                return _licence_refusal_outbound(inbound, notice_language(user))
            return OutboundMessage(
                text=route.text,
                channel_thread_id=inbound.channel_thread_id,
                channel_user_id=inbound.channel_user_id,
                suppress=route.suppress,
            )

        # Build structured context so the agent knows the platform, channel
        # type (DM / private / public), and any adapter-specific metadata.
        message_context = _build_message_context(inbound)
        if is_anonymous and message_context is not None:
            message_context["is_anonymous"] = True

        # For anonymous users, use a synthetic user ID so downstream code
        # (LLM model resolution, memory) degrades gracefully to defaults.
        effective_user_id = str(user.id) if user else "anonymous"

        # 4b. Resolve channel file attachments → FileRef list.
        #     Downloads from platform, uploads to S3, creates FileAttachment.
        _missed_files: list[InboundFileAttachment] = []
        channel_files = await _resolve_channel_files(
            db, inbound, effective_user_id, missed_out=_missed_files
        )
        if turn.stored_file_ids and user is not None:
            # A queued message: its files were stored when it was queued.
            channel_files = [
                *channel_files,
                *await _stored_channel_files(
                    db, turn.stored_file_ids, user.id, inbound.channel.value
                ),
            ]
        # 4c. Voice notes: the audio the message carries, heard as text in
        #     the turn (and in the stored user message), never dropped.
        await _hear_voice_notes(db, inbound, channel_files, _missed_files, user)
        if channel_files:
            logger.info(
                "Resolved %d channel file(s) for %s message",
                len(channel_files),
                inbound.channel.value,
            )

        # The turn's FileContext, built as the chat services build their own
        # (the conversation's files plus this message's), held here so the
        # files the turn produced can be shown on the reply.
        from app.services.agent_chat import load_conversation_files

        turn_file_context = await load_conversation_files(
            db, str(conv.id) if conv else None, channel_files or None
        )
        _initial_file_ids = {ref.file_id for ref in turn_file_context.list_files()}

        # 5a. Channel backfill: load prior messages from the platform API
        #     so the agent has context about the ongoing thread.
        #
        #     For channel threads (Teams channels, Slack public channels)
        #     we always refresh the thread context — the DB history only
        #     contains the bot's own messages, not the full thread with
        #     other participants.  For DMs / group chats, backfill only
        #     runs on the first message (no DB history yet).  A Slack DM
        #     thread reply the web also reaches is a direct message: its DB
        #     history holds every message, the web turns included.
        if is_external_channel and message_context is not None:
            _is_channel_thread = _is_channel_thread_message(inbound) and not _cross_surface
            if _is_channel_thread or not history:
                backfill = await _load_channel_backfill(inbound)
                if backfill:
                    message_context["prior_conversation"] = backfill
                    # For channel threads, the platform API backfill is
                    # the authoritative source of context (it includes
                    # all participants' messages, including the bot's
                    # own).  Discard DB history so stale messages from
                    # previous unrelated interactions in the same channel
                    # don't drown out the thread context.
                    if _is_channel_thread and history:
                        logger.info(
                            "Discarding %d DB history messages for channel "
                            "thread — backfill is authoritative",
                            len(history),
                        )
                        history = []

        # 5b. Apply conversation memory compaction for external channels.
        #     The web REST path already does this (chat.py), but external
        #     channels (Slack, Teams, WhatsApp, Telegram) were sending raw
        #     history — this caused stale context bleed in long-lived
        #     conversations (especially Teams personal chats where the
        #     channel_thread_id is permanent).
        if is_external_channel and history:
            history = await _compact_history(
                db,
                history,
                effective_user_id=effective_user_id,
                agent_id=answering_agent_id,
                conversation_id=conv.id if conv else None,
            )
        # After compaction, as on the web: its cut keeps the last messages
        # and would split a replayed tool call from its result.
        if _ch_replay:
            history = [*history, *_ch_replay]

        # 5c. @mentions, when the deployment turned them on:
        #     the other agents an identified user names as ``@key`` answer
        #     after the routed one (6c), each on an agentic action of its own -
        #     read now, so the routed agent's run reports whether the quota
        #     charged or refused it (``quota_out``).
        from app.services.mention_dispatch import TurnMetering as _TurnMetering

        _channel_metering = _TurnMetering()
        _channel_mentioned = await _read_channel_mentions(
            db,
            inbound,
            user,
            channel_bot=channel_bot,
            bot_whitelist_passed=bot_whitelist_passed,
            is_external_channel=is_external_channel,
            is_anonymous=is_anonymous,
        )
        _channel_meters_mentions = any(i != str(answering_agent_id) for i in _channel_mentioned)
        # Who answers after the routed agent, read on the message it
        # answers, as on the web chat; 6c resolves them again once it has run.
        _channel_agents_after = (
            await _channel_mention_targets(
                db, user, _channel_mentioned, answering_agent_id, notice_language(user)
            )
            if _channel_meters_mentions
            else []
        )

        # 6. Build LLM context and run
        suppress = False
        refused_without_account = False
        # A conversation the web also reaches: the turn runs with the
        # session tools its web turns saved, shows on its open web streams and
        # stops on the web Stop.
        _turn_kwargs: dict = {}
        if _cross_surface and conv is not None:
            from app.services.chat_saved_tool_selection import session_tool_kwargs
            from app.services.chat_selection import usable_saved_selection

            # The agent's defaults added since the web save join it, as the
            # web does on reopen: an integration bound since then has tools.
            # An id unbound since the save, which its owner could not bind
            # themselves, is left out.
            _saved = await usable_saved_selection(db, user_id=conv.user_id, conversation=conv)
            _turn_kwargs.update(session_tool_kwargs(_saved, route.agent))
        if turn.held:
            _turn_kwargs.update(on_event=turn.on_event, cancel_event=turn.cancel_event)
            if turn.checkpoint is not None:
                _turn_kwargs["on_checkpoint"] = turn.checkpoint.offer
        try:
            if route.agent is not None:
                # Conversation bound to an agent — use full agentic chat
                from app.services.agent_chat import (
                    QUOTA_REFUSAL_ERROR_CODES,
                    run_agent_chat,
                )
                from app.tools.approval import OUTCOME_BOT_UNINTERRUPTED

                turn_bot = await _bot_governing_turn(
                    db, route.agent, inbound.channel, channel_bot
                )
                # A bot an administrator switched to uninterrupted runs the
                # whole turn without pausing, not merely its own tools:
                # this surface cannot collect a decision, so a gated tool
                # called through a bot is otherwise simply unusable.

                turn_errors: dict = {}
                assistant_text, transfer_agent_id = await run_agent_chat(
                    db,
                    effective_user_id,
                    route.agent,
                    history,
                    inbound.text,
                    context=message_context,
                    files=channel_files or None,
                    conversation_id=str(conv.id) if conv else None,
                    file_context=turn_file_context,
                    quota_out=_channel_metering.quota if _channel_meters_mentions else None,
                    agents_after=_channel_agents_after,
                    error_out=turn_errors,
                    # The reason travels with the decision so the trail names
                    # this bot rather than reading as an ungated agent.
                    hitl_off_outcome=(
                        OUTCOME_BOT_UNINTERRUPTED
                        if turn_bot is not None
                        and not getattr(turn_bot, "requires_approval", True)
                        else None
                    ),
                    **_turn_kwargs,
                )
                # A turn with no account the quota gate refused reached
                # no model: it is answered where a model would have answered,
                # like the licence refusal, and gets no generated title either.
                refused_without_account = (
                    is_anonymous
                    and turn_errors.get("error_code") in QUOTA_REFUSAL_ERROR_CODES
                )
                # If the agent transferred, re-bind conversation
                if transfer_agent_id:
                    target_agent_id = answering_agent_id = uuid.UUID(transfer_agent_id)
                    if conv:
                        conv.agent_id = uuid.UUID(transfer_agent_id)
            elif route.general_assistant and user is not None:
                # General chat with agent handover
                from app.services.general_chat import run_general_chat

                # No ``request_approval``: a bot turn is a single inbound
                # message with no way to hold the turn open and ask, so a
                # gated call unwinds here. ``run_agent_chat`` catches
                # its own pause and returns the explanation; the general
                # assistant re-raises it for its callers to handle, and
                # without this the broad handler below told the user "I
                # encountered an error" for the gate working as designed —
                # while the same tool on an agent, on this same channel,
                # explained itself.
                from app.tools.base import HumanInputRequestedError

                try:
                    assistant_text, new_agent_id = await run_general_chat(
                        db,
                        str(user.id),
                        history,
                        inbound.text,
                        files=channel_files or None,
                        conversation_id=str(conv.id) if conv else None,
                        file_context=turn_file_context,
                        quota_out=_channel_metering.quota if _channel_meters_mentions else None,
                        agents_after=_channel_agents_after,
                        **_turn_kwargs,
                    )
                except HumanInputRequestedError as _ch_hitl:
                    from app.tools.approval import no_approval_channel_message

                    logger.info(
                        "General Assistant stopped for approval on %s: %s",
                        inbound.channel.value,
                        _ch_hitl.question,
                    )
                    assistant_text = no_approval_channel_message(_ch_hitl)
                    new_agent_id = None

                # If handover occurred, bind the conversation to the agent
                if new_agent_id:
                    target_agent_id = answering_agent_id = uuid.UUID(new_agent_id)
                    if conv:
                        conv.agent_id = uuid.UUID(new_agent_id)
            else:
                # The router answers itself (or stays silent in a group).
                suppress = route.suppress
                assistant_text = route.text

        except Exception as e:
            logger.error("Error handling message: %s", format_exception_message(e), exc_info=True)
            assistant_text = notice("processing_error", notice_language(user))
            _channel_errored = True

        # 6b. Check if the agent decided to remain silent
        _routed_silent = False
        if not suppress and _is_no_response(assistant_text):
            suppress = True
            _routed_silent = True
            logger.info(
                "Agent returned [NO_RESPONSE] for %s message — suppressing reply",
                inbound.channel.value,
            )

        # The files the routed agent's turn produced; a file it only
        # fetched goes out when the reply names it.
        _delivered_ids = set(
            files_for_reply(
                turn_file_context,
                [
                    ref.file_id
                    for ref in turn_file_context.list_files()
                    if ref.file_id not in _initial_file_ids
                ],
                assistant_text,
            )
        )
        _turn_files = [
            ref for ref in turn_file_context.list_files() if ref.file_id in _delivered_ids
        ]
        # The routed agent's trace, read before any mentioned agent runs; none
        # of its own when it never reached the quota gate (it did not run).
        _ext_trace_id = await _load_llm_trace_id(str(conv.id)) if conv else None
        if _channel_meters_mentions and not _channel_metering.quota:
            _ext_trace_id = None

        # 6c. @mentioned agents: each other agent the message names
        #     as ``@key`` answers in turn after the conversation's own, and the
        #     replies go out together, one labelled section per agent.
        _mention_replies: list = []
        _assistant_metadata: dict | None = None
        # A notice a shared service wrote in English goes out in the reader's
        # language; the conversation stores it as written.
        _outbound_source = localized_reply(assistant_text, notice_language(user))
        # A routed agent that chose silence knew who answers after it:
        # the agents the user named still answer.
        if (
            _channel_mentioned
            and is_external_channel
            and (not suppress or _routed_silent)
            and user is not None
            and not is_anonymous
        ):
            try:
                _mention_replies, _assistant_metadata, _composed = await _answer_channel_mentions(
                    db,
                    inbound,
                    user=user,
                    channel_bot=channel_bot,
                    answering_agent_id=answering_agent_id,
                    first_reply=_outbound_source,
                    history=history,
                    message_context=message_context,
                    channel_files=channel_files,
                    conversation_id=str(conv.id) if conv else None,
                    mentioned=_channel_mentioned,
                    metering=_channel_metering,
                    bot_whitelist_passed=bot_whitelist_passed,
                    # A reply the routed agent did not write (an error, a
                    # "not available" line) is a note, not its words.
                    first_failed=_channel_errored or not _channel_metering.quota,
                    first_silent=_routed_silent,
                    file_context=turn_file_context,
                )
                if _composed:
                    _outbound_source = _composed
            except Exception:
                logger.warning(
                    "Mentioned agents could not answer a %s message; only the "
                    "conversation's agent answered",
                    inbound.channel.value,
                    exc_info=True,
                )
                _mention_replies, _assistant_metadata = [], None
        # The routed agent left the message to the agents the user named:
        # the turn is theirs - their replies are stored and sent, its
        # silence is not, and the last reply is the turn's answer.
        _store_routed_reply = True
        # The agent the activity entry names: the one whose reply it shows.
        _activity_agent_id = answering_agent_id
        if _routed_silent and _mention_replies:
            suppress = False
            _store_routed_reply = False
            assistant_text = _mention_replies[-1].text
            _activity_agent_id = safe_uuid(_mention_replies[-1].target.agent_id) or answering_agent_id
            _turn_files = []

        # An agent's run wrote under the channel's format override (Slack's
        # mrkdwn) and its reply is stored as written: marked, so the web
        # converts that reply only. The General Assistant and the
        # router's own answers are Markdown or plain text.
        _reply_format = (
            override_content_format(message_context.get("platform"))
            if route.agent is not None and not _channel_errored and message_context
            else None
        )
        if _reply_format:
            _assistant_metadata = {**(_assistant_metadata or {}), CONTENT_FORMAT_META_KEY: _reply_format}

        # 7. Post-processing: persist messages, log activity, commit.
        _stored: dict = {}
        conv = await _finalize_message(
            db,
            inbound,
            user=user,
            conv=conv,
            target_agent_id=target_agent_id,
            answering_agent_id=_activity_agent_id,
            is_anonymous=is_anonymous,
            is_external_channel=is_external_channel,
            suppress=suppress,
            assistant_text=assistant_text,
            needs_ai_title=_needs_ai_title,
            ai_title=not refused_without_account,
            llm_trace_id=_ext_trace_id,
            user_files=channel_files or None,
            turn_files=_turn_files or None,
            assistant_metadata=_assistant_metadata,
            mention_replies=_mention_replies,
            store_reply=_store_routed_reply,
            stored_out=_stored,
        )
        if turn.checkpoint is not None and conv is not None:
            # Answered: the work it kept for a resume goes with it. A turn
            # that failed leaves its work for the next one, as on the web.
            await turn.checkpoint.aclose(flush=_channel_errored)
            if not _channel_errored and turn.checkpoint.may_hold_row:
                from app.services import turn_checkpoint

                await turn_checkpoint.discard(db, conv.id)
                await db.commit()
        # The exchange is stored: the conversation's web viewers get it.
        turn.show_user_message(_stored.get("user_message"))
        turn.finish(
            message_id=_stored.get("assistant_message_id"),
            content="" if suppress else assistant_text,
            content_format=_reply_format,
        )

        # 9. Return outbound — resolve internal image URLs and [[FILE:…]]
        #    download markers for external channels (web chat renders the
        #    markers as cards in the frontend; channels need real links).
        outbound_text = _outbound_source
        outbound_meta = {"conversation_id": str(conv.id)} if conv else {}
        if is_external_channel and assistant_text and not suppress:
            # The files the turn produced that the reply does not name: the
            # chips the web chat shows under a reply, as links - the
            # mentioned agents' after the routed agent's, in the one message.
            _outbound_files = _turn_files
            _mention_file_ids = {i for _reply in _mention_replies for i in _reply.file_ids}
            if _mention_file_ids:
                _outbound_files = [
                    *_turn_files,
                    *(r for r in turn_file_context.list_files() if r.file_id in _mention_file_ids),
                ]
            from app.channels.base import (
                channel_image_scope,
                file_scheme_images_to_api,
                resolve_channel_dashboard_markers,
                resolve_channel_file_markers,
                resolve_channel_images,
                resolve_channel_presentation_markers,
                resolve_channel_report_markers,
                resolve_channel_video_markers,
                strip_channel_investigation_markers,
                strip_channel_work_item_markers,
                trusted_media_hosts,
            )
            from app.core.config import settings as _cfg
            from app.services.chat_charts import charts_as_tables

            # ``![alt](file:<uuid>)`` names the file the internal path names:
            # signed below like any image of the turn, never doubled by a
            # produced-file link.
            _outbound_source = file_scheme_images_to_api(_outbound_source)
            _outbound_source = _with_produced_file_markers(_outbound_source, _outbound_files)
            # A platform page named without its scheme is a link the reader
            # can click, not plain text (#10115).
            from app.services.outbound_links import bare_platform_links_as_urls

            _outbound_source = bare_platform_links_as_urls(_outbound_source)

            # Scope the marker resolver to this conversation/user so an
            # LLM-emitted marker can never mint a signed public link for an
            # arbitrary FileAttachment UUID outside the requester's context.
            outbound_text = await resolve_channel_file_markers(
                db,
                _outbound_source,
                _cfg.base_url,
                conversation_id=conv.id if conv else None,
                user_id=user.id if user else None,
                language=notice_language(user),
            )
            # Interactive decks have no channel representation — link to the
            # web-app viewer instead of leaking the raw marker.
            outbound_text = await resolve_channel_presentation_markers(
                db,
                outbound_text,
                _cfg.frontend_url,
                conversation_id=conv.id if conv else None,
                user_id=user.id if user else None,
                language=notice_language(user),
            )
            # Same for structured reports — the document lives behind the
            # authenticated viewer, so the channel gets a link.
            outbound_text = await resolve_channel_report_markers(
                db,
                outbound_text,
                _cfg.frontend_url,
                conversation_id=conv.id if conv else None,
                user_id=user.id if user else None,
                language=notice_language(user),
            )
            # An investigation has no standalone viewer route, so its marker
            # is stripped rather than linked — see the helper's docstring.
            outbound_text = strip_channel_investigation_markers(outbound_text)
            # A channel draws no chart: a chart block is sent as its table.
            outbound_text = charts_as_tables(outbound_text)
            # A video plays in the Video Studio page: the channel gets its link.
            outbound_text = await resolve_channel_video_markers(
                db,
                outbound_text,
                _cfg.frontend_url,
                conversation_id=conv.id if conv else None,
                user_id=user.id if user else None,
                language=notice_language(user),
            )
            # A dashboard is a web page of widgets: the channel gets its link.
            outbound_text = await resolve_channel_dashboard_markers(
                db,
                outbound_text,
                _cfg.frontend_url,
                conversation_id=conv.id if conv else None,
                user_id=user.id if user else None,
                language=notice_language(user),
            )
            # A scheduled work item's card is a web chat surface.
            outbound_text = strip_channel_work_item_markers(outbound_text)
            # Same scope for images: only a file or an image of this
            # conversation or its user gets a signed link.
            image_scope = await channel_image_scope(
                db,
                outbound_text,
                conversation_id=conv.id if conv else None,
                user_id=user.id if user else None,
            )
            # An image on a media integration's host goes out as an image too.
            outbound_text, embedded_images = resolve_channel_images(
                outbound_text,
                _cfg.base_url,
                allowed_ids=image_scope,
                media_hosts=trusted_media_hosts(),
            )
            if embedded_images:
                outbound_meta["embedded_images"] = [
                    {
                        "image_id": img.image_id,
                        "alt_text": img.alt_text,
                        "public_url": img.public_url,
                        "source": img.source,
                    }
                    for img in embedded_images
                ]
        if _approval_retired and outbound_text and not suppress:
            retired = notice("channel_approval_retired", notice_language(user))
            outbound_text = f"{retired}\n\n{outbound_text}"

        return OutboundMessage(
            text=outbound_text,
            channel_thread_id=inbound.channel_thread_id
            or (str(conv.id) if conv else ""),
            channel_user_id=inbound.channel_user_id,
            metadata=outbound_meta,
            suppress=suppress,
        )


# ---------------------------------------------------------------------------
# Channel availability helpers
# ---------------------------------------------------------------------------


def _extract_channel_id(inbound: InboundMessage) -> str | None:
    """Extract the platform channel/group ID from an inbound message.

    Used for channel-level receive scoping against channel_config targets.
    """
    if inbound.channel == ChannelType.SLACK:
        # Slack channel_thread_id is "channel_id" or "channel_id:thread_ts";
        # channel_user_id is the sender's member id, never a channel.
        thread_id = inbound.channel_thread_id or ""
        return thread_id.split(":")[0] or None
    if inbound.channel == ChannelType.TEAMS:
        meta = inbound.metadata or {}
        team_id = meta.get("team_id", "")
        channel_id = meta.get("channel_id", "")
        if team_id and channel_id:
            return f"{team_id}:{channel_id}"

        # Teams: conversation_id in channel_thread_id.
        # Personal (1:1) chats use a per-user conversation ID (e.g. "a:xxx")
        # which never matches configured receive_channels (channel IDs like
        # "19:xxx@thread.tacv2").  Returning None bypasses receive_channels
        # scoping so DMs always reach the agent.
        #
        # Detection: prefer metadata.conversation_type when available,
        # fall back to the conversation ID prefix — personal chat IDs
        # never start with "19:" (they start with "a:" or similar).
        conv_type = meta.get("conversation_type", "")
        thread_id = inbound.channel_thread_id or ""
        is_personal = conv_type == "personal" or (
            not conv_type and thread_id and not thread_id.startswith("19:")
        )
        if is_personal:
            return None
        # Group chats (multi-party private chats) have per-chat conversation
        # IDs that cannot be enumerated by the channel picker (whitelists
        # only ever contain team channels), so a configured whitelist would
        # permanently block every group chat.  Return None so group chats
        # bypass receive_channels scoping — they remain gated by
        # allow_private / only_mention / agent routing like DMs.
        if conv_type == "groupChat":
            return None
        return inbound.channel_thread_id
    if inbound.channel == ChannelType.WHATSAPP:
        # WhatsApp: group_id for groups, phone number for DMs.
        # channel_thread_id already holds the correct value (set by the
        # adapter: group_id for groups, sender phone for DMs).
        return inbound.channel_thread_id
    if inbound.channel == ChannelType.TELEGRAM:
        # Telegram: chat_id
        return inbound.channel_thread_id
    if inbound.channel == ChannelType.DISCORD:
        # Discord: channel ID (set by the adapter as channel_thread_id)
        return inbound.channel_thread_id
    if inbound.channel == ChannelType.GOOGLE_CHAT:
        # Google Chat: space name ("spaces/AAA...")
        return inbound.channel_thread_id
    return None


# Backwards-compatible alias — the implementation moved to channels/base.py
# so the publish-whitelist intersection in services/integration_tools.py can
# share it without importing the router.
_teams_channel_component = teams_channel_component


def _channel_id_matches(
    channel: ChannelType,
    incoming_id: str,
    allowed_ids: set[str],
) -> bool:
    """Return True when *incoming_id* matches a whitelist entry.

    Exact match first; for Teams, fall back to comparing the normalized
    channel-id component (see :func:`_teams_channel_component`).
    """
    if incoming_id in allowed_ids:
        return True
    if channel == ChannelType.TEAMS:
        incoming_component = _teams_channel_component(incoming_id)
        return any(
            _teams_channel_component(allowed) == incoming_component
            for allowed in allowed_ids
            if allowed
        )
    return False


def _agent_allows_channel_reason(
    agent: Agent,
    channel: ChannelType,
    inbound: InboundMessage | None = None,
    channel_bot: ChannelBot | None = None,
    *,
    bot_whitelist_passed: bool = True,
) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` explaining why the agent was accepted or rejected."""
    ch_value = channel.value

    if ch_value == "web":
        if agent.available_channel_types is None:
            return True, "web allowed (default)"
        ok = "web" in agent.available_channel_types
        return ok, "web allowed" if ok else "web not in available_channel_types"

    if agent.channel_config:
        ch_cfg = agent.channel_config.get(ch_value)
        if not ch_cfg:
            return False, f"no {ch_value} entry in channel_config"
        if not ch_cfg.get("receive_enabled", False):
            return False, "receive_enabled=false"

        if not bot_whitelist_passed and not ch_cfg.get("bypass_whitelist"):
            return False, "bot whitelist rejected and bypass_whitelist=false"

        receive_channels = ch_cfg.get("receive_channels", [])
        if receive_channels and inbound:
            if _is_private_conversation(inbound) and _resolve_allow_private(
                channel_bot, agent, channel
            ):
                return True, "private conversation bypasses receive_channels"

            incoming_channel_id = _extract_channel_id(inbound)
            if incoming_channel_id:
                allowed_ids = {ch.get("id") for ch in receive_channels if ch.get("id")}
                if not _channel_id_matches(channel, incoming_channel_id, allowed_ids):
                    return (
                        False,
                        f"channel {incoming_channel_id} not in receive_channels whitelist",
                    )

        if (
            inbound
            and _resolve_only_mention(channel_bot, agent, channel)
            and not _is_private_conversation(inbound)
            and not _is_bot_mentioned(inbound)
            and not (inbound.metadata or {}).get("is_thread_continuation")
        ):
            return (
                False,
                "only_mention=true but bot was not @mentioned (not a DM, not a thread continuation)",
            )

        return True, "all checks passed"

    if agent.available_channel_types is None:
        return False, "no channel_config and available_channel_types is null (web only)"
    ok = ch_value in agent.available_channel_types
    return ok, (
        "legacy available_channel_types"
        if ok
        else f"{ch_value} not in available_channel_types"
    )


def _agent_allows_channel(
    agent: Agent,
    channel: ChannelType,
    inbound: InboundMessage | None = None,
    channel_bot: ChannelBot | None = None,
    *,
    bot_whitelist_passed: bool = True,
) -> bool:
    """Return True if the agent accepts messages from *channel*."""
    allowed, _ = _agent_allows_channel_reason(
        agent,
        channel,
        inbound,
        channel_bot,
        bot_whitelist_passed=bot_whitelist_passed,
    )
    return allowed


async def _ga_allows_channel(db: AsyncSession, channel: ChannelType) -> bool:
    """Return True if the General Assistant accepts messages from *channel*.

    When ``channel_config`` is set, non-web channels are checked against it
    (same structure as Agent.channel_config).  Otherwise falls back to the
    legacy ``available_channel_types`` list.
    """
    from app.models.general_assistant_config import GeneralAssistantConfig

    result = await db.execute(select(GeneralAssistantConfig).limit(1))
    config = result.scalar_one_or_none()
    if not config:
        return channel == ChannelType.WEB  # Fresh install: web chat only

    ch_value = channel.value

    # Web channel: check available_channel_types (channel_config is for external only)
    if ch_value == "web":
        if config.available_channel_types is None:
            return True
        return "web" in config.available_channel_types

    # Non-web: prefer channel_config when available
    if config.channel_config:
        ch_cfg = config.channel_config.get(ch_value)
        if not ch_cfg:
            return False
        return bool(ch_cfg.get("receive_enabled", False))

    # Legacy fallback: available_channel_types
    if config.available_channel_types is None:
        return True  # Legacy rows with NULL = all channels
    return ch_value in config.available_channel_types
