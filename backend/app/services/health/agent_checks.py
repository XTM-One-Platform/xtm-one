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

Eleven deterministic checks on the **enrolled** agents alone
(:func:`app.services.health.engine.enrolled_agent_ids`), registered on the
engine when this module is imported. Each answers one result per subject it
read, failing or passing, so a finding resolves by itself once its cause is
gone (two passing reads in a row). A subject that left the read (an assignment
paused or deleted, an agent unenrolled or deleted) answers a passing result for
its open finding (:func:`_close_out`), so applying the recommended remedy
resolves the finding too.

=============================  ==========  ===========  ===============================================
Check                          Severity    Category     Rule (shared with the fleet findings)
=============================  ==========  ===========  ===============================================
``agents.assignment_failing``  warning     availability last 3 judged runs of an enabled assignment failed
``agents.schedule_silent``     info        configuration enabled schedule silent for 14 days
``agents.cost_spike``          warning     cost         a day's tokens over 3x the agent's daily average
``agents.owner_disabled``      warning     configuration the agent's owner account is deactivated
``agents.failure_spike``       warning     availability over 5 % of an assignment's last 20 settled runs failed
``agents.feedback_drop``       warning     performance  weighted score 10 points under the window before
``agents.cost_drift``          info        cost         cost per run up 20 % on the window before
``agents.model_change``        info        configuration last day's runs used a model the 7 days before did not
``agents.integration_broken``  warning     availability a bound integration in error, to re-authorize, or failing its test
``agents.orphaned_claims``     warning     availability an owned work item held by a run that ended
``agents.blocked_without_blocker`` info    configuration an owned blocked work item names nothing it waits for
=============================  ==========  ===========  ===============================================

The rules are the pure functions of :mod:`app.services.fleet_health_signals`
and :mod:`app.services.health.signals` (one rule, two readers); the last three
read their thresholds from the Platform Health signal settings
(``HealthSignalSettings``), so a threshold edited there applies to the check on
its next pass. A check reads counts, statuses and ids, never content,
calls no model and is never metered. With AI switched off (``ai_disabled``)
agents do not run, so these checks record nothing: an open finding is kept as
it was, neither refreshed nor resolved.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.docs_links import docs_url
from app.services.fleet_health_signals import (
    FAILING_RUNS,
    SILENT_AFTER,
    SPIKE_FACTOR,
    SPIKE_MIN_TOKENS,
    assignment_is_failing,
    blocked_without_blocker,
    is_cost_spike,
    schedule_is_silent,
)
from app.services.health.engine import Check, CheckResult, close_out, enrolled_agent_ids, register_check
from app.services.health.settings import HealthSignalSettings
from app.services.health.signals import FAILURE_LOOKBACK, cost_crossed, failure_crossed, feedback_crossed

MODULE = "agents"
#: How often the agent checks run (section 3, "Scheduling").
AGENT_CHECK_INTERVAL = timedelta(minutes=15)
#: Where a finding of these checks is explained.
DOC_LINK = docs_url("administration/health", "agent-checks")


AgentRead = Callable[[AsyncSession, datetime, list[uuid.UUID]], Awaitable[list[CheckResult]]]


async def _close_out(db: AsyncSession, check_id: str, read: Sequence[CheckResult]) -> list[CheckResult]:
    """A passing result for each open finding of ``check_id`` whose subject this pass did not read.

    The subject left the check's scope (paused, deleted, unenrolled): its cause
    is gone for this check, so the engine resolves it after two such reads
    (:func:`app.services.health.engine.close_out`).
    """
    return await close_out(db, check_id, read)


