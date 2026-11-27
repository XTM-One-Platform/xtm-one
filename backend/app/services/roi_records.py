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

"""ROI storage: record what a run produced, read the organization's baselines.

Slice 2 of issue #8990 (``dev-docs/ctem-fleets.md`` section 5.2), on top of
the pure core ``services/roi.py``:

* :func:`record_run_units` is called once a run of an agent whose
  ``output_schema`` declares ``x-xtm-roi`` completes: it counts the units in
  the run's structured output (never the model's prose), reads the run's LLM
  cost from its traces and its duration, and writes one ``roi_run_records``
  row (one per run: a second call for the same run updates nothing). It is
  best effort: a failure is logged and never fails the run.
* :func:`set_run_outcome` and :func:`apply_review` record the review of a
  run or a work item (accepted, modified, rejected, or back to unreviewed),
  the measured review minutes, the reviewer and a note; a record is written
  unreviewed (``outcome`` ``None``, issue #9310). :func:`list_records` is the
  review queue.
* :func:`effective_baselines` is the catalog with the organization's
  overrides applied and the organization's custom units added, and the set
  of overridden units the figures label; :func:`upsert_override` /
  :func:`delete_override` are the admin writes.
* Custom units (issue #9252, ``dev-docs/roi-outcomes.md`` section 3.1):
  :func:`create_unit`, :func:`update_unit` and :func:`archive_unit` write the
  organization's own units next to the built-in catalog, each write a new
  version in ``roi_unit_versions``; :func:`known_units` is the set an agent may
  declare (built-ins and custom units not archived).
* Declarations (issue #9285, ``dev-docs/roi-outcomes.md`` section 3.2):
  :func:`put_declaration` / :func:`delete_declaration` set the unit an agent,
  an assignment or a fleet declares without editing an ``output_schema``;
  :func:`resolve_declaration` picks the one a run counts under (assignment,
  agent, legacy ``x-xtm-roi``, home fleet); :func:`record_work_item_units`
  writes the ``per_work_item`` record of a work item that reached ``done``.
* Review row (``dev-docs/roi-outcomes.md`` section 6.2, slice O7):
  :func:`review_block` is the ``roi`` block the run and work item detail
  responses carry; :func:`may_review` is the rule the review route applies
  (a fleet's ROI readers review its records, ``roi_access``, issue #9350).
* Rates by role (issue #9321, ``dev-docs/roi-outcomes.md`` section 3.4):
  :func:`put_rate` / :func:`delete_rate` set the organization's hourly rate of
  a built-in or custom role in a currency; :func:`list_rates` is what the
  report layers over the built-in table.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.models.roi import RoiBaselineOverride, RoiDeclaration, RoiRate, RoiRunRecord, RoiUnit, RoiUnitVersion
from app.services import roi

logger = logging.getLogger(__name__)


def structured_output_of(result_summary: str | None) -> Any:
    """The run's structured output parsed from its result, ``None`` when it is not JSON."""
    if not result_summary:
        return None
    try:
        return json.loads(result_summary)
    except (TypeError, ValueError):
        return None


def run_duration_s(started_at: datetime | None, completed_at: datetime | None) -> float:
    """Seconds between start and end, 0 when either is unknown."""
    if started_at is None or completed_at is None:
        return 0.0
    return max((completed_at - started_at).total_seconds(), 0.0)


async def _run_llm_cost(db: AsyncSession, run_id: uuid.UUID) -> float:
    from app.models.llm_trace import LLMTrace

    total = (
        await db.execute(
            select(func.coalesce(func.sum(LLMTrace.total_cost), 0)).where(LLMTrace.assignment_run_id == run_id)
        )
    ).scalar()
    return float(total or 0)


SUBJECT_AGENT = "agent"
SUBJECT_ASSIGNMENT = "assignment"
SUBJECT_FLEET = "fleet"
SUBJECT_TYPES: tuple[str, ...] = (SUBJECT_AGENT, SUBJECT_ASSIGNMENT, SUBJECT_FLEET)

COUNT_PER_RUN = "per_run"
COUNT_PER_OUTPUT_ITEM = "per_output_item"
COUNT_PER_WORK_ITEM = "per_work_item"
COUNT_MODES: tuple[str, ...] = (COUNT_PER_RUN, COUNT_PER_OUTPUT_ITEM, COUNT_PER_WORK_ITEM)

#: Where a resolved declaration came from, in resolution order.
SOURCE_ASSIGNMENT = "assignment"
SOURCE_AGENT = "agent"
SOURCE_OUTPUT_SCHEMA = "output_schema"
SOURCE_FLEET = "fleet"


@dataclass(frozen=True)
class ResolvedDeclaration:
    """The unit a run or a work item counts under, and how."""

    unit: str
    count_mode: str
    count_path: str | None = None
    units_per_count: int = 1
    source: str = SOURCE_OUTPUT_SCHEMA


def legacy_declaration(output_schema: Mapping[str, Any] | None) -> ResolvedDeclaration | None:
    """The ``x-xtm-roi`` of an ``output_schema`` as a ``per_output_item`` declaration."""
    declaration = roi.unit_declaration(output_schema)
    if declaration is None:
        return None
    return ResolvedDeclaration(
        unit=declaration.unit,
        count_mode=COUNT_PER_OUTPUT_ITEM,
        count_path=declaration.count_path,
        source=SOURCE_OUTPUT_SCHEMA,
    )


def _of_row(row: RoiDeclaration | Any, source: str) -> ResolvedDeclaration:
    return ResolvedDeclaration(
        unit=row.unit,
        count_mode=row.count_mode,
        count_path=row.count_path,
        units_per_count=max(int(row.units_per_count or 1), 1),
        source=source,
    )


