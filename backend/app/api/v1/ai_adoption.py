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

"""AI Adoption endpoints: agentic AI usage on the platform.

Admin-only endpoints that serve the AI Adoption dashboard (Overview and
Adoption Gaps) from the platform's own daily metrics. The development tools
(GitHub Copilot, Cursor) are no longer tracked (#11380): every query reads the
platform provider alone.
"""

import contextlib
import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.api.v1.helpers import parse_uuid
from app.core.database import get_db
from app.models.ai_adoption_metric import AiAdoptionMetric
from app.models.group import GroupMember
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter()

#: The provider id the platform's own daily metrics are stored under.
PLATFORM_PROVIDER = "copilot_platform"

#: The ``data`` keys whose values add up to a user's activity score: what ranks
#: the top users and tells inactive from low-activity ones.
ACTIVITY_KEYS: tuple[str, ...] = ("conversations", "messages", "assignment_runs")

#: Below this activity score a user is low-activity.
ACTIVITY_THRESHOLD = 10


def activity_score(data: object) -> int:
    """A metric row's contribution to its user's activity score (``ACTIVITY_KEYS``)."""
    if not isinstance(data, dict):
        return 0
    score = 0
    for key in ACTIVITY_KEYS:
        value = data.get(key)
        if value is not None:
            with contextlib.suppress(ValueError, TypeError):
                score += int(value)
    return score


async def _group_user_ids(db: AsyncSession, group_id: str | None) -> set | None:
    """The members of the groups ``group_id`` names (comma-separated), or None."""
    if not group_id:
        return None
    gids = [parse_uuid(g.strip(), label="group ID") for g in group_id.split(",") if g.strip()]
    if not gids:
        return None
    rows = await db.execute(select(GroupMember.user_id).where(GroupMember.group_id.in_(gids)))
    return {row[0] for row in rows}


def _platform_metrics(*columns, user_ids: set | None = None):
    """A select over the platform's metric rows of identified users, ``user_ids`` alone when given."""
    statement = select(*columns).where(
        AiAdoptionMetric.provider == PLATFORM_PROVIDER,
        AiAdoptionMetric.user_id.isnot(None),
    )
    if user_ids is not None:
        statement = statement.where(AiAdoptionMetric.user_id.in_(user_ids))
    return statement


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


