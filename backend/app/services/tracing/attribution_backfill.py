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

"""Stamp the traces and rollups written earlier with their attribution and department.

The trace sink stamps every new trace (``attribution.stamp_traces``). The
traces written before, and their ``llm_daily_stats`` rows, are stamped once
by this walk, run from the trace maintenance in bounded steps (the days a
time budget allows per run), newest first, from today back to the oldest day
with data. Where it stands is the ``trace_attribution_backfill`` row, so a
restart resumes it.

Per day:

* the traces with no attribution, and - once a department exists - those
  whose department was never resolved (or resolved before an administrator's
  "apply the mapping to history"), get :func:`stamp_traces`'s stamp, their
  department marked ``department_backfilled``; the day's rollup is recorded
  stale, so ``rebuild_daily_stats`` rebuilds it from them;
* the day's rollup rows with no attribution get the rule the rollup can
  answer alone - its user, else its agent's owner (it carries no run nor
  parent) - and their department, at once: a day whose traces the purge
  removed has nothing better, and a day with traces reads stamped until its
  rebuild replaces them with the trace-level stamp.

Every stamp is a ``WHERE`` on what is still missing, so a second walk changes
nothing (idempotent). The walk starts again when the first department is
created (the history is then resolved from that first mapping) and when an
administrator asks to apply the mapping to history (``request_history_rewrite``);
otherwise a mapping change never rewrites history. The last
:data:`_RECHECK_DAYS` days are walked on every run: a replica still on the
previous build writes unstamped traces during a rolling deploy.

Writes run under the rollup lock (``trace_storage.DAILY_STATS_LOCK_SQL``),
taken before any trace row is locked, like the repricing.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, text, true, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.models.department import TraceAttributionBackfill
from app.models.llm_trace import LLM_TRACE_SOURCES, LLMDailyStats, LLMTrace
from app.services.departments import DepartmentMap, load_department_map, resolve_user_departments
from app.services.tracing.attribution import TraceRefs, stamp_traces

logger = logging.getLogger(__name__)

_STATE_ID = 1
#: The most days of history one run walks, and how long it keeps walking.
_DAYS_PER_RUN = 60
_BUDGET_SECONDS = 20.0
_RECHECK_DAYS = 2
_BATCH = 1000

_STAMP_TRACE_SQL = text(
    """
    UPDATE llm_traces SET
        attributed_user_id = :attributed_user_id,
        attribution = :attribution,
        department_id = :department_id,
        department_resolved_at = :department_resolved_at,
        department_backfilled = :department_backfilled,
        agent_name_snapshot = COALESCE(agent_name_snapshot, :agent_name_snapshot)
    WHERE id = :trace_id
    """
)

#: The rule a rollup row can answer alone: it carries a user and an agent, no run nor parent.
_STAMP_ROLLUP_SQL = text(
    """
    UPDATE llm_daily_stats s SET
        attributed_user_id = COALESCE(s.user_id, a.user_id),
        attribution = CASE
            WHEN s.user_id IS NOT NULL THEN 'user'
            WHEN a.user_id IS NOT NULL THEN 'agent_owner'
            WHEN s.agent_id IS NOT NULL THEN 'unknown'
            ELSE 'system' END,
        agent_name_snapshot = COALESCE(s.agent_name_snapshot, a.name)
    FROM llm_daily_stats r
    LEFT JOIN agents a ON a.id = r.agent_id
    WHERE r.id = s.id AND s.date = :day AND s.attribution IS NULL
    """
)


@dataclass
class BackfillResult:
    days: int = 0
    traces: int = 0
    rollup_rows: int = 0
    completed: bool = False


async def _state(db: AsyncSession) -> TraceAttributionBackfill:
    state = await db.get(TraceAttributionBackfill, _STATE_ID)
    if state is None:
        state = TraceAttributionBackfill(id=_STATE_ID)
        db.add(state)
        await db.flush()
    return state


async def restart_walk(db: AsyncSession, *, rewrite: bool = False) -> None:
    """Walk the whole history again, in the caller's transaction; *rewrite* resolves every department again."""
    state = await _state(db)
    state.cursor_date = None
    state.completed_at = None
    if rewrite:
        state.department_rewrite_before = datetime.now(UTC)


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    return start, start + timedelta(days=1)


