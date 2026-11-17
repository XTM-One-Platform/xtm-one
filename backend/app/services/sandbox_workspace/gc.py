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

"""The sandbox workspace sweep: what is deleted, when, and why.

A workspace is deleted, its manifest and every object under its prefix with
it, when

- its conversation is archived or deleted (at once from the archive / delete
  routes through :func:`collect_conversations`, and by the sweep for every
  other path that archives or deletes one);
- its assignment run ended: a terminal status (``completed``,
  ``failed``, ``skipped``, ``cancelled``) is never left, so nothing reads the
  workspace again (a retry or a follow-up is a new run with a workspace of its
  own). At once from the run-end path through :func:`collect_runs_soon`, and
  by the sweep for every run already terminal (the backlog of an install that
  ran before this rule). Except a workspace labelled with a work item that is
  still open (#9868): the next run of that item adopts it
  (``WorkspaceStore.open``); it goes once the item is done, cancelled or gone,
  or after the run idle TTL below;
- its assignment run is gone;
- it was not used for ``CODE_INTERPRETER_RUN_WORKSPACE_IDLE_TTL_HOURS`` (a
  run's, unless the run waits on a person) or
  ``CODE_INTERPRETER_WORKSPACE_IDLE_TTL_HOURS`` (a conversation's, and a run's
  that waits on a person).

The sweep also removes the objects of a prefix no row names any more (a
workspace deleted while its objects could not be). It runs from the worker's
reconciler once an hour for the whole install: each process asks every
:data:`ATTEMPT_INTERVAL_SECONDS`, a short Redis lease renewed after every
deletion keeps one sweep at a time, and a marker set when a sweep finishes spaces them
:data:`SWEEP_INTERVAL_SECONDS` apart (fail-open: two sweeps delete the same
things twice, harmlessly). A sweep cut short by a rollout leaves no marker and
its lease expires within :data:`_LEASE_SECONDS`, so another worker resumes it
within minutes. Each pass is bounded by :data:`SWEEP_BATCH`, at most
:data:`SWEEP_PASSES` passes a sweep; objects go in bulk (S3 DeleteObjects).
What each pass freed (workspaces and bytes per reason) is summed in Redis as
it goes and the finished sweep is recorded, for ``get_platform_overview``
(:func:`freed_totals`).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.core.utils import format_exception_message
from app.services.sandbox_workspace import audit, metrics
from app.services.sandbox_workspace.store import OBJECT_PREFIX, WorkspaceStore, default_store

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 3600
#: How often each process asks whether a sweep is due (one Redis read): a sweep
#: cut by a rollout is resumed within this delay once its lease expired.
ATTEMPT_INTERVAL_SECONDS = 300
SWEEP_BATCH = 200
#: Passes of :data:`SWEEP_BATCH` one sweep makes while there is something due:
#: the backlog of ended runs is collected in a few hours, never in one burst.
SWEEP_PASSES = 25
_LEASE_KEY = "sandbox_workspace:gc:lease"
_FREED_KEY = "sandbox_workspace:gc:freed"
_LAST_SWEEP_KEY = "sandbox_workspace:gc:last_sweep"
_SWEPT_KEY = "sandbox_workspace:gc:swept"
#: The lease of a sweep in progress, renewed after every deletion: a sweep cut by a
#: rollout holds it this long at most, never the hour.
_LEASE_SECONDS = 600


@dataclass
class SweepReport:
    deleted: dict[str, int] = field(default_factory=dict)
    bytes_freed: dict[str, int] = field(default_factory=dict)
    objects_removed: int = 0
    orphan_prefixes: int = 0

    def add(self, reason: str, size_bytes: int = 0) -> None:
        self.deleted[reason] = self.deleted.get(reason, 0) + 1
        self.bytes_freed[reason] = self.bytes_freed.get(reason, 0) + max(0, int(size_bytes or 0))

    def merge(self, other: SweepReport) -> None:
        for reason, count in other.deleted.items():
            self.deleted[reason] = self.deleted.get(reason, 0) + count
        for reason, size in other.bytes_freed.items():
            self.bytes_freed[reason] = self.bytes_freed.get(reason, 0) + size
        self.objects_removed += other.objects_removed
        self.orphan_prefixes += other.orphan_prefixes

    def payload(self) -> dict:
        return {
            "deleted": dict(self.deleted),
            "bytes_freed": dict(self.bytes_freed),
            "objects_removed": self.objects_removed,
            "orphan_prefixes": self.orphan_prefixes,
        }


#: A candidate: workspace id, owner, why it is due, the bytes it holds.
Candidate = tuple[uuid.UUID, uuid.UUID | None, str, int]


def idle_ttl() -> timedelta:
    from app.core.config import settings

    return timedelta(hours=max(1, int(settings.code_interpreter_workspace_idle_ttl_hours)))


def run_idle_ttl() -> timedelta:
    """How long a run's workspace may sit unused while its run has not ended."""
    from app.core.config import settings

    return timedelta(hours=max(1, int(settings.code_interpreter_run_workspace_idle_ttl_hours)))


