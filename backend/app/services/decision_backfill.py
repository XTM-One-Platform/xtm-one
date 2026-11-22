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

"""The backfill of the decisions table and its read switch (#842, S2b; human-gates.md section 8).

The dual write of S2 (``decision_hooks``) opens a decision when a gate opens
AFTER the deploy; a gate opened before it has no row. :func:`backfill` copies
every gate still open into ``decisions`` with an ``asked`` event stamped
``backfill``, from the same columns and with the same ask, recipient and
deadline rules the hook uses (``run_opening``, ``flow_opening``,
``work_item_opening``):

- a run ``awaiting_human`` with no stored answer (free-text HITL or tool
  approval);
- a flow run whose ``state.waiting`` is an unreleased ``human`` park;
- a work item ``needs_human`` that is not finished.

A source already answered, or that already has an open decision, is skipped,
so a re-run writes nothing (the open-source unique index is the backstop).
Batched by keyset (``id``), so no batch holds a long lock.

:func:`backfill_report` is the count check: per source, the open gates, the
ones with an open decision and the ids of the first missing ones. The Alembic
data migration logs it after the copy and ``GET /api/v1/decisions/backfill``
(admins) repeats it as the health probe. :func:`read_switch` is the switch the
readers of the table (the inbox, the bell and the cards, from S3) consult: on
only when ``DECISIONS_READ_ENABLED`` is set AND the report has nothing
missing AND the table exists, so a downgrade turns it off by itself.

Everything here takes a SYNC connection (the migration's); an async caller
goes through ``AsyncConnection.run_sync``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.models.decision_hooks import flow_opening, open_flow_park, run_opening, work_item_opening

logger = logging.getLogger(__name__)

#: Rows read per keyset batch.
BATCH_SIZE = 200
#: The surface and the ``detail.source`` of the ``asked`` event a backfill writes.
BACKFILL_STAMP = "backfill"
#: Missing ids a report lists per source.
REPORT_SAMPLE = 20

# Light table clauses, not the mapped models: a later change of a model must
# never change what this migration read when it shipped.
_runs = sa.table(
    "assignment_runs",
    sa.column("id"),
    sa.column("status"),
    sa.column("user_id"),
    sa.column("is_company_managed"),
    sa.column("human_input_question"),
    sa.column("human_input_response"),
    sa.column("messages", sa.JSON),
    sa.column("pending_approvals", sa.JSON),
    sa.column("approval_decisions", sa.JSON),
)
_flow_runs = sa.table(
    "flow_runs",
    sa.column("id"),
    sa.column("status"),
    sa.column("user_id"),
    sa.column("is_company_managed"),
    sa.column("state", sa.JSON),
)
_work_items = sa.table(
    "work_items",
    sa.column("id"),
    sa.column("status"),
    sa.column("user_id"),
    sa.column("waits_for_user_id"),
    sa.column("needs_human"),
    sa.column("ask", sa.JSON),
    sa.column("blocked_reason"),
    sa.column("title"),
)
_decisions = sa.table(
    "decisions",
    sa.column("id"),
    sa.column("source_type"),
    sa.column("source_id"),
    sa.column("kind"),
    sa.column("ask", sa.JSON),
    sa.column("recipient"),
    sa.column("status"),
    sa.column("deadline"),
    sa.column("created_at"),
    sa.column("updated_at"),
)
_events = sa.table(
    "decision_events",
    sa.column("id"),
    sa.column("decision_id"),
    sa.column("event"),
    sa.column("surface"),
    sa.column("detail", sa.JSON),
    sa.column("created_at"),
)


def run_is_open(row: Any) -> bool:
    """A run row waits for a person and holds no answer yet."""
    return row.status == "awaiting_human" and not row.human_input_response and not row.approval_decisions


def flow_run_is_open(row: Any) -> bool:
    """A flow run row is parked on an Ask-user node not released yet."""
    return open_flow_park(row.state)


def work_item_is_open(row: Any) -> bool:
    """A work item row waits for a person and is not finished."""
    return bool(row.needs_human) and row.status not in ("done", "cancelled")


@dataclass(frozen=True)
class Source:
    """One engine the backfill reads: its table, the SQL prefilter, the open test and the opening."""

    source_type: str
    table: Any
    prefilter: Any
    is_open: Callable[[Any], bool]
    opening: Callable[[Any], Any]


SOURCES: tuple[Source, ...] = (
    Source("run", _runs, _runs.c.status == "awaiting_human", run_is_open, run_opening),
    Source(
        "flow_run",
        _flow_runs,
        _flow_runs.c.status == "awaiting",
        flow_run_is_open,
        lambda row: flow_opening(row, row.state),
    ),
    Source(
        "work_item",
        _work_items,
        sa.and_(_work_items.c.needs_human.is_(True), _work_items.c.status.not_in(("done", "cancelled"))),
        work_item_is_open,
        work_item_opening,
    ),
)


def _batches(conn: Connection, source: Source, batch_size: int) -> Iterator[list[Any]]:
    last_id: Any = None
    while True:
        query = sa.select(source.table).where(source.prefilter).order_by(source.table.c.id).limit(batch_size)
        if last_id is not None:
            query = query.where(source.table.c.id > last_id)
        rows = list(conn.execute(query).fetchall())
        if not rows:
            return
        yield rows
        last_id = rows[-1].id


def _with_open_decision(conn: Connection, source_type: str, ids: list[Any]) -> set[Any]:
    if not ids:
        return set()
    rows = conn.execute(
        sa.select(_decisions.c.source_id).where(
            _decisions.c.source_type == source_type,
            _decisions.c.status == "open",
            _decisions.c.source_id.in_(ids),
        )
    )
    return {row[0] for row in rows}


@dataclass
class SourceCount:
    """The count check of one source: open gates, how many have their open decision, the first missing."""

    open_sources: int = 0
    with_decision: int = 0
    missing: list[str] = field(default_factory=list)

    @property
    def missing_count(self) -> int:
        return self.open_sources - self.with_decision

    def as_dict(self) -> dict[str, Any]:
        return {
            "open_sources": self.open_sources,
            "with_decision": self.with_decision,
            "missing": self.missing_count,
            "missing_ids": list(self.missing),
        }


def has_decisions_table(conn: Connection) -> bool:
    return sa.inspect(conn).has_table("decisions")


def backfill(conn: Connection, *, batch_size: int = BATCH_SIZE, now: datetime | None = None) -> dict[str, int]:
    """Copy every open gate without an open decision into ``decisions``; the rows written per source."""
    stamp = now or datetime.now(UTC)
    written: dict[str, int] = {}
    for source in SOURCES:
        count = 0
        for rows in _batches(conn, source, batch_size):
            open_rows = [row for row in rows if source.is_open(row)]
            already = _with_open_decision(conn, source.source_type, [row.id for row in open_rows])
            decisions: list[dict[str, Any]] = []
            events: list[dict[str, Any]] = []
            for row in open_rows:
                if row.id in already:
                    continue
                kind, ask, recipient, deadline = source.opening(row)
                decision_id = uuid.uuid4()
                decisions.append(
                    {
                        "id": decision_id,
                        "source_type": source.source_type,
                        "source_id": row.id,
                        "kind": kind,
                        "ask": ask,
                        "recipient": recipient,
                        "status": "open",
                        "deadline": deadline,
                        "created_at": stamp,
                        "updated_at": stamp,
                    }
                )
                events.append(
                    {
                        "id": uuid.uuid4(),
                        "decision_id": decision_id,
                        "event": "asked",
                        "surface": BACKFILL_STAMP,
                        "detail": {"kind": kind, "source": BACKFILL_STAMP},
                        "created_at": stamp,
                    }
                )
            if decisions:
                conn.execute(sa.insert(_decisions), decisions)
                conn.execute(sa.insert(_events), events)
                count += len(decisions)
        written[source.source_type] = count
    return written


def backfill_report(conn: Connection, *, batch_size: int = BATCH_SIZE) -> dict[str, SourceCount]:
    """The count check per source: open gates, those with their open decision, the first missing ids."""
    report: dict[str, SourceCount] = {}
    table = has_decisions_table(conn)
    for source in SOURCES:
        counted = SourceCount()
        for rows in _batches(conn, source, batch_size):
            open_ids = [row.id for row in rows if source.is_open(row)]
            counted.open_sources += len(open_ids)
            covered = _with_open_decision(conn, source.source_type, open_ids) if table else set()
            counted.with_decision += len(covered)
            for source_id in open_ids:
                if source_id not in covered and len(counted.missing) < REPORT_SAMPLE:
                    counted.missing.append(str(source_id))
        report[source.source_type] = counted
    return report


def report_complete(report: dict[str, SourceCount]) -> bool:
    """Every open gate has its open decision."""
    return all(count.missing_count == 0 for count in report.values())


def read_switch(conn: Connection, *, enabled: bool | None = None) -> dict[str, Any]:
    """Whether the readers may read the table: the setting, the table and a complete count check.

    *enabled* defaults to ``settings.decisions_read_enabled``. The answer
    names why it is off, so the health probe says what to do.
    """
    if enabled is None:
        from app.core.config import settings

        enabled = bool(settings.decisions_read_enabled)
    if not has_decisions_table(conn):
        return {"on": False, "reason": "the decisions table does not exist (downgraded)", "setting": enabled}
    report = backfill_report(conn)
    complete = report_complete(report)
    if not enabled:
        reason = "DECISIONS_READ_ENABLED is off"
    elif not complete:
        reason = "the backfill count does not match the open gates: run the backfill again"
    else:
        reason = None
    return {
        "on": bool(enabled and complete),
        "reason": reason,
        "setting": enabled,
        "complete": complete,
        "sources": {name: count.as_dict() for name, count in report.items()},
    }


def log_report(report: dict[str, SourceCount], written: dict[str, int] | None = None) -> None:
    """One log line per source with the count check (the migration's record)."""
    for name, count in report.items():
        logger.info(
            "decisions backfill %s: %s written, %s open, %s with a decision, %s missing",
            name,
            (written or {}).get(name, 0),
            count.open_sources,
            count.with_decision,
            count.missing_count,
        )
