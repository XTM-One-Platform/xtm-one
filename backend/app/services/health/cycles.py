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

"""The Platform Health cycle runner (#8261, PRD 6.3).

Every piece of Platform Health work is a run of one of the fleet's assignments
(``fleet_seed.FLEET_ASSIGNMENTS``, invariant 1), and every such run starts
here, inside a budget cycle (level 1's ``budget.open_cycle``):

* :func:`start_cycle` - a schedule, a signal or an on-demand request. The cycle
  is opened first (licence gate, budget caps), then the run is started with
  ``trigger_context["concierge_cycle_id"]``, the key the engine reads to give
  the run's tools their cycle.
* :func:`run_due_schedules` - the fleet-wide schedules of Settings > Agents and automations > Platform Health.
* :func:`settle_cycles` - books what each cycle's runs spent (their LLM
  traces, delegated runs included), stops a cycle's runs at a cap, closes the
  cycles whose whole run tree ended and builds their digest
  (``digest.build_digest``).

**One open cycle per target** is the database's decision: a partial unique
index on ``concierge_cycles`` (migration ``c8cnc8254081``), so two signals,
or a signal and a manager's request, racing on one target open one cycle. A
fleet-wide schedule does not start while its previous cycle is still open
(:func:`run_due_schedules`; the scheduler tick runs on one worker at a time).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Literal, get_args

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.health import HealthCycle
from app.models.health_cycle import HealthCycleRun, HealthScheduleMark
from app.services.health.fleet_seed import AssignmentKey, fleet_assignment

logger = logging.getLogger(__name__)

CycleTrigger = Literal["schedule", "signal", "on_demand"]
#: ``module``: a registered platform (``PlatformRegistration.id``), the Module Analyst's target (#101 S14).
TargetType = Literal["agent", "flow", "assignment", "module"]
ScheduleKey = Literal["memory", "quality", "flows", "digest"]
SCHEDULE_KEYS: Final[tuple[str, ...]] = get_args(ScheduleKey)

#: Why no cycle started. The budget codes are level 1's ``BudgetRefusalCode``.
CycleRefusal = Literal[
    "inactive",
    "no_assignment",
    "paused",
    "not_in_scope",
    "not_found",
    "cycle_open",
    "start_failed",
    "concierge_disabled",
    "ai_disabled",
    "license_required",
    "transient",
    "cycle_budget_reached",
    "month_budget_reached",
    "cycle_closed",
]

#: What the caller of :func:`start_cycle` may be told for each refusal: generic,
#: the reason stays in the logs. ``{name}`` is Platform Health's name (``naming``).
REFUSAL_MESSAGES: Final[dict[str, str]] = {
    "inactive": "{name} is not enabled.",
    "no_assignment": "{name}'s fleet is not set up yet.",
    "paused": "{name}'s assignment for this work is paused.",
    "not_in_scope": "This target is outside {name}'s supervision scope.",
    "not_found": "Target not found.",
    "cycle_open": "{name} is already working on this target.",
    "start_failed": "{name} could not start this work.",
    "concierge_disabled": "{name} is not enabled.",
    "ai_disabled": "Agentic AI is turned off on this platform.",
    "license_required": "Agentic AI needs a licence on this platform.",
    "transient": "{name} could not check its budget. Try again shortly.",
    "cycle_budget_reached": "{name}'s budget per cycle is reached.",
    "month_budget_reached": "{name}'s monthly budget is reached.",
    "cycle_closed": "{name} could not start this work.",
}

#: Which fleet assignment each schedule starts.
_SCHEDULE_ASSIGNMENT: Final[dict[str, AssignmentKey]] = {
    "memory": "memory",
    "quality": "quality",
    "flows": "flows",
    "digest": "digest",
}

#: The trigger-context key every Platform Health run carries: the cycle it charges.
CYCLE_CONTEXT_KEY: Final = "concierge_cycle_id"

#: A cycle still open this long after it opened lost its run: closed as failed.
STALE_CYCLE_AFTER: Final = timedelta(hours=24)

#: How long after a run ended its traces may still be on their way to the store.
TRACE_SETTLE_DELAY: Final = timedelta(minutes=2)


@dataclass(frozen=True)
class CycleStart:
    cycle_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None
    refusal: str | None = None
    #: With ``cycle_open``: the cycle already working on the target.
    open_cycle_id: uuid.UUID | None = None

    @property
    def started(self) -> bool:
        return self.refusal is None

    def message(self, name: str) -> str | None:
        """The refusal told to the caller, Platform Health called *name*; ``None`` once started."""
        if not self.refusal:
            return None
        # str.replace, not str.format: the name is an administrator's text, and
        # a brace in it is a brace.
        return REFUSAL_MESSAGES.get(self.refusal, REFUSAL_MESSAGES["start_failed"]).replace("{name}", name)


async def platform_language_code(db: AsyncSession) -> str:
    """The platform language (``PlatformSettings.platform_language``, decision B22), English when unset."""
    from app.api.v1.platform_settings_helpers import get_platform_settings

    language = (getattr(await get_platform_settings(db), "platform_language", None) or "auto").strip().lower()
    return "en" if language in ("", "auto") else language[:10]


async def _target_in_scope(db: AsyncSession, target_type: str, target_id: uuid.UUID) -> bool | None:
    """Whether the target is supervised; ``None`` when it does not exist or is a sandbox copy."""
    from app.models.agent import Agent
    from app.models.assignment import Assignment
    from app.models.flow import Flow
    from app.services.health.consent import in_supervision_scope

    if target_type == "module":
        from app.services.health.module_analyst import registration_in_scope

        return await registration_in_scope(db, target_id)
    if target_type == "agent":
        if (
            await db.execute(select(Agent.id).where(Agent.id == target_id, Agent.is_sandbox.is_(False)))
        ).scalar_one_or_none() is None:
            return None
        return await in_supervision_scope(db, "agent", target_id)
    if target_type == "flow":
        if (
            await db.execute(select(Flow.id).where(Flow.id == target_id, Flow.is_sandbox.is_(False)))
        ).scalar_one_or_none() is None:
            return None
        return await in_supervision_scope(db, "flow", target_id)
    agent_id = (await db.execute(select(Assignment.agent_id).where(Assignment.id == target_id))).scalar_one_or_none()
    if agent_id is None:
        return None
    return await in_supervision_scope(db, "agent", agent_id)


async def _open_cycle_id(db: AsyncSession, *, target_type: str | None, target_id: uuid.UUID | None) -> uuid.UUID | None:
    query = select(HealthCycle.id).where(
        HealthCycle.status == "open",
        HealthCycle.target_type == target_type,
        HealthCycle.target_id == target_id,
    )
    return (await db.execute(query.limit(1))).scalar_one_or_none()


async def start_cycle(
    db: AsyncSession,
    *,
    key: AssignmentKey,
    trigger: CycleTrigger,
    target_type: TargetType | None = None,
    target_id: uuid.UUID | None = None,
    signal: dict | None = None,
    requested_by_user_id: uuid.UUID | None = None,
    payload: dict | None = None,
) -> CycleStart:
    """Open a cycle and start its run; commits. Never raises for a refusal.

    The run is started with no ``triggered_by_user_id``, whoever asked: it is
    Platform Health's work (its scope and consent), never the requester's. The
    requester is recorded on the cycle.
    """
    from app.services.assignment_engine import trigger_assignment
    from app.services.health.availability import health_active
    from app.services.health.budget import BudgetRefusedError, close_cycle, open_cycle

    if (target_type is None) != (target_id is None):
        raise ValueError("a target needs both its type and its id")
    if not await health_active(db):
        return CycleStart(refusal="inactive")
    pair = await fleet_assignment(db, key)
    if pair is None:
        return CycleStart(refusal="no_assignment")
    assignment, agent = pair
    if not assignment.enabled or not agent.enabled:
        return CycleStart(refusal="paused")
    if target_id is not None:
        in_scope = await _target_in_scope(db, target_type, target_id)
        if in_scope is None:
            return CycleStart(refusal="not_found")
        if not in_scope:
            return CycleStart(refusal="not_in_scope")

    specialist = agent.health_role
    try:
        async with db.begin_nested():
            cycle_id = await open_cycle(
                db, specialist=specialist, trigger=trigger, target_type=target_type, target_id=target_id
            )
    except BudgetRefusedError as exc:
        # The savepoint is rolled back; the caller's transaction is left as it was.
        return CycleStart(refusal=exc.code)
    except IntegrityError:
        open_id = await _open_cycle_id(db, target_type=target_type, target_id=target_id)
        return CycleStart(refusal="cycle_open", open_cycle_id=open_id)

    db.add(
        HealthCycleRun(
            cycle_id=cycle_id,
            agent_id=agent.id,
            assignment_id=assignment.id,
            target_type=target_type,
            target_id=target_id,
            signal=signal,
            requested_by_user_id=requested_by_user_id,
        )
    )
    await db.commit()

    run_payload = {
        "cycle_id": str(cycle_id),
        "language": await platform_language_code(db),
        **({"target_type": target_type, "target_id": str(target_id)} if target_id is not None else {}),
        **({"signal": signal} if signal else {}),
        **(payload or {}),
    }
    trigger_context = {
        "type": "manual",
        CYCLE_CONTEXT_KEY: str(cycle_id),
        "concierge_trigger": trigger,
        "triggered_at": datetime.now(UTC).isoformat(),
    }
    try:
        run_id = await trigger_assignment(
            assignment, agent, payload=run_payload, db=db, trigger_context=trigger_context
        )
    except Exception:
        logger.exception("Platform Health cycle %s: the run of %r could not be started", cycle_id, assignment.name)
        await db.rollback()
        await close_cycle(db, cycle_id, status="failed", reason="The run could not be started.")
        await db.commit()
        return CycleStart(cycle_id=cycle_id, refusal="start_failed")
    if run_id is None:
        await close_cycle(db, cycle_id, status="cancelled", reason="The run was not started.")
        await db.commit()
        return CycleStart(cycle_id=cycle_id, refusal="start_failed")
    record = await db.get(HealthCycleRun, cycle_id)
    if record is not None:
        record.run_id = uuid.UUID(str(run_id))
    await db.commit()
    return CycleStart(cycle_id=cycle_id, run_id=uuid.UUID(str(run_id)))


async def inherited_cycle_context(db: AsyncSession, parent_run_id: str | uuid.UUID | None) -> dict | None:
    """The trigger context a run delegated from *parent_run_id* takes: its parent's cycle, if any.

    A specialist the orchestrator delegates to runs as a child run; giving it
    the parent's ``concierge_cycle_id`` lets every level read its cycle from
    the run it has in hand, at any depth.
    """
    from app.models.assignment import AssignmentRun

    if not parent_run_id:
        return None
    try:
        parent_id = uuid.UUID(str(parent_run_id))
    except ValueError:
        return None
    context = (
        await db.execute(select(AssignmentRun.trigger_context).where(AssignmentRun.id == parent_id))
    ).scalar_one_or_none()
    cycle_id = (context or {}).get(CYCLE_CONTEXT_KEY) if isinstance(context, dict) else None
    return {"type": "delegation", CYCLE_CONTEXT_KEY: str(cycle_id)} if cycle_id else None


def _schedule_cron(settings, key: str) -> tuple[bool, str]:
    schedules = settings.schedules
    return bool(getattr(schedules, f"{key}_enabled")), getattr(schedules, key)


async def run_due_schedules(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Start the fleet-wide cycles whose schedule is due; the number started. Commits.

    A schedule's first sighting only records it: it first fires at its next
    occurrence, never at boot. A due schedule refused (budget, licence, a
    cycle still open) is marked fired with its refusal, so it is retried at
    its next occurrence, not on every tick.
    """
    from zoneinfo import ZoneInfo

    from app.core.utils import next_cron_run
    from app.services.health.settings import load_health_settings

    now = now or datetime.now(UTC)
    settings = await load_health_settings(db)
    tz = ZoneInfo(settings.schedules.timezone)
    started = 0
    for key in SCHEDULE_KEYS:
        enabled, cron = _schedule_cron(settings, key)
        mark = await db.get(HealthScheduleMark, key)
        if mark is None:
            db.add(HealthScheduleMark(schedule=key, fired_at=now))
            await db.commit()
            continue
        if not enabled:
            continue
        due = next_cron_run(cron, mark.fired_at, tz=tz)
        if due is None or due > now:
            continue
        previous = await db.get(HealthCycle, mark.cycle_id) if mark.cycle_id else None
        if previous is not None and previous.status == "open":
            result = CycleStart(refusal="cycle_open", open_cycle_id=previous.id)
        else:
            payload = await _digest_payload(db, mark.fired_at, now) if key == "digest" else None
            result = await start_cycle(db, key=_SCHEDULE_ASSIGNMENT[key], trigger="schedule", payload=payload)
        mark = await db.get(HealthScheduleMark, key)
        mark.fired_at = now
        mark.cycle_id = result.cycle_id if result.started else result.open_cycle_id
        mark.refusal = result.refusal
        await db.commit()
        if result.started:
            started += 1
        else:
            logger.info("Platform Health schedule %r due, not started: %s", key, result.refusal)
    return started