def _terminal_statuses() -> tuple[str, ...]:
    from app.services.assignment_engine import TERMINAL_RUN_STATUSES

    return TERMINAL_RUN_STATUSES


def _not_handed_over():
    """The workspaces an ended run leaves for nobody: no work item, or one done, cancelled or gone (#9868).

    A workspace labelled with an open work item outlives its run so the next
    run of the item adopts it; the run idle TTL still bounds it.
    """
    from sqlalchemy import exists, not_, select

    from app.models.sandbox_workspace import SandboxWorkspace as W
    from app.models.work_item import WORK_ITEM_TERMINAL_STATUSES, WorkItem

    open_item = exists(
        select(WorkItem.id).where(
            WorkItem.id == W.work_item_id,
            WorkItem.status.not_in(WORK_ITEM_TERMINAL_STATUSES),
        )
    )
    return not_(open_item)


async def _candidates(
    session_factory, now: datetime, *, skip: frozenset[uuid.UUID] = frozenset()
) -> list[Candidate]:
    from sqlalchemy import and_, or_, select

    from app.models.assignment import AssignmentRun
    from app.models.chat import Conversation
    from app.models.sandbox_workspace import SandboxWorkspace as W

    def batch(query):
        if skip:
            query = query.where(W.id.not_in(list(skip)))
        return query.limit(SWEEP_BATCH)

    found: list[Candidate] = []
    async with session_factory() as db:
        conversation_gone = (
            await db.execute(
                batch(
                    select(W.id, W.user_id, W.size_bytes, Conversation.id.is_(None))
                    .outerjoin(Conversation, Conversation.id == W.conversation_id)
                    .where(
                        W.conversation_id.is_not(None),
                        or_(Conversation.id.is_(None), Conversation.is_archived.is_(True)),
                    )
                )
            )
        ).all()
        found += [
            (wid, uid, "conversation_deleted" if gone else "conversation_archived", size)
            for wid, uid, size, gone in conversation_gone
        ]
        run_gone = (
            await db.execute(
                batch(
                    select(W.id, W.user_id, W.size_bytes)
                    .outerjoin(AssignmentRun, AssignmentRun.id == W.assignment_run_id)
                    .where(and_(W.assignment_run_id.is_not(None), AssignmentRun.id.is_(None)))
                )
            )
        ).all()
        found += [(wid, uid, "run_deleted", size) for wid, uid, size in run_gone]
        run_ended = (
            await db.execute(
                batch(
                    select(W.id, W.user_id, W.size_bytes)
                    .join(AssignmentRun, AssignmentRun.id == W.assignment_run_id)
                    .where(AssignmentRun.status.in_(_terminal_statuses()), _not_handed_over())
                )
            )
        ).all()
        found += [(wid, uid, "run_ended", size) for wid, uid, size in run_ended]
        # A run that waits on a person may wait for days: its workspace keeps
        # the conversation's idle TTL, every other live run's the shorter one.
        run_idle = (
            await db.execute(
                batch(
                    select(W.id, W.user_id, W.size_bytes)
                    .join(AssignmentRun, AssignmentRun.id == W.assignment_run_id)
                    .where(
                        AssignmentRun.status != "awaiting_human",
                        W.last_used_at < now - run_idle_ttl(),
                    )
                )
            )
        ).all()
        found += [(wid, uid, "idle", size) for wid, uid, size in run_idle]
        idle = (
            await db.execute(
                batch(select(W.id, W.user_id, W.size_bytes).where(W.last_used_at < now - idle_ttl()))
            )
        ).all()
        found += [(wid, uid, "idle", size) for wid, uid, size in idle]
    seen: set[uuid.UUID] = set()
    unique: list[Candidate] = []
    for wid, uid, reason, size in found:
        if wid not in seen:
            seen.add(wid)
            unique.append((wid, uid, reason, int(size or 0)))
    return unique


async def delete_workspace(
    store: WorkspaceStore,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID | None,
    reason: str,
    report: SweepReport,
    *,
    size_bytes: int = 0,
) -> None:
    from app.services.sandbox_workspace.kernel import kernel_pool
    from app.services.sandbox_workspace.local import process_cache

    removed = await store.delete_workspace(workspace_id)
    await kernel_pool().drop(str(workspace_id))
    process_cache().forget(workspace_id)
    report.add(reason, size_bytes)
    report.objects_removed += removed
    metrics.operation("gc_delete", reason)
    await audit.record_system(
        "delete",
        anchor_user_id=user_id,
        workspace_id=workspace_id,
        summary=f"Deleted a sandbox workspace ({reason.replace('_', ' ')}), {removed} object(s), {size_bytes} byte(s)",
        details={"reason": reason, "objects_removed": removed, "bytes_freed": size_bytes},
    )


