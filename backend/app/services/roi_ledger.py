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

"""The ROI ledger: who every value fact counts for (issue #12077, slice L1).

Design: ``dev-docs/roi-outcomes.md`` section 12.3 and 12.11. A value fact
(``roi_run_records``) names the person it counts for and the rule that named
them, so a person's "Mine" reads only their own facts and the organization
total is the same sum for every reader.

* A run record: the person of the run as a trace of that run would be
  attributed (``tracing/attribution.py``, ONE rule set): the run's acting
  identity (``AssignmentRun.user_id``), else the assignment's owner
  (``assignment``), else the agent's owner (``agent_owner``).
* A work item record: the person the item works for (``WorkItem.user_id``,
  rule ``work_item``), else the agent's owner (``agent_owner``); an item that
  no longer exists names nobody (``unknown``).
* A record whose run and item are both gone: ``unknown``. It counts in the
  organization ("No person"), never in a Mine.

A chat deliverable (``roi_chat_records``) already names its person; it gains
the fleet of its agent, stamped when the row is first written
(``roi_chat_records.record_deliverable``) and backfilled once for the rows
written before, flagged ``fleet_backfilled``.

:func:`run_ledger_backfill` stamps the rows written before this slice, in
bounded batches from the trace maintenance; every write is a ``WHERE`` on
what is still missing, so a second pass changes nothing.

Cost facts (slice L2, design section 12.3): the AI cost of a day, per person,
attribution, agent, fleet and class (``roi_cost_facts``). :func:`cost_facts`
reads them from ``llm_traces`` with ONE query, which serves the days not
closed yet live; :func:`close_cost_days` writes the complete days with the
same query, from the trace maintenance right after the daily rollup. A day
whose traces no longer add up to its rollup (purged, or partly purged) is
closed from ``llm_daily_stats`` instead, so the facts of a day always sum to
the cost LLM Analytics shows for it; a day whose rollup cost moved since it
was closed (a repricing) is closed again.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Hashable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.services import fleet_value_marks
from app.services.tracing.attribution import TraceRefs, attribute

logger = logging.getLogger(__name__)

#: The rule of a work item record: the person the item works for.
ATTRIBUTION_WORK_ITEM = "work_item"
#: A record whose run and item are gone: counted in the organization only.
ATTRIBUTION_UNKNOWN = "unknown"

#: Rows stamped per batch, and how long one maintenance step keeps going.
_BATCH = 500
_BUDGET_SECONDS = 10.0

Person = tuple[uuid.UUID | None, str]


async def persons_of_runs(
    db: AsyncSession, runs: Mapping[Hashable, tuple[uuid.UUID | None, uuid.UUID | None]]
) -> dict[Hashable, Person]:
    """``key -> (person, attribution)`` of each ``(run_id, agent_id)``, by the trace rules."""
    return await attribute(
        db, {key: TraceRefs(assignment_run_id=run_id, agent_id=agent_id) for key, (run_id, agent_id) in runs.items()}
    )


async def person_of_run(
    db: AsyncSession, *, run_id: uuid.UUID | None, agent_id: uuid.UUID | None, user_id: uuid.UUID | None = None
) -> Person:
    """The person a run's record counts for; *user_id* is the run's acting identity when already known."""
    if user_id is not None:
        return user_id, "assignment"
    return (await persons_of_runs(db, {"run": (run_id, agent_id)}))["run"]


async def persons_of_work_items(
    db: AsyncSession, items: Mapping[Hashable, tuple[uuid.UUID | None, uuid.UUID | None]]
) -> dict[Hashable, Person]:
    """``key -> (person, attribution)`` of each ``(work_item_id, agent_id)``."""
    from app.models.work_item import WorkItem

    ids = sorted({item_id for item_id, _agent in items.values() if item_id is not None}, key=str)
    found: dict[uuid.UUID, uuid.UUID | None] = {}
    if ids:
        rows = await db.execute(select(WorkItem.id, WorkItem.user_id).where(WorkItem.id.in_(ids)))
        found = {row.id: row.user_id for row in rows.all()}
    people: dict[Hashable, Person] = {}
    owners: dict[Hashable, TraceRefs] = {}
    for key, (item_id, agent_id) in items.items():
        if item_id not in found:
            people[key] = (None, ATTRIBUTION_UNKNOWN)
        elif found[item_id] is not None:
            people[key] = (found[item_id], ATTRIBUTION_WORK_ITEM)
        else:
            owners[key] = TraceRefs(agent_id=agent_id)
    for key, (person, rule) in (await attribute(db, owners)).items():
        people[key] = (person, rule) if person is not None else (None, ATTRIBUTION_UNKNOWN)
    return people


async def person_of_work_item(
    db: AsyncSession, *, work_item_id: uuid.UUID | None, agent_id: uuid.UUID | None
) -> Person:
    """The person a work item's record counts for."""
    return (await persons_of_work_items(db, {"item": (work_item_id, agent_id)}))["item"]