def _agent_check(check_id: str, read: AgentRead) -> Callable[[AsyncSession, datetime], Awaitable[list[CheckResult]]]:
    """Run ``read`` on the enrolled agents, then close out the subjects it no longer reads.

    With AI switched off agents do not run: nothing is read and nothing is
    closed out, so an open finding is kept as it was.
    """

    async def run(db: AsyncSession, now: datetime) -> list[CheckResult]:
        from app.services.ai_state import is_ai_enabled

        if not await is_ai_enabled(db):
            return []
        agents = sorted(await enrolled_agent_ids(db))
        results = await read(db, now, agents) if agents else []
        return results + await _close_out(db, check_id, results)

    run.__name__ = read.__name__
    run.__doc__ = read.__doc__
    return run


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


async def assignment_failing(db: AsyncSession, now: datetime, agents: list[uuid.UUID]) -> list[CheckResult]:
    """An enabled assignment of an enrolled agent whose last :data:`FAILING_RUNS` judged runs failed."""
    from app.models.assignment import Assignment, AssignmentRun

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


async def schedule_silent(db: AsyncSession, now: datetime, agents: list[uuid.UUID]) -> list[CheckResult]:
    """An enabled schedule of an enrolled agent that has not fired for :data:`SILENT_AFTER`."""
    from app.models.assignment import Assignment

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


async def cost_spike(db: AsyncSession, now: datetime, agents: list[uuid.UUID]) -> list[CheckResult]:
    """An enrolled agent whose tokens of the last day are over :data:`SPIKE_FACTOR` times its daily average."""
    from app.models.assignment import AssignmentRun

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
    results: list[CheckResult] = []
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


async def owner_disabled(db: AsyncSession, now: datetime, agents: list[uuid.UUID]) -> list[CheckResult]:
    """An enrolled agent whose owner account is deactivated."""
    from app.models.agent import Agent
    from app.models.user import User

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


async def _signal_settings(db: AsyncSession) -> HealthSignalSettings:
    from app.services.health.settings import load_health_settings

    return (await load_health_settings(db)).signals


async def failure_spike(db: AsyncSession, now: datetime, agents: list[uuid.UUID]) -> list[CheckResult]:
    """An enabled assignment of an enrolled agent whose failure rate over its last settled runs crosses the threshold."""
    from app.models.assignment import Assignment, AssignmentRun
    from app.services.health.signals import last_settled_statuses

    s = await _signal_settings(db)
    if not s.failure_enabled:
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
    statuses = await last_settled_statuses(
        db,
        AssignmentRun,
        AssignmentRun.assignment_id,
        [aid for aid, _ in assignments],
        s.failure_last_runs,
        created_after=now - FAILURE_LOOKBACK,
    )
    results: list[CheckResult] = []
    for aid, agent_id in assignments:
        last = statuses.get(aid, [])
        crossed, rate = failure_crossed(last, s)
        results.append(
            _result(
                "agents.failure_spike",
                "warning",
                "availability",
                crossed,
                {"agent_id": str(agent_id), "assignment_id": str(aid)},
                evidence={
                    "values": {
                        "failure_rate_pct": None if rate is None else round(rate, 1),
                        "runs": len(last),
                        "failed": sum(1 for status in last if status == "failed"),
                    },
                    "threshold": f"over {s.failure_rate_pct:g} % of the last {s.failure_last_runs} settled runs",
                },
                recommendation="Read the failed runs of the assignment for a common cause; fix it or pause the assignment.",
            )
        )
    return results


