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

"""Continuing one conversation across the web, Slack and Teams.

One conversation record, several ways in. A conversation keeps the channel it
started on; this module holds every rule that lets the same record be
continued from another surface:

* **Continue on the web.** A Slack or Teams conversation of an identified user
  is listed in their web conversations and accepts web turns when it is a
  DIRECT-MESSAGE thread (:func:`is_private_channel_thread`,
  :func:`web_listable_clause`, :func:`web_continuation_refusal`). A group
  channel thread is never listed nor continued on the web: its history holds
  other people's messages, and every other participant reads what the agent
  answers there - a web turn would carry the owner's private context into
  that thread.
* **Continue in a channel.** A conversation the web may continue can be bound
  to a direct-message thread of the Slack or Teams bot
  (``ConversationChannelBinding``, :func:`bind_thread`); the router then finds
  the conversation from that thread (:func:`find_bound_conversation`), for a
  sender who resolves to the conversation's owner only.
* **One turn at a time, whatever the channel.** A channel message on a
  conversation the web also reaches takes the conversation's turn lock
  (``chat:processing:{id}``) through :class:`ChannelTurn`, which also fans the
  turn out to the conversation's open web sockets and honours the web Stop.
  A message that finds the turn taken is queued and answered as the next
  turn (:func:`take_turn_or_queue`, :func:`drain_channel_queue`), claimed
  until its turn answers it so a process that dies meanwhile does not lose
  it; the files stored for it are deleted when it never runs
  (:func:`discard_queued_files`, :func:`expired_queued_files_clause`).

Access is unchanged: continuing is the owner's, as on the web (a share is
read-only); an anonymous channel conversation (``user_id IS NULL``) is never
listed or continued.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message, safe_uuid
from app.models.chat import Conversation
from app.models.conversation_channel_binding import ConversationChannelBinding
from app.models.file_attachment import FileAttachment
from app.services.channel_notices import notice
from app.services.chat_turn_guard import CHANNEL_QUEUE_TTL_SECONDS

logger = logging.getLogger(__name__)

#: The channels a conversation can be continued in, and from.
CONTINUABLE_CHANNELS: tuple[str, ...] = ("slack", "teams")

#: One thread continues one conversation (``ConversationChannelBinding``).
_THREAD_KEY = "uq_conversation_channel_bindings_thread"

#: How the thread id of a direct-message thread starts, per channel: a Slack
#: DM channel id starts with ``D`` (``D...:<ts>``), a Teams personal chat
#: conversation id with ``a:``. Group threads never do (Slack ``C`` / ``G``,
#: Teams ``19:``).
_PRIVATE_THREAD_PREFIXES: dict[str, str] = {"slack": "D", "teams": "a:"}

#: What a Slack / Teams message that finds the conversation's turn taken is
#: told: it is queued and answered as the next turn. In English; the router
#: sends it in the reader's language (``channel_notices.localized_reply``).
QUEUED_NOTICE = notice("channel_queued")
#: ...or, past ``chat_turn_guard.CHANNEL_QUEUE_MAX_MESSAGES``, that it was not.
QUEUE_FULL_NOTICE = notice("channel_queue_full")

#: The machine-readable reasons a continuation is unavailable, served to the
#: web app with a sentence.
REASON_NOT_CONTINUABLE = "not_continuable"
REASON_NOT_CONFIGURED = "not_configured"
REASON_AGENT_UNAVAILABLE = "agent_unavailable"
REASON_DIRECT_MESSAGES_OFF = "direct_messages_off"
REASON_TEAMS_CHAT_UNKNOWN = "teams_chat_unknown"
REASON_STARTED_THERE = "started_there"
REASON_USER_NOT_FOUND = "user_not_found"
REASON_DELIVERY_FAILED = "delivery_failed"


class ChannelContinuationError(Exception):
    """A conversation could not be continued in a channel, with the reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


_CHANNEL_LABELS: dict[str, str] = {
    "slack": "Slack",
    "teams": "Microsoft Teams",
    "whatsapp": "WhatsApp",
    "telegram": "Telegram",
    "discord": "Discord",
    "google_chat": "Google Chat",
    "web_embed": "the embedded chat",
}


def channel_label(channel: str) -> str:
    """The product name of *channel* in a sentence."""
    return _CHANNEL_LABELS.get(channel, channel)


# ── Which conversations the web continues ─────────────────────────────────


def is_private_channel_thread(channel: str | None, channel_thread_id: str | None) -> bool:
    """Whether a stored Slack / Teams thread is a direct message with the bot."""
    prefix = _PRIVATE_THREAD_PREFIXES.get(channel or "")
    return bool(prefix and channel_thread_id and channel_thread_id.startswith(prefix))


def web_listable_clause():
    """The SQL rule of :func:`continuable_on_web` for the conversation list.

    Web conversations, plus the Slack and Teams direct-message threads. The
    caller scopes it to the user's own (or shared) rows.
    """
    channel_threads = [
        and_(
            Conversation.channel == channel,
            Conversation.channel_thread_id.like(f"{prefix}%"),
        )
        for channel, prefix in _PRIVATE_THREAD_PREFIXES.items()
    ]
    return or_(Conversation.channel == "web", *channel_threads)


def continuable_on_web(conv: Any) -> bool:
    """Whether the web lists and continues *conv*: see the module docstring."""
    # ``web`` is the column default, not yet set on a row never flushed.
    channel = getattr(conv, "channel", None) or "web"
    if channel == "web":
        return True
    if getattr(conv, "user_id", None) is None:
        return False
    return is_private_channel_thread(channel, getattr(conv, "channel_thread_id", None))


def not_continuable_detail(conv: Any) -> str | None:
    """Why *conv* is not continued from another surface, or ``None`` when it is."""
    if continuable_on_web(conv):
        return None
    label = channel_label(getattr(conv, "channel", None) or "another channel")
    if getattr(conv, "channel", None) in CONTINUABLE_CHANNELS:
        return (
            f"This conversation is a {label} thread shared with other people. "
            f"Continue it in {label}."
        )
    return f"This conversation can only be continued in {label}."


def web_continuation_refusal(conv: Any) -> str | None:
    """Why the web may not send in *conv*, or ``None`` when it may.

    Refused: a Slack or Teams group thread. A conversation of another channel
    keeps what it always had (its owner may send in it), as it is never
    listed on the web.
    """
    if getattr(conv, "channel", None) not in CONTINUABLE_CHANNELS:
        return None
    return not_continuable_detail(conv)