async def _record_freed(report: SweepReport) -> None:
    """Sum what *report* freed into Redis (fail-open): ``get_platform_overview`` reads it back.

    Called after every pass of a sweep and after every collect, so what was
    freed is counted even when a rollout cuts the sweep before its end.
    """
    if not report.deleted:
        return
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        for reason, count in report.deleted.items():
            await client.hincrby(_FREED_KEY, f"{reason}:workspaces", int(count))
            await client.hincrby(_FREED_KEY, f"{reason}:bytes", int(report.bytes_freed.get(reason, 0)))
    except Exception as exc:
        logger.debug("Sandbox workspace sweep: could not record what it freed: %s", format_exception_message(exc))


async def _record_sweep(report: SweepReport, *, finished_at: datetime) -> None:
    """Record a finished sweep (``last_sweep``) and space the next one an interval later (fail-open)."""
    import json

    try:
        from app.core.redis import redis_client

        client = await redis_client()
        await client.set(_LAST_SWEEP_KEY, json.dumps({"finished_at": finished_at.isoformat(), **report.payload()}))
        await client.set(_SWEPT_KEY, finished_at.isoformat(), ex=SWEEP_INTERVAL_SECONDS - 60)
        await client.delete(_LEASE_KEY)
    except Exception as exc:
        logger.debug("Sandbox workspace sweep: could not record the sweep: %s", format_exception_message(exc))


async def _renew_lease() -> None:
    """Keep the lease of the sweep in progress for another :data:`_LEASE_SECONDS` (fail-open)."""
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        await client.set(_LEASE_KEY, "1", ex=_LEASE_SECONDS)
    except Exception as exc:
        logger.debug("Sandbox workspace sweep: could not renew its lease: %s", format_exception_message(exc))


async def freed_totals() -> dict | None:
    """What the clean-up freed since the counters began, per reason, and the last sweep; ``None`` unread."""
    import json

    try:
        from app.core.redis import redis_client

        client = await redis_client()
        raw = await client.hgetall(_FREED_KEY) or {}
        last = await client.get(_LAST_SWEEP_KEY)
    except Exception:
        return None
    by_reason: dict[str, dict[str, int]] = {}
    for key, value in raw.items():
        key = key.decode() if isinstance(key, bytes) else str(key)
        reason, _, measure = key.rpartition(":")
        if reason and measure in ("workspaces", "bytes"):
            by_reason.setdefault(reason, {"workspaces": 0, "bytes": 0})[measure] = int(value)
    try:
        last_sweep = json.loads(last) if last else None
    except (TypeError, ValueError):
        last_sweep = None
    return {
        "by_reason": by_reason,
        "bytes": sum(v["bytes"] for v in by_reason.values()),
        "workspaces": sum(v["workspaces"] for v in by_reason.values()),
        "last_sweep": last_sweep,
    }


async def sweep(*, store: WorkspaceStore | None = None, session_factory=None, now: datetime | None = None) -> SweepReport:
    """One pass: delete what is due, then the objects of prefixes no row names."""
    from sqlalchemy import select

    from app.models.sandbox_workspace import SandboxWorkspace as W

    if session_factory is None:
        from app.core.database import async_session as session_factory
    store = store or default_store()
    moment = now or datetime.now(UTC)
    report = SweepReport()
    failed: set[uuid.UUID] = set()
    for _ in range(SWEEP_PASSES):
        due = await _candidates(session_factory, moment, skip=frozenset(failed))
        if not due:
            break
        step = SweepReport()
        for workspace_id, user_id, reason, size in due:
            try:
                await delete_workspace(store, workspace_id, user_id, reason, step, size_bytes=size)
                await _renew_lease()
            except Exception as exc:
                failed.add(workspace_id)
                logger.warning(
                    "Sandbox workspace sweep: could not delete %s: %s", workspace_id, format_exception_message(exc)
                )
        await _record_freed(step)
        report.merge(step)
    groups = await store.blobs.list_groups(OBJECT_PREFIX)
    ids: dict[str, uuid.UUID] = {}
    for group in groups[: SWEEP_BATCH * 5]:
        head = group[len(OBJECT_PREFIX) :].strip("/")
        try:
            ids[head] = uuid.UUID(head)
        except ValueError:
            continue
    if ids:
        async with session_factory() as db:
            live = set((await db.execute(select(W.id).where(W.id.in_(list(ids.values()))))).scalars().all())
        for head, workspace_id in list(ids.items())[:SWEEP_BATCH]:
            if workspace_id in live:
                continue
            keys = await store.blobs.list_keys(f"{OBJECT_PREFIX}{head}/")
            report.objects_removed += await store.blobs.delete(keys)
            report.orphan_prefixes += 1
            metrics.operation("gc_delete", "orphan_objects")
    if report.deleted or report.orphan_prefixes:
        logger.info(
            "Sandbox workspace sweep: deleted %s workspace(s) %s, %d byte(s) freed %s, "
            "%d orphan prefix(es), %d object(s) removed",
            sum(report.deleted.values()),
            report.deleted,
            sum(report.bytes_freed.values()),
            report.bytes_freed,
            report.orphan_prefixes,
            report.objects_removed,
        )
    await _record_sweep(report, finished_at=datetime.now(UTC))
    return report