@router.get("/overview")
async def get_ai_adoption_overview(
    days: int = Query(default=30, ge=1, le=9999),
    group_id: str | None = Query(default=None),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Active users, their previous-period count and the top users of the period."""
    start = date.today() - timedelta(days=days)
    prev_start = start - timedelta(days=days)
    group_user_ids = await _group_user_ids(db, group_id)

    total_users = (
        await db.execute(select(func.count(User.id)).where(User.is_active.is_(True)))
    ).scalar() or 0
    total_metrics = (
        await db.execute(
            _platform_metrics(func.count(AiAdoptionMetric.id), user_ids=group_user_ids).where(
                AiAdoptionMetric.date >= start
            )
        )
    ).scalar() or 0
    active_user_count = (
        await db.execute(
            _platform_metrics(func.count(func.distinct(AiAdoptionMetric.user_id)), user_ids=group_user_ids).where(
                AiAdoptionMetric.date >= start
            )
        )
    ).scalar() or 0
    prev_active_user_count = (
        await db.execute(
            _platform_metrics(func.count(func.distinct(AiAdoptionMetric.user_id)), user_ids=group_user_ids).where(
                AiAdoptionMetric.date >= prev_start, AiAdoptionMetric.date < start
            )
        )
    ).scalar() or 0

    scores: dict = {}
    rows = await db.execute(
        _platform_metrics(AiAdoptionMetric.user_id, AiAdoptionMetric.data, user_ids=group_user_ids).where(
            AiAdoptionMetric.date >= start
        )
    )
    for uid, data in rows:
        scores[uid] = scores.get(uid, 0) + activity_score(data)
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:50]

    names: dict = {}
    if ranked:
        name_rows = await db.execute(
            select(User.id, User.email, User.full_name).where(User.id.in_([uid for uid, _ in ranked]))
        )
        names = {uid: {"email": email, "name": name} for uid, email, name in name_rows}

    top_users = []
    for uid, score in ranked:
        info = names.get(uid, {})
        top_users.append(
            {
                "user_id": str(uid),
                "name": info.get("name") or info.get("email") or str(uid),
                "email": info.get("email"),
                "activity_score": score,
            }
        )

    return {
        "period_days": days,
        "total_tracked_users": total_users,
        "total_metrics": total_metrics,
        "active_user_count": active_user_count,
        "prev_active_user_count": prev_active_user_count,
        "top_users": top_users,
    }


# ---------------------------------------------------------------------------
# Adoption gaps: who is NOT adopting
# ---------------------------------------------------------------------------


def _months(today: date) -> list[str]:
    """The last six calendar months, oldest first, as ``YYYY-MM``."""
    months: set[str] = set()
    for i in range(6):
        months.add((today.replace(day=1) - timedelta(days=i * 30)).strftime("%Y-%m"))
    return sorted(months)


@router.get("/gaps")
async def get_adoption_gaps(
    days: int = Query(default=30, ge=1, le=9999),
    group_id: str | None = Query(default=None),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """The users not adopting agentic AI: inactive, low-activity, and the trend."""
    today = date.today()
    start = today - timedelta(days=days)
    prev_start = start - timedelta(days=days)
    group_user_ids = await _group_user_ids(db, group_id)

    def scoped(statement):
        if group_user_ids is not None:
            return statement.where(AiAdoptionMetric.user_id.in_(group_user_ids))
        return statement

    user_rows = await db.execute(select(User.id, User.email, User.full_name).where(User.is_active.is_(True)))
    all_users = {row.id: {"email": row.email, "name": row.full_name or row.email} for row in user_rows}
    if group_user_ids is not None:
        all_users = {uid: info for uid, info in all_users.items() if uid in group_user_ids}
    total_users = len(all_users)

    async def _scores(since: date, until: date | None = None) -> dict:
        statement = _platform_metrics(AiAdoptionMetric.user_id, AiAdoptionMetric.data).where(
            AiAdoptionMetric.date >= since
        )
        if until is not None:
            statement = statement.where(AiAdoptionMetric.date < until)
        out: dict = {}
        for uid, data in await db.execute(scoped(statement)):
            out[uid] = out.get(uid, 0) + activity_score(data)
        return out

    async def _avg_days(since: date, until: date | None = None) -> float:
        statement = (
            _platform_metrics(func.count(func.distinct(AiAdoptionMetric.date)))
            .where(AiAdoptionMetric.date >= since)
            .group_by(AiAdoptionMetric.user_id)
        )
        if until is not None:
            statement = statement.where(AiAdoptionMetric.date < until)
        values = [row[0] for row in await db.execute(scoped(statement))]
        return round(sum(values) / len(values), 1) if values else 0

    scores = await _scores(start)
    inactive: list[dict] = []
    low_activity: list[dict] = []
    for uid, info in all_users.items():
        score = scores.get(uid, 0)
        entry = {"user_id": str(uid), "name": info["name"], "email": info["email"], "score": score}
        if score == 0:
            inactive.append(entry)
        elif score < ACTIVITY_THRESHOLD:
            low_activity.append(entry)
    low_activity.sort(key=lambda entry: entry["score"])

    prev_scores = await _scores(prev_start, start)
    prev_inactive = sum(1 for uid in all_users if prev_scores.get(uid, 0) == 0)
    prev_low = sum(1 for uid in all_users if 0 < prev_scores.get(uid, 0) < ACTIVITY_THRESHOLD)

    trend_start = today - timedelta(days=180)
    month = func.to_char(AiAdoptionMetric.date, "YYYY-MM").label("month")
    months = _months(today)

    active_rows = await db.execute(
        scoped(
            _platform_metrics(month, func.count(func.distinct(AiAdoptionMetric.user_id)))
            .where(AiAdoptionMetric.date >= trend_start)
            .group_by(literal_column("month"))
        )
    )
    active_by_month = dict(active_rows.all())
    trend = [
        {
            "date": f"{m}-01",
            "active_users": active_by_month.get(m, 0),
            "inactive_users": max(0, total_users - active_by_month.get(m, 0)),
            "total_users": total_users,
            "adoption_rate": round(active_by_month.get(m, 0) / total_users * 100, 1) if total_users else 0,
        }
        for m in months
    ]

    volume_rows = await db.execute(
        scoped(
            _platform_metrics(month, func.count(AiAdoptionMetric.id))
            .where(AiAdoptionMetric.date >= trend_start)
            .group_by(literal_column("month"))
        )
    )
    volume_by_month = dict(volume_rows.all())
    volume = [{"date": f"{m}-01", "value": volume_by_month.get(m, 0)} for m in months]

    return {
        "period_days": days,
        "total_users": total_users,
        "summary": {
            "inactive_count": len(inactive),
            "low_activity_count": len(low_activity),
            "active_count": total_users - len(inactive) - len(low_activity),
            "avg_active_days": await _avg_days(start),
        },
        "prev_summary": {
            "inactive_count": prev_inactive,
            "low_activity_count": prev_low,
            "active_count": total_users - prev_inactive - prev_low,
            "avg_active_days": await _avg_days(prev_start, start),
        },
        "inactive": inactive,
        "low_activity": low_activity,
        "trend": trend,
        "volume": volume,
        "activity_threshold": ACTIVITY_THRESHOLD,
    }
