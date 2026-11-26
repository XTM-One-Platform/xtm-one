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

"""Agent memory endpoints – list, search, update & clear persistent memory.

Provides access to an agent's durable memory records stored in PostgreSQL
with vector embeddings.

For **company-managed agents**, non-admin users receive only aggregate
statistics (entry counts by type) — the actual memory content is restricted
to administrators to prevent leaking personal information or sensitive
organizational knowledge that was accumulated across multiple users.

Write operations (update, delete, clear, maintain) require management
permission (``can_manage``): the owner, an admin, or a ``config`` /
``config_with_chats`` grantee of a private agent; an admin of a
company-managed one.

Import (``POST /{agent_id}/memory``) recreates exported entries for a
caller who can manage the agent, in the scopes that caller may read.
"""

import logging
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_ai_enabled
from app.api.v1.helpers import (
    can_manage,
    get_user_agent,
    get_user_group_ids,
    parse_path_id,
)
from app.core.database import get_db
from app.models.agent import Agent
from app.models.agent_memory_entry import AgentMemoryEntry
from app.models.user import User
from app.services.audit import log_audit
from app.services.persistent_memory import memory_content_restricted, memory_full_visibility

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class UpdateMemoryRequest(BaseModel):
    content: str


class ImportMemoryEntry(BaseModel):
    """One entry of an import, the fields the list route returns."""

    content: str
    memory_type: str = "fact"
    subject_key: str | None = None
    source: str | None = None
    user_id: uuid.UUID | None = None
    created_at: datetime | None = None


class ImportMemoryRequest(BaseModel):
    # The list route's page cap: what one GET returns one POST takes back.
    entries: list[ImportMemoryEntry] = Field(min_length=1, max_length=200)


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------


def _is_memory_restricted(agent: Agent, user: User) -> bool:
    """Return True when memory content should be hidden from this user.

    Company-managed agents restrict memory access to admins only because the
    memory may contain data accumulated from conversations with other users.
    The rule is ``persistent_memory.memory_content_restricted``, which the
    memory copy of a clone applies too.
    """
    return memory_content_restricted(agent, user)


def _memory_scope_filter(agent: Agent, user: User):
    """Return a SQLAlchemy filter limiting visible memory rows, or ``None``.

    Mirrors the runtime search semantics (``search_agent_memories``): a user
    may only see agent-level entries (``user_id IS NULL``) plus their own
    user-scoped entries.  Without this filter, a group member of a shared
    agent could read other members' per-user scoped facts (MEM-1).

    Full visibility (``None`` — no additional filter) is granted only when:

    - the user **owns** the agent (private agents accumulate memory for the
      owner; group-shared private agents still belong to the owner), or
    - the user is an **admin** and the agent is **company-managed** (admins
      are the designated curators of company-managed agent memory).

    Conservative choice: for group-shared *private* agents, admins who do
    not own the agent get the same scoping as regular group members — admin
    role does not imply access to another user's personal memory on an
    agent the admin does not manage.

    The rule is ``persistent_memory.memory_full_visibility``, which the memory
    copy of a clone applies too.
    """
    if memory_full_visibility(agent, user):
        return None
    return or_(
        AgentMemoryEntry.user_id.is_(None),
        AgentMemoryEntry.user_id == user.id,
    )


_NOT_A_MANAGER = "Only a manager of this agent can change its memory."


