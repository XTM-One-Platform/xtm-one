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

"""Workset ingestion — building the corpus.

This module owns the write path: resolving a workset, giving it its internal
corpus, and putting items into it. Reading (search, digest, progress) and the
agent-facing tools build on what this establishes.

Properties the rest of the feature leans on:

* **Ingestion is idempotent, and a correction is not a duplicate.** An item is
  keyed by ``external_ref``. Re-adding the same source with the SAME content
  changes nothing; re-adding it with DIFFERENT content is a *revision* — the
  revision counter bumps, the derived state resets to ``pending``, and the old
  summary is dropped because it described different text (a summary or a score
  given in that same call describes the new text, and is kept). Source
  metadata given (date, URL, tags) is applied either way. This is what makes a
  sliding-window re-run cheap: read the window plus an overlap and let dedup
  discard what is already known.
* **Content never enters a context on the way in.** It is written to a
  ``KnowledgeDocument``; embedding happens in the existing background job,
  queued by the caller AFTER it commits (see :class:`AddOutcome`).
* **A budget overflow evicts, and says so.** Feasibility is decided BEFORE
  anything is mutated, so a refused add never leaves content evicted for
  nothing. Nothing is silently lost: an evicted item keeps its row, the counter
  is public, and every read path reports coverage.
* **A reference is borrowed, never owned.** An item ingested by reference
  points at a document in someone else's collection: this module authorizes
  that reference on the way in, and never rewrites or deletes the document.

**Trust**: item content comes from documents this platform did not author.
Nothing here interprets it — it is stored, hashed and counted, never parsed
for instructions.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import load_only

from app.core.config import settings
from app.core.scopes import COORDINATION_SCOPE_WORKSPACE
from app.core.utils import CORRELATION_KEY_MAX_LENGTH, fit_copy_name, format_exception_message
from app.models.workset import (
    WORKSET_ACTIVE_STATUS,
    WORKSET_DEFAULT_MAX_AUTO_CALLS,
    WORKSET_DEFAULT_MAX_CONTENT_CHARS,
    WORKSET_DEFAULT_MAX_ITEMS,
    WORKSET_DEFAULT_PROCESSING,
    WORKSET_DEFAULT_SCOPE,
    WORKSET_ITEM_EVICTED_STATUS,
    WORKSET_ITEM_PENDING_STATUS,
    WORKSET_ITEM_READY_STATUS,
    WORKSET_PRIORITY_MAX,
    WORKSET_PRIORITY_MIN,
    WORKSET_PROCESSING_MODES,
    WORKSET_RESOLVED_STATUS,
    WORKSET_SCOPES,
    WORKSET_VALUE_SOURCES,
    Workset,
    WorksetItem,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.user import User

logger = logging.getLogger(__name__)

# Chunking for a workset's corpus. Smaller than the KB default (512/128): a
# report is read for specific passages far more often than end to end, and a
# tighter chunk makes a search hit land on the paragraph that matters rather
# than the section around it. Expansion on the read path is what recovers
# surrounding context when it IS wanted.
WORKSET_CHUNK_SIZE = 384
WORKSET_CHUNK_OVERLAP = 96

# Content is queued for embedding whatever its length. Short content used to
# be stored and left unqueued, but its document then sat `pending` until the
# stuck-document sweeper queued it as a lost job minutes later, and the
# automatic pass - which rides that job - waited with it. The processor already
# knows what to do with it: it embeds what it can and marks a document with no
# usable chunk unprocessable. The pass then summarises only an item whose
# document is searchable, and fails one whose document never will be.

# How many eviction candidates are loaded at a time. One page covers every
# realistic overflow; the loop past it exists so a size deficit made up of many
# small items is still handled correctly.
_EVICTION_PAGE = 200


class WorksetSourceError(ValueError):
    """The caller's content source is unusable — refused before any write."""


@dataclass
class AddOutcome:
    """What ``add_item`` did, in terms the caller can report verbatim."""

    item: WorksetItem | None = None
    # created | unchanged | revised | updated | refused
    action: str = "created"
    # Items whose content was reclaimed to make room for this one.
    evicted: list[uuid.UUID] = field(default_factory=list)
    error: str | None = None
    # Stored, but something the caller must know about it: an item of an
    # explicit workset whose automatic budget is spent is kept and will not
    # be summarised automatically.
    warning: str | None = None
    # Documents that still need embedding. The caller queues them AFTER its
    # commit — see `queue_pending`. Enqueuing inside the transaction races the
    # worker: it can pick the job up while the insert is still invisible, find
    # no row, treat the entry as stale and return cleanly, and SAQ never
    # retries it. The item would sit `pending` forever.
    pending_documents: list[uuid.UUID] = field(default_factory=list)
    # `auto` items this call left `pending` with no embedding job of their own
    # coming: a borrowed document is never re-embedded by its borrower, and a
    # metadata re-add queues nothing. The automatic pass rides that job, so
    # `queue_pending` gives them their pass after the commit instead.
    auto_pass_items: list[uuid.UUID] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and self.item is not None


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()


def clamp_priority(value: Any) -> int | None:
    """A usable 0-100 score, or None when the value is not one.

    Shared with the automatic pass so a model's answer and an agent's tool call
    are held to the same standard. `bool` is rejected before `int` on purpose:
    `int(True)` is 1, so a JSON `true` would otherwise be stored as the score
    "1" — a silently wrong number, which is worse than a refused one. A float
    is truncated and an out-of-range number is clamped, both of which are fair
    readings of intent; a boolean is not.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        score = int(value)
    except (TypeError, ValueError):
        return None
    return max(WORKSET_PRIORITY_MIN, min(score, WORKSET_PRIORITY_MAX))


def bounded_key(composed: str) -> str:
    """*composed*, fitted to the key column without losing its discriminator.

    Plain truncation cuts at the TAIL, which is exactly where the
    distinguishing part of a composed key lives: a run id at the end of a long
    flow path, or the descriptive suffix of an `external_ref`. Two different
    keys sharing a 200-character prefix would collapse onto one stored value —
    two concurrent runs sharing a corpus, or two sources deduping against each
    other. Both failures are silent: no error, just someone else's documents in
    the analysis window.

    So an over-length key keeps a readable head and earns a digest of the WHOLE
    original, which makes a collision cryptographically improbable rather than
    merely unlikely.
    """
    if len(composed) <= CORRELATION_KEY_MAX_LENGTH:
        return composed
    digest = hashlib.sha256(composed.encode("utf-8", "replace")).hexdigest()[:12]
    return composed[: CORRELATION_KEY_MAX_LENGTH - 13] + "-" + digest


# ── Implicit worksets: idle expiry, rolling budget, rotation ─────────────────
#
# An IMPLICIT workset is the corpus the runtime derives on its own for an
# agent's chat or for an assignment, never one a flow node or a caller names.
# It lives as long as its agent, so the lifecycle an explicit corpus gets from
# its run or its builder node has to come from here:
#
# * an idle expiry, renewed on every write, so the sweep purges a corpus its
#   agent stopped using (`workset_implicit_idle_days`);
# * a ROLLING automatic-summarisation budget: `max_auto_calls` per window of
#   `workset_implicit_budget_window_hours`, never a lifetime ceiling that
#   leaves a long-lived agent writing into a dead corpus;
# * rotation: a resolved implicit workset is retired under a suffixed key on
#   the next write, which starts a fresh generation under the implicit key, so
#   the retention sweep only ever deletes a corpus nobody writes to.


def _implicit_budget_window() -> timedelta | None:
    hours = settings.workset_implicit_budget_window_hours
    return timedelta(hours=hours) if hours and hours > 0 else None


def _implicit_idle_expiry(now: datetime) -> datetime | None:
    days = settings.workset_implicit_idle_days
    return now + timedelta(days=days) if days and days > 0 else None


def budget_window_over(workset: Workset, now: datetime | None = None) -> bool:
    """An implicit workset whose budget window has run out (or never opened).

    Its counter then starts again from 0 on the next charge, so a spent
    counter in that state is not a spent budget.
    """
    if not getattr(workset, "implicit", False):
        return False
    window = _implicit_budget_window()
    if window is None:
        return False
    started = getattr(workset, "auto_calls_window_started_at", None)
    return started is None or started + window <= (now or datetime.now(UTC))


def budget_resets_at(workset: Workset) -> datetime | None:
    """When an implicit workset's rolling budget replenishes; None for a lifetime ceiling."""
    if not getattr(workset, "implicit", False):
        return None
    window = _implicit_budget_window()
    started = getattr(workset, "auto_calls_window_started_at", None)
    if window is None or started is None:
        return None
    return started + window