async def feedback_drop(db: AsyncSession, now: datetime, agents: list[uuid.UUID]) -> list[CheckResult]:
    """An enrolled agent whose weighted feedback score dropped on the window before."""
    from app.services.annotations import agents_weighted_score_window

    s = await _signal_settings(db)
    if not s.feedback_enabled:
        return []
    window = timedelta(days=s.feedback_window_days)
    since = now - window
    current = await agents_weighted_score_window(db, agents, since=since, until=now)
    previous = await agents_weighted_score_window(db, agents, since=since - window, until=since)
    results: list[CheckResult] = []
    for agent_id in agents:
        cur, prev = current.get(agent_id), previous.get(agent_id)
        cur_score = cur.score if cur else None
        prev_score = prev.score if prev else None
        ratings = cur.ratings if cur else 0
        results.append(
            _result(
                "agents.feedback_drop",
                "warning",
                "performance",
                feedback_crossed(cur_score, prev_score, ratings, s),
                {"agent_id": str(agent_id)},
                evidence={
                    "values": {
                        "score": None if cur_score is None else round(cur_score * 100, 1),
                        "previous_score": None if prev_score is None else round(prev_score * 100, 1),
                        "ratings": ratings,
                    },
                    "threshold": (
                        f"{s.feedback_drop_points:g} points over {s.feedback_window_days} days, "
                        f"{s.feedback_min_ratings} ratings minimum"
                    ),
                },
                recommendation="Read the low-rated answers of the window; adjust the persona, skill or tools they share.",
            )
        )
    return results


async def cost_drift(db: AsyncSession, now: datetime, agents: list[uuid.UUID]) -> list[CheckResult]:
    """An enrolled agent whose cost per assignment run rose on the window before (daily rollup, whole days)."""
    from app.models.llm_trace import LLMDailyStats

    s = await _signal_settings(db)
    if not s.cost_enabled:
        return []
    today = now.date()
    window = timedelta(days=s.cost_window_days)
    current_from, previous_from = today - window, today - 2 * window
    period = LLMDailyStats.date >= current_from
    sums: dict[uuid.UUID, dict[bool, tuple[float, int]]] = {}
    for agent_id, is_current, cost, runs in (
        await db.execute(
            select(
                LLMDailyStats.agent_id,
                period.label("current"),
                func.coalesce(func.sum(LLMDailyStats.total_cost), 0),
                func.coalesce(func.sum(LLMDailyStats.trace_count), 0),
            )
            .where(
                LLMDailyStats.source == "assignment",
                LLMDailyStats.agent_id.in_(agents),
                LLMDailyStats.date >= previous_from,
                LLMDailyStats.date < today,
            )
            .group_by(LLMDailyStats.agent_id, period)
        )
    ).all():
        sums.setdefault(agent_id, {})[bool(is_current)] = (float(cost), int(runs))
    results: list[CheckResult] = []
    for agent_id in agents:
        by_period = sums.get(agent_id, {})
        cur_cost, cur_runs = by_period.get(True, (0.0, 0))
        prev_cost, prev_runs = by_period.get(False, (0.0, 0))
        crossed, cur, prev = cost_crossed(cur_cost, cur_runs, prev_cost, prev_runs, s)
        results.append(
            _result(
                "agents.cost_drift",
                "info",
                "cost",
                crossed,
                {"agent_id": str(agent_id)},
                evidence={
                    "values": {
                        "cost_per_run": None if cur is None else round(cur, 6),
                        "previous_cost_per_run": None if prev is None else round(prev, 6),
                        "runs": cur_runs,
                        "previous_runs": prev_runs,
                    },
                    "threshold": (
                        f"+{s.cost_increase_pct:g} % over {s.cost_window_days} days, {s.cost_min_runs} runs minimum"
                    ),
                },
                recommendation="Compare a recent run with an older one for a larger read, a longer loop or a model change.",
            )
        )
    return results


#: The integration states that stop its tools (``token_refresh``: an error
#: after a failed refresh, ``reauthorize`` after a dead grant).
BROKEN_INTEGRATION_STATUSES = frozenset({"error", "reauthorize"})
#: The day a model change is read on, and the days before it is compared with.
MODEL_CHANGE_DAYS = 7


