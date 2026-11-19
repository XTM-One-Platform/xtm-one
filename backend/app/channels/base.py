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

"""Channel abstraction – the contract every messaging platform implements.

Architecture:
    Inbound messages (from user) → channel adapter → InboundMessage → router → agent
    Agent response → OutboundMessage → channel adapter → platform API → user
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import hmac
import inspect
import logging
import re
import secrets
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Collection
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from app.core.security import secret_key_is_guessable

# Defined in core (core never imports from channels), re-exported for the adapters.
from app.core.security import timing_safe_str_eq as timing_safe_str_eq
from app.core.shutdown_finalizers import (
    start_shutdown_finalizer,
    track_background_turn,
    wait_for_shutdown_finalizer,
)
from app.core.utils import format_exception_message, safe_uuid
from app.services.channel_notices import notice, notice_language

logger = logging.getLogger(__name__)


class ChannelType(StrEnum):
    WEB = "web"
    WEB_EMBED = "web_embed"
    SLACK = "slack"
    TEAMS = "teams"
    WHATSAPP = "whatsapp"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    GOOGLE_CHAT = "google_chat"


# Mapping from channel type (channel_config key) to bot provider key.
# Shared by agents.py (validation) and integration_tools.py (tool building).
CHANNEL_TO_BOT_PROVIDER: dict[str, str] = {
    "slack": "slack_bot",
    "teams": "teams_bot",
    "whatsapp": "whatsapp_bot",
    "telegram": "telegram_bot",
    "discord": "discord_bot",
    "google_chat": "googlechat_bot",
}


def teams_channel_component(ref: str) -> str:
    """Return the channel-id component of a Teams channel reference.

    Teams channel references exist in two formats: whitelist entries built
    from the Graph channel picker use ``{aad_group_id}:{channel_id}`` (the
    team part is the Entra ID group GUID), while Bot Framework reports the
    team as a thread-style conversation ID (``19:...@thread.tacv2``).  The
    two team components therefore NEVER match — comparing full references
    silently mismatches whitelisted Teams channels.  Channel IDs are
    globally unique, so matching is normalized to the channel component: it
    always starts with a ``19:`` marker, and everything before the *last*
    ``19:`` is the team component.  Any ``;messageid=<root>`` thread suffix
    is stripped first.

    Shared by the inbound receive-whitelist matching (``channels/router.py``)
    and the publish-whitelist intersection (``services/integration_tools.py``).
    """
    base = ref.split(";", 1)[0]
    idx = base.rfind("19:")
    if idx > 0:
        return base[idx:]
    return base


@dataclass
class InboundFileAttachment:
    """A file attachment received from an external channel.

    Channel adapters populate these from platform-specific payloads.
    The router downloads the file content, uploads to S3, and converts
    to :class:`FileRef` for the agent loop.

    Attributes:
        url: Download URL (platform-specific, may require auth headers).
        filename: Original filename (or a generated one if unknown).
        content_type: MIME type (may be empty if the platform doesn't report it).
        size: File size in bytes (0 if unknown).
        platform_file_id: Platform-specific file identifier (e.g. Slack file ID).
        auth_headers: HTTP headers required to download from ``url`` (e.g.
            ``{"Authorization": "Bearer <token>"}`` for Slack).
    """

    url: str
    filename: str = "attachment"
    content_type: str = "application/octet-stream"
    size: int = 0
    platform_file_id: str = ""
    auth_headers: dict[str, str] = field(default_factory=dict)


#: Text used when a message carries file(s) but no text (Teams, Telegram) -
#: the agent still needs a user turn to react to.
FILE_ONLY_PLACEHOLDER = "(The user shared a file without a message.)"


@dataclass
class InboundMessage:
    """A normalized user message from any channel.

    Attributes:
        channel: Which platform the message came from.
        text: Plain-text content of the message.
        user_email: User's email (for identifying the Copilot user).
        channel_user_id: The sender's own platform ID (Slack member ID, Teams
            user ID, WhatsApp number, ...), never a channel or group ID: the
            router resolves and auto-links the sender by it.
        channel_thread_id: Thread/conversation ID on the platform.
        channel_message_id: Unique message ID on the platform.
        attachments: File URLs or references (legacy, prefer file_attachments).
        file_attachments: Typed file attachment metadata from channel adapters.
        raw: Original platform payload (for debugging / advanced handling).
        agent_id: If the user explicitly invoked an agent.
        metadata: Extra key-value pairs.
    """

    channel: ChannelType
    text: str
    user_email: str | None = None
    channel_user_id: str | None = None
    channel_thread_id: str | None = None
    channel_message_id: str | None = None
    attachments: list[str] = field(default_factory=list)
    file_attachments: list[InboundFileAttachment] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    agent_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class OutboundMessage:
    """A response from the copilot to be sent back on a channel.

    Attributes:
        text: Plain-text (or Markdown) response.
        channel_thread_id: Thread to reply in (for threaded conversations).
        channel_user_id: Platform user to send to (for DMs).
        attachments: File URLs or structured blocks.
        metadata: Extra data for the channel adapter (e.g. Slack blocks).
        suppress: When True, the channel adapter should NOT send this message.
            Used when the agent decides it should remain silent (e.g. the
            message is a greeting in a public channel and the persona says
            not to respond).
    """

    text: str
    channel_thread_id: str | None = None
    channel_user_id: str | None = None
    attachments: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    suppress: bool = False


class BaseChannel(ABC):
    """Abstract base for all channel adapters."""

    channel_type: ChannelType

    # Whether the platform exposes a typing-indicator API. Adapters whose
    # ``send_typing_indicator`` is a no-op (WhatsApp, Google Chat) set this
    # to False so ``typing_keepalive`` skips the keepalive loop entirely.
    supports_typing: bool = True

    @abstractmethod
    async def send_message(self, message: OutboundMessage) -> str | None:
        """Send a message to the platform.

        Returns the platform message ID (for future edits/threading) or None.
        """
        raise NotImplementedError

    @abstractmethod
    async def send_typing_indicator(self, channel_thread_id: str | None = None) -> None:
        """Show a typing/thinking indicator on the platform."""
        raise NotImplementedError


@asynccontextmanager
async def typing_keepalive(
    channel: BaseChannel,
    channel_thread_id: str | None = None,
    interval: float = 3.0,
):
    """Async context manager that sends typing indicators at *interval* seconds.

    Usage::

        async with typing_keepalive(channel, thread_id):
            outbound = await handle_message(inbound)  # long-running

    Teams, Slack, Telegram, and WhatsApp all expire their typing indicator
    after a few seconds.  This helper keeps it alive for the full duration
    of the wrapped block so the user always sees a "typing…" state.

    Channels whose ``send_typing_indicator`` is a no-op (WhatsApp, Google
    Chat — their platform APIs expose no typing endpoint) set
    ``supports_typing = False`` so no keepalive task is spawned at all;
    otherwise a background task would wake every 3 s for the whole agent
    run doing nothing.
    """
    if not getattr(channel, "supports_typing", True):
        yield
        return

    stop = asyncio.Event()

    async def _loop() -> None:
        while not stop.is_set():
            try:
                await channel.send_typing_indicator(channel_thread_id)
            except Exception:
                logger.debug(
                    "typing_keepalive: failed to send indicator", exc_info=True
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        stop.set()
        _ = await task  # wait for keepalive loop to finish


# ── Bounded, tracked channel message processing ──────────────────────────────
# Every channel webhook acknowledges the platform with 200 immediately and
# processes the inbound message in a fire-and-forget task. Two latent problems
# with a bare ``asyncio.create_task`` for that work:
#   1. No backpressure — an inbound burst (a busy Teams/Slack tenant at the
#      start of the workday) spawns an UNBOUNDED number of concurrent tasks,
#      each of which opens its own DB session (routing + the agentic loop) and
#      contends for the small web-process connection pool. That is exactly how
#      the pool gets driven to exhaustion (and uvicorn's concurrency backstop
#      gets tripped) during the morning ramp.
#   2. The event loop only keeps a *weak* reference to a task, so a
#      fire-and-forget task can be garbage-collected mid-flight and its
#      exceptions silently swallowed.
# ``spawn_channel_task`` fixes both: a process-wide semaphore caps how many
# inbound messages are processed at once (queueing the rest), and a strong
# reference is held until completion with exceptions logged.
_channel_processing_sem: asyncio.Semaphore | None = None
_channel_tasks: set[asyncio.Task] = set()


def _get_channel_semaphore() -> asyncio.Semaphore | None:
    """Return the lazily-created channel-processing semaphore (None = uncapped).

    Created on first use so it binds to the running web-process event loop
    (the only loop that processes channel messages). ``<= 0`` disables the cap.
    """
    global _channel_processing_sem
    from app.core.config import settings

    limit = settings.channel_max_concurrent_processing
    if limit <= 0:
        return None
    if _channel_processing_sem is None:
        _channel_processing_sem = asyncio.Semaphore(limit)
    return _channel_processing_sem


#: How long an adapter waits for the notice of a turn a shutdown cut.
CUT_NOTICE_WAIT_SECONDS = 5.0


async def tell_cut_turn(inbound: InboundMessage, post: Callable[[str], Awaitable[Any]]) -> None:
    """Tell the sender a platform update cut the turn answering *inbound*: never silence.

    Called by an adapter whose processing task the shutdown drain cancelled.
    Says nothing when the router already handed the turn on and told the
    sender (``channel_continuation.CUT_HANDLED_META_KEY``). The post runs as a
    shutdown finalizer, so the drain's cancellation never reaches it. Never raises.
    """
    from app.services.channel_continuation import CUT_HANDLED_META_KEY

    if (inbound.metadata or {}).get(CUT_HANDLED_META_KEY):
        return

    async def _tell() -> None:
        await post(notice("channel_turn_cut", notice_language()))

    task = start_shutdown_finalizer(_tell(), name=f"{inbound.channel.value}-turn-cut-notice")
    with contextlib.suppress(asyncio.CancelledError):
        await wait_for_shutdown_finalizer(task, CUT_NOTICE_WAIT_SECONDS)


def spawn_channel_task(coro: Awaitable[Any], *, name: str | None = None) -> asyncio.Task:
    """Schedule inbound channel-message processing as a bounded, tracked task.

    Drop-in replacement for ``asyncio.create_task`` at channel webhook entry
    points. Bounds concurrency via a process-wide semaphore so an inbound burst
    cannot spawn unbounded concurrent DB-session-holding agentic loops and
    exhaust the connection pool, keeps a strong reference so the task is never
    garbage-collected mid-flight, and logs (instead of swallowing) any
    exception. The webhook has already returned 200, so excess messages simply
    queue on the semaphore instead of being dropped. The shutdown drain ends
    the task like a request it cuts, and a recycle waits for it.
    """
    sem = _get_channel_semaphore()

    async def _runner() -> None:
        if sem is None:
            await coro
            return
        async with sem:
            await coro

    task = asyncio.create_task(_runner(), name=name)
    _channel_tasks.add(task)
    track_background_turn(task)

    def _on_done(finished: asyncio.Task) -> None:
        _channel_tasks.discard(finished)
        if finished.cancelled():
            # The task can be cancelled before it ever awaited ``coro`` - e.g.
            # while parked on the semaphore during shutdown, or before its first
            # step ran. Close the (possibly un-started) coroutine to avoid a
            # "coroutine ... was never awaited" RuntimeWarning. The task is
            # finished here, so ``coro`` is never suspended and close() is a
            # safe no-op once it has already run to completion.
            if inspect.iscoroutine(coro):
                coro.close()
            return
        exc = finished.exception()
        if exc is not None:
            logger.error(
                "Channel processing task %s failed: %s",
                name or "channel-task",
                format_exception_message(exc),
                exc_info=exc,
            )

    task.add_done_callback(_on_done)
    return task


async def get_channel_bot(provider: str) -> Any:
    """Look up the first connected ChannelBot for *provider*, preferring company-managed.

    Returns the SQLAlchemy ``ChannelBot`` instance or ``None``.
    Shared by all channel adapters so the DB-query + ordering logic lives in one place.
    """
    from sqlalchemy import select

    from app.core.database import async_session
    from app.models.channel_bot import ChannelBot

    async with async_session() as db:
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


# ── Webhook authentication helpers ───────────────────────────────────────────

# Sentinel returned by channel credential loaders when an encrypted credential
# field EXISTS but cannot be decrypted (Fernet key rotation, corrupted row).
# It is deliberately non-empty and can never match a real secret / app id /
# hex key, so every verification path runs and fails — the webhook fails
# CLOSED (401) instead of silently degrading to unauthenticated, which is the
# correct behaviour when credentials were configured but are unreadable.
DECRYPT_FAILED_CREDENTIAL = "\x00__decrypt_failed__\x00"

_unauthenticated_warned: set[str] = set()


def warn_unauthenticated_once(key: str, message: str) -> None:
    """Log an UNAUTHENTICATED-webhook warning once per process per channel.

    Webhook endpoints are called for every inbound message — logging the
    missing-credential warning on each request drowns real warnings on a
    busy tenant. One line per process is enough to surface the gap.
    """
    if key in _unauthenticated_warned:
        return
    _unauthenticated_warned.add(key)
    logger.warning(message)


# ── Logging an inbound message ───────────────────────────────────────────────

#: The most message text a DEBUG line may carry. Nothing logged at INFO or
#: above carries message text, a sender's name or phone number.
LOG_PREVIEW_CHARS = 80


def log_preview(text: str | None) -> str:
    """A bounded ``repr`` of message text, for DEBUG log lines only."""
    if not text:
        return "(empty)"
    if len(text) <= LOG_PREVIEW_CHARS:
        return repr(text)
    return repr(text[:LOG_PREVIEW_CHARS]) + "..."


#: The key of a WhatsApp log digest while ``SECRET_KEY`` is guessable: never
#: stored, so the digests then correlate within this process only.
_PROCESS_LOG_ID_KEY = secrets.token_bytes(32)


def _log_id_key() -> bytes:
    from app.core.config import settings

    if secret_key_is_guessable(settings.secret_key):
        return _PROCESS_LOG_ID_KEY
    return hmac.new(settings.secret_key.encode(), b"xtm-one channel log id", hashlib.sha256).digest()


def log_id(channel: str, value: str | None) -> str:
    """How an identifier of *channel* appears in a log line.

    WhatsApp identifies a person by their phone number: it is their user id,
    the thread of their direct messages, and it is base64-encoded inside every
    ``wamid.`` message id. A WhatsApp identifier is logged as a keyed digest
    (``wa-`` and 16 hex digits of an HMAC-SHA256 under a key derived from
    ``SECRET_KEY``): the lines of one chat or message still correlate, and the
    number cannot be recovered by hashing candidate numbers, which a plain
    hash would allow. While ``SECRET_KEY`` is guessable (short or a published
    placeholder) the key is a random one of this process instead, so the
    digests correlate within the process only. The ids of every other channel
    are platform ids, logged as they are.
    """
    if not value:
        return "(none)"
    if channel != ChannelType.WHATSAPP:
        return str(value)
    digest = hmac.new(_log_id_key(), str(value).encode(), hashlib.sha256)
    return f"wa-{digest.hexdigest()[:16]}"


# ── Message chunking ─────────────────────────────────────────────────────────

CHANNEL_MAX_CHARS: dict[str, int] = {
    "whatsapp": 4096,
    "telegram": 4096,
    "slack": 39000,
    # The Bot Framework cap is ~28 KB of serialized payload BYTES, not
    # characters — non-ASCII text (accents, emoji, CJK) encodes to multiple
    # bytes each, so the character budget stays well below 28 000 to leave
    # headroom for the JSON envelope and multi-byte characters.
    "teams": 20000,
    "discord": 2000,
    "google_chat": 4096,
    "web": 100000,
}


def chunk_message(text: str, max_chars: int = 4096) -> list[str]:
    """Split *text* into chunks of at most *max_chars*, preserving Markdown.

    The chunker is **code-fence aware**: it never leaves a code block unclosed.
    When forced to split inside a fenced block, it closes the fence at the end
    of the current chunk and reopens it (with the same language tag) at the
    start of the next chunk.

    Break preference (best to worst):
    1. Paragraph break (blank line)
    2. Line break (single newline)
    3. Sentence boundary (. ! ? followed by space)
    4. Whitespace (space/tab)
    5. Hard break (anywhere)
    """
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remaining = text
    open_fence: str | None = None  # e.g. "```python" if inside a code block

    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break

        # Find the best split point within max_chars
        window = remaining[:max_chars]

        # Track code fence state within the window
        fence_lang = open_fence  # carry over from previous chunk
        in_fence = open_fence is not None
        lines = window.split("\n")
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("```"):
                if in_fence:
                    in_fence = False
                    fence_lang = None
                else:
                    in_fence = True
                    fence_lang = stripped  # e.g. "```python" or "```"

        # Reserve room for a possible fence-close ("\n```") so the emitted
        # chunk never overflows max_chars on hard-capped channels (Telegram /
        # Discord reject over-length messages outright).
        reserve = len("\n```") if (in_fence and fence_lang is not None) else 0
        split_limit = max(1, max_chars - reserve)

        # Try break points in preference order
        split_at = _find_split_point(window, split_limit)

        chunk = remaining[:split_at].rstrip()
        remaining = remaining[split_at:].lstrip("\n")

        # If we're inside a code fence, close it
        if in_fence and fence_lang is not None:
            chunk += "\n```"
            open_fence = fence_lang  # reopen in next chunk
        else:
            open_fence = None

        # If we need to reopen a fence from the previous chunk
        if open_fence and remaining:
            remaining = f"{open_fence}\n{remaining}"

        if chunk:
            chunks.append(chunk)

    return chunks if chunks else [text]


def _find_split_point(text: str, max_chars: int) -> int:
    """Find the best position to split *text* (which is already <= *max_chars*).

    Searches backwards from the end for the preferred break types.
    """
    # 1. Paragraph break (double newline)
    idx = text.rfind("\n\n", 0, max_chars)
    if idx > max_chars // 4:  # don't split too early
        return idx + 2

    # 2. Single newline
    idx = text.rfind("\n", 0, max_chars)
    if idx > max_chars // 4:
        return idx + 1

    # 3. Sentence boundary
    for punct in (". ", "! ", "? "):
        idx = text.rfind(punct, 0, max_chars)
        if idx > max_chars // 4:
            return idx + len(punct)

    # 4. Whitespace
    idx = text.rfind(" ", 0, max_chars)
    if idx > max_chars // 4:
        return idx + 1

    # 5. Hard break
    return max_chars


# ── Image URL resolution for external channels ──────────────────────────────

# Matches markdown images pointing to internal API paths:
#   ![alt](/api/v1/chat/images/{uuid})
#   ![alt](/api/v1/chat/files/{uuid}/download)
_INTERNAL_IMAGE_RE = re.compile(
    r"!\[([^\]]*)\]\((/api/v1/chat/(images|files)/([0-9a-fA-F\-]{36})(?:/download)?)\)"
)

# A Markdown image naming a file of the conversation by its id, the form the
# file tools document (``![alt](file:<uuid>)``). Angle brackets and a
# quoted title are accepted; a ``file:///`` path is not an id and never matches.
_FILE_SCHEME_IMAGE_RE = re.compile(
    r"!\[([^\]\n]*)\]\(\s*<?file:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})>?(?:\s+\"[^\"\n]*\")?\s*\)"
)

# A Markdown image at an https address: ``![alt](https://host/path)``, angle
# brackets and a quoted title accepted. Sent as an image only when its host is
# a trusted media host (:func:`trusted_media_hosts`).
_EXTERNAL_IMAGE_RE = re.compile(
    r"!\[([^\]\n]*)\]\(\s*<?(https://[^\s<>()]+)>?(?:\s+\"[^\"\n]*\")?\s*\)",
    re.IGNORECASE,
)

#: ``EmbeddedImage.source`` of an image at a trusted media host's address.
EXTERNAL_IMAGE_SOURCE = "external"
#: External images a reply sends at most, the rest keep their Markdown line.
MAX_EXTERNAL_CHANNEL_IMAGES = 4
#: The size an adapter that uploads bytes downloads at most for one of them.
MAX_EXTERNAL_IMAGE_BYTES = 8 * 1024 * 1024


def file_scheme_images_to_api(text: str) -> str:
    """*text* with every ``![alt](file:<uuid>)`` pointing at the file's download path.

    The path is the one :func:`resolve_channel_images` signs, scoped to the
    conversation or its user, so a channel shows the image the web
    chat shows inline; an id outside that scope keeps its alt text.
    """
    if not text or "file:" not in text.lower():
        return text
    return _FILE_SCHEME_IMAGE_RE.sub(
        lambda m: f"![{m.group(1)}](/api/v1/chat/files/{m.group(2).lower()}/download)", text
    )


@dataclass
class EmbeddedImage:
    """An image reference extracted from assistant text."""

    image_id: str
    alt_text: str
    original_path: str
    public_url: str
    source: str  # "images", "files" or EXTERNAL_IMAGE_SOURCE


# Matches [[FILE:uuid]] download-card markers emitted by file-generating
# tools (generate_file, code interpreter outputs, download_* tools).
# Case-insensitive for the same reason as the frontend FILE_MARKER_RE:
# a marker echoed back by an LLM or reformatted upstream must not leak
# through as raw text.
_FILE_MARKER_RE = re.compile(
    r"\[\[FILE:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\]\]",
    re.IGNORECASE,
)

# Matches [[PRESENTATION:uuid]] markers emitted by generate_presentation.
# Case-insensitive for the same reason as _FILE_MARKER_RE.
#
# The marker may carry the slide to land on: ``[[PRESENTATION:<uuid>
# #/<slide id>]]`` — the fragment the viewer already reads off its URL, spelled
# the same way. Group 2 is the slide id; the grammar is ``SlideId``'s
# (``presentation_spec.py``), URL-fragment safe by construction. The class
# already spells both cases, so IGNORECASE (kept for the uuid) never widens it,
# and the id is used as written — slide ids are case-sensitive.
_PRESENTATION_MARKER_RE = re.compile(
    r"\[\[PRESENTATION:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:#/([A-Za-z0-9_-]{1,64}))?\]\]",
    re.IGNORECASE,
)

# Matches [[REPORT:uuid]] markers emitted by generate_report.
_REPORT_MARKER_RE = re.compile(
    r"\[\[REPORT:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\]\]",
    re.IGNORECASE,
)

# Matches [[INVESTIGATION:uuid]] markers emitted by a Deep Investigation run.
# Case-insensitive for the same reason as _REPORT_MARKER_RE.
_INVESTIGATION_MARKER_RE = re.compile(
    r"\[\[INVESTIGATION:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\]\]",
    re.IGNORECASE,
)

# Matches [[WORK_ITEM:uuid]] markers: the card of a scheduled work item,
# appended by the turn that scheduled it and by its outcome notice.
_WORKITEM_MARKER_RE = re.compile(
    r"\[\[WORK_ITEM:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\]\]",
    re.IGNORECASE,
)

# Matches [[VIDEO:uuid]] markers emitted by the Video Studio tools.
_VIDEO_MARKER_RE = re.compile(
    r"\[\[VIDEO:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\]\]",
    re.IGNORECASE,
)

# Matches [[DASHBOARD:uuid]] markers emitted by the custom dashboard tools.
_DASHBOARD_MARKER_RE = re.compile(
    r"\[\[DASHBOARD:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\]\]",
    re.IGNORECASE,
)

#: Every artifact-marker family, for the surfaces that show a message as a
#: plain text snippet and cannot resolve a marker into anything. Kept next to
#: the individual patterns so a new family cannot be added without this tuple
#: showing up in the same diff.
_ALL_ARTIFACT_MARKER_RES = (
    _FILE_MARKER_RE,
    _PRESENTATION_MARKER_RE,
    _REPORT_MARKER_RE,
    _INVESTIGATION_MARKER_RE,
    _WORKITEM_MARKER_RE,
    _VIDEO_MARKER_RE,
    _DASHBOARD_MARKER_RE,
)
#: How each marker family of an artifact a conversation can be about opens,
#: for a conversation tool finding the conversations that carry one
#: (``list_conversations(object_type=...)``). Kept beside the patterns.
ARTIFACT_MARKER_OPENINGS: dict[str, str] = {
    "file": "[[FILE:",
    "presentation": "[[PRESENTATION:",
    "report": "[[REPORT:",
    "video": "[[VIDEO:",
}
_UUID_SRC = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def _relative_viewer_link_re(viewer_path: str) -> re.Pattern[str]:
    """Match a RELATIVE web-app viewer path (``/presentations/<uuid>#/<slide>``).

    The ``edit_presentation`` result hands the agent its slide links in that
    relative form — the shape the web chat turns into an in-place
    navigation. Pasted into a reply that leaves for Slack / Teams / WhatsApp
    it stays relative and resolves nowhere, so the channel resolver
    absolutizes it against the same ``frontend_url`` the marker link uses.
    The lookbehind refuses a path already inside an absolute URL (the char
    before ``/presentations`` is then part of a host or port) and the
    fragment grammar is the marker's (``SlideId``). Group 1 is the uuid,
    group 2 the fragment (empty when absent).
    """
    return re.compile(
        rf"(?<![\w/:.-])/{re.escape(viewer_path)}/({_UUID_SRC})((?:#/[A-Za-z0-9_-]{{1,64}})?)",
        re.IGNORECASE,
    )


def artifact_markers(text: str) -> list[str]:
    """Every artifact marker of ``text`` (``_ALL_ARTIFACT_MARKER_RES``), as
    written, in order of appearance and each once: what a reader cut short of
    the text still names (``read_conversation_history``)."""
    found = sorted(
        (m.start(), m.group(0)) for pattern in _ALL_ARTIFACT_MARKER_RES for m in pattern.finditer(text or "")
    )
    return list(dict.fromkeys(marker for _, marker in found))


def strip_artifact_markers(text: str, *, keep_lines: bool = False) -> str:
    """Remove every artifact marker (``_ALL_ARTIFACT_MARKER_RES``: file,
    presentation, report, investigation) from ``text`` and collapse the
    whitespace they leave behind.

    The markers are render instructions for the chat surface, not prose: any
    place that shows a message as a plain text snippet (the conversation
    list's last-message preview, a notification) must drop them, or the raw
    ``[[PRESENTATION:<uuid>]]`` leaks next to the sentence that introduced
    the deck. Strip BEFORE truncating a preview — a marker cut in half no
    longer matches any pattern and would leak as a bare uuid fragment.

    Newlines are collapsed too: a marker usually sits alone on its own line
    after a blank one, and a one-line preview must not keep the gap. With
    *keep_lines* (an answer drawn as paragraphs, a dashboard's answer widget)
    the lines stay and only the blank lines a marker leaves are folded.
    """
    if not text or "[[" not in text:
        return text
    cleaned = text
    for pattern in _ALL_ARTIFACT_MARKER_RES:
        cleaned = pattern.sub("", cleaned)
    if keep_lines:
        lines = [line.rstrip() for line in cleaned.splitlines()]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return re.sub(r"\s+", " ", cleaned).strip()


def strip_file_markers(text: str) -> str:
    """Remove every ``[[FILE:…]]`` marker from ``text``, keeping the other artifact markers.

    For a surface that shows the files of an answer as chips of its own - the
    public widget draws one per file under the answer - where the
    marker has no renderer and would read as raw text.
    """
    if not text or "[[FILE" not in text.upper():
        return text
    return _FILE_MARKER_RE.sub("", text).strip()


_FILE_MARKER_HEAD = "[[FILE:"
_FILE_MARKER_MAX_CHARS = len(_FILE_MARKER_HEAD) + 36 + len("]]")
_PARTIAL_FILE_MARKER_RE = re.compile(r"\[\[FILE:[0-9A-F-]{0,36}\]?")


class FileMarkerStreamFilter:
    """:func:`strip_file_markers` for text that arrives in chunks (a streamed answer).

    A marker can be split across chunks, so a tail that may still become one
    is held back until the next chunk decides; :meth:`flush` releases what is
    held at the end of the stream (an unfinished marker is ordinary text).
    """

    def __init__(self) -> None:
        self._held = ""

    def feed(self, chunk: str) -> str:
        text = _FILE_MARKER_RE.sub("", self._held + (chunk or ""))
        self._held = ""
        for index in range(max(0, len(text) - _FILE_MARKER_MAX_CHARS), len(text)):
            if text[index] != "[":
                continue
            tail = text[index:].upper()
            if _FILE_MARKER_HEAD.startswith(tail) or _PARTIAL_FILE_MARKER_RE.fullmatch(tail):
                self._held = text[index:]
                return text[:index]
        return text

    def flush(self) -> str:
        held, self._held = self._held, ""
        return held


# Characters that break out of Markdown link-text syntax (``[text](url)``)
# or smuggle extra markup: square brackets end the text early, backslashes
# escape our own delimiters, backticks open code spans, and newlines split
# the link.  Filenames are user/LLM-controlled, so they are stripped of
# these instead of backslash-escaped — escaping is not portable across the
# per-channel Markdown dialects this text is later converted to (e.g.
# ``markdown_to_slack`` rewrites ``[text](url)`` with its own regex).
_MD_LINK_TEXT_UNSAFE_RE = re.compile(r"[\[\]`\\\r\n]")


def sanitize_md_link_text(text: str, fallback: str | None = None) -> str:
    """Strip Markdown metacharacters from user/LLM-controlled link text.

    Use for any display text embedded in a ``[text](url)`` link that is
    later delivered to an external channel (filenames, titles).  Returns
    *fallback* when nothing survives the stripping: by default the word
    "file" in the platform default language (``channel_notices``).
    """
    cleaned = _MD_LINK_TEXT_UNSAFE_RE.sub("", text or "").strip()
    if cleaned:
        return cleaned
    return fallback if fallback is not None else notice("link_file", notice_language())


async def resolve_channel_file_markers(
    db,
    text: str,
    base_url: str,
    *,
    conversation_id=None,
    user_id=None,
    language: str | None = None,
) -> str:
    """Rewrite ``[[FILE:uuid]]`` markers into signed public download links.

    Web chat renders these markers as inline download cards in the
    frontend; external channels (Slack, Teams, WhatsApp, …) have no such
    renderer, so without this rewrite the raw ``[[FILE:…]]`` text leaks
    verbatim to channel users.  Each marker becomes a Markdown link to the
    tokened public download endpoint — the same machinery
    :func:`resolve_channel_images` uses for images.

    Marker text is LLM-controlled, so links are only ever signed for files
    inside the caller's scope: the attachment must belong to
    *conversation_id* or *user_id* (pass what the call site has — for
    anonymous channel conversations only the conversation scope exists).
    Markers referencing unknown or out-of-scope file ids are stripped (a
    signed link for them would exfiltrate someone else's file; a 404 link
    is useless).  When no scope is supplied at all, nothing is signed and
    every marker is stripped — fail closed.

    Fail-open only on unexpected errors: the original text is returned
    unchanged (raw markers are noise, not a leak). A file with no usable
    name is linked as "file" in *language* (the reader's).
    """
    if not text or "[[FILE" not in text.upper():
        return text
    label = notice("link_file", language or notice_language())

    try:
        import uuid as _uuid

        from sqlalchemy import or_, select

        from app.core.security import generate_image_token
        from app.models.file_attachment import FileAttachment

        marker_ids = {m.group(1).lower() for m in _FILE_MARKER_RE.finditer(text)}
        if not marker_ids:
            return text

        scope_filters = []
        if conversation_id is not None:
            scope_filters.append(FileAttachment.conversation_id == conversation_id)
        if user_id is not None:
            scope_filters.append(FileAttachment.user_id == user_id)
        if not scope_filters:
            # No scope to authorize against — never sign, strip all markers.
            return _FILE_MARKER_RE.sub("", text)

        result = await db.execute(
            select(FileAttachment).where(
                FileAttachment.id.in_([_uuid.UUID(i) for i in marker_ids]),
                or_(*scope_filters),
            )
        )
        filenames = {
            str(fa.id).lower(): (fa.filename or label)
            for fa in result.scalars().all()
        }

        def _replace(match: re.Match[str]) -> str:
            file_id = match.group(1).lower()
            filename = filenames.get(file_id)
            if not filename:
                # Unknown or out-of-scope file — drop the marker instead of
                # leaking it or signing a link we cannot authorize.
                return ""
            # Filenames can contain Markdown metacharacters that would break
            # the link syntax or inject markup — strip them from the display
            # text (the URL itself is platform-generated and safe).
            filename = sanitize_md_link_text(filename, label)
            token, expires = generate_image_token(file_id)
            url = (
                f"{base_url.rstrip('/')}/api/v1/chat/files/{file_id}/public"
                f"?token={token}&expires={expires}"
            )
            return f"[{filename}]({url})"

        return _FILE_MARKER_RE.sub(_replace, text)
    except Exception as exc:
        logger.warning("Failed to resolve channel file markers: %s", format_exception_message(exc))
        return text


def artifact_scope_filter(model, *, conversation_id=None, user_id=None):
    """The rows of *model* a marker in an answer may name, or ``None`` (fail closed).

    A marker is LLM text: it names only a row of the answer's conversation or
    of its user. With neither, nothing qualifies.
    """
    from sqlalchemy import or_

    filters = []
    if conversation_id is not None:
        filters.append(model.conversation_id == conversation_id)
    if user_id is not None:
        filters.append(model.user_id == user_id)
    return or_(*filters) if filters else None


async def _resolve_channel_artifact_markers(
    db,
    text: str,
    frontend_url: str,
    *,
    marker_re: re.Pattern[str],
    marker_prefix: str,
    model,
    viewer_path: str,
    label_key: str,
    conversation_id=None,
    user_id=None,
    language: str | None = None,
) -> str:
    """Rewrite ``[[KIND:uuid]]`` artifact markers into web-app viewer links.

    Shared by decks and reports: both are rows the frontend
    renders behind an authenticated viewer, and neither has any representation in
    Slack / Teams / WhatsApp, so external channels get a Markdown link instead.
    Without this the raw marker text would leak verbatim to channel users.

    The scoping contract is the security-relevant part, and the reason this is one
    function rather than one per artifact type: markers are only linked when the
    row belongs to *conversation_id* or *user_id*, markers for unknown /
    out-of-scope rows are stripped rather than linked, and with no scope at all
    nothing is linked — fail closed. A second copy of this could fall behind and
    start linking artifacts across conversations.

    Fail-open only on unexpected errors: the original text is returned unchanged
    (a raw marker is noise, not a leak — the link is what carries authority).

    The same pass absolutizes a RELATIVE viewer link the agent wrote itself
    (``[slide 4](/presentations/<uuid>#/<slide>)`` — the shape its tool result
    gave it), under the same scope: an in-scope row's path becomes the
    absolute web-app URL, fragment kept; anything else is left exactly as
    written — the agent's prose is not stripped, and a relative link was
    already unresolvable there, so nothing gets worse.

    A row with no title is linked by the catalog's *label_key* ("report",
    "presentation") in *language*, the reader's.
    """
    fallback_label = notice(label_key, language or notice_language())
    link_re = _relative_viewer_link_re(viewer_path)
    has_markers = bool(text) and marker_prefix in text.upper()
    has_links = bool(text) and f"/{viewer_path}/" in text
    if not (has_markers or has_links):
        return text

    try:
        import uuid as _uuid

        from sqlalchemy import select

        marker_ids = {m.group(1).lower() for m in marker_re.finditer(text)} if has_markers else set()
        link_ids = {m.group(1).lower() for m in link_re.finditer(text)} if has_links else set()
        wanted_ids = marker_ids | link_ids
        if not wanted_ids:
            return text

        scope = artifact_scope_filter(model, conversation_id=conversation_id, user_id=user_id)
        if scope is None:
            return marker_re.sub("", text) if marker_ids else text

        result = await db.execute(
            select(model).where(
                model.id.in_([_uuid.UUID(i) for i in wanted_ids]),
                scope,
            )
        )
        titles = {
            str(row.id).lower(): (row.title or fallback_label)
            for row in result.scalars().all()
        }
        base = frontend_url.rstrip("/")

        def _absolutize(match: re.Match[str]) -> str:
            artifact_id = match.group(1).lower()
            if artifact_id not in titles:
                return match.group(0)
            return f"{base}/{viewer_path}/{artifact_id}{match.group(2)}"

        def _replace(match: re.Match[str]) -> str:
            artifact_id = match.group(1).lower()
            title = titles.get(artifact_id)
            if not title:
                return ""
            # A deck marker may name the slide to land on: the link
            # keeps it as the ``#/<slide id>`` fragment the viewer reads, so
            # a channel user opens the deck ON that slide. The report marker
            # has no such group.
            focus = match.group(2) if match.re.groups >= 2 else None
            fragment = f"#/{focus}" if focus else ""
            return (
                f"[{sanitize_md_link_text(title, fallback_label)}]"
                f"({base}/{viewer_path}/{artifact_id}{fragment})"
            )

        # Markers first: the links they produce are absolute, and the
        # lookbehind keeps the second pass off them.
        text = marker_re.sub(_replace, text) if marker_ids else text
        return link_re.sub(_absolutize, text) if link_ids else text
    except Exception as exc:
        logger.warning(
            "Failed to resolve channel %s markers: %s",
            viewer_path,
            format_exception_message(exc),
        )
        return text


async def resolve_channel_presentation_markers(
    db,
    text: str,
    frontend_url: str,
    *,
    conversation_id=None,
    user_id=None,
    language: str | None = None,
) -> str:
    """Rewrite ``[[PRESENTATION:uuid]]`` markers into web-app deck links."""
    from app.models.presentation import Presentation

    return await _resolve_channel_artifact_markers(
        db,
        text,
        frontend_url,
        marker_re=_PRESENTATION_MARKER_RE,
        marker_prefix="[[PRESENTATION",
        model=Presentation,
        viewer_path="presentations",
        label_key="link_presentation",
        conversation_id=conversation_id,
        user_id=user_id,
        language=language,
    )


async def resolve_channel_report_markers(
    db,
    text: str,
    frontend_url: str,
    *,
    conversation_id=None,
    user_id=None,
    language: str | None = None,
) -> str:
    """Rewrite ``[[REPORT:uuid]]`` markers into web-app report links."""
    from app.models.report import Report

    return await _resolve_channel_artifact_markers(
        db,
        text,
        frontend_url,
        marker_re=_REPORT_MARKER_RE,
        marker_prefix="[[REPORT",
        model=Report,
        viewer_path="reports",
        label_key="link_report",
        conversation_id=conversation_id,
        user_id=user_id,
        language=language,
    )


async def resolve_channel_video_markers(
    db,
    text: str,
    frontend_url: str,
    *,
    conversation_id=None,
    user_id=None,
    language: str | None = None,
) -> str:
    """Rewrite ``[[VIDEO:uuid]]`` markers into Video Studio links."""
    from app.models.video_studio import VideoProject

    return await _resolve_channel_artifact_markers(
        db,
        text,
        frontend_url,
        marker_re=_VIDEO_MARKER_RE,
        marker_prefix="[[VIDEO",
        model=VideoProject,
        viewer_path="videos",
        label_key="link_video",
        conversation_id=conversation_id,
        user_id=user_id,
        language=language,
    )


async def resolve_channel_dashboard_markers(
    db,
    text: str,
    frontend_url: str,
    *,
    conversation_id=None,
    user_id=None,
    language: str | None = None,
) -> str:
    """Rewrite ``[[DASHBOARD:uuid]]`` markers into links to the dashboard page.

    A dashboard is a web page of widgets with no channel representation: Slack,
    Teams and the embedded product panels get a link, scoped like a report's.
    """
    from app.models.custom_dashboard import CustomDashboard

    return await _resolve_channel_artifact_markers(
        db,
        text,
        frontend_url,
        marker_re=_DASHBOARD_MARKER_RE,
        marker_prefix="[[DASHBOARD",
        model=CustomDashboard,
        viewer_path="dashboards",
        label_key="link_dashboard",
        conversation_id=conversation_id,
        user_id=user_id,
        language=language,
    )


def strip_channel_investigation_markers(text: str) -> str:
    """Remove ``[[INVESTIGATION:uuid]]`` markers for external channels.

    Stripped rather than rewritten into a link, unlike decks and reports: an
    investigation has **no standalone viewer route**. The live block renders
    inside the conversation it belongs to, so there is no URL a Slack or Teams
    user could usefully open — linking one would hand them a 404. When a
    standalone viewer exists this becomes a
    ``_resolve_channel_artifact_markers`` call like the other two, and gains
    that helper's conversation/user scoping with it.

    Pure text, so no database scoping is needed here: nothing is minted, and a
    stripped marker leaks nothing. The reason it must still happen is that the
    raw marker would otherwise reach channel users as visible noise.
    """
    # No `text.upper()` fast path: the regex is already IGNORECASE, and `sub`
    # on a message with no marker is a scan without a copy.
    if not text:
        return text
    return _INVESTIGATION_MARKER_RE.sub("", text)


def strip_channel_work_item_markers(text: str) -> str:
    """Remove ``[[WORK_ITEM:uuid]]`` markers for external channels.

    The card is a web chat surface (time, action, Cancel / Reschedule); the
    sentence around the marker already says what was scheduled, so a channel
    reader loses nothing but the raw marker.
    """
    if not text:
        return text
    return _WORKITEM_MARKER_RE.sub("", text).rstrip()


async def channel_image_scope(
    db,
    text: str,
    *,
    conversation_id=None,
    user_id=None,
) -> set[str]:
    """The image ids in *text* a channel reply may sign: those of its scope.

    The address is LLM-controlled, so a file (``FileAttachment``) or a
    generated image (``ChatImage``) is only vouched for when it belongs to
    *conversation_id* or *user_id* - the scope
    :func:`resolve_channel_file_markers` applies to markers. No scope, or no
    address, is an empty set without a query. Lower-case ids.
    """
    if not text or (conversation_id is None and user_id is None):
        return set()
    wanted: dict[str, set[uuid.UUID]] = {"files": set(), "images": set()}
    for match in _INTERNAL_IMAGE_RE.finditer(text):
        resource_id = safe_uuid(match.group(4))
        if resource_id is not None:
            wanted[match.group(3)].add(resource_id)
    if not wanted["files"] and not wanted["images"]:
        return set()

    from sqlalchemy import or_, select

    from app.models.chat_image import ChatImage
    from app.models.file_attachment import FileAttachment

    allowed: set[str] = set()
    try:
        for model, ids in ((FileAttachment, wanted["files"]), (ChatImage, wanted["images"])):
            if not ids:
                continue
            scope = []
            if conversation_id is not None:
                scope.append(model.conversation_id == conversation_id)
            if user_id is not None:
                scope.append(model.user_id == user_id)
            rows = await db.execute(select(model.id).where(model.id.in_(ids), or_(*scope)))
            allowed.update(str(row_id).lower() for row_id in rows.scalars().all())
    except Exception as exc:
        # Nothing vouched for is nothing signed: the images keep their alt text.
        logger.warning("Failed to scope channel images: %s", format_exception_message(exc))
        return set()
    return allowed


@functools.cache
def trusted_media_hosts() -> frozenset[str]:
    """The hosts whose images an external channel sends as images.

    Declared by the integration providers that serve media
    (``integration_tools.PROVIDER_MEDIA_HOSTS``), lower-case, each standing
    for itself and its subdomains. The address of an image is LLM-controlled:
    only a media CDN, where a query string reaches no one who could read it,
    is fetched or forwarded; any other host keeps its Markdown line.
    """
    from app.services.integration_tools import PROVIDER_MEDIA_HOSTS

    return frozenset(
        host.strip(".").lower() for hosts in PROVIDER_MEDIA_HOSTS.values() for host in hosts if host.strip(".")
    )


def is_trusted_media_url(url: str, hosts: Collection[str]) -> bool:
    """Whether *url* is an https address on one of *hosts* or a subdomain.

    No userinfo and no port but 443, so ``https://media.example@evil.example``
    or ``https://evil.example/?media.example`` never pass.
    """
    try:
        parsed = urlsplit(url or "")
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
        return False
    if port not in (None, 443):
        return False
    host = (parsed.hostname or "").rstrip(".").lower()
    return bool(host) and any(host == trusted or host.endswith(f".{trusted}") for trusted in hosts)


def external_image_id(url: str) -> str:
    """A stable id for an external image (adapters key cards on it)."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


def resolve_channel_images(
    text: str,
    base_url: str,
    *,
    allowed_ids: Collection[str] | None = None,
    media_hosts: Collection[str] = (),
) -> tuple[str, list[EmbeddedImage]]:
    """Replace internal image paths with publicly accessible signed URLs.

    Returns the rewritten text and a list of extracted ``EmbeddedImage``
    objects (useful for channels that need to upload images natively), in
    the order they appear.

    ``base_url`` should be the platform's external base URL
    (e.g. ``https://xtm-one.example.com``). Only an id of *allowed_ids*
    (:func:`channel_image_scope`) is signed; any other image keeps its alt
    text and gets no link, and without a scope nothing is signed.

    An https image on one of *media_hosts* (:func:`trusted_media_hosts`) is
    extracted too, as source :data:`EXTERNAL_IMAGE_SOURCE` with its own address
    as ``public_url`` (a GIF a media integration found): its line stays as it is,
    each address once, at most :data:`MAX_EXTERNAL_CHANNEL_IMAGES`. Any other
    external image is left alone, never fetched nor forwarded.
    """
    from app.core.security import generate_image_token

    allowed = {str(resource_id).lower() for resource_id in allowed_ids or ()}
    found: list[tuple[int, EmbeddedImage]] = []

    def _replace(m: re.Match[str]) -> str:
        alt = m.group(1)
        original_path = m.group(2)
        source = m.group(3)  # "images" or "files"
        resource_id = m.group(4)
        if resource_id.lower() not in allowed:
            return alt
        token, expires = generate_image_token(resource_id)
        public_url = f"{base_url.rstrip('/')}/api/v1/chat/{source}/{resource_id}/public?token={token}&expires={expires}"
        found.append(
            (
                m.start(),
                EmbeddedImage(
                    image_id=resource_id,
                    alt_text=alt,
                    original_path=original_path,
                    public_url=public_url,
                    source=source,
                ),
            )
        )
        return f"![{alt}]({public_url})"

    # Read on the text as written: the signed links the substitution writes
    # are https addresses too, never to be taken for external images.
    seen: set[str] = set()
    if media_hosts and text:
        for m in _EXTERNAL_IMAGE_RE.finditer(text):
            url = m.group(2)
            if url in seen or not is_trusted_media_url(url, media_hosts):
                continue
            if len(seen) >= MAX_EXTERNAL_CHANNEL_IMAGES:
                break
            seen.add(url)
            found.append(
                (
                    m.start(),
                    EmbeddedImage(
                        image_id=external_image_id(url),
                        alt_text=m.group(1),
                        original_path=url,
                        public_url=url,
                        source=EXTERNAL_IMAGE_SOURCE,
                    ),
                )
            )

    resolved_text = _INTERNAL_IMAGE_RE.sub(_replace, text)
    found.sort(key=lambda item: item[0])
    return resolved_text, [image for _, image in found]


async def fetch_image_bytes(
    resource_id: str, source: str = "images"
) -> tuple[bytes, str] | None:
    """Fetch image/file bytes from the database or S3 by ID.

    ``source`` should be ``"images"`` (ChatImage table) or ``"files"``
    (FileAttachment + S3).

    Returns ``(data, content_type)`` or ``None`` if not found.
    Used by channel adapters that need to upload images natively.
    """
    import uuid as _uuid

    from sqlalchemy import select

    from app.core.database import async_session

    try:
        if source == "files":
            from app.models.file_attachment import FileAttachment
            from app.services.file_storage import file_storage

            async with async_session() as db:
                result = await db.execute(
                    select(FileAttachment).where(
                        FileAttachment.id == _uuid.UUID(resource_id)
                    )
                )
                file_att = result.scalar_one_or_none()
                if file_att and file_att.s3_object_name:
                    data = await file_storage.download(file_att.s3_object_name)
                    if data:
                        return data, file_att.content_type or "image/png"
        else:
            from app.models.chat_image import ChatImage

            async with async_session() as db:
                result = await db.execute(
                    select(ChatImage).where(ChatImage.id == _uuid.UUID(resource_id))
                )
                image = result.scalar_one_or_none()
                if image and image.image_data:
                    return image.image_data, image.content_type or "image/png"
    except Exception as exc:
        logger.debug(
            "Failed to fetch image bytes for %s (%s): %s", resource_id, source, format_exception_message(exc)
        )
    return None


async def fetch_external_image_bytes(url: str) -> tuple[bytes, str] | None:
    """The bytes and type of an image at a trusted media host, or ``None``.

    Checked again against :func:`trusted_media_hosts` (an adapter never
    fetches an address the resolver did not vouch for), downloaded through
    the outbound URL policy on every hop (``social_media.fetch_public_media``),
    bounded by :data:`MAX_EXTERNAL_IMAGE_BYTES`, and typed by its magic number,
    never by the header the host sent: anything but PNG, JPEG, GIF or WebP is
    ``None``.
    """
    if not is_trusted_media_url(url, trusted_media_hosts()):
        return None
    from app.integrations.providers.social_media import fetch_public_media
    from app.services.builtin_tools.image_assets import sniff_raster_type

    fetched = await fetch_public_media(url, source="The image host", max_bytes=MAX_EXTERNAL_IMAGE_BYTES, what="image")
    if isinstance(fetched, str):
        # A FetchError: refused, over the cap or a failed answer. No address logged.
        logger.debug("External channel image not fetched: %s", log_preview(fetched))
        return None
    content_type = sniff_raster_type(fetched.data)
    if content_type is None:
        return None
    return fetched.data, content_type


async def fetch_embedded_image(img_info: dict[str, Any]) -> tuple[bytes, str] | None:
    """The bytes of an embedded image (``OutboundMessage.metadata["embedded_images"]``).

    The one reader of the adapters that upload bytes (Slack, Telegram,
    Discord): a platform image or file by its id, an external one by its
    address (:func:`fetch_external_image_bytes`).
    """
    source = str(img_info.get("source") or "images")
    if source == EXTERNAL_IMAGE_SOURCE:
        return await fetch_external_image_bytes(str(img_info.get("public_url") or ""))
    return await fetch_image_bytes(str(img_info.get("image_id") or ""), source=source)


_UPLOAD_EXTENSIONS: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def image_upload_filename(stem: str, content_type: str) -> str:
    """*stem* with the extension of *content_type* (``.png`` when unknown).

    A platform reads the extension: a GIF uploaded as ``.png`` is drawn still.
    """
    base = (content_type or "").split(";", 1)[0].strip().lower()
    return f"{stem}{_UPLOAD_EXTENSIONS.get(base, '.png')}"
