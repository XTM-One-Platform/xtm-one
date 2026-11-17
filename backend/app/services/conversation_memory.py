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

"""Conversation memory – automatic compaction with Pinned Directives.

Long conversations easily exceed the LLM context window AND, more
importantly, **dilute the user's original instructions** behind dozens of
tool calls, intermediate answers, and follow-ups.  The previous version
of this module compacted old messages into a single generic "topics
discussed" summary which preserved *what was talked about* but routinely
dropped the *imperative constraints* the user had set at the top of the
conversation (e.g. "use field filter on subregion, not country",
"prefix currency with €", "ADS metrics must be YTD average").  As more
turns piled on, the agent appeared to "forget" those early instructions.

This module now performs **automatic, incremental compaction** with two
co-operating outputs produced by a single LLM call:

1. **Pinned Directives** – a structured block of user-set constraints,
   corrections, formats, and preferences.  It carries forward across
   every compaction cycle and is rendered at the top of the compacted
   system message with strong "obey these going forward" framing, so the
   LLM sees the imperative content even in 100+-turn conversations.
2. **Rolling Summary** – a concise, narrative summary of the prior
   conversation.  Updated *incrementally*: each compaction merges the
   previous summary with the messages being compacted, instead of
   rebuilding from scratch.

The compaction trigger and the recent-window size (``KEEP_RECENT``) work
together with a sliding window: once the history's estimated tokens pass
``COMPACTION_WINDOW_SHARE`` of the model's context window (or its message
count passes the ``SUMMARIZE_THRESHOLD`` safety net), the oldest messages
are merged into the summary and the most recent ``KEEP_RECENT`` messages
are kept verbatim.  Subsequent turns extend the recent window until a
trigger fires again, at which point the next compaction *rolls forward*
the existing summary.

Memory auto-flush remains: before compaction, key facts are extracted
and reconciled into the agent's durable memory (see
``persistent_memory.reconcile_and_persist_facts``).  This ensures
contradictions are resolved (old fact is *superseded* by the new one)
rather than silently coexisting in the durable store.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass

from app.core.redaction import redact_known_secrets_for_display, redact_secrets
from app.core.utils import format_exception_message
from app.llm.base import (
    BaseLLMProvider,
    ChatMessage,
    ChatResponse,
    ContextOverflowError,
)
from app.llm.cut_answers import answer_was_cut, chat_retrying_a_cut
from app.llm.model_limits import retry_output_budget
from app.services.langfuse_tracing import traced_generation

logger = logging.getLogger(__name__)

# Strong references to background flush tasks so the event loop's weak
# reference policy doesn't garbage-collect them while they're still
# running.  Tasks self-remove via ``add_done_callback`` once they finish.
_pending_flush_tasks: set[asyncio.Task] = set()

# ── Context window management constants ──────────────────────────────────
# The compaction trigger reads the model's context window: a cycle runs when
# the estimated tokens of the history pass ``COMPACTION_WINDOW_SHARE`` of the
# window the conversation's model resolves to (the share the agent loop gives
# the session messages, ``agent_loop._SESSION_FRACTION``), estimated the way the
# agent loop estimates them (``agent_loop._CHARS_PER_TOKEN`` characters a
# token). A 1M-window model is summarised at ~600k tokens of history, a
# 128k-window model at ~77k: the share applies to each model's own window.
#
# SUMMARIZE_THRESHOLD is only the safety net on the message count: past it a
# cycle runs whatever the tokens. It stays below the 50 rows
# ``channels.router.get_conversation_history`` loads past the watermark, so
# a conversation is summarised before its oldest messages leave the loaded
# window. Before the window-aware trigger it was the only trigger, at 30: a
# conversation at 29% of a 1M window was summarised every 16 messages.
# MAX_HISTORY_MESSAGES is the hard ceiling used as a fallback when
# summarization fails, and KEEP_RECENT controls how many recent messages are
# kept verbatim. Invariant: MAX_HISTORY_MESSAGES >= SUMMARIZE_THRESHOLD >
# KEEP_RECENT.
#
# CADENCE: after a compaction the history is ``[summary] + KEEP_RECENT
# recent messages``. On the count, the strict ``>`` trigger fires again only
# after a full new batch of ``SUMMARIZE_THRESHOLD - KEEP_RECENT + 1``
# messages (31 with the 45/15 split); on the tokens, only once
# ``_MIN_TOKEN_TRIGGER_BATCH`` messages beyond the recent window have been
# stored and the history crosses the share again. The gap
# (THRESHOLD - KEEP_RECENT) MUST be at least as large as KEEP_RECENT: with the
# old 20/15 split the interval was just 6 messages and users saw
# "Summarizing earlier conversation..." every couple of messages.
MAX_HISTORY_MESSAGES = 50
SUMMARIZE_THRESHOLD = 45  # Safety net: summarize past this many messages
KEEP_RECENT = 15  # Keep this many recent messages in full
COMPACTION_WINDOW_SHARE = 0.60  # Summarize past this share of the window
_CHARS_PER_TOKEN = 3  # Same estimate as the agent loop's gates
# Messages a token-triggered cycle needs beyond the recent window, so a
# recent window that alone weighs past the share is not re-summarised on
# every turn for one or two new messages (the agent loop's in-loop
# compaction handles a heavy recent window).
_MIN_TOKEN_TRIGGER_BATCH = 4
_TRANSCRIPT_CONTENT_CAP = 500  # Max chars per message in transcripts
_TOOL_CONTENT_CAP = 200  # Shorter cap for tool results in transcripts

# Cap on the number of Pinned Directives we carry forward across
# compactions.  Each directive ends up in the system message of *every*
# subsequent turn — unbounded growth here directly burns context window
# (the very problem compaction is meant to solve).  The cap is also
# stated explicitly in the compaction prompt below so the LLM trims
# the list itself; this constant is the post-processing safety net.
_MAX_DIRECTIVES = 20

# Markers used to detect (and round-trip) the compacted system message
# across successive compaction cycles.  Kept stable so the parser can
# extract the prior summary + directives and feed them into the next
# compaction round.
_COMPACTION_MARKER = "[Conversation Compaction]"
_DIRECTIVES_HEADING = "Pinned Directives (still active — obey going forward)"
_SUMMARY_HEADING = "Rolling Summary"

# Compiled patterns for round-tripping the compacted system message.
_DIRECTIVES_BLOCK_RE = re.compile(
    rf"##\s*{re.escape(_DIRECTIVES_HEADING)}\s*\n(?P<body>.*?)(?=\n##\s|\Z)",
    re.DOTALL,
)
_SUMMARY_BLOCK_RE = re.compile(
    rf"##\s*{re.escape(_SUMMARY_HEADING)}\s*\n(?P<body>.*?)(?=\n##\s|\Z)",
    re.DOTALL,
)


def _build_transcript(messages: list[ChatMessage], *, max_entries: int = 30) -> str:
    """Build a readable transcript from chat messages, capped to *max_entries*.

    Includes ``user``, ``assistant``, and ``tool`` messages so that
    tool-heavy conversations retain context about which tools were called
    and what they returned.  Each message's content is truncated to keep
    the transcript compact.
    """
    parts: list[str] = []
    for msg in messages:
        if msg.role in ("user", "assistant"):
            prefix = "User" if msg.role == "user" else "Assistant"
            parts.append(f"{prefix}: {(msg.content or '')[:_TRANSCRIPT_CONTENT_CAP]}")
        elif msg.role == "tool":
            tool_label = msg.name or "unknown"
            parts.append(
                f"Tool ({tool_label}): {(msg.content or '')[:_TOOL_CONTENT_CAP]}"
            )
    return "\n".join(parts[-max_entries:])


def _is_compaction_message(msg: ChatMessage) -> bool:
    """True when *msg* is a previously injected compaction system message."""
    return (
        msg.role == "system"
        and isinstance(msg.content, str)
        and msg.content.startswith(_COMPACTION_MARKER)
    )


# ── Pause after repeated compactions cut at max_tokens ────────────
# A compaction still cut after its retry keeps the previous compaction and
# persists nothing, so the watermark does not move: left alone, every later
# turn of that conversation would pay the compactor calls (and the fact flush)
# again. The first cut is retried on the next turn - a long answer is often a
# one-off. From the second consecutive cut the compactor is not called for that
# conversation for a pause that doubles with every further cut, up to a cap;
# those turns run on the previous compaction and the recent window, the history
# a failed compaction already leaves. A successful compaction ends it. Kept in
# the process, like the models ``anthropic_provider`` learns: a restart or
# another replica may try once more, a bounded cost for no round trip.
_CUT_BACKOFF_BASE_SECS = 15 * 60
_CUT_BACKOFF_MAX_SECS = 6 * 60 * 60
_CUT_BACKOFF_MAX_CONVERSATIONS = 4096
# conversation id -> (consecutive cuts, monotonic time the pause ends)
_compaction_cuts: OrderedDict[str, tuple[int, float]] = OrderedDict()
_monotonic = time.monotonic


def _compaction_paused(conversation_id: str | None) -> bool:
    """True while the compactor calls of *conversation_id* are paused."""
    if conversation_id is None:
        return False
    entry = _compaction_cuts.get(conversation_id)
    return entry is not None and _monotonic() < entry[1]


def _record_compaction_cut(conversation_id: str | None) -> int:
    """Count one more consecutive cut; return the pause it starts, in seconds."""
    if conversation_id is None:
        return 0
    cuts = _compaction_cuts.pop(conversation_id, (0, 0.0))[0] + 1
    pause = 0
    if cuts >= 2:
        pause = min(_CUT_BACKOFF_BASE_SECS * 2 ** min(cuts - 2, 16), _CUT_BACKOFF_MAX_SECS)
    _compaction_cuts[conversation_id] = (cuts, _monotonic() + pause)
    while len(_compaction_cuts) > _CUT_BACKOFF_MAX_CONVERSATIONS:
        _compaction_cuts.popitem(last=False)
    return pause


def compaction_context_window(llm: object, model: str | None) -> int:
    """The context window, in tokens, the compaction trigger measures against.

    Resolved like the agent loop resolves it
    (:func:`app.llm.model_limits.resolve_effective_context_window`): the
    admin per-model override, the model metadata, the family table, the
    provider default, the fallback. The pre-loop trigger and the in-loop
    compaction gate therefore never disagree on how large the window is.
    """
    from app.llm.model_limits import resolve_effective_context_window

    return resolve_effective_context_window(
        model=model,
        overrides=getattr(llm, "context_windows", None),
        provider_default=getattr(llm, "context_window", None),
    )


def history_tokens(history: list[ChatMessage]) -> int:
    """Estimated tokens of *history* as it is sent, the agent loop's estimate."""
    from app.llm.base import message_payload_chars

    return sum(message_payload_chars(m) for m in history) // _CHARS_PER_TOKEN