async def model_change(db: AsyncSession, now: datetime, agents: list[uuid.UUID]) -> list[CheckResult]:
    """An enrolled agent whose assignment runs of the last whole day used a model none of the days before used.

    Read on the daily rollup (``llm_daily_stats``), whole days: the finding
    stays open the day after the change, then resolves once the new model is
    part of the window before. An agent with no earlier runs records nothing.
    """
    from app.models.llm_trace import LLMDailyStats

    yesterday = now.date() - timedelta(days=1)
    first = yesterday - timedelta(days=MODEL_CHANGE_DAYS)
    used: dict[uuid.UUID, dict[bool, set[str]]] = {}
    for agent_id, day, model in (
        await db.execute(
            select(LLMDailyStats.agent_id, LLMDailyStats.date, LLMDailyStats.model)
            .where(
                LLMDailyStats.source == "assignment",
                LLMDailyStats.agent_id.in_(agents),
                LLMDailyStats.date >= first,
                LLMDailyStats.date <= yesterday,
                LLMDailyStats.trace_count > 0,
            )
            .distinct()
        )
    ).all():
        used.setdefault(agent_id, {}).setdefault(day == yesterday, set()).add(model)
    results: list[CheckResult] = []
    for agent_id in agents:
        by_day = used.get(agent_id, {})
        latest, before = by_day.get(True, set()), by_day.get(False, set())
        new = sorted(latest - before) if before else []
        results.append(
            _result(
                "agents.model_change",
                "info",
                "configuration",
                bool(new),
                {"agent_id": str(agent_id)},
                evidence={
                    "values": {"new_models": new, "previous_models": sorted(before), "day": yesterday.isoformat()},
                    "threshold": f"a model not used in the {MODEL_CHANGE_DAYS} days before",
                },
                recommendation="Compare the agent's runs before and after the change; pin the model if the change was not intended.",
            )
        )
    return results


async def integration_broken(db: AsyncSession, now: datetime, agents: list[uuid.UUID]) -> list[CheckResult]:
    """An integration bound to an enrolled agent in a state that stops its tools, or whose last connectivity test failed."""
    from app.core.utils import safe_uuid
    from app.models.agent import Agent
    from app.models.integration import Integration

    bindings: list[tuple[uuid.UUID, uuid.UUID]] = []
    for agent_id, bound in (
        await db.execute(select(Agent.id, Agent.integration_bindings).where(Agent.id.in_(agents)))
    ).all():
        for ids in (bound or {}).values():
            for raw in ids if isinstance(ids, list) else [ids]:
                integration_id = safe_uuid(str(raw))
                if integration_id is not None:
                    bindings.append((agent_id, integration_id))
    if not bindings:
        return []
    states = {
        iid: (provider, status, tested)
        for iid, provider, status, tested in (
            await db.execute(
                select(
                    Integration.id, Integration.provider, Integration.status, Integration.connection_test_status
                ).where(Integration.id.in_({iid for _, iid in bindings}))
            )
        ).all()
    }
    results: list[CheckResult] = []
    for agent_id, integration_id in sorted(set(bindings), key=str):
        if integration_id not in states:
            # A binding to a deleted integration is pruned by the agent's own
            # save path; nothing to check.
            continue
        provider, status, tested = states[integration_id]
        results.append(
            _result(
                "agents.integration_broken",
                "warning",
                "availability",
                status in BROKEN_INTEGRATION_STATUSES or tested == "error",
                {"agent_id": str(agent_id), "integration_id": str(integration_id)},
                evidence={"values": {"provider": provider, "status": status, "connection_test": tested}},
                recommendation="Open the integration, test it, and reconnect or re-authorize it.",
            )
        )
    return results


async def _owned_open_items(db: AsyncSession, agents: list[uuid.UUID]) -> dict[uuid.UUID, list[Any]]:
    from app.models.work_item import WORK_ITEM_OPEN_STATUSES, WorkItem

    rows = (
        await db.execute(
            select(
                WorkItem.id,
                WorkItem.owner_agent_id,
                WorkItem.status,
                WorkItem.claimed_by_run_id,
                WorkItem.blocked_on,
                WorkItem.blocked_on_refs,
                WorkItem.blocked_reason,
                WorkItem.needs_human,
                WorkItem.ask,
                WorkItem.payload,
                WorkItem.action_mode,
            ).where(WorkItem.owner_agent_id.in_(agents), WorkItem.status.in_(WORK_ITEM_OPEN_STATUSES))
        )
    ).all()
    owned: dict[uuid.UUID, list[Any]] = {}
    for row in rows:
        owned.setdefault(row.owner_agent_id, []).append(row)
    return owned