async def platform_chat_refusal(db: AsyncSession, conv: Any) -> str | None:
    """Why the platform chat API may not run a turn in *conv*, or ``None``.

    That API runs its turn without the conversation's lock (its own
    transport), so it stays off every conversation a Slack or Teams turn can
    reach: a group thread (the web's sentence), a direct-message thread, and
    a conversation continued in a direct message of the bot.
    """
    refusal = web_continuation_refusal(conv)
    if refusal:
        return refusal
    bound = await db.execute(
        select(ConversationChannelBinding.id)
        .where(ConversationChannelBinding.conversation_id == conv.id)
        .limit(1)
    )
    if getattr(conv, "channel", None) in CONTINUABLE_CHANNELS or bound.scalar_one_or_none():
        return (
            "This conversation is continued in Slack or Teams. Continue it there "
            "or in the web app."
        )
    return None


async def web_send_refusal(conv: Any) -> str | None:
    """Why a web send (REST or SSE stream) may not go into *conv* now, or ``None``.

    A group channel thread is continued on its channel only; and while a turn
    a channel message started runs, a web send is refused rather than folded:
    that turn has no steering bridge and answers on its channel. A turn guard
    that cannot be read refuses nothing - the send path meets it next.
    """
    refusal = web_continuation_refusal(conv)
    if refusal:
        return refusal
    from app.services import chat_turn_guard

    try:
        holder = await chat_turn_guard.channel_turn_holder(str(conv.id))
    except Exception:
        return None
    return channel_turn_busy_text(holder) if holder else None


def conversation_link(conversation_id: object) -> str:
    """The web address of a conversation, for a channel message."""
    from app.core.config import settings

    return f"{settings.frontend_url.rstrip('/')}/chat?c={conversation_id}"


def channel_turn_busy_text(holder: str | None) -> str:
    """What a web send is told while a channel message's turn runs."""
    where = channel_label(holder) if holder else "another channel"
    return (
        f"A reply to a message sent from {where} is being written in this "
        "conversation. Send yours again once it arrives."
    )


#: How long a parent wake waits for a channel message's turn to end before
#: folding into it all the same (the next turn then reads it in its history).
WAKE_CHANNEL_TURN_WAIT_SECONDS = 600.0
_HOLD_POLL_SECONDS = 2.0


async def hold_off_channel_turns(
    conversation_id: object,
    *,
    wait_seconds: float = 0.0,
    before_wait: Callable[[], Awaitable[None]] | None = None,
) -> tuple[str | None, str | None]:
    """Keep channel turns off *conversation_id* while a web send or a wake joins it.

    Called right before the caller stores its message: until
    :func:`release_channel_hold`, called once ``acquire_or_fold`` returned, a
    channel message waits for the lock, so what the caller folds goes into a
    turn that drains it (a channel turn has no steering bridge). Returns
    ``(hold, None)``, or ``(None, refusal)`` when a channel message's turn
    still holds the conversation after *wait_seconds* (*before_wait* runs once
    before the first wait). A guard that cannot be reached holds nothing and
    refuses nothing: ``(None, None)``.
    """
    from app.services import chat_turn_guard

    cid = str(conversation_id)
    deadline = time.monotonic() + wait_seconds
    waited = False
    while True:
        try:
            hold, holder = await chat_turn_guard.reserve_turn_entry(cid)
        except Exception as exc:
            logger.warning(
                "Turn guard unavailable for conversation %s; holding no channel turn off: %s",
                cid,
                format_exception_message(exc),
            )
            return None, None
        if hold is not None:
            _HOLD_KEEPERS[hold] = asyncio.create_task(
                _keep_hold(cid, hold, asyncio.current_task()), name=f"channel-hold:{cid}"
            )
            return hold, None
        if time.monotonic() >= deadline:
            return None, channel_turn_busy_text(holder)
        if not waited:
            waited = True
            if before_wait is not None:
                await before_wait()
        await asyncio.sleep(_HOLD_POLL_SECONDS)


#: The task renewing each live hold, until :func:`release_channel_hold`.
_HOLD_KEEPERS: dict[str, asyncio.Task] = {}


async def _keep_hold(conversation_id: str, hold: str, owner: asyncio.Task | None) -> None:
    """Renew *hold* while its caller works, however long its writes take.

    Until the task that took it (*owner*) ends: one that never released it -
    an exception on its way - lets channel turns in again at once. And for at
    most ``TURN_ENTRY_RESERVATION_MAX_SECONDS``.
    """
    from app.services import chat_turn_guard

    deadline = time.monotonic() + chat_turn_guard.TURN_ENTRY_RESERVATION_MAX_SECONDS
    interval = chat_turn_guard.TURN_ENTRY_RESERVATION_SECONDS / 3
    try:
        while time.monotonic() < deadline:
            if owner is None:
                await asyncio.sleep(interval)
            else:
                await asyncio.wait({owner}, timeout=interval)
                if owner.done():
                    _HOLD_KEEPERS.pop(hold, None)
                    await _give_back_hold(conversation_id, hold)
                    return
            try:
                if not await chat_turn_guard.renew_turn_entry(conversation_id, hold):
                    return
            except Exception:
                logger.debug("Could not renew the channel hold of %s", conversation_id, exc_info=True)
    finally:
        _HOLD_KEEPERS.pop(hold, None)


async def release_channel_hold(conversation_id: object, hold: str | None) -> None:
    """Let channel turns in again (:func:`hold_off_channel_turns`). Never raises."""
    if hold is None:
        return
    keeper = _HOLD_KEEPERS.pop(hold, None)
    if keeper is not None:
        keeper.cancel()
    await _give_back_hold(conversation_id, hold)


async def _give_back_hold(conversation_id: object, hold: str) -> None:
    from app.services import chat_turn_guard

    try:
        await chat_turn_guard.release_turn_entry(str(conversation_id), hold)
        # A channel message the hold kept off may be queued: if the caller
        # took no turn (it failed on its way), no release would start it.
        if await chat_turn_guard.channel_messages_owed(str(conversation_id)):
            schedule_queue_drain(conversation_id)
    except Exception as exc:
        logger.debug(
            "Could not release the channel hold of conversation %s: %s",
            conversation_id,
            format_exception_message(exc),
        )


# ── Bindings ──────────────────────────────────────────────────────────────


async def find_bound_conversation(
    db: AsyncSession,
    channel: str,
    channel_thread_id: str | None,
    *,
    user_id: uuid.UUID,
) -> Conversation | None:
    """The conversation a direct-message thread continues, for *user_id*.

    ``None`` unless the thread is bound, by *user_id*, to a conversation
    *user_id* still owns and has not archived: a binding never hands a
    conversation to anyone but its owner.
    """
    if not channel_thread_id or channel not in CONTINUABLE_CHANNELS:
        return None
    result = await db.execute(
        select(Conversation)
        .join(
            ConversationChannelBinding,
            ConversationChannelBinding.conversation_id == Conversation.id,
        )
        .where(
            ConversationChannelBinding.channel == channel,
            ConversationChannelBinding.channel_thread_id == channel_thread_id,
            ConversationChannelBinding.user_id == user_id,
            Conversation.user_id == user_id,
            Conversation.is_archived.is_(False),
        )
        .limit(1)
    )
    return result.scalars().first()


