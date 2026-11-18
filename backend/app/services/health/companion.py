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

"""The Concierge companion (#8261): a user's opt-in and pet, and what the pet shows.

Off by default: a user turns it on in their profile and picks a pet of the
library (``pets.py``); a stored value that does not validate reads as off.

:func:`companion_activity` is what the pet animates, read only from what the
user may already see, without any model call:

* ``failed`` - a cycle in their scope, or one of their own runs, failed in the
  last :data:`FAILURE_WINDOW`;
* ``waiting`` - one of their own runs waits for their approval;
* ``review`` - a proposal waits for a decision they may take (level 3's rule,
  ``improvement_proposals.approval_error``);
* ``running`` - a cycle in their scope is open, or one of their own runs runs;
* ``idle`` otherwise.

Their scope is every cycle for an administrator, the cycles they asked for
otherwise ("Improve with ..."). The web app adds the chat turn on screen,
which never reaches the server as an activity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

#: How long a failure keeps the pet on its ``failed`` row.
FAILURE_WINDOW: Final = timedelta(minutes=30)
#: The most pending proposals one activity read checks the decision rule on.
REVIEW_SCAN: Final = 500

CompanionState = Literal["idle", "running", "waiting", "failed", "review"]


class CompanionPreference(BaseModel):
    """``users.concierge_companion``: shown or not, and which pet (``None``: the default)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    pet: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")


def read_preference(user: User) -> CompanionPreference:
    stored = user.concierge_companion
    if not isinstance(stored, dict):
        return CompanionPreference()
    try:
        return CompanionPreference.model_validate(stored)
    except ValidationError:
        return CompanionPreference()


@dataclass(frozen=True)
class CompanionActivity:
    state: CompanionState
    running: int
    waiting: int
    review: int
    failed_at: datetime | None


#: How long a user's review count is reused: the pet polls every 30 s, the rule reads every target.
REVIEW_TTL: Final = timedelta(seconds=60)
_REVIEW_CACHE_MAX: Final = 10_000
_review_cache: dict[tuple[str, bool], tuple[datetime, int]] = {}


async def _review_count(db: AsyncSession, user: User) -> int:
    """The pending proposals *user* may decide, recomputed at most every :data:`REVIEW_TTL` per user."""
    key = (str(user.id), bool(user.is_admin))
    now = datetime.now(UTC)
    cached = _review_cache.get(key)
    if cached is not None and now - cached[0] < REVIEW_TTL:
        return cached[1]
    count = await _count_reviews(db, user)
    if len(_review_cache) >= _REVIEW_CACHE_MAX:
        _review_cache.clear()
    _review_cache[key] = (now, count)
    return count


async def _count_reviews(db: AsyncSession, user: User) -> int:
    from app.api.v1.helpers import get_user_group_ids
    from app.models.improvement_proposal import ImprovementProposal
    from app.services import improvement_proposals as proposals
    from app.services.health.cycle_access import readable_proposals

    rows = list(
        (
            await db.execute(
                select(ImprovementProposal)
                .where(proposals.surface_filter(), ImprovementProposal.status == "pending_approval")
                .order_by(ImprovementProposal.created_at.desc())
                .limit(REVIEW_SCAN)
            )
        ).scalars()
    )
    if not rows:
        return 0
    group_ids = await get_user_group_ids(db, user.id)
    readable = await readable_proposals(db, user, group_ids, rows)
    return sum(
        1 for _, target, handler in readable if proposals.approval_error(handler, target, user, group_ids) is None
    )


async def companion_activity(db: AsyncSession, user: User) -> CompanionActivity:
    from app.models.assignment import AssignmentRun
    from app.models.health import HealthCycle
    from app.models.health_cycle import HealthCycleRun

    since = datetime.now(UTC) - FAILURE_WINDOW
    cycles = select(HealthCycle.id).outerjoin(HealthCycleRun, HealthCycleRun.cycle_id == HealthCycle.id)
    if not user.is_admin:
        cycles = cycles.where(HealthCycleRun.requested_by_user_id == user.id)
    open_cycles = int(
        await db.scalar(select(func.count()).select_from(cycles.where(HealthCycle.status == "open").subquery())) or 0
    )
    failed_cycle_at = await db.scalar(
        select(func.max(HealthCycle.closed_at)).where(
            HealthCycle.id.in_(cycles.where(HealthCycle.status == "failed", HealthCycle.closed_at >= since)),
        )
    )
    own = AssignmentRun.user_id == user.id
    by_status = dict(
        (
            await db.execute(
                select(AssignmentRun.status, func.count())
                .where(own, AssignmentRun.status.in_(("running", "awaiting_human")))
                .group_by(AssignmentRun.status)
            )
        ).all()
    )
    # A failure path may leave completed_at unset: the run's start, else its creation, stands in.
    ended = func.coalesce(AssignmentRun.completed_at, AssignmentRun.started_at, AssignmentRun.created_at)
    failed_run_at = await db.scalar(
        select(func.max(ended)).where(own, AssignmentRun.status == "failed", ended >= since)
    )
    review = await _review_count(db, user)
    running = open_cycles + int(by_status.get("running", 0))
    waiting = int(by_status.get("awaiting_human", 0))
    failed_at = max((at for at in (failed_cycle_at, failed_run_at) if at is not None), default=None)
    state: CompanionState = (
        "failed"
        if failed_at is not None
        else "waiting"
        if waiting
        else "review"
        if review
        else "running"
        if running
        else "idle"
    )
    return CompanionActivity(state=state, running=running, waiting=waiting, review=review, failed_at=failed_at)