def pick_declaration(
    *,
    assignment: Any | None,
    agent: Any | None,
    output_schema: Mapping[str, Any] | None,
    fleet: Any | None,
) -> ResolvedDeclaration | None:
    """Resolution order of design section 3.2, first match wins (pure).

    ``assignment``, ``agent`` and ``fleet`` are the stored declaration rows of
    each subject (``None`` when it declares none).
    """
    if assignment is not None:
        return _of_row(assignment, SOURCE_ASSIGNMENT)
    if agent is not None:
        return _of_row(agent, SOURCE_AGENT)
    legacy = legacy_declaration(output_schema)
    if legacy is not None:
        return legacy
    if fleet is not None:
        return _of_row(fleet, SOURCE_FLEET)
    return None


async def _declarations_of(db: AsyncSession, subjects: list[tuple[str, uuid.UUID]]) -> dict[str, RoiDeclaration]:
    pairs = [(t, i) for t, i in subjects if i is not None]
    if not pairs:
        return {}
    rows = (
        (
            await db.execute(
                select(RoiDeclaration).where(tuple_(RoiDeclaration.subject_type, RoiDeclaration.subject_id).in_(pairs))
            )
        )
        .scalars()
        .all()
    )
    return {row.subject_type: row for row in rows}


async def resolve_declaration(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID | None,
    output_schema: Mapping[str, Any] | None = None,
    assignment_id: uuid.UUID | None = None,
    fleet_id: uuid.UUID | None = None,
) -> ResolvedDeclaration | None:
    """The declaration a run of *agent_id* (under *assignment_id*) counts under.

    Assignment, then agent, then the agent's legacy ``x-xtm-roi``, then the
    home fleet *fleet_id*. ``None`` when nothing declares a unit.
    """
    subjects: list[tuple[str, uuid.UUID]] = []
    if assignment_id is not None:
        subjects.append((SUBJECT_ASSIGNMENT, assignment_id))
    if agent_id is not None:
        subjects.append((SUBJECT_AGENT, agent_id))
    if fleet_id is not None:
        subjects.append((SUBJECT_FLEET, fleet_id))
    rows = await _declarations_of(db, subjects)
    return pick_declaration(
        assignment=rows.get(SUBJECT_ASSIGNMENT),
        agent=rows.get(SUBJECT_AGENT),
        output_schema=output_schema,
        fleet=rows.get(SUBJECT_FLEET),
    )


#: ``per_work_item`` selectors in ``count_path`` (#10412): only the done items that match count.
SELECT_PULL_REQUEST_MERGED = "pull_request_merged"
SELECT_RESULT_CONTAINS = "result_contains:"


def work_item_selector_error(count_path: str | None) -> str | None:
    """Why *count_path* is not a ``per_work_item`` selector, ``None`` when empty or valid."""
    path = (count_path or "").strip()
    if not path or path == SELECT_PULL_REQUEST_MERGED:
        return None
    if path.startswith(SELECT_RESULT_CONTAINS):
        if any(t.strip() for t in path[len(SELECT_RESULT_CONTAINS) :].split("|")):
            return None
        return "result_contains: needs at least one text, several separated by |"
    return f"a per_work_item selector is {SELECT_PULL_REQUEST_MERGED} or {SELECT_RESULT_CONTAINS}TEXT|TEXT"


def units_of_work_item(declaration: ResolvedDeclaration, *, payload: Any, result: str | None) -> int:
    """Units a done work item produces under a ``per_work_item`` *declaration* (pure).

    No selector: ``units_per_count``. ``pull_request_merged``: only when the
    item names a merged pull request (``payload.pull_requests``).
    ``result_contains:A|B``: only when the completion result holds one of the
    texts, case-insensitive. A non-matching item counts 0.
    """
    path = (declaration.count_path or "").strip()
    if not path:
        return declaration.units_per_count
    if path == SELECT_PULL_REQUEST_MERGED:
        entries = payload.get("pull_requests") if isinstance(payload, Mapping) else None
        merged = isinstance(entries, list) and any(
            isinstance(e, Mapping) and e.get("state") == "merged" for e in entries
        )
        return declaration.units_per_count if merged else 0
    if path.startswith(SELECT_RESULT_CONTAINS):
        text = (result or "").casefold()
        needles = [t.strip().casefold() for t in path[len(SELECT_RESULT_CONTAINS) :].split("|") if t.strip()]
        return declaration.units_per_count if text and any(n in text for n in needles) else 0
    return 0


#: Summaries a run ends with when a gate skipped it: it ran no agent and
#: produced nothing (``assignment_activation_check.SKIP_SUMMARY_PREFIX`` and
#: ``assignment_engine._ACTIVATION_SKIP_SUMMARY_PREFIX``, pinned by a test).
GATE_SKIP_SUMMARY_PREFIXES: tuple[str, ...] = ("Skipped: gate check", "Skipped: activation check returned")


def gate_skipped(result_summary: str | None) -> bool:
    """Whether a completed run was ended by its tool check or its LLM gate."""
    return result_summary is not None and result_summary.startswith(GATE_SKIP_SUMMARY_PREFIXES)


def units_of_run(declaration: ResolvedDeclaration, *, status: str | None, result_summary: str | None) -> int | None:
    """Units a finished run produced under *declaration*; ``None`` when runs do not count.

    ``per_run``: ``units_per_count`` for a completed run, 0 otherwise; a run a
    gate skipped (tool check or activation prompt) completed with nothing done
    and counts 0, in the live hook and in the backfill alike.
    ``per_output_item``: the count at the declared path of the structured
    output (today's rule). ``per_work_item``: counted at the work item's
    completion, never at the run's.
    """
    if declaration.count_mode == COUNT_PER_RUN:
        if status != "completed" or gate_skipped(result_summary):
            return 0
        return declaration.units_per_count
    if declaration.count_mode == COUNT_PER_OUTPUT_ITEM:
        if not declaration.count_path:
            return 0
        counted = roi.count_units(
            roi.UnitDeclaration(unit=declaration.unit, count_path=declaration.count_path),
            structured_output_of(result_summary),
        )
        return counted * declaration.units_per_count
    return None


