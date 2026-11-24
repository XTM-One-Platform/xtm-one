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

"""Agent memory store/search tool builders.

Moved verbatim from the original ``app/services/builtin_tools.py`` module
as part of the package split (mechanical refactor - no behavior change).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.services.leaked_reasoning import strip_leading_reasoning
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger


async def _resolve_provider_model() -> tuple | None:
    """Resolve ``(embedding_provider, model)`` in a short-lived DB session.

    The persistent-memory helpers embed text via an HTTP call. If the provider
    is resolved on the same session that later runs the vector query/write, that
    session's pooled connection stays checked out across the embed round-trip.
    Resolving it here (session closed before the embed) and passing the result
    as ``_provider_model`` keeps the connection free during the embed, so one
    pooled connection is not pinned per in-flight memory search/store under
    load. Returns ``None`` on failure so callers fall back to the legacy path.
    """
    from app.core.database import async_session
    from app.llm.router import get_embedding_provider

    try:
        async with async_session() as _prov_db:
            provider, model, _provider_id = await get_embedding_provider(_prov_db)
        return (provider, model)
    except Exception:
        return None


# The screen runs after the write, off the tool's path, and bounds itself: at
# most ``_MEMORY_SCREEN_CONCURRENCY`` evaluate at once in a process, at most
# ``_MEMORY_SCREEN_MAX_PENDING`` wait for a slot, and each evaluation takes at
# most ``_MEMORY_SCREEN_TIMEOUT_SECS`` (main's Nova engine asks every ``llm:``
# condition of the rule set in turn, with no bound of its own).
_MEMORY_SCREEN_TIMEOUT_SECS = 10.0
_MEMORY_SCREEN_CONCURRENCY = 4
_MEMORY_SCREEN_MAX_PENDING = 256
# How long shutdown waits for pending screens before cancelling them.
_MEMORY_SCREEN_DRAIN_SECS = 5.0

# ``AuditLog.user_id`` is NOT NULL, and a write no user made (an unattended
# assignment run, an autonomous run's agent-level write) has no user for
# ``log_security_event`` to resolve: its match is recorded under this system
# actor, anchored on the agent's owner.
_MEMORY_GUARD_AUDIT_ACTOR = "nova_guard"


@dataclass
class _ScreenPool:
    loop: asyncio.AbstractEventLoop
    slots: asyncio.Semaphore
    pending: set[asyncio.Task] = field(default_factory=set)


_screen_pool: _ScreenPool | None = None


def _current_screen_pool() -> _ScreenPool:
    """The screens of the running event loop (a semaphore binds to one loop)."""
    global _screen_pool
    loop = asyncio.get_running_loop()
    if _screen_pool is None or _screen_pool.loop is not loop:
        _screen_pool = _ScreenPool(loop, asyncio.Semaphore(_MEMORY_SCREEN_CONCURRENCY))
    return _screen_pool


async def _agent_owner_id(db: AsyncSession, agent_id: str) -> uuid.UUID | None:
    from sqlalchemy import select

    from app.core.utils import safe_uuid
    from app.models.agent import Agent
    from app.models.user import User

    agent_uuid = safe_uuid(agent_id)
    if agent_uuid is None:
        return None
    return (
        await db.execute(
            select(User.id).join(Agent, Agent.user_id == User.id).where(Agent.id == agent_uuid)
        )
    ).scalar_one_or_none()


async def _audit_memory_write_match(
    db: AsyncSession,
    *,
    user_id: str,
    agent_id: str,
    owner_id: uuid.UUID | None,
    details: dict[str, Any],
) -> None:
    """Record a Nova match on a memory write, with or without an acting user."""
    from app.core.utils import safe_uuid
    from app.services.audit import log_security_event, log_system_audit

    event = {
        "action": "security_alert",
        "entity_type": "security",
        "entity_id": agent_id,
        "entity_name": f"agent:{agent_id}",
        "summary": "Nova rule match",
        "details": details,
    }
    if safe_uuid(user_id) is not None:
        # A user deleted since the write is anchored on the owner too.
        await log_security_event(
            db, user_id=user_id, system_actor=_MEMORY_GUARD_AUDIT_ACTOR, anchor_agent_id=agent_id, **event
        )
        return
    if owner_id is None:
        logger.warning(
            "Nova rule match on a memory write of agent %s, which has no owner "
            "to anchor its audit entry on",
            agent_id,
        )
        return
    await log_system_audit(
        db, actor=_MEMORY_GUARD_AUDIT_ACTOR, anchor_user_id=owner_id, **event
    )


async def _screen_memory_write(
    content: str, *, agent_id: str, user_id: str, source: str
) -> None:
    """Evaluate a stored memory *content* with the connected Nova guard.

    A stored memory is re-read into the system prompt on every later run, so
    a payload that lands there keeps acting with no further attacker input.
    Runs only while the Nova integration is connected, like the chat message
    check, and audits a match: under the acting user, or, for a write no user
    made, under the ``system:nova_guard`` actor anchored on the agent's owner
    (a WARNING without the content when the agent has none). Three sessions,
    none of them holding a connection while the engine waits on a model: the
    Nova switch and the owner are read first, the engine runs on a
    :class:`~app.core.database.StatementScopedSession`, the audit row is
    written last.
    """
    from app.core.database import StatementScopedSession, async_session
    from app.core.utils import safe_uuid
    from app.services.integration_state import any_provider_connected
    from app.services.nova_engine import evaluate_prompt
    from app.services.nova_guard import worst_action

    async with async_session() as read_db:
        if not await any_provider_connected(read_db, "nova"):
            return
        owner_id = (
            await _agent_owner_id(read_db, agent_id) if safe_uuid(user_id) is None else None
        )

    async with async_session() as guard_db:
        matches = await evaluate_prompt(StatementScopedSession(guard_db), content, user_id=user_id or None)
    if not matches:
        return

    async with async_session() as audit_db:
        await _audit_memory_write_match(
            audit_db,
            user_id=user_id,
            agent_id=agent_id,
            owner_id=owner_id,
            details={
                "source": "nova_guard",
                "surface": "memory_write",
                "memory_source": source,
                "rules": [m.rule_name for m in matches],
                "categories": sorted({m.category for m in matches if m.category}),
                "agent_id": agent_id,
                "rule_action": worst_action(matches),
                "action": "allowed",
                "blocked": False,
            },
        )
        await audit_db.commit()


async def _run_memory_screen(pool: _ScreenPool, content: str, **ids: str) -> None:
    try:
        async with pool.slots:
            await asyncio.wait_for(
                _screen_memory_write(content, **ids), timeout=_MEMORY_SCREEN_TIMEOUT_SECS
            )
    except Exception as exc:
        logger.warning(
            "Memory write screening failed (agent=%s, source=%s): %s",
            ids["agent_id"],
            ids["source"],
            type(exc).__name__,
        )


def _spawn_memory_screen(
    content: str, *, agent_id: str, user_id: str, source: str
) -> asyncio.Task | None:
    """Screen a memory write after it was stored, as a tracked background task.

    The write and the tool's answer never wait on it. The task is held until it
    ends (the loop keeps only a weak reference), never raises, and is drained
    at shutdown by :func:`drain_memory_screens`.
    """
    pool = _current_screen_pool()
    if len(pool.pending) >= _MEMORY_SCREEN_MAX_PENDING:
        logger.warning(
            "Memory write of agent %s not screened: %d screens already pending",
            agent_id,
            len(pool.pending),
        )
        return None
    task = asyncio.create_task(
        _run_memory_screen(pool, content, agent_id=agent_id, user_id=user_id, source=source),
        name=f"memory-screen:{agent_id}",
    )
    pool.pending.add(task)
    task.add_done_callback(pool.pending.discard)
    return task


async def drain_memory_screens(timeout: float | None = _MEMORY_SCREEN_DRAIN_SECS) -> None:
    """Wait for the pending memory write screens, then cancel what is left.

    For shutdown, once the producers of memory writes are stopped
    (``timeout=None`` waits for all of them). A screen started while it waits
    is waited for too, within the same deadline. Never raises: a screen that
    failed has already logged, and one cancelled here leaves its write stored
    unscreened, as a timeout does.
    """
    loop = asyncio.get_running_loop()
    pool = _screen_pool
    if pool is None or pool.loop is not loop:
        return
    deadline = None if timeout is None else loop.time() + timeout
    while pool.pending:
        remaining = None if deadline is None else deadline - loop.time()
        if remaining is not None and remaining <= 0:
            break
        await asyncio.wait(set(pool.pending), timeout=remaining)
    left = set(pool.pending)
    if not left:
        return
    for task in left:
        task.cancel()
    await asyncio.gather(*left, return_exceptions=True)
    logger.warning("Cancelled %d pending memory write screen(s) at shutdown", len(left))


# ── Agent Memory Store ────────────────────────────────────────────────────────

_BASE_MEMORY_TYPES = (
    "Category: 'style' for communication patterns, 'fact' for specific facts, "
    "'learned' for general observations, 'state' for what an object that "
    "changes (a document, a deck, a ticket) looks like now - give it a "
    "subject_key so the next observation of that object replaces it."
)
_TRADECRAFT_MEMORY_TYPE_DOC = (
    " Also 'tradecraft' for a durable lesson about how a platform behaves or a "
    "mistake not to repeat. Tradecraft is shared with every user of this agent "
    "and survives operations that clear the agent's other memory, so it must be "
    "generalizable and must never name a specific target - no host, address, "
    "URL, entity id, or person. A host, address, URL, entity id or email is "
    "detected and refused automatically; a person named in prose is not "
    "detectable, so keeping people out is on you. Never store a lesson that "
    "says skip payload-based contracts because they produce none, or prefer a "
    "native injector to avoid a fork - fork them and add parsers instead."
)


def _memory_type_description(allow_tradecraft: bool) -> str:
    """Describe the memory_type enum, mentioning tradecraft only when usable."""
    if allow_tradecraft:
        return _BASE_MEMORY_TYPES + _TRADECRAFT_MEMORY_TYPE_DOC
    return _BASE_MEMORY_TYPES


def build_memory_store_tool(
    agent_id: str,
    user_id: str,
    *,
    source: str = "tool",
    allow_tradecraft: bool = False,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build the agent memory store tool.

    Allows agents to explicitly persist memories (style patterns, learned
    facts, important observations) to their durable PostgreSQL memory with
    vector embeddings for later semantic retrieval via ``search_memory``.
    The same executor carries ``invalidate_memory``, which retires entries
    that no longer hold: retiring is writing the same memory.

    Args:
        source: Origin label stored with each memory entry (e.g. ``"tool"``
            for explicit agent tool calls, ``"assignment"`` for assignment engine).
        allow_tradecraft: Whether this agent may write the durable, AGENT-LEVEL
            ``tradecraft`` class.  Off by default because such a row is
            stored unscoped and is injected into every other user's conversation
            with the agent - on a shared or company-managed agent that turns an
            ordinary memory write into a cross-user publish.  Gate it on
            ``is_tradecraft_writer_agent``, never on caller preference.
    """

    async def _store_memory(
        content: str,
        memory_type: str = "learned",
        subject_key: str | None = None,
        supersedes: str | None = None,
    ) -> str:
        from app.core.database import async_session
        from app.services.persistent_memory import (
            TRADECRAFT_MEMORY_TYPE,
            TradecraftNotAuthorizedError,
            TradecraftRejectedError,
            normalize_memory_type,
            normalize_subject_key,
            persist_memory,
        )

        # The class is model-supplied and every check on it is an exact match,
        # so it is normalised once here - the same way the store normalises it.
        # Comparing the raw value would let a capitalised "Tradecraft" walk past
        # the authorization gate below AND past the target guard in the store,
        # landing an unguarded, uncapped, never-pruned row instead of either the
        # refusal or the guarded write the agent asked for.
        stored_type = normalize_memory_type(memory_type)

        # ``persist_memory`` refuses it with the same ``None`` as a dedup skip,
        # which this tool would report as a successful write.
        if not strip_leading_reasoning(content or "").strip():
            return "Rejected: the content is only a reasoning block. Store the fact itself."

        if stored_type == TRADECRAFT_MEMORY_TYPE and not allow_tradecraft:
            # Refuse rather than downgrade to a user-scoped write: silently
            # storing it under another type would leave the agent believing it
            # had published a durable cross-user lesson.
            return (
                "Rejected: this agent cannot write tradecraft memory here. That "
                "class is agent-level and shared with every user, so it is "
                "limited to designated agents running inside an authorized run. "
                "Use 'learned' or 'fact' instead."
            )

        subject_key = normalize_subject_key(subject_key)
        supersedes = (supersedes or "").strip() or None
        superseded: list[str] = []
        try:
            provider_model = await _resolve_provider_model()
            async with async_session() as mem_db:
                entry = await persist_memory(
                    mem_db,
                    agent_id=agent_id,
                    content=content,
                    memory_type=memory_type,
                    source=source,
                    user_id=user_id if user_id else None,
                    subject_key=subject_key,
                    supersedes=supersedes,
                    superseded_out=superseded,
                    _provider_model=provider_model,
                )
                await mem_db.commit()
            if entry is not None:
                # What was stored (``persist_memory`` sanitises it) is what a
                # later prompt re-reads, so it is what is screened.
                _spawn_memory_screen(
                    entry.content, agent_id=agent_id, user_id=user_id, source=source
                )
            if stored_type == TRADECRAFT_MEMORY_TYPE:
                # ``persist_memory`` returns None when semantic dedup matched an
                # existing entry. Reporting that as a fresh write would leave the
                # agent believing it had added something new.
                if entry is None:
                    return (
                        "Already known - an equivalent tradecraft entry exists, so "
                        "nothing was added."
                    )
                return (
                    "Tradecraft recorded. It is shared across every operator and "
                    "survives the end of this run."
                )
            return f"Memory stored successfully (type: {stored_type})." + _supersession_note(
                entry, stored_type, subject_key, supersedes, superseded
            )
        except (TradecraftRejectedError, TradecraftNotAuthorizedError) as rejected:
            # A refusal the agent can act on, not a failure: surfacing the reason
            # verbatim is what lets it rephrase into a generalizable lesson (or
            # fall back to another type) instead of silently losing the write.
            return str(rejected)
        except Exception as e:
            logger.error("Failed to store memory: %s", format_exception_message(e), exc_info=True)
            return "Failed to store memory. Please try again later."

    async def _invalidate_memory(
        entry_id: str | None = None, subject_key: str | None = None
    ) -> str:
        from app.core.database import async_session
        from app.services.persistent_memory import (
            may_manage_shared_memory,
            normalize_subject_key,
            retire_memories,
        )

        entry_id = (entry_id or "").strip() or None
        subject = normalize_subject_key(subject_key)
        if entry_id is None and subject is None:
            return (
                "Rejected: name what no longer holds - entry_id (an id "
                "search_memory lists) or subject_key (every entry on that subject)."
            )
        try:
            async with async_session() as mem_db:
                # The person who manages the agent's memory (its owner, an
                # administrator) retires its shared pool too, as the memory
                # screen lets them delete it.
                shared = await may_manage_shared_memory(mem_db, agent_id=agent_id, user_id=user_id or None)
                outcome = await retire_memories(
                    mem_db,
                    agent_id=agent_id,
                    user_id=user_id if user_id else None,
                    entry_id=entry_id,
                    subject_key=subject,
                    include_shared=shared,
                )
                await mem_db.commit()
        except Exception as e:
            logger.error("Failed to retire memory: %s", format_exception_message(e), exc_info=True)
            return "Failed to retire memory. Please try again later."
        return _retirement_answer(outcome, entry_id=entry_id, subject_key=subject)

    executor = CallableToolExecutor()
    executor.register("store_memory", _store_memory)
    executor.register("invalidate_memory", _invalidate_memory)

    definition = ToolDefinition(
        name="store_memory",
        description=(
            "Store a piece of information in your persistent memory for future "
            "reference. Use this to save learned patterns (writing style, "
            "communication preferences), important facts, or any observation you "
            "want to remember across sessions. Each entry is stored with a vector "
            "embedding for semantic search via search_memory. Store individual "
            "observations as separate entries for best searchability. For "
            "something that changes, store memory_type 'state' with a "
            "subject_key: the newest state entry on a subject replaces the older "
            "ones, which stay searchable but leave your context."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "The information to remember. Be specific and "
                        "self-contained — each entry should make sense on its own."
                    ),
                },
                "memory_type": {
                    "type": "string",
                    # Only advertised to an agent that may actually use it: naming
                    # the class to every other agent would invite a write the tool
                    # then has to refuse.
                    "description": _memory_type_description(allow_tradecraft),
                    "default": "learned",
                },
                "subject_key": {
                    "type": "string",
                    "description": (
                        "Optional. The object this entry is about, as a stable "
                        "key: 'presentation:DECK_ID', 'doc:DOC_ID', "
                        "'ticket:TICKET_ID'. Reuse exactly the same key for the "
                        "same object."
                    ),
                },
                "supersedes": {
                    "type": "string",
                    "description": (
                        "Optional. The id of an entry this one replaces, as "
                        "search_memory lists it, whatever its type. The old "
                        "entry is kept, flagged superseded."
                    ),
                },
            },
            "required": ["content"],
        },
        source="builtin:memory_store",
    )

    invalidate_definition = ToolDefinition(
        name="invalidate_memory",
        description=(
            "Retire memory entries that no longer hold: an observation of an "
            "object that has changed since, a fact that is no longer true. Name "
            "one entry (entry_id, as search_memory lists it) or a subject "
            "(subject_key: every active entry on it). Your own entries are "
            "retired, and the agent's shared entries too when the person you "
            "act for manages the agent's memory (its owner, an administrator). "
            "A retired entry is kept and searchable, flagged superseded, and "
            "no longer reaches your context. To record what replaces it, call "
            "store_memory with supersedes instead."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "entry_id": {
                    "type": "string",
                    "description": "The id of the entry to retire, as search_memory lists it.",
                },
                "subject_key": {
                    "type": "string",
                    "description": "Retire every active entry about this subject, e.g. 'doc:X'.",
                },
            },
        },
        source="builtin:memory_store",
    )

    return [definition, invalidate_definition], executor


