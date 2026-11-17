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

"""``query_platform_analytics``: the platform's LLM usage, by any dimension.

Every figure is read by ``services/tracing/breakdown.py`` from the read path
the LLM Analytics dashboard uses (past 7 days the daily rollup and the days it
lacks, within 7 days the window's traces), with the same window and filters,
so a total here is the dashboard's. The one exception is ``latency``, the
median, read from the latency histograms of the dashboard's latency panel.

The metric matrix (:func:`capabilities`, returned with every result): every
metric but ``latency`` over every time bucket and every dimension, with
``split_by`` on any second dimension. A refusal names the nearest combination
that is computed.

Usage counts for the person it is ATTRIBUTED to (``tracing/attribution.py``:
the trace's user, else its run's, its parent's, its conversation's or its
agent owner's) unless ``attribution: raw``; what nobody is behind is the
``unattributed`` block, by reason. A department is the one stored with the
usage when it was written (``departments.py``): department rows never
overlap and add up to the total. A group is its members TODAY and a person in
several groups counts in each (``overlapping_membership``). Costs the platform
hides (``OBSERVABILITY_COST_DATA_ENABLED=false``) are served as null, never as
zero.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from calendar import monthrange
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from itertools import product
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.utils import safe_uuid
from app.models.agent import Agent
from app.models.department import Department, TraceAttributionBackfill
from app.models.group import GROUP_KINDS, Group, GroupMember
from app.models.llm_trace import LLMDailyStats, LLMTrace
from app.models.platform_settings import PlatformSettings
from app.models.user import User
from app.services.departments import (
    UNASSIGNED_KEY,
    UNATTRIBUTED_KEY,
    department_label,
    load_department_map,
)
from app.services.platform_builds import history_note, release_order, release_windows
from app.services.platform_insights.projection import (
    DEFAULT_LAST_N,
    PROJECTION_METHODS,
    ProjectionError,
    add_months,
    fit,
)
from app.services.platform_insights.scope import (
    COST_MASKED_NOTE,
    CURRENT_MEMBERSHIP_NOTE,
    InsightRefusedError,
    InsightScope,
    as_list,
    group_member_ids,
    parse_instant,
    resolve_groups,
    resolve_users,
)
from app.services.tracing.attribution import RESIDUAL_NOTES
from app.services.tracing.breakdown import ATTRIBUTION_MODES, UNSTAMPED, BreakdownRow, Selection, Sums, breakdown

METRICS: tuple[str, ...] = (
    "cost",
    "tokens",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "traces",
    "errors",
    "error_rate",
    "active_users",
    "duration",
    "latency",
    "feedback",
    "cost_per_active_user",
    "cost_per_trace",
    "cost_per_1k_tokens",
)
DIMENSIONS: tuple[str, ...] = ("department", "group", "user", "agent", "model", "source")
TIME_BUCKETS: tuple[str, ...] = ("day", "week", "month", "release")
GROUP_BYS: tuple[str, ...] = (*DIMENSIONS, *TIME_BUCKETS)
#: ``latency`` is the median of the latency histograms: computed as a total and by these only.
LATENCY_GROUPINGS: frozenset[str | None] = frozenset({None, "model", "day"})

UNITS: dict[str, str] = {
    "cost": "USD",
    "tokens": "tokens",
    "input_tokens": "tokens",
    "output_tokens": "tokens",
    "cached_tokens": "tokens",
    "traces": "traces",
    "errors": "traces",
    "error_rate": "percent",
    "active_users": "people",
    "duration": "ms",
    "latency": "ms",
    "feedback": "score",
    "cost_per_active_user": "USD",
    "cost_per_trace": "USD",
    "cost_per_1k_tokens": "USD",
}
#: What a row's value is, so the reader never adds up what does not add up.
VALUE_KINDS: dict[str, str] = {
    "cost": "sum",
    "tokens": "sum",
    "input_tokens": "sum",
    "output_tokens": "sum",
    "cached_tokens": "sum",
    "traces": "count",
    "errors": "count",
    "error_rate": "rate",
    "active_users": "distinct",
    "duration": "average",
    "latency": "median",
    "feedback": "average",
    "cost_per_active_user": "ratio",
    "cost_per_trace": "ratio",
    "cost_per_1k_tokens": "ratio",
}
ADDITIVE_KINDS = frozenset({"sum", "count"})
COST_METRICS = frozenset({"cost", "cost_per_active_user", "cost_per_trace", "cost_per_1k_tokens"})

#: The most rows a call returns; a larger result is cut and says so.
MAX_ROWS = 500
MAX_LIMIT = 200
DEFAULT_LIMIT = 50
MAX_EXPORT_ROWS = 100_000
MAX_HORIZON_MONTHS = 36

NO_AGENT_LABEL = "(no agent: General Assistant, proxy, platform calls)"
NO_MODEL_LABEL = "(no model)"
UNGROUPED_LABEL = "(in no group)"

STALE_NOTE = (
    "The daily aggregation is stale (last run more than 48 hours ago): days without a rollup are "
    "read live, but say the figures may move once the aggregation catches up."
)

#: The columns of an export (``export: csv``), every metric of a row.
EXPORT_METRICS: tuple[str, ...] = (
    "traces",
    "errors",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "tokens",
    "cost",
    "active_users",
    "duration",
    "error_rate",
    "feedback",
)


def capabilities() -> dict[str, Any]:
    """The metric matrix, returned with every result and published in the tool description."""
    return {
        "metrics": list(METRICS),
        "group_by": [*DIMENSIONS, *TIME_BUCKETS],
        "split_by": "any dimension (" + ", ".join(DIMENSIONS) + ") other than group_by, under any group_by",
        "matrix": "every metric over every group_by and split_by, except latency",
        "latency": "the median: as a total, by model and by day, no split_by; duration (the average) has every grouping",
        "filters": [
            "agent_ids",
            "user_ids",
            "group_ids",
            "department_ids",
            "models",
            "sources",
            "exclude_user_ids",
            "exclude_group_ids",
            "exclude_department_ids",
            "complement",
            "group_kinds",
            "attribution",
        ],
        "projection": {"methods": list(PROJECTION_METHODS), "group_by": "month", "max_horizon_months": MAX_HORIZON_MONTHS},
        "compare_to": ["previous_period"],
        "export": ["csv"],
    }


def check_combination(metric: str, group_by: str | None, split_by: str | None) -> None:
    """Refuse a cell of the matrix that is not computed, naming the nearest one that is."""
    if split_by is not None:
        if split_by not in DIMENSIONS:
            raise InsightRefusedError(f"Error: split_by must be one of {', '.join(DIMENSIONS)}.")
        if group_by is None:
            raise InsightRefusedError(
                f"Error: split_by needs a group_by: use group_by {split_by} for one row per {split_by}, "
                f"or group_by month with split_by {split_by} for one series per {split_by}."
            )
        if split_by == group_by:
            others = ", ".join(dim for dim in DIMENSIONS if dim != group_by)
            raise InsightRefusedError(
                f"Error: split_by repeats group_by ({group_by}): use another dimension as split_by ({others}), "
                f"or group_by {group_by} without split_by."
            )
    if metric == "latency" and (split_by is not None or group_by not in LATENCY_GROUPINGS):
        asked = " by ".join(part for part in (group_by, split_by) if part) or "this grouping"
        raise InsightRefusedError(
            f"Error: latency (the median) is computed as a total, by model and by day, without split_by, "
            f"not by {asked}. Use metric duration (the average duration in ms), which every grouping and "
            "split_by has, or latency by day."
        )


def metric_value(metric: str, sums: Sums) -> float | None:
    """*metric* of a row from its sums; ``None`` when the row has nothing to compute it from."""
    tokens = sums.input_tokens + sums.output_tokens
    if metric == "cost":
        return sums.cost
    if metric == "tokens":
        return tokens
    if metric in {"input_tokens", "output_tokens", "cached_tokens"}:
        return getattr(sums, metric)
    if metric == "traces":
        return sums.trace_count
    if metric == "errors":
        return sums.error_count
    if metric == "error_rate":
        return 100 * sums.error_count / sums.trace_count if sums.trace_count else None
    if metric == "active_users":
        return sums.people_count
    if metric == "duration":
        return sums.duration_weighted / sums.duration_weight if sums.duration_weight else None
    if metric == "feedback":
        return sums.feedback_weighted / sums.feedback_weight if sums.feedback_weight else None
    if metric == "cost_per_active_user":
        return sums.cost / sums.people_count if sums.people_count else None
    if metric == "cost_per_trace":
        return sums.cost / sums.trace_count if sums.trace_count else None
    if metric == "cost_per_1k_tokens":
        return sums.cost / (tokens / 1000) if tokens else None
    raise InsightRefusedError(f"Error: metric must be one of {', '.join(METRICS)}.")


def _totals(sums: Sums) -> dict[str, Any]:
    return {
        "total_cost": sums.cost,
        "total_tokens": sums.input_tokens + sums.output_tokens,
        "input_tokens": sums.input_tokens,
        "output_tokens": sums.output_tokens,
        "cached_tokens": sums.cached_tokens,
        "trace_count": sums.trace_count,
        "error_count": sums.error_count,
        "error_rate": metric_value("error_rate", sums),
        "active_users": sums.people_count,
        "avg_duration_ms": metric_value("duration", sums),
        "avg_feedback_score": metric_value("feedback", sums),
    }


def _mask_costs(value: Any) -> Any:
    """*value* with every cost field nulled (the platform hides costs)."""
    if isinstance(value, dict):
        return {key: (None if "cost" in key else _mask_costs(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask_costs(item) for item in value]
    return value


@dataclass
class _Filters:
    """The store's analytics filters (``AnalyticsFilters``): people are the selection's."""

    period: str
    date_from: datetime | None
    date_to: datetime | None
    agent_ids: str | None
    user_ids: str | None
    models: str | None
    sources: str | None


def _csv(values: list[str]) -> str | None:
    return ",".join(values) if values else None


@dataclass
class _Request:
    metric: str
    group_by: str | None
    split_by: str | None
    period: str
    start: datetime
    end: datetime
    prev_start: datetime
    prev_end: datetime
    filters: _Filters
    selection: Selection
    limit: int
    group_kinds: frozenset[str] | None
    #: Outside the platform scope, the groups a group reading may list.
    listed_groups: frozenset[str] | None
    named_groups: frozenset[str]
    projection: dict[str, Any] | None
    compare_to: str | None
    export: str | None
    echo: dict[str, Any]
    caveats: list[str] = field(default_factory=list)

    @property
    def dims(self) -> list[str]:
        return [dim for dim in (self.group_by if self.group_by in DIMENSIONS else None, self.split_by) if dim]

    @property
    def bucket(self) -> str | None:
        return self.group_by if self.group_by in TIME_BUCKETS else None


def _text(args: dict[str, Any], name: str) -> str | None:
    return (str(args.get(name) or "").strip().lower()) or None


def _projection_args(raw: Any) -> dict[str, Any] | None:
    if raw in (None, "", {}):
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {"method": raw}
    if not isinstance(raw, dict):
        raise InsightRefusedError("Error: projection must be {method, horizon_months}.")
    method = str(raw.get("method") or "linear").strip().lower()
    if method not in PROJECTION_METHODS:
        raise InsightRefusedError(f"Error: projection.method must be one of {', '.join(PROJECTION_METHODS)}.")
    try:
        horizon = int(raw.get("horizon_months") or 12)
        last_n = int(raw.get("last_n") or DEFAULT_LAST_N)
    except (TypeError, ValueError):
        raise InsightRefusedError("Error: projection.horizon_months and projection.last_n are whole numbers.") from None
    if not 1 <= horizon <= MAX_HORIZON_MONTHS:
        raise InsightRefusedError(f"Error: projection.horizon_months must be between 1 and {MAX_HORIZON_MONTHS}.")
    return {"method": method, "horizon_months": horizon, "last_n": max(1, last_n)}


async def resolve_department_keys(db: AsyncSession, refs: Any) -> list[tuple[str, str]]:
    """``(key, label)`` of the departments *refs* name: an id, an exact name (any case), ``unassigned`` or ``unattributed``."""
    entries = as_list(refs)
    if not entries:
        return []
    rows = (await db.execute(select(Department.id, Department.name))).all()
    names = {row.id: row.name for row in rows}
    resolved: list[tuple[str, str]] = []
    unknown: list[str] = []
    for entry in entries:
        folded = entry.casefold()
        if folded in {UNASSIGNED_KEY, UNATTRIBUTED_KEY}:
            resolved.append((folded, department_label(folded, names)))
            continue
        parsed = safe_uuid(entry)
        if parsed is not None:
            # A deleted department's id still names its history.
            resolved.append((str(parsed), department_label(parsed, names)))
            continue
        matches = [department_id for department_id, name in names.items() if name.casefold() == folded]
        if len(matches) == 1:
            resolved.append((str(matches[0]), names[matches[0]]))
        else:
            unknown.append(entry)
    if unknown:
        raise InsightRefusedError(
            f"Error: no department matches {', '.join(map(repr, unknown))} (name one by id, exact name, "
            "'unassigned' or 'unattributed')."
        )
    return list(dict.fromkeys(resolved))


def _uuids(values: Any) -> frozenset[uuid.UUID]:
    return frozenset(parsed for value in values if (parsed := safe_uuid(str(value))) is not None)


async def _parse(db: AsyncSession, scope: InsightScope, args: dict[str, Any]) -> _Request:
    from app.services.tracing.read_store import resolve_date_range

    metric = _text(args, "metric") or "cost"
    if metric not in METRICS:
        raise InsightRefusedError(f"Error: metric must be one of {', '.join(METRICS)}.")
    group_by = _text(args, "group_by")
    if group_by is not None and group_by not in GROUP_BYS:
        raise InsightRefusedError(f"Error: group_by must be one of {', '.join(GROUP_BYS)}, or omitted for the total.")
    split_by = _text(args, "split_by")
    check_combination(metric, group_by, split_by)
    projection = _projection_args(args.get("projection"))
    if projection is not None and group_by != "month":
        raise InsightRefusedError(
            "Error: a projection fits complete months: use group_by month (with split_by for one projection per series)."
        )
    compare_to = _text(args, "compare_to")
    if compare_to is not None and compare_to != "previous_period":
        raise InsightRefusedError("Error: compare_to must be previous_period.")
    if compare_to is not None and group_by in TIME_BUCKETS:
        raise InsightRefusedError(
            f"Error: compare_to compares a total or a dimension grouping with the previous window; a {group_by} "
            "series already holds its earlier buckets - widen the period, or compare group_by "
            f"{split_by or 'department'} instead."
        )
    export = _text(args, "export")
    if export is not None and export != "csv":
        raise InsightRefusedError("Error: export must be csv.")
    attribution = _text(args, "attribution") or "attributed"
    if attribution not in ATTRIBUTION_MODES:
        raise InsightRefusedError(f"Error: attribution must be one of {', '.join(ATTRIBUTION_MODES)}.")
    kinds = [kind.lower() for kind in as_list(args.get("group_kinds"))]
    bad_kinds = [kind for kind in kinds if kind not in GROUP_KINDS]
    if bad_kinds:
        raise InsightRefusedError(f"Error: group_kinds must be among {', '.join(GROUP_KINDS)}.")
    try:
        limit = max(1, min(int(args.get("limit") or DEFAULT_LIMIT), MAX_LIMIT))
    except (TypeError, ValueError):
        raise InsightRefusedError("Error: limit is a whole number.") from None

    period = str(args.get("period") or "30d").strip().lower()
    date_from = parse_instant(args.get("date_from"), label="date_from")
    date_to = parse_instant(args.get("date_to"), label="date_to")
    try:
        start, end, prev_start, prev_end = resolve_date_range(period, date_from, date_to)
    except HTTPException as exc:
        raise InsightRefusedError(f"Error: {exc.detail}") from None

    agent_ids = as_list(args.get("agent_ids"))
    bad_agents = [value for value in agent_ids if safe_uuid(value) is None]
    if bad_agents:
        raise InsightRefusedError(f"Error: agent_ids must be agent UUIDs, not {', '.join(map(repr, bad_agents))}.")

    caveats: list[str] = []
    named_users = await resolve_users(db, args.get("user_ids"), scope, team=True)
    groups = await resolve_groups(db, args.get("group_ids"), scope, team=True)
    excluded_users = await resolve_users(db, args.get("exclude_user_ids"), scope, team=True)
    excluded_groups = await resolve_groups(db, args.get("exclude_group_ids"), scope, team=True)
    departments = await resolve_department_keys(db, args.get("department_ids"))
    excluded_departments = await resolve_department_keys(db, args.get("exclude_department_ids"))
    complement = bool(args.get("complement"))
    if complement and not (named_users or groups or departments):
        raise InsightRefusedError(
            "Error: complement reads everyone NOT in the named people, groups or departments: name some "
            "(user_ids, group_ids or department_ids)."
        )

    people: frozenset[uuid.UUID] | None = _uuids(user.id for user in named_users) if named_users else None
    if groups:
        members = _uuids(await group_member_ids(db, groups))
        people = members if people is None else people & members
    excluded_people = _uuids(user.id for user in excluded_users)
    if excluded_groups:
        excluded_people |= _uuids(await group_member_ids(db, excluded_groups))
    if groups or excluded_groups or group_by == "group" or split_by == "group" or scope.managed:
        caveats.append(CURRENT_MEMBERSHIP_NOTE)
    reach: frozenset[uuid.UUID] | None = None
    listed: frozenset[str] | None = None
    if not scope.platform:
        # Outside the platform scope, only the caller's usage and, for a group
        # manager, their teams' members', as they are today.
        reach = _uuids(scope.usage_reach())
        listed = frozenset(scope.group_ids) | scope.managed_group_ids

    selection = Selection(
        attribution=attribution,
        user_ids=people,
        department_keys=frozenset(key for key, _label in departments) if departments else None,
        complement=complement,
        exclude_user_ids=excluded_people,
        exclude_department_keys=frozenset(key for key, _label in excluded_departments),
        reach=reach,
    )
    filters = _Filters(
        period=period,
        date_from=date_from,
        date_to=date_to,
        agent_ids=_csv([str(uuid.UUID(value)) for value in agent_ids]),
        user_ids=None,
        models=_csv(as_list(args.get("models"))),
        sources=_csv(as_list(args.get("sources"))),
    )
    echo = {
        "agent_ids": as_list(filters.agent_ids),
        "users": [user.email for user in named_users],
        "groups": [group.name for group in groups],
        "departments": [label for _key, label in departments],
        "models": as_list(filters.models),
        "sources": as_list(filters.sources),
        "exclude_users": [user.email for user in excluded_users],
        "exclude_groups": [group.name for group in excluded_groups],
        "exclude_departments": [label for _key, label in excluded_departments],
        "complement": complement,
        "group_kinds": kinds,
    }
    return _Request(
        metric=metric,
        group_by=group_by,
        split_by=split_by,
        period=period,
        start=start,
        end=end,
        prev_start=prev_start,
        prev_end=prev_end,
        filters=filters,
        selection=selection,
        limit=limit,
        group_kinds=frozenset(kinds) if kinds else None,
        listed_groups=listed,
        named_groups=frozenset(group.id for group in groups),
        projection=projection,
        compare_to=compare_to,
        export=export,
        echo=echo,
        caveats=caveats,
    )


#: The breakdown key each dimension is read by; a group is folded from its members' rows.
_SQL_KEY = {"department": "department", "group": "person", "user": "person", "agent": "agent", "model": "model", "source": "source"}


@dataclass
class _Memberships:
    groups_of: dict[str, list[str]]
    names: dict[str, str]
    member_counts: dict[str, int]


async def _memberships(
    db: AsyncSession, persons: list[str], *, kinds: frozenset[str] | None, listed: frozenset[str] | None, team: bool
) -> _Memberships:
    from app.services.group_managers import team_member_clause

    groups_of: dict[str, list[str]] = defaultdict(list)
    ids = [parsed for value in persons if (parsed := safe_uuid(value)) is not None]
    statement = select(GroupMember.user_id, GroupMember.group_id).join(Group, Group.id == GroupMember.group_id)
    if kinds:
        statement = statement.where(Group.kind.in_(sorted(kinds)))
    for index in range(0, len(ids), 5000):
        for user_id, group_id in (await db.execute(statement.where(GroupMember.user_id.in_(ids[index : index + 5000])))).all():
            if listed is None or str(group_id) in listed:
                groups_of[str(user_id)].append(str(group_id))
    group_ids = sorted({group_id for values in groups_of.values() for group_id in values})
    names: dict[str, str] = {}
    counts: dict[str, int] = {}
    if group_ids:
        counted = GroupMember.group_id == Group.id
        if team:
            counted = counted & team_member_clause()
        rows = await db.execute(
            select(Group.id, Group.name, func.count(GroupMember.id))
            .outerjoin(GroupMember, counted)
            .where(Group.id.in_([uuid.UUID(group_id) for group_id in group_ids]))
            .group_by(Group.id, Group.name)
        )
        for group_id, name, members in rows.all():
            names[str(group_id)] = name
            counts[str(group_id)] = int(members)
    return _Memberships(groups_of=dict(groups_of), names=names, member_counts=counts)


async def agent_identities(db: AsyncSession, agent_ids: list[str]) -> dict[str, dict[str, Any]]:
    """``agent id -> {name, status, owner_email, owner_name, company_managed}``; a deleted agent by its last known name."""
    ids = [parsed for value in agent_ids if (parsed := safe_uuid(value)) is not None]
    if not ids:
        return {}
    rows = await db.execute(
        select(Agent.id, Agent.name, Agent.enabled, Agent.is_company_managed, User.email, User.full_name)
        .outerjoin(User, User.id == Agent.user_id)
        .where(Agent.id.in_(ids))
    )
    identities = {
        str(row.id): {
            "name": row.name,
            "status": "active" if row.enabled is not False else "disabled",
            "owner_email": row.email,
            "owner_name": row.full_name,
            "company_managed": bool(row.is_company_managed),
        }
        for row in rows.all()
    }
    missing = [agent_id for agent_id in ids if str(agent_id) not in identities]
    if missing:
        snapshots: dict[str, str] = {}
        for model in (LLMDailyStats, LLMTrace):
            found = await db.execute(
                select(model.agent_id, func.max(model.agent_name_snapshot))
                .where(model.agent_id.in_(missing), model.agent_name_snapshot.is_not(None))
                .group_by(model.agent_id)
            )
            for agent_id, name in found.all():
                snapshots.setdefault(str(agent_id), name)
        for agent_id in missing:
            key = str(agent_id)
            identities[key] = {
                "name": snapshots.get(key) or f"Deleted agent ({key[:8]})",
                "status": "deleted",
                "owner_email": None,
                "owner_name": None,
                "company_managed": False,
            }
    return identities


@dataclass
class _Read:
    """A computed read: its cells, keyed ``(bucket, *dimension keys)``, and what labels them."""

    cells: dict[tuple, Sums]
    labels: dict[str, dict[str, Any]]
    extras: dict[str, dict[str, dict[str, Any]]]
    totals: Sums
    ungrouped: Sums | None
    overlapping: bool


async def _compute(
    db: AsyncSession,
    scope: InsightScope,
    request: _Request,
    *,
    start: datetime,
    end: datetime,
    windows: list,
    keep_unattributed_people: bool = False,
) -> _Read:
    from app.services.tracing.read_store import use_daily_stats

    viewer = SimpleNamespace(id=scope.user.id, is_admin=True)
    common = {
        "viewer": viewer,
        "user_group_ids": list(scope.group_ids),
        "start": start,
        "end": end,
        "use_daily": use_daily_stats(start, end),
        "selection": request.selection,
        "windows": windows,
    }
    dims = request.dims
    keys = list(dict.fromkeys(_SQL_KEY[dim] for dim in dims))
    rows: list[BreakdownRow] = await breakdown(db, request.filters, keys=keys, bucket=request.bucket, **common)
    total_rows = rows if not keys and request.bucket is None else await breakdown(db, request.filters, **common)
    totals = total_rows[0].sums if total_rows else Sums()

    fold = "group" in dims
    memberships = _Memberships({}, {}, {})
    if fold:
        persons = sorted({row.keys["person"] for row in rows if row.keys.get("person")})
        memberships = await _memberships(
            db, persons, kinds=request.group_kinds, listed=request.listed_groups, team=not scope.platform
        )
    cells: dict[tuple, Sums] = {}
    ungrouped: Sums | None = None
    overlapping = False
    for row in rows:
        person = row.keys.get("person")
        choices: list[list[Any]] = []
        for dim in dims:
            if dim == "group":
                group_ids = memberships.groups_of.get(person or "", [])
                if request.named_groups:
                    group_ids = [group_id for group_id in group_ids if group_id in request.named_groups]
                overlapping = overlapping or len(group_ids) > 1
                if person is None:
                    choices.append([])
                elif not group_ids:
                    ungrouped = ungrouped or Sums()
                    ungrouped.add(row.sums, person)
                    choices.append([])
                else:
                    choices.append(group_ids)
            elif dim == "user" and person is None and not keep_unattributed_people:
                choices.append([])
            else:
                choices.append([row.keys[_SQL_KEY[dim]]])
        for combo in product(*choices):
            key = (row.keys.get("bucket"), *combo)
            if fold:
                cells.setdefault(key, Sums()).add(row.sums, person)
            elif key in cells:
                cells[key].add(row.sums)
            else:
                cells[key] = row.sums

    labels: dict[str, dict[str, Any]] = {}
    extras: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for position, dim in enumerate(dims, start=1):
        values = sorted({key[position] for key in cells}, key=str)
        if dim == "agent":
            identities = await agent_identities(db, [value for value in values if value])
            labels[dim] = {value: identities[value]["name"] if value in identities else NO_AGENT_LABEL for value in values}
            for value in values:
                identity = identities.get(value) if value else None
                extras[dim][value] = (
                    {key: identity[key] for key in ("status", "owner_email", "owner_name", "company_managed")}
                    if identity
                    else {"status": None, "owner_email": None, "owner_name": None, "company_managed": False}
                )
            continue
        labels[dim] = await _labels(db, dim, values, memberships)
        if dim == "group":
            for value in values:
                if scope.platform or value in scope.managed_group_ids:
                    extras[dim][value] = {"member_count": memberships.member_counts.get(value, 0)}
        elif dim == "user":
            # A person's groups today, within those the caller may name outside the platform scope.
            people = await _memberships(
                db, [value for value in values if value], kinds=None, listed=request.listed_groups, team=False
            )
            for value in values:
                names = {people.names[group_id] for group_id in people.groups_of.get(value or "", []) if group_id in people.names}
                extras[dim][value] = {"groups": sorted(names, key=str.casefold)}
    return _Read(cells=cells, labels=labels, extras=extras, totals=totals, ungrouped=ungrouped, overlapping=overlapping)


async def _labels(db: AsyncSession, dim: str, values: list[Any], memberships: _Memberships) -> dict[str, Any]:
    if dim == "department":
        names = (await load_department_map(db, fresh=True)).names
        return {value: department_label(value, names) for value in values}
    if dim == "user":
        ids = [parsed for value in values if value and (parsed := safe_uuid(value)) is not None]
        emails: dict[str, str] = {}
        for index in range(0, len(ids), 5000):
            rows = await db.execute(select(User.id, User.email).where(User.id.in_(ids[index : index + 5000])))
            emails.update({str(user_id): email for user_id, email in rows.all()})
        return {value: (emails.get(value) or value) if value else UNATTRIBUTED_KEY for value in values}
    if dim == "group":
        return {value: memberships.names.get(value, value) for value in values}
    if dim == "model":
        return {value: value or NO_MODEL_LABEL for value in values}
    return {value: value for value in values}


def _bucket_bounds(bucket: str, value: str) -> tuple[date, date] | None:
    if bucket == "month":
        first = date.fromisoformat(value)
        return first, add_months(first, 1)
    if bucket == "week":
        first = date.fromisoformat(value)
        return first, first + timedelta(days=7)
    if bucket == "day":
        first = date.fromisoformat(value)
        return first, first + timedelta(days=1)
    return None


def _partial(bucket: str | None, value: Any, start: datetime, end: datetime) -> bool:
    """Whether the window cuts the time bucket *value* (its first or its last)."""
    if bucket not in {"week", "month"} or not value:
        return False
    first, after = _bucket_bounds(bucket, value)
    begins = datetime.combine(first, datetime.min.time(), tzinfo=UTC)
    finishes = datetime.combine(after, datetime.min.time(), tzinfo=UTC)
    return begins < start or finishes > end


async def _unattributed(db: AsyncSession, scope: InsightScope, request: _Request, *, start, end) -> dict[str, Any] | None:
    from app.services.tracing.read_store import use_daily_stats

    rows = await breakdown(
        db,
        request.filters,
        viewer=SimpleNamespace(id=scope.user.id, is_admin=True),
        user_group_ids=list(scope.group_ids),
        start=start,
        end=end,
        use_daily=use_daily_stats(start, end),
        keys=["attribution"],
        selection=request.selection,
        only_unattributed=True,
    )
    if not rows:
        return None
    total = Sums()
    classes: dict[str, Any] = {}
    for row in rows:
        total.add(row.sums)
        attribution = row.keys["attribution"]
        classes[attribution] = {
            "trace_count": row.sums.trace_count,
            "total_cost": row.sums.cost,
            "total_tokens": row.sums.input_tokens + row.sums.output_tokens,
            "what": RESIDUAL_NOTES.get(
                attribution,
                "not stamped yet: written before attribution existed, waiting for the backfill"
                if attribution == UNSTAMPED
                else "the trace has no user of its own; with attribution attributed it counts for a person",
            ),
        }
    return {
        "trace_count": total.trace_count,
        "total_cost": total.cost,
        "total_tokens": total.input_tokens + total.output_tokens,
        "avg_cost_per_trace": total.cost / total.trace_count if total.trace_count else 0.0,
        "classes": classes,
    }


def _ungrouped_block(sums: Sums | None) -> dict[str, Any] | None:
    if sums is None or not sums.trace_count:
        return None
    return {
        "trace_count": sums.trace_count,
        "total_cost": sums.cost,
        "total_tokens": sums.input_tokens + sums.output_tokens,
        "active_user_count": sums.people_count,
        "avg_cost_per_active_user": sums.cost / sums.people_count if sums.people_count else 0.0,
    }


def _rank_value(metric: str, sums: Sums) -> float:
    value = metric_value(metric, sums) if VALUE_KINDS[metric] in ADDITIVE_KINDS else sums.trace_count
    return float(value or 0)


def _format_rows(request: _Request, read: _Read, *, windows: list) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The tidy rows of *read* for the request's metric, and what the ``limit`` folded."""
    metric, dims, bucket = request.metric, request.dims, request.bucket
    other: dict[str, Any] | None = None
    cells = dict(read.cells)
    if dims and bucket is None:
        ranked: dict[Any, Sums] = defaultdict(Sums)
        for key, sums in cells.items():
            ranked[key[1]].add(sums)
        order = sorted(ranked, key=lambda value: (-_rank_value(metric, ranked[value]), str(value)))
        kept = set(order[: request.limit])
        rest = [value for value in order if value not in kept]
        if rest:
            folded = Sums()
            for value in rest:
                folded.add(ranked[value])
            other = {
                "dimension_count": len(rest),
                "trace_count": folded.trace_count,
                "value": metric_value(metric, folded) if VALUE_KINDS[metric] in ADDITIVE_KINDS else None,
            }
            cells = {key: sums for key, sums in cells.items() if key[1] in kept}
    rows: list[dict[str, Any]] = []
    for key, sums in cells.items():
        value = metric_value(metric, sums)
        if not dims:
            row: dict[str, Any] = {"bucket": key[0], "dimension": None, "value": value}
        elif bucket is not None:
            dim = dims[0]
            row = {"bucket": key[0], "dimension": read.labels[dim][key[1]], "dimension_id": key[1], "value": value}
            row.update(read.extras.get(dim, {}).get(key[1], {}))
        elif len(dims) == 1:
            dim = dims[0]
            row = {"bucket": None, "dimension": read.labels[dim][key[1]], "dimension_id": key[1], "value": value}
            row.update(read.extras.get(dim, {}).get(key[1], {}))
            if dim in {"department", "group"}:
                row["active_user_count"] = sums.people_count
        else:
            first, second = dims
            row = {
                "bucket": read.labels[first][key[1]],
                "bucket_id": key[1],
                "dimension": read.labels[second][key[2]],
                "dimension_id": key[2],
                "value": value,
            }
        if bucket and _partial(bucket, key[0], request.start, request.end):
            row["partial"] = True
        rows.append(row)
    if bucket == "release":
        rows.sort(key=lambda row: (release_order(row["bucket"], windows), str(row["dimension"])))
    elif bucket is not None:
        rows.sort(key=lambda row: (str(row["bucket"]), str(row["dimension"])))
    else:
        rows.sort(key=lambda row: (row["value"] is None, -(row["value"] or 0), str(row.get("bucket")), str(row["dimension"])))
    return rows, other


