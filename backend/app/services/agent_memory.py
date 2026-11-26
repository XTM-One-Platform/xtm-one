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

"""Agent memory service – Redis-backed short-term and medium-term memory.

This module manages the first two tiers of the three-tier agent memory system:

**Tier 1 – Short-term** (Redis): Rolling list of recent run summaries and chat
interactions.  Provides immediate context about what happened recently.
  - Max 10 entries, TTL 24 hours (refreshed on write)
  - Stored as a Redis LIST of JSON objects

**Tier 2 – Medium-term** (Redis): Consolidated summary of patterns, preferences,
and learnings extracted by the LLM.  Provides deeper understanding that
accumulates over time.
  - Single Redis STRING, TTL 30 days (refreshed on update)
  - Updated after every Nth short-term entry (or on explicit consolidation)
  - Also persisted to PostgreSQL (see ``persistent_memory.py``) for durability

**Tier 3 – Persistent** (PostgreSQL + pgvector): Durable facts, style notes,
and extracted knowledge stored with vector embeddings for semantic search.
  - Survives Redis restarts, retained indefinitely
  - Facts are auto-extracted during conversation context compaction
  - Consolidated memory is persisted after each Redis consolidation
  - Searchable via the ``search_memory`` agent tool and the Memory API
  - See ``persistent_memory.py`` for the persistent tier implementation

**Scoping model:**

All functions accept an optional ``scope_id`` that isolates memory per
caller context:

- **Authenticated chat** (user talks to agent):
  ``scope_id=user_id`` → key ``agent_mem:{tier}:{agent_id}:scope:{user_id}``
  Each user has a private memory lane with each agent.

- **Assignment run**: the lane of the user the run acts as
  (``assignment_helpers.assignment_memory_scope``: ``run.user_id``, else the
  agent's owner), the same lane that user's chat with the agent reads. A run
  never writes the agent-level lane, so what its acting user's integrations
  and knowledge bases returned reaches no other user.

- **Agent-level** (``scope_id=None`` → key ``agent_mem:{tier}:{agent_id}``):
  written only by the designated agent-level writer inside a trusted
  autonomous run (``agent_chat._derive_memory_scope_id``); its consolidation
  persists with ``user_id = NULL``, which every user's chat reads.

- **Public chat** (embedded widget):
  Agent memory is **not used** at all — visitors only get per-visitor
  memory via ``user_memory``.  This prevents any cross-context leakage.

Memory flow:
  1. Before an assignment run or chat, ``load_memory`` retrieves tiers 1 & 2
     from Redis, and ``load_persistent_context`` retrieves tier 3 from
     PostgreSQL.  All three are injected into the system prompt.
  2. ``format_memory_for_prompt`` renders Redis memory into a prompt block.
  3. After a run completes, ``store_short_term_memory`` saves the summary.
  4. Periodically, ``consolidate_memory`` asks the LLM to merge short-term
     entries into the medium-term summary and persists to PostgreSQL.
  5. During context compaction, ``extract_and_persist_facts`` extracts key
     facts and stores them in the persistent tier.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.core.redaction import redact_known_secrets
from app.core.redis import redis_client
from app.core.utils import format_exception_message
from app.llm.cut_answers import answer_was_cut
from app.llm.model_limits import retry_output_budget
from app.services.langfuse_tracing import traced_generation
from app.services.leaked_reasoning import strip_leading_reasoning
from app.services.memory_tombstones import (
    account_tombstone,
    agent_tombstone,
    bury,
    undo_if_buried,
)

if TYPE_CHECKING:
    from app.llm.base import BaseLLMProvider

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────
SHORT_TERM_MAX_ENTRIES = 10
SHORT_TERM_TTL = 86_400  # 24 hours
MEDIUM_TERM_TTL = 2_592_000  # 30 days
CONSOLIDATION_THRESHOLD = 5  # Consolidate after this many new short-term entries

# Maximum characters for the medium-term summary injected into the system
# prompt.  The consolidation prompt instructs the LLM to stay under 400 words
# (~2 400 chars), but in practice summaries can accrete much more via repeated
# consolidation rounds.  This hard cap prevents a single bloated memory from
# consuming the majority of the context window and slowing LLM responses.
# The value is deliberately generous (2.5× the ~2,400-char target)
# so legitimate dense summaries are never truncated.
_MAX_MEDIUM_TERM_PROMPT_CHARS = 6_000

# Maximum characters of an oversized summary fed to the compression LLM call.
# Bounds the prompt even for pathologically accreted summaries.
_COMPRESS_INPUT_MAX_CHARS = 16_000

# Output token budget for the consolidation and compression LLM calls.  Both
# prompts ask for max 400 words (~600 tokens); 1_200 leaves headroom for
# markdown structure so a legitimate summary is not cut by the output cap.
# A consolidation that STILL hits the cap is retried once with double the
# budget, then boundary-trimmed - a generation cut by ``max_tokens``
# ends mid-word, and that clipped text used to be stored to Redis, persisted
# durably to PostgreSQL, and injected into every later system prompt as-is.
# Both calls pass ``disable_thinking``: on a model that thinks by default the
# thinking would share this budget with the summary.
_SUMMARY_MAX_TOKENS = 1_200

# Notice appended to a summary whose generation was cut by the output token
# budget.  It is stored WITH the summary so the durable persisted
# copy and every later prompt injection carry the signal that trailing
# content is missing, instead of ending silently mid-sentence.
_CLIPPED_GENERATION_NOTICE = (
    "\n[Note: this summary was cut at the model output limit; "
    "trailing items may be missing.]"
)

# Notice appended when a stored summary is cut to fit a character budget
# (prompt injection cap, compression hard-truncate fallback).
_BUDGET_TRUNCATION_NOTICE = (
    "\n[...memory summary truncated to fit the prompt budget...]"
)


def _strip_stale_notices(text: str) -> str:
    """Remove stored truncation notices from a summary fed back to the LLM.

    The notices are meta-signals about a PAST generation, not memory
    content.  A stored summary carrying one is fed verbatim to the next
    consolidation / compression call as "existing memory", where the model
    tends to copy the notice into its fresh output - and a second cut would
    then stack a new notice on top of the copied one.  Stripping them from
    the LLM INPUT keeps notices out of the model's hands entirely; the
    stored copy keeps its notice (it is still accurate for that copy).
    """
    for notice in (_CLIPPED_GENERATION_NOTICE, _BUDGET_TRUNCATION_NOTICE):
        text = text.replace(notice, "")
        # The model may have re-emitted a prior notice without the leading
        # newline; drop that form too.
        text = text.replace(notice.lstrip("\n"), "")
    return text.rstrip()


def _drop_clipped_tail(text: str) -> str:
    """Drop the trailing fragment of a generation cut by ``max_tokens``.

    A generation that hit the output token budget stops mid-word.  Cutting
    back to the last line or sentence boundary keeps only complete
    statements.  If no boundary exists in the second half of the text -
    losing more than half the summary is worse than one dangling clause -
    the final whitespace-delimited fragment is discarded instead, so a
    mid-word artifact is never kept.  A raw generation that ends in
    whitespace is evidence the final word was completed BEFORE the cut, so
    the fragment drop is skipped for it.  A single unbroken token is
    returned unchanged (there is nothing safe to cut back to).
    """
    ends_on_complete_word = bool(text) and text[-1].isspace()
    stripped = text.rstrip()
    if not stripped or stripped.endswith((".", "!", "?", ":")):
        return stripped
    best = -1
    for sep in ("\n", ". ", "! ", "? ", ": "):
        idx = stripped.rfind(sep)
        if idx == -1:
            continue
        cut = idx + (0 if sep == "\n" else 1)
        best = max(best, cut)
    if best > len(stripped) // 2:
        return stripped[:best].rstrip()
    if ends_on_complete_word:
        return stripped
    space = stripped.rfind(" ")
    if space > 0:
        return stripped[:space].rstrip()
    return stripped


def _truncate_at_boundary(text: str, limit: int, notice: str) -> str:
    """Truncate *text* to at most *limit* chars without a mid-word cut.

    The cut point prefers, in order, a paragraph break, a line break, a
    sentence end, then a word boundary - falling back to a hard slice only
    when the text has no boundary in the second half of the budget.
    *notice* is appended after the cut; the returned string INCLUDING the
    notice always fits within *limit*, so callers can budget on the
    constant they pass.  When *notice* itself does not fit within *limit*,
    the notice is dropped and the text is hard-sliced - the size contract
    holds for any input, not just the module constants.
    """
    if len(text) <= limit:
        return text
    if len(notice) >= limit:
        return text[:limit]
    budget = limit - len(notice)
    clipped = text[:budget]
    for sep in ("\n\n", "\n", ". ", " "):
        idx = clipped.rfind(sep)
        if idx > budget // 2:
            clipped = clipped[: idx + (1 if sep == ". " else 0)]
            break
    return clipped.rstrip() + notice


# ── Redis key helpers ────────────────────────────────────────────────────────


def _base_key(agent_id: str, scope_id: str | None) -> str:
    """Build the base key segment, optionally scoped to a user.

    - ``scope_id=None`` → ``agent_mem:{agent_id}``  (agent-level, for assignments)
    - ``scope_id="<user_id>"`` → ``agent_mem:{agent_id}:scope:{user_id}``  (per-user)
    """
    if scope_id:
        return f"agent_mem:{agent_id}:scope:{scope_id}"
    return f"agent_mem:{agent_id}"


#: Every key a lane holds: the writers write only these, and a purge that
#: deletes ``lane_keys`` (or scans a ``_base_key`` pattern) clears them all.
_TIERS = ("short", "medium", "counter")


def _tier_key(agent_id: str, scope_id: str | None, tier: str) -> str:
    return f"{_base_key(agent_id, scope_id)}:{tier}"


def _short_term_key(agent_id: str, scope_id: str | None = None) -> str:
    return _tier_key(agent_id, scope_id, "short")


def _medium_term_key(agent_id: str, scope_id: str | None = None) -> str:
    return _tier_key(agent_id, scope_id, "medium")


def _counter_key(agent_id: str, scope_id: str | None = None) -> str:
    """Tracks entries since last consolidation."""
    return _tier_key(agent_id, scope_id, "counter")


def lane_keys(agent_id: str, scope_id: str | None = None) -> tuple[str, ...]:
    """Every Redis key of one memory lane (the agent's own, or one user's)."""
    return tuple(_tier_key(agent_id, scope_id, tier) for tier in _TIERS)


def _lane_tombstones(agent_id: str, scope_id: str | None) -> tuple[str, ...]:
    """What deletes a lane: its agent, or (for a user's lane) that account.

    A ``scope_id`` that is a conversation id (anonymous chat) never has an
    account tombstone, so checking it is harmless.
    """
    if scope_id:
        return (agent_tombstone(agent_id), account_tombstone(scope_id))
    return (agent_tombstone(agent_id),)


async def _undo_if_lane_deleted(redis, agent_id: str, scope_id: str | None) -> bool:
    return await undo_if_buried(
        redis, _lane_tombstones(agent_id, scope_id), lane_keys(agent_id, scope_id)
    )


# ── Public API ───────────────────────────────────────────────────────────────


async def load_memory(agent_id: str, *, scope_id: str | None = None) -> dict:
    """Load all memory tiers for an agent.

    Args:
        agent_id: The agent UUID.
        scope_id: Optional user UUID to isolate memory per-user.
                  ``None`` for autonomous / assignment contexts.

    Returns::

        {
            "short_term": [{"assignment": ..., "summary": ..., "timestamp": ...}, ...],
            "medium_term": "consolidated summary string or empty",
        }

    Lines of the summary and short-term entries that state a persistent
    entry since superseded or retired are left out
    (``memory_superseded``, #11861).
    """
    redis = await redis_client()

    # Short-term: list of recent entries (newest first)
    raw_entries = await redis.lrange(
        _short_term_key(agent_id, scope_id),
        0,
        SHORT_TERM_MAX_ENTRIES - 1,
    )
    short_term: list[dict] = []
    for raw in raw_entries:
        try:
            short_term.append(json.loads(raw))
        except (TypeError, ValueError):
            logger.debug(
                "Skipping malformed short-term memory entry for agent %s scope %s",
                agent_id,
                scope_id or "global",
            )
            continue

    # Medium-term: consolidated string
    medium_term = await redis.get(_medium_term_key(agent_id, scope_id)) or ""

    memory = {"short_term": short_term, "medium_term": medium_term}
    if not (short_term or medium_term):
        return memory
    # Both tiers are text built before an entry was superseded or retired:
    # what no longer holds is left out here, so neither the prompt nor the
    # next consolidation reads it (#11861).
    from app.services.memory_superseded import drop_superseded, load_superseded

    return drop_superseded(memory, await load_superseded(agent_id, scope_id))


async def entries_starting_with(redis: Any, key: str, prefix: str | None, limit: int) -> list[str]:
    """The raw entries of the short-term list *key* whose summary starts with *prefix*.

    *prefix* is redacted the way a stored summary is, so it matches what was
    written. A malformed entry never matches.
    """
    if not prefix:
        return []
    wanted = redact_known_secrets(prefix)
    stale: list[str] = []
    for raw in await redis.lrange(key, 0, limit - 1):
        try:
            summary = json.loads(raw).get("summary")
        except (TypeError, ValueError, AttributeError):
            continue
        if isinstance(summary, str) and summary.startswith(wanted):
            stale.append(raw)
    return stale


async def store_short_term_memory(
    agent_id: str,
    *,
    source: str,
    summary: str,
    source_type: str = "assignment",
    scope_id: str | None = None,
    replaces_prefix: str | None = None,
) -> int:
    """Store an entry in short-term memory.

    Args:
        agent_id: The agent UUID.
        source: Human label for the source (assignment name, "chat", etc.).
        summary: The text to remember.
        source_type: "assignment" or "chat".
        scope_id: Optional user UUID to isolate memory per-user.
        replaces_prefix: The entries whose summary starts with it are removed
            before this one is written: the note of a chat turn a rollout cut,
            once its resumed turn answered (``interrupted_turn``).

    Returns:
        The current consolidation counter value.
    """
    # A summary that was only reasoning stores nothing, like an empty one.
    summary = strip_leading_reasoning(summary or "")
    if not summary.strip():
        return 0

    redis = await redis_client()
    key = _short_term_key(agent_id, scope_id)
    ctr_key = _counter_key(agent_id, scope_id)
    stale = await entries_starting_with(redis, key, replaces_prefix, SHORT_TERM_MAX_ENTRIES)

    entry = json.dumps(
        {
            "source": source,
            "source_type": source_type,
            # Redacted before the cap, so a cut never leaves part of a value.
            "summary": redact_known_secrets(summary)[:1500],  # Cap to prevent bloat
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )

    pipe = redis.pipeline()
    for raw in stale:
        pipe.lrem(key, 0, raw)
    pipe.lpush(key, entry)
    pipe.ltrim(key, 0, SHORT_TERM_MAX_ENTRIES - 1)
    pipe.expire(key, SHORT_TERM_TTL)
    pipe.incr(ctr_key)
    pipe.expire(ctr_key, SHORT_TERM_TTL)
    # Checked after the write (see ``memory_tombstones``).
    pipe.exists(*_lane_tombstones(agent_id, scope_id))
    results = (await pipe.execute())[len(stale) :]
    if results[5]:
        await redis.delete(*lane_keys(agent_id, scope_id))
        return 0

    return int(results[3])  # counter value after INCR


async def consolidate_memory(
    agent_id: str,
    llm: BaseLLMProvider,
    *,
    force: bool = False,
    scope_id: str | None = None,
    scope_is_user_id: bool = True,
    is_company_managed: bool = False,
    model: str | None = None,
) -> None:
    """Consolidate short-term entries into the medium-term summary.

    Only runs when the counter exceeds ``CONSOLIDATION_THRESHOLD`` (or when
    *force* is True).  Resets the counter afterward.

    When *is_company_managed* is ``True``, the consolidation prompt instructs
    the LLM to focus on organisational knowledge and avoid storing personal
    user information, since the agent is shared across all users.

    *scope_is_user_id* tells the durable-persistence step whether *scope_id*
    is a real user UUID. For **anonymous** external-channel users the chat
    layer scopes memory to the CONVERSATION id instead (so context stays
    within the conversation and never leaks across conversations) — that
    scope is valid for the Redis tiers (plain key namespacing) but must
    NEVER be written to ``agent_memory_entries.user_id``, which has a
    foreign key to ``users``. When ``False``, the Redis
    medium-term summary is still updated but the PostgreSQL persist step is
    skipped: persisting agent-level (``user_id=NULL``) instead would leak
    anonymous conversation content into the shared agent memory every user
    sees.
    """
    redis = await redis_client()

    if not force:
        counter = int(await redis.get(_counter_key(agent_id, scope_id)) or 0)
        if counter < CONSOLIDATION_THRESHOLD:
            return

    memory = await load_memory(agent_id, scope_id=scope_id)
    short_entries = memory["short_term"]
    if not short_entries:
        return

    existing_medium = memory["medium_term"]

    # Build a transcript of short-term entries for the LLM
    transcript_parts: list[str] = []
    for entry in reversed(short_entries):  # chronological order
        ts = entry.get("timestamp", "")[:16]
        src = entry.get("source", "?")
        summary = strip_leading_reasoning(entry.get("summary", ""))
        transcript_parts.append(f"[{ts}] {src}: {summary}")

    new_context = "\n".join(transcript_parts)

    from app.llm.base import ChatMessage

    # Build the consolidation system prompt — for company-managed agents,
    # add explicit guidance to avoid storing personal information.
    base_prompt = (
        "You are a memory manager for an AI agent. Your job is to maintain "
        "a concise, useful memory document that the agent will read before "
        "every task.\n\n"
        "Update the existing memory by integrating the new activity log. "
    )

    if is_company_managed:
        base_prompt += (
            "\nIMPORTANT: This is a SHARED agent used by the entire organisation. "
            "Multiple different users interact with it. The memory you produce "
            "must be suitable for the whole team — not personalised to any "
            "individual.\n\n"
            "Focus on:\n"
            "- Organisational knowledge, processes, and conventions\n"
            "- Product information, technical facts, and shared context\n"
            "- Important decisions, outcomes, and their rationale\n"
            "- Factual corrections and clarifications that benefit everyone\n"
            "- Errors or issues encountered and how they were resolved\n\n"
            "Rules:\n"
            "- Do NOT store personal user information (names, individual "
            "preferences, private details, personal communication styles).\n"
            "- Do NOT store the user's preferred language, tone, formality, "
            "or other cross-agent personal preferences — those belong in "
            "the platform-wide user profile, not in this agent's memory.\n"
            "- Do NOT create user profiles or track individual habits.\n"
            "- Treat every line as something the entire team will read.\n"
            "- Be concise: max 400 words.\n"
            "- Use bullet points for clarity.\n"
            "- Remove outdated or contradicted information.\n"
            "- If existing memory is empty, create a fresh summary.\n"
            "- Do NOT include timestamps — focus on durable knowledge."
        )
    else:
        # This memory is per-user scoped, but it must still focus on
        # AGENT-SPECIFIC knowledge.  Cross-agent personal preferences
        # (preferred language, tone, communication style, formality) are
        # tracked separately in the platform-wide user profile (see
        # ``user_memory.py``) so they follow the user across every agent
        # they interact with.  Capturing them here would make them sticky
        # to this single agent and could make the agent reply in (e.g.)
        # French even after the user switched to English with a different
        # agent.
        base_prompt += (
            "Focus on agent-specific context:\n"
            "- Important decisions, outcomes, and their rationale for "
            "this agent's domain\n"
            "- Key facts, contacts, identifiers, and references useful "
            "for future tasks with this agent\n"
            "- Workflows, tools, dashboards, filters, or configurations "
            "the user has pinned for this agent\n"
            "- Errors or issues encountered with this agent and how "
            "they were resolved\n\n"
            "Do NOT capture in this agent's memory:\n"
            "- The user's preferred language, locale, tone, formality, "
            "or communication style — those are cross-agent personal "
            "preferences and live in the platform-wide user profile.\n"
            "- The user's name, role, team, or other identity details — "
            "those also live in the user profile.\n"
            "- Anything that would apply equally to every other agent "
            "the user interacts with.\n\n"
            "Rules:\n"
            "- Be concise: max 400 words.\n"
            "- Use bullet points for clarity.\n"
            "- Remove outdated or contradicted information.\n"
            "- If existing memory is empty, create a fresh summary.\n"
            "- Do NOT include timestamps — focus on durable knowledge."
        )

    # Feed the LLM a notice-free view of the existing memory: a stale
    # truncation notice is meta-signal about a past generation, and the
    # model would otherwise copy it into the fresh summary.  The
    # stored ``existing_medium`` keeps its notice for the no-response
    # fallback below.
    existing_for_prompt = strip_leading_reasoning(_strip_stale_notices(existing_medium))
    consolidation_messages = [
        ChatMessage(role="system", content=base_prompt),
        ChatMessage(
            role="user",
            content=(
                f"Existing memory:\n{existing_for_prompt or '(empty — first consolidation)'}\n\n"
                f"New activity to integrate:\n{new_context}"
            ),
        ),
    ]
    try:
        async with traced_generation(
            name="agent_memory_consolidate", messages=consolidation_messages
        ) as _gen:
            _gen.response = await llm.chat(
                messages=consolidation_messages,
                model=model,
                max_tokens=_SUMMARY_MAX_TOKENS,
                temperature=0.3,
                disable_thinking=True,
            )

        response = _gen.response
        # The consolidated summary is stored durably (Redis + PostgreSQL)
        # and injected into every later system prompt, so a generation cut
        # by the output token budget - it ends mid-word - must never be
        # stored as-is.  Retry once with a doubled budget; if the
        # model still overruns, trim the incomplete tail at a clean
        # boundary and mark the cut explicitly.
        if answer_was_cut(response):
            logger.warning(
                "Memory consolidation for agent %s hit the %d-token output "
                "budget - retrying once with a doubled budget",
                agent_id,
                _SUMMARY_MAX_TOKENS,
            )
            # An empty OR failing retry must not clobber the usable clipped
            # first response: keeping it (its stop_reason is still
            # ``max_tokens``) routes it through the trim-and-notice fallback
            # below, so the complete statements it contains are stored
            # instead of silently falling back to the OLD medium-term
            # summary and losing the newly consolidated activity.  A retry
            # exception must be contained here for the same reason - letting
            # it escape to the outer handler would discard the first
            # response entirely.
            retry_response = None
            try:
                async with traced_generation(
                    name="agent_memory_consolidate_retry",
                    messages=consolidation_messages,
                ) as _retry:
                    _retry.response = await llm.chat(
                        messages=consolidation_messages,
                        model=model,
                        max_tokens=retry_output_budget(llm, model, _SUMMARY_MAX_TOKENS),
                        temperature=0.3,
                        disable_thinking=True,
                    )
                retry_response = _retry.response
            except Exception as retry_err:
                logger.warning(
                    "Memory consolidation retry for agent %s failed: %s - "
                    "keeping the clipped first response",
                    agent_id,
                    format_exception_message(retry_err),
                )
            if retry_response is not None:
                retry_content = retry_response.content
                if retry_content and retry_content.strip():
                    response = retry_response
                else:
                    logger.warning(
                        "Memory consolidation retry for agent %s returned an "
                        "empty response - keeping the clipped first response",
                        agent_id,
                    )

        # Whitespace-only content is truthy but carries nothing storable: it
        # must select the old-summary fallback (never erase the existing
        # memory) and must not earn a truncation notice - trimming it would
        # otherwise store a bare notice as the whole summary.
        # Before the emptiness check: a reasoning-only answer falls back to
        # the old summary rather than storing an empty one (#12356).
        content = strip_leading_reasoning(response.content or "")
        has_content = bool(content.strip())
        updated = content if has_content else strip_leading_reasoning(existing_medium)
        if has_content and answer_was_cut(response):
            logger.warning(
                "Memory consolidation for agent %s was cut by the output "
                "token budget even after the retry - trimming to the last "
                "complete statement and marking the truncation",
                agent_id,
            )
            updated = _drop_clipped_tail(updated) + _CLIPPED_GENERATION_NOTICE
        if updated:
            # Write-time clamp: never store a summary larger than
            # the prompt cap (_MAX_MEDIUM_TERM_PROMPT_CHARS).  The
            # consolidation prompt asks for max 400 words but accretion over
            # many rounds can blow past it, and nothing else ever shrinks
            # the Redis tier (the hourly worker maintenance is
            # PostgreSQL-only).  Compress via one extra LLM pass;
            # hard-truncate as a last resort.  This also self-heals
            # PREVIOUSLY oversized summaries on their next consolidation.
            if len(updated) > _MAX_MEDIUM_TERM_PROMPT_CHARS:
                updated = await _compress_summary(
                    updated, llm, model=model, agent_id=agent_id
                )
            await redis.set(
                _medium_term_key(agent_id, scope_id), updated, ex=MEDIUM_TERM_TTL
            )
            # Detached from the turn: the agent or account may have been
            # deleted during the LLM call (see ``memory_tombstones``).
            if await _undo_if_lane_deleted(redis, agent_id, scope_id):
                return

            # Persist consolidated memory to DB for durability + vector search.
            # Skipped for non-user scopes (anonymous conversations): the scope
            # id is a conversation UUID that violates the users FK, and the
            # content must stay conversation-scoped (Redis-only) rather than
            # leak into the shared agent-level memory.
            if not scope_is_user_id:
                logger.debug(
                    "Skipping durable persistence of consolidated memory for "
                    "agent %s: scope %s is an anonymous conversation scope "
                    "(Redis tiers only)",
                    agent_id,
                    scope_id,
                )
            else:
                try:
                    from app.core.database import async_session
                    from app.services.persistent_memory import persist_memory

                    async with async_session() as db:
                        await persist_memory(
                            db,
                            agent_id=agent_id,
                            content=updated,
                            memory_type="consolidated",
                            source="consolidation",
                            user_id=scope_id,
                        )
                        await db.commit()
                except Exception as persist_err:
                    logger.warning(
                        "Failed to persist consolidated memory to DB for agent %s: %s",
                        agent_id,
                        format_exception_message(persist_err),
                    )

        # Reset counter
        await redis.set(_counter_key(agent_id, scope_id), 0, ex=SHORT_TERM_TTL)
        if await _undo_if_lane_deleted(redis, agent_id, scope_id):
            return
        scope_label = f" (scope={scope_id})" if scope_id else ""
        logger.info("Consolidated memory for agent %s%s", agent_id, scope_label)

    except Exception as e:
        logger.warning("Failed to consolidate memory for agent %s: %s", agent_id, format_exception_message(e))


async def _compress_summary(
    summary: str,
    llm: BaseLLMProvider,
    *,
    model: str | None = None,
    agent_id: str = "unknown",
) -> str:
    """Compress an oversized medium-term summary back under the prompt cap.

    One LLM pass re-condenses the summary to the 400-word target the
    consolidation prompt already asks for.  If the call fails or the result
    is still oversized, the summary is hard-truncated so the returned value
    is ALWAYS within ``_MAX_MEDIUM_TERM_PROMPT_CHARS`` — callers can store
    it without re-checking.
    """
    from app.llm.base import ChatMessage

    compressed: str | None = None
    try:
        messages = [
            ChatMessage(
                role="system",
                content=(
                    "You are a memory curator. The following agent memory "
                    "summary has grown too large. Rewrite it as a tighter "
                    "version that preserves all durable, still-relevant "
                    "knowledge.\n\n"
                    "Rules:\n"
                    "- Max 400 words. This is a hard limit.\n"
                    "- Use bullet points.\n"
                    "- Merge redundant or overlapping items.\n"
                    "- Drop outdated, contradicted, or one-off details.\n"
                    "- Do NOT add headers, timestamps, or meta-commentary."
                ),
            ),
            ChatMessage(
                role="user",
                # Notice-free view: a stale truncation notice is not
                # memory content and must not survive the rewrite - the
                # fresh generation gets its own notice if it is cut.
                content=(
                    "Memory summary to compress:\n\n"
                    f"{strip_leading_reasoning(_strip_stale_notices(summary))[:_COMPRESS_INPUT_MAX_CHARS]}"
                ),
            ),
        ]
        async with traced_generation(
            name="agent_memory_compress", messages=messages
        ) as _gen:
            _gen.response = await llm.chat(
                messages=messages,
                model=model,
                max_tokens=_SUMMARY_MAX_TOKENS,
                temperature=0.2,
                disable_thinking=True,
            )
        compressed = strip_leading_reasoning(_gen.response.content or "").strip() or None
        if compressed and answer_was_cut(_gen.response):
            # The compression itself hit the output budget: the text ends
            # mid-word.  Trim to the last complete statement and mark the
            # cut so the stored summary is never silently partial.
            compressed = _drop_clipped_tail(compressed) + _CLIPPED_GENERATION_NOTICE
    except Exception as e:
        logger.warning(
            "Medium-term memory compression LLM call failed for agent %s: %s",
            agent_id,
            format_exception_message(e),
        )

    result = compressed if compressed else strip_leading_reasoning(summary)
    if len(result) > _MAX_MEDIUM_TERM_PROMPT_CHARS:
        # Hard-truncate fallback: cut at a clean boundary and mark the cut
        # instead of slicing mid-word.
        result = _truncate_at_boundary(
            result, _MAX_MEDIUM_TERM_PROMPT_CHARS, _BUDGET_TRUNCATION_NOTICE
        )
    logger.info(
        "Compressed medium-term memory for agent %s: %d -> %d chars",
        agent_id,
        len(summary),
        len(result),
    )
    return result


async def compress_medium_term_memory(
    agent_id: str,
    llm: BaseLLMProvider,
    *,
    scope_id: str | None = None,
    model: str | None = None,
) -> bool:
    """Compress the stored medium-term summary if it exceeds the prompt cap.

    Self-healing entry point used by ``auto_heal_agent_memory``:
    loads the Redis medium-term summary for the given scope and, when it is
    larger than ``_MAX_MEDIUM_TERM_PROMPT_CHARS``, rewrites it with one LLM
    compression pass (hard truncation fallback) and stores the result back.

    Returns ``True`` when a compression was performed.
    """
    redis = await redis_client()
    key = _medium_term_key(agent_id, scope_id)
    medium = await redis.get(key) or ""
    if len(medium) <= _MAX_MEDIUM_TERM_PROMPT_CHARS:
        return False

    compressed = await _compress_summary(medium, llm, model=model, agent_id=agent_id)
    await redis.set(key, compressed, ex=MEDIUM_TERM_TTL)
    return not await _undo_if_lane_deleted(redis, agent_id, scope_id)


async def clear_memory(agent_id: str, *, tombstone: bool = False) -> None:
    """Delete all memory for an agent (used on agent deletion).

    Clears both the unscoped (assignment-level) keys and all per-user scoped
    keys that match the ``agent_mem:{agent_id}:*`` pattern. With *tombstone*
    (the agent is gone for good) a write still in flight for it undoes itself
    instead of recreating the memory (see ``memory_tombstones``).
    """
    redis = await redis_client()
    if tombstone:
        await bury(redis, agent_tombstone(agent_id))

    # Delete the unscoped (agent-level / assignment) keys
    await redis.delete(*lane_keys(agent_id))

    # Also delete any per-user scoped keys (agent_mem:{agent_id}:scope:*)
    pattern = _base_key(agent_id, "*")
    cursor: int | bytes = 0
    while True:
        cursor, keys = await redis.scan(cursor, match=pattern, count=200)
        if keys:
            await redis.delete(*keys)
        if not cursor:
            break

    logger.info("Cleared all memory for agent %s", agent_id)


async def clear_deleted_agent_memory(*agent_ids: str) -> None:
    """Drop the Redis memory of agents whose deletion has COMMITTED.

    Every path that deletes an agent calls this after its commit: before it,
    a failed delete would leave the agent with its memory gone, and a
    tombstone would block the memory of an agent that still exists. The
    tombstone stops a consolidation still in flight for the agent from writing
    its memory back (see ``memory_tombstones``). Best effort, one agent at a
    time - the agents are already gone.
    """
    for agent_id in agent_ids:
        try:
            await clear_memory(agent_id, tombstone=True)
        except Exception as exc:
            logger.warning(
                "Redis memory cleanup failed for deleted agent %s: %s",
                agent_id,
                format_exception_message(exc),
            )


async def clear_user_scoped_memory(user_id: str) -> None:
    """Delete *user_id*'s private memory lane with every agent (account deletion).

    Only the ``agent_mem:{agent_id}:scope:{user_id}:*`` keys go: the agents'
    own memory and the lanes of other users are left as they are. The caller
    buries the account tombstone first, so a write still in flight for one of
    these lanes undoes itself (see ``memory_tombstones``).
    """
    redis = await redis_client()
    pattern = f"{_base_key('*', user_id)}:*"
    cursor: int | bytes = 0
    while True:
        cursor, keys = await redis.scan(cursor, match=pattern, count=200)
        if keys:
            await redis.delete(*keys)
        if not cursor:
            break


def format_memory_for_prompt(memory: dict, *, agent_label: str | None = None) -> str:
    """Render loaded memory into a block suitable for the system prompt.

    The medium-term consolidated summary is capped at
    ``_MAX_MEDIUM_TERM_PROMPT_CHARS`` characters to prevent runaway accretion
    from bloating the system prompt and slowing LLM responses.  A
    truncation notice is appended so the LLM knows the data was cut.

    *agent_label* (agent name or id) is only used to attribute the truncation
    WARNING so the transient truncation can be correlated with the automatic
    compression that follows (write-time clamp / self-heal pass).

    Returns an empty string if there is nothing to inject.
    """
    parts: list[str] = []

    # Recall strips too, so summaries stored before #12356 stop reaching the prompt.
    medium = strip_leading_reasoning(memory.get("medium_term") or "")
    short = memory.get("short_term")

    if medium:
        parts.append("── Agent Memory (accumulated knowledge) ──")
        if len(medium) > _MAX_MEDIUM_TERM_PROMPT_CHARS:
            # Transient state: the write-time clamp in ``consolidate_memory``
            # rewrites the oversized summary at its next consolidation cycle,
            # and the assignment-path cap breach schedules an immediate
            # ``auto_heal_agent_memory`` pass.  No operator action
            # is required — this truncation only bridges until then.
            logger.warning(
                "Agent '%s' medium-term memory (%d chars) exceeds cap "
                "(%d chars) — truncating to fit the system prompt memory "
                "budget. The summary will be compressed automatically at "
                "the next consolidation or self-heal pass.",
                agent_label or "unknown",
                len(medium),
                _MAX_MEDIUM_TERM_PROMPT_CHARS,
            )
            # Keep the final block (notice included) within the cap so the
            # constant is a strict upper bound for prompt-size budgeting.
            # Boundary-aware so the injected block never ends mid-word
            # right before the notice.
            medium = _truncate_at_boundary(
                medium, _MAX_MEDIUM_TERM_PROMPT_CHARS, _BUDGET_TRUNCATION_NOTICE
            )
        parts.append(medium)

    if short:
        parts.append("\n── Recent Activity ──")
        for entry in reversed(short):  # chronological order
            ts = entry.get("timestamp", "")[:16]
            src = entry.get("source", "?")
            summary = strip_leading_reasoning(entry.get("summary", ""))
            parts.append(f"• [{ts}] {src}: {summary[:300]}")

    return "\n".join(parts) if parts else ""