async def _digest_payload(db: AsyncSession, since: datetime, until: datetime) -> dict[str, Any]:
    """The weekly digest's trigger payload: its window and the Modules part read from the stored reports (13.8).

    ``modules`` is computed here, never by the model: per module, the check rounds of the
    window and the checks failed most. A read that fails leaves ``modules`` out and the
    digest says the part is unavailable.
    """
    from app.services.health.check_reports import weekly_module_summary

    payload: dict[str, Any] = {"since": since.isoformat()}
    try:
        async with db.begin_nested():
            payload["modules"] = await weekly_module_summary(db, since=since, until=until)
    except Exception:
        logger.exception("Platform Health weekly digest: the module check reports could not be read")
        payload["modules_unavailable"] = True
    return payload


async def _run_tree(db: AsyncSession, root_id: uuid.UUID) -> list:
    """The cycle's run and every run delegated from it, at any depth: ``(id, status, error, completed_at)``."""
    from app.models.assignment import AssignmentRun
    from app.services.orchestration_engine import find_child_runs

    ids = [root_id, *await find_child_runs(db, [root_id])]
    query = select(AssignmentRun.id, AssignmentRun.status, AssignmentRun.error, AssignmentRun.completed_at)
    return list((await db.execute(query.where(AssignmentRun.id.in_(ids)))).all())


