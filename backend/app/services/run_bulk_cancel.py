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

"""Bulk cancel of assignment runs that have not started yet.

An agent whose daily agentic quota is exhausted, or a burst of deliveries on a
busy assignment, can leave hundreds of runs waiting in ``pending`` /
``delayed`` / ``queued``. Deleting them loses the trail, and the per-run path
is one request per run. This cancels every waiting run matching a filter in
one transaction and says how many.

Only runs that never started are touched: ``running`` and ``awaiting_human``
belong to the emergency stop (``services.panic``) and to the per-run delete,
which also stop the work a run already handed off. A waiting run owns at most
its dispatch execution, which is cancelled with it so the reconciler frees
the slot instead of claiming a run that is no longer there to execute.

Shared by ``POST /assignments/runs/bulk-cancel`` and the agent tool
``cancel_runs``: both are administrator-only, and both report the same count.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import chunked
from app.models.assignment import AssignmentRun

#: The statuses a bulk cancel may touch: a run that has not started.
CANCELLABLE_RUN_STATUSES: tuple[str, ...] = ("pending", "delayed", "queued")

#: Ids per ``UPDATE ... WHERE id IN (...)`` statement.
_ID_CHUNK = 1000


class BulkCancelFilterError(ValueError):
    """The filter names a status a bulk cancel does not touch, or nothing at all."""


@dataclass(frozen=True)
class BulkCancelFilter:
    """Which waiting runs to cancel. At least one of the scoping fields is set."""

    agent_id: uuid.UUID | None = None
    assignment_id: uuid.UUID | None = None
    statuses: tuple[str, ...] = CANCELLABLE_RUN_STATUSES
    created_before: datetime | None = None

    def __post_init__(self) -> None:
        if not self.statuses:
            raise BulkCancelFilterError(
                "statuses must name at least one of: " + ", ".join(CANCELLABLE_RUN_STATUSES)
            )
        unknown = sorted(set(self.statuses) - set(CANCELLABLE_RUN_STATUSES))
        if unknown:
            raise BulkCancelFilterError(
                f"Cannot bulk-cancel runs in status {', '.join(unknown)}: only runs that "
                f"have not started ({', '.join(CANCELLABLE_RUN_STATUSES)}) are cancelled here."
            )
        if self.agent_id is None and self.assignment_id is None and self.created_before is None:
            # A filter on status alone would cancel every waiting run of the
            # platform: refuse it so a typo never empties every queue at once.
            raise BulkCancelFilterError(
                "Name an agent_id, an assignment_id or created_before: a status alone "
                "would cancel every waiting run on the platform."
            )


def cancel_reason(actor: str) -> str:
    """The text written in ``error`` and shown next to the ``cancelled`` status."""
    return f"Cancelled by {actor}"


def _matching(filters: BulkCancelFilter):
    clauses = [AssignmentRun.status.in_(filters.statuses)]
    if filters.agent_id is not None:
        clauses.append(AssignmentRun.agent_id == filters.agent_id)
    if filters.assignment_id is not None:
        clauses.append(AssignmentRun.assignment_id == filters.assignment_id)
    if filters.created_before is not None:
        before = filters.created_before
        if before.tzinfo is None:
            before = before.replace(tzinfo=UTC)
        clauses.append(AssignmentRun.created_at < before)
    return clauses


async def count_cancellable_runs(db: AsyncSession, filters: BulkCancelFilter) -> int:
    """How many runs ``cancel_waiting_runs`` would cancel now. Writes nothing."""
    return int(
        await db.scalar(select(func.count()).select_from(AssignmentRun).where(*_matching(filters)))
        or 0
    )


async def cancel_waiting_runs(db: AsyncSession, filters: BulkCancelFilter, *, actor: str) -> int:
    """Cancel every run matching *filters* that has not started. Returns the count.

    Each ``UPDATE`` re-checks the status, so a run the reconciler promoted to
    ``running`` between the select and the write is left alone and not counted.
    Flushes but does not commit: the caller owns the transaction.
    """
    from app.services.orchestration_engine import cancel_executions_for_runs

    run_ids = list((await db.execute(select(AssignmentRun.id).where(*_matching(filters)))).scalars().all())
    if not run_ids:
        return 0
    now = datetime.now(UTC)
    reason = cancel_reason(actor)
    cancelled: list[uuid.UUID] = []
    for batch in chunked(run_ids, _ID_CHUNK):
        result = await db.execute(
            update(AssignmentRun)
            .where(AssignmentRun.id.in_(batch), AssignmentRun.status.in_(filters.statuses))
            .values(status="cancelled", completed_at=now, error=reason)
            .returning(AssignmentRun.id)
        )
        cancelled.extend(result.scalars().all())
    if cancelled:
        # A ``pending`` / ``delayed`` run already owns its dispatch execution:
        # cancel it, or the reconciler claims it against a cancelled run.
        await cancel_executions_for_runs(db, cancelled)
    await db.flush()
    return len(cancelled)
