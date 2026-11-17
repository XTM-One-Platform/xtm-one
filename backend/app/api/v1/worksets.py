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

"""Workset REST API.

Human visibility over the working corpora agents build: list them, see what a
corpus holds and how far its processing has got, close one, purge one. The
agent-facing path is the ``workset`` built-in tools; this surface exists so an
operator can answer "what did it actually collect, and what did it leave out?"
without reading the database.

Deliberately **read and purge only**. There is no endpoint that writes a
summary or a priority: those are conclusions about a document, and a corpus
whose analysis can be hand-edited through a REST call stops being able to say
where any of it came from. Discarding an item is the one editorial act
offered, because it is recorded as one.

Visibility is the two-tier model the corpus itself uses: your own worksets plus
the company-managed ones. **Visibility is not permission**: a company-managed
corpus is readable by everyone and writable only by admins, the same rule the
work-item API applies. A workset's internal knowledge base stays invisible
throughout — it is storage owned by this primitive, not a collection.

The list, its count and the bulk delete take ``vis``: ``mine``
(created for me, or owned by an agent I own), ``shared`` (the rest, not
company-managed), ``company`` (company-managed, not mine) or ``all`` (the
default). The [DEPRECATED] ``ownership`` (``mine``, ``shared`` = not mine,
alias ``company_managed``) is read when ``vis`` is absent. Operations >
Worksets opens on Mine.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, require_ai_enabled
from app.api.v1.helpers import (
    fleet_operations_filter,
    parse_path_id,
    resolve_list_ownership,
    workspace_filter,
)
from app.models.user import User
from app.models.workset import Workset
from app.services.ownership_scope import ownership_clause
from app.services.visibility import SegmentCounts, Visibility, segment_counts

router = APIRouter()


class WorksetResolveRequest(BaseModel):
    note: str | None = None


class WorksetBulkDeleteRequest(BaseModel):
    ids: list[str] | None = None
    resolved_only: bool = False
    ownership: str | None = None
    vis: str | None = None


def _visible(user: User):
    """READ visibility: own + company-managed, the two tiers a corpus is
    created under.

    Visibility is not permission. A company-managed workset is readable by
    everyone and writable only by admins — see :func:`_assert_writable`. The
    first cut used this predicate as the only gate, which let any user resolve
    or purge a fleet-wide corpus.
    """
    return or_(
        Workset.user_id == user.id,
        Workset.is_company_managed.is_(True),
    )


def _owned(user: User, ownership: str | None, vis: str | None = None) -> list:
    """The Operations Mine | Shared | Company split of the visible worksets.

    ``vis`` first, else the [DEPRECATED] ``ownership`` (``resolve_list_ownership``).
    """
    clause = ownership_clause(
        resolve_list_ownership(vis, ownership),
        user_uuid=user.id,
        user_column=Workset.user_id,
        agent_columns=(Workset.owner_agent_id,),
        company_column=Workset.is_company_managed,
    )
    return [] if clause is None else [clause]


def _assert_writable(workset: Workset, user: User, action: str) -> None:
    """Refuse a mutation on a company-managed corpus by a non-admin.

    Same rule the work-item API applies, for the same reason: a fleet-wide
    corpus is somebody's job, and "I could see it" is not "it is mine to
    delete".
    """
    if workset.is_company_managed and not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail=f"Only admins can {action} company-managed worksets",
        )


async def _load(db: AsyncSession, user: User, workset_id: str) -> Workset:
    parsed = parse_path_id(workset_id, not_found_detail="Workset not found")
    row = (
        await db.execute(
            select(Workset).where(Workset.id == parsed, _visible(user))
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Workset not found")
    return row


def _serialize(workset: Workset) -> dict:
    from app.services.builtin_tools.workset import is_implicit_workset_key

    return {
        "id": str(workset.id),
        "key": workset.key,
        # Opened by the runtime for an agent chat or an assignment, never
        # sealed: the UI says "Rolling" instead of "Still collecting".
        "implicit": is_implicit_workset_key(workset.key),
        "label": workset.label,
        "scope": workset.scope,
        "scope_ref": workset.scope_ref,
        "status": workset.status,
        "processing": workset.processing,
        "workers": workset.workers,
        "item_count": workset.item_count,
        "evicted_count": workset.evicted_count,
        "content_chars": workset.content_chars,
        "max_items": workset.max_items,
        "max_content_chars": workset.max_content_chars,
        "max_auto_calls": workset.max_auto_calls,
        "auto_calls_used": workset.auto_calls_used,
        "declared_total": workset.declared_total,
        "declared_total_source": workset.declared_total_source,
        "sealed_at": (
            workset.sealed_at.isoformat() if workset.sealed_at else None
        ),
        "watermark_at": (
            workset.watermark_at.isoformat() if workset.watermark_at else None
        ),
        "last_progress_at": (
            workset.last_progress_at.isoformat()
            if workset.last_progress_at
            else None
        ),
        "expires_at": (
            workset.expires_at.isoformat() if workset.expires_at else None
        ),
        "resolved_at": (
            workset.resolved_at.isoformat() if workset.resolved_at else None
        ),
        "is_company_managed": workset.is_company_managed,
        "owner_agent_id": (
            str(workset.owner_agent_id) if workset.owner_agent_id else None
        ),
        "created_at": workset.created_at.isoformat(),
        "updated_at": workset.updated_at.isoformat(),
    }


def _in_workspace(workspace: str | None, fleet: str | None = None) -> list:
    """``?workspace=``: the corpora of a workspace's Operations tab.

    ``?fleet=``: the corpora owned by a current home member of the fleet.
    Both compose.
    """
    clauses = []
    if workspace is not None:
        clauses.append(workspace_filter(workspace, Workset.workspace_id, Workset.owner_agent_id))
    if fleet:
        clauses.append(fleet_operations_filter(fleet, Workset.owner_agent_id))
    return clauses


def _narrowed(
    search: str | None,
    status_filter: str | None,
    scope: str | None,
    scope_ref: str | None,
) -> list:
    """The list's narrowing filters, shared by the list, its count and the segment counts."""
    clauses = []
    if search:
        like = f"%{search.strip()}%"
        clauses.append(or_(Workset.key.ilike(like), Workset.label.ilike(like)))
    if status_filter:
        clauses.append(Workset.status == status_filter)
    # One scope or several (``run,flow``): the Scope filter of the table is a multi-select.
    scopes = [part.strip() for part in (scope or "").split(",") if part.strip()]
    if scopes:
        clauses.append(Workset.scope.in_(scopes))
    if scope_ref:
        clauses.append(Workset.scope_ref == scope_ref.strip())
    return clauses


