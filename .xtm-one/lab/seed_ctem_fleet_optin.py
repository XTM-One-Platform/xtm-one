"""The lab's CTEM fleet opt-in states: on, partially on, paused, on and stale, on and failing, not set up.

Started by ``lab.py`` (``seed_ctem_fleet_optin``, ``LAB_CTEM_FLEET_OPTIN=mixed``, the default of the
XTM One labs) with the backend's interpreter, from ``backend/`` and with the backend's environment,
once the built-in fleets, the tracked organizations and the registered OpenCTI integration exist.
Every seeded fleet automation ships disabled (``app.agents.fleet_opt_in``) and no API switches a
built-in fleet's automations on for a lab, so the command center's Fleet activity card only had the
"all off" invitation to render and every CTEM fleet's run estimate read 0. This writes, through the
platform's models and its own pause helpers:

* the ON fleet: every fleet automation enabled as the administrator (``run_as_user_id``), its
  schedule anchored now (``mark_schedule_resumed``: no catch-up run), one OpenCTI automation's
  ``provision`` block serving the tracked organizations, the fleet's organization scope ``monitored``
  and the monitored tracked organization linked to :data:`IDENTITY`, so ``GET
  /fleets/{id}/run-estimate`` answers ``expected_runs_per_month`` above 0 (``lab_asgi`` answers the
  count);
* the PARTIAL fleet: its first fleet automation enabled, the others left as seeded;
* the PAUSED fleet: every fleet automation paused with ``user_toggle``, a reason outside
  ``assignment_pause.EXPECTED_REASONS``;
* the STALE fleet (#9670): every fleet automation enabled and one of its members owning a blocked
  work item quiet for :data:`STALE_AGE` and past its due date, so ``GET /fleets/{id}/health`` reads
  ``stale`` (``work_item._stale_sql_clause``); blocked, so no inbox dispatches it;
* the FAILING fleet (#9670): every fleet automation enabled and one of its members' latest run a
  failure of its own work :data:`FAILED_AGE` ago, so the health reads ``failing``
  (``run_outcome.member_verdict``);
* with fewer fleets than states (the four built-in CTEM fleets of #1048), the health state left
  over (``failing``) is carried by the PARTIAL fleet (:func:`extras`, the row's ``also``);
* every other CTEM fleet left as seeded (not set up). With four or five built-in CTEM fleets none is
  left: ``lab.py`` forks one (every copied automation is created switched off, and a fork of a
  ``ctem-*`` fleet is a CTEM fleet) for the Not set up state (#9689).

The trigger provisioning pass is frozen by ``lab_asgi`` and nothing answers OpenCTI here, so no
trigger is created. Arguments: the administrator's user id, the ``organizations.json``
``seed_organizations`` wrote, and the JSON file the states are written to. Idempotent.

``--all-off <out>`` (``LAB_CTEM_FLEET_OPTIN=off``, #9703) switches off every fleet automation of
every built-in CTEM fleet instead (:func:`all_off`) and writes the counts to *out*.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: The fixture Organization (``lab_asgi.LAB_OPENCTI_ORGANIZATIONS``, Acme Corporation), as in ``seed_run_estimate``.
IDENTITY = "identity--0b5c3e2a-lab0-4000-8000-000000000001"
#: The pause reason of the paused fleet: a person switched it off, so it reads "Paused", not "Not set up".
PAUSE_REASON = "user_toggle"
#: Fleet slugs tried first for the ON fleet (its OpenCTI automations can serve tracked organizations).
ON_PREFERRED = ("ctem-vuln-prioritization",)
#: The marker of the rows this seed writes (the stale item's key prefix, the failed run's context).
SEEDED_BY = "lab-ctem-fleet-optin"
#: How long ago the stale fleet's work item last moved (past the 24 h ``work_item_stale_after_hours``).
STALE_AGE = timedelta(hours=48)
#: How long ago the failing fleet's run failed (inside ``run_outcome.HEALTH_WINDOW``).
FAILED_AGE = timedelta(minutes=30)
#: States whose fleet has every automation enabled.
ALL_ON = ("on", "stale", "failing")
#: The pause reason of ``--all-off`` (#9703): expected (``assignment_pause.EXPECTED_REASONS``), so Not set up.
ALL_OFF_REASON = "system_seed"


def trackable(trigger_config: object) -> bool:
    """Whether an automation's ``provision`` block can serve the tracked organizations.

    An OpenCTI ``provision`` block that reads the primary profile (``organization_context``) is never
    combined with tracked organizations (``read_provision_spec`` refuses it).
    """
    if not isinstance(trigger_config, dict) or trigger_config.get("provider") != "opencti":
        return False
    block = trigger_config.get("provision")
    return isinstance(block, dict) and not block.get("organization_context")


def plan(fleets: dict[str, list[tuple[str, bool]]]) -> dict[str, str]:
    """The state of each fleet slug: ``on``, ``partial``, ``paused``, ``stale``, ``failing`` or ``not_set_up``.

    *fleets* maps a slug to its fleet automations as ``(assignment id, trackable)``. The ON fleet has a
    trackable automation (a preferred slug first), the PARTIAL one at least two automations, the
    PAUSED, STALE and FAILING ones at least one; STALE then FAILING take the next fleets with an
    automation, the rest stay not set up (none with five fleets: ``lab.py`` forks one for that state,
    #9689). The order is the slugs', so a restarted lab plans the same states. Only the ON fleet is
    required: with fewer fleets than states (the single seeded CTEM loop of #2105) a state with no
    fleet left is not planned, and ``lab.py`` logs it as not seeded (#2261).
    """
    slugs = sorted(fleets)
    candidates = [s for s in ON_PREFERRED if s in fleets] + slugs
    on = next((s for s in candidates if any(t for _, t in fleets[s])), None)
    if on is None:
        raise RuntimeError(f"no CTEM fleet has an OpenCTI automation that serves tracked organizations: {slugs}")
    rest = [s for s in slugs if s != on]
    partial = next((s for s in rest if len(fleets[s]) >= 2), None)
    paused = next((s for s in rest if s != partial and fleets[s]), None)
    states = dict.fromkeys(slugs, "not_set_up")
    states[on] = "on"
    if partial is not None:
        states[partial] = "partial"
    if paused is not None:
        states[paused] = "paused"
    for state in ("stale", "failing"):
        left = [s for s in rest if states[s] == "not_set_up"]
        chosen = next((s for s in left if fleets[s]), None)
        if chosen is not None:
            states[chosen] = state
    return states


#: The health states a fleet of its own reads when there are enough built-in CTEM fleets.
HEALTH_STATES = ("stale", "failing")


def extras(states: dict[str, str]) -> dict[str, list[str]]:
    """The health states :func:`plan` left without a fleet, carried by the PARTIAL fleet.

    With the four built-in CTEM fleets of #1048 (on, partial, paused, stale) no fleet is left for
    ``failing``: the partial fleet's enabled automation then carries the failed run, so its health
    reads ``failing`` while its opt-in still reads partially on, and every state of
    ``lab.CTEM_FLEET_OPTIN_REQUIRED`` is rendered. Five fleets or more: nothing.
    """
    missing = [s for s in HEALTH_STATES if s not in states.values()]
    partial = next((s for s, state in states.items() if state == "partial"), None)
    return {partial: missing} if missing and partial is not None else {}


async def seed_stale_item(db, work_item, admin: uuid.UUID, slug: str, agent_id: uuid.UUID) -> None:
    """One blocked work item of *agent_id*, quiet for :data:`STALE_AGE` and past due (idempotent by key).

    Blocked with no ``blocked_on``: stale (``is_work_item_stale``) and never dispatched to an inbox.
    """
    from sqlalchemy import select

    key = f"{SEEDED_BY}:stale:{slug}"
    then = datetime.now(UTC) - STALE_AGE
    item = (await db.execute(select(work_item).where(work_item.correlation_key == key))).scalars().first()
    if item is None:
        item = work_item(
            user_id=admin,
            owner_agent_id=agent_id,
            correlation_key=key,
            title="Seeded by the lab: a work item nobody moved for two days",
            description="The Stale state of the command center's Fleet activity.",
            source="agent",
        )
        db.add(item)
    item.owner_agent_id = agent_id
    item.status = "blocked"
    item.blocked_on = None
    item.blocked_reason = "Seeded by the lab: waits on nothing, so it reads stale."
    item.due_at = then
    item.created_at = then
    item.last_activity_at = then


async def seed_failed_run(db, assignment_run, admin: uuid.UUID, assignment) -> None:
    """A failed run of *assignment* :data:`FAILED_AGE` ago, its agent's latest (idempotent within the hour)."""
    from sqlalchemy import select

    now = datetime.now(UTC)
    recent = (
        await db.execute(
            select(assignment_run.id).where(
                assignment_run.agent_id == assignment.agent_id,
                assignment_run.trigger_context["seeded_by"].as_string() == SEEDED_BY,
                assignment_run.created_at >= now - timedelta(hours=1),
            )
        )
    ).first()
    if recent is not None:
        return
    started = now - FAILED_AGE
    db.add(
        assignment_run(
            agent_id=assignment.agent_id,
            assignment_id=assignment.id,
            user_id=admin,
            status="failed",
            error="Seeded by the lab: the agent's own work failed (the source could not be read).",
            trigger_payload={"seeded_by": SEEDED_BY},
            trigger_context={"type": "schedule", "seeded_by": SEEDED_BY},
            result_summary="Seeded by the lab: the Failing state of the command center's Fleet activity.",
            created_at=started,
            started_at=started,
            completed_at=started + timedelta(minutes=1),
        )
    )


async def seed(admin_id: str, tracked: list[str]) -> dict:
    """Write the opt-in states; answer, per fleet slug, its id, state and enabled / paused counts."""
    from sqlalchemy import select

    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from app.agents.builtin_agents import FLEET_OPT_IN_ASSIGNMENTS
    from app.core.database import async_session, engine
    from app.models.agent import Agent
    from app.models.assignment import Assignment, AssignmentRun
    from app.models.fleet import FLEET_ROLE_MEMBER, Fleet, FleetMember
    from app.models.organization import Organization
    from app.models.work_item import WorkItem
    from app.services.assignment_pause import (
        clear_pause_provenance,
        mark_schedule_resumed,
        pause_assignment,
    )

    admin = uuid.UUID(admin_id)
    async with async_session() as db:
        fleets = (
            (
                await db.execute(
                    select(Fleet).where(Fleet.is_builtin.is_(True), Fleet.slug.like("ctem-%")).order_by(Fleet.slug)
                )
            )
            .scalars()
            .all()
        )
        automations: dict[str, list[Assignment]] = {}
        for fleet in fleets:
            rows = (
                await db.execute(
                    select(Assignment, Agent.slug)
                    .join(Agent, Agent.id == Assignment.agent_id)
                    .join(FleetMember, FleetMember.agent_id == Agent.id)
                    .where(
                        FleetMember.fleet_id == fleet.id,
                        FleetMember.role == FLEET_ROLE_MEMBER,
                        Agent.is_builtin.is_(True),
                    )
                    .order_by(Agent.slug, Assignment.name)
                )
            ).all()
            automations[fleet.slug] = [a for a, slug in rows if (slug, a.name) in FLEET_OPT_IN_ASSIGNMENTS]
        if not automations:
            raise RuntimeError("no built-in CTEM fleet is seeded")
        states = plan({s: [(str(a.id), trackable(a.trigger_config)) for a in rows] for s, rows in automations.items()})
        carried = extras(states)
        by_slug = {f.slug: f for f in fleets}

        def switch_on(assignment: Assignment) -> None:
            if not assignment.enabled:
                mark_schedule_resumed(assignment)
            assignment.enabled = True
            assignment.run_as_user_id = assignment.run_as_user_id or admin
            clear_pause_provenance(assignment)

        for slug, state in states.items():
            rows = automations[slug]
            if state in ALL_ON:
                for assignment in rows:
                    switch_on(assignment)
            if state == "stale":
                await seed_stale_item(db, WorkItem, admin, slug, rows[0].agent_id)
            elif state == "failing":
                await seed_failed_run(db, AssignmentRun, admin, rows[0])
            if state == "on":
                served = next(a for a in rows if trackable(a.trigger_config))
                config = dict(served.trigger_config)
                config["provision"] = {**config["provision"], "organizations": "tracked"}
                served.trigger_config = config
                by_slug[slug].organization_scope = {"mode": "monitored"}
            elif state == "partial":
                switch_on(rows[0])
            elif state == "paused":
                for assignment in rows:
                    if assignment.enabled or assignment.paused_reason != PAUSE_REASON:
                        pause_assignment(
                            assignment,
                            reason=PAUSE_REASON,
                            details="Paused by the lab: the Paused state of the command center's Fleet activity.",
                            actor_user_id=admin,
                            actor_label="lab",
                        )
            for extra in carried.get(slug, []):
                if extra == "stale":
                    await seed_stale_item(db, WorkItem, admin, slug, rows[0].agent_id)
                elif extra == "failing":
                    await seed_failed_run(db, AssignmentRun, admin, rows[0])
        organizations = [await db.get(Organization, uuid.UUID(i)) for i in tracked]
        monitored = next(
            (o for o in organizations if o is not None and o.archived_at is None and o.monitoring == "on"), None
        )
        if monitored is None:
            raise RuntimeError("no monitored tracked organization: the ON fleet's estimate has nothing to count")
        monitored.opencti_organization_id = IDENTITY
        await db.commit()
        answer = {
            slug: {
                "id": str(by_slug[slug].id),
                "state": state,
                "also": carried.get(slug, []),
                "total": len(automations[slug]),
                "enabled": sum(1 for a in automations[slug] if a.enabled),
                "paused": sum(1 for a in automations[slug] if not a.enabled and a.paused_reason == PAUSE_REASON),
            }
            for slug, state in states.items()
        }
    await engine.dispose()
    return answer


async def all_off() -> dict:
    """``LAB_CTEM_FLEET_OPTIN=off`` (#9703): every fleet automation of every built-in CTEM fleet switched off.

    A seeded automation ships disabled, so on a fresh lab this writes nothing; one found enabled (a
    restarted lab, a boot that enabled it) is switched off with ``system_seed``, an expected reason, so
    the fleet reads Not set up, never Paused. Answers, per fleet slug, its id and total / enabled counts.
    """
    from sqlalchemy import select

    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from app.agents.builtin_agents import FLEET_OPT_IN_ASSIGNMENTS
    from app.core.database import async_session, engine
    from app.models.agent import Agent
    from app.models.assignment import Assignment
    from app.models.fleet import FLEET_ROLE_MEMBER, Fleet, FleetMember
    from app.services.assignment_pause import pause_assignment

    answer: dict = {}
    async with async_session() as db:
        fleets = (
            (
                await db.execute(
                    select(Fleet).where(Fleet.is_builtin.is_(True), Fleet.slug.like("ctem-%")).order_by(Fleet.slug)
                )
            )
            .scalars()
            .all()
        )
        for fleet in fleets:
            rows = (
                await db.execute(
                    select(Assignment, Agent.slug)
                    .join(Agent, Agent.id == Assignment.agent_id)
                    .join(FleetMember, FleetMember.agent_id == Agent.id)
                    .where(FleetMember.fleet_id == fleet.id, FleetMember.role == FLEET_ROLE_MEMBER)
                )
            ).all()
            automations = [a for a, slug in rows if (slug, a.name) in FLEET_OPT_IN_ASSIGNMENTS]
            for assignment in automations:
                if assignment.enabled:
                    pause_assignment(
                        assignment,
                        reason=ALL_OFF_REASON,
                        details="Switched off by the lab: every CTEM fleet not set up.",
                        actor_label="lab",
                    )
            answer[fleet.slug] = {
                "id": str(fleet.id),
                "total": len(automations),
                "enabled": sum(1 for a in automations if a.enabled),
            }
        await db.commit()
    await engine.dispose()
    return answer


def main() -> None:
    if sys.argv[1] == "--all-off":
        Path(sys.argv[2]).write_text(json.dumps(asyncio.run(all_off())))
        return
    admin_id, organizations, out = sys.argv[1:4]
    tracked = json.loads(Path(organizations).read_text())["tracked"]
    Path(out).write_text(json.dumps(asyncio.run(seed(admin_id, tracked))))


if __name__ == "__main__":
    main()