async def _book_runs(
    db: AsyncSession, cycle_id: uuid.UUID, runs: list, *, now: datetime, own_ledger: bool = False
) -> bool:
    """Charge each ended run's LLM traces on the cycle, once per run; whether every one is booked.

    Traces reach the store after the run ends: a run that ended moments ago
    with none yet is booked at a later tick. ``own_ledger``: the cycle's spend is
    already on the Module Analyst's own ledger (#101 S14b-2, dev-docs 12.5), so
    each run is marked booked with nothing charged here: one ceiling, no double count.
    """
    if own_ledger:
        from app.services.health.budget import charge, charged_refs

        done = await charged_refs(db, "run", [run.id for run in runs])
        for run in runs:
            if run.id not in done:
                await charge(db, cycle_id, tokens=0, cost_usd=Decimal(0), ref=("run", run.id))
        return True
    from sqlalchemy import func

    from app.models.llm_trace import LLMTrace
    from app.services.health.budget import charge, charged_refs

    done = await charged_refs(db, "run", [run.id for run in runs])
    runs = [run for run in runs if run.id not in done]
    if not runs:
        return True
    tokens = func.coalesce(
        LLMTrace.total_tokens, func.coalesce(LLMTrace.input_tokens, 0) + func.coalesce(LLMTrace.output_tokens, 0)
    )
    spend = {
        row.assignment_run_id: row
        for row in (
            await db.execute(
                select(
                    LLMTrace.assignment_run_id,
                    func.coalesce(func.sum(tokens), 0).label("tokens"),
                    func.coalesce(func.sum(LLMTrace.total_cost), 0).label("cost"),
                )
                .where(LLMTrace.assignment_run_id.in_([run.id for run in runs]))
                .group_by(LLMTrace.assignment_run_id)
            )
        ).all()
    }
    booked = True
    for run in runs:
        used = spend.get(run.id)
        if used is None and run.completed_at is not None and run.completed_at > now - TRACE_SETTLE_DELAY:
            booked = False
            continue
        await charge(
            db,
            cycle_id,
            tokens=int(used.tokens) if used else 0,
            cost_usd=Decimal(used.cost) if used else Decimal(0),
            ref=("run", run.id),
        )
    return booked