def budget_spent(workset: Workset, now: datetime | None = None) -> bool:
    """The automatic pass may not pay for another call on *workset* now."""
    if budget_window_over(workset, now):
        return False
    return (workset.auto_calls_used or 0) >= max(0, workset.max_auto_calls or 0)


def spent_budget_message(workset: Workset) -> str:
    """Why the automatic pass refused an item, and how to finish it by hand."""
    message = (
        f"This workset's automatic summarisation budget is spent "
        f"({workset.auto_calls_used}/{workset.max_auto_calls} calls)."
    )
    resets = budget_resets_at(workset)
    if resets is not None:
        message += f" It replenishes at {resets.isoformat(timespec='minutes')}."
    return message + (
        " Summarise this item with workset_set_summary and "
        "workset_set_priority, or raise the workset's call budget and "
        "re-ingest it."
    )


async def roll_budget_window(db: AsyncSession, workset: Workset, *, now: datetime | None = None) -> bool:
    """Open a new budget window on an implicit workset whose window ran out.

    One conditional UPDATE, for the reason `_charge_call` reserves in one: a
    reset written from Python would send a stale absolute counter back over a
    concurrent charge. Returns whether a new window was opened.
    """
    if not getattr(workset, "implicit", False):
        return False
    window = _implicit_budget_window()
    if window is None:
        return False
    now = now or datetime.now(UTC)
    opened = (
        await db.execute(
            update(Workset)
            .where(
                Workset.id == workset.id,
                Workset.implicit.is_(True),
                or_(
                    Workset.auto_calls_window_started_at.is_(None),
                    Workset.auto_calls_window_started_at <= now - window,
                ),
            )
            .values(auto_calls_used=0, auto_calls_window_started_at=now)
            .returning(Workset.id)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()
    if opened is None:
        return False
    await db.refresh(workset, ["auto_calls_used", "auto_calls_window_started_at"])
    return True


def _retired_key(stored_key: str, workset_id: uuid.UUID) -> str:
    """The key a retired generation of an implicit workset moves to."""
    return bounded_key(f"{stored_key}~{workset_id.hex[:12]}")


def _scope_key(scope: str, scope_ref: str | None, key: str) -> str:
    """The stored key, namespaced by scope.

    A run-scoped workset must not collide with a flow-scoped one of the same
    name, and two concurrent runs of the same flow must not share a corpus —
    the isolation IS the scope's promise, so a non-global scope with no
    reference is refused rather than quietly collapsed into one namespace.
    That promise survives the column's length limit through
    :func:`bounded_key`, which never discards the discriminating tail.
    """
    if scope == "global":
        return bounded_key(key)
    if not scope_ref:
        raise WorksetSourceError(
            f"A '{scope}'-scoped workset needs the id it is scoped to; "
            "without it every run would share one corpus."
        )
    return bounded_key(f"{scope}:{scope_ref}:{key}")


async def resolve_workset(
    db: AsyncSession,
    user: User,
    *,
    key: str,
    scope: str = WORKSET_DEFAULT_SCOPE,
    scope_ref: str | None = None,
    workspace_id: uuid.UUID | None = None,
    label: str | None = None,
    owner_agent_id: uuid.UUID | None = None,
    processing: str = WORKSET_DEFAULT_PROCESSING,
    processor_agent_id: uuid.UUID | None = None,
    workers: int = 1,
    llm_config: dict | None = None,
    max_items: int = WORKSET_DEFAULT_MAX_ITEMS,
    max_content_chars: int = WORKSET_DEFAULT_MAX_CONTENT_CHARS,
    max_auto_calls: int = WORKSET_DEFAULT_MAX_AUTO_CALLS,
    expires_at: datetime | None = None,
    is_company_managed: bool = False,
    write: bool = True,
    implicit: bool = False,
    ingest: bool = False,
) -> Workset:
    """Get, or create, the workset *key* names in this scope.

    ``implicit=True`` is the runtime's own corpus for an agent's chat or an
    assignment (see the section above): its idle expiry is renewed on every
    resolve, and an ingest (``ingest=True``) into a resolved or sealed
    generation retires it under a suffixed key and starts a fresh generation
    under *key*.

    ``write=False`` is a read: in a workspace it needs only read access to the
    workspace, and it never creates the workset there (refused when there is
    none).

    Concurrency-safe: two workers resolving the same key at once both end up
    with the same row. A plain select-then-insert would lose the unique
    constraint on one of them and raise instead of resolving, which is exactly
    the case an idempotent resolver exists for.

    Creation does NOT build the corpus KB — that happens on the first item
    with content, so a workset used only for coordination costs nothing.
    """
    if scope not in WORKSET_SCOPES:
        scope = WORKSET_DEFAULT_SCOPE
    if processing not in WORKSET_PROCESSING_MODES:
        processing = WORKSET_DEFAULT_PROCESSING
    # The `workspace` scope keeps its referent in the typed `workspace_id`
    # column, not in `scope_ref` — but it still namespaces the KEY, on
    # the workspace id. The column and the namespace do different jobs and
    # both are wanted: the column gives referential integrity and a delete
    # behaviour in DDL, the namespace means a query that forgets to filter on
    # it MISSES rather than crossing into another workspace's corpus.
    #
    # `ck_worksets_workspace_scope` requires the pairing, so a caller that
    # names the scope without the id is refused here rather than at write time
    # — the same shape `_scope_key` already enforces for run and flow.
    if scope == COORDINATION_SCOPE_WORKSPACE:
        if workspace_id is None:
            raise WorksetSourceError(
                "A 'workspace'-scoped workset needs the workspace it belongs "
                "to; without it every workspace would share one corpus."
            )
        # The WRITE tier for anything but a read, and this is the sharpest
        # place in the feature for it.
        # A workset owns a knowledge base, its documents and its vectors, so a
        # caller who can only READ the workspace but resolves a workset in it
        # can ingest documents into a corpus every agent in that workspace
        # then retrieves from and acts on. That is the injection half of the
        # escalation the shared-state paths guard against, applied to RAG
        # content — and worse, because the poison is retrieved rather than
        # merely read.
        #
        # `workspace_scope_ids(..., writable=True)` rather than a fresh check:
        # it is the same function the shared-state tiers use, so "who may
        # write here" has one answer, and archived workspaces are already
        # excluded by it.
        from app.services.workspaces import workspace_scope_ids

        if workspace_id not in await workspace_scope_ids(db, user, writable=write):
            raise WorksetSourceError(
                "You do not have permission to use a workset in that "
                "workspace."
            )
        scope_ref = None
        key_ref = str(workspace_id)
    else:
        if workspace_id is not None:
            raise WorksetSourceError(
                f"A '{scope}'-scoped workset cannot belong to a workspace."
            )
        key_ref = scope_ref
    stored_key = _scope_key(scope, key_ref, (key or "").strip() or "default")

    existing = await _load_workset(
        db, user.id, stored_key, workspace_id=workspace_id
    )
    if existing is not None:
        if existing.is_company_managed != is_company_managed:
            # A company-managed corpus is readable by everyone who can use the
            # agent behind it, a personal one by its owner alone: writing one
            # tier's documents into the other's corpus would publish them, or
            # hide a shared corpus's content from its readers.
            # The key is runtime-derived (the binding chooses it), so the model
            # cannot "use another key". The agent's company-managed toggle
            # moves the worksets it owns with it (`retier_agent_worksets`),
            # so this is reached by a corpus another agent or a flow
            # created under the same key, or one left behind before that
            # toggle did so. Operations > Worksets offers no re-tiering, only
            # deletion: once the corpus is gone the next write creates it again
            # in the caller's tier.
            raise WorksetSourceError(
                "That workset belongs to another visibility tier "
                f"({'company-managed' if existing.is_company_managed else 'personal'}); "
                "ask an admin to delete it in Operations > Worksets, and the "
                "next write starts a new one in this tier."
            )
        if not implicit:
            return existing
        # Only a NEW item rotates: a summary or a priority written to an item
        # of the retired generation must still find it there.
        if write and ingest and (existing.status == WORKSET_RESOLVED_STATUS or existing.sealed_at is not None):
            await _retire_generation(db, existing)
        else:
            existing.implicit = True
            renewed = _implicit_idle_expiry(datetime.now(UTC))
            if renewed is not None and existing.status != WORKSET_RESOLVED_STATUS:
                existing.expires_at = renewed
            return existing
    if not write and workspace_id is not None:
        raise WorksetSourceError("That workspace has no such workset yet.")

    now = datetime.now(UTC)
    values = {
        "id": uuid.uuid4(),
        "user_id": user.id,
        "is_company_managed": is_company_managed,
        "key": stored_key,
        "label": label or key,
        "scope": scope,
        "scope_ref": scope_ref,
        "workspace_id": workspace_id,
        "owner_agent_id": owner_agent_id,
        "status": WORKSET_ACTIVE_STATUS,
        "processing": processing,
        "processor_agent_id": processor_agent_id,
        "workers": max(1, min(int(workers or 1), 16)),
        "llm_config": llm_config or None,
        "max_items": max(1, int(max_items or WORKSET_DEFAULT_MAX_ITEMS)),
        "max_content_chars": max(
            1, int(max_content_chars or WORKSET_DEFAULT_MAX_CONTENT_CHARS)
        ),
        # 0 is meaningful here and is NOT replaced by the default: it turns the
        # automatic pass off by budget, which is a legitimate thing to ask for.
        "max_auto_calls": max(
            0,
            int(
                max_auto_calls
                if max_auto_calls is not None
                else WORKSET_DEFAULT_MAX_AUTO_CALLS
            ),
        ),
        "auto_calls_used": 0,
        "implicit": implicit,
        "item_count": 0,
        "evicted_count": 0,
        "content_chars": 0,
        "expires_at": expires_at or (_implicit_idle_expiry(now) if implicit else None),
        "created_at": now,
        "updated_at": now,
    }
    # ON CONFLICT must name the INDEX that actually governs this row, and which
    # one that is depends on the scope. `uq_worksets_user_key_unfiled`
    # and `uq_worksets_workspace_key` are PARTIAL unique indexes, so they are
    # targeted by their columns AND their predicate — `ON CONFLICT ON
    # CONSTRAINT` does not work on a partial index at all, which is why naming
    # the old constraint here would now raise instead of resolving.
    if workspace_id is not None:
        conflict = {
            "index_elements": ["workspace_id", "key"],
            "index_where": Workset.workspace_id.is_not(None),
        }
    else:
        conflict = {
            "index_elements": ["user_id", "key"],
            "index_where": Workset.workspace_id.is_(None),
        }
    await db.execute(
        pg_insert(Workset).values(**values).on_conflict_do_nothing(**conflict)
    )
    # Reload rather than trusting the insert: on conflict the row is the one
    # the other worker wrote, not ours.
    workset = await _load_workset(
        db, user.id, stored_key, workspace_id=workspace_id
    )
    if workset is None:  # pragma: no cover — the insert or the conflict wins
        raise RuntimeError("The workset could not be resolved after insert.")
    return workset


async def _retire_generation(db: AsyncSession, workset: Workset) -> None:
    """Move a resolved or sealed implicit workset out of its key.

    It keeps its items for the retention window an operator inspects them
    in (resolved now when it was only sealed), and the runtime's next write
    creates a fresh generation under the implicit key instead of writing
    into a corpus that reads as finished. One UPDATE guarded on the row's
    current key, so two workers retiring the same generation at once rename
    it once and both resolve the fresh row.
    """
    now = datetime.now(UTC)
    await db.execute(
        update(Workset)
        .where(Workset.id == workset.id, Workset.key == workset.key)
        .values(
            key=_retired_key(workset.key, workset.id),
            status=WORKSET_RESOLVED_STATUS,
            resolved_at=func.coalesce(Workset.resolved_at, now),
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    db.expunge(workset)


async def _load_workset(
    db: AsyncSession,
    user_id: uuid.UUID,
    stored_key: str,
    *,
    workspace_id: uuid.UUID | None = None,
) -> Workset | None:
    """The row a key names, in the scope that OWNS it.

    A workspace-scoped corpus is identified by ``(workspace_id, key)`` and
    NOT by the acting user — that is the whole point of the scope, and it is
    what makes two PEOPLE pointed at one workspace read the same corpus rather
    than each building their own. Everything else keeps being
    identified by ``(user_id, key)``.

    The two branches mirror the two partial unique indexes exactly. They have
    to: a lookup that disagreed with the index would find nothing and then
    conflict on insert.
    """
    where = (
        (Workset.workspace_id == workspace_id,)
        if workspace_id is not None
        else (Workset.user_id == user_id, Workset.workspace_id.is_(None))
    )
    return (
        await db.execute(
            select(Workset).where(Workset.key == stored_key, *where)
        )
    ).scalar_one_or_none()


async def _lock_for_write(db: AsyncSession, workset: Workset) -> None:
    """Take *workset*'s row lock and reload it, for the rest of the transaction.

    The counters (``item_count``, ``evicted_count``, ``content_chars``) are
    moved in Python and the eviction plan reads them, so two writers working
    from the same loaded value would each write N+1 and lose an update, then
    check ``max_items`` against the undercount. Every item write takes this
    lock first: the plan, the evictions, the insert and the counter moves of
    one write are serialised per workset, and the values they start from are
    the committed ones (``populate_existing``), never what the caller loaded
    before another writer committed. Workset first, then its items: the order
    the processing worker locks in, so the two cannot deadlock. The session
    autoflushes before the SELECT, so nothing this transaction already changed
    is lost by the reload.
    """
    await db.execute(
        select(Workset).where(Workset.id == workset.id).with_for_update().execution_options(populate_existing=True)
    )


async def retier_agent_worksets(
    db: AsyncSession, agent_id: uuid.UUID, is_company_managed: bool
) -> int:
    """Move the worksets *agent_id* owns, and their internal corpus, to its new tier.

    The agent's company-managed toggle calls this in its own transaction, next
    to the mirror onto its assignments and runs. The key a chat or an
    assignment run resolves is derived by the runtime and the tier comes from
    the agent, so a corpus left in the old tier would make every later
    ``workset_*`` call of the agent a refusal from :func:`resolve_workset`
    that nothing can act on.

    Only the tier moves: ``user_id`` is part of a corpus's identity (the
    unfiled unique index), so each person's corpus stays theirs. Only the
    worksets' own system-managed knowledge bases follow, never a collection
    an item merely references. Returns how many worksets moved.
    """
    from app.models.knowledge_base import KnowledgeBase

    moving = (
        Workset.owner_agent_id == agent_id,
        Workset.is_company_managed.is_distinct_from(is_company_managed),
    )
    # The corpus first: the workset predicate is what names it, and it no
    # longer matches once the workset row has moved.
    kb = KnowledgeBase.__table__
    await db.execute(
        kb.update()
        .where(
            kb.c.id.in_(
                select(Workset.knowledge_base_id).where(
                    *moving, Workset.knowledge_base_id.is_not(None)
                )
            ),
            kb.c.is_system_managed.is_(True),
        )
        .values(is_company_managed=is_company_managed)
    )
    moved = await db.execute(
        Workset.__table__.update()
        .where(*moving)
        .values(is_company_managed=is_company_managed, updated_at=datetime.now(UTC))
    )
    return moved.rowcount or 0


async def ensure_corpus(
    db: AsyncSession, workset: Workset, user: User
) -> uuid.UUID:
    """The workset's internal KnowledgeBase, created on first use.

    Marked ``is_internal`` so it never appears in a list, a picker or an agent
    binding — it is this workset's storage, and the workset row is what
    authorizes reading it.

    The workset row is locked first: two concurrent first ingestions would
    otherwise both see no corpus, create one each, and leave an orphan KB with
    half the documents in it.
    """
    from app.models.knowledge_base import KnowledgeBase

    if workset.knowledge_base_id is not None:
        return workset.knowledge_base_id

    locked = (
        await db.execute(
            select(Workset).where(Workset.id == workset.id).with_for_update()
        )
    ).scalar_one()
    if locked.knowledge_base_id is not None:
        # Another worker won the race while we waited for the lock.
        workset.knowledge_base_id = locked.knowledge_base_id
        return locked.knowledge_base_id

    kb = KnowledgeBase(
        id=uuid.uuid4(),
        user_id=user.id,
        is_company_managed=workset.is_company_managed,
        name=fit_copy_name(workset.key, KnowledgeBase.__table__.c.name, prefix="workset:"),
        description=(
            "Internal corpus for a workset. Not a user-facing collection: it "
            "is created, filled and purged with the workset that owns it."
        ),
        is_internal=True,
        is_system_managed=True,
        chunk_size=WORKSET_CHUNK_SIZE,
        chunk_overlap=WORKSET_CHUNK_OVERLAP,
    )
    db.add(kb)
    await db.flush()
    locked.knowledge_base_id = kb.id
    workset.knowledge_base_id = kb.id
    return kb.id


# ── Budget ───────────────────────────────────────────────────────────────────


async def _eviction_plan(
    db: AsyncSession,
    workset: Workset,
    *,
    incoming_chars: int,
    incoming_priority: int | None,
    added_items: int,
    freed_chars_upfront: int = 0,
    exclude_item_id: uuid.UUID | None = None,
) -> tuple[list[WorksetItem], str | None]:
    """Decide, without mutating anything, what would have to go.

    Returns ``(candidates_to_evict, error)``. Feasibility is settled here and
    the caller only mutates once it knows the add will succeed — otherwise a
    refused add can leave existing content permanently evicted in exchange for
    nothing, which is worse than either outcome on its own.

    Order: the lowest SCORED item first, then unscored items oldest-first by
    their own date. An unscored item has not been judged yet — treating
    "unknown" as worth more than "known low" is the conservative read, and the
    alternative (evicting by age alone) would throw away the most recent
    reports, which is exactly backwards for a reporting window.

    A pinned item is never evicted, and an item is never evicted to make room
    for an incoming one of LOWER priority — that would trade a better item for
    a worse one and call it success.
    """
    projected_items = workset.item_count + added_items
    projected_chars = (
        workset.content_chars + incoming_chars - freed_chars_upfront
    )
    over_items = projected_items > workset.max_items
    over_chars = projected_chars > workset.max_content_chars
    if not over_items and not over_chars:
        return [], None

    conditions = [
        WorksetItem.workset_id == workset.id,
        WorksetItem.pinned.is_(False),
        WorksetItem.status != WORKSET_ITEM_EVICTED_STATUS,
    ]
    if exclude_item_id is not None:
        # The item being revised is not a candidate for its own eviction.
        conditions.append(WorksetItem.id != exclude_item_id)
    if not over_items:
        # Only the SIZE budget is breached, so reclaiming a zero-content row
        # would free nothing. When the ITEM budget is breached, zero-content
        # rows (references, short inline items) are exactly what has to go —
        # they count against `max_items` like any other.
        conditions.append(WorksetItem.content_chars > 0)

    # Paged, not "load the whole corpus". Ingesting N items would otherwise
    # hydrate every row on every add — O(N²) over a two-week window, which is
    # the exact scale this feature exists for. One page is almost always
    # enough; the loop is what keeps it CORRECT when a size deficit has to be
    # covered by many small items.
    base = (
        select(WorksetItem)
        .options(
            load_only(
                WorksetItem.id,
                WorksetItem.priority_score,
                WorksetItem.content_chars,
                WorksetItem.knowledge_document_id,
                WorksetItem.document_is_foreign,
                WorksetItem.status,
                WorksetItem.chunk_count,
                WorksetItem.pinned,
                WorksetItem.occurred_at,
            )
        )
        .where(*conditions)
        .order_by(
            # NULLs last: scored-low goes before never-judged.
            WorksetItem.priority_score.is_(None),
            WorksetItem.priority_score.asc(),
            WorksetItem.occurred_at.asc().nullsfirst(),
            # A total order, so paging cannot repeat or skip a row.
            WorksetItem.id.asc(),
        )
    )

    plan: list[WorksetItem] = []
    freed_chars = 0
    freed_items = 0
    offset = 0
    exhausted = False
    while not exhausted:
        page = (
            (await db.execute(base.limit(_EVICTION_PAGE).offset(offset)))
            .scalars()
            .all()
        )
        if len(page) < _EVICTION_PAGE:
            exhausted = True
        offset += len(page)
        if not page:
            break
        for candidate in page:
            if (
                not (
                    over_items
                    and projected_items - freed_items > workset.max_items
                )
                and not (
                    over_chars
                    and projected_chars - freed_chars > workset.max_content_chars
                )
            ):
                exhausted = True
                break
            if (
                incoming_priority is not None
                and candidate.priority_score is not None
                and candidate.priority_score > incoming_priority
            ):
                # Everything left is worth more than what is arriving.
                exhausted = True
                break
            freed_chars += candidate.content_chars
            freed_items += 1
            plan.append(candidate)

    still_over_items = (
        over_items and projected_items - freed_items > workset.max_items
    )
    still_over_chars = (
        over_chars and projected_chars - freed_chars > workset.max_content_chars
    )
    if still_over_items or still_over_chars:
        bound = "item" if still_over_items else "content-size"
        return [], (
            f"This workset is at its {bound} budget and nothing could be "
            "evicted to make room (the remaining items are pinned, or worth "
            "more than what is arriving). Nothing was evicted. Raise the "
            "budget, or resolve the workset and start a new window."
        )
    return plan, None


async def _evict_item(
    db: AsyncSession, workset: Workset, item: WorksetItem
) -> None:
    """Reclaim one item's CONTENT, keeping everything derived from it.

    The summary and priority are the expensive part and stay readable; what
    goes is the raw text and its vectors. The row survives so the corpus can
    still say what it once held — a workset that quietly forgot an item would
    report a coverage it does not have.
    """
    if item.knowledge_document_id is not None and not item.document_is_foreign:
        await _delete_document(db, workset, item.knowledge_document_id)

    workset.content_chars = max(0, workset.content_chars - item.content_chars)
    workset.item_count = max(0, workset.item_count - 1)
    workset.evicted_count += 1
    item.knowledge_document_id = None
    item.content_chars = 0
    item.chunk_count = 0
    item.status = WORKSET_ITEM_EVICTED_STATUS
    item.claimed_by_agent_id = None
    item.claimed_at = None


# ── Documents ────────────────────────────────────────────────────────────────


async def _delete_document(
    db: AsyncSession, workset: Workset, document_id: uuid.UUID
) -> None:
    """Remove a workset-OWNED document, its chunks, and the KB's count.

    Never called for a borrowed document — that check belongs to the callers,
    which know whether the item owns what it points at.
    """
    from app.models.embedding import Embedding
    from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
    from app.services.kb_helpers import bump_kb_revision

    if workset.knowledge_base_id is not None:
        # `source_id` is a plain string column, not an FK — no cascade exists,
        # so the chunks are deleted explicitly (the same idiom the KB
        # document-delete path uses).
        await db.execute(
            delete(Embedding).where(
                Embedding.knowledge_base_id == workset.knowledge_base_id,
                Embedding.source_id == str(document_id),
            )
        )
    result = await db.execute(
        delete(KnowledgeDocument).where(KnowledgeDocument.id == document_id)
    )
    if result.rowcount and workset.knowledge_base_id is not None:
        kb = await db.get(KnowledgeBase, workset.knowledge_base_id)
        if kb is not None:
            # The denormalized statistic every other KB write path maintains.
            kb.document_count = max(0, (kb.document_count or 0) - result.rowcount)
        await bump_kb_revision(db, workset.knowledge_base_id)


async def _write_document(
    db: AsyncSession,
    workset: Workset,
    user: User,
    *,
    title: str,
    content: str,
    replace_document_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Store *content* in the workset's corpus.

    *replace_document_id* rewrites a workset-OWNED document in place, so the
    id stays valid for stored citations. It is never a borrowed document's id:
    the callers switch a borrowed item to a fresh owned document instead of
    rewriting someone else's collection.
    """
    from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
    from app.services.kb_helpers import bump_kb_revision, reset_changed_document

    kb_id = await ensure_corpus(db, workset, user)

    if replace_document_id is not None:
        existing = await db.get(KnowledgeDocument, replace_document_id)
        if existing is not None:
            # UPDATED in place, not deleted and re-inserted with the same id.
            # `workset_items.knowledge_document_id` is ON DELETE SET NULL, so a
            # delete NULLs the item's FK in the database — and re-assigning the
            # same id in Python afterwards is not a change SQLAlchemy would
            # emit an UPDATE for, so the row would silently keep the NULL and
            # every stored citation would dangle.
            await reset_changed_document(db, existing)
            existing.filename = title[:500]
            existing.content = content
            existing.content_hash = _hash(content)
            await bump_kb_revision(db, kb_id)
            await db.flush()
            return existing.id

    document = KnowledgeDocument(
        id=uuid.uuid4(),
        knowledge_base_id=kb_id,
        filename=title[:500],
        source_type="text",
        content=content,
        content_hash=_hash(content),
        status="pending",
    )
    db.add(document)
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is not None:
        kb.document_count = (kb.document_count or 0) + 1
    await bump_kb_revision(db, kb_id)
    await db.flush()
    return document.id


async def readable_document_ids(
    db: AsyncSession, user: User, document_ids: list[uuid.UUID] | set[uuid.UUID]
) -> set[uuid.UUID]:
    """Which of *document_ids* this user can still open, right now.

    The single predicate behind both halves of borrowed-content authorization:
    the check at ingest (:func:`_authorize_reference`) and the re-check on
    every read of a borrowed item (``workset_read``). One query, resolved
    through the same visibility boundary the knowledge-base API uses, so a
    workset can never see further than its owner can.

    An internal corpus is excluded for everyone by that boundary, which is what
    stops one workset borrowing another's storage.
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument

    wanted = list(document_ids)
    if not wanted:
        return set()
    rows = (
        (
            await db.execute(
                select(KnowledgeDocument.id)
                .join(
                    KnowledgeBase,
                    KnowledgeBase.id == KnowledgeDocument.knowledge_base_id,
                )
                .where(
                    KnowledgeDocument.id.in_(wanted),
                    user_visible_filter(
                        KnowledgeBase,
                        user.id,
                        await get_user_group_ids(db, user.id),
                        is_admin=user.is_admin,
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


async def _authorize_reference(
    db: AsyncSession, user: User, document_id: uuid.UUID
) -> None:
    """Refuse a document reference the caller cannot already read.

    Workset reads are authorized by the WORKSET, not by the document's own
    collection — so an unchecked reference would turn `add_item` into a way to
    read any document by id. This is the check on the way IN; the read path
    re-checks a borrowed document every time it serves its content, because
    access granted at ingest is a snapshot and collections get unshared.
    """
    if not await readable_document_ids(db, user, [document_id]):
        raise WorksetSourceError(
            "That document does not exist, or is not one you can read. A "
            "workset may only reference documents its owner can already open."
        )


async def queue_pending(outcome: AddOutcome) -> None:
    """Hand this outcome's documents to the background embedding job.

    Call it AFTER committing. Enqueuing inside the transaction races the
    worker, which can pick the job up while the insert is still invisible,
    find no row, treat the entry as stale and return cleanly — SAQ never
    retries, and the item sits `pending` forever.

    Deliberately fire-and-forget: an unreachable queue leaves the document
    pending, which the progress read reports as outstanding work. Losing an
    already-durable ingestion because Redis blinked would be worse.

    The ``auto`` items no embedding job is coming for get their automatic pass
    here too (``workset_processing.run_pass_for_unqueued_items``), after the
    same commit and for the same reason.
    """
    from app.services.kb_helpers import enqueue_document_processing

    for document_id in outcome.pending_documents:
        try:
            await enqueue_document_processing(str(document_id))
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "Workset item %s could not be queued for embedding: %s",
                document_id,
                format_exception_message(exc),
            )
    if outcome.auto_pass_items:
        from app.services.workset_processing import run_pass_for_unqueued_items

        await run_pass_for_unqueued_items(outcome.auto_pass_items)


# ── Ingestion ────────────────────────────────────────────────────────────────


def _resolve_source(
    content: str | None,
    knowledge_document_id: uuid.UUID | None,
    file_attachment_id: uuid.UUID | None,
) -> str:
    """Exactly one content source. Returns which one."""
    supplied = [
        name
        for name, value in (
            ("inline", content),
            ("knowledge_document", knowledge_document_id),
            ("file_attachment", file_attachment_id),
        )
        if value
    ]
    if len(supplied) > 1:
        raise WorksetSourceError(
            "Give exactly one content source — "
            f"{', '.join(supplied)} were all supplied, and silently preferring "
            "one would store a hash describing content that was ignored."
        )
    if not supplied:
        return "none"
    if supplied[0] == "file_attachment":
        # The extraction path is not built yet. Accepting the reference would
        # create an item with no content that stays pending forever while the
        # contract promises background extraction — refusing says so now.
        raise WorksetSourceError(
            "Ingesting a stored file is not implemented yet. Extract the text "
            "and pass it as content."
        )
    return supplied[0]


def closed_for_writes(workset: Workset) -> str | None:
    """Why *workset* takes no more items, or None when it is open.

    Sealing closes ingestion: from then on "complete" is a claim about a
    corpus that cannot grow, so an item added after it would be invisible to
    every read that already called the corpus whole. Resolving hands the
    corpus to the retention sweep: an item written into it after that would be
    purged with it, and its writer would never know. Both are refused at the
    write rather than accepted into a corpus that can no longer account for
    them; the refusal says what to do instead, because the writer is usually
    an agent that will act on it.

    Implicit runtime worksets (an agent's chat corpus, an assignment's) never
    reach this refusal for a resolved row once the runtime rolls their key
    over to a fresh generation; until then the refusal keeps a write out of a
    corpus the sweep is about to delete.
    """
    name = workset.label or workset.key
    if workset.status == WORKSET_RESOLVED_STATUS:
        return (
            f"workset '{name}' is resolved and will be purged by the retention "
            "sweep, so it takes no new item; collect into a new workset (another "
            "key) instead."
        )
    if workset.sealed_at is not None:
        return (
            f"workset '{name}' is sealed (ingestion was closed at "
            f"{workset.sealed_at.isoformat(timespec='seconds')}), so it takes no "
            "new item and its reads keep meaning what they said; collect into a "
            "new workset (another key) instead."
        )
    return None


async def add_item(
    db: AsyncSession,
    workset: Workset,
    user: User,
    *,
    title: str,
    content: str | None = None,
    knowledge_document_id: uuid.UUID | None = None,
    file_attachment_id: uuid.UUID | None = None,
    external_ref: str | None = None,
    occurred_at: datetime | None = None,
    source_url: str | None = None,
    tags: list[str] | None = None,
    summary: str | None = None,
    priority: int | None = None,
    pinned: bool = False,
    extra_metadata: dict | None = None,
) -> AddOutcome:
    """Put one document into *workset*, idempotently.

    Exactly one content source: inline *content*, or a reference to an existing
    *knowledge_document_id* (not copied, not re-embedded, and authorized
    against the caller's own visibility). A metadata-only add — no source at
    all — is allowed for an item whose text is not available, and records
    exactly that.

    A sealed or a resolved workset takes no write at all, new item or re-add
    alike: see :func:`closed_for_writes`.

    The caller commits, then calls :func:`queue_pending` with the outcome.
    """
    closed = closed_for_writes(workset)
    if closed is not None:
        return AddOutcome(action="refused", error=closed)
    title = (title or "").strip() or "Untitled"
    # Digest-suffixed rather than truncated: two source ids sharing a long
    # prefix must not dedup against each other.
    ref = bounded_key((external_ref or "").strip()) or None
    priority_score = clamp_priority(priority)
    # The rule `set_summary` applies: a blank summary is no summary, or an item
    # given one could be called done without anything to read.
    summary = (summary or "").strip() or None

    try:
        # `file_attachment_id` is validated, not stored: `_resolve_source`
        # refuses it while the extraction path does not exist.
        source = _resolve_source(
            content, knowledge_document_id, file_attachment_id
        )
        if source == "knowledge_document" and knowledge_document_id is not None:
            await _authorize_reference(db, user, knowledge_document_id)
    except WorksetSourceError as exc:
        return AddOutcome(action="refused", error=str(exc))

    # Before the dedup lookup too: a revision moves the counters as well.
    await _lock_for_write(db, workset)
    # Again on the committed row: a seal or a resolve that landed after the
    # caller loaded it is seen here, and none can land until this commits.
    closed = closed_for_writes(workset)
    if closed is not None:
        return AddOutcome(action="refused", error=closed)

    existing: WorksetItem | None = None
    if ref is not None:
        existing = (
            await db.execute(
                select(WorksetItem).where(
                    WorksetItem.workset_id == workset.id,
                    WorksetItem.external_ref == ref,
                )
            )
        ).scalar_one_or_none()

    content_hash = _hash(content) if content else None

    if existing is not None:
        return await _update_existing(
            db,
            workset,
            user,
            existing,
            title=title,
            content=content,
            content_hash=content_hash,
            occurred_at=occurred_at,
            source_url=source_url,
            tags=tags,
            summary=summary,
            priority_score=priority_score,
        )

    # An item the automatic pass would have to pay for, on a budget that is
    # spent, is stored with a warning that says so, on an implicit workset as
    # on an explicit one (#2206): a ``to_workset`` diversion cannot retry or
    # pass a summary, so refusing it dropped a tool result the caller never
    # saw. The content stays readable and searchable; the warning names when
    # the budget comes back and how to summarise the item by hand.
    warning: str | None = None
    needs_auto_call = workset.processing == "auto" and source != "none" and not (summary and priority_score is not None)
    if needs_auto_call:
        await roll_budget_window(db, workset)
        if budget_spent(workset):
            warning = spent_budget_message(workset)

    incoming_chars = len(content or "")
    plan, error = await _eviction_plan(
        db,
        workset,
        incoming_chars=incoming_chars,
        incoming_priority=priority_score,
        added_items=1,
    )
    if error is not None:
        return AddOutcome(action="refused", error=error)

    evicted: list[uuid.UUID] = []
    for candidate in plan:
        await _evict_item(db, workset, candidate)
        evicted.append(candidate.id)

    document_id: uuid.UUID | None = None
    document_is_foreign = False
    pending: list[uuid.UUID] = []
    if knowledge_document_id is not None:
        # Borrowed: not copied, not re-embedded, and never deleted by this
        # workset's purge — it belongs to someone else's collection.
        document_id = knowledge_document_id
        document_is_foreign = True
    elif content:
        # Stored whatever its length: short content is content, and dropping
        # it because it is too small to embed would lose it outright — the
        # item row has nowhere to keep text.
        document_id = await _write_document(
            db, workset, user, title=title, content=content
        )
        pending.append(document_id)

    item = WorksetItem(
        id=uuid.uuid4(),
        workset_id=workset.id,
        external_ref=ref,
        title=title,
        source_type=source if source != "none" else "inline",
        source_url=source_url,
        occurred_at=occurred_at,
        knowledge_document_id=document_id,
        document_is_foreign=document_is_foreign,
        content_hash=content_hash,
        content_chars=incoming_chars,
        summary=summary or None,
        summary_source="agent" if summary else None,
        priority_score=priority_score,
        priority_source="agent" if priority_score is not None else None,
        status=WORKSET_ITEM_PENDING_STATUS,
        pinned=bool(pinned),
        tags=tags or [],
        extra_metadata=extra_metadata or None,
    )
    db.add(item)
    workset.item_count += 1
    workset.content_chars += incoming_chars
    workset.updated_at = datetime.now(UTC)
    await db.flush()
    auto_pass: list[uuid.UUID] = []
    if workset.processing == "auto":
        if document_id is None:
            await _settle_without_content(db, workset, item)
        elif document_is_foreign:
            # Its borrower never embeds it, so the pass that rides the
            # embedding job would wait on the owner's next re-embed.
            auto_pass.append(item.id)
    return AddOutcome(
        item=item,
        action="created",
        evicted=evicted,
        warning=warning,
        pending_documents=pending,
        auto_pass_items=auto_pass,
    )


_NO_CONTENT_ERROR = (
    "The item has no content for the automatic summariser to read. Summarise "
    "it with workset_set_summary and workset_set_priority, or re-ingest it "
    "with its content."
)


async def _settle_without_content(
    db: AsyncSession, workset: Workset, item: WorksetItem
) -> None:
    """Decide, at ingest, an ``auto`` item that has no document at all.

    The automatic pass rides the job that embeds an item's document, so an
    item without one is never reached by it, and in ``auto`` mode nothing else
    drains the queue: left ``pending`` it would keep the workset unsettled for
    good. It is ``ready`` when the caller gave both values, and otherwise
    failed the way the pass fails an empty document - still finishable by
    hand, since writing both values promotes a failed item.
    """
    _mark_ready(item)
    if item.status == WORKSET_ITEM_PENDING_STATUS:
        await fail_item(
            db, workset, item.id, error=_NO_CONTENT_ERROR, max_attempts=1
        )


async def _update_existing(
    db: AsyncSession,
    workset: Workset,
    user: User,
    existing: WorksetItem,
    *,
    title: str,
    content: str | None,
    content_hash: str | None,
    occurred_at: datetime | None,
    source_url: str | None,
    tags: list[str] | None,
    summary: str | None,
    priority_score: int | None,
) -> AddOutcome:
    """Re-adding a known ``external_ref``: unchanged, revised, or updated."""
    if content_hash is not None and existing.content_hash == content_hash:
        # The overlap of a sliding window: seen, unchanged, nothing to write.
        # A fully derived item still `pending` behind it waits on nothing (a
        # row stranded before its promotion existed): the promotion is owed,
        # and costs no model call. Anything else is left alone - overlaps are
        # routine, and one landing mid-pass must not pay for a second pass.
        # The metadata given is still the source's latest word: a caller that
        # dedupes on `occurred_at` would otherwise see the item age out of its
        # window although it was just handled again.
        _refresh_metadata(
            existing, occurred_at=occurred_at, source_url=source_url, tags=tags
        )
        existing.updated_at = datetime.now(UTC)
        return AddOutcome(
            item=existing,
            action="unchanged",
            auto_pass_items=(
                await _owed_after_re_add(db, workset, existing)
                if _fully_derived(existing)
                else []
            ),
        )

    if content_hash is not None:
        # A CORRECTION. The source said something different this time, so
        # everything derived from the old text is void — including a summary an
        # agent wrote, which described text that no longer exists. This is the
        # one case where agent-written state is discarded, and it is discarded
        # rather than silently kept beside content it does not match.
        delta = max(0, len(content or "") - existing.content_chars)
        # An evicted item brought back by a correction is live again: it
        # counts against the item budget and leaves the evicted count.
        revived = existing.status == WORKSET_ITEM_EVICTED_STATUS
        plan, error = await _eviction_plan(
            db,
            workset,
            incoming_chars=delta,
            # What the revised item will be worth: the score given with the
            # new text describes it, the stored one described the old text.
            incoming_priority=(
                priority_score
                if priority_score is not None
                else existing.priority_score
            ),
            # The item already counts against the budget (unless it was
            # evicted); only its GROWTH is new, and it must never be
            # considered for its own eviction.
            added_items=1 if revived else 0,
            exclude_item_id=existing.id,
        )
        if error is not None:
            return AddOutcome(action="refused", error=error)

        evicted: list[uuid.UUID] = []
        for candidate in plan:
            await _evict_item(db, workset, candidate)
            evicted.append(candidate.id)

        # A borrowed document is never rewritten: the correction becomes a
        # workset-owned document and the item stops being a reference. Passing
        # the foreign id to the writer would delete a document belonging to
        # another collection.
        replace_id = (
            existing.knowledge_document_id
            if not existing.document_is_foreign
            else None
        )
        document_id = await _write_document(
            db,
            workset,
            user,
            title=title,
            content=content or "",
            replace_document_id=replace_id,
        )
        workset.content_chars = max(
            0, workset.content_chars - existing.content_chars
        ) + len(content or "")
        if revived:
            workset.item_count += 1
            workset.evicted_count = max(0, workset.evicted_count - 1)
        existing.knowledge_document_id = document_id
        existing.document_is_foreign = False
        existing.source_type = "inline"
        existing.content_hash = content_hash
        existing.content_chars = len(content or "")
        existing.chunk_count = 0
        existing.revision += 1
        existing.title = title
        _clear_summary(existing)
        existing.priority_score = None
        existing.priority_reason = None
        existing.priority_source = None
        existing.priority_components = None
        existing.status = WORKSET_ITEM_PENDING_STATUS
        existing.attempt_count = 0
        existing.error_message = None
        existing.claimed_by_agent_id = None
        existing.claimed_at = None
        existing.processing_started_at = None
        existing.processing_completed_at = None
        # What the caller gave in this same call describes the NEW text, so it
        # replaces what was just voided rather than being dropped with it.
        # The item stays `pending` either way: `ready` also means
        # searchable, and the job embedding the new text promotes a fully
        # derived item without a model call, as it does on create.
        if summary:
            existing.summary = summary
            existing.summary_source = "agent"
        if priority_score is not None:
            existing.priority_score = priority_score
            existing.priority_source = "agent"
        _refresh_metadata(
            existing, occurred_at=occurred_at, source_url=source_url, tags=tags
        )
        existing.updated_at = datetime.now(UTC)
        await db.flush()
        pending = [document_id] if content else []
        return AddOutcome(
            item=existing,
            action="revised",
            evicted=evicted,
            pending_documents=pending,
        )

    # Metadata-only re-add: refresh what was given, leave derived state alone.
    _refresh_metadata(
        existing, occurred_at=occurred_at, source_url=source_url, tags=tags
    )
    if existing.summary is not None and not _has_summary(existing):
        # Stored before ingest trimmed summaries: a blank one is none, so the
        # summary given now replaces it and nothing is promoted on it.
        _clear_summary(existing)
    filled = False
    if summary and not existing.summary:
        existing.summary = summary
        existing.summary_source = "agent"
        filled = True
    if priority_score is not None and existing.priority_score is None:
        existing.priority_score = priority_score
        existing.priority_source = "agent"
        filled = True
    existing.updated_at = datetime.now(UTC)
    if filled and workset.processing == "auto" and existing.status == "failed":
        # No pass ever reloads a failed row, and writing its values by hand is
        # how every failure message says to finish it: this re-add wrote them,
        # so it promotes the item the way `set_summary` / `set_priority` would.
        _mark_ready(existing)
        if existing.status != "failed":
            workset.last_progress_at = datetime.now(UTC)
    return AddOutcome(
        item=existing,
        action="updated",
        auto_pass_items=await _owed_after_re_add(db, workset, existing),
    )


def _refresh_metadata(
    item: WorksetItem,
    *,
    occurred_at: datetime | None,
    source_url: str | None,
    tags: list[str] | None,
) -> None:
    """Apply the source metadata a re-add gave, whatever its content did.

    Every branch of a re-add takes it: it describes the source, not the text,
    so neither an unchanged nor a revised content is a reason to keep the
    previous value. What was not given is left as it was.
    """
    if occurred_at is not None:
        item.occurred_at = occurred_at
    if source_url:
        item.source_url = source_url
    if tags:
        item.tags = tags


async def _owed_after_re_add(
    db: AsyncSession, workset: Workset, item: WorksetItem
) -> list[uuid.UUID]:
    """The ``auto`` item a metadata re-add leaves owed its pass, if any.

    Such a re-add queues no embedding job, and the pass - with the promotion
    of an item that already carries both values - rides that job. So a re-add
    that gave a pending item its last missing value left it waiting on nothing,
    and re-ingesting a borrowed item that never got its pass changed nothing
    for it. The pass is owed when the item is fully derived, or when it was
    never attempted: an attempted item still ``pending`` has its retry
    scheduled, and a second pass would only spend that retry early. An item
    without a document is decided now, as at ingest.
    """
    if workset.processing != "auto" or item.status != WORKSET_ITEM_PENDING_STATUS:
        return []
    if item.knowledge_document_id is None:
        await _settle_without_content(db, workset, item)
        return []
    if _fully_derived(item) or not item.attempt_count:
        return [item.id]
    return []


def _has_summary(item: WorksetItem) -> bool:
    """A summary to read, a blank one counting as none (the ``set_summary`` rule)."""
    return bool((item.summary or "").strip())


def _clear_summary(item: WorksetItem) -> None:
    """Drop the summary with its provenance: a source describes a value."""
    item.summary = None
    item.summary_source = None


def _fully_derived(item: WorksetItem) -> bool:
    """Both derived values, a blank summary counting as none."""
    return _has_summary(item) and item.priority_score is not None


# ── Derived state: summary, priority, lifecycle ──────────────────────────────
#
# Everything here writes what an agent (or the automatic pass) concluded about
# an item, as opposed to the item itself. Two rules hold across all of it:
#
# * **Provenance is recorded.** `summary_source` / `priority_source` say who
#   wrote a value, because the automatic pass only ever fills what is empty and
#   never overwrites what an agent wrote — and that precedence is unenforceable
#   without knowing which is which.
# * **An item is addressed within its workset.** Every lookup carries the
#   workset id, so an item id from elsewhere resolves to nothing rather than
#   being written through.


async def _load_item(
    db: AsyncSession, workset: Workset, item_id: uuid.UUID
) -> WorksetItem | None:
    return (
        await db.execute(
            select(WorksetItem).where(
                WorksetItem.id == item_id,
                WorksetItem.workset_id == workset.id,
            )
        )
    ).scalar_one_or_none()


def _mark_ready(item: WorksetItem) -> None:
    """Promote an unfinished item once it carries both derived values.

    A summary alone is not "ready": the digest ranks by priority, and an item
    with no score sorts as never-judged. Requiring both is what keeps `pending`
    meaning "still needs work" rather than "needs some unspecified part of it".

    A `failed` item is promoted too, and that is the point of admitting it
    here: `failed` means "the automatic path gave up on this", not "this row is
    beyond repair". Every failure message tells the agent to write the summary
    and the score by hand, and a corpus where doing exactly that left the item
    failed — and the coverage permanently incomplete — would be telling it to
    do useless work. The automatic pass can never take this branch: it only
    ever loads `pending` rows, so promotion out of `failed` is always somebody
    deliberately fixing it.

    A blank summary is none here too: rows stored before ingest trimmed
    summaries can hold one, and promoting on it would call an item done with
    nothing to read.
    """
    if item.status in (WORKSET_ITEM_PENDING_STATUS, "failed") and _fully_derived(item):
        item.status = WORKSET_ITEM_READY_STATUS
        item.error_message = None
        item.processing_completed_at = datetime.now(UTC)


async def set_summary(
    db: AsyncSession,
    workset: Workset,
    item_id: uuid.UUID,
    summary: str,
    *,
    source: str = "agent",
    overwrite: bool = True,
) -> WorksetItem | None:
    """Record what an item says. Returns None when the item is not in *workset*.

    *overwrite* is False for the automatic pass: it fills gaps and never
    replaces an agent's own reading of a document.
    """
    item = await _load_item(db, workset, item_id)
    if item is None:
        return None
    summary = (summary or "").strip()
    if not summary:
        return item
    if _has_summary(item) and not overwrite:
        return item
    item.summary = summary
    item.summary_source = source if source in WORKSET_VALUE_SOURCES else "agent"
    item.updated_at = datetime.now(UTC)
    _mark_ready(item)
    workset.last_progress_at = datetime.now(UTC)
    return item


async def set_priority(
    db: AsyncSession,
    workset: Workset,
    item_id: uuid.UUID,
    score: Any,
    *,
    reason: str | None = None,
    source: str = "agent",
    components: dict | None = None,
    overwrite: bool = True,
) -> WorksetItem | None:
    """Record how much an item matters, and why."""
    item = await _load_item(db, workset, item_id)
    if item is None:
        return None
    clamped = clamp_priority(score)
    if clamped is None:
        return item
    if item.priority_score is not None and not overwrite:
        return item
    item.priority_score = clamped
    item.priority_reason = (reason or "").strip() or None
    item.priority_source = source if source in WORKSET_VALUE_SOURCES else "agent"
    if components:
        item.priority_components = components
    item.updated_at = datetime.now(UTC)
    _mark_ready(item)
    workset.last_progress_at = datetime.now(UTC)
    return item


async def discard_item(
    db: AsyncSession, workset: Workset, item_id: uuid.UUID, *, reason: str | None
) -> WorksetItem | None:
    """Drop an item from consideration, keeping the row and its reason.

    Not a delete: a corpus that silently forgets what it rejected cannot
    explain its own coverage, and "we looked at it and set it aside" is a
    different statement from "it was never there".
    """
    item = await _load_item(db, workset, item_id)
    if item is None:
        return None
    item.status = "discarded"
    item.error_message = (reason or "").strip() or None
    item.claimed_by_agent_id = None
    item.claimed_at = None
    item.updated_at = datetime.now(UTC)
    return item


async def claim_next_pending(
    db: AsyncSession,
    workset: Workset,
    *,
    agent_id: uuid.UUID | None,
    stale_after: timedelta = timedelta(minutes=15),
) -> WorksetItem | None:
    """Atomically take the next item awaiting derived state.

    One conditional UPDATE, so K workers draining the same queue can never
    both take the same item — the `claim_work_item` pattern, for the same
    reason.

    A claim older than *stale_after* is reclaimable: a worker that died
    mid-item would otherwise park it forever, and the queue would drain to a
    stop while reporting outstanding work. Reclaiming is safe because
    processing an item twice costs a duplicate summary, while losing one costs
    coverage.

    Oldest-first by the document's own date, so a window is processed in the
    order it happened rather than the order it arrived.
    """
    cutoff = datetime.now(UTC) - stale_after
    now = datetime.now(UTC)
    candidate = (
        select(WorksetItem.id)
        .where(
            WorksetItem.workset_id == workset.id,
            WorksetItem.status == WORKSET_ITEM_PENDING_STATUS,
            or_(
                WorksetItem.claimed_at.is_(None),
                WorksetItem.claimed_at < cutoff,
            ),
        )
        .order_by(
            WorksetItem.occurred_at.asc().nullslast(),
            WorksetItem.created_at.asc(),
        )
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    updated = (
        await db.execute(
            update(WorksetItem)
            .where(WorksetItem.id == candidate)
            .values(
                claimed_by_agent_id=agent_id,
                claimed_at=now,
                # The FIRST attempt's start is what an ETA measures from, so a
                # re-claim must not reset it.
                processing_started_at=func.coalesce(
                    WorksetItem.processing_started_at, now
                ),
                attempt_count=WorksetItem.attempt_count + 1,
                updated_at=now,
            )
            .returning(WorksetItem.id)
        )
    ).scalar_one_or_none()
    if updated is None:
        return None
    return await _load_item(db, workset, updated)


async def fail_item(
    db: AsyncSession,
    workset: Workset,
    item_id: uuid.UUID,
    *,
    error: str,
    max_attempts: int = 3,
) -> WorksetItem | None:
    """Release a claim after a failed attempt, terminally once budget is out.

    Terminal matters more than it looks: a `failed` item is what lets a waiter
    stop. An item that stayed `pending` forever after exhausting its retries
    would make "is anything still in flight?" answer yes for good.
    """
    item = await _load_item(db, workset, item_id)
    if item is None:
        return None
    item.error_message = (error or "").strip()[:2000] or None
    # An UNCLAIMED failure counts its own attempt. The drain path already
    # counted when it claimed, but the automatic pass never claims — without
    # this its failures would never accumulate, the item would stay `pending`
    # for good, and every waiter on the workset would hang on it.
    if item.claimed_at is None:
        item.attempt_count += 1
    item.claimed_by_agent_id = None
    item.claimed_at = None
    if item.attempt_count >= max_attempts:
        item.status = "failed"
        item.processing_completed_at = datetime.now(UTC)
    item.updated_at = datetime.now(UTC)
    workset.last_progress_at = datetime.now(UTC)
    return item


async def declare_total(
    db: AsyncSession, workset: Workset, total: Any, *, source: str | None = None
) -> Workset:
    """Record how many items the caller expects to add.

    A CLAIM, kept distinct from `item_count` and never reconciled with it: the
    gap between what was announced and what arrived is information, and
    quietly closing it would destroy the only signal that an ingestion lost
    something.

    Held to the same input standard as :func:`clamp_priority`, and for the same
    reason: `int(True)` is 1, so a tool call carrying `{"total": true}` would
    otherwise record a declared total of "1" — a number that reads as real and
    was never claimed by anyone. That fabricated 1 would corrupt the single
    diagnostic this column exists to provide.
    """
    del db
    if total is None or isinstance(total, bool):
        return workset
    try:
        declared = int(total)
    except (TypeError, ValueError):
        return workset
    workset.declared_total = max(0, declared)
    workset.declared_total_source = (source or "").strip() or None
    workset.updated_at = datetime.now(UTC)
    return workset


async def seal(db: AsyncSession, workset: Workset) -> Workset:
    """Close ingestion — what makes "complete" mean anything.

    Until this is set, everything added being processed does NOT mean the
    corpus is whole, and every read says so. Idempotent: re-sealing keeps the
    original timestamp, because the first close is the one that happened.

    Sealing is also what triggers near-duplicate clustering — see
    :func:`seal_and_cluster`, which is what the tool and node paths call.
    """
    del db
    if workset.sealed_at is None:
        workset.sealed_at = datetime.now(UTC)
        workset.updated_at = workset.sealed_at
    return workset


async def seal_and_cluster(db: AsyncSession, workset: Workset) -> Workset:
    """Seal, then queue the clustering pass the seal makes answerable.

    Split from :func:`seal` rather than folded into it because the enqueue must
    happen after the caller's COMMIT — a job that dequeues before the seal is
    visible would cluster a corpus it believes is still open. The caller's
    session is committed here, which is why this is the entry point the tool
    and the flow node use and :func:`seal` stays a pure state change.
    """
    from app.services.workset_clustering import enqueue_clustering

    already_sealed = workset.sealed_at is not None
    await seal(db, workset)
    await db.commit()
    if not already_sealed:
        await enqueue_clustering(workset.id)
    return workset


async def purge_workset(db: AsyncSession, workset: Workset) -> None:
    """Delete a workset, its items, and the corpus it OWNS.

    A borrowed document is never touched: it belongs to another collection, and
    this workset only ever pointed at it. Deleting one would let a workspace
    take content out of somebody's knowledge base on its way out.

    Shared by the REST purge and the retention sweep on purpose — a workset is
    the heaviest of the coordination primitives (documents, chunks, vectors),
    so "what exactly gets deleted" must have one answer, not two that drift.
    The caller commits.
    """
    from app.models.embedding import Embedding
    from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument

    owned = (
        (
            await db.execute(
                select(WorksetItem.knowledge_document_id).where(
                    WorksetItem.workset_id == workset.id,
                    WorksetItem.knowledge_document_id.is_not(None),
                    WorksetItem.document_is_foreign.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    kb_id = workset.knowledge_base_id
    if kb_id is not None:
        await db.execute(
            delete(Embedding).where(Embedding.knowledge_base_id == kb_id)
        )
    if owned:
        await db.execute(
            delete(KnowledgeDocument).where(KnowledgeDocument.id.in_(owned))
        )
    # `workset_items` cascades with the workset row.
    await db.execute(delete(Workset).where(Workset.id == workset.id))
    if kb_id is not None:
        await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))


async def sweep_worksets(
    *, resolved_after: timedelta | None, limit: int = 200
) -> dict:
    """Purge worksets past their retention. Returns what it removed.

    Two independent reasons a workspace is due, and both are needed:

    * **Resolved** past *resolved_after* — the tidy path, where the agent said
      it was finished and the corpus is kept only long enough for an operator
      to look at what a run collected.
    * **Expired** — an explicit ``expires_at`` from the builder node's "keep
      for" field. This is the one that matters: nothing resolves a workset when
      its run fails halfway, and a run-scoped corpus that outlives its run is
      exactly the storage leak the TTL exists to prevent.

    A resolved workset holding an item created after its resolution is not
    swept on the resolved path: something is still writing to it.

    An ACTIVE workset with no expiry is never swept. It may belong to a
    fortnightly job, and deleting it on inactivity would silently make the next
    run start cold — the opposite of what a `flow`-scoped corpus is for.

    Capped per call and oldest-first, like the other autonomous sweeps, so a
    backlog is drained over several ticks rather than monopolising one.
    """
    from app.core.database import async_session

    now = datetime.now(UTC)
    page = min(max(int(limit or 1), 1), 2000)
    # A NULL `expires_at` never satisfies the comparison, so an active corpus
    # with no TTL is left alone without needing a second predicate.
    due = [Workset.expires_at < now]
    if resolved_after is not None:
        # Fall back to `updated_at` when `resolved_at` is unexpectedly NULL, so
        # a resolved row can never become unpurgeable.
        anchor = func.coalesce(Workset.resolved_at, Workset.updated_at)
        # A resolved workset that took an item AFTER it was resolved is still
        # being written to: `add_item` refuses such a write now, but a row
        # written before that guard, or by a path that bypasses it, holds a
        # corpus somebody is using. It is left for an operator rather than
        # deleted under its writer.
        written_after = (
            select(WorksetItem.id)
            .where(
                WorksetItem.workset_id == Workset.id,
                WorksetItem.created_at > anchor,
            )
            .exists()
        )
        due.append((Workset.status == WORKSET_RESOLVED_STATUS) & (anchor < now - resolved_after) & ~written_after)

    purged = {"resolved": 0, "expired": 0}
    async with async_session() as db:
        rows = (
            (
                await db.execute(
                    select(Workset)
                    .where(or_(*due))
                    .order_by(Workset.updated_at.asc())
                    .limit(page)
                )
            )
            .scalars()
            .all()
        )
        for workset in rows:
            expired = (
                workset.expires_at is not None and workset.expires_at < now
            )
            await purge_workset(db, workset)
            purged["expired" if expired else "resolved"] += 1
        if rows:
            await db.commit()
    return purged


async def resolve(
    db: AsyncSession, workset: Workset, *, note: str | None = None
) -> Workset:
    """Conclude the workset, handing it to the retention sweep."""
    del db
    if workset.status != WORKSET_RESOLVED_STATUS:
        workset.status = WORKSET_RESOLVED_STATUS
        workset.resolved_at = datetime.now(UTC)
        workset.updated_at = workset.resolved_at
    if note:
        workset.label = f"{workset.label or workset.key} — {note.strip()}"[:500]
    return workset