#: The sorts the Operations table offers (``sort``), each with its column; the
#: default (``updated``, newest first) is the list's historical order.
_SORTS = {
    "name": func.lower(func.coalesce(Workset.label, Workset.key)),
    "items": Workset.item_count,
    "size": Workset.content_chars,
    "updated": Workset.updated_at,
    "created": Workset.created_at,
}


def _order(sort: str | None, direction: str | None) -> list:
    """The ORDER BY of the list: the chosen column, then the id so pages never overlap."""
    column = _SORTS.get(sort or "updated", Workset.updated_at)
    ordered = column.asc() if direction == "asc" else column.desc()
    return [ordered, Workset.id]


@router.get("")
async def list_worksets(
    search: str | None = None,
    status_filter: str | None = None,
    scope: str | None = None,
    scope_ref: str | None = None,
    ownership: str | None = None,
    vis: str | None = None,
    workspace: str | None = None,
    fleet: str | None = None,
    sort: str | None = None,
    direction: str | None = None,
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """The corpora visible to this user, newest activity first.

    ``scope_ref`` narrows to what one run (or one flow) built, which is what
    the builder's run panel asks for: an operator watching a run needs the
    corpus of THAT run, not every corpus the flow has ever produced.
    ``workspace`` keeps the corpora filed in it or owned by a home member of
    the fleet it hosts; ``fleet`` the corpora owned by a home member
    of that fleet. ``sort`` (``name``, ``items``, ``size``, ``updated``,
    ``created``; an unknown value is the default) and ``direction``
    (``asc`` | ``desc``, default ``desc``) order the page the Operations
    table asks for.
    """
    query = select(Workset).where(
        _visible(user),
        *_owned(user, ownership, vis),
        *_in_workspace(workspace, fleet),
        *_narrowed(search, status_filter, scope, scope_ref),
    )
    rows = (
        (
            await db.execute(
                query.order_by(*_order(sort, direction)).limit(max(1, min(int(limit), 200))).offset(max(0, int(offset)))
            )
        )
        .scalars()
        .all()
    )
    return [_serialize(row) for row in rows]


@router.get("/count")
async def count_worksets(
    search: str | None = None,
    status_filter: str | None = None,
    scope: str | None = None,
    scope_ref: str | None = None,
    ownership: str | None = None,
    vis: str | None = None,
    workspace: str | None = None,
    fleet: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """How many corpora the list holds under the same filters, search included: the table's total."""
    query = (
        select(func.count())
        .select_from(Workset)
        .where(
            _visible(user),
            *_owned(user, ownership, vis),
            *_in_workspace(workspace, fleet),
            *_narrowed(search, status_filter, scope, scope_ref),
        )
    )
    return {"count": await db.scalar(query) or 0}


@router.get("/segment-counts", response_model=SegmentCounts)
async def workset_segment_counts(
    search: str | None = None,
    status_filter: str | None = None,
    scope: str | None = None,
    scope_ref: str | None = None,
    workspace: str | None = None,
    fleet: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SegmentCounts:
    """How many corpora each visibility segment holds under the list's other filters.

    The list's filters, search included. Declared before ``GET /{workset_id}``.
    """

    async def count(segment: Visibility) -> int:
        query = (
            select(func.count())
            .select_from(Workset)
            .where(
                _visible(user),
                *_owned(user, None, segment),
                *_in_workspace(workspace, fleet),
                *_narrowed(search, status_filter, scope, scope_ref),
            )
        )
        return await db.scalar(query) or 0

    return await segment_counts(count)


@router.get("/{workset_id}")
async def get_workset(
    workset_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """One workset, with the same coverage and progress an agent reads.

    The operator sees exactly what the agents see — including that an unsealed
    corpus is never called complete, and that a declared total which disagrees
    with what arrived is reported rather than reconciled.
    """
    from app.services.workset_read import coverage_of, progress_of

    workset = await _load(db, user, workset_id)
    coverage = await coverage_of(db, workset)
    progress = await progress_of(db, workset)
    return {
        **_serialize(workset),
        "coverage": {
            "total": coverage.total,
            "ready": coverage.ready,
            "pending": coverage.pending,
            "failed": coverage.failed,
            "evicted": coverage.evicted,
            "discarded": coverage.discarded,
            "sealed": coverage.sealed,
            "complete": coverage.complete,
            "settled": coverage.settled,
            "auto_budget_exhausted": coverage.auto_budget_exhausted,
            "description": coverage.describe(),
        },
        "progress": {
            "processed": progress.processed,
            "remaining": progress.remaining,
            "in_progress": progress.in_progress,
            "throughput_per_min": progress.throughput_per_min,
            "samples": progress.samples,
            "eta_seconds": progress.eta_seconds,
            "eta_range": list(progress.eta_range) if progress.eta_range else None,
            "eta_confidence": progress.eta_confidence,
            "stalled": progress.stalled,
            "description": progress.describe(),
        },
    }


@router.get("/{workset_id}/items")
async def list_workset_items(
    workset_id: str,
    status_filter: str | None = None,
    min_priority: int | None = None,
    sort: str = "priority",
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """What the corpus holds — metadata and summaries, never the source text.

    Reading a document verbatim is what the agent tools are for, under their
    own bounds; a REST endpoint that streamed raw ingested content would be a
    much wider surface than this needs.
    """
    from app.services.workset_read import list_items

    workset = await _load(db, user, workset_id)
    page = await list_items(
        db,
        workset,
        status=status_filter,
        min_priority=min_priority,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    return {
        "total": page.total,
        "offset": page.offset,
        "items": [
            {
                "id": str(item.item_id),
                "title": item.title,
                "status": item.status,
                "priority_score": item.priority_score,
                "occurred_at": item.occurred_at,
                "summary": item.summary,
                "corroboration_count": item.corroboration_count,
                "source_url": item.source_url,
                "tags": item.tags,
                "revision": item.revision,
                "chars": item.chars,
            }
            for item in page.items
        ],
        "coverage": page.coverage.describe(),
    }


@router.post("/{workset_id}/resolve")
async def resolve_workset_endpoint(
    workset_id: str,
    body: WorksetResolveRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Conclude a workset, handing it to the retention sweep."""
    from app.services.workset import resolve as resolve_status

    workset = await _load(db, user, workset_id)
    _assert_writable(workset, user, "resolve")
    await resolve_status(db, workset, note=body.note)
    await db.commit()
    return _serialize(workset)


@router.delete("/{workset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workset(
    workset_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> None:
    workset = await _load(db, user, workset_id)
    _assert_writable(workset, user, "delete")
    await _purge(db, workset)
    await db.commit()


@router.post("/bulk-delete")
async def bulk_delete_worksets(
    body: WorksetBulkDeleteRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> dict:
    query = select(Workset).where(_visible(user), *_owned(user, body.ownership, body.vis))
    if body.ids:
        parsed: list[uuid.UUID] = []
        for raw in body.ids:
            try:
                parsed.append(uuid.UUID(raw))
            except (ValueError, AttributeError, TypeError):
                continue
        if not parsed:
            return {"deleted": 0}
        query = query.where(Workset.id.in_(parsed))
    elif body.resolved_only:
        query = query.where(Workset.status == "resolved")
    else:
        # Refusing an unfiltered bulk delete: "delete everything visible" is
        # never what someone means, and a corpus mid-run is somebody's job.
        raise HTTPException(
            status_code=400,
            detail="Pass ids, or resolved_only, rather than deleting everything.",
        )

    rows = (await db.execute(query)).scalars().all()
    refused = [row for row in rows if row.is_company_managed and not user.is_admin]
    if refused:
        # Refused, not silently skipped: reporting "deleted 3" for a request
        # that named 5 would hide the ones that are not yours to purge.
        raise HTTPException(
            status_code=403,
            detail=(
                f"{len(refused)} of these worksets are company-managed and "
                "only admins can delete them. Narrow the selection."
            ),
        )
    for row in rows:
        await _purge(db, row)
    await db.commit()
    return {"deleted": len(rows)}


async def _purge(db: AsyncSession, workset: Workset) -> None:
    """Delete a workset and the corpus it owns.

    The implementation lives in the service, shared with the retention sweep:
    what a purge deletes — and, more importantly, what it must NOT delete, a
    borrowed document — has to have one answer rather than two that drift.
    """
    from app.services.workset import purge_workset

    await purge_workset(db, workset)