def _complete_months(start: datetime, end: datetime, coverage: date | None, today: date) -> list[date]:
    """The months wholly inside the window, before the current one and within the data the platform holds."""
    month = date(start.year, start.month, 1)
    if datetime.combine(month, datetime.min.time(), tzinfo=UTC) < start:
        month = add_months(month, 1)
    current = date(today.year, today.month, 1)
    months: list[date] = []
    while datetime.combine(add_months(month, 1), datetime.min.time(), tzinfo=UTC) <= end and month < current:
        if coverage is None or coverage <= month:
            months.append(month)
        month = add_months(month, 1)
    return months


def _project(request: _Request, read: _Read, *, coverage: date | None, now: datetime) -> dict[str, Any]:
    spec = request.projection or {}
    metric = request.metric
    additive = VALUE_KINDS[metric] in ADDITIVE_KINDS
    months = _complete_months(request.start, request.end, coverage, now.date())
    current = date(now.year, now.month, 1)
    split = request.split_by
    series_keys = sorted({key[1] for key in read.cells}, key=str) if split else [None]
    by_series: dict[Any, dict[str, Sums]] = defaultdict(dict)
    for key, sums in read.cells.items():
        by_series[key[1] if split else None][key[0]] = sums
    result_series: list[dict[str, Any]] = []
    projected_total: dict[str, float] = defaultdict(float)
    elapsed = (now - datetime.combine(current, datetime.min.time(), tzinfo=UTC)).total_seconds() / 86400
    days_in_month = monthrange(current.year, current.month)[1]
    for series_key in series_keys:
        cells = by_series.get(series_key, {})
        label = read.labels[split][series_key] if split else None
        basis: list[tuple[date, float]] = []
        for month in months:
            sums = cells.get(month.isoformat())
            value = metric_value(metric, sums) if sums is not None else (0.0 if additive else None)
            if value is not None:
                basis.append((month, float(value)))
        entry: dict[str, Any] = {"dimension": label, "dimension_id": series_key}
        to_date = cells.get(current.isoformat())
        if to_date is not None and request.end > datetime.combine(current, datetime.min.time(), tzinfo=UTC):
            actual = metric_value(metric, to_date)
            entry["current_month"] = {
                "bucket": current.isoformat(),
                "actual_to_date": actual,
                "run_rate": (actual * days_in_month / elapsed) if additive and actual is not None and elapsed > 0 else actual,
                "elapsed_days": round(elapsed, 2),
                "days_in_month": days_in_month,
                "partial": True,
            }
        try:
            fitted = fit(spec["method"], [value for _month, value in basis], horizon=spec["horizon_months"], last_n=spec["last_n"])
        except ProjectionError as exc:
            entry["error"] = str(exc)
            result_series.append(entry)
            continue
        last = basis[-1][0]
        offset = len(basis) - len(fitted.fitted)
        entry["basis"] = [
            {"bucket": month.isoformat(), "actual": value, "fitted": fitted.fitted[index - offset] if index >= offset else None}
            for index, (month, value) in enumerate(basis)
        ]
        entry["projected"] = [
            {"bucket": add_months(last, step).isoformat(), "value": value}
            for step, value in enumerate(fitted.projected, start=1)
        ]
        entry["params"] = fitted.params
        entry["r2"] = fitted.r2
        entry["mape"] = fitted.mape
        if fitted.notes:
            entry["notes"] = fitted.notes
        if additive:
            for point in entry["projected"]:
                projected_total[point["bucket"]] += point["value"]
        result_series.append(entry)
    result: dict[str, Any] = {
        "method": spec["method"],
        "horizon_months": spec["horizon_months"],
        "basis_months": [month.isoformat() for month in months],
        "series": result_series,
        "rows": [
            {"bucket": point["bucket"], "dimension": entry["dimension"], "value": point["value"], "projected": True}
            for entry in result_series
            for point in entry.get("projected", [])
        ],
        "note": (
            "Fitted on complete months only: the window's first month when the window cuts it, the current month "
            "and any month before the data the platform holds are left out. The current month is extrapolated "
            "from its elapsed days (current_month.run_rate, partial)."
        ),
    }
    if spec["method"] == "last_n_avg":
        result["last_n"] = spec["last_n"]
    if additive and projected_total:
        ordered = sorted(projected_total.items())
        result["projected_total"] = [{"bucket": bucket, "value": value} for bucket, value in ordered]
        result["next_12_months_total"] = sum(value for _bucket, value in ordered[:12])
    return result