def _supersession_note(
    entry,
    stored_type: str,
    subject_key: str | None,
    supersedes: str | None,
    superseded: list[str],
) -> str:
    """What a write naming a subject or an entry to replace did; empty for any other write."""
    from app.core.utils import safe_uuid
    from app.services.persistent_memory import STATE_MEMORY_TYPE

    if entry is None or not (subject_key or supersedes or stored_type == STATE_MEMORY_TYPE):
        return ""
    note = f" Entry id: {entry.id}"
    note += f", subject_key: {subject_key}." if subject_key else "."
    if superseded:
        noun = "entry" if len(superseded) == 1 else "entries"
        note += (
            f" It supersedes {len(superseded)} earlier {noun} ({', '.join(superseded)}),"
            " kept and searchable but no longer in your context."
        )
    if supersedes and str(safe_uuid(supersedes)) not in superseded:
        note += (
            f" Entry {supersedes[:64]} was not superseded: it is not an active entry "
            "of the memory this session writes."
        )
    if stored_type == STATE_MEMORY_TYPE and not subject_key:
        note += " Without a subject_key, no later state entry can supersede it."
    return note


def _retirement_answer(outcome, *, entry_id: str | None, subject_key: str | None) -> str:
    if outcome.retired:
        count = len(outcome.retired)
        noun = "entry" if count == 1 else "entries"
        answer = (
            f"Retired {count} memory {noun} ({', '.join(outcome.retired)}). Retired "
            "entries stay searchable, flagged superseded, and no longer reach your context."
        )
    else:
        named = " and ".join(
            part
            for part in (
                f"entry_id {entry_id[:64]}" if entry_id else "",
                f"subject_key {subject_key}" if subject_key else "",
            )
            if part
        )
        answer = (
            "Nothing retired: no active entry of the memory this session writes "
            f"matches {named}."
        )
    if outcome.left_shared:
        noun, verb = ("entry", "stays") if outcome.left_shared == 1 else ("entries", "stay")
        answer += (
            f" {outcome.left_shared} matching {noun} of the agent's shared memory "
            f"{verb} active: only the agent's owner or an administrator retires "
            "the shared memory (from their session, or the agent's memory screen). "
            "A newer state entry on the same subject is the one you are shown."
        )
    return answer