async def record_run_units(
    db: AsyncSession,
    *,
    run: Any,
    output_schema: Mapping[str, Any] | None = None,
    fleet_id: uuid.UUID | None = None,
    declaration: ResolvedDeclaration | None = None,
) -> RoiRunRecord | None:
    """Write the ROI record of a completed run whose agent declares a unit.

    ``run`` is an ``AssignmentRun`` (or any object with ``id``, ``agent_id``,
    ``status``, ``result_summary``, ``started_at`` and ``completed_at``).
    *declaration* is the resolved one (:func:`resolve_declaration`); without
    it the legacy ``x-xtm-roi`` of *output_schema* is read, as before #9285.
    Returns the row written, ``None`` when nothing declares a unit, the
    declaration counts per work item, the run already has a record, or the
    write failed (logged, never raised).
    """
    if declaration is None:
        declaration = legacy_declaration(output_schema)
    if declaration is None:
        return None
    try:
        run_id = getattr(run, "id", None)
        units = units_of_run(
            declaration,
            status=getattr(run, "status", "completed"),
            result_summary=getattr(run, "result_summary", None),
        )
        if units is None:
            return None
        llm_cost = await _run_llm_cost(db, run_id) if run_id is not None else 0.0
        completed_at = getattr(run, "completed_at", None) or datetime.now(UTC)
        from app.services.roi_ledger import person_of_run

        # Who the units count for, by the trace rules (#12077, ``roi_ledger``).
        person, attribution = await person_of_run(
            db, run_id=run_id, agent_id=getattr(run, "agent_id", None), user_id=getattr(run, "user_id", None)
        )
        values = {
            "id": uuid.uuid4(),
            "run_id": run_id,
            "agent_id": getattr(run, "agent_id", None),
            "user_id": person,
            "attribution": attribution,
            "fleet_id": fleet_id,
            "unit": declaration.unit[:64],
            "units": units,
            "llm_cost": llm_cost,
            "duration_s": run_duration_s(getattr(run, "started_at", None), completed_at),
            # Unreviewed until a person marks it (#9310).
            "outcome": None,
        }
        stmt = pg_insert(RoiRunRecord).values(**values).on_conflict_do_nothing(index_elements=["run_id"])
        # A savepoint: a failed insert rolls back alone and never poisons the run's transaction.
        async with db.begin_nested():
            written = (await db.execute(stmt.returning(RoiRunRecord.id))).scalar()
        if written is None:
            return None
        return RoiRunRecord(**values)
    except Exception as exc:  # accounting never fails a run
        logger.warning("ROI record of run %s not written: %s", getattr(run, "id", "?"), format_exception_message(exc))
        return None


async def record_work_item_units(
    db: AsyncSession,
    *,
    work_item_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    payload: Any = None,
    result: str | None = None,
) -> RoiRunRecord | None:
    """Write the ``per_work_item`` record of a work item that reached ``done``.

    The owner agent's declaration (or its home fleet's) must count
    ``per_work_item``, and the item must match its selector
    (:func:`units_of_work_item`, #10412); one record per work item, a second
    call writes nothing. Best effort in its own savepoint: a failure is logged and never
    fails the completion.
    """
    if agent_id is None:
        return None
    try:
        from app.models.agent import Agent
        from app.services.fleet_runs import home_fleet_defaults

        fleet_id = (await home_fleet_defaults(db, agent_id)).fleet_id
        # A savepoint: a failed read never poisons the completion's transaction.
        async with db.begin_nested():
            output_schema = (await db.execute(select(Agent.output_schema).where(Agent.id == agent_id))).scalar()
            declaration = await resolve_declaration(
                db,
                agent_id=agent_id,
                output_schema=output_schema if isinstance(output_schema, Mapping) else None,
                fleet_id=fleet_id,
            )
        if declaration is None or declaration.count_mode != COUNT_PER_WORK_ITEM:
            return None
        units = units_of_work_item(declaration, payload=payload, result=result)
        if units <= 0:
            return None
        from app.services.roi_ledger import person_of_work_item

        # The person the item works for (#12077, ``roi_ledger``).
        person, attribution = await person_of_work_item(db, work_item_id=work_item_id, agent_id=agent_id)
        values = {
            "id": uuid.uuid4(),
            "run_id": None,
            "work_item_id": work_item_id,
            "agent_id": agent_id,
            "user_id": person,
            "attribution": attribution,
            "fleet_id": fleet_id,
            "unit": declaration.unit[:64],
            "units": units,
            "llm_cost": 0.0,
            "duration_s": 0.0,
            # Unreviewed until a person marks it (#9310).
            "outcome": None,
        }
        stmt = pg_insert(RoiRunRecord).values(**values).on_conflict_do_nothing(index_elements=["work_item_id"])
        async with db.begin_nested():
            written = (await db.execute(stmt.returning(RoiRunRecord.id))).scalar()
        if written is None:
            return None
        return RoiRunRecord(**values)
    except Exception as exc:  # accounting never fails a completion
        logger.warning("ROI record of work item %s not written: %s", work_item_id, format_exception_message(exc))
        return None


async def get_declaration(db: AsyncSession, subject_type: str, subject_id: uuid.UUID) -> RoiDeclaration | None:
    """The declaration a subject stores, ``None`` when it has none."""
    return (
        await db.execute(
            select(RoiDeclaration).where(
                RoiDeclaration.subject_type == subject_type, RoiDeclaration.subject_id == subject_id
            )
        )
    ).scalar_one_or_none()


def validate_declaration(
    *, subject_type: str, unit: str, count_mode: str, count_path: str | None, units_per_count: int, known: set[str]
) -> None:
    """Raise ``ValueError`` naming what is wrong with a declaration."""
    if subject_type not in SUBJECT_TYPES:
        raise ValueError(f"subject_type must be one of {', '.join(SUBJECT_TYPES)}")
    if count_mode not in COUNT_MODES:
        raise ValueError(f"count_mode must be one of {', '.join(COUNT_MODES)}")
    if unit not in known:
        raise ValueError(f"unit {unit!r} is not a known unit")
    if count_mode == COUNT_PER_OUTPUT_ITEM and not (count_path or "").strip():
        raise ValueError("count_path is required when units are counted per output item")
    if count_mode == COUNT_PER_WORK_ITEM:
        selector_error = work_item_selector_error(count_path)
        if selector_error:
            raise ValueError(selector_error)
    if units_per_count < 1:
        raise ValueError("units_per_count must be at least 1")