async def _coverage(db: AsyncSession) -> dict[str, Any]:
    from app.services.tracing.read_store import oldest_trace_started_at

    aggregate = (await db.execute(select(func.min(LLMDailyStats.date)))).scalar()
    oldest = await oldest_trace_started_at(db)
    raw = oldest.date() if oldest else None
    candidates = [value for value in (aggregate, raw) if value is not None]
    last = (
        await db.execute(
            select(PlatformSettings.last_aggregation_at)
            .order_by(PlatformSettings.created_at.desc())
            .limit(1)
        )
    ).scalar()
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return {
        "stats_coverage_start": min(candidates) if candidates else None,
        "raw_trace_coverage_start": raw,
        "aggregate_coverage_start": aggregate,
        "last_aggregation_at": last,
        "aggregation_stale": last is None or (datetime.now(UTC) - last).total_seconds() > 48 * 3600,
    }


async def _null_cost_models(db: AsyncSession, scope: InsightScope, request: _Request) -> list[dict[str, Any]]:
    from app.services.tracing.read_store import build_trace_filters
    from app.services.tracing.unpriced_models import null_cost_models

    traces = build_trace_filters(
        request.filters,
        user=SimpleNamespace(id=scope.user.id, is_admin=True),
        user_group_ids=list(scope.group_ids),
        start=request.start,
        end=request.end,
    )
    predicate = request.selection.predicate(LLMTrace)
    if predicate is not None:
        traces = traces.where(predicate)
    return await null_cost_models(db, traces)


