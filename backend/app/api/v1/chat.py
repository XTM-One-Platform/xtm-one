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

"""Chat API endpoints – conversations and real-time messaging.

REST endpoints for conversation management, and the runner the web chat's
HTTP + SSE send streams its turns through (``run_chat_connection``).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, BeforeValidator, Field, StringConstraints, model_validator
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    AI_TEMPORARILY_UNAVAILABLE_DETAIL,
    get_current_user,
    require_ai_enabled,
    require_chat_enabled,
)
from app.api.v1.helpers import (
    enqueue_document_processing,
    owned_conversation,
    parse_conversation_id,
    parse_path_id,
    parse_uuid,
)
from app.channels.base import strip_artifact_markers
from app.channels.router import get_conversation_history, move_messages_to_end, store_message
from app.core.database import get_db
from app.core.feature_flags import is_feature_enabled
from app.core.rate_limit import enforce_rate_limit, refund_rate_limit
from app.core.redaction import known_secrets_scoped, redact_frame, redact_known_secrets_for_display, renew_known_secrets
from app.core.redis import redis_client
from app.core.security_options import security_option
from app.core.sessions import (
    SESSION_RECHECK_SECONDS,
    SessionEndedError,
    SessionEndReason,
    WebSession,
    recheck_session,
)
from app.core.shutdown_finalizers import start_shutdown_finalizer, wait_for_shutdown_finalizer
from app.core.utils import (
    chat_processing_key,
    format_exception_message,
    is_graceful_shutdown_cancellation,
    safe_uuid,
)
from app.core.worker_shutdown import cut_cause, drain_cut_cause
from app.llm.base import INTERNAL_FILE_SOURCES
from app.models.chat import Conversation, Message, MessageFeedback
from app.models.user import User
from app.services import chat_turn_stream, turn_checkpoint
from app.services.annotations import normalize_comment
from app.services.channel_continuation import (
    hold_off_channel_turns,
    list_bindings,
    release_channel_hold,
    web_continuation_refusal,
    web_listable_clause,
    web_send_refusal,
)
from app.services.chat_messages import (
    attachment_download_url,
    attachment_meta_from_file_attachment,
    message_cursor_filter,
    message_is_hidden,
    parse_attachments,
    parse_message_metadata,
    read_visible_message_page,
    sent_message_text,
)
from app.services.chat_saved_tool_selection import persist_tool_selection, saved_tool_selection
from app.services.chat_selection import refused_chat_selection, usable_saved_selection
from app.services.chat_turn_guard import TURN_MARKER_TTL_SECONDS, SteeringPayload
from app.services.chat_web_transport import StreamConsumerGoneError, session_lock_state
from app.services.context_usage import ContextUsageTracker, read_context_usage
from app.services.conversation_references import read_conversation_refs
from app.services.cut_turn_resume import AUTO_RESUME_META_KEY, enqueue_cut_turn
from app.services.deep_investigation_chat import (
    run_deep_investigation_turn,
)
from app.services.desktop_session import load_desktop_context
from app.services.failure_causes import (
    TOOL_OVERFLOW_DETAIL,
    FailureCause,
    classify_run_failure,
)
from app.services.feedback_criteria import ValidatedScore, replace_scores
from app.services.file_access import authorize_file_access
from app.services.file_responses import (
    INLINE_FILE_CONTENT_TYPES,
    file_not_modified,
    file_response_headers,
    token_max_age,
)
from app.services.general_assistant_link import conversation_stand_ins, turn_stand_in
from app.services.integration_credentials import (
    UNAVAILABLE_INTEGRATIONS_META_KEY,
    clean_unavailable_integrations,
)
from app.services.interrupted_turn import (
    INTERRUPTED_CAUSE_META_KEY,
    INTERRUPTED_META_KEY,
    INTERRUPTED_RESUMABLE,
    INTERRUPTED_STATE_META_KEY,
    TurnProgress,
    interrupted_reply,
)
from app.services.investigation_pack_builder import MAX_EXPOSED_SETTINGS, MAX_STEPS
from app.services.langfuse_tracing import arm_root_trace_link, load_latest_langfuse_ids
from app.services.mention_dispatch import (
    ANSWERED_BY_KEY,
    DISPATCH_BUDGET_SECONDS,
    MENTION_FAILED_KEY,
    MENTIONED_AGENT_HANDOVER_TYPES,
    MENTIONED_AGENTS_FIELD,
    FoldedMentions,
    MentionReply,
    MentionTarget,
    TurnMetering,
    TurnRefusedError,
    answer_folded_mentions,
    dispatch_mentions,
    folded_mention_history,
    folded_mention_ids,
    folded_mentions,
    quota_refused,
    reply_metadata,
    resolve_mention_targets,
    run_on_own_session,
)
from app.services.output_format import read_content_format
from app.services.presentation_studio import load_studio_context
from app.services.runner_activity import (
    RUNNER_ACTIVITY_META_KEY,
    RunnerActivityLog,
    sanitize_activity_list,
)
from app.services.tracing.dispatcher import get_tracing_dispatcher
from app.services.tracing.feedback_scores import (
    FEEDBACK_SCORE_NAME,
    begin_score_action,
    end_score_action,
    rater_score_payload,
)
from app.services.visibility import SegmentCounts, Visibility, segment_counts

# Aliased: this module raises nothing of the kind itself, and the name at the
# except clause should say which case it is catching.
from app.tools.base import HumanInputRequestedError as _GeneralChatPaused

logger = logging.getLogger(__name__)

#: How many source packs one message may confirm. The picker offers a handful;
#: this only bounds what raw client JSON can ask the server to resolve.
MAX_CONFIRMED_PACKS = 16
#: Bounds on the pack options one message may carry (`pack_options_from_frame`):
#: every option one built pack can offer, a switch per step plus each exposed
#: setting (the built-in packs offer fewer).
MAX_PACK_OPTIONS = MAX_STEPS + MAX_EXPOSED_SETTINGS
MAX_PACK_OPTION_TEXT = 64


class InvalidPackOptionsError(ValueError):
    """A frame's pack options that cannot be read whole."""


def pack_options_from_frame(raw: Any) -> dict[str, dict[str, str]] | None:
    """The pack options a Deep Investigation frame confirmed, by pack slug.

    Raw client JSON, read whole or refused (`InvalidPackOptionsError`): a
    choice dropped here would run at its default, and the default of a step
    the user switched off is on. Keys and values are stripped text within
    bounds; a choice the pack does not offer is refused when the run resolves
    its packs.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict) or len(raw) > MAX_CONFIRMED_PACKS:
        raise InvalidPackOptionsError("source_pack_options is not a bounded object")

    def _text(value: Any) -> str:
        if not isinstance(value, str):
            raise InvalidPackOptionsError("a pack option is not text")
        value = value.strip()
        if not value or len(value) > MAX_PACK_OPTION_TEXT:
            raise InvalidPackOptionsError("a pack option is empty or too long")
        return value

    options: dict[str, dict[str, str]] = {}
    seen_slugs: set[str] = set()
    for slug, chosen in raw.items():
        if not isinstance(chosen, dict) or len(chosen) > MAX_PACK_OPTIONS:
            raise InvalidPackOptionsError("a pack's options are not a bounded object")
        slug = _text(slug)
        kept = {_text(key): _text(value) for key, value in chosen.items()}
        # Two keys alike once stripped would keep one choice of the two.
        if slug in seen_slugs or len(kept) != len(chosen):
            raise InvalidPackOptionsError("two pack options name the same thing")
        seen_slugs.add(slug)
        if kept:
            options[slug] = kept
    return options or None


router = APIRouter()

# Per-user ceiling on chat message sends (REST send-message endpoint and the
# web chat's streamed send share the same bucket). Generous for human typing,
# tight enough to blunt scripted abuse of the agentic loop.
CHAT_MESSAGES_PER_MINUTE = 30

# User-facing detail returned when a stored file's S3 key no longer exists
# (deleted, expired via lifecycle policy, or never persisted). Surfaced as a
# 404 instead of leaking the raw storage error as a 500.
FILE_UNAVAILABLE_DETAIL = "This file is no longer available"

# --- Message pagination -------------------------------------------
# The conversation detail endpoint returns the LATEST page of messages
# (previously it returned the OLDEST 500 and silently dropped everything
# newer). Older history is fetched incrementally through
# ``GET /conversations/{id}/messages?before=<message_id>&limit=N``.

# Default number of most-recent messages returned by the detail endpoint.
DEFAULT_MESSAGE_PAGE_SIZE = 100
# Hard ceiling on any single message page (mirrors the legacy 500 cap).
MAX_MESSAGE_PAGE_SIZE = 500
# Default page size for the older-history endpoint.
DEFAULT_OLDER_PAGE_SIZE = 50

# Roles surfaced to the chat UI (tool messages are internal).
CHAT_MESSAGE_ROLES = ("user", "assistant", "system")

# --- Conversation files panel -------------------------------------
# Hard ceiling on a single page of the conversation-files listing. Long-running
# conversations with a chatty ``generate_file`` agent can accumulate hundreds of
# rows; the panel pages rather than streaming the whole set.
DEFAULT_CONVERSATION_FILES_PAGE_SIZE = 100
MAX_CONVERSATION_FILES_PAGE_SIZE = 500

# --- Admin break-glass access to conversation files ---------------
# A non-owner admin may list another user's conversation files for support and
# debugging, but only after explicitly acknowledging the confidentiality
# expectation. The acknowledgement is written to the audit trail AND grants a
# short-lived, per-(admin, conversation) Redis lease, so one deliberate click
# is bounded in time and can never become standing ambient access.
#
# The lease deliberately lives in Redis rather than the session/JWT: it must be
# revocable and must expire on its own even if the admin keeps the tab open.
CONVERSATION_FILE_ACCESS_GRANT_TTL_SECONDS = 15 * 60

# Machine-readable ``detail`` returned when a non-owner admin lists files with
# no live acknowledgement. The frontend switches on this exact string to raise
# the confidentiality gate instead of rendering a generic error, so it is part
# of the API contract — do not reword it without updating
# ``ConversationFilesPanel`` / ``useConversationFiles``.
CONFIDENTIALITY_ACK_REQUIRED_DETAIL = "confidentiality_acknowledgement_required"

# Origin buckets surfaced by the files panel, derived from
# ``FileAttachment.source``. Kept as an explicit mapping rather than a
# ``startswith`` chain so a new writer of ``source`` shows up as ``other``
# (visible, unclassified) instead of being silently mislabelled as something
# the user uploaded.
#
# ``chat``          – composer upload (api upload_file_to_conversation)
# ``channel:*``     – inbound attachment from Slack/Teams/Discord/Telegram
# ``agent_generated`` – ``generate_file`` builtin tool output
# ``agent``         – agent-produced media (e.g. generated images)
# ``mcp``           – MCP tool output
_UPLOADED_FILE_SOURCES = frozenset({"chat"})
_GENERATED_FILE_SOURCES = frozenset({"agent_generated", "agent", "mcp"})
# Internal files the user never lists: a tool result an interrupted turn kept
# whole for its resume (``turn_checkpoint.SPILL_SOURCE``). The resuming agent
# still reads it by the id its replay names; it is no output of the
# conversation and may carry what the tool read (a mailbox, a page).
_INTERNAL_FILE_SOURCES = INTERNAL_FILE_SOURCES


def _listed_file_predicate():
    """``FileAttachment.source`` predicate keeping the files a user lists."""
    from app.models.file_attachment import FileAttachment

    return or_(
        FileAttachment.source.is_(None),
        func.lower(FileAttachment.source).notin_(sorted(_INTERNAL_FILE_SOURCES)),
    )


def _conversation_file_origin(source: str | None) -> str:
    """Bucket a ``FileAttachment.source`` into a panel origin label."""
    src = (source or "").strip().lower()
    if src in _UPLOADED_FILE_SOURCES or src.startswith("channel:"):
        return "uploaded"
    if src in _GENERATED_FILE_SOURCES:
        return "generated"
    return "other"


def _origin_sql_predicate(origin: str | None):
    """Translate an origin bucket into a ``FileAttachment.source`` predicate.

    Kept in lockstep with :func:`_conversation_file_origin` — the Python
    mapping labels each row for the response, this builds the equivalent SQL so
    filtering and counting stay server-side instead of loading every row of a
    long conversation to slice it in memory. ``None`` means "no filter".
    """
    from app.models.file_attachment import FileAttachment

    if origin is None:
        return None
    # ``_conversation_file_origin`` lower-cases before matching, so this must
    # too — otherwise a row written as ``"Chat"`` would be *labelled*
    # ``uploaded`` in the response but excluded by an ``origin=uploaded``
    # filter, and the buckets would no longer partition the set. Every writer
    # currently uses a lowercase literal; normalising here means a future one
    # that doesn't cannot introduce a silent filtering hole. No index is lost:
    # the ``conversation_id`` predicate does the narrowing, and ``source`` is
    # unindexed either way.
    source_lc = func.lower(FileAttachment.source)
    channel_like = source_lc.like("channel:%")
    if origin == "uploaded":
        return or_(source_lc.in_(sorted(_UPLOADED_FILE_SOURCES)), channel_like)
    if origin == "generated":
        return source_lc.in_(sorted(_GENERATED_FILE_SOURCES))
    # ``other`` — everything the two known buckets do not claim. ``source`` is
    # NOT NULL in the schema, but a NULL would fall out of both ``NOT IN`` and
    # ``NOT LIKE`` under SQL's three-valued logic, so match it explicitly:
    # ``_conversation_file_origin(None)`` already labels such a row ``other``,
    # and this keeps ``uploaded + generated + other == all`` true either way.
    return or_(
        FileAttachment.source.is_(None),
        and_(
            source_lc.notin_(
                sorted(_UPLOADED_FILE_SOURCES | _GENERATED_FILE_SOURCES)
            ),
            ~channel_like,
        ),
    )


def conversation_file_access_grant_key(
    admin_id: object, conversation_id: object
) -> str:
    """Redis key for an admin's break-glass lease on one conversation.

    Single source of truth for the key format — the writer
    (:func:`acknowledge_conversation_file_access`) and the reader
    (:func:`_has_conversation_file_access_grant`) must not drift, or the
    acknowledgement would be recorded in the audit trail while the listing
    kept returning 403.
    """
    return f"chat:files:break-glass:{admin_id}:{conversation_id}"

# Max reasoning prose (chars) persisted in assistant message metadata and
# echoed in the stream's ``done`` frame — keeps metadata_json and the frame small
# even for very chatty reasoning models.
_REASONING_META_MAX = 20_000


class ReasoningAccumulator:
    """Head+tail bounded accumulator for reasoning prose.

    The previous accumulation kept only the FIRST ``_REASONING_META_MAX``
    chars, so on long budget-exhausted turns the FINAL wind-down reasoning —
    the part that explains why the agent stopped — was exactly what got
    silently dropped from the reasoning-details panel.  This keeps the head
    (the model's initial plan) plus a rolling tail (the latest reasoning,
    including the wind-down) and joins them with an elision marker only when
    characters were actually discarded.  The rendered result never exceeds
    ``_REASONING_META_MAX``.

    Shared by the web chat and the embedded platform chat proxy
    (``api/v1/platform.py``) — single source of truth for the cap semantics.
    """

    HEAD_MAX = 12_000
    ELISION = "\n\n[… earlier reasoning elided …]\n\n"
    TAIL_MAX = _REASONING_META_MAX - HEAD_MAX - len(ELISION)

    def __init__(self) -> None:
        self._head = ""
        self._tail = ""
        self._elided = False

    def add(self, text: str) -> None:
        if not text:
            return
        if len(self._head) < self.HEAD_MAX:
            take = self.HEAD_MAX - len(self._head)
            self._head += text[:take]
            text = text[take:]
        if text:
            combined = self._tail + text
            if len(combined) > self.TAIL_MAX:
                combined = combined[-self.TAIL_MAX :]
                self._elided = True
            self._tail = combined

    def render(self) -> str:
        if not self._tail:
            return self._head
        joiner = self.ELISION if self._elided else ""
        return f"{self._head}{joiner}{self._tail}"

    def reset(self) -> None:
        self._head = ""
        self._tail = ""
        self._elided = False


# Paragraphs shorter than this are never dropped by
# ``strip_reasoning_echoed_in_reply``: a two-word reasoning line that also
# happens to appear in the answer is not the duplicate the filter targets.
_REASONING_ECHO_MIN_CHARS = 40


def strip_reasoning_echoed_in_reply(reasoning: str, reply: str) -> str:
    """Drop reasoning paragraphs that are repeated verbatim in *reply*.

    The hold-back window routes a short pre-tool answer (under
    ``_PREAMBLE_HOLDBACK_CHARS``) into the reasoning pane as ``thinking_text``.
    When the turn then closes with a one-line recap, the chat deliverable
    rescue (``select_best_chat_reply``) merges that same text into the
    persisted reply — so without this filter the reasoning-details panel
    showed a full copy of the visible answer.  Deterministic post-processing
    at the one place that knows both texts (the web chat turn's metadata),
    no prompt change.

    Paragraph-level (blank-line separated, the ``thinking_text`` block shape):
    a paragraph is dropped when its stripped text is at least
    ``_REASONING_ECHO_MIN_CHARS`` long and occurs verbatim in *reply*.  Any
    other paragraph — including a genuine thought that merely resembles the
    answer — is kept.  Blank runs collapse so the rendered panel keeps its
    two-newline paragraph rhythm.
    """
    if not reasoning or not reply:
        return reasoning
    paragraphs = reasoning.split("\n\n")
    kept = [
        paragraph
        for paragraph in paragraphs
        if not (
            len(paragraph.strip()) >= _REASONING_ECHO_MIN_CHARS
            and paragraph.strip() in reply
        )
    ]
    if len(kept) == len(paragraphs):
        return reasoning
    # Collapse the blank runs left where paragraphs were removed.
    return "\n\n".join(p for p in kept if p.strip())


# Upper bound (seconds) on a single frame send.  A zombie client (TCP
# backpressure, no FIN) can block a stream write indefinitely; because
# ``_send_lock`` serializes all senders, one stuck send would freeze
# agent-loop status events and the loop itself.  10 s is far above any
# healthy stream write.
_SEND_TIMEOUT = 10.0
# How often a connection relaying the turn its send folded into checks that
# turn is still in flight.
FOLD_RELAY_PROBE_SECONDS = 5.0
# How long that relay keeps the stream open while Redis cannot say the turn
# still runs: the turn marker's lifetime, past which a running turn would have
# refreshed it. The turn is then closed as lost.
FOLD_RELAY_UNCONFIRMED_SECONDS = float(TURN_MARKER_TTL_SECONDS)

# Soft budget (seconds) for the post-loop persistence phase (P1.4):
# attachment linking + store_message + AI title + commit.  A DB
# lock wait here silently delays the ``done`` frame AFTER the answer has
# fully streamed — the user sees a finished response that never "lands".
# The budget is deliberately SOFT (a WARNING watchdog, not a hard cancel):
# aborting a commit mid-flight could lose the persisted answer, which is
# strictly worse than a late ``done`` frame.  The watchdog re-warns every
# budget interval while the phase is still running.
_POST_LOOP_SOFT_BUDGET_SECS = 60.0


def _is_cancelled_terminal(
    cancel_event: asyncio.Event | None, assistant_text: str
) -> bool:
    """True when the turn ended as a user cancellation (P2.5).

    Both conditions are required: the cancel event alone is not enough (a
    cancel can land in the tiny window AFTER the loop already produced a
    real answer — that answer must still be delivered as ``done``), and
    the sentinel text alone is not enough (an LLM could theoretically echo
    it).  The sentinel is the agent loop's single cancellation terminal
    (``_CANCELLED_MESSAGE``), shared so the two can never drift.
    """
    from app.tools.agent_loop import _CANCELLED_MESSAGE

    return bool(
        cancel_event is not None
        and cancel_event.is_set()
        and (assistant_text or "").strip() == _CANCELLED_MESSAGE
    )


@contextlib.asynccontextmanager
async def _soft_phase_budget(phase: str, conversation_id: str):
    """WARNING-log watchdog for a bounded-latency phase (soft timeout).

    Spawns a timer that logs a WARNING each time the wrapped block exceeds
    another ``_POST_LOOP_SOFT_BUDGET_SECS`` window, then cancels the timer
    on exit.  Never cancels the wrapped work — see the constant's comment.
    """
    started = time.monotonic()

    async def _watchdog() -> None:
        while True:
            await asyncio.sleep(_POST_LOOP_SOFT_BUDGET_SECS)
            logger.warning(
                "%s for conversation %s still running after %.0fs (soft "
                "budget %.0fs) — likely a DB lock wait or slow commit; the "
                "done frame is delayed but the phase is NOT cancelled",
                phase,
                conversation_id,
                time.monotonic() - started,
                _POST_LOOP_SOFT_BUDGET_SECS,
            )

    timer = asyncio.create_task(_watchdog())
    try:
        yield
    finally:
        timer.cancel()
        # Drain the cancelled watchdog task; ``return_exceptions=True``
        # retrieves its CancelledError so asyncio never logs "exception
        # was never retrieved" (same idiom as the agent loop's racers).
        await asyncio.gather(timer, return_exceptions=True)


async def _load_feedback_map(
    db: AsyncSession, message_ids: list[uuid.UUID], user_id: uuid.UUID
) -> dict[uuid.UUID, MessageFeedback]:
    """Load the requesting user's feedback for *message_ids*, keyed by message."""
    if not message_ids:
        return {}
    feedback_result = await db.execute(
        select(MessageFeedback).where(
            MessageFeedback.message_id.in_(message_ids),
            MessageFeedback.user_id == user_id,
        )
    )
    return {fb.message_id: fb for fb in feedback_result.scalars().all()}


def _is_statement_timeout_error(exc: Exception) -> bool:
    """Return whether an exception represents a PostgreSQL statement timeout."""
    orig = getattr(exc, "orig", None)
    pgcode = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    if pgcode == "57014":
        return True
    msg = str(orig or exc).lower()
    return "statement timeout" in msg


# User-facing message for HTTP 413 / payload-size provider errors.  A single
# constant so the raw-exception branch and the chain-ended-on-a-size-error
# branch of ``_friendly_error_message`` (which classifies an
# ``AllProvidersFailedError`` on its LAST failure) can never drift apart.
# This variant is composed into "I encountered an error: …" frames;
# the streaming-path variant spoken in the assistant's own voice lives in
# ``llm/router.REQUEST_TOO_LARGE_STREAMING_MESSAGE`` — keep the guidance in
# sync (both must point at starting a new conversation; enforced by
# ``test_streaming_message_and_error_frame_message_are_consistent``).
_REQUEST_TOO_LARGE_MESSAGE = (
    "The request is too large for the AI model to process. "
    "Your conversation may have grown too long — try starting a new "
    "conversation. If the problem persists with a fresh conversation, "
    "try reducing the size of any attachments."
)


# User-facing message per failure cause. The classifier in
# ``services/failure_causes`` decides WHAT failed; this table decides how to
# say it. Splitting the two is the point of the issue: the classification used
# to be re-derived at every site that needed to phrase an error, so a taxonomy
# change meant editing four keyword ladders and hoping they stayed in step.
#
# Causes absent from the table fall through to ``_GENERIC_ERROR_MESSAGE``. That
# is deliberate: a cause worth *recording* is not automatically a cause worth
# *explaining differently* to the person in the chat, and inventing a distinct
# sentence for each would be churn with no reader.
_GENERIC_ERROR_MESSAGE = (
    "Something went wrong while processing your request. "
    "Please try again — if the issue persists, contact your administrator."
)

_CAUSE_MESSAGES: dict[str, str] = {
    FailureCause.REQUEST_TOO_LARGE: _REQUEST_TOO_LARGE_MESSAGE,
    FailureCause.ALL_PROVIDERS_FAILED: (
        "All configured AI providers failed to process your request. "
        "Please try again — if the issue persists, contact your administrator."
    ),
    FailureCause.PROVIDER_RATE_LIMITED: (
        "The AI service is currently rate-limited. Please wait a moment and try again."
    ),
    FailureCause.PROVIDER_OVERLOADED: AI_TEMPORARILY_UNAVAILABLE_DETAIL,
    FailureCause.PROVIDER_CONNECTION_FAILED: AI_TEMPORARILY_UNAVAILABLE_DETAIL,
    FailureCause.PROVIDER_AUTH_REJECTED: (
        "The AI provider rejected the request due to an authentication issue. "
        "An administrator may need to check the provider configuration."
    ),
    FailureCause.PROVIDER_TIMEOUT: (
        "The request to the AI model timed out. Please try again — "
        "if the issue persists, try a shorter message."
    ),
    FailureCause.PLATFORM_QUOTA_EXCEEDED: (
        "Your AI usage quota has been reached. "
        "Please contact your administrator to raise it."
    ),
    FailureCause.MODEL_DECLINED: (
        "The AI model declined to answer this request under its usage policy. "
        "Try rephrasing it — if a legitimate request keeps being declined, "
        "contact your administrator."
    ),
}

# A cyber decline names its own way out: legitimate security work is what the
# category catches most on this platform.
_CYBER_DECLINE_DETAIL = "cyber"
_CYBER_DECLINE_MESSAGE = (
    "The AI model declined this request under its cybersecurity safeguards. "
    "If this is legitimate security work, an administrator can configure a "
    "fallback model or enroll the provider account in Anthropic's Cyber "
    "Verification Program."
)

# Overflow speaks in two voices depending on WHAT overflowed — the
# conversation, or the agent's bound tool schemas. Same cause, different fix.
_TOOL_OVERFLOW_MESSAGE = (
    "This agent has too many tools configured for the current AI model. "
    "An administrator may need to reduce the number of bound tools."
)
_CONVERSATION_OVERFLOW_MESSAGE = (
    "The conversation has grown too long for the AI model to process. "
    "Try starting a new conversation or sending a shorter message."
)

# Every model refused the input itself: the same input fails the same way, so
# the user is told what was refused (and its limit), never to try again.
_INPUT_REJECTED_MESSAGE = (
    "The AI model could not process your input: {reason}. Sending it again will "
    "not help — change the attachment or the message, then send it."
)


def _friendly_error_message(exc: Exception) -> str:
    """Translate a raw LLM/provider exception into a user-friendly message.

    Thin wrapper over the shared classifier: this function no longer decides
    what went wrong, only how to phrase it. ``ContextOverflowError`` keeps its
    bespoke rendering because that exception carries the actual numbers
    (estimated tokens, context window, model) the message quotes — a cause
    alone cannot reproduce it.
    """
    from app.llm.base import ContextOverflowError, overflow_friendly_message
    from app.llm.router import input_rejection

    if isinstance(exc, ContextOverflowError):
        return overflow_friendly_message(exc)

    classification = classify_run_failure(exc)

    if classification.cause is FailureCause.MODEL_REJECTED_REQUEST and (reason := input_rejection(exc)):
        return _INPUT_REJECTED_MESSAGE.format(reason=reason)

    if classification.cause is FailureCause.CONTEXT_OVERFLOW:
        return (
            _TOOL_OVERFLOW_MESSAGE
            if classification.detail == TOOL_OVERFLOW_DETAIL
            else _CONVERSATION_OVERFLOW_MESSAGE
        )
    if (
        classification.cause is FailureCause.MODEL_DECLINED
        and classification.detail == _CYBER_DECLINE_DETAIL
    ):
        return _CYBER_DECLINE_MESSAGE
    if classification.cause is FailureCause.ALL_PROVIDERS_FAILED and getattr(exc, "content_declined", False):
        # Every model that answered declined the content and the last leg
        # failed otherwise: a retry asks the same models again.
        return _CAUSE_MESSAGES[FailureCause.MODEL_DECLINED]

    return _CAUSE_MESSAGES.get(classification.cause, _GENERIC_ERROR_MESSAGE)


#: True for platform-injected wake messages that must not render in the UI.
_message_is_hidden = message_is_hidden


def _visible_message_responses(
    messages: list[Message],
    feedback_by_message_id: dict | None = None,
) -> list[MessageResponse]:
    """Serialize conversation messages, omitting hidden platform wakes."""
    feedback = feedback_by_message_id or {}
    return [
        _build_message_response(m, feedback.get(m.id))
        for m in messages
        if not _message_is_hidden(m)
    ]


async def _mark_deleted_attachments(db: AsyncSession, responses: list[MessageResponse]) -> list[MessageResponse]:
    """Stamp each attachment whose file is a tombstone (#396) with ``deleted_at`` / ``deleted_reason``.

    One query for the page. A message stores its attachments as written; the
    file's bytes may have gone since (a retention sweep, its owner, an
    administrator): the card then draws the deleted state instead of a
    broken preview or a 410 on click.
    """
    from app.models.file_attachment import FileAttachment

    ids: set[uuid.UUID] = set()
    for response in responses:
        for attachment in response.attachments or []:
            try:
                ids.add(uuid.UUID(str(attachment.file_id)))
            except (TypeError, ValueError):
                continue
    if not ids:
        return responses
    rows = (
        await db.execute(
            select(FileAttachment.id, FileAttachment.deleted_at, FileAttachment.deleted_reason).where(
                FileAttachment.id.in_(ids), FileAttachment.deleted_at.is_not(None)
            )
        )
    ).all()
    gone = {str(row[0]): (row[1], row[2]) for row in rows}
    if not gone:
        return responses
    for response in responses:
        for attachment in response.attachments or []:
            hit = gone.get(str(attachment.file_id))
            if hit:
                attachment.deleted_at = hit[0].isoformat()
                attachment.deleted_reason = hit[1]
    return responses


def _deserialize_tool_call_trace(raw: object) -> list[ToolCallTraceEntry] | None:
    """Build a list of ToolCallTraceEntry from raw metadata.

    Returns ``None`` if the raw value is missing, empty, or not a list; malformed
    individual entries are skipped (and logged) but do not invalidate the whole list.
    """
    if not isinstance(raw, list) or not raw:
        return None
    entries: list[ToolCallTraceEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            entries.append(
                ToolCallTraceEntry(
                    name=str(item.get("name", "")),
                    input=str(item.get("input", "")),
                    output=str(item.get("output", "")),
                    success=bool(item.get("success", True)),
                )
            )
        except Exception as exc:
            logger.warning("Skipping malformed tool_call_trace entry: %s", format_exception_message(exc))
    return entries or None


def _shown(response: MessageResponse) -> MessageResponse:
    """A stored message as a person sees it: a secret variable's marker as the display text.

    The content, the reasoning, the tool calls and the approvals alike: no API
    response hands out a restorable marker. The same object when none is held.
    """
    data = response.model_dump()
    shown = redact_known_secrets_for_display(data)
    return response if shown is data else MessageResponse.model_validate(shown)


def _build_message_response(
    m: Message, user_feedback: MessageFeedback | None = None
) -> MessageResponse:
    """Build a MessageResponse from a Message model, extracting all metadata.

    Inline attachment normalisation mirrors ``parse_attachments`` above
    rather than calling it because this helper also reads sibling
    metadata keys (``tool_names`` etc.) from the same ``meta`` dict —
    factoring it out would mean parsing the JSON twice on every message
    in the response.  Same isinstance guards (list at the top, dict per
    entry) so malformed metadata cannot crash message serialisation.
    """
    meta = parse_message_metadata(m.metadata_json)
    # Same defensive posture as the trace/attachment guards: a malformed
    # ``reasoning`` value (non-string, blank) collapses to None instead of
    # failing MessageResponse validation for the whole message.
    raw_reasoning = meta.get("reasoning")
    reasoning = (
        raw_reasoning
        if isinstance(raw_reasoning, str) and raw_reasoning.strip()
        else None
    )
    # Retired proposals (FR-002c4). Read only when the retirement marker is
    # present: without that check an *answerable* pending set would render as
    # expired, telling the user their decision was too late while the prompt was
    # still live.
    expired_approvals: list[dict] | None = None
    if meta.get("approvals_retired"):
        _raw_expired = meta.get("pending_approvals")
        if isinstance(_raw_expired, list) and _raw_expired:
            expired_approvals = [
                p for p in _raw_expired if isinstance(p, dict)
            ] or None

    raw_attachments = meta.get("attachments")
    attachments: list[dict] | None = None
    if isinstance(raw_attachments, list) and raw_attachments:
        attachments = [a for a in raw_attachments if isinstance(a, dict)] or None
    if attachments:
        for att in attachments:
            file_id = att.get("file_id")
            if file_id:
                att["download_url"] = attachment_download_url(
                    file_id,
                    conversation_id=str(m.conversation_id),
                    message_id=str(m.id),
                )
    context_usage = read_context_usage(meta)
    return _shown(MessageResponse(
        id=str(m.id),
        role=m.role,
        content=m.content,
        channel=m.channel,
        channel_message_id=m.channel_message_id,
        sequence=m.sequence,
        created_at=m.created_at.isoformat(),
        attachments=attachments,
        tool_names=meta.get("tool_names"),
        tool_call_count=meta.get("tool_call_count"),
        iterations=meta.get("iterations"),
        transfer_chain=meta.get("transfer_chain"),
        expired_approvals=expired_approvals,
        user_feedback=(
            MessageFeedbackResponse(
                rating=user_feedback.rating, comment=user_feedback.comment
            )
            if user_feedback
            else None
        ),
        tool_call_trace=_deserialize_tool_call_trace(meta.get("tool_call_trace")),
        runner_activity=sanitize_activity_list(meta.get(RUNNER_ACTIVITY_META_KEY)),
        is_truncated=bool(meta.get("is_truncated")) or None,
        interrupted=bool(meta.get(INTERRUPTED_META_KEY)) or None,
        reasoning=reasoning,
        llm_trace_id=str(m.llm_trace_id) if m.llm_trace_id else None,
        hidden=bool(meta.get("hidden")) or None,
        answered_by=_answered_by_payload(meta),
        mention_failed=bool(meta.get(MENTION_FAILED_KEY)) or None,
        agent_switch=_agent_switch_payload(meta),
        resumed_from=_resumed_from_payload(meta),
        wake=_wake_payload(meta),
        scheduled_outcome=_scheduled_outcome_payload(meta),
        conversation_refs=read_conversation_refs(meta) or None,
        content_format=read_content_format(meta),
        unavailable_integrations=_unavailable_integrations(
            meta.get(UNAVAILABLE_INTEGRATIONS_META_KEY)
        ),
        context_tokens=context_usage.tokens if context_usage else None,
        context_window=context_usage.window if context_usage else None,
        context_breakdown=context_usage.breakdown if context_usage else None,
        context_level=context_usage.level if context_usage else None,
    ))


def _answered_by_payload(meta: dict) -> dict[str, str] | None:
    """The ``answered_by`` attribution of a message, string values only."""
    raw = meta.get(ANSWERED_BY_KEY)
    if not isinstance(raw, dict) or not isinstance(raw.get("agent_name"), str):
        return None
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def _agent_switch_payload(meta: dict) -> AgentSwitchNote | None:
    """The note of a conversation continued with another agent, when well-formed."""
    from app.services.conversation_continuity import AGENT_SWITCH_KEY

    raw = meta.get(AGENT_SWITCH_KEY)
    if not isinstance(raw, dict) or not isinstance(raw.get("to"), dict):
        return None

    def _side(value: object) -> AgentSwitchSide | None:
        if not isinstance(value, dict):
            return None
        return AgentSwitchSide(
            agent_id=value.get("agent_id") if isinstance(value.get("agent_id"), str) else None,
            agent_name=value.get("agent_name") if isinstance(value.get("agent_name"), str) else None,
            general_assistant=bool(value.get("general_assistant")),
            deleted=bool(value.get("deleted")),
        )

    return AgentSwitchNote(
        from_agent=_side(raw.get("from")),
        to_agent=_side(raw.get("to")),
        reason=raw.get("reason") if isinstance(raw.get("reason"), str) else None,
    )


def _resumed_from_payload(meta: dict) -> ResumedFromNote | None:
    """The marker of a reply the platform resumed by itself, when well-formed."""
    from app.services.cut_turn_resume import RESUMED_FROM_META_KEY

    raw = meta.get(RESUMED_FROM_META_KEY)
    if not isinstance(raw, dict):
        return None

    def _int(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def _str(value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    return ResumedFromNote(
        cause=_str(raw.get("cause")),
        resume=_int(raw.get("resume")),
        of=_int(raw.get("of")),
        cut_at=_str(raw.get("cut_at")),
    )


def _wake_payload(meta: dict[str, Any]) -> WakeNote | None:
    """The work a wake turn's reply reports on, when well-formed (#11796)."""
    from app.services.parent_wake import PARENT_WAKE_SOURCE, WAKE_META_KEY

    raw = meta.get(WAKE_META_KEY)
    if not isinstance(raw, dict) or raw.get("source") != PARENT_WAKE_SOURCE:
        return None

    def _str(value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    return WakeNote(
        source=PARENT_WAKE_SOURCE,
        task_id=_str(raw.get("task_id")),
        agent_name=_str(raw.get("agent_name")),
        kind=_str(raw.get("kind")),
    )


def _scheduled_outcome_payload(meta: dict[str, Any]) -> ScheduledOutcomeNote | None:
    """The run a scheduled outcome reports on, when well-formed (#11950)."""
    from app.services.scheduled_work_items import SCHEDULED_OUTCOME_META_KEY

    raw = meta.get(SCHEDULED_OUTCOME_META_KEY)
    if not isinstance(raw, dict) or not isinstance(raw.get("work_item_id"), str):
        return None

    def _str(value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    return ScheduledOutcomeNote(
        work_item_id=raw["work_item_id"],
        run_id=_str(raw.get("run_id")),
        state=_str(raw.get("state")),
    )


def _unavailable_integrations(raw: object) -> list[UnavailableIntegration] | None:
    """The integrations a reply's turn did not use for its user."""
    items = clean_unavailable_integrations(raw)
    return [UnavailableIntegration(**item) for item in items] or None


# A relayed ``user_message`` (``chat_turn_stream.publish_user_message``)
# is the row a refetch would return.
chat_turn_stream.set_user_message_serializer(
    lambda message: _build_message_response(message).model_dump(mode="json")
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


#: The ``conversations.title`` column is ``VARCHAR(500)``.
_CONVERSATION_TITLE_MAX_LENGTH = 500


def _strip_title(value: Any) -> Any:
    # NUL too: the engine strips it from every bound string, so a title of NUL
    # and whitespace would otherwise reach the column empty.
    return value.replace("\x00", "").strip() if isinstance(value, str) else value


def _create_title(value: Any) -> Any:
    # Blank means "use the default title", as "" always did; a longer title is
    # cut to the column like the Studio title below instead of failing the
    # insert.
    if not isinstance(value, str):
        return value
    return _strip_title(value)[:_CONVERSATION_TITLE_MAX_LENGTH].rstrip() or None


class ConversationCreate(BaseModel):
    agent_id: str | None = None
    title: Annotated[str | None, BeforeValidator(_create_title)] = None
    # Optional, and NOT defaulted to the caller's default workspace.
    # A conversation with no workspace is a real state — the shape every
    # conversation predating workspaces has — so filing new ones
    # automatically would make "unfiled" mean two different things depending
    # on when the thread was created.
    workspace_id: str | None = None
    # Bind the conversation to a deck (Presentation Studio): every turn
    # then carries the deck's context and tools. Must be visible to the caller.
    presentation_id: str | None = None
    # Bind it to a video instead (Video Studio), the same way.
    video_project_id: str | None = None
    # The dashboard this conversation is bound to (Dashboard Studio).
    custom_dashboard_id: str | None = None
    # The fleet this conversation changes (fleet page "Modify with AI").
    fleet_id: str | None = None


class ConversationUpdate(BaseModel):
    # Stripped BEFORE the length check: a blank title is refused like an empty
    # one, never stored empty.
    #
    # Optional: a PATCH may be filing
    # ONLY, with no rename in it, and a required title would refuse that. The
    # constraints still apply to a title that IS supplied.
    title: (
        Annotated[
            str,
            StringConstraints(min_length=1, max_length=_CONVERSATION_TITLE_MAX_LENGTH),
            BeforeValidator(_strip_title),
        ]
        | None
    ) = None
    #: File the thread into a workspace, or unfile it with an explicit null.
    #: Creation already accepted one; there was no way to move an
    #: existing thread, so a workspace could only ever hold conversations
    #: started inside it — and the report an investigation files there was
    #: unreachable for every thread that already existed.
    #:
    #: `model_fields_set` distinguishes "leave it alone" from "unfile it",
    #: which `None` alone cannot express.
    workspace_id: str | None = None


class ConversationResponse(BaseModel):
    id: str
    channel: str
    agent_id: str | None = None
    agent_name: str | None = None
    agent_icon: str | None = None
    #: The agent that answers an unbound conversation in the General
    #: Assistant's place (``general_assistant_link.turn_stand_in``):
    #: a Slack or Teams conversation continued on the web.
    answering_agent_id: str | None = None
    transfer_source_agent_id: str | None = None
    transfer_source_agent_name: str | None = None
    title: str
    is_archived: bool
    created_at: str
    updated_at: str
    message_count: int = 0
    last_message_preview: str | None = None
    tool_count: int = 0
    # The deck this conversation is bound to (Presentation Studio).
    presentation_id: str | None = None
    # The video this conversation is bound to (Video Studio).
    video_project_id: str | None = None
    # The dashboard this conversation is bound to (Dashboard Studio).
    custom_dashboard_id: str | None = None
    # The fleet this conversation changes (fleet page "Modify with AI").
    fleet_id: str | None = None
    # The requester's access level on this row. ``read`` only ever
    # appears in the ``scope=shared`` list; the owner's own list is all
    # ``owner``.
    access: Literal["owner", "read"] = "owner"
    # Who owns the conversation — populated only for shared rows, where "who
    # sent me this" is the row's most useful context. ``None`` on the owner's
    # own list (it would be their own name on every row) and on the rare
    # ownerless conversation.
    owner_name: str | None = None
    # The workspace this thread is filed under, or None for unfiled.
    workspace_id: str | None = None
    #: The agent this conversation was held with when that agent was deleted,
    #: and its last name: what to continue it from.
    deleted_agent_id: str | None = None
    deleted_agent_name: str | None = None

    model_config = {"from_attributes": True}


class MessageAttachment(BaseModel):
    filename: str
    type: str
    size: int
    file_id: str | None = None
    download_url: str | None = None
    # ``content_type`` and ``file_tag`` are emitted by
    # ``attachment_meta_from_file_attachment`` and consumed by the
    # frontend ``FileChip`` / ``WorkingFileChip`` components
    # (``resolveAttachmentMime`` → ``mimeVisual`` picks the icon from
    # ``content_type``; ``attachmentFileTag`` picks the download-card
    # vs working-chip render from ``file_tag``).  Pydantic v2 defaults
    # to ``extra="ignore"`` so omitting them from this schema silently
    # strips both fields from the REST response (``GET /conversations``,
    # ``POST /conversations/{id}/messages``) — the streamed ``done`` payload
    # uses raw dicts and was unaffected, which is why the gap only
    # surfaced on initial conversation reload.  Declared explicitly
    # here so the REST and streamed paths render file chips identically.
    content_type: str | None = None
    file_tag: Literal["working_file", "download_file"] | None = None
    # When the file was written (ISO 8601) and the hex SHA-256 of its bytes:
    # what a card shows to tell two files of the same name apart. Absent on
    # an attachment stored before they were recorded, and ``sha256`` on an
    # upload.
    created_at: str | None = None
    sha256: str | None = None
    # The tombstone (#396), read live from the file's row when the page is
    # served (``_mark_deleted_attachments``): the card shows the file deleted,
    # muted, with the date and why, and never asks for its bytes.
    deleted_at: str | None = None
    deleted_reason: str | None = None


class MessageFeedbackResponse(BaseModel):
    rating: Literal["positive", "negative"]
    comment: str | None = None


class MessageFeedbackUpsert(BaseModel):
    rating: Literal["positive", "negative"]
    comment: str | None = Field(default=None, max_length=2000)


class ToolCallTraceEntry(BaseModel):
    """Per-tool-call execution record for the chat reasoning details panel."""

    name: str
    input: str  # JSON-serialized arguments (pre-bounded by `_trim_trace_text`)
    output: str  # Tool result text (pre-bounded by `_trim_trace_text`)
    success: bool


class UnavailableIntegration(BaseModel):
    """One integration a reply's turn did not use for its user."""

    provider: str
    #: ``oauth_credentials_not_shared`` (its owner's personal connected account
    #: is not shared with the user) or ``integration_not_visible`` (the
    #: integration is not shared with the user at all).
    reason: str
    #: Its name, only when the user can see the integration.
    name: str | None = None


class AgentSwitchSide(BaseModel):
    """One side of a change of agent: an agent (deleted, maybe) or the General Assistant."""

    agent_id: str | None = None
    agent_name: str | None = None
    general_assistant: bool = False
    deleted: bool = False


class AgentSwitchNote(BaseModel):
    """The note written when a conversation continues with another agent."""

    #: ``None`` when nothing recorded who held it before.
    from_agent: AgentSwitchSide | None = None
    to_agent: AgentSwitchSide | None = None
    #: ``continued`` (the owner chose it) or ``agent_deleted`` (a deletion's successor).
    reason: str | None = None


class ResumedFromNote(BaseModel):
    """How the platform resumed, by itself, the cut turn this reply finishes.

    Read from ``cut_turn_resume.resumed_from_meta``; only the keys the
    transcript's marker shows.
    """

    #: ``stop`` (a platform restart) or ``recycle`` (a worker recycle).
    cause: str | None = None
    #: Which automatic resume this was, and the most a turn is given.
    resume: int | None = None
    of: int | None = None
    #: When the turn was cut (ISO 8601).
    cut_at: str | None = None


class WakeNote(BaseModel):
    """The finished work a reply reports on, when no message asked for it.

    Read from ``parent_wake.wake_note``: a background task, an A2A partner's
    task or a Video Studio job ended and woke the conversation.
    """

    #: ``background_task_wake``.
    source: str
    #: The task's id, ``a2a-partner`` for a partner, or the work's label.
    task_id: str | None = None
    #: The agent (or partner) that did the work.
    agent_name: str | None = None
    #: ``task``, ``partner`` (an A2A partner answered) or ``job``; none on a
    #: note stored before the kind existed.
    kind: str | None = None


class ScheduledOutcomeNote(BaseModel):
    """The run a scheduled one-off's outcome message reports on (#11950).

    Read from the ``scheduled_outcome`` metadata ``deliver_outcome`` stores:
    the web chat draws its "View run" action from it.
    """

    work_item_id: str
    run_id: str | None = None
    #: ``done``, ``blocked``, ``missed`` or ``cancelled``.
    state: str | None = None


class MessageResponse(BaseModel):
    id: str
    role: str
    content: str
    channel: str
    channel_message_id: str | None = None
    sequence: int
    created_at: str
    attachments: list[MessageAttachment] | None = None
    tool_names: list[str] | None = None
    tool_call_count: int | None = None
    iterations: int | None = None
    transfer_chain: list[dict[str, str]] | None = None
    user_feedback: MessageFeedbackResponse | None = None
    # Proposals this message asked about and nobody answered, retired because the
    # conversation moved on (FR-002c4). Present so the surface can render
    # them as **expired** rather than dropping them: a question the product
    # asked, and a gated call that never ran, should not vanish from the
    # conversation that produced it. Never answerable — a decision for a retired
    # set is refused with 409.
    expired_approvals: list[dict] | None = None
    tool_call_trace: list[ToolCallTraceEntry] | None = None
    #: What the agent did on a machine during the turn: one display
    #: record per ``computer_use`` command, shaped by ``runner_activity.py``.
    runner_activity: list[dict[str, Any]] | None = None
    is_truncated: bool | None = None
    #: A server restart cut the turn this reply stands for.
    interrupted: bool | None = None
    reasoning: str | None = None
    llm_trace_id: str | None = None
    hidden: bool | None = None
    #: The agent that wrote this reply, on a turn several @mentioned agents
    #: answered: ``agent_id``, ``agent_name``, ``agent_icon``.
    answered_by: dict[str, str] | None = None
    #: A note written in place of a mentioned agent that could not answer.
    mention_failed: bool | None = None
    #: The note of a conversation continued with another agent.
    agent_switch: AgentSwitchNote | None = None
    #: The reply of a turn the platform resumed by itself after a cut.
    resumed_from: ResumedFromNote | None = None
    #: The reply of a turn finished work woke, no message asking for it.
    wake: WakeNote | None = None
    #: The run a scheduled one-off's outcome reports on: the web chat's "View run" action.
    scheduled_outcome: ScheduledOutcomeNote | None = None
    #: The conversations a user message referenced with @, as it was
    #: sent: ``conversation_id``, ``title``, ``key``. A reference, never content.
    conversation_refs: list[dict[str, str]] | None = None
    #: The markup of a reply written under a channel's format override,
    #: ``slack_mrkdwn`` (``output_format.CONTENT_FORMAT_META_KEY``);
    #: ``None``: the Markdown the web renders, whatever the channel.
    content_format: str | None = None
    #: The integrations this reply's turn did not use for its user because of
    #: who may use them: shown under the reply, never a silent absence.
    unavailable_integrations: list[UnavailableIntegration] | None = None
    #: How full the context window was when the run that wrote this reply last
    #: called the model (``services/context_usage.py``): the composer's
    #: gauge. Char-derived estimates; the breakdown's buckets sum to the tokens.
    #: ``context_level`` is the loop's own warning (``CONTEXT_LEVELS``).
    context_tokens: int | None = None
    context_window: int | None = None
    context_breakdown: dict[str, int] | None = None
    context_level: str | None = None
    #: The chart blocks the reply carries (``services/chat_charts.py``),
    #: in the order they stand in ``content``: the presentation ``chart``
    #: contract (kind, categories, series, unit, format, title, caption).
    #: Read from the content, never stored apart from it.
    charts: list[dict[str, Any]] | None = None

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def _charts_from_content(self) -> MessageResponse:
        if self.charts is None:
            from app.services.chat_charts import extract_charts

            self.charts = extract_charts(self.content) or None
        return self


class ToolOverrides(BaseModel):
    builtin_tool_ids: list[str] = []
    custom_tool_ids: list[str] = []
    integration_ids: list[str] = []
    mcp_server_ids: list[str] = []
    knowledge_base_ids: list[str] = []
    # Snapshot of the agent / General Assistant *default* tool set at the time
    # these overrides were saved (flat union of all default tool ids across
    # categories). On reopen, the frontend uses this to auto-enable tools that
    # were bound to the agent AFTER the last save (e.g. a freshly added
    # integration) without re-enabling tools the user explicitly disabled for
    # the conversation. ``None`` for overrides saved before this field existed.
    default_tool_ids: list[str] | None = None


async def _saved_tool_overrides(db: AsyncSession, conv: Conversation) -> ToolOverrides | None:
    """The conversation's saved session tool selection, for its current agent,
    less what its owner may no longer load (``usable_saved_selection``): the
    chat page restores its picker from it and sends it back with the next message."""
    saved = await usable_saved_selection(db, user_id=conv.user_id, conversation=conv)
    if saved is None:
        return None
    try:
        return ToolOverrides(**saved)
    except ValueError:
        return None


class ConversationCompactionInfo(BaseModel):
    """Persisted compaction state surfaced for transparency (P4.3).

    Long conversations are compacted server-side (Pinned Directives +
    Rolling Summary); this exposes what the agent actually "remembers" of
    the compacted history so users can audit it.
    """

    directives: str = ""
    summary: str = ""
    compacted_count: int = 0


class InterruptedTurnInfo(BaseModel):
    """A turn of the conversation that ended without its answer.

    It took the lock and its process stopped (a restart, a deploy, a crash)
    or it was closed with the interrupted terminal: the conversation says so
    until the next turn starts or the owner dismisses it.
    """

    started_at: str | None = None
    #: ``None`` when its process stopped without a word and its lock lapsed.
    interrupted_at: str | None = None
    #: Completed tool calls the next turn resumes instead of running again
    #: (``turn_checkpoint.kept_tool_calls``).
    tool_calls_kept: int = 0
    #: ``interrupted_resumable`` when the next turn resumes kept tool work,
    #: ``interrupted`` when it starts from the messages alone.
    state: str = "interrupted"
    #: Queued for its automatic resume while agentic AI may run:
    #: it resumes by itself and the notice offers no Resume click to
    #: race it.
    auto_resume: bool = False


class AwaitedWorkItem(BaseModel):
    """One piece of work the conversation waits on (``conversation_awaited_work``)."""

    kind: str
    id: str
    name: str | None = None
    status: str
    percent: int = 0
    stage: str | None = None


class AwaitedWorkResponse(BaseModel):
    """The work that resumes the conversation once it ends; empty for a reader who is not its owner."""

    items: list[AwaitedWorkItem] = Field(default_factory=list)


class ConversationDetailResponse(BaseModel):
    id: str
    channel: str
    agent_id: str | None = None
    #: See ``ConversationResponse.answering_agent_id``.
    answering_agent_id: str | None = None
    #: See ``ConversationResponse.deleted_agent_id``.
    deleted_agent_id: str | None = None
    deleted_agent_name: str | None = None
    transfer_source_agent_id: str | None = None
    transfer_source_agent_name: str | None = None
    title: str
    is_archived: bool
    created_at: str
    updated_at: str
    messages: list[MessageResponse]
    tool_overrides: ToolOverrides | None = None
    is_processing: bool = False
    # Proposals an in-flight turn is paused on. The ``approval_required``
    # frame is sent once over one stream, so a reload would otherwise lose the
    # prompt while the turn waits for a decision that can no longer be given —
    # the conversation goes silent with nothing on screen to explain it. A
    # reloading client already fetches this endpoint, so the pause is restored
    # from the same response that tells it a turn is running.
    pending_approvals: list[dict] | None = None
    # A turn that ended without its answer, when none is in flight.
    interrupted_turn: InterruptedTurnInfo | None = None
    # The deck this conversation is bound to (Presentation Studio).
    presentation_id: str | None = None
    # The video this conversation is bound to (Video Studio).
    video_project_id: str | None = None
    # The dashboard this conversation is bound to (Dashboard Studio).
    custom_dashboard_id: str | None = None
    # True when messages older than the returned page exist (fetch them via
    # ``GET /conversations/{id}/messages?before=<oldest_message_id>``).
    has_more: bool = False
    # Persisted compaction summary (None until the first compaction cycle).
    compaction: ConversationCompactionInfo | None = None
    # The requester's access level: ``owner`` or ``read``. A ``read``
    # viewer gets the transcript and its attachments and nothing else — the
    # UI uses this to raise the read-only banner, hide the composer and drop
    # the owner-only menu entries. It is a *reflection* of the server's
    # decision, never the enforcement: every write endpoint re-derives
    # ownership for itself.
    # Where this thread is filed, so a client can show and change
    # it without a second request.
    workspace_id: str | None = None
    access: Literal["owner", "read"] = "owner"
    # Link tier in force, owner-only — a read viewer has no business
    # knowing whether they got in by a named grant or by the org-wide link,
    # and surfacing it would leak the owner's sharing posture.
    link_access: Literal["private", "org_read"] | None = None
    # Whether the web may send in this conversation: false for a Slack
    # or Teams group thread, which is continued on its channel only.
    web_continuable: bool = True
    # The channels this conversation is also continued in, owner-only.
    continued_in: list[str] = Field(default_factory=list)
    # Owner's display name, for the shared viewer's "Shared with you by X"
    # banner. Omitted for the owner (it would be their own name) and for
    # ownerless channel-originated conversations.
    owner_name: str | None = None


class MessagePageResponse(BaseModel):
    """A chronological page of conversation messages (older-history endpoint)."""

    messages: list[MessageResponse]
    has_more: bool = False


class ConversationFileItem(BaseModel):
    """One file attached to (or produced inside) a conversation."""

    id: str
    filename: str
    content_type: str
    size: int
    created_at: str
    # ``uploaded`` (user-sent) / ``generated`` (agent-produced) / ``other``.
    # See :func:`_conversation_file_origin` for the source→origin mapping.
    origin: Literal["uploaded", "generated", "other"]
    # Raw ``FileAttachment.source`` — kept alongside ``origin`` so support can
    # tell a Slack attachment from a composer upload without a schema change.
    source: str
    # ``None`` when the message linkage was never established or was dropped
    # (``message_id`` is ``ON DELETE SET NULL``). The panel hides its
    # jump-to-message affordance in that case rather than linking nowhere.
    message_id: str | None = None
    download_url: str
    # The tombstone (#396): set when the file's bytes were deleted (a
    # retention, its owner, an administrator); the panel draws a deleted card
    # and the download answers 410.
    deleted_at: str | None = None
    deleted_reason: str | None = None


class ConversationFilesResponse(BaseModel):
    """A page of conversation files, newest first."""

    files: list[ConversationFileItem]
    total: int
    # True when the requester is a non-owner admin viewing via break-glass, so
    # the panel can keep the "you are viewing another user's files" banner up
    # for the whole session rather than only at the moment of acknowledgement.
    via_break_glass: bool = False


class ConversationFileAccessAckResponse(BaseModel):
    """Result of a break-glass acknowledgement."""

    status: Literal["granted"] = "granted"
    # Seconds the grant remains valid — the panel re-raises the gate on expiry.
    expires_in_seconds: int


class ConversationShareItem(BaseModel):
    """One grant on a conversation, resolved for display."""

    id: str
    principal_type: Literal["user", "group"]
    principal_id: str
    principal_name: str
    # Users only — disambiguates two colleagues with the same display name in
    # the share list. ``None`` for groups, whose names are unique.
    principal_email: str | None = None
    permission: Literal["read"]
    created_at: str


class ConversationSharesResponse(BaseModel):
    """The full sharing state of a conversation, owner-only.

    Every mutating share endpoint returns this same shape so the dialog always
    re-renders from the server's view rather than patching its local list —
    a client that guesses wrong about the outcome of a grant is a client that
    shows someone as having access when they do not.
    """

    shares: list[ConversationShareItem]
    link_access: Literal["private", "org_read"]


class ConversationShareTarget(BaseModel):
    """A user or group the conversation can be shared with (picker row)."""

    type: Literal["user", "group"]
    id: str
    name: str
    email: str | None = None


class ConversationShareTargetsResponse(BaseModel):
    users: list[ConversationShareTarget]
    groups: list[ConversationShareTarget]


class CreateConversationShareRequest(BaseModel):
    """Grant read access to exactly one principal."""

    user_id: str | None = None
    group_id: str | None = None

    @model_validator(mode="after")
    def _exactly_one_principal(self) -> CreateConversationShareRequest:
        """Mirror the table's ``num_nonnulls(...) = 1`` CHECK at the edge.

        The database would reject both-or-neither anyway, but as a 500 from a
        constraint violation rather than a 422 the caller can act on.
        """
        if (self.user_id is None) == (self.group_id is None):
            raise ValueError("Provide exactly one of user_id or group_id.")
        return self


class UpdateLinkAccessRequest(BaseModel):
    link_access: Literal["private", "org_read"]


class SendMessageRequest(BaseModel):
    # Optional so a resume can carry decisions and no text, which is the shape
    # the approval contract documents. A plain send still requires it — see the
    # validator below, which refuses a request that is neither.
    content: str | None = None
    # Multimodal: images as base64 strings or URLs
    images: list[str] | None = None
    # File attachments: list of file_ids from prior upload (create_message=false).
    # When provided, the referenced files are attached to this user message
    # and their extracted text is included as context for the LLM.
    file_ids: list[str] | None = None
    # Per-message tool overrides.
    # When provided, these *replace* the defaults for this message.
    builtin_tool_ids: list[str] | None = None
    custom_tool_ids: list[str] | None = None
    integration_ids: list[str] | None = None
    mcp_server_ids: list[str] | None = None
    knowledge_base_ids: list[str] | None = None
    # Snapshot of the agent / GA default tool set at send time. Persisted
    # alongside the overrides so reopening the conversation can tell newly
    # bound tools apart from tools the user explicitly disabled (see
    # ToolOverrides.default_tool_ids).
    default_tool_ids: list[str] | None = None
    # Dynamic scoping headers for integration instances
    scope_headers: dict[str, str] | None = None
    # Decisions on a turn that ended at a gated tool call (FR-002c1).
    # Present makes this request a *resume* rather than a new message: the
    # approved calls run with the arguments the reviewer was shown and the
    # conversation carries on from there, so ``content`` is not required. Same
    # per-decision shape as the web chat's ``/chat/messages/approve``, so a
    # client needs one vocabulary rather than two.
    decisions: list[dict] | None = None

    # Presentation Studio: the 1-based slide the user is looking at in
    # the deck this conversation is bound to. Ignored (never an error) when the
    # conversation has no binding or the index is out of range.
    studio_slide: int | None = None
    # Video Studio: the id of the scene the user is looking at in the
    # video this conversation is bound to. Ignored the same way.
    studio_scene: str | None = None
    # Dashboard Studio: the id of the widget the user has selected in the
    # dashboard this conversation is bound to. Ignored the same way.
    studio_widget: str | None = None
    # XTM One Desktop: the id of the runner the native app embeds, i.e.
    # the machine this message was typed on. Resolved per turn against the
    # caller's runner visibility; ignored (never an error) when absent, not a
    # UUID, revoked or not visible.
    desktop_runner_id: str | None = None
    # @mentions: the agents the message names, in mention order. Each
    # one other than the conversation's agent answers after it, in turn.
    mentioned_agent_ids: list[str] | None = None
    # @conversations: the conversations the message references, picked
    # from the @ menu (``GET /chat/conversation-references``). Those the sender
    # can open reach the agent as a brief and through ``read_conversation``.
    referenced_conversation_ids: list[str] | None = None

    @model_validator(mode="after")
    def _content_or_decisions(self) -> SendMessageRequest:
        """A request must say something: text to send, or decisions to apply.

        Both is refused rather than silently favouring one. A resume continues a
        turn that already has its user message, so text arriving with decisions
        has nowhere to go — it would reach the model and never be persisted,
        which is worse than being told to send it separately.
        """
        if self.decisions:
            if (self.content or "").strip():
                raise ValueError(
                    "Send either content or decisions, not both: a resume "
                    "continues an existing turn and cannot carry new text."
                )
            if self.file_ids:
                # There is no new user message to attach them to, so linking
                # them would re-parent the files onto the *previous* turn's
                # message and break its message-scoped download URLs.
                raise ValueError(
                    "Files cannot be attached to an approval decision. Send "
                    "them with a message instead."
                )
            if self.referenced_conversation_ids:
                # Same reason: the references are recorded on the new user
                # message, and a resume has none.
                raise ValueError(
                    "Conversations cannot be referenced in an approval decision. "
                    "Reference them in a message instead."
                )
            return self
        if self.content is None:
            raise ValueError("content is required unless decisions are supplied")
        return self


class SendMessageResponse(BaseModel):
    user_message: MessageResponse
    # The conversation agent's reply (with @mentions, the first of the turn).
    assistant_message: MessageResponse
    # The mentioned agents' replies, in the order they answered.
    mention_messages: list[MessageResponse] | None = None
    conversation_id: str
    transfer_agent_id: str | None = None
    transfer_source_agent_id: str | None = None
    # Machine-readable failure envelope. When the agent pipeline refuses or
    # fails to produce a real deliverable (e.g. a Filigran AI extraction
    # denied for licence/quota reasons), the HTTP status stays 200 but these
    # fields carry the real cause so callers (OpenCTI import-doc proxy /
    # connector) can surface it instead of the misleading buried assistant
    # text. Both default to None; on a success response they serialize as
    # ``null`` (this endpoint does not set ``response_model_exclude_none``),
    # so callers must treat ``null`` — not "absent" — as "no error". This is
    # a purely additive, non-breaking contract change: existing clients that
    # ignore unknown fields are unaffected.
    error: str | None = None
    error_code: str | None = None
    # Proposals this turn paused on, when it ended at the approval gate.
    # Without them a REST caller reads "I need approval before running: X" as
    # prose and never learns a single ``tool_call_id``, so the ``decisions``
    # resume this endpoint documents cannot be performed from the API at all.
    # The web chat's stream gets them on its ``approval_required`` frame and the
    # embed from ``pending-approvals``; this is REST's copy of the same fact.
    pending_approvals: list[dict] | None = None


class TTSRequest(BaseModel):
    text: str
    # A voice of the platform's speech model; omitted, the voice chosen in
    # Settings > AI and models > Media > Speech generation, else the model's first.
    voice: str | None = Field(None, max_length=200)
    format: str = "mp3"  # mp3 or wav: a preference, the response says what came


async def _get_owned_message(
    db: AsyncSession,
    user: User,
    conversation_id: str,
    message_id: str,
) -> tuple[Conversation, Message]:
    conv_uuid = parse_conversation_id(conversation_id, not_found_detail="Message not found")
    msg_uuid = parse_path_id(message_id, not_found_detail="Message not found")

    result = await db.execute(
        select(Conversation, Message)
        .join(Message, Message.conversation_id == Conversation.id)
        .where(
            Conversation.id == conv_uuid,
            Conversation.user_id == user.id,
            Message.id == msg_uuid,
        )
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Message not found")
    conv, msg = row
    return conv, msg


async def _push_langfuse_feedback_score(
    db: AsyncSession,
    msg: Message,
    rating: Literal["positive", "negative"],
    comment: str | None,
) -> None:
    meta = parse_message_metadata(msg.metadata_json)
    trace_id = meta.get("langfuse_trace_id")
    generation_id = meta.get("langfuse_generation_id")
    if not trace_id and not generation_id:
        return

    from app.services.langfuse_tracing import get_langfuse_client

    client = await get_langfuse_client(db)
    if not client:
        return

    score_payload: dict[str, object] = {
        "name": "user-feedback",
        "value": 1 if rating == "positive" else 0,
    }
    if comment:
        score_payload["comment"] = comment
    if isinstance(generation_id, str) and generation_id:
        score_payload["observation_id"] = generation_id
    elif isinstance(trace_id, str) and trace_id:
        score_payload["trace_id"] = trace_id

    try:
        if hasattr(client, "score"):
            client.score(**score_payload)
        elif hasattr(client, "create_score"):
            client.create_score(**score_payload)
    except Exception:
        logger.warning(
            "Failed to push Langfuse feedback score for message %s",
            msg.id,
            exc_info=True,
        )


async def _retract_langfuse_feedback_score(
    db: AsyncSession,
    msg: Message,
) -> None:
    """Push a score retraction to Langfuse when the user removes their feedback.

    The Langfuse scores API is append-only (scores are immutable records), so we
    cannot delete the original score.  Instead we push a new score entry with
    ``value=0`` and an explanatory comment so that observers of the Langfuse UI
    see the retraction event alongside the original score.
    """
    meta = parse_message_metadata(msg.metadata_json)
    trace_id = meta.get("langfuse_trace_id")
    generation_id = meta.get("langfuse_generation_id")
    if not trace_id and not generation_id:
        return

    from app.services.langfuse_tracing import get_langfuse_client

    client = await get_langfuse_client(db)
    if not client:
        return

    score_payload: dict[str, object] = {
        "name": "user-feedback",
        "value": 0,
        "comment": "feedback retracted by user",
    }
    if isinstance(generation_id, str) and generation_id:
        score_payload["observation_id"] = generation_id
    elif isinstance(trace_id, str) and trace_id:
        score_payload["trace_id"] = trace_id

    try:
        if hasattr(client, "score"):
            client.score(**score_payload)
        elif hasattr(client, "create_score"):
            client.create_score(**score_payload)
    except Exception:
        logger.warning(
            "Failed to push Langfuse score retraction for message %s",
            msg.id,
            exc_info=True,
        )


async def _begin_local_feedback_action(
    db: AsyncSession, msg: Message, user_id: uuid.UUID
) -> datetime | None:
    """Time this action for the local trace store, or ``None`` when the message
    has no trace to score. Read before any external call: see ``begin_score_action``."""
    if msg.llm_trace_id is None:
        return None
    return await begin_score_action(db, msg.llm_trace_id, user_id, FEEDBACK_SCORE_NAME)


async def _local_feedback_score(
    db: AsyncSession,
    msg: Message,
    *,
    user_id: uuid.UUID,
    action_at: datetime | None,
) -> dict[str, object] | None:
    """The rater's score on the message's trace once this action is applied
    (``rater_score_payload``), or ``None`` when the message has no trace."""
    if action_at is None:
        return None
    return await rater_score_payload(
        db,
        trace_id=msg.llm_trace_id,
        user_id=user_id,
        created_at=action_at,
        conversation_id=msg.conversation_id,
        focus_message_id=msg.id,
    )


async def _record_local_feedback_score(msg: Message, payload: dict[str, object] | None) -> None:
    # After the commit, like the Langfuse push: the feedback row is durable, so
    # a failure here must never surface as a 500.
    if payload is None:
        return
    try:
        await get_tracing_dispatcher().record_score(payload)
    except Exception:
        logger.warning("Unexpected error recording the local feedback score for message %s", msg.id, exc_info=True)


# ---------------------------------------------------------------------------
# Conversation sharing
#
# Conversations stay owner-scoped by default. Sharing widens *reads* only, and
# only to principals that already exist on the platform, so every reader is an
# authenticated account that can be named in the audit trail and cut off again.
#
# Three ways a non-owner can read a conversation:
#
#   1. a named grant to them personally (``conversation_shares``);
#   2. a named grant to a group they belong to;
#   3. ``Conversation.link_access == "org_read"`` — "anyone on the platform
#      with the link". This is the tier that makes the ``/chat?c=<id>`` URL
#      pasteable into a Teams thread or a Jira ticket, without the URL becoming a bearer token for the unauthenticated
#      world.
#
# Admins are deliberately NOT on that list. Ambient admin read would silently
# defeat the audited break-glass gate in front of another user's
# conversation files — an admin who needs to look at someone else's
# conversation goes through that gate, which records who looked and when.
# ---------------------------------------------------------------------------

#: Shortest accepted share-target query. The picker is the only people
#: directory a non-admin can read, so it answers "find the colleague I am
#: thinking of", not "list the org": one character would return a page of
#: arbitrary accounts to anybody with a login.
SHARE_TARGET_MIN_QUERY_LENGTH = 2
#: Hard ceiling on a single page of results, so the endpoint cannot be paged
#: through into a full directory dump.
SHARE_TARGET_MAX_RESULTS = 25

#: Requester owns the conversation: full read + write + management.
CONVERSATION_ACCESS_OWNER = "owner"
#: Requester may read the transcript and its attachments, nothing else.
CONVERSATION_ACCESS_READ = "read"

#: ``Conversation.link_access`` value that admits any authenticated user who
#: holds the conversation URL. Mirrors the model-level CHECK constraint.
LINK_ACCESS_PRIVATE = "private"
LINK_ACCESS_ORG_READ = "org_read"
CONVERSATION_LINK_ACCESS_VALUES = (LINK_ACCESS_PRIVATE, LINK_ACCESS_ORG_READ)


async def _has_conversation_share(
    db: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> bool:
    """True when a named grant admits *user_id* to *conversation_id*.

    One round trip: group membership is resolved as a correlated subquery
    rather than a separate "which groups am I in" fetch, because this runs on
    every read of a conversation the requester does not own.
    """
    from app.models.conversation_share import ConversationShare
    from app.models.group import GroupMember

    member_group_ids = select(GroupMember.group_id).where(
        GroupMember.user_id == user_id
    )
    result = await db.execute(
        select(ConversationShare.id)
        .where(
            ConversationShare.conversation_id == conversation_id,
            or_(
                ConversationShare.shared_with_user_id == user_id,
                ConversationShare.shared_with_group_id.in_(member_group_ids),
            ),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def conversation_access(
    db: AsyncSession, user: User, conv: Conversation
) -> str | None:
    """Return the requester's access level on *conv*, or ``None`` for no access.

    :data:`CONVERSATION_ACCESS_OWNER` / :data:`CONVERSATION_ACCESS_READ` /
    ``None``. Single source of truth for "may this user see this conversation"
    — every read-side surface routes through it (or through
    :func:`resolve_conversation_for_read`) so the tiers cannot drift apart the
    way per-endpoint inline predicates do.

    Ordered cheapest-first: the owner check is a field comparison and covers
    the overwhelming majority of calls, so the share lookup only runs for
    genuine non-owner reads.
    """
    if conv.user_id is not None and conv.user_id == user.id:
        return CONVERSATION_ACCESS_OWNER
    if await _has_conversation_share(db, user.id, conv.id):
        return CONVERSATION_ACCESS_READ
    if conv.link_access == LINK_ACCESS_ORG_READ:
        return CONVERSATION_ACCESS_READ
    return None


async def resolve_conversation_for_read(
    conversation_id: str,
    user: User,
    db: AsyncSession,
) -> tuple[Conversation, str]:
    """Load a conversation the requester may read, or raise.

    Returns ``(conversation, access_level)`` where the level is
    :data:`CONVERSATION_ACCESS_OWNER` or :data:`CONVERSATION_ACCESS_READ`, so
    callers can vary the *response* (hide owner-only affordances) without
    re-deriving the *decision*.

    Raises ``404`` — never ``403`` — when access is refused, deliberately
    identical to the conversation-does-not-exist response. A 403 would confirm
    the id is real, letting any authenticated user enumerate other people's
    conversations by walking ids. This is the same indistinguishable-404
    convention the owner-scoped endpoints in this module already use.
    """
    conv_uuid = parse_conversation_id(conversation_id)
    result = await db.execute(select(Conversation).where(Conversation.id == conv_uuid))
    conv = result.scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    access = await conversation_access(db, user, conv)
    if access is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv, access


# ---------------------------------------------------------------------------
# REST Endpoints
# ---------------------------------------------------------------------------


@router.get("/suggestions", response_model=list[str])
async def get_chat_suggestions(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    agent_slug: str | None = Query(
        None,
        description=(
            "Agent to tailor the suggestions to. Omitted (or unknown) returns "
            "the platform's generic set, so a caller that does not track the "
            "selected agent still gets something usable."
        ),
    ),
):
    """Suggested opening actions for the chat welcome screen."""
    from app.api.v1.helpers import get_user_group_ids
    from app.services.chat_suggestions import resolve_chat_suggestions

    user_group_ids = await get_user_group_ids(db, user.id)
    return await resolve_chat_suggestions(db, user.id, user_group_ids, agent_slug)


class ConversationReferenceCandidate(BaseModel):
    """A conversation the @ menu offers to reference."""

    id: str
    title: str
    #: What picking it inserts after ``@``: the title as a slug.
    key: str
    updated_at: str | None = None
    is_own: bool
    #: Who shared it with the caller; ``None`` on their own conversations.
    owner_name: str | None = None


class ConversationReferenceList(BaseModel):
    conversations: list[ConversationReferenceCandidate]


@router.get("/conversation-references", response_model=ConversationReferenceList)
async def search_conversation_references(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=20)] = 8,
    exclude: Annotated[
        str | None,
        Query(description="A conversation to leave out: the one being written in."),
    ] = None,
):
    """The conversations the composer's @ menu offers.

    The caller's own (web chat and embedded chat) and those shared with them
    by name, searched by title, most recent first. A read of the caller's own
    data, gated on authentication alone like the conversation list.
    """
    from app.services.conversation_references import search_reference_candidates

    rows = await search_reference_candidates(
        db, user, query=q, limit=limit, exclude=safe_uuid(exclude) if exclude else None
    )
    return ConversationReferenceList(
        conversations=[ConversationReferenceCandidate(**row) for row in rows]
    )


async def last_message_previews(db: AsyncSession, conv_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Each conversation's last user or assistant message, as a list row shows it.

    One query for all of them. A background task's wake is not a message the
    user sees; render markers are stripped before the cut, so a
    ``[[PRESENTATION:…]]`` closing a reply never shows as raw text and one cut
    mid-uuid never matches again; past 150 characters the text is cut with
    ``...``. Served by the web chat's list and the embedded chat's history.
    """
    previews: dict[uuid.UUID, str] = {}
    if not conv_ids:
        return previews
    ranked = (
        select(
            Message.conversation_id,
            Message.content,
            func.row_number()
            .over(
                partition_by=Message.conversation_id,
                order_by=Message.sequence.desc(),
            )
            .label("rn"),
        )
        .where(
            Message.conversation_id.in_(conv_ids),
            Message.role.in_(["user", "assistant"]),
            or_(
                Message.metadata_json.is_(None),
                ~Message.metadata_json.contains("background_task_wake"),
            ),
        )
        .subquery()
    )
    last_msgs = await db.execute(
        select(ranked.c.conversation_id, ranked.c.content).where(ranked.c.rn == 1)
    )
    for conversation_id, content in last_msgs:
        text = strip_artifact_markers(content or "")
        previews[conversation_id] = (text[:150] + "...") if len(text) > 150 else text
    return previews


@router.get("/conversations", response_model=list[ConversationResponse])
async def list_conversations(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    archived: bool = Query(False),
    workspace_id: str | None = Query(
        default=None,
        description=(
            "Restrict to conversations filed under this workspace. Applies "
            "to scope='own' only — a shared conversation is listed because "
            "someone named you on it, and the owner's filing is not yours to "
            "browse by."
        ),
    ),
    scope: Literal["own", "shared"] = Query(
        default="own",
        description=(
            "'own' — conversations you started (the default sidebar list). "
            "'shared' — conversations others have shared with you by name."
        ),
    ),
    presentation_id: str | None = Query(
        default=None,
        description=(
            "Only conversations bound to this presentation (Presentation "
            "Studio) — the studio rail's history for one deck."
        ),
    ),
    video_project_id: str | None = Query(
        default=None,
        description=(
            "Only conversations bound to this video (Video Studio) - the editor rail's history for one video."
        ),
    ),
    custom_dashboard_id: str | None = Query(
        default=None,
        description="Only conversations bound to this dashboard (Dashboard Studio) - the rail's history.",
    ),
    fleet_id: str | None = Query(
        default=None,
        description="Only conversations bound to this fleet (fleet page Modify with AI) - the rail's history.",
    ),
):
    from app.models.conversation_share import ConversationShare
    from app.models.group import GroupMember

    studio_filter = []
    if presentation_id is not None:
        studio_filter.append(
            Conversation.presentation_id
            == parse_uuid(presentation_id, label="presentation ID")
        )
    if video_project_id is not None:
        studio_filter.append(
            Conversation.video_project_id
            == parse_uuid(video_project_id, label="video ID")
        )
    if custom_dashboard_id is not None:
        studio_filter.append(
            Conversation.custom_dashboard_id
            == parse_uuid(custom_dashboard_id, label="dashboard ID")
        )
    if fleet_id is not None:
        studio_filter.append(Conversation.fleet_id == parse_uuid(fleet_id, label="fleet ID"))

    workspace_filter = []
    if workspace_id is not None:
        if scope == "shared":
            # Refused rather than ignored: a caller filtering by workspace
            # would otherwise read the whole shared list as that workspace's.
            raise HTTPException(
                status_code=422,
                detail="workspace_id applies to scope='own' only.",
            )
        # No visibility check: this filters the caller's OWN conversations, so
        # an id they cannot see simply matches none of their rows. Resolving
        # the workspace first would turn "no results" into "that workspace
        # exists" — an existence oracle bought for nothing.
        workspace_filter.append(
            Conversation.workspace_id
            == parse_uuid(workspace_id, label="workspace ID")
        )

    if scope == "shared":
        # Conversations shared with this user *by name* — personally or via a
        # group they belong to.
        #
        # ``link_access == "org_read"`` conversations are deliberately NOT
        # here. That tier means "anybody with the link may open this", which
        # is a reachability rule, not an invitation: listing every org-readable
        # conversation would put threads the user has never been pointed at,
        # and has no reason to care about, in their sidebar — and would turn a
        # side panel into a directory of everyone's chats. Those stay
        # reachable by URL only.
        #
        # This mirrors ``helpers.run_visible_filter``, where the *list* filter
        # is intentionally narrower than the *visibility* rule for the same
        # reason.
        member_group_ids = select(GroupMember.group_id).where(
            GroupMember.user_id == user.id
        )
        shared_conv_ids = select(ConversationShare.conversation_id).where(
            or_(
                ConversationShare.shared_with_user_id == user.id,
                ConversationShare.shared_with_group_id.in_(member_group_ids),
            )
        )
        query = (
            select(Conversation)
            .where(
                Conversation.id.in_(shared_conv_ids),
                # A share does not survive the owner archiving the thread, and
                # ``is_archived`` is the owner's state — the recipient has no
                # archive of their own to filter by.
                Conversation.is_archived.is_(False),
                web_listable_clause(),
                # Defensive: a grant naming the owner should be impossible
                # (``create_conversation_share`` rejects it), but if one ever
                # existed the conversation would appear in both lists.
                Conversation.user_id != user.id,
                *studio_filter,
            )
            .order_by(Conversation.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await db.execute(query)
        convs = list(result.scalars().all())
        return await _serialize_conversation_list(
            db, convs, user, access=CONVERSATION_ACCESS_READ
        )

    # The personal chat sidebar lists the conversations the web continues:
    # web ones, and the user's Slack / Teams direct-message threads.
    # A group channel thread (and every other channel) stays on the agent
    # detail page's Chats tab.
    query = (
        select(Conversation)
        .where(
            Conversation.user_id == user.id,
            Conversation.is_archived == archived,
            web_listable_clause(),
            *studio_filter,
            *workspace_filter,
        )
        .order_by(Conversation.updated_at.desc())
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(query)
    convs = list(result.scalars().all())
    return await _serialize_conversation_list(
        db, convs, user, access=CONVERSATION_ACCESS_OWNER
    )


async def _serialize_conversation_list(
    db: AsyncSession,
    convs: list[Conversation],
    user: User,
    *,
    access: str,
) -> list[ConversationResponse]:
    """Enrich a page of conversations for the sidebar.

    Shared by both scopes of :func:`list_conversations` so the "own" and
    "shared with me" lists cannot drift into rendering differently — the only
    intended difference between them is *which* conversations are in the page,
    not what a row looks like.

    Every lookup is batched: a page of 50 conversations must not become 150
    queries. *access* is stamped on every row rather than re-derived per
    conversation — the caller already knows it from the scope it queried.
    """
    from app.models.agent import Agent

    # Pre-fetch agent names/icons for all referenced agents in one query
    agent_ids = {c.agent_id for c in convs if c.agent_id}
    agent_ids |= {
        c.transfer_source_agent_id for c in convs if c.transfer_source_agent_id
    }
    agent_map: dict[uuid.UUID, tuple[str, str]] = {}
    if agent_ids:
        agent_result = await db.execute(
            select(Agent.id, Agent.name, Agent.icon).where(Agent.id.in_(agent_ids))
        )
        for row in agent_result:
            agent_map[row.id] = (row.name, row.icon)

    # Batch-load message counts for all conversations (avoids N+1 queries)
    conv_ids = [c.id for c in convs]
    count_map: dict[uuid.UUID, int] = {}
    if conv_ids:
        count_rows = await db.execute(
            select(Message.conversation_id, func.count())
            .where(Message.conversation_id.in_(conv_ids))
            .group_by(Message.conversation_id)
        )
        for row in count_rows:
            count_map[row[0]] = row[1]

    # Batch-load last user/assistant message per conversation (avoids N+1 queries)
    last_msg_map = await last_message_previews(db, conv_ids)

    # Owner display names — only needed for the "shared with me" list, where
    # "who sent me this" is the row's most useful piece of context. Skipped
    # entirely for the owner's own list, where every row has the same answer.
    owner_names: dict[uuid.UUID, str] = {}
    if access != CONVERSATION_ACCESS_OWNER:
        owner_ids = {c.user_id for c in convs if c.user_id is not None}
        if owner_ids:
            owner_rows = await db.execute(
                select(User.id, User.full_name).where(User.id.in_(owner_ids))
            )
            owner_names = {r.id: r.full_name for r in owner_rows}

    # Who answers each unbound one in the General Assistant's place.
    stand_ins = await conversation_stand_ins(db, convs)

    # The last name of the agent a deletion took from each.
    from app.services.conversation_continuity import agent_labels

    deleted_names = await agent_labels(db, {c.deleted_agent_id for c in convs})

    responses = []
    # Owners, groups, agents' bindings and visibility read once for the whole list.
    selection_lookups: dict = {}
    for c in convs:
        # Count tools from the saved session tool selection, as the
        # conversation read restores it. The count is display-only: a row
        # whose check fails counts its selection as saved, never fails the list.
        tool_count = 0
        try:
            overrides = await usable_saved_selection(db, user_id=c.user_id, conversation=c, lookups=selection_lookups)
        except Exception:
            logger.warning("Saved tool selection of conversation %s not checked for the list", c.id, exc_info=True)
            overrides = saved_tool_selection(c)
        if overrides is not None:
            with contextlib.suppress(Exception):
                tool_count = (
                    len(overrides.get("builtin_tool_ids", []))
                    + len(overrides.get("custom_tool_ids", []))
                    + len(overrides.get("integration_ids", []))
                    + len(overrides.get("mcp_server_ids", []))
                    + len(overrides.get("knowledge_base_ids", []))
                )

        agent_name, agent_icon = (
            agent_map.get(c.agent_id, (None, None)) if c.agent_id else (None, None)
        )
        ts_name, _ = (
            agent_map.get(c.transfer_source_agent_id, (None, None))
            if c.transfer_source_agent_id
            else (None, None)
        )

        responses.append(
            ConversationResponse(
                id=str(c.id),
                channel=c.channel,
                agent_id=str(c.agent_id) if c.agent_id else None,
                agent_name=agent_name,
                agent_icon=agent_icon,
                answering_agent_id=str(stand_ins[c.id].id) if c.id in stand_ins else None,
                transfer_source_agent_id=(str(c.transfer_source_agent_id) if c.transfer_source_agent_id else None),
                transfer_source_agent_name=ts_name,
                title=c.title,
                is_archived=c.is_archived,
                created_at=c.created_at.isoformat(),
                updated_at=c.updated_at.isoformat(),
                message_count=count_map.get(c.id, 0),
                last_message_preview=last_msg_map.get(c.id),
                tool_count=tool_count,
                presentation_id=(str(c.presentation_id) if c.presentation_id else None),
                video_project_id=(str(c.video_project_id) if c.video_project_id else None),
                custom_dashboard_id=(str(c.custom_dashboard_id) if c.custom_dashboard_id else None),
                fleet_id=str(c.fleet_id) if c.fleet_id else None,
                access=access,
                owner_name=(owner_names.get(c.user_id) if c.user_id is not None else None),
                workspace_id=str(c.workspace_id) if c.workspace_id else None,
                deleted_agent_id=str(c.deleted_agent_id) if c.deleted_agent_id else None,
                deleted_agent_name=(
                    deleted_names[c.deleted_agent_id].name if c.deleted_agent_id in deleted_names else None
                ),
            )
        )

    return responses


async def _chat_agent(db: AsyncSession, user: User, agent_id: str):
    """The agent *user* may hold a conversation with: visible to them, enabled.

    ``get_user_agent`` (three-tier visibility) keeps a known UUID from opening
    a conversation with another user's private agent; an unknown or invisible
    one is the same 404. Starting a conversation and continuing one with
    another agent take the same rule.
    """
    from app.api.v1.helpers import get_user_agent, get_user_group_ids

    _user_group_ids = await get_user_group_ids(db, user.id)
    agent = await get_user_agent(
        db, user.id, agent_id, _user_group_ids, is_admin=user.is_admin
    )
    if not agent.enabled:
        raise HTTPException(status_code=400, detail="Agent is disabled")
    return agent


@router.post(
    "/conversations",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(
    body: ConversationCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _chat_gate: None = Depends(require_chat_enabled),
):
    if body.agent_id:
        await _chat_agent(db, user, body.agent_id)

    workspace_uuid = None
    if body.workspace_id:
        # Resolved through the caller's own three-tier visibility, exactly as
        # the agent above is. Without this, a known UUID would let anyone file
        # a thread into someone else's workspace — and a workspace is about to
        # become a shared container, so a stray thread in it is a thread other
        # people can see listed. Filing takes the MANAGE right, whatever the
        # verb: a filed conversation works in the workspace's coordination
        # scope, which a read_only grantee may not write to.
        from app.api.v1.helpers import get_manageable_entity
        from app.models.workspace import Workspace
        from app.services.workspaces import (
            WorkspaceActionRefusedError,
            ensure_workspace_accepts_conversations,
        )

        workspace = await get_manageable_entity(
            db,
            Workspace,
            user,
            body.workspace_id,
            action="use",
            entity_label="Workspace",
        )
        try:
            ensure_workspace_accepts_conversations(workspace)
        except WorkspaceActionRefusedError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
        workspace_uuid = workspace.id
    # Presentation Studio binding: the deck must be visible to the
    # caller — a read-only grant is enough to open a studio on it (the agent is
    # then told it may not save), an invisible deck is a 404 like everywhere
    # else, so the binding can never be used to probe for a deck's existence.
    presentation_uuid: uuid.UUID | None = None
    presentation_title: str | None = None
    if body.presentation_id:
        from app.services.presentations import load_visible_presentation

        presentation_uuid = parse_uuid(body.presentation_id, label="presentation ID")
        loaded = await load_visible_presentation(
            db, presentation_uuid, user_id=user.id
        )
        if loaded is None:
            raise HTTPException(status_code=404, detail="Presentation not found")
        presentation_title = loaded[0].title
    # Video Studio binding: the same rule for a video.
    video_uuid: uuid.UUID | None = None
    if body.video_project_id and presentation_uuid is None:
        video_uuid, video_title = await _visible_video_binding(db, user, body.video_project_id)
        presentation_title = video_title
    # Dashboard Studio binding: the same rule for a custom dashboard.
    dashboard_uuid: uuid.UUID | None = None
    if body.custom_dashboard_id and presentation_uuid is None and video_uuid is None:
        dashboard_uuid, dashboard_title = await _visible_dashboard_binding(db, user, body.custom_dashboard_id)
        presentation_title = dashboard_title
    # Fleet binding (Modify with AI): the same rule for a fleet.
    fleet_uuid: uuid.UUID | None = None
    if body.fleet_id and presentation_uuid is None and video_uuid is None and dashboard_uuid is None:
        fleet_uuid, presentation_title = await _visible_fleet_binding(db, user, body.fleet_id)

    agent_uuid = uuid.UUID(body.agent_id) if body.agent_id else None
    if agent_uuid is None:
        # A conversation without an agent is the General Assistant's: the
        # agent linked to it answers it from the first message.
        from app.services.general_assistant_link import general_assistant_stand_in

        stand_in = await general_assistant_stand_in(db)
        if stand_in is not None:
            agent_uuid = stand_in.id

    conv = Conversation(
        user_id=user.id,
        channel="web",
        agent_id=agent_uuid,
        title=body.title or (presentation_title and f"Studio: {presentation_title}"[:500]) or "New conversation",
        workspace_id=workspace_uuid,
        presentation_id=presentation_uuid,
        video_project_id=video_uuid,
        custom_dashboard_id=dashboard_uuid,
        fleet_id=fleet_uuid,
    )
    db.add(conv)
    await db.commit()
    return ConversationResponse(
        id=str(conv.id),
        channel=conv.channel,
        agent_id=str(conv.agent_id) if conv.agent_id else None,
        title=conv.title,
        is_archived=conv.is_archived,
        created_at=conv.created_at.isoformat(),
        updated_at=conv.updated_at.isoformat(),
        workspace_id=str(conv.workspace_id) if conv.workspace_id else None,
        presentation_id=(str(conv.presentation_id) if conv.presentation_id else None),
        video_project_id=(str(conv.video_project_id) if conv.video_project_id else None),
        custom_dashboard_id=(str(conv.custom_dashboard_id) if conv.custom_dashboard_id else None),
        fleet_id=str(conv.fleet_id) if conv.fleet_id else None,
    )


@dataclass(frozen=True)
class ConversationTurnState:
    """Where a conversation's turn stands, as a reloading client restores it."""

    is_processing: bool = False
    pending_approvals: list[dict] | None = None
    interrupted_turn: InterruptedTurnInfo | None = None


async def read_conversation_turn_state(
    db: AsyncSession, conv: Conversation, *, owner: bool
) -> ConversationTurnState:
    """The in-flight turn, what it is paused on, or the turn that ended without its answer.

    Read by the conversation detail and the web chat's restore. A Redis that
    cannot answer reads as no turn in flight.
    """
    _pending_approvals: list[dict] | None = None
    _interrupted = None
    try:
        _r = await redis_client()
        _is_processing = bool(await _r.exists(chat_processing_key(conv.id)))
        if _is_processing:
            from app.services.chat_turn_guard import get_pending_approvals

            _pending_approvals = await get_pending_approvals(str(conv.id))
        else:
            from app.services.chat_turn_guard import interrupted_turn

            _interrupted = await interrupted_turn(str(conv.id))
    except Exception:
        _is_processing = False

    # A turn that ended without its answer, with the tool work its
    # checkpoint keeps for the next turn.
    _interrupted_info: InterruptedTurnInfo | None = None
    if _interrupted is not None:
        from app.services.cut_turn_resume import resumes_by_itself as _cut_turn_resumes
        from app.services.turn_checkpoint import kept_tool_calls

        _kept = 0
        with contextlib.suppress(Exception):
            async with db.begin_nested():
                _kept = await kept_tool_calls(db, conv.id)
        _interrupted_info = InterruptedTurnInfo(
            # Queued AND agentic AI on: with AI off the queue waits.
            auto_resume=await _cut_turn_resumes(conv.id),
            started_at=_interrupted.started_at.isoformat() if _interrupted.started_at else None,
            interrupted_at=(
                _interrupted.interrupted_at.isoformat() if _interrupted.interrupted_at else None
            ),
            tool_calls_kept=_kept,
            state=INTERRUPTED_RESUMABLE if _kept else "interrupted",
        )

    # A shared conversation is readable by everyone it was shared with, and by
    # any org user under org-link read — but the decision is the owner's alone.
    # FR-008a: a shared channel MAY carry a notice that approval
    # is pending; the arguments and the decision MUST be delivered where only
    # that user can act. The notice survives redaction, the payload does not.
    if not owner:
        from app.tools.approval import redact_proposals

        _pending_approvals = redact_proposals(_pending_approvals)
    return ConversationTurnState(_is_processing, _pending_approvals, _interrupted_info)


@router.get(
    "/conversations/{conversation_id}", response_model=ConversationDetailResponse
)
async def get_conversation(
    conversation_id: str,
    limit: int = Query(
        default=DEFAULT_MESSAGE_PAGE_SIZE,
        ge=1,
        le=MAX_MESSAGE_PAGE_SIZE,
        description="Number of most-recent messages to return.",
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Readable by the owner or by anyone the conversation is shared with.
    # ``access`` rides out on the response so the UI knows to render
    # the read-only banner and hide the composer rather than offering actions
    # the write-side endpoints would refuse.
    conv, access = await resolve_conversation_for_read(conversation_id, user, db)

    # Latest page, newest-first with a +1 sentinel, then reversed back to
    # chronological order. Previously this returned the OLDEST 500 messages
    # and silently dropped everything newer on long threads.
    messages, has_more = await read_visible_message_page(
        db,
        [Message.conversation_id == conv.id, Message.role.in_(CHAT_MESSAGE_ROLES)],
        limit,
    )
    feedback_by_message_id = await _load_feedback_map(
        db, [m.id for m in messages], user.id
    )

    _tool_overrides = await _saved_tool_overrides(db, conv)

    # Resolve transfer source agent name (if set)
    _ts_name: str | None = None
    if conv.transfer_source_agent_id:
        from app.models.agent import Agent

        _ts_result = await db.execute(
            select(Agent.name).where(Agent.id == conv.transfer_source_agent_id)
        )
        _ts_name = _ts_result.scalar_one_or_none()

    _turn_state = await read_conversation_turn_state(
        db, conv, owner=access == CONVERSATION_ACCESS_OWNER
    )

    # Owner's name, for the shared viewer's "Shared with you by X" banner.
    # Only looked up for a non-owner: the owner already knows.
    _owner_name: str | None = None
    if access != CONVERSATION_ACCESS_OWNER and conv.user_id is not None:
        _owner_result = await db.execute(
            select(User.full_name).where(User.id == conv.user_id)
        )
        _owner_name = _owner_result.scalar_one_or_none()

    # The direct-message threads it is continued in: the owner's own.
    _continued_in: list[str] = []
    if access == CONVERSATION_ACCESS_OWNER:
        try:
            async with db.begin_nested():
                _continued_in = sorted({b.channel for b in await list_bindings(db, conv.id)})
        except Exception as exc:
            logger.warning(
                "Could not read where conversation %s is continued: %s",
                conv.id,
                format_exception_message(exc),
            )

    # Surface the persisted compaction summary for transparency (P4.3).
    _compaction_info: ConversationCompactionInfo | None = None
    if conv.compaction_state_json:
        from app.services.conversation_memory import parse_compaction_state

        _cstate = parse_compaction_state(conv.compaction_state_json)
        if _cstate is not None:
            _compaction_info = ConversationCompactionInfo(
                directives=_cstate["directives"],
                summary=_cstate["summary"],
                compacted_count=_cstate["compacted_count"],
            )

    # Who answers it, for its owner: the one who sends in it.
    _answering = await turn_stand_in(
        db, agent_id=conv.agent_id, conversation_channel=conv.channel, user_id=conv.user_id
    )
    from app.services.conversation_continuity import agent_labels

    _deleted = (await agent_labels(db, [conv.deleted_agent_id])).get(conv.deleted_agent_id)

    return ConversationDetailResponse(
        id=str(conv.id),
        channel=conv.channel,
        agent_id=str(conv.agent_id) if conv.agent_id else None,
        answering_agent_id=str(_answering.id) if _answering is not None else None,
        deleted_agent_id=str(conv.deleted_agent_id) if conv.deleted_agent_id else None,
        deleted_agent_name=_deleted.name if _deleted is not None else None,
        transfer_source_agent_id=(
            str(conv.transfer_source_agent_id)
            if conv.transfer_source_agent_id
            else None
        ),
        transfer_source_agent_name=_ts_name,
        title=conv.title,
        is_archived=conv.is_archived,
        created_at=conv.created_at.isoformat(),
        updated_at=conv.updated_at.isoformat(),
        workspace_id=str(conv.workspace_id) if conv.workspace_id else None,
        messages=await _mark_deleted_attachments(db, _visible_message_responses(messages, feedback_by_message_id)),
        tool_overrides=_tool_overrides,
        is_processing=_turn_state.is_processing,
        pending_approvals=_turn_state.pending_approvals,
        interrupted_turn=_turn_state.interrupted_turn,
        presentation_id=(
            str(conv.presentation_id) if conv.presentation_id else None
        ),
        video_project_id=(
            str(conv.video_project_id) if conv.video_project_id else None
        ),
        custom_dashboard_id=(
            str(conv.custom_dashboard_id) if conv.custom_dashboard_id else None
        ),
        has_more=has_more,
        compaction=_compaction_info,
        access=access,
        link_access=(
            conv.link_access if access == CONVERSATION_ACCESS_OWNER else None
        ),
        owner_name=_owner_name,
        web_continuable=web_continuation_refusal(conv) is None,
        continued_in=_continued_in,
    )


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=MessagePageResponse,
)
async def get_conversation_messages(
    conversation_id: str,
    before: str | None = Query(
        default=None,
        description=(
            "Message ID cursor — return messages strictly older than this "
            "message. Omit to fetch the latest page."
        ),
    ),
    limit: int = Query(
        default=DEFAULT_OLDER_PAGE_SIZE,
        ge=1,
        le=MAX_MESSAGE_PAGE_SIZE,
        description="Maximum number of messages to return.",
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cursor-paginated message history for a conversation.

    Returns up to *limit* messages older than the *before* cursor in
    chronological order, plus ``has_more`` indicating whether earlier
    history exists. The cursor is stable across inserts: rows are ordered
    by ``(sequence, created_at, id)``.
    """
    # Paging back through history is a read: shared viewers get the same
    # cursor contract as the owner.
    conv, _access = await resolve_conversation_for_read(conversation_id, user, db)

    criteria = [Message.conversation_id == conv.id, Message.role.in_(CHAT_MESSAGE_ROLES)]
    if before:
        anchor_uuid = parse_uuid(before, label="cursor message ID")
        anchor_result = await db.execute(
            select(Message).where(
                Message.id == anchor_uuid,
                Message.conversation_id == conv.id,
            )
        )
        anchor = anchor_result.scalar_one_or_none()
        if not anchor:
            raise HTTPException(status_code=404, detail="Cursor message not found")
        criteria.append(message_cursor_filter(anchor))

    messages, has_more = await read_visible_message_page(db, criteria, limit)
    feedback_by_message_id = await _load_feedback_map(
        db, [m.id for m in messages], user.id
    )
    return MessagePageResponse(
        messages=await _mark_deleted_attachments(db, _visible_message_responses(messages, feedback_by_message_id)),
        has_more=has_more,
    )


@router.patch(
    "/conversations/{conversation_id}",
    response_model=ConversationResponse,
)
async def update_conversation(
    conversation_id: str,
    body: ConversationUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Rename a conversation, or file it into a workspace.

    Both are the owner acting on their own thread, and both are one PATCH so a
    client never has to sequence two calls and handle half of it failing.
    """
    conv_uuid = parse_conversation_id(conversation_id)
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conv_uuid,
            Conversation.user_id == user.id,
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    filing = "workspace_id" in body.model_fields_set
    target_workspace_id = None
    if filing and body.workspace_id is not None:
        # Resolved once, through the caller's visibility and the manage right,
        # exactly as creation does it: filing binds the thread to a scope whose
        # shared state and knowledge bases its agents then work with.
        from app.api.v1.helpers import get_manageable_entity
        from app.models.workspace import Workspace
        from app.services.workspaces import (
            WorkspaceActionRefusedError,
            ensure_workspace_accepts_conversations,
        )

        workspace = await get_manageable_entity(
            db, Workspace, user, body.workspace_id,
            action="use", entity_label="Workspace",
        )
        try:
            ensure_workspace_accepts_conversations(workspace)
        except WorkspaceActionRefusedError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
        target_workspace_id = workspace.id

    new_title = body.title.strip() if body.title is not None else None

    for attempt in range(2):
        # Applied on every attempt: the retry re-reads the row after a
        # rollback, which drops whatever the first attempt had set.
        if new_title is not None:
            conv.title = new_title
        if filing:
            conv.workspace_id = target_workspace_id
        try:
            await db.flush()
            await db.refresh(conv)
            break
        except DBAPIError as exc:
            if not _is_statement_timeout_error(exc):
                raise
            await db.rollback()
            logger.warning(
                "Conversation rename timed out for conversation %s (attempt %d/2)",
                conv_uuid,
                attempt + 1,
            )
            if attempt == 1:
                raise HTTPException(
                    status_code=409,
                    detail="Conversation is busy right now. Please try renaming it again.",
                ) from exc
            await asyncio.sleep(0.1)
            result = await db.execute(
                select(Conversation).where(
                    Conversation.id == conv_uuid,
                    Conversation.user_id == user.id,
                )
            )
            conv = result.scalar_one_or_none()
            if not conv:
                raise HTTPException(status_code=404, detail="Conversation not found") from exc
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return ConversationResponse(
        id=str(conv.id),
        channel=conv.channel,
        agent_id=str(conv.agent_id) if conv.agent_id else None,
        title=conv.title,
        is_archived=conv.is_archived,
        created_at=conv.created_at.isoformat(),
        updated_at=conv.updated_at.isoformat(),
        workspace_id=str(conv.workspace_id) if conv.workspace_id else None,
        presentation_id=(
            str(conv.presentation_id) if conv.presentation_id else None
        ),
        video_project_id=(
            str(conv.video_project_id) if conv.video_project_id else None
        ),
        custom_dashboard_id=(
            str(conv.custom_dashboard_id) if conv.custom_dashboard_id else None
        ),
    )


class ConversationPresentationBinding(BaseModel):
    """Body of ``PUT /conversations/{id}/presentation`` (Presentation Studio)."""

    # ``None`` unbinds: the conversation goes back to being an ordinary chat.
    presentation_id: str | None = None


@router.put(
    "/conversations/{conversation_id}/presentation",
    response_model=ConversationResponse,
)
async def bind_conversation_presentation(
    conversation_id: str,
    body: ConversationPresentationBinding,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bind an EXISTING conversation to a deck (Presentation Studio).

    ``POST /conversations`` binds at creation; this is the other way in — the
    chat card's "Open in studio" carries the conversation that produced the
    deck into the studio, so the agent keeps everything it already knows about
    the work instead of starting over from the deck's outline. A conversation
    is bound to at most one deck: re-binding moves it. Owner only, and the
    deck must be visible to the caller — the same rule as binding at creation,
    so this can no more probe for a deck's existence than that can.

    Deliberately NOT behind the AI gate: this is own-conversation metadata, like
    renaming or sharing it — no model call. The studio turn the binding sets up
    is gated where it runs (``send_message`` / the web chat's send), so with AI off
    the studio still opens read-only over a deck the user already has.
    """
    from app.services.presentations import load_visible_presentation

    conv_uuid = parse_conversation_id(conversation_id)
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conv_uuid,
                Conversation.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if body.presentation_id is None:
        conv.presentation_id = None
    else:
        presentation_uuid = parse_uuid(body.presentation_id, label="presentation ID")
        loaded = await load_visible_presentation(
            db, presentation_uuid, user_id=user.id
        )
        if loaded is None:
            raise HTTPException(status_code=404, detail="Presentation not found")
        conv.presentation_id = presentation_uuid
        # One subject at a time: a deck replaces a dashboard or fleet binding.
        conv.custom_dashboard_id = None
        conv.fleet_id = None
    await db.flush()
    await db.refresh(conv)
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return ConversationResponse(
        id=str(conv.id),
        channel=conv.channel,
        agent_id=str(conv.agent_id) if conv.agent_id else None,
        title=conv.title,
        is_archived=conv.is_archived,
        created_at=conv.created_at.isoformat(),
        updated_at=conv.updated_at.isoformat(),
        workspace_id=str(conv.workspace_id) if conv.workspace_id else None,
        presentation_id=(
            str(conv.presentation_id) if conv.presentation_id else None
        ),
        video_project_id=(
            str(conv.video_project_id) if conv.video_project_id else None
        ),
        custom_dashboard_id=(
            str(conv.custom_dashboard_id) if conv.custom_dashboard_id else None
        ),
    )


async def _visible_video_binding(
    db: AsyncSession, user: User, video_project_id: str
) -> tuple[uuid.UUID, str]:
    """The video a conversation binds to, visible to *user*, or the unknown video's 404."""
    from app.services.video_studio.projects import load_visible_project

    video_uuid = parse_uuid(video_project_id, label="video ID")
    project = await load_visible_project(db, video_uuid, user)
    if project is None:
        raise HTTPException(status_code=404, detail="Video not found")
    return project.id, project.title


class ConversationVideoBinding(BaseModel):
    """Body of ``PUT /conversations/{id}/video`` (Video Studio)."""

    # ``None`` unbinds: the conversation goes back to being an ordinary chat.
    video_project_id: str | None = None


@router.put(
    "/conversations/{conversation_id}/video",
    response_model=ConversationResponse,
)
async def bind_conversation_video(
    conversation_id: str,
    body: ConversationVideoBinding,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bind an EXISTING conversation to a video (Video Studio).

    The presentation binding's twin: the chat that made a video carries into
    its editor ("Open in editor"), so the agent keeps what it knows. Owner
    only, the video visible to the caller; a conversation edits one video (or
    one deck) at a time, so binding it to a video unbinds any deck. Not behind
    the AI gate for the same reason as the deck binding.
    """
    conv_uuid = parse_conversation_id(conversation_id)
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conv_uuid,
                Conversation.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if body.video_project_id is None:
        conv.video_project_id = None
    else:
        conv.video_project_id, _title = await _visible_video_binding(
            db, user, body.video_project_id
        )
        conv.presentation_id = None
        conv.custom_dashboard_id = None
        conv.fleet_id = None
    await db.flush()
    await db.refresh(conv)
    await db.commit()

    return ConversationResponse(
        id=str(conv.id),
        channel=conv.channel,
        agent_id=str(conv.agent_id) if conv.agent_id else None,
        title=conv.title,
        is_archived=conv.is_archived,
        created_at=conv.created_at.isoformat(),
        updated_at=conv.updated_at.isoformat(),
        workspace_id=str(conv.workspace_id) if conv.workspace_id else None,
        presentation_id=None,
        video_project_id=(
            str(conv.video_project_id) if conv.video_project_id else None
        ),
        custom_dashboard_id=(
            str(conv.custom_dashboard_id) if conv.custom_dashboard_id else None
        ),
    )


async def _visible_dashboard_binding(
    db: AsyncSession, user: User, custom_dashboard_id: str
) -> tuple[uuid.UUID, str]:
    """The dashboard a conversation binds to, readable by *user* as themselves, or the unknown dashboard's 404."""
    from app.services.custom_dashboards import load_dashboard_for

    # Off, no dashboard is offered: binding to one answers like an unknown id.
    if not is_feature_enabled("DASHBOARD_STUDIO"):
        raise HTTPException(status_code=404, detail="Dashboard not found")
    dashboard_uuid = parse_uuid(custom_dashboard_id, label="dashboard ID")
    found = await load_dashboard_for(db, dashboard_uuid, user_id=user.id)
    if found is None:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return found[0].id, found[0].title


class ConversationDashboardBinding(BaseModel):
    """Body of ``PUT /conversations/{id}/dashboard`` (Dashboard Studio)."""

    # ``None`` unbinds: the conversation goes back to being an ordinary chat.
    custom_dashboard_id: str | None = None


@router.put(
    "/conversations/{conversation_id}/dashboard",
    response_model=ConversationResponse,
)
async def bind_conversation_dashboard(
    conversation_id: str,
    body: ConversationDashboardBinding,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bind an EXISTING conversation to a custom dashboard (Dashboard Studio).

    The video binding's twin: the chat that built a dashboard carries into its
    studio, so the agent keeps what it knows. Owner only, the dashboard
    readable by the caller as themselves; a conversation edits one subject at a
    time, so binding it to a dashboard unbinds any deck or video.
    """
    conv_uuid = parse_conversation_id(conversation_id)
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conv_uuid,
                Conversation.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if body.custom_dashboard_id is None:
        conv.custom_dashboard_id = None
    else:
        conv.custom_dashboard_id, _title = await _visible_dashboard_binding(db, user, body.custom_dashboard_id)
        conv.presentation_id = None
        conv.video_project_id = None
        conv.fleet_id = None
    await db.flush()
    await db.refresh(conv)
    await db.commit()

    return ConversationResponse(
        id=str(conv.id),
        channel=conv.channel,
        agent_id=str(conv.agent_id) if conv.agent_id else None,
        title=conv.title,
        is_archived=conv.is_archived,
        created_at=conv.created_at.isoformat(),
        updated_at=conv.updated_at.isoformat(),
        workspace_id=str(conv.workspace_id) if conv.workspace_id else None,
        presentation_id=None,
        video_project_id=None,
        custom_dashboard_id=str(conv.custom_dashboard_id) if conv.custom_dashboard_id else None,
    )


async def _visible_fleet_binding(db: AsyncSession, user: User, fleet_id: str) -> tuple[uuid.UUID, str]:
    """The fleet a conversation binds to, visible to *user* as themselves and not archived, or the unknown fleet's 404."""
    from app.services.fleet_studio import load_fleet_studio_context

    fleet_uuid = parse_uuid(fleet_id, label="fleet ID")
    found = await load_fleet_studio_context(db, user_id=user.id, fleet_id=fleet_uuid)
    if found is None:
        raise HTTPException(status_code=404, detail="Fleet not found")
    return found.fleet.id, found.fleet.name


class ConversationFleetBinding(BaseModel):
    """Body of ``PUT /conversations/{id}/fleet`` (fleet page Modify with AI)."""

    # ``None`` unbinds: the conversation goes back to being an ordinary chat.
    fleet_id: str | None = None


@router.put(
    "/conversations/{conversation_id}/fleet",
    response_model=ConversationResponse,
)
async def bind_conversation_fleet(
    conversation_id: str,
    body: ConversationFleetBinding,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bind an EXISTING conversation to a fleet (fleet page Modify with AI).

    The dashboard binding's twin: owner only, the fleet visible to the caller
    as themselves; a conversation edits one subject at a time, so binding it
    to a fleet unbinds any deck, video or dashboard.
    """
    conv_uuid = parse_conversation_id(conversation_id)
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conv_uuid,
                Conversation.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if body.fleet_id is None:
        conv.fleet_id = None
    else:
        conv.fleet_id, _name = await _visible_fleet_binding(db, user, body.fleet_id)
        conv.presentation_id = None
        conv.video_project_id = None
        conv.custom_dashboard_id = None
    await db.flush()
    await db.refresh(conv)
    await db.commit()

    return ConversationResponse(
        id=str(conv.id),
        channel=conv.channel,
        agent_id=str(conv.agent_id) if conv.agent_id else None,
        title=conv.title,
        is_archived=conv.is_archived,
        created_at=conv.created_at.isoformat(),
        updated_at=conv.updated_at.isoformat(),
        workspace_id=str(conv.workspace_id) if conv.workspace_id else None,
        presentation_id=None,
        video_project_id=None,
        custom_dashboard_id=None,
        fleet_id=str(conv.fleet_id) if conv.fleet_id else None,
    )


class ConversationAgentBinding(BaseModel):
    """Body of ``PUT /conversations/{id}/agent``."""

    # ``None`` continues it with the General Assistant (its stand-in when one
    # is linked, as a new conversation would be).
    agent_id: str | None = None


@router.put(
    "/conversations/{conversation_id}/agent",
    response_model=ConversationResponse,
)
async def continue_conversation_with_agent(
    conversation_id: str,
    body: ConversationAgentBinding,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _chat_gate: None = Depends(require_chat_enabled),
):
    """Continue an EXISTING conversation with another agent, in place.

    The conversation keeps its whole history; from the next message the new
    agent answers in it and reads the earlier turns labelled with the agent
    that wrote them, the deleted agent's included. A note "Continued by
    <agent> on <date>" marks the change (``conversation_continuity``). Owner
    only, a web conversation (a Slack or Teams thread is answered by the agent
    its bot routes to), the agent one the caller could start a conversation
    with, and not while a turn is running in it.
    """
    from app.services.conversation_continuity import agent_labels, continue_with_agent

    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == parse_conversation_id(conversation_id),
                Conversation.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conv.channel != "web":
        raise HTTPException(
            status_code=400,
            detail=(
                "This conversation is answered by the agent its channel routes to: "
                "continue it there, or start a conversation with the agent here."
            ),
        )
    if body.agent_id:
        agent = await _chat_agent(db, user, body.agent_id)
    else:
        from app.services.general_assistant_link import general_assistant_stand_in

        agent = await general_assistant_stand_in(db)
    try:
        _r = await redis_client()
        _running = bool(await _r.exists(chat_processing_key(conv.id)))
    except Exception:
        _running = False
    if _running:
        raise HTTPException(
            status_code=409,
            detail="An answer is in progress: wait for it to end, or stop it, then continue with another agent.",
        )
    await continue_with_agent(db, conv, agent)
    await db.commit()
    await db.refresh(conv)

    names = await agent_labels(db, [conv.agent_id, conv.deleted_agent_id])
    current = names.get(conv.agent_id) if conv.agent_id else None
    deleted = names.get(conv.deleted_agent_id) if conv.deleted_agent_id else None
    return ConversationResponse(
        id=str(conv.id),
        channel=conv.channel,
        agent_id=str(conv.agent_id) if conv.agent_id else None,
        agent_name=current.name if current else None,
        agent_icon=current.icon if current else None,
        title=conv.title,
        is_archived=conv.is_archived,
        created_at=conv.created_at.isoformat(),
        updated_at=conv.updated_at.isoformat(),
        workspace_id=str(conv.workspace_id) if conv.workspace_id else None,
        presentation_id=str(conv.presentation_id) if conv.presentation_id else None,
        video_project_id=str(conv.video_project_id) if conv.video_project_id else None,
        deleted_agent_id=str(conv.deleted_agent_id) if conv.deleted_agent_id else None,
        deleted_agent_name=deleted.name if deleted else None,
    )


@router.delete(
    "/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def archive_conversation(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == parse_conversation_id(conversation_id),
            Conversation.user_id == user.id,
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conv.is_archived = True
    archived_id = conv.id
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    # Its sandbox workspace goes with it, off the request.
    from app.services.sandbox_workspace.gc import collect_conversations_soon

    collect_conversations_soon([archived_id], reason="conversation_archived")


@router.delete(
    "/conversations/{conversation_id}/interrupted-turn",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def dismiss_interrupted_turn(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stop showing the conversation's interrupted turn, owner only.

    Its checkpointed tool work stays: the next turn still resumes it.
    """
    from app.services import chat_turn_guard

    result = await db.execute(
        select(Conversation.id).where(
            Conversation.id == parse_conversation_id(conversation_id),
            Conversation.user_id == user.id,
        )
    )
    conv_id = result.scalar_one_or_none()
    if conv_id is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        await chat_turn_guard.dismiss_interrupted_turn(str(conv_id))
    except Exception as exc:
        logger.warning(
            "Could not dismiss the interrupted turn of conversation %s: %s",
            conv_id,
            format_exception_message(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The interrupted response could not be dismissed. Try again.",
        ) from None


@router.get(
    "/conversations/{conversation_id}/awaited-work",
    response_model=AwaitedWorkResponse,
)
async def get_awaited_work(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The work that resumes the conversation once it ends.

    Readable wherever the conversation is; the work is its owner's, so any
    other reader gets an empty list.
    """
    from app.services.conversation_awaited_work import awaited_work

    conv, access = await resolve_conversation_for_read(conversation_id, user, db)
    if access != CONVERSATION_ACCESS_OWNER:
        return AwaitedWorkResponse()
    return AwaitedWorkResponse(
        items=[AwaitedWorkItem(**item.as_dict()) for item in await awaited_work(conv.id)]
    )


class SandboxWorkspaceFileInfo(BaseModel):
    path: str
    size: int
    mtime: float


class SandboxWorkspaceInfo(BaseModel):
    """The conversation's code interpreter workspace: its size, use and files."""

    exists: bool = False
    size_bytes: int = 0
    file_count: int = 0
    quota_bytes: int = 0
    last_used_at: str | None = None
    last_gc_at: str | None = None
    kernel_restarts: int = 0
    bytes_read: int = 0
    bytes_written: int = 0
    #: The most recently changed first, at most 200.
    files: list[SandboxWorkspaceFileInfo] = Field(default_factory=list)


@router.get(
    "/conversations/{conversation_id}/sandbox-workspace",
    response_model=SandboxWorkspaceInfo,
)
async def get_sandbox_workspace(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The conversation's sandbox workspace, owner only: anyone else reads the conversation's 404."""
    from app.services.sandbox_workspace.manifest import Limits
    from app.services.sandbox_workspace.session import WorkspaceSession, WorkspaceUnavailableError
    from app.services.sandbox_workspace.store import WorkspaceScope

    conv, access = await resolve_conversation_for_read(conversation_id, user, db)
    if access != CONVERSATION_ACCESS_OWNER:
        raise HTTPException(status_code=404, detail="Conversation not found")
    quota = Limits.from_settings().quota_bytes
    session = WorkspaceSession(WorkspaceScope(user_id=user.id, conversation_id=conv.id))
    try:
        record = await session.record(create=False)
        entries = await session.entries() if record is not None else {}
    except WorkspaceUnavailableError:
        record, entries = None, {}
    if record is None:
        return SandboxWorkspaceInfo(quota_bytes=quota)
    files = sorted(entries.values(), key=lambda e: (-e.mtime, e.path))[:200]
    return SandboxWorkspaceInfo(
        exists=True,
        size_bytes=record.size_bytes,
        file_count=record.file_count,
        quota_bytes=quota,
        last_used_at=record.last_used_at.isoformat() if record.last_used_at else None,
        last_gc_at=record.last_gc_at.isoformat() if record.last_gc_at else None,
        kernel_restarts=record.kernel_restarts,
        bytes_read=record.bytes_read,
        bytes_written=record.bytes_written,
        files=[SandboxWorkspaceFileInfo(path=e.path, size=e.size, mtime=e.mtime) for e in files],
    )


@router.delete("/conversations", status_code=status.HTTP_204_NO_CONTENT)
async def archive_all_conversations(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Archive all non-archived conversations for the current user."""
    await archive_user_conversations(db, user)


async def archive_user_conversations(
    db: AsyncSession, user: User, *, web_listed_only: bool = False
) -> None:
    """Archive the user's non-archived conversations, and their sandbox workspaces with them.

    *web_listed_only* keeps it to what the web sidebar lists (``web_listable_clause``):
    its clear-all never reaches a channel's conversations it does not show.
    """
    scope = [web_listable_clause()] if web_listed_only else []
    archived = await db.execute(
        update(Conversation)
        .where(
            Conversation.user_id == user.id,
            Conversation.is_archived.is_(False),
            *scope,
        )
        .values(is_archived=True)
        .returning(Conversation.id)
    )
    archived_ids = list(archived.scalars().all())
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    # Their sandbox workspaces go with them, off the request.
    from app.services.sandbox_workspace.gc import collect_conversations_soon

    collect_conversations_soon(archived_ids, reason="conversation_archived")


@router.post(
    "/conversations/{conversation_id}/return-to-source",
    response_model=ConversationDetailResponse,
)
async def return_to_source(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Return the conversation to the agent it was at before the last transfer."""
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == parse_conversation_id(conversation_id),
            Conversation.user_id == user.id,
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if not conv.transfer_source_agent_id:
        raise HTTPException(status_code=400, detail="No transfer source to return to")

    conv.agent_id = conv.transfer_source_agent_id
    conv.transfer_source_agent_id = None
    await db.commit()
    await db.refresh(conv)

    messages, has_more = await read_visible_message_page(
        db,
        [Message.conversation_id == conv.id, Message.role.in_(CHAT_MESSAGE_ROLES)],
        DEFAULT_MESSAGE_PAGE_SIZE,
    )

    _tool_overrides = await _saved_tool_overrides(db, conv)

    return ConversationDetailResponse(
        id=str(conv.id),
        channel=conv.channel,
        agent_id=str(conv.agent_id) if conv.agent_id else None,
        title=conv.title,
        is_archived=conv.is_archived,
        created_at=conv.created_at.isoformat(),
        updated_at=conv.updated_at.isoformat(),
        messages=await _mark_deleted_attachments(db, _visible_message_responses(messages)),
        tool_overrides=_tool_overrides,
        has_more=has_more,
    )


#: ``knowledge_base_skipped_reason`` when the agent has knowledge bases but no
#: write grant on any of them. The other values are the shared gate's
#: ``helpers.KB_WRITE_*`` reasons.
KB_UPLOAD_NO_WRITE_GRANT = "no_write_grant"


async def _resolve_upload_knowledge_base(
    db: AsyncSession, user: User, agent: Any
) -> tuple[Any, Any]:
    """Pick the knowledge base a chat upload is copied into, if any.

    Returns ``(knowledge_base, None)``, or ``(None, refusal)`` when the agent
    has knowledge bases but none may receive the file, or ``(None, None)``
    when it has none. The candidates are the agent's write scope
    (``writable_knowledge_base_ids``: bound AND granted ``write``, in binding
    order) and each must pass the shared content-write gate AS THE UPLOADER:
    not package-installed, not built-in, manageable by the uploader
    (company-managed = admins only, share = config tier), not read-only.

    The uploader's rights count here, while ``store_knowledge`` only needs the
    agent's grant, because the upload is the uploader's own write with no
    agent judgment in between - the same write as the knowledge base's own
    upload route, which applies the same gate. The first candidate that
    passes is used; when none does, the first candidate's refusal is reported.
    """
    from app.api.v1.helpers import (
        KnowledgeBaseWriteRefusal,
        get_user_group_ids,
        knowledge_base_write_refusal,
    )
    from app.models.knowledge_base import KnowledgeBase

    if not agent.knowledge_base_ids:
        return None, None
    no_grant = KnowledgeBaseWriteRefusal(
        KB_UPLOAD_NO_WRITE_GRANT,
        "the agent has no write access to its knowledge bases",
    )
    candidate_ids = agent.writable_knowledge_base_ids
    candidate_uuids = [u for u in (safe_uuid(k) for k in candidate_ids) if u]
    if not candidate_uuids:
        return None, no_grant
    rows = await db.execute(
        select(KnowledgeBase).where(KnowledgeBase.id.in_(candidate_uuids))
    )
    kb_by_id = {str(kb.id): kb for kb in rows.scalars().all()}
    group_ids = None if user.is_admin else await get_user_group_ids(db, user.id)
    first_refusal = None
    for kb_id in candidate_ids:
        kb = kb_by_id.get(str(kb_id))
        if kb is None:
            continue
        refusal = await knowledge_base_write_refusal(
            db,
            kb,
            user,
            group_ids,
            action="add documents to",
            content_action="add documents to it",
        )
        if refusal is None:
            return kb, None
        first_refusal = first_refusal or refusal
    return None, first_refusal or no_grant


async def _turn_offers_code_interpreter(db: AsyncSession, conv: Conversation | None, user_id: object) -> bool:
    """:func:`_upload_offers_code_interpreter` for a turn that holds the user's id alone; ``False`` on any doubt."""
    if conv is None:
        return False
    try:
        user = await db.get(User, user_id)
        return user is not None and await _upload_offers_code_interpreter(db, conv, user)
    except Exception as exc:
        logger.debug("Code interpreter offer not resolved for a resume: %s", format_exception_message(exc))
        return False


async def _upload_offers_code_interpreter(db: AsyncSession, conv: Conversation, user: User) -> bool:
    """Whether the turn that reads an upload message offers the code interpreter.

    The message is written before any turn runs, so no loop has bound its
    tool surface yet: this is the decision the next turn's entry point makes
    for the code interpreter from the conversation's agent (or the General
    Assistant) effective tools - the deployment switch, the built-in
    selection the conversation saved (else the agent's own list, or the
    General Assistant's with the user's profile override), then the agent's
    deny list - read through the same ``code_interpreter_offered`` as the
    loop's surface. A same-named MCP or custom tool, which would replace the
    built-in on that surface, is not resolved here - an MCP server's tools
    are only known once it is connected; everything produced inside a turn
    reads the loop's final surface instead.
    """
    if security_option("disable_code_interpreter"):
        return False
    selection: list[str] | None = None
    saved = (saved_tool_selection(conv) or {}).get("builtin_tool_ids")
    if isinstance(saved, list):
        selection = [str(tool_id) for tool_id in saved]
    deny_list: list[str] | None = None
    if conv.agent_id:
        from app.models.agent import Agent

        agent = (await db.execute(select(Agent).where(Agent.id == conv.agent_id))).scalar_one_or_none()
        if agent is None or not agent.enabled:
            # A disabled agent runs no turn: nothing will offer the tool.
            return False
        builtin_ids = selection if selection is not None else list(agent.builtin_tool_ids or [])
        deny_list = agent.tool_deny_list
    else:
        from app.models.general_assistant_config import GeneralAssistantConfig, general_assistant_builtin_tool_ids

        ga_config = (await db.execute(select(GeneralAssistantConfig).limit(1))).scalar_one_or_none()
        builtin_ids = (
            selection
            if selection is not None
            else general_assistant_builtin_tool_ids(ga_config.builtin_tool_ids if ga_config else None, user)
        )
    if "code_interpreter" not in builtin_ids:
        return False
    from app.services.builtin_tools import apply_tool_deny_list, build_code_interpreter_tool
    from app.services.code_interpreter import code_interpreter_offered

    definitions, _executor = build_code_interpreter_tool()
    return code_interpreter_offered(apply_tool_deny_list(definitions, deny_list))


@router.post("/conversations/{conversation_id}/upload")
async def upload_file_to_conversation(
    conversation_id: str,
    file: UploadFile = File(...),
    create_message: bool = Query(
        True,
        description="When False, only store the file in S3 without creating a chat message. Used when files will be attached to the next user message.",
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Upload a file to a conversation.

    The file is stored persistently in S3/MinIO and its metadata is
    recorded in the ``file_attachments`` table so it can be retrieved
    later via the download endpoint.

    When ``create_message=True`` (default), a user message is created with
    the extracted text content.  When ``create_message=False``, only the
    file is stored — the caller is expected to attach the returned
    ``file_id`` to a subsequent ``send_message`` call via ``file_ids``.
    """
    from app.models.file_attachment import FileAttachment
    from app.services.chunking import extract_text_from_file_async
    from app.services.file_storage import file_storage, guess_content_type

    result = await db.execute(
        select(Conversation).where(
            Conversation.id == parse_conversation_id(conversation_id),
            Conversation.user_id == user.id,
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    # A Slack or Teams group thread is continued on its channel only:
    # a file stored here would reach the turns its other participants read.
    _continuation_refusal = web_continuation_refusal(conv)
    if _continuation_refusal:
        raise HTTPException(status_code=409, detail=_continuation_refusal)

    raw_bytes = await file.read()
    from app.services.file_storage import MAX_UPLOAD_SIZE_BYTES

    if len(raw_bytes) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({len(raw_bytes):,} bytes). Maximum is {MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)} MB.",
        )
    filename = file.filename or "uploaded_file"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"
    content_type = file.content_type or guess_content_type(filename)

    # ---- Store file in S3 ----
    s3_prefix = f"chat/{conversation_id}"
    s3_object_name = await file_storage.upload(
        data=raw_bytes,
        filename=filename,
        content_type=content_type,
        prefix=s3_prefix,
    )

    # ---- Create FileAttachment record ----
    file_att = FileAttachment(
        user_id=user.id,
        conversation_id=conv.id,
        filename=filename,
        content_type=content_type,
        size=len(raw_bytes),
        s3_object_name=s3_object_name,
        source="chat",
    )
    db.add(file_att)
    await db.flush()  # Get the ID

    file_id = str(file_att.id)

    # ---- Extract text for LLM context (best-effort) ----
    from app.llm.file_resolver import file_kind, upload_header
    from app.services.attachment_extract import (
        attachment_kind,
        inline_attachment_text_async,
        inline_truncation_footer,
    )

    content = ""
    # An HTML / JSON / JSON Lines / CSV attachment is recognised by extension
    # OR MIME type (``.htm``, ``.xhtml``, a ``text/html`` upload under another
    # name), so the rendered message below is never skipped for want of an
    # ``.html`` suffix.
    structured_attachment = attachment_kind(filename, content_type) is not None
    is_text_extractable = structured_attachment or ext in (
        "pdf",
        "docx",
        "xlsx",
        "xls",
        "csv",
        "txt",
        "md",
        "json",
        "xml",
        "html",
        "py",
        "js",
        "ts",
        "tsx",
        "jsx",
    )
    if is_text_extractable:
        try:
            # Off-loop: parsing a 40 MB PDF/XLSX inline froze the event loop
            # and failed the liveness probe.
            content = await extract_text_from_file_async(raw_bytes, filename)
        except Exception as exc:
            logger.warning("Text extraction failed for %s: %s", filename, format_exception_message(exc))

    # ---- Upload-only mode: return metadata without creating a message ----
    if not create_message:
        # Commit before answering: the caller attaches this file_id to its next
        # message, and the get_db teardown only commits after the response is sent.
        await db.commit()
        return {
            "status": "ok",
            "filename": filename,
            "type": ext,
            "size": len(raw_bytes),
            "file_id": file_id,
            "download_url": f"/api/v1/chat/files/{file_id}/download",
            "content_type": content_type,
            "characters": len(content) if content else 0,
            "vectorized": False,
            "message": "File uploaded (no message created). Attach file_id to the next message.",
        }

    # ---- Build message content ----
    if content and len(content.strip()) >= 10:
        # The MESSAGE carries what the inline fallback inlines
        # (``inline_attachment_text``): the plain extraction whole
        # while it fits the message cap, and for an HTML / JSON / CSV file
        # the cap would cut, its rendering rather than a prefix of raw source -
        # the rendering ``read_file_content(mode="extract_structured")``
        # serves, so the footer's offset means the same characters there.
        # ``content`` above is that plain extraction: the rule decides from it
        # instead of extracting the file again, and it stays the knowledge-base
        # copy below.
        max_content_chars = 50_000
        display_content, total = content[:max_content_chars], len(content)
        # Only a rendering or a cut can name the code interpreter: the decision
        # reads the agent or General Assistant row, so it is taken for those alone.
        code_interpreter = len(content) > max_content_chars and await _upload_offers_code_interpreter(
            db, conv, user
        )
        if structured_attachment:
            try:
                inline = await inline_attachment_text_async(
                    raw_bytes, filename, content_type, code_interpreter, max_content_chars, content
                )
                display_content, total = inline.text, inline.total
            except Exception as exc:
                logger.warning(
                    "Attachment rendering failed for %s: %s",
                    filename,
                    format_exception_message(exc),
                )
        if total > max_content_chars:
            display_content += "\n\n" + inline_truncation_footer(
                file_id=file_id,
                filename=filename,
                content_type=content_type,
                shown=max_content_chars,
                total=total,
                code_interpreter=code_interpreter,
            )
        # The file is named by its id; the uploaded name - the client's text,
        # often a third party's - follows in an untrusted block.
        message_content = f"{upload_header(file_id, filename)}\n\n{display_content}"
    else:
        # Non-text file (image, video, etc.) — just reference it
        message_content = upload_header(
            file_id, filename, f" ({file_kind(filename)}, {len(raw_bytes):,} bytes)"
        )

    # ---- Build attachment metadata with file_id for download ----
    attachment_meta = json.dumps(
        {
            "attachments": [
                {
                    "filename": filename,
                    "type": ext,
                    "size": len(raw_bytes),
                    "file_id": file_id,
                    "content_type": content_type,
                }
            ],
        }
    )

    msg = await store_message(
        db,
        conv.id,
        role="user",
        content=message_content,
        channel="web",
        metadata_json=attachment_meta,
    )

    # Link the file attachment to the message
    file_att.message_id = msg.id

    # ---- Copy into the agent's knowledge base, when it may receive it ----
    # The file is attached and in the message above whatever happens here;
    # only this copy into a shared collection is gated.
    from app.api.v1.helpers import log_kb_activity
    from app.models.agent import Agent
    from app.models.knowledge_base import KnowledgeDocument
    from app.services.kb_helpers import bump_kb_revision

    target_kb = None
    skipped = None
    if conv.agent_id and content and len(content.strip()) >= 10:
        agent_result = await db.execute(select(Agent).where(Agent.id == conv.agent_id))
        agent = agent_result.scalar_one_or_none()
        if agent:
            target_kb, skipped = await _resolve_upload_knowledge_base(db, user, agent)

    if target_kb is not None:
        kb_id = str(target_kb.id)
        source_type = "pdf" if ext == "pdf" else "docx" if ext == "docx" else "text"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        doc = KnowledgeDocument(
            knowledge_base_id=target_kb.id,
            filename=filename,
            source_type=source_type,
            content=content,
            content_hash=content_hash,
            status="pending",
        )
        db.add(doc)
        await db.flush()
        target_kb.document_count = (target_kb.document_count or 0) + 1
        await bump_kb_revision(db, target_kb.id)
        await log_kb_activity(
            db,
            target_kb.id,
            "document_added",
            details=f"Uploaded file '{filename}' in a chat conversation",
            source_type=source_type,
            document_count=1,
        )

        await db.commit()

        # Enqueue embedding job
        await enqueue_document_processing(str(doc.id))

        return {
            "status": "ok",
            "filename": filename,
            "type": ext,
            "size": len(raw_bytes),
            "file_id": file_id,
            "download_url": f"/api/v1/chat/files/{file_id}/download",
            "content_type": content_type,
            "characters": len(content) if content else 0,
            "vectorized": True,
            "knowledge_base_id": kb_id,
            "knowledge_base_skipped_reason": None,
            "message": "File uploaded and queued for vectorization into knowledge base.",
        }

    message = "File uploaded. Content included in conversation for AI analysis."
    if skipped is not None:
        message += (
            " It was not added to the agent's knowledge base: "
            f"{skipped.detail.rstrip('.')}."
        )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return {
        "status": "ok",
        "filename": filename,
        "type": ext,
        "size": len(raw_bytes),
        "file_id": file_id,
        "download_url": f"/api/v1/chat/files/{file_id}/download",
        "content_type": content_type,
        "characters": len(content) if content else 0,
        "vectorized": False,
        "knowledge_base_skipped_reason": skipped.reason if skipped else None,
        "message": message,
    }


# ---------------------------------------------------------------------------
# Conversation files panel
# ---------------------------------------------------------------------------


async def _has_conversation_file_access_grant(
    admin_id: uuid.UUID, conversation_id: uuid.UUID
) -> bool:
    """True when this admin holds a live break-glass lease on the conversation.

    **Fails closed.** A Redis outage means we cannot prove the admin
    acknowledged anything, and the payload here is another user's private
    conversation content — so an unreachable Redis denies access rather than
    degrading to "allow". The caller surfaces this as the same
    acknowledgement-required response, which the UI turns into the gate; the
    admin re-acknowledges once Redis is back.
    """
    try:
        r = await redis_client()
        return bool(
            await r.exists(
                conversation_file_access_grant_key(admin_id, conversation_id)
            )
        )
    except Exception as exc:
        logger.warning(
            "Break-glass grant lookup failed (denying access) for admin=%s "
            "conversation=%s: %s",
            admin_id,
            conversation_id,
            format_exception_message(exc),
        )
        return False


async def _resolve_conversation_for_file_listing(
    conversation_id: str,
    user: User,
    db: AsyncSession,
) -> tuple[Conversation, bool]:
    """Authorize a conversation-files request.

    Returns ``(conversation, via_break_glass)``.

    Four outcomes:

    * **Owner** — unrestricted, ``via_break_glass=False``.
    * **Shared viewer** — someone the conversation is shared with, or
      any authenticated user when ``link_access`` is ``org_read``. Files are
      part of the transcript they were already granted, so they list without
      breaking any glass and ``via_break_glass`` stays ``False``.
    * **Non-owner admin** — allowed only while holding a live break-glass
      lease (see :func:`acknowledge_conversation_file_access`); otherwise
      ``403`` with :data:`CONFIDENTIALITY_ACK_REQUIRED_DETAIL`.
    * **Anyone else** — ``404``, deliberately identical to the
      conversation-not-found response so a non-admin cannot probe which
      conversation ids exist. (The owner-scoped endpoints in this module use
      the same indistinguishable-404 convention.)

    Order matters: the share check runs *before* the admin branch, so an admin
    who has legitimately been shared a conversation reads it as an ordinary
    recipient instead of being pushed through a break-glass acknowledgement
    that would misreport them in the audit trail as having overridden access
    they were actually granted.
    """
    conv_uuid = parse_conversation_id(conversation_id)
    result = await db.execute(select(Conversation).where(Conversation.id == conv_uuid))
    conv = result.scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    access = await conversation_access(db, user, conv)
    if access is not None:
        return conv, False

    if user.role != "admin":
        raise HTTPException(status_code=404, detail="Conversation not found")

    if not await _has_conversation_file_access_grant(user.id, conv.id):
        raise HTTPException(
            status_code=403, detail=CONFIDENTIALITY_ACK_REQUIRED_DETAIL
        )
    return conv, True


@router.post(
    "/conversations/{conversation_id}/files/access-ack",
    response_model=ConversationFileAccessAckResponse,
)
async def acknowledge_conversation_file_access(
    conversation_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Record an admin's confidentiality acknowledgement ("break the glass").

    Listing another user's conversation files is a legitimate support and
    debugging need, but the contents are private by default — so the access is
    deliberately made *deliberate*: the admin confirms, in the UI, that they
    understand the confidentiality expectation, and that confirmation lands
    here before any filename is returned.

    Two things happen, in this order:

    1. An audit entry is written (``action="break_glass"``,
       ``entity_type="conversation_files"``) naming the admin, the
       conversation and its owner. This is the durable record, and it is
       committed even though the grant below may still fail — an
       acknowledgement that was made must not be able to vanish.
    2. A short-lived Redis lease is set
       (:data:`CONVERSATION_FILE_ACCESS_GRANT_TTL_SECONDS`) that
       :func:`_resolve_conversation_for_file_listing` requires.

    Owners never need this: acknowledging on your own conversation is a no-op
    that writes no audit entry (it would be noise), and the listing endpoint
    already admits owners directly. Non-admins get the same ``404`` the
    listing returns, so this endpoint cannot be used to probe conversation
    existence either.
    """
    from app.services.audit import log_audit

    conv_uuid = parse_conversation_id(conversation_id)
    result = await db.execute(select(Conversation).where(Conversation.id == conv_uuid))
    conv = result.scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Owning your own conversation needs no glass to break.
    if conv.user_id == user.id:
        return ConversationFileAccessAckResponse(
            expires_in_seconds=CONVERSATION_FILE_ACCESS_GRANT_TTL_SECONDS
        )

    if user.role != "admin":
        raise HTTPException(status_code=404, detail="Conversation not found")

    owner_email: str | None = None
    if conv.user_id is not None:
        owner_result = await db.execute(
            select(User.email).where(User.id == conv.user_id)
        )
        owner_email = owner_result.scalar_one_or_none()

    await log_audit(
        db,
        user=user,
        action="break_glass",
        entity_type="conversation_files",
        entity_id=conv.id,
        entity_name=conv.title,
        summary=(
            f"Admin '{user.email}' acknowledged confidentiality to view files "
            f"in conversation '{conv.title}'"
            + (f" owned by '{owner_email}'" if owner_email else "")
        ),
        details={
            "conversation_id": str(conv.id),
            "conversation_owner_id": (
                str(conv.user_id) if conv.user_id else None
            ),
            "conversation_owner_email": owner_email,
            "grant_ttl_seconds": CONVERSATION_FILE_ACCESS_GRANT_TTL_SECONDS,
            "reason": "admin_debug_conversation_files",
        },
        request=request,
    )
    # Commit the audit entry BEFORE minting the grant. If the grant write
    # below fails we still have a durable record that the acknowledgement was
    # made; the reverse ordering could hand out access with no audit trail.
    await db.commit()

    try:
        r = await redis_client()
        await r.setex(
            conversation_file_access_grant_key(user.id, conv.id),
            CONVERSATION_FILE_ACCESS_GRANT_TTL_SECONDS,
            "1",
        )
    except Exception as exc:
        logger.error(
            "Failed to persist break-glass grant for admin=%s conversation=%s: %s",
            user.id,
            conv.id,
            format_exception_message(exc),
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Could not record the access acknowledgement. Please try "
                "again in a moment."
            ),
        ) from None

    return ConversationFileAccessAckResponse(
        expires_in_seconds=CONVERSATION_FILE_ACCESS_GRANT_TTL_SECONDS
    )


@router.get(
    "/conversations/{conversation_id}/files",
    response_model=ConversationFilesResponse,
)
async def list_conversation_files(
    conversation_id: str,
    limit: int = Query(
        default=DEFAULT_CONVERSATION_FILES_PAGE_SIZE,
        ge=1,
        le=MAX_CONVERSATION_FILES_PAGE_SIZE,
        description="Number of files to return, newest first.",
    ),
    offset: int = Query(default=0, ge=0),
    origin: Literal["uploaded", "generated", "other"] | None = Query(
        default=None,
        description="Filter to one origin bucket (omit for all files).",
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List every file attached to or produced inside a conversation.

    Backs the "Conversation files" panel: user uploads and agent-generated
    artifacts in one place, newest first, so a long thread's outputs don't have
    to be found by scrolling back to the message that carries them.

    Authorization is delegated to
    :func:`_resolve_conversation_for_file_listing` — owners always, non-owner
    admins only behind an audited break-glass acknowledgement.

    ``total`` reports the size of the *filtered* set, so the panel can page
    without re-deriving counts client-side.
    """
    from app.models.file_attachment import FileAttachment

    conv, via_break_glass = await _resolve_conversation_for_file_listing(
        conversation_id, user, db
    )

    where = [FileAttachment.conversation_id == conv.id, _listed_file_predicate()]
    origin_predicate = _origin_sql_predicate(origin)
    if origin_predicate is not None:
        where.append(origin_predicate)

    total = (
        await db.execute(
            select(func.count()).select_from(FileAttachment).where(*where)
        )
    ).scalar_one()

    # Newest first, with ``id`` as a deterministic tiebreaker: ``created_at``
    # has sub-second collisions when an agent writes several files in one turn,
    # and an unstable sort would make rows jump between pages.
    result = await db.execute(
        select(FileAttachment)
        .where(*where)
        .order_by(FileAttachment.created_at.desc(), FileAttachment.id.desc())
        .offset(offset)
        .limit(limit)
    )
    page = list(result.scalars().all())

    # Which download endpoint to point at depends on WHO is asking:
    #
    # * Owners get the message-scoped URL when the ``message_id`` linkage
    #   exists — it is the narrower of the two authorization paths.
    # * Break-glass admins must get the id-scoped URL, because
    #   ``download_message_file`` matches ``Conversation.user_id == user.id``
    #   and so 403s for every non-owner regardless of role. Handing an admin
    #   message-scoped URLs would list files that cannot be downloaded. The
    #   id-scoped endpoint routes through ``_authorize_file_access``, whose
    #   owner/admin branch admits them.
    #
    # Rows with no ``message_id`` fall back to the id-scoped URL either way.
    def _download_url(row) -> str:
        if via_break_glass or not row.message_id:
            return attachment_download_url(str(row.id))
        return attachment_download_url(
            str(row.id),
            conversation_id=str(conv.id),
            message_id=str(row.message_id),
        )

    return ConversationFilesResponse(
        files=[
            ConversationFileItem(
                id=str(r.id),
                filename=r.filename,
                content_type=r.content_type or "application/octet-stream",
                size=r.size or 0,
                created_at=r.created_at.isoformat(),
                origin=_conversation_file_origin(r.source),
                source=r.source or "",
                message_id=str(r.message_id) if r.message_id else None,
                download_url=_download_url(r),
                deleted_at=r.deleted_at.isoformat() if getattr(r, "deleted_at", None) else None,
                deleted_reason=getattr(r, "deleted_reason", None),
            )
            for r in page
        ],
        total=total,
        via_break_glass=via_break_glass,
    )


# ---------------------------------------------------------------------------
# Conversation sharing management
#
# Every endpoint below is **owner-only**. Managing who can read a conversation
# is not itself a shareable capability: a recipient who could re-share would
# make the owner's revocation meaningless, and there is no re-share tier in
# the permission model to express it.
#
# Admins are not admitted either, for the same reason they get no ambient read
# (see the sharing helpers above) — an admin who needs to see the transcript
# goes through the audited break-glass path, and an admin editing someone's
# share list is a support action nobody has asked for.
# ---------------------------------------------------------------------------


async def _resolve_owned_conversation(
    conversation_id: str,
    user: User,
    db: AsyncSession,
) -> Conversation:
    """Load a conversation the requester owns, or ``404`` (:func:`owned_conversation`)."""
    return await owned_conversation(db, user, conversation_id)


async def _serialize_shares(
    db: AsyncSession, conv: Conversation
) -> list[ConversationShareItem]:
    """Load a conversation's grants with their principals' display names.

    Two batched lookups rather than one join per row: the share list is small
    but the owner reopens the dialog often, and the names come from two
    different tables.
    """
    from app.models.conversation_share import ConversationShare
    from app.models.group import Group

    result = await db.execute(
        select(ConversationShare)
        .where(ConversationShare.conversation_id == conv.id)
        .order_by(ConversationShare.created_at.asc())
    )
    shares = list(result.scalars().all())
    if not shares:
        return []

    user_ids = {s.shared_with_user_id for s in shares if s.shared_with_user_id}
    group_ids = {s.shared_with_group_id for s in shares if s.shared_with_group_id}

    user_rows: dict[uuid.UUID, tuple[str, str]] = {}
    if user_ids:
        res = await db.execute(
            select(User.id, User.full_name, User.email).where(User.id.in_(user_ids))
        )
        user_rows = {r.id: (r.full_name, r.email) for r in res}

    group_names: dict[uuid.UUID, str] = {}
    if group_ids:
        res = await db.execute(
            select(Group.id, Group.name).where(Group.id.in_(group_ids))
        )
        group_names = {r.id: r.name for r in res}

    items: list[ConversationShareItem] = []
    for s in shares:
        if s.shared_with_user_id is not None:
            name, email = user_rows.get(
                s.shared_with_user_id, ("Unknown user", "")
            )
            items.append(
                ConversationShareItem(
                    id=str(s.id),
                    principal_type="user",
                    principal_id=str(s.shared_with_user_id),
                    principal_name=name,
                    principal_email=email or None,
                    permission=s.permission,
                    created_at=s.created_at.isoformat(),
                )
            )
        else:
            items.append(
                ConversationShareItem(
                    id=str(s.id),
                    principal_type="group",
                    principal_id=str(s.shared_with_group_id),
                    principal_name=group_names.get(
                        s.shared_with_group_id, "Unknown group"
                    ),
                    principal_email=None,
                    permission=s.permission,
                    created_at=s.created_at.isoformat(),
                )
            )
    return items


@router.get("/share-targets", response_model=ConversationShareTargetsResponse)
async def search_share_targets(
    q: str = Query(
        ...,
        min_length=SHARE_TARGET_MIN_QUERY_LENGTH,
        max_length=200,
        description="Name / email fragment to match, case-insensitive.",
    ),
    limit: int = Query(default=10, ge=1, le=SHARE_TARGET_MAX_RESULTS),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Search users and groups the caller can share a conversation with.

    This is the platform's only non-admin-readable people directory, so it is
    kept deliberately narrow:

    * a **minimum query length** — it answers "who is this person I already
      know about", not "enumerate the org". A blank or single-character query
      is rejected by the validator rather than returning the first page of
      every account.
    * a **hard result cap**, so the endpoint cannot be paged through into a
      full dump.
    * **only the fields the picker renders** — id, display name, email. No
      role, no activity, no resource counts (contrast ``GET /users``, which
      returns all of that and stays admin-only).
    * **active users only**, and never the caller: sharing with a deactivated
      account grants nothing, and sharing with yourself is a no-op that would
      just clutter the list.

    Groups are matched by name. Group *membership* is not exposed — the picker
    shows the group, not who is in it, so this cannot be used to map the org
    chart either.
    """
    from app.models.group import Group

    # ``ilike`` with a leading wildcard cannot use a btree index, but the
    # result cap bounds the scan and the user/group tables are small relative
    # to the conversation tables this module usually touches.
    pattern = f"%{q.strip()}%"

    user_result = await db.execute(
        select(User.id, User.full_name, User.email)
        .where(
            User.is_active.is_(True),
            User.id != user.id,
            or_(User.full_name.ilike(pattern), User.email.ilike(pattern)),
        )
        .order_by(User.full_name.asc())
        .limit(limit)
    )
    group_result = await db.execute(
        select(Group.id, Group.name)
        .where(Group.name.ilike(pattern))
        .order_by(Group.name.asc())
        .limit(limit)
    )

    return ConversationShareTargetsResponse(
        users=[
            ConversationShareTarget(
                type="user",
                id=str(r.id),
                name=r.full_name,
                email=r.email,
            )
            for r in user_result
        ],
        groups=[
            ConversationShareTarget(type="group", id=str(r.id), name=r.name)
            for r in group_result
        ],
    )


@router.get(
    "/conversations/{conversation_id}/shares",
    response_model=ConversationSharesResponse,
)
async def list_conversation_shares(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List who this conversation is shared with, and its link tier."""
    conv = await _resolve_owned_conversation(conversation_id, user, db)
    return ConversationSharesResponse(
        shares=await _serialize_shares(db, conv),
        link_access=conv.link_access,
    )


@router.post(
    "/conversations/{conversation_id}/shares",
    response_model=ConversationSharesResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation_share(
    conversation_id: str,
    body: CreateConversationShareRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Grant read access to one user or one group.

    Idempotent: re-sharing with a principal that already has a grant returns
    the current list unchanged rather than 409-ing. The owner's intent
    ("this person should have access") is already satisfied, and the partial
    unique indexes would reject the duplicate row anyway — surfacing that as
    an error would make the dialog fail on a double-click.

    The principal must exist and, for users, be active. A grant to a
    deactivated account grants nothing today but would silently come back to
    life if the account were reactivated, which is not something the owner
    consented to at share time.
    """
    from app.models.conversation_share import (
        SHARE_PERMISSION_READ,
        ConversationShare,
    )
    from app.models.group import Group
    from app.services.audit import log_audit

    conv = await _resolve_owned_conversation(conversation_id, user, db)

    principal_name: str
    if body.user_id is not None:
        target_uuid = parse_uuid(body.user_id, label="user ID")
        if target_uuid == user.id:
            raise HTTPException(
                status_code=400,
                detail="You already own this conversation.",
            )
        res = await db.execute(
            select(User.full_name).where(
                User.id == target_uuid, User.is_active.is_(True)
            )
        )
        found = res.scalar_one_or_none()
        if found is None:
            raise HTTPException(status_code=404, detail="User not found")
        principal_name = found
        existing_clause = ConversationShare.shared_with_user_id == target_uuid
    else:
        target_uuid = parse_uuid(body.group_id or "", label="group ID")
        res = await db.execute(select(Group.name).where(Group.id == target_uuid))
        found = res.scalar_one_or_none()
        if found is None:
            raise HTTPException(status_code=404, detail="Group not found")
        principal_name = found
        existing_clause = ConversationShare.shared_with_group_id == target_uuid

    existing = await db.execute(
        select(ConversationShare.id).where(
            ConversationShare.conversation_id == conv.id,
            existing_clause,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return ConversationSharesResponse(
            shares=await _serialize_shares(db, conv),
            link_access=conv.link_access,
        )

    db.add(
        ConversationShare(
            conversation_id=conv.id,
            shared_with_user_id=target_uuid if body.user_id is not None else None,
            shared_with_group_id=target_uuid if body.user_id is None else None,
            permission=SHARE_PERMISSION_READ,
            created_by=user.id,
        )
    )

    principal_kind = "user" if body.user_id is not None else "group"
    await log_audit(
        db,
        user=user,
        action="share",
        entity_type="conversation",
        entity_id=conv.id,
        entity_name=conv.title,
        summary=(
            f"'{user.email}' shared conversation '{conv.title}' with "
            f"{principal_kind} '{principal_name}'"
        ),
        details={
            "conversation_id": str(conv.id),
            "principal_type": principal_kind,
            "principal_id": str(target_uuid),
            "principal_name": principal_name,
            "permission": SHARE_PERMISSION_READ,
        },
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return ConversationSharesResponse(
        shares=await _serialize_shares(db, conv),
        link_access=conv.link_access,
    )


@router.delete(
    "/conversations/{conversation_id}/shares/{share_id}",
    response_model=ConversationSharesResponse,
)
async def delete_conversation_share(
    conversation_id: str,
    share_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke one grant. Takes effect on the recipient's next request."""
    from app.models.conversation_share import ConversationShare
    from app.services.audit import log_audit

    conv = await _resolve_owned_conversation(conversation_id, user, db)
    share_uuid = parse_path_id(share_id, not_found_detail="Share not found")

    result = await db.execute(
        select(ConversationShare).where(
            ConversationShare.id == share_uuid,
            # Scope to the conversation from the path: without this, knowing
            # any share id would let an owner delete a grant on a conversation
            # they do not own.
            ConversationShare.conversation_id == conv.id,
        )
    )
    share = result.scalar_one_or_none()
    if share is None:
        raise HTTPException(status_code=404, detail="Share not found")

    principal_kind = "user" if share.shared_with_user_id else "group"
    principal_id = share.shared_with_user_id or share.shared_with_group_id
    await db.delete(share)
    await log_audit(
        db,
        user=user,
        action="unshare",
        entity_type="conversation",
        entity_id=conv.id,
        entity_name=conv.title,
        summary=(
            f"'{user.email}' revoked {principal_kind} access to conversation "
            f"'{conv.title}'"
        ),
        details={
            "conversation_id": str(conv.id),
            "principal_type": principal_kind,
            "principal_id": str(principal_id) if principal_id else None,
        },
        request=request,
    )
    # Commit before answering: the revoke must hold on the recipient's next
    # request, and the get_db teardown only commits after the response is sent.
    await db.commit()

    return ConversationSharesResponse(
        shares=await _serialize_shares(db, conv),
        link_access=conv.link_access,
    )


@router.put(
    "/conversations/{conversation_id}/link-access",
    response_model=ConversationSharesResponse,
)
async def set_conversation_link_access(
    conversation_id: str,
    body: UpdateLinkAccessRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Set how far the conversation URL reaches on its own.

    ``org_read`` is the tier that makes the link pasteable into a Teams thread
    or a ticket without enumerating recipients up front — the reader still
    authenticates and is still named in the audit trail. Switching back to
    ``private`` is immediate: there is no token to leak or expire, so
    revocation is a single UPDATE.

    Audited on **change only** — re-asserting the current value is a no-op, and
    logging it would bury real access-posture changes in noise.
    """
    from app.services.audit import log_audit

    conv = await _resolve_owned_conversation(conversation_id, user, db)
    previous = conv.link_access
    if previous == body.link_access:
        return ConversationSharesResponse(
            shares=await _serialize_shares(db, conv),
            link_access=conv.link_access,
        )

    conv.link_access = body.link_access
    widening = body.link_access == LINK_ACCESS_ORG_READ
    await log_audit(
        db,
        user=user,
        action="share" if widening else "unshare",
        entity_type="conversation",
        entity_id=conv.id,
        entity_name=conv.title,
        summary=(
            f"'{user.email}' "
            + (
                "opened conversation "
                f"'{conv.title}' to any authenticated platform user with the link"
                if widening
                else f"restored conversation '{conv.title}' to private"
            )
        ),
        details={
            "conversation_id": str(conv.id),
            "link_access": body.link_access,
            "previous_link_access": previous,
        },
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return ConversationSharesResponse(
        shares=await _serialize_shares(db, conv),
        link_access=conv.link_access,
    )


async def _rest_fold_response(
    conversation_id: str,
    *,
    folded: bool,
    user_message: MessageResponse,
    accepted_token: str | None = None,
) -> JSONResponse:
    """The ``202`` a REST send gets when another turn holds the conversation.

    ``folded_into_active_turn``: the running turn is guaranteed to consume
    the message (mid-run injection or its exit-drain follow-up pass).
    ``queued_for_next_turn``: the pathological hand-off alternation hit the
    requeue fallback - a turn already running picks the persisted message up
    live, otherwise it shapes the next turn (see ``acquire_or_fold``). Either
    way ``turn_id`` names the turn whose frames answer it, as the
    web chat's ``folded_into_active_turn`` frame does (``fold_turn_id``).
    """
    content: dict = {
        "status": "folded_into_active_turn" if folded else "queued_for_next_turn",
        "conversation_id": conversation_id,
        "user_message": jsonable_encoder(user_message),
    }
    turn_id = await chat_turn_stream.fold_turn_id(conversation_id, accepted_token, folded=folded)
    if turn_id:
        content["turn_id"] = turn_id
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=content)


#: How long a turn the graceful shutdown cut waits for its reply to be stored
#: before it releases the conversation's lock; past it the lifespan drain
#: still waits for the write.
INTERRUPTED_REPLY_WAIT_SECONDS = 10.0


async def _store_interrupted_reply(conversation_id: uuid.UUID, content: str, meta: dict) -> None:
    from app.core.database import async_session

    async with async_session() as db:
        await store_message(
            db,
            conversation_id,
            role="assistant",
            content=content,
            channel="web",
            metadata_json=json.dumps(meta),
        )
        await db.commit()


async def _store_cut_turn(
    conversation_id: uuid.UUID,
    *,
    content: str,
    meta: dict,
    checkpoint: turn_checkpoint.TurnCheckpoint,
    user_id: object = None,
    user_message_id: object = None,
    turn_id: str | None = None,
    entry: str = "web",
    lock_token: str | None = None,
    trace_link: object = None,
) -> None:
    """Store what a turn the graceful shutdown cut did, before the process exits.

    A turn cut by a platform stop (a rollout, a scale-in, the worker recycle)
    is also queued for its automatic resume by the next process
    (:mod:`app.services.cut_turn_resume`), while it still holds the
    conversation; its reply then says so (``AUTO_RESUME_META_KEY``)
    instead of asking for the one click.

    The reply (``meta`` carries the tool trace; :data:`INTERRUPTED_META_KEY`
    and :data:`INTERRUPTED_CAUSE_META_KEY` are added) and the checkpoint's
    latest snapshot, which the next turn resumes, are written by shutdown
    finalizers on sessions of their own. The reply is awaited, bounded, so it
    is readable before the turn's lock goes and its viewers, told the turn was
    interrupted, read the conversation back.
    """
    cause = cut_cause()
    logger.warning(
        "Chat turn of conversation %s cut by the graceful shutdown timeout "
        "(worker stop cause: %s); storing its partial reply",
        conversation_id,
        cause or "unknown",
    )
    queued = False
    if user_id is not None and user_message_id is not None:
        from app.services.in_process_runs import chat_parent_run_for

        queued = await enqueue_cut_turn(
            conversation_id,
            user_id=user_id,
            user_message_id=user_message_id,
            turn_id=turn_id,
            # Only the drain of a stopping server cuts a turn here: a platform
            # stop even when the stop hook recorded no cause.
            cause=drain_cut_cause(),
            entry=entry,
            lock_token=lock_token,
            # The turn's chat-handover parent run, kept for the resumed turn
            # to adopt instead of failed by the shutdown sweep.
            parent_run_id=chat_parent_run_for(conversation_id),
            # The turn's own trace: its row may not be flushed yet when the
            # next process resumes it, so the resume could not query it
            # and linked nothing (``resumed_from.trace_id`` null).
            trace_id=getattr(trace_link, "trace_id", None),
        )
    reply = start_shutdown_finalizer(
        _store_interrupted_reply(
            conversation_id,
            content,
            {
                **meta,
                INTERRUPTED_META_KEY: True,
                INTERRUPTED_CAUSE_META_KEY: cause,
                # Its completed tool work is kept for the next turn:
                # "Continue where you left off" resumes it.
                INTERRUPTED_STATE_META_KEY: INTERRUPTED_RESUMABLE if checkpoint.holds_work else "interrupted",
                # Resumed by the next process, no click.
                **({AUTO_RESUME_META_KEY: True} if queued else {}),
            },
        ),
        name=f"interrupted-reply:{conversation_id}",
    )
    start_shutdown_finalizer(
        checkpoint.aclose(flush=True), name=f"turn-checkpoint:{conversation_id}"
    )
    await wait_for_shutdown_finalizer(reply, INTERRUPTED_REPLY_WAIT_SECONDS)


@router.post(
    "/conversations/{conversation_id}/messages", response_model=SendMessageResponse
)
@known_secrets_scoped("conversation_id")
async def send_message(
    conversation_id: str,
    body: SendMessageRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _chat_gate: None = Depends(require_chat_enabled),
):
    # Per-user (not per-IP) throttle — shares the bucket with the web chat's send.
    await enforce_rate_limit(
        None,
        "chat-send",
        limit=CHAT_MESSAGES_PER_MINUTE,
        window_seconds=60,
        bucket=str(user.id),
    )

    result = await db.execute(
        select(Conversation).where(
            Conversation.id == parse_conversation_id(conversation_id),
            Conversation.user_id == user.id,
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        # A decision aimed at somebody else's conversation is the case FR-008a
        # names, and it must not be silently ignored (SC-017). The scoped SELECT
        # above already refuses it — this only records that it happened, from an
        # unscoped existence check that reads no content and changes no
        # response. Recorded before raising, so an attempt is in the trail even
        # though the caller learns nothing beyond 404.
        if body.decisions:
            from app.services import chat_approval_resume as _refuse

            _exists = await db.execute(
                select(Conversation.id).where(
                    Conversation.id == parse_path_id(conversation_id, not_found_detail="Conversation not found")
                )
            )
            if _exists.scalar_one_or_none() is not None:
                await _refuse.record_refused_decision(
                    db,
                    user_id=str(user.id),
                    conversation_id=conversation_id,
                    reason="not_the_paused_user",
                    surface="rest",
                    summary=(
                        "Refused a tool approval decision for a conversation "
                        "belonging to another user"
                    ),
                )
        raise HTTPException(status_code=404, detail="Conversation not found")

    # A group channel thread is continued on its channel only, and a turn a
    # channel message runs is never folded into: refused before
    # anything is stored.
    _continuation_refusal = await web_send_refusal(conv)
    if _continuation_refusal:
        raise HTTPException(status_code=409, detail=_continuation_refusal)

    # A selection that ADDS a tool, knowledge base or integration the caller
    # cannot use is refused before anything is stored.
    _selection_refusal = await refused_chat_selection(
        db,
        user_id=user.id,
        conversation=conv,
        requested={
            "mcp_server_ids": body.mcp_server_ids,
            "custom_tool_ids": body.custom_tool_ids,
            "knowledge_base_ids": body.knowledge_base_ids,
            "integration_ids": body.integration_ids,
        },
    )
    if _selection_refusal:
        raise HTTPException(status_code=400, detail=_selection_refusal)

    # ── Approvals on this conversation (FR-002c1/c2/c4) ─────────────
    #
    # Two cases, and they are mutually exclusive. With ``decisions`` this
    # request resumes the turn that ended at a gated call: the approved calls
    # run with the arguments the reviewer was shown and the conversation carries
    # on. Without them, a new message means the conversation has moved on, so
    # anything still pending is retired (FR-002c4) — never on a clock, only
    # because the person asked for something else instead.
    #
    # FR-008a needs no check of its own here: the SELECT above is scoped to
    # ``Conversation.user_id == user.id``, so the only caller who can reach a
    # pending proposal is the user whose call is paused. The surfaces where that
    # is *not* automatic are the shared channels.
    from app.services import chat_approval_resume as _appr

    _resume_proposals: list[dict] | None = None
    _resume_decisions: dict[str, dict] | None = None
    _resume_message = None
    if body.decisions:
        _pending = await _appr.load_pending(db, conv.id)
        if _pending is None:
            # Recorded as well as refused (FR-002c4, FR-020): a decision for a
            # set that was answered or retired is a real attempt to run a gated
            # call, and a bare 409 leaves nothing behind saying so.
            await _appr.record_refused_decision(
                db,
                user_id=str(user.id),
                conversation_id=str(conv.id),
                reason="no_pending_proposal",
                surface="rest",
                summary=(
                    "Refused a tool approval decision: no pending proposal "
                    "(already answered, or superseded by a later message)"
                ),
            )
            # 409 rather than 404: the conversation exists, the proposal does
            # not any more. The client should refresh — somebody answered it,
            # the turn finished, or a later message retired it. Silently
            # accepting would tell the user they approved something that never
            # ran.
            raise HTTPException(
                status_code=409,
                detail=(
                    "No pending tool approval on this conversation. It was "
                    "already answered, or a later message superseded it."
                ),
            )
        _resume_message, _resume_proposals = _pending
        # Only the id survives the turn. ``build_approval_gate`` can roll the
        # session back mid-turn (a failed platform read does, and the error is
        # swallowed), which expires every instance — reading this one afterwards
        # would raise lazy IO outside a greenlet and 500 the request *after* the
        # approved tool had already run, leaving the set unanswered so a retry
        # ran it twice. Same reason ``conv_id`` is captured into a local below.
        _resume_message_id = _resume_message.id
        _resume_decisions = _appr.normalize_decisions(body.decisions)
        try:
            _appr.require_complete(_resume_proposals, _resume_decisions)
        except _appr.PartialDecisionSetError as _partial:
            await _appr.record_refused_decision(
                db,
                user_id=str(user.id),
                conversation_id=str(conv.id),
                reason="partial_decision_set",
                surface="rest",
                summary="Refused a partial tool approval decision set",
                extra={"missing": _partial.missing},
            )
            raise HTTPException(status_code=422, detail=str(_partial)) from None
    # Presentation Studio: resolve the bound deck for THIS turn against
    # the caller's current visibility. ``None`` when unbound or no longer
    # readable — the turn then runs as a plain chat.
    _studio = await load_studio_context(
        db,
        user_id=user.id,
        presentation_id=conv.presentation_id,
        focused_index=body.studio_slide,
    )
    # Video Studio: the video this conversation edits, the same way.
    if _studio is None and conv.video_project_id is not None:
        from app.services.video_studio.studio import load_video_studio_context

        _studio = await load_video_studio_context(
            db,
            user_id=user.id,
            video_project_id=conv.video_project_id,
            focused_scene=body.studio_scene,
        )
    # Dashboard Studio: the dashboard this conversation builds, the same way.
    if _studio is None and conv.custom_dashboard_id is not None:
        from app.services.dashboard_studio import load_dashboard_studio_context

        _studio = await load_dashboard_studio_context(
            db,
            user_id=user.id,
            dashboard_id=conv.custom_dashboard_id,
            focused_widget=body.studio_widget,
        )
    # Fleet page Modify with AI: the fleet this conversation changes, the same way.
    if _studio is None and conv.fleet_id is not None:
        from app.services.fleet_studio import load_fleet_studio_context

        _studio = await load_fleet_studio_context(db, user_id=user.id, fleet_id=conv.fleet_id)
    # XTM One Desktop: the machine this message was typed on, when the
    # caller may target it. ``None`` on the web and for an invisible runner —
    # the turn then runs as a plain chat.
    _desktop = await load_desktop_context(
        db, user_id=user.id, runner_id=body.desktop_runner_id
    )

    # ---- Handle file attachments (file_ids from prior upload) ----
    attachment_meta_json: str | None = None
    file_refs: list = []
    resolved_file_ids: list[uuid.UUID] = []
    if body.file_ids:
        from app.llm.base import FileRef
        from app.models.file_attachment import FileAttachment

        attachments_list: list[dict] = []
        missing_file_ids: list[str] = []
        for fid in body.file_ids:
            try:
                fid_uuid = uuid.UUID(fid)
            except (ValueError, TypeError):
                logger.warning(
                    "send_message: ignoring malformed file_id %r (conversation=%s, user=%s)",
                    fid,
                    conversation_id,
                    user.id,
                )
                missing_file_ids.append(str(fid))
                continue
            fa_result = await db.execute(
                select(FileAttachment).where(
                    FileAttachment.id == fid_uuid,
                    FileAttachment.user_id == user.id,
                )
            )
            fa = fa_result.scalar_one_or_none()
            if not fa:
                logger.warning(
                    "send_message: file_id %s not found or not owned by user "
                    "(conversation=%s, user=%s) — attachment will be dropped",
                    fid,
                    conversation_id,
                    user.id,
                )
                missing_file_ids.append(fid)
                continue

            attachments_list.append(
                {
                    "filename": fa.filename,
                    "type": _attachment_type_label(
                        fa.filename, fa.content_type
                    ),
                    "size": fa.size,
                    "file_id": str(fa.id),
                    "content_type": fa.content_type,
                }
            )
            file_refs.append(FileRef.from_attachment(fa))
            resolved_file_ids.append(fa.id)

        if attachments_list:
            attachment_meta_json = json.dumps({"attachments": attachments_list})

        # Abort with a clear error when the client sent file_ids but every
        # one of them failed to resolve — silently sending the message
        # without its attachments is confusing and leads to the LLM
        # hallucinating "I can't read the file".
        if body.file_ids and not file_refs:
            raise HTTPException(
                status_code=400,
                detail=(
                    "None of the provided file_ids could be resolved. "
                    "Please re-upload the files. "
                    f"Unresolved: {', '.join(missing_file_ids)}"
                ),
            )

    # A resume adds no user text: the turn it continues already has its user
    # message, and inventing an empty one would put a blank turn in the
    # transcript and hand the model an empty content block.
    # One normalized value, used everywhere below. ``body.content`` is optional
    # now (a resume carries decisions instead), and a resume adds no user text at
    # all: the turn it continues already has its user message, and inventing an
    # empty one would put a blank turn in the transcript and hand the model an
    # empty content block. The validator refuses content alongside decisions, so
    # this cannot silently discard something the client sent.
    message_content = "" if _resume_proposals else (body.content or "")

    # Conversations referenced with @: only what the sender can open,
    # recorded on the message as references, read by the agent as a brief.
    from app.services.conversation_references import (
        build_reference_brief,
        resolve_referenced_conversations,
        with_conversation_refs,
    )

    reference_resolution = await resolve_referenced_conversations(
        db, user, body.referenced_conversation_ids, current_conversation_id=conv.id
    )
    reference_brief = await build_reference_brief(db, reference_resolution)
    attachment_meta_json = with_conversation_refs(attachment_meta_json, reference_resolution)
    # Video Studio: the scene on screen stays with the request (get_video feedback reads it).
    if conv.video_project_id is not None:
        from app.services.video_studio.studio_feedback import with_studio_scene

        attachment_meta_json = with_studio_scene(attachment_meta_json, body.studio_scene)

    _rest_channel_hold: str | None = None
    if _resume_proposals:
        _existing_user = await db.execute(
            select(Message)
            .where(Message.conversation_id == conv.id, Message.role == "user")
            .order_by(Message.sequence.desc(), Message.created_at.desc())
            .limit(1)
        )
        user_msg = _existing_user.scalar_one_or_none()
        if user_msg is None:
            # A pending proposal with no user message behind it cannot happen:
            # the gate only fires inside a turn a message started. Refuse rather
            # than fabricate one, so a corrupted conversation is visible.
            raise HTTPException(
                status_code=409,
                detail="Conversation has a pending approval but no user message.",
            )
    else:
        # Channel turns wait until this send has its turn or its fold: a
        # channel turn drains nothing folded into it.
        _rest_channel_hold, _continuation_refusal = await hold_off_channel_turns(conv.id)
        if _continuation_refusal:
            raise HTTPException(status_code=409, detail=_continuation_refusal)
        # Store user message (with attachment metadata if present)
        user_msg = await store_message(
            db,
            conv.id,
            role="user",
            content=message_content,
            channel="web",
            metadata_json=attachment_meta_json,
        )

    # Link the resolved file attachments to this message
    if resolved_file_ids:
        from app.models.file_attachment import FileAttachment

        for file_uuid in resolved_file_ids:
            # ``user_msg_attachments`` (built below via ``parse_attachments``
            # with ``message_id=user_msg_id``) emits message-scoped download
            # URLs of the form ``/conversations/<id>/messages/<msg>/files/<fid>/download``.
            # If this UPDATE silently fails (the previous shape was
            # ``contextlib.suppress(Exception)``), those URLs 404 because the
            # message_id never gets linked — and the response goes out
            # claiming the file is downloadable while it's actually
            # unreachable via the scoped endpoint. Mirrors the assistant-side
            # linkage fix from round 1 (this same try / except: logger.exception
            # shape) so user-uploaded attachments and assistant-generated files
            # share one diagnostic path through audit tooling. The legacy
            # ``/api/v1/chat/files/{file_id}/download`` URL stays reachable
            # via ``_authorize_file_access`` (owner-by-``user_id`` check), so
            # the file isn't orphaned — only the message-scoped URL goes 404,
            # which is exactly the failure mode the log reports.
            try:
                await db.execute(
                    update(FileAttachment)
                    .where(
                        FileAttachment.id == file_uuid,
                        FileAttachment.user_id == user.id,
                    )
                    .values(message_id=user_msg.id)
                )
            except Exception:
                logger.exception(
                    "Failed to link user-uploaded file %s to message %s "
                    "(conversation=%s); message-scoped download URL will "
                    "404 but the legacy /chat/files/{id}/download fallback "
                    "still works via owner check.",
                    file_uuid,
                    user_msg.id,
                    conv.id,
                )

    _needs_ai_title = conv.title == "New conversation"
    if _needs_ai_title:
        conv.title = message_content[:200]

    # Commit user message + metadata before the LLM call.  The
    # store_message() UPDATE on conversations.updated_at acquires a row
    # lock; committing here releases it immediately so concurrent requests
    # on the same conversation don't block and hit the statement timeout.
    await db.commit()

    # Capture immutable ORM attributes into locals before the agentic loop.
    # A db.rollback() during tool execution (e.g. KB query_count increment)
    # expires all session-bound objects; subsequent lazy-load of conv.id
    # triggers MissingGreenlet in async context.
    conv_id = conv.id
    conv_agent_id = conv.agent_id
    # A Slack or Teams conversation the General Assistant owns is answered on
    # the web by the agent standing in for it, as on its channel.
    _rest_stand_in = await turn_stand_in(
        db,
        agent_id=conv_agent_id,
        conversation_channel=getattr(conv, "channel", None),
        user_id=user.id,
    )
    if _rest_stand_in is not None:
        conv_agent_id = _rest_stand_in.id
    user_id_str = str(user.id)
    user_msg_id = str(user_msg.id)
    user_msg_role = user_msg.role
    user_msg_content = user_msg.content
    user_msg_channel = user_msg.channel
    user_msg_sequence = user_msg.sequence
    user_msg_created_at = user_msg.created_at.isoformat()
    user_msg_attachments = parse_attachments(
        user_msg.metadata_json,
        conversation_id=str(conv_id),
        message_id=user_msg_id,
    )

    # ── Single-flight turn lock ────────────────────────────────
    # Never run a second concurrent agent loop on a conversation whose
    # turn is already in flight (typically a web chat turn surviving a
    # page reload).  The message is already persisted above — fold it
    # into the running loop as steering and tell the caller.
    from app.services import chat_turn_guard

    _user_msg_payload = MessageResponse(
        id=user_msg_id,
        role=user_msg_role,
        content=user_msg_content,
        channel=user_msg_channel,
        channel_message_id=None,
        sequence=user_msg_sequence,
        created_at=user_msg_created_at,
        attachments=user_msg_attachments,
        llm_trace_id=None,
    )

    # The user message is committed: show it on the conversation's open
    # streams now — whether this send becomes the turn or is folded.
    _rest_origin = uuid.uuid4().hex
    chat_turn_stream.publish_live(
        str(conv_id),
        {"type": "user_message", "message": jsonable_encoder(_user_msg_payload)},
        origin=_rest_origin,
    )

    # Fold payloads carry only the RESOLVED attachment ids (unresolvable
    # ids were dropped with a warning above), so the steering payload
    # matches the attachments actually stored on the persisted user
    # message and the bridge does not re-warn on known-bad ids.
    _rest_fold_file_ids = [
        str(ref.file_id)
        for ref in (file_refs or [])
        if getattr(ref, "file_id", None)
    ]
    # Files a parent wake announced but could not deliver (its entry lost the
    # hand-off race and was purged at this acquisition): registered in
    # this turn's FileContext once it exists, below.
    _rest_purged_context_ids: list[str] = []
    # Likewise the folds this acquisition purged that name agents:
    # their text is in the history this turn loads, their agents are owed.
    _rest_purged_mentions: list = []
    # The messages folded into this turn whose @mentioned agents it owes an
    # answer, in arrival order: answered after its own agents, handed
    # back by ``_rest_end_turn`` when the turn ends first.
    _rest_folded_mentions: list[FoldedMentions] = []
    # The turn a fold goes into, read by the fold itself.
    _rest_accepted_by: list[str] = []
    _rest_turn_token, _rest_folded = await chat_turn_guard.acquire_or_fold(
        str(conv_id),
        message_content,
        file_ids=_rest_fold_file_ids,
        purged_context_file_ids=_rest_purged_context_ids,
        # Folded, the message is steering for the agent already answering:
        # the other agents it @mentions ride the fold and answer it once that
        # turn's own agents have.
        mentioned_agent_ids=folded_mention_ids(body.mentioned_agent_ids, conv_agent_id),
        purged_mentions=_rest_purged_mentions,
        accepted_by=_rest_accepted_by,
    )
    # A Stop recorded from here on is this turn's.
    _rest_turn_started_at = datetime.now(UTC)
    # The credential the account check before a follow-up reads: a session's
    # verified claims, or a credential no password change revokes.
    _rest_session = getattr(user, "web_session", None)
    _rest_iat = _rest_session.claims.get("iat") if _rest_session is not None else None
    _rest_issued_at = _rest_iat if isinstance(_rest_iat, int) else None
    await release_channel_hold(conv_id, _rest_channel_hold)
    if _rest_turn_token is None and _resume_proposals:
        # A resume cannot fold. Folding hands the *text* to a running turn, and
        # a resume has none — the decisions would be dropped on the floor while
        # the client was told 202 and its approved call never ran. Refuse, so it
        # retries once the turn in flight has finished.
        raise HTTPException(
            status_code=409,
            detail=(
                "A turn is already running on this conversation. Retry the "
                "approval once it has finished."
            ),
        )

    if _rest_turn_token is None:
        # Folded into the running turn — or, in the pathological
        # alternation case (``_rest_folded`` False, logged inside the
        # guard), requeued for the next turn's bridge.  Either way the
        # message is persisted and no concurrent loop starts.
        if _rest_folded:
            logger.info(
                "REST send folded into the in-flight turn for conversation "
                "%s instead of starting a concurrent agent loop",
                conv_id,
            )
        return await _rest_fold_response(
            str(conv_id),
            folded=_rest_folded,
            user_message=_user_msg_payload,
            accepted_token=_rest_accepted_by[0] if _rest_accepted_by else None,
        )

    # The turn is ours, so the conversation really is moving on: retire anything
    # still pending (FR-002c4). Deliberately after the fold check — doing
    # it before meant a send that folded into somebody else's in-flight turn
    # wiped that turn's pending set, and the prompt vanished from a reload while
    # the paused turn sat waiting for it.
    if not _resume_proposals and await _appr.retire_for_new_turn(
        db, conv.id, surface="rest"
    ):
        await db.commit()

    # ── Turn-lock refresher (started at acquisition) ───────────
    # The pre-loop setup below (history load + compaction, which awaits a
    # full LLM summarization on compaction-cycle turns) can legitimately
    # run long — the refresher must already be ticking so the marker
    # cannot lapse before the agent loop begins and let a concurrent
    # request acquire it.  Cancelled by the pre-loop exception handler
    # and by ``_rest_end_turn`` below.
    async def _rest_lock_refresher() -> None:
        while True:
            await asyncio.sleep(chat_turn_guard.REST_REFRESH_INTERVAL_SECONDS)
            try:
                await chat_turn_guard.refresh_turn(str(conv_id), _rest_turn_token)
            except Exception:
                logger.debug(
                    "Failed to refresh REST turn lock for %s",
                    conv_id,
                    exc_info=True,
                )

    _rest_refresher_task = asyncio.create_task(_rest_lock_refresher())

    # Fan this turn's progress out to the conversation's open streams and
    # keep its resumable journal: a REST turn has no stream of its
    # own, so without this a viewer of the conversation saw a bare
    # "Thinking..." until the answer landed. ``on_event`` only — no
    # ``on_token`` — so the LLM call mode of the REST path is unchanged.
    _rest_publisher = chat_turn_stream.TurnPublisher(
        str(conv_id), _rest_turn_token, origin=_rest_origin
    )
    _rest_publisher.start()
    # The conversation agent's run: how full its context was at its last model
    # call, stored on its reply for the composer's gauge.
    _rest_context_usage = ContextUsageTracker()

    # The tool calls of every agent the turn runs: what it stores as its reply
    # when the graceful shutdown cuts it.
    _rest_progress = TurnProgress()

    async def _rest_primary_event(event: dict) -> None:
        _rest_context_usage.observe(event)
        _rest_progress.observe(event)
        await _rest_publisher.on_event(event)

    _rest_turn_ended = False
    # The folds the loop never took, queued after its last look.
    _rest_unanswered: list = []

    async def _rest_end_turn(*, follow_up: bool = False) -> None:
        """End this request's turn, once: stop the lock refresher, flush and
        close the publisher (the terminal frame goes out, the journal is
        dropped) and only THEN release the turn lock.

        The lock is held through the persistence phase - the web chat turn's
        and parent-wake ordering: the marker never disappears while no reply is
        readable, a stream attaching in between is never resumed into a turn
        whose ``done`` it missed, and a second send never acquires the lock
        with a history that omits this answer. The release-and-drain
        handshake catches folds that raced the end of the turn,
        whose senders were told the turn took them: with *follow_up* (a turn
        stored and not stopped, its account still allowed to chat:
        ``_may_follow_up``) they and the folds the loop never took are
        answered by a follow-up turn once the lock is released
        (``schedule_fold_follow_up``); on any other exit they are logged,
        persisted user messages that shape the next turn. The agents such a
        fold mentions, and those of a fold this turn recorded but did not
        ask, are handed back to the inbox: the message is in the
        thread, its mentions are not.

        A turn that never reached its own ``done`` (torn down, or failed in
        the persistence phase) is closed for its viewers with the interrupted
        terminal - a no-op once the ``done`` was offered - so they leave the
        working state now rather than at their next safety poll.
        """
        nonlocal _rest_turn_ended
        if _rest_turn_ended:
            return
        _rest_turn_ended = True
        _rest_refresher_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _rest_refresher_task
        with contextlib.suppress(Exception):
            await _rest_publisher.aclose(terminal=chat_turn_stream.interrupted_frame())
        _rest_owed_folds = [m.payload for m in _rest_folded_mentions if m.payload is not None]
        _rest_folded_mentions.clear()
        _rest_stranded = list(_rest_unanswered)
        _rest_unanswered.clear()
        with contextlib.suppress(Exception):
            from app.services.parent_wake import schedule_fold_follow_up

            _rest_stranded += await chat_turn_guard.release_turn_and_drain(
                str(conv_id), _rest_turn_token
            )
            _rest_handed_back = await chat_turn_guard.requeue_owed_mentions(
                str(conv_id),
                [
                    *_rest_owed_folds,
                    *(p for p in _rest_stranded if all(p is not o for o in _rest_owed_folds)),
                ],
            )
            if _rest_handed_back:
                logger.info(
                    "%d folded message(s) of conversation %s handed back "
                    "with the agents they mention, for the next turn",
                    _rest_handed_back,
                    conv_id,
                )
            _rest_followed = (
                follow_up
                and bool(_rest_stranded)
                and await _may_follow_up(
                    str(conv_id),
                    _rest_turn_started_at,
                    user.id,
                    _rest_issued_at,
                    session_token=_rest_session is not None,
                )
                and schedule_fold_follow_up(
                    conversation_id=str(conv_id), user_id=str(user.id), folds=_rest_stranded
                )
            )
            if _rest_stranded and not _rest_followed:
                logger.warning(
                    "%d steering fold(s) landed as the REST turn for "
                    "conversation %s finished — the messages are persisted "
                    "and will shape the next turn",
                    len(_rest_stranded),
                    conv_id,
                )

    # ── Pre-loop setup (lock held) ──────────────────────────────────────
    # From here until the agent-loop ``try`` below, this request owns the
    # conversation's turn lock but is not yet inside the ``finally`` that
    # releases it.  An exception in this section (DB error on the history
    # load, etc.) must stop the refresher and release the lock instead of
    # leaking it for a full TTL — during that window every send would fold
    # into a turn that no longer exists.
    try:
        # Load history + apply memory management for long conversations
        history = await get_conversation_history(db, conv_id)

        # The user message was already committed above, so history includes
        # it.  Strip it to avoid sending the same turn twice to the LLM (the
        # chat services append user_message explicitly with multimodal
        # metadata).
        if (
            history
            and history[-1].role == "user"
            and history[-1].content == message_content
        ):
            history = history[:-1]

        # Apply memory management — skip when agent has skip_llm enabled.
        # ``_rest_prebuilt_llm`` carries the compaction-path LLM resolution
        # forward (P3.3) so the chat service and the AI-title generation
        # don't re-resolve it.
        _rest_prebuilt_llm: tuple | None = None
        _rest_skip_llm = False
        if conv_agent_id:
            from app.models.agent import Agent as _AgentSkipCheckRest

            _sr = await db.execute(
                select(_AgentSkipCheckRest.skip_llm).where(
                    _AgentSkipCheckRest.id == conv_agent_id
                )
            )
            _sr_val = _sr.scalar_one_or_none()
            _rest_skip_llm = bool(_sr_val) if _sr_val is not None else False

        if not _rest_skip_llm:
            try:
                from app.llm.router import get_llm_for_user as _get_llm
                from app.services.conversation_memory import (
                    manage_conversation_memory,
                )

                _llm, _llm_model = await _get_llm(db, user_id_str)
                _rest_prebuilt_llm = (_llm, _llm_model)
                # Check agent flags so fact extraction respects
                # company-managed and memory_persistent_enabled settings.
                _agent_company_managed = False
                _agent_memory_persistent = True
                if conv_agent_id:
                    from app.models.agent import Agent as _AgentModel

                    _a_res = await db.execute(
                        select(
                            _AgentModel.is_company_managed,
                            _AgentModel.memory_persistent_enabled,
                        ).where(_AgentModel.id == conv_agent_id)
                    )
                    _a_row = _a_res.one_or_none()
                    if _a_row is not None:
                        _agent_company_managed = bool(_a_row[0])
                        _agent_memory_persistent = bool(_a_row[1])
                # Return the pooled connection before compaction: on
                # compaction-cycle turns this awaits a full LLM summarization
                # while the reads above hold an open transaction.
                from app.core.database import release_db_connection_before_loop

                await release_db_connection_before_loop(db)
                history = await manage_conversation_memory(
                    _llm,
                    history,
                    agent_id=str(conv_agent_id) if conv_agent_id else None,
                    user_id=user_id_str,
                    is_company_managed=_agent_company_managed,
                    memory_persistent_enabled=_agent_memory_persistent,
                    model=_llm_model,
                    conversation_id=str(conv_id),
                )
            except Exception as exc:
                logger.warning("Conversation memory management failed: %s", format_exception_message(exc), exc_info=True)

        # Same resume and checkpoint as the web chat turn.
        _rest_resumed, _rest_replay = await turn_checkpoint.load_replay(
            db, conv_id, user_id=user.id, code_interpreter=await _upload_offers_code_interpreter(db, conv, user)
        )
        if _rest_replay:
            history = [*history, *_rest_replay]
        _rest_checkpoint = turn_checkpoint.TurnCheckpoint(
            conv_id,
            turn_id=chat_turn_stream.public_turn_id(_rest_turn_token),
            resumed=_rest_resumed,
            user_id=str(user.id),
        )
        _rest_keep_checkpoint = False
        # The id of this turn's trace, named by its cut record.
        _rest_trace_link = arm_root_trace_link()

        from app.tools.base import FileContext, files_for_reply

        _rest_transfer_agent_id: str | None = None
        _rest_transfer_source_id: str | None = None
        # Optional failure envelope populated by run_agent_chat (skip_llm
        # pipeline refusal/failure). Stays None on success.
        _rest_error_out: dict[str, str] = {}
        _llm_file_context = FileContext.from_file_refs(file_refs)
        _initial_file_ids: set[str] = {
            ref.file_id
            for ref in _llm_file_context.list_files()
            if getattr(ref, "file_id", None)
        }
        # Every stored file of the conversation, whatever compaction or a
        # resumed turn kept of the messages that carried it.
        from app.services.conversation_files import (
            conversation_file_refs,
            register_conversation_files,
        )

        register_conversation_files(
            _llm_file_context, await conversation_file_refs(conv_id), _initial_file_ids
        )
        for _hist_msg in history:
            if _hist_msg.files:
                for _href in _hist_msg.files:
                    _llm_file_context.add(_href)
                    if getattr(_href, "file_id", None):
                        _initial_file_ids.add(_href.file_id)
        if _rest_purged_context_ids:
            # A purged wake's files: in the context (the wake text
            # in history says they are attached) and NOT in the initial set,
            # so they are attached to this turn's reply like a file it wrote.
            from app.services.sub_agent_files import register_context_files

            await register_context_files(
                _llm_file_context, _rest_purged_context_ids, user_id=user.id
            )
        # A purged fold naming agents: its text is in the history
        # just loaded, its agents answer it after this turn's.
        for _rest_purged in _rest_purged_mentions:
            _rest_purged_refs: list = []
            if _rest_purged.file_ids:
                with contextlib.suppress(Exception):
                    _rest_purged_refs, _ = await _resolve_steering_files(
                        db, _rest_purged.file_ids, user.id, str(conv_id)
                    )
            _rest_purged_owed = folded_mentions(_rest_purged, _rest_purged_refs)
            if _rest_purged_owed is not None:
                _rest_folded_mentions.append(_rest_purged_owed)
        # @mentions, same contract as the web chat turn: the other
        # agents this message names answer after the conversation's own, and a
        # lookup error leaves the turn exactly as it is without mentions.
        try:
            _rest_mention_targets = await resolve_mention_targets(
                db,
                user_id=user.id,
                raw_ids=body.mentioned_agent_ids,
                answering_agent_id=str(conv_agent_id) if conv_agent_id else None,
            )
        except Exception:
            logger.warning(
                "Could not resolve the mentioned agents of a message in "
                "conversation %s; only the conversation's agent answers",
                conv_id,
                exc_info=True,
            )
            _rest_mention_targets = []
        _rest_turn_history = list(history)
        _rest_primary_failed = False
        _rest_primary_target: MentionTarget | None = None
        _rest_mention_replies: list[MentionReply] = []
        # What the conversation agent's run was charged, and what each
        # reply's run wrote: its trace, the files it produced, the
        # integrations it could not use and how full its context was.
        _rest_metering = TurnMetering()
        _rest_primary_trace: dict[str, str] | None = None
        _rest_primary_file_ids: set[str] | None = None
        _rest_mention_traces: dict[str, dict[str, str]] = {}
        _rest_mention_files: dict[str, list[str]] = {}
        _rest_mention_unavailable: dict[str, list[dict[str, str]]] = {}
        _rest_mention_usage: dict[str, dict[str, Any]] = {}
    except BaseException:
        _rest_refresher_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _rest_refresher_task
        with contextlib.suppress(Exception):
            await _rest_publisher.aclose(terminal=chat_turn_stream.interrupted_frame())
        with contextlib.suppress(Exception):
            await chat_turn_guard.release_turn(str(conv_id), _rest_turn_token)
        # The purged folds' agents are still owed an answer.
        with contextlib.suppress(Exception):
            await chat_turn_guard.requeue_owed_mentions(str(conv_id), _rest_purged_mentions)
        raise

    # ── Steering bridge for the REST loop ───────────────────────
    # Bridge cross-stream steering folds into this turn's queue so a
    # message sent from the web UI while this REST turn runs is injected
    # mid-run instead of spawning a concurrent loop.  Lives exactly as
    # long as the agent loop (see the ``finally`` below), alongside the
    # lock refresher started at acquisition above.
    from app.services.parent_wake import PartnerAnswerQueue

    _rest_steering_queue = PartnerAnswerQueue(str(conv_id))
    # Filled when the turn stops at a gated tool. This endpoint does not
    # stream, so there is no way to reach the client mid-turn — it takes the
    # end-the-turn path and hands the proposals back on the message.
    _rest_approvals_out: list = []
    _rest_wait_skip_event = asyncio.Event()
    # Real cancel event for the REST loop: before this, nothing
    # could cancel a REST turn at all — the web chat's ``cancel`` only set
    # the event of a turn bound to its own stream.  The bridge below sets it
    # when a cross-process cancel request addressed to this turn arrives.
    _rest_cancel_event = asyncio.Event()
    _rest_bridge_stop = asyncio.Event()

    async def _rest_steering_bridge() -> None:
        from app.core.database import async_session as _rb_session
        from app.services.parent_wake import without_stopped_partner_answers
        from app.services.sub_agent_files import (
            register_context_files as _register_context_files,
        )
        from app.tools.agent_loop import SteeringMessage as _RBSteeringMessage

        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    _rest_bridge_stop.wait(), timeout=chat_turn_guard.BRIDGE_POLL_SECONDS
                )
            if _rest_bridge_stop.is_set():
                return
            # Cross-process cancel — Stop clicked in the web UI
            # while this REST turn runs.  Token-bound: a stale request
            # for a previous turn is discarded, never honoured.
            try:
                if await chat_turn_guard.consume_turn_cancel(
                    str(conv_id), _rest_turn_token
                ):
                    logger.info(
                        "Cross-process cancel received for the REST turn "
                        "of conversation %s",
                        conv_id,
                    )
                    _rest_cancel_event.set()
                    _rest_wait_skip_event.set()
            except Exception:
                logger.debug(
                    "Cross-process cancel check failed for %s",
                    conv_id,
                    exc_info=True,
                )
            # "Continue now" pressed on a socket while this REST turn waits.
            try:
                if await chat_turn_guard.consume_wait_skip(
                    str(conv_id), _rest_turn_token
                ):
                    _rest_wait_skip_event.set()
            except Exception:
                logger.debug(
                    "Cross-process wait skip check failed for %s",
                    conv_id,
                    exc_info=True,
                )
            try:
                _payloads = await chat_turn_guard.drain_steering_inbox(
                    str(conv_id)
                )
            except Exception:
                logger.debug(
                    "Steering inbox drain failed for %s",
                    conv_id,
                    exc_info=True,
                )
                continue
            # A partner's answer whose work the user stopped is not injected:
            # it was pushed before the Stop, and is in the history.
            _payloads = await without_stopped_partner_answers(
                str(conv_id), _payloads
            )
            for _p in _payloads:
                _p_refs: list = []
                if _p.file_ids:
                    try:
                        async with _rb_session() as _rbdb:
                            _p_refs, _ = await _resolve_steering_files(
                                _rbdb, _p.file_ids, user.id, str(conv_id)
                            )
                    except Exception:
                        logger.warning(
                            "Failed to resolve folded steering attachments "
                            "for %s",
                            conv_id,
                            exc_info=True,
                        )
                for _pref in _p_refs:
                    _llm_file_context.add(_pref)
                # A background task's files folded by a parent wake are
                # REGISTERED in this turn's context only, never put on the
                # steering message: the wake text names them and
                # the model reads one with read_file_content; inlined, nine
                # CSVs would be 720k characters of steering text.
                await _register_context_files(
                    _llm_file_context, _p.context_file_ids, user_id=user.id
                )
                # The agents it @mentions answer it once this turn's own have;
                # a fold handed back is in the thread already.
                _p_owed = folded_mentions(_p, _p_refs)
                if _p_owed is not None:
                    _rest_folded_mentions.append(_p_owed)
                if _p.mentions_only:
                    continue
                _rest_steering_queue.put_fold(
                    _RBSteeringMessage(text=_p.text, files=_p_refs)
                    if _p_refs
                    else _p.text,
                    _p,
                )
                _rest_wait_skip_event.set()

    _rest_bridge_task = asyncio.create_task(_rest_steering_bridge())
    # The integrations the turn did not use for its user, shown on the
    # reply.
    _rest_unavailable_integrations: list[dict] = []

    async def _save_rest_selection() -> None:
        # Saved once an agent or the General Assistant runs the message, as
        # the web turn does: a send answered by "no longer exists" or a
        # disabled agent, or folded into a running turn, leaves the saved
        # selection as it was.
        if persist_tool_selection(
            conv,
            builtin_tool_ids=body.builtin_tool_ids,
            custom_tool_ids=body.custom_tool_ids,
            integration_ids=body.integration_ids,
            mcp_server_ids=body.mcp_server_ids,
            knowledge_base_ids=body.knowledge_base_ids,
            default_tool_ids=body.default_tool_ids,
        ):
            await db.commit()

    # Set once the conversation agent has answered: a turn cut while the
    # agents it mentions answer keeps that answer.
    assistant_text = ""
    try:
        if conv_agent_id:
            # Full agentic chat: persona + MCP tools + knowledge bases
            from app.models.agent import Agent
            from app.services.agent_chat import run_agent_chat

            agent_result = await db.execute(
                select(Agent).where(Agent.id == conv_agent_id)
            )
            agent = agent_result.scalar_one_or_none()
            if not agent:
                assistant_text = (
                    "The agent bound to this conversation no longer exists."
                )
                _rest_primary_failed = True
            elif not agent.enabled:
                assistant_text = "This agent is currently disabled."
                _rest_primary_failed = True
            else:
                await _save_rest_selection()
                try:
                    assistant_text, transfer_agent_id = await run_agent_chat(
                        db,
                        user_id_str,
                        agent,
                        history,
                        message_content,
                        conversation_id=str(conv_id),
                        images=body.images,
                        files=file_refs or None,
                        file_context=_llm_file_context,
                        session_builtin_tool_ids=body.builtin_tool_ids,
                        session_custom_tool_ids=body.custom_tool_ids,
                        session_integration_ids=body.integration_ids,
                        session_mcp_server_ids=body.mcp_server_ids,
                        session_knowledge_base_ids=body.knowledge_base_ids,
                        scope_headers=body.scope_headers,
                        prebuilt_llm=_rest_prebuilt_llm,
                        steering_queue=_rest_steering_queue,
                        on_checkpoint=_rest_checkpoint.offer,
                        wait_skip_event=_rest_wait_skip_event,
                        cancel_event=_rest_cancel_event,
                        error_out=_rest_error_out,
                        approvals_out=_rest_approvals_out,
                        resume_proposals=_resume_proposals,
                        resume_decisions=_resume_decisions,
                        # A standing approval granted while impersonating must
                        # name both identities (FR-017b), as the web chat and the
                        # platform stream already do.
                        impersonated_by=getattr(user, "impersonated_by", None),
                        approved_call_ids=(
                            _appr.approved_ids(_resume_proposals, _resume_decisions)
                            if _resume_proposals and _resume_decisions is not None
                            else None
                        ),
                        approved_shapes=(
                            _appr.approved_shapes(_resume_proposals, _resume_decisions)
                            if _resume_proposals and _resume_decisions is not None
                            else None
                        ),
                        presentation_studio=_studio,
                        desktop=_desktop,
                        on_event=_rest_primary_event,
                        unavailable_integrations_out=_rest_unavailable_integrations,
                        reference_brief=reference_brief,
                        agents_after=_rest_mention_targets,
                        # Whether the quota charged or refused this run:
                        # a refusal asks no mentioned agent, a folded
                        # message's either.
                        quota_out=(
                            _rest_metering.quota
                            if _rest_mention_targets or _rest_folded_mentions
                            else None
                        ),
                    )
                except Exception as _rest_primary_exc:
                    # Mentioned agents still to answer, a folded
                    # message's too: the failure becomes this agent's
                    # note and they answer. Alone, the turn fails as it
                    # always has.
                    if not (_rest_mention_targets or _rest_folded_mentions):
                        raise
                    logger.error(
                        "Chat error: %s",
                        format_exception_message(_rest_primary_exc),
                        exc_info=True,
                    )
                    assistant_text = (
                        f"I encountered an error: {_friendly_error_message(_rest_primary_exc)}"
                    )
                    transfer_agent_id = None
                    _rest_primary_failed = True
                    _rest_keep_checkpoint = True

                if transfer_agent_id:
                    # The agent that ran, the one standing in included.
                    _rest_transfer_source_id = str(conv_agent_id)
                    conv.transfer_source_agent_id = conv_agent_id
                    conv.agent_id = uuid.UUID(transfer_agent_id)
                    _rest_transfer_agent_id = transfer_agent_id
        else:
            # General chat with agent handover capability
            from app.services.general_chat import run_general_chat

            await _save_rest_selection()
            try:
                assistant_text, new_agent_id = await run_general_chat(
                    db,
                    user_id_str,
                    history,
                    message_content,
                    conversation_id=str(conv_id),
                    session_builtin_tool_ids=body.builtin_tool_ids,
                    session_custom_tool_ids=body.custom_tool_ids,
                    session_integration_ids=body.integration_ids,
                    session_mcp_server_ids=body.mcp_server_ids,
                    session_knowledge_base_ids=body.knowledge_base_ids,
                    images=body.images,
                    files=file_refs or None,
                    file_context=_llm_file_context,
                    prebuilt_llm=_rest_prebuilt_llm,
                    steering_queue=_rest_steering_queue,
                    on_checkpoint=_rest_checkpoint.offer,
                    resume_proposals=_resume_proposals,
                    resume_decisions=_resume_decisions,
                    # Same as the agent branch: FR-017b applies wherever a
                    # standing approval can be granted.
                    impersonated_by=getattr(user, "impersonated_by", None),
                    wait_skip_event=_rest_wait_skip_event,
                    cancel_event=_rest_cancel_event,
                    presentation_studio=_studio,
                    desktop=_desktop,
                    on_event=_rest_primary_event,
                    unavailable_integrations_out=_rest_unavailable_integrations,
                    reference_brief=reference_brief,
                    agents_after=_rest_mention_targets,
                    quota_out=(
                        _rest_metering.quota
                        if _rest_mention_targets or _rest_folded_mentions
                        else None
                    ),
                )
            except _GeneralChatPaused:
                # A gated call unwinding the turn is not a failure to
                # fold into the mentioned agents' note: the handler below owns it.
                raise
            except Exception as _rest_primary_exc:
                # Same contract as the agent path.
                if not (_rest_mention_targets or _rest_folded_mentions):
                    raise
                logger.error(
                    "Chat error: %s",
                    format_exception_message(_rest_primary_exc),
                    exc_info=True,
                )
                assistant_text = (
                    f"I encountered an error: {_friendly_error_message(_rest_primary_exc)}"
                )
                new_agent_id = None
                _rest_primary_failed = True
                _rest_keep_checkpoint = True

            # If handover occurred, bind the conversation to the agent
            if new_agent_id:
                conv.agent_id = uuid.UUID(new_agent_id)

        # @mentioned agents: each other agent the message names
        # answers in turn, under the same lock and with the cancel bridge
        # still listening, each on an agentic action of its own. Their
        # replies are stored after the conversation agent's own, in the
        # persistence phase below. A quota refusal ends the turn. The agents
        # the messages folded into the turn mention answer them next,
        # the same way.
        # The conversation's agent once the turn is over (a transfer or a
        # handover rebinds it), else the one that answered unbound.
        _rest_answering_uuid = conv.agent_id or conv_agent_id
        _rest_answering_agent = str(_rest_answering_uuid) if _rest_answering_uuid else None
        _rest_answering_targets = [
            t for t in _rest_mention_targets if t.agent_id != _rest_answering_agent
        ]
        if _rest_cancel_event.is_set() or _rest_metering.refused:
            # A Stop or a refused charge asks none of them.
            _rest_folded_mentions.clear()
        if (
            (_rest_answering_targets or _rest_folded_mentions)
            and not _rest_cancel_event.is_set()
            and not _rest_metering.refused
        ):
            from app.services.agent_chat import run_agent_chat as _run_mentioned_agent
            from app.services.mention_dispatch import conversation_agent_target

            _rest_primary_target = await conversation_agent_target(db, _rest_answering_uuid)
            # What the conversation agent's run wrote, read before any
            # mentioned agent runs: its trace (none when it never reached the
            # quota gate) and its files.
            _rest_primary_trace = (
                await load_latest_langfuse_ids(str(conv_id)) if _rest_metering.quota else {}
            )
            _rest_primary_file_ids = {
                ref.file_id
                for ref in _llm_file_context.list_files()
                if getattr(ref, "file_id", None) and ref.file_id not in _initial_file_ids
            }
            _rest_mention_stopped = asyncio.Event()

            async def _rest_mention_event(event: dict) -> None:
                # A run the dispatch cut no longer publishes into the turn.
                if not _rest_mention_stopped.is_set():
                    _rest_progress.observe(event)
                    await _rest_publisher.on_event(event)

            async def _rest_mention_start(target: MentionTarget, index: int, total: int) -> None:
                _rest_publisher.offer(
                    {
                        "type": "status",
                        "status": "mention_answering",
                        "agent_id": target.agent_id,
                        "agent_name": target.name,
                        "agent_icon": target.icon,
                        "position": index + 1,
                        "total": total,
                    }
                )

            async def _rest_mention_reply(reply: MentionReply) -> None:
                # What the run that wrote it left behind; a note no run wrote
                # carries neither a trace nor files.
                reply.trace = _rest_mention_traces.pop(reply.target.agent_id, {})
                reply.file_ids = files_for_reply(
                    _llm_file_context,
                    _rest_mention_files.pop(reply.target.agent_id, []),
                    reply.text,
                )
                reply.unavailable_integrations = _rest_mention_unavailable.pop(
                    reply.target.agent_id, []
                )
                reply.context_usage = _rest_mention_usage.pop(reply.target.agent_id, {})

            async def _rest_run_mentioned(
                mentioned_agent,
                mention_history,
                mention_cancel,
                agents_after,
                *,
                content: str = message_content,
                files: list | None = file_refs or None,
                images: list | None = body.images,
            ) -> str:
                return await run_on_own_session(
                    mentioned_agent,
                    lambda own_db, own_agent: _rest_run_on(
                        own_db, own_agent, mention_history, mention_cancel, agents_after, content, files, images
                    ),
                )

            async def _rest_run_on(
                own_db, mentioned_agent, mention_history, mention_cancel, agents_after, content, files, images
            ) -> str:
                _agent_key = str(mentioned_agent.id)
                _files_before = {
                    ref.file_id
                    for ref in _llm_file_context.list_files()
                    if getattr(ref, "file_id", None)
                }
                # Its own agentic action, charged before it runs.
                _mention_quota: dict = {}
                _mention_unavailable: list[dict] = []
                # Its own context reading: a reply shows the run that wrote
                # it, never the conversation agent's.
                _mention_usage = ContextUsageTracker()

                async def _mention_event(event: dict) -> None:
                    _mention_usage.observe(event)
                    await _rest_mention_event(event)

                try:
                    _mention_text, _ = await _run_mentioned_agent(
                        own_db,
                        user_id_str,
                        mentioned_agent,
                        mention_history,
                        content,
                        conversation_id=str(conv_id),
                        images=images,
                        files=files,
                        file_context=_llm_file_context,
                        steering_queue=_rest_steering_queue,
                        wait_skip_event=_rest_wait_skip_event,
                        cancel_event=mention_cancel,
                        presentation_studio=_studio,
                        desktop=_desktop,
                        on_event=_mention_event,
                        quota_out=_mention_quota,
                        unavailable_integrations_out=_mention_unavailable,
                        reference_brief=reference_brief,
                        agents_after=agents_after,
                        allowed_handover_types=MENTIONED_AGENT_HANDOVER_TYPES,
                    )
                finally:
                    # The files this run wrote are shown by its own reply, and
                    # so are the integrations it could not use and the context
                    # it ran in.
                    _rest_mention_usage[_agent_key] = _mention_usage.meta()
                    _rest_mention_files[_agent_key] = [
                        ref.file_id
                        for ref in _llm_file_context.list_files()
                        if getattr(ref, "file_id", None) and ref.file_id not in _files_before
                    ]
                    _rest_mention_unavailable[_agent_key] = clean_unavailable_integrations(
                        _mention_unavailable
                    )
                if quota_refused(_mention_quota):
                    raise TurnRefusedError(_mention_text)
                _rest_mention_traces[_agent_key] = await load_latest_langfuse_ids(str(conv_id))
                return _mention_text

            # The replies the turn's message got, grown by the dispatch: what
            # a folded message's agents read next.
            _rest_turn_earlier: list[MentionReply] = (
                []
                if _rest_primary_failed or not (assistant_text or "").strip()
                else [MentionReply(_rest_primary_target, assistant_text)]
            )
            # One budget for every mentioned agent of the turn.
            _rest_mention_deadline = time.monotonic() + DISPATCH_BUDGET_SECONDS
            _rest_mention_replies = await dispatch_mentions(
                _rest_answering_targets,
                history=_rest_turn_history,
                user_message=message_content,
                earlier=_rest_turn_earlier,
                run_agent=_rest_run_mentioned,
                cancel_event=_rest_cancel_event,
                on_start=_rest_mention_start,
                on_reply=_rest_mention_reply,
                describe_error=_friendly_error_message,
                stopped=_rest_mention_stopped,
            )
            if any(r.refused for r in _rest_mention_replies):
                _rest_folded_mentions.clear()
            _rest_mention_replies += await answer_folded_mentions(
                _rest_folded_mentions,
                db=db,
                user_id=user.id,
                answering_agent_id=_rest_answering_agent,
                history=folded_mention_history(
                    _rest_turn_history, message_content, _rest_turn_earlier
                ),
                # A folded message's own text and attachments.
                run_agent_for=lambda m: functools.partial(
                    _rest_run_mentioned, content=m.text, files=m.files or None, images=None
                ),
                cancel_event=_rest_cancel_event,
                deadline=_rest_mention_deadline,
                on_start=_rest_mention_start,
                on_reply=_rest_mention_reply,
                describe_error=_friendly_error_message,
                stopped=_rest_mention_stopped,
            )
    except _GeneralChatPaused as _ga_hitl:
        # The general assistant installs a gate but no channel to answer it, so
        # a gated call unwinds the turn here. Without this it fell into
        # the generic handler below and the user read "I encountered an error"
        # for a product decision working exactly as intended — while the same
        # tool, on an agent, would have shown them a prompt.
        from app.tools.approval import no_approval_channel_message

        logger.info("General chat stopped for approval: %s", _ga_hitl.question)
        assistant_text = no_approval_channel_message(_ga_hitl)
        # And hand the proposals back, exactly as the agent branch does. The
        # prose above is the fallback a client that cannot render approvals
        # reads; without the structured set beside it a REST caller has no
        # ``tool_call_id`` to name, so the resume this endpoint documents was
        # unreachable for the general assistant alone. ``general_chat`` has
        # already stamped ``mark_paused`` and drained its bookkeeping before
        # re-raising, so nothing else is owed here.
        _rest_approvals_out.extend(getattr(_ga_hitl, "proposals", None) or [])
    except Exception as e:
        logger.error("Chat error: %s", format_exception_message(e), exc_info=True)
        assistant_text = f"I encountered an error: {_friendly_error_message(e)}"
        _rest_keep_checkpoint = True
    except asyncio.CancelledError as _rest_cancel:
        # Cut by uvicorn's bounded drain: the turn's tool work becomes
        # its reply, after the conversation agent's answer when the agents it
        # mentions were still answering, stored before the ``finally`` ends
        # the turn. Any other cancellation stores nothing, as before.
        if is_graceful_shutdown_cancellation(_rest_cancel):
            _cut_text = interrupted_reply(_rest_progress, cut_cause())
            if assistant_text.strip():
                _cut_text = f"{assistant_text.rstrip()}\n\n{_cut_text}"
            _cut_meta = _rest_progress.meta()
            if _rest_unavailable := clean_unavailable_integrations(_rest_unavailable_integrations):
                _cut_meta[UNAVAILABLE_INTEGRATIONS_META_KEY] = _rest_unavailable
            _cut_meta.update(_rest_context_usage.meta())
            _cut_meta.update(_rest_context_usage.command_meta())
            await _store_cut_turn(
                conv_id,
                content=_cut_text,
                meta=_cut_meta,
                checkpoint=_rest_checkpoint,
                # Resumed by the next process.
                user_id=user.id,
                user_message_id=user_msg_id,
                turn_id=_rest_checkpoint.turn_id,
                entry="rest",
                lock_token=_rest_turn_token,
                trace_link=_rest_trace_link,
            )
        raise
    finally:
        # Stop the steering bridge once the batch it holds is
        # queued: the loop is over, nothing can be injected any more.  The
        # lock refresher keeps ticking and the turn lock stays HELD through
        # the persistence phase below; both end in ``_rest_end_turn`` once
        # the reply is committed and the terminal frame published.
        await chat_turn_guard.stop_bridge(_rest_bridge_task, _rest_bridge_stop)
        # Steering items the loop never injected (folded/steered during the
        # final answer, or bridged in the instant the loop returned): their
        # senders were told the turn took them, so ``_rest_end_turn``
        # answers them in its follow-up, or logs them on a torn-down turn.
        _rest_unanswered.extend(_rest_steering_queue.take_unanswered())
        # The request is being torn down (cancellation): the persistence
        # phase will not run, so the turn ends here - journal dropped, no
        # task lingering, lock released.
        if sys.exc_info()[0] is not None:
            await _rest_end_turn()

    # Persistence phase, lock still held: the reply is stored and
    # COMMITTED, the terminal ``done`` is offered, and ``_rest_end_turn`` in
    # the ``finally`` flushes it, drops the journal and releases the lock -
    # on every exit, a DB error in ``store_message`` included, so a failed
    # request never leaks a drainer task, a journal until its TTL, or the
    # lock until its TTL.
    try:
        # Guard against empty/blank assistant response
        if not assistant_text or not assistant_text.strip():
            assistant_text = (
                "I encountered an issue while processing your request. Please try again."
            )

        # On a multi-agent turn the conversation agent's reply keeps
        # the trace and the files of its own run, read before the mentioned
        # agents ran; each of theirs is stored with its own below.
        _assistant_meta_dict = (
            dict(_rest_primary_trace)
            if _rest_primary_trace is not None
            else await load_latest_langfuse_ids(str(conv_id))
        )

        # A file the turn only fetched is shown when the answer names it.
        _new_generated_ids = set(
            files_for_reply(
                _llm_file_context,
                _rest_primary_file_ids
                if _rest_primary_file_ids is not None
                else [
                    ref.file_id
                    for ref in _llm_file_context.list_files()
                    if getattr(ref, "file_id", None) and ref.file_id not in _initial_file_ids
                ],
                assistant_text,
            )
        )
        _assistant_attachment_rows: list = []
        if _new_generated_ids:
            # A file THIS turn wrote is born bound to this conversation and to no
            # message yet. A file that reached the turn from a background sub-agent
            # task (``get_full_task_result`` puts the child's refs into the context)
            # was born with ``conversation_id = NULL``, and may already be
            # SHOWN by a message - the parent-wake reply here, or a turn of another
            # conversation of the same user that read the same task. The shared
            # admission read keeps only rows no message shows yet that are unbound
            # or ours: re-pointing a shown row would break the chip that shows it
            # without making the one written here work.
            from app.services.sub_agent_files import load_attachment_rows

            try:
                _assistant_attachment_rows = await load_attachment_rows(
                    [
                        ref.file_id
                        for ref in _llm_file_context.list_files()
                        if getattr(ref, "file_id", None) in _new_generated_ids
                    ],
                    db=db,
                    conversation_id=conv_id,
                )
            except Exception:
                logger.exception(
                    "Failed to load FileAttachment rows for new generated files"
                )

        if _assistant_attachment_rows:
            _assistant_meta_dict["attachments"] = [
                attachment_meta_from_file_attachment(fa)
                for fa in _assistant_attachment_rows
            ]
        if _rest_mention_replies and _rest_primary_target is not None:
            # A multi-agent turn: the conversation agent's reply is
            # attributed like the mentioned agents' that follow it.
            _assistant_meta_dict.update(
                reply_metadata(_rest_primary_target, failed=_rest_primary_failed)
            )
        if _rest_unavailable := clean_unavailable_integrations(_rest_unavailable_integrations):
            _assistant_meta_dict[UNAVAILABLE_INTEGRATIONS_META_KEY] = _rest_unavailable
        _assistant_meta_dict.update(_rest_context_usage.meta())
        _assistant_meta_dict.update(_rest_context_usage.command_meta())

        # Pending tool approvals. Carried on the assistant message rather
        # than in a new column: chat has no run row to park in, the client already
        # reads message metadata, and a message is exactly the right lifetime — the
        # proposals belong to the turn that produced them.
        if _rest_approvals_out:
            _assistant_meta_dict["pending_approvals"] = _rest_approvals_out

        # The set just decided stops being answerable, whatever the turn did next —
        # including pausing again on a *different* call, whose proposals ride out on
        # the new assistant message above. Marked rather than deleted so the
        # conversation keeps the record of what was asked (FR-002c4), and so a
        # duplicate submission finds nothing pending and is refused instead of
        # running an approved call twice.
        if _resume_proposals:
            await _appr.mark_answered_by_id(db, _resume_message_id)

        _assistant_meta_json = (
            json.dumps(_assistant_meta_dict) if _assistant_meta_dict else None
        )

        # A turn that failed leaves its work for the next one; any other end
        # deletes it with the answer below.
        await _rest_checkpoint.aclose(flush=_rest_keep_checkpoint)
        assistant_msg = await store_message(
            db,
            conv_id,
            role="assistant",
            content=assistant_text,
            channel="web",
            metadata_json=_assistant_meta_json,
            llm_trace_id=safe_uuid(_assistant_meta_dict.get("llm_trace_id")),
        )
        if _assistant_attachment_rows:
            # The other half of the message-scoped download URL contract:
            # ``message_id`` (and ``conversation_id`` for a sub-agent's file, born
            # with neither). The shared bind repeats the admission predicate in
            # its UPDATE, so a row another message bound meanwhile is skipped
            # rather than re-homed, and reports what it bound; the block written
            # above is then narrowed to that, so no chip points at a 404.
            from app.services.sub_agent_files import bind_attachment_rows

            try:
                _bound_rows = await bind_attachment_rows(
                    db,
                    _assistant_attachment_rows,
                    conversation_id=conv_id,
                    message_id=assistant_msg.id,
                )
                if len(_bound_rows) != len(_assistant_attachment_rows):
                    _assistant_meta_dict["attachments"] = [
                        attachment_meta_from_file_attachment(fa) for fa in _bound_rows
                    ]
                    if not _bound_rows:
                        _assistant_meta_dict.pop("attachments", None)
                    assistant_msg.metadata_json = (
                        json.dumps(_assistant_meta_dict) if _assistant_meta_dict else None
                    )
            except Exception:
                # Failure here breaks the message-scoped download URL the
                # response is about to ship (those URLs lookup files by the
                # ``message_id`` linkage we just tried to set). Log so
                # operators can spot the orphaned rows in audit tooling,
                # but don't crash the request — the response itself is
                # already buffered and the legacy ``/files/{id}/download``
                # endpoint will still work as a fallback for anyone with
                # the right scope (it now checks ``conversation_id``
                # ownership too — see ``download_file`` above).
                logger.exception(
                    "Failed to link agent-generated FileAttachment rows to "
                    "assistant message %s; message-scoped download URLs will "
                    "404 for these files until the linkage is repaired",
                    assistant_msg.id,
                )
        # Capture assistant message attributes immediately — the title
        # generation LLM call below may leave the session in a state where
        # lazy-loading ORM attributes triggers MissingGreenlet.
        asst_msg_id = str(assistant_msg.id)
        asst_msg_role = assistant_msg.role
        asst_msg_content = assistant_msg.content
        asst_msg_channel = assistant_msg.channel
        asst_msg_sequence = assistant_msg.sequence
        asst_msg_created_at = assistant_msg.created_at.isoformat()
        asst_msg_attachments = parse_attachments(
            assistant_msg.metadata_json,
            conversation_id=str(conv_id),
            message_id=asst_msg_id,
        )

        # Commit the reply BEFORE the turn ends: the marker is what
        # tells every other view of the conversation whether to expect an
        # answer, so it must outlive the moment the answer became readable.
        # ``expire_on_commit=False`` keeps the attributes captured above
        # usable; the commit-release on the AI-title path below and the
        # final commit become no-ops for these rows.
        if not _rest_keep_checkpoint and _rest_checkpoint.may_hold_row:
            await turn_checkpoint.discard(db, conv_id)
        await db.commit()

        # The mentioned agents' replies, stored after the
        # conversation agent's, in the order they answered.
        _rest_mention_responses: list[MessageResponse] = []
        _rest_mention_frame_attachments: list[list[dict] | None] = []
        if _rest_mention_replies:
            from app.services.sub_agent_files import (
                bind_attachment_rows as _bind_reply_rows,
            )
            from app.services.sub_agent_files import (
                load_attachment_rows as _load_reply_rows,
            )

            for _reply in _rest_mention_replies:
                _reply_meta = {**_reply.trace, **_reply.metadata(), **_reply.context_usage}
                _reply_rows: list = []
                _reply_file_ids = _reply.file_ids
                if _reply_file_ids:
                    try:
                        _reply_rows = await _load_reply_rows(
                            _reply_file_ids, db=db, conversation_id=conv_id
                        )
                    except Exception:
                        logger.exception(
                            "Failed to load the files of a mentioned agent's reply"
                        )
                if _reply_rows:
                    _reply_meta["attachments"] = [
                        attachment_meta_from_file_attachment(fa) for fa in _reply_rows
                    ]
                _reply_msg = await store_message(
                    db,
                    conv_id,
                    role="assistant",
                    content=_reply.text,
                    channel="web",
                    metadata_json=json.dumps(_reply_meta) if _reply_meta else None,
                    llm_trace_id=safe_uuid(_reply.trace.get("llm_trace_id")),
                )
                if _reply_rows:
                    try:
                        _bound_rows = await _bind_reply_rows(
                            db,
                            _reply_rows,
                            conversation_id=conv_id,
                            message_id=_reply_msg.id,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to link the files of a mentioned agent's reply %s",
                            _reply_msg.id,
                        )
                        _bound_rows = _reply_rows
                    if len(_bound_rows) != len(_reply_rows):
                        if _bound_rows:
                            _reply_meta["attachments"] = [
                                attachment_meta_from_file_attachment(fa)
                                for fa in _bound_rows
                            ]
                        else:
                            _reply_meta.pop("attachments", None)
                        _reply_msg.metadata_json = json.dumps(_reply_meta)
                _rest_mention_responses.append(_build_message_response(_reply_msg))
                _rest_mention_frame_attachments.append(
                    parse_attachments(
                        _reply_msg.metadata_json,
                        conversation_id=str(conv_id),
                        message_id=str(_reply_msg.id),
                    )
                )
            await db.commit()

        # The answer is durable: close the turn on the conversation's open
        # streams with the same ``done`` frame a web chat turn ends with, so a
        # viewer commits the message instead of waiting for a refetch.
        # Published by ``_rest_end_turn`` in the ``finally`` below, BEFORE the
        # lock is released - so no viewer sees the marker gone while its turn
        # is still "in flight", and no stream attaching in between is resumed
        # into a finished turn.
        _rest_done_payload: dict = {
            "type": "done",
            "content": asst_msg_content,
            "message_id": asst_msg_id,
            "conversation_id": str(conv_id),
        }
        if _rest_transfer_agent_id:
            _rest_done_payload["transfer_agent_id"] = _rest_transfer_agent_id
            _rest_done_payload["transfer_source_agent_id"] = _rest_transfer_source_id
        if asst_msg_attachments:
            _rest_done_payload["attachments"] = asst_msg_attachments
        if _rest_unavailable := clean_unavailable_integrations(_rest_unavailable_integrations):
            _rest_done_payload[UNAVAILABLE_INTEGRATIONS_META_KEY] = _rest_unavailable
        _rest_done_payload.update(_rest_context_usage.meta())
        if _rest_mention_responses:
            # Every reply but the last is an intermediate segment for a viewer.
            _rest_done_payload["intermediate"] = True
            if _rest_primary_target is not None:
                _rest_done_payload.update(
                    reply_metadata(_rest_primary_target, failed=_rest_primary_failed)
                )
            _rest_publisher.offer(_rest_done_payload)
            for _index, (_reply, _reply_response, _reply_attachments) in enumerate(
                zip(
                    _rest_mention_replies,
                    _rest_mention_responses,
                    _rest_mention_frame_attachments,
                    strict=True,
                )
            ):
                _reply_frame: dict = {
                    "type": "done",
                    "content": _reply_response.content,
                    "message_id": _reply_response.id,
                    "conversation_id": str(conv_id),
                    **_reply.metadata(),
                    **_reply.context_usage,
                }
                if _reply_attachments:
                    _reply_frame["attachments"] = _reply_attachments
                if _index < len(_rest_mention_responses) - 1:
                    _reply_frame["intermediate"] = True
                _rest_publisher.offer(_reply_frame)
        else:
            _rest_publisher.offer(_rest_done_payload)
    finally:
        # Stored and not stopped: what was folded in too late is followed up.
        await _rest_end_turn(
            follow_up=sys.exc_info()[0] is None and not _rest_cancel_event.is_set()
        )

    # Generate AI-powered conversation title on the first exchange
    if _needs_ai_title:
        try:
            from app.core.database import release_db_connection_before_loop
            from app.llm.router import get_llm_for_user as _get_llm_title
            from app.services.conversation_memory import generate_conversation_title

            if _rest_prebuilt_llm is not None:
                # Reuse the compaction-path resolution (P3.3) — identical
                # plain user call.
                _llm_title, _title_model = _rest_prebuilt_llm
            else:
                _llm_title, _title_model = await _get_llm_title(db, user_id_str)
            # Commit-release before the title LLM call (~1-3s): the message
            # persistence above holds an open transaction that would pin the
            # pooled connection for the whole generation.
            # ``conv`` stays usable (expire_on_commit=False); the title write
            # below lands with the get_db teardown commit.
            await release_db_connection_before_loop(db)
            _ai_title = await generate_conversation_title(
                _llm_title, message_content, assistant_text, model=_title_model
            )
            if _ai_title:
                conv.title = _ai_title
        except Exception as _title_exc:
            logger.warning("AI title generation failed: %s", format_exception_message(_title_exc))

    # Commit HERE, not on ``get_db`` teardown. The teardown commit runs after
    # the response is sent, so a client that reads the conversation - or the
    # delegation rows the turn recorded on this same session
    # (``a2a/client/delegation.py`` ``_record``), or the transfer fields on
    # ``conv`` - right after this response can arrive before the transaction
    # lands and see the turn missing. The A2A e2e specs do exactly that: a
    # conversation read 14 ms after a 200 that carried the reply came back
    # with the user message alone. Same rule as ``update_agent``;
    # ``expire_on_commit=False`` keeps the attributes read below usable, and
    # on the AI-title path above this is a no-op after the commit-release.
    await db.commit()

    return SendMessageResponse(
        user_message=MessageResponse(
            id=user_msg_id,
            role=user_msg_role,
            content=user_msg_content,
            channel=user_msg_channel,
            channel_message_id=None,
            sequence=user_msg_sequence,
            created_at=user_msg_created_at,
            attachments=user_msg_attachments,
            conversation_refs=reference_resolution.meta() or None,
            llm_trace_id=None,
        ),
        assistant_message=MessageResponse(
            id=asst_msg_id,
            role=asst_msg_role,
            content=redact_known_secrets_for_display(asst_msg_content),
            channel=asst_msg_channel,
            channel_message_id=None,
            sequence=asst_msg_sequence,
            created_at=asst_msg_created_at,
            attachments=asst_msg_attachments,
            llm_trace_id=str(assistant_msg.llm_trace_id) if assistant_msg.llm_trace_id else None,
            answered_by=_answered_by_payload(_assistant_meta_dict),
            mention_failed=bool(_assistant_meta_dict.get(MENTION_FAILED_KEY)) or None,
            unavailable_integrations=_unavailable_integrations(_rest_unavailable_integrations),
            **_rest_context_usage.meta(),
        ),
        mention_messages=_rest_mention_responses or None,
        conversation_id=str(conv_id),
        transfer_agent_id=_rest_transfer_agent_id,
        transfer_source_agent_id=_rest_transfer_source_id,
        error=_rest_error_out.get("error"),
        error_code=_rest_error_out.get("error_code"),
        # The arguments as the reviewer sees them; the stored copy keeps
        # the restorable markers the resume reads.
        pending_approvals=redact_known_secrets_for_display(_rest_approvals_out) or None,
    )


async def load_message_feedback(
    db: AsyncSession, msg: Message, user_id: uuid.UUID, *, lock: bool = False
) -> MessageFeedback | None:
    """*user_id*'s rating of *msg*, if they rated it (held ``FOR UPDATE`` with *lock*)."""
    query = select(MessageFeedback).where(
        MessageFeedback.message_id == msg.id,
        MessageFeedback.user_id == user_id,
    )
    if lock:
        query = query.with_for_update()
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def apply_message_feedback(
    db: AsyncSession,
    msg: Message,
    user: User,
    rating: Literal["positive", "negative"],
    comment: str | None,
    *,
    scores: Sequence[ValidatedScore] | None = None,
) -> MessageFeedback:
    """Record *user*'s rating of *msg*, then forward it to Langfuse and the trace store.

    The one write path behind the chat feedback endpoint and the ``message``
    target of the ``/api/v1/annotations`` router. *comment* arrives
    already normalized (``services.annotations.normalize_comment``). *scores*,
    already validated (``services.feedback_criteria.validate_scores``), replace
    the rating's per-criterion scores in the same transaction; ``None``
    leaves them as they are, which is what the chat bubble's thumbs do. The
    trace store gets the rater's one score on the message's trace.
    """
    # Before the rating row is read and locked: the rater's score lock always
    # comes first, and the action's time is read under it.
    action_at = await _begin_local_feedback_action(db, msg, user.id)
    # Scores are replaced delete-then-insert: two submits of the same scores
    # must queue on the rating row, not race on the scores' unique key.
    feedback = await load_message_feedback(db, msg, user.id, lock=scores is not None)
    if feedback:
        feedback.rating = rating
        feedback.comment = comment
    else:
        feedback = MessageFeedback(
            message_id=msg.id,
            user_id=user.id,
            rating=rating,
            comment=comment,
        )
        db.add(feedback)
    if scores is not None:
        await db.flush()  # a new row gets its id here
        await replace_scores(db, message_feedback_id=feedback.id, scores=scores)

    local_score = await _local_feedback_score(db, msg, user_id=user.id, action_at=action_at)
    if action_at is not None:
        await end_score_action(db, action_at)
    await db.commit()
    # Push to Langfuse after commit so feedback is persisted regardless of Langfuse availability.
    # Wrapped in try/except so that any unexpected error in get_langfuse_client or the score push
    # never surfaces as a 500 to the user — the feedback row is already durable at this point.
    try:
        await _push_langfuse_feedback_score(db, msg, rating, comment)
    except Exception:
        logger.warning("Unexpected error pushing Langfuse feedback score for message %s", msg.id, exc_info=True)
    await _record_local_feedback_score(msg, local_score)
    return feedback


async def retract_message_feedback(db: AsyncSession, msg: Message, user: User) -> bool:
    """Delete *user*'s rating of *msg*; True when there was one to delete.

    The retraction is forwarded only when a row was actually deleted: a repeated
    DELETE (a double click, a retry) or one with nothing to delete changed no
    rating, and Langfuse, whose scores are append-only, would otherwise record a
    "feedback retracted" score for a rating that no longer existed.
    """
    action_at = await _begin_local_feedback_action(db, msg, user.id)
    result = await db.execute(
        MessageFeedback.__table__.delete().where(
            MessageFeedback.message_id == msg.id,
            MessageFeedback.user_id == user.id,
        )
    )
    deleted = bool(result.rowcount)
    local_score = None
    if deleted and action_at is not None:
        local_score = await _local_feedback_score(db, msg, user_id=user.id, action_at=action_at)
        await end_score_action(db, action_at)
    await db.commit()
    if not deleted:
        return False
    # Push retraction after commit so the row deletion is durable regardless of Langfuse availability.
    # Wrapped in try/except so that any unexpected error never surfaces as a 500 to the user.
    try:
        await _retract_langfuse_feedback_score(db, msg)
    except Exception:
        logger.warning("Unexpected error retracting Langfuse feedback score for message %s", msg.id, exc_info=True)
    await _record_local_feedback_score(msg, local_score)
    return True


@router.get(
    "/conversations/{conversation_id}/messages/{message_id}/feedback",
    response_model=MessageFeedbackResponse | None,
)
async def get_message_feedback(
    conversation_id: str,
    message_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _, msg = await _get_owned_message(db, user, conversation_id, message_id)
    feedback = await load_message_feedback(db, msg, user.id)
    if not feedback:
        return None
    return MessageFeedbackResponse(rating=feedback.rating, comment=feedback.comment)


@router.post(
    "/conversations/{conversation_id}/messages/{message_id}/feedback",
    response_model=MessageFeedbackResponse,
)
async def upsert_message_feedback(
    conversation_id: str,
    message_id: str,
    body: MessageFeedbackUpsert,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _, msg = await _get_owned_message(db, user, conversation_id, message_id)
    feedback = await apply_message_feedback(
        db, msg, user, body.rating, normalize_comment(body.comment)
    )
    return MessageFeedbackResponse(rating=feedback.rating, comment=feedback.comment)


@router.delete(
    "/conversations/{conversation_id}/messages/{message_id}/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_message_feedback(
    conversation_id: str,
    message_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _, msg = await _get_owned_message(db, user, conversation_id, message_id)
    await retract_message_feedback(db, msg, user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Text-to-Speech
# ---------------------------------------------------------------------------


async def _speech_api_key(db: AsyncSession, user: User, capability: str) -> str:
    """The key of the OpenAI provider *user* may use for transcription (``transcription.openai_key``).

    A key read from a vault that cannot be read is skipped like an undecryptable one,
    but when no provider has a key that failure is raised (a 502), not "no provider".
    """
    from app.llm.transcription import TranscriptionUnavailableError, openai_key

    try:
        return await openai_key(db, user.id, capability)
    except TranscriptionUnavailableError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@router.post("/tts")
async def text_to_speech(
    body: TTSRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Speak *text* with the platform's speech model.

    The model and voice chosen in Settings > AI and models > Media > Speech generation
    (``router.get_speech_provider``), among the providers the user may use.
    Returns the audio: MP3, or WAV for a provider that speaks only WAV.
    """
    from app.llm.identity import set_llm_caller
    from app.llm.router import get_speech_provider
    from app.llm.speech import SpeechRequest

    set_llm_caller(user.id, user.email)
    try:
        target = await get_speech_provider(db, user_id=user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    try:
        result = await target.provider.synthesize_speech(
            SpeechRequest(
                model=target.model,
                text=body.text[:4096],
                voice=body.voice or target.voice,
                audio_format="wav" if body.format == "wav" else "mp3",
            )
        )
    except Exception as e:
        logger.error("TTS error: %s", format_exception_message(e), exc_info=True)
        raise HTTPException(
            status_code=502,
            detail=f"TTS generation failed: {format_exception_message(e)}",
        ) from None
    return Response(
        content=result.audio,
        media_type=result.mime,
        headers={"Content-Disposition": "inline"},
    )


# ---------------------------------------------------------------------------
# Speech-to-Text (Audio Transcription)
# ---------------------------------------------------------------------------


@router.post("/stt")
async def speech_to_text(
    audio: UploadFile = File(...),
    language: str | None = Query(
        None, description="BCP-47 language hint (e.g. 'en', 'fr')"
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Transcribe audio to text with the platform's speech-to-text (``app.llm.transcription``).

    Accepts audio files (mp3, mp4, mpeg, mpga, m4a, wav, webm, ogg).
    Requires an OpenAI LLM provider the user may use.
    Returns JSON with the transcription text.
    """
    # Validate file type
    valid_types = {
        "audio/mpeg",
        "audio/mp3",
        "audio/mp4",
        "audio/mpga",
        "audio/m4a",
        "audio/wav",
        "audio/x-wav",
        "audio/webm",
        "audio/ogg",
        "audio/flac",
    }
    content_type = audio.content_type or ""
    if content_type and content_type not in valid_types:
        # Also accept by extension
        filename = (audio.filename or "").lower()
        valid_extensions = {
            ".mp3",
            ".mp4",
            ".mpeg",
            ".mpga",
            ".m4a",
            ".wav",
            ".webm",
            ".ogg",
            ".flac",
        }
        if not any(filename.endswith(ext) for ext in valid_extensions):
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported audio format: {content_type}. Supported: mp3, mp4, wav, webm, ogg, flac.",
            )

    from app.llm.transcription import MAX_AUDIO_BYTES, Transcriber

    audio_bytes = await audio.read()
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=400, detail="Audio file too large. Maximum 25 MB."
        )

    api_key = await _speech_api_key(db, user, "speech-to-text")

    try:
        # The filename tells the API the audio's format.
        transcript = await Transcriber(api_key).transcribe(
            audio_bytes, filename=audio.filename or "audio.webm", language=language
        )
        return {"text": transcript.text}
    except Exception as e:
        logger.error("STT error: %s", format_exception_message(e), exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Transcription failed: {format_exception_message(e)}",
        ) from None


# ---------------------------------------------------------------------------
# Web chat turns (``run_chat_connection``)
# ---------------------------------------------------------------------------

# Strong references to in-flight cancel-cascade tasks — asyncio keeps only
# weak references to tasks, so without this set a cascade could be
# garbage-collected mid-run.  Entries remove themselves on completion.
_cancel_cascade_tasks: set[asyncio.Task] = set()


def _spawn_cancel_cascade(conversation_id: str, stopped_at=None) -> None:
    """Cascade a user cancel to background sub-agent tasks, off the ack path.

    Runs :func:`app.services.sub_agent.cancel_tasks_for_conversation` in a
    background task so the DB/engine work never delays the ``cancelled`` ack
    to the client.  Best-effort by contract — errors are logged inside the
    task and never propagate.

    The Stop's moment is taken here, synchronously, and handed to the
    cascade: the user can send a new message before the cascade reaches the
    database, and what that turn delegates is not the work they stopped.
    """
    from datetime import UTC, datetime

    if stopped_at is None:
        stopped_at = datetime.now(UTC)

    async def _run() -> None:
        try:
            from app.services.sub_agent import cancel_tasks_for_conversation

            await cancel_tasks_for_conversation(
                None, conversation_id, stopped_at=stopped_at
            )
        except Exception:
            logger.warning(
                "Failed to cascade cancellation to sub-agent tasks "
                "for conversation %s",
                conversation_id,
                exc_info=True,
            )

    task = asyncio.create_task(_run())
    _cancel_cascade_tasks.add(task)
    task.add_done_callback(_cancel_cascade_tasks.discard)


async def stop_conversation_work(
    conversation_id: str, request_cancel: Callable[[], Awaitable[bool]]
) -> bool:
    """Stop the conversation's turn and the background work it started: the web stream's Stop.

    *request_cancel* asks the turn, on this replica or another, and its answer
    is returned. When it raises, the exception propagates and nothing is
    stopped: the Stop's record is withdrawn and no cascade runs.
    """
    return await _stop_conversation_work(conversation_id, request_cancel)


async def _stop_conversation_work(
    conversation_id: str, request_cancel: Callable[[], Awaitable[bool]] | None = None
) -> bool | None:
    """Record the Stop in the turn guard, run *request_cancel*, then cascade it.

    One Redis write on the acknowledgement path, bounded, before the cancel
    request: a partner wake that takes the turn after that request found no
    marker reads it when it re-checks, while the cascade has not committed
    ``Conversation.stopped_at`` yet. Fail-open - a Redis problem costs only
    that window, never the acknowledgement. A *request_cancel* that raises
    withdraws the record (best-effort) and propagates before the cascade, so
    a Stop that reached nothing refuses no partner answer.
    """
    from datetime import UTC, datetime

    from app.services import chat_turn_guard

    # One moment for both records: sampled twice, work sent between the two
    # samples would be newer than one Stop and older than the other.
    stopped_at = datetime.now(UTC)
    try:
        await asyncio.wait_for(
            chat_turn_guard.record_stop(conversation_id, stopped_at),
            timeout=1.0,
        )
    except Exception:
        logger.debug(
            "Could not record the stop of conversation %s in the turn guard",
            conversation_id,
            exc_info=True,
        )
    requested = None
    if request_cancel is not None:
        try:
            requested = await request_cancel()
        except Exception:
            try:
                await asyncio.wait_for(chat_turn_guard.forget_stop(conversation_id, stopped_at), timeout=1.0)
            except Exception:
                logger.debug(
                    "Could not withdraw the stop of conversation %s from the turn guard",
                    conversation_id,
                    exc_info=True,
                )
            raise
    _spawn_cancel_cascade(conversation_id, stopped_at)
    return requested


async def _stopped_during_turn(conversation_id: str, turn_started_at: datetime) -> bool:
    """Whether the user stopped the conversation since its turn took the lock.

    Read from the Stop's record in the turn guard (``record_stop``), written
    before the turn is asked: a Stop that came between two passes or during
    the persistence reached no pass. Fail-closed, like the account check
    before a follow-up: a follow-up is new work on the account's behalf.
    """
    from app.services import chat_turn_guard

    try:
        stopped_at = await chat_turn_guard.recorded_stop(conversation_id)
    except Exception:
        logger.debug("Could not read the stop of conversation %s", conversation_id, exc_info=True)
        return True
    return stopped_at is not None and stopped_at >= turn_started_at


async def _may_follow_up(
    conversation_id: str,
    turn_started_at: datetime,
    user_id: uuid.UUID,
    issued_at: int | None,
    *,
    session_token: bool = True,
) -> bool:
    """Whether a turn that reached its own end may follow up the folds it left unanswered.

    A follow-up is new work on the account's behalf: none after a Stop
    recorded since the turn took the lock, even one no pass read
    (:func:`_stopped_during_turn`), nor once the account may no longer chat
    (:func:`resolve_account_rejection`, as for a follow-up pass). Fail-closed.
    The streamed and the REST turn ask it.
    """
    if await _stopped_during_turn(conversation_id, turn_started_at):
        return False
    from app.core.database import async_session

    try:
        async with async_session() as db:
            rejection = await resolve_account_rejection(
                db, user_id, issued_at, session_token=session_token
            )
    except Exception:
        logger.debug("Account check before a follow-up failed", exc_info=True)
        return False
    return rejection is None


async def _admit_leftover_steers(
    queue, leftover: list, steered_texts: list[str]
) -> list:
    """*leftover* as the turn's steering queue admits it.

    What the queue held back - here, or at an iteration boundary of the pass
    - leaves *steered_texts* too, whose tail *leftover* is, so a follow-up
    pass replays exactly what the loop injected. Matched by identity, as the
    queue hands back the objects put on it. A held-back text is in the
    history the next turn the user starts loads.
    """
    from app.services.parent_wake import PartnerAnswerQueue
    from app.tools.agent_loop import steering_text

    if not isinstance(queue, PartnerAnswerQueue):
        return leftover
    leftover = await queue.admit(leftover)
    for item in queue.take_held_back():
        text = steering_text(item)
        for index in range(len(steered_texts) - 1, -1, -1):
            if steered_texts[index] is text:
                del steered_texts[index]
                break
    return leftover


def _attachment_type_label(filename: str, content_type: str | None) -> str:
    """File-type label for attachment metadata (UI badge / image preview).

    Prefer the filename extension; for extensionless names derive the
    label from the content-type subtype (``image/png`` → ``png``) so a
    binary upload is not mislabelled ``txt`` — the frontend's
    ``isImageType`` matches subtype labels like ``png`` / ``jpeg``, so
    an extensionless screenshot still renders as an image preview.
    Structured-syntax suffixes are stripped (``image/svg+xml`` → ``svg``,
    not ``svg+xml``) to stay compatible with that extension-style
    matching.  ``file`` when neither source is usable (including a
    trailing-dot filename like ``"report."`` whose extension is empty).
    """
    if "." in filename:
        ext = filename.rsplit(".", 1)[-1].strip().lower()
        if ext:
            return ext
    if content_type and "/" in content_type:
        subtype = content_type.split("/", 1)[1].split(";", 1)[0].strip().lower()
        # RFC 6839 structured-syntax suffix (``svg+xml``, ``ld+json``):
        # keep only the base name so the label matches file-extension
        # checks like the frontend's ``isImageType``.
        subtype = subtype.split("+", 1)[0].strip()
        # Vendor subtypes: surface the last dotted segment
        # (``vnd.openxmlformats-officedocument.spreadsheetml.sheet`` →
        # ``sheet``) rather than the unwieldy full path — same rule as
        # ``attachment_meta_from_file_attachment``.
        if subtype.startswith("vnd."):
            subtype = subtype.rsplit(".", 1)[-1]
        if subtype and subtype != "octet-stream":
            return subtype
    return "file"


async def _resolve_steering_files(
    db,
    file_ids: list[str],
    user_id: uuid.UUID,
    conversation_id: str,
) -> tuple[list, str | None]:
    """Resolve uploaded FileAttachment ids for a steering message.

    Returns ``(file_refs, attachment_metadata_json)``.  Unknown /
    unowned / malformed ids are skipped with a warning — mirroring the
    regular send paths — so a partially valid selection still steers.
    All rows are fetched in ONE ``IN`` query (steering happens on the
    hot chat path; per-id round-trips would be an N+1).
    """
    from app.llm.base import FileRef
    from app.models.file_attachment import FileAttachment

    valid_ids: list[uuid.UUID] = []
    for fid in file_ids:
        try:
            valid_ids.append(uuid.UUID(fid))
        except (ValueError, TypeError):
            logger.warning(
                "Steering: ignoring malformed file_id %r (conversation=%s)",
                fid,
                conversation_id,
            )
    rows: dict[uuid.UUID, FileAttachment] = {}
    if valid_ids:
        try:
            fa_result = await db.execute(
                select(FileAttachment).where(
                    FileAttachment.id.in_(valid_ids),
                    FileAttachment.user_id == user_id,
                )
            )
            rows = {fa.id: fa for fa in fa_result.scalars()}
        except Exception:
            logger.warning(
                "Steering: FileAttachment lookup failed (conversation=%s)",
                conversation_id,
                exc_info=True,
            )
    refs: list = []
    attachments_list: list[dict] = []
    for fid_uuid in valid_ids:
        fa = rows.get(fid_uuid)
        if not fa:
            logger.warning(
                "Steering: file_id %s not found or not owned by user "
                "(conversation=%s)",
                fid_uuid,
                conversation_id,
            )
            continue
        attachments_list.append(
            {
                "filename": fa.filename,
                "type": _attachment_type_label(fa.filename, fa.content_type),
                "size": fa.size,
                "file_id": str(fa.id),
                "content_type": fa.content_type,
            }
        )
        refs.append(FileRef.from_attachment(fa))
    meta_json = (
        json.dumps({"attachments": attachments_list}) if attachments_list else None
    )
    return refs, meta_json


# LLM providers emit very small deltas (often just a few characters). Sending
# one frame per delta floods slow links with hundreds of tiny frames per
# second, which amplifies the stall-then-burst effect on congested
# connections. Tokens are buffered and flushed at most this often —
# imperceptible latency, ~10-20x fewer frames.
STREAM_COALESCE_SECONDS = 0.05


class _StreamCoalescer:
    """Coalesces LLM token deltas and reasoning deltas into at most one frame
    per run per ``STREAM_COALESCE_SECONDS`` window.

    ``add()`` buffers an answer chunk (a ``stream`` frame), ``add_thinking()``
    a reasoning chunk (a ``thinking_text`` status frame, whose only field is
    its ``content`` - the client appends it, so merged chunks read the same):
    consecutive chunks of one kind make one run and runs keep their order.
    Reasoning streams tens to hundreds of deltas a second, and each
    was its own awaited send while the provider stream waited.

    ``flush()`` drains the runs immediately (callers invoke it before
    ``status``/``done``/``error`` frames so the on-wire ordering matches the
    order events were produced by the agent loop); ``aclose()`` cancels the
    armed flush and drains the tail, so no task lingers after the turn
    completes.

    One flush may send several frames, so it holds a lock from the
    snapshot to its last send, and every caller takes it even with nothing
    buffered: a status frame sent after ``flush()`` returns can never
    overtake a run an in-progress flush has not sent yet. Runs a cancelled
    flush did not reach are put back for the next one.
    """

    def __init__(
        self,
        send: Callable[[dict], Awaitable[bool]],
        delay: float = STREAM_COALESCE_SECONDS,
    ) -> None:
        self._send = send
        self._delay = delay
        self._runs: list[tuple[str, list[str]]] = []
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    def add(self, chunk: str) -> None:
        """Buffer a token delta and arm the delayed flush if idle."""
        self._add("stream", chunk)

    def add_thinking(self, chunk: str) -> None:
        """Buffer a reasoning delta and arm the delayed flush if idle."""
        self._add("thinking", chunk)

    def _add(self, kind: str, chunk: str) -> None:
        if self._runs and self._runs[-1][0] == kind:
            self._runs[-1][1].append(chunk)
        else:
            self._runs.append((kind, [chunk]))
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._delayed_flush())

    async def flush(self) -> None:
        """Emit the buffered runs, in order, one frame per run."""
        async with self._lock:
            runs, self._runs = self._runs, []
            for index, (kind, chunks) in enumerate(runs):
                content = "".join(chunks)
                frame = (
                    {"type": "stream", "content": content}
                    if kind == "stream"
                    else {"type": "status", "status": "thinking_text", "content": content}
                )
                try:
                    await self._send(frame)
                except asyncio.CancelledError:
                    self._runs[:0] = runs[index + 1 :]
                    raise

    async def _delayed_flush(self) -> None:
        # Loop until the buffer is observed empty AFTER a flush: a chunk can
        # arrive while ``flush()``'s send is awaiting (``add()`` sees a
        # non-done task and does not arm a new one), and without the re-check
        # that tail would sit buffered until the next event instead of going
        # out within the coalescing window. There is no ``await`` between the
        # emptiness check and ``return``, so ``add()`` can never observe a
        # done task while a chunk it just buffered is left undrained.
        while True:
            await asyncio.sleep(self._delay)
            await self.flush()
            if not self._runs:
                return

    async def aclose(self) -> None:
        """Cancel any armed delayed flush and drain the remaining tail.

        Called from the per-message ``finally`` so the delayed task cannot
        wake up after the turn completes and attempt a send on a closed or
        recycled connection (nor linger as a pending task at teardown).
        """
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.flush()


#: Title generations running past their turn's ``done``; a reference is kept
#: until each finishes, or the loop could collect it mid-flight.
_turn_title_tasks: set[asyncio.Task] = set()


async def _generate_title_after_done(
    *,
    conversation_id: uuid.UUID,
    user_id: str,
    provisional_title: str,
    user_text: str,
    assistant_text: str,
    prebuilt_llm: tuple | None,
    send: Callable[[dict], Awaitable[bool]],
    origin: str,
) -> None:
    """Give a new conversation its AI title once the first turn is ``done``.

    The title is a second LLM call (~1-3 s); run before ``done`` it kept the
    composer in its working state after the answer had finished, on the
    first exchange of every conversation. It runs here instead,
    on sessions of its own - none is held across the call - and
    only replaces the provisional title the turn set, never a rename made
    meanwhile. The owner's streams are told with a ``conversation_title``
    frame: this one through *send*, the others (another tab, another
    device), which refetched the list at ``done`` before the title existed,
    through the conversation's relay, under this stream's *origin*. A
    shared viewer's stream runs no relay and reads the title from its own
    list refetches.
    """
    from app.core.database import async_session
    from app.llm.router import get_llm_for_user
    from app.services.conversation_memory import generate_conversation_title

    try:
        if prebuilt_llm is not None:
            llm, model = prebuilt_llm
        else:
            async with async_session() as db:
                llm, model = await get_llm_for_user(db, user_id)
        title = await generate_conversation_title(llm, user_text, assistant_text, model=model)
        if not title:
            return
        async with async_session() as db:
            result = await db.execute(
                update(Conversation)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.title == provisional_title,
                )
                .values(title=title)
            )
            await db.commit()
        if result.rowcount:
            frame = {
                "type": "conversation_title",
                "conversation_id": str(conversation_id),
                "title": title,
            }
            chat_turn_stream.publish_live(str(conversation_id), frame, origin=origin)
            await send(frame)
    except Exception as exc:
        logger.warning("Chat AI title generation failed: %s", format_exception_message(exc))


async def resolve_account_rejection(
    db: AsyncSession, user_id: uuid.UUID, issued_at: int | None, *, session_token: bool = True
) -> str | None:
    """Return why the account may no longer chat, or ``None`` to proceed.

    The account-state checks of ``deps.get_current_user_ungated``, run again
    before a turn and before each follow-up pass: a turn outlives the request
    that authenticated it. The token's subject must still exist and be active,
    and a session token (*session_token*) must not predate the last password
    change, which does not revoke an API key or a platform JWT. A deactivated
    account keeps its share grants only because it cannot authenticate.
    """
    row = (
        await db.execute(
            select(User.is_active, User.password_changed_at).where(User.id == user_id)
        )
    ).first()
    if row is None or not row.is_active:
        return "Invalid token"
    if session_token and row.password_changed_at is not None and (
        issued_at is None or issued_at < int(row.password_changed_at.timestamp())
    ):
        return "Session expired"
    return None


class SteerRefusedError(Exception):
    """A steering message was not taken: ``message`` says why, ``status_code`` and ``headers`` for HTTP."""

    def __init__(
        self, message: str, *, status_code: int, headers: Mapping[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.headers = dict(headers or {})


@dataclass(frozen=True)
class PersistedSteer:
    message: Message
    #: The attachments it carries, resolved for the running turn.
    file_refs: list
    #: Whose ``chat-send`` slot it took: given back when it is withdrawn.
    user_id: uuid.UUID


async def persist_web_steer(
    *,
    conversation_id: str,
    user_id: uuid.UUID,
    content: str,
    file_ids: list[str] | None,
) -> PersistedSteer:
    """Rate-limit and persist one steering message of a web conversation.

    Used by the web stream's steer route, which then hands the stored
    message to the running turn and publishes it once a turn took it
    (:func:`withdraw_web_steer` when none did). Raises
    :class:`SteerRefusedError` when the message was not stored. A message
    :func:`withdraw_web_steer` takes back gives its ``chat-send`` slot back.
    """
    from app.core.database import async_session

    conv_uuid = safe_uuid(conversation_id)
    # Same per-user throttle as the main send paths (shared bucket).
    try:
        await enforce_rate_limit(
            None,
            "chat-send",
            limit=CHAT_MESSAGES_PER_MINUTE,
            window_seconds=60,
            bucket=str(user_id),
        )
    except HTTPException as rl_exc:
        raise SteerRefusedError(
            str(rl_exc.detail), status_code=rl_exc.status_code, headers=rl_exc.headers
        ) from None

    # Persist now so thread ordering is correct (the steering user
    # message lands BEFORE the assistant message it influences).
    # Only enqueue + ack after a successful commit — otherwise the
    # agent would incorporate a message that is missing from the
    # persisted history after a refresh/reconnect.  Attachments are
    # resolved in the same session so the stored steering message
    # carries its attachment metadata and the FileAttachment rows are
    # linked to it, exactly like a regular send.
    _steer_file_refs: list = []
    try:
        async with async_session() as steer_db:
            _steer_attach_meta: str | None = None
            if file_ids:
                (
                    _steer_file_refs,
                    _steer_attach_meta,
                ) = await _resolve_steering_files(
                    steer_db, file_ids, user_id, conversation_id
                )
            _steer_msg_row = await store_message(
                steer_db,
                conv_uuid,
                role="user",
                content=content,
                channel="web",
                metadata_json=_steer_attach_meta,
            )
            if _steer_file_refs:
                from app.models.file_attachment import FileAttachment

                # One bulk UPDATE (not per-ref round-trips) — the ids
                # come straight from ``_resolve_steering_files`` rows,
                # so they are valid UUID strings by construction.
                # Guarded to UNLINKED rows of THIS conversation: a
                # reused ``file_id`` that is already linked to another
                # message (or belongs to a different conversation)
                # must not be re-pointed here — that would break the
                # original message's scoped download URL and let
                # attachments "move" between messages.  The steering
                # message still references the file via its metadata
                # (legacy /chat/files/{id}/download stays reachable).
                _steer_ref_ids = [
                    uuid.UUID(_sref.file_id) for _sref in _steer_file_refs
                ]
                await steer_db.execute(
                    update(FileAttachment)
                    .where(
                        FileAttachment.id.in_(_steer_ref_ids),
                        FileAttachment.user_id == user_id,
                        FileAttachment.message_id.is_(None),
                        FileAttachment.conversation_id == conv_uuid,
                    )
                    .values(message_id=_steer_msg_row.id)
                )
            await steer_db.commit()
    except Exception:
        logger.exception(
            "Failed to persist steering message for conversation %s",
            conversation_id,
        )
        raise SteerRefusedError(
            "Your message could not be saved - please send it again.",
            status_code=503,
        ) from None
    return PersistedSteer(message=_steer_msg_row, file_refs=_steer_file_refs, user_id=user_id)


async def withdraw_web_steer(steer: PersistedSteer) -> None:
    """Delete a stored steering message no turn took, its attachments unlinked.

    The client then sends the text as the next turn with the same
    ``file_ids``: the files must be free to link to that message, and the
    thread must not keep a second, unanswered copy. Its ``chat-send`` slot
    is given back: the resend is charged for it.
    """
    from app.core.database import async_session
    from app.models.file_attachment import FileAttachment

    async with async_session() as db:
        await db.execute(
            update(FileAttachment)
            .where(FileAttachment.message_id == steer.message.id)
            .values(message_id=None)
        )
        await db.execute(delete(Message).where(Message.id == steer.message.id))
        await db.commit()
    await refund_rate_limit(None, "chat-send", bucket=str(steer.user_id))


class ChatConnection(Protocol):
    """The transport one web chat send runs over (an HTTP + SSE stream).

    Its first inbound frame is the send; the ``cancel`` / ``skip_wait`` frames
    a route injects follow. The runner ends once that send's turn (title
    included) or fold is delivered.
    """

    async def send_json(self, data: dict) -> None:
        """Write *data*; every frame of the stream's turn names it (``turn_id``), as its
        ``turn_started`` or ``folded_into_active_turn`` does."""
        ...

    async def receive_json(self) -> Any: ...

    async def close(self, *, reason: str = "") -> None: ...

    async def end_session(self, reason: SessionEndReason) -> None:
        """Tell the client its session ended, so the web app signs out, then close."""
        ...

    async def end_app_locked(self) -> None:
        """Tell the client its companion session locked, so the web app shows its lock screen, then close."""
        ...

    async def end_stream(self) -> None:
        """Complete the response without a word; a turn running keeps running."""
        ...

    async def refuse(self, detail: str, *, status_code: int, headers: Mapping[str, str] | None = None) -> None:
        """Refuse the send before its turn started, with the HTTP status and headers it answers, then close."""
        ...

    def accept_steering(self, handoff: Callable[[SteeringPayload], None] | None) -> None:
        """While set, the steer route on this process hands its messages to *handoff*, not to the inbox.

        ``None``: the turn reads no more steering, and the route refuses it
        rather than folding it where nothing answers it.
        """
        ...

    def turn_released(self) -> None:
        """The turn this connection ran released the conversation's lock: the routes reach a next turn elsewhere."""
        ...

    def client_present(self) -> bool: ...


@dataclass(frozen=True)
class ChatConnectionAuth:
    """Who a chat connection runs for, resolved by its transport before it starts."""

    user_id: uuid.UUID
    session: WebSession
    impersonated_by: str | None = None
    token_issued_at: int | None = None
    # The fan-out identity of the connection; a fresh one when the transport names none.
    origin: str | None = None


async def run_chat_connection(
    conn: ChatConnection, conversation_id: str, auth: ChatConnectionAuth
) -> None:
    """Serve one web chat send: its turn, or the turn it folds into, with cancels and fan-out.

    The transport has authenticated the caller and passed the chat gates.
    """
    user_id = auth.user_id
    # _turn_: per-send turn state.
    _turn_impersonated_by = auth.impersonated_by
    token_issued_at = auth.token_issued_at
    _turn_session = auth.session
    # ``None`` for a malformed id, which names no conversation: the per-message
    # lookup below answers it like an unknown one, so no turn ever runs without
    # it set.
    conv_uuid = safe_uuid(conversation_id)

    from app.core.database import async_session

    # Identity of this stream on the conversation's fan-out channel:
    # every frame it publishes carries it as ``origin`` and the relay never
    # echoes a stream's own frames back to it.
    _origin = auth.origin or uuid.uuid4().hex
    # Fan-out publisher of the turn THIS stream owns — bound when the turn
    # lock is acquired, closed (journal dropped) with the terminal frame or
    # by ``_clear_processing_marker`` on every other exit path. Declared
    # before ``_safe_send_json`` so the closure never reads an unbound name.
    _turn_publisher: chat_turn_stream.TurnPublisher | None = None
    _turn_token: str | None = None
    # When the turn took the lock: a Stop recorded since is this turn's.
    _turn_started_at = datetime.now(UTC)

    # The tool calls of the message being processed: the names started, the
    # iterations, and the per-call trace (name, input, output, success) for
    # the reasoning details panel, folded from the agent loop's events. What
    # a turn the graceful shutdown cuts stores as its reply.
    _turn_progress = TurnProgress()
    # The answer streamed by the loop's current pass, as the panel shows it (a
    # new pass, or a retracted stream, starts it over): what a Stop keeps.
    _turn_answer_parts: list[str] = []
    # The text of the turn's intermediate segments: they carry no
    # files, so a file one of them names is attached to the turn's final
    # message, which reads them with its own text.
    _turn_segment_texts: list[str] = []
    _turn_transfer_chain: list[dict[str, str]] = []
    # The integrations the turn did not use for its user: carried by
    # the turn's first message (a segment or the final answer), then cleared.
    _turn_unavailable_integrations: list[dict] = []
    # Machine activity: the display record of every computer_use
    # command, folded from the tool's ``runner_command_*`` progress frames.
    _turn_runner_activity = RunnerActivityLog()
    # How full the context window was at the run's last model call,
    # the composer's gauge. It measures the RUN: a segment keeps it, the end of
    # a run (the turn's, a mentioned agent's reply) clears it.
    _turn_context_usage = ContextUsageTracker()
    # Flipped true whenever the loop emits a budget-driven ``wind_down`` or
    # ``composing`` event, signalling that the iteration budget was reached
    # and the final answer is only a best-effort summary.  Mid-loop re-prompts
    # (``composing`` with a reason in ``CONTINUATION_COMPOSING_REASONS`` —
    # stall guard, steering resume-promise guard) do NOT count — the run
    # continues with its full budget.
    _turn_is_truncated: bool = False
    # Accumulated reasoning prose (``thinking_text`` events) for the
    # reasoning-details panel.  Held-back pre-tool preambles and retracted
    # provisional answers both arrive through ``thinking_text`` (see
    # agent_loop's hold-back window).  Head+tail bounded so the final
    # wind-down reasoning survives the cap on long turns.
    _turn_reasoning = ReasoningAccumulator()
    # The agent the message being written belongs to, on a turn several
    # @mentioned agents answer: ``reply_metadata`` of that agent, empty
    # on every other turn so their metadata and frames are unchanged.
    _turn_answered_by_meta: dict = {}
    _stream_closed = False

    # Serializes frames from concurrent senders (agent-loop callbacks vs the
    # delayed stream-flush task) so frames can never interleave mid-send.
    _send_lock = asyncio.Lock()

    async def _safe_send_json(data: dict) -> bool:
        """Send JSON over the connection, returning False if already closed.

        The send is bounded by ``_SEND_TIMEOUT``: a zombie client (TCP
        backpressure with no FIN — backgrounded tab, dead network path)
        would otherwise block the stream write for minutes, and because
        ``_send_lock`` serializes every sender, ONE stuck send would freeze
        status events and the agent loop itself (``_emit`` awaits this
        function).  On timeout the stream is
        treated as dead: mark closed, log, return False — the agent loop
        continues heads-down and the answer is still persisted.

        Fan-out first: while this stream owns a turn, every turn
        frame is also offered to the publisher — BEFORE the dead-stream
        early return, because a turn that outlives its stream (page reload)
        is exactly the turn the reloaded page needs to follow. Frames this
        stream merely relays (``relayed``) are never re-published.

        The connection names the turn on every frame of it (``turn_id``,
        #4395), the id the publisher stamps on the copies other streams
        receive.
        """
        nonlocal _stream_closed
        # Model output that echoed a secret variable value is shown as the
        # display text, never the value nor its restorable marker;
        # the frame's own type and ids are never rewritten.
        data = redact_frame(data, display=True)
        if _turn_publisher is not None and not data.get("relayed"):
            _turn_publisher.offer(data)
        if _stream_closed:
            return False
        try:
            async with _send_lock:
                await asyncio.wait_for(
                    conn.send_json(data), timeout=_SEND_TIMEOUT
                )
            return True
        except TimeoutError:
            _stream_closed = True
            logger.warning(
                "Chat stream send timed out after %ss (zombie client?) for "
                "conversation %s — marking the stream dead; the turn continues "
                "and the answer will be persisted",
                _SEND_TIMEOUT,
                conversation_id,
            )
            return False
        except StreamConsumerGoneError:
            _stream_closed = True
            return False

    # ── Token coalescing (see _StreamCoalescer) ─────────────────────────
    # Status events drain the buffer first so the relative ordering of
    # ``stream`` and ``status`` frames is preserved.
    _coalescer = _StreamCoalescer(_safe_send_json)

    async def _send_intermediate_segment_frames(
        content: str,
        *,
        message_id: str | None,
        meta: dict,
        transfer: dict | None = None,
    ) -> None:
        """Send the frame pair that finalizes a MID-TURN segment.

        Single source of truth for the two intermediate-segment producers —
        the loop's ``segment_complete`` event and the steering fold-in — so
        the wire contract can never drift between them:

        1. ``done`` carrying the stored payload, flagged ``intermediate:
           True``.  The flag tells a segment-aware client that this is NOT
           a turn boundary: commit the message and clear the streaming and
           reasoning buffers, but keep the working state armed and skip the
           end-of-turn refetch — the final ``done`` (which never carries
           the flag) does that.
        2. ``thinking`` (also flagged) to re-arm the working UI on a client
           that ignores the flag (it treats every ``done`` as terminal) —
           a segment-aware client uses it only as a status refresh.
        """
        _turn_segment_texts.append(content)
        frame: dict = {
            "type": "done",
            "content": content,
            "conversation_id": conversation_id,
            "intermediate": True,
            **meta,
        }
        if message_id:
            frame["message_id"] = message_id
        if transfer:
            frame.update(transfer)
        await _safe_send_json(frame)
        await _safe_send_json({"type": "thinking", "intermediate": True})

    async def _on_event(event: dict) -> None:
        nonlocal _turn_is_truncated
        status_ = event.get("status")
        # ── Mid-turn segment delivery ────────────────────
        # A live-streamed answer the user already read is about to be
        # followed by more work in the same turn (a tool round, more
        # waiting after a partial wake, a guard re-prompt): the loop hands
        # it over as a completed segment instead of retracting it.  Persist
        # it as an intermediate assistant message and send the exact frame
        # sequence the steering fold-in uses for its intermediate segments
        # (flagged ``done`` with the stored payload, then ``thinking``) so
        # a page reload shows the segment identically.
        if status_ == "segment_complete":
            # Strip only for the blank check — persist and send the text
            # VERBATIM: it must match what already streamed to the client,
            # and a report opening with an indented Markdown code block
            # would lose its leading indentation (and stop rendering as
            # code after a reload) if stripped here.
            _seg_text = str(event.get("content") or "")
            if not _seg_text.strip():
                return
            # Drain any buffered token tail BEFORE the ``done`` frame —
            # same rationale as the fold-in path: the client clears its
            # streaming buffer on ``done``, so a coalesced tail arriving
            # after it would duplicate the end of this segment into the
            # next streaming bubble.
            await _coalescer.flush()
            _seg_meta = _collect_turn_meta()
            _seg_langfuse = await load_latest_langfuse_ids(conversation_id)
            _seg_meta.update(_seg_langfuse)
            _seg_message_id: str | None = None
            try:
                # Own short-lived session (the steering-listener /
                # build_presentation_tools precedent): this callback runs
                # from INSIDE the agent loop, so the request session belongs
                # to the running turn — ``run_agent_chat`` released its
                # connection before the loop, but committing the request
                # session from here could flush unrelated pending state and
                # would couple this write to whatever transaction the turn
                # opens next.  A fresh session commits exactly one row.
                async with async_session() as _seg_db:
                    _seg_msg = await store_message(
                        _seg_db,
                        conv_uuid,
                        role="assistant",
                        content=_seg_text,
                        channel="web",
                        metadata_json=(
                            json.dumps(_seg_meta) if _seg_meta else None
                        ),
                        llm_trace_id=safe_uuid(
                            _seg_langfuse.get("llm_trace_id")
                        ),
                    )
                    await _seg_db.commit()
                    _seg_message_id = str(_seg_msg.id)
            except Exception:
                # The user already read the text — still finalize the
                # bubble (without a message_id) so it survives on screen
                # for this session; only a reload loses it.  Never let a
                # persistence failure break the running turn.
                logger.exception(
                    "Failed to persist mid-turn segment for conversation %s",
                    conversation_id,
                )
            await _send_intermediate_segment_frames(
                _seg_text, message_id=_seg_message_id, meta=_seg_meta
            )
            # The tracking state now belongs to the stored segment — reset
            # it so the rest of the turn attributes tool calls / reasoning
            # to its own (next) message, mirroring the fold-in path.
            _reset_turn_tracking()
            return
        # Tool calls, iterations and the per-call trace for message metadata.
        _turn_progress.observe(event)
        if event.get("status") in ("thinking", "stream_retract"):
            _turn_answer_parts.clear()
        # A computer_use command appeared, changed state or ended: the card
        # the transcript shows is persisted with the message it belongs to.
        _turn_runner_activity.observe(event)
        _turn_context_usage.observe(event)
        # Accumulate reasoning prose for the reasoning-details panel.
        # Head+tail bounded (see ReasoningAccumulator) so the final
        # wind-down reasoning is never silently dropped by the cap. The
        # frame itself rides the coalescer, like answer tokens.
        if status_ == "thinking_text":
            if event.get("content"):
                _turn_reasoning.add(str(event["content"]))
                _coalescer.add_thinking(str(event["content"]))
            return
        # Track transfer chain for reasoning details
        if status_ == "transfer_start" and event.get("agent_name"):
            _turn_transfer_chain.append(
                {"agent_id": event.get("agent_id", ""), "agent_name": event["agent_name"]}
            )
        # Detect iteration-budget truncation.
        #   "wind_down" — agent_loop tells the LLM to wrap up because the
        #                 iteration counter is approaching `max_iterations`.
        #   "composing" — tools have been stripped from the LLM request to
        #                 force a final text response within the budget.
        # Either signal means the final response is best-effort and the agent
        # may not have completed every planned step.  Mid-loop re-prompts
        # (stall guard, steering resume-promise guard) also emit "composing"
        # but the run then CONTINUES with its full budget — they must never
        # flag the turn as truncated; their reasons are single-sourced
        # in ``CONTINUATION_COMPOSING_REASONS``.
        if status_ == "wind_down" or status_ == "composing":
            from app.tools.agent_loop import CONTINUATION_COMPOSING_REASONS

            if status_ == "wind_down" or (
                event.get("reason") not in CONTINUATION_COMPOSING_REASONS
            ):
                _turn_is_truncated = True
        # Drain pending token text first so stream/status ordering on the
        # wire matches the order events were produced by the agent loop.
        await _coalescer.flush()
        # Slim, like every relayed copy: a full tool output and the prompt
        # copies of each model call were sent on every iteration, and the
        # send is awaited by the loop.
        await _safe_send_json(chat_turn_stream.slim_frame(chat_turn_stream.event_frame(event)))

    async def _on_token(chunk: str) -> None:
        _turn_answer_parts.append(chunk)
        _coalescer.add(chunk)

    def _collect_turn_meta(*, final_content: str | None = None) -> dict:
        """Snapshot the per-turn tool/transfer tracking state.

        The returned dict uses the exact keys shared by the assistant
        message ``metadata_json`` and the ``done`` frame, so both the
        final answer and intermediate steering segments attribute tool
        calls / iterations / transfers to the message they actually
        belong to.

        ``final_content`` is the text the message will carry: reasoning
        paragraphs repeated verbatim in it are dropped — the
        deliverable rescue can merge held-back pre-tool text into
        the reply, and the same text must not also sit in the reasoning
        panel.
        """
        meta: dict = _turn_progress.meta()
        if _turn_transfer_chain:
            meta["transfer_chain"] = list(_turn_transfer_chain)
        if _turn_runner_activity:
            # No command is in flight at a segment boundary or at the final
            # answer, so a card still live here never reported back (a
            # stopped turn, a cancelled tool): it is persisted closed.
            meta[RUNNER_ACTIVITY_META_KEY] = _turn_runner_activity.snapshot()
        if _turn_is_truncated:
            meta["is_truncated"] = True
        if _turn_unavailable := clean_unavailable_integrations(_turn_unavailable_integrations):
            meta[UNAVAILABLE_INTEGRATIONS_META_KEY] = _turn_unavailable
        _reasoning_text = _turn_reasoning.render()
        if final_content:
            _reasoning_text = strip_reasoning_echoed_in_reply(
                _reasoning_text, final_content
            )
        _reasoning_text = _reasoning_text.strip()
        if _reasoning_text:
            meta["reasoning"] = _reasoning_text
        meta.update(_turn_context_usage.meta())
        if _turn_answered_by_meta:
            meta.update(_turn_answered_by_meta)
        return meta

    def _reset_turn_tracking(*, run_ended: bool = False) -> None:
        """Reset per-turn tracking at a turn (or steering segment) boundary.

        ``run_ended``: the run that wrote the message is over, so its context
        reading goes too; a segment of a run still going keeps it.
        """
        nonlocal _turn_is_truncated
        _turn_progress.reset()
        _turn_answer_parts.clear()
        _turn_transfer_chain.clear()
        _turn_runner_activity.reset()
        _turn_is_truncated = False
        _turn_reasoning.reset()
        _turn_unavailable_integrations.clear()
        if run_ended:
            _turn_context_usage.clear()

    # Redis key marking this conversation's in-flight turn, refreshed by the
    # keepalive task below while the turn runs.  Set when a turn
    # starts, cleared on every exit path.  Without the periodic refresh the
    # key's 300 s TTL expired mid-turn on long conversations (a turn can
    # legitimately run for tens of minutes), so a reloading client saw
    # ``is_processing=false`` and rendered nothing while the loop was still
    # working.
    #
    # Since #2442 the marker doubles as the conversation's single-flight
    # TURN LOCK: its value is this turn's unique token (``_turn_token``),
    # acquired via ``chat_turn_guard.acquire_turn``.  A message arriving for
    # a conversation whose lock is held by ANOTHER turn (page reload while
    # the previous turn still runs, second tab, another replica) is folded
    # into the running loop as steering instead of spawning a concurrent
    # second agent loop.  (``_turn_token`` itself is declared next to
    # ``_turn_publisher`` above, before ``_safe_send_json``.)
    _active_processing_key: str | None = None
    # Per-turn bridge task feeding steering folded from other streams from
    # the Redis inbox into this turn's in-process steering queue.  Rebound
    # per turn; cancelled by ``_clear_processing_marker`` and the per-turn
    # finalization so it can never outlive its turn.
    _turn_steer_bridge_task: asyncio.Task | None = None
    # Asks the bridge to stop once the batch it holds is queued (rebound per
    # turn): cancelled mid-batch, it would lose what it took off the inbox.
    _turn_bridge_stop = asyncio.Event()
    # The folds the turn's loop never took, queued after its last look: the
    # turn's end answers them in a follow-up.
    _turn_unanswered_folds: list[SteeringPayload] = []
    # Steers the web steer route hands over on this process, read by the
    # bridge as soon as ``_turn_steer_wake`` is set, before the inbox's.
    _turn_local_steers: list[SteeringPayload] = []
    _turn_steer_wake = asyncio.Event()

    def _hand_steer_to_bridge(payload: SteeringPayload) -> None:
        _turn_local_steers.append(payload)
        _turn_steer_wake.set()

    async def _stop_steer_bridge() -> None:
        nonlocal _turn_steer_bridge_task
        from app.services import chat_turn_guard as _sb_guard

        _task, _turn_steer_bridge_task = _turn_steer_bridge_task, None
        _turn_steer_wake.set()
        await _sb_guard.stop_bridge(_task, _turn_bridge_stop)

    def _stop_local_steering() -> None:
        """The steer route refuses steers from now on; one handed over and not read is in the thread.

        Only a turn that runs no follow-up pass for it (a Stop, an error, a
        teardown) leaves one here: the leftover check answers them otherwise.
        """
        conn.accept_steering(None)
        if _turn_local_steers:
            logger.warning(
                "%d steering message(s) arrived too late to be injected into "
                "the turn for conversation %s - the messages are persisted "
                "and will shape the next turn",
                len(_turn_local_steers),
                conversation_id,
            )
            _turn_local_steers.clear()
    # The messages folded into the current turn whose @mentioned agents it
    # still owes an answer, in arrival order: recorded by the bridge
    # and the leftover drain, answered after the turn's own agents, and
    # handed back by ``_clear_processing_marker`` when the turn ends first.
    # Cleared, never rebound: the bridge holds this list.
    _turn_folded_mentions: list[FoldedMentions] = []

    async def _refresh_turn_marker() -> None:
        """Refresh the ``chat:processing:{conv_id}`` Redis marker while a turn is active.

        Reconnecting and reloading clients keep detecting the in-flight
        response for the whole turn: the marker's TTL (300 s) is far
        shorter than a long-conversation turn.
        """
        while True:
            await asyncio.sleep(20)
            # Snapshot the key + token: ``_clear_processing_marker`` can
            # rebind them to None concurrently (this loop awaits between
            # check and use).
            _ka_key = _active_processing_key
            _ka_token = _turn_token
            if _ka_key is None or _ka_token is None:
                continue
            # Best-effort: a Redis blip must never kill the refresh loop (the
            # marker self-heals on the next tick). ``refresh_turn`` re-creates
            # an expired marker but never clobbers a different turn's lock
            #.
            try:
                from app.services import chat_turn_guard as _ka_guard

                await _ka_guard.refresh_turn(str(conversation_id), _ka_token)
                if _active_processing_key is None:
                    # The turn ended (marker cleared) while this refresh
                    # was in flight — undo the phantom re-create instead
                    # of leaving a "processing" ghost for a full TTL.
                    await _ka_guard.release_turn(str(conversation_id), _ka_token)
            except Exception:
                logger.debug(
                    "Failed to refresh chat processing marker %s",
                    _ka_key,
                    exc_info=True,
                )

    async def _clear_processing_marker(*, follow_up: bool = False) -> None:
        """Release the turn (``_release_processing_marker``), then take the connection off this process's routes.

        Taken off after the release, not at the end of the stream (an AI
        title is still on its way): a Stop or steer meant for a next turn,
        which may run on another replica, would otherwise land here.
        """
        try:
            await _release_processing_marker(follow_up=follow_up)
        finally:
            conn.turn_released()

    async def _release_processing_marker(*, follow_up: bool = False) -> None:
        """Release the in-flight turn lock (best-effort) and stop the
        keepalive refresh for it.

        Single cleanup path shared by the success exit, the mid-run
        cancellation handler and the outer handler-teardown handlers so no
        exit can leave a stale marker behind.  Release is
        token-guarded: if this turn's TTL lapsed and another turn
        legitimately acquired the lock, their marker is left untouched.

        The release-and-drain handshake catches a fold that raced the end of
        the turn: pushed after the fold-in loop's last leftover check, while
        the turn still held the lock, so its sender was told the turn took
        it. With *follow_up* - a turn that reached its own end, not stopped,
        its account still allowed to chat - those folds and the ones its
        bridge queued after the loop's last look are answered by a follow-up
        turn once the lock is released (``schedule_fold_follow_up``). On any
        other exit they are logged: persisted user messages, they shape the
        next turn's history. The agents such a fold mentions, and those of a
        fold this turn recorded but did not ask, are handed back to the
        inbox: the message is in the thread, its mentions are not."""
        nonlocal _active_processing_key, _turn_token
        nonlocal _turn_publisher
        _stop_local_steering()
        await _stop_steer_bridge()
        # The success path closes the publisher right after publishing the
        # terminal frame; every other exit (error, cancellation, teardown)
        # lands here with it still open — close the turn for its viewers
        # with the interrupted terminal (they would otherwise hold "Thinking"
        # until their next safety poll), flush what it holds and drop the
        # journal so a viewer is not resumed into a turn that is gone.
        if _turn_publisher is not None:
            _publisher, _turn_publisher = _turn_publisher, None
            with contextlib.suppress(Exception):
                await _publisher.aclose(terminal=chat_turn_stream.interrupted_frame())
        if _active_processing_key is None:
            return
        _token = _turn_token
        _active_processing_key = None
        _turn_token = None
        if _token is None:
            return
        _owed_folds = [m.payload for m in _turn_folded_mentions if m.payload is not None]
        _turn_folded_mentions.clear()
        _unanswered = list(_turn_unanswered_folds)
        _turn_unanswered_folds.clear()
        with contextlib.suppress(Exception):
            from app.services import chat_turn_guard as _cm_guard
            from app.services.parent_wake import schedule_fold_follow_up

            _unanswered += await _cm_guard.release_turn_and_drain(
                str(conversation_id), _token
            )
            if _unanswered and follow_up:
                follow_up = await _may_follow_up(
                    str(conversation_id), _turn_started_at, user_id, token_issued_at
                )
            _handed_back = await _cm_guard.requeue_owed_mentions(
                str(conversation_id),
                [*_owed_folds, *(p for p in _unanswered if all(p is not o for o in _owed_folds))],
            )
            if _handed_back:
                logger.info(
                    "%d folded message(s) of conversation %s handed back "
                    "with the agents they mention, for the next turn",
                    _handed_back,
                    conversation_id,
                )
            _followed = follow_up and schedule_fold_follow_up(
                conversation_id=str(conversation_id), user_id=str(user_id), folds=_unanswered
            )
            if _unanswered and not _followed:
                logger.warning(
                    "%d steering fold(s) landed as the turn for "
                    "conversation %s was finalizing — the messages are "
                    "persisted in the thread and will shape the next turn",
                    len(_unanswered),
                    conversation_id,
                )

    # Cancel event — set when the client sends {"type": "cancel"} to abort
    # the current agent loop.  A new Event is created per message.
    _cancel_event: asyncio.Event | None = None
    # Wait-skip event — set when the client sends
    # {"type": "skip_wait"} to end the agent's CURRENT pause early without
    # aborting the run.  Deliberately separate from ``_cancel_event``: "stop
    # waiting, carry on" and "stop everything" are different intents, and the
    # loop clears the event after each wait so one skip ends one pause.
    _wait_skip_event: asyncio.Event | None = None
    # Steering queue — mid-run user messages sent while the agent is still
    # processing.  A new Queue is created per message alongside the cancel
    # event; the agent loop drains it at iteration boundaries and the
    # post-run leftover check folds late arrivals into a follow-up turn.
    _steering_queue: asyncio.Queue | None = None
    # Steering texts successfully enqueued during the current turn, in
    # arrival order.  The fold-in path uses this to reconstruct the
    # canonical thread for a follow-up pass: texts injected into the
    # running loop are drained from the queue (absent from the leftovers)
    # but still belong to the prompt history — without replaying them the
    # follow-up pass would run against a history that diverges from the
    # persisted thread.  Appended in the same synchronous step as
    # ``put_nowait`` so that, at any event-loop step, the queue contents
    # equal the tail of this list.
    _turn_steered_texts: list[str] = []

    # Whether this send's client can answer an approval prompt, as the
    # send declares. Defaults False: a client that cannot answer would leave
    # the turn paused.
    _turn_supports_approval = {"value": False}

    async def _turn_request_approval(proposals: list[dict]) -> dict[str, dict]:
        """Ask over the stream, then wait for the answer.

        The frame goes out here; the reply arrives on the approve route
        (``apply_approval_decisions``), possibly on another replica, and
        lands in Redis, which is what this then drains.
        """
        from app.services.chat_turn_guard import (
            await_approval_decision,
            clear_pending_approvals,
            set_pending_approvals,
        )

        # Record the pause *before* announcing it: a client that reloads in the
        # gap would otherwise fetch the conversation, be told a turn is running,
        # and find no prompt to answer it with.
        await set_pending_approvals(str(conversation_id), proposals)
        await _safe_send_json(
            {"type": "approval_required", "proposals": proposals}
        )
        try:
            # Without ``client_present`` the abandoned-approval guard can never
            # fire: it reads "no way to tell" as "someone is there". A closed
            # tab then held the turn — and its processing marker — until the
            # loop's own timeout, which is often longer than the guard's 30
            # minutes. Reconnecting resets it; a reviewer thinking is not on a
            # clock.
            return await await_approval_decision(
                str(conversation_id),
                client_present=conn.client_present,
            )
        finally:
            # Whether the decision arrived or the turn was cancelled out from
            # under the wait, the pause is over and must not be replayed to the
            # next client that loads this conversation.
            await clear_pending_approvals(str(conversation_id))
            # Every stream was shown the prompt, so every stream is told it is
            # gone: a card left up would take a click that decides nothing.
            with contextlib.suppress(Exception):
                await _safe_send_json({"type": "approval_resolved"})

    async def _listen_during_processing() -> None:
        """Handle the cancel / skip-wait frames a route injects while the agent is processing.

        Steering arrives through the steer route (in process, else Redis),
        approvals through Redis. A ``cancel`` frame ends the listener after
        the ack: the run is aborting. The cancel route has already recorded
        the Stop and stopped the background work.
        """
        nonlocal _cancel_event, _wait_skip_event
        while True:
            msg = await conn.receive_json()
            if not isinstance(msg, dict):
                continue
            msg_type = msg.get("type")
            if msg_type == "skip_wait":
                # "Continue now" — end the agent's current pause but keep the
                # run alive.  Unlike ``cancel`` the listener stays up: the turn
                # continues and may steer, wait or stream again.  Setting the
                # event when no wait is in flight is harmless — the loop clears
                # it after the next wait, and a wait that has not started yet
                # legitimately reads it as "don't pause".
                if _wait_skip_event:
                    _wait_skip_event.set()
                await _safe_send_json({"type": "wait_skipped"})
                continue
            if msg_type == "cancel":
                if _cancel_event:
                    _cancel_event.set()
                await _safe_send_json({"type": "cancelled", "provisional": True})
                return

    async def _close_ended_session(reason: SessionEndReason) -> None:
        """End this stream because its session ended: the web app signs out.

        A turn already running keeps running and is stored, as on a reload.
        """
        nonlocal _stream_closed
        _stream_closed = True
        async with _send_lock:
            with contextlib.suppress(Exception):
                await conn.end_session(reason)

    async def _close_locked_session(state: str) -> None:
        """End this stream because its companion session locked behind its PIN, the PIN ended it, or its lock cannot be read.

        A turn already running keeps running and is stored, as on a reload. A
        lock ends the stream with an ``app_locked`` frame, never an ``error``
        the transcript would show: the app shows its lock screen. A lock that
        cannot be read just ends it: the app's next request reads the lock
        (``session_lock_state``).
        """
        nonlocal _stream_closed
        _stream_closed = True
        async with _send_lock:
            with contextlib.suppress(Exception):
                if state == "locked":
                    await conn.end_app_locked()
                else:
                    await conn.end_stream()

    async def _drop_kept_stop() -> None:
        """Forget the Stop kept for this send, which starts no turn of its own.

        Left behind, it would stop the tab's next send.
        """
        from app.services import chat_turn_guard as _stop_guard

        if conv_uuid is None:
            return
        try:
            await _stop_guard.drop_pending_cancel(str(conv_uuid), _origin)
        except Exception:
            # A kept Stop left behind expires shortly.
            logger.warning(
                "Could not drop the kept Stop of conv %s", conv_uuid, exc_info=True
            )

    async def _refuse_send(
        detail: str, status_code: int, headers: Mapping[str, str] | None = None
    ) -> None:
        """Refuse the send before its turn started: the client reads it as an HTTP error, not a frame."""
        nonlocal _stream_closed
        _stream_closed = True
        await _drop_kept_stop()
        async with _send_lock:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    conn.refuse(detail, status_code=status_code, headers=headers),
                    timeout=_SEND_TIMEOUT,
                )

    async def _watch_session() -> None:
        """End the stream once its session ends, idle or past its lifetime, while it stays open.

        Reads the deadlines, never moves them: neither a keepalive nor a frame
        the server sends is the person's activity.
        """
        nonlocal _turn_session
        while not _stream_closed:
            await asyncio.sleep(SESSION_RECHECK_SECONDS)
            try:
                _turn_session = await recheck_session(_turn_session)
            except SessionEndedError as exc:
                await _close_ended_session(exc.reason)
                return
            lock_state = await session_lock_state(user_id, _turn_session)
            if lock_state != "open":
                await _close_locked_session(lock_state)
                return

    _session_watch_task = asyncio.create_task(
        _watch_session(), name=f"chat-session-watch:{conversation_id}"
    )

    # The connection ends once its send is delivered: the turn it ran with its
    # title, or the turn it folded into up to that turn's end.
    _send_title_task: asyncio.Task | None = None
    # And its reply suggestion (``services/chat_reply_suggestions.py``).
    _send_suggestion_task: asyncio.Task | None = None

    async def _relay_folded_turn(token: str) -> None:
        """Relay the turn the send folded into, until its end reaches the client.

        The relay shows only a turn in flight when it attaches, so a turn that
        ended before is closed from its tombstone as soon as the relay attached,
        or as lost; so is a turn Redis cannot vouch for during
        ``FOLD_RELAY_UNCONFIRMED_SECONDS``.
        """
        ended = asyncio.Event()
        # Set when the relay (re)attached or the end went out: probe now.
        wake = asyncio.Event()
        delivering = asyncio.Lock()
        turn_id = chat_turn_stream.public_turn_id(token)

        def _end() -> None:
            ended.set()
            wake.set()

        async def _send_until_end(frame: dict) -> bool:
            # The relay and the close below both write here: nothing follows the end.
            async with delivering:
                if ended.is_set():
                    return False
                if not await _safe_send_json(frame):
                    _end()
                    return False
                if chat_turn_stream.is_terminal_frame(frame) and not chat_turn_stream.is_provisional_end(frame):
                    _end()
                    return False
                return True

        async def _close_turn(terminal: dict | None) -> None:
            terminal = terminal or chat_turn_stream.lost_turn_frame()
            await _send_until_end({**terminal, "turn_id": terminal.get("turn_id") or turn_id})

        relay = asyncio.create_task(
            chat_turn_stream.relay_turn_frames(
                conversation_id,
                origin=_origin,
                send=_send_until_end,
                is_closed=lambda: _stream_closed or ended.is_set(),
                attached=wake,
            ),
            name=f"chat-turn-fold-relay:{conversation_id}",
        )
        confirmed_at = time.monotonic()
        try:
            while not ended.is_set() and not relay.done() and conn.client_present():
                # A probe follows each attachment at once: a turn over by then
                # never reaches the relay live.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), timeout=FOLD_RELAY_PROBE_SECONDS)
                wake.clear()
                if ended.is_set() or relay.done() or not conn.client_present():
                    break
                turn_end = await chat_turn_stream.probe_turn_end(str(conversation_id), token)
                if turn_end is None:
                    if time.monotonic() - confirmed_at >= FOLD_RELAY_UNCONFIRMED_SECONDS:
                        await _close_turn(None)
                        break
                    continue
                confirmed_at = time.monotonic()
                if not turn_end.over:
                    continue
                if turn_end.terminal is None and not turn_end.ended:
                    # Over with no tombstone: its producer may still deliver
                    # the end, which the relay forwards for a turn it shows.
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(ended.wait(), timeout=FOLD_RELAY_PROBE_SECONDS)
                await _close_turn(turn_end.terminal)
                break
        finally:
            relay.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await relay

    # Started with the turn; the `finally` below stops it on every way out, an
    # exception included, or its loop would outlive the stream.
    marker_refresh_task: asyncio.Task[None] | None = None
    try:
        # The turn resolves the variables its user may read.
        renew_known_secrets(conversation_id)
        data = await conn.receive_json()

        if data.get("supports_tool_approval"):
            _turn_supports_approval["value"] = True
        # Only a client that offers it gets a reply suggestion: a caller
        # reading to the end of the stream must not wait on one.
        _turn_supports_reply_suggestions = bool(data.get("supports_reply_suggestions"))

        # Response mode for this turn, read from the send: the client
        # owns the toggle. Anything other than the one recognised value runs
        # the normal agent turn: an unknown mode degrades, never refuses.
        #
        # With "DEEP_INVESTIGATION" off the mode is an unknown
        # one: the message runs as the normal agent turn.
        _turn_deep_investigation = data.get("mode") == "deep_investigation" and is_feature_enabled(
            "DEEP_INVESTIGATION"
        )
        # Source packs the user confirmed in the picker. Absent
        # means "use the recommendation", which is what the mode did before
        # the picker existed — so an older client keeps working. A list of
        # strings: the send's request model refuses anything else.
        _raw_packs = data.get("source_pack_slugs")
        if _raw_packs is not None:
            # ABSENT means "use the server's recommendation". EMPTY means
            # the caller confirmed nothing, which is not the same thing:
            # collapsing it to None widened an explicit "none of these"
            # into every recommended pack — the exact opposite of the
            # request, on the one feature whose premise is that a run
            # never queries what the user did not confirm. The turn
            # refuses an empty selection, the way it already refuses an
            # unknown pack and for the same reason.
            #
            # Stripped and capped: a stray space otherwise produces "That
            # source pack is no longer available", which sends the reader
            # looking for the wrong bug.
            # De-duplicated BEFORE the cap, so a repeated slug cannot
            # consume a slot and push a distinct one out. `plan_for`
            # already de-dupes the SOURCES, so nothing was queried twice —
            # but the run persists these slugs as its audit record of what
            # the user confirmed, and `["web-general", "web-general"]` is
            # not what anybody ticked.
            _turn_pack_slugs = list(
                dict.fromkeys(
                    p.strip()
                    for p in _raw_packs
                    if p.strip()
                )
            )[:MAX_CONFIRMED_PACKS]
        else:
            _turn_pack_slugs = None
        # The choices made under the confirmed packs (a pivot's depth, a
        # step switched off): read whole or the turn is refused.
        try:
            _turn_pack_options = pack_options_from_frame(data.get("source_pack_options"))
        except InvalidPackOptionsError as exc:
            logger.info("refused a Deep Investigation frame's pack options: %s", exc)
            await _refuse_send(
                "The investigation's choices could not be read. Confirm the sources again.", 422
            )
            return

        content = sent_message_text(data.get("content"), data.get("file_ids"))
        if not content:
            await _refuse_send("Empty message", 400)
            return

        # Per-user throttle (shared bucket with the REST send-message
        # endpoint): the 429 and its ``Retry-After`` go back as they are.
        try:
            await enforce_rate_limit(
                None,
                "chat-send",
                limit=CHAT_MESSAGES_PER_MINUTE,
                window_seconds=60,
                bucket=str(user_id),
            )
        except HTTPException as rl_exc:
            await _refuse_send(str(rl_exc.detail), rl_exc.status_code, rl_exc.headers)
            return

        # No turn starts on a session that ended since the last check.
        try:
            _turn_session = await recheck_session(_turn_session)
        except SessionEndedError as exc:
            await _close_ended_session(exc.reason)
            return

        _cancel_event = asyncio.Event()
        _wait_skip_event = asyncio.Event()
        from app.services.parent_wake import PartnerAnswerQueue

        _steering_queue = PartnerAnswerQueue(conversation_id)
        _turn_steered_texts = []
        _turn_answered_by_meta.clear()
        _turn_folded_mentions.clear()
        # Skip the turn entirely when the stream is already closed —
        # running the agent loop for a client that can never receive
        # the frames would just burn LLM tokens.
        if not await _safe_send_json({"type": "thinking"}):
            return

        # Keeps the turn's processing marker alive while it runs.
        marker_refresh_task = asyncio.create_task(_refresh_turn_marker())

        from app.core.database import async_session as session_factory

        async with session_factory() as db:
            account_rejection = await resolve_account_rejection(
                db, user_id, token_issued_at
            )
            if account_rejection is not None:
                marker_refresh_task.cancel()
                await _refuse_send(account_rejection, 401)
                return
            conv = None
            if conv_uuid is not None:
                result = await db.execute(
                    select(Conversation).where(
                        Conversation.id == conv_uuid,
                        Conversation.user_id == user_id,
                    )
                )
                conv = result.scalar_one_or_none()
            if not conv:
                marker_refresh_task.cancel()
                await _refuse_send("Conversation not found", 404)
                return

            # A group channel thread, or a turn a channel message runs,
            # refuses the send before anything is stored, with
            # the REST path's sentence.
            _turn_continuation_refusal = await web_send_refusal(conv)
            if _turn_continuation_refusal:
                marker_refresh_task.cancel()
                await _refuse_send(_turn_continuation_refusal, 409)
                return

            # A selection that ADDS a tool, knowledge base or integration
            # the caller cannot use is refused before anything is stored,
            # with the REST path's sentence.
            _turn_selection_refusal = await refused_chat_selection(
                db,
                user_id=user_id,
                conversation=conv,
                requested={
                    "mcp_server_ids": data.get("mcp_server_ids"),
                    "custom_tool_ids": data.get("custom_tool_ids"),
                    "knowledge_base_ids": data.get("knowledge_base_ids"),
                    "integration_ids": data.get("integration_ids"),
                },
            )
            if _turn_selection_refusal:
                marker_refresh_task.cancel()
                await _refuse_send(_turn_selection_refusal, 400)
                return

            # ── Resolve file attachments BEFORE storing the user ──
            # message so ``metadata_json`` can carry the attachment
            # list.  This is what allows ``get_conversation_history``
            # to rehydrate FileRef objects on later turns so the LLM
            # keeps access to files previously sent in the chat.
            turn_file_refs: list | None = None
            turn_resolved_file_ids: list[uuid.UUID] = []
            turn_attachment_meta_json: str | None = None
            # A list of strings: the send's request model refuses anything else.
            turn_file_ids = data.get("file_ids") or []
            if turn_file_ids:
                from app.llm.base import FileRef
                from app.models.file_attachment import FileAttachment

                turn_file_refs = []
                turn_attachments_list: list[dict] = []
                for fid in turn_file_ids:
                    try:
                        fid_uuid = uuid.UUID(fid)
                    except (ValueError, TypeError):
                        logger.warning(
                            "Web chat send: ignoring malformed file_id %r "
                            "(conversation=%s, user=%s)",
                            fid,
                            conversation_id,
                            user_id,
                        )
                        continue
                    try:
                        fa_result = await db.execute(
                            select(FileAttachment).where(
                                FileAttachment.id == fid_uuid,
                                FileAttachment.user_id == user_id,
                            )
                        )
                        fa = fa_result.scalar_one_or_none()
                    except Exception:
                        logger.warning(
                            "Web chat send: FileAttachment lookup failed for %s",
                            fid,
                            exc_info=True,
                        )
                        fa = None
                    if not fa:
                        logger.warning(
                            "Web chat send: file_id %s not found or not owned "
                            "by user (conversation=%s, user=%s)",
                            fid,
                            conversation_id,
                            user_id,
                        )
                        continue
                    turn_attachments_list.append(
                        {
                            "filename": fa.filename,
                            "type": _attachment_type_label(
                                fa.filename, fa.content_type
                            ),
                            "size": fa.size,
                            "file_id": str(fa.id),
                            "content_type": fa.content_type,
                        }
                    )
                    turn_file_refs.append(FileRef.from_attachment(fa))
                    turn_resolved_file_ids.append(fa.id)
                if turn_attachments_list:
                    turn_attachment_meta_json = json.dumps(
                        {"attachments": turn_attachments_list}
                    )

            # Conversations referenced with @ - the REST path's
            # rule: only what the sender can open, stored as references.
            from app.services.conversation_references import (
                REFERENCED_CONVERSATIONS_FIELD,
                build_reference_brief,
                resolve_referenced_conversations,
                with_conversation_refs,
            )

            turn_reference_resolution = await resolve_referenced_conversations(
                db,
                user_id,
                data.get(REFERENCED_CONVERSATIONS_FIELD),
                current_conversation_id=conv.id,
            )
            turn_reference_brief = await build_reference_brief(db, turn_reference_resolution)
            turn_attachment_meta_json = with_conversation_refs(
                turn_attachment_meta_json, turn_reference_resolution
            )
            # Video Studio: the scene on screen stays with the request (the REST path's rule).
            if conv.video_project_id is not None:
                from app.services.video_studio.studio_feedback import with_studio_scene

                turn_attachment_meta_json = with_studio_scene(turn_attachment_meta_json, data.get("studio_scene"))

            # Channel turns wait until this send has its turn or its
            # fold (#5134; see the REST path).
            (
                _turn_channel_hold,
                _turn_continuation_refusal,
            ) = await hold_off_channel_turns(conv.id)
            if _turn_continuation_refusal:
                marker_refresh_task.cancel()
                await _refuse_send(_turn_continuation_refusal, 409)
                return

            turn_user_msg = await store_message(
                db,
                conv.id,
                role="user",
                content=content,
                channel="web",
                metadata_json=turn_attachment_meta_json,
            )

            # Link the FileAttachment rows to the stored user message
            # so later queries (and ``message_id`` joins) can find them.
            # Same observability contract as the REST ``send_message``
            # path and the round-1 assistant-side linkage fix: a silent
            # ``contextlib.suppress`` here would let the user response
            # carry a message-scoped download URL that 404s while the
            # file itself remains reachable via the legacy
            # ``/chat/files/{id}/download`` URL (owner-by-``user_id``
            # check inside ``_authorize_file_access``). Logging means
            # the orphan row shows up in audit tooling.
            if turn_resolved_file_ids:
                from app.models.file_attachment import FileAttachment

                for file_uuid in turn_resolved_file_ids:
                    try:
                        await db.execute(
                            update(FileAttachment)
                            .where(
                                FileAttachment.id == file_uuid,
                                FileAttachment.user_id == user_id,
                            )
                            .values(message_id=turn_user_msg.id)
                        )
                    except Exception:
                        logger.exception(
                            "Failed to link user-uploaded file %s to "
                            "web chat message %s (conversation=%s); "
                            "message-scoped download URL will 404 but "
                            "the legacy /chat/files/{id}/download "
                            "fallback still works via owner check.",
                            file_uuid,
                            turn_user_msg.id,
                            conv.id,
                        )

            _turn_needs_ai_title = conv.title == "New conversation"
            if _turn_needs_ai_title:
                conv.title = content[:200]
                _turn_provisional_title = conv.title

            # Commit user message before the LLM call to release the
            # row lock on conversations.updated_at (same rationale as
            # the REST send_message endpoint).
            await db.commit()

            # Capture immutable ORM attributes into locals (same
            # rationale as the REST send_message endpoint).
            conv_id = conv.id
            conv_agent_id = conv.agent_id
            # The agent standing in for the General Assistant answers a
            # Slack or Teams conversation it owns here too.
            _turn_stand_in = await turn_stand_in(
                db,
                agent_id=conv_agent_id,
                conversation_channel=getattr(conv, "channel", None),
                user_id=user_id,
            )
            _turn_stand_in_id = _turn_stand_in.id if _turn_stand_in is not None else None
            if _turn_stand_in_id is not None:
                conv_agent_id = _turn_stand_in_id

            # The user message is committed: show it on the
            # conversation's other streams now — whether this
            # send becomes the turn or is folded into a running one.
            chat_turn_stream.publish_user_message(str(conv_id), turn_user_msg, origin=_origin)

            # ── Single-flight turn lock ────────────────────
            # Become the conversation's one in-flight turn, or fold
            # this message into the turn that is already running.
            # Without this, a message sent after a page reload (or
            # from a second tab / another replica) while the previous
            # turn was still running spawned a SECOND concurrent
            # agent loop on the same conversation.
            from app.services import chat_turn_guard

            # Only the RESOLVED attachment ids (unresolvable ids were
            # dropped with a warning above) — keeps the fold payload
            # aligned with the persisted user message's attachments.
            _turn_fold_file_ids = [
                str(ref.file_id)
                for ref in (turn_file_refs or [])
                if getattr(ref, "file_id", None)
            ]
            # A purged wake's files (#3656; see the REST path).
            _turn_purged_context_ids: list[str] = []
            # The folds this acquisition purged that name agents (#4650;
            # see the REST path).
            _turn_purged_mentions: list = []
            # The turn a fold goes into, read by the fold itself.
            _turn_accepted_by: list[str] = []
            _turn_token, _turn_folded = await chat_turn_guard.acquire_or_fold(
                str(conv_id),
                content,
                file_ids=_turn_fold_file_ids,
                purged_context_file_ids=_turn_purged_context_ids,
                # Folded, the message is steering for the agent already
                # answering: the other agents it @mentions ride the fold
                # and answer it once that turn's own agents have.
                mentioned_agent_ids=folded_mention_ids(
                    data.get(MENTIONED_AGENTS_FIELD), conv_agent_id
                ),
                purged_mentions=_turn_purged_mentions,
                accepted_by=_turn_accepted_by,
            )
            _turn_started_at = datetime.now(UTC)
            await release_channel_hold(conv_id, _turn_channel_hold)
            if _turn_token is None:
                # Folded: the running turn answers this message —
                # or, in the pathological alternation case
                # (``_turn_folded`` False, logged inside the guard),
                # requeued for the next turn's bridge.  Tell the
                # client, then relay that turn to its end
                # (``_relay_folded_turn``).
                if _turn_folded:
                    logger.info(
                        "Folded message into the in-flight turn for "
                        "conversation %s instead of starting a "
                        "concurrent agent loop",
                        conv_id,
                    )
                marker_refresh_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await marker_refresh_task
                await _drop_kept_stop()
                _fold_frame: dict = {
                    "type": "folded_into_active_turn",
                    "conversation_id": str(conv_id),
                    # False on the (practically unreachable)
                    # requeue fallback: the persisted message
                    # shapes the next turn instead of being
                    # injected into a currently running one.
                    "folded": _turn_folded,
                }
                # The turn that answers the folded message: its frames follow
                # this frame on this stream (_relay_folded_turn starts after it).
                _fold_turn_id = await chat_turn_stream.fold_turn_id(
                    str(conv_id),
                    _turn_accepted_by[0] if _turn_accepted_by else None,
                    folded=_turn_folded,
                )
                if _fold_turn_id:
                    _fold_frame["turn_id"] = _fold_turn_id
                await _safe_send_json(_fold_frame)
                if _turn_folded and _turn_accepted_by:
                    # Closed first: the turn relayed can run for minutes.
                    await db.close()
                    await _relay_folded_turn(_turn_accepted_by[0])
                return

            # The turn is ours, so the conversation really is moving on:
            # retire anything still pending (#173, FR-002c4). Deliberately
            # AFTER the fold check, exactly as the REST path documents.
            # Before it, a send that folded into somebody else's in-flight
            # turn wiped that turn's pending set — and on this surface the
            # proposals live only in Redis, so that was their single copy:
            # the paused turn stayed blocked in ``await_approval_decision``,
            # its TTL refresh a no-op on a deleted key, a reload showed no
            # prompt, and there was no REST resume to fall back on.
            from app.services import chat_approval_resume as _turn_appr

            await _turn_appr.retire_for_new_turn(db, conv.id, surface="web_stream")

            # Names the turn this send started, before any of its frames.
            await _safe_send_json(
                {
                    "type": "turn_started",
                    "turn_id": chat_turn_stream.public_turn_id(_turn_token),
                }
            )
            # A Stop this client sent before the lock was ours found no
            # turn and was kept for this send: the turn ends as that Stop
            # would have ended it. Checked once the connection is
            # reachable, so a Stop sent from here on reaches it directly.
            try:
                if await chat_turn_guard.consume_pending_cancel(str(conv_id), _origin):
                    _cancel_event.set()
                    _wait_skip_event.set()
            except Exception:
                logger.warning(
                    "Could not read the Stop kept for the send of conv %s",
                    conv_id,
                    exc_info=True,
                )

            # Hand the key to the keepalive task, which refreshes the
            # TTL every ~20 s while the turn runs.  Cleared on
            # every exit path below.
            _active_processing_key = chat_processing_key(conv_id)

            # This stream owns the turn: fan its frames out to the
            # conversation's other streams and keep the resumable
            # journal. From here every frame ``_safe_send_json``
            # sends is also published; the terminal frame closes it.
            _turn_publisher = chat_turn_stream.TurnPublisher(
                str(conv_id), _turn_token, origin=_origin
            )
            _turn_publisher.start()

            # A purged fold naming agents: its text is in the
            # history this turn loads, its agents answer it after this
            # turn's own - or it is handed back on any exit.
            for _turn_purged in _turn_purged_mentions:
                _turn_purged_refs: list = []
                if _turn_purged.file_ids:
                    with contextlib.suppress(Exception):
                        _turn_purged_refs, _ = await _resolve_steering_files(
                            db, _turn_purged.file_ids, user_id, str(conv_id)
                        )
                _turn_purged_owed = folded_mentions(_turn_purged, _turn_purged_refs)
                if _turn_purged_owed is not None:
                    _turn_folded_mentions.append(_turn_purged_owed)

            # Bridge task: texts folded into this turn from other
            # streams or replicas land in the Redis steering inbox —
            # feed them into this turn's in-process steering queue so
            # the agent loop injects them at iteration boundaries,
            # exactly like a steer handed over in process.  Appends to
            # ``_turn_steered_texts`` in the same synchronous step as
            # ``put_nowait`` to preserve the fold-in replay invariant
            # (queue contents == tail of the list).
            _turn_bridge_stop = asyncio.Event()

            async def _steering_inbox_bridge(
                # Per-turn constants bound at task creation (B023-safe).
                # ``_turn_steered_texts`` is intentionally read late via
                # the enclosing scope: the fold-in loop REBINDS it to a
                # fresh list between passes and the bridge must append
                # to the current one (see the replay invariant).
                _bridge_conv_id: str = str(conv_id),
                _bridge_queue: asyncio.Queue = _steering_queue,
                _bridge_file_context=None,
                _bridge_skip_wait: asyncio.Event | None = _wait_skip_event,
                _bridge_cancel: asyncio.Event | None = _cancel_event,
                _bridge_token: str | None = _turn_token,
                _bridge_folded: list = _turn_folded_mentions,
                _bridge_local: list = _turn_local_steers,
                _bridge_wake: asyncio.Event = _turn_steer_wake,
                _bridge_stop: asyncio.Event = _turn_bridge_stop,
            ) -> None:
                from app.core.database import async_session as _bridge_session
                from app.services.parent_wake import (
                    without_stopped_partner_answers,
                )
                from app.services.sub_agent_files import (
                    register_context_files as _register_context_files,
                )
                from app.tools.agent_loop import SteeringMessage

                while True:
                    # A steer handed over in this process wakes the
                    # bridge at once; the inbox is polled.
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            _bridge_wake.wait(),
                            timeout=chat_turn_guard.BRIDGE_POLL_SECONDS,
                        )
                    _bridge_wake.clear()
                    if _bridge_stop.is_set():
                        return
                    # Cross-process cancel: a Stop that did not
                    # reach this process directly (another replica) lands here — the request is
                    # bound to this turn's token, so a stale request for
                    # a dead turn is discarded instead of killing us.
                    if _bridge_cancel is not None and _bridge_token:
                        try:
                            if await chat_turn_guard.consume_turn_cancel(
                                _bridge_conv_id, _bridge_token
                            ):
                                logger.info(
                                    "Cross-process cancel received for "
                                    "the in-flight turn of conversation "
                                    "%s",
                                    _bridge_conv_id,
                                )
                                _bridge_cancel.set()
                                if _bridge_skip_wait is not None:
                                    _bridge_skip_wait.set()
                        except Exception:
                            logger.debug(
                                "Cross-process cancel check failed for "
                                "%s",
                                _bridge_conv_id,
                                exc_info=True,
                            )
                    # "Continue now" sent to another replica (the web
                    # stream's skip-wait), addressed to this turn like
                    # the cancel above.
                    if _bridge_skip_wait is not None and _bridge_token:
                        try:
                            if await chat_turn_guard.consume_wait_skip(
                                _bridge_conv_id, _bridge_token
                            ):
                                _bridge_skip_wait.set()
                        except Exception:
                            logger.debug(
                                "Cross-process skip-wait check failed for %s",
                                _bridge_conv_id,
                                exc_info=True,
                            )
                    # A local steer stays in ``_bridge_local`` until it is
                    # queued: the leftover check takes the ones still
                    # there, and they are then skipped here.
                    _local_batch = list(_bridge_local)
                    _payloads = list(_local_batch)
                    try:
                        _payloads += await chat_turn_guard.drain_steering_inbox(
                            _bridge_conv_id
                        )
                    except Exception:
                        logger.debug(
                            "Steering inbox drain failed for %s",
                            _bridge_conv_id,
                            exc_info=True,
                        )
                        if not _payloads:
                            continue
                    # Not injected: a partner's answer whose work the user
                    # stopped (#4577; see the REST bridge).
                    _payloads = await without_stopped_partner_answers(
                        _bridge_conv_id, _payloads
                    )
                    for _p in _payloads:
                        _p_refs: list = []
                        if _p.file_ids:
                            # Attachments folded from another stream —
                            # resolve them so the running loop injects a
                            # true multimodal steering message.
                            try:
                                async with _bridge_session() as _bdb:
                                    _p_refs, _ = await _resolve_steering_files(
                                        _bdb,
                                        _p.file_ids,
                                        user_id,
                                        _bridge_conv_id,
                                    )
                            except Exception:
                                logger.warning(
                                    "Failed to resolve folded steering "
                                    "attachments for %s",
                                    _bridge_conv_id,
                                    exc_info=True,
                                )
                        if _p_refs and _bridge_file_context is not None:
                            for _pref in _p_refs:
                                _bridge_file_context.add(_pref)
                        # A background task's files folded by a parent
                        # wake: registered in the context, never put on
                        # the steering message (#3656; see the REST
                        # bridge).
                        await _register_context_files(
                            _bridge_file_context,
                            _p.context_file_ids,
                            user_id=user_id,
                        )
                        # The agents it @mentions answer it once this
                        # turn's own have.
                        _owed = folded_mentions(_p, _p_refs)
                        if _owed is not None:
                            _bridge_folded.append(_owed)
                        if _p.mentions_only:
                            # Handed back by a turn that ended first: its
                            # text is in the thread already.
                            continue
                        if any(_p is _l for _l in _local_batch):
                            if not any(_p is _l for _l in _bridge_local):
                                continue
                            _bridge_local[:] = [_l for _l in _bridge_local if _l is not _p]
                        _bridge_queue.put_fold(
                            SteeringMessage(text=_p.text, files=_p_refs)
                            if _p_refs
                            else _p.text,
                            _p,
                        )
                        _turn_steered_texts.append(_p.text)
                        if _bridge_skip_wait is not None:
                            _bridge_skip_wait.set()

            # NOTE: the bridge task is started AFTER the turn's
            # FileContext is bound (below) so folded attachments can be
            # made visible to the running loop's file tools.

            # Setup-phase visibility (P2.1): between the ``thinking``
            # ack and the loop's first event, the user previously saw a
            # static indicator through history load, compaction, memory
            # and tool assembly. Emit granular ``preparing`` frames so
            # the status bubble reflects the actual phase.
            # The turn's pre-generation timeline starts here; run_agent_chat
            # takes it and the trace stores it (``tracing/turn_stages``).
            from app.services.tracing import turn_stages as _turn_stages_mod

            _turn_stages = _turn_stages_mod.begin_turn()
            await _safe_send_json(
                {"type": "status", "status": "preparing", "phase": "history"}
            )
            history = await get_conversation_history(db, conv_id)
            _turn_stages.mark("history")

            # Strip the just-stored user message to avoid sending the
            # same turn twice (same rationale as the REST endpoint).
            if (
                history
                and history[-1].role == "user"
                and history[-1].content == content
            ):
                history = history[:-1]

            # Apply memory management for long conversations (same as REST endpoint)
            # Skip this entirely when the agent has skip_llm enabled — no LLM
            # means we don't need context compaction or memory management.
            # ``_turn_prebuilt_llm`` carries the compaction-path LLM
            # resolution forward (P3.3) so the chat service and the
            # AI-title generation don't re-resolve it.
            _turn_prebuilt_llm: tuple | None = None
            _turn_skip_llm = False
            if conv_agent_id:
                from app.models.agent import Agent as _AgentSkipCheck

                _skip_res = await db.execute(
                    select(_AgentSkipCheck.skip_llm).where(
                        _AgentSkipCheck.id == conv_agent_id
                    )
                )
                _skip_row = _skip_res.scalar_one_or_none()
                _turn_skip_llm = bool(_skip_row) if _skip_row is not None else False

            if not _turn_skip_llm:
                try:
                    from app.llm.router import get_llm_for_user as _get_llm_ws
                    from app.services.conversation_memory import (
                        compaction_context_window as _compaction_window_ws,
                    )
                    from app.services.conversation_memory import (
                        manage_conversation_memory as _manage_ws,
                    )
                    from app.services.conversation_memory import (
                        should_compact as _should_compact_ws,
                    )

                    _llm_ws, _llm_ws_model = await _get_llm_ws(db, str(user_id))
                    # Compaction visibility (P2.1): past the trigger
                    # this phase runs a summarization LLM call that can
                    # take tens of seconds — tell the user what is
                    # happening instead of leaving "Thinking...".
                    # ``should_compact`` is the same predicate the engine
                    # uses, measured against the same model's context
                    # window, so the frame is only sent when a compactor
                    # LLM call will actually run.
                    if _should_compact_ws(
                        history,
                        conversation_id=str(conv_id),
                        context_window=_compaction_window_ws(_llm_ws, _llm_ws_model),
                    ):
                        await _safe_send_json(
                            {
                                "type": "status",
                                "status": "preparing",
                                "phase": "compaction",
                            }
                        )
                    # LLM-resolution reuse (P3.3): the chat service and
                    # the AI-title generation below reuse this plain
                    # user resolution instead of re-resolving.
                    _turn_prebuilt_llm = (_llm_ws, _llm_ws_model)
                    _turn_agent_company_managed = False
                    _turn_agent_memory_persistent = True
                    if conv_agent_id:
                        from app.models.agent import Agent as _AgentModelWs

                        _aws_res = await db.execute(
                            select(
                                _AgentModelWs.is_company_managed,
                                _AgentModelWs.memory_persistent_enabled,
                            ).where(_AgentModelWs.id == conv_agent_id)
                        )
                        _aws_row = _aws_res.one_or_none()
                        if _aws_row is not None:
                            _turn_agent_company_managed = bool(_aws_row[0])
                            _turn_agent_memory_persistent = bool(_aws_row[1])
                    # Return the pooled connection before compaction: on
                    # compaction-cycle turns this call awaits a full LLM
                    # summarization (seconds to tens of seconds), and the
                    # reads above opened a transaction that would pin the
                    # connection for the whole call (#1973 phase 2).
                    from app.core.database import (
                        release_db_connection_before_loop as _release_ws,
                    )

                    await _release_ws(db)
                    history = await _manage_ws(
                        _llm_ws,
                        history,
                        agent_id=str(conv_agent_id) if conv_agent_id else None,
                        user_id=str(user_id),
                        is_company_managed=_turn_agent_company_managed,
                        memory_persistent_enabled=_turn_agent_memory_persistent,
                        model=_llm_ws_model,
                        conversation_id=str(conv_id),
                    )
                except Exception as _turn_mem_exc:
                    logger.warning(
                        "Chat conversation memory management failed: %s",
                        format_exception_message(_turn_mem_exc),
                        exc_info=True,
                    )
            _turn_stages.mark("compaction")

            # The tool work of a turn that ended before it stored its
            # answer goes right before this message, past the
            # compaction, which keys its watermark on stored rows.
            # The resume note names attachments/ only when this turn can call the code interpreter (#2677).
            _turn_resumed, _turn_replay = await turn_checkpoint.load_replay(
                db,
                conv_id,
                user_id=user_id,
                code_interpreter=await _turn_offers_code_interpreter(db, conv, user_id),
            )
            if _turn_replay:
                history = [*history, *_turn_replay]
            _turn_checkpoint = turn_checkpoint.TurnCheckpoint(
                conv_id,
                turn_id=(
                    chat_turn_stream.public_turn_id(_turn_token)
                    if _turn_token
                    else None
                ),
                resumed=_turn_resumed,
                user_id=str(user_id),
            )
            _turn_keep_checkpoint = False
            # The id of this turn's trace, named by its cut record.
            _turn_trace_link = arm_root_trace_link()

            from app.tools.base import FileContext, files_for_reply

            _turn_segment_texts.clear()
            _turn_file_context = FileContext.from_file_refs(turn_file_refs or [])
            _turn_initial_file_ids: set[str] = {
                ref.file_id
                for ref in _turn_file_context.list_files()
                if getattr(ref, "file_id", None)
            }
            # Every stored file of the conversation, whatever compaction
            # or a resumed turn kept of its messages.
            from app.services.conversation_files import (
                conversation_file_refs as _turn_conversation_file_refs,
            )
            from app.services.conversation_files import (
                register_conversation_files as _turn_register_conversation_files,
            )

            _turn_register_conversation_files(
                _turn_file_context,
                await _turn_conversation_file_refs(conv_id),
                _turn_initial_file_ids,
            )
            for _hist_msg in history:
                if _hist_msg.files:
                    for _hist_ref in _hist_msg.files:
                        _turn_file_context.add(_hist_ref)
                        if getattr(_hist_ref, "file_id", None):
                            _turn_initial_file_ids.add(_hist_ref.file_id)
            if _turn_purged_context_ids:
                # A purged wake's files (#3656; see the REST path):
                # registered, not in the initial set, so this turn's
                # reply attaches them.
                from app.services.sub_agent_files import (
                    register_context_files as _turn_register_context_files,
                )

                await _turn_register_context_files(
                    _turn_file_context, _turn_purged_context_ids, user_id=user_id
                )

            # Start the steering bridge now that
            # the turn's FileContext exists — folded attachments are
            # added to it so the loop's file tools can access them.
            _turn_steer_bridge_task = asyncio.create_task(
                _steering_inbox_bridge(
                    _bridge_file_context=_turn_file_context,
                )
            )
            conn.accept_steering(_hand_steer_to_bridge)

            # What the conversation agent's run was charged on a turn
            # several @mentioned agents answer: a refusal asks
            # none of them.
            _turn_metering = TurnMetering()
            # The trace of the turn's final message on a multi-agent turn
            # (the run that wrote it, never merely the latest one).
            _turn_final_trace: dict[str, str] | None = None
            try:
                # Extract optional session tool overrides from the send.
                # Use a plain ``.get()`` (no ``or None`` coercion) so an
                # explicit empty selection (``[]`` — the user disabled every
                # tool in that category) is preserved and a truly-absent field
                # stays ``None``. ``run_agent_chat`` / ``run_general_chat`` and
                # ``build_tool_overrides_payload`` both key on ``is not None``:
                # ``[]`` means "no tools" (honoured + persisted), ``None`` means
                # "use the agent defaults". Coercing ``[]`` to ``None`` (the old
                # behaviour) silently ignored a disable-all selection at runtime
                # and skipped persisting it — and diverged from the REST path,
                # which passes ``body.*`` (Pydantic, ``[]`` preserved) as-is.
                turn_builtin_ids = data.get("builtin_tool_ids")
                turn_custom_tool_ids = data.get("custom_tool_ids")
                turn_integration_ids = data.get("integration_ids")
                turn_mcp_server_ids = data.get("mcp_server_ids")
                turn_knowledge_base_ids = data.get("knowledge_base_ids")
                turn_default_tool_ids = data.get("default_tool_ids")
                turn_scope_headers = data.get("scope_headers") or None

                # File attachments (``turn_file_refs`` / ``turn_file_ids``)
                # were resolved and linked to the stored user message
                # above — before ``db.commit()``.

                _turn_transfer_agent_id: str | None = None
                _turn_transfer_agent_name: str | None = None
                _turn_transfer_source_id: str | None = None
                # Machine-readable failure envelope for the ``done``
                # frame — mirrors the REST SendMessageResponse.error(_code)
                # so a skip_llm pipeline refusal (e.g. Filigran AI denied)
                # is not a silent success over the stream either. Populated
                # by ``run_agent_chat``; stays empty on success.
                _turn_error_out: dict[str, str] = {}

                # Presentation Studio: the deck this conversation
                # is bound to, resolved per turn against the caller's
                # current visibility, plus the slide on their screen.
                # ``None`` when unbound or no longer readable. Resolved
                # AFTER the turn state above is bound: the ``done`` frame
                # built past the ``except`` below reads
                # ``_turn_transfer_agent_id``, so a lookup that raises before
                # that name exists would turn the friendly error frame
                # into an ``UnboundLocalError`` and a 1011 close.
                _turn_studio = await load_studio_context(
                    db,
                    user_id=user_id,
                    presentation_id=conv.presentation_id,
                    focused_index=data.get("studio_slide"),
                )
                # Video Studio: the video this conversation
                # edits, the same way, plus the scene on screen.
                if _turn_studio is None and conv.video_project_id is not None:
                    from app.services.video_studio.studio import (
                        load_video_studio_context,
                    )

                    _turn_studio = await load_video_studio_context(
                        db,
                        user_id=user_id,
                        video_project_id=conv.video_project_id,
                        focused_scene=data.get("studio_scene"),
                    )
                # Dashboard Studio: the dashboard this conversation
                # builds, the same way, plus the widget selected on it.
                if _turn_studio is None and conv.custom_dashboard_id is not None:
                    from app.services.dashboard_studio import (
                        load_dashboard_studio_context,
                    )

                    _turn_studio = await load_dashboard_studio_context(
                        db,
                        user_id=user_id,
                        dashboard_id=conv.custom_dashboard_id,
                        focused_widget=data.get("studio_widget"),
                    )
                # Fleet page Modify with AI: the fleet this
                # conversation changes, the same way.
                if _turn_studio is None and conv.fleet_id is not None:
                    from app.services.fleet_studio import load_fleet_studio_context

                    _turn_studio = await load_fleet_studio_context(db, user_id=user_id, fleet_id=conv.fleet_id)
                # XTM One Desktop: the machine this message was
                # typed on, when the caller may target it. Same fail-open
                # contract as the studio binding above.
                _turn_desktop = await load_desktop_context(
                    db,
                    user_id=user_id,
                    runner_id=data.get("desktop_runner_id"),
                )

                # @mentions: the other agents this message names
                # answer after the conversation's own, in mention order,
                # once the fold-in loop below is done. Fail-open: a lookup
                # error leaves the turn exactly as it is without mentions.
                try:
                    _turn_mention_targets = await resolve_mention_targets(
                        db,
                        user_id=user_id,
                        raw_ids=data.get(MENTIONED_AGENTS_FIELD),
                        answering_agent_id=(
                            str(conv_agent_id) if conv_agent_id else None
                        ),
                    )
                except Exception:
                    logger.warning(
                        "Could not resolve the mentioned agents of a "
                        "message in conversation %s; only the "
                        "conversation's agent answers",
                        conv_id,
                        exc_info=True,
                    )
                    _turn_mention_targets = []
                # What every mentioned agent answers: the message as sent,
                # against the thread as it stood (the fold-in loop below
                # rebinds ``content`` / ``history`` / ``turn_file_refs``).
                _turn_content = content
                _turn_history = list(history)
                _turn_file_refs = turn_file_refs
                _turn_primary_failed = False

                # The per-conversation tool selection is saved by the pass
                # that runs an agent or the General Assistant, once per
                # message: a turn no agent answers (one not found or
                # disabled, a deep investigation) leaves the saved one as it
                # was. Committed at once rather than autoflushed by the
                # turn's first SELECT, which would hold the row lock through
                # the pre-loop setup (tool/MCP/KB loading) and block the
                # steer route's separate-session persist (it bumps
                # conversations.updated_at).
                _turn_selection_saved = False

                async def _save_turn_selection() -> None:
                    nonlocal _turn_selection_saved
                    if _turn_selection_saved:
                        return
                    _turn_selection_saved = True
                    if persist_tool_selection(
                        conv,
                        builtin_tool_ids=turn_builtin_ids,
                        custom_tool_ids=turn_custom_tool_ids,
                        integration_ids=turn_integration_ids,
                        mcp_server_ids=turn_mcp_server_ids,
                        knowledge_base_ids=turn_knowledge_base_ids,
                        default_tool_ids=turn_default_tool_ids,
                        scope=data.get("tool_selection_scope"),
                    ):
                        await db.commit()

                # Mid-run steering fold-in loop.  Each pass runs one full
                # chat turn.  Steering messages that arrive while the
                # agent loop is mid-iteration are injected directly by
                # ``run_agent_loop``; messages that arrive too late (e.g.
                # during the final answer stream, or on the direct
                # no-tools path) remain in ``_steering_queue`` — the
                # leftover check at the bottom stores the completed
                # segment, surfaces it to the client, and immediately
                # runs a follow-up pass so a steering message is never
                # silently dropped.
                while True:
                    # Each pass is its own charge: the last one's outcome
                    # decides whether the mentioned agents are asked
                    #.
                    _turn_metering.quota.clear()
                    _turn_primary_failed = False
                    if _turn_deep_investigation:
                        # Routed away from the agent loop entirely:
                        # the orchestrator owns the turn, and its
                        # `investigation_*` frames ride `_on_event` — the
                        # same channel the loop uses for `tool_start`, so
                        # no new transport is involved. There is no model
                        # to steer: a message sent meanwhile is a leftover,
                        # answered by an ordinary follow-up pass below.
                        #
                        # Everything the turn does — metering, the quota
                        # gate, the run, a message for every failure —
                        # lives in the service so it can be TESTED. Only
                        # the cancel listener stays here: it reads the frames
                        # the routes inject into this connection.
                        _cancel_listener = asyncio.create_task(
                            _listen_during_processing()
                        )
                        try:
                            assistant_text = (
                                await run_deep_investigation_turn(
                                    db,
                                    subject=content,
                                    user_id=user_id,
                                    conversation_id=conv_id,
                                    on_event=_on_event,
                                    source_pack_slugs=_turn_pack_slugs,
                                    pack_options=_turn_pack_options,
                                    cancel_event=_cancel_event,
                                )
                            )
                        finally:
                            # Same teardown as the agent branches: leaving
                            # the listener up would keep consuming frames
                            # meant for the outer loop's next turn.
                            _cancel_listener.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await _cancel_listener
                    elif conv_agent_id:
                        from app.models.agent import Agent
                        from app.services.agent_chat import run_agent_chat

                        agent_result = await db.execute(
                            select(Agent).where(Agent.id == conv_agent_id)
                        )
                        agent = agent_result.scalar_one_or_none()
                        if not agent:
                            assistant_text = "Agent not found."
                            _turn_primary_failed = True
                        elif not agent.enabled:
                            assistant_text = "This agent is currently disabled."
                            _turn_primary_failed = True
                        else:
                            await _save_turn_selection()
                            # Background task handling cancel + steering
                            # frames while the agent loop is running.
                            _cancel_listener = asyncio.create_task(
                                _listen_during_processing()
                            )
                            # Fresh envelope per pass so a prior steering
                            # pass's error never leaks into this frame.
                            _turn_error_out.clear()
                            # Investigations this turn ran, if the agent
                            # has the `deep_investigation` tool. The block
                            # is attached from this list below rather than
                            # from a marker the model was asked to echo.
                            _turn_investigations: list[str] = []
                            try:
                                assistant_text, transfer_agent_id = (
                                    await run_agent_chat(
                                        db,
                                        str(user_id),
                                        agent,
                                        history,
                                        content,
                                        conversation_id=str(conv_id),
                                        on_event=_on_event,
                                        request_approval=(
                                            _turn_request_approval
                                            if _turn_supports_approval["value"]
                                            else None
                                        ),
                                        impersonated_by=_turn_impersonated_by,
                                        on_token=_on_token,
                                        error_out=_turn_error_out,
                                        files=turn_file_refs or None,
                                        file_context=_turn_file_context,
                                        session_builtin_tool_ids=turn_builtin_ids,
                                        session_custom_tool_ids=turn_custom_tool_ids,
                                        session_integration_ids=turn_integration_ids,
                                        session_mcp_server_ids=turn_mcp_server_ids,
                                        session_knowledge_base_ids=turn_knowledge_base_ids,
                                        scope_headers=turn_scope_headers,
                                        cancel_event=_cancel_event,
                                        wait_skip_event=_wait_skip_event,
                                        steering_queue=_steering_queue,
                                        # The web chat is the one surface that
                                        # persists mid-turn reports as
                                        # completed segments —
                                        # see the ``segment_complete``
                                        # branch in ``_on_event``.
                                        deliver_segments=True,
                                        on_checkpoint=_turn_checkpoint.offer,
                                        prebuilt_llm=_turn_prebuilt_llm,
                                        investigations_out=_turn_investigations,
                                        presentation_studio=_turn_studio,
                                        desktop=_turn_desktop,
                                        # Whether the quota charged or
                                        # refused this run: a
                                        # refusal asks no mentioned agent,
                                        # a folded message's either.
                                        quota_out=(
                                            _turn_metering.quota
                                            if _turn_mention_targets or _turn_folded_mentions
                                            else None
                                        ),
                                        unavailable_integrations_out=_turn_unavailable_integrations,
                                        reference_brief=turn_reference_brief,
                                        agents_after=_turn_mention_targets,
                                    )
                                )
                            except Exception as _turn_primary_exc:
                                # Mentioned agents still to answer,
                                # a folded message's too: the
                                # failure becomes this agent's note and
                                # they answer. Alone, the turn fails as
                                # it always has.
                                if not (_turn_mention_targets or _turn_folded_mentions):
                                    raise
                                logger.error(
                                    "Chat turn error: %s",
                                    format_exception_message(_turn_primary_exc),
                                    exc_info=True,
                                )
                                assistant_text = (
                                    "I encountered an error: "
                                    f"{_friendly_error_message(_turn_primary_exc)}"
                                )
                                transfer_agent_id = None
                                _turn_primary_failed = True
                                _turn_keep_checkpoint = True
                            finally:
                                _cancel_listener.cancel()
                                with contextlib.suppress(asyncio.CancelledError):
                                    await _cancel_listener

                            # Attach the investigation panel(s). Appended
                            # here, from what the tool actually ran, so a
                            # model that never mentions the run cannot
                            # cost the reader the panel — and one that
                            # hallucinates a marker cannot conjure one.
                            if _turn_investigations:
                                from app.services.investigation_events import (
                                    build_investigation_marker,
                                )

                                # Reconcile what the agent WROTE against
                                # the sources the run actually produced.
                                # The synthesis validates its own draft;
                                # nothing validated the agent that then
                                # rewrote it, and on the first real run
                                # that agent cited [14], [16] and [17]
                                # against a key stopping at [13] — three
                                # citations resolving to nothing, in prose
                                # that looked checkable.
                                assistant_text = await _reconcile_investigation_citations(
                                    db, assistant_text, _turn_investigations
                                )

                                _markers = [
                                    build_investigation_marker(_iid)
                                    for _iid in _turn_investigations
                                    if build_investigation_marker(_iid) not in assistant_text
                                ]
                                if _markers:
                                    assistant_text = "\n\n".join(
                                        [assistant_text.rstrip(), *_markers]
                                    )

                            if transfer_agent_id:
                                # The agent that ran, the one standing in included.
                                _turn_transfer_source_id = str(conv_agent_id)
                                conv.transfer_source_agent_id = conv_agent_id
                                conv.agent_id = uuid.UUID(transfer_agent_id)
                                _turn_transfer_agent_id = transfer_agent_id
                                _t_res = await db.execute(
                                    select(Agent.name).where(
                                        Agent.id == uuid.UUID(transfer_agent_id)
                                    )
                                )
                                _turn_transfer_agent_name = _t_res.scalar_one_or_none()
                                # Commit the rebind NOW: a follow-up
                                # steering pass would otherwise autoflush
                                # this pending Conversation UPDATE on its
                                # first SELECT and hold the row lock
                                # through the next pass's pre-loop setup,
                                # blocking the steer route's
                                # separate-session persist (it bumps
                                # conversations.updated_at).
                                await db.commit()
                    else:
                        # General chat with agent handover capability
                        from app.services.general_chat import run_general_chat

                        await _save_turn_selection()
                        _cancel_listener = asyncio.create_task(
                            _listen_during_processing()
                        )
                        try:
                            assistant_text, new_agent_id = await run_general_chat(
                                db,
                                str(user_id),
                                history,
                                content,
                                conversation_id=str(conv_id),
                                request_approval=(
                                    _turn_request_approval
                                    if _turn_supports_approval["value"]
                                    else None
                                ),
                                impersonated_by=_turn_impersonated_by,
                                session_builtin_tool_ids=turn_builtin_ids,
                                session_custom_tool_ids=turn_custom_tool_ids,
                                session_integration_ids=turn_integration_ids,
                                session_mcp_server_ids=turn_mcp_server_ids,
                                session_knowledge_base_ids=turn_knowledge_base_ids,
                                on_event=_on_event,
                                on_token=_on_token,
                                files=turn_file_refs or None,
                                file_context=_turn_file_context,
                                cancel_event=_cancel_event,
                                wait_skip_event=_wait_skip_event,
                                steering_queue=_steering_queue,
                                # Same opt-in as the agent path above:
                                # the General Assistant
                                # delegates and waits like any agent,
                                # so its flushed mid-turn reports are
                                # persisted as completed segments too.
                                deliver_segments=True,
                                on_checkpoint=_turn_checkpoint.offer,
                                prebuilt_llm=_turn_prebuilt_llm,
                                presentation_studio=_turn_studio,
                                desktop=_turn_desktop,
                                quota_out=(
                                    _turn_metering.quota
                                    if _turn_mention_targets or _turn_folded_mentions
                                    else None
                                ),
                                unavailable_integrations_out=_turn_unavailable_integrations,
                                reference_brief=turn_reference_brief,
                                agents_after=_turn_mention_targets,
                            )
                        except _GeneralChatPaused as _ga_hitl:
                            # Only reachable when this send's client
                            # cannot answer a prompt: ``_turn_supports_approval``
                            # stayed False and no channel was passed above.
                            # A client that can pauses mid-turn
                            # instead and never unwinds here. Left to the generic handler this
                            # reached the user as "I encountered an error"
                            # for the feature working as designed.
                            from app.tools.approval import (
                                no_approval_channel_message,
                            )

                            logger.info(
                                "General chat stopped for approval: %s",
                                _ga_hitl.question,
                            )
                            assistant_text = no_approval_channel_message(
                                _ga_hitl
                            )
                            new_agent_id = None
                        except Exception as _turn_primary_exc:
                            # Same contract as the agent path.
                            if not (_turn_mention_targets or _turn_folded_mentions):
                                raise
                            logger.error(
                                "Chat turn error: %s",
                                format_exception_message(_turn_primary_exc),
                                exc_info=True,
                            )
                            assistant_text = (
                                "I encountered an error: "
                                f"{_friendly_error_message(_turn_primary_exc)}"
                            )
                            new_agent_id = None
                            _turn_primary_failed = True
                            _turn_keep_checkpoint = True
                        finally:
                            _cancel_listener.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await _cancel_listener

                        # If handover occurred, bind the conversation to
                        # the agent. Commit immediately for the same
                        # reason as the transfer rebind above — a
                        # follow-up steering pass must not run with an
                        # open Conversation write lock.
                        if new_agent_id:
                            conv.agent_id = uuid.UUID(new_agent_id)
                            await db.commit()

                    # ── Steering leftovers → immediate follow-up turn ──
                    from app.llm.base import ChatMessage as _SteerMsg
                    from app.services.parent_wake import (
                        without_stopped_partner_answers,
                    )
                    from app.services.sub_agent_files import (
                        register_context_files as _register_context_files,
                    )
                    from app.tools.agent_loop import (
                        SteeringMessage as _SteeringMessage,
                    )
                    from app.tools.agent_loop import (
                        drain_steering_queue,
                        steering_text,
                    )

                    # The in-process hand-off closes while this check
                    # looks (re-opened for a follow-up pass): the steer
                    # route refuses a steer from now on, so the client
                    # sends it as the next turn, and the ones it handed
                    # over that the bridge has not queued are taken here
                    # (the bridge then skips them). The teardown answers
                    # none of them.
                    conn.accept_steering(None)
                    _unqueued_local = list(_turn_local_steers)
                    _turn_local_steers.clear()
                    _leftover = drain_steering_queue(_steering_queue)
                    # The stored steers among them, read before the admit
                    # below forgets their inbox entries: moved after the
                    # answer they came too late to shape.
                    _leftover_message_ids: list[str] = _steering_queue.message_ids_of(
                        _leftover
                    )
                    _turn_cancelled = (
                        _cancel_event is not None and _cancel_event.is_set()
                    )
                    if not _turn_cancelled:
                        # A Stop sent through another replica reaches this
                        # turn at the bridge's next poll: a partner's
                        # answer the bridge queued before it is not
                        # followed up meanwhile.
                        _leftover = await _admit_leftover_steers(
                            _steering_queue, _leftover, _turn_steered_texts
                        )
                    if not _leftover and not _turn_cancelled:
                        # Folds the bridge hasn't picked up yet: a
                        # message sent from a reloaded page, a second tab or
                        # another replica while this turn was streaming its
                        # final answer.  Drained here so it triggers the same
                        # follow-up pass as a leftover in the queue.  Extend the replay
                        # list too — leftovers must stay the tail of
                        # ``_turn_steered_texts`` for the injected-
                        # prefix arithmetic below.
                        _remote_payloads = [
                            *_unqueued_local,
                            *await chat_turn_guard.drain_steering_inbox(
                                str(conv_id)
                            ),
                        ]
                        _unqueued_local = []
                        # No follow-up pass for a partner's answer whose
                        # work the user stopped: the Stop may have
                        # landed after this pass returned and before the
                        # bridge read the cancel request.
                        _remote_payloads = await without_stopped_partner_answers(
                            str(conv_id), _remote_payloads
                        )
                        if _remote_payloads:
                            _remote_leftover: list = []
                            for _rp in _remote_payloads:
                                _rp_refs: list = []
                                if _rp.file_ids:
                                    with contextlib.suppress(Exception):
                                        (
                                            _rp_refs,
                                            _,
                                        ) = await _resolve_steering_files(
                                            db,
                                            _rp.file_ids,
                                            user_id,
                                            str(conv_id),
                                        )
                                if _rp_refs and _turn_file_context is not None:
                                    for _rp_ref in _rp_refs:
                                        _turn_file_context.add(_rp_ref)
                                # A parent wake's task files:
                                # registered for the follow-up pass, never
                                # inlined (see the REST bridge).
                                await _register_context_files(
                                    _turn_file_context,
                                    _rp.context_file_ids,
                                    user_id=user_id,
                                )
                                # Its @mentioned agents answer it after
                                # this turn's own; a fold handed
                                # back is in the thread already.
                                _rp_owed = folded_mentions(_rp, _rp_refs)
                                if _rp_owed is not None:
                                    _turn_folded_mentions.append(_rp_owed)
                                if _rp.mentions_only:
                                    continue
                                if _rp.message_id:
                                    _leftover_message_ids.append(_rp.message_id)
                                _remote_leftover.append(
                                    _SteeringMessage(
                                        text=_rp.text, files=_rp_refs
                                    )
                                    if _rp_refs
                                    else _rp.text
                                )
                                _turn_steered_texts.append(_rp.text)
                            _leftover = _remote_leftover
                    # Taken above and not answered by this pass's
                    # leftovers: to the bridge for the follow-up pass, or
                    # to the teardown's log on a Stop.
                    _turn_local_steers.extend(_unqueued_local)
                    if not _leftover or _turn_cancelled:
                        break
                    # A follow-up pass is new work on the account's
                    # behalf: gated like the turn that started it.
                    if (
                        await resolve_account_rejection(
                            db, user_id, token_issued_at
                        )
                        is not None
                    ):
                        break
                    conn.accept_steering(_hand_steer_to_bridge)
                    if _turn_local_steers:
                        _turn_steer_wake.set()
                    # The completed segment becomes an intermediate
                    # assistant message: store it, surface it to the
                    # client (``done`` + ``thinking`` re-arms the UI),
                    # then fold everything into history and run the next
                    # pass with the steering text as the new user turn.
                    # The steering user message itself was already
                    # persisted by the listener.
                    if assistant_text and assistant_text.strip():
                        # Drain any buffered token tail of the completed
                        # segment BEFORE its ``done`` frame.  The client
                        # clears its streaming buffer on ``done``; a
                        # coalesced tail arriving after it would be
                        # appended to the NEXT segment's streaming bubble
                        # and duplicate the end of this one.  ``flush()``
                        # is enough — the armed delayed-flush task sees
                        # an empty buffer and exits, and the coalescer is
                        # reused as-is by the follow-up pass.
                        await _coalescer.flush()
                        _seg_meta = _collect_turn_meta()
                        _seg_langfuse = (
                            {}
                            if _turn_deep_investigation
                            else await load_latest_langfuse_ids(str(conv_id))
                        )
                        _seg_meta.update(_seg_langfuse)
                        _seg_msg = await store_message(
                            db,
                            conv_id,
                            role="assistant",
                            content=assistant_text,
                            channel="web",
                            metadata_json=(
                                json.dumps(_seg_meta) if _seg_meta else None
                            ),
                            llm_trace_id=safe_uuid(_seg_langfuse.get("llm_trace_id")),
                        )
                        if _leftover_message_ids:
                            # The steers this segment never read follow it
                            # in the thread: stored when sent, they would
                            # read above the answer that ignored them.
                            try:
                                await move_messages_to_end(
                                    db, conv_id, _leftover_message_ids
                                )
                            except Exception:
                                logger.warning(
                                    "Could not move the late steers of conv %s after the segment",
                                    conv_id,
                                    exc_info=True,
                                )
                        await db.commit()
                        _seg_transfer: dict | None = None
                        if _turn_transfer_agent_id:
                            _seg_transfer = {
                                "transfer_agent_id": _turn_transfer_agent_id,
                                "transfer_agent_name": _turn_transfer_agent_name,
                                "transfer_source_agent_id": _turn_transfer_source_id,
                            }
                        await _send_intermediate_segment_frames(
                            assistant_text,
                            message_id=str(_seg_msg.id),
                            meta=_seg_meta,
                            transfer=_seg_transfer,
                        )
                        # The tracking state above now belongs to the
                        # stored intermediate message — reset it so the
                        # follow-up pass's metadata and ``done`` payload
                        # reflect only its own segment.
                        _reset_turn_tracking()
                        _turn_transfer_agent_id = None
                        _turn_transfer_agent_name = None
                        _turn_transfer_source_id = None
                    # The follow-up answers the leftover as an ordinary
                    # message, never as a second investigation.
                    _turn_deep_investigation = False
                    history.append(_SteerMsg(role="user", content=content))
                    # Steering texts that WERE injected into the loop
                    # this pass are drained from the queue (absent from
                    # ``_leftover``) but still belong to the canonical
                    # thread: they were persisted before the assistant
                    # segment and influenced it.  Replay them between
                    # the user turn and the segment so the follow-up
                    # pass runs against the same history a reload would
                    # produce.  ``_leftover`` is always the tail of
                    # ``_turn_steered_texts`` (FIFO queue, appended in
                    # the same synchronous step as ``put_nowait``, and
                    # no listener runs between loop return and this
                    # drain), so the prefix is exactly the injected set.
                    for _steer_injected in _turn_steered_texts[
                        : len(_turn_steered_texts) - len(_leftover)
                    ]:
                        history.append(
                            _SteerMsg(role="user", content=_steer_injected)
                        )
                    if assistant_text and assistant_text.strip():
                        history.append(
                            _SteerMsg(role="assistant", content=assistant_text)
                        )
                    for _steer_extra in _leftover[:-1]:
                        history.append(
                            _SteerMsg(
                                role="user",
                                content=steering_text(_steer_extra),
                                files=(
                                    (_steer_extra.files or None)
                                    if isinstance(
                                        _steer_extra, _SteeringMessage
                                    )
                                    else None
                                ),
                            )
                        )
                    _last_leftover = _leftover[-1]
                    content = steering_text(_last_leftover)
                    # Reset for the follow-up pass: its own steers must
                    # not re-replay this pass's texts at the next
                    # segment boundary.  Rebinding is safe here — the
                    # listener was cancelled and awaited above, so no
                    # concurrent append can be lost.
                    _turn_steered_texts = []
                    # Attachments belong to the original user message;
                    # the file context already carries them forward.  A
                    # steered attachment on the follow-up turn itself is
                    # re-attached so the model sees it multimodally.
                    turn_file_refs = (
                        (_last_leftover.files or None)
                        if isinstance(_last_leftover, _SteeringMessage)
                        else None
                    )
                    # A transfer / handover in the completed segment
                    # rebinds the conversation — the follow-up pass must
                    # run against the new agent; unbound, the one standing
                    # in for the General Assistant answers it again.
                    conv_agent_id = conv.agent_id or _turn_stand_in_id

                # ── @mentioned agents ──────────────────────────
                # The conversation's agent has answered; each other agent
                # the message names now answers in turn, in the same turn
                # and under the same lock, as its own message and bubble.
                # A reply is finalized with the intermediate-segment frames
                # only once the next one starts or arrives - so no
                # agent's tokens land in the previous bubble - and the last
                # reply takes the final path below. Each agent that runs is
                # an agentic action of its own, and a quota refusal
                # ends the turn: no agent answers past it. The agents the
                # messages folded into the turn mention answer them next,
                # the same way.
                _turn_answering_uuid = conv.agent_id or conv_agent_id
                _turn_current_agent = str(_turn_answering_uuid) if _turn_answering_uuid else None
                _turn_answering_targets = [
                    t for t in _turn_mention_targets if t.agent_id != _turn_current_agent
                ]
                if (_cancel_event is not None and _cancel_event.is_set()) or _turn_metering.refused:
                    # A Stop or a refused charge asks none of them.
                    _turn_folded_mentions.clear()
                if (
                    (_turn_answering_targets or _turn_folded_mentions)
                    and not (_cancel_event is not None and _cancel_event.is_set())
                    and not _turn_metering.refused
                ):
                    from app.services.agent_chat import (
                        run_agent_chat as _run_mentioned_agent,
                    )
                    from app.services.mention_dispatch import (
                        conversation_agent_target,
                    )
                    from app.services.sub_agent_files import (
                        bind_attachment_rows as _bind_reply_rows,
                    )
                    from app.services.sub_agent_files import (
                        load_attachment_rows as _load_reply_rows,
                    )

                    _turn_primary_reply = MentionReply(
                        await conversation_agent_target(db, _turn_answering_uuid),
                        assistant_text
                        if assistant_text and assistant_text.strip()
                        else "I encountered an issue while processing "
                        "your request. Please try again.",
                        failed=_turn_primary_failed,
                        # Its own trace: none when it never reached the
                        # quota gate (a missing or disabled agent).
                        trace=(
                            await load_latest_langfuse_ids(str(conv_id))
                            if _turn_metering.quota
                            else {}
                        ),
                    )
                    _turn_pending: list[MentionReply] = [_turn_primary_reply]
                    # The conversation agent's envelope describes its own
                    # reply, which is no longer the turn's last: it rides
                    # that reply's frame.
                    _turn_primary_envelope = dict(_turn_error_out)
                    _turn_error_out.clear()
                    # Trace ids per mentioned run, read the moment it ends.
                    _turn_mention_traces: dict[str, dict[str, str]] = {}

                    # Per-turn values bound at definition (B023-safe, like
                    # ``_steering_inbox_bridge``): these run within this turn.
                    async def _turn_finalize_pending(
                        _pending: list[MentionReply] = _turn_pending,
                        _conv_id: uuid.UUID = conv_id,
                        _file_context=_turn_file_context,
                        _claimed_file_ids: set = _turn_initial_file_ids,
                        _primary: MentionReply = _turn_primary_reply,
                        _envelope: dict = _turn_primary_envelope,
                    ) -> None:
                        """Store and close the reply waiting for its successor."""
                        if not _pending:
                            return
                        _reply = _pending.pop()
                        await _coalescer.flush()
                        _turn_answered_by_meta.clear()
                        _turn_answered_by_meta.update(_reply.metadata())
                        _reply_meta = _collect_turn_meta(final_content=_reply.text)
                        _reply_meta.update(_reply.trace)
                        # The files its run wrote are shown by its own message:
                        # claimed here, so the turn's final message binds only
                        # the last agent's. A file it only fetched is shown by
                        # the reply that names it.
                        _reply_file_ids = files_for_reply(
                            _file_context,
                            [
                                ref.file_id
                                for ref in _file_context.list_files()
                                if getattr(ref, "file_id", None)
                                and ref.file_id not in _claimed_file_ids
                            ],
                            _reply.text,
                        )
                        _claimed_file_ids.update(_reply_file_ids)
                        _reply_rows: list = []
                        if _reply_file_ids:
                            try:
                                _reply_rows = await _load_reply_rows(
                                    _reply_file_ids, db=db, conversation_id=_conv_id
                                )
                            except Exception:
                                logger.exception(
                                    "Failed to load the files of a mentioned "
                                    "agent's reply for conversation %s",
                                    _conv_id,
                                )
                        if _reply_rows:
                            _reply_meta["attachments"] = [
                                attachment_meta_from_file_attachment(fa)
                                for fa in _reply_rows
                            ]
                        _reply_message_id: str | None = None
                        try:
                            _reply_msg = await store_message(
                                db,
                                _conv_id,
                                role="assistant",
                                content=_reply.text,
                                channel="web",
                                metadata_json=json.dumps(_reply_meta),
                                llm_trace_id=safe_uuid(_reply.trace.get("llm_trace_id")),
                            )
                            if _reply_rows:
                                try:
                                    _bound_rows = await _bind_reply_rows(
                                        db,
                                        _reply_rows,
                                        conversation_id=_conv_id,
                                        message_id=_reply_msg.id,
                                    )
                                except Exception:
                                    logger.exception(
                                        "Failed to link the files of a mentioned "
                                        "agent's reply %s",
                                        _reply_msg.id,
                                    )
                                    _bound_rows = _reply_rows
                                if len(_bound_rows) != len(_reply_rows):
                                    if _bound_rows:
                                        _reply_meta["attachments"] = [
                                            attachment_meta_from_file_attachment(fa)
                                            for fa in _bound_rows
                                        ]
                                    else:
                                        _reply_meta.pop("attachments", None)
                                    _reply_msg.metadata_json = json.dumps(_reply_meta)
                            await db.commit()
                            _reply_message_id = str(_reply_msg.id)
                        except Exception:
                            logger.exception(
                                "Failed to persist a mentioned agent's reply "
                                "for conversation %s",
                                _conv_id,
                            )
                            with contextlib.suppress(Exception):
                                await db.rollback()
                        _frame_meta = dict(_reply_meta)
                        _frame_attachments = (
                            parse_attachments(
                                json.dumps(_reply_meta),
                                conversation_id=str(_conv_id),
                                message_id=_reply_message_id,
                            )
                            if _reply_message_id
                            else None
                        )
                        if _frame_attachments:
                            _frame_meta["attachments"] = _frame_attachments
                        else:
                            _frame_meta.pop("attachments", None)
                        if _reply is _primary and _envelope.get("error"):
                            _frame_meta["error"] = _envelope["error"]
                            _frame_meta["error_code"] = _envelope.get("error_code")
                        await _send_intermediate_segment_frames(
                            _reply.text, message_id=_reply_message_id, meta=_frame_meta
                        )
                        _reset_turn_tracking(run_ended=True)
                        _turn_answered_by_meta.clear()

                    async def _turn_mention_start(
                        target: MentionTarget, index: int, total: int
                    ) -> None:
                        await _turn_finalize_pending()
                        # Its mid-run segments are attributed to it too.
                        _turn_answered_by_meta.update(reply_metadata(target))
                        await _safe_send_json(
                            {
                                "type": "status",
                                "status": "mention_answering",
                                "agent_id": target.agent_id,
                                "agent_name": target.name,
                                "agent_icon": target.icon,
                                "position": index + 1,
                                "total": total,
                            }
                        )

                    async def _turn_mention_reply(
                        reply: MentionReply,
                        _pending: list[MentionReply] = _turn_pending,
                        _traces: dict = _turn_mention_traces,
                    ) -> None:
                        await _turn_finalize_pending()
                        # A note no run wrote carries no trace.
                        reply.trace = _traces.pop(reply.target.agent_id, {})
                        _pending.append(reply)

                    _turn_mention_stopped = asyncio.Event()

                    async def _turn_mention_event(
                        event: dict, _stopped: asyncio.Event = _turn_mention_stopped
                    ) -> None:
                        # A run the dispatch cut no longer reaches the stream.
                        if not _stopped.is_set():
                            await _on_event(event)

                    async def _turn_mention_token(
                        chunk: str, _stopped: asyncio.Event = _turn_mention_stopped
                    ) -> None:
                        if not _stopped.is_set():
                            await _on_token(chunk)

                    async def _turn_run_mentioned(
                        mentioned_agent,
                        mention_history,
                        mention_cancel,
                        agents_after,
                        _conv_id: uuid.UUID = conv_id,
                        _content: str = _turn_content,
                        _file_refs: list | None = _turn_file_refs,
                        _file_context=_turn_file_context,
                        _skip_event: asyncio.Event | None = _wait_skip_event,
                        _queue: asyncio.Queue | None = _steering_queue,
                        _turn_studio: Any = _turn_studio,
                        _turn_desktop: Any = _turn_desktop,
                        _reference_brief: str | None = turn_reference_brief,
                        _traces: dict = _turn_mention_traces,
                    ) -> str:
                        _mention_listener = asyncio.create_task(
                            _listen_during_processing()
                        )

                        async def _run_on(own_db, own_agent) -> str:
                            # Its own agentic action, charged before it
                            # runs.
                            _mention_quota: dict = {}
                            _mention_text, _ = await _run_mentioned_agent(
                                own_db,
                                str(user_id),
                                own_agent,
                                mention_history,
                                _content,
                                conversation_id=str(_conv_id),
                                on_event=_turn_mention_event,
                                on_token=_turn_mention_token,
                                files=_file_refs or None,
                                file_context=_file_context,
                                cancel_event=mention_cancel,
                                wait_skip_event=_skip_event,
                                steering_queue=_queue,
                                deliver_segments=True,
                                presentation_studio=_turn_studio,
                                desktop=_turn_desktop,
                                quota_out=_mention_quota,
                                # Collected into its own reply by
                                # ``_collect_turn_meta``, like its tools.
                                unavailable_integrations_out=_turn_unavailable_integrations,
                                reference_brief=_reference_brief,
                                agents_after=agents_after,
                                allowed_handover_types=MENTIONED_AGENT_HANDOVER_TYPES,
                            )
                            if quota_refused(_mention_quota):
                                raise TurnRefusedError(_mention_text)
                            _traces[str(own_agent.id)] = await load_latest_langfuse_ids(
                                str(_conv_id)
                            )
                            return _mention_text

                        try:
                            return await run_on_own_session(mentioned_agent, _run_on)
                        finally:
                            _mention_listener.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await _mention_listener

                    # The replies the turn's message got, grown by the
                    # dispatch: what a folded message's agents read next.
                    _turn_earlier: list[MentionReply] = (
                        []
                        if _turn_primary_failed
                        or not (assistant_text and assistant_text.strip())
                        else list(_turn_pending)
                    )
                    # One budget for every mentioned agent of the turn.
                    _turn_mention_deadline = time.monotonic() + DISPATCH_BUDGET_SECONDS
                    _turn_mention_replies = await dispatch_mentions(
                        _turn_answering_targets,
                        history=_turn_history,
                        user_message=_turn_content,
                        earlier=_turn_earlier,
                        run_agent=_turn_run_mentioned,
                        cancel_event=_cancel_event,
                        on_start=_turn_mention_start,
                        on_reply=_turn_mention_reply,
                        describe_error=_friendly_error_message,
                        stopped=_turn_mention_stopped,
                    )
                    if any(r.refused for r in _turn_mention_replies):
                        _turn_folded_mentions.clear()
                    _turn_mention_replies += await answer_folded_mentions(
                        _turn_folded_mentions,
                        db=db,
                        user_id=user_id,
                        answering_agent_id=_turn_current_agent,
                        history=folded_mention_history(
                            _turn_history, _turn_content, _turn_earlier
                        ),
                        # A folded message's own text and attachments.
                        run_agent_for=lambda m, _run=_turn_run_mentioned: functools.partial(
                            _run, _content=m.text, _file_refs=m.files or None
                        ),
                        cancel_event=_cancel_event,
                        deadline=_turn_mention_deadline,
                        on_start=_turn_mention_start,
                        on_reply=_turn_mention_reply,
                        describe_error=_friendly_error_message,
                        stopped=_turn_mention_stopped,
                    )
                    if _turn_mention_replies:
                        # The last reply is the turn's final message.
                        _turn_last_reply = _turn_pending.pop()
                        _turn_answered_by_meta.clear()
                        _turn_answered_by_meta.update(_turn_last_reply.metadata())
                        assistant_text = _turn_last_reply.text
                        _turn_final_trace = dict(_turn_last_reply.trace)
                    else:
                        # No mentioned agent answered (a cancel before the
                        # first, or folded messages naming none): the
                        # conversation agent's reply stays the final one,
                        # unlabelled, exactly as on a turn without them.
                        _turn_pending.clear()
                        _turn_error_out.update(_turn_primary_envelope)
            except asyncio.CancelledError as _turn_cancel:
                # Handler-task cancellation (server shutdown, connection
                # teardown, worker recycle) is NOT an ``Exception`` — it
                # previously bypassed the handler below entirely, so a
                # killed mid-run turn died with ZERO log lines.
                # Leave a WARNING trail and clear the processing marker
                # so a reloading client doesn't poll a phantom in-flight
                # response.  Intermediate steering segments committed
                # earlier in the fold-in loop (if any) are already stored.
                try:
                    if is_graceful_shutdown_cancellation(_turn_cancel):
                        # Cut by uvicorn's bounded drain: what the
                        # turn did since its last stored message becomes
                        # its reply, stored before the lock goes.
                        _cut_text = interrupted_reply(_turn_progress, cut_cause())
                        await _store_cut_turn(
                            conv_id,
                            content=_cut_text,
                            meta={
                                **_collect_turn_meta(final_content=_cut_text),
                                **_turn_context_usage.command_meta(),
                            },
                            checkpoint=_turn_checkpoint,
                            # Resumed by the next process.
                            user_id=user_id,
                            user_message_id=turn_user_msg.id,
                            turn_id=_turn_checkpoint.turn_id,
                            entry="web",
                            lock_token=_turn_token,
                            trace_link=_turn_trace_link,
                        )
                    else:
                        logger.warning(
                            "Chat turn cancelled mid-run for "
                            "conversation %s (server shutdown or "
                            "connection teardown) — the turn's final "
                            "assistant message was not persisted; its "
                            "checkpointed tool work is resumed by the "
                            "next turn",
                            conv_id,
                        )
                finally:
                    await _clear_processing_marker()
                raise
            except Exception as e:
                logger.error("Chat turn error: %s", format_exception_message(e), exc_info=True)
                assistant_text = (
                    f"I encountered an error: {_friendly_error_message(e)}"
                )
                _turn_keep_checkpoint = True
                await _coalescer.flush()
                # Provisional: the turn still stores its reply and closes with
                # that stored end.
                await _safe_send_json({"type": "error", "content": assistant_text, "provisional": True})
            finally:
                # Cancel any pending delayed flush and drain the token
                # tail so the client sees the complete streamed text
                # before the done/error frame, and no flush task can
                # wake up after this message completes.
                await _coalescer.aclose()
                # The fold-in loop has exited — no more steering can be
                # injected into this turn, so stop the steering
                # bridge NOW, once the batch it holds is queued.
                # Folds landing during the final persistence phase below
                # stay in the Redis inbox and are caught by
                # ``_clear_processing_marker``'s release-and-drain
                # handshake.
                _stop_local_steering()
                await _stop_steer_bridge()
                # What the bridge queued after the fold-in loop's last
                # leftover check (the @mentioned agents' runs and the
                # persistence phase keep it running) was told the turn
                # took it: a turn that reaches its own end answers it in
                # its follow-up (``_clear_processing_marker``). A
                # cancelled or killed turn logs it: the messages are
                # persisted, they shape the next turn.
                if _steering_queue is not None:
                    _turn_undelivered = _steering_queue.take_unanswered()
                    if sys.exc_info()[0] is None:
                        _turn_unanswered_folds.extend(_turn_undelivered)
                    elif _turn_undelivered:
                        logger.warning(
                            "%d steering message(s) arrived too late to "
                            "be injected into the turn for conversation "
                            "%s — the messages are persisted and will "
                            "shape the next turn",
                            len(_turn_undelivered),
                            conv_id,
                        )
                # Keepalive lifecycle (P2.3): on the SUCCESS path the
                # keepalive must survive through the post-loop
                # persistence phase below (it keeps proxies + the
                # processing marker alive while store_message / title /
                # commit run) and is cancelled after the done frame.
                # When an exception is propagating out of this block
                # (cancellation / teardown), stop it now — the message
                # processing is over.
                if sys.exc_info()[0] is not None:
                    marker_refresh_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await marker_refresh_task

            # Guard against empty/blank assistant response
            if not assistant_text or not assistant_text.strip():
                assistant_text = "I encountered an issue while processing your request. Please try again."

            # Post-loop visibility (P2.3): the final token has flowed
            # but persistence (store_message + title + commit) is still
            # ahead — surface it instead of a silent gap before done.
            await _safe_send_json({"type": "status", "status": "persisting"})

            # Build assistant message metadata with tool usage summary.
            # The final text is passed so reasoning that the deliverable
            # rescue merged into the reply is not duplicated.
            _assistant_meta: str | None = None
            # A Stop keeps what the answer had written, as the panel showed it,
            # in place of the loop's cancellation text; the turn still ends
            # with its ``cancelled`` frame.
            _turn_stopped = _is_cancelled_terminal(_cancel_event, assistant_text)
            if _turn_stopped and "".join(_turn_answer_parts).strip():
                assistant_text = "".join(_turn_answer_parts)
            _turn_meta = _collect_turn_meta(final_content=assistant_text)
            # The ``/context`` reading is stored, never sent on the frame.
            _meta_dict: dict = {**_turn_meta, **_turn_context_usage.command_meta()}
            # On a multi-agent turn the final message carries the
            # trace of the run that wrote it, not merely the latest one. An
            # investigation makes no traced model call: the latest ids are
            # a previous turn's.
            _meta_dict.update(
                _turn_final_trace
                if _turn_final_trace is not None
                else {}
                if _turn_deep_investigation
                else await load_latest_langfuse_ids(str(conv_id))
            )

            # A file the turn only fetched is shown when the final answer
            # or one of the turn's segments names it.
            _turn_generated_ids = set(
                files_for_reply(
                    _turn_file_context,
                    [
                        ref.file_id
                        for ref in _turn_file_context.list_files()
                        if getattr(ref, "file_id", None)
                        and ref.file_id not in _turn_initial_file_ids
                    ],
                    "\n".join([*_turn_segment_texts, assistant_text or ""]),
                )
            )
            _turn_attachment_rows: list = []
            if _turn_generated_ids:
                # Same admission read as the REST path: only rows no
                # message shows yet, unbound or ours.
                from app.services.sub_agent_files import load_attachment_rows

                try:
                    _turn_attachment_rows = await load_attachment_rows(
                        [
                            ref.file_id
                            for ref in _turn_file_context.list_files()
                            if getattr(ref, "file_id", None) in _turn_generated_ids
                        ],
                        db=db,
                        conversation_id=conv_id,
                    )
                except Exception:
                    logger.exception(
                        "Failed to load FileAttachment rows for new "
                        "web chat generated files"
                    )
            if _turn_attachment_rows:
                _meta_dict["attachments"] = [
                    attachment_meta_from_file_attachment(fa)
                    for fa in _turn_attachment_rows
                ]
            if _meta_dict:
                _assistant_meta = json.dumps(_meta_dict)

            _turn_llm_trace_id = safe_uuid(_meta_dict.get("llm_trace_id"))
            # Soft budget on the whole post-loop persistence phase
            # (P1.4): a DB lock wait here delays the ``done`` frame
            # after the answer already streamed — surface it as a
            # WARNING watchdog instead of dying silently.
            async with _soft_phase_budget(
                "Post-loop persistence", str(conv_id)
            ):
                # A turn that failed leaves its work for the next one; any
                # other end deletes it with the answer below.
                await _turn_checkpoint.aclose(flush=_turn_keep_checkpoint)
                try:
                    assistant_msg = await store_message(
                        db,
                        conv_id,
                        role="assistant",
                        content=assistant_text,
                        channel="web",
                        metadata_json=_assistant_meta,
                        llm_trace_id=_turn_llm_trace_id,
                    )
                except Exception as _store_exc:
                    logger.warning(
                        "store_message failed, recovering session: %s",
                        format_exception_message(_store_exc),
                    )
                    with contextlib.suppress(Exception):
                        await db.rollback()
                    assistant_msg = await store_message(
                        db,
                        conv_id,
                        role="assistant",
                        content=assistant_text,
                        channel="web",
                        metadata_json=_assistant_meta,
                        llm_trace_id=_turn_llm_trace_id,
                    )

                if _turn_attachment_rows:
                    # See the REST path: the shared bind sets both halves
                    # of the message-scoped URL, skips (never re-homes) a
                    # row another message bound meanwhile, and the block
                    # is narrowed to what it bound.
                    from app.services.sub_agent_files import (
                        bind_attachment_rows,
                    )

                    try:
                        _turn_bound_rows = await bind_attachment_rows(
                            db,
                            _turn_attachment_rows,
                            conversation_id=conv_id,
                            message_id=assistant_msg.id,
                        )
                        if len(_turn_bound_rows) != len(_turn_attachment_rows):
                            _meta_dict["attachments"] = [
                                attachment_meta_from_file_attachment(fa)
                                for fa in _turn_bound_rows
                            ]
                            if not _turn_bound_rows:
                                _meta_dict.pop("attachments", None)
                            assistant_msg.metadata_json = (
                                json.dumps(_meta_dict) if _meta_dict else None
                            )
                    except Exception:
                        # Same rationale as the REST path: the message-scoped
                        # download URL we're about to ship in the ``done``
                        # event depends on the ``message_id`` linkage we just
                        # tried to set. Log loudly so the orphaned rows show
                        # up in audit tooling; the response itself still goes
                        # out and the tightened legacy ``/files/{id}/download``
                        # endpoint will gate the fallback URL on the same
                        # scope match.
                        logger.exception(
                            "Failed to link agent-generated FileAttachment "
                            "rows to assistant message %s; message-scoped "
                            "download URLs will 404 for these files until "
                            "the linkage is repaired",
                            assistant_msg.id,
                        )

                if not _turn_keep_checkpoint and _turn_checkpoint.may_hold_row:
                    await turn_checkpoint.discard(db, conv_id)
                await db.commit()

            # Cancel-state coherence (P2.5): the client already received
            # a ``cancelled`` ack when it sent the cancel frame; closing
            # the turn with ``done`` + "The request was cancelled." put
            # the client in a done-after-cancelled double state.  Close
            # with a terminal ``cancelled`` frame instead (the persisted
            # cancellation marker, or what the answer had written, still
            # appears on the next refetch).
            if _turn_stopped:
                _done_payload = {
                    "type": "cancelled",
                    "message_id": str(assistant_msg.id),
                    "conversation_id": str(conv_id),
                }
            else:
                _done_payload = {
                    "type": "done",
                    "content": assistant_text,
                    "message_id": str(assistant_msg.id),
                    "conversation_id": str(conv_id),
                    **_turn_meta,
                }
                if _turn_transfer_agent_id:
                    _done_payload["transfer_agent_id"] = _turn_transfer_agent_id
                    _done_payload["transfer_agent_name"] = _turn_transfer_agent_name
                    _done_payload["transfer_source_agent_id"] = _turn_transfer_source_id
                _done_attachments = parse_attachments(
                    assistant_msg.metadata_json,
                    conversation_id=str(conv_id),
                    message_id=str(assistant_msg.id),
                )
                if _done_attachments:
                    _done_payload["attachments"] = _done_attachments
                # Additive failure envelope: a skip_llm pipeline refusal
                # rides the ``done`` frame so clients can read the real
                # cause instead of scraping the assistant text. Absent on
                # success (existing clients ignore unknown fields).
                if _turn_error_out.get("error"):
                    _done_payload["error"] = _turn_error_out["error"]
                    _done_payload["error_code"] = _turn_error_out.get("error_code")
            # Fan the terminal frame out and close the journal BEFORE the
            # marker is released: a viewer must never see the
            # marker gone while its turn is still "in flight", and a
            # stream attaching in between must not be resumed into a
            # finished turn. The direct send below no longer publishes
            # (the publisher is closed), so the frame goes out once.
            if _turn_publisher is not None:
                _turn_publisher.offer(_done_payload)
                _publisher, _turn_publisher = _turn_publisher, None
                with contextlib.suppress(Exception):
                    await _publisher.aclose()

            # Clear the processing flag now that the response is stored;
            # what was folded into the turn too late for it is answered by
            # a follow-up, unless the user stopped the turn.
            await _clear_processing_marker(
                follow_up=not (_cancel_event is not None and _cancel_event.is_set())
            )

            if not await _safe_send_json(_done_payload):
                # The answer IS persisted — the client just can't be
                # told right now (stream died during a long turn).  Log
                # it so "the user saw nothing but the message exists in
                # the DB" is diagnosable instead of silent.  The
                # frontend recovers via its reconnect polling / the
                # is_processing marker on reload.
                logger.warning(
                    "Chat stream closed before the %s frame could be "
                    "delivered for conversation %s — assistant message "
                    "%s is persisted and will appear on reload",
                    _done_payload["type"],
                    conv_id,
                    assistant_msg.id,
                )

            if _turn_needs_ai_title:
                _title_task = asyncio.create_task(
                    _generate_title_after_done(
                        conversation_id=conv_id,
                        user_id=str(user_id),
                        provisional_title=_turn_provisional_title,
                        user_text=content,
                        assistant_text=assistant_text,
                        prebuilt_llm=_turn_prebuilt_llm,
                        send=_safe_send_json,
                        origin=_origin,
                    ),
                    name=f"chat-title:{conv_id}",
                )
                _turn_title_tasks.add(_title_task)
                _title_task.add_done_callback(_turn_title_tasks.discard)
                _send_title_task = _title_task

            # The user's likely next message, once the answer is out
            # (``services/chat_reply_suggestions.py``): a turn that ended on an
            # answer only - not a stop, not an error - and for a client that
            # offers it (``supports_reply_suggestions``).
            if _turn_supports_reply_suggestions and not _turn_stopped and not _turn_error_out.get("error"):
                from app.services.chat_reply_suggestions import (
                    send_suggestion_after_done,
                )

                _suggestion_task = asyncio.create_task(
                    send_suggestion_after_done(
                        send=_safe_send_json,
                        origin=_origin,
                        conversation_id=conv_id,
                        message_id=assistant_msg.id,
                        user_id=user_id,
                        agent_id=_turn_transfer_agent_id or conv_agent_id,
                        user_text=content,
                        assistant_text=assistant_text,
                        turn_meta=_turn_meta,
                    ),
                    name=f"chat-reply-suggestion:{conv_id}",
                )
                _turn_title_tasks.add(_suggestion_task)
                _suggestion_task.add_done_callback(_turn_title_tasks.discard)
                _send_suggestion_task = _suggestion_task

            # The next turn's compaction, while the user reads this answer.
            # The margin covers the messages this turn stored; the
            # precompute checks the trigger on the history it loads.
            if _turn_prebuilt_llm is not None:
                from app.services.conversation_memory import (
                    compaction_context_window as _compaction_window_next,
                )
                from app.services.conversation_memory import (
                    precompute_compaction,
                )
                from app.services.conversation_memory import (
                    should_compact as _should_compact_next,
                )

                if _should_compact_next(
                    history,
                    conversation_id=str(conv_id),
                    ahead=4,
                    context_window=_compaction_window_next(*_turn_prebuilt_llm),
                ):
                    precompute_compaction(
                        _turn_prebuilt_llm[0],
                        conversation_id=str(conv_id),
                        agent_id=str(conv_agent_id) if conv_agent_id else None,
                        user_id=str(user_id),
                        memory_persistent_enabled=_turn_agent_memory_persistent,
                        model=_turn_prebuilt_llm[1],
                    )

            # The turn is delivered: stop the marker refresh, kept up through
            # the persistence above.
            marker_refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await marker_refresh_task

        # The stream ends with the turn's title and its reply suggestion.
        _after_done = [t for t in (_send_title_task, _send_suggestion_task) if t is not None]
        if _after_done:
            await asyncio.gather(*_after_done, return_exceptions=True)

    except asyncio.CancelledError:
        # Catch-all for handler-task cancellation OUTSIDE the agent-loop
        # block above — e.g. during the post-loop persistence/commit, the
        # AI-title generation or the final ``done`` send.  Without this the
        # cancellation would bypass every handler below (CancelledError is
        # not an ``Exception``) and leave the processing marker stale with
        # no trace, which is part of the original failure mode.
        if _active_processing_key is not None:
            logger.warning(
                "Chat connection cancelled during turn finalization "
                "for conversation %s (server shutdown or connection "
                "teardown) — check the surrounding log context for whether "
                "the assistant message was persisted",
                conversation_id,
            )
            await _clear_processing_marker()
        raise
    except StreamConsumerGoneError as e:
        # The stream's consumer went away: tab closed, network drop,
        # navigation away. A normal occurrence.
        logger.debug(
            "Chat stream closed (client gone) for conversation %s: %s", conversation_id, format_exception_message(e)
        )
        await _clear_processing_marker()
    except Exception as e:
        logger.error("Chat connection error: %s", format_exception_message(e), exc_info=True)
        await _clear_processing_marker()
        # The person reads this: what it means for them, never the exception's
        # text. An HTTP error before the turn started, its `error` frame after.
        with contextlib.suppress(Exception):
            await conn.refuse(_friendly_error_message(e), status_code=500)
    finally:
        _session_watch_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await _session_watch_task
        if marker_refresh_task is not None:
            marker_refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await marker_refresh_task


# ---------------------------------------------------------------------------
# Chat image serving
# ---------------------------------------------------------------------------


@router.get("/images/{image_id}")
async def get_chat_image(
    image_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Serve a generated chat image by ID.

    The image must belong to the requesting user (or the user must be admin);
    an image they may not read gets the unknown-image 404
    (:func:`~app.services.file_access.load_readable_chat_image`).
    """
    from app.services.file_access import load_readable_chat_image

    image = await load_readable_chat_image(db, image_id, user)

    # A ChatImage is never rewritten, so a day is right; it is keyed on the
    # credential that was authorised for it.
    return Response(
        content=image.image_data,
        media_type=image.content_type,
        headers={"Cache-Control": "private, max-age=86400", "Vary": "Authorization"},
    )


# ---------------------------------------------------------------------------
# Public image serving (signed token, no JWT — for external channels)
# ---------------------------------------------------------------------------


@router.get("/images/{image_id}/public")
async def get_chat_image_public(
    image_id: str,
    token: str = "",
    expires: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """Serve a generated chat image via time-limited signed token.

    Used by external channels (Slack, Teams, WhatsApp, Telegram, Discord)
    that cannot provide JWT authentication.  The token is an HMAC-SHA256
    signature over ``{image_id}:{expires}`` using the platform secret key.
    """
    from app.core.security import verify_image_token
    from app.models.chat_image import ChatImage

    if not token or not expires:
        raise HTTPException(status_code=401, detail="Missing token or expires")

    if not verify_image_token(image_id, token, expires):
        raise HTTPException(status_code=403, detail="Invalid or expired token")

    result = await db.execute(
        select(ChatImage).where(
            ChatImage.id == parse_path_id(image_id, not_found_detail="Image not found")
        )
    )
    image = result.scalar_one_or_none()
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")

    # ``private``: the token is the only authorisation this answer carries, and
    # a shared cache honouring ``public`` would keep serving the image after the
    # token expired or the image was deleted.
    return Response(
        content=image.image_data,
        media_type=image.content_type,
        headers={"Cache-Control": f"private, max-age={token_max_age(expires)}"},
    )


# ---------------------------------------------------------------------------
# Public file serving (signed token, no JWT — for external channels)
# ---------------------------------------------------------------------------

#: The one answer contract for a ``FileAttachment``'s bytes (disposition,
#: ``nosniff``, validator, lifetime) lives in ``app.services.file_responses`` so
#: the template asset route answers the same way; the private names are
#: what this module's routes and the tests that pin the contract already use.
_INLINE_FILE_CONTENT_TYPES = INLINE_FILE_CONTENT_TYPES
_file_response_headers = file_response_headers


@router.get("/files/{file_id}/public")
async def get_file_public(
    file_id: str,
    token: str = "",
    expires: int = 0,
    if_none_match: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
):
    """Serve an S3-stored file via time-limited signed token.

    Used by external channels for generated images stored in S3. The answer is
    ``private`` and never kept past the token: the token is the only
    authorisation it carries.
    """
    from app.core.security import verify_image_token
    from app.models.file_attachment import FileAttachment
    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    if not token or not expires:
        raise HTTPException(status_code=401, detail="Missing token or expires")

    if not verify_image_token(file_id, token, expires):
        raise HTTPException(status_code=403, detail="Invalid or expired token")

    result = await db.execute(
        select(FileAttachment).where(
            FileAttachment.id == parse_path_id(file_id, not_found_detail="File not found")
        )
    )
    file_att = result.scalar_one_or_none()
    if not file_att or not file_att.s3_object_name:
        raise HTTPException(status_code=404, detail="File not found")
    from app.services.file_retention import raise_if_deleted

    raise_if_deleted(file_att)

    not_modified = file_not_modified(
        file_att, if_none_match, max_age=token_max_age(expires)
    )
    if not_modified is not None:
        return not_modified

    try:
        data = await file_storage.download(file_att.s3_object_name)
    except FileNotFoundInStorageError:
        logger.warning(
            "Public file no longer available: file_id=%s key=%s",
            file_att.id,
            file_att.s3_object_name,
        )
        raise HTTPException(
            status_code=404, detail=FILE_UNAVAILABLE_DETAIL
        ) from None
    if not data:
        raise HTTPException(status_code=404, detail="File data not found")

    # The link is public for as long as its token lives and is minted for ANY
    # ``[[FILE:id]]`` a channel reply carries (``resolve_channel_file_markers``),
    # so it opens in the browser only what cannot run as a page on the platform
    # origin - raster images, the documents a browser shows, audio and video -
    # and downloads the rest, nothing sniffed.
    # Freshness starts when the answer is sent, so the token's remaining life is
    # read after the download: a slow read must not keep the bytes past it.
    return Response(
        content=data,
        media_type=file_att.content_type or "application/octet-stream",
        headers=_file_response_headers(file_att, max_age=token_max_age(expires), inline_documents=True),
    )


# ---------------------------------------------------------------------------
# File download (S3-stored attachments)
# ---------------------------------------------------------------------------


@router.get("/conversations/{conversation_id}/messages/{message_id}/files/{file_id}/download")
async def download_message_file(
    conversation_id: str,
    message_id: str,
    file_id: str,
    if_none_match: Annotated[str | None, Header()] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.file_attachment import FileAttachment
    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    # An attachment is part of the transcript, so it follows the transcript's
    # access rule: whoever may read the conversation may download the
    # files in it. Refusals stay 404 rather than the previous 403 — the two
    # now have to be indistinguishable, or "403 = exists, 404 = does not"
    # would turn this endpoint into the conversation-id oracle that
    # ``resolve_conversation_for_read`` exists to prevent.
    conv, _access = await resolve_conversation_for_read(conversation_id, user, db)

    msg_uuid = parse_path_id(message_id, not_found_detail="Message not found")
    msg_result = await db.execute(
        select(Message).where(
            Message.id == msg_uuid,
            Message.conversation_id == conv.id,
        )
    )
    if not msg_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Message not found")

    file_uuid = parse_path_id(file_id, not_found_detail="File not found")
    file_result = await db.execute(
        select(FileAttachment).where(
            FileAttachment.id == file_uuid,
            FileAttachment.message_id == msg_uuid,
            FileAttachment.conversation_id == conv.id,
        )
    )
    file_att = file_result.scalar_one_or_none()
    if not file_att:
        raise HTTPException(status_code=404, detail="File not found")
    from app.services.file_retention import raise_if_deleted, touch

    raise_if_deleted(file_att)
    # The last use renews the file's retention; committed before the answer.
    await touch(db, [file_att.id])
    await db.commit()

    not_modified = file_not_modified(file_att, if_none_match)
    if not_modified is not None:
        return not_modified

    # Stream the bytes through the backend rather than 307-redirecting to
    # ``file_storage.presigned_url(...)``. The presigned URL host is
    # derived from ``settings.s3_endpoint`` which, in Docker dev / CI /
    # any deployment with a private object-store address (``minio:9000``,
    # internal VPC, etc.), is unreachable from the browser. A redirect
    # there would 0-byte for any external client even though this API
    # endpoint itself is reachable. ``download_file`` already takes this
    # shape — keep both endpoints consistent so a future S3-endpoint
    # rewrite (public vs internal split) only needs to land in one
    # place.
    try:
        data = await file_storage.download(file_att.s3_object_name)
    except FileNotFoundInStorageError:
        logger.warning(
            "File no longer available for message-scoped endpoint: "
            "file_id=%s key=%s",
            file_att.id,
            file_att.s3_object_name,
        )
        raise HTTPException(
            status_code=404, detail=FILE_UNAVAILABLE_DETAIL
        ) from None
    except Exception as exc:
        logger.error(
            "Failed to download file for message-scoped endpoint: file_id=%s err=%s",
            file_att.id,
            format_exception_message(exc),
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="File download failed") from None

    return Response(
        content=data,
        media_type=file_att.content_type or "application/octet-stream",
        headers=_file_response_headers(file_att),
    )


#: The one authorization contract for reading a ``FileAttachment``. It lives in
#: ``app.services.file_access`` so non-HTTP callers (the ``attach_template_asset``
#: built-in tool) enforce the same rule instead of re-deriving it; the
#: private alias is kept because it is what this module's routes and the tests
#: that pin the contract already name.
_authorize_file_access = authorize_file_access


@router.get("/files/{file_id}/download")
async def download_file(
    file_id: str,
    if_none_match: Annotated[str | None, Header()] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Download a file attachment by ID.

    See :func:`_authorize_file_access` for the full authorization
    contract — owner / admin / conversation-scoped fallback /
    assignment-run-scoped fallback, with orphan files rejected so the
    legacy permissive ``NULL means everyone`` semantics can never
    resurface. A file the caller may not read gets the unknown-file 404
    (:func:`~app.services.file_access.load_readable_file`).

    Returns the raw binary with appropriate Content-Type and
    Content-Disposition, or a ``304`` when ``If-None-Match`` names the bytes
    already held (a template asset replaced in place keeps this URL).
    """
    from app.services.file_access import load_readable_file
    from app.services.file_retention import raise_if_deleted, touch
    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    file_att = await load_readable_file(db, file_id, user)
    raise_if_deleted(file_att)
    # The last use renews the file's retention; committed before the answer.
    await touch(db, [file_att.id])
    await db.commit()

    not_modified = file_not_modified(file_att, if_none_match)
    if not_modified is not None:
        return not_modified

    try:
        data = await file_storage.download(file_att.s3_object_name)
    except FileNotFoundInStorageError:
        logger.warning(
            "File no longer available: file_id=%s key=%s",
            file_att.id,
            file_att.s3_object_name,
        )
        raise HTTPException(
            status_code=404, detail=FILE_UNAVAILABLE_DETAIL
        ) from None
    except Exception as exc:
        logger.error("File download failed: %s", format_exception_message(exc))
        raise HTTPException(status_code=500, detail="File download failed") from None

    # Raster images inline, everything else downloaded (see
    # ``_file_response_headers`` for the contract shared by the file routes).
    return Response(
        content=data,
        media_type=file_att.content_type,
        headers=_file_response_headers(file_att),
    )


@router.get("/files/{file_id}/info")
async def get_file_info(
    file_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get file attachment metadata without downloading the binary.

    Uses the same authorization contract as :func:`download_file` — see
    :func:`_authorize_file_access` for details. Without this, anyone with
    a valid session could enumerate ``filename``/``content_type``/``size``
    /``s3_object_name`` metadata for any NULL-user file via this endpoint.
    """
    from app.services.file_access import load_readable_file

    file_att = await load_readable_file(db, file_id, user)

    return {
        "id": str(file_att.id),
        "filename": file_att.filename,
        "content_type": file_att.content_type,
        "size": file_att.size,
        "source": file_att.source,
        "download_url": f"/api/v1/chat/files/{file_id}/download",
        "created_at": file_att.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Sub-agent tasks (background agent delegation) — LEGACY, kept for compat
# ---------------------------------------------------------------------------


class BulkDeleteTasksRequest(BaseModel):
    """Bulk-delete background tasks by explicit ids or by matching filters."""

    task_ids: list[str] | None = None
    all_matching_filters: bool = False
    # Filters mirror the list endpoint (only used with all_matching_filters).
    scope: str = "mine"
    vis: str | None = None
    status: str | None = None
    agent_id: str | None = None
    search: str | None = None


class BulkDeleteTasksResponse(BaseModel):
    deleted: int


def _gate_task_scope(scope: str, is_admin: bool) -> None:
    """Reject ``scope='all'`` for non-admins (shared across task endpoints)."""
    if scope == "all" and not is_admin:
        raise HTTPException(
            status_code=403, detail="Admin access required for scope=all"
        )


def _task_scope_from_vis(vis: str | None, scope: str, is_admin: bool) -> str | None:
    """The task scope a ``?vis=`` segment asks for, or ``None`` for an empty segment.

    A background task belongs to the person who spawned it; the tasks a
    caller sees are their own, every user's for an admin. Those that are not
    the caller's are split by the agent that delegated them: Company
    when it is company-managed, Shared otherwise, so Mine, Shared and Company
    partition All. A non-admin sees only their own tasks: their Shared and
    Company are empty, never a 403. Without ``vis`` the [DEPRECATED]
    ``scope`` keeps its meaning and its admin gate.
    """
    from app.services.visibility import to_visibility

    segment = to_visibility(vis)
    if segment is None:
        _gate_task_scope(scope, is_admin)
        # The [DEPRECATED] scope only ever knew mine | all.
        return scope if scope == "all" else "mine"
    if segment in ("shared", "company"):
        return segment if is_admin else None
    if segment == "all" and is_admin:
        return "all"
    return "mine"


_TASK_FLEET_DESCRIPTION = (
    "Only the tasks whose parent or target agent is a current home member of "
    "this fleet. A value that is not a UUID is a 422."
)
_TASK_LIVENESS_DESCRIPTION = (
    "no_live_execution: only the pending or running tasks whose execution is missing "
    "or no longer pending or running, the zombies the sweep settles."
)
_TASK_STATUS_DESCRIPTION = (
    "One status or several comma-separated (pending, running, completed, incomplete, "
    "failed, cancelled); an empty or unknown value is a 422."
)
_TASK_SORT_DESCRIPTION = (
    "created (newest first, the default) | created_asc | last_progress (the execution's "
    "last move first) | last_progress_asc | status | status_desc | from | from_desc | to | to_desc "
    "(the delegating or the target agent's name)"
)


class BackgroundTaskStatusCounts(BaseModel):
    """How many background tasks each status holds under the list's other filters."""

    counts: dict[str, int]


def _checked_task_status(task_status: str | None) -> str | None:
    """The ``status`` filter as the service takes it, a 422 on an empty or unknown value."""
    from app.services.sub_agent import background_task_statuses

    if task_status is None:
        return None
    try:
        return ",".join(background_task_statuses(task_status))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/sub-agent-tasks")
async def list_sub_agent_tasks(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    agent_id: str | None = Query(None, description="Match parent OR target agent"),
    task_status: str | None = Query(None, alias="status", description=_TASK_STATUS_DESCRIPTION),
    scope: str = Query("mine", description="[DEPRECATED] mine | all (all is admin-only); use vis"),
    vis: str | None = Query(None, description="mine | shared | company | all (wins over scope)"),
    search: str | None = Query(None, description="Match description or agent name"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    fleet: Annotated[str | None, Query(description=_TASK_FLEET_DESCRIPTION)] = None,
    liveness: Annotated[Literal["no_live_execution"] | None, Query(description=_TASK_LIVENESS_DESCRIPTION)] = None,
    sort: Annotated[
        Literal[
            "created",
            "created_asc",
            "last_progress",
            "last_progress_asc",
            "status",
            "status_desc",
            "from",
            "from_desc",
            "to",
            "to_desc",
        ],
        Query(description=_TASK_SORT_DESCRIPTION),
    ] = "created",
):
    """List background sub-agent tasks (enriched with agent names + run linkage).

    ``scope='all'`` lists every user's tasks (admin only); otherwise only the
    caller's own. ``offset`` paginates the list (paired with the count
    endpoint). Powers both the chat banner and the Background Tasks control
    screen. ``liveness`` and ``sort`` narrow to the tasks with no live
    execution and order by the execution's last progress.
    """
    from app.services.sub_agent import list_background_tasks

    is_admin = user.role == "admin"
    task_status = _checked_task_status(task_status)
    effective = _task_scope_from_vis(vis, scope, is_admin)
    if effective is None:
        return []
    return await list_background_tasks(
        db,
        requester_id=str(user.id),
        is_admin=is_admin,
        scope=effective,
        status=task_status,
        agent_id=agent_id,
        search=search,
        limit=limit,
        offset=offset,
        fleet_id=parse_uuid(fleet, label="fleet") if fleet else None,
        liveness=liveness,
        sort=sort,
    )


@router.get("/sub-agent-tasks/count")
async def count_sub_agent_tasks(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    agent_id: str | None = Query(None, description="Match parent OR target agent"),
    task_status: str | None = Query(None, alias="status", description=_TASK_STATUS_DESCRIPTION),
    scope: str = Query("mine", description="[DEPRECATED] mine | all (all is admin-only); use vis"),
    vis: str | None = Query(None, description="mine | shared | company | all (wins over scope)"),
    search: str | None = Query(None, description="Match description or agent name"),
    fleet: Annotated[str | None, Query(description=_TASK_FLEET_DESCRIPTION)] = None,
    liveness: Annotated[Literal["no_live_execution"] | None, Query(description=_TASK_LIVENESS_DESCRIPTION)] = None,
):
    """Return the number of background tasks matching the given filters.

    Same filter semantics as the list endpoint. Powers the "Showing X-Y of Z"
    pagination total and the "select all N matching" bulk-selection UI.
    """
    from app.services.sub_agent import count_background_tasks

    is_admin = user.role == "admin"
    task_status = _checked_task_status(task_status)
    effective = _task_scope_from_vis(vis, scope, is_admin)
    if effective is None:
        return {"count": 0}
    count = await count_background_tasks(
        db,
        requester_id=str(user.id),
        is_admin=is_admin,
        scope=effective,
        status=task_status,
        agent_id=agent_id,
        search=search,
        fleet_id=parse_uuid(fleet, label="fleet") if fleet else None,
        liveness=liveness,
    )
    return {"count": count}


@router.get("/sub-agent-tasks/status-counts", response_model=BackgroundTaskStatusCounts)
async def sub_agent_task_status_counts(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    agent_id: str | None = Query(None, description="Match parent OR target agent"),
    scope: str = Query("mine", description="[DEPRECATED] mine | all (all is admin-only); use vis"),
    vis: str | None = Query(None, description="mine | shared | company | all (wins over scope)"),
    search: str | None = Query(None, description="Match description or agent name"),
    fleet: Annotated[str | None, Query(description=_TASK_FLEET_DESCRIPTION)] = None,
    liveness: Annotated[Literal["no_live_execution"] | None, Query(description=_TASK_LIVENESS_DESCRIPTION)] = None,
) -> BackgroundTaskStatusCounts:
    """How many background tasks each status holds under the list's other filters.

    The counts of the Status pill of Operations > Background Tasks (#9378 slice
    8e): the list's filters, never ``status`` itself, so each number is what one
    click on that status shows.
    """
    from app.services.sub_agent import background_task_status_counts

    is_admin = user.role == "admin"
    effective = _task_scope_from_vis(vis, scope, is_admin)
    if effective is None:
        return BackgroundTaskStatusCounts(counts={})
    counts = await background_task_status_counts(
        db,
        requester_id=str(user.id),
        is_admin=is_admin,
        scope=effective,
        agent_id=agent_id,
        search=search,
        fleet_id=parse_uuid(fleet, label="fleet") if fleet else None,
        liveness=liveness,
    )
    return BackgroundTaskStatusCounts(counts=counts)


@router.get("/sub-agent-tasks/segment-counts", response_model=SegmentCounts)
async def sub_agent_task_segment_counts(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    agent_id: str | None = Query(None, description="Match parent OR target agent"),
    task_status: str | None = Query(None, alias="status", description=_TASK_STATUS_DESCRIPTION),
    search: str | None = Query(None, description="Match description or agent name"),
    fleet: Annotated[str | None, Query(description=_TASK_FLEET_DESCRIPTION)] = None,
    liveness: Annotated[Literal["no_live_execution"] | None, Query(description=_TASK_LIVENESS_DESCRIPTION)] = None,
) -> SegmentCounts:
    """How many background tasks each visibility segment holds under the list's other filters.

    Each segment is counted as the list answers it (``_task_scope_from_vis``):
    for an admin All is every user's tasks and Mine + Shared + Company == All;
    for anyone else Shared and Company are 0 and All is Mine.
    """
    from app.services.sub_agent import count_background_tasks

    is_admin = user.role == "admin"
    task_status = _checked_task_status(task_status)
    fleet_id = parse_uuid(fleet, label="fleet") if fleet else None

    async def count(segment: Visibility) -> int:
        effective = _task_scope_from_vis(segment, "mine", is_admin)
        if effective is None:
            return 0
        return await count_background_tasks(
            db,
            requester_id=str(user.id),
            is_admin=is_admin,
            scope=effective,
            status=task_status,
            agent_id=agent_id,
            search=search,
            fleet_id=fleet_id,
            liveness=liveness,
        )

    return await segment_counts(count)


@router.post("/sub-agent-tasks/bulk-delete", response_model=BulkDeleteTasksResponse)
async def bulk_delete_sub_agent_tasks(
    body: BulkDeleteTasksRequest,
    user: User = Depends(get_current_user),
):
    """Delete multiple background tasks at once (owner or admin).

    Two modes: provide ``task_ids`` to delete specific tasks, or set
    ``all_matching_filters=True`` with the same filters as the list endpoint to
    delete every matching task. In-flight tasks are cancelled before removal so
    no orchestration execution is left stranded.
    """
    from app.services.sub_agent import delete_background_tasks

    is_admin = user.role == "admin"
    effective_scope: str | None = body.scope
    if body.all_matching_filters:
        effective_scope = _task_scope_from_vis(body.vis, body.scope, is_admin)
        if effective_scope is None:
            # A non-admin's Shared and Company hold no background task: nothing matches.
            return BulkDeleteTasksResponse(deleted=0)
    if not body.task_ids and not body.all_matching_filters:
        raise HTTPException(
            status_code=400,
            detail="Provide either task_ids or set all_matching_filters=True",
        )
    deleted = await delete_background_tasks(
        requester_id=str(user.id),
        is_admin=is_admin,
        task_ids=body.task_ids,
        all_matching_filters=body.all_matching_filters,
        scope=effective_scope or "mine",
        status=_checked_task_status(body.status),
        agent_id=body.agent_id,
        search=body.search,
    )
    return BulkDeleteTasksResponse(deleted=deleted)


@router.get("/sub-agent-tasks/{task_id}")
async def get_sub_agent_task(
    task_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get details of a specific sub-agent task (legacy)."""
    from app.services.sub_agent import get_task_result

    result = await get_task_result(db, task_id, str(user.id))
    if not result:
        raise HTTPException(status_code=404, detail="Task not found")
    return result


@router.post("/sub-agent-tasks/{task_id}/cancel")
async def cancel_sub_agent_task_endpoint(
    task_id: str,
    user: User = Depends(get_current_user),
):
    """Cancel a background sub-agent task (owner or admin).

    Pending tasks are cancelled outright; a task already running on a worker is
    flipped to ``cancelled`` with its lease released and delivery suppressed —
    the worker bails on its next step rather than being hard-killed mid-tool.
    """
    from app.services.sub_agent import cancel_sub_agent_task

    result = await cancel_sub_agent_task(
        task_id, str(user.id), is_admin=(user.role == "admin")
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return result


@router.delete("/sub-agent-tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sub_agent_task_endpoint(
    task_id: str,
    user: User = Depends(get_current_user),
):
    """Delete a single background task (owner or admin).

    If the task is still in flight it is cancelled first (lease released, a
    running worker bails on its next step) so the row can be removed without
    stranding its orchestration execution.
    """
    from app.services.sub_agent import delete_background_tasks

    deleted = await delete_background_tasks(
        requester_id=str(user.id),
        is_admin=(user.role == "admin"),
        task_ids=[task_id],
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Task not found")


# ---------------------------------------------------------------------------
# Agent Delegations — unified audit trail (consult, transfer, background_task)
# ---------------------------------------------------------------------------

# An admin reads every user's delegations of an agent: the agent page
# shows its delegations whoever started them, like its runs and chats.
# The rows carry conversation content (task descriptions, results), so a read
# that serves rows of OTHER users is an audited oversight read, on the terms of
# the runner command history read: one ``break_glass`` entry per admin,
# per agent, per window, committed before the rows are returned, the marker
# that spaces them written only once the row is committed.
DELEGATION_OVERSIGHT_ACTION = "break_glass"
DELEGATION_OVERSIGHT_ENTITY_TYPE = "agent_delegations"


def delegation_oversight_key(admin_id: object, agent_id: object) -> str:
    """Redis marker for one admin's audited oversight window on one agent."""
    return f"agent:delegations:oversight:{admin_id}:{agent_id}"


async def _audit_delegation_oversight(
    db: AsyncSession,
    user: User,
    agent_id: str,
    rows: list[dict],
    request: Request,
) -> None:
    """Record an admin's read of delegations that other users started.

    No entry when every row served is the admin's own. A Redis failure audits
    anyway (a missing marker must never mean a missing record), and the marker
    is written only after the commit, so a failed audit write fails the read
    and leaves nothing that would cover its retry.
    """
    from app.api.v1.runners import RUNNER_HISTORY_OVERSIGHT_AUDIT_WINDOW_SECONDS
    from app.models.agent import Agent
    from app.services.audit import log_audit

    admin_id = str(user.id)
    owner_ids = sorted({r["user_id"] for r in rows if r["user_id"] != admin_id}, key=str)
    if not owner_ids:
        return

    window = RUNNER_HISTORY_OVERSIGHT_AUDIT_WINDOW_SECONDS
    key = delegation_oversight_key(user.id, agent_id)
    redis = None
    try:
        redis = await redis_client()
        if await redis.exists(key):
            return
    except Exception as exc:
        redis = None
        logger.warning(
            "Delegation oversight marker unavailable (auditing anyway) for "
            "admin=%s agent=%s: %s",
            user.id,
            agent_id,
            format_exception_message(exc),
        )

    agent_uuid = uuid.UUID(agent_id)
    agent_name = (
        await db.execute(select(Agent.name).where(Agent.id == agent_uuid))
    ).scalar_one_or_none()
    known_owner_ids = [uuid.UUID(o) for o in owner_ids if o is not None]
    owner_emails: list[str] = []
    if known_owner_ids:
        owner_emails = sorted(
            (
                await db.execute(select(User.email).where(User.id.in_(known_owner_ids)))
            ).scalars().all()
        )

    shown = ", ".join(f"'{e}'" for e in owner_emails[:5])
    if len(owner_emails) > 5:
        shown += f" and {len(owner_emails) - 5} more"
    await log_audit(
        db,
        user=user,
        action=DELEGATION_OVERSIGHT_ACTION,
        entity_type=DELEGATION_OVERSIGHT_ENTITY_TYPE,
        entity_id=agent_uuid,
        entity_name=agent_name,
        summary=(
            f"Admin '{user.email}' read the delegations of agent "
            f"'{agent_name or agent_id}'"
            + (f" started by {shown}" if shown else " started by other users")
        ),
        details={
            "agent_id": agent_id,
            "delegation_owner_ids": owner_ids,
            "delegation_owner_emails": owner_emails,
            "window_seconds": window,
            "reason": "admin_oversight_agent_delegations",
        },
        request=request,
    )
    await db.commit()
    if redis is None:
        return
    try:
        await redis.set(key, "1", ex=window)
    except Exception as exc:
        logger.warning(
            "Delegation oversight marker not written for admin=%s agent=%s: %s",
            user.id,
            agent_id,
            format_exception_message(exc),
        )


@router.get("/delegations")
async def list_delegations(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    agent_id: str | None = Query(
        None, description="Filter by agent (as parent or target)"
    ),
    parent_agent_id: str | None = Query(None),
    target_agent_id: str | None = Query(None),
    delegation_type: str | None = Query(
        None, alias="type", description="consult | transfer | background_task"
    ),
    delegation_status: str | None = Query(None, alias="status"),
    conversation_id: str | None = Query(None),
    limit: int = Query(50, le=200),
):
    """List agent delegations (consult, transfer, background_task).

    The caller's own delegations, except for an admin reading ONE agent's
    (``agent_id`` / ``parent_agent_id`` / ``target_agent_id``): every user's,
    audited when rows of other users are served
    (:func:`_audit_delegation_oversight`). An unfiltered admin list stays the
    admin's own.
    """
    from app.services.delegation import list_delegations as _list

    if agent_id:
        agent_id = str(parse_uuid(agent_id, label="agent ID"))
    if parent_agent_id:
        parent_agent_id = str(parse_uuid(parent_agent_id, label="parent agent ID"))
    if target_agent_id:
        target_agent_id = str(parse_uuid(target_agent_id, label="target agent ID"))
    if conversation_id:
        conversation_id = str(parse_uuid(conversation_id, label="conversation ID"))

    audited_agent_id = agent_id or parent_agent_id or target_agent_id
    admin_reach = user.is_admin and audited_agent_id is not None

    rows = await _list(
        db,
        None if admin_reach else str(user.id),
        agent_id=agent_id,
        parent_agent_id=parent_agent_id,
        target_agent_id=target_agent_id,
        delegation_type=delegation_type,
        status=delegation_status,
        conversation_id=conversation_id,
        limit=limit,
    )
    if admin_reach:
        await _audit_delegation_oversight(db, user, audited_agent_id, rows, request)
    return rows


@router.get("/delegations/{delegation_id}")
async def get_delegation(
    delegation_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a specific delegation - any user's for an admin, audited on the
    delegating agent when it is another user's."""
    from app.services.delegation import get_delegation as _get

    delegation_uuid = parse_path_id(delegation_id, not_found_detail="Delegation not found")
    result = await _get(
        db, str(delegation_uuid), user_id=None if user.is_admin else str(user.id)
    )
    if not result:
        raise HTTPException(status_code=404, detail="Delegation not found")
    if user.is_admin:
        await _audit_delegation_oversight(
            db, user, result["parent_agent_id"], [result], request
        )
    return result


async def _reconcile_investigation_citations(
    db: AsyncSession, assistant_text: str, investigation_ids: list[str]
) -> str:
    """Neutralise citations the run's sources do not back, and say so.

    The synthesis validates its own draft and refuses one that cites an
    excerpt it was not given. Nothing validated the agent that then rewrote
    that draft for the user — and the first real run through a consulting
    agent produced exactly the gap: markers past the end of the key, in prose
    that read as sourced.

    Failure here is swallowed. The reconciliation improves a message that
    already exists; losing the whole reply because a lookup raised would be a
    far worse trade than leaving a citation unchecked.
    """
    import re as _re
    import uuid as _uuid

    from sqlalchemy import select

    from app.models.investigation import Investigation
    from app.services.investigation_report import (
        UNRESOLVED_MARKER,
        reconcile_citations,
        render_source_key,
        unresolved_disclosure,
    )

    try:
        keys: set[int] = set()
        catalogue: list[dict] = []
        ids: list[_uuid.UUID] = []
        for raw in investigation_ids:
            try:
                ids.append(raw if isinstance(raw, _uuid.UUID) else _uuid.UUID(str(raw)))
            except (ValueError, AttributeError, TypeError):
                continue
        if not ids:
            return assistant_text

        rows = (
            await db.execute(
                select(Investigation.report_sources).where(Investigation.id.in_(ids))
            )
        ).all()
        # Kept per run, never unioned. Every run numbers its sources from [1],
        # so merging two keys makes a marker written against the second run
        # validate against the first and resolve to the WRONG source — which
        # passes silently, because the number is in range. That is strictly
        # worse than an unresolved marker: nothing discloses it, and the
        # citation looks checkable while pointing somewhere else.
        per_run: list[tuple[set[int], list[dict]]] = []
        for (sources,) in rows:
            run_keys: set[int] = set()
            run_catalogue: list[dict] = []
            for source in sources or []:
                if isinstance(source, dict) and isinstance(source.get("n"), int):
                    run_keys.add(source["n"])
                    run_catalogue.append(source)
            if run_keys:
                per_run.append((run_keys, run_catalogue))

        if len(per_run) > 1:
            # Two published keys in one turn. A bare [9] cannot be resolved
            # against either of them, and guessing is the failure this whole
            # path exists to prevent — so every marker is neutralised and both
            # keys are shown, labelled. The per-turn cap should make this
            # unreachable; it is here because "should" is not a guarantee, and
            # the cap was wrong once already.
            logger.warning(
                "%d investigations published keys in one turn; citations "
                "cannot be resolved and were neutralised",
                len(per_run),
            )
            marked = _re.sub(
                r"\[(\d{1,3})\]", UNRESOLVED_MARKER, assistant_text
            )
            blocks = [
                block
                for position, (_keys, cat) in enumerate(per_run, start=1)
                if (
                    block := render_source_key(
                        cat,
                        intro=(
                            f"*Investigation {position} of {len(per_run)} — "
                            "the sources it queried, numbered as that run "
                            "numbered them:*"
                        ),
                    )
                )
            ]
            parts = [
                marked.rstrip(),
                "*This turn ran more than one investigation, and each numbers "
                "its sources from [1]. The markers above cannot be matched to "
                "one of them, so they were removed rather than resolved to "
                "the wrong source.*",
                *blocks,
            ]
            return "\n\n".join(p for p in parts if p)

        if per_run:
            keys, catalogue = per_run[0]

        # No key means no report was published — the run was refused, or
        # produced nothing groundable. There is nothing to reconcile against,
        # and rewriting every bracketed number in the message would mangle
        # text that was never a citation.
        if not keys:
            return assistant_text

        reconciled, replaced = reconcile_citations(assistant_text, keys)

        # Nothing in the message resolves to this run. Either the agent
        # dropped every marker, or it renumbered them into a scheme of its own
        # — `[A1]`, `[A6]` — which no marker-matching can recognise, because
        # they were never this run's numbers. The citations cannot be repaired
        # from here; what the reader can still be given is the list of what
        # was actually queried, stated as the run's.
        if not keys & {
            int(n) for n in _re.findall(r"\[(\d{1,3})\]", reconciled)
        }:
            key_block = render_source_key(catalogue)
            return f"{reconciled.rstrip()}\n\n{key_block}" if key_block else reconciled

        if not replaced:
            return assistant_text

        logger.warning(
            "investigation citations neutralised in an assistant reply: %s",
            ", ".join(f"[{n}]" for n in replaced),
        )
        disclosure = unresolved_disclosure(replaced)
        return f"{reconciled.rstrip()}\n\n{disclosure}" if disclosure else reconciled
    except Exception as exc:
        logger.warning(
            "could not reconcile investigation citations: %s",
            format_exception_message(exc),
        )
        return assistant_text
