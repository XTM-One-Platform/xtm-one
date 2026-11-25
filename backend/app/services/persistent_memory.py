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

"""Persistent agent memory – durable memory with vector search in PostgreSQL.

Extends the Redis-based agent memory with a durable DB layer:
  - Consolidated memories are persisted to ``agent_memory_entries`` with embeddings.
  - Agents can semantically search their own accumulated knowledge.
  - Memories survive Redis restarts and are available across sessions.

Public entry points:
  1. ``persist_memory()`` – called after Redis consolidation to durably store.
  2. ``search_agent_memories()`` – semantic search over an agent's memory entries.
  3. ``extract_and_persist_facts()`` – extract key facts during context compaction.
  4. ``reconcile_and_persist_facts()`` – extract + reconcile facts (supersedes
     contradicting old entries instead of letting them coexist).
  5. ``load_persistent_context()`` – load facts for system prompt injection
     (relevance-ranked against an optional query, recency fallback).
  6. ``clear_agent_memories()`` – delete all persistent memories for an agent.
  7. ``embed_text()`` – generate an embedding for a given text string.

Memory hygiene (added for long-running agents):
  8. ``deduplicate_fact()`` – check semantic similarity before persisting a fact.
  9. ``prune_old_memories()`` – age-based removal of stale facts.
  10. ``enforce_memory_cap()`` – per-agent cap on total fact entries.
  11. ``consolidate_agent_facts()`` – merge related facts into condensed summaries.
  12. ``reembed_stale_memories()`` – re-embed entries whose embedding model or
      dimensions no longer match the platform embedding target.
  13. ``run_memory_maintenance()`` – orchestrates all hygiene tasks for all agents.
  14. ``get_memory_health()`` – per-agent memory statistics for the health endpoint.
  15. ``auto_heal_agent_memory()`` – reactive, throttled per-agent self-heal
      (persistent-tier hygiene + Redis medium-term compression) triggered
      when an agent's combined memory block exceeds the assignment prompt
      memory budget.

Tradecraft memory:
  16. ``TRADECRAFT_MEMORY_TYPE`` / ``tradecraft_leak_reason()`` – a durable,
      agent-level, target-free memory class for knowledge that must OUTLIVE
      the engagement that produced it.  ``clear_agent_memories()`` can be
      asked to preserve it, so a run-end purge drops forensic state without
      erasing what the agent learned about the platform.

State memory and supersession:
  17. ``STATE_MEMORY_TYPE`` – what an evolving object (a document, a deck, a
      ticket) looked like when the entry was stored, named by its
      ``subject_key``.  ``persist_memory()`` supersedes the writer's older
      state entry on the same subject (and the entry ``supersedes`` names);
      ``retire_memories()`` retires entries that no longer hold.  A superseded
      entry is kept and searchable, ranked after the active ones, and never
      reaches a prompt: of the state entries a reader sees on one subject only
      the newest is active, in SQL (``_active_for_reader``) and in the
      renderer.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple

from sqlalchemy import (
    and_,
    delete,
    exists,
    func,
    literal,
    not_,
    or_,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.utils import format_exception_message, is_zero_norm_vector, safe_uuid
from app.llm.base import ContextOverflowError
from app.llm.cut_answers import answer_was_cut
from app.llm.model_limits import retry_output_budget
from app.llm.provider_access import ProviderNotUsableError, acting_for, excluded_user_ids
from app.models.agent_memory_entry import AgentMemoryEntry
from app.services.langfuse_tracing import traced_generation
from app.services.leaked_reasoning import strip_leading_reasoning
from app.services.memory_holds import held_by_consolidation

if TYPE_CHECKING:
    from collections.abc import Collection

    from app.llm.base import BaseLLMProvider

logger = logging.getLogger(__name__)

# Code-fence wrappers the LLM occasionally emits around JSON.  ``str.strip("`")``
# is too aggressive — it would consume backticks that legitimately belong to
# the JSON payload (e.g. fact text containing a backtick).  These regexes
# strip ONLY a leading ```` ``` ```` (optionally tagged ``json``) and a trailing
# ```` ``` ````.
_FENCE_OPEN_RE = re.compile(r"^```(?:json)?\s*", re.IGNORECASE)
_FENCE_CLOSE_RE = re.compile(r"\s*```\s*$")


def _strip_code_fences(text_val: str) -> str:
    """Strip a single leading/trailing Markdown code fence from *text_val*.

    Unlike ``str.strip("`")`` this preserves backticks that are part of
    the actual content (e.g. a fact like \"prefix with `prod_`\").
    """
    return _FENCE_CLOSE_RE.sub("", _FENCE_OPEN_RE.sub("", text_val.strip()))


def _sanitize_memory_text(text_val: str) -> str:
    """Strip PostgreSQL-incompatible null bytes from memory text fields.

    asyncpg rejects ``\\x00`` in TEXT columns with
    ``CharacterNotInRepertoireError``, which aborts ``db.flush()`` and
    silently drops the memory entry.  Upstream tool/GraphQL
    responses occasionally carry raw null bytes, so every text field
    written to ``agent_memory_entries`` must pass through this helper.
    """
    return text_val.replace("\x00", "")


# Minimum cosine similarity score for search results to be considered relevant.
_RELEVANCE_THRESHOLD = 0.3

# Cap for the per-turn query embedding issued by ``load_persistent_context``
# in the chat/assignment SETUP path (P1.3).  Without it the
# embedding HTTP call rides the provider SDK read timeout (up to 600 s) and
# a hung embedding endpoint stalls every turn before the loop even starts.
# On timeout the loader degrades to the recency-based (non-semantic) load.
_SETUP_EMBED_TIMEOUT_SECS = 30.0
# A chat turn waits for the embedding before its first provider request, so
# it passes a much shorter budget: past it the turn takes the same recency
# fallback rather than holding the user's first token (dev:chat-reactivity R2).
CHAT_TURN_EMBED_BUDGET_SECS = 0.4
# The chat turn's whole wait for the embedding, counted from the start of its
# persistent tier: provider resolution and the eligibility probe come out of
# it, and what is left of the 0.4 s tier bound pays for the final vector or
# recency query (dev:chat-reactivity R2, #8757).
CHAT_TURN_EMBED_DEADLINE_SECS = 0.3
# Below this window an embed cannot answer; the recency load answers instead.
_MIN_EMBED_WINDOW_SECS = 0.05
# Maximum number of facts extracted per context compaction cycle.
_MAX_FACTS_PER_COMPACTION = 10
# Output budget of one fact-extraction answer (one fact per line). An answer
# that hits it is retried once with twice the budget, capped at the model's
# output ceiling; a list still cut loses its last line, the one the budget cut,
# so a fragment never becomes a candidate fact.
_EXTRACT_MAX_TOKENS = 500
# Minimum character length for a fact to be worth persisting.
_MIN_FACT_LENGTH = 10

# ── Memory hygiene constants ─────────────────────────────────────────────────
# Cosine similarity threshold for considering two facts as duplicates.
_DEDUP_SIMILARITY_THRESHOLD = 0.92
# Lower bound for the "related fact" zone used by reconciliation.  Facts
# falling in [_RECONCILE_RELATED_THRESHOLD, _DEDUP_SIMILARITY_THRESHOLD)
# are semantically close enough to potentially CONTRADICT each other
# without being exact duplicates — exactly the case the LLM curator must
# resolve (e.g. "use country filter" vs "use subregion filter").
_RECONCILE_RELATED_THRESHOLD = 0.65
# Maximum number of related existing facts to surface to the LLM per
# reconciliation call (keeps the prompt small even for agents with
# hundreds of facts).
_RECONCILE_MAX_RELATED = 12
# Output budget of one curator answer: headroom for the Sonnet 5 tokenizer
# (~30% more tokens for the same text). An answer that still hits it is never
# applied; the retry gets twice the budget, capped at the model's output
# ceiling.
_RECONCILE_MAX_TOKENS = 1500
# Default retention period for facts (days).  Facts older than this are pruned.
_FACT_RETENTION_DAYS = 90
# Maximum number of fact entries per (agent, user_id scope).  The agent-level
# pool (user_id IS NULL) and each user-scoped pool are capped independently so
# one user's facts can never evict another user's.
_MAX_FACTS_PER_AGENT = 200
# Batch size for fact consolidation — how many related facts to merge at once.
_CONSOLIDATION_BATCH_SIZE = 15
# Minimum number of facts before fact consolidation is triggered.
_CONSOLIDATION_MIN_FACTS = 50
# Minimum facts a scope must hold to be worth an LLM merge call.
_CONSOLIDATION_MIN_BATCH = 3
# Output budget of one merge. The batch is deleted once the merge lands, so a
# merge cut at it never lands: it is retried once with twice the budget, capped
# at the model's output ceiling, and a merge still cut leaves the batch intact
# for the next run.
_CONSOLIDATION_MAX_TOKENS = 800
# Upper bound on the number of scopes consolidated per maintenance run.
# Heavily shared agents can accumulate facts under many user_id scopes;
# without a bound, one maintenance cycle would issue O(#scopes) LLM calls.
# The most crowded scopes are processed first; the rest drain on later runs.
_CONSOLIDATION_MAX_SCOPES_PER_RUN = 10
# Batch size for re-embedding stale memory entries (embedding model change).
_REEMBED_BATCH_SIZE = 50

# ── Tradecraft memory ────────────────────────────────────────────────────────
# A durable class for knowledge that must OUTLIVE the engagement that produced
# it: how a platform behaves, which capabilities exist, which mistakes not to
# repeat.  Deliberately separate from ``fact`` because the two have opposite
# retention needs.  A run's forensic facts (target lists, proven verdicts,
# resolved scope) MUST be purged when the run ends or the next engagement
# "resumes" a dead attack path and harvests phantom verdicts; a lesson about
# the platform stays true forever.  See ``clear_agent_memories``.
TRADECRAFT_MEMORY_TYPE = "tradecraft"
# Hard cap per agent, enforced at write time with oldest-first eviction.
# ``load_persistent_context`` bounds what reaches the prompt, so the risk of an
# unbounded pool is not prompt size but relevance: tradecraft is exempt from
# age-based pruning (a lesson does not expire), so without a cap it would grow
# forever and crowd the fixed number of injected slots with stale lessons.
_MAX_TRADECRAFT_PER_AGENT = 50

# ── State memory and supersession ────────────────────────────────────
# What an evolving object looked like when the entry was stored. Age-pruned
# like a fact; a newer state entry on the same ``subject_key`` supersedes it.
STATE_MEMORY_TYPE = "state"
# The width of ``agent_memory_entries.subject_key``.
SUBJECT_KEY_MAX_CHARS = 255
# Superseded state entries kept per (agent, scope, subject), newest first: the
# history ``search_memory`` still finds, bounded so an object observed every
# hour does not grow its subject without limit.
_MAX_SUPERSEDED_PER_SUBJECT = 10
# Carried by a prompt memory block holding a state entry, and only then.
STATE_GUIDANCE = (
    "State entries describe a past observation; re-read the system of record "
    "before acting on them."
)


def normalize_memory_type(memory_type: str) -> str:
    """Canonicalise a caller-supplied ``memory_type``.

    Every decision keyed on the memory class is an exact string comparison -
    the tradecraft leak guard and agent-level scoping here, the
    ``allow_tradecraft`` authorization gate in the ``store_memory`` tool, the
    ``consolidated`` upsert, the ``fact`` / ``learned`` prune and cap filters,
    and the purge's ``preserve_memory_types`` list.  ``store_memory`` takes
    this argument straight from the model, which spells it ``Tradecraft`` or
    `` tradecraft `` often enough to matter, and an unnormalised variant opts
    out of ALL of them at once: it walks past the authorization gate, past the
    target guard, keeps the user scope, and is then never deduplicated, capped
    or age-pruned (``prune_old_memories`` only ages ``fact`` / ``learned``).
    Both the store and any caller that branches on the stored class must
    normalise through here, so the two can never disagree.
    """
    return _sanitize_memory_text(memory_type).strip().lower()


# Target identifiers that may never enter tradecraft.  The entire safety
# argument for carrying tradecraft across runs is that it holds no trace of
# the engagement that produced it: the moment a host, address or entity id
# survives a purge, a later run can "recall" a verdict about a target that is
# not even in its scope — precisely the failure the purge exists to prevent.
# Detection is deterministic rather than an LLM judgement call because this is
# a correctness boundary, and it errs towards refusing: a false positive costs
# one rephrase, a false negative leaks a target across engagements forever.
_TRADECRAFT_FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("an IP address", re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")),
    ("a URL", re.compile(r"\b[a-z][a-z0-9+.-]*://", re.IGNORECASE)),
    ("an email address", re.compile(r"\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b", re.IGNORECASE)),
    (
        "an entity id (UUID)",
        re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            re.IGNORECASE,
        ),
    ),
    # Any dotted name is treated as a hostname, with NO file-extension escape
    # hatch: a great many extensions are also live TLDs (``.sh``, ``.py``,
    # ``.md``, ``.zip``, ``.rs``), so an allow-list would wave through exactly
    # the hostnames this guard exists to stop - ``honey.scanme.sh`` reads as a
    # shell script and is a real host. Refusing "config.yaml" too is the
    # accepted cost: a lesson rarely needs a file name and can say "the config
    # file" instead, whereas a leaked host is permanent.
    (
        "a hostname or domain",
        re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}\b", re.IGNORECASE),
    ),
)

# IPv6 is validated rather than pattern-matched.  A regex that covers every
# compressed form (``::1``, ``fe80::1``, ``2001:db8::1``) without misfiring is
# not worth maintaining, and a partial one silently lets those literals - all
# perfectly routable targets - through.  Candidates are any run of hex and
# colons; ``ipaddress`` decides.  A bonus of parsing over matching: "12:30"
# fails to parse, so ordinary times are not refused.
_IPV6_CANDIDATE_RE = re.compile(r"[0-9A-Fa-f:]{3,}")


def _contains_ipv6(content: str) -> bool:
    """Return True when *content* holds a parseable IPv6 literal, any form."""
    for match in _IPV6_CANDIDATE_RE.finditer(content):
        token = match.group(0)
        if ":" not in token:
            continue
        try:
            ipaddress.ip_address(token)
        except ValueError:
            continue
        return True
    return False


# The identifiers of the engagement currently driving this task, when one is.
# ``None`` means "no trusted run context", which is also what gates the ability
# to write tradecraft at all (see ``in_tradecraft_run_context``).
#
# Lexical patterns can only catch STRUCTURED identifiers.  A single-label host
# (``dc01``, ``DESKTOP-1``) and a person's name are just words, and every
# heuristic that catches them also rejects the domain's own vocabulary -
# ``ipv4``, ``ps1``, ``log4j``, ``T1059``, "Active Directory".  So instead of
# guessing what a target looks like, the run tells us what its targets ARE, and
# those exact strings are refused.  Zero false positives, and it closes the
# in-scope-person case that no name heuristic could.
# The value is a MUTABLE set, and deliberately so.  The agent loop runs each
# tool in its own child task, and a child inherits a COPY of the context: a
# child that rebound this ContextVar would update only its own copy, so the
# scope tool's discovery would be invisible to the next tool call and the guard
# would still be matching the cycle-start snapshot.  Binding one set per cycle
# and mutating it in place means every child shares the same object, while
# concurrent runs - which bind their own - stay fully isolated.
class _TradecraftRunContext:
    """The live target set of one engagement, plus whether writes are allowed.

    A single mutable object per cycle for the reason above: both fields have to
    be changeable from inside a child task and seen by the next one.
    ``suspensions`` exists so a scope MUTATION can close the window around itself
    - while the perimeter is being replaced, the guard's target set is by
    definition stale, and a concurrent write must be refused rather than checked
    against it.

    It is a COUNT rather than a flag because tool calls run under
    ``asyncio.gather``: two replacements can overlap, and a flag would let the
    first one to finish re-authorize writes while the second is still rewriting
    the perimeter - reopening the exact window the suspension exists to close.
    """

    __slots__ = ("suspensions", "targets")

    def __init__(self, targets: set[str]) -> None:
        self.targets = targets
        self.suspensions = 0


_tradecraft_run_targets: ContextVar[_TradecraftRunContext | None] = ContextVar(
    "tradecraft_run_targets", default=None
)


def _normalized_targets(target_identifiers: object) -> set[str]:
    """Normalise declared identifiers for matching.

    Every non-empty declared value is kept, however short.  These come from the
    authoritative scope, so matching one is never a heuristic false positive -
    a host really named ``db``, or a person named ``Li``, is as much a target as
    any other, and dropping it would leave it permanently storable.
    """
    targets: set[str] = set()
    if isinstance(target_identifiers, (list, tuple, set, frozenset)):
        for raw in target_identifiers:
            if not isinstance(raw, str):
                continue
            token = raw.strip().lower()
            if token:
                targets.add(token)
    return targets


def set_tradecraft_run_context(target_identifiers: object) -> object:
    """Mark this task as running inside a trusted engagement, with its targets.

    Returns a token for :func:`reset_tradecraft_run_context`.  Task-local, so
    concurrent runs never see each other's targets.  Unusable values are
    skipped rather than raising - a malformed scope payload must not abort a
    cycle, and the lexical guard still applies.
    """
    return _tradecraft_run_targets.set(
        _TradecraftRunContext(_normalized_targets(target_identifiers))
    )


def extend_tradecraft_run_context(target_identifiers: object) -> None:
    """Add newly discovered targets to the current run context.

    The set handed to :func:`set_tradecraft_run_context` is only a snapshot from
    the start of the cycle, but the perimeter moves DURING one: the standard
    first cycle opens with an empty allow-list, the operator answers the scoping
    question, and the agent records the resolved scope mid-cycle. Without this,
    the guard would still be matching against the empty snapshot and a later
    write in that same cycle could persist a target the run had just adopted.

    Mutates the set IN PLACE rather than rebinding the ContextVar, because this
    runs inside the scope tool's own child task: rebinding would update that
    child's copy of the context and leave the next tool call still looking at
    the cycle-start snapshot.

    No-op outside a run context - it must never create one, since holding a
    context is what authorizes the write in the first place.
    """
    context = _tradecraft_run_targets.get()
    if context is None:
        return
    context.targets |= _normalized_targets(target_identifiers)


def suspend_tradecraft_writes() -> None:
    """Refuse tradecraft writes until :func:`resume_tradecraft_writes`.

    Called around a scope REPLACEMENT. While the perimeter is being rewritten the
    guard's target set is stale by definition, and tool calls can run
    concurrently, so a write racing the change would be checked against the old
    scope. Suspending is also the fail-closed state: if the replacement cannot be
    confirmed, writes simply stay off for the rest of the cycle, costing it its
    lessons - which a later cycle can re-learn, unlike a leaked target.

    Nests: each suspension must be released by its own resume, so overlapping
    replacements cannot re-authorize each other.
    """
    context = _tradecraft_run_targets.get()
    if context is not None:
        context.suspensions += 1


def resume_tradecraft_writes() -> None:
    """Release ONE suspension once that perimeter change has been confirmed.

    Writes stay off while any other replacement is still outstanding.  Clamped at
    zero so an unpaired resume cannot bank credit that would silently swallow a
    later, real suspension.
    """
    context = _tradecraft_run_targets.get()
    if context is not None and context.suspensions > 0:
        context.suspensions -= 1


def reset_tradecraft_run_context(token: object) -> None:
    """Restore the previous run context; never raises on an alien token."""
    try:
        _tradecraft_run_targets.reset(token)  # type: ignore[arg-type]
    except (ValueError, TypeError):  # pragma: no cover - defensive
        logger.debug("Ignoring stale tradecraft run-context token")


def in_tradecraft_run_context() -> bool:
    """True when a trusted engagement is running AND its perimeter is settled."""
    context = _tradecraft_run_targets.get()
    return context is not None and context.suspensions == 0


# Whether THIS task is executing inside a trusted autonomous run at all. This is
# deliberately DISTINCT from ``_tradecraft_run_targets`` above and answers a
# different question. The tradecraft context carries the run's resolved target
# names for the write guard, so it can only be established once the perimeter has
# SETTLED - which means ``in_tradecraft_run_context()`` is legitimately False on
# a first cycle (or an OpenAEV outage) whose scope has not resolved yet, and
# stays False while a perimeter replacement is suspended. Fine for authorizing a
# durable tradecraft WRITE (a lost lesson is re-learnable, a leaked target is
# not), but WRONG for the memory-SCOPE decision: the orchestrator's own memory
# must land in the shared agent-level pool from its very FIRST cycle (WS5), which
# is exactly when no perimeter exists. So the scope decision reads this
# whole-call marker instead, set UNCONDITIONALLY around the orchestrator's chat
# and independent of the perimeter. It is still only half the gate - the caller
# pairs it with the designated-writer identity, so a specialist consulted mid-run
# (which inherits this ContextVar) stays per-user.
_autonomous_run_active: ContextVar[bool] = ContextVar(
    "autonomous_run_active", default=False
)


def set_autonomous_run_context() -> object:
    """Mark this task (and its children) as running a trusted autonomous run.

    Set UNCONDITIONALLY around the orchestrator's whole chat call - unlike
    :func:`set_tradecraft_run_context`, which waits for the perimeter to resolve
    because it must carry target names. Returns a token for
    :func:`reset_autonomous_run_context`. Task-local, so concurrent runs and
    ordinary chats never see each other's marker.
    """
    return _autonomous_run_active.set(True)


def reset_autonomous_run_context(token: object) -> None:
    """Restore the previous autonomous-run marker; never raises on an alien token."""
    try:
        _autonomous_run_active.reset(token)  # type: ignore[arg-type]
    except (ValueError, TypeError):  # pragma: no cover - defensive
        logger.debug("Ignoring stale autonomous-run token")


def in_autonomous_run_context() -> bool:
    """True inside a trusted autonomous run, regardless of perimeter state.

    The memory-SCOPE signal (WS5): the orchestrator writes agent-level from its
    first cycle, before its scope resolves. NOT a tradecraft-write gate - a
    tradecraft write still requires the settled perimeter that
    :func:`in_tradecraft_run_context` reports.
    """
    return _autonomous_run_active.get()


def _run_target_leak(content: str) -> bool:
    """True when *content* names one of the current run's own targets."""
    context = _tradecraft_run_targets.get()
    targets = context.targets if context is not None else None
    if not targets:
        return False
    lowered = content.lower()
    # Boundaries rather than a plain substring test: the latter would fire on any
    # lesson merely containing the token as a fragment of a longer word.
    return any(
        re.search(rf"(?<![0-9a-z]){re.escape(target)}(?![0-9a-z])", lowered)
        for target in targets
    )


def tradecraft_leak_reason(content: str) -> str | None:
    """Return what makes *content* unfit for tradecraft memory, or ``None``.

    Tradecraft survives the run-end purge, so it must generalise: "the arsenal
    has no Linux persistence action for this technique" is a lesson, while
    "host X is vulnerable to CVE-YYYY-NNNNN" is a forensic verdict that must
    die with its engagement.  Returns a short human phrase naming the offending
    identifier class, suitable for :func:`tradecraft_refusal_message`.

    Two layers, because neither is sufficient alone.  The patterns above catch
    STRUCTURED identifiers (address, URL, FQDN, entity id, email) in any
    content.  Unstructured ones - a single-label host like ``dc01``, a person's
    name - are indistinguishable from ordinary words, so they are caught by
    matching against the identifiers the current run declared
    (:func:`set_tradecraft_run_context`).  Do not replace the second layer with
    a name heuristic: every rule that catches ``dc01`` also rejects ``ipv4``,
    ``ps1``, ``log4j`` and ``T1059``, which are exactly what a lesson is made
    of.  Outside a run context the second layer is inert, which is safe because
    writing tradecraft requires that context in the first place.
    """
    for label, pattern in _TRADECRAFT_FORBIDDEN_PATTERNS:
        if pattern.search(content):
            return label
    if _contains_ipv6(content):
        return "an IPv6 address"
    if _run_target_leak(content):
        return "a target from the current run's scope"
    return None


def tradecraft_skip_fork_reason(content: str) -> str | None:
    """Refuse lessons that teach skipping payload-based no-parser contracts.

    Those contracts are fork candidates (duplicate + parsers on the copy). A
    native injector that already emits findings is reuse of THAT contract, not
    a substitute. Production stored the opposite lesson because a formatter
    said they "cannot feed" chaining; that row is injected into every later
    run. A correct "when none, fork" lesson must still pass.
    """
    lowered = content.lower()
    if "avoiding an unnecessary" in lowered and "fork" in lowered:
        return "a skip-fork lesson"
    # The lesson tells the model NOT to fork: an "avoid" / "skip" / "without"
    # type verb governing "fork" within a short window. Ordering matters, so
    # this must NOT fire on "prefer forking payload-based dead-ends; avoid
    # updating the shared original", where "avoid" governs the update and
    # forking is the (correct) recommendation.
    if re.search(
        r"\b(?:avoid(?:s|ing)?|skip(?:s|ping)?|without|instead of|rather than|never)\b"
        r"[^.]{0,40}?\bfork",
        lowered,
    ):
        return "a skip-fork lesson"
    # A correct lesson names the fork. The production skip lesson stopped at
    # "cannot feed" and then preferred a native injector instead.
    if (
        "cannot feed" in lowered
        and "chained" in lowered
        and not any(
            token in lowered
            for token in ("fork it", "fork them", "duplicate", "add parsers", "add regex")
        )
    ):
        return "a skip-fork lesson"
    return None


def tradecraft_skip_fork_message() -> str:
    """Refusal when tradecraft tries to teach skip-fork / prefer-native-instead."""
    return (
        "Rejected: payload-based actions that produce no findings are fork "
        "candidates - duplicate them and add REGEX parsers on the copy. Do not "
        "store a lesson that says skip them or prefer a native injector to avoid "
        "a fork. A native injector that already emits findings is reuse of that "
        "contract, not a substitute for forking a relevant payload-based one."
    )


def tradecraft_refusal_message(reason: str) -> str:
    """Build the actionable refusal returned to an agent on a rejected write.

    Single source of truth so every caller refuses identically and always
    tells the agent how to rephrase instead of just saying no.
    """
    return (
        "Rejected: tradecraft memory must not name a specific target, and this "
        f"contains {reason}. Tradecraft outlives the engagement, so record the "
        "generalizable lesson instead of the observation about one target - for "
        "example 'the arsenal has no Linux persistence action for this technique' "
        "rather than a verdict about a particular host. Target-specific findings "
        "belong in the run's own state, not in memory."
    )


class TradecraftNotAuthorizedError(PermissionError):
    """Raised when a tradecraft write happens outside a trusted run context.

    Distinct from :class:`TradecraftRejectedError`: the content may be perfectly
    good, but the caller is not executing an engagement and so may not publish an
    agent-level row that every user of the agent will see.
    """

    def __init__(self) -> None:
        super().__init__(
            "Rejected: tradecraft memory can only be written from inside an "
            "authorized run. Use 'learned' or 'fact' to record this instead."
        )


class TradecraftRejectedError(ValueError):
    """Raised when a tradecraft write names a specific target or a skip-fork lesson.

    Carries the already-formatted, actionable refusal as its message so tool
    layers can surface it verbatim to the model.
    """

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or tradecraft_refusal_message(reason))
        self.reason = reason