async def _stop_runs(db: AsyncSession, run_ids: list[uuid.UUID], reason: str) -> None:
    """Cancel *run_ids* and what they delegated, as the emergency stop does; commits."""
    from sqlalchemy import update

    from app.a2a.client.delegation import spawn_run_cancel
    from app.a2a.tasks import cascade_cancel
    from app.models.assignment import AssignmentRun
    from app.services.assignment_engine import ACTIVE_RUN_STATUSES, QUEUED_RUN_STATUS
    from app.services.orchestration_engine import cancel_executions_for_runs

    stoppable = (*ACTIVE_RUN_STATUSES, QUEUED_RUN_STATUS)
    await db.execute(
        update(AssignmentRun)
        .where(AssignmentRun.id.in_(run_ids), AssignmentRun.status.in_(stoppable))
        .values(status="cancelled", completed_at=datetime.now(UTC), error=reason)
    )
    tree = await cascade_cancel(db, run_ids, reason=reason)
    await cancel_executions_for_runs(db, tree)
    await db.commit()
    spawn_run_cancel(list(dict.fromkeys(tree)))


_CAPS: Final = ("cycle_budget_reached", "month_budget_reached")


async def _settle_cycle(db: AsyncSession, cycle_id: uuid.UUID, root_id: uuid.UUID, *, now: datetime) -> int:
    """Book one open cycle's ended runs; stop the rest past a cap, or close it (1) once all ended. Commits."""
    from app.services.assignment_engine import TERMINAL_RUN_STATUSES
    from app.services.health.budget import REFUSAL_MESSAGES, budget_refusal, close_cycle, remaining

    tree = await _run_tree(db, root_id)
    from app.services.health.identity import MODULE_CHECKER_ROLES

    specialist = await db.scalar(select(HealthCycle.specialist).where(HealthCycle.id == cycle_id))
    own_ledger = specialist in MODULE_CHECKER_ROLES
    booked = await _book_runs(
        db,
        cycle_id,
        [run for run in tree if run.status in TERMINAL_RUN_STATUSES],
        now=now,
        own_ledger=own_ledger,
    )
    await db.commit()
    active = [run.id for run in tree if run.status not in TERMINAL_RUN_STATUSES]
    if active:
        # A module checker stops at its own ceiling, call by call, never at the fleet caps.
        cap = None if own_ledger else (await remaining(db, cycle_id)).refusal
        if cap in _CAPS:
            logger.info("Platform Health cycle %s: %s, its runs are stopped", cycle_id, cap)
            await _stop_runs(db, active, REFUSAL_MESSAGES[cap])
        return 0
    root = next((run for run in tree if run.id == root_id), None)
    if not booked or root is None:
        return 0
    refusal = None if own_ledger else await budget_refusal(db, cycle_id)
    if refusal in _CAPS:
        # A cap reached when the run ends may have cut its work short, and
        # which tool call it refused is not recorded: never read as completed.
        status, reason = "budget_reached", refusal
    elif root.status == "completed":
        status, reason = "completed", None
    elif root.status == "failed":
        status, reason = "failed", "The run failed."
    else:
        status, reason = "cancelled", f"The run ended {root.status}."
    if root.error and status == "failed":
        logger.info("Platform Health cycle %s: run failed: %r", cycle_id, str(root.error)[:300])
    await close_cycle(db, cycle_id, status=status, reason=reason)
    await db.commit()
    return 1


