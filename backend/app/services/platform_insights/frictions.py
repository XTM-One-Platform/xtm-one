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

"""``query_platform_frictions``: the friction ledger counted by agent, kind, class or tool.

The rows of ``agent_frictions`` (``services/frictions.py``, issue #8528) over a
window: how many frictions were recorded, how often they were met (``count``
summed), how many are still without a disposition, and the same counted per
agent, kind, class, tool or disposition, optionally per day / week / month of
their recording, as tidy rows ``{bucket, dimension, value}`` ``render_chart``
draws. An administrator's agent reads the platform; anyone else the frictions
recorded for them (``agent_frictions.user_id``).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import String, func, literal, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import safe_uuid
from app.models.agent import Agent
from app.models.agent_friction import FRICTION_CLASSES, FRICTION_KINDS, AgentFriction
from app.services.platform_insights.scope import InsightRefusedError, InsightScope, as_list, resolve_period

#: What a count can be cut by.
GROUP_BYS: tuple[str, ...] = ("agent", "kind", "class", "tool", "disposition")
BUCKETS: tuple[str, ...] = ("day", "week", "month")
#: What each row counts: records, occurrences (repeats included) or records left undisposed.
MEASURES: tuple[str, ...] = ("recorded", "occurrences", "undisposed")
MAX_ROWS = 500
#: The label of a friction with no disposition yet, in a group_by disposition.
UNDISPOSED_LABEL = "undisposed"


def _measure(measure: str):
    if measure == "occurrences":
        return func.coalesce(func.sum(AgentFriction.count), 0)
    if measure == "undisposed":
        return func.count().filter(AgentFriction.disposed_at.is_(None))
    return func.count()


def _dimension(group_by: str):
    if group_by == "agent":
        return AgentFriction.agent_id.cast(String)
    if group_by == "kind":
        return AgentFriction.kind
    if group_by == "class":
        return AgentFriction.friction_class
    if group_by == "tool":
        return AgentFriction.tool
    return func.coalesce(AgentFriction.disposition_kind, literal(UNDISPOSED_LABEL))


def _bucket(bucket: str):
    utc = func.timezone(literal_column("'UTC'"), AgentFriction.recorded_at)
    pattern = "YYYY-MM" if bucket == "month" else "YYYY-MM-DD"
    return func.to_char(func.date_trunc(bucket, utc), pattern)


def _choice(args: dict[str, Any], key: str, allowed: tuple[str, ...], default: str | None) -> str | None:
    value = (str(args.get(key) or "").strip().lower()) or default
    if value is not None and value not in allowed:
        raise InsightRefusedError(f"Error: {key} must be one of {', '.join(allowed)}.")
    return value


def _subset(args: dict[str, Any], key: str, allowed: tuple[str, ...]) -> list[str]:
    values = [value.lower() for value in as_list(args.get(key))]
    unknown = [value for value in values if value not in allowed]
    if unknown:
        raise InsightRefusedError(
            f"Error: unknown {key} {', '.join(map(repr, unknown))}. Known: {', '.join(allowed)}."
        )
    return values


async def query_platform_frictions(db: AsyncSession, scope: InsightScope, args: dict[str, Any]) -> dict[str, Any]:
    group_by = _choice(args, "group_by", GROUP_BYS, None)
    bucket = _choice(args, "bucket", BUCKETS, None)
    measure = _choice(args, "measure", MEASURES, "recorded") or "recorded"
    kinds = _subset(args, "kinds", FRICTION_KINDS)
    classes = _subset(args, "classes", FRICTION_CLASSES)
    period, start, end = resolve_period(args.get("period"), args.get("date_from"), args.get("date_to"))

    agent_refs = as_list(args.get("agent_ids"))
    agent_ids = [parsed for ref in agent_refs if (parsed := safe_uuid(ref)) is not None]
    if len(agent_ids) != len(agent_refs):
        raise InsightRefusedError("Error: agent_ids must be agent UUIDs.")

    clauses = [AgentFriction.recorded_at >= start, AgentFriction.recorded_at <= end]
    if not scope.platform:
        clauses.append(AgentFriction.user_id == scope.user.id)
    if agent_ids:
        clauses.append(AgentFriction.agent_id.in_(agent_ids))
    if kinds:
        clauses.append(AgentFriction.kind.in_(kinds))
    if classes:
        clauses.append(AgentFriction.friction_class.in_(classes))
    if args.get("undisposed") is True:
        clauses.append(AgentFriction.disposed_at.is_(None))

    totals_row = (
        await db.execute(
            select(
                func.count().label("recorded"),
                func.coalesce(func.sum(AgentFriction.count), 0).label("occurrences"),
                func.count().filter(AgentFriction.disposed_at.is_(None)).label("undisposed"),
            ).where(*clauses)
        )
    ).one()
    result: dict[str, Any] = {
        **scope.without_team().header(),
        "period": period,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "filters": {
            "agent_ids": [str(agent_id) for agent_id in agent_ids],
            "kinds": kinds,
            "classes": classes,
            "undisposed": args.get("undisposed") is True,
        },
        "totals": {
            "recorded": int(totals_row.recorded or 0),
            "occurrences": int(totals_row.occurrences or 0),
            "undisposed": int(totals_row.undisposed or 0),
        },
        "measure": measure,
        "caveats": [
            "'recorded' counts frictions (one per kind, tool and evidence in a run); 'occurrences' adds the "
            "repeats a run met again. The ledger starts with the build that shipped it: nothing earlier exists.",
        ],
    }
    if group_by is None and bucket is None:
        return result

    columns = []
    groups = []
    if bucket is not None:
        bucket_column = _bucket(bucket).label("bucket")
        columns.append(bucket_column)
        groups.append(literal_column("bucket"))
    if group_by is not None:
        dimension_column = _dimension(group_by).label("dimension")
        columns.append(dimension_column)
        groups.append(literal_column("dimension"))
    statement = (
        select(*columns, _measure(measure).label("value"))
        .where(*clauses)
        .group_by(*groups)
        .order_by(*groups)
        .limit(MAX_ROWS + 1)
    )
    fetched = (await db.execute(statement)).all()
    names: dict[str, str] = {}
    if group_by == "agent":
        ids = {safe_uuid(row.dimension) for row in fetched} - {None}
        if ids:
            names = {
                str(row.id): row.name
                for row in (await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(ids)))).all()
            }
    rows = []
    for row in fetched[:MAX_ROWS]:
        entry: dict[str, Any] = {
            "bucket": getattr(row, "bucket", None),
            "dimension": getattr(row, "dimension", None),
            "value": int(row.value or 0),
        }
        if group_by == "agent":
            entry["agent_id"] = entry["dimension"]
            entry["dimension"] = names.get(entry["dimension"], entry["dimension"])
        if group_by == "tool" and entry["dimension"] == "":
            entry["dimension"] = "(no tool)"
        rows.append(entry)
    result.update(group_by=group_by, bucket=bucket, rows=rows, truncated=len(fetched) > MAX_ROWS)
    if bucket in {"week", "month"}:
        result["caveats"].append(f"The first and last {bucket} cover only the part of it inside the window.")
    return result
