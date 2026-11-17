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

"""Retroactive ROI records: a declaration counts the subject's whole history (issue #10006, slice S1).

``record_run_units`` writes a run's record when the run completes and
``record_work_item_units`` a work item's when it reaches ``done``
(``services/roi_records.py``). A declaration set later used to count only
what came after it. :func:`backfill_subject` replays the same rules over the
history the database holds for one subject (an agent, an assignment or a
fleet), so declaring outcomes on a desk that ran for weeks shows its weeks.

Rules, the ones of the completion path:

* Runs: every ``completed`` run of the subject (an assignment's own runs, an
  agent's runs, a fleet's member agents' runs). The declaration is resolved
  per run exactly as at completion (assignment, agent, legacy ``x-xtm-roi`` of
  a JSON-output agent, home fleet), so an assignment's own declaration still
  wins over the fleet's. ``per_run`` and ``per_output_item`` count the run
  (the latter from the stored structured output); ``per_work_item`` never
  counts a run.
* Work items: every ``done`` item owned by the subject's agents whose
  resolved declaration counts ``per_work_item`` and whose selector it
  matches (``roi_records.units_of_work_item``, #10412; an assignment
  declaration never applies to a work item, as at completion). The record of
  an item that no longer matches is recounted to 0 units.
* Idempotent: the unique ``run_id`` and ``work_item_id`` keys are kept; a
  second pass writes nothing.
* A record that exists and now resolves to another unit or count is
  recounted (``unit``, ``units``). Its outcome, review minutes and reviewer
  are never touched, so a reviewed record keeps its review.
* The LLM cost of an old run is read from its traces; a run whose traces are
  past retention gets 0 and is counted in ``cost_not_retained``.
* Every record written here carries ``backfilled_at`` and the ``created_at``
  of its run's (or item's) completion, so the report windows place it in its
  own period.
* No model call, no quota: a deterministic database pass.

The home fleet stored on a backfilled record is the agent's home fleet now
(the one at the run's end is not kept anywhere for a run without a record).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.models.roi import RoiRunRecord
from app.services import roi_records
from app.services.roi_records import (
    COUNT_PER_WORK_ITEM,
    SUBJECT_AGENT,
    SUBJECT_ASSIGNMENT,
    SUBJECT_FLEET,
    ResolvedDeclaration,
)

logger = logging.getLogger(__name__)

#: Rows read per page: bounds the memory of a pass over a long history.
PAGE_SIZE = 500

#: The SAQ job that runs a pass (``app.worker.roi_backfill_job``).
JOB_NAME = "roi_backfill_job"


@dataclass
class BackfillResult:
    """What a pass found (``dry_run``) or wrote."""

    subject_type: str
    subject_id: str
    dry_run: bool
    runs_scanned: int = 0
    work_items_scanned: int = 0
    records_created: int = 0
    records_recounted: int = 0
    units_added: int = 0
    cost_not_retained: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def subject_agent_ids(db: AsyncSession, subject_type: str, subject_id: uuid.UUID) -> list[uuid.UUID]:
    """The agents whose history a subject's declaration covers."""
    from app.models.assignment import Assignment
    from app.models.fleet import FLEET_ROLE_MEMBER, FleetMember

    if subject_type == SUBJECT_AGENT:
        return [subject_id]
    if subject_type == SUBJECT_ASSIGNMENT:
        agent_id = (await db.execute(select(Assignment.agent_id).where(Assignment.id == subject_id))).scalar()
        return [agent_id] if agent_id is not None else []
    if subject_type == SUBJECT_FLEET:
        rows = (
            await db.execute(
                select(FleetMember.agent_id).where(
                    FleetMember.fleet_id == subject_id,
                    FleetMember.role == FLEET_ROLE_MEMBER,
                    FleetMember.agent_id.is_not(None),
                )
            )
        ).scalars()
        return list(dict.fromkeys(rows))
    raise ValueError(f"subject_type must be one of {', '.join(roi_records.SUBJECT_TYPES)}")