async def _department_resolution(db: AsyncSession) -> dict[str, Any]:
    mapping = await load_department_map(db, fresh=True)
    state = await db.get(TraceAttributionBackfill, 1)
    return {
        "configured": mapping.configured,
        "departments": len(mapping.names),
        "sources": list(mapping.settings.sources),
        "history_stamped_at": state.completed_at.isoformat() if state and state.completed_at else None,
    }


async def _window_totals(db: AsyncSession, scope: InsightScope, request: _Request, *, start, end) -> Sums:
    from app.services.tracing.read_store import use_daily_stats

    rows = await breakdown(
        db,
        request.filters,
        viewer=SimpleNamespace(id=scope.user.id, is_admin=True),
        user_group_ids=list(scope.group_ids),
        start=start,
        end=end,
        use_daily=use_daily_stats(start, end),
        selection=request.selection,
    )
    return rows[0].sums if rows else Sums()


ExportSink = Callable[[str, bytes], Awaitable[str]]


async def query_platform_analytics(
    db: AsyncSession, scope: InsightScope, args: dict[str, Any], *, export_sink: ExportSink | None = None
) -> dict[str, Any]:
    """The read *args* describe, as tidy rows; ``export: csv`` hands the CSV to *export_sink* (the turn's files)."""
    request = await _parse(db, scope, args)
    if request.export:
        return await _export_to_file(db, scope, request, export_sink)
    caveats = request.caveats
    result: dict[str, Any] = {
        **scope.header(),
        "metric": request.metric,
        "group_by": request.group_by,
        "split_by": request.split_by,
        "attribution": request.selection.attribution,
        "unit": UNITS[request.metric],
        "value_kind": VALUE_KINDS[request.metric],
        "window": {"period": request.period, "start": request.start.isoformat(), "end": request.end.isoformat()},
        "filters": request.echo,
    }
    if request.selection.reach is not None and not request.selection.reach:
        result.update(rows=[], totals=None, note="Nobody is in reach of this read: nothing to report.", caveats=caveats)
        return result

    windows = await release_windows(db) if request.group_by == "release" else []
    now = datetime.now(UTC)
    coverage = await _coverage(db)
    extras: dict[str, Any] = {}
    previous_totals: Sums | None = None
    if request.metric == "latency":
        rows, totals_sums = await _latency_rows(db, scope, request)
        read = None
    else:
        read = await _compute(db, scope, request, start=request.start, end=request.end, windows=windows)
        totals_sums = read.totals
        rows, other = _format_rows(request, read, windows=windows)
        if other is not None:
            extras["other"] = other
            extras["other_note"] = f"Past the first {request.limit} (limit), the rest are summed in 'other'."
        if read.overlapping:
            caveats.append(
                "Some people belong to several of these groups and count in each of them: the group rows add up "
                "to more than the total. Departments never overlap: group_by department for a partition."
            )
        extras["ungrouped"] = _ungrouped_block(read.ungrouped) if "group" in request.dims else None
        if request.compare_to:
            previous = await _compute(db, scope, request, start=request.prev_start, end=request.prev_end, windows=windows)
            previous_totals = previous.totals
            before = {key: metric_value(request.metric, sums) for key, sums in previous.cells.items()}
            for row in rows:
                if len(request.dims) == 2:
                    key: tuple = (None, row.get("bucket_id"), row.get("dimension_id"))
                elif request.dims:
                    key = (None, row.get("dimension_id"))
                else:
                    key = (None,)
                prior = before.get(key)
                row["previous_value"] = prior
                row["delta"] = None if row["value"] is None or prior is None else row["value"] - prior
                row["delta_pct"] = (
                    100 * (row["value"] - prior) / prior if row["value"] is not None and prior else None
                )
            extras["compared_window"] = {"start": request.prev_start.isoformat(), "end": request.prev_end.isoformat()}
        if request.projection:
            extras["projection"] = _project(request, read, coverage=coverage["stats_coverage_start"], now=now)
    if request.bucket in {"week", "month"}:
        caveats.append(f"The first and last {request.bucket} cover only the part of it inside the window ('partial').")
    if request.group_by == "release":
        result["release_history"] = [window.payload() for window in windows]
        caveats.append(history_note(windows, request.start))
        caveats.append("Analytics are day-grained: a day on which a new build was first seen counts for that build.")
    if "department" in request.dims or request.echo["departments"] or request.echo["exclude_departments"]:
        resolution = await _department_resolution(db)
        extras["department_resolution"] = resolution
        if not resolution["configured"]:
            caveats.append(
                "No department is defined yet (Settings > Organization > Departments): everyone reads as "
                "Unassigned."
            )
    if totals_sums.unstamped_count:
        caveats.append(
            f"{totals_sums.unstamped_count} trace(s) of the window were written before attribution existed and "
            "are not stamped yet (the backfill is on its way): they count for their own user and no department."
        )
    if request.metric == "cached_tokens":
        caveats.append("Cached tokens are recorded since attribution: older usage counts none.")
    if coverage["aggregation_stale"]:
        caveats.append(STALE_NOTE)
    starts_before = coverage["stats_coverage_start"] is not None and request.start.date() < coverage["stats_coverage_start"]
    if starts_before:
        caveats.append(
            f"This window starts before the oldest data the platform still holds "
            f"({coverage['stats_coverage_start']}): the days before it are missing data, not zero activity."
        )
    null_cost = await _null_cost_models(db, scope, request)
    unknown_cost = sum(item["trace_count"] for item in null_cost)
    unpriced = [{k: item[k] for k in ("model", "trace_count", "tokens")} for item in null_cost if not item["has_price"]]
    unrepriced = [{k: item[k] for k in ("model", "trace_count", "tokens")} for item in null_cost if item["has_price"]]
    if unknown_cost and request.metric in COST_METRICS:
        caveats.append(
            f"{unknown_cost} trace(s) of the window have no known cost, left out of every cost: "
            + (f"no price for {', '.join(item['model'] for item in unpriced[:5])}" if unpriced else "")
            + ("; " if unpriced and unrepriced else "")
            + (f"priced since, not repriced yet: {', '.join(item['model'] for item in unrepriced[:5])}" if unrepriced else "")
            + ". An administrator sets a price with its effective date in Settings > Usage and quotas > Model pricing "
            "and reprices from that date."
        )
    if previous_totals is None:
        previous_totals = await _window_totals(db, scope, request, start=request.prev_start, end=request.prev_end)
    truncated = len(rows) > MAX_ROWS
    result.update(
        rows=rows[:MAX_ROWS],
        truncated=truncated,
        totals=_totals(totals_sums),
        previous_window_totals=_totals(previous_totals),
        aggregation_stale=coverage["aggregation_stale"],
        last_aggregation_at=coverage["last_aggregation_at"].isoformat() if coverage["last_aggregation_at"] else None,
        stats_coverage_start=str(coverage["stats_coverage_start"]) if coverage["stats_coverage_start"] else None,
        raw_trace_coverage_start=(
            str(coverage["raw_trace_coverage_start"]) if coverage["raw_trace_coverage_start"] else None
        ),
        aggregate_coverage_start=(
            str(coverage["aggregate_coverage_start"]) if coverage["aggregate_coverage_start"] else None
        ),
        window_starts_before_coverage=starts_before,
        unknown_cost_trace_count=unknown_cost,
        unpriced_models=unpriced,
        unrepriced_models=unrepriced,
        unattributed=await _unattributed(db, scope, request, start=request.start, end=request.end),
        overlapping_membership=bool(read and read.overlapping),
        **extras,
        capabilities=capabilities(),
    )
    cost_masked = not settings.observability_cost_data_enabled
    result["cost_masked"] = cost_masked
    if cost_masked:
        for key in ("totals", "previous_window_totals", "unattributed", "ungrouped", "other"):
            if result.get(key) is not None:
                result[key] = _mask_costs(result[key])
        if request.metric in COST_METRICS:
            for row in result["rows"]:
                for name in ("value", "previous_value", "delta", "delta_pct"):
                    if name in row:
                        row[name] = None
            if result.get("other"):
                result["other"]["value"] = None
            if "projection" in result:
                result["projection"] = {"note": "Costs are hidden on this platform: nothing to project."}
        caveats.append(COST_MASKED_NOTE)
    result["caveats"] = caveats
    return result