async def _oldest_day(db: AsyncSession) -> date | None:
    """The oldest UTC day with a trace or a rollup row."""
    from app.services.tracing.read_store import oldest_trace_started_at

    oldest_trace = await oldest_trace_started_at(db)
    oldest_rollup = (await db.execute(select(func.min(LLMDailyStats.date)))).scalar()
    days = [value for value in (oldest_rollup, oldest_trace.astimezone(UTC).date() if oldest_trace else None) if value]
    return min(days) if days else None


def _trace_needs(mapping: DepartmentMap, rewrite_before: datetime | None):
    needs = [LLMTrace.attribution.is_(None)]
    if mapping.configured:
        needs.append(LLMTrace.department_resolved_at.is_(None))
        if rewrite_before is not None:
            needs.append(LLMTrace.department_resolved_at < rewrite_before)
    return or_(*needs)


def _parent_id(row: Any) -> uuid.UUID | None:
    if row.parent_trace_id is not None:
        return row.parent_trace_id
    try:
        return uuid.UUID(str(row.metadata_parent)) if row.metadata_parent else None
    except ValueError:
        return None


async def _stamp_day_traces(
    db: AsyncSession, day: date, mapping: DepartmentMap, rewrite_before: datetime | None
) -> int:
    from app.services.trace_storage import DAILY_STATS_LOCK_SQL, mark_daily_stats_stale

    start, end = _day_bounds(day)
    stamped = 0
    last_id: uuid.UUID | None = None
    while True:
        query = (
            select(
                LLMTrace.id,
                LLMTrace.user_id,
                LLMTrace.agent_id,
                LLMTrace.assignment_run_id,
                LLMTrace.parent_trace_id,
                LLMTrace.metadata_json["parent_trace_id"].astext.label("metadata_parent"),
                LLMTrace.conversation_id,
                LLMTrace.attribution,
                LLMTrace.attributed_user_id,
            )
            .where(
                LLMTrace.source.in_(LLM_TRACE_SOURCES),
                LLMTrace.started_at >= start,
                LLMTrace.started_at < end,
                _trace_needs(mapping, rewrite_before),
            )
            .order_by(LLMTrace.id)
            .limit(_BATCH)
        )
        if last_id is not None:
            query = query.where(LLMTrace.id > last_id)
        rows = (await db.execute(query)).all()
        if not rows:
            break
        last_id = rows[-1].id
        unstamped = {
            row.id: TraceRefs(row.user_id, row.agent_id, row.assignment_run_id, _parent_id(row), row.conversation_id)
            for row in rows
            if row.attribution is None
        }
        stamps = await stamp_traces(db, unstamped, mapping=mapping)
        # Already attributed, only the department is missing: their attribution stands.
        persons = [row.attributed_user_id for row in rows if row.attribution is not None and row.attributed_user_id]
        departments = await resolve_user_departments(db, persons, mapping) if mapping.configured else {}
        now = datetime.now(UTC)
        values: list[dict[str, Any]] = []
        for row in rows:
            stamp = stamps.get(row.id)
            if stamp is not None:
                values.append(
                    {
                        "trace_id": row.id,
                        "attributed_user_id": stamp.attributed_user_id,
                        "attribution": stamp.attribution,
                        "department_id": stamp.department_id,
                        "department_resolved_at": now if stamp.department_resolved else None,
                        "department_backfilled": stamp.department_resolved,
                        "agent_name_snapshot": stamp.agent_name_snapshot,
                    }
                )
            else:
                values.append(
                    {
                        "trace_id": row.id,
                        "attributed_user_id": row.attributed_user_id,
                        "attribution": row.attribution,
                        "department_id": departments.get(row.attributed_user_id) if row.attributed_user_id else None,
                        "department_resolved_at": now,
                        "department_backfilled": True,
                        "agent_name_snapshot": None,
                    }
                )
        # The rollup lock first, before any trace row is locked (the repricing's order).
        await db.execute(text(DAILY_STATS_LOCK_SQL))
        await mark_daily_stats_stale(db, [day])
        await db.execute(_STAMP_TRACE_SQL, values)
        await db.commit()
        stamped += len(values)
        if len(rows) < _BATCH:
            break
    return stamped