async def collect_conversations(conversation_ids: list[uuid.UUID], *, reason: str) -> None:
    """Delete the workspaces of conversations just archived or deleted (after their commit)."""
    from sqlalchemy import select

    from app.core.database import async_session
    from app.models.sandbox_workspace import SandboxWorkspace as W

    if not conversation_ids:
        return
    try:
        async with async_session() as db:
            rows = (
                await db.execute(
                    select(W.id, W.user_id, W.size_bytes).where(W.conversation_id.in_(conversation_ids))
                )
            ).all()
        store = default_store()
        report = SweepReport()
        for workspace_id, user_id, size in rows:
            await delete_workspace(store, workspace_id, user_id, reason, report, size_bytes=int(size or 0))
        await _record_freed(report)
    except Exception as exc:
        logger.warning(
            "Could not delete the sandbox workspace(s) of %d conversation(s) (%s); the sweep does: %s",
            len(conversation_ids),
            reason,
            format_exception_message(exc),
        )


def collect_conversations_soon(conversation_ids: list[uuid.UUID], *, reason: str) -> None:
    """:func:`collect_conversations` in a task of its own: a route never waits on the object store."""
    try:
        asyncio.get_running_loop().create_task(
            collect_conversations(list(conversation_ids), reason=reason), name="sandbox-workspace-collect"
        )
    except RuntimeError:
        return


async def collect_runs(run_ids: list[uuid.UUID], *, session_factory=None, store: WorkspaceStore | None = None) -> SweepReport:
    """Delete the workspaces of runs that just ended; a run not terminal keeps its workspace.

    The status is read again here, after the run's commit: only a terminal
    run, which is never left, has its workspace removed. A failure is logged
    and left to the hourly sweep.
    """
    from sqlalchemy import select

    from app.models.assignment import AssignmentRun
    from app.models.sandbox_workspace import SandboxWorkspace as W

    report = SweepReport()
    if not run_ids:
        return report
    if session_factory is None:
        from app.core.database import async_session as session_factory
    try:
        async with session_factory() as db:
            rows = (
                await db.execute(
                    select(W.id, W.user_id, W.size_bytes)
                    .join(AssignmentRun, AssignmentRun.id == W.assignment_run_id)
                    .where(
                        W.assignment_run_id.in_(list(run_ids)),
                        AssignmentRun.status.in_(_terminal_statuses()),
                        _not_handed_over(),
                    )
                )
            ).all()
        if not rows:
            return report
        store = store or default_store()
        for workspace_id, user_id, size in rows:
            await delete_workspace(store, workspace_id, user_id, "run_ended", report, size_bytes=int(size or 0))
        await _record_freed(report)
    except Exception as exc:
        logger.warning(
            "Could not delete the sandbox workspace(s) of %d ended run(s); the sweep does: %s",
            len(run_ids),
            format_exception_message(exc),
        )
    return report


def collect_runs_soon(run_ids: list[object]) -> None:
    """:func:`collect_runs` in a task of its own: the run's ending never waits on the object store."""
    from app.core.utils import safe_uuid

    ids = [rid for rid in (safe_uuid(str(r)) for r in run_ids if r) if rid is not None]
    if not ids:
        return
    try:
        task = asyncio.get_running_loop().create_task(collect_runs(ids), name="sandbox-workspace-collect-runs")
    except RuntimeError:
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)


#: Strong references to the collect tasks, so none is garbage-collected mid-way.
_pending: set[asyncio.Task] = set()


async def take_lease() -> bool:
    """Whether this process runs the sweep now (fail-open).

    No while a sweep finished less than an interval ago (the swept marker) or
    another process holds the lease of a sweep in progress; that lease is
    short and renewed after every pass, so a sweep cut by a rollout is resumed
    by the next process that asks once it expires.
    """
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        if await client.exists(_SWEPT_KEY):
            return False
        return bool(await client.set(_LEASE_KEY, "1", nx=True, ex=_LEASE_SECONDS))
    except Exception:
        return True