async def _latency_rows(db: AsyncSession, scope: InsightScope, request: _Request) -> tuple[list[dict[str, Any]], Sums]:
    """``latency`` (the median) from the latency histograms; the total from the grouped read."""
    from app.services.tracing.read_store import use_daily_stats
    from app.services.tracing.store import build_trace_read_store, ensure_trace_read_provider_supported

    selection = request.selection
    if selection.department_keys is not None or selection.exclude_department_keys or selection.complement:
        raise InsightRefusedError(
            "Error: latency (the median) reads the traces' own users, without departments, exclusions or a "
            "complement. Use metric duration (the average in ms), which every filter has."
        )
    if selection.exclude_user_ids:
        raise InsightRefusedError(
            "Error: latency (the median) takes no exclusion. Use metric duration (the average in ms), which every "
            "filter has."
        )
    if selection.attribution != "raw" and (selection.user_ids is not None or selection.reach is not None):
        request.caveats.append(
            "Latency (the median) reads the people's own traces: their assignment runs, sub-agents and agents' "
            "webhooks are left out. Metric duration (the average) counts the usage attributed to them."
        )
    people = selection.user_ids
    if selection.reach is not None:
        people = selection.reach if people is None else people & selection.reach
    if people is not None and not people:
        return [], Sums()
    filters = replace(request.filters, user_ids=_csv(sorted(str(person) for person in people)) if people else None)
    ensure_trace_read_provider_supported()
    store = build_trace_read_store()
    viewer = SimpleNamespace(id=scope.user.id, is_admin=True)
    use_daily = use_daily_stats(request.start, request.end)
    latency = await store.analytics_latency(
        db, filters=filters, user=viewer, user_group_ids=list(scope.group_ids), start=request.start, end=request.end,
        use_daily=use_daily,
    )
    rows: list[dict[str, Any]] = []
    if request.group_by is None:
        overall = latency.get("overall") or {}
        rows.append({"bucket": None, "dimension": None, "value": overall.get("p50"), "percentiles": overall})
    elif request.group_by == "model":
        for item in latency.get("by_model") or []:
            rows.append({"bucket": None, "dimension": item["model"], "value": item.get("p50"), "p95": item.get("p95")})
    else:
        for point in latency.get("daily_median") or []:
            rows.append({"bucket": point["date"], "dimension": None, "value": point.get("p50")})
    if latency.get("approximate") and rows:
        rows[0]["approximate"] = True
    return rows, await _window_totals(db, scope, request, start=request.start, end=request.end)


