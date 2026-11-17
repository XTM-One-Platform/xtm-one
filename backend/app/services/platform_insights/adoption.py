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

"""``query_ai_adoption``: the AI Adoption dashboard, as tidy rows.

For an administrator the payloads ARE the dashboard's: the ``api/v1/ai_adoption``
route functions are called with the same arguments the page sends, then trimmed
to rows - so the two cannot disagree. The dashboard tracks agentic AI on the
platform alone (#11380): no development tool, no provider, no sync status.

Outside the administrator scope the dashboard is not readable; the caller gets
their own metric rows, scored with the dashboard's activity keys.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_adoption_metric import AiAdoptionMetric
from app.services.platform_insights.scope import InsightRefusedError, InsightScope, resolve_groups

VIEWS: tuple[str, ...] = ("overview", "gaps")

#: The most people a list of the result names.
MAX_PEOPLE = 25
MAX_ROWS = 500


def _people(entries: list[dict[str, Any]], *fields: str) -> list[dict[str, Any]]:
    return [{field: entry.get(field) for field in fields} for entry in entries[:MAX_PEOPLE]]


async def _admin_view(db: AsyncSession, scope: InsightScope, view: str, days: int, group_id: str | None) -> dict[str, Any]:
    from app.api.v1 import ai_adoption as dashboard

    if view == "overview":
        payload = await dashboard.get_ai_adoption_overview(days=days, group_id=group_id, user=scope.user, db=db)
        return {
            "rows": [{"bucket": None, "dimension": "active users", "value": payload.get("active_user_count")}],
            "value_label": "active users",
            "totals": {
                "active_user_count": payload.get("active_user_count"),
                "prev_active_user_count": payload.get("prev_active_user_count"),
                "total_tracked_users": payload.get("total_tracked_users"),
                "total_metrics": payload.get("total_metrics"),
            },
            "top_users": _people(payload.get("top_users") or [], "name", "email", "activity_score"),
        }
    payload = await dashboard.get_adoption_gaps(days=days, group_id=group_id, user=scope.user, db=db)
    summary = payload.get("summary") or {}
    return {
        "rows": [
            {"bucket": None, "dimension": state, "value": summary.get(f"{state}_count")}
            for state in ("active", "low_activity", "inactive")
        ],
        "trend_rows": [
            {"bucket": point["date"], "dimension": "adoption rate", "value": point["adoption_rate"]}
            for point in payload.get("trend") or []
        ],
        "value_label": "users",
        "totals": {
            "total_users": payload.get("total_users"),
            "activity_threshold": payload.get("activity_threshold"),
            "avg_active_days": summary.get("avg_active_days"),
        },
        "previous_summary": payload.get("prev_summary"),
        "inactive": _people(payload.get("inactive") or [], "name", "email"),
        "people_note": f"Lists name at most {MAX_PEOPLE} people; the counts are complete.",
    }


async def _own_view(db: AsyncSession, scope: InsightScope, days: int) -> dict[str, Any]:
    from app.api.v1.ai_adoption import PLATFORM_PROVIDER, activity_score

    start = date.today() - timedelta(days=days)
    statement = select(AiAdoptionMetric.date, AiAdoptionMetric.data).where(
        AiAdoptionMetric.user_id == scope.user.id,
        AiAdoptionMetric.provider == PLATFORM_PROVIDER,
        AiAdoptionMetric.date >= start,
    )
    scores: dict[str, int] = {}
    for day, data in (await db.execute(statement)).all():
        key = day.isoformat()
        scores[key] = scores.get(key, 0) + activity_score(data)
    rows = [{"bucket": day, "dimension": "activity", "value": value} for day, value in sorted(scores.items())]
    return {
        "rows": rows,
        "value_label": "your activity score (the dashboard's activity keys)",
        "totals": {"active_days": len(rows)},
    }


async def query_ai_adoption(db: AsyncSession, scope: InsightScope, args: dict[str, Any]) -> dict[str, Any]:
    view = str(args.get("view") or "overview").strip().lower()
    if view not in VIEWS:
        raise InsightRefusedError(f"Error: view must be one of {', '.join(VIEWS)}.")
    try:
        days = max(1, min(int(args.get("days") or 30), 365))
    except (TypeError, ValueError):
        raise InsightRefusedError("Error: days must be a whole number of days (1-365).") from None
    groups = await resolve_groups(db, args.get("group_id"), scope)

    result: dict[str, Any] = {**scope.header(), "view": view, "days": days}
    if scope.platform:
        group_ids = ",".join(group.id for group in groups) or None
        result.update(await _admin_view(db, scope, view, days, group_ids))
        result["groups"] = [group.name for group in groups]
        if groups:
            result["caveats"] = ["A group filter reads the group's members TODAY."]
    else:
        if view != "overview":
            result["note"] = f"The {view} view is an administrator view; your own activity is served instead."
        result.update(await _own_view(db, scope, days))
    if isinstance(result.get("rows"), list) and len(result["rows"]) > MAX_ROWS:
        result["rows"] = result["rows"][:MAX_ROWS]
        result["truncated"] = True
    return result