async def _stamp_day_rollup(
    db: AsyncSession, day: date, mapping: DepartmentMap, *, walked: bool, rewrite: bool
) -> int:
    """Stamp the rollup rows of *day* the trace-level stamp has not reached; the rows changed.

    The department of a row is resolved when the row is attributed here, and,
    on a day of the walk (*walked*), when it was never resolved - every row on
    a *rewrite*. A recent day re-checked on every run only gets the rows a
    replica on the previous build wrote: a row resolved Unassigned stays so.
    """
    from app.services.trace_storage import DAILY_STATS_LOCK_SQL

    await db.execute(text(DAILY_STATS_LOCK_SQL))
    fresh = (
        await db.execute(
            select(LLMDailyStats.id).where(LLMDailyStats.date == day, LLMDailyStats.attribution.is_(None))
        )
    ).scalars().all()
    changed = (await db.execute(_STAMP_ROLLUP_SQL, {"day": day})).rowcount or 0
    if mapping.configured:
        needs = [LLMDailyStats.id.in_(fresh)] if fresh else []
        if rewrite:
            needs = [true()]
        elif walked:
            needs.append(and_(LLMDailyStats.department_id.is_(None), LLMDailyStats.department_backfilled.is_not(True)))
        if needs:
            persons = (
                await db.execute(
                    select(LLMDailyStats.attributed_user_id)
                    .where(LLMDailyStats.date == day, LLMDailyStats.attributed_user_id.is_not(None), or_(*needs))
                    .distinct()
                )
            ).scalars().all()
            for person, department_id in (await resolve_user_departments(db, persons, mapping)).items():
                result = await db.execute(
                    update(LLMDailyStats)
                    .where(LLMDailyStats.date == day, LLMDailyStats.attributed_user_id == person, or_(*needs))
                    .values(department_id=department_id, department_backfilled=True)
                    .execution_options(synchronize_session=False)
                )
                changed += result.rowcount or 0
    await db.commit()
    return changed


async def run_attribution_backfill(
    db: AsyncSession,
    *,
    max_days: int = _DAYS_PER_RUN,
    budget_seconds: float = _BUDGET_SECONDS,
    now: datetime | None = None,
) -> BackfillResult:
    """One step of the walk: the recent days, then older ones - up to *max_days*, while
    *budget_seconds* last (one day at least). Commits; never raises."""
    result = BackfillResult()
    now = now or datetime.now(UTC)
    today = now.date()
    started = time.monotonic()
    try:
        state = await _state(db)
        cursor, completed, rewrite_before = state.cursor_date, state.completed_at, state.department_rewrite_before
        await db.commit()
        mapping = await load_department_map(db, fresh=True)
        oldest = await _oldest_day(db)
        candidates: list[date] = []
        if completed is None and oldest is not None:
            start = (cursor - timedelta(days=1)) if cursor else today
            candidates = [day for day in (start - timedelta(days=offset) for offset in range(max_days)) if day >= oldest]
        for day in (today - timedelta(days=offset) for offset in range(_RECHECK_DAYS)):
            result.traces += await _stamp_day_traces(db, day, mapping, rewrite_before)
            result.rollup_rows += await _stamp_day_rollup(db, day, mapping, walked=False, rewrite=False)
            result.days += 1
        walked: list[date] = []
        for day in candidates:
            if walked and time.monotonic() - started > budget_seconds:
                break
            result.traces += await _stamp_day_traces(db, day, mapping, rewrite_before)
            result.rollup_rows += await _stamp_day_rollup(
                db, day, mapping, walked=True, rewrite=rewrite_before is not None
            )
            result.days += 1
            walked.append(day)
        state = await _state(db)
        if walked:
            state.cursor_date = walked[-1]
        if completed is None and (oldest is None or not candidates or walked[-1] <= oldest):
            state.completed_at = now
            # Every department resolved before it was resolved again on the way.
            state.department_rewrite_before = None
            result.completed = True
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.error("Trace attribution backfill failed: %s", format_exception_message(exc), exc_info=True)
        return result
    if result.traces or result.rollup_rows:
        logger.info(
            "Trace attribution backfill stamped %d trace(s) and %d rollup row(s) over %d day(s)%s",
            result.traces,
            result.rollup_rows,
            result.days,
            " - history done" if result.completed else "",
        )
    return result