class _Resolver:
    """The declaration of a run or a work item, resolved as at completion, cached per key."""

    def __init__(self, agent_ids: list[uuid.UUID]) -> None:
        self.agent_ids = agent_ids
        self.schemas: dict[uuid.UUID, Mapping[str, Any] | None] = {}
        self.fleets: dict[uuid.UUID, uuid.UUID | None] = {}
        self.cache: dict[tuple[Any, ...], ResolvedDeclaration | None] = {}

    async def load(self, db: AsyncSession) -> None:
        from app.models.agent import Agent
        from app.services.fleet_runs import home_fleet_defaults_of

        if not self.agent_ids:
            return
        rows = (
            await db.execute(
                select(Agent.id, Agent.output_format, Agent.output_schema).where(Agent.id.in_(self.agent_ids))
            )
        ).all()
        for agent_id, output_format, output_schema in rows:
            self.schemas[agent_id] = (
                output_schema if output_format == "json" and isinstance(output_schema, Mapping) else None
            )
        homes = await home_fleet_defaults_of(db, self.agent_ids)
        self.fleets = {agent_id: home.fleet_id for agent_id, home in homes.items()}

    async def of(
        self, db: AsyncSession, agent_id: uuid.UUID, assignment_id: uuid.UUID | None, *, work_item: bool = False
    ) -> ResolvedDeclaration | None:
        key = (agent_id, assignment_id, work_item)
        if key not in self.cache:
            schema = self.schemas.get(agent_id)
            if work_item:
                # ``record_work_item_units`` reads the raw schema, whatever the output format.
                from app.models.agent import Agent

                raw = (await db.execute(select(Agent.output_schema).where(Agent.id == agent_id))).scalar()
                schema = raw if isinstance(raw, Mapping) else None
            self.cache[key] = await roi_records.resolve_declaration(
                db,
                agent_id=agent_id,
                output_schema=schema,
                assignment_id=None if work_item else assignment_id,
                fleet_id=self.fleets.get(agent_id),
            )
        return self.cache[key]


async def _existing(db: AsyncSession, column: Any, ids: list[uuid.UUID]) -> dict[uuid.UUID, RoiRunRecord]:
    if not ids:
        return {}
    rows = (await db.execute(select(RoiRunRecord).where(column.in_(ids)))).scalars().all()
    return {getattr(row, column.key): row for row in rows}


async def _recount(
    db: AsyncSession, row: RoiRunRecord, unit: str, units: int, result: BackfillResult, *, dry_run: bool
) -> None:
    """Align an existing record with the declaration now resolved; its review is kept."""
    if row.unit == unit and row.units == units:
        return
    result.records_recounted += 1
    result.units_added += units - int(row.units or 0)
    if dry_run:
        return
    await db.execute(update(RoiRunRecord).where(RoiRunRecord.id == row.id).values(unit=unit, units=units))


async def _insert(db: AsyncSession, values: dict[str, Any], conflict: str) -> bool:
    stmt = pg_insert(RoiRunRecord).values(**values).on_conflict_do_nothing(index_elements=[conflict])
    async with db.begin_nested():
        return (await db.execute(stmt.returning(RoiRunRecord.id))).scalar() is not None


async def _backfill_runs(
    db: AsyncSession,
    resolver: _Resolver,
    subject_type: str,
    subject_id: uuid.UUID,
    result: BackfillResult,
    *,
    dry_run: bool,
    now: datetime,
) -> None:
    from app.models.assignment import AssignmentRun

    query = select(
        AssignmentRun.id,
        AssignmentRun.agent_id,
        AssignmentRun.assignment_id,
        AssignmentRun.status,
        AssignmentRun.result_summary,
        AssignmentRun.started_at,
        AssignmentRun.completed_at,
    ).where(AssignmentRun.status == "completed")
    if subject_type == SUBJECT_ASSIGNMENT:
        query = query.where(AssignmentRun.assignment_id == subject_id)
    else:
        query = query.where(AssignmentRun.agent_id.in_(resolver.agent_ids))
    last: uuid.UUID | None = None
    while True:
        page_query = query.order_by(AssignmentRun.id).limit(PAGE_SIZE)
        if last is not None:
            page_query = page_query.where(AssignmentRun.id > last)
        page = (await db.execute(page_query)).all()
        if not page:
            return
        last = page[-1].id
        existing = await _existing(db, RoiRunRecord.run_id, [r.id for r in page])
        for run in page:
            result.runs_scanned += 1
            declaration = await resolver.of(db, run.agent_id, run.assignment_id)
            if declaration is None:
                continue
            units = roi_records.units_of_run(declaration, status=run.status, result_summary=run.result_summary)
            if units is None:
                continue
            unit = declaration.unit[:64]
            if run.id in existing:
                await _recount(db, existing[run.id], unit, units, result, dry_run=dry_run)
                continue
            llm_cost = await roi_records._run_llm_cost(db, run.id)
            if llm_cost == 0:
                result.cost_not_retained += 1
            result.records_created += 1
            result.units_added += units
            if dry_run:
                continue
            completed_at = run.completed_at or now
            written = await _insert(
                db,
                {
                    "id": uuid.uuid4(),
                    "run_id": run.id,
                    "agent_id": run.agent_id,
                    "fleet_id": resolver.fleets.get(run.agent_id),
                    "unit": unit,
                    "units": units,
                    "llm_cost": llm_cost,
                    "duration_s": roi_records.run_duration_s(run.started_at, completed_at),
                    "outcome": None,
                    "created_at": completed_at,
                    "backfilled_at": now,
                },
                "run_id",
            )
            if not written:  # a completion raced this pass: the run already has its record
                result.records_created -= 1
                result.units_added -= units