@dataclass
class LedgerBackfillResult:
    """What one step stamped (``dry_run``: would stamp)."""

    dry_run: bool
    run_records: int = 0
    work_item_records: int = 0
    orphan_records: int = 0
    chat_records: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def _stamp_records(db: AsyncSession, result: LedgerBackfillResult, *, dry_run: bool) -> int:
    """One batch of run records with no attribution yet; the number read."""
    from app.models.roi import RoiRunRecord

    rows = (
        await db.execute(
            select(RoiRunRecord.id, RoiRunRecord.run_id, RoiRunRecord.work_item_id, RoiRunRecord.agent_id)
            .where(RoiRunRecord.attribution.is_(None))
            .order_by(RoiRunRecord.id)
            .limit(_BATCH)
        )
    ).all()
    if not rows:
        return 0
    runs = {row.id: (row.run_id, row.agent_id) for row in rows if row.run_id is not None}
    items = {row.id: (row.work_item_id, row.agent_id) for row in rows if row.run_id is None and row.work_item_id}
    people: dict[Hashable, Person] = {}
    people.update(await persons_of_runs(db, runs))
    people.update(await persons_of_work_items(db, items))
    for row in rows:
        people.setdefault(row.id, (None, ATTRIBUTION_UNKNOWN))
    result.run_records += len(runs)
    result.work_item_records += len(items)
    result.orphan_records += len(rows) - len(runs) - len(items)
    if not dry_run:
        for record_id, (person, rule) in people.items():
            await db.execute(
                update(RoiRunRecord)
                .where(RoiRunRecord.id == record_id, RoiRunRecord.attribution.is_(None))
                .values(user_id=person, attribution=rule)
                .execution_options(synchronize_session=False)
            )
    return len(rows)


async def _stamp_chat_fleets(db: AsyncSession, result: LedgerBackfillResult, *, dry_run: bool) -> int:
    """One batch of chat records written before their fleet was stamped; the number read."""
    from app.models.roi import RoiChatRecord
    from app.services.fleet_runs import home_fleet_defaults_of

    rows = (
        await db.execute(
            select(RoiChatRecord.id, RoiChatRecord.agent_id)
            .where(RoiChatRecord.fleet_backfilled.is_(None))
            .order_by(RoiChatRecord.id)
            .limit(_BATCH)
        )
    ).all()
    if not rows:
        return 0
    homes = await home_fleet_defaults_of(db, [row.agent_id for row in rows if row.agent_id is not None])
    result.chat_records += len(rows)
    if not dry_run:
        for row in rows:
            home = homes.get(row.agent_id) if row.agent_id is not None else None
            await db.execute(
                update(RoiChatRecord)
                .where(RoiChatRecord.id == row.id, RoiChatRecord.fleet_backfilled.is_(None))
                .values(fleet_id=home.fleet_id if home else None, fleet_backfilled=True)
                .execution_options(synchronize_session=False)
            )
    return len(rows)


async def run_ledger_backfill(
    db: AsyncSession, *, dry_run: bool = False, budget_seconds: float = _BUDGET_SECONDS
) -> LedgerBackfillResult:
    """Stamp the person of past run records and the fleet of past chat records, batch by batch.

    Commits after each batch (a dry run reads one batch of each and writes
    nothing); stops when nothing is left or *budget_seconds* is spent. Never raises.
    """
    result = LedgerBackfillResult(dry_run=dry_run)
    started = time.monotonic()
    try:
        for stamp in (_stamp_records, _stamp_chat_fleets):
            while True:
                read = await stamp(db, result, dry_run=dry_run)
                if dry_run:
                    break
                await db.commit()
                if read < _BATCH or time.monotonic() - started > budget_seconds:
                    break
    except Exception as exc:  # bookkeeping never fails the maintenance
        await db.rollback()
        logger.error("ROI ledger backfill failed: %s", format_exception_message(exc), exc_info=True)
        return result
    if not dry_run and (result.run_records or result.work_item_records or result.orphan_records or result.chat_records):
        logger.info("ROI ledger backfill stamped %s", result.as_dict())
    return result