# ── Agent Memory Search ──────────────────────────────────────────────────────


def build_memory_search_tool(
    agent_id: str,
    user_id: str,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build the agent memory semantic-search tool.

    Allows agents to search their own persistent memory (accumulated knowledge,
    extracted facts, style notes) using natural language queries.
    """

    async def _search_memory(query: str, limit: int = 5) -> str:
        from app.core.database import async_session
        from app.llm.provider_access import ProviderNotUsableError
        from app.services.persistent_memory import search_agent_memories

        provider_model = await _resolve_provider_model()
        try:
            async with async_session() as mem_db:
                results = await search_agent_memories(
                    mem_db,
                    agent_id=agent_id,
                    query=query,
                    user_id=user_id,
                    limit=limit,
                    _provider_model=provider_model,
                )
        except ProviderNotUsableError as refused:
            # The embedding provider excludes this person: say so,
            # never "No relevant memories found".
            return str(refused)
        if not results:
            return "No relevant memories found."

        from app.services.content_safety import wrap_memory

        # The rows the system prompt fences come back here too.
        answer = wrap_memory("\n\n".join(_search_row(r) for r in results))
        notes = _search_notes(results)
        return f"{answer}\n\n{notes}" if notes else answer

    executor = CallableToolExecutor()
    executor.register("search_memory", _search_memory)

    definition = ToolDefinition(
        name="search_memory",
        description=(
            "Search your own persistent memory for previously learned information, "
            "facts, preferences, and accumulated knowledge. Use this when you need "
            "to recall something from past conversations or assignment executions that "
            "may not be in the current conversation context. Each result carries its "
            "id, stored_at, subject_key and superseded=true/false; superseded entries "
            "no longer hold and are listed after the active ones."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What are you trying to remember? Describe the information you're looking for.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of results to return (default 5)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
        source="builtin:memory_search",
        reads_own_configuration=True,
    )

    return [definition], executor


def _search_row(row: dict) -> str:
    """One ``search_memory`` result: its type, what it is and whether it holds, then its text."""
    fields = [
        f"id={row['id']}" if row.get("id") else "",
        f"stored_at={row.get('stored_at') or 'unknown'}",
        f"subject_key={row.get('subject_key') or 'none'}",
        f"superseded={'true' if row.get('superseded') else 'false'}",
        f"superseded_by={row['superseded_by']}" if row.get("superseded_by") else "",
        f"score={row['score']}",
    ]
    content = strip_leading_reasoning(row["content"])
    return f"[{row['memory_type']}] ({' '.join(field for field in fields if field)}) {content}"


def _search_notes(rows: list[dict]) -> str:
    """The platform's reading of the rows, outside the memory they quote."""
    from app.services.persistent_memory import STATE_GUIDANCE, STATE_MEMORY_TYPE

    notes = []
    if any(row.get("superseded") for row in rows):
        notes.append("superseded=true: the entry no longer holds; it is listed for history.")
    if any(row.get("memory_type") == STATE_MEMORY_TYPE and not row.get("superseded") for row in rows):
        notes.append(STATE_GUIDANCE)
    return "\n".join(notes)
