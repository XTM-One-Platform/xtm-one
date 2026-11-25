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

"""Fleet control (#9769, slices S2 and S4b; #11060): enable a fleet, pause every automation of it at once, resume exactly those, set it back to not set up.

"Enable fleet" (S4b) switches on every automation of the fleet that is off,
except one an emergency stop holds: a seeded fleet automation (``created_disabled``),
one a person switched off, one the fleet's Pause all switched off. It is the
fleet-wide form of the per-automation switch and follows its rules: a seeded
fleet automation runs as the run-as user the caller picks (the caller by
default, ``app.services.fleet_run_as``) and is refused while that user lacks a
connected module it needs, so the whole call is refused rather than half
applied; a dry run answers the plan (each automation and what it misses) for
the setup dialog. Refused during a platform emergency stop, as Resume is.

"Pause all" switches off every fleet automation that is on (the population of
:func:`app.services.fleet_views.is_fleet_automation`: on a built-in fleet its
seeded fleet automations, on a custom fleet every assignment of its home
members) and stamps each with ``paused_reason = fleet_pause``
(:data:`app.services.assignment_pause.FLEET_PAUSE_REASON`). "Resume" re-enables
the assignments carrying that stamp and nothing else, so an automation a person
had switched off before the pause, one an emergency stop holds or one the
platform quarantined stays off.

"Back to not set up" (#11060) switches off every fleet automation that is on
and re-stamps every off one ``created_disabled`` (one an emergency stop holds
excepted), so a fleet still being built reads "Not set up" again without its
assignments being deleted and recreated.

Runs in flight continue: a pause only stops the next trigger, as the per
assignment switch does. The OpenCTI / OpenAEV triggers provisioned for a
switched-off assignment are removed by the provisioning tick, as today.

The caller owns the commit; every switch writes its pause or resume audit row
(``assignment_pause``) and the caller writes one fleet row for the whole action.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.fleet import Fleet
from app.models.user import User
from app.services.assignment_pause import (
    CREATED_DISABLED_REASON,
    FLEET_PAUSE_REASON,
    PANIC_STOP_REASON,
    actor_display_name,
    pause_assignment,
    record_pause_audits,
    resume_assignment,
)
from app.services.fleets import FleetActionRefusedError, fleet_members

RESUME_REFUSED_DURING_STOP = (
    "An emergency stop is in effect. Lift it (profile menu or banner) before resuming a fleet."
)
ENABLE_REFUSED_DURING_STOP = "An emergency stop is in effect. Lift it (profile menu or banner) before enabling a fleet."
ENABLE_NOTHING_TO_ENABLE = "Every automation of this fleet is already on."


@dataclass(frozen=True)
class FleetControlOutcome:
    """What a pause or a resume switched: ``changed`` assignments, by name."""

    changed: int
    names: tuple[str, ...]


#: ``(assignment, agent)``: a fleet automation and the agent that carries it.
_AutomationRow = tuple[Any, Agent]


async def _fleet_automation_rows(
    db: AsyncSession, fleet: Fleet, *, lock: bool, dashboard: bool = False
) -> list[_AutomationRow]:
    """``(assignment, agent)`` of *fleet*'s home members' fleet automations, assignments locked when *lock*.

    *dashboard* adds the dashboard schedules of a built-in fleet's members (#2157), which
    Pause all and Resume act on beside the fleet automations.
    """
    from app.models.assignment import Assignment
    from app.services.fleet_views import home_member_ids, is_dashboard_schedule, is_fleet_automation

    ids = home_member_ids(await fleet_members(db, fleet.id), fleet.orchestrator_agent_id)
    if not ids:
        return []
    query = (
        select(Assignment, Agent)
        .join(Agent, Agent.id == Assignment.agent_id)
        .where(Assignment.agent_id.in_(ids))
        .order_by(Assignment.name, Assignment.id)
    )
    if lock:
        query = query.with_for_update(of=Assignment).execution_options(populate_existing=True)
    builtin_fleet = bool(getattr(fleet, "is_builtin", False))
    return [
        (assignment, agent)
        for assignment, agent in (await db.execute(query)).all()
        if is_fleet_automation(agent.slug, bool(agent.is_builtin), assignment.name, builtin_fleet=builtin_fleet)
        or (
            dashboard
            and is_dashboard_schedule(agent.slug, bool(agent.is_builtin), assignment.name, builtin_fleet=builtin_fleet)
        )
    ]


async def _fleet_automations(db: AsyncSession, fleet: Fleet, *, lock: bool, dashboard: bool = False) -> list[Any]:
    """The fleet automations of *fleet*'s home members, rows locked for the write when *lock*.

    *dashboard* adds the members' dashboard schedules (#2157), the population of Pause all and Resume.
    """
    return [
        assignment for assignment, _agent in await _fleet_automation_rows(db, fleet, lock=lock, dashboard=dashboard)
    ]


async def pausable_count(db: AsyncSession, fleet: Fleet) -> int:
    """How many automations Pause all would switch off now (a dry run, nothing locked)."""
    return sum(1 for a in await _fleet_automations(db, fleet, lock=False, dashboard=True) if a.enabled)


async def resumable_count(db: AsyncSession, fleet: Fleet) -> int:
    """How many automations Resume would switch back on now (a dry run, nothing locked)."""
    return sum(
        1
        for a in await _fleet_automations(db, fleet, lock=False, dashboard=True)
        if not a.enabled and a.paused_reason == FLEET_PAUSE_REASON
    )


async def pause_fleet(
    db: AsyncSession, fleet: Fleet, *, actor: User, request: Request | None = None
) -> FleetControlOutcome:
    """Switch off every fleet automation of *fleet* that is on, stamped ``fleet_pause``.

    Idempotent: nothing on, nothing changed. Off automations keep their own
    pause stamp, so a resume never switches them on. The dashboard schedules of a
    built-in fleet's members (#2157) are paused too: they run while the badge reads off.
    """
    label = actor_display_name(actor)
    details = f"Every automation of the fleet '{fleet.name}' paused by {label}."
    paused = [a for a in await _fleet_automations(db, fleet, lock=True, dashboard=True) if a.enabled]
    for assignment in paused:
        pause_assignment(
            assignment,
            reason=FLEET_PAUSE_REASON,
            details=details,
            actor_user_id=actor.id,
            actor_label=label,
        )
    await record_pause_audits(
        db, assignments=paused, user=actor, reason=FLEET_PAUSE_REASON, details=details, request=request
    )
    return FleetControlOutcome(changed=len(paused), names=tuple(a.name for a in paused))


async def resume_fleet(
    db: AsyncSession, fleet: Fleet, *, actor: User, request: Request | None = None
) -> FleetControlOutcome:
    """Switch back on exactly the fleet automations *fleet*'s pause switched off.

    Refused (409) while a platform emergency stop is in effect: the stop's
    boundary is lifted by the stop's own resume, never fleet by fleet.
    """
    from app.services.panic import platform_stop_open

    if await platform_stop_open(db):
        raise FleetActionRefusedError(409, RESUME_REFUSED_DURING_STOP)
    resumed = [
        a
        for a in await _fleet_automations(db, fleet, lock=True, dashboard=True)
        if not a.enabled and a.paused_reason == FLEET_PAUSE_REASON
    ]
    for assignment in resumed:
        await resume_assignment(db, assignment=assignment, user=actor, request=request, note=f"Fleet '{fleet.name}' resumed")
    return FleetControlOutcome(changed=len(resumed), names=tuple(a.name for a in resumed))


@dataclass(frozen=True)
class FleetUnsetOutcome(FleetControlOutcome):
    """What "Back to not set up" changed, plus the automations an emergency stop holds, left as they are."""

    held_by_stop: tuple[str, ...] = ()


def _unsettable(assignment: Any) -> bool:
    """On, or off with a stamp other than ``created_disabled``; never one an emergency stop holds."""
    if assignment.enabled:
        return True
    return assignment.paused_reason not in (CREATED_DISABLED_REASON, PANIC_STOP_REASON)


async def unsettable_count(db: AsyncSession, fleet: Fleet) -> tuple[int, tuple[str, ...]]:
    """Dry run of :func:`unset_fleet`: how many automations it would change, and the stopped ones it leaves."""
    automations = await _fleet_automations(db, fleet, lock=False)
    held = tuple(a.name for a in automations if not a.enabled and a.paused_reason == PANIC_STOP_REASON)
    return sum(1 for a in automations if _unsettable(a)), held


async def unset_fleet(
    db: AsyncSession, fleet: Fleet, *, actor: User, request: Request | None = None
) -> FleetUnsetOutcome:
    """Put *fleet* back to "Not set up" without deleting anything (#11060).

    Every fleet automation that is on is switched off, and every one that is off
    with another stamp (a person's or an agent's switch-off, Pause all, a
    quarantine) is re-stamped ``created_disabled``, so the fleet reads "Not set
    up", 0 on, no Resume. Assignments, ids, prompts, triggers and run history
    are kept; Enable fleet switches them on again as for a new fleet. One an
    emergency stop holds is left untouched (only the stop's own resume lifts
    it) and named in the outcome. Idempotent: a second call changes nothing.
    Not refused during an emergency stop: like Pause all, it only removes
    future work.
    """
    label = actor_display_name(actor)
    details = f"Set back to not set up by {label}."
    automations = await _fleet_automations(db, fleet, lock=True)
    held = tuple(a.name for a in automations if not a.enabled and a.paused_reason == PANIC_STOP_REASON)
    changed = [a for a in automations if _unsettable(a)]
    for assignment in changed:
        pause_assignment(
            assignment,
            reason=CREATED_DISABLED_REASON,
            details=details,
            actor_user_id=actor.id,
            actor_label=label,
        )
    await record_pause_audits(
        db, assignments=changed, user=actor, reason=CREATED_DISABLED_REASON, details=details, request=request
    )
    return FleetUnsetOutcome(changed=len(changed), names=tuple(a.name for a in changed), held_by_stop=held)


def _enableable(assignment: Any) -> bool:
    """Off, and not held by an emergency stop (only the stop's own resume lifts that)."""
    return not assignment.enabled and assignment.paused_reason != PANIC_STOP_REASON


ENABLE_UNKNOWN_SELECTION = "Pick automations of this fleet that are off: {names} cannot be switched on here."
ENABLE_EMPTY_SELECTION = "Pick at least one automation to switch on."


@dataclass(frozen=True)
class FleetEnableItem:
    """One automation Enable fleet can switch on, and the modules its run-as user misses."""

    id: uuid.UUID
    name: str
    #: Module providers (``opencti``, ``openaev``, ``opencrq``) the automation acts through.
    providers: tuple[str, ...]
    #: Of ``providers``, the ones the run-as user has no connected integration for.
    missing: tuple[str, ...]
    #: Part of this call: every automation that is off unless the call names a subset (#9173 S5).
    selected: bool = True
    #: Runs a day its schedule starts (0 for a trigger that is not a schedule, #1538).
    runs_per_day: float = 0.0
    #: It runs once per event (event, platform event, webhook, work item): its daily count is not known ahead.
    event_driven: bool = False


@dataclass(frozen=True)
class FleetEnablePlan:
    """What Enable fleet can switch on, what this call picks, as whom, and whether it can.

    ``items`` lists every automation that is off (the setup dialog's checklist);
    ``ready``, ``missing`` and ``changed`` read the selected ones only, so a
    partial enable is refused only for a module a picked automation needs.
    """

    run_as_user_id: uuid.UUID
    items: tuple[FleetEnableItem, ...]

    @property
    def selected(self) -> tuple[FleetEnableItem, ...]:
        return tuple(item for item in self.items if item.selected)

    @property
    def changed(self) -> int:
        return len(self.selected)

    @property
    def ready(self) -> bool:
        picked = self.selected
        return bool(picked) and not any(item.missing for item in picked)

    @property
    def missing(self) -> list[str]:
        """Every provider some selected automation misses, in a stable order."""
        seen = {p for item in self.selected for p in item.missing}
        from app.services.fleet_run_as import MODULE_PROVIDERS

        return [p for p in MODULE_PROVIDERS if p in seen]


#: Days of history the average cost of one run is read over.
ESTIMATE_COST_DAYS = 30
#: Days a schedule too rare for a month is counted over (#1538, UX round 2 B3).
YEAR_DAYS = 365
#: Trigger kinds that start one run per event: their daily count is unknown before they run.
EVENT_TRIGGERS = frozenset({"integration_event", "platform_event", "webhook", "work_item"})


@dataclass(frozen=True)
class FleetEnableEstimate:
    """The expected load of the automations Enable fleet picks (#1538): an order of magnitude, not a quote.

    ``scheduled_runs_per_day`` counts the schedules of the picked automations
    (their cron over 30 days, the About tab basis, divided by 30).
    ``event_automations`` counts the picked automations that run once per
    event (their rate depends on the events, bounded by ``max_concurrent_runs``).
    ``avg_cost_per_run`` is the mean cost of their runs of the last
    :data:`ESTIMATE_COST_DAYS` days from the local trace store, None when none
    ran; ``cost_per_day`` multiplies it by the scheduled runs, None without it.
    """

    scheduled_runs_per_day: float
    event_automations: int
    avg_cost_per_run: float | None
    cost_per_day: float | None
    cost_days: int = ESTIMATE_COST_DAYS


def scheduled_runs_per_day(assignments: list[Any]) -> float:
    """The runs a day the schedules of *assignments* start (an unreadable cron counts 0).

    Kept to four decimals, never one: a monthly schedule is 0.0333 a day, and a
    per-item rate rounded to 0.0 made a monthly-only pick read as no run at all
    once the client summed the items (#1538, UX round 1 B3). The client rounds
    the total it shows.

    A schedule that never fires in the 30-day window (a quarterly, half-yearly
    or yearly cron) is counted over a full year instead, its fires divided by
    :data:`YEAR_DAYS`, so it never reads as no run (#1538, UX round 2 B3).
    """
    from app.services.fleet_doc_generation import QUOTA_DAYS, cron_fires

    total = 0.0
    for assignment in assignments:
        if str(getattr(assignment, "trigger_type", "") or "") != "schedule":
            continue
        config = assignment.trigger_config if isinstance(assignment.trigger_config, dict) else {}
        cron = config.get("cron")
        monthly = cron_fires(cron, QUOTA_DAYS)
        if monthly:
            total += monthly / QUOTA_DAYS
        elif monthly == 0:
            total += (cron_fires(cron, YEAR_DAYS) or 0) / YEAR_DAYS
    return round(total, 4)


async def average_run_cost(db: AsyncSession, assignment_ids: list[uuid.UUID], *, days: int) -> float | None:
    """Mean cost of one run of *assignment_ids* over the last *days*, from ``llm_traces``; None when none ran."""
    if not assignment_ids:
        return None
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func

    from app.models.assignment import AssignmentRun
    from app.models.llm_trace import LLMTrace

    since = datetime.now(UTC) - timedelta(days=days)
    runs, cost = (
        await db.execute(
            select(
                func.count(func.distinct(LLMTrace.assignment_run_id)),
                func.coalesce(func.sum(LLMTrace.total_cost), 0),
            )
            .join(AssignmentRun, AssignmentRun.id == LLMTrace.assignment_run_id)
            .where(AssignmentRun.assignment_id.in_(assignment_ids), LLMTrace.started_at >= since)
        )
    ).one()
    if not runs:
        return None
    return round(float(cost or 0) / int(runs), 4)


async def estimate_enable(db: AsyncSession, assignments: list[Any]) -> FleetEnableEstimate:
    """The :class:`FleetEnableEstimate` of the picked *assignments* (read only)."""
    per_day = scheduled_runs_per_day(assignments)
    events = sum(1 for a in assignments if str(getattr(a, "trigger_type", "") or "") in EVENT_TRIGGERS)
    avg = await average_run_cost(db, [a.id for a in assignments], days=ESTIMATE_COST_DAYS)
    return FleetEnableEstimate(
        scheduled_runs_per_day=per_day,
        event_automations=events,
        avg_cost_per_run=avg,
        cost_per_day=None if avg is None else round(avg * per_day, 2),
    )


def check_enable_selection(rows: list[_AutomationRow], assignment_ids: list[uuid.UUID] | None) -> None:
    """Refuse (422) an empty pick, or an id that is not an automation of the fleet that is off."""
    if assignment_ids is None:
        return
    if not assignment_ids:
        raise FleetActionRefusedError(422, ENABLE_EMPTY_SELECTION)
    enableable = {a.id for a, _agent in rows if _enableable(a)}
    unknown = [str(i) for i in assignment_ids if i not in enableable]
    if unknown:
        raise FleetActionRefusedError(422, ENABLE_UNKNOWN_SELECTION.format(names=", ".join(unknown)))


async def _enable_plan(
    db: AsyncSession,
    rows: list[_AutomationRow],
    run_as_user_id: uuid.UUID,
    assignment_ids: list[uuid.UUID] | None = None,
) -> tuple[FleetEnablePlan, list[_AutomationRow]]:
    """The plan over *rows* and the ``(assignment, agent)`` pairs this call would switch on.

    *assignment_ids* ``None`` picks every automation that is off; a list picks
    those (checked first with :func:`check_enable_selection`).
    """
    from app.services.fleet_run_as import is_fleet_automation, missing_providers, required_providers

    check_enable_selection(rows, assignment_ids)
    picked = None if assignment_ids is None else set(assignment_ids)
    candidates = [(a, agent) for a, agent in rows if _enableable(a)]
    targets = [(a, agent) for a, agent in candidates if picked is None or a.id in picked]
    cache: dict[tuple[str, ...], list[str]] = {}
    items: list[FleetEnableItem] = []
    for assignment, agent in candidates:
        # Only a seeded fleet automation runs as the picked user (#9188); any
        # other assignment keeps its own identity, as the per-automation switch does.
        providers = tuple(required_providers(agent, assignment)) if is_fleet_automation(agent, assignment) else ()
        if providers and providers not in cache:
            cache[providers] = await missing_providers(db, run_as_user_id, list(providers))
        missing = cache.get(providers, []) if providers else []
        items.append(
            FleetEnableItem(
                id=assignment.id,
                name=assignment.name,
                providers=providers,
                missing=tuple(missing),
                selected=picked is None or assignment.id in picked,
                runs_per_day=scheduled_runs_per_day([assignment]),
                event_driven=str(getattr(assignment, "trigger_type", "") or "") in EVENT_TRIGGERS,
            )
        )
    return FleetEnablePlan(run_as_user_id=run_as_user_id, items=tuple(items)), targets


async def plan_fleet_enable(
    db: AsyncSession,
    fleet: Fleet,
    *,
    run_as_user_id: uuid.UUID,
    assignment_ids: list[uuid.UUID] | None = None,
) -> FleetEnablePlan:
    """What Enable fleet would switch on now, as *run_as_user_id* (a dry run, nothing locked or written)."""
    rows = await _fleet_automation_rows(db, fleet, lock=False)
    plan, _targets = await _enable_plan(db, rows, run_as_user_id, assignment_ids)
    return plan


async def estimate_fleet_enable(
    db: AsyncSession, fleet: Fleet, *, assignment_ids: list[uuid.UUID] | None = None
) -> FleetEnableEstimate:
    """The expected load of what Enable fleet picks (every automation that is off, or *assignment_ids*)."""
    rows = await _fleet_automation_rows(db, fleet, lock=False)
    picked = None if assignment_ids is None else set(assignment_ids)
    targets = [a for a, _agent in rows if _enableable(a) and (picked is None or a.id in picked)]
    return await estimate_enable(db, targets)


async def enable_fleet(
    db: AsyncSession,
    fleet: Fleet,
    *,
    actor: User,
    run_as_user_id: uuid.UUID,
    assignment_ids: list[uuid.UUID] | None = None,
    request: Request | None = None,
) -> FleetControlOutcome:
    """Switch on the automations of *fleet* that are off, seeded fleet automations as *run_as_user_id*.

    *assignment_ids* picks some of them (the setup dialog's partial enable,
    #9173 S5); ``None`` picks every one. All or nothing over the pick: refused
    (409) during a platform emergency stop, (409) when nothing is off, (422)
    for an empty pick or an id that is not an automation of the fleet that is
    off, (400) when the run-as user misses a module a picked automation
    needs, before anything is written. Each switch clears its pause stamp,
    anchors its schedule at now (no catch-up run) and writes its resume audit row.
    """
    from app.services.fleet_run_as import is_fleet_automation, missing_refusal
    from app.services.panic import platform_stop_open

    if await platform_stop_open(db):
        raise FleetActionRefusedError(409, ENABLE_REFUSED_DURING_STOP)
    rows = await _fleet_automation_rows(db, fleet, lock=True)
    if not any(_enableable(a) for a, _agent in rows):
        raise FleetActionRefusedError(409, ENABLE_NOTHING_TO_ENABLE)
    plan, targets = await _enable_plan(db, rows, run_as_user_id, assignment_ids)
    if not targets:
        raise FleetActionRefusedError(409, ENABLE_NOTHING_TO_ENABLE)
    if plan.missing:
        raise FleetActionRefusedError(400, missing_refusal(plan.missing))
    from app.services.fleets import is_fleet_health_answered, is_fleet_health_check, is_fleet_past_due_listener

    for assignment, agent in targets:
        # The weekly health check (#11700), its answer listener (#1406) and the overdue
        # items listener (#1662) read the fleet as the person who enabled it, never as
        # the seed owner.
        seeded_health = (
            is_fleet_health_check(assignment)
            or is_fleet_health_answered(assignment)
            or is_fleet_past_due_listener(assignment)
        )
        if is_fleet_automation(agent, assignment) or (seeded_health and assignment.run_as_user_id is None):
            assignment.run_as_user_id = run_as_user_id
        await resume_assignment(
            db, assignment=assignment, user=actor, request=request, note=f"Fleet '{fleet.name}' enabled"
        )
    return FleetControlOutcome(changed=len(targets), names=tuple(a.name for a, _agent in targets))


def fleet_control_audit_details(outcome: FleetControlOutcome, fleet_id: uuid.UUID) -> dict:
    """The ``details`` of the fleet's own pause / resume audit row."""
    return {"fleet_id": str(fleet_id), "assignments": outcome.changed, "names": list(outcome.names)}