async def settle_cycles(db: AsyncSession, *, limit: int = 50) -> int:
    """Book the spend of every open cycle's runs, stop a cycle past a cap, close the
    cycles whose whole run tree ended, then build every missing digest; the number closed. Commits.

    Delegation is asynchronous: a cycle closes once its run and every run
    delegated from it ended, so the digest reads the specialists' work.
    """
    from app.services.health.budget import close_stale_cycles
    from app.services.health.digest import build_digest

    rows = (
        await db.execute(
            select(HealthCycle.id, HealthCycleRun.run_id)
            .join(HealthCycleRun, HealthCycleRun.cycle_id == HealthCycle.id)
            .where(HealthCycle.status == "open", HealthCycleRun.run_id.is_not(None))
            .order_by(HealthCycle.opened_at)
            .limit(limit)
        )
    ).all()
    closed = 0
    now = datetime.now(UTC)
    for cycle_id, root_id in rows:
        try:
            closed += await _settle_cycle(db, cycle_id, root_id, now=now)
        except Exception:
            logger.exception("Platform Health cycle %s could not be settled", cycle_id)
            await db.rollback()
    await close_stale_cycles(db, older_than=STALE_CYCLE_AFTER)
    await db.commit()

    pending = (
        (
            await db.execute(
                select(HealthCycle)
                .join(HealthCycleRun, HealthCycleRun.cycle_id == HealthCycle.id)
                .where(HealthCycle.status != "open", HealthCycleRun.digest_built_at.is_(None))
                # Newest first: a digest that keeps failing never holds back the next ones.
                .order_by(HealthCycle.closed_at.desc().nulls_last())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    for cycle in pending:
        try:
            await build_digest(db, cycle)
            await db.commit()
        except Exception:
            logger.exception("Platform Health cycle %s: the digest could not be built", cycle.id)
            await db.rollback()
    return closed


async def health_tick(*, ai_on: bool) -> None:
    """The scheduler's Platform Health step (``autonomous._run_autonomous_tick_inner``).

    Settling runs whatever the switches: a cycle opened before the flag or
    agentic AI went off still closes and gets its digest. Starting work needs
    both, and the administrator's switch (``start_cycle``).
    """
    from app.core.database import async_session
    from app.services.health.availability import health_enabled
    from app.services.health.evaluations import settle_health_evaluations
    from app.services.health.experiments import settle_running_experiments

    # An evaluation outlives the run that launched it: charged (or stopped at a
    # cap) and its experiment's result recorded here, on its original cycle.
    for settle in (settle_health_evaluations, settle_running_experiments):
        try:
            async with async_session() as db:
                await settle(db)
        except Exception:
            logger.exception("Platform Health tick: %s failed", settle.__name__)
    async with async_session() as db:
        await settle_cycles(db)
    if not ai_on or not health_enabled():
        return
    async with async_session() as db:
        await run_due_schedules(db)
    from app.services.health.signals import signals_tick

    await signals_tick()
    # The Module Analyst's triggers (#101 S14a-2b, section 12.2): XTM One mode only.
    from app.services.health.module_analyst import module_analyst_tick

    await module_analyst_tick()