async def put_declaration(
    db: AsyncSession,
    *,
    subject_type: str,
    subject_id: uuid.UUID,
    unit: str,
    count_mode: str,
    count_path: str | None = None,
    units_per_count: int = 1,
    user_id: uuid.UUID | None = None,
) -> RoiDeclaration:
    """Create or replace a subject's declaration; the unit must be known (not archived)."""
    validate_declaration(
        subject_type=subject_type,
        unit=unit,
        count_mode=count_mode,
        count_path=count_path,
        units_per_count=units_per_count,
        known=await known_units(db),
    )
    keeps_path = count_mode in (COUNT_PER_OUTPUT_ITEM, COUNT_PER_WORK_ITEM)
    path = (count_path.strip() or None) if keeps_path and count_path else None
    row = await get_declaration(db, subject_type, subject_id)
    if row is None:
        row = RoiDeclaration(id=uuid.uuid4(), subject_type=subject_type, subject_id=subject_id)
        db.add(row)
    row.unit = unit
    row.count_mode = count_mode
    row.count_path = path
    row.units_per_count = units_per_count
    row.updated_by_user_id = user_id
    row.updated_at = datetime.now(UTC)
    await db.flush()
    return row


async def delete_declaration(db: AsyncSession, subject_type: str, subject_id: uuid.UUID) -> bool:
    """Drop a subject's declaration; ``False`` when it had none."""
    row = await get_declaration(db, subject_type, subject_id)
    if row is None:
        return False
    await db.delete(row)
    await db.flush()
    return True


#: The longest review note kept (``roi_run_records.review_note``).
REVIEW_NOTE_MAX = 500


def apply_review(
    row: RoiRunRecord,
    *,
    outcome: str | None,
    review_min: float | None = None,
    note: str | None = None,
    user_id: uuid.UUID | None = None,
) -> None:
    """Write a review on *row*; ``outcome`` ``None`` returns it to unreviewed.

    Raises ``ValueError`` naming a bad outcome, negative minutes or a note
    over :data:`REVIEW_NOTE_MAX` characters. Back to unreviewed clears the
    reviewer, the time and the note; the measured minutes are kept only when
    given again.
    """
    if outcome is not None and outcome not in roi.OUTCOMES:
        raise ValueError(f"outcome {outcome!r} is not one of {', '.join(roi.OUTCOMES)}")
    if review_min is not None and review_min < 0:
        raise ValueError("review_min cannot be negative")
    if note is not None and len(note) > REVIEW_NOTE_MAX:
        raise ValueError(f"note cannot be longer than {REVIEW_NOTE_MAX} characters")
    row.outcome = outcome
    if outcome is None:
        row.reviewed_by_user_id = None
        row.reviewed_at = None
        row.review_note = None
        row.review_min = review_min
        return
    row.reviewed_by_user_id = user_id
    row.reviewed_at = datetime.now(UTC)
    row.review_note = (note.strip() or None) if note is not None else None
    if review_min is not None:
        row.review_min = review_min


async def set_run_outcome(
    db: AsyncSession,
    run_id: uuid.UUID,
    *,
    outcome: str | None,
    review_min: float | None = None,
    note: str | None = None,
    user_id: uuid.UUID | None = None,
) -> bool:
    """Record the review of a run; ``False`` when it has no ROI record."""
    row = (await db.execute(select(RoiRunRecord).where(RoiRunRecord.run_id == run_id))).scalar_one_or_none()
    if row is None:
        if outcome is not None and outcome not in roi.OUTCOMES:
            raise ValueError(f"outcome {outcome!r} is not one of {', '.join(roi.OUTCOMES)}")
        return False
    apply_review(row, outcome=outcome, review_min=review_min, note=note, user_id=user_id)
    return True


async def get_record(db: AsyncSession, record_id: uuid.UUID) -> RoiRunRecord | None:
    """One ROI record by its id, ``None`` when absent."""
    return await db.get(RoiRunRecord, record_id)


def _records_query(
    query: Any,
    *,
    agent_id: uuid.UUID | None,
    fleet_id: uuid.UUID | None,
    outcome: str | None,
    since: datetime | None,
    search: str | None = None,
) -> Any:
    """*query* narrowed the way the report counts (``roi_report.load_report``): one filter for the list and its count.

    *search* keeps the records whose title (:func:`record_titles`: the work
    item's title, else the run's assignment name) contains it, case-insensitive,
    so the list's search box narrows the rows and the count alike (#10570).
    """
    if agent_id is not None:
        query = query.where(RoiRunRecord.agent_id == agent_id)
    if fleet_id is not None:
        query = query.where(RoiRunRecord.fleet_id == fleet_id)
    if since is not None:
        query = query.where(RoiRunRecord.created_at >= since)
    if fleet_id is not None:
        # A fleet's records are counted from its last factory reset (#2580), as the report counts them.
        from app.services.fleet_value_marks import counted_clause

        query = query.where(counted_clause(RoiRunRecord.created_at, RoiRunRecord.fleet_id))
    if outcome == roi.OUTCOME_UNREVIEWED:
        query = query.where(RoiRunRecord.outcome.is_(None))
    elif outcome is not None:
        if outcome not in roi.OUTCOMES:
            raise ValueError(f"outcome must be one of {', '.join((*roi.OUTCOMES, roi.OUTCOME_UNREVIEWED))}")
        query = query.where(RoiRunRecord.outcome == outcome)
    needle = (search or "").strip()
    if needle:
        from app.models.assignment import Assignment, AssignmentRun
        from app.models.work_item import WorkItem

        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        items = select(WorkItem.id).where(WorkItem.title.ilike(pattern, escape="\\"))
        runs = (
            select(AssignmentRun.id)
            .join(Assignment, Assignment.id == AssignmentRun.assignment_id)
            .where(Assignment.name.ilike(pattern, escape="\\"))
        )
        query = query.where(
            or_(
                RoiRunRecord.work_item_id.in_(items),
                RoiRunRecord.work_item_id.is_(None) & RoiRunRecord.run_id.in_(runs),
            )
        )
    return query