async def orphaned_claims(db: AsyncSession, now: datetime, agents: list[uuid.UUID]) -> list[CheckResult]:
    """Work items owned by an enrolled agent, held by a run that ended (the platform's claim sweep releases them)."""
    from sqlalchemy import not_

    from app.models.work_item import WorkItem
    from app.services.work_item import CLAIM_HELD_STATUSES, _holder_alive_clause

    orphaned: dict[uuid.UUID, list[str]] = {}
    for item_id, owner in (
        await db.execute(
            select(WorkItem.id, WorkItem.owner_agent_id).where(
                WorkItem.owner_agent_id.in_(agents),
                WorkItem.status.in_(CLAIM_HELD_STATUSES),
                WorkItem.claimed_by_run_id.isnot(None),
                not_(_holder_alive_clause(WorkItem.claimed_by_run_id)),  # type: ignore[no-untyped-call]
            )
        )
    ).all():
        orphaned.setdefault(owner, []).append(str(item_id))
    return [
        _result(
            "agents.orphaned_claims",
            "warning",
            "availability",
            bool(orphaned.get(agent_id)),
            {"agent_id": str(agent_id)},
            evidence={
                "values": {"items": sorted(orphaned.get(agent_id, []))[:10], "count": len(orphaned.get(agent_id, []))}
            },
            recommendation="Released by the platform's claim sweep; if it persists, read the item's last run.",
        )
        for agent_id in agents
    ]


async def blocked_without_blocker_items(db: AsyncSession, now: datetime, agents: list[uuid.UUID]) -> list[CheckResult]:
    """Blocked work items owned by an enrolled agent that name nothing they wait for."""
    owned = await _owned_open_items(db, agents)
    results: list[CheckResult] = []
    for agent_id in agents:
        bare = sorted(
            str(row.id)
            for row in owned.get(agent_id, [])
            if row.status == "blocked"
            # A scheduled one-off has its own reschedule and cancel actions (#862).
            and getattr(row, "action_mode", None) is None
            and blocked_without_blocker(
                blocked_on=row.blocked_on,
                blocked_on_refs=row.blocked_on_refs,
                blocked_reason=row.blocked_reason,
                needs_human=row.needs_human,
                owner_agent_id=row.owner_agent_id,
                ask=row.ask,
                payload=row.payload,
            )
        )
        results.append(
            _result(
                "agents.blocked_without_blocker",
                "info",
                "configuration",
                bool(bare),
                {"agent_id": str(agent_id)},
                evidence={"values": {"items": bare[:10], "count": len(bare)}},
                recommendation=(
                    "Give each item a blocker the platform releases (blocked_on, blocked_on_refs) or park it for a "
                    "person with needs_human and an ask card; else reopen it."
                ),
            )
        )
    return results


for _check_id, _run in (
    ("agents.assignment_failing", assignment_failing),
    ("agents.schedule_silent", schedule_silent),
    ("agents.cost_spike", cost_spike),
    ("agents.owner_disabled", owner_disabled),
    ("agents.failure_spike", failure_spike),
    ("agents.feedback_drop", feedback_drop),
    ("agents.cost_drift", cost_drift),
    ("agents.model_change", model_change),
    ("agents.integration_broken", integration_broken),
    ("agents.orphaned_claims", orphaned_claims),
    ("agents.blocked_without_blocker", blocked_without_blocker_items),
):
    register_check(
        Check(check_id=_check_id, module=MODULE, interval=AGENT_CHECK_INTERVAL, run=_agent_check(_check_id, _run))
    )