async def export_rows(
    db: AsyncSession, scope: InsightScope, args: dict[str, Any]
) -> tuple[list[str], list[list[Any]], dict[str, Any]]:
    """Every row of a read with every metric as a column, uncut: ``(header, rows, totals)``."""
    return await _export_rows(db, scope, await _parse(db, scope, {**args, "export": None}))


async def _export_rows(
    db: AsyncSession, scope: InsightScope, request: _Request
) -> tuple[list[str], list[list[Any]], dict[str, Any]]:
    """The rows partition the total for every dimension but a group (a person in
    several counts in each): the usage of nobody is a row of its own."""
    windows = await release_windows(db) if request.group_by == "release" else []
    if request.selection.reach is not None and not request.selection.reach:
        return [], [], _totals(Sums())
    read = await _compute(
        db, scope, request, start=request.start, end=request.end, windows=windows, keep_unattributed_people=True
    )
    dims = request.dims
    header = [*(["bucket"] if request.bucket else []), *[part for dim in dims for part in (dim, f"{dim}_id")]]
    header += ["partial"] if request.bucket else []
    header += list(EXPORT_METRICS)
    masked = not settings.observability_cost_data_enabled
    rows: list[list[Any]] = []
    ordered = sorted(read.cells.items(), key=lambda item: tuple(str(part) for part in item[0]))
    for key, sums in ordered[:MAX_EXPORT_ROWS]:
        line: list[Any] = [key[0]] if request.bucket else []
        for position, dim in enumerate(dims, start=1):
            value = key[position]
            label = read.labels[dim].get(value)
            if dim == "user" and value is None:
                label = "unattributed"
            line += [label, value]
        if request.bucket:
            line.append(_partial(request.bucket, key[0], request.start, request.end))
        for metric in EXPORT_METRICS:
            value = metric_value(metric, sums)
            line.append(None if masked and metric in COST_METRICS else value)
        rows.append(line)
    totals = _totals(read.totals)
    return header, rows, (_mask_costs(totals) if masked else totals)