def should_compact(
    history: list[ChatMessage],
    *,
    conversation_id: str | None = None,
    ahead: int = 0,
    context_window: int | None = None,
) -> bool:
    """True when :func:`manage_conversation_memory` will run a compactor call.

    Single source of truth for the compaction trigger, shared by
    the engine itself and by callers that surface a "summarizing" status
    to the user BEFORE invoking it (the web chat's turn runner).  The
    predicate mirrors the two guards inside
    :func:`manage_conversation_memory`: the raw length check AND the
    check on the *real* message count after stripping a rehydrated
    compaction system message at index 0.  Without the second half, the
    UI announces a summarization on the boundary turn where the engine
    actually early-returns without any LLM call.  Given the
    *conversation_id*, it is also False while that conversation's compactor
    calls are paused after repeated ``max_tokens`` cuts.

    Two triggers, either one enough: the estimated tokens of the history
    (:func:`history_tokens`, the compaction message included since it is
    sent) past ``COMPACTION_WINDOW_SHARE`` of *context_window*, with at least
    ``_MIN_TOKEN_TRIGGER_BATCH`` real messages beyond the recent window; or
    the real message count past ``SUMMARIZE_THRESHOLD``, the safety net.
    Without a *context_window* only the safety net applies. Callers pass
    :func:`compaction_context_window` of the provider and model the
    compactor will run on - the one :func:`manage_conversation_memory`
    measures with - so the status frame and the compactor call agree.

    *ahead* counts messages the history is about to gain: the end of a turn
    asks whether the next one may compact, before the messages the turn
    stored are loaded again (:func:`precompute_compaction`). It adds to the
    count alone; the tokens are those of the history as it is.
    """
    if not history or _compaction_paused(conversation_id):
        return False
    rest = history[1:] if _is_compaction_message(history[0]) else history
    if len(rest) + ahead > SUMMARIZE_THRESHOLD:
        return True
    if context_window is None or context_window <= 0:
        return False
    if len(rest) + ahead < KEEP_RECENT + _MIN_TOKEN_TRIGGER_BATCH:
        return False
    return history_tokens(history) > int(context_window * COMPACTION_WINDOW_SHARE)


# ── Persisted compaction state ────────────────────────────────────
# The compaction output used to live only in the in-memory history of the
# current turn: history is rebuilt from raw DB messages on every turn, so
# the roll-forward detection above could never fire and a FULL summarization
# LLM call re-ran from scratch on every message past SUMMARIZE_THRESHOLD.
# The state is now persisted on ``conversations.compaction_state_json`` and
# rehydrated by ``get_conversation_history`` (which prepends the rendered
# compaction message and loads only messages newer than the watermark), so
# the summarizer runs once per compaction cycle (every
# SUMMARIZE_THRESHOLD - KEEP_RECENT + 1 new messages) and rolls the prior
# directives/summary forward instead of rebuilding them.


def parse_compaction_state(raw_json: str | None) -> dict | None:
    """Parse ``conversations.compaction_state_json`` into a validated dict.

    Returns ``None`` when the column is empty or the payload is malformed
    (callers then fall back to the legacy full-history load).  The returned
    dict has keys ``directives`` (str), ``summary`` (str),
    ``through_sequence`` (int — ``messages.sequence`` watermark of the
    newest message covered by the compaction) and ``compacted_count`` (int).
    """
    if not raw_json:
        return None
    try:
        data = json.loads(raw_json)
    except (TypeError, ValueError):
        logger.warning("Malformed compaction_state_json — ignoring stored state")
        return None
    if not isinstance(data, dict):
        return None
    through_sequence = data.get("through_sequence")
    if not isinstance(through_sequence, int):
        return None
    directives = data.get("directives")
    summary = data.get("summary")
    compacted_count = data.get("compacted_count")
    return {
        "directives": directives if isinstance(directives, str) else "",
        "summary": summary if isinstance(summary, str) else "",
        "through_sequence": through_sequence,
        "compacted_count": compacted_count if isinstance(compacted_count, int) else 0,
    }


def render_stored_compaction(state: dict) -> ChatMessage:
    """Render a persisted compaction state back into the system message."""
    return _render_compaction_message(
        directives=state.get("directives", ""),
        summary=state.get("summary", ""),
        compacted_count=state.get("compacted_count", 0),
    )


def compaction_state_is_stale(state: dict, max_sequence: int | None) -> bool:
    """True when the persisted compaction no longer matches the message log.

    P4.3: the state's ``through_sequence`` watermark
    anchors the summary to ``messages.sequence``.  When messages covered by
    (or up to) the watermark are deleted — a purge/trim that removes the
    newest rows, or the whole thread — ``max(sequence)`` drops BELOW the
    watermark and the summary describes content that no longer exists;
    worse, ``store_message`` restarts the sequence from the new max, so
    the ``sequence > watermark`` history filter would hide every new
    message forever.  Such a state must be discarded (the caller clears it
    and falls back to the full-history load).

    ``max_sequence == through_sequence`` is VALID (compaction just ran and
    no new message has landed yet) — only a strictly smaller max is stale.
    """
    through_sequence = state.get("through_sequence")
    if not isinstance(through_sequence, int):
        return True
    return max_sequence is None or max_sequence < through_sequence