# ── Cost facts (slice L2) ─────────────────────────────────────────────────────

#: The class of a usage from its trace source (design 12.3): an automation, a chat, else the
#: platform's own (memory, evaluation, proxy). ONE rule, applied in SQL to traces and rollup alike.
_CLASS_SQL = (
    "CASE WHEN {alias}.source IN ('assignment', 'flow') THEN 'automation' "
    "WHEN {alias}.source IN ('agent-chat', 'general-chat') THEN 'chat' ELSE 'platform' END"
)

#: One closing at a time across replicas (transaction-scoped).
_CLOSE_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('roi_cost_facts_close'))"
#: Days closed per maintenance step: a cold start catches up over several ticks.
_CLOSE_MAX_DAYS = 14
#: A day's facts match its rollup when their costs differ by less than this.
_COST_TOLERANCE = Decimal("0.000001")

_TRACE_FACTS_SQL = f"""
    SELECT
        (t.started_at AT TIME ZONE 'UTC')::date AS day,
        t.attributed_user_id AS user_id,
        t.attribution AS attribution,
        t.agent_id AS agent_id,
        r.fleet_id AS fleet_id,
        {_CLASS_SQL.format(alias="t")} AS cost_class,
        COALESCE(SUM(t.total_cost) FILTER (WHERE t.total_cost IS NOT NULL), 0) AS cost,
        COUNT(*) AS trace_count
    FROM llm_traces t
    LEFT JOIN roi_run_records r ON r.run_id = t.assignment_run_id
    WHERE t.started_at >= :start
      AND t.started_at < :end
      AND t.source = ANY(:sources)
    GROUP BY 1, 2, 3, 4, 5, 6
"""  # noqa: S608 - the class rule is a module constant, not input

_ROLLUP_FACTS_SQL = f"""
    SELECT
        s.date AS day,
        s.attributed_user_id AS user_id,
        s.attribution AS attribution,
        s.agent_id AS agent_id,
        CAST(NULL AS uuid) AS fleet_id,
        {_CLASS_SQL.format(alias="s")} AS cost_class,
        COALESCE(SUM(s.total_cost), 0) AS cost,
        COALESCE(SUM(s.trace_count), 0) AS trace_count
    FROM llm_daily_stats s
    WHERE s.date = :day
    GROUP BY 1, 2, 3, 4, 5, 6
"""  # noqa: S608 - the class rule is a module constant, not input

#: Per complete day: the rollup's cost, the traces' cost and count, and the facts' cost and count.
_DAY_TOTALS_SQL = """
    SELECT d.day, d.rollup_cost, d.rollup_traces,
           (SELECT COALESCE(SUM(f.cost), 0) FROM roi_cost_facts f WHERE f.day = d.day) AS facts_cost,
           (SELECT COUNT(*) FROM roi_cost_facts f WHERE f.day = d.day) AS facts_rows
    FROM (
        SELECT s.date AS day, COALESCE(SUM(s.total_cost), 0) AS rollup_cost,
               COALESCE(SUM(s.trace_count), 0) AS rollup_traces
        FROM llm_daily_stats s
        WHERE s.date BETWEEN :first AND :last
        GROUP BY s.date
    ) d
    ORDER BY d.day DESC
"""

_DAY_TRACE_COUNT_SQL = """
    SELECT COUNT(*) FROM llm_traces t
    WHERE t.started_at >= :start AND t.started_at < :end AND t.source = ANY(:sources)
"""


def _day_bounds(first: date, last: date) -> dict[str, Any]:
    from app.models.llm_trace import LLM_TRACE_SOURCES

    start = datetime.combine(first, datetime.min.time(), tzinfo=UTC)
    end = datetime.combine(last, datetime.min.time(), tzinfo=UTC) + timedelta(days=1)
    return {"start": start, "end": end, "sources": list(LLM_TRACE_SOURCES)}


