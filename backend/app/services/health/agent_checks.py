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

"""Platform Health agent checks (#101 S3, ``dev-docs/platform-health.md`` section 4.1).

Four deterministic checks on the **enrolled** agents alone
(:func:`app.services.health.engine.enrolled_agent_ids`), registered on the
engine when this module is imported. Each answers one result per subject it
read, failing or passing, so a finding resolves by itself once its cause is
gone (two passing reads in a row).

=============================  ==========  ===========  ===============================================
Check                          Severity    Category     Rule (shared with the fleet findings)
=============================  ==========  ===========  ===============================================
``agents.assignment_failing``  warning     availability last 3 judged runs of an enabled assignment failed
``agents.schedule_silent``     info        configuration enabled schedule silent for 14 days
``agents.cost_spike``          warning     cost         a day's tokens over 3x the agent's daily average
``agents.owner_disabled``      warning     configuration the agent's owner account is deactivated
=============================  ==========  ===========  ===============================================

The rules are the pure functions of :mod:`app.services.fleet_health_signals`
(one rule, two readers). A check reads counts, statuses and ids, never content,
calls no model and is never metered. With AI switched off (``ai_disabled``)
agents do not run, so these checks record nothing: an open finding is kept as
it was, neither refreshed nor resolved.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.fleet_health_signals import (
    FAILING_RUNS,
    SILENT_AFTER,
    SPIKE_FACTOR,
    SPIKE_MIN_TOKENS,
    assignment_is_failing,
    is_cost_spike,
    schedule_is_silent,
)
from app.services.health.engine import Check, CheckResult, enrolled_agent_ids, register_check

MODULE = "agents"
#: How often the agent checks run (section 3, "Scheduling").
AGENT_CHECK_INTERVAL = timedelta(minutes=15)
#: Where a finding of these checks is explained.
DOC_LINK = "/docs/administration/platform-health#agent-checks"


async def _targets(db: AsyncSession) -> list[uuid.UUID]:
    """The enrolled agents, or none when AI is switched off (agents do not run then)."""
    from app.services.ai_state import is_ai_enabled

    if not await is_ai_enabled(db):
        return []
    return sorted(await enrolled_agent_ids(db))


def _result(
    check_id: str, severity: str, category: str, failing: bool, subject: dict[str, str], **kw: Any
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        module=MODULE,
        category=category,
        severity=severity,
        failing=failing,
        subject=subject,
        doc_link=DOC_LINK,
        **kw,
    )


async def assignment_failing(db: AsyncSession, now: datetime) -> Sequence[CheckResult]:
    """An enabled assignment of an enrolled agent whose last :data:`FAILING_RUNS` judged runs failed."""
    from app.models.assignment import Assignment, AssignmentRun

    agents = await _targets(db)
    if not agents:
        return []
    assignments = (
        await db.execute(
            select(Assignment.id, Assignment.agent_id).where(
                Assignment.agent_id.in_(agents), Assignment.enabled.is_(True)
            )
        )
    ).all()
    if not assignments:
        return []
    rank = (
        func.row_number()
        .over(
            partition_by=AssignmentRun.assignment_id,
            order_by=(AssignmentRun.created_at.desc(), AssignmentRun.id.desc()),
        )
        .label("rank")
    )
    ranked = (
        select(AssignmentRun.assignment_id, AssignmentRun.status, AssignmentRun.error, rank)
        .where(AssignmentRun.assignment_id.in_([aid for aid, _ in assignments]))
        .subquery()
    )
    runs: dict[uuid.UUID, list[tuple[str | None, str | None]]] = {}
    for aid, status, error in (
        await db.execute(
            select(ranked.c.assignment_id, ranked.c.status, ranked.c.error)
            .where(ranked.c.rank <= FAILING_RUNS * 3)
            .order_by(ranked.c.assignment_id, ranked.c.rank)
        )
    ).all():
        runs.setdefault(aid, []).append((status, error))
    return [
        _result(
            "agents.assignment_failing",
            "warning",
            "availability",
            assignment_is_failing(runs.get(aid, [])),
            {"agent_id": str(agent_id), "assignment_id": str(aid)},
            evidence={"values": {"judged_runs_read": len(runs.get(aid, []))}, "threshold": FAILING_RUNS},
            recommendation="Read the last failed run of the assignment; fix it or pause the assignment.",
        )
        for aid, agent_id in assignments
    ]


async def schedule_silent(db: AsyncSession, now: datetime) -> Sequence[CheckResult]:
    """An enabled schedule of an enrolled agent that has not fired for :data:`SILENT_AFTER`."""
    from app.models.assignment import Assignment

    agents = await _targets(db)
    if not agents:
        return []
    rows = (
        await db.execute(
            select(Assignment.id, Assignment.agent_id, Assignment.created_at, Assignment.last_triggered_at).where(
                Assignment.agent_id.in_(agents),
                Assignment.enabled.is_(True),
                Assignment.trigger_type == "schedule",
            )
        )
    ).all()
    return [
        _result(
            "agents.schedule_silent",
            "info",
            "configuration",
            schedule_is_silent(created_at=created, last_triggered_at=last, now=now),
            {"agent_id": str(agent_id), "assignment_id": str(aid)},
            evidence={
                "values": {"last_triggered_at": last.isoformat() if last else None},
                "threshold": f"{SILENT_AFTER.days} days",
            },
            recommendation="Check the schedule and its gate; delete the assignment if its work is over.",
        )
        for aid, agent_id, created, last in rows
    ]


async def cost_spike(db: AsyncSession, now: datetime) -> Sequence[CheckResult]:
    """An enrolled agent whose tokens of the last day are over :data:`SPIKE_FACTOR` times its daily average."""
    from app.models.assignment import AssignmentRun

    agents = await _targets(db)
    if not agents:
        return []
    day, week = now - timedelta(days=1), now - timedelta(days=8)
    tokens = func.coalesce(AssignmentRun.total_input_tokens, 0) + func.coalesce(AssignmentRun.total_output_tokens, 0)
    totals = {
        aid: (int(today or 0), int(before or 0))
        for aid, today, before in (
            await db.execute(
                select(
                    AssignmentRun.agent_id,
                    func.sum(tokens).filter(AssignmentRun.created_at >= day),
                    func.sum(tokens).filter(AssignmentRun.created_at < day),
                )
                .where(AssignmentRun.agent_id.in_(agents), AssignmentRun.created_at >= week)
                .group_by(AssignmentRun.agent_id)
            )
        ).all()
    }
    results = []
    for agent_id in agents:
        today, before = totals.get(agent_id, (0, 0))
        results.append(
            _result(
                "agents.cost_spike",
                "warning",
                "cost",
                is_cost_spike(today_tokens=today, week_tokens=before),
                {"agent_id": str(agent_id)},
                evidence={
                    "values": {"tokens_last_day": today, "tokens_previous_7_days": before},
                    "threshold": f"{SPIKE_FACTOR:g}x the daily average, {SPIKE_MIN_TOKENS:,} tokens minimum",
                },
                recommendation="Read the agent's runs of the day for a loop or an oversized read.",
            )
        )
    return results


async def owner_disabled(db: AsyncSession, now: datetime) -> Sequence[CheckResult]:
    """An enrolled agent whose owner account is deactivated."""
    from app.models.agent import Agent
    from app.models.user import User

    agents = await _targets(db)
    if not agents:
        return []
    rows = (
        await db.execute(
            select(Agent.id, User.is_active).join(User, User.id == Agent.user_id).where(Agent.id.in_(agents))
        )
    ).all()
    return [
        _result(
            "agents.owner_disabled",
            "warning",
            "configuration",
            not active,
            {"agent_id": str(agent_id)},
            evidence={"values": {"owner_active": bool(active)}},
            recommendation="Hand the agent to an active owner or disable it.",
        )
        for agent_id, active in rows
    ]


for _check_id, _run in (
    ("agents.assignment_failing", assignment_failing),
    ("agents.schedule_silent", schedule_silent),
    ("agents.cost_spike", cost_spike),
    ("agents.owner_disabled", owner_disabled),
):
    register_check(Check(check_id=_check_id, module=MODULE, interval=AGENT_CHECK_INTERVAL, run=_run))