async def clear_compaction_state(
    conversation_id: str, *, observed_through_sequence: int | None = None
) -> None:
    """Clear ``conversations.compaction_state_json`` (best-effort).

    Uses a fresh session (like ``_persist_compaction_state``) so callers
    holding a read-only request session are never affected.  Failures are
    logged and swallowed — a missed clear only means the stale state is
    re-detected (and re-cleared) on the next turn.

    Given *observed_through_sequence* - the watermark of the stale state the
    caller read - the state is cleared only while it still carries that
    watermark, under the row lock ``_persist_compaction_state`` takes: a
    compaction persisted since the read is kept.
    """
    import uuid as _uuid

    try:
        from sqlalchemy import select as _sa_select
        from sqlalchemy import update as _sa_update

        from app.core.database import async_session
        from app.models.chat import Conversation as _Conv

        async with async_session() as db:
            if observed_through_sequence is not None:
                stored = (
                    await db.execute(
                        _sa_select(_Conv.compaction_state_json)
                        .where(_Conv.id == _uuid.UUID(conversation_id))
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                current = parse_compaction_state(stored)
                if (
                    current is not None
                    and current["through_sequence"] != observed_through_sequence
                ):
                    logger.info(
                        "Kept the compaction state of %s (through_sequence=%d): it "
                        "was persisted after the stale one (through_sequence=%d) "
                        "was read",
                        conversation_id,
                        current["through_sequence"],
                        observed_through_sequence,
                    )
                    return
            await db.execute(
                _sa_update(_Conv)
                .where(_Conv.id == _uuid.UUID(conversation_id))
                .values(compaction_state_json=None)
            )
            await db.commit()
        logger.info(
            "Cleared stale compaction state for conversation %s",
            conversation_id,
        )
    except Exception:
        logger.warning(
            "Failed to clear stale compaction state for conversation %s",
            conversation_id,
            exc_info=True,
        )


async def _persist_compaction_state(
    conversation_id: str,
    *,
    directives: str,
    summary: str,
    through_sequence: int,
    compacted_count: int,
) -> None:
    """Persist the compaction state on the conversation row (best-effort).

    Uses a fresh session (like ``_flush_facts_to_memory``) so the caller's
    session — whose pooled connection was released before the compaction
    LLM call — is never touched.  Failures are logged and swallowed:
    a missed persist only means the next turn re-compacts.

    The stored state only ever moves forward: a compaction precomputed after
    a turn (:func:`precompute_compaction`) runs outside the turn lock, so a
    turn on another process may persist a newer one first. The row lock
    orders the two writers, and a state that covers at least as much as this
    one is kept.
    """
    import uuid as _uuid

    try:
        from sqlalchemy import select as _sa_select
        from sqlalchemy import update as _sa_update

        from app.core.database import async_session
        from app.models.chat import Conversation as _Conv

        payload = json.dumps(
            {
                "directives": directives,
                "summary": summary,
                "through_sequence": through_sequence,
                "compacted_count": compacted_count,
            }
        )
        async with async_session() as db:
            stored = (
                await db.execute(
                    _sa_select(_Conv.compaction_state_json)
                    .where(_Conv.id == _uuid.UUID(conversation_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            current = parse_compaction_state(stored)
            if current is not None and current["through_sequence"] >= through_sequence:
                logger.info(
                    "Kept the stored compaction state of %s (through_sequence=%d): it "
                    "covers at least as much as this one (through_sequence=%d)",
                    conversation_id,
                    current["through_sequence"],
                    through_sequence,
                )
                return
            await db.execute(
                _sa_update(_Conv)
                .where(_Conv.id == _uuid.UUID(conversation_id))
                .values(compaction_state_json=payload)
            )
            await db.commit()
        logger.info(
            "Persisted conversation compaction state for %s: %d directive "
            "char(s), %d summary char(s), through_sequence=%d, "
            "compacted_count=%d",
            conversation_id,
            len(directives),
            len(summary),
            through_sequence,
            compacted_count,
        )
    except Exception:
        logger.warning(
            "Failed to persist compaction state for conversation %s — the "
            "next turn will re-compact",
            conversation_id,
            exc_info=True,
        )


# Extracts the cumulative compacted-message count from a rendered
# compaction message preamble so a roll-forward can keep the running total.
_COMPACTED_COUNT_RE = re.compile(
    rf"^{re.escape(_COMPACTION_MARKER)} — (\d+) earlier"
)


def _parse_compaction_message(content: str) -> tuple[str, str]:
    """Extract (directives_block, summary_block) from a compaction message.

    Returns empty strings for missing sections.  The parser tolerates
    minor formatting drift but expects the headings emitted by
    :func:`_render_compaction_message`.
    """
    directives = ""
    summary = ""
    m_dir = _DIRECTIVES_BLOCK_RE.search(content)
    if m_dir:
        directives = m_dir.group("body").strip()
        # An empty placeholder body should round-trip as empty so we don't
        # carry forward "_(none yet)_" into the next prompt.
        if directives.lower() in {"_(none yet)_", "(none yet)", "_(none)_", "(none)"}:
            directives = ""
    m_sum = _SUMMARY_BLOCK_RE.search(content)
    if m_sum:
        summary = m_sum.group("body").strip()
        if summary.lower() in {"_(none yet)_", "(none yet)"}:
            summary = ""
    return directives, summary


def _render_compaction_message(
    *,
    directives: str,
    summary: str,
    compacted_count: int,
) -> ChatMessage:
    """Render the directives + summary block as a single system message.

    The static framing (preamble paragraph instructing the LLM how to
    treat the blocks) lives ABOVE the section headings so that the
    parser — and the next roll-forward — only round-trips the dynamic
    content under each heading.  Without this separation the preamble
    would be concatenated into the directives body on every cycle and
    grow without bound.
    """
    directives_body = directives.strip() or "_(none yet)_"
    summary_body = summary.strip() or "_(none yet)_"

    content = (
        f"{_COMPACTION_MARKER} — {compacted_count} earlier messages compacted.\n\n"
        f"The block below preserves the imperative content of the earlier "
        f"conversation. The directives are CONSTRAINTS, CORRECTIONS, FORMATS, "
        f"and PREFERENCES the user set earlier. **Treat them as active "
        f"instructions** and apply them to every subsequent turn unless the "
        f"user explicitly revokes or supersedes them. When a new user message "
        f"conflicts with a directive below, follow the newer message and "
        f"update your behaviour accordingly — never silently revert to the "
        f"older constraint.\n\n"
        f"## {_DIRECTIVES_HEADING}\n"
        f"{directives_body}\n\n"
        f"## {_SUMMARY_HEADING}\n"
        f"{summary_body}"
    )
    return ChatMessage(role="system", content=content)


# ── Compaction precomputed after a turn ───────────────────────────
# A compaction cycle runs the compactor LLM call before the turn that crosses
# the threshold can start: seconds, up to tens of seconds, of "Summarizing
# earlier conversation" every SUMMARIZE_THRESHOLD - KEEP_RECENT + 1 messages.
# The web chat starts it instead right after the turn that takes the
# conversation past the threshold, while the user reads the answer. It loads
# the history the next turn will load (that turn strips its own new message)
# and compacts it the way that turn would, persisting it like any compaction,
# so the next turn rehydrates the compacted history. A turn that
# starts while it still runs waits for it and catches up with its result
# instead of paying a second compactor call. Kept in the process like the cut
# back-off: a next turn that runs on another process compacts on its own.
_PRECOMPUTED_MAX_CONVERSATIONS = 1024


@dataclass(frozen=True)
class _Precomputed:
    """A persisted compaction: its message and the newest sequence it covers."""

    message: ChatMessage
    through_sequence: int


_precomputed: OrderedDict[str, asyncio.Task[_Precomputed | None]] = OrderedDict()
_running_precomputes: set[asyncio.Task[_Precomputed | None]] = set()


def precompute_compaction(
    llm: BaseLLMProvider,
    *,
    conversation_id: str,
    agent_id: str | None,
    user_id: str | None,
    memory_persistent_enabled: bool,
    model: str | None,
) -> asyncio.Task[_Precomputed | None] | None:
    """Start, in the background, the compaction the next turn would run.

    Returns the task, or ``None`` when one is already running for the
    conversation. It runs in a context of its own: it outlives the turn that
    started it and must not inherit that turn's trace or run state.
    """
    running = _precomputed.get(conversation_id)
    if running is not None and not running.done():
        return None
    task = asyncio.create_task(
        _precompute(
            llm,
            conversation_id=conversation_id,
            agent_id=agent_id,
            user_id=user_id,
            memory_persistent_enabled=memory_persistent_enabled,
            model=model,
        ),
        name=f"compaction-precompute:{conversation_id}",
        context=contextvars.Context(),
    )
    _running_precomputes.add(task)
    task.add_done_callback(_running_precomputes.discard)
    _precomputed[conversation_id] = task
    _precomputed.move_to_end(conversation_id)
    while len(_precomputed) > _PRECOMPUTED_MAX_CONVERSATIONS:
        _precomputed.popitem(last=False)
    return task


async def _precompute(
    llm: BaseLLMProvider,
    *,
    conversation_id: str,
    agent_id: str | None,
    user_id: str | None,
    memory_persistent_enabled: bool,
    model: str | None,
) -> _Precomputed | None:
    token: str | None = None
    try:
        import uuid as _uuid

        from app.channels.router import get_conversation_history
        from app.core.database import async_session

        # Claimed before the history is loaded: a compaction another process
        # persists after this claim is taken is waited for, never repeated.
        held_elsewhere, token = await _claim_compaction(conversation_id)
        if held_elsewhere:
            logger.debug(
                "Compaction of %s already running elsewhere, not precomputed",
                conversation_id,
            )
            return None
        async with async_session() as db:
            history = await get_conversation_history(db, _uuid.UUID(conversation_id))
        window = compaction_context_window(llm, model)
        if not should_compact(history, conversation_id=conversation_id, context_window=window):
            return None
        _, persisted = await _compact_history(
            llm,
            history,
            agent_id=agent_id,
            user_id=user_id,
            memory_persistent_enabled=memory_persistent_enabled,
            model=model,
            conversation_id=conversation_id,
        )
        return persisted
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "Compaction precompute for conversation %s failed, the next turn "
            "compacts on its own: %s",
            conversation_id,
            format_exception_message(exc),
            exc_info=True,
        )
        return None
    finally:
        await _release_compaction_claim(conversation_id, token)


async def _apply_precomputed(
    conversation_id: str, history: list[ChatMessage]
) -> list[ChatMessage]:
    """*history* caught up with the conversation's precomputed compaction.

    A turn that loaded its history before the precompute persisted waits for
    it, then drops the messages it covers and puts its message in front: the
    history ``get_conversation_history`` would load now. A precompute is used
    once, and only by a history that still holds messages it covers - one
    loaded after it already starts past its watermark, possibly behind a
    newer compaction.
    """
    task = _precomputed.get(conversation_id)
    if task is None:
        return history
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if task.cancelled() and not (current is not None and current.cancelling()):
            result = None
        else:
            raise
    if _precomputed.get(conversation_id) is task:
        del _precomputed[conversation_id]
    if result is None:
        return history
    return _catch_up(history, result)


def _catch_up(history: list[ChatMessage], result: _Precomputed) -> list[ChatMessage]:
    """*history* with the messages *result* covers replaced by its message.

    Unchanged when the history holds none of them: it was loaded past the
    watermark, possibly behind a newer compaction.
    """
    rest = history[1:] if history and _is_compaction_message(history[0]) else history
    covered = [
        m
        for m in rest
        if getattr(m, "db_sequence", None) is not None
        and m.db_sequence <= result.through_sequence
    ]
    if not covered:
        return history
    newer = [
        m
        for m in rest
        if getattr(m, "db_sequence", None) is None
        or m.db_sequence > result.through_sequence
    ]
    return [result.message, *newer]


# ── One compaction per conversation across processes ───────────────────────
# A precompute runs outside the turn lock, so the next turn may start on
# another process (another replica, a REST or channel turn) while it still
# runs, and compact the same messages a second time: a second compactor call
# and a second fact flush for one cycle. Whoever runs a compaction holds a
# Redis claim on the conversation: a precompute that finds it held leaves the
# compaction to its holder, a turn waits for the holder and catches up with the
# state it persisted. Fail-open: without Redis each compacts on its own, as
# before, and the stored state still only moves forward.
_CLAIM_POLL_SECS = 0.25
# Short, renewed while the holder runs: a process that dies mid-compaction
# holds up the next turn for at most this long.
_CLAIM_TTL_SECS = 30
_CLAIM_RENEW_SECS = 10
_RELEASE_IF_OWNER_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)
_RENEW_IF_OWNER_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
)
_claim_renewers: dict[str, asyncio.Task[None]] = {}
_orphan_claim_releases: set[asyncio.Task[None]] = set()


def _claim_key(conversation_id: str) -> str:
    return f"compaction:claim:{conversation_id}"


def _claim_wait_cap_secs() -> int:
    # The longest a live holder runs: two compactor calls (the JSON-only retry).
    return 2 * _COMPACTOR_TIMEOUT_SECS + _CLAIM_TTL_SECS


async def _claim_redis():
    from app.core.redis import redis_client

    return await redis_client()


async def _renew_claim(key: str, token: str) -> None:
    # Never renewed past the longest a turn waits on it: a holder stuck longer,
    # or one whose release was lost, stops holding the conversation up.
    until = time.monotonic() + _claim_wait_cap_secs()
    while True:
        await asyncio.sleep(_CLAIM_RENEW_SECS)
        if time.monotonic() >= until:
            return
        try:
            redis = await _claim_redis()
            if not await redis.eval(_RENEW_IF_OWNER_LUA, 1, key, token, _CLAIM_TTL_SECS):
                return
        except Exception as exc:
            logger.debug("Compaction claim %s not renewed: %s", key, format_exception_message(exc))


async def _release_once_claimed(claimed: asyncio.Future, key: str, token: str) -> None:
    try:
        if await claimed:
            redis = await _claim_redis()
            await redis.eval(_RELEASE_IF_OWNER_LUA, 1, key, token)
    except Exception as exc:
        logger.debug(
            "Compaction claim %s of a cancelled caller not released (it expires on its own): %s",
            key,
            format_exception_message(exc),
        )


async def _claim_compaction(conversation_id: str) -> tuple[bool, str | None]:
    """``(held_elsewhere, token)``: the token releases a claim this caller took."""
    import uuid as _uuid

    key = _claim_key(conversation_id)
    token = _uuid.uuid4().hex
    try:
        redis = await _claim_redis()
        claimed = asyncio.ensure_future(redis.set(key, token, nx=True, ex=_CLAIM_TTL_SECS))
        try:
            acquired = await asyncio.shield(claimed)
        except asyncio.CancelledError:
            # The SET may still land, with nobody left holding its token.
            release = asyncio.create_task(_release_once_claimed(claimed, key, token))
            _orphan_claim_releases.add(release)
            release.add_done_callback(_orphan_claim_releases.discard)
            raise
        if not acquired:
            return True, None
    except Exception as exc:
        logger.debug(
            "Compaction claim for %s unavailable, compacting unclaimed: %s",
            conversation_id,
            format_exception_message(exc),
        )
        return False, None
    _claim_renewers[token] = asyncio.create_task(
        _renew_claim(key, token), name=f"compaction-claim:{conversation_id}"
    )
    return False, token


async def _release_compaction_claim(conversation_id: str, token: str | None) -> None:
    if token is None:
        return
    renewer = _claim_renewers.pop(token, None)
    if renewer is not None:
        renewer.cancel()
    try:
        redis = await _claim_redis()
        await redis.eval(_RELEASE_IF_OWNER_LUA, 1, _claim_key(conversation_id), token)
    except Exception as exc:
        logger.debug(
            "Compaction claim for %s not released (it expires on its own): %s",
            conversation_id,
            format_exception_message(exc),
        )


async def _wait_for_compaction_elsewhere(conversation_id: str, deadline: float) -> None:
    """Return once no one holds the conversation's claim, or at *deadline*
    (``time.monotonic()``)."""
    try:
        redis = await _claim_redis()
        while await redis.exists(_claim_key(conversation_id)):
            if time.monotonic() >= deadline:
                return
            await asyncio.sleep(_CLAIM_POLL_SECS)
    except Exception as exc:
        logger.debug(
            "Could not wait on the compaction claim of %s: %s",
            conversation_id,
            format_exception_message(exc),
        )
        # Never a busy loop for a caller that claims again right away.
        await asyncio.sleep(_CLAIM_POLL_SECS)


async def _load_stored_compaction(conversation_id: str) -> dict | None:
    """The conversation's persisted compaction state, ``None`` on any failure."""
    import uuid as _uuid

    try:
        from sqlalchemy import select as _sa_select

        from app.core.database import async_session
        from app.models.chat import Conversation as _Conv

        async with async_session() as db:
            raw = (
                await db.execute(
                    _sa_select(_Conv.compaction_state_json).where(
                        _Conv.id == _uuid.UUID(conversation_id)
                    )
                )
            ).scalar_one_or_none()
        return parse_compaction_state(raw)
    except Exception as exc:
        logger.debug(
            "Could not read the stored compaction of %s: %s",
            conversation_id,
            format_exception_message(exc),
        )
        return None


async def _catch_up_with_stored(
    conversation_id: str, history: list[ChatMessage]
) -> list[ChatMessage]:
    """*history* caught up with a compaction another process persisted."""
    state = await _load_stored_compaction(conversation_id)
    if state is None:
        return history
    max_sequence = max(
        (m.db_sequence for m in history if getattr(m, "db_sequence", None) is not None),
        default=None,
    )
    if compaction_state_is_stale(state, max_sequence):
        return history
    return _catch_up(
        history,
        _Precomputed(render_stored_compaction(state), state["through_sequence"]),
    )


async def manage_conversation_memory(
    llm: BaseLLMProvider,
    history: list[ChatMessage],
    *,
    agent_id: str | None = None,
    user_id: str | None = None,
    is_company_managed: bool = False,
    memory_persistent_enabled: bool = True,
    model: str | None = None,
    conversation_id: str | None = None,
) -> list[ChatMessage]:
    """Apply sliding-window compaction with Pinned Directives.

    The function is **idempotent** when the conversation is short enough
    (:func:`should_compact` returns ``False`` — the estimated tokens of the
    history are at or below ``COMPACTION_WINDOW_SHARE`` of the model's
    context window and the *real* message count, excluding a rehydrated
    compaction system message at index 0, is at or below
    ``SUMMARIZE_THRESHOLD``): it returns the input unchanged.
    Once a trigger fires, the oldest messages (everything
    except the last ``KEEP_RECENT``) are compacted into a single
    structured system message containing:

    - a **Pinned Directives** block (user-set constraints / corrections)
    - a **Rolling Summary** block (narrative recap)

    On subsequent compactions the previous compaction message is
    detected, parsed, and **rolled forward** so that directives from
    early in the conversation survive indefinitely.

    When ``agent_id`` is provided **and** ``memory_persistent_enabled``
    is ``True``, key facts are extracted from the messages being
    compacted and reconciled into the agent's durable memory via
    :func:`persistent_memory.reconcile_and_persist_facts` so that
    contradictions (e.g. a corrected filter) supersede old facts rather
    than coexisting with them.

    When *is_company_managed* is ``True``, the fact extraction focuses
    on organisational knowledge rather than personal user details.

    When *conversation_id* is provided, a successful compaction is
    persisted on ``conversations.compaction_state_json`` so
    follow-up turns rehydrate the summary instead of re-running the
    summarizer LLM call on every message: ``get_conversation_history``
    prepends the stored compaction message and only loads messages newer
    than the persisted sequence watermark, which also makes the
    roll-forward path here actually fire across turns.

    Returns a (possibly shorter) message list with the compaction system
    message prepended (and the prior compaction message replaced, if
    one was present).
    """
    # Trigger gate: ``should_compact`` is the single source of
    # truth shared with callers that surface a "summarizing" status to
    # the user.  It counts *real* messages (a rehydrated compaction
    # system message at index 0 is excluded), so the re-compaction
    # cadence is a full ``SUMMARIZE_THRESHOLD - KEEP_RECENT + 1`` batch
    # of new messages — never a per-turn thrash.  It measures the history
    # against the context window of the model the compactor runs on.
    window = compaction_context_window(llm, model)
    if conversation_id is not None and should_compact(history, context_window=window):
        history = await _apply_precomputed(conversation_id, history)
    if not should_compact(history, context_window=window):
        return history
    token: str | None = None
    # Released on every way out once taken, a cancelled turn included: a claim
    # left behind holds up every later compaction of the conversation.
    try:
        if conversation_id is not None:
            deadline = time.monotonic() + _claim_wait_cap_secs()
            held_elsewhere, token = await _claim_compaction(conversation_id)
            # Wait for each holder in turn until this turn owns the claim or no
            # compaction is left to run. Only past the longest a live holder runs
            # does the turn compact unclaimed, rather than stall.
            while held_elsewhere:
                await _wait_for_compaction_elsewhere(conversation_id, deadline)
                history = await _catch_up_with_stored(conversation_id, history)
                if not should_compact(history, context_window=window):
                    return history
                if time.monotonic() >= deadline:
                    logger.warning(
                        "Compaction claim of %s still held after %ds, compacting unclaimed",
                        conversation_id,
                        _claim_wait_cap_secs(),
                    )
                    break
                held_elsewhere, token = await _claim_compaction(conversation_id)
            # A compaction another process persisted since this history was
            # loaded (just before the claim was taken).
            history = await _catch_up_with_stored(conversation_id, history)
            if not should_compact(history, context_window=window):
                return history
        compacted, _ = await _compact_history(
            llm,
            history,
            agent_id=agent_id,
            user_id=user_id,
            memory_persistent_enabled=memory_persistent_enabled,
            model=model,
            conversation_id=conversation_id,
        )
        return compacted
    finally:
        if conversation_id is not None:
            await _release_compaction_claim(conversation_id, token)


async def _compact_history(
    llm: BaseLLMProvider,
    history: list[ChatMessage],
    *,
    agent_id: str | None,
    user_id: str | None,
    memory_persistent_enabled: bool,
    model: str | None,
    conversation_id: str | None,
) -> tuple[list[ChatMessage], _Precomputed | None]:
    """One compaction cycle of a history past the trigger.

    Returns the history the turn runs on and, when the compaction was
    persisted, what a turn that loaded its history before the persist needs
    to catch up with it (:func:`_apply_precomputed`).
    """
    # ── Detect & strip prior compaction (roll-forward) ────────────────
    # The compaction message, when present, lives at index 0 of the
    # history (it's a system message we prepended on a previous turn —
    # or, with persisted compaction state, rehydrated from the DB by
    # ``get_conversation_history``).
    prior_directives = ""
    prior_summary = ""
    prior_compacted_count = 0
    rest = history
    if _is_compaction_message(history[0]):
        prior_directives, prior_summary = _parse_compaction_message(
            history[0].content or ""
        )
        _count_match = _COMPACTED_COUNT_RE.match(history[0].content or "")
        if _count_match:
            prior_compacted_count = int(_count_match.group(1))
        rest = history[1:]

    old_messages = rest[:-KEEP_RECENT]
    recent_messages = rest[-KEEP_RECENT:]

    if _compaction_paused(conversation_id):
        logger.debug(
            "Compaction of conversation %s paused after repeated max_tokens cuts",
            conversation_id,
        )
        return _keep_previous_compaction(
            history, prior_directives, prior_summary, recent_messages
        ), None

    # ── Memory auto-flush: reconcile key facts into durable memory ──
    # Scoped per-user when a user is identified; skipped for anonymous
    # users (no durable cross-session memory) and when persistent
    # memory is disabled on the agent.
    #
    # The flush is fire-and-forget — we don't await it because the
    # caller (an HTTP request) shouldn't wait on a multi-second LLM
    # call.  We keep a strong reference in ``_pending_flush_tasks`` so
    # the task isn't GC'd mid-run, and use ``async with db.begin()``
    # inside the task to keep the SUPERSEDE DELETE + INSERT atomic
    # even if the task is cancelled during shutdown.
    if agent_id and old_messages and user_id and memory_persistent_enabled:
        task = asyncio.create_task(
            _flush_facts_to_memory(
                llm,
                old_messages,
                agent_id=agent_id,
                user_id=user_id,
                is_company_managed=False,
                model=model,
            )
        )
        _pending_flush_tasks.add(task)
        task.add_done_callback(_pending_flush_tasks.discard)

    # ── Compact: produce directives + summary in a single LLM call ──
    try:
        directives, summary = await _compact_messages(
            llm,
            old_messages,
            prior_directives=prior_directives,
            prior_summary=prior_summary,
            model=model,
        )
        total_compacted = prior_compacted_count + len(old_messages)
        compaction_msg = _render_compaction_message(
            directives=directives,
            summary=summary,
            compacted_count=total_compacted,
        )
        # ── Persist the compaction so follow-up turns reuse it ──
        # The watermark is the DB sequence of the newest message covered
        # by this compaction (stamped by ``get_conversation_history``).
        # Without a watermark (synthetic history, e.g. tests or callers
        # that build messages by hand) the state cannot be anchored to
        # the message log, so we skip persistence — behaviour then
        # matches the legacy per-turn compaction.
        persisted: _Precomputed | None = None
        if conversation_id is not None:
            _through_sequence = max(
                (
                    m.db_sequence
                    for m in old_messages
                    if getattr(m, "db_sequence", None) is not None
                ),
                default=None,
            )
            if _through_sequence is not None:
                await _persist_compaction_state(
                    conversation_id,
                    directives=directives,
                    summary=summary,
                    through_sequence=_through_sequence,
                    compacted_count=total_compacted,
                )
                persisted = _Precomputed(compaction_msg, _through_sequence)
            else:
                logger.debug(
                    "Compaction for conversation %s not persisted — no DB "
                    "sequence watermark on the compacted messages",
                    conversation_id,
                )
        if conversation_id is not None:
            _compaction_cuts.pop(conversation_id, None)
        return [compaction_msg, *recent_messages], persisted
    except ContextOverflowError as exc:
        # Pre-flight overflow guard tripped — the compactor itself can't
        # fit in the window. Fall back to either re-attaching the prior
        # compaction (if any) or the recent-window hard cap. ``exc`` carries
        # provider-resolved token counts; surface them in the log so
        # operators can size deployments accordingly.
        logger.warning(
            "Skipping compaction: conversation exceeds context window "
            "(%s tokens vs %s limit)",
            exc.estimated_tokens,
            exc.context_window,
        )
        return _keep_previous_compaction(
            history, prior_directives, prior_summary, recent_messages
        ), None
    except CompactionTruncatedError as exc:
        pause = _record_compaction_cut(conversation_id)
        logger.warning(
            "Compaction of conversation %s skipped, previous compaction kept: %s "
            "(%s)",
            conversation_id,
            format_exception_message(exc),
            f"compactor paused for {pause // 60} min after repeated cuts"
            if pause
            else "retried on the next turn",
        )
        return _keep_previous_compaction(
            history, prior_directives, prior_summary, recent_messages
        ), None
    except Exception as exc:
        logger.warning("Failed to compact conversation: %s", format_exception_message(exc), exc_info=True)
        # Fallback: drop to the recent window but preserve the previous
        # compaction message if we had one so the directives don't die.
        return _keep_previous_compaction(
            history, prior_directives, prior_summary, recent_messages
        ), None


def _keep_previous_compaction(
    history: list[ChatMessage],
    prior_directives: str,
    prior_summary: str,
    recent_messages: list[ChatMessage],
) -> list[ChatMessage]:
    """The history of a turn whose compaction cycle compacted nothing.

    The previous compaction, when there is one, in front of the recent
    window - so the accumulated directives survive - else the recent-window
    hard cap. ``compacted_count=0`` reflects reality: the old messages were
    NOT compacted this cycle (the count would otherwise be misleading in logs
    and the preamble text).
    """
    if prior_directives or prior_summary:
        return [
            _render_compaction_message(
                directives=prior_directives,
                summary=prior_summary,
                compacted_count=0,
            ),
            *recent_messages,
        ]
    return history[-MAX_HISTORY_MESSAGES:]


async def _flush_facts_to_memory(
    llm: BaseLLMProvider,
    messages: list[ChatMessage],
    *,
    agent_id: str,
    user_id: str | None,
    is_company_managed: bool = False,
    model: str | None = None,
) -> None:
    """Extract key facts from messages about to be compacted and reconcile.

    Uses :func:`persistent_memory.reconcile_and_persist_facts` so that
    new facts contradicting existing ones supersede the old entries,
    instead of silently coexisting and confusing the agent on future
    turns.

    This coroutine is launched via :func:`asyncio.create_task` from
    :func:`manage_conversation_memory` and runs detached from the caller's
    request lifecycle.  Because of that:

    - Atomicity of the SUPERSEDE DELETE + replacement INSERT pair lives
      *inside* :func:`reconcile_and_persist_facts` now: the apply phase
      runs in one short transaction that the function commits itself.
      There is deliberately NO outer ``db.begin()`` here — a
      whole-function transaction used to pin one pooled DB connection
      across the extraction LLM call, N embedding round-trips and up to
      two curator LLM calls (30-90s per conversation compaction), which
      was a main driver of the production pool-exhaustion warnings.
      A mid-flight cancel still cannot leave deletions without
      replacements: nothing is committed until the apply phase's single
      commit.
    - :class:`asyncio.CancelledError` is caught and logged separately so
      a task cancellation during shutdown is visible in logs (and isn't
      mis-attributed to a real reconciliation failure).
    """
    import asyncio as _asyncio

    try:
        transcript = _build_transcript(messages, max_entries=20)
        if not transcript:
            return

        from app.core.database import async_session
        from app.services.persistent_memory import reconcile_and_persist_facts

        async with async_session() as db:
            result = await reconcile_and_persist_facts(
                db,
                llm,
                agent_id=agent_id,
                messages_text=transcript,
                user_id=user_id,
                is_company_managed=is_company_managed,
                model=model,
            )
            if result.get("curator_degraded"):
                logger.warning(
                    "Auto-flush (agent %s): reconciliation curator degraded — "
                    "facts persisted as plain ADDs without conflict resolution",
                    agent_id,
                )
            if result["added"] or result["superseded"] or result.get("merged"):
                logger.info(
                    "Auto-flush (agent %s): %d added, %d superseded, %d merged, "
                    "%d old facts deleted, %d skipped",
                    agent_id,
                    result["added"],
                    result["superseded"],
                    result.get("merged", 0),
                    result.get("deleted", 0),
                    result["skipped"],
                )
    except _asyncio.CancelledError:
        # Task cancelled (typically during graceful shutdown).  The
        # ``async with async_session()`` exit discards any uncommitted
        # apply-phase work, so no partial state is left behind.  Re-raise
        # so the event loop can finish unwinding the task.
        logger.info(
            "Memory auto-flush cancelled for agent %s — DB transaction rolled back",
            agent_id,
        )
        raise
    except Exception as exc:
        logger.warning(
            "Memory auto-flush failed for agent %s: %s",
            agent_id,
            format_exception_message(exc),
            exc_info=True,
        )


# ── Compaction prompt ────────────────────────────────────────────────────

_COMPACTION_SYSTEM_PROMPT = """You are a conversation compactor for an AI agent.

Your task is to read a conversation excerpt (and, optionally, the result
of a previous compaction) and produce TWO outputs:

1. **directives** — A bullet list of every active CONSTRAINT, CORRECTION,
   PREFERENCE, FORMAT REQUIREMENT, or RULE the user has set. These are
   imperative instructions the agent must continue to follow IN THIS
   CONVERSATION.

   Include:
   - User-stated preferences for the current task (e.g. "use field
     filter on subregion, not country", "ADS metrics must be YTD
     average", "currency prefix is €", "always prefix dataset names
     with `prod_`").
   - Explicit corrections the user made to earlier agent output
     (e.g. "no — those should be subregions, not countries").
   - Specific data sources, filters, dimensions, dashboards, or
     identifiers the user pinned.
   - Output format rules (markdown / JSON / plain / units / date
     formats / number formats).

   When a new statement supersedes an earlier one (e.g. "actually use
   subregion, not country"), output ONLY the newer version. Never list
   both the old and new form.

   Exclude:
   - The user's preferred reply language, locale, tone, formality,
     or general communication style. Those are cross-agent personal
     preferences — they belong in the user's platform-wide profile,
     not in this agent's conversation directives. Capturing them here
     would make the agent stick to (e.g.) French even after the user
     switched to English with a different agent.
   - The user's name, role, team, or other identity details (also
     part of the cross-agent user profile).
   - The agent's own decisions or assumptions (unless the user
     confirmed them).
   - Generic conversation topics ("we discussed dashboards").
   - One-off questions that have been fully answered.
   - Implementation details the agent worked out internally.

   Keep the directives list short — at most 20 items. If you have more,
   collapse near-duplicates and drop the least imperative ones first.
   If no directives are present, output an empty list.

2. **summary** — A concise narrative summary (max 200 words) of what
   was discussed, what was decided, and what is still pending. Use
   bullet points. Focus on:
   - Key topics covered and decisions made.
   - Important data, names, IDs, or references mentioned.
   - Open questions or tasks still pending.
   Do NOT repeat the directives in this section — they belong in the
   directives output above.

Output a single JSON object with this exact shape:
{
  "directives": ["...", "..."],
  "summary": "- ...\\n- ..."
}

Output ONLY the JSON object. No code fences, no preamble, no commentary."""


# Strict JSON Schema for the compactor output. Used both as a contract for
# native structured output (OpenAI ``response_format`` / Anthropic
# ``output_config.format`` / Gemini ``response_schema`` / Bedrock
# ``outputConfig.textFormat``) and as the validation target for the SAP
# pipeline when the provider doesn't support native enforcement.
#
# Keep the schema minimal — ``additionalProperties: false`` is added by
# ``build_native_structured_output``'s normalizer, and a tiny schema
# survives all four provider-specific transforms without surprises.
_COMPACTOR_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "directives": {
            "type": "array",
            "items": {"type": "string"},
        },
        "summary": {"type": "string"},
    },
    "required": ["directives", "summary"],
}

# Output cap for the compactor LLM call. 700 was too tight for a full
# 20-directive list with a 200-word summary — JSON regularly got
# truncated mid-bracket, which guaranteed a parse failure on the back
# end and silently dropped every freshly extracted directive.
# Bumped again for Claude Sonnet 5's new tokenizer, which emits ~30% more
# tokens for the same text (so the old 1200 cap holds ~30% less content).
# An answer that still hits it is retried once with twice the budget.
_COMPACTOR_MAX_TOKENS = 2000


class CompactionTruncatedError(RuntimeError):
    """The compactor's answer was still cut at ``max_tokens`` after its retry.

    Raised instead of returning a compaction built from a partial answer, so
    ``manage_conversation_memory`` keeps the previous compaction and persists
    nothing: the same messages are compacted again on the next turn.
    """


# Wall-clock cap (seconds) on ONE compactor LLM call.  The compaction runs
# in the pre-loop phase where the user sees only a static "Thinking..."
# indicator; without this cap the call was bounded only by the provider
# SDK's HTTP read timeout (up to 600 s — and there can be two calls per
# cycle with the JSON-only retry).  On timeout the caller's fallback path
# keeps the prior directives/summary, so nothing is lost.
_COMPACTOR_TIMEOUT_SECS = 120


@dataclass
class _CompactionParse:
    """Result of parsing a compactor response.

    ``clean`` distinguishes a usable JSON object (the Schema-Aligned
    Parsing pipeline returned directives and/or a summary) from a
    fallback recovery (the model returned prose / bullets that we
    salvaged after JSON parsing failed).  ``_compact_messages`` uses the
    flag to decide whether to trust the model's directive roll-forward
    (clean) or to re-attach the carried-forward ``prior_directives``
    itself (not clean) so accumulated user constraints are never
    silently wiped by a transient non-JSON response.
    """

    directives: list[str]
    summary: str
    clean: bool
    raw_len: int
    parse_error: str | None


# Matches a SINGLE leading list marker (``-`` / ``*`` / ``•``) followed by
# whitespace, with optional leading indentation. Used to strip the bullet
# glyph the renderer (and occasionally the LLM) prepends WITHOUT corrupting
# directives whose text legitimately begins with one of those characters
# (e.g. ``-O2``, ``*args``, ``*.csv``). Only one marker is removed and only
# when whitespace follows it, so ``- -O2`` round-trips to ``-O2`` instead of
# being mangled to ``O2``.
_BULLET_MARKER_RE = re.compile(r"^\s*[-*•]\s+")


def _strip_bullet_marker(text: str) -> str:
    """Strip a single leading ``- `` / ``* `` / ``• `` list marker, if present.

    Removes at most one bullet glyph, and only when it is followed by
    whitespace, so directive text that legitimately starts with a dash,
    asterisk, or bullet (``-O2``, ``*args``, ``*.csv``) is preserved
    verbatim.  Surrounding whitespace is trimmed from the result.
    """
    return _BULLET_MARKER_RE.sub("", text, count=1).strip()


def _split_directive_lines(block: str) -> list[str]:
    """Split a rendered Pinned Directives block into individual directives.

    Strips the leading bullet glyph (``-`` / ``*`` / ``•``) from each
    line so a carried-forward block round-trips back into the same shape
    the compactor would have emitted.  Used to re-attach prior directives
    when the compactor fails to return parseable JSON (and therefore
    could not roll them forward itself).
    """
    lines: list[str] = []
    for line in block.splitlines():
        text = _strip_bullet_marker(line)
        if text:
            lines.append(text)
    return lines


async def _run_compactor_chat(
    llm: BaseLLMProvider,
    messages: list[ChatMessage],
    chat_kwargs: dict,
    *,
    trace_name: str,
) -> ChatResponse:
    """Invoke the compactor LLM once, with tracing + structured-output guard.

    ``chat_kwargs`` must NOT contain ``messages`` — the message list is
    supplied separately so the same kwargs (``max_tokens`` /
    ``temperature`` / ``structured_output``) can be reused for both the
    initial call and the JSON-only retry without mutating the caller's
    dict.

    The narrow ``TypeError`` fallback retries once *without*
    ``structured_output`` when (and only when) the provider stub rejects
    that exact keyword argument — every other ``TypeError`` propagates so
    a real SDK bug isn't masked by a silently doubled call.
    """
    kw = dict(chat_kwargs)
    kw["messages"] = messages
    async with traced_generation(name=trace_name, messages=messages) as _gen:
        try:
            # ``_COMPACTOR_TIMEOUT_SECS`` bounds the call: compaction runs in
            # the pre-loop phase (static "Thinking..." for the user), so it
            # must never ride the provider SDK's 600 s HTTP read timeout.  A
            # TimeoutError propagates to ``manage_conversation_memory``'s
            # fallback, which preserves the prior directives.
            _gen.response = await asyncio.wait_for(
                llm.chat(**kw), timeout=_COMPACTOR_TIMEOUT_SECS
            )
        except TypeError as exc:
            message = str(exc)
            # Optional provider hints that an old provider without ``**kwargs``
            # rejects by name (canonical "unexpected keyword argument 'X'").
            # If the TypeError names one we actually passed, strip them ALL and
            # retry once (a signature that rejects one usually rejects the
            # others too). Every other TypeError propagates so a real SDK bug is
            # not masked by a silently doubled call.
            optional_hints = ("structured_output", "disable_thinking")
            if not any(h in kw and h in message for h in optional_hints):
                raise
            for hint in optional_hints:
                kw.pop(hint, None)
            _gen.response = await asyncio.wait_for(
                llm.chat(**kw), timeout=_COMPACTOR_TIMEOUT_SECS
            )
    return _gen.response


async def _compact_messages(
    llm: BaseLLMProvider,
    messages: list[ChatMessage],
    *,
    prior_directives: str = "",
    prior_summary: str = "",
    model: str | None = None,
) -> tuple[str, str]:
    """Compact *messages* into (directives_block, summary_block).

    When ``prior_directives`` / ``prior_summary`` are non-empty (from a
    previous compaction cycle) they are folded into the input so the
    compactor can roll them forward, removing now-superseded directives
    and extending the narrative summary.

    Returns the directives bullet list and the rolling summary as raw
    Markdown strings ready for inclusion in the compaction system
    message.

    The LLM call is steered into well-formed JSON by passing a strict
    JSON Schema through the provider-native ``structured_output`` kwarg
    when supported (OpenAI / Anthropic / Gemini / Bedrock / OpenAI-
    compatible). For providers without native enforcement, the
    Schema-Aligned Parsing pipeline (extract → json-repair → validate)
    still recovers from the most common malformations.
    """
    transcript = _build_transcript(messages, max_entries=MAX_HISTORY_MESSAGES)

    user_blocks: list[str] = []
    if prior_directives:
        user_blocks.append(
            "Previous Pinned Directives (still active unless contradicted "
            "by new messages below):\n" + prior_directives
        )
    if prior_summary:
        user_blocks.append(
            "Previous Rolling Summary (extend / refine with new info below):\n"
            + prior_summary
        )
    user_blocks.append("New conversation excerpt to compact:\n\n" + transcript)
    user_content = "\n\n".join(user_blocks)

    # Build native structured-output config when the provider supports
    # it. The helper returns ``None`` for providers without a JSON
    # response-format API; in that case we still rely on the SAP
    # pipeline below to repair the raw text.
    from app.services.structured_output import (
        build_native_structured_output,
        build_schema_prompt,
        unwrap_failover_llm,
    )

    compaction_messages = [
        ChatMessage(role="system", content=_COMPACTION_SYSTEM_PROMPT),
        ChatMessage(role="user", content=user_content),
    ]
    # ``chat_kwargs`` intentionally excludes ``messages`` so it can be
    # reused for the JSON-only retry below without mutation.
    chat_kwargs: dict = {
        "model": model,
        "max_tokens": _COMPACTOR_MAX_TOKENS,
        "temperature": 0.2,
        # Mechanical directives+summary extraction — no reasoning needed. On
        # Claude Sonnet 5 / Opus 4.7+ adaptive thinking is ON by default and
        # shares the max_tokens budget with the response, so disable it to give
        # the whole budget to the JSON (the Anthropic provider only honours this
        # for models that support it; other providers ignore the kwarg).
        "disable_thinking": True,
    }
    structured_output_cfg = build_native_structured_output(llm, _COMPACTOR_SCHEMA)
    if structured_output_cfg is not None:
        chat_kwargs["structured_output"] = structured_output_cfg

    response = await _run_compactor_chat(
        llm, compaction_messages, chat_kwargs, trace_name="compaction_summary"
    )
    raw = (response.content or "").strip()
    parsed = _parse_compaction_response(raw)
    # An answer cut at ``max_tokens`` is never used, however clean it parses:
    # json-repair closes the cut body, so a directive list cut short reads as
    # a complete roll-forward and would replace the prior directives.
    cut = answer_was_cut(response)

    # ── One retry: a doubled budget for a cut answer, JSON-only for prose ──
    # A cut answer gets the same request with twice the budget (the
    # idiom of ``agent_memory.consolidate_memory``), capped at the model's
    # output ceiling so the retry is not itself an HTTP 400. When the first
    # response wasn't usable JSON — the provider ignored the response_format
    # (common with self-hosted OpenAI-compatible endpoints), an older
    # Anthropic model fell back to prompt-only, or the model just emitted
    # prose — it gets one more chance with the schema restated and a blunt
    # "JSON only" instruction. Bounded to a single extra call so a model that
    # *never* emits JSON cannot double the compaction cost indefinitely.
    retry_budget = _COMPACTOR_MAX_TOKENS
    if cut or not parsed.clean:
        if cut:
            retry_budget = retry_output_budget(llm, model, _COMPACTOR_MAX_TOKENS)
            logger.warning(
                "Compactor answer hit the %d-token output budget (model=%r) - "
                "retrying once with max_tokens=%d",
                _COMPACTOR_MAX_TOKENS,
                getattr(response, "model", None),
                retry_budget,
            )
            retry_messages = compaction_messages
            retry_kwargs = {**chat_kwargs, "max_tokens": retry_budget}
        else:
            retry_reminder = (
                "Your previous response could not be parsed as JSON.\n\n"
                + build_schema_prompt(_COMPACTOR_SCHEMA, name="compaction")
                + "\n\nReturn ONLY the JSON object — start with '{' and end with "
                "'}'. No markdown fences, no preamble, no commentary."
            )
            retry_messages = [
                *compaction_messages,
                ChatMessage(role="user", content=retry_reminder),
            ]
            retry_kwargs = chat_kwargs
        retry_response = await _run_compactor_chat(
            llm,
            retry_messages,
            retry_kwargs,
            trace_name="compaction_summary_retry",
        )
        retry_raw = (retry_response.content or "").strip()
        retry_parsed = _parse_compaction_response(retry_raw)
        retry_cut = answer_was_cut(retry_response)
        # A complete retry replaces a cut answer even as prose (the prose
        # fallback below then runs on it); after a prose answer only a clean
        # retry does.
        if not retry_cut and (cut or retry_parsed.clean):
            response, raw, parsed = retry_response, retry_raw, retry_parsed
            cut = False
    if cut:
        raise CompactionTruncatedError(
            "Compactor answer still cut at max_tokens after a retry with "
            f"max_tokens={retry_budget}"
        )

    # ── Preserve prior directives when the compactor failed ───────────
    # On a clean parse the model already rolled the previous directives
    # forward (that is the contract of the prompt), so we use exactly
    # what it returned. On a FAILED parse the model almost certainly did
    # NOT honour that contract — returning empty here would silently wipe
    # every accumulated user constraint. Re-attach the carried-forward
    # directives instead, with any recovered bullets appended after them.
    recovered_list = parsed.directives
    prior_list: list[str] = []
    if not parsed.clean and prior_directives:
        prior_list = _split_directive_lines(prior_directives)
    directives_list = [*prior_list, *recovered_list]

    # Render directives as a Markdown bullet list.  Strip a single leading
    # "-" / "*" / "•" marker (when followed by whitespace) that the LLM may
    # have left so we render uniform bullets regardless of provider quirks,
    # without corrupting directives whose text legitimately starts with one
    # of those characters (e.g. "-O2", "*args").
    cleaned_directives: list[str] = []
    seen: set[str] = set()
    for item in directives_list:
        if not isinstance(item, str):
            continue
        text = _strip_bullet_marker(item)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned_directives.append(f"- {text}")

    # Safety net: even though the prompt asks for at most 20 items, a
    # model can ignore that.  Hard-cap the list so unbounded growth
    # across compactions can't blow up the system message size.
    if len(cleaned_directives) > _MAX_DIRECTIVES:
        logger.warning(
            "Compactor returned %d directives (> cap of %d) — truncating",
            len(cleaned_directives),
            _MAX_DIRECTIVES,
        )
        cleaned_directives = cleaned_directives[:_MAX_DIRECTIVES]

    directives_block = "\n".join(cleaned_directives)
    # Prefer the fresh summary; fall back to the prior summary so a
    # failed parse never drops the narrative recap either.
    summary_block = parsed.summary.strip() or prior_summary.strip()

    # ── Context-aware diagnostics ─────────────────────────────────────
    # The raw parse failure is logged HERE (not inside the parser) so we
    # can surface the provider/model that produced the non-JSON output
    # and downgrade to INFO when no data was actually lost.
    if not parsed.clean:
        target = unwrap_failover_llm(llm)
        provider = getattr(target, "provider_name", None) or getattr(
            llm, "provider_name", None
        )
        used_model = getattr(response, "model", None) or getattr(
            target, "default_model", None
        )
        if prior_list:
            # Common case in long conversations: the model returned prose,
            # but we kept every prior directive and used the prose as the
            # summary — no data lost, so INFO keeps the logs quiet.
            logger.info(
                "Compactor returned non-JSON output (provider=%r model=%r "
                "raw_len=%d parse_error=%r); preserved %d prior directive(s), "
                "%d recovered — no directives lost.",
                provider,
                used_model,
                parsed.raw_len,
                parsed.parse_error,
                len(prior_list),
                len(recovered_list),
            )
        else:
            # No prior directives to fall back on (e.g. the first
            # compaction of the conversation), so this cycle genuinely
            # depends on bullet recovery — worth a WARNING.
            logger.warning(
                "Compactor returned non-JSON output (provider=%r model=%r "
                "raw_len=%d parse_error=%r); recovered %d directive(s) via "
                "bullet fallback, no prior directives to preserve.",
                provider,
                used_model,
                parsed.raw_len,
                parsed.parse_error,
                len(recovered_list),
            )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Compactor raw_preview=%r", _preview_raw_output(raw))

    return directives_block, summary_block


# Line-leading bullet markers used by the regex fallback recovery path:
# ``-``, ``*``, ``•``, ``▪``, ``·``, ``➤``, or any decimal number followed
# by ``.`` or ``)`` (e.g. ``1.``, ``2)``). Tolerates leading whitespace,
# unicode dashes (``–`` / ``—``), and the rare model that uses ``→`` as a
# bullet glyph.
_BULLET_RE = re.compile(
    r"^\s*(?:[-*•▪·➤→–—]|\d+[.)])\s+(?P<text>.+?)\s*$",
    re.MULTILINE,
)

# Headings that mark the start of a SUMMARY / recap section in the
# compactor's free-text fallback. Bullets BELOW one of these must NOT
# be promoted to Pinned Directives — those are re-injected as active
# instructions on every subsequent turn, so a conversation-recap line
# like ``- discussed dashboards`` would become a permanent constraint
# (``always discuss dashboards``) on the agent's behaviour.
#
# Anchored at line start, optional Markdown header marker, optional
# trailing colon. Case-insensitive. Single-line bodies only (no
# ``.+?`` matching that could swallow real directive bullets).
_SUMMARY_HEADING_RE = re.compile(
    r"(?im)^[ \t]*(?:#+\s*)?(?:"
    r"(?:rolling\s+)?summary"
    r"|topics\s+discussed"
    r"|decisions(?:\s+made)?"
    r"|open\s+questions?"
    r"|recap"
    r"|conversation\s+(?:summary|history|recap)"
    r")[ \t]*:?[ \t]*$",
)

# Headings that mark the start of an explicit DIRECTIVES section. When
# the compactor's free-text fallback labels the directives explicitly,
# we trust that label absolutely and extract ONLY bullets in that
# section — even if a ``Summary:`` heading appears earlier in the text
# (a malformed response that puts
# ``Summary:`` before ``Directives:`` was previously losing every
# directive because the pre-summary slice was empty).
#
# Same anchoring rules as ``_SUMMARY_HEADING_RE``: line start, optional
# Markdown header marker (``#``, ``##``, ...), optional trailing colon,
# case-insensitive. Recognises the noun variants the prompt itself
# enumerates ("constraints, corrections, preferences, format
# requirements, or rules"), plus ``directives`` and the ``Active`` /
# ``Pinned`` qualifiers the LLM tends to add for emphasis.
_DIRECTIVES_HEADING_RE = re.compile(
    r"(?im)^[ \t]*(?:#+\s*)?(?:"
    r"(?:active\s+|pinned\s+|active\s+pinned\s+)?directives"
    r"|constraints"
    r"|corrections"
    r"|rules"
    r"|preferences"
    # ``Format requirements`` (the prompt's exact noun phrase) and the
    # bare ``Requirements`` shorthand. The two-word variant must come
    # first or the alternation engine matches ``Requirements`` and
    # leaves a stray ``Format `` prefix on the previous line, breaking
    # the line-anchor.
    r"|format[ \t]+requirements"
    r"|requirements"
    r")[ \t]*:?[ \t]*$",
)

# Cap on raw-output preview length attached to *debug* log lines. The
# warning log itself only carries metadata (length + parse error) so
# we don't leak conversation content into default production log
# targets; this cap exists so even at DEBUG the preview stays
# greppable and doesn't dwarf the surrounding log entry.
_RAW_PREVIEW_CAP = 200

# Last-resort parse fallback keeps at most this many chars when we can't
# recover any directives and have to preserve a raw summary snippet so the
# compacted message still carries *something* from the failed JSON response.
_PARSE_SUMMARY_FALLBACK_CAP = 1500

# Patterns redacted from the raw-output preview before it reaches a
# log handler. The threat model is: the LLM occasionally echoes back
# credential-shaped tokens the user pasted into the conversation
# (Bearer headers, JWTs, ``fcp-``/``mcp-``/``sk-`` API keys, long
# base64-ish hashes). Even at DEBUG level we want those redacted, so
# a developer who later forwards the log file doesn't ship secrets
# along with it. Patterns ordered longest-/most-specific-first so a
# JWT match isn't sliced apart by the generic long-token rule.
# The patterns moved to `app.core.redaction` when the investigation queriers
# turned out to need the same pass on a third-party MCP server's error string.
# Kept as a module-local alias so this file's callers and its tests read as
# before — the point of the move was one copy of the patterns, not a rename.
_redact_sensitive = redact_secrets


def _preview_raw_output(raw: str, *, max_chars: int = _RAW_PREVIEW_CAP) -> str:
    """Build a single-line, length-capped, credential-redacted preview.

    Order matters here: redaction runs BEFORE the newline-flattening
    step. Once ``\\n`` is converted to the literal two-char ``\\n``
    escape, the trailing ``n`` becomes a word char that would defeat
    the leading ``\\b`` in patterns like ``\\b(bearer|basic)…``,
    silently disabling Bearer-token redaction. Doing the redaction
    against the original whitespace-anchored text preserves every
    pattern's word-boundary assumptions.

    Control characters (newlines, tabs, etc.) are collapsed into
    ``\\n`` and ``\\t`` literals AFTER redaction so the resulting line
    is greppable, and the result is hard-truncated to *max_chars*
    with an explicit ``…`` marker so operators can tell at a glance
    whether the LLM was truncated.
    """
    if not raw:
        return ""
    # 1. Redact credential-shaped tokens FIRST — see docstring.
    redacted = _redact_sensitive(raw)
    # 2. Now flatten control characters to literal escapes.
    flat = (
        redacted.replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    # 3. Strip any other ASCII control characters (keep printable + the
    # backslash escapes we just introduced).
    flat = "".join(ch for ch in flat if ch >= " " or ch == "\\")
    if len(flat) > max_chars:
        return flat[:max_chars] + "…"
    return flat


def _extract_bullets_from_text(raw: str) -> list[str]:
    """Best-effort recovery of directive-shaped lines from free-text output.

    Used as a last resort when even json-repair cannot salvage a valid
    object from the compactor's response. The scan is
    **bidirectionally section-aware**:

    1. If the raw text contains an explicit DIRECTIVES heading
       (``Directives:``, ``Active directives:``, ``Pinned Directives:``,
       ``Constraints:``, ``Rules:``, ...), the scan extracts ONLY the
       bullets in that section — from the end of the heading line up
       to either the next SUMMARY heading or end-of-text. This wins
       unconditionally, even if a ``Summary:`` heading appears earlier
       in the text.
    2. Otherwise, if a SUMMARY heading is present anywhere, the scan
       takes everything ABOVE it as candidate directives
       (recap bullets below ``Summary:`` are never promoted).
    3. Otherwise, the entire raw text is scanned.

    Empty bullets and one-word fragments are filtered out (they're
    almost always section headers like ``- Directives:`` rather than
    actual constraints).
    """
    # Bidirectional section gate.
    #
    # Priority 1: an explicit ``Directives:``-style heading. When
    # present, we trust it absolutely and ignore any bullets outside
    # the labelled section. This handles the order-independent
    # malformed-output case where the LLM emits the summary section
    # first and the directives section second — without the directives
    # heading taking priority, the ``cut at first summary
    # heading`` rule would slice everything off and silently drop
    # every directive the user had set (exactly the failure mode the
    # bullet fallback exists to prevent).
    dir_match = _DIRECTIVES_HEADING_RE.search(raw)
    if dir_match is not None:
        section_start = dir_match.end()
        tail = raw[section_start:]
        # Walk forward to the next summary heading inside the tail (if
        # any) so a ``Directives: ... Summary: ...`` layout doesn't
        # pull recap bullets into the directive list.
        next_summary = _SUMMARY_HEADING_RE.search(tail)
        scan_target = tail[: next_summary.start()] if next_summary else tail
    else:
        # Priority 2: no explicit directives heading — fall back to
        # the pre-summary slice (everything above the first
        # summary heading, or the whole text if no heading exists).
        cut = _SUMMARY_HEADING_RE.search(raw)
        scan_target = raw[: cut.start()] if cut is not None else raw

    out: list[str] = []
    seen: set[str] = set()
    for match in _BULLET_RE.finditer(scan_target):
        text = match.group("text").strip()
        # Drop trailing colons left over from section headers
        # (``Directives:``) and meta-labels (``Note:``) that occasionally
        # leak in when the LLM emits a free-text breakdown.
        if text.endswith(":") and len(text.split()) <= 3:
            continue
        if len(text) < 3:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _parse_compaction_response(raw: str) -> _CompactionParse:
    """Tolerant, side-effect-free parser for the compactor's response.

    Pipeline:
    1. Run the Schema-Aligned Parsing (SAP) pipeline from
       :mod:`app.services.structured_output` — that already handles
       markdown fences, prose preamble, trailing commas, single
       quotes, unquoted keys, and partial truncation.
    2. On success, return a ``clean`` result with ``(directives,
       summary)`` typed against the compactor schema.
    3. On total parse failure, try a regex-based bullet recovery as a
       last resort so directive-shaped lines aren't silently dropped,
       and return a NOT-``clean`` result so the caller can re-attach the
       carried-forward directives instead of trusting the failed roll
       forward.

    This function does NOT log — the failure is logged by
    :func:`_compact_messages` (which has the provider/model context and
    knows whether any data was actually lost) and by the
    :func:`_parse_compaction_json` compatibility wrapper.
    """
    if not raw:
        return _CompactionParse(
            directives=[], summary="", clean=False, raw_len=0, parse_error=None
        )

    from app.services.structured_output import validate_and_parse

    data, errors = validate_and_parse(raw, _COMPACTOR_SCHEMA, allow_partial=True)

    if isinstance(data, dict):
        raw_directives = data.get("directives") or []
        summary = data.get("summary") or ""
        if not isinstance(summary, str):
            summary = str(summary)
        # The schema requires ``directives: array of string`` but
        # ``allow_partial=True`` lets through dicts whose ``directives``
        # field is the wrong shape (commonly a single bullet-string the
        # model emitted instead of an array, or an array containing
        # non-string items). Coerce shape-by-shape rather than
        # collapsing everything to ``[]`` — the previous shape silently
        # dropped real directive content as soon as ``summary`` was
        # non-empty.
        directives: list[str]
        if isinstance(raw_directives, list):
            directives = [d for d in raw_directives if isinstance(d, str) and d.strip()]
        elif isinstance(raw_directives, str):
            # String shape: run the bullet extractor over it. If the
            # string has bullet markers we recover them; if it's a
            # single sentence with no bullets the extractor returns []
            # and the recovery path below runs over the full raw text.
            directives = _extract_bullets_from_text(raw_directives)
        else:
            directives = []
        # SAP returned *something* — even if validation flagged minor
        # issues (under ``allow_partial=True``) the coerced object is
        # usually usable. Only fall through to bullet recovery on the
        # raw text when we got absolutely nothing useful back.
        if directives or summary.strip():
            return _CompactionParse(
                directives=directives,
                summary=summary,
                clean=True,
                raw_len=len(raw),
                parse_error=None,
            )

    # ── Last-resort recovery ─────────────────────────────────────────
    # Pull directive-shaped bullets out of the raw text so the user's
    # imperative content isn't silently dropped.
    recovered = _extract_bullets_from_text(raw)
    first_error = errors[0] if errors else "no parse error reported"
    # When the bullet fallback successfully extracted directives, the raw
    # text was almost certainly a free-text directive list (no real
    # summary content) — leave the summary block empty rather than
    # duplicating the bullets there. Otherwise, keep the legacy
    # summary-only fallback so the compacted message still carries
    # *something* useful from the failed JSON response.
    summary_fallback = "" if recovered else raw[:_PARSE_SUMMARY_FALLBACK_CAP]
    return _CompactionParse(
        directives=recovered,
        summary=summary_fallback,
        clean=False,
        raw_len=len(raw),
        parse_error=first_error,
    )


def _parse_compaction_json(raw: str) -> tuple[list[str], str]:
    """Backward-compatible ``(directives, summary)`` wrapper.

    Thin shim over :func:`_parse_compaction_response` that preserves the
    historical 2-tuple contract for direct callers and tests. It also
    logs the metadata-only WARNING on a fallback so direct usage still
    surfaces the failure. The production path (:func:`_compact_messages`)
    calls :func:`_parse_compaction_response` directly and does its own
    provider/model-aware logging, so it does NOT route through this
    wrapper (avoiding a duplicate warning).

    The WARNING log carries ONLY metadata (length + parse error +
    recovered count) so we don't leak conversation content (which can
    include credential-shaped tokens echoed back by the LLM) into default
    production log aggregators. The redacted raw preview ships at DEBUG
    level for operators with full log access — see
    :func:`_redact_sensitive` for the patterns scrubbed before the
    preview is emitted.
    """
    parsed = _parse_compaction_response(raw)
    # ``raw_len`` guard keeps empty input from logging a spurious warning.
    if not parsed.clean and parsed.raw_len:
        logger.warning(
            "Compactor returned non-JSON output; recovered %d directive(s) via "
            "bullet fallback (parse_error=%r, raw_len=%d)",
            len(parsed.directives),
            parsed.parse_error,
            parsed.raw_len,
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Compactor raw_preview=%r", _preview_raw_output(raw))
    return parsed.directives, parsed.summary


# ── Conversation title generation ────────────────────────────────────────

# A 3-6 word title in its ``{"title": ...}`` envelope; one cut at this budget
# is retried once with twice the room and otherwise dropped for the caller's
# fallback title.
_TITLE_MAX_TOKENS = 40
# Hard cap on the title call and the retry of a cut title together.
_TITLE_TIMEOUT_SECONDS = 20
# The clock the title deadline is counted on (monotonic seconds); a seam so a
# test drives the deadline without depending on how loaded its worker is.
_title_clock = time.monotonic

_TITLE_SYSTEM_PROMPT = (
    "You write short, descriptive titles for chat conversations. "
    "You are shown the first user message and the assistant's reply as data "
    "to label — never answer it, react to it, greet anyone, or continue the "
    "conversation. "
    'Reply with ONLY a JSON object whose "title" field holds the title: 3 to 6 '
    "words, no quotes and no trailing punctuation inside it."
)

#: The title answer. A model that answers the exchange instead of
#: labelling it is told apart by the shape of its answer, never by its words:
#: native structured output enforces the object where the provider has it, and
#: an answer that is not this object is no title.
_TITLE_SCHEMA: dict = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
}

# A real title is a short noun phrase: past these bounds the answer is a
# sentence, rejected rather than truncated into a misleading label.
_MAX_TITLE_WORDS = 12
_MAX_TITLE_CHARS = 80


def _parse_generated_title(raw: str | None) -> str | None:
    """The title of the model's ``{"title": ...}`` answer, cleaned, or *None*.

    *None* when the answer is not that object, its title is not a string, or
    the title is blank, under three characters, or longer than a short noun
    phrase: the caller keeps its deterministic fallback (the user's first
    message, trimmed). Read as written, never type-coerced: an object or a
    number in ``title`` is no title.
    """
    from app.services.structured_output import extract_json_text, repair_json_text

    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(repair_json_text(extract_json_text(raw)))
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("title"), str):
        return None
    # A title is shown everywhere and stored in a bounded column the engine's
    # content redaction does not cover; redacted once parsed, so a
    # secret the JSON escaped is matched as the model wrote it.
    title = redact_known_secrets_for_display(data["title"])
    first_line = next((line.strip() for line in title.splitlines() if line.strip()), "")
    cleaned = first_line.strip("\"'` ")
    cleaned = re.sub(r"\s+", " ", cleaned).rstrip(".!?,;:").strip()
    if len(cleaned) < 3:
        return None
    if len(cleaned) > _MAX_TITLE_CHARS or len(cleaned.split()) > _MAX_TITLE_WORDS:
        return None
    return cleaned


async def generate_conversation_title(
    llm: BaseLLMProvider,
    user_message: str,
    assistant_message: str,
    *,
    model: str | None = None,
) -> str | None:
    """Generate an AI-powered conversation title from the first exchange.

    Returns a short descriptive title, or *None* when generation fails OR the
    model produced something that is not a usable title (callers keep their
    deterministic fallback in that case).

    *model* is the resolved chat model (agent override -> agent default ->
    user preference -> platform default). Callers must pass it so titles run
    on the configured default chat model instead of the provider's built-in
    default (which may be a non-chat / stale model on image-only providers).
    """
    from app.services.structured_output import build_native_structured_output

    user_preview = user_message[:500]
    assistant_preview = assistant_message[:500]
    title_messages = [
        ChatMessage(role="system", content=_TITLE_SYSTEM_PROMPT),
        ChatMessage(
            role="user",
            content=(
                "Write a short title (3 to 6 words) describing the topic of "
                "the conversation below. Reply with the JSON object only.\n\n"
                "<conversation>\n"
                f"User: {user_preview}\n\n"
                f"Assistant: {assistant_preview}\n"
                "</conversation>"
            ),
        ),
    ]
    structured_output = build_native_structured_output(llm, _TITLE_SCHEMA)
    structured_kwargs = {"structured_output": structured_output} if structured_output is not None else {}
    # Title generation runs AFTER the answer streamed but BEFORE the ``done``
    # frame is sent — an unbounded call here (provider SDK read timeouts reach
    # 600 s, failover multiplies that) visibly freezes the turn's completion.
    # Cap it hard, the retry of a cut title included: one deadline for both,
    # from the moment the first call is made; a timeout simply keeps the
    # deterministic fallback title.
    deadline: float | None = None

    async def _title(max_tokens: int):
        nonlocal deadline
        if deadline is not None and deadline <= _title_clock():
            raise TimeoutError("conversation title time budget spent")
        async with traced_generation(
            name="conversation_title", messages=title_messages
        ) as _gen:
            if deadline is None:
                deadline = _title_clock() + _TITLE_TIMEOUT_SECONDS
            _gen.response = await asyncio.wait_for(
                llm.chat(
                    messages=title_messages,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=0.3,
                    **structured_kwargs,
                ),
                timeout=deadline - _title_clock(),
            )
        return _gen.response

    try:
        response, cut = await chat_retrying_a_cut(
            llm, model, _TITLE_MAX_TOKENS, _title, label="Conversation title"
        )
        if cut:
            # A title cut mid-word is not a title: keep the fallback.
            return None
        raw = response.content or ""
        title = _parse_generated_title(raw)
        if title:
            return title
        # No title object, or one holding no title. Drop it so the caller
        # keeps its fallback; log a short, credential-redacted preview at
        # DEBUG to aid diagnosis without spamming logs or leaking secrets the
        # model may have echoed back.
        if raw.strip() and logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Discarding non-title model output for conversation title: %r",
                _preview_raw_output(redact_known_secrets_for_display(raw), max_chars=120),
            )
    except ContextOverflowError:
        logger.warning("Title generation skipped: context overflow")
    except Exception as exc:
        logger.warning("AI title generation failed: %s", format_exception_message(exc))
    return None