async def list_records(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID | None = None,
    fleet_id: uuid.UUID | None = None,
    outcome: str | None = None,
    since: datetime | None = None,
    search: str | None = None,
    direction: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> list[RoiRunRecord]:
    """The ROI records, newest first: the review queue with ``outcome="unreviewed"``.

    *outcome* is one of :data:`roi.OUTCOMES`, ``"unreviewed"`` (a ``NULL``
    outcome) or ``None`` for every record; *since* keeps the records of a
    report window (``roi_report.window_since``), so a list counts what the
    report's figures count (#10570); *search* keeps the records whose title
    contains it; *direction* (``"desc"`` or ``"asc"``) orders them by date,
    the list's one sortable column.
    """
    if direction not in ("asc", "desc"):
        raise ValueError("direction must be asc or desc")
    query = _records_query(
        select(RoiRunRecord), agent_id=agent_id, fleet_id=fleet_id, outcome=outcome, since=since, search=search
    )
    by_date = RoiRunRecord.created_at.asc() if direction == "asc" else RoiRunRecord.created_at.desc()
    query = query.order_by(by_date, RoiRunRecord.id).offset(offset).limit(limit)
    return list((await db.execute(query)).scalars().all())


async def count_records(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID | None = None,
    fleet_id: uuid.UUID | None = None,
    outcome: str | None = None,
    since: datetime | None = None,
    search: str | None = None,
) -> int:
    """How many records :func:`list_records` pages through under the same filter (#10570)."""
    query = _records_query(
        select(func.count()).select_from(RoiRunRecord),
        agent_id=agent_id,
        fleet_id=fleet_id,
        outcome=outcome,
        since=since,
        search=search,
    )
    return int((await db.execute(query)).scalar_one())


async def record_titles(db: AsyncSession, rows: Iterable[RoiRunRecord]) -> dict[uuid.UUID, str]:
    """Each record's title by record id: its work item's title, else its run's assignment name (#10570)."""
    from app.models.assignment import Assignment, AssignmentRun
    from app.models.work_item import WorkItem

    rows = list(rows)
    item_ids = {r.work_item_id for r in rows if r.work_item_id is not None}
    run_ids = {r.run_id for r in rows if r.run_id is not None and r.work_item_id is None}
    items: dict[uuid.UUID, str] = {}
    runs: dict[uuid.UUID, str] = {}
    if item_ids:
        found = await db.execute(select(WorkItem.id, WorkItem.title).where(WorkItem.id.in_(item_ids)))
        items = {wid: title for wid, title in found.all()}
    if run_ids:
        found = await db.execute(
            select(AssignmentRun.id, Assignment.name)
            .join(Assignment, Assignment.id == AssignmentRun.assignment_id)
            .where(AssignmentRun.id.in_(run_ids))
        )
        runs = {rid: name for rid, name in found.all()}
    titles: dict[uuid.UUID, str] = {}
    for r in rows:
        title = items.get(r.work_item_id) if r.work_item_id is not None else runs.get(r.run_id) if r.run_id else None
        if title:
            titles[r.id] = title
    return titles


def record_dict(row: RoiRunRecord) -> dict[str, Any]:
    """The JSON shape of one ROI record (``outcome`` ``None`` while unreviewed)."""
    return {
        "id": str(row.id),
        # A work item's record is not a run: the records list says which (#10570).
        "kind": "work_item" if row.work_item_id else "run",
        "run_id": str(row.run_id) if row.run_id else None,
        "work_item_id": str(row.work_item_id) if row.work_item_id else None,
        "agent_id": str(row.agent_id) if row.agent_id else None,
        "fleet_id": str(row.fleet_id) if row.fleet_id else None,
        "unit": row.unit,
        "units": row.units,
        "llm_cost": row.llm_cost,
        "duration_s": row.duration_s,
        "outcome": row.outcome,
        "review_min": row.review_min,
        "reviewed_by_user_id": str(row.reviewed_by_user_id) if row.reviewed_by_user_id else None,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        "review_note": row.review_note,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def may_review(
    row: RoiRunRecord | Any,
    *,
    user_id: uuid.UUID | None,
    is_admin: bool,
    subject_owner_id: uuid.UUID | None,
    company_managed: bool = False,
    fleet_reader: bool = False,
) -> bool:
    """Whether *user_id* may mark *row*: the rule of ``PUT /roi/records/{id}/outcome``.

    An administrator always; otherwise the owner of the run (never a
    company-managed one) or of the work item, or a reader of the record's
    fleet ROI (*fleet_reader*, ``roi_access.fleet_roi_reader``, issue #9350),
    and only while the record is unreviewed or reviewed by that same person.
    """
    if is_admin:
        return True
    if user_id is None or company_managed or (subject_owner_id != user_id and not fleet_reader):
        return False
    return row.reviewed_by_user_id is None or row.reviewed_by_user_id == user_id


def review_view(
    row: RoiRunRecord | Any,
    *,
    baseline: roi.Baseline | None,
    can_review: bool,
) -> dict[str, Any]:
    """The ``roi`` block of a run or work item response (``dev-docs/roi-outcomes.md`` 6.2).

    What the outcome review row needs and nothing more: the record id it
    writes to, the unit by name, the outcome (``None`` while unreviewed), the
    measured review minutes and the unit's own as a placeholder, the note and
    whether the reader may change it. No hours, no value: the figures stay on
    the ROI views.
    """
    return {
        "record_id": str(row.id),
        "unit": row.unit,
        "unit_name": baseline.name if baseline is not None else row.unit,
        "units": row.units,
        "outcome": row.outcome,
        "review_min": row.review_min,
        "default_review_min": baseline.review_min if baseline is not None else None,
        "review_note": row.review_note,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        "can_review": can_review,
    }


async def review_block(
    db: AsyncSession,
    *,
    run_id: uuid.UUID | None = None,
    work_item_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None,
    is_admin: bool,
    subject_owner_id: uuid.UUID | None,
    company_managed: bool = False,
) -> dict[str, Any] | None:
    """The ``roi`` block of one run or one work item, ``None`` when it has no record.

    Never raises: the block is best effort on a detail response.
    """
    if (run_id is None) == (work_item_id is None):
        raise ValueError("pass exactly one of run_id and work_item_id")
    try:
        column = RoiRunRecord.run_id if run_id is not None else RoiRunRecord.work_item_id
        row = (await db.execute(select(RoiRunRecord).where(column == (run_id or work_item_id)))).scalar_one_or_none()
        if row is None:
            return None
        catalog, _ = await effective_baselines(db)
        fleet_reader = False
        if not is_admin and subject_owner_id != user_id and row.fleet_id is not None:
            from app.services.roi_access import fleet_roi_reader

            fleet_reader = await fleet_roi_reader(db, user_id=user_id, is_admin=False, fleet_id=row.fleet_id)
        allowed = may_review(
            row,
            user_id=user_id,
            is_admin=is_admin,
            subject_owner_id=subject_owner_id,
            company_managed=company_managed,
            fleet_reader=fleet_reader,
        )
        return review_view(row, baseline=catalog.get(row.unit), can_review=allowed)
    except Exception as exc:  # a detail response never fails on its ROI block
        logger.warning("ROI block of %s not read: %s", run_id or work_item_id, format_exception_message(exc))
        return None


def apply_overrides(
    overrides: list[RoiBaselineOverride] | list[Any],
    catalog: Mapping[str, roi.Baseline] | None = None,
) -> tuple[dict[str, roi.Baseline], set[str]]:
    """The catalog with ``overrides`` applied, and the units they override.

    An override of a unit the catalog does not know is ignored (a custom
    agent's unit carries no baseline until slice 3 lets an admin add one).
    """
    merged = dict(catalog if catalog is not None else roi.BASELINES_BY_UNIT)
    overridden: set[str] = set()
    for o in overrides:
        base = merged.get(o.unit)
        if base is None:
            continue
        merged[o.unit] = replace(
            base,
            manual_min_low=float(o.manual_min_low),
            manual_min_likely=float(o.manual_min_likely),
            manual_min_high=float(o.manual_min_high),
            review_min=float(o.review_min),
            minutes_label=roi.LABEL_OVERRIDE,
            minutes_source=o.source or "organization override",
        )
        overridden.add(o.unit)
    return merged, overridden


async def effective_baselines(db: AsyncSession) -> tuple[dict[str, roi.Baseline], set[str]]:
    """The organization's baselines: the default catalog with its overrides applied,
    plus its custom units (archived ones included, so their history still counts).
    """
    rows = list((await db.execute(select(RoiBaselineOverride))).scalars().all())
    custom = list((await db.execute(select(RoiUnit))).scalars().all())
    catalog = dict(roi.BASELINES_BY_UNIT)
    for unit in custom:
        catalog.setdefault(unit.key, custom_baseline(unit))
    return apply_overrides(rows, catalog)


# ── Custom units (#9252) ─────────────────────────────────────────────

#: A custom unit's key: a slug, never a built-in key.
UNIT_KEY_PATTERN = re.compile(r"^[a-z0-9_]{2,64}$")

#: The figure label of a custom unit's minutes.
LABEL_CUSTOM_UNIT = "organization unit"


def custom_baseline(unit: RoiUnit | Any) -> roi.Baseline:
    """The :class:`roi.Baseline` a custom unit row stands for."""
    return roi.Baseline(
        unit=unit.key,
        name=unit.name,
        fleet="",
        role=unit.role or "",
        manual_min_low=float(unit.manual_min_low),
        manual_min_likely=float(unit.manual_min_likely),
        manual_min_high=float(unit.manual_min_high),
        review_min=float(unit.review_min),
        rate_role=unit.rate_role or "analyst",
        minutes_label=LABEL_CUSTOM_UNIT,
        minutes_source=unit.source or LABEL_CUSTOM_UNIT,
    )


def validate_unit(
    *,
    name: str,
    low: float,
    likely: float,
    high: float,
    review: float,
    rate_role: str,
    rate_roles: Iterable[str] = (),
) -> None:
    """Raise ``ValueError`` naming what is wrong with a custom unit's fields.

    ``rate_role`` is a built-in role or one of ``rate_roles``, the roles the
    organization gave a rate (#9321).
    """
    if not name or not name.strip():
        raise ValueError("a unit needs a name")
    if min(low, likely, high, review) < 0:
        raise ValueError("minutes cannot be negative")
    if not low <= likely <= high:
        raise ValueError("manual minutes must be ordered low <= likely <= high")
    if review >= likely:
        raise ValueError("review minutes must stay under the likely manual minutes")
    allowed = set(roi.HOURLY_RATES) | set(rate_roles)
    if rate_role not in allowed:
        raise ValueError(f"rate role {rate_role!r} is not one of: {', '.join(sorted(allowed))}")


def unit_snapshot(unit: RoiUnit) -> dict[str, Any]:
    """The JSON form of a custom unit, as the API answers it and a version keeps it."""
    return {
        "key": unit.key,
        "name": unit.name,
        "role": unit.role,
        "manual_min_low": float(unit.manual_min_low),
        "manual_min_likely": float(unit.manual_min_likely),
        "manual_min_high": float(unit.manual_min_high),
        "review_min": float(unit.review_min),
        "rate_role": unit.rate_role,
        "source": unit.source,
        "archived_at": unit.archived_at.isoformat() if unit.archived_at else None,
        "version": int(unit.version),
    }


async def _write_version(db: AsyncSession, unit: RoiUnit, user_id: uuid.UUID | None) -> None:
    db.add(
        RoiUnitVersion(
            id=uuid.uuid4(), unit_key=unit.key, version=int(unit.version), snapshot=unit_snapshot(unit), user_id=user_id
        )
    )
    await db.flush()


async def list_units(db: AsyncSession) -> list[RoiUnit]:
    """Every custom unit, archived ones included, by name."""
    return list((await db.execute(select(RoiUnit).order_by(RoiUnit.name, RoiUnit.key))).scalars().all())


async def known_units(db: AsyncSession) -> set[str]:
    """The units an agent may declare: the built-ins and the custom units not archived."""
    rows = (await db.execute(select(RoiUnit.key).where(RoiUnit.archived_at.is_(None)))).scalars().all()
    return set(roi.BASELINES_BY_UNIT) | set(rows)


async def create_unit(
    db: AsyncSession,
    *,
    key: str,
    name: str,
    role: str = "",
    low: float,
    likely: float,
    high: float,
    review: float,
    rate_role: str = "analyst",
    source: str = "",
    user_id: uuid.UUID | None = None,
) -> RoiUnit:
    """Add a custom unit (admin); ``ValueError`` names a bad field or a taken key."""
    if not UNIT_KEY_PATTERN.match(key or ""):
        raise ValueError("a unit key is 2 to 64 characters of a-z, 0-9 and _")
    if key in roi.BASELINES_BY_UNIT:
        raise ValueError(f"unit {key!r} is a built-in unit; override its minutes instead")
    validate_unit(
        name=name,
        low=low,
        likely=likely,
        high=high,
        review=review,
        rate_role=rate_role,
        rate_roles=await rate_roles(db),
    )
    if await db.get(RoiUnit, key) is not None:
        raise ValueError(f"unit {key!r} already exists")
    unit = RoiUnit(
        key=key,
        name=name.strip()[:200],
        role=(role or "").strip()[:100],
        manual_min_low=low,
        manual_min_likely=likely,
        manual_min_high=high,
        review_min=review,
        rate_role=rate_role,
        source=(source or "")[:300],
        version=1,
        created_by_user_id=user_id,
        updated_by_user_id=user_id,
    )
    db.add(unit)
    await db.flush()
    await _write_version(db, unit, user_id)
    return unit


async def update_unit(
    db: AsyncSession,
    key: str,
    *,
    name: str,
    role: str = "",
    low: float,
    likely: float,
    high: float,
    review: float,
    rate_role: str = "analyst",
    source: str = "",
    user_id: uuid.UUID | None = None,
) -> RoiUnit | None:
    """Replace a custom unit's fields as a new version; ``None`` when it does not exist."""
    unit = await db.get(RoiUnit, key)
    if unit is None:
        return None
    validate_unit(
        name=name,
        low=low,
        likely=likely,
        high=high,
        review=review,
        rate_role=rate_role,
        rate_roles=await rate_roles(db),
    )
    unit.name = name.strip()[:200]
    unit.role = (role or "").strip()[:100]
    unit.manual_min_low, unit.manual_min_likely, unit.manual_min_high = low, likely, high
    unit.review_min = review
    unit.rate_role = rate_role
    unit.source = (source or "")[:300]
    unit.version = int(unit.version) + 1
    unit.updated_by_user_id = user_id
    await db.flush()
    await _write_version(db, unit, user_id)
    return unit


async def archive_unit(db: AsyncSession, key: str, *, user_id: uuid.UUID | None = None) -> RoiUnit | None:
    """Archive a custom unit (never deleted: its records keep their figures).

    ``None`` when it does not exist; an already archived unit is answered as is.
    """
    unit = await db.get(RoiUnit, key)
    if unit is None or unit.archived_at is not None:
        return unit
    unit.archived_at = datetime.now(UTC)
    unit.version = int(unit.version) + 1
    unit.updated_by_user_id = user_id
    await db.flush()
    await _write_version(db, unit, user_id)
    return unit


async def unit_versions(db: AsyncSession, key: str) -> list[RoiUnitVersion]:
    """Every version of a custom unit, newest first."""
    query = select(RoiUnitVersion).where(RoiUnitVersion.unit_key == key).order_by(RoiUnitVersion.version.desc())
    return list((await db.execute(query)).scalars().all())


def validate_override(unit: str, low: float, likely: float, high: float, review: float) -> None:
    """Raise ``ValueError`` naming what is wrong with an override.

    Only a built-in unit is overridden: a custom unit's minutes are its own
    fields, changed with :func:`update_unit` (a new version).
    """
    if unit not in roi.BASELINES_BY_UNIT:
        raise ValueError(f"unit {unit!r} is not a built-in unit of the ROI catalog")
    if min(low, likely, high, review) < 0:
        raise ValueError("minutes cannot be negative")
    if not low <= likely <= high:
        raise ValueError("manual minutes must be ordered low <= likely <= high")
    if review >= likely:
        raise ValueError("review minutes must stay under the likely manual minutes")


async def upsert_override(
    db: AsyncSession,
    *,
    unit: str,
    low: float,
    likely: float,
    high: float,
    review: float,
    source: str = "",
    user_id: uuid.UUID | None = None,
) -> RoiBaselineOverride:
    """Set the organization's minutes for one catalog unit (admin)."""
    validate_override(unit, low, likely, high, review)
    row = await db.get(RoiBaselineOverride, unit)
    if row is None:
        row = RoiBaselineOverride(unit=unit)
        db.add(row)
    row.manual_min_low, row.manual_min_likely, row.manual_min_high = low, likely, high
    row.review_min = review
    row.source = source[:300]
    row.updated_by_user_id = user_id
    return row


# ── Default / Customized / Reset (#9836 R3, design 10.4) ─────────────────

#: A built-in value as the seed ships it.
ORIGIN_DEFAULT = "default"
#: A built-in value an administrator changed (Reset to default drops the change).
ORIGIN_CUSTOMIZED = "customized"
#: The organization's own value, with no built-in default behind it.
ORIGIN_CUSTOM = "custom"


async def user_names(db: AsyncSession, ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    """The display name of each user id (full name, else e-mail); an unknown id is left out."""
    from app.models.user import User

    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = (await db.execute(select(User.id, User.full_name, User.email).where(User.id.in_(wanted)))).all()
    return {row.id: (row.full_name or row.email) for row in rows}


def origin_fields(row: Any | None, names: Mapping[uuid.UUID, str], *, has_default: bool) -> dict[str, Any]:
    """``origin``, ``customized_by`` and ``customized_at`` of a value *row* stores (``None``: the default).

    *row* carries ``updated_by_user_id`` and ``updated_at`` (an override, a rate,
    a declaration). Without a built-in default behind it the origin is ``custom``.
    """
    if row is None:
        return {"origin": ORIGIN_DEFAULT, "customized_by": None, "customized_at": None}
    by = getattr(row, "updated_by_user_id", None)
    at = getattr(row, "updated_at", None)
    return {
        "origin": ORIGIN_CUSTOMIZED if has_default else ORIGIN_CUSTOM,
        "customized_by": names.get(by) if by is not None else None,
        "customized_at": at.isoformat() if at else None,
    }


def baseline_default(unit: str) -> dict[str, Any] | None:
    """The catalog's own minutes of a built-in *unit* (the diff's left side), ``None`` for a custom unit."""
    base = roi.BASELINES_BY_UNIT.get(unit)
    if base is None:
        return None
    return {
        "manual_min_low": base.manual_min_low,
        "manual_min_likely": base.manual_min_likely,
        "manual_min_high": base.manual_min_high,
        "review_min": base.review_min,
        "minutes_label": base.minutes_label,
        "minutes_source": base.minutes_source,
    }


async def baseline_origins(db: AsyncSession) -> dict[str, dict[str, Any]]:
    """Unit -> its origin fields, for every built-in unit the organization customized."""
    rows = list((await db.execute(select(RoiBaselineOverride))).scalars().all())
    names = await user_names(db, (r.updated_by_user_id for r in rows))
    return {r.unit: origin_fields(r, names, has_default=True) for r in rows if r.unit in roi.BASELINES_BY_UNIT}


def rate_default(role: str, currency: str) -> dict[str, Any] | None:
    """The built-in rate of *role* in *currency* (amount, label, source), ``None`` when there is none."""
    rate = roi.HOURLY_RATES.get(role, {}).get((currency or "").upper())
    if rate is None:
        return None
    return {"amount": rate.amount, "label": rate.label, "source": rate.source}


async def delete_override(db: AsyncSession, unit: str) -> bool:
    """Drop the organization's override of ``unit``; ``False`` when it had none."""
    row = await db.get(RoiBaselineOverride, unit)
    if row is None:
        return False
    await db.delete(row)
    return True


# ── Rates by role (#9321) ─────────────────────────────────────────────

#: A rate role: a slug, built-in (``analyst``, ``ciso``) or the organization's own.
RATE_ROLE_PATTERN = re.compile(r"^[a-z0-9_]{2,64}$")

#: An ISO 4217 currency code.
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")


def validate_rate(role: str, currency: str, amount: float) -> None:
    """Raise ``ValueError`` naming what is wrong with a rate."""
    if not RATE_ROLE_PATTERN.match(role or ""):
        raise ValueError("a rate role is 2 to 64 characters of a-z, 0-9 and _")
    if not CURRENCY_PATTERN.match(currency or ""):
        raise ValueError("a currency is a three-letter ISO 4217 code")
    if not amount > 0:
        raise ValueError("an hourly rate must be above zero")


async def list_rates(db: AsyncSession) -> list[RoiRate]:
    """The organization's rates, by role then currency."""
    return list((await db.execute(select(RoiRate).order_by(RoiRate.role, RoiRate.currency))).scalars().all())


async def rate_roles(db: AsyncSession) -> set[str]:
    """The roles the organization gave a rate (a custom unit may name one)."""
    return set((await db.execute(select(RoiRate.role).distinct())).scalars().all())


async def put_rate(
    db: AsyncSession,
    *,
    role: str,
    currency: str,
    amount: float,
    source: str = "",
    user_id: uuid.UUID | None = None,
) -> RoiRate:
    """Set the organization's hourly rate of ``role`` in ``currency`` (admin)."""
    currency = (currency or "").upper()
    validate_rate(role, currency, amount)
    row = await db.get(RoiRate, (role, currency))
    if row is None:
        row = RoiRate(role=role, currency=currency)
        db.add(row)
    row.amount = float(amount)
    row.source = (source or "")[:300]
    row.updated_by_user_id = user_id
    await db.flush()
    return row


async def delete_rate(db: AsyncSession, *, role: str, currency: str) -> bool:
    """Drop the organization's rate of ``role`` in ``currency``; ``False`` when it had none.

    A role a custom unit still names keeps resolving (organization rate, then
    ``analyst``), so a deletion never breaks a figure.
    """
    row = await db.get(RoiRate, (role, (currency or "").upper()))
    if row is None:
        return False
    await db.delete(row)
    await db.flush()
    return True


def rate_dict(row: RoiRate, names: Mapping[uuid.UUID, str] | None = None) -> dict[str, Any]:
    """The JSON form of a rate: Customized over a built-in rate (with its ``default``), else Custom."""
    default = rate_default(row.role, row.currency)
    return {
        **origin_fields(row, names or {}, has_default=default is not None),
        "default": default,
        "role": row.role,
        "currency": row.currency,
        "amount": float(row.amount),
        "source": row.source,
        "builtin_role": row.role in roi.HOURLY_RATES,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