async def _backfill_work_items(
    db: AsyncSession, resolver: _Resolver, result: BackfillResult, *, dry_run: bool, now: datetime
) -> None:
    from app.models.work_item import WorkItem

    if not resolver.agent_ids:
        return
    query = select(
        WorkItem.id,
        WorkItem.owner_agent_id,
        WorkItem.payload,
        WorkItem.result,
        WorkItem.completed_at,
        WorkItem.updated_at,
    ).where(WorkItem.status == "done", WorkItem.owner_agent_id.in_(resolver.agent_ids))
    last: uuid.UUID | None = None
    while True:
        page_query = query.order_by(WorkItem.id).limit(PAGE_SIZE)
        if last is not None:
            page_query = page_query.where(WorkItem.id > last)
        page = (await db.execute(page_query)).all()
        if not page:
            return
        last = page[-1].id
        existing = await _existing(db, RoiRunRecord.work_item_id, [r.id for r in page])
        for item in page:
            result.work_items_scanned += 1
            declaration = await resolver.of(db, item.owner_agent_id, None, work_item=True)
            if declaration is None or declaration.count_mode != COUNT_PER_WORK_ITEM:
                continue
            unit = declaration.unit[:64]
            # The declaration's selector (#10412): an item that does not match counts 0,
            # and its existing record is recounted to 0 (its review kept).
            units = roi_records.units_of_work_item(declaration, payload=item.payload, result=item.result)
            if item.id in existing:
                await _recount(db, existing[item.id], unit, units, result, dry_run=dry_run)
                continue
            if units <= 0:
                continue
            result.records_created += 1
            result.units_added += units
            if dry_run:
                continue
            written = await _insert(
                db,
                {
                    "id": uuid.uuid4(),
                    "run_id": None,
                    "work_item_id": item.id,
                    "agent_id": item.owner_agent_id,
                    "fleet_id": resolver.fleets.get(item.owner_agent_id),
                    "unit": unit,
                    "units": units,
                    "llm_cost": 0.0,
                    "duration_s": 0.0,
                    "outcome": None,
                    "created_at": item.completed_at or item.updated_at or now,
                    "backfilled_at": now,
                },
                "work_item_id",
            )
            if not written:
                result.records_created -= 1
                result.units_added -= units


async def backfill_subject(
    db: AsyncSession,
    *,
    subject_type: str,
    subject_id: uuid.UUID,
    dry_run: bool = False,
    now: datetime | None = None,
) -> BackfillResult:
    """Count the subject's past runs and done work items under the declarations in force.

    ``dry_run`` answers what a pass would write and writes nothing. The
    caller commits.
    """
    now = now or datetime.now(UTC)
    result = BackfillResult(subject_type=subject_type, subject_id=str(subject_id), dry_run=dry_run)
    agent_ids = [a for a in await subject_agent_ids(db, subject_type, subject_id) if a is not None]
    if not agent_ids:
        return result
    resolver = _Resolver(agent_ids)
    await resolver.load(db)
    await _backfill_runs(db, resolver, subject_type, subject_id, result, dry_run=dry_run, now=now)
    if subject_type != SUBJECT_ASSIGNMENT:
        await _backfill_work_items(db, resolver, result, dry_run=dry_run, now=now)
    return result


async def run_backfill(subject_type: str, subject_id: str) -> BackfillResult | None:
    """The worker side: one pass in its own session, committed; ``None`` on failure (logged)."""
    from app.core.database import async_session

    try:
        sid = uuid.UUID(subject_id)
        async with async_session() as db:
            result = await backfill_subject(db, subject_type=subject_type, subject_id=sid)
            await db.commit()
        logger.info("ROI backfill of %s %s: %s", subject_type, subject_id, result.as_dict())
        return result
    except Exception as exc:  # a failed pass leaves history uncounted, never broken
        logger.warning("ROI backfill of %s %s failed: %s", subject_type, subject_id, format_exception_message(exc))
        return None


async def enqueue_backfill(subject_type: str, subject_id: uuid.UUID) -> bool:
    """Queue a pass for the subject; ``False`` when the queue is unreachable (logged).

    Call it AFTER the declaration is committed: the job re-reads it.
    """
    import asyncio

    try:
        from saq import Job

        from app.core.config import settings as app_settings
        from app.services import kb_helpers
        from app.worker import queue

        await asyncio.wait_for(
            queue.enqueue(
                Job(
                    function=JOB_NAME,
                    kwargs={"subject_type": subject_type, "subject_id": str(subject_id)},
                    # Unique per enqueue: a declaration changed twice runs twice, the second pass recounts.
                    key=f"roi-backfill-{subject_type}-{subject_id}-{uuid.uuid4().hex[:8]}",
                    timeout=app_settings.worker_job_timeout,
                )
            ),
            timeout=kb_helpers._ENQUEUE_TIMEOUT_SECONDS,
        )
        return True
    except Exception as exc:
        logger.warning(
            "ROI backfill of %s %s could not be queued: %s", subject_type, subject_id, format_exception_message(exc)
        )
        return False