async def _with_home_fleets(db: AsyncSession, rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """*rows* with the agent's home fleet where the run left none, the grains that meet summed."""
    from app.services.fleet_runs import home_fleet_defaults_of

    homes = await home_fleet_defaults_of(
        db, [row["agent_id"] for row in rows if row["fleet_id"] is None and row["agent_id"] is not None]
    )
    facts: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        fleet_id = row["fleet_id"]
        if fleet_id is None and row["agent_id"] is not None and row["agent_id"] in homes:
            fleet_id = homes[row["agent_id"]].fleet_id
        key = (row["day"], row["user_id"], row["attribution"], row["agent_id"], fleet_id, row["cost_class"])
        fact = facts.setdefault(
            key,
            {
                "day": row["day"],
                "user_id": row["user_id"],
                "attribution": row["attribution"],
                "agent_id": row["agent_id"],
                "fleet_id": fleet_id,
                "cost_class": row["cost_class"],
                "cost": Decimal(0),
                "trace_count": 0,
            },
        )
        fact["cost"] += Decimal(row["cost"] or 0)
        fact["trace_count"] += int(row["trace_count"] or 0)
    return sorted(facts.values(), key=lambda f: (f["day"], str(f["user_id"]), str(f["agent_id"]), f["cost_class"]))


async def cost_facts(db: AsyncSession, *, first: date, last: date) -> list[dict[str, Any]]:
    """The cost facts of the days *first* to *last* read live from ``llm_traces`` (writes nothing).

    The query :func:`close_cost_days` writes a closed day with, so a live day and a closed one
    carry the same grain and the same figures.
    """
    rows = (await db.execute(text(_TRACE_FACTS_SQL), _day_bounds(first, last))).mappings().all()
    return await _with_home_fleets(db, [dict(row) for row in rows])


@dataclass
class CostCloseResult:
    """What one step closed: the days from their traces and from the rollup, and the rows written."""

    from_traces: int = 0
    from_rollup: int = 0
    rows: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def _close_day(db: AsyncSession, day: date, *, rollup_traces: int) -> tuple[str, int]:
    """Replace the facts of *day*, in the caller's transaction; ``(built_from, rows)``.

    From the traces while they are all there (as many as the rollup counted), else from the
    rollup, which outlives the purge (its fleet is the agent's home fleet now).
    """
    from app.models.roi import RoiCostFact

    bounds = _day_bounds(day, day)
    traces = int((await db.execute(text(_DAY_TRACE_COUNT_SQL), bounds)).scalar() or 0)
    if traces >= rollup_traces:
        built_from = "traces"
        rows = (await db.execute(text(_TRACE_FACTS_SQL), bounds)).mappings().all()
    else:
        built_from = "rollup"
        rows = (await db.execute(text(_ROLLUP_FACTS_SQL), {"day": day})).mappings().all()
    facts = await _with_home_fleets(db, [dict(row) for row in rows])
    await db.execute(delete(RoiCostFact).where(RoiCostFact.day == day).execution_options(synchronize_session=False))
    if facts:
        await db.execute(
            insert(RoiCostFact),
            [{**fact, "id": uuid.uuid4(), "built_from": built_from} for fact in facts],
        )
    return built_from, len(facts)


async def close_cost_days(
    db: AsyncSession, *, now: datetime | None = None, max_days: int = _CLOSE_MAX_DAYS
) -> CostCloseResult:
    """Close the complete days whose facts are missing or no longer match their rollup.

    A day is due once its ``llm_daily_stats`` rollup exists (the daily aggregation runs first):
    when it has no facts, or when their cost differs from the rollup's (a repricing rebuilt the
    rollup). Newest first, at most *max_days* per call, each day in its own transaction under
    the closing lock. Never raises: a failed day is rolled back and retried at the next step.
    """
    from app.services.trace_storage import latest_complete_day

    result = CostCloseResult()
    last = latest_complete_day(now)
    try:
        totals = (await db.execute(text(_DAY_TOTALS_SQL), {"first": date(1970, 1, 1), "last": last})).mappings().all()
        await db.rollback()
    except Exception as exc:  # bookkeeping never fails the maintenance
        await db.rollback()
        logger.error("ROI cost facts read failed: %s", format_exception_message(exc), exc_info=True)
        return result
    due = [
        row
        for row in totals
        if not row["facts_rows"] or abs(Decimal(row["facts_cost"]) - Decimal(row["rollup_cost"])) > _COST_TOLERANCE
    ][:max_days]
    for row in due:
        try:
            await db.execute(text(_CLOSE_LOCK_SQL))
            built_from, written = await _close_day(db, row["day"], rollup_traces=int(row["rollup_traces"] or 0))
            await db.commit()
        except Exception as exc:
            await db.rollback()
            logger.error("ROI cost facts of %s failed: %s", row["day"], format_exception_message(exc), exc_info=True)
            continue
        result.rows += written
        if built_from == "traces":
            result.from_traces += 1
        else:
            result.from_rollup += 1
    if result.from_traces or result.from_rollup:
        logger.info("ROI cost facts closed %s", result.as_dict())
    return result


#: How far back a window "since the start" reads the cost facts left open (the longest trace retention).
_OPEN_DAYS_HORIZON = timedelta(days=365)


async def ledger_facts(
    db: AsyncSession, *, first: date | None, last: date, currency: str | None, now: datetime
) -> tuple[list[Any], str]:
    """Every fact of the days *first* (``None``: since the start) to *last*, priced in *currency*.

    Value facts from ``roi_run_records`` and the counted ``roi_chat_records``; cost facts from
    the closed days of ``roi_cost_facts``, and live from ``llm_traces`` for the days after the
    newest closed one (:func:`cost_facts`, the same grain). Answers the facts and the currency used.
    """
    from app.models.roi import RoiChatRecord, RoiCostFact, RoiRunRecord
    from app.services import roi_chat, roi_figures, roi_report
    from app.services.roi_chat_backfill import user_minutes
    from app.services.roi_chat_series import counted_minutes

    pricing = await roi_report.load_pricing(db, currency)
    # A fleet's value is measured from its last factory reset (#2580): its earlier facts are left
    # out here, the one read every figure goes through, so the fleet view, its row of the
    # organization's breakdown and the organization total exclude the same part.
    marks = await fleet_value_marks.load_marks(db)

    def rate_of(unit: str) -> tuple[Any, float | None]:
        baseline = pricing.baselines.get(unit)
        if baseline is None:
            return None, None
        rate = (pricing.rates.get(baseline.rate_role) or {}).get(pricing.currency)
        return baseline, rate.amount if rate is not None else None

    start = datetime.combine(first, datetime.min.time(), tzinfo=UTC) if first else None
    end = datetime.combine(last, datetime.min.time(), tzinfo=UTC) + timedelta(days=1)
    facts: list[Any] = []

    runs = select(RoiRunRecord).where(RoiRunRecord.created_at < end)
    if start is not None:
        runs = runs.where(RoiRunRecord.created_at >= start)
    for rec in (await db.execute(runs)).scalars():
        if not fleet_value_marks.counts(rec.fleet_id, rec.created_at, marks):
            continue
        baseline, rate = rate_of(rec.unit)
        facts.append(
            roi_figures.run_fact(
                day=rec.created_at.date(),
                person_id=rec.user_id,
                attribution=rec.attribution,
                agent_id=rec.agent_id,
                fleet_id=rec.fleet_id,
                unit=rec.unit,
                units=rec.units,
                outcome=rec.outcome,
                review_min=rec.review_min,
                baseline=baseline,
                rate=rate,
            )
        )

    chats_query = select(RoiChatRecord).where(RoiChatRecord.created_at < end)
    if start is not None:
        chats_query = chats_query.where(RoiChatRecord.created_at >= start)
    chats = [
        c
        for c in (await db.execute(chats_query)).scalars()
        if fleet_value_marks.counts(c.fleet_id, c.created_at, marks)
    ]
    measured = await user_minutes(db, {c.conversation_id for c in chats if c.conversation_id})
    for chat in chats:
        if not roi_chat.is_counted(
            chat.signals or [], last_edit_at=chat.last_edit_at, now=now, deleted_at=chat.deleted_at
        ):
            continue
        unit = roi_chat.UNIT_BY_KIND[chat.kind]
        estimate = roi_chat.estimate(
            chat.kind,
            chat.size,
            user_min=measured.get(chat.conversation_id, 0.0) if chat.conversation_id else 0.0,
            baselines=pricing.baselines,
        )
        _, rate = rate_of(unit)
        facts.append(
            roi_figures.chat_fact(
                day=chat.created_at.date(),
                person_id=chat.user_id,
                agent_id=chat.agent_id,
                fleet_id=chat.fleet_id,
                unit=unit,
                minutes=counted_minutes(estimate, chat.adjusted_min),
                rate=rate,
            )
        )

    closed_query = select(RoiCostFact).where(RoiCostFact.day <= last)
    if first is not None:
        closed_query = closed_query.where(RoiCostFact.day >= first)
    closed = list((await db.execute(closed_query)).scalars())
    newest_closed = (await db.execute(select(func.max(RoiCostFact.day)))).scalar()
    open_first = newest_closed + timedelta(days=1) if newest_closed else (first or (now - _OPEN_DAYS_HORIZON).date())
    if first is not None:
        open_first = max(open_first, first)
    rows: list[Mapping[str, Any]] = [
        {
            "day": c.day,
            "user_id": c.user_id,
            "attribution": c.attribution,
            "agent_id": c.agent_id,
            "fleet_id": c.fleet_id,
            "cost_class": c.cost_class,
            "cost": c.cost,
        }
        for c in closed
    ]
    if open_first <= last:
        rows.extend(await cost_facts(db, first=open_first, last=last))
    if marks:
        rows = await _clip_cost_rows(db, rows, marks, first=first, last=last)
    facts.extend(
        roi_figures.LedgerFact(
            day=row["day"],
            person_id=row["user_id"],
            attribution=row["attribution"],
            agent_id=row["agent_id"],
            fleet_id=row["fleet_id"],
            cost_class=row["cost_class"],
            cost=float(row["cost"] or 0),
        )
        for row in rows
    )
    return facts, pricing.currency


async def _clip_cost_rows(
    db: AsyncSession,
    rows: list[Mapping[str, Any]],
    marks: Mapping[uuid.UUID, datetime],
    *,
    first: date | None,
    last: date,
) -> list[Mapping[str, Any]]:
    """*rows* without the cost a reset fleet spent before its mark (#2580).

    A day before the mark's day is left out whole; the mark's own day is read again from the
    traces from the mark's minute (the grain of :func:`cost_facts`), so the spend of the minutes
    after the reset still counts.
    """
    kept = [r for r in rows if fleet_value_marks.counts(r["fleet_id"], r["day"], marks)]
    for fleet_id, mark in marks.items():
        day = mark.date()
        if day > last or (first is not None and day < first):
            continue
        bounds = _day_bounds(day, day)
        bounds["start"] = mark
        live = (await db.execute(text(_TRACE_FACTS_SQL), bounds)).mappings().all()
        kept.extend(r for r in await _with_home_fleets(db, [dict(x) for x in live]) if r["fleet_id"] == fleet_id)
    return kept


async def figures(
    db: AsyncSession,
    *,
    scope: str,
    viewer: Any,
    scope_id: uuid.UUID | None = None,
    window: str = "30d",
    currency: str | None = None,
    now: datetime | None = None,
) -> Any:
    """THE function that computes a ROI figure (design 12.4): the facts of *window*, then
    ``roi_figures.compute`` for *scope* as *viewer* (a ``roi_figures.Viewer``) reads it.

    Costs follow the observability cost setting: masked, every cost-derived figure is ``None``.
    """
    from app.core.config import settings
    from app.services import roi_figures, roi_report

    now = now or datetime.now(UTC)
    first, last = _window_days(window, now)
    facts, used_currency = await ledger_facts(db, first=first, last=last, currency=currency, now=now)
    result = roi_figures.compute(
        facts,
        scope=scope,
        viewer=viewer,
        scope_id=scope_id,
        window_first=first,
        window_last=last,
        cost_masked=not settings.observability_cost_data_enabled,
        period_days=roi_report.window_days(window, now=now),
    )
    result.currency = used_currency
    return result


def _window_days(window: str, now: datetime) -> tuple[date | None, date]:
    """The first (``None``: since the start) and last day of *window* ending *now*."""
    from app.services import roi_report

    since = roi_report.window_since(window, now=now)
    return (since.date() if since is not None else None), now.date()


async def figures_for_subjects(
    db: AsyncSession,
    *,
    subject_type: str,
    ids: list[uuid.UUID],
    viewer: Any,
    window: str = "30d",
    currency: str | None = None,
    now: datetime | None = None,
) -> tuple[str, dict[uuid.UUID, Any]]:
    """The figures of every agent or fleet of one list page, from ONE load of the window's facts (slice L4).

    Each entry equals :func:`figures` of that object's scope for the same viewer and window
    (invariant d). Answers the currency used and the figures by id.
    """
    from app.core.config import settings
    from app.services import roi_figures, roi_report

    now = now or datetime.now(UTC)
    first, last = _window_days(window, now)
    if not ids:
        pricing = await roi_report.load_pricing(db, currency)
        return pricing.currency, {}
    facts, used_currency = await ledger_facts(db, first=first, last=last, currency=currency, now=now)
    by_id = roi_figures.compute_subjects(
        facts,
        subject_type=subject_type,
        ids=ids,
        viewer=viewer,
        window_first=first,
        window_last=last,
        cost_masked=not settings.observability_cost_data_enabled,
        period_days=roi_report.window_days(window, now=now),
    )
    for result in by_id.values():
        result.currency = used_currency
    return used_currency, by_id


async def chat_figures(
    db: AsyncSession,
    *,
    viewer: Any,
    mine: bool = False,
    agent_id: uuid.UUID | None = None,
    fleet_id: uuid.UUID | None = None,
    window: str = "30d",
    currency: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """The chat totals of the series' scope from the ledger (#12077 L4b, design 12.8): ``by_source.chat``.

    The scope is the one ``GET /roi/chat`` reads: ``mine``, else the agent, else the fleet, else
    the organization. Answers ``{scope, id, currency, cost_masked, since, totals}``, ``totals`` the
    chat row of :func:`figures` (zeros when the scope holds no chat fact), so the chat card and the
    Value page's By source can never differ. An agent or fleet the viewer does not see
    (``roi_access.reader_scope``, the rule of ``GET /roi?scope=agent|fleet``) answers ``None``:
    its every-person total is not theirs to read.
    """
    from app.services import roi_figures

    if mine:
        scope, scope_id = roi_figures.SCOPE_MINE, None
    elif agent_id is not None:
        if not viewer.sees_agent(agent_id):
            return None
        scope, scope_id = roi_figures.SCOPE_AGENT, agent_id
    elif fleet_id is not None:
        if not viewer.sees_fleet(fleet_id):
            return None
        scope, scope_id = roi_figures.SCOPE_FLEET, fleet_id
    else:
        scope, scope_id = roi_figures.SCOPE_ORGANIZATION, None
    result = await figures(db, scope=scope, viewer=viewer, scope_id=scope_id, window=window, currency=currency, now=now)
    row = next((r.totals for r in result.by_source if r.key == roi_figures.CLASS_CHAT), roi_figures.Totals())
    return {
        "scope": result.scope,
        "id": str(scope_id) if scope_id else None,
        "currency": result.currency,
        "cost_masked": result.cost_masked,
        "since": result.since.isoformat() if result.since else None,
        "totals": row.as_dict(cost_masked=result.cost_masked, period_days=result.period_days),
    }


async def viewer_for(db: AsyncSession, *, user_id: uuid.UUID | None, is_admin: bool) -> Any:
    """The ``roi_figures.Viewer`` of *user_id*: the agents and fleets of ``roi_access.reader_scope``.

    An administrator reads the organization, with "By person" when ``roi_by_person_visible``
    is on (D12-1); anyone else names the agents and fleets they see, the rest folded, under
    the k rule of ``roi_min_group_size`` (#12077 L5, design 12.5).
    """
    from app.services import roi_access, roi_figures

    scope = await roi_access.reader_scope(db, user_id=user_id, is_admin=is_admin)
    by_person, k = await anonymity_settings(db)
    organization = bool(scope.organization)
    return roi_figures.Viewer(
        user_id=user_id,
        organization=organization,
        agent_ids=frozenset(scope.agent_ids),
        fleet_ids=frozenset(scope.fleet_ids),
        by_person=organization and by_person,
        min_group_size=None if organization else k,
    )


async def anonymity_settings(db: AsyncSession) -> tuple[bool, int]:
    """``(roi_by_person_visible, roi_min_group_size)`` of the organization (#12077 L5).

    A k outside its bounds reads as the nearest bound; a failed read answers the defaults
    (By person off, k = 5): the rule never opens on an error.
    """
    from app.api.v1.platform_settings_helpers import get_platform_settings
    from app.services import roi_figures

    try:
        row = await get_platform_settings(db)
    except Exception as exc:  # the defaults, never a 500 and never the rule off
        logger.warning("ROI anonymity settings not read: %s", exc)
        return False, roi_figures.MIN_GROUP_SIZE_DEFAULT
    raw = getattr(row, "roi_min_group_size", None)
    k = roi_figures.MIN_GROUP_SIZE_DEFAULT if raw is None else int(raw)
    k = max(roi_figures.MIN_GROUP_SIZE_MIN, min(roi_figures.MIN_GROUP_SIZE_MAX, k))
    return bool(getattr(row, "roi_by_person_visible", False)), k