async def _managed_agent(db: AsyncSession, user: User, agent_id: str) -> Agent:
    """The agent *user* may change the memory of, else 404 (unseen) or 403.

    The rule is ``can_manage``: the owner, an admin, or a ``config`` /
    ``config_with_chats`` grantee of a private agent; an admin of a
    company-managed one. A ``read_only`` grantee sees the memory and changes
    none of it.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    agent = await get_user_agent(db, user.id, agent_id, user_group_ids, is_admin=user.is_admin)
    if not can_manage(user, agent, user_group_ids):
        raise HTTPException(status_code=403, detail=_NOT_A_MANAGER)
    return agent


def _to_response(entry: AgentMemoryEntry, *, restricted: bool = False) -> dict:
    """Convert a memory entry to an API response dict.

    ``superseded`` is the stored flag: retired, or replaced by a newer
    entry of its own pool. The search adds the per-reader rule on top.
    """
    return {
        "id": str(entry.id),
        "agent_id": str(entry.agent_id),
        "user_id": str(entry.user_id) if entry.user_id else None,
        "content": "" if restricted else entry.content,
        "memory_type": entry.memory_type,
        "source": entry.source,
        "subject_key": None if restricted else entry.subject_key,
        "superseded": entry.superseded_at is not None,
        "superseded_at": entry.superseded_at.isoformat() if entry.superseded_at else None,
        "superseded_by_id": str(entry.superseded_by_id) if entry.superseded_by_id else None,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
        "restricted": restricted,
    }


@router.get("/{agent_id}/memory")
async def list_agent_memories(
    agent_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    memory_type: Annotated[list[str] | None, Query(description="One or several types (repeated)")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    source: Annotated[list[str] | None, Query(description="One or several sources (repeated)")] = None,
    state: Annotated[
        Literal["current", "superseded"] | None,
        Query(description="current: still holds; superseded: retired or replaced"),
    ] = None,
    q: Annotated[str | None, Query(max_length=200, description="Words the content or subject holds")] = None,
    sort: Annotated[Literal["date", "type"], Query(description="Order by creation date or type")] = "date",
    direction: Annotated[
        Literal["asc", "desc"], Query(description="Order direction; newest first breaks ties")
    ] = "desc",
):
    """List persistent memory entries for an agent.

    For company-managed agents, non-admin users receive entries with
    ``content`` redacted (empty string) — they can see metadata
    (type, source, timestamp) and aggregate counts but not the actual
    memory text.

    Entries are additionally scoped per user (see ``_memory_scope_filter``):
    non-owners only see agent-level entries plus their own user-scoped
    entries, mirroring the ``/memory/search`` semantics.

    Returns ``{ entries: [...], total_count: N }`` so the frontend can
    display accurate totals even when the result set is capped by *limit*.

    The Memory tab's data table pages, filters (type, source, state), searches
    (``q``, a text match that reaches no provider) and sorts through this route.
    ``q`` reads the content, so it is refused where the content is redacted.
    """
    agent = await get_user_agent(db, user.id, agent_id, is_admin=user.is_admin)
    restricted = _is_memory_restricted(agent, user)

    base_filter = [AgentMemoryEntry.agent_id == agent.id]
    scope_filter = _memory_scope_filter(agent, user)
    if scope_filter is not None:
        base_filter.append(scope_filter)
    if memory_type:
        base_filter.append(AgentMemoryEntry.memory_type.in_(memory_type))
    if source:
        base_filter.append(AgentMemoryEntry.source.in_(source))
    if state == "current":
        base_filter.append(AgentMemoryEntry.superseded_at.is_(None))
    elif state == "superseded":
        base_filter.append(AgentMemoryEntry.superseded_at.is_not(None))
    words = (q or "").strip()
    if words:
        if restricted:
            raise HTTPException(
                status_code=403,
                detail="Memory search is restricted to administrators for company-managed agents.",
            )
        pattern = f"%{words}%"
        base_filter.append(
            or_(
                AgentMemoryEntry.content.ilike(pattern),
                AgentMemoryEntry.subject_key.ilike(pattern),
            )
        )

    # Total count (before limit)
    count_result = await db.execute(
        select(func.count()).select_from(AgentMemoryEntry).where(*base_filter)
    )
    total_count = count_result.scalar() or 0

    # Paginated entries; newest first breaks ties whatever the order asked.
    newest = AgentMemoryEntry.created_at.desc()
    if sort == "type":
        kind = AgentMemoryEntry.memory_type
        order = [kind.asc() if direction == "asc" else kind.desc(), newest]
    else:
        order = [AgentMemoryEntry.created_at.asc() if direction == "asc" else newest]
    query = (
        select(AgentMemoryEntry)
        .where(*base_filter)
        .order_by(*order, AgentMemoryEntry.id)
        .limit(limit)
        .offset(offset)
    )

    result = await db.execute(query)
    entries = result.scalars().all()
    return {
        "entries": [_to_response(e, restricted=restricted) for e in entries],
        "total_count": total_count,
    }


#: Set on a memory search whose semantic half the embedding provider refused
#: to the caller; the body then holds the text matches alone.
SEMANTIC_SEARCH_REFUSED_HEADER = "X-Semantic-Search-Refused"


@router.get("/{agent_id}/memory/search")
async def search_agent_memory(
    agent_id: str,
    response: Response,
    query: str = Query(..., min_length=1),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(10, le=50),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Combined semantic + text search over an agent's persistent memory.

    Runs both pgvector cosine similarity search and a case-insensitive text
    search (ILIKE), then merges the results.  This ensures that searches for
    exact terms like proper nouns (e.g. "Samuel") still return matches even
    when semantic similarity is low.

    For company-managed agents, non-admin users are forbidden from searching
    memory content to prevent data leakage across users.

    When the embedding provider excludes the caller, the semantic half
    is refused and the text half - which reaches no provider - still answers:
    the refusal travels in the ``X-Semantic-Search-Refused`` header, so the
    keyword hits are never mistaken for a full search and never withheld.
    """
    agent = await get_user_agent(db, user.id, agent_id, is_admin=user.is_admin)

    if _is_memory_restricted(agent, user):
        raise HTTPException(
            status_code=403,
            detail="Memory search is restricted to administrators for company-managed agents.",
        )

    from app.llm.provider_access import ProviderNotUsableError
    from app.services.persistent_memory import (
        format_stored_at,
        search_agent_memories,
        superseded_for_reader,
    )

    # 1. Semantic (embedding) search - refused when the embedding provider
    # excludes the caller; the text search below still runs.
    try:
        semantic_results = await search_agent_memories(
            db,
            agent_id=str(agent.id),
            query=query,
            user_id=str(user.id),
            limit=limit,
        )
    except ProviderNotUsableError as refused:
        semantic_results = []
        response.headers[SEMANTIC_SEARCH_REFUSED_HEADER] = str(refused)

    # 2. Text (ILIKE) search — catches exact keywords that semantic misses
    scope_filters = [AgentMemoryEntry.agent_id == agent.id]
    scope_filters.append(
        (AgentMemoryEntry.user_id.is_(None)) | (AgentMemoryEntry.user_id == user.id)
    )
    superseded = superseded_for_reader(str(user.id))
    text_query = (
        select(AgentMemoryEntry, superseded.label("superseded"))
        .where(*scope_filters)
        .where(AgentMemoryEntry.content.ilike(f"%{query}%"))
        .order_by(superseded, AgentMemoryEntry.created_at.desc())
        .limit(limit)
    )
    text_result = await db.execute(text_query)
    text_entries = text_result.all()

    # 3. Merge results — semantic first, then text matches not already present
    seen_ids: set[str] = {r["id"] for r in semantic_results}
    merged = list(semantic_results)

    for e, is_superseded in text_entries:
        eid = str(e.id)
        if eid not in seen_ids:
            seen_ids.add(eid)
            merged.append(
                {
                    "id": eid,
                    "content": e.content,
                    "memory_type": e.memory_type,
                    "source": e.source,
                    "score": 0.0,  # text match — no embedding score
                    "match_type": "text",
                    "created_at": e.created_at.isoformat() if e.created_at else None,
                    "stored_at": format_stored_at(e.created_at),
                    "subject_key": e.subject_key,
                    "superseded": bool(is_superseded),
                    "superseded_by": str(e.superseded_by_id) if e.superseded_by_id else None,
                }
            )

    # Tag semantic results as well
    for r in semantic_results:
        if "match_type" not in r:
            r["match_type"] = "semantic"

    return merged[:limit]