_TRADECRAFT_SUBJECT_REFUSAL = (
    "Rejected: tradecraft is a lesson about the platform, not an observation "
    "of one object, so it takes no subject_key and supersedes no entry. Record "
    "what an object looks like now as memory_type 'state' instead."
)


def normalize_subject_key(subject_key: object) -> str | None:
    """Canonicalise a caller-supplied ``subject_key``; ``None`` when it names nothing.

    The key is rendered on its entry's line in a prompt, so whitespace runs
    (line breaks included) become one space and control characters go. Case
    is kept: an object id may be case-sensitive. Cut at the column width.
    """
    if subject_key is None:
        return None
    value = " ".join(_sanitize_memory_text(str(subject_key)).split())
    value = "".join(ch for ch in value if ch.isprintable())
    return value[:SUBJECT_KEY_MAX_CHARS] or None


def _readable_by(model: Any, user_id: Any) -> Any:
    """The entries of *model* a reader sees: the agent-level pool, plus its own.

    *user_id* is the reader's UUID, ``None`` for a reader with no user (an
    assignment run, an anonymous chat), or a column naming it.
    """
    if user_id is None:
        return model.user_id.is_(None)
    return or_(model.user_id.is_(None), model.user_id == user_id)


def _written_by(model, user_id: uuid.UUID | None):
    """The one pool a writer's entries land in, and the only one it may retire from.

    A user's session never supersedes the agent-level pool, which every other
    user of the agent reads, nor another user's entries.
    """
    if user_id is None:
        return model.user_id.is_(None)
    return model.user_id == user_id


def _shadowed_by_newer_state(user_id):
    """A state entry another active state entry on its subject outdates for this reader.

    The writer's own older state entry is flagged when the newer one is
    stored; this covers what a write cannot flag: a reader who sees both the
    agent-level pool and their own, each holding a state entry on the subject.
    The newest one the reader sees wins, ``created_at`` then ``id``.
    """
    newer = aliased(AgentMemoryEntry)
    return and_(
        AgentMemoryEntry.memory_type == STATE_MEMORY_TYPE,
        AgentMemoryEntry.subject_key.is_not(None),
        exists().where(
            newer.agent_id == AgentMemoryEntry.agent_id,
            newer.subject_key == AgentMemoryEntry.subject_key,
            newer.memory_type == STATE_MEMORY_TYPE,
            newer.superseded_at.is_(None),
            _readable_by(newer, user_id),
            tuple_(newer.created_at, newer.id)
            > tuple_(AgentMemoryEntry.created_at, AgentMemoryEntry.id),
        ),
    )


def superseded_for_reader(user_id: str | None):
    """SQL: the entry no longer holds for this reader (retired, or outdated by a newer state).

    The definition ``search_memory`` reports as ``superseded`` and the prompt
    block leaves out (its negation, ``_active_for_reader``).
    """
    return or_(
        AgentMemoryEntry.superseded_at.is_not(None),
        _shadowed_by_newer_state(safe_uuid(user_id)),
    )


def _active_for_reader(user_id: uuid.UUID | None):
    return and_(
        AgentMemoryEntry.superseded_at.is_(None),
        not_(_shadowed_by_newer_state(user_id)),
    )


async def _lock_subject(
    db: AsyncSession, agent_id: str, user_id: uuid.UUID | None, subject_key: str
) -> None:
    """Serialize state writes on one (agent, scope, subject) for this transaction.

    Two concurrent writes would otherwise each find no active predecessor and
    both stay active. The read side picks the newest one anyway; the lock keeps
    the stored flags right too.
    """
    digest = hashlib.blake2b(
        f"memory-subject|{agent_id}|{user_id or ''}|{subject_key}".encode(), digest_size=8
    ).digest()
    key = int.from_bytes(digest, "big", signed=True)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def _supersede(
    db: AsyncSession,
    entry: AgentMemoryEntry,
    *,
    target_id: uuid.UUID | None,
    by_subject: bool,
) -> list[str]:
    """Flag the writer's active entries *entry* replaces; their ids.

    *target_id* is the entry ``supersedes`` names; *by_subject* adds the
    writer's state entries on *entry*'s subject. Both are kept, pointed at
    *entry* through ``superseded_by_id``.
    """
    replaced = []
    if target_id is not None:
        replaced.append(AgentMemoryEntry.id == target_id)
    if by_subject:
        replaced.append(
            and_(
                AgentMemoryEntry.memory_type == STATE_MEMORY_TYPE,
                AgentMemoryEntry.subject_key == entry.subject_key,
            )
        )
    if not replaced:
        return []
    result = await db.execute(
        update(AgentMemoryEntry)
        .where(
            AgentMemoryEntry.agent_id == entry.agent_id,
            _written_by(AgentMemoryEntry, entry.user_id),
            AgentMemoryEntry.superseded_at.is_(None),
            AgentMemoryEntry.id != entry.id,
            or_(*replaced),
        )
        .values(superseded_at=datetime.now(UTC), superseded_by_id=entry.id)
        .returning(AgentMemoryEntry.id)
        .execution_options(synchronize_session=False)
    )
    ids = [str(row_id) for row_id in result.scalars().all()]
    if by_subject and ids:
        await _trim_subject_history(db, entry)
    return ids


