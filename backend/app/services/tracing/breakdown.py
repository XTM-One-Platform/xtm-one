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

"""LLM usage grouped by any dimensions over the analytics read path.

The rows of ``read_store.analytics_stats_subquery`` - past 7 days the rollup
and the days it lacks, within 7 days the window's traces - grouped in SQL by a
time bucket and any of: the department, the person, the agent, the model, the
source, the attribution class. Every row carries the sums every metric is
computed from, and the number of distinct people it covers; the per-group
reads fold person rows (a person is in many groups).

Who a read covers is a :class:`Selection`, decided on the person the usage is
attributed to (``attribution.py``) - or, with ``attribution="raw"``, the
trace's own user - and on the department stored with it (``departments.py``):
a row with no attribution yet (written earlier and not stamped by the
backfill) counts for its own user and for no department.

Department keys: a department's id, ``unassigned`` (a person no source
resolves), ``unattributed`` (usage attributed to nobody). Every row has
exactly one, so the department rows of a read add up to its total.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime, String, and_, case, cast, false, func, literal, literal_column, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.departments import UNASSIGNED_KEY, UNATTRIBUTED_KEY
from app.services.platform_builds import BEFORE_HISTORY_BUCKET

ATTRIBUTION_MODES: tuple[str, ...] = ("attributed", "raw")
#: The attribution class of a row the backfill has not reached yet.
UNSTAMPED = "unstamped"


@dataclass(frozen=True)
class Selection:
    """Who a read covers.

    The named sets (*user_ids*, *department_keys*) narrow together; with
    *complement* the read covers everything OUTSIDE them instead. The
    exclusions then remove people and departments, and *reach* (a
    non-administrator's own usage and their teams') bounds it all.
    """

    attribution: str = "attributed"
    user_ids: frozenset[uuid.UUID] | None = None
    department_keys: frozenset[str] | None = None
    complement: bool = False
    exclude_user_ids: frozenset[uuid.UUID] = frozenset()
    exclude_department_keys: frozenset[str] = frozenset()
    reach: frozenset[uuid.UUID] | None = None

    def person(self, columns: Any):
        if self.attribution == "raw":
            return columns.user_id
        return func.coalesce(columns.attributed_user_id, columns.user_id)

    def department_key(self, columns: Any):
        person = self.person(columns)
        if self.attribution == "raw":
            return case(
                (columns.user_id.is_(None), literal(UNATTRIBUTED_KEY)),
                (columns.department_id.is_not(None), cast(columns.department_id, String)),
                else_=literal(UNASSIGNED_KEY),
            )
        return case(
            (and_(columns.attribution.is_not(None), columns.department_id.is_not(None)), cast(columns.department_id, String)),
            (person.is_not(None), literal(UNASSIGNED_KEY)),
            else_=literal(UNATTRIBUTED_KEY),
        )

    def predicate(self, columns: Any):
        person = self.person(columns)
        department = self.department_key(columns)
        named = []
        if self.user_ids is not None:
            named.append(person.in_(sorted(self.user_ids, key=str)) if self.user_ids else false())
        if self.department_keys is not None:
            named.append(department.in_(sorted(self.department_keys)) if self.department_keys else false())
        clauses = []
        if named:
            inside = and_(*named)
            clauses.append(not_(func.coalesce(inside, false())) if self.complement else inside)
        if self.exclude_user_ids:
            clauses.append(or_(person.is_(None), person.not_in(sorted(self.exclude_user_ids, key=str))))
        if self.exclude_department_keys:
            clauses.append(department.not_in(sorted(self.exclude_department_keys)))
        if self.reach is not None:
            clauses.append(person.in_(sorted(self.reach, key=str)) if self.reach else false())
        return and_(*clauses) if clauses else None


@dataclass
class Sums:
    """What every metric of a row is computed from."""

    trace_count: int = 0
    error_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost: float = 0.0
    duration_weighted: float = 0.0
    duration_weight: int = 0
    feedback_weighted: float = 0.0
    feedback_weight: int = 0
    unstamped_count: int = 0
    people_count: int = 0
    people: set[str] = field(default_factory=set)

    def add(self, other: Sums, person: str | None = None) -> None:
        """Add *other*'s sums; the people are counted again only from *person* (a fold of person rows)."""
        for name in (
            "trace_count",
            "error_count",
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "cost",
            "duration_weighted",
            "duration_weight",
            "feedback_weighted",
            "feedback_weight",
            "unstamped_count",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        if person is not None:
            self.people.add(person)
            self.people_count = len(self.people)


@dataclass
class BreakdownRow:
    keys: dict[str, Any]
    sums: Sums


def _bucket_expression(bucket: str, day: Any, windows: Sequence[Any]):
    if bucket == "week":
        return cast(func.date_trunc("week", day), Date)
    if bucket == "month":
        return cast(func.date_trunc("month", day), Date)
    if bucket == "release":
        # The newest build first seen by the end of the day (``platform_builds.release_of_day``).
        day_end = func.timezone(literal_column("'UTC'"), cast(day, DateTime)) + literal_column("interval '1 day'")
        if not windows:
            return literal(BEFORE_HISTORY_BUCKET)
        return case(
            *[(day_end > window.first_seen_at, literal(window.label)) for window in reversed(windows)],
            else_=literal(BEFORE_HISTORY_BUCKET),
        )
    return day


def _key_expression(key: str, base: Any, selection: Selection):
    if key == "department":
        return selection.department_key(base.c)
    if key == "person":
        return selection.person(base.c)
    if key == "agent":
        return base.c.agent_id
    if key == "model":
        return base.c.model
    if key == "source":
        return base.c.source
    if key == "attribution":
        return func.coalesce(base.c.attribution, literal(UNSTAMPED))
    raise ValueError(f"unknown breakdown key {key!r}")


def _key_value(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


async def breakdown(
    db: AsyncSession,
    filters: Any,
    *,
    viewer: Any,
    user_group_ids: list[str],
    start: datetime,
    end: datetime,
    use_daily: bool,
    keys: Sequence[str] = (),
    bucket: str | None = None,
    selection: Selection = Selection(),
    windows: Sequence[Any] = (),
    only_unattributed: bool = False,
) -> list[BreakdownRow]:
    """The window's usage grouped by *bucket* and *keys*, one :class:`BreakdownRow` per group.

    *filters* are the store's (agents, models, sources; its ``user_ids`` stay
    empty - people are the *selection*'s). *only_unattributed* reads only the
    usage attributed to nobody.
    """
    from app.services.tracing.read_store import analytics_stats_subquery

    base = await analytics_stats_subquery(
        db, filters, user=viewer, user_group_ids=user_group_ids, start=start, end=end, use_daily=use_daily
    )
    person = selection.person(base.c)
    expressions: dict[str, Any] = {}
    if bucket is not None:
        expressions["bucket"] = _bucket_expression(bucket, base.c.date, windows)
    for key in keys:
        expressions[key] = _key_expression(key, base, selection)
    with_duration = base.c.avg_duration_ms.is_not(None)
    with_feedback = base.c.avg_feedback_score.is_not(None)
    query = select(
        *(expression.label(name) for name, expression in expressions.items()),
        func.coalesce(func.sum(base.c.trace_count), 0).label("trace_count"),
        func.coalesce(func.sum(base.c.error_count), 0).label("error_count"),
        func.coalesce(func.sum(base.c.total_input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(base.c.total_output_tokens), 0).label("output_tokens"),
        func.coalesce(func.sum(base.c.total_cached_input_tokens), 0).label("cached_tokens"),
        func.sum(base.c.total_cost).filter(base.c.total_cost.is_not(None)).label("cost"),
        func.sum(base.c.avg_duration_ms * base.c.trace_count).filter(with_duration).label("duration_weighted"),
        func.sum(base.c.trace_count).filter(with_duration).label("duration_weight"),
        func.sum(base.c.avg_feedback_score * base.c.trace_count).filter(with_feedback).label("feedback_weighted"),
        func.sum(base.c.trace_count).filter(with_feedback).label("feedback_weight"),
        func.sum(base.c.trace_count).filter(base.c.attribution.is_(None)).label("unstamped_count"),
        func.count(func.distinct(person)).label("people_count"),
    )
    predicate = selection.predicate(base.c)
    if predicate is not None:
        query = query.where(predicate)
    if only_unattributed:
        query = query.where(person.is_(None))
    if expressions:
        # By position: a grouped expression rendered twice carries its literals
        # as two binds, which PostgreSQL does not read as the same expression.
        query = query.group_by(*(literal_column(str(position)) for position in range(1, len(expressions) + 1)))
    rows: list[BreakdownRow] = []
    for row in (await db.execute(query)).mappings().all():
        if not row["trace_count"]:
            continue
        rows.append(
            BreakdownRow(
                keys={name: _key_value(row[name]) for name in expressions},
                sums=Sums(
                    trace_count=int(row["trace_count"] or 0),
                    error_count=int(row["error_count"] or 0),
                    input_tokens=int(row["input_tokens"] or 0),
                    output_tokens=int(row["output_tokens"] or 0),
                    cached_tokens=int(row["cached_tokens"] or 0),
                    cost=float(row["cost"] or 0),
                    duration_weighted=float(row["duration_weighted"] or 0),
                    duration_weight=int(row["duration_weight"] or 0),
                    feedback_weighted=float(row["feedback_weighted"] or 0),
                    feedback_weight=int(row["feedback_weight"] or 0),
                    unstamped_count=int(row["unstamped_count"] or 0),
                    people_count=int(row["people_count"] or 0),
                ),
            )
        )
    return rows