@dataclass(frozen=True)
class BoundThread:
    """A thread bound to a conversation by :func:`bind_thread`."""

    binding: ConversationChannelBinding
    #: The conversation the thread continued until now, when it was another one.
    previous_conversation_id: uuid.UUID | None


async def bind_thread(
    db: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    channel: str,
    channel_thread_id: str,
) -> BoundThread:
    """Bind a direct-message thread to a conversation; a bound thread is re-pointed.

    Two binds of one thread at once resolve on the ``(channel,
    channel_thread_id)`` key: one inserts, the other re-points the row under
    its lock. Flushes, never commits. A thread released and bound again under
    every attempt is a :class:`ChannelContinuationError` (``delivery_failed``).
    """
    for _attempt in range(3):
        inserted = (
            await db.scalars(
                pg_insert(ConversationChannelBinding)
                .values(
                    conversation_id=conversation_id,
                    user_id=user_id,
                    channel=channel,
                    channel_thread_id=channel_thread_id,
                )
                .on_conflict_do_nothing(constraint=_THREAD_KEY)
                .returning(ConversationChannelBinding)
            )
        ).first()
        if inserted is not None:
            return BoundThread(inserted, None)
        existing = (
            await db.execute(
                select(ConversationChannelBinding)
                .where(
                    ConversationChannelBinding.channel == channel,
                    ConversationChannelBinding.channel_thread_id == channel_thread_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if existing is None:
            continue  # released between the two statements: insert again
        previous = existing.conversation_id
        existing.conversation_id = conversation_id
        existing.user_id = user_id
        existing.updated_at = datetime.now(UTC)
        await db.flush()
        return BoundThread(existing, previous if previous != conversation_id else None)
    raise ChannelContinuationError(
        REASON_DELIVERY_FAILED,
        f"The {channel_label(channel)} thread kept changing while it was linked. Try again in a moment.",
    )


async def list_bindings(
    db: AsyncSession, conversation_id: uuid.UUID
) -> list[ConversationChannelBinding]:
    """The threads a conversation continues in, oldest first."""
    result = await db.execute(
        select(ConversationChannelBinding)
        .where(ConversationChannelBinding.conversation_id == conversation_id)
        .order_by(ConversationChannelBinding.created_at)
    )
    return list(result.scalars().all())


async def unbind_conversation(
    db: AsyncSession, conversation_id: uuid.UUID, channel: str
) -> int:
    """Stop continuing a conversation in *channel*. Returns how many threads."""
    result = await db.execute(
        delete(ConversationChannelBinding).where(
            ConversationChannelBinding.conversation_id == conversation_id,
            ConversationChannelBinding.channel == channel,
        )
    )
    return int(result.rowcount or 0)


async def unbind_thread(
    db: AsyncSession, channel: str, channel_thread_id: str | None, *, user_id: uuid.UUID
) -> list[uuid.UUID]:
    """Release a thread its owner reset (``/new``). Returns the conversations it continued."""
    if not channel_thread_id:
        return []
    result = await db.execute(
        delete(ConversationChannelBinding)
        .where(
            ConversationChannelBinding.channel == channel,
            ConversationChannelBinding.channel_thread_id == channel_thread_id,
            ConversationChannelBinding.user_id == user_id,
        )
        .returning(ConversationChannelBinding.conversation_id)
    )
    return list(result.scalars().all())


async def audit_released_threads(
    db: AsyncSession,
    *,
    user: Any,
    conversation_ids: list[uuid.UUID],
    channel: str,
    how: str,
) -> None:
    """Record ``unbind`` for each conversation a thread stopped continuing.

    *how* ends the summary: what released the thread (``/new``, a newer
    binding). Flushes with the change, never commits.
    """
    from app.services.audit import log_audit

    for conversation_id in conversation_ids:
        conv = await db.get(Conversation, conversation_id)
        title = conv.title if conv is not None else None
        await log_audit(
            db,
            user=user,
            action="unbind",
            entity_type="conversation",
            entity_id=conversation_id,
            entity_name=title,
            summary=(
                f"'{user.email}' stopped continuing conversation '{title}' in "
                f"{channel_label(channel)}: {how}"
            ),
            details={"conversation_id": str(conversation_id), "channel": channel},
        )


# ── What a channel turn reads off the conversation ───────────────────────


async def retire_pending_approval(db: AsyncSession, conv: Conversation, channel: str) -> bool:
    """Retire a tool call left waiting for approval, as a web send taking the turn does.

    A turn paused live on a decision holds the lock, so a channel message never
    gets here while one waits: it is queued and runs once that turn ends, as a
    web message sent then is folded into it. What can be left here is a turn
    that ended at the gate (FR-002c1); a message that takes the turn moves the
    conversation on and retires it (FR-002c4) through the helper every send
    path calls. ``True`` when one was retired (committed).
    """
    from app.services import chat_approval_resume

    retired = await chat_approval_resume.retire_for_new_turn(db, conv.id, surface=channel)
    if retired:
        await db.commit()
    return retired


# ── The turn lock of a channel message ───────────────────────────────────


class ChannelTurn:
    """The turn a channel message holds on a conversation the web also reaches.

    Takes the conversation's ``chat:processing`` lock (a channel token, so a
    web send refuses instead of folding into it), keeps it alive, turns a web
    Stop into :attr:`cancel_event`, and fans the turn out to the
    conversation's open web sockets (:attr:`publisher`). :meth:`release` gives
    the lock back and hands any message folded into the turn to the next one.

    A guard that cannot be reached (Redis down) is not a refusal: the turn
    runs unguarded and the failure is logged, as the router's other Redis
    guards do.
    """

    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.conversation_id: str | None = None
        self.token: str | None = None
        #: The message was queued behind another turn and is being answered now.
        self.queued = False
        #: Its files, stored when it was queued.
        self.stored_file_ids: list[str] = []
        self.cancel_event = asyncio.Event()
        self.publisher: Any = None
        #: The turn's ``TurnCheckpoint`` (the router's): its completed tool
        #: work, flushed when a shutdown cuts the turn so its resume replays it.
        self.checkpoint: Any = None
        self._origin = f"channel-{uuid.uuid4().hex}"
        self._keepalive: asyncio.Task | None = None
        self._finished = False

    @property
    def held(self) -> bool:
        return self.token is not None

    async def acquire(self, conversation_id: object) -> bool:
        """Become the conversation's turn; ``False`` when another one holds it.

        One attempt, never a wait: a message that finds the turn taken is
        queued (:func:`take_turn_or_queue`). The web sees the turn only once
        :meth:`start` ran.
        """
        from app.services import chat_turn_guard

        cid = str(conversation_id)
        try:
            token = await chat_turn_guard.acquire_turn(cid, channel=self.channel)
        except Exception as exc:
            logger.warning(
                "Turn guard unavailable for a %s message on conversation %s; "
                "running it unguarded: %s",
                self.channel,
                cid,
                format_exception_message(exc),
            )
            return True
        if not token:
            return False
        self.conversation_id = cid
        self.token = token
        return True

    def adopt_queued(self) -> None:
        """Take over the turn a queue drain holds for this message (:func:`drain_channel_queue`)."""
        queued = _QUEUED_TURN.get()
        if queued is None or queued.adopted or queued.channel != self.channel:
            return
        queued.adopted = True
        self.queued = True
        self.conversation_id = queued.conversation_id
        self.token = queued.token
        self.stored_file_ids = list(queued.file_ids)

    def start(self) -> None:
        """Show the held turn on the web and keep it alive (a web Stop reaches it)."""
        if not self.held or self._keepalive is not None:
            return
        from app.services import chat_turn_stream

        try:
            self.publisher = chat_turn_stream.TurnPublisher(
                self.conversation_id, self.token, origin=self._origin
            )
            self.publisher.start()
        except Exception as exc:
            logger.debug("Channel turn fan-out unavailable: %s", format_exception_message(exc))
            self.publisher = None
        self._keepalive = asyncio.create_task(
            self._keep_alive(), name=f"channel-turn:{self.conversation_id}"
        )

    async def _keep_alive(self) -> None:
        from app.services import chat_turn_guard

        cid, token = str(self.conversation_id), str(self.token)
        last_refresh = time.monotonic()
        while True:
            await asyncio.sleep(chat_turn_guard.BRIDGE_POLL_SECONDS)
            try:
                if await chat_turn_guard.consume_turn_cancel(cid, token):
                    logger.info(
                        "Stop requested for the %s turn of conversation %s",
                        self.channel,
                        self.conversation_id,
                    )
                    self.cancel_event.set()
                if time.monotonic() - last_refresh >= chat_turn_guard.REST_REFRESH_INTERVAL_SECONDS:
                    await chat_turn_guard.refresh_turn(cid, token)
                    last_refresh = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Channel turn keepalive failed for %s", cid, exc_info=True)

    async def on_event(self, event: dict) -> None:
        """``on_event`` of the turn's chat service: its progress, for the web."""
        if self.publisher is not None:
            with contextlib.suppress(Exception):
                await self.publisher.on_event(event)

    def show_user_message(self, message: object) -> None:
        """Show the stored channel message on the conversation's open web sockets."""
        if self.publisher is None or message is None:
            return
        from app.services import chat_turn_stream

        chat_turn_stream.publish_user_message(
            str(self.conversation_id), message, origin=self._origin
        )

    def finish(
        self, *, message_id: str | None, content: str, content_format: str | None = None
    ) -> None:
        """End the turn for its web viewers with its stored reply.

        *content_format*: the reply's markup when it is not Markdown, as
        stored on its row (``output_format.CONTENT_FORMAT_META_KEY``).
        """
        if self.publisher is None or self._finished:
            return
        self._finished = True
        frame: dict[str, Any] = {
            "type": "done",
            "conversation_id": self.conversation_id,
            "channel": self.channel,
        }
        if message_id and content:
            frame.update(content=content, message_id=message_id)
            if content_format:
                frame["content_format"] = content_format
        self.publisher.offer(frame)

    async def release(self, *, hand_over: bool = True) -> None:
        """Give the lock back; a message folded into this turn goes to the next one.

        Without *hand_over*, the queue waits for the scheduler's sweep: a turn
        cut by a shutdown (:func:`hand_on_cut_turn`) never restarts its message
        on the process that is stopping.
        """
        if not self.held:
            return
        from app.services import chat_turn_guard, chat_turn_stream

        cid, token = str(self.conversation_id), str(self.token)
        self.token = None
        keepalive, self._keepalive = self._keepalive, None
        publisher, self.publisher = self.publisher, None
        if keepalive is not None:
            keepalive.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await keepalive
        if publisher is not None:
            with contextlib.suppress(Exception):
                await publisher.aclose(terminal=chat_turn_stream.interrupted_frame())
        if self.queued:
            # Answered, or refused: its claim goes before the lock does, or
            # the next drain would take it for one a dead process left.
            with contextlib.suppress(Exception):
                await chat_turn_guard.answer_claimed_channel_messages(cid, token)
        try:
            # The release also starts the next queued message, if any.
            stranded = await chat_turn_guard.release_turn_and_drain(cid, token, hand_over=hand_over)
            if stranded:
                # Already persisted user messages (a parent wake, a send that
                # raced the refusal): the next turn reads them in its history.
                await chat_turn_guard.requeue_stranded(cid, stranded)
        except Exception as exc:
            logger.warning(
                "Could not release the %s turn of conversation %s: %s",
                self.channel,
                cid,
                format_exception_message(exc),
            )


# ── Messages queued behind a running turn ───────────────────────────────


@dataclass
class _QueuedTurn:
    """The turn a queue drain took for the message it runs (:meth:`ChannelTurn.adopt_queued`)."""

    conversation_id: str
    channel: str
    token: str
    #: The message's files, stored when it was queued (``FileAttachment`` ids).
    file_ids: list[str]
    adopted: bool = False
    #: Put back by :func:`hand_on_cut_turn`: its files wait for the resume.
    resumed: bool = False


#: Set by :func:`drain_channel_queue` for the task that answers the queued
#: message: the router's :class:`ChannelTurn` takes the turn over from it.
_QUEUED_TURN: ContextVar[_QueuedTurn | None] = ContextVar("channel_queued_turn", default=None)


class QueuedMessageUndeliverableError(Exception):
    """Raised by an adapter's ``run_queued_message`` that cannot answer at all (no bot connected, no service URL).

    The drain takes it as a failed start: started again, then dropped and the
    sender told (:func:`_tell_dropped`).
    """

#: Said when the queue cannot be read and a turn holds the conversation.
TURN_BUSY_NOTICE = notice("channel_turn_busy")


def _queue_item(inbound: Any, file_ids: list[str] | None = None) -> str:
    """*inbound* as stored in the queue: no raw payload and no attachment URL.

    A platform's file URL can be a credential and short-lived (a Teams
    personal-chat ``downloadUrl``): the files are stored when the message is
    queued, and the queue keeps their ids (*file_ids*).
    """
    return json.dumps(
        {
            "channel": inbound.channel.value,
            "text": inbound.text or "",
            "user_email": inbound.user_email,
            "channel_user_id": inbound.channel_user_id,
            "channel_thread_id": inbound.channel_thread_id,
            "channel_message_id": inbound.channel_message_id,
            "agent_id": inbound.agent_id,
            "metadata": {
                key: value
                for key, value in (inbound.metadata or {}).items()
                if value is None or isinstance(value, str | int | float | bool)
            },
            "file_ids": list(file_ids or []),
            "queued_at": datetime.now(UTC).isoformat(),
        }
    )


def _decode_queue_item(raw: object) -> dict | None:
    if isinstance(raw, bytes):
        raw = raw.decode(errors="replace")
    try:
        item = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    if not isinstance(item, dict) or item.get("channel") not in CONTINUABLE_CHANNELS:
        return None
    return item


def _same_thread(a: dict, b: dict) -> bool:
    def key(item: dict) -> tuple:
        return (
            item.get("channel"),
            item.get("channel_thread_id"),
            (item.get("user_email") or "").lower(),
        )

    return key(a) == key(b)


def _inbound_from_queue(items: list[dict]) -> Any:
    """One message answering *items*: their texts in arrival order.

    Their files, stored when they were queued, travel with the turn
    (:attr:`_QueuedTurn.file_ids`).
    """
    from app.channels.base import ChannelType, InboundMessage

    first, last = items[0], items[-1]
    return InboundMessage(
        channel=ChannelType(first["channel"]),
        text="\n\n".join(text for item in items if (text := item.get("text") or "")),
        user_email=first.get("user_email"),
        channel_user_id=first.get("channel_user_id"),
        channel_thread_id=first.get("channel_thread_id"),
        channel_message_id=last.get("channel_message_id"),
        agent_id=first.get("agent_id"),
        metadata=dict(first.get("metadata") or {}),
    )


def _queued_file_ids(items: list[dict]) -> list[str]:
    return [
        str(file_id)
        for item in items
        for file_id in item.get("file_ids") or []
        if isinstance(file_id, str) and file_id
    ]


#: Ends the ``FileAttachment.source`` of a file stored for a queued message
#: (``channel:slack:queued``) until the turn answering it claims it; the files
#: panel still counts it a channel upload.
_QUEUED_FILE_MARK = ":queued"

#: How long a marked file may wait for its turn: the queue's lifetime - a
#: message queued longer is never run (:func:`_claim_queued_group`) - and a
#: margin. Past it, its message never ran - its queue entry expired, was
#: dropped or lost, with the ids it held - and the upload purge deletes it.
QUEUED_FILE_RETENTION = timedelta(seconds=CHANNEL_QUEUE_TTL_SECONDS) + timedelta(hours=1)


def queued_file_source(channel: str) -> str:
    """The ``FileAttachment.source`` of a file stored for a queued *channel* message."""
    return f"channel:{channel}{_QUEUED_FILE_MARK}"


def _waiting_files_clause() -> Any:
    """The files stored for queued messages that no turn claimed and no message shows."""
    return and_(
        FileAttachment.source.in_([queued_file_source(channel) for channel in CONTINUABLE_CHANNELS]),
        FileAttachment.message_id.is_(None),
    )


def expired_queued_files_clause(now: datetime) -> Any:
    """The files of queued messages that never ran: still marked, on no message, older than :data:`QUEUED_FILE_RETENTION`.

    Matched by the install-wide purge of the uploads no message took
    (:func:`app.services.widget_uploads.purge_expired_uploads`), which locks
    the rows it deletes and skips locked ones.
    """
    return and_(_waiting_files_clause(), FileAttachment.created_at < now - QUEUED_FILE_RETENTION)


async def claim_queued_files(
    db: AsyncSession, file_ids: list[str], *, channel: str, user_id: object
) -> list[FileAttachment]:
    """Take the files of a queued message for the turn answering it: *user_id*'s rows, in *file_ids* order.

    One UPDATE lifts the mark, so this claim and the purge never both win a
    row: a claimed file is no longer the purge's, and a file the purge holds
    is not claimed - the message runs without it. Does not commit.
    """
    wanted = [file_id for file_id in (safe_uuid(i) for i in file_ids) if file_id is not None]
    if not wanted:
        return []
    rows = (
        await db.execute(
            update(FileAttachment)
            .where(
                FileAttachment.id.in_(wanted),
                FileAttachment.user_id == user_id,
                FileAttachment.source == queued_file_source(channel),
                FileAttachment.message_id.is_(None),
            )
            .values(source=f"channel:{channel}")
            .returning(FileAttachment)
        )
    ).scalars().all()
    by_id = {row.id: row for row in rows}
    return [by_id[file_id] for file_id in wanted if file_id in by_id]


async def discard_queued_files(file_ids: list[str]) -> int:
    """Delete the files of a queued message that no message shows, object then row. Returns how many.

    Called once the message is refused after its files were stored, once its
    turn is over - answered, refused, or never reaching the router: a file
    the turn did not attach to the stored message is named nowhere else -
    and once it is dropped. A file on a message is kept. A file the turn
    claimed goes back under the mark first, so one whose object the store
    did not delete is the upload purge's. Never raises.
    """
    wanted = [file_id for file_id in (safe_uuid(i) for i in file_ids) if file_id is not None]
    if not wanted:
        return 0
    from app.core.database import async_session
    from app.services.widget_uploads import purge_unsent_files

    try:
        async with async_session() as db:
            await db.execute(
                update(FileAttachment)
                .where(
                    FileAttachment.id.in_(wanted),
                    FileAttachment.source.in_([f"channel:{channel}" for channel in CONTINUABLE_CHANNELS]),
                    FileAttachment.conversation_id.is_(None),
                    FileAttachment.message_id.is_(None),
                )
                .values(source=FileAttachment.source + _QUEUED_FILE_MARK)
                .execution_options(synchronize_session=False)
            )
            await db.commit()
        return await purge_unsent_files(
            and_(
                FileAttachment.id.in_(wanted),
                FileAttachment.conversation_id.is_(None),
                _waiting_files_clause(),
            )
        )
    except Exception as exc:
        logger.warning(
            "Could not delete the %d file(s) of a queued message: %s",
            len(wanted),
            format_exception_message(exc),
        )
        return 0


async def claim_channel_message(inbound: Any) -> bool:
    """``False`` when *inbound* was already received: a Slack retry or a Teams redelivery.

    Read before a message takes the turn or is queued, so a redelivered event
    is neither answered nor queued twice. Fails open.
    """
    from app.services import chat_turn_guard

    if not inbound.channel_message_id or not inbound.channel_thread_id:
        return True
    try:
        return await chat_turn_guard.claim_channel_message(
            inbound.channel.value, inbound.channel_thread_id, inbound.channel_message_id
        )
    except Exception as exc:
        logger.debug("Channel message claim unavailable: %s", format_exception_message(exc))
        return True


async def take_turn_or_queue(
    turn: ChannelTurn,
    conversation_id: object,
    inbound: Any,
    *,
    store_files: Callable[[], Awaitable[list[str]]] | None = None,
) -> str | None:
    """Give *inbound* the conversation's turn, or queue it behind the one running.

    ``None`` when *turn* holds the conversation now; otherwise what the sender
    is told: queued (:data:`QUEUED_NOTICE`, answered as the next turn) or the
    queue is full (:data:`QUEUE_FULL_NOTICE`). Arrival order holds: a message
    that finds others waiting queues behind them even when no turn runs. A
    message about to be queued has its files stored first (*store_files*,
    their ``FileAttachment`` ids), while their platform URLs still work. A
    guard that cannot be read runs the message unguarded, as before.
    """
    from app.services import chat_turn_guard

    cid = str(conversation_id)
    try:
        waiting = await chat_turn_guard.channel_queue_length(cid)
        # A message a dead holder claimed is owed first, too.
        owed = bool(waiting) or await chat_turn_guard.channel_messages_owed(cid)
    except Exception as exc:
        logger.warning(
            "Channel queue of conversation %s unreadable: %s", cid, format_exception_message(exc)
        )
        waiting, owed = 0, False
    if not owed and await turn.acquire(cid):
        return None
    if waiting >= chat_turn_guard.CHANNEL_QUEUE_MAX_MESSAGES:
        logger.info(
            "%s message not queued: the queue of conversation %s is full",
            inbound.channel.value,
            cid,
        )
        return QUEUE_FULL_NOTICE
    file_ids = await store_files() if store_files and inbound.file_attachments else []
    try:
        place = await chat_turn_guard.enqueue_channel_message(cid, _queue_item(inbound, file_ids))
    except Exception as exc:
        logger.warning(
            "Could not queue a %s message for conversation %s: %s",
            inbound.channel.value,
            cid,
            format_exception_message(exc),
        )
        await discard_queued_files(file_ids)
        return TURN_BUSY_NOTICE
    if place is None:
        logger.info(
            "%s message not queued: the queue of conversation %s is full",
            inbound.channel.value,
            cid,
        )
        await discard_queued_files(file_ids)
        return QUEUE_FULL_NOTICE
    logger.info(
        "%s message queued behind the turn of conversation %s (place %d)",
        inbound.channel.value,
        cid,
        place,
    )
    # The turn may have ended between the check and the queueing: its release
    # then found nothing to start, so this drain does (or finds it taken).
    schedule_queue_drain(cid)
    return QUEUED_NOTICE


def schedule_queue_drain(conversation_id: object) -> None:
    """Answer the conversation's next queued message once a processing slot is free."""
    from app.channels.base import spawn_channel_task

    spawn_channel_task(drain_channel_queue(str(conversation_id)), name="channel-queue-drain")


#: How many times a queued message is started: one whose process died while
#: answering it, or whose adapter failed before the router took it, is
#: started once more, then dropped.
QUEUED_MESSAGE_ATTEMPTS = 2


def _queued_too_long(item: dict, now: datetime) -> bool:
    """Whether *item* waited longer than the queue lives: dropped, not answered that late."""
    queued_at = item.get("queued_at")
    try:
        at = datetime.fromisoformat(queued_at) if isinstance(queued_at, str) else None
    except ValueError:
        return False
    if at is None:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return now - at > timedelta(seconds=CHANNEL_QUEUE_TTL_SECONDS)


async def _claim_queued_group(conversation_id: str, token: str) -> tuple[list[dict], list[dict]]:
    """Claim the next message and the ones of the same thread right behind it; and those met on the way that waited too long."""
    from app.services import chat_turn_guard

    group: list[dict] = []
    expired: list[dict] = []
    now = datetime.now(UTC)
    while len(group) < chat_turn_guard.CHANNEL_QUEUE_MAX_MESSAGES:
        if group:
            following = _decode_queue_item(await chat_turn_guard.peek_channel_queue(conversation_id))
            if following is None or not _same_thread(group[0], following):
                break
        raw = await chat_turn_guard.claim_queued_channel_message(conversation_id, token)
        if raw is None:
            break
        item = _decode_queue_item(raw)
        if item is None:
            logger.warning("Dropped an unreadable queued message of conversation %s", conversation_id)
            continue
        (expired if _queued_too_long(item, now) else group).append(item)
    if expired:
        logger.info(
            "Dropped %d queued message(s) of conversation %s that waited over %d s",
            len(expired),
            conversation_id,
            CHANNEL_QUEUE_TTL_SECONDS,
        )
    return group, expired


async def _requeue_claimed(conversation_id: str, token: str, *, recovered: bool) -> None:
    """Put the claimed messages back at the head of the queue, or drop the ones started :data:`QUEUED_MESSAGE_ATTEMPTS` times.

    What a drain whose adapter failed before the router took them did not
    answer, or what a holder that died left claimed (*recovered*). The turn
    of a dead holder may have stored them already: never stored twice, they
    are then dropped, the sender told when no reply was stored either. The
    files of a dropped message are deleted.
    """
    from app.services import chat_turn_guard

    claimed = await chat_turn_guard.claimed_channel_messages(conversation_id)
    if not claimed:
        return
    items = [item for item in map(_decode_queue_item, claimed) if item is not None]
    answered = await _stored_turn(conversation_id, items) if recovered else None
    if answered is not None:
        if await chat_turn_guard.requeue_claimed_channel_messages(conversation_id, token, []):
            logger.warning(
                "Not answering %d queued message(s) of conversation %s again: the turn of a "
                "process that died stored them%s",
                len(items),
                conversation_id,
                "" if answered else ", and no reply",
            )
            if not answered:
                await _tell_dropped(items)
            await discard_queued_files(_queued_file_ids(items))
        return
    again: list[dict] = []
    dropped: list[dict] = []
    for item in items:
        attempts = item.get("attempts")
        item["attempts"] = (attempts if isinstance(attempts, int) else 0) + 1
        (dropped if item["attempts"] >= QUEUED_MESSAGE_ATTEMPTS else again).append(item)
    if not await chat_turn_guard.requeue_claimed_channel_messages(
        conversation_id, token, [json.dumps(item) for item in again]
    ):
        return
    if dropped:
        logger.warning(
            "Dropped %d queued message(s) of conversation %s, started %d times without an answer",
            len(dropped),
            conversation_id,
            QUEUED_MESSAGE_ATTEMPTS,
        )
        await _tell_dropped(dropped)
        await discard_queued_files(_queued_file_ids(dropped))


async def _stored_turn(conversation_id: str, items: list[dict]) -> bool | None:
    """Whether a turn stored *items* as a user message: ``True`` with a reply after it, ``False`` without, ``None`` never.

    Read off their platform message ids, which the router stores on the user
    message. A database that cannot be read is ``None``: started again.
    """
    from app.core.database import async_session
    from app.models.chat import Message

    ids = [str(mid) for mid in (item.get("channel_message_id") for item in items) if mid]
    cid = safe_uuid(conversation_id)
    if not ids or cid is None:
        return None
    try:
        async with async_session() as db:
            sequence = await db.scalar(
                select(Message.sequence)
                .where(
                    Message.conversation_id == cid,
                    Message.role == "user",
                    Message.channel_message_id.in_(ids),
                )
                .order_by(Message.sequence.desc())
                .limit(1)
            )
            if sequence is None:
                return None
            reply = await db.scalar(
                select(Message.id)
                .where(
                    Message.conversation_id == cid,
                    Message.role == "assistant",
                    Message.sequence > sequence,
                )
                .limit(1)
            )
    except Exception as exc:
        logger.warning(
            "Could not read whether conversation %s stored its queued message: %s",
            conversation_id,
            format_exception_message(exc),
        )
        return None
    return reply is not None


async def _tell_dropped(items: list[dict]) -> None:
    """Tell the sender of each thread of *items*, dropped without an answer, to send them again. Never raises.

    Through the adapter that would have answered (``post_queued_notice``), in
    the sender's language; a bot no longer connected tells nobody.
    """
    from app.services.channel_notices import notice_language

    threads: list[list[dict]] = []
    for item in items:
        same = next((group for group in threads if _same_thread(group[0], item)), None)
        if same is None:
            threads.append([item])
        else:
            same.append(item)
    for group in threads:
        inbound = _inbound_from_queue(group)
        try:
            sender = await _sender(inbound.user_email)
        except Exception:
            logger.debug("Queued message sender lookup failed", exc_info=True)
            sender = None
        language = notice_language(sender)
        try:
            post_queued_notice, _run = _adapter_of(inbound)
            told = await post_queued_notice(inbound, notice("channel_queued_dropped", language))
        except Exception as exc:
            logger.warning(
                "Could not tell a %s sender their queued message was dropped: %s",
                inbound.channel.value,
                format_exception_message(exc),
            )
            continue
        if not told:
            logger.warning(
                "Could not tell a %s sender their queued message was dropped: the bot did not post",
                inbound.channel.value,
            )


async def _sender(email: str | None) -> Any:
    """The platform user a queued message came from, ``None`` when unknown."""
    if not email:
        return None
    from sqlalchemy import func

    from app.core.database import async_session
    from app.models.user import User

    async with async_session() as db:
        return await db.scalar(select(User).where(func.lower(User.email) == email.lower()).limit(1))


def _adapter_of(inbound: Any) -> tuple[Callable[..., Awaitable[Any]], Callable[..., Awaitable[Any]]]:
    """The ``(post_queued_notice, run_queued_message)`` of *inbound*'s channel adapter."""
    if inbound.channel.value == "slack":
        from app.channels.slack import post_queued_notice, run_queued_message
    else:
        from app.channels.teams import post_queued_notice, run_queued_message
    return post_queued_notice, run_queued_message


async def _run_queued_message(inbound: Any) -> None:
    """Hand *inbound* to its adapter, which answers it like a message just received."""
    _post, run_queued_message = _adapter_of(inbound)
    await run_queued_message(inbound)


async def drain_channel_queue(conversation_id: str) -> bool:
    """Answer the conversation's next queued message(s) as its next turn. ``True`` when one ran.

    The lock is taken BEFORE anything is claimed and only its holder claims,
    so two processes never run the same message; a drain that finds the turn
    taken does nothing, since that turn's release drains again. A claimed
    message stays claimed until its turn answers or refuses it (the router's
    :meth:`ChannelTurn.release`, or here when it never reached the router):
    one a dead process left is put back first and started again, at most
    :data:`QUEUED_MESSAGE_ATTEMPTS` times in all, unless that process's turn
    stored it already (:func:`_requeue_claimed`). A message dropped without
    an answer - started too often, or queued longer than the queue lives - is
    told so on its thread (:func:`_tell_dropped`). Consecutive messages of
    one thread are folded into one turn and one reply, as the web folds a
    message sent mid-turn into the turn that answers it; a message of
    another thread waits for the turn after. The turn is handed to the
    router through :data:`_QUEUED_TURN`. Then the files stored for the
    messages that the turn did not attach are deleted
    (:func:`discard_queued_files`), and those of a message dropped.
    """
    from app.services import chat_turn_guard

    cid = str(conversation_id)
    token: str | None = None
    try:
        left = await chat_turn_guard.claimed_channel_messages(cid)
        raw_head = left[0] if left else await chat_turn_guard.peek_channel_queue(cid)
        if raw_head is None:
            return False
        head = _decode_queue_item(raw_head)
        token = await chat_turn_guard.acquire_turn(
            cid, channel=head["channel"] if head else CONTINUABLE_CHANNELS[0]
        )
        if not token:
            return False
        await _requeue_claimed(cid, token, recovered=True)
        group, expired = await _claim_queued_group(cid, token)
    except Exception as exc:
        logger.warning(
            "Could not drain the channel queue of conversation %s: %s",
            cid,
            format_exception_message(exc),
        )
        if token:
            # What it claimed stays claimed; the sweep starts it again.
            with contextlib.suppress(Exception):
                await chat_turn_guard.release_turn(cid, token, hand_over=False)
        return False
    if expired:
        await _tell_dropped(expired)
        await discard_queued_files(_queued_file_ids(expired))
    if not group:
        with contextlib.suppress(Exception):
            await chat_turn_guard.answer_claimed_channel_messages(cid, token)
        with contextlib.suppress(Exception):
            await chat_turn_guard.release_turn(cid, token)
        return False
    queued = _QueuedTurn(
        conversation_id=cid,
        channel=group[0]["channel"],
        token=token,
        file_ids=_queued_file_ids(group),
    )
    logger.info(
        "Answering %d queued %s message(s) of conversation %s",
        len(group),
        queued.channel,
        cid,
    )
    context = _QUEUED_TURN.set(queued)
    # Until it returns: a drain cancelled meanwhile did not answer it either.
    failed = True
    try:
        await _run_queued_message(_inbound_from_queue(group))
        failed = False
    except QueuedMessageUndeliverableError as exc:
        logger.warning(
            "A queued %s message of conversation %s could not be answered: %s",
            queued.channel,
            cid,
            exc,
        )
    except Exception as exc:
        logger.error(
            "A queued %s message of conversation %s failed: %s",
            queued.channel,
            cid,
            format_exception_message(exc),
            exc_info=True,
        )
    finally:
        _QUEUED_TURN.reset(context)
        if queued.resumed:
            # A shutdown cut it and put it back: the next process answers it,
            # with its files.
            pass
        elif queued.adopted:
            # The router answered it, and forgot its claim on release.
            await discard_queued_files(queued.file_ids)
        else:
            with contextlib.suppress(Exception):
                if failed:
                    await _requeue_claimed(cid, token, recovered=False)
                else:
                    await chat_turn_guard.answer_claimed_channel_messages(cid, token)
            with contextlib.suppress(Exception):
                await chat_turn_guard.release_turn(cid, token)
            if not failed:
                await discard_queued_files(queued.file_ids)
    return True


# ── A turn cut by a platform update (rolling deploy, restart) ────────────

#: Set in the metadata of a queued item put back by :func:`hand_on_cut_turn`,
#: so of the message its resuming turn answers: that turn replays the cut
#: turn's checkpointed tool work (the router), and is never put back again.
RESUMED_META_KEY = "resumed_after_cut"

#: Set on the inbound message once its cut turn was handed on: its adapter
#: then says nothing more about the cut.
CUT_HANDLED_META_KEY = "turn_cut_handled"

#: How long a cut turn may spend handing its message on before its lock goes.
CUT_HANDOFF_SECONDS = 10.0

RESUMED = "resumed"
TOLD = "told"
ANSWERED = "answered"


async def _mark_files_waiting(file_ids: list[str]) -> None:
    """Mark the files a cut turn claimed as queued again, for the turn that resumes it."""
    wanted = [file_id for file_id in (safe_uuid(i) for i in file_ids) if file_id is not None]
    if not wanted:
        return
    from app.core.database import async_session

    async with async_session() as db:
        await db.execute(
            update(FileAttachment)
            .where(
                FileAttachment.id.in_(wanted),
                FileAttachment.source.in_([f"channel:{channel}" for channel in CONTINUABLE_CHANNELS]),
                FileAttachment.message_id.is_(None),
            )
            .values(source=FileAttachment.source + _QUEUED_FILE_MARK)
            .execution_options(synchronize_session=False)
        )
        await db.commit()


async def _post_turn_notice(inbound: Any, key: str) -> bool:
    """Post the notice *key* on *inbound*'s thread through its adapter. Never raises."""
    from app.services.channel_notices import notice_language

    try:
        sender = await _sender(inbound.user_email)
    except Exception:
        logger.debug("Cut turn sender lookup failed", exc_info=True)
        sender = None
    try:
        post_queued_notice, _run = _adapter_of(inbound)
        return bool(await post_queued_notice(inbound, notice(key, notice_language(sender))))
    except Exception as exc:
        logger.warning(
            "Could not tell a %s sender their turn was cut by a platform update: %s",
            inbound.channel.value,
            format_exception_message(exc),
        )
        return False


async def hand_on_cut_turn(turn: ChannelTurn, inbound: Any) -> str | None:
    """Put the message of a turn a shutdown cut back at the head of its queue, and say so.

    Called while *turn* still holds the conversation, before its lock goes
    without a hand-over (:meth:`ChannelTurn.release`): the scheduler's sweep of
    the next process answers it from the queue (:func:`sweep_channel_queues`,
    :func:`drain_channel_queue`), and a queued message passes the redelivery
    claim by design. Nothing is posted in the thread: the next process answers
    the message as if the cut never happened (issue #10772).

    Not put back, and the sender told to send it again (``channel_turn_cut``):
    a message already resumed ``cut_turn_resume.MAX_AUTO_RESUMES`` times, the
    web turn's cap (:func:`chat_turn_guard.claim_channel_resume`; a cut the
    turn itself causes would loop),
    one stored without a reply (it would be stored twice), one whose files
    were not stored yet (their platform URLs do not outlive a deploy). A
    message whose reply was stored is left alone: answered, never twice.
    Returns :data:`RESUMED`, :data:`TOLD`, :data:`ANSWERED`, or ``None`` when
    the turn is not one this handles. Never raises.
    """
    from app.core.worker_shutdown import cut_cause
    from app.services import chat_turn_guard

    cid, token = turn.conversation_id, turn.token
    if (
        not cid
        or not token
        or inbound.channel.value not in CONTINUABLE_CHANNELS
        or not inbound.channel_message_id
        or not inbound.channel_thread_id
    ):
        return None
    try:
        item = json.loads(_queue_item(inbound, turn.stored_file_ids))
        answered = await _stored_turn(cid, [item])
        if answered:
            outcome = ANSWERED
        elif (
            answered is False
            or (inbound.file_attachments and not turn.stored_file_ids)
            or not await chat_turn_guard.claim_channel_resume(
                inbound.channel.value, inbound.channel_thread_id, inbound.channel_message_id
            )
        ):
            outcome = TOLD
        else:
            item.setdefault("metadata", {})[RESUMED_META_KEY] = True
            await _mark_files_waiting(turn.stored_file_ids)
            # Replaces the claim of a queued turn, which this item answers.
            if not await chat_turn_guard.requeue_claimed_channel_messages(cid, token, [json.dumps(item)]):
                outcome = TOLD
            else:
                await chat_turn_guard.index_channel_queue(cid)
                queued = _QUEUED_TURN.get()
                if turn.queued and queued is not None:
                    queued.resumed = True
                outcome = RESUMED
    except Exception as exc:
        logger.warning(
            "Could not put back the %s message of conversation %s that a shutdown cut: %s",
            inbound.channel.value,
            cid,
            format_exception_message(exc),
        )
        outcome = TOLD
    if inbound.metadata is not None:
        inbound.metadata[CUT_HANDLED_META_KEY] = True
    logger.warning(
        "The %s turn of conversation %s was cut by a platform update (%s): %s",
        inbound.channel.value,
        cid,
        cut_cause() or "the process shut down",
        {
            RESUMED: "its message is put back for the next process",
            TOLD: "not resumed, the sender is told to send it again",
            ANSWERED: "its reply was already stored",
        }[outcome],
    )
    if outcome == TOLD:
        if turn.queued:
            with contextlib.suppress(Exception):
                await chat_turn_guard.answer_claimed_channel_messages(cid, token)
        await _post_turn_notice(inbound, "channel_turn_cut")
    return outcome


async def sweep_channel_queues() -> int:
    """Drain the queues no release will: their turn's process died holding the lock.

    Run by the scheduler tick and, every
    :data:`cut_turn_resume.SWEEP_INTERVAL_SECONDS`, by every process
    (:func:`cut_turn_resume.run_sweep_loop`), so a turn a rollout cut and put
    back (:func:`hand_on_cut_turn`) is answered by the next pod within seconds,
    not at the next scheduler window (#11924). A conversation whose lock is
    gone (its TTL ran out, or released without a hand-over) while messages
    wait, or while a message it claimed was never answered, gets a drain; one
    with neither leaves the index. A process asked to stop drains nothing: a
    turn started there is cut again by its own drain and spends one of the
    message's automatic resumes. Returns how many drains were scheduled.
    """
    from app.core.worker_shutdown import shutdown_request
    from app.services import chat_turn_guard

    if shutdown_request() is not None:
        return 0
    scheduled = 0
    for cid in await chat_turn_guard.queued_conversations():
        try:
            if not await chat_turn_guard.channel_messages_owed(cid):
                await chat_turn_guard.forget_queued_conversation(cid)
                continue
            if await chat_turn_guard.is_turn_active(cid):
                continue
        except Exception:
            logger.debug("Channel queue sweep skipped conversation %s", cid, exc_info=True)
            continue
        schedule_queue_drain(cid)
        scheduled += 1
    return scheduled