async def _trim_subject_history(db: AsyncSession, entry: AgentMemoryEntry) -> int:
    """Delete the writer's superseded state entries on *entry*'s subject past the newest few."""
    same_history = (
        AgentMemoryEntry.agent_id == entry.agent_id,
        _written_by(AgentMemoryEntry, entry.user_id),
        AgentMemoryEntry.subject_key == entry.subject_key,
        AgentMemoryEntry.memory_type == STATE_MEMORY_TYPE,
        AgentMemoryEntry.superseded_at.is_not(None),
    )
    keep_ids = (
        select(AgentMemoryEntry.id)
        .where(*same_history)
        .order_by(AgentMemoryEntry.created_at.desc(), AgentMemoryEntry.id.desc())
        .limit(_MAX_SUPERSEDED_PER_SUBJECT)
        .scalar_subquery()
    )
    result = await db.execute(
        delete(AgentMemoryEntry)
        .where(*same_history, AgentMemoryEntry.id.notin_(keep_ids))
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def persist_memory(
    db: AsyncSession,
    *,
    agent_id: str,
    content: str,
    memory_type: str = "consolidated",
    source: str = "chat",
    user_id: str | None = None,
    skip_dedup: bool = False,
    subject_key: str | None = None,
    supersedes: str | None = None,
    superseded_out: list[str] | None = None,
    _provider_model: tuple | None = None,
    _precomputed_embedding: list[float] | None = None,
) -> AgentMemoryEntry | None:
    """Persist a memory entry to the database with an embedding.

    Called after Redis consolidation to create a durable copy.
    If a consolidated entry already exists for this agent+user scope,
    it is updated rather than duplicated.

    *subject_key* names the object the entry describes. A
    :data:`STATE_MEMORY_TYPE` entry with a subject supersedes the writer's
    active state entries on that subject, and *supersedes* names one more
    entry it replaces, whatever its type. Only the writer's own pool is ever
    flagged (``_written_by``): an id outside it is left as it is. The ids
    flagged are appended to *superseded_out*. An entry naming *supersedes* is
    stored even when a near-duplicate exists: it is the replacement the caller
    asked for.

    For **fact** entries, semantic deduplication is applied: if an existing
    fact with cosine similarity >= ``_DEDUP_SIMILARITY_THRESHOLD`` already
    exists for the same agent+scope, the new fact is skipped and ``None``
    is returned.

    Pass ``skip_dedup=True`` to bypass that check.  It MUST be used by
    callers that have already DELETED this entry's predecessors and so
    have to guarantee the curated replacement is stored — fact
    consolidation (merges N facts into 1 after deleting the N) and
    reconciliation SUPERSEDE/MERGE (replaces contradicting facts after
    deleting them).  A dedup skip there would delete the source facts
    without persisting the merged/curated result, *losing* information;
    in the repeating consolidation maintenance loop it would also re-run
    forever on the same retained facts.

    When *_provider_model* is supplied as ``(provider, model)`` the
    embedding provider DB lookup is skipped (batch optimisation).

    When *_precomputed_embedding* is supplied (together with
    *_provider_model* for the model provenance stamp), the embedding
    HTTP round-trip is skipped entirely.  Callers that already embedded
    the exact same text (e.g. the reconciliation flow, which embeds
    every candidate up-front for the contradiction-zone lookup) MUST
    pass it so no embedding network call happens while the caller's
    transaction holds a pooled connection.
    """
    # Every writer passes here, so no stored entry keeps a model's leaked
    # reasoning: recalled into a prompt, it makes Claude decline (#12356).
    content = strip_leading_reasoning(_sanitize_memory_text(content))
    if not content.strip():
        logger.warning("Memory entry for agent %s not stored: empty once leaked reasoning is removed", agent_id)
        return None
    # Normalised, not merely sanitised: every class check below is an exact
    # comparison, so a model-supplied ``"Tradecraft"`` would land as a class of
    # its own - no leak guard, no forced agent-level scope, no dedup, no cap,
    # and no age pruning either.
    memory_type = normalize_memory_type(memory_type)
    source = _sanitize_memory_text(source)
    subject_key = normalize_subject_key(subject_key)
    if memory_type == "consolidated":
        # The one rolling summary of a scope, rewritten in place: it describes
        # no single object and replaces nothing but itself.
        subject_key = supersedes = None

    if memory_type == TRADECRAFT_MEMORY_TYPE:
        # Enforced here rather than in the tool layer so NO code path can write
        # a leaky or user-scoped tradecraft entry: this class outlives the
        # run-end purge, and a target that slips through is recalled by every
        # later engagement.  Tradecraft is always agent-level — it is knowledge
        # about the platform, not about the operator who happened to trigger
        # the run that learned it.
        #
        # Authorization comes first, and for the same reason. The chat tool is
        # gated on the run context too, but that gate only covers the caller it
        # is built for; this function is the single write funnel, so checking it
        # only there would leave every other caller - an assignment path today,
        # a new tool tomorrow - able to publish an unscoped cross-user row from
        # outside any engagement.
        if not in_tradecraft_run_context():
            raise TradecraftNotAuthorizedError
        leak = tradecraft_leak_reason(content)
        if leak:
            raise TradecraftRejectedError(leak)
        skip = tradecraft_skip_fork_reason(content)
        if skip:
            raise TradecraftRejectedError(skip, message=tradecraft_skip_fork_message())
        if subject_key or supersedes:
            raise TradecraftRejectedError("a subject", message=_TRADECRAFT_SUBJECT_REFUSAL)
        user_id = None

    excluded = False
    if _precomputed_embedding is not None:
        embedding: list[float] | None = _precomputed_embedding
        embedding_model = _provider_model[1] if _provider_model else None
    else:
        # Generate embedding (may be None if the provider is unavailable).
        # Callers can pass _provider_model to skip repeated DB lookups.
        # The resolved model id is stamped on the row together with the vector
        # dimension so stale entries can be detected after a model change.
        try:
            embedding, embedding_model = await embed_text_with_model(
                db, content, _provider_model=_provider_model, refuse_excluded=True
            )
        except ProviderNotUsableError:
            # The embedding provider excludes the person this memory is for:
            # no vector BY DESIGN, so neither a retry (it would meet
            # the same exclusion) nor a warning about a failure.
            embedding, embedding_model, excluded = None, None, True
        if embedding is None and not excluded:
            # MEM-7: retry once — embedding failures are often transient (rate
            # limit, network blip). A row stored with a NULL embedding is
            # permanently invisible to semantic search, so one retry is cheap
            # insurance before accepting that outcome.  The retry must go
            # through ``embed_text_with_model`` too: a successful retry stamped
            # without ``embedding_model`` / ``embedding_dimensions`` provenance
            # would be excluded by ``_matching_dimensions_filter`` and stay
            # invisible to every dimension-filtered search until the next
            # re-embed pass.
            embedding, embedding_model = await embed_text_with_model(
                db, content, _provider_model=_provider_model
            )
    embedding_dimensions = len(embedding) if embedding is not None else None
    if embedding is None and excluded:
        logger.info(
            "Persisting memory without embedding for agent %s: the embedding "
            "provider excludes the person it is for",
            agent_id,
        )
    elif embedding is None:
        # Persist anyway (durable beats lost): the row is counted by
        # ``get_memory_health()`` as ``no_embedding_count`` so maintenance
        # can pick it up later.
        logger.warning(
            "Persisting memory without embedding for agent %s after retry — "
            "entry will not appear in semantic search results",
            agent_id,
        )

    parsed_user_id = safe_uuid(user_id)

    # For consolidated memories, upsert (one active consolidated per scope)
    if memory_type == "consolidated":
        existing = await db.execute(
            select(AgentMemoryEntry).where(
                AgentMemoryEntry.agent_id == uuid.UUID(agent_id),
                AgentMemoryEntry.user_id == parsed_user_id,
                AgentMemoryEntry.memory_type == "consolidated",
            )
        )
        entry = existing.scalar_one_or_none()
        if entry:
            entry.content = content
            entry.embedding = embedding
            entry.embedding_model = embedding_model
            entry.embedding_dimensions = embedding_dimensions
            entry.source = source
            await db.flush()
            await _announce_persisted(db, entry)
            logger.info("Updated consolidated memory for agent %s", agent_id)
            return entry

    if memory_type == TRADECRAFT_MEMORY_TYPE:
        # Serialize the whole dedup -> insert -> cap sequence for this agent.
        # Concurrent engagements share one orchestrator, so without this two
        # cycles can both see "no duplicate", both insert, and both evaluate the
        # cap without seeing the other's row - leaving duplicates and an
        # over-cap pool. Transaction-scoped, so it releases on commit/rollback
        # with no unlock path to forget.
        #
        # Taken here rather than at the top of the function: the embedding above
        # is an outbound HTTP call, and holding a lock (plus the pooled
        # connection the lock statement checks out) across it is the exact
        # pool-pinning hazard the ``_precomputed_embedding`` contract documents.
        # Everything the race can corrupt happens below this line.
        await _lock_tradecraft_pool(db, agent_id)

        # Re-validate against the perimeter as it stands NOW. The checks at the
        # top of this function ran before the embedding round-trip, and a scope
        # replacement can land during it: tool calls are concurrent, so the
        # target set this content was cleared against may no longer be the run's,
        # and the write would commit a lesson naming a host adopted mid-flight.
        # Repeating it is pure regex over one string, so the cheapest possible
        # way to make the suspension cover an in-flight write and not just the
        # ones that start during it.
        if not in_tradecraft_run_context():
            raise TradecraftNotAuthorizedError
        leak = tradecraft_leak_reason(content)
        if leak:
            raise TradecraftRejectedError(leak)
        skip = tradecraft_skip_fork_reason(content)
        if skip:
            raise TradecraftRejectedError(skip, message=tradecraft_skip_fork_message())

    target_id = safe_uuid(supersedes) if supersedes else None
    by_subject = memory_type == STATE_MEMORY_TYPE and subject_key is not None
    if by_subject:
        # After the embedding round-trip, like the tradecraft lock: no lock is
        # held across an outbound call.
        await _lock_subject(db, agent_id, parsed_user_id, subject_key)

    # ── Semantic deduplication for facts ──────────────────────────────────
    # ``skip_dedup`` callers have already deleted this entry's source facts
    # and must not lose the curated replacement to a near-duplicate match.
    if (
        memory_type in ("fact", TRADECRAFT_MEMORY_TYPE)
        and embedding is not None
        and not skip_dedup
        and supersedes is None
    ):
        is_dup = await _is_duplicate_fact(
            db,
            agent_id=agent_id,
            user_id=parsed_user_id,
            embedding=embedding,
            memory_type=memory_type,
        )
        if is_dup:
            logger.debug(
                "Skipping duplicate fact for agent %s (similarity >= %.2f)",
                agent_id,
                _DEDUP_SIMILARITY_THRESHOLD,
            )
            return None

    # Create new entry
    entry = AgentMemoryEntry(
        agent_id=uuid.UUID(agent_id),
        user_id=parsed_user_id,
        content=content,
        memory_type=memory_type,
        source=source,
        subject_key=subject_key,
        embedding=embedding,
        embedding_model=embedding_model,
        embedding_dimensions=embedding_dimensions,
    )
    db.add(entry)
    await db.flush()
    if target_id is not None or by_subject:
        replaced = await _supersede(db, entry, target_id=target_id, by_subject=by_subject)
        if superseded_out is not None:
            superseded_out.extend(replaced)
    if memory_type == TRADECRAFT_MEMORY_TYPE:
        # At write time rather than in the hourly maintenance pass: tradecraft
        # is age-pruning exempt, so nothing else would ever bound it, and the
        # prompt cost of an overflow is paid on the very next run.
        await _enforce_tradecraft_cap(db, agent_id)
    await _announce_persisted(db, entry)
    logger.info("Persisted %s memory for agent %s", memory_type, agent_id)
    return entry


# Strong references to in-flight platform-hook deliveries. asyncio only holds
# weak references to tasks, so without this a delivery scheduled from the
# after-commit hook could be garbage-collected mid-flight — the same reason
# ``EventBus`` keeps its own set.
_hook_delivery_tasks: set[asyncio.Task] = set()


# Where a session's undelivered hook payloads queue up until it commits.
_HOOK_QUEUE_ATTR = "_memory_persisted_hook_queue"


def _schedule_hook_delivery(db: AsyncSession, payload: dict) -> None:
    """Publish ``memory.persisted`` to the platform hooks AFTER the commit.

    The hook bus leaves the database entirely — a listener is an outbound HTTP
    POST to a third party — so it is the one half of the announcement a
    rollback cannot take back. Emitting inline would let a caller that persists
    several memories in one transaction and then fails tell an external system
    about writes that never landed, with no way to retract them. Fact
    extraction, reconciliation and consolidation all persist in a loop inside
    one transaction, so this is a live path, not a theoretical one.

    Deferring it is less obvious than it looks, because ``after_commit`` fires
    for a **SAVEPOINT release too** — including the one this module's own flow
    emission opens, which would deliver the hook microseconds after the write
    and defeat the whole point. Measured against PostgreSQL, the session's
    nesting state separates the two cleanly in both directions:

    =======================  ==============  ================  ==========
    what happened            after_commit    after_rollback    in_nested
    =======================  ==============  ================  ==========
    SAVEPOINT released       fires           --                True
    SAVEPOINT rolled back    --              fires             True
    outer commit             fires           --                False
    outer rollback           --              fires             False
    =======================  ==============  ================  ==========

    So both listeners ignore anything raised while a SAVEPOINT is in progress.
    That also gives the failed-flow-emission case the right answer for free:
    the savepoint rolls back, the memory row survives, and the hook still fires
    on the eventual commit. ``after_soft_rollback`` is deliberately NOT used —
    it reports ``in_nested`` as False even for a savepoint failure, so it
    cannot tell the two apart.

    One listener pair per session, with the payloads queued on the session:
    registering per write would make a loop caller's savepoints O(n²) in
    listeners. The queue is cleared on an outer rollback, so a later commit on
    a reused session cannot deliver writes that were discarded.

    The listeners run in the commit's greenlet, on the event loop's own thread,
    so scheduling the coroutine there is safe — and they swallow everything,
    because raising inside ``after_commit`` would turn a hook problem into a
    failed commit.
    """
    session = db.sync_session
    queue: list[dict] | None = getattr(session, _HOOK_QUEUE_ATTR, None)
    if queue is None:
        queue = []
        setattr(session, _HOOK_QUEUE_ATTR, queue)
        loop = asyncio.get_running_loop()

        def _on_commit(sess: Any) -> None:
            try:
                if sess.in_nested_transaction():
                    return  # a SAVEPOINT release, not the durable commit
                pending, queue[:] = list(queue), []
                if not pending:
                    return
                from app.services.event_bus import MEMORY_PERSISTED, event_bus

                for item in pending:
                    task = loop.create_task(
                        event_bus.emit(MEMORY_PERSISTED, item)
                    )
                    _hook_delivery_tasks.add(task)
                    task.add_done_callback(_hook_delivery_tasks.discard)
            except Exception:  # pragma: no cover — belt for the commit path
                logger.debug(
                    "Memory hook-bus delivery could not be scheduled",
                    exc_info=True,
                )

        def _on_rollback(sess: Any) -> None:
            # Outer rollback only: a savepoint rollback leaves the memory rows
            # in place, so their deliveries stay queued for the real commit.
            with contextlib.suppress(Exception):
                if not sess.in_nested_transaction():
                    queue.clear()

        sa_event.listen(session, "after_commit", _on_commit)
        sa_event.listen(session, "after_rollback", _on_rollback)

    queue.append(payload)


async def _announce_persisted(db: AsyncSession, entry: AgentMemoryEntry) -> None:
    """Publish a durable write to the platform hooks and the flow event bus.

    Both exits of :func:`persist_memory` come through here — the insert and the
    consolidated upsert — because a rewritten operational block is exactly the
    state change a listener cares about, not a lesser event than a first write.

    Entirely fail-open, and in two independent halves, each tied to the
    transaction in the way its audience needs:

    * the flow emission runs in a SAVEPOINT, so a bad event declaration or a
      dispatch error rolls back to here and leaves the memory write intact —
      and the event row it writes commits or rolls back WITH the memory;
    * the platform-hook delivery is deferred to after the commit, because it
      is the half that escapes the database and cannot be retracted.

    Recording what an agent learned must never depend on anyone listening.
    """
    with contextlib.suppress(Exception):
        from app.services.memory_flow_events import memory_event_payload

        # The payload is snapshotted NOW: after the commit the instance may be
        # expired, and refreshing it from an after-commit hook would emit a
        # lazy load on a session the caller has already finished with.
        _schedule_hook_delivery(db, memory_event_payload(entry))

    try:
        from app.services.memory_flow_events import emit_memory_persisted

        async with db.begin_nested():
            await emit_memory_persisted(db, entry)
    except Exception:
        logger.debug(
            "Memory flow-event emission failed for entry %s",
            entry.id,
            exc_info=True,
        )


async def _lock_tradecraft_pool(db: AsyncSession, agent_id: str) -> None:
    """Take a transaction-scoped advisory lock over one agent's tradecraft pool.

    The key is derived from the agent UUID in Python rather than with PostgreSQL's
    ``hashtext`` so it does not depend on an undocumented internal whose hashing
    could change between majors.  Best-effort: a backend that does not provide
    advisory locks must not make the write fail outright - the pool degrades to
    the pre-existing unserialized behaviour (possible duplicate, transient
    over-cap), which the next write reconciles.
    """
    try:
        # Positive and inside int8 so the value is a valid advisory-lock key.
        key = uuid.UUID(agent_id).int % (2**62)
    except (ValueError, AttributeError, TypeError):  # pragma: no cover
        return
    try:
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    except Exception:  # pragma: no cover - non-PostgreSQL / stub sessions
        logger.debug(
            "Advisory lock unavailable for tradecraft pool of agent %s", agent_id
        )


async def _enforce_tradecraft_cap(db: AsyncSession, agent_id: str) -> int:
    """Evict the oldest tradecraft entries beyond ``_MAX_TRADECRAFT_PER_AGENT``.

    Tradecraft is a single agent-level pool (writes are forced to
    ``user_id IS NULL``), so unlike ``enforce_memory_cap`` there is no
    per-user scope to keep separate.  Returns the number evicted.
    """
    aid = uuid.UUID(agent_id)
    keep_ids = (
        select(AgentMemoryEntry.id)
        .where(
            AgentMemoryEntry.agent_id == aid,
            AgentMemoryEntry.memory_type == TRADECRAFT_MEMORY_TYPE,
        )
        # ``id`` breaks created_at ties so the kept set is deterministic - two
        # lessons written in the same transaction share a timestamp.
        .order_by(AgentMemoryEntry.created_at.desc(), AgentMemoryEntry.id.desc())
        .limit(_MAX_TRADECRAFT_PER_AGENT)
        .scalar_subquery()
    )
    result = await db.execute(
        delete(AgentMemoryEntry).where(
            AgentMemoryEntry.agent_id == aid,
            AgentMemoryEntry.memory_type == TRADECRAFT_MEMORY_TYPE,
            AgentMemoryEntry.id.notin_(keep_ids),
        )
    )
    evicted = int(getattr(result, "rowcount", 0) or 0)
    if evicted:
        await db.flush()
        logger.info(
            "Evicted %d tradecraft entries over the %d cap for agent %s",
            evicted,
            _MAX_TRADECRAFT_PER_AGENT,
            agent_id,
        )
    return evicted


def _matching_dimensions_filter(query_embedding: list[float]):
    """SQL filter restricting cosine searches to vectors of the query's size.

    pgvector's ``<=>`` operator errors out (or returns garbage) when the
    two vectors have different dimensions — which happens after an admin
    changes the platform embedding model/dimensions while old entries are
    still stamped with the previous model.  Rows whose
    ``embedding_dimensions`` is NULL (legacy/unknown provenance) are
    treated as non-matching; the memory maintenance re-embed task picks
    them up and stamps them with the current model.
    """
    return AgentMemoryEntry.embedding_dimensions == len(query_embedding)


async def _is_duplicate_fact(
    db: AsyncSession,
    *,
    agent_id: str,
    user_id: uuid.UUID | None,
    embedding: list[float],
    memory_type: str = "fact",
) -> bool:
    """Return True if a semantically similar entry already exists.

    Uses pgvector cosine distance to check for duplicates above the
    ``_DEDUP_SIMILARITY_THRESHOLD``.  *memory_type* scopes the comparison to
    one class so a tradecraft lesson is only ever deduplicated against other
    tradecraft, never against a run's facts.  A superseded entry is no
    duplicate: a fact that holds again must be storable again.
    """
    parsed_agent_id = uuid.UUID(agent_id)
    distance = AgentMemoryEntry.embedding.cosine_distance(embedding)
    filters = [
        AgentMemoryEntry.agent_id == parsed_agent_id,
        AgentMemoryEntry.memory_type == memory_type,
        AgentMemoryEntry.superseded_at.is_(None),
        AgentMemoryEntry.embedding.is_not(None),
        _matching_dimensions_filter(embedding),
        distance < (1 - _DEDUP_SIMILARITY_THRESHOLD),
    ]
    if user_id:
        filters.append(
            or_(AgentMemoryEntry.user_id.is_(None), AgentMemoryEntry.user_id == user_id)
        )
    else:
        filters.append(AgentMemoryEntry.user_id.is_(None))

    result = await db.execute(
        select(AgentMemoryEntry.id).where(*filters).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def search_agent_memories(
    db: AsyncSession,
    *,
    agent_id: str,
    query: str,
    user_id: str | None = None,
    limit: int = 5,
    _provider_model: tuple | None = None,
) -> list[dict]:
    """Semantic search over an agent's persistent memory entries.

    Uses pgvector cosine distance for similarity search.
    Searches both agent-level memories and user-scoped memories.

    When *_provider_model* is supplied as ``(provider, model)`` the embedding
    provider DB lookup is skipped, so the query embedding (an HTTP call) does
    not check out ``db``'s pooled connection — the connection is only acquired
    for the vector query below. Callers on the hot RAG path pass this to avoid
    pinning a connection across the embed round-trip (pool-exhaustion fix).

    Every row says when it was stored (``stored_at``), what it is about
    (``subject_key``) and whether it still holds for this reader
    (``superseded``, :func:`superseded_for_reader`; ``superseded_by`` when a
    successor was recorded). Superseded rows rank after every active one: the
    relevance floor is applied in SQL so the ordering cannot trade a relevant
    superseded row for an irrelevant active one.
    """
    # A search the embedding provider refuses to its person is refused, not
    # answered "nothing found": ``ProviderNotUsableError`` propagates.
    query_embedding = await embed_text(
        db, query, _provider_model=_provider_model, refuse_excluded=True
    )
    if not query_embedding:
        return []

    # When a user_id is provided, search both user-scoped and agent-level
    # entries.  When user_id is None, restrict to agent-level entries only
    # to prevent cross-user leakage.
    parsed_uid = safe_uuid(user_id)
    parsed_agent_id = uuid.UUID(agent_id)
    distance = AgentMemoryEntry.embedding.cosine_distance(query_embedding)
    score = (literal(1.0) - distance).label("score")
    superseded = superseded_for_reader(user_id)
    filters = [
        AgentMemoryEntry.agent_id == parsed_agent_id,
        AgentMemoryEntry.embedding.is_not(None),
        _matching_dimensions_filter(query_embedding),
        distance < (1 - _RELEVANCE_THRESHOLD),
    ]
    if parsed_uid:
        filters.append(
            or_(
                AgentMemoryEntry.user_id.is_(None),
                AgentMemoryEntry.user_id == parsed_uid,
            )
        )
    else:
        filters.append(AgentMemoryEntry.user_id.is_(None))

    result = await db.execute(
        select(
            AgentMemoryEntry.id,
            AgentMemoryEntry.content,
            AgentMemoryEntry.memory_type,
            AgentMemoryEntry.source,
            AgentMemoryEntry.user_id,
            AgentMemoryEntry.created_at,
            AgentMemoryEntry.subject_key,
            AgentMemoryEntry.superseded_by_id,
            superseded.label("superseded"),
            score,
        )
        .where(*filters)
        .order_by(superseded, distance)
        .limit(limit)
    )
    rows = result.all()

    results = [
        {
            "id": str(row.id),
            "content": row.content,
            "memory_type": row.memory_type,
            "source": row.source,
            "score": round(float(row.score), 4) if row.score else 0.0,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "stored_at": format_stored_at(row.created_at),
            "subject_key": row.subject_key,
            "superseded": bool(row.superseded),
            "superseded_by": str(row.superseded_by_id) if row.superseded_by_id else None,
            "scope": "user" if row.user_id else "agent",
        }
        for row in rows
        if row.score and row.score > _RELEVANCE_THRESHOLD
    ]

    return results


def format_stored_at(stored_at: datetime | None) -> str | None:
    """When an entry was stored, as an agent reads it: UTC, to the second."""
    if stored_at is None:
        return None
    if stored_at.tzinfo is None:
        stored_at = stored_at.replace(tzinfo=UTC)
    return stored_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class MemoryRetirement(NamedTuple):
    """What :func:`retire_memories` did."""

    #: The ids of the entries it retired.
    retired: list[str]
    #: Active entries matching the request the writer reads but may not
    #: retire: the agent-level pool, read by every user of the agent.
    left_shared: int


async def may_manage_shared_memory(db: AsyncSession, *, agent_id: str, user_id: str | None) -> bool:
    """Whether *user_id* may retire the agent-level pool of *agent_id*.

    The people who manage the agent (``helpers.can_manage_as``), the same as
    the memory screen's write routes. Everyone else retires only their own pool.
    """
    from app.api.v1.helpers import can_manage_as, get_user_group_ids
    from app.models.agent import Agent
    from app.models.user import User

    aid, uid = safe_uuid(agent_id), safe_uuid(user_id)
    if aid is None or uid is None:
        return False
    agent = await db.get(Agent, aid)
    if agent is None:
        return False
    role = (await db.execute(select(User.role).where(User.id == uid))).scalar_one_or_none()
    if role is None:
        return False
    return can_manage_as(uid, role == "admin", agent, await get_user_group_ids(db, uid))


async def retire_memories(
    db: AsyncSession,
    *,
    agent_id: str,
    user_id: str | None,
    entry_id: str | None = None,
    subject_key: str | None = None,
    include_shared: bool = False,
) -> MemoryRetirement:
    """Retire the writer's active entries that no longer hold.

    The entry *entry_id* names, or every active entry on *subject_key*
    (both given: the entry, if it is on that subject), in the writer's own
    pool (``_written_by``), plus the agent-level pool when *include_shared*
    (the caller checked :func:`may_manage_shared_memory`). Another user's
    entries are never retired. A retired entry is kept and searchable,
    flagged superseded with no successor, and never reaches a prompt again.
    Retiring changes nothing a retirement already did. Flushes; the caller
    commits.
    """
    conditions = []
    if entry_id is not None:
        parsed_entry = safe_uuid(entry_id)
        if parsed_entry is None:
            return MemoryRetirement([], 0)
        conditions.append(AgentMemoryEntry.id == parsed_entry)
    subject_key = normalize_subject_key(subject_key)
    if subject_key is not None:
        conditions.append(AgentMemoryEntry.subject_key == subject_key)
    if not conditions:
        return MemoryRetirement([], 0)

    aid = uuid.UUID(agent_id)
    parsed_uid = safe_uuid(user_id)
    matching = (
        AgentMemoryEntry.agent_id == aid,
        AgentMemoryEntry.superseded_at.is_(None),
        *conditions,
    )
    pool = _readable_by(AgentMemoryEntry, parsed_uid) if include_shared else _written_by(AgentMemoryEntry, parsed_uid)
    result = await db.execute(
        update(AgentMemoryEntry)
        .where(*matching, pool)
        .values(superseded_at=datetime.now(UTC), superseded_by_id=None)
        .returning(AgentMemoryEntry.id)
        .execution_options(synchronize_session=False)
    )
    retired = [str(row_id) for row_id in result.scalars().all()]
    left_shared = 0
    if parsed_uid is not None and not include_shared:
        left_shared = (
            await db.execute(
                select(func.count())
                .select_from(AgentMemoryEntry)
                .where(*matching, AgentMemoryEntry.user_id.is_(None))
            )
        ).scalar() or 0
    if retired:
        await db.flush()
        logger.info("Retired %d memory entries of agent %s", len(retired), agent_id)
    return MemoryRetirement(retired, int(left_shared))


# ── Memory import ────────────────────────────────────────────────────
# One call recreates at most this many entries: the page cap of the list route,
# so what one GET returns one POST takes back.
MEMORY_IMPORT_MAX_ENTRIES = 200
# The widths of ``agent_memory_entries.memory_type`` and ``.source``.
_MEMORY_TYPE_MAX_CHARS = 30
_SOURCE_MAX_CHARS = 50
MEMORY_IMPORT_SOURCE = "import"


class MemoryImportError(ValueError):
    """An imported entry the store refuses; ``index`` is its place in the request."""

    def __init__(self, index: int, message: str) -> None:
        super().__init__(f"entries[{index}]: {message}")
        self.index = index


class MemoryImportEntry(NamedTuple):
    """One entry to recreate, as an export lists it."""

    content: str
    memory_type: str = "fact"
    subject_key: str | None = None
    source: str | None = None
    user_id: uuid.UUID | None = None
    created_at: datetime | None = None


def _import_created_at(value: datetime | None, now: datetime) -> datetime:
    """The stored date of an imported entry: its own, in UTC, never in the future."""
    if value is None:
        return now
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return min(value, now)


def prepare_import_entry(index: int, item: MemoryImportEntry) -> tuple[str, str, str | None]:
    """*item*'s stored content, memory type and subject; :class:`MemoryImportError` when refused."""
    content = _sanitize_memory_text(item.content or "").strip()
    if not content:
        raise MemoryImportError(index, "content is empty")
    memory_type = normalize_memory_type(item.memory_type or "fact")
    if not memory_type or len(memory_type) > _MEMORY_TYPE_MAX_CHARS:
        raise MemoryImportError(
            index, f"memory_type must be 1 to {_MEMORY_TYPE_MAX_CHARS} characters"
        )
    if memory_type == TRADECRAFT_MEMORY_TYPE:
        raise MemoryImportError(index, "tradecraft entries cannot be imported")
    subject_key = normalize_subject_key(item.subject_key)
    if memory_type == "consolidated":
        subject_key = None
    return content, memory_type, subject_key


async def import_memories(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    entries: list[MemoryImportEntry],
) -> list[AgentMemoryEntry]:
    """Recreate exported memory entries on *agent_id*: a migration or a restore.

    Each entry keeps its ``created_at``, ``memory_type``, ``subject_key``,
    ``source`` and user scope; the caller has checked who may write which
    scope. Entries are stored oldest first, so a ``state`` entry supersedes
    the earlier active one on its subject in its pool exactly as
    :func:`persist_memory` would have, and an imported state entry older than
    one the agent already holds lands superseded by it. No semantic dedup and
    no fact cap: the source curated them, and the maintenance task applies
    the cap later. Embeddings are left empty, the stale state
    :func:`reembed_stale_memories` fills, so no embedding call happens while
    the transaction holds a connection. Tradecraft is refused: it is
    only written from inside an engagement. Flushes; the caller commits.
    """
    now = datetime.now(UTC)
    prepared: list[tuple[datetime, int, MemoryImportEntry, str, str, str | None]] = []
    for index, item in enumerate(entries):
        content, memory_type, subject_key = prepare_import_entry(index, item)
        created_at = _import_created_at(item.created_at, now)
        prepared.append((created_at, index, item, content, memory_type, subject_key))

    prepared.sort(key=lambda row: (row[0], row[1]))
    created: list[AgentMemoryEntry] = []
    for created_at, _index, item, content, memory_type, subject_key in prepared:
        source = _sanitize_memory_text(item.source or MEMORY_IMPORT_SOURCE).strip()
        entry = AgentMemoryEntry(
            id=uuid.uuid4(),
            agent_id=agent_id,
            user_id=item.user_id,
            content=content,
            memory_type=memory_type,
            source=(source or MEMORY_IMPORT_SOURCE)[:_SOURCE_MAX_CHARS],
            subject_key=subject_key,
            created_at=created_at,
            updated_at=created_at,
        )
        by_subject = memory_type == STATE_MEMORY_TYPE and subject_key is not None
        if by_subject:
            await _lock_subject(db, str(agent_id), item.user_id, subject_key)
            newer_id = (
                await db.execute(
                    select(AgentMemoryEntry.id)
                    .where(
                        AgentMemoryEntry.agent_id == agent_id,
                        _written_by(AgentMemoryEntry, item.user_id),
                        AgentMemoryEntry.memory_type == STATE_MEMORY_TYPE,
                        AgentMemoryEntry.subject_key == subject_key,
                        AgentMemoryEntry.superseded_at.is_(None),
                        AgentMemoryEntry.created_at > created_at,
                    )
                    .order_by(AgentMemoryEntry.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if newer_id is not None:
                entry.superseded_at = now
                entry.superseded_by_id = newer_id
                by_subject = False
        db.add(entry)
        await db.flush()
        if by_subject:
            await _supersede(db, entry, target_id=None, by_subject=True)
        created.append(entry)
    if created:
        logger.info("Imported %d memory entries into agent %s", len(created), agent_id)
    return created


async def export_memories(
    db: AsyncSession, *, agent_id: uuid.UUID
) -> list[dict[str, str | None]]:
    """The agent's active entries, oldest first, in the shape :func:`import_memories` takes.

    Superseded entries stay behind: re-imported, a retired one would come back
    active. Tradecraft stays behind too, it cannot be imported. The caller
    checks the reader may see every scope.
    """
    rows = (
        await db.execute(
            select(AgentMemoryEntry)
            .where(
                AgentMemoryEntry.agent_id == agent_id,
                AgentMemoryEntry.superseded_at.is_(None),
                AgentMemoryEntry.memory_type != TRADECRAFT_MEMORY_TYPE,
            )
            .order_by(AgentMemoryEntry.created_at, AgentMemoryEntry.id)
        )
    ).scalars().all()
    return [
        {
            "content": row.content,
            "memory_type": row.memory_type,
            "subject_key": row.subject_key,
            "source": row.source,
            "user_id": str(row.user_id) if row.user_id else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


# ── Memory read rule and copy ────────────────────────────────────────


def memory_content_restricted(agent: Any, user: Any) -> bool:
    """Whether *user* sees only counts of *agent*'s memory, never its content.

    A company-managed agent accumulates memory from every user's
    conversations: its content is an administrator's.
    """
    return bool(agent.is_company_managed and user.role != "admin")


def memory_full_visibility(agent: Any, user: Any) -> bool:
    """Whether *user* reads every pool of *agent*'s memory, other users' included.

    The owner of the agent does, and an administrator on a company-managed
    agent. Anyone else reads the agent-level pool and their own entries
    (the runtime rule of :func:`search_agent_memories`).
    """
    if agent.user_id == user.id:
        return True
    return bool(user.is_admin and agent.is_company_managed)


class MemoryCopyRefusedError(PermissionError):
    """The caller may not read the content of the source's memory."""


MEMORY_COPY_REFUSED = (
    "The memory of a company-managed agent is an administrator's: "
    "it is not copied for anyone else."
)


async def copy_agent_memories(
    db: AsyncSession,
    *,
    source: Any,
    target_agent_id: uuid.UUID,
    user: Any,
) -> int:
    """Copy the persistent memory entries of *source* that *user* may read onto *target_agent_id*.

    Each copy keeps its content, ``memory_type``, ``subject_key``, ``source``,
    user scope, dates, embedding and superseded state; a ``superseded_by_id``
    points at the copy of the successor (``None`` when the successor is in a
    pool *user* does not read). The read rule is the memory API's
    (:func:`memory_content_restricted`, :func:`memory_full_visibility`):
    :class:`MemoryCopyRefusedError` when *user* sees counts only. Answers the
    number of entries copied. Flushes; the caller commits.
    """
    if memory_content_restricted(source, user):
        raise MemoryCopyRefusedError(MEMORY_COPY_REFUSED)
    query = select(AgentMemoryEntry).where(AgentMemoryEntry.agent_id == source.id)
    if not memory_full_visibility(source, user):
        query = query.where(_readable_by(AgentMemoryEntry, user.id))
    rows = (
        await db.execute(query.order_by(AgentMemoryEntry.created_at, AgentMemoryEntry.id))
    ).scalars().all()
    id_map = {row.id: uuid.uuid4() for row in rows}
    copies = [
        AgentMemoryEntry(
            id=id_map[row.id],
            agent_id=target_agent_id,
            user_id=row.user_id,
            content=row.content,
            memory_type=row.memory_type,
            source=row.source,
            subject_key=row.subject_key,
            superseded_at=row.superseded_at,
            superseded_by_id=id_map.get(row.superseded_by_id) if row.superseded_by_id else None,
            embedding=row.embedding,
            embedding_model=row.embedding_model,
            embedding_dimensions=row.embedding_dimensions,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]
    if copies:
        db.add_all(copies)
        await db.flush()
        logger.info(
            "Copied %d memory entries of agent %s into agent %s",
            len(copies), source.id, target_agent_id,
        )
    return len(copies)


async def extract_and_persist_facts(
    db: AsyncSession,
    llm: BaseLLMProvider,
    *,
    agent_id: str,
    messages_text: str,
    user_id: str | None = None,
    is_company_managed: bool = False,
    model: str | None = None,
) -> list[AgentMemoryEntry]:
    """Extract key facts from conversation and persist them to durable memory.

    Uses the LLM to extract durable facts, then persists each with an
    embedding.  Only relies on the standard 0.92-cosine dedup short-circuit
    to avoid persisting near-duplicates.

    .. note::
       Prefer :func:`reconcile_and_persist_facts` for context compaction —
       it additionally detects CONTRADICTIONS (facts in the
       [0.65, 0.92) similarity zone) and SUPERSEDES the older entry,
       which fixes the long-standing issue where a corrected user
       directive (e.g. "use subregion, not country") would silently
       coexist with the original stale fact.  This function is kept for
       backward compatibility with assignment-engine code paths that
       expect a list of created ``AgentMemoryEntry`` rows.

    When *is_company_managed* is ``True`` **and** no ``user_id`` is given
    (agent-level extraction, e.g. assignments), the prompt focuses on
    organisational knowledge.  When a ``user_id`` is present, facts are
    per-user scoped so personal preferences are safe to extract.
    """
    # The same prompts and the same guard as the reconciliation path: a list
    # cut at its budget never has its cut line persisted as a fact.
    facts = await _extract_candidate_facts(
        llm,
        messages_text=messages_text,
        is_company_managed=is_company_managed,
        has_user_scope=bool(user_id),
        model=model,
    )
    if not facts:
        return []

    try:
        # Resolve embedding provider once for all facts in this batch.
        # Failure is non-fatal — falls back to per-call default resolution
        # inside ``persist_memory`` / ``embed_text``.
        _pm: tuple | None = None
        try:
            from app.llm.router import get_embedding_provider

            _pm = await get_embedding_provider(db)
        except Exception as exc:
            logger.debug(
                "Embedding provider lookup failed in extract_and_persist_facts "
                "(falling back to default resolution): %s",
                exc,
                exc_info=True,
            )

        entries: list[AgentMemoryEntry] = []
        skipped = 0
        for fact in facts[:_MAX_FACTS_PER_COMPACTION]:
            if len(fact) < _MIN_FACT_LENGTH:
                continue
            entry = await persist_memory(
                db,
                agent_id=agent_id,
                content=fact,
                memory_type="fact",
                source="compaction",
                user_id=user_id,
                _provider_model=_pm,
            )
            if entry is not None:
                entries.append(entry)
            else:
                skipped += 1

        logger.info(
            "Extracted %d facts (%d deduplicated) from conversation for agent %s",
            len(entries),
            skipped,
            agent_id,
        )
        return entries

    except ContextOverflowError as e:
        logger.warning(
            "Skipping fact extraction for agent %s: conversation exceeds "
            "context window (%s tokens vs %s limit)",
            agent_id,
            e.estimated_tokens,
            e.context_window,
        )
        return []
    except Exception as e:
        logger.warning("Failed to extract facts for agent %s: %s", agent_id, e)
        return []


# ── Memory reconciliation (conflict-aware persistence) ────────────────────


async def _extract_candidate_facts(
    llm: BaseLLMProvider,
    *,
    messages_text: str,
    is_company_managed: bool,
    has_user_scope: bool,
    model: str | None = None,
) -> list[str]:
    """Ask the LLM to extract candidate facts from a transcript.

    Returns a deduplicated list of non-empty fact strings.  Empty list
    when the model says "NONE" or returns nothing parseable.
    """
    from app.llm.base import ChatMessage

    if is_company_managed and not has_user_scope:
        extraction_prompt = (
            "Extract key durable facts from this conversation that should be "
            "remembered long-term by a SHARED agent used by the entire organisation.\n\n"
            "Focus on:\n"
            "- Organisational knowledge, processes, and conventions\n"
            "- Product information and technical facts\n"
            "- Important decisions and their rationale\n"
            "- Factual corrections and clarifications\n"
            "- Action items and commitments\n\n"
            "Do NOT extract:\n"
            "- Personal user information (names, individual preferences, private details)\n"
            "- Individual communication styles or habits\n"
            "- The user's preferred language, locale, tone, or formality "
            "(these are cross-agent personal preferences tracked in the "
            "user's platform-wide profile, not the agent's shared memory)\n"
            "- User-specific context that wouldn't benefit the whole team\n\n"
            "Output each fact on a separate line, prefixed with '- '.\n"
            "Only output facts worth remembering for the whole team. If nothing "
            "is noteworthy, output 'NONE'."
        )
    else:
        # Per-user agent memory MUST still avoid cross-agent personal
        # preferences (preferred language, tone, communication style).
        # Those live in the user's platform-wide profile so they follow
        # the user across every agent.  Capturing them here makes them
        # sticky to a single agent and causes regressions like the agent
        # replying in French to a user who is now writing in English.
        extraction_prompt = (
            "Extract key durable facts from this conversation that should be "
            "remembered long-term for this user's interactions with THIS "
            "specific agent.\n\n"
            "Focus on:\n"
            "- Agent-domain decisions and their rationale\n"
            "- Important names, identifiers, dates, dashboards, filters, "
            "or references the user pinned for this agent\n"
            "- Action items and commitments scoped to this agent's work\n"
            "- Technical details, tools, or configurations discussed\n\n"
            "Do NOT extract:\n"
            "- The user's preferred language, locale, tone, formality, "
            "or communication style (these are cross-agent personal "
            "preferences tracked in the user's platform-wide profile, "
            "not in this agent's memory)\n"
            "- The user's name, role, team, or other identity details\n"
            "- Anything that would apply equally to every other agent "
            "the user interacts with\n\n"
            "When the user CORRECTS a previous decision, output the corrected "
            "version as a single self-contained statement (e.g. write 'Use "
            "subregion filter, not country' rather than two separate facts).\n\n"
            "Output each fact on a separate line, prefixed with '- '.\n"
            "Only output facts worth remembering. If nothing is noteworthy, "
            "output 'NONE'."
        )

    extraction_messages = [
        ChatMessage(role="system", content=extraction_prompt),
        ChatMessage(
            role="user",
            content=f"Conversation to extract facts from:\n\n{messages_text[:8000]}",
        ),
    ]
    chat_kwargs: dict = {
        "messages": extraction_messages,
        "model": model,
        "max_tokens": _EXTRACT_MAX_TOKENS,
        "temperature": 0.2,
        # A mechanical listing: on the models that think by default, thinking
        # would share the small budget with the list.
        "disable_thinking": True,
    }
    try:
        async with traced_generation(
            name="memory_fact_extract", messages=extraction_messages
        ) as _gen:
            _gen.response = await llm.chat(**chat_kwargs)
    except Exception as exc:
        logger.warning("Fact extraction LLM call failed: %s", exc)
        return []
    response = _gen.response

    if answer_was_cut(response):
        retry_budget = retry_output_budget(llm, model, _EXTRACT_MAX_TOKENS)
        logger.warning(
            "Fact extraction answer hit max_tokens=%d - retrying once with "
            "max_tokens=%d",
            _EXTRACT_MAX_TOKENS,
            retry_budget,
        )
        try:
            async with traced_generation(
                name="memory_fact_extract_retry", messages=extraction_messages
            ) as _gen:
                _gen.response = await llm.chat(
                    **{**chat_kwargs, "max_tokens": retry_budget}
                )
        except Exception as exc:
            logger.warning(
                "Fact extraction retry failed - keeping the complete lines of "
                "the first answer: %s",
                format_exception_message(exc),
            )
        else:
            response = _gen.response

    facts_text = strip_leading_reasoning(response.content or "")
    if not facts_text or facts_text.strip().upper() == "NONE":
        return []

    lines = facts_text.strip().split("\n")
    if answer_was_cut(response):
        # The budget cut the last line: a fragment, never a fact.
        lines = lines[:-1]

    facts: list[str] = []
    seen: set[str] = set()
    for line in lines:
        cleaned = line.strip().removeprefix("- ").strip()
        if not cleaned or cleaned.upper() == "NONE":
            continue
        if len(cleaned) < _MIN_FACT_LENGTH:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        facts.append(cleaned)
    return facts[:_MAX_FACTS_PER_COMPACTION]


async def _find_related_facts(
    db: AsyncSession,
    *,
    agent_id: str,
    user_id: uuid.UUID | None,
    embedding: list[float],
    limit: int = _RECONCILE_MAX_RELATED,
    min_similarity: float = _RECONCILE_RELATED_THRESHOLD,
    max_similarity: float = _DEDUP_SIMILARITY_THRESHOLD,
) -> list[dict]:
    """Find existing facts in the [min, max) similarity zone for *embedding*.

    The default range — ``[_RECONCILE_RELATED_THRESHOLD,
    _DEDUP_SIMILARITY_THRESHOLD)`` — is the "contradiction zone": close
    enough to potentially conflict but not exact duplicates.  Excluding
    near-duplicates (similarity ≥ 0.92) is deliberate: those are caught
    by the dedup short-circuit inside ``persist_memory`` and would
    otherwise waste tokens in the reconciliation prompt.

    Returns dicts with ``id``, ``content``, and ``score`` ordered by
    descending similarity.  Used by reconciliation to surface the facts
    a new candidate might contradict.

    Note on parameter naming: the pgvector ``<=>`` operator returns
    *cosine distance* (``1 - similarity``).  The two SQL bind params
    therefore invert the human-friendly bounds — ``min_distance`` is
    derived from ``max_similarity`` and vice versa.  Kept this way to
    keep the SQL idiomatic; callers reason in similarity space.
    """
    if not embedding:
        return []

    parsed_agent_id = uuid.UUID(agent_id)
    distance = AgentMemoryEntry.embedding.cosine_distance(embedding)
    score = (literal(1.0) - distance).label("score")
    filters = [
        AgentMemoryEntry.agent_id == parsed_agent_id,
        AgentMemoryEntry.memory_type == "fact",
        # A superseded fact no longer holds: nothing to reconcile against.
        AgentMemoryEntry.superseded_at.is_(None),
        AgentMemoryEntry.embedding.is_not(None),
        _matching_dimensions_filter(embedding),
        # ``>`` (strict) on min_distance excludes near-duplicates whose
        # similarity equals ``max_similarity`` — the dedup short-circuit
        # in ``persist_memory`` already covers that range with strict ``<``.
        distance > (1 - max_similarity),
        distance <= (1 - min_similarity),
    ]
    if user_id:
        filters.append(
            or_(AgentMemoryEntry.user_id.is_(None), AgentMemoryEntry.user_id == user_id)
        )
    else:
        filters.append(AgentMemoryEntry.user_id.is_(None))

    result = await db.execute(
        select(AgentMemoryEntry.id, AgentMemoryEntry.content, score)
        .where(*filters)
        .order_by(distance)
        .limit(limit)
    )
    return [
        {
            "id": str(row.id),
            "content": row.content,
            "score": round(float(row.score), 4) if row.score else 0.0,
        }
        for row in result.fetchall()
    ]


def _reconcile_decision_prompt() -> str:
    """System prompt for the reconciliation curator LLM call."""
    return (
        "You are a memory curator. Your job is to decide how new candidate "
        "facts should be reconciled against existing facts in an agent's "
        "durable memory.\n\n"
        "For each NEW fact, choose ONE action:\n"
        "- \"ADD\" — the fact is new information; persist it.\n"
        "- \"SKIP\" — the fact is already represented by an existing fact; "
        "do not persist a duplicate.\n"
        "- \"SUPERSEDE\" — the fact CONTRADICTS or REPLACES one or more "
        "existing facts (e.g. the user changed a preference, corrected a "
        "filter, swapped a data source). Delete the listed existing facts "
        "and persist the new one in their place.\n"
        "- \"MERGE\" — the fact refines an existing one without contradicting "
        "it; emit a merged statement and delete the listed existing facts.\n\n"
        "Rules:\n"
        "- Be conservative: when in doubt between ADD and SUPERSEDE, prefer "
        "SUPERSEDE only when the contradiction is unambiguous and the new "
        "fact replaces the old one in scope.\n"
        "- Each existing fact id may appear under SUPERSEDE/MERGE for at "
        "most one new fact in a single response.\n"
        "- Never invent fact ids — only reference the ids provided in the "
        "input.\n\n"
        "Output shape:\n"
        "{\n"
        '  "decisions": [\n'
        "    {\n"
        '      "new_fact": "...exact text from input...",\n'
        '      "action": "ADD" | "SKIP" | "SUPERSEDE" | "MERGE",\n'
        '      "superseded_ids": ["...uuid..."],  // only for SUPERSEDE/MERGE\n'
        '      "merged_content": "..."             // only for MERGE\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Your ENTIRE reply must be that single JSON object — starting with "
        "'{' and ending with '}'. No prose, no code fences."
    )


def _reconcile_retry_prompt() -> str:
    """Terser, stricter JSON-only reprompt for the curator retry (MEM-5).

    Used when the first curator call fails or returns unparseable output —
    a minimal contract maximises the chance of valid JSON on the retry.
    """
    return (
        "Reconcile each candidate fact against its related existing facts. "
        "For each candidate output one decision with action ADD, SKIP, "
        "SUPERSEDE, or MERGE. Only reference existing fact ids given in the "
        "input. Return ONLY the JSON object below - no prose, no code "
        "fences, nothing before the '{' or after the '}':\n"
        '{"decisions": [{"new_fact": "EXACT_CANDIDATE_TEXT", '
        '"action": "ADD", "superseded_ids": [], "merged_content": ""}]}'
    )


# Strict JSON Schema for the reconciliation curator output. Passed through the
# provider-native ``structured_output`` kwarg (OpenAI response_format /
# Anthropic output_config.format / Gemini response_schema / Bedrock) so the
# model returns well-formed JSON instead of prose. Newer models
# (``claude-sonnet-5``) ignored the prompt-only "respond with ONLY JSON"
# instruction and emitted prose, which forced the ADD-all fallback and let
# contradicting facts coexist. Providers without native enforcement still fall
# back to prompt-only + ``_parse_reconcile_response``.
_RECONCILE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "new_fact": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["ADD", "SKIP", "SUPERSEDE", "MERGE"],
                    },
                    "superseded_ids": {"type": "array", "items": {"type": "string"}},
                    "merged_content": {"type": "string"},
                },
                "required": ["new_fact", "action"],
            },
        },
    },
    "required": ["decisions"],
}


def _parse_reconcile_response(raw: str) -> list[dict]:
    """Parse the reconciliation LLM response into a list of decisions.

    Delegates the messy-output handling to the shared Schema-Aligned
    Parsing helpers (``structured_output``): ``extract_json_text`` pulls
    the JSON payload out of markdown code fences and surrounding prose
    (preamble sentences, trailing sign-offs), and ``repair_json_text``
    fixes common syntax slips (single quotes, trailing commas, unquoted
    keys).  The previous fence-strip + first-``{``/last-``}`` slice
    missed exactly these production failure modes and forced the
    ADD-all degradation far more often than necessary.

    A bare top-level decisions array (wrapper object omitted) is
    tolerated — the caller's candidate-text validation drops any
    decision that does not reference a real candidate fact, so an
    incidental array cannot be misapplied.

    Every degraded outcome (pure prose, unrepairable JSON, wrong shape)
    logs a WARNING so the ADD-all fallback stays observable in
    production, matching the log contract.
    """
    from app.services.structured_output import extract_json_text, repair_json_text

    text_val = (raw or "").strip()
    if not text_val:
        return []

    candidate = extract_json_text(text_val)
    # Short-circuit pure prose: without a JSON container there is nothing
    # to repair — json-repair would coerce free text into a bare JSON
    # string, which would then fail the shape check below without ever
    # having been JSON in the first place.
    if not candidate or candidate[0] not in "{[":
        logger.warning("Reconciliation returned non-JSON output; defaulting to ADD-all")
        return []

    try:
        obj = json.loads(candidate)
    except Exception:
        try:
            obj = json.loads(repair_json_text(candidate))
        except Exception:
            logger.warning(
                "Reconciliation returned non-JSON output; defaulting to ADD-all"
            )
            return []

    decisions = obj.get("decisions") if isinstance(obj, dict) else obj
    if not isinstance(decisions, list):
        logger.warning(
            "Reconciliation returned JSON with an unexpected shape (%s); "
            "defaulting to ADD-all",
            type(obj).__name__,
        )
        return []
    return [d for d in decisions if isinstance(d, dict)]


async def reconcile_and_persist_facts(
    db: AsyncSession,
    llm: BaseLLMProvider,
    *,
    agent_id: str,
    messages_text: str,
    user_id: str | None = None,
    is_company_managed: bool = False,
    model: str | None = None,
) -> dict:
    """Extract facts and reconcile them against existing durable memory.

    This is the conflict-aware replacement for
    :func:`extract_and_persist_facts`: candidate facts that CONTRADICT
    existing memory entries supersede (delete) the old entries instead
    of silently coexisting with them.  This addresses the long-standing
    issue where an agent appeared to "remember" a user correction while
    *also* still acting on the original (now-stale) directive.

    Workflow:
      1. Extract candidate facts from *messages_text* via the LLM.
      2. For each candidate, embed it and look up existing facts in the
         "contradiction zone" (cosine similarity ∈ [0.65, 0.92)) — close
         enough to potentially conflict but not exact duplicates.
      3. Ask the LLM curator to decide ADD / SKIP / SUPERSEDE / MERGE
         for every candidate, given the related existing facts.
      4. Apply the decisions: delete superseded ids, persist new /
         merged content.  Plain ADDs still benefit from the standard
         dedup check inside :func:`persist_memory`.

    Returns a summary dict::

        {"added": int, "superseded": int, "skipped": int,
         "merged": int, "deleted": int, "curator_degraded": bool}

    ``superseded`` / ``merged`` count the *new* facts whose decision was
    SUPERSEDE / MERGE.  ``deleted`` counts the actual number of *old*
    fact rows that were removed (one new fact can supersede several
    existing ones, in which case ``deleted`` > ``superseded``).

    Falls back to plain :func:`persist_memory` for every candidate if
    the curator call fails — the worst case is the previous behaviour,
    never a worse outcome.  The curator call is retried once with a
    terser JSON-only reprompt before falling back; when the fallback is
    taken anyway, ``curator_degraded`` is ``True`` so callers can
    observe that reconciliation was skipped (contradicting facts may
    now coexist until the next maintenance pass).

    **Connection-hold contract:** the session's pooled
    connection is committed-released before every LLM / embedding
    round-trip (extraction happens before the first DB touch; the
    session is committed after the provider lookup and again after the
    contradiction-zone queries, before the curator call).  The apply
    phase runs in one final short transaction that this function
    commits itself — SUPERSEDE deletes and their replacement inserts
    land atomically.  Callers must therefore NOT wrap this function in
    an outer ``db.begin()`` transaction.
    """
    summary: dict = {
        "added": 0,
        "superseded": 0,
        "skipped": 0,
        "merged": 0,
        "deleted": 0,
        "curator_degraded": False,
    }

    parsed_user_id = safe_uuid(user_id)
    facts = await _extract_candidate_facts(
        llm,
        messages_text=messages_text,
        is_company_managed=is_company_managed,
        has_user_scope=parsed_user_id is not None,
        model=model,
    )
    if not facts:
        return summary

    # Resolve embedding provider once for the whole batch.  Failure to
    # resolve is non-fatal — embeddings are then computed lazily by the
    # default ``embed_text`` path (or skipped if no provider is configured),
    # so we keep ``_pm = None`` and log for visibility.
    _pm: tuple | None = None
    try:
        from app.llm.router import get_embedding_provider

        _pm = await get_embedding_provider(db)
    except Exception as exc:
        logger.debug(
            "Embedding provider lookup failed in reconcile_and_persist_facts "
            "(falling back to default resolution): %s",
            exc,
            exc_info=True,
        )

    # Release the pooled connection the provider lookup just checked out
    # before the embedding round-trips below — with ``_pm`` resolved the
    # gather never touches the session, so holding a connection across N
    # embedding HTTP calls would pin it for no reason.
    if db.in_transaction():
        await db.commit()

    # Embed every candidate. When ``_pm`` resolved successfully the
    # embedding calls bypass ``get_embedding_provider(db)`` entirely
    # and only hit the provider HTTP client, so they're safe to gather
    # concurrently — turning the previous O(n × embedding_latency)
    # sequential loop into a single round-trip dominated the memory
    # auto-flush cost for agents extracting 5–10 facts at a time.
    #
    # However when the up-front provider lookup FAILED (``_pm is None``)
    # each ``embed_text`` call would internally call
    # ``get_embedding_provider(db)`` again, which issues ``db.execute(...)``
    # on the shared ``AsyncSession``. SQLAlchemy's async session is not
    # safe for concurrent operations — ``asyncio.gather`` over N such
    # calls raises ``InvalidRequestError: this session is provisioning
    # a new connection; concurrent operations are not permitted``. Fall
    # back to a sequential per-fact loop in that case so each retry of
    # the failed provider lookup happens serially against the session.
    import asyncio as _aio

    if _pm is not None:
        embeddings = await _aio.gather(
            *[embed_text(db, fact, _provider_model=_pm) for fact in facts]
        )
    else:
        embeddings = []
        for fact in facts:
            embeddings.append(await embed_text(db, fact))

    # Related-fact lookups MUST stay sequential — they all share the
    # same AsyncSession and SQLAlchemy forbids concurrent operations
    # on one session ("session is provisioning a new connection").
    related_by_fact: list[list[dict]] = []
    for emb in embeddings:
        if emb is None:
            related_by_fact.append([])
            continue
        related_by_fact.append(
            await _find_related_facts(
                db,
                agent_id=agent_id,
                user_id=parsed_user_id,
                embedding=emb,
            )
        )

    # The contradiction-zone queries above re-opened a transaction; commit
    # to hand the connection back before the (multi-second, possibly
    # retried) curator LLM call below.
    if db.in_transaction():
        await db.commit()

    # Only candidates with at least one related existing fact need the
    # curator LLM call — sending "(none)" candidates wastes tokens and
    # increases the risk of the LLM hallucinating cross-references.  The
    # "related-free" candidates go straight to ``persist_memory`` (plain
    # ADD path) below.
    curator_candidate_idxs = [
        i for i, rel in enumerate(related_by_fact) if rel
    ]
    decisions: list[dict] = []
    if curator_candidate_idxs:
        from app.llm.base import ChatMessage

        related_blocks: list[str] = []
        for i in curator_candidate_idxs:
            fact = facts[i]
            related = related_by_fact[i]
            related_blocks.append(
                "Candidate: " + fact + "\nRelated existing facts:\n"
                + "\n".join(
                    f"  - id={r['id']} score={r['score']}: {strip_leading_reasoning(r['content'])}"
                    for r in related
                )
            )

        user_content = (
            "Reconcile the following candidate facts:\n\n"
            + "\n\n".join(related_blocks)
        )
        # An attempt only counts as successful when at least one decision
        # references a real candidate: ``_parse_reconcile_response`` validates
        # JSON shape only, so a degenerate-but-parseable response (e.g.
        # ``{"decisions": [{}]}`` or hallucinated ``new_fact`` texts) would
        # otherwise stop the retry while every candidate silently falls
        # through to plain ADD with ``curator_degraded=False``.
        candidate_texts = {
            facts[i].strip().lower() for i in curator_candidate_idxs
        }
        # First attempt uses the full curator prompt; the retry uses a terser,
        # stricter JSON-only reprompt (models that drifted into prose on the
        # first call usually comply when re-asked with a minimal contract).
        prompt_attempts = [
            (_reconcile_decision_prompt(), "memory_reconcile"),
            (_reconcile_retry_prompt(), "memory_reconcile_retry"),
        ]
        # Steer the curator into well-formed JSON via provider-native structured
        # output when the provider/model supports it (falls back to prompt-only
        # + the tolerant parser otherwise). Built once and reused across both
        # attempts.
        from app.services.structured_output import build_native_structured_output

        structured_cfg = build_native_structured_output(llm, _RECONCILE_SCHEMA)
        max_tokens = _RECONCILE_MAX_TOKENS
        for system_prompt, trace_name in prompt_attempts:
            reconcile_messages = [
                ChatMessage(role="system", content=system_prompt),
                ChatMessage(role="user", content=user_content),
            ]
            try:
                async with traced_generation(
                    name=trace_name, messages=reconcile_messages
                ) as _gen:
                    _chat_kw: dict = {
                        "messages": reconcile_messages,
                        "model": model,
                        "max_tokens": max_tokens,
                        "temperature": 0.1,
                        # Mechanical JSON classification — no reasoning needed.
                        # Sonnet 5 / Opus 4.7+ run adaptive thinking on by
                        # default and it shares the max_tokens budget, so
                        # disable it (the Anthropic provider only honours this
                        # for models that support it; others ignore the kwarg).
                        "disable_thinking": True,
                    }
                    if structured_cfg is not None:
                        _chat_kw["structured_output"] = structured_cfg
                    try:
                        _gen.response = await llm.chat(**_chat_kw)
                    except TypeError as _te:
                        # A provider without ``**kwargs`` may reject an optional
                        # hint (``structured_output`` / ``disable_thinking``) by
                        # name. If the TypeError names one we passed, strip them
                        # all and retry once (mirrors the compaction guard). Any
                        # other TypeError propagates to the outer handler below.
                        _hints = ("structured_output", "disable_thinking")
                        _msg = str(_te)
                        if not any(h in _chat_kw and h in _msg for h in _hints):
                            raise
                        for _hint in _hints:
                            _chat_kw.pop(_hint, None)
                        _gen.response = await llm.chat(**_chat_kw)
                if answer_was_cut(_gen.response):
                    # Never applied: json-repair closes a cut answer into
                    # decisions that look complete - a MERGE cut inside
                    # ``merged_content`` would delete the superseded facts
                    # for the bare candidate.
                    logger.warning(
                        "Reconciliation LLM call (%s) for agent %s was cut at "
                        "max_tokens=%d - ignoring its decisions",
                        trace_name,
                        agent_id,
                        max_tokens,
                    )
                    parsed = []
                    max_tokens = retry_output_budget(llm, model, max_tokens)
                else:
                    parsed = _parse_reconcile_response(_gen.response.content or "")
            except Exception as exc:
                # exc_info: curator failures are retried and may be
                # intermittent — the stack trace is what distinguishes a
                # provider error from a parse/tracing error after the fact.
                logger.warning(
                    "Reconciliation LLM call (%s) failed for agent %s: %s",
                    trace_name,
                    agent_id,
                    exc,
                    exc_info=True,
                )
                parsed = []
            decisions = [
                d
                for d in parsed
                if (d.get("new_fact") or "").strip().lower() in candidate_texts
            ]
            if parsed and not decisions:
                logger.warning(
                    "Reconciliation LLM call (%s) for agent %s returned %d "
                    "decision(s) but none referenced a candidate fact — "
                    "treating the attempt as failed",
                    trace_name,
                    agent_id,
                    len(parsed),
                )
            if decisions:
                break
        if not decisions:
            # MEM-5: both curator attempts failed (call error or unparseable
            # output). Every candidate falls through to plain ADD below —
            # never worse than legacy behaviour, but contradicting facts in
            # the [0.65, 0.92) zone will coexist until the next pass. Flag
            # the degradation so callers can observe it.
            summary["curator_degraded"] = True
            logger.warning(
                "Reconciliation curator failed twice for agent %s — "
                "falling back to plain persistence for %d candidate fact(s); "
                "contradicting facts may coexist",
                agent_id,
                len(curator_candidate_idxs),
            )

    # Index decisions by the candidate text for quick lookup.
    decisions_by_fact: dict[str, dict] = {}
    for d in decisions:
        fact_text = (d.get("new_fact") or "").strip()
        if fact_text:
            decisions_by_fact[fact_text.lower()] = d

    # Pre-embed MERGE replacement texts BEFORE the apply phase below so no
    # embedding HTTP round-trip runs while the apply transaction holds a
    # pooled connection.  Candidate facts reuse the embeddings
    # already computed for the contradiction-zone lookup.  With ``_pm``
    # resolved, ``embed_text`` never touches the session.
    merged_embeddings: dict[str, list[float] | None] = {}
    if _pm is not None:
        for d in decisions_by_fact.values():
            if (d.get("action") or "").strip().upper() == "MERGE":
                m_text = strip_leading_reasoning(d.get("merged_content") or "").strip()
                if m_text and m_text not in merged_embeddings:
                    merged_embeddings[m_text] = await embed_text(
                        db, m_text, _provider_model=_pm
                    )

    # Track existing-fact ids already deleted by an earlier candidate.
    # Without this, the LLM occasionally maps the *same* existing id to
    # two different new candidates — both would call DELETE on it and
    # then INSERT a new fact, producing two replacements for one old
    # entry (the opposite of dedup). comment thread.
    consumed_superseded_ids: set[str] = set()

    # Apply each candidate's decision.  Plain ADD (or no decision) falls
    # through to ``persist_memory`` which still applies dedup.
    for candidate_idx, fact in enumerate(facts):
        decision = decisions_by_fact.get(fact.strip().lower())
        action = (decision or {}).get("action", "ADD").upper() if decision else "ADD"
        superseded_ids_raw = (decision or {}).get("superseded_ids") or []
        # Stripped where read: an empty one falls back to ``fact`` before the
        # superseded facts are deleted, and the embedding above matches it.
        merged_content = strip_leading_reasoning((decision or {}).get("merged_content") or "")

        # Validate superseded ids: must be UUIDs that actually appear in
        # the related facts for this candidate and must not have been
        # consumed by an earlier candidate in this batch.  Drops both
        # hallucinated ids and cross-candidate reuse.
        valid_related_ids = {r["id"] for r in related_by_fact[candidate_idx]}
        superseded_uuids: list[uuid.UUID] = []
        for sid in superseded_ids_raw:
            if not isinstance(sid, str) or sid not in valid_related_ids:
                continue
            if sid in consumed_superseded_ids:
                logger.debug(
                    "Reconcile %s for agent %s: ignoring superseded_id %s "
                    "(already consumed by an earlier candidate)",
                    action,
                    agent_id,
                    sid,
                )
                continue
            parsed = safe_uuid(sid)
            if parsed:
                superseded_uuids.append(parsed)
                consumed_superseded_ids.add(sid)

        if action == "SKIP":
            summary["skipped"] += 1
            logger.debug("Reconcile SKIP for agent %s: %s", agent_id, fact[:120])
            continue

        # MEM-6: a SUPERSEDE/MERGE decision whose superseded ids ALL failed
        # validation (hallucinated, cross-candidate reuse, malformed) would
        # otherwise insert the new fact while silently leaving the old
        # contradicting facts in place — and count it as superseded/merged.
        # Make the degradation explicit: downgrade to ADD (persist_memory
        # dedup still applies) and log it. When SOME ids were valid we
        # proceed as before with the valid subset.
        if action in ("SUPERSEDE", "MERGE") and not superseded_uuids:
            logger.warning(
                "Reconcile %s for agent %s had no valid superseded ids "
                "(%d proposed, all invalid or already consumed) — "
                "downgrading to ADD; old facts retained: '%s'",
                action,
                agent_id,
                len(superseded_ids_raw),
                fact[:120],
            )
            action = "ADD"

        deleted_now = 0
        if action in ("SUPERSEDE", "MERGE") and superseded_uuids:
            result = await db.execute(
                delete(AgentMemoryEntry).where(
                    AgentMemoryEntry.id.in_(superseded_uuids)
                )
            )
            await db.flush()
            # Trust the DELETE's ACTUAL affected-row count, not the number of
            # validated ids: a concurrent prune / cap eviction can remove some
            # (or all) of these rows between ``_find_related_facts`` and this
            # DELETE.  Only rows we truly deleted may enable the dedup bypass
            # below and increment ``summary["deleted"]`` - otherwise a 0-row
            # delete would persist an unnecessary near-duplicate (the
            # predecessors it was meant to replace are already gone) and
            # over-report the count.  ``rowcount`` is -1 (or, defensively,
            # unavailable) only when the driver cannot report it (asyncpg
            # always can) - fall back to the validated-id count in that case.
            rowcount = getattr(result, "rowcount", None)
            deleted_now = (
                rowcount
                if isinstance(rowcount, int) and rowcount >= 0
                else len(superseded_uuids)
            )
            if deleted_now:
                logger.info(
                    "Reconcile %s for agent %s: deleted %d old fact(s) — '%s'",
                    action,
                    agent_id,
                    deleted_now,
                    fact[:120],
                )

        content_to_persist = (
            merged_content.strip() if action == "MERGE" and merged_content.strip() else fact
        )

        # Reuse the embeddings computed before the apply phase so
        # ``persist_memory`` performs no embedding HTTP call while this
        # transaction holds a pooled connection.  Only applies
        # when the provider was resolved up-front (``_pm``) — otherwise
        # the model provenance stamp would be missing and the row would
        # be flagged stale by the next re-embed pass.
        _precomputed: list[float] | None = None
        if _pm is not None:
            if action == "MERGE" and content_to_persist in merged_embeddings:
                _precomputed = merged_embeddings[content_to_persist]
            else:
                _precomputed = embeddings[candidate_idx]

        # When this candidate actually deleted predecessors (SUPERSEDE /
        # MERGE), the curated replacement MUST land — bypass dedup so a
        # chance resemblance to a THIRD (non-superseded) fact can't drop
        # it after we've already removed the facts it was meant to
        # replace.  Plain ADDs (no deletion) keep dedup to avoid
        # persisting redundant near-duplicates.
        entry = await persist_memory(
            db,
            agent_id=agent_id,
            content=content_to_persist,
            memory_type="fact",
            source="compaction" if action != "MERGE" else "reconciliation",
            user_id=user_id,
            skip_dedup=deleted_now > 0,
            _provider_model=_pm,
            _precomputed_embedding=_precomputed,
        )
        if entry is not None:
            if action == "SUPERSEDE":
                summary["superseded"] += 1
            elif action == "MERGE":
                summary["merged"] += 1
            else:
                summary["added"] += 1
        else:
            summary["skipped"] += 1

        # ``deleted`` counts the actual number of old fact rows that were
        # removed across all SUPERSEDE / MERGE actions in this batch.
        # ``superseded`` and ``merged`` count *new* facts whose decision
        # was SUPERSEDE / MERGE — when one new fact replaces 3 old ones
        # those counters are 1 each while ``deleted`` is 3.
        summary["deleted"] = summary.get("deleted", 0) + deleted_now

    # Single atomic commit for the whole apply phase: SUPERSEDE / MERGE
    # deletes and their replacement inserts land together, or (on any
    # exception propagating out of the loop above) not at all — the
    # caller's ``async with async_session()`` exit discards the
    # uncommitted transaction.  Transaction ownership moved here from
    # ``_flush_facts_to_memory`` so the earlier commit-release points can
    # hand the pooled connection back around the LLM / embedding calls.
    if db.in_transaction():
        await db.commit()

    return summary


async def load_persistent_context(
    db: AsyncSession,
    *,
    agent_id: str,
    user_id: str | None = None,
    limit: int = 10,
    query: str | None = None,
    _provider_model: tuple | None = None,
    release_before_embed: bool = False,
    embed_timeout_secs: float | None = None,
    embed_deadline: float | None = None,
    provider_model_task: asyncio.Task | None = None,
    timings: dict[str, float | bool] | None = None,
) -> str:
    """Load persistent memories (facts, style, preference) for prompt injection.

    Returns a formatted text block suitable for inclusion in the system prompt,
    complementing the Redis-based short-term and medium-term memory: the
    entries :func:`load_persistent_entries` selects, rendered by
    :func:`format_persistent_context`.
    """
    return format_persistent_context(
        await load_persistent_entries(
            db,
            agent_id=agent_id,
            user_id=user_id,
            limit=limit,
            query=query,
            _provider_model=_provider_model,
            release_before_embed=release_before_embed,
            embed_timeout_secs=embed_timeout_secs,
            embed_deadline=embed_deadline,
            provider_model_task=provider_model_task,
            timings=timings,
        )
    )


async def load_persistent_entries(
    db: AsyncSession,
    *,
    agent_id: str,
    user_id: str | None = None,
    limit: int = 10,
    query: str | None = None,
    _provider_model: tuple | None = None,
    release_before_embed: bool = False,
    embed_timeout_secs: float | None = None,
    embed_deadline: float | None = None,
    provider_model_task: asyncio.Task | None = None,
    timings: dict[str, float | bool] | None = None,
) -> list[AgentMemoryEntry]:
    """The persistent entries injected into the system prompt.

    Returned in the order they were chosen - the most relevant to *query*
    first, then the newest - which is the order a budget keeps them in
    (:func:`format_persistent_context`).  Only non-consolidated entries are
    returned (consolidated memory is already injected via the Redis
    medium-term slot).

    When *query* is provided (e.g. the latest user message), entries are
    selected by **semantic relevance** to the query via pgvector cosine
    distance — preserving the exact scoping semantics of
    :func:`search_agent_memories` (agent-level entries + the given user's
    entries) and the same ``_RELEVANCE_THRESHOLD`` floor.  When fewer than
    *limit* relevant entries match, the remainder is filled by recency
    (deduplicated by id).  When *query* is ``None`` or the embedding cannot
    be computed, the original recency-only behaviour applies.

    This bridges the gap where extracted facts from context compaction and
    style-learning entries were only reachable via the ``search_memory`` tool.

    When *_provider_model* is supplied as ``(provider, model)`` the query
    embedding bypasses the ``get_embedding_provider(db)`` lookup, so the
    embedding HTTP round-trip issues no work on *db* — callers that pass a
    request-scoped session should pre-resolve the provider in a throwaway
    session so this per-turn call does not pin the request's pooled
    connection across the embed.

    When *release_before_embed* is ``True`` the session is additionally
    committed after the existence probe, handing the pooled connection back
    for the duration of the embedding HTTP call (SQLAlchemy re-acquires on
    the next query).  Only safe for callers whose session holds no pending
    writes at this point — both chat and assignment setup phases qualify
    (reads only until their pre-loop release).

    *embed_timeout_secs* bounds the query embedding (default
    ``_SETUP_EMBED_TIMEOUT_SECS``); a chat turn passes
    ``CHAT_TURN_EMBED_BUDGET_SECS`` so a slow embedding endpoint degrades to
    the recency load instead of delaying the first token.

    *embed_deadline* (a ``time.monotonic()`` instant) bounds the whole wait
    for the embedding, provider resolution included: the embed gets what is
    left of it after the probe, and none at all below
    ``_MIN_EMBED_WINDOW_SECS`` (the recency load answers instead), so a
    caller with an end-to-end budget is not overrun by the steps before the
    embed.  *provider_model_task* resolves ``(provider, model)`` concurrently
    with the probe and is awaited only when the embedding is needed; it is
    the caller's to cancel.  *timings*, when given, is filled with the
    milliseconds of each step (``probe_ms``, ``resolve_ms``, ``embed_ms``,
    ``vector_ms``, ``recency_ms``), ``embed_window_ms`` and
    ``embed_timed_out`` / ``embed_skipped``, so a trace can name the step
    that holds the load.  With *embed_deadline* the recency rows are read
    while the embedding is in flight and a timed-out embedding is not waited
    for while it unwinds, so the load is bounded by the deadline plus one
    query (#8801).

    Only entries that still hold for this reader are eligible: a
    superseded entry, and a state entry a newer state entry on its subject
    outdates, never take a slot.
    """
    # When a user_id is provided, load both user-scoped and agent-level
    # entries.  When user_id is None (company-managed agents, anonymous
    # users, assignments), restrict to agent-level entries only to prevent
    # leaking per-user facts across different users.
    parsed_uid = safe_uuid(user_id)
    if parsed_uid:
        scope_conditions = (
            AgentMemoryEntry.agent_id == uuid.UUID(agent_id),
            AgentMemoryEntry.memory_type != "consolidated",
            (AgentMemoryEntry.user_id == parsed_uid)
            | (AgentMemoryEntry.user_id.is_(None)),
            _active_for_reader(parsed_uid),
        )
    else:
        scope_conditions = (
            AgentMemoryEntry.agent_id == uuid.UUID(agent_id),
            AgentMemoryEntry.memory_type != "consolidated",
            AgentMemoryEntry.user_id.is_(None),
            _active_for_reader(None),
        )

    entries: list[AgentMemoryEntry] = []

    def _record(key: str, started: float) -> None:
        if timings is not None:
            timings[key] = round((time.monotonic() - started) * 1000, 1)

    # Cheap probe before paying for an embedding call: most agents start with
    # zero persistent entries, and this path runs on every chat turn. It
    # counts up to ``limit + 1``: a scope holding at most *limit* entries is
    # returned whole by the recency load below whatever the query, so the
    # embedding round-trip and the vector search could not change the
    # answer.
    held = 0
    if query and query.strip():
        started = time.monotonic()
        probe = await db.execute(
            select(AgentMemoryEntry.id).where(*scope_conditions).limit(limit + 1)
        )
        held = len(probe.scalars().all())
        _record("probe_ms", started)
        if not held:
            return []

    # With an end-to-end deadline (the chat turn) the recency rows are read
    # on *db* while the provider resolves and the embedding is in flight -
    # neither touches *db* - so the recency query no longer adds to the
    # embed wait (#8801).  Without one (assignments) it runs only when the
    # semantic search leaves slots, as before.
    recency_task: asyncio.Task | None = None
    recent_rows: list[AgentMemoryEntry] | None = None

    async def _load_recent() -> list[AgentMemoryEntry]:
        started = time.monotonic()
        result = await db.execute(
            select(AgentMemoryEntry)
            .where(*scope_conditions)
            .order_by(AgentMemoryEntry.created_at.desc())
            .limit(limit)
        )
        rows = list(result.scalars().all())
        _record("recency_ms", started)
        return rows

    try:
        if held > limit:
            from app.core.database import release_db_connection_before_loop

            if embed_deadline is not None:

                async def _recent_then_release() -> list[AgentMemoryEntry]:
                    rows = await _load_recent()
                    if release_before_embed:
                        await release_db_connection_before_loop(db)
                    return rows

                recency_task = asyncio.create_task(_recent_then_release())
            elif release_before_embed:
                await release_db_connection_before_loop(db)

            # Cap the per-turn query embedding (P1.3): this
            # HTTP round-trip runs in the pre-loop setup phase of EVERY chat
            # turn and previously rode the provider's SDK read timeout (up to
            # 600 s).  A slow/hung embedding endpoint must degrade to the
            # recency-based memory load below, not stall the whole turn.
            embed_budget = (
                _SETUP_EMBED_TIMEOUT_SECS if embed_timeout_secs is None else embed_timeout_secs
            )

            def _window() -> float:
                if embed_deadline is None:
                    return embed_budget
                return min(embed_budget, embed_deadline - time.monotonic())

            query_embedding = None
            can_embed = True
            if _provider_model is None and provider_model_task is not None:
                started = time.monotonic()
                try:
                    # shield: the task is the caller's, a timeout here leaves
                    # it to the caller to cancel.
                    _provider_model = await asyncio.wait_for(
                        asyncio.shield(provider_model_task), timeout=max(_window(), 0.0)
                    )
                except TimeoutError:
                    can_embed = False
                _record("resolve_ms", started)
            window = _window()
            if not can_embed or window < _MIN_EMBED_WINDOW_SECS:
                if timings is not None:
                    timings["embed_skipped"] = True
            else:
                # Without a resolved provider the embed looks it up on *db*,
                # which the recency read must have finished with first.
                detached = embed_deadline is not None and _provider_model is not None
                if not detached and recency_task is not None:
                    recent_rows = await recency_task
                    recency_task = None
                    window = _window()
                if timings is not None:
                    timings["embed_window_ms"] = round(window * 1000, 1)
                started = time.monotonic()
                try:
                    if detached:
                        query_embedding = await _embed_within(
                            embed_text(db, query, _provider_model=_provider_model), window
                        )
                    else:
                        query_embedding = await asyncio.wait_for(
                            embed_text(db, query, _provider_model=_provider_model),
                            timeout=window,
                        )
                except TimeoutError:
                    logger.warning(
                        "Persistent-memory query embedding timed out after %.1fs — "
                        "skipping semantic search and falling back to the "
                        "recency-based memory load",
                        window,
                    )
                    if timings is not None:
                        timings["embed_timed_out"] = True
                _record("embed_ms", started)
            if recency_task is not None:
                recent_rows = await recency_task
                recency_task = None
            if query_embedding is not None:
                started = time.monotonic()
                distance = AgentMemoryEntry.embedding.cosine_distance(query_embedding)
                relevant = await db.execute(
                    select(AgentMemoryEntry)
                    .where(
                        *scope_conditions,
                        AgentMemoryEntry.embedding.is_not(None),
                        _matching_dimensions_filter(query_embedding),
                        distance < (1 - _RELEVANCE_THRESHOLD),
                    )
                    .order_by(distance)
                    .limit(limit)
                )
                entries = list(relevant.scalars().all())
                _record("vector_ms", started)

        if len(entries) < limit:
            # Recency path: the original behaviour (query is None or embedding
            # failed), or top-up when fewer than *limit* relevant hits exist.
            #
            # Fetching ``limit`` rows is always sufficient here — no over-fetch
            # is needed to compensate for the id dedup below.  Every relevant
            # entry that also appears in this recency window is skipped as a
            # duplicate, but it already occupies one slot in ``entries``, so the
            # union of both result sets always contains at least
            # ``min(limit, total_eligible_rows)`` unique entries.
            if recent_rows is None:
                recent_rows = await _load_recent()
            seen_ids = {entry.id for entry in entries}
            for entry in recent_rows:
                if entry.id in seen_ids:
                    continue
                entries.append(entry)
                seen_ids.add(entry.id)
                if len(entries) >= limit:
                    break
    finally:
        if recency_task is not None and not recency_task.done():
            recency_task.cancel()

    return entries


def _drain_abandoned(task: asyncio.Task) -> None:
    """Retrieve an abandoned task's outcome so it is never logged as unretrieved."""
    if not task.cancelled():
        task.exception()


async def _embed_within(embed: Awaitable[list[float] | None], window: float) -> list[float] | None:
    """The embedding if it answers within *window* seconds, else ``TimeoutError``.

    Unlike ``asyncio.wait_for``, a timeout does not wait for the cancelled call
    to unwind: an HTTP client closing its in-flight request can take hundreds
    of milliseconds after the cancel, which ``wait_for`` adds to the caller's
    wait (#8801).  The call is cancelled and left to finish on its own; it
    must not use a session the caller goes on using.
    """
    task = asyncio.ensure_future(embed)
    done, _pending = await asyncio.wait({task}, timeout=window)
    if task in done:
        return task.result()
    task.cancel()
    task.add_done_callback(_drain_abandoned)
    raise TimeoutError


_PERSISTENT_CONTEXT_HEADER = "── Persistent Knowledge (durable facts & learned patterns) ──"

# Below this, a cut entry is a fragment rather than knowledge.
_MIN_CUT_ENTRY_CHARS = 200


def _omitted_entries_notice(count: int) -> str:
    noun = "entry" if count == 1 else "entries"
    return f"[...{count} more persistent memory {noun} left out to fit the prompt budget...]"


def format_persistent_context(
    entries: list[AgentMemoryEntry], *, max_chars: int | None = None
) -> str:
    """Render *entries* as the persistent knowledge block, oldest first.

    With *max_chars* the block, notice included, never exceeds it: it holds
    the entries that fit whole, taken in the order given (the order
    :func:`load_persistent_entries` chose them in), and one line says how many
    it left out.  An entry is cut only when none fits whole - then the first
    one, at a clean boundary.  The entries left out stay stored and
    searchable; a budget decides what one prompt carries, not what the agent
    keeps.

    Only entries that still hold are rendered: a superseded entry is
    dropped, and of the state entries on one subject only the newest. A state
    entry is dated ("as of <date>, verify live"), and a block holding one
    carries :data:`STATE_GUIDANCE` under its header.
    """
    lines = [(entry, _entry_line(entry)) for entry in _current_entries(entries)]
    guidance = len(STATE_GUIDANCE) + 1 if any(_is_state(entry) for entry, _ in lines) else 0
    omitted = 0
    if max_chars is not None:
        lines, omitted = _fit_entry_lines(lines, max_chars, reserved=guidance)
    if not lines:
        return ""

    # Render in chronological order (oldest first), matching the historical
    # recency-only output so prompt assembly is untouched.
    lines.sort(key=lambda pair: pair[0].created_at or datetime.min.replace(tzinfo=UTC))

    parts = [_PERSISTENT_CONTEXT_HEADER]
    if any(_is_state(entry) for entry, _ in lines):
        parts.append(STATE_GUIDANCE)
    parts.extend(line for _, line in lines)
    if omitted:
        parts.append(_omitted_entries_notice(omitted))
    return "\n".join(parts)


def _is_state(entry: AgentMemoryEntry) -> bool:
    return entry.memory_type == STATE_MEMORY_TYPE


def _recency(entry: AgentMemoryEntry) -> tuple[datetime, int]:
    """The order ``_shadowed_by_newer_state`` compares in: ``created_at``, then ``id``."""
    return (
        entry.created_at or datetime.min.replace(tzinfo=UTC),
        entry.id.int if entry.id else 0,
    )


def _current_entries(entries: list[AgentMemoryEntry]) -> list[AgentMemoryEntry]:
    """*entries* that still hold, in the order given."""
    newest: dict[str, AgentMemoryEntry] = {}
    for entry in entries:
        if entry.superseded_at is None and _is_state(entry) and entry.subject_key:
            held = newest.get(entry.subject_key)
            if held is None or _recency(entry) > _recency(held):
                newest[entry.subject_key] = entry
    return [
        entry
        for entry in entries
        if entry.superseded_at is None
        and not (_is_state(entry) and entry.subject_key and newest[entry.subject_key] is not entry)
    ]


def _entry_line(entry: AgentMemoryEntry) -> str:
    content = strip_leading_reasoning(entry.content)
    if not _is_state(entry):
        return f"• [{entry.memory_type}] {content}"
    label = f"state: {entry.subject_key}" if entry.subject_key else "state"
    stored = format_stored_at(entry.created_at)
    day = stored[:10] if stored else "an unknown date"
    return f"• [{label}] as of {day}, verify live: {content}"


def _fit_entry_lines(
    lines: list[tuple[AgentMemoryEntry, str]], max_chars: int, *, reserved: int = 0
) -> tuple[list[tuple[AgentMemoryEntry, str]], int]:
    """The rendered entries kept within *max_chars*, and how many were left out.

    *reserved* characters are kept for a line the block may add (the state
    guidance).
    """
    max_chars -= reserved
    if len(_PERSISTENT_CONTEXT_HEADER) + sum(1 + len(line) for _, line in lines) <= max_chars:
        return lines, 0
    # The notice is reserved at its widest, every entry left out.
    room = (
        max_chars
        - len(_PERSISTENT_CONTEXT_HEADER)
        - 1
        - len(_omitted_entries_notice(len(lines)))
    )
    kept: list[tuple[AgentMemoryEntry, str]] = []
    for entry, line in lines:
        if 1 + len(line) <= room:
            kept.append((entry, line))
            room -= 1 + len(line)
    if not kept and lines and room - 1 >= _MIN_CUT_ENTRY_CHARS:
        from app.services.agent_memory import _truncate_at_boundary

        entry, line = lines[0]
        kept.append((entry, _truncate_at_boundary(line, room - 1, " [...]")))
    return kept, len(lines) - len(kept)


async def clear_agent_memories(
    db: AsyncSession,
    agent_id: str,
    *,
    preserve_memory_types: Collection[str] | None = None,
) -> int:
    """Delete persistent memories for an agent.

    Called when an agent is deleted.  Uses ``DELETE ... rowcount`` so the
    count and the delete are observed atomically: a count-then-delete
    pattern would expose a TOCTOU race where rows inserted between the
    count and the delete would be silently dropped from the returned
    total.

    *preserve_memory_types* keeps the named classes.  It exists for callers
    that purge an ENGAGEMENT rather than the agent — the OpenAEV autonomous
    run purge must drop every forensic row (target lists, proven verdicts,
    resolved scope) while keeping :data:`TRADECRAFT_MEMORY_TYPE`, which is
    knowledge about the platform and outlives any single run.  It defaults to
    ``None`` so agent deletion and the operator's "clear all memory" action
    keep wiping everything.
    """
    conditions = [AgentMemoryEntry.agent_id == uuid.UUID(agent_id)]
    if preserve_memory_types:
        conditions.append(
            AgentMemoryEntry.memory_type.notin_(list(preserve_memory_types))
        )
    result = await db.execute(delete(AgentMemoryEntry).where(*conditions))
    count = int(getattr(result, "rowcount", 0) or 0)
    # The Memory Steward history of the agent's lanes names entries that are
    # gone (a preserved class is never consolidated): it goes with them.
    from app.models.memory_consolidation import (
        MemoryConsolidation,
        MemoryConsolidationTombstone,
        MemoryLaneCheckpoint,
    )

    for model in (MemoryConsolidation, MemoryLaneCheckpoint, MemoryConsolidationTombstone):
        await db.execute(delete(model).where(model.agent_id == uuid.UUID(agent_id)))
    if not count:
        return 0
    await db.flush()
    logger.info("Cleared %d persistent memories for agent %s", count, agent_id)
    return count


# ── Embedding helpers ────────────────────────────────────────────────────────


async def embed_text(
    db: AsyncSession,
    text: str,
    *,
    _provider_model: tuple | None = None,
    refuse_excluded: bool = False,
) -> list[float] | None:
    """Generate an embedding for the given text using the configured provider.

    When *_provider_model* is supplied as ``(provider, model)`` the DB
    lookup for the embedding provider is skipped — useful when the caller
    embeds multiple texts in the same request and can resolve once.
    *refuse_excluded*: see :func:`embed_text_with_model`.
    """
    embedding, _model = await embed_text_with_model(
        db, text, _provider_model=_provider_model, refuse_excluded=refuse_excluded
    )
    return embedding


async def embed_text_with_model(
    db: AsyncSession,
    text: str,
    *,
    _provider_model: tuple | None = None,
    refuse_excluded: bool = False,
) -> tuple[list[float] | None, str | None]:
    """Generate an embedding and return ``(embedding, model_id)``.

    When the embedding provider excludes the person the text is for, a
    write gets ``(None, None)`` and stores its entry without a vector, while a
    search (*refuse_excluded*) gets the ``ProviderNotUsableError`` to refuse
    with, rather than an empty result that reads as "nothing remembered".

    The model identifier is returned alongside the vector so callers can
    stamp ``embedding_model`` / ``embedding_dimensions`` provenance on the
    rows they write — required for stale-embedding detection when the
    platform embedding model changes (mirrors the KB ``embeddings`` table).

    Returns ``(None, None)`` when embedding fails.
    """
    try:
        if _provider_model:
            provider, model = _provider_model[:2]
        else:
            from app.llm.router import get_embedding_provider

            provider, model, _provider_id = await get_embedding_provider(db)
        embedding = await provider.embed(text[:8000], model=model)
        if not embedding:
            # Some providers return an empty list instead of raising on
            # failure (e.g. a missing ``embedding`` field in the response).
            # Stamping ``embedding_dimensions=0`` would make the row both
            # unsearchable and — depending on the expected-dims probe —
            # potentially invisible to the stale clause.  Treat it as a
            # failure so callers store NULL embedding/provenance instead.
            logger.error(
                "Embedding provider returned an empty vector (text_len=%d) — "
                "treating as failure; downstream memory entry will be stored "
                "without an embedding",
                len(text or ""),
            )
            return None, None
        if is_zero_norm_vector(embedding):
            # A well-shaped all-zeros vector is equally unusable: cosine
            # distance against it is NaN, so a row stamped with it would
            # poison similarity searches while looking healthy (it has
            # matching model/dimensions provenance and would never be
            # flagged stale).  Same failure treatment as the empty case —
            # the caller stores NULL and ``no_embedding_count`` stays
            # meaningful.
            logger.error(
                "Embedding provider returned a zero-norm vector "
                "(%d dims, text_len=%d) — treating as failure; downstream "
                "memory entry will be stored without an embedding",
                len(embedding),
                len(text or ""),
            )
            return None, None
        return embedding, model
    except ProviderNotUsableError as e:
        # The embedding provider excludes the person this memory is for:
        # their text is not sent to it, by design - not a fault. The
        # entry is stored without a vector, and the re-embedding pass leaves
        # it alone for as long as the exclusion holds.
        if refuse_excluded:
            raise
        logger.info("Memory text not embedded: %s", e)
        return None, None
    except Exception as e:
        # ERROR-level: a None embedding causes ``persist_memory`` to store
        # a memory entry WITHOUT an embedding vector, making it invisible
        # to ``search_agent_memories`` forever. Over time this silently
        # corrupts the memory index. Operators must know.
        logger.error(
            "Failed to generate embedding for memory (text_len=%d) — "
            "downstream memory entry will not appear in semantic search: %s",
            len(text or ""),
            e,
            exc_info=True,
        )
        return None, None


# ── Memory hygiene ───────────────────────────────────────────────────────────


# Cache of probed model-native embedding dimensions, keyed by model id.  A
# model's native output size never changes, so one probe per process per
# model is enough.  Used when the admin leaves PlatformSettings
# ``embedding_dimensions`` unset (= model-native size): without the probe the
# stale clause could not flag rows stamped with a previously-configured
# shortened dimension, leaving them permanently invisible to the
# dimension-filtered searches (they would never be re-embedded).
_NATIVE_DIMS_CACHE: dict[str, int] = {}


async def _resolve_embedding_target(
    db: AsyncSession,
) -> tuple[BaseLLMProvider | None, str | None, int | None]:
    """Resolve the current embedding ``(provider, model, expected_dims)``.

    ``expected_dims`` is the admin-configured ``embedding_dimensions`` from
    PlatformSettings.  When unset (``None`` = model-native size) the native
    dimension is probed from the provider once and cached, so staleness
    checks still catch rows stamped with a previously-configured shortened
    dimension.  ``expected_dims`` stays ``None`` only if the probe fails.
    Returns ``(None, None, None)`` when no embedding provider is configured —
    callers degrade gracefully (no re-embedding, no model-based staleness).
    """
    try:
        from app.llm.router import get_embedding_provider

        provider, model, _provider_id = await get_embedding_provider(db)
    except Exception as exc:
        logger.debug(
            "Embedding provider lookup failed while resolving embedding "
            "target: %s",
            exc,
            exc_info=True,
        )
        return None, None, None

    from app.api.v1.platform_settings_helpers import get_platform_settings

    ps = await get_platform_settings(db)
    expected_dims = ps.embedding_dimensions
    if expected_dims is None:
        expected_dims = _NATIVE_DIMS_CACHE.get(model)
        if expected_dims is None:
            try:
                probe = await provider.embed("dimension probe", model=model)
                if probe:
                    expected_dims = len(probe)
                    _NATIVE_DIMS_CACHE[model] = expected_dims
                else:
                    logger.debug(
                        "Native-dimension probe returned an empty vector for "
                        "embedding model %s (dimension-mismatch staleness "
                        "disabled this run)",
                        model,
                    )
            except Exception as exc:
                logger.debug(
                    "Native-dimension probe failed for embedding model %s "
                    "(dimension-mismatch staleness disabled this run): %s",
                    model,
                    exc,
                    exc_info=True,
                )
    return provider, model, expected_dims


def _stale_embedding_clause(model: str, expected_dims: int | None):
    """SQL OR-clause matching entries whose embedding provenance is stale.

    Stale = missing vector, unknown provenance (NULL model/dimensions
    stamp, i.e. pre-migration rows), or a model/dimension that differs
    from the current platform embedding target.
    """
    conditions = [
        AgentMemoryEntry.embedding.is_(None),
        AgentMemoryEntry.embedding_model.is_(None),
        AgentMemoryEntry.embedding_model != model,
        AgentMemoryEntry.embedding_dimensions.is_(None),
    ]
    if expected_dims is not None:
        conditions.append(AgentMemoryEntry.embedding_dimensions != expected_dims)
    return or_(*conditions)


async def _embedding_excluded_users(db: AsyncSession) -> frozenset[str] | None:
    """The users the platform embedding provider excludes.

    ``None`` when they could not be read: the caller must then leave every
    user's content alone rather than risk sending an excluded person's.
    """
    try:
        from app.llm.router import get_embedding_provider

        # The platform's provider itself, not the one some bound person may use.
        with acting_for(None):
            _provider, _model, provider_id = await get_embedding_provider(db)
        return await excluded_user_ids(db, provider_id)
    except Exception as exc:
        logger.warning(
            "Could not read who the embedding provider excludes; user-scoped "
            "memories are not re-embedded this pass: %s",
            format_exception_message(exc),
        )
        return None


async def reembed_stale_memories(
    db: AsyncSession,
    *,
    agent_id: str | None = None,
    batch_size: int = _REEMBED_BATCH_SIZE,
) -> int:
    """Re-embed memory entries whose embedding is missing or stale.

    Stale entries are those whose ``embedding`` is NULL, whose
    ``embedding_model`` differs from the current platform embedding model
    (or is NULL — pre-migration rows), or whose ``embedding_dimensions``
    differs from the admin-configured dimension.  Each entry is re-embedded
    with the current model in batches and stamped with the new provenance,
    mirroring the KB stale-embedding reprocess path.

    If *agent_id* is given, only that agent's entries are processed;
    otherwise all agents (scheduled maintenance).

    Returns the number of entries successfully re-embedded.  Returns 0
    without touching anything when no embedding provider is configured.
    """
    # ``model is None`` (not falsy) — model-optional providers (e.g. TEI)
    # legitimately resolve to an empty model string while embedding calls
    # still work; only a failed provider lookup disables re-embedding.
    # The platform's embedding target, whoever the context acts for: the
    # people it excludes are skipped entry by entry below.
    with acting_for(None):
        provider, model, expected_dims = await _resolve_embedding_target(db)
    if provider is None or model is None:
        return 0
    # The people the embedding provider is never used for: their
    # entries stay stale rather than be sent to it. ``None`` = unknown, so
    # every user-scoped entry is left for a later pass.
    excluded = await _embedding_excluded_users(db)

    conditions = [_stale_embedding_clause(model, expected_dims)]
    if agent_id:
        conditions.append(AgentMemoryEntry.agent_id == uuid.UUID(agent_id))

    reembedded = 0
    failed = 0

    # Iterate the stale set with a (created_at, id) keyset cursor instead of
    # snapshotting every stale id into memory up front — a platform-wide
    # model change marks EVERY entry stale at once, and shared agents can
    # accumulate facts under many user scopes.  A keyset is the only safe
    # pagination here: re-stamped rows leave the stale set (plain
    # LIMIT/OFFSET would skip rows) while entries that persistently fail to
    # embed REMAIN stale (a "repeat until no stale rows" loop would spin
    # forever on them).  The strictly-increasing cursor visits each stale
    # row exactly once per run with bounded memory.
    last_key: tuple | None = None
    while True:
        page_q = select(AgentMemoryEntry).where(*conditions)
        if last_key is not None:
            page_q = page_q.where(
                tuple_(AgentMemoryEntry.created_at, AgentMemoryEntry.id) > last_key
            )
        page_q = page_q.order_by(
            AgentMemoryEntry.created_at.asc(), AgentMemoryEntry.id.asc()
        ).limit(batch_size)
        rows = (await db.execute(page_q)).scalars().all()
        if not rows:
            break

        # Hand the pooled connection back before the page's embedding HTTP
        # round-trips — a platform-wide model change makes this loop run for
        # a long time and holding the connection across every ``embed`` call
        # pins it for the whole pass.  The ORM rows stay usable
        # (``expire_on_commit=False``); attribute writes below are flushed
        # by the next commit.
        if db.in_transaction():
            await db.commit()

        for entry in rows:
            if entry.user_id is not None and (excluded is None or str(entry.user_id) in excluded):
                continue
            try:
                vec = await provider.embed(entry.content[:8000], model=model)
            except Exception as exc:
                failed += 1
                logger.warning(
                    "Failed to re-embed memory entry %s: %s", entry.id, exc
                )
                continue
            if not vec:
                # Some providers return ``[]`` instead of raising on failure
                # — log per entry (like the exception path) so operators can
                # trace which entries/provider responses keep failing.
                failed += 1
                logger.warning(
                    "Embedding provider returned an empty vector while "
                    "re-embedding memory entry %s (content_len=%d) — entry "
                    "left stale for the next maintenance pass",
                    entry.id,
                    len(entry.content or ""),
                )
                continue
            if is_zero_norm_vector(vec):
                # Stamping a zero-norm vector with the current model/dims
                # would permanently remove the entry from the stale set
                # (provenance matches) while poisoning cosine searches.
                # Leave it stale so a later pass can retry instead.
                failed += 1
                logger.warning(
                    "Embedding provider returned a zero-norm vector while "
                    "re-embedding memory entry %s (content_len=%d) — entry "
                    "left stale for the next maintenance pass",
                    entry.id,
                    len(entry.content or ""),
                )
                continue
            entry.embedding = [float(v) for v in vec]
            entry.embedding_model = model
            entry.embedding_dimensions = len(entry.embedding)
            reembedded += 1
        # Commit (not just flush) per page so the write transaction — and
        # its pooled connection — is held only for the brief batched UPDATE,
        # never across the next page's embedding calls.  Per-page
        # durability also means an interrupted pass keeps its progress.
        await db.commit()

        if len(rows) < batch_size:
            break  # short page — nothing left after this cursor position
        last_key = (rows[-1].created_at, rows[-1].id)

    if reembedded or failed:
        scope = f"agent {agent_id}" if agent_id else "all agents"
        logger.info(
            "Re-embedded %d stale memory entries (%d failed) with model %s for %s",
            reembedded,
            failed,
            model,
            scope,
        )
    return reembedded


async def get_memory_health(
    db: AsyncSession,
    *,
    agent_id: str,
) -> dict:
    """Return memory statistics and health indicators for an agent.

    Used by the memory health API endpoint to give admins visibility
    into the state of an agent's persistent memory.
    """
    aid = uuid.UUID(agent_id)

    # Counts by memory_type
    type_counts_q = await db.execute(
        select(AgentMemoryEntry.memory_type, func.count())
        .where(AgentMemoryEntry.agent_id == aid)
        .group_by(AgentMemoryEntry.memory_type)
    )
    type_counts: dict[str, int] = {row[0]: row[1] for row in type_counts_q.all()}
    total = sum(type_counts.values())

    # Oldest and newest fact
    oldest_q = await db.execute(
        select(AgentMemoryEntry.created_at)
        .where(AgentMemoryEntry.agent_id == aid, AgentMemoryEntry.memory_type == "fact")
        .order_by(AgentMemoryEntry.created_at.asc())
        .limit(1)
    )
    oldest_fact = oldest_q.scalar_one_or_none()

    newest_q = await db.execute(
        select(AgentMemoryEntry.created_at)
        .where(AgentMemoryEntry.agent_id == aid, AgentMemoryEntry.memory_type == "fact")
        .order_by(AgentMemoryEntry.created_at.desc())
        .limit(1)
    )
    newest_fact = newest_q.scalar_one_or_none()

    # Entries without embeddings (cannot be searched)
    no_embedding_q = await db.execute(
        select(func.count())
        .select_from(AgentMemoryEntry)
        .where(
            AgentMemoryEntry.agent_id == aid,
            AgentMemoryEntry.embedding.is_(None),
        )
    )
    no_embedding_count = no_embedding_q.scalar() or 0

    # Entries whose embedding provenance is stale (model/dimensions differ
    # from the current platform target, or NULL-stamped legacy rows).
    # These are invisible to dimension-filtered semantic search until the
    # re-embed maintenance task processes them.
    # ``is not None`` (not truthiness) — model-optional providers resolve to
    # an empty model string, which is a valid embedding target; only a failed
    # provider lookup (None) skips the stale count.
    _provider, current_model, expected_dims = await _resolve_embedding_target(db)
    stale_embedding_count = 0
    if current_model is not None:
        stale_embedding_q = await db.execute(
            select(func.count())
            .select_from(AgentMemoryEntry)
            .where(
                AgentMemoryEntry.agent_id == aid,
                AgentMemoryEntry.embedding.is_not(None),
                _stale_embedding_clause(current_model, expected_dims),
            )
        )
        stale_embedding_count = stale_embedding_q.scalar() or 0

    # Entries older than retention period
    retention_cutoff = datetime.now(UTC) - timedelta(days=_FACT_RETENTION_DAYS)
    stale_q = await db.execute(
        select(func.count())
        .select_from(AgentMemoryEntry)
        .where(
            AgentMemoryEntry.agent_id == aid,
            AgentMemoryEntry.memory_type == "fact",
            AgentMemoryEntry.created_at < retention_cutoff,
        )
    )
    stale_count = stale_q.scalar() or 0

    fact_count = type_counts.get("fact", 0) + type_counts.get("learned", 0)

    # ``over_cap`` mirrors the per-scope cap semantics of
    # ``enforce_memory_cap``: sum of each scope's excess over the cap.
    # Under-cap scopes contribute 0, so the HAVING clause lets the DB
    # return only the scopes that matter (same optimisation as the
    # eviction query in ``enforce_memory_cap``).
    scope_counts_q = await db.execute(
        select(AgentMemoryEntry.user_id, func.count())
        .where(
            AgentMemoryEntry.agent_id == aid,
            AgentMemoryEntry.memory_type.in_(["fact", "learned"]),
            ~held_by_consolidation(),
        )
        .group_by(AgentMemoryEntry.user_id)
        .having(func.count() > _MAX_FACTS_PER_AGENT)
    )
    over_cap = sum(
        max(0, row[1] - _MAX_FACTS_PER_AGENT) for row in scope_counts_q.all()
    )

    return {
        "total_entries": total,
        "by_type": type_counts,
        "fact_count": fact_count,
        "oldest_fact": oldest_fact.isoformat() if oldest_fact else None,
        "newest_fact": newest_fact.isoformat() if newest_fact else None,
        "no_embedding_count": no_embedding_count,
        "stale_embedding_count": stale_embedding_count,
        "embedding_model": current_model,
        "stale_count": stale_count,
        "over_cap": over_cap,
        "retention_days": _FACT_RETENTION_DAYS,
        "max_facts_per_agent": _MAX_FACTS_PER_AGENT,
        "dedup_similarity_threshold": _DEDUP_SIMILARITY_THRESHOLD,
    }


async def prune_old_memories(
    db: AsyncSession,
    *,
    agent_id: str | None = None,
    retention_days: int = _FACT_RETENTION_DAYS,
) -> int:
    """Delete fact entries older than *retention_days*.

    If *agent_id* is given, prunes only that agent; otherwise prunes all
    agents (used by the scheduled maintenance task).

    Consolidated and style entries are never pruned — only ``fact``,
    ``learned`` and ``state`` types are subject to age-based removal, a
    superseded entry of those types included - except one a Memory Steward
    consolidation holds for its undo, which its own purge deletes.
    """
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    conditions = [
        AgentMemoryEntry.memory_type.in_(["fact", "learned", STATE_MEMORY_TYPE]),
        AgentMemoryEntry.created_at < cutoff,
        ~held_by_consolidation(),
    ]
    if agent_id:
        conditions.append(AgentMemoryEntry.agent_id == uuid.UUID(agent_id))

    # Use ``DELETE ... rowcount`` directly so the operation is atomic.  A
    # count-then-delete pattern races with concurrent writers and skews
    # the reported total.
    result = await db.execute(delete(AgentMemoryEntry).where(*conditions))
    count = int(getattr(result, "rowcount", 0) or 0)
    if count:
        await db.flush()
        scope = f"agent {agent_id}" if agent_id else "all agents"
        logger.info(
            "Pruned %d stale memory entries (older than %dd) for %s",
            count,
            retention_days,
            scope,
        )
    return count


async def enforce_memory_cap(
    db: AsyncSession,
    *,
    agent_id: str,
    max_facts: int = _MAX_FACTS_PER_AGENT,
) -> int:
    """Enforce a per-scope cap on fact entries, evicting the oldest.

    The cap is enforced **per (agent, user_id scope)** rather than
    agent-globally: the agent-level pool (``user_id IS NULL``) and each
    user-scoped pool independently hold up to *max_facts* entries.  An
    agent-global cap would let one user's burst of facts evict another
    user's (or the shared agent-level) facts — a cross-user interference
    issue on multi-user agents (MEM-10).  Eviction within a scope removes
    that scope's oldest entries first.

    An entry a Memory Steward consolidation holds for its undo is neither
    counted nor evicted: it is superseded already, and its own purge deletes
    it after the retention.

    Returns the total number of entries evicted across all scopes.
    """
    aid = uuid.UUID(agent_id)

    # Count current facts per scope (NULL = agent-level, UUID = per-user).
    # The HAVING clause makes the DB return only scopes that are actually
    # over the cap — on heavily shared agents most scopes are under cap and
    # would otherwise inflate the result set just to be skipped in Python.
    counts_q = await db.execute(
        select(AgentMemoryEntry.user_id, func.count())
        .where(
            AgentMemoryEntry.agent_id == aid,
            AgentMemoryEntry.memory_type.in_(["fact", "learned"]),
            ~held_by_consolidation(),
        )
        .group_by(AgentMemoryEntry.user_id)
        .having(func.count() > max_facts)
    )
    scope_counts: list[tuple[uuid.UUID | None, int]] = [
        (row[0], row[1]) for row in counts_q.all()
    ]

    total_evicted = 0
    for scope_uid, current in scope_counts:
        excess = current - max_facts
        if excess <= 0:  # safety net — HAVING already excludes these
            continue

        scope_filter = (
            AgentMemoryEntry.user_id.is_(None)
            if scope_uid is None
            else AgentMemoryEntry.user_id == scope_uid
        )

        # Find the N entries to evict within this scope: the superseded ones
        # first (they no longer hold), then the oldest.
        oldest_q = await db.execute(
            select(AgentMemoryEntry.id)
            .where(
                AgentMemoryEntry.agent_id == aid,
                AgentMemoryEntry.memory_type.in_(["fact", "learned"]),
                scope_filter,
                ~held_by_consolidation(),
            )
            .order_by(
                AgentMemoryEntry.superseded_at.is_(None),
                AgentMemoryEntry.created_at.asc(),
            )
            .limit(excess)
        )
        ids_to_delete = [row[0] for row in oldest_q.all()]
        if not ids_to_delete:
            continue

        await db.execute(
            delete(AgentMemoryEntry).where(AgentMemoryEntry.id.in_(ids_to_delete))
        )
        total_evicted += len(ids_to_delete)
        logger.info(
            "Evicted %d oldest fact entries for agent %s scope %s (cap=%d, was=%d)",
            len(ids_to_delete),
            agent_id,
            scope_uid or "agent-level",
            max_facts,
            current,
        )

    if total_evicted:
        await db.flush()
    return total_evicted


async def consolidate_agent_facts(
    db: AsyncSession,
    *,
    agent_id: str,
    min_facts: int = _CONSOLIDATION_MIN_FACTS,
    batch_size: int = _CONSOLIDATION_BATCH_SIZE,
) -> int:
    """Merge related facts into condensed summaries for an agent, per scope.

    Consolidation is **scope-aware** (MEM-2): facts are batched and merged
    strictly within their ``user_id`` scope (NULL = agent-level, UUID =
    per-user), and the merged entry is persisted with that same scope.
    Merging across scopes would leak one user's facts into the agent-level
    pool (or another user's pool) via the merged entry — and the old
    implementation took the scope from only the first row of a mixed batch.

    The trigger condition is unchanged: consolidation only runs when the
    agent's **total** fact count (across all scopes) reaches *min_facts*.
    Once triggered, only scopes holding at least ``_CONSOLIDATION_MIN_BATCH``
    facts are selected (HAVING filter — no per-scope probe queries for
    scopes that would be skipped anyway), the most crowded scopes first,
    bounded to ``_CONSOLIDATION_MAX_SCOPES_PER_RUN`` scopes per run so a
    heavily shared agent can't turn one maintenance cycle into O(#scopes)
    LLM calls.  Each selected scope's oldest batch (up to *batch_size*
    facts) is merged into a single condensed entry by the LLM, the
    originals are deleted, and the merged result is persisted in-scope.

    The merged entry is persisted with ``skip_dedup=True``: it is a
    deliberately curated replacement for the batch that was just deleted,
    so it MUST land regardless of whether it happens to be similar to
    another (out-of-batch) fact in the scope.  Without this, a merged
    summary that scored >= ``_DEDUP_SIMILARITY_THRESHOLD`` against a
    newer fact would be skipped, the savepoint would roll back, and the
    same oldest batch would be re-selected and re-merged on every
    subsequent maintenance run — an endless loop that spams warnings,
    burns an LLM call per run, and never reduces the fact count.

    The delete + persist pair still runs inside a SAVEPOINT so a genuine
    persistence failure (an exception, or the defensive ``None`` guard)
    rolls back the delete and retains the original facts.

    Superseded facts are never merged: the merged entry is active,
    and merging one would bring back what no longer holds.

    Returns the total number of original facts that were merged (and
    deleted) across all scopes.
    """
    aid = uuid.UUID(agent_id)
    fact_types = ["fact", "learned"]
    mergeable = (
        AgentMemoryEntry.agent_id == aid,
        AgentMemoryEntry.memory_type.in_(fact_types),
        AgentMemoryEntry.superseded_at.is_(None),
    )

    # Check if consolidation is warranted (total count across scopes)
    count_q = await db.execute(
        select(func.count()).select_from(AgentMemoryEntry).where(*mergeable)
    )
    fact_count = count_q.scalar() or 0
    if fact_count < min_facts:
        return 0

    # Eligible scopes (NULL and specific user_ids): only those holding at
    # least a mergeable batch, most crowded first, bounded per run.
    scopes_q = await db.execute(
        select(AgentMemoryEntry.user_id)
        .where(*mergeable)
        .group_by(AgentMemoryEntry.user_id)
        .having(func.count() >= _CONSOLIDATION_MIN_BATCH)
        .order_by(func.count().desc())
        .limit(_CONSOLIDATION_MAX_SCOPES_PER_RUN)
    )
    scopes: list[uuid.UUID | None] = [row[0] for row in scopes_q.all()]
    if not scopes:
        return 0

    try:
        from app.llm.base import ChatMessage
        from app.llm.router import get_llm_for_user

        # Resolve provider AND model via the standard chain (agent default ->
        # platform default) so this background maintenance call runs on the
        # configured default chat model instead of the first-configured
        # provider's built-in default — which may be a non-chat / stale model
        # on image-only providers. ``user_id=None`` skips the
        # user-preference tier (there is no user in maintenance context).
        # Resolved again for each user scope below, so a provider that excludes
        # that user never merges their facts.
        with acting_for(None):
            agent_llm, agent_model = await get_llm_for_user(db, None)
    except Exception as e:
        logger.warning("Fact consolidation failed for agent %s: %s", agent_id, e)
        return 0

    total_merged = 0
    for scope_uid in scopes:
        llm, consolidation_model = agent_llm, agent_model
        if scope_uid is not None:
            try:
                with acting_for(scope_uid):
                    llm, consolidation_model = await get_llm_for_user(db, None)
            except Exception as e:
                logger.info(
                    "Fact consolidation skipped for agent %s scope %s: %s",
                    agent_id,
                    scope_uid,
                    format_exception_message(e),
                )
                continue
        scope_filter = (
            AgentMemoryEntry.user_id.is_(None)
            if scope_uid is None
            else AgentMemoryEntry.user_id == scope_uid
        )

        # Load the oldest batch of facts within this scope only
        facts_q = await db.execute(
            select(AgentMemoryEntry)
            .where(*mergeable, scope_filter)
            .order_by(AgentMemoryEntry.created_at.asc())
            .limit(batch_size)
        )
        facts = facts_q.scalars().all()
        # Safety net behind the HAVING filter (a concurrent prune/eviction
        # may have shrunk the scope between the two queries).
        if len(facts) < _CONSOLIDATION_MIN_BATCH:
            continue

        # Build a text block of this scope's facts for the LLM
        facts_text = "\n".join(f"- {strip_leading_reasoning(f.content)}" for f in facts)

        # Hand the pooled connection back before the (multi-second) merge
        # LLM call — the hourly maintenance pass runs up to 5 agents
        # concurrently and each scope's consolidation would otherwise pin
        # a connection for the whole generation.  The ``facts``
        # rows stay usable after commit (``expire_on_commit=False``); the
        # delete + persist pair below re-opens a transaction.
        if db.in_transaction():
            await db.commit()

        try:
            merge_kwargs: dict = {
                "model": consolidation_model,
                "messages": [
                    ChatMessage(
                        role="system",
                        content=(
                            "You are a memory curator. Merge the following individual facts "
                            "into a concise, well-organized summary. Remove redundancies, "
                            "combine related items, and preserve all unique information.\n\n"
                            "Output the merged result as a single block of bullet points. "
                            "Each bullet should be a self-contained piece of knowledge.\n"
                            "Do NOT include headers, timestamps, or meta-commentary."
                        ),
                    ),
                    ChatMessage(
                        role="user",
                        content=f"Facts to merge:\n\n{facts_text[:8000]}",
                    ),
                ],
                "max_tokens": _CONSOLIDATION_MAX_TOKENS,
                "temperature": 0.2,
                # A mechanical merge: on the models that think by default,
                # thinking would share the budget with the merged list.
                "disable_thinking": True,
            }
            response = await llm.chat(**merge_kwargs)
            if answer_was_cut(response):
                retry_budget = retry_output_budget(
                    llm, consolidation_model, _CONSOLIDATION_MAX_TOKENS
                )
                logger.warning(
                    "Fact consolidation for agent %s scope %s hit max_tokens=%d - "
                    "retrying once with max_tokens=%d",
                    agent_id,
                    scope_uid or "agent-level",
                    _CONSOLIDATION_MAX_TOKENS,
                    retry_budget,
                )
                response = await llm.chat(**{**merge_kwargs, "max_tokens": retry_budget})
                if answer_was_cut(response):
                    logger.warning(
                        "Fact consolidation for agent %s scope %s still cut at "
                        "max_tokens=%d - its %d facts are kept for the next run",
                        agent_id,
                        scope_uid or "agent-level",
                        retry_budget,
                        len(facts),
                    )
                    continue

            merged_content = strip_leading_reasoning(response.content or "")
            if not merged_content or len(merged_content.strip()) < _MIN_FACT_LENGTH:
                logger.warning(
                    "Fact consolidation returned empty result for agent %s scope %s",
                    agent_id,
                    scope_uid or "agent-level",
                )
                continue

            # Delete the originals and persist the merged result atomically
            # inside a SAVEPOINT: if ``persist_memory`` raises (genuine
            # persistence failure) the savepoint rolls back and the scope's
            # original facts are retained instead of being lost when the
            # outer maintenance transaction commits.  Deleting first (within
            # the savepoint) is deliberate — it prevents any future dedup
            # logic from matching the merged entry against its own
            # about-to-be-deleted source facts.
            fact_ids = [f.id for f in facts]
            async with db.begin_nested():
                await db.execute(
                    delete(AgentMemoryEntry).where(AgentMemoryEntry.id.in_(fact_ids))
                )

                # Persist the merged result with the SAME scope as its batch.
                # ``skip_dedup=True`` guarantees the curated merge lands even
                # when it resembles an out-of-batch fact — otherwise a dedup
                # skip would roll back the delete and the same oldest batch
                # would be re-merged on every maintenance run forever.
                merged_entry = await persist_memory(
                    db,
                    agent_id=agent_id,
                    content=merged_content.strip(),
                    memory_type="fact",
                    source="consolidation",
                    user_id=str(scope_uid) if scope_uid else None,
                    skip_dedup=True,
                )
                if merged_entry is None:
                    # Defensive: with ``skip_dedup=True`` a fact entry is
                    # always created, so this only trips on an unforeseen
                    # persistence failure.  Raising inside ``begin_nested``
                    # rolls the savepoint back (delete undone); the outer
                    # handler logs and moves on to the next scope.
                    raise RuntimeError(
                        "merged entry was not persisted (persistence "
                        "failure) — originals retained"
                    )

            await db.flush()
            total_merged += len(facts)
            logger.info(
                "Consolidated %d facts into 1 merged entry for agent %s scope %s",
                len(facts),
                agent_id,
                scope_uid or "agent-level",
            )

        except ContextOverflowError as e:
            logger.warning(
                "Skipping fact consolidation for agent %s scope %s: content "
                "exceeds context window (%s tokens vs %s limit)",
                agent_id,
                scope_uid or "agent-level",
                e.estimated_tokens,
                e.context_window,
            )
            continue
        except Exception as e:
            logger.warning(
                "Fact consolidation failed for agent %s scope %s: %s",
                agent_id,
                scope_uid or "agent-level",
                e,
            )
            continue

    return total_merged


# Throttle for the reactive self-heal pass: at most one automatic
# maintenance per agent per this window, enforced via a Redis SET NX EX key.
# The hourly scheduled maintenance still runs on its own cadence — this only
# bounds the extra LLM/DB work triggered by prompt-cap breaches, which can
# fire on every assignment run of a bloated agent.
_AUTO_HEAL_THROTTLE_SECONDS = 21_600  # 6 hours

# When a self-heal pass fails after acquiring the throttle, the key's TTL is
# shortened to this value so a transient failure (DB hiccup, LLM outage) can
# retry soon — still bounded per agent, so a persistent failure cannot turn
# into a per-run retry storm.
_AUTO_HEAL_FAILURE_RETRY_SECONDS = 1_800  # 30 minutes


async def auto_heal_agent_memory(agent_id: str, *, scope_id: str | None = None) -> bool:
    """Self-healing memory maintenance for one agent, triggered reactively.

    Called (fire-and-forget) when an agent's combined memory block exceeds
    the assignment prompt memory budget (``_MAX_ASSIGNMENT_MEMORY_CHARS`` in
    ``assignment_helpers``) so the platform remediates memory bloat
    automatically instead of logging instructions no user ever sees.
    Performs:

    1. The same persistent-tier hygiene as the hourly scheduler for this
       agent only: prune, cap enforcement, fact consolidation, re-embedding.
    2. A compression pass on the Redis medium-term summary for *scope_id*
       (an assignment run passes the lane it reads; ``None`` is the
       agent-level lane) when it exceeds the prompt cap.

    Throttled per agent and lane via Redis (``_AUTO_HEAL_THROTTLE_SECONDS``)
    so a burst of runs on a bloated lane cannot stack LLM calls.  Opens its own
    DB session — safe to schedule from any context.

    Returns ``True`` when a maintenance pass actually ran (throttle acquired).
    """
    from app.core.redis import redis_client

    # Per lane: one user's heal never holds another user's lane truncated.
    throttle_key = f"agent_mem_autoheal:{agent_id}" + (f":scope:{scope_id}" if scope_id else "")
    try:
        redis = await redis_client()
        acquired = await redis.set(
            throttle_key,
            "1",
            nx=True,
            ex=_AUTO_HEAL_THROTTLE_SECONDS,
        )
        if not acquired:
            return False
    except Exception as e:
        # Redis being unavailable must not turn into an unthrottled LLM
        # storm — skip the pass, the hourly scheduler still covers hygiene.
        logger.warning(
            "Memory self-heal throttle check failed for agent %s — skipping: %s",
            agent_id,
            format_exception_message(e),
        )
        return False

    logger.info("Running automatic memory self-heal for agent %s", agent_id)

    from app.core.database import async_session as _session_factory

    try:
        async with _session_factory() as db:
            pruned = await prune_old_memories(db, agent_id=agent_id)
            evicted = await enforce_memory_cap(db, agent_id=agent_id)
            consolidated = await consolidate_agent_facts(db, agent_id=agent_id)
            reembedded = await reembed_stale_memories(db, agent_id=agent_id)

            # Redis medium-term compression needs an LLM — resolve it via the
            # standard chain (platform default) while the session is open.
            compressed = False
            try:
                from app.llm.router import get_llm_for_user
                from app.services.agent_memory import compress_medium_term_memory

                # For the scope's own person - no one for the agent-level
                # scope - never for whichever run triggered the self-heal
                # (inherited through ``create_task``): that person's LLM
                # provider exclusions must neither route nor block shared
                # maintenance. Facts and re-embedding above scope
                # themselves the same way.
                with acting_for(scope_id):
                    llm, model = await get_llm_for_user(db, None)
                    compressed = await compress_medium_term_memory(
                        agent_id, llm, scope_id=scope_id, model=model
                    )
            except Exception as e:
                logger.warning(
                    "Medium-term compression skipped during self-heal for "
                    "agent %s: %s",
                    agent_id,
                    format_exception_message(e),
                )

            await db.commit()

        logger.info(
            "Memory self-heal complete for agent %s: %d pruned, %d evicted, "
            "%d consolidated, %d re-embedded, medium-term compressed=%s",
            agent_id,
            pruned,
            evicted,
            consolidated,
            reembedded,
            compressed,
        )
        return True
    except Exception as e:
        logger.warning(
            "Memory self-heal failed for agent %s: %s",
            agent_id,
            format_exception_message(e),
        )
        # The throttle was acquired but remediation did not happen — shorten
        # the key's TTL so a transient failure can retry soon instead of
        # leaving the agent truncating memory for the full 6h window.
        try:
            await redis.expire(throttle_key, _AUTO_HEAL_FAILURE_RETRY_SECONDS)
        except Exception:
            logger.debug(
                "Could not shorten self-heal throttle TTL for agent %s",
                agent_id,
            )
        return False


async def run_memory_maintenance(db: AsyncSession) -> dict:
    """Run all memory hygiene tasks across all agents.

    Called periodically by the autonomous scheduler.  Agents are processed
    concurrently (bounded by a semaphore) using separate DB sessions so that
    one slow agent doesn't block maintenance for others.
    """
    import asyncio

    from app.core.database import async_session as _session_factory

    summary = {
        "pruned": 0,
        "evicted": 0,
        "consolidated": 0,
        "reembedded": 0,
        "agents_processed": 0,
    }

    agent_ids_q = await db.execute(
        select(AgentMemoryEntry.agent_id)
        .where(AgentMemoryEntry.agent_id.isnot(None))
        .distinct()
    )
    agent_ids = [str(row[0]) for row in agent_ids_q.all()]

    if not agent_ids:
        return summary

    _sem = asyncio.Semaphore(5)

    async def _maintain_one(aid: str) -> tuple[int, int, int, int]:
        async with _sem, _session_factory() as agent_db:
            pruned = await prune_old_memories(agent_db, agent_id=aid)
            evicted = await enforce_memory_cap(agent_db, agent_id=aid)
            consolidated = await consolidate_agent_facts(agent_db, agent_id=aid)
            reembedded = await reembed_stale_memories(agent_db, agent_id=aid)
            await agent_db.commit()
            return pruned, evicted, consolidated, reembedded

    tasks = []
    for aid in agent_ids:
        tasks.append(_maintain_one(aid))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    for aid, result in zip(agent_ids, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning("Memory maintenance failed for agent %s: %s", aid, result)
            continue
        pruned, evicted, consolidated, reembedded = result
        summary["pruned"] += pruned
        summary["evicted"] += evicted
        summary["consolidated"] += consolidated
        summary["reembedded"] += reembedded
        summary["agents_processed"] += 1

    total_actions = (
        summary["pruned"]
        + summary["evicted"]
        + summary["consolidated"]
        + summary["reembedded"]
    )
    if total_actions:
        logger.info(
            "Memory maintenance complete: %d pruned, %d evicted, %d consolidated, "
            "%d re-embedded across %d agents",
            summary["pruned"],
            summary["evicted"],
            summary["consolidated"],
            summary["reembedded"],
            summary["agents_processed"],
        )

    return summary