# ---------------------------------------------------------------------------
# Write operations — update, delete entry, clear all
# ---------------------------------------------------------------------------


def has_full_memory_visibility(agent: Agent, user: User) -> bool:
    """True when *user* sees every memory entry of *agent*, every user's scope included."""
    return _memory_scope_filter(agent, user) is None


def import_scope_error(
    agent: Agent, user: User, user_ids: set[uuid.UUID]
) -> str | None:
    """Why *user* may not write entries scoped to *user_ids* on *agent*, or ``None``.

    The read rule of ``_memory_scope_filter``, applied to a write: a caller
    with full visibility writes any scope; any other manager writes the
    agent-level pool and their own scope only.
    """
    if has_full_memory_visibility(agent, user):
        return None
    if user_ids - {user.id}:
        return (
            "Only the agent's owner (or an admin, on a company-managed agent) "
            "can import entries scoped to another user."
        )
    return None


async def unknown_user_ids(db: AsyncSession, user_ids: set[uuid.UUID]) -> set[uuid.UUID]:
    """The ids of *user_ids* no user on this platform carries."""
    if not user_ids:
        return set()
    known = await db.execute(select(User.id).where(User.id.in_(user_ids)))
    return user_ids - set(known.scalars().all())


@router.post("/{agent_id}/memory")
async def import_agent_memories(
    agent_id: str,
    body: ImportMemoryRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Recreate exported persistent memory entries on an agent.

    For a migration or a restore: each entry keeps its ``created_at``,
    ``memory_type``, ``subject_key``, ``source`` and user scope, and a
    ``state`` entry supersedes the earlier one on its subject. At most 200
    entries per call. The caller must be able to manage the agent; a caller
    without full memory visibility imports only agent-level entries and
    their own. Every ``user_id`` must name a user of this platform.
    """
    from app.services.persistent_memory import (
        MemoryImportEntry,
        MemoryImportError,
        import_memories,
    )

    user_group_ids = await get_user_group_ids(db, user.id)
    agent = await get_user_agent(
        db, user.id, agent_id, user_group_ids, is_admin=user.is_admin
    )
    if not can_manage(user, agent, user_group_ids):
        raise HTTPException(
            status_code=403,
            detail="Only a manager of this agent can import its memory.",
        )
    user_ids = {e.user_id for e in body.entries if e.user_id is not None}
    scope_err = import_scope_error(agent, user, user_ids)
    if scope_err:
        raise HTTPException(status_code=403, detail=scope_err)
    unknown = await unknown_user_ids(db, user_ids)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown user_id: {', '.join(sorted(str(u) for u in unknown))}",
        )

    try:
        created = await import_memories(
            db,
            agent_id=agent.id,
            entries=[MemoryImportEntry(**e.model_dump()) for e in body.entries],
        )
    except MemoryImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await log_audit(
        db,
        user=user,
        action="import",
        entity_type="agent_memory",
        entity_id=agent.id,
        entity_name=agent.name,
        summary=f"Imported {len(created)} memory entries into agent '{agent.name}'",
        details={"count": len(created)},
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return {
        "imported": len(created),
        "entries": [_to_response(e) for e in created],
    }


@router.put("/{agent_id}/memory/{entry_id}")
async def update_memory_entry(
    agent_id: str,
    entry_id: str,
    body: UpdateMemoryRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Update the content of a single persistent memory entry.

    Requires management permission (``_managed_agent``).
    Entries outside the caller's memory scope (another user's scoped
    entries — see ``_memory_scope_filter``) return 404, so a non-owner
    can never modify another user's scoped entry.
    """
    agent = await _managed_agent(db, user, agent_id)

    entry_uuid = parse_path_id(entry_id, not_found_detail="Memory entry not found.")
    entry_filter = [
        AgentMemoryEntry.id == entry_uuid,
        AgentMemoryEntry.agent_id == agent.id,
    ]
    scope_filter = _memory_scope_filter(agent, user)
    if scope_filter is not None:
        entry_filter.append(scope_filter)
    result = await db.execute(select(AgentMemoryEntry).where(*entry_filter))
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Memory entry not found.")

    # Same null-byte guard as ``persist_memory``: asyncpg rejects
    # \x00 in TEXT columns, which would abort the flush below with a 500.
    from app.services.persistent_memory import _sanitize_memory_text

    new_content = _sanitize_memory_text(body.content)
    entry.content = new_content

    # Re-generate embedding for the updated content, stamping the model and
    # vector dimension so stale-embedding detection keeps working.
    try:
        from app.services.persistent_memory import embed_text_with_model

        embedding, embedding_model = await embed_text_with_model(db, new_content)
    except Exception:
        embedding, embedding_model = None, None
        logger.warning("Failed to regenerate embedding for memory entry %s", entry_id)

    if embedding:
        entry.embedding = embedding
        entry.embedding_model = embedding_model
        entry.embedding_dimensions = len(embedding)
    else:
        # The content changed but the re-embed failed: keeping the OLD
        # vector would make semantic search score the entry against stale
        # text, and the still-current provenance stamp would hide it from
        # the re-embed maintenance task.  Null everything so the entry is
        # consistently "stale" and recoverable by reembed_stale_memories.
        entry.embedding = None
        entry.embedding_model = None
        entry.embedding_dimensions = None

    await db.flush()

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="agent_memory",
        entity_id=entry.id,
        entity_name=f"Memory entry ({entry.memory_type})",
        summary=f"Updated memory entry for agent '{agent.name}'",
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return _to_response(entry)


@router.delete("/{agent_id}/memory/{entry_id}", status_code=204)
async def delete_memory_entry(
    agent_id: str,
    entry_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Delete a single persistent memory entry.

    Requires management permission (``_managed_agent``).
    Entries outside the caller's memory scope (another user's scoped
    entries — see ``_memory_scope_filter``) return 404, so a non-owner
    can never delete another user's scoped entry.
    """
    agent = await _managed_agent(db, user, agent_id)

    entry_uuid = parse_path_id(entry_id, not_found_detail="Memory entry not found.")
    entry_filter = [
        AgentMemoryEntry.id == entry_uuid,
        AgentMemoryEntry.agent_id == agent.id,
    ]
    scope_filter = _memory_scope_filter(agent, user)
    if scope_filter is not None:
        entry_filter.append(scope_filter)
    result = await db.execute(select(AgentMemoryEntry).where(*entry_filter))
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Memory entry not found.")

    await db.execute(delete(AgentMemoryEntry).where(AgentMemoryEntry.id == entry.id))
    await db.flush()
    # A deleted entry is not restorable by a consolidation undo (#8258).
    from app.services.health.memory_history import purge_entry_consolidations

    await purge_entry_consolidations(db, entry.id)

    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="agent_memory",
        entity_id=entry.id,
        entity_name=f"Memory entry ({entry.memory_type})",
        summary=f"Deleted memory entry for agent '{agent.name}'",
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()


@router.get("/{agent_id}/memory/health")
async def get_agent_memory_health(
    agent_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return memory statistics and health indicators for an agent.

    Includes entry counts by type, staleness indicators, cap status,
    and hygiene configuration values.  Available to anyone who can see the agent.
    """
    agent = await get_user_agent(db, user.id, agent_id, is_admin=user.is_admin)

    from app.services.persistent_memory import get_memory_health

    return await get_memory_health(db, agent_id=str(agent.id))


@router.post("/{agent_id}/memory/maintain")
async def trigger_memory_maintenance(
    agent_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Trigger on-demand memory maintenance for a specific agent.

    Runs pruning, cap enforcement, and fact consolidation.
    Requires management permission.
    """
    agent = await _managed_agent(db, user, agent_id)

    from app.services.persistent_memory import (
        consolidate_agent_facts,
        enforce_memory_cap,
        prune_old_memories,
        reembed_stale_memories,
    )

    pruned = await prune_old_memories(db, agent_id=str(agent.id))
    evicted = await enforce_memory_cap(db, agent_id=str(agent.id))
    consolidated = await consolidate_agent_facts(db, agent_id=str(agent.id))
    reembedded = await reembed_stale_memories(db, agent_id=str(agent.id))

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="agent_memory",
        entity_id=agent.id,
        entity_name=agent.name,
        summary=(
            f"Memory maintenance for agent '{agent.name}': "
            f"{pruned} pruned, {evicted} evicted, {consolidated} consolidated, "
            f"{reembedded} re-embedded"
        ),
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return {
        "pruned": pruned,
        "evicted": evicted,
        "consolidated": consolidated,
        "reembedded": reembedded,
        "message": (
            f"Maintenance complete: {pruned} stale entries pruned, "
            f"{evicted} over-cap entries evicted, "
            f"{consolidated} facts consolidated, "
            f"{reembedded} stale embeddings re-embedded."
        ),
    }


@router.delete("/{agent_id}/memory", status_code=204)
async def clear_all_agent_memories(
    agent_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Delete all persistent memory entries for an agent.

    Requires management permission (``_managed_agent``).
    Also clears Redis short-term and medium-term memory caches.
    """
    agent = await _managed_agent(db, user, agent_id)

    from app.services.persistent_memory import clear_agent_memories

    # Tier 3: Delete all persistent (PostgreSQL) memory entries
    count = await clear_agent_memories(db, str(agent.id))

    # Tiers 1 & 2: Clear all Redis memory (short-term, medium-term, counter,
    # including per-user scoped keys).  Uses the canonical clear_memory()
    # which correctly matches the key format used by agent_memory.py.
    try:
        from app.services.agent_memory import clear_memory

        await clear_memory(str(agent.id))
    except Exception:
        logger.warning("Redis memory cleanup failed for agent %s", agent_id)

    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="agent_memory",
        entity_id=agent.id,
        entity_name=agent.name,
        summary=f"Cleared all memory ({count} entries) for agent '{agent.name}'",
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