def rows_to_csv(header: list[str], rows: list[list[Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows:
        writer.writerow(["" if value is None else value for value in row])
    return buffer.getvalue().encode("utf-8")


def export_filename(group_by: str | None, split_by: str | None) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    return f"llm-usage-{group_by or 'total'}{'-' + split_by if split_by else ''}-{stamp}.csv"


async def _export_to_file(
    db: AsyncSession, scope: InsightScope, request: _Request, sink: ExportSink | None
) -> dict[str, Any]:
    """``export: csv`` through the tool: the CSV is the turn's file; returns where it is and what it holds."""
    header, rows, totals = await _export_rows(db, scope, request)
    filename = export_filename(request.group_by, request.split_by)
    payload = rows_to_csv(header, rows)
    result: dict[str, Any] = {
        **scope.header(),
        "export": "csv",
        "filename": filename,
        "row_count": len(rows),
        "truncated": len(rows) >= MAX_EXPORT_ROWS,
        "columns": header,
        "totals": totals,
        "window": {"period": request.period, "start": request.start.isoformat(), "end": request.end.isoformat()},
        "caveats": request.caveats,
    }
    if sink is not None:
        marker = await sink(filename, payload)
        result["file"] = marker
        result["instruction"] = f"Put {marker} in your reply where the file belongs: it renders as a download."
    else:
        result["note"] = (
            "This surface keeps no file: the same CSV is served by GET /api/v1/insights/analytics/export with "
            "these arguments as query parameters."
        )
        result["preview"] = rows[:20]
    return result


async def analytics_export_csv(db: AsyncSession, scope: InsightScope, args: dict[str, Any]) -> tuple[str, bytes]:
    """``(filename, CSV bytes)`` of a read, for the REST export."""
    request = await _parse(db, scope, {**args, "export": None})
    header, rows, _totals = await _export_rows(db, scope, request)
    return export_filename(request.group_by, request.split_by), rows_to_csv(header, rows)
