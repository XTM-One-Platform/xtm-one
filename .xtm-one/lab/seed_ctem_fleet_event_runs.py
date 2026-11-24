"""The lab's CTEM fleet event runs: one event-driven run with its trace on each built-in CTEM fleet.

Started by ``lab.py`` (``seed_ctem_fleet_event_runs``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once the backend answers and the built-in
fleets are seeded: no API creates an ``AssignmentRun`` with a given ``trigger_context`` (a run
is started by its trigger), so this writes them through the platform's model. Arguments: the
user who owns every run (the lab administrator, so ``run_visible_filter`` shows them) and the
JSON file the answer is written to.

For each fleet of ``FLEETS`` (slug, in display order), the event is read from the fleet's own
assignments, never invented (``pick_trigger``): the first ``integration_event`` assignment of a
home member (its ``provider`` and its ``event``, or the first of ``provision.event_types``),
else the first ``platform_event`` one (its ``event``). On that assignment, one ``completed``
run of the orchestrator (``plan``):

* ``trigger_context`` ``{type, provider, event}`` as the delivery path writes it;
* ``messages``: the prompt, one ``consult_agent`` call per other home member (at most two) with
  its answer, and the final response, so the run page draws more than one agentic step;
* one completed child run per consulted member (``parent_run_id``, a ``delegation`` consult
  ``trigger_payload``, the shape ``agent_chat`` writes): the handovers.

A fleet that is not seeded, has no home member or no event-driven assignment is answered with
its reason (``skipped``), never given a trigger. No model call.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

SEEDED_BY = {"seeded_by": "lab"}
#: The built-in CTEM fleets, by slug (``app/agents/builtin_fleets.py``, ``ctem_ai_security.py``):
#: the generic CTEM loop and two of its use cases (CTEM-31, #1048).
FLEETS: tuple[str, ...] = ("ctem-loop", "ctem-vuln-prioritization", "ctem-ai-security")
#: The trigger types that make a run event-driven, the integration event first.
EVENT_TRIGGERS: tuple[str, ...] = ("integration_event", "platform_event")
#: Members the orchestrator consults in the seeded trace.
MAX_CONSULTS = 2
#: How long before the seed the run started: older than the failed run of ``seed_ctem_fleet_optin``
#: (``FAILED_AGE``, 30 minutes) so the failing fleet's latest run stays that failure (health ``failing``).
STARTED_AGO = timedelta(minutes=50)


def event_name(trigger_type: str, config: dict | None) -> str | None:
    """The event an assignment subscribes to, read from its ``trigger_config``; ``None`` when it names none."""
    config = config or {}
    raw = config.get("event")
    if isinstance(raw, list):
        raw = next((e for e in raw if isinstance(e, str) and e), None)
    if isinstance(raw, str) and raw:
        return raw
    if trigger_type == "integration_event":
        provision = config.get("provision") if isinstance(config.get("provision"), dict) else {}
        types = provision.get("event_types")
        if isinstance(types, list):
            return next((e for e in types if isinstance(e, str) and e), None)
    return None


def pick_trigger(assignments: list[dict]) -> dict | None:
    """The first event-driven assignment (integration event first), with its provider and event; else ``None``.

    *assignments*: ``{id, name, agent_id, trigger_type, trigger_config}`` of the fleet's home members.
    """
    for trigger_type in EVENT_TRIGGERS:
        for row in assignments:
            if row["trigger_type"] != trigger_type:
                continue
            config = row.get("trigger_config") or {}
            event = event_name(trigger_type, config)
            provider = config.get("provider") if trigger_type == "integration_event" else None
            if event and (provider or trigger_type == "platform_event"):
                return {**row, "event": event, "provider": provider}
    return None


def plan(fleet: str, trigger: dict, orchestrator: str, members: list[tuple[str, str]], now: datetime) -> dict:
    """The orchestrator run and its child runs (``AssignmentRun`` columns), times relative to *now*.

    *members*: ``(agent id, name)`` of the home members other than the orchestrator.
    """
    consulted = members[:MAX_CONSULTS]
    started = now - STARTED_AGO
    context = {"type": trigger["trigger_type"], "event": trigger["event"], **SEEDED_BY}
    if trigger.get("provider"):
        context["provider"] = trigger["provider"]
    source = f"{trigger['provider']} {trigger['event']}" if trigger.get("provider") else trigger["event"]
    calls = [
        {"id": f"call_lab_consult_{i}", "name": "consult_agent",
         "arguments": {"agent_id": agent, "question": f"Seeded by the lab: your read of the {source} event for {fleet}."}}
        for i, (agent, _name) in enumerate(consulted)
    ]
    messages: list[dict] = [{"role": "user", "content": f"Seeded by the lab: the {source} event arrived ({trigger['name']})."}]
    if calls:
        messages.append({"role": "assistant", "content": "Handing the event to the fleet's members.", "tool_calls": calls})
        messages += [
            {"role": "tool", "name": "consult_agent", "tool_call_id": call["id"],
             "content": f"{name}: seeded by the lab, the event is relevant and handled."}
            for call, (_agent, name) in zip(calls, consulted, strict=True)
        ]
    messages.append({"role": "assistant", "content": f"Seeded by the lab: {fleet} handled the {source} event."})
    run = {
        "assignment_id": trigger["id"], "agent_id": orchestrator, "status": "completed", "error": None,
        "trigger_payload": {**SEEDED_BY, "event": trigger["event"]}, "trigger_context": context,
        "messages": messages,
        "actions_taken": [{"tool": "consult_agent", "arguments": call["arguments"], "source": "builtin"} for call in calls],
        "result_summary": f"Seeded by the lab: {fleet} handled the {source} event.",
        "created_at": started, "started_at": started, "completed_at": started + timedelta(minutes=3),
    }
    children = [
        {
            "assignment_id": None, "agent_id": agent, "status": "completed", "error": None,
            "trigger_payload": {"trigger_type": "delegation", "delegation_type": "consult",
                                "parent_agent_id": orchestrator, "task": call["arguments"]["question"], **SEEDED_BY},
            "result_summary": f"{name}: seeded by the lab, the event is relevant and handled.",
            "created_at": started + timedelta(minutes=1), "started_at": started + timedelta(minutes=1),
            "completed_at": started + timedelta(minutes=2),
        }
        for call, (agent, name) in zip(calls, consulted, strict=True)
    ]
    return {"run": run, "children": children}


async def _fleet_rows(db, fleet) -> tuple[str | None, list[tuple[str, str]], list[dict]]:
    """The orchestrator id, the other home members ``(id, name)`` and their assignments."""
    from sqlalchemy import select

    from app.models.agent import Agent
    from app.models.assignment import Assignment
    from app.models.fleet import FLEET_ROLE_MEMBER, FleetMember

    homes = (
        await db.execute(
            select(Agent.id, Agent.name)
            .join(FleetMember, FleetMember.agent_id == Agent.id)
            .where(FleetMember.fleet_id == fleet.id, FleetMember.role == FLEET_ROLE_MEMBER)
            .order_by(Agent.name)
        )
    ).all()
    root = str(fleet.orchestrator_agent_id) if fleet.orchestrator_agent_id else None
    ids = [str(agent) for agent, _name in homes]
    rows = (
        await db.execute(
            select(Assignment.id, Assignment.name, Assignment.agent_id, Assignment.trigger_type, Assignment.trigger_config)
            .where(Assignment.agent_id.in_([agent for agent, _name in homes]))
            .order_by((Assignment.agent_id != fleet.orchestrator_agent_id), Assignment.name)
        )
    ).all() if homes else []
    assignments = [
        {"id": str(a), "name": n, "agent_id": str(agent), "trigger_type": t, "trigger_config": c}
        for a, n, agent, t, c in rows
    ]
    members = [(str(agent), name) for agent, name in homes if str(agent) != root]
    return (root if root in ids else None), members, assignments


async def seed(user_id: str) -> dict:
    """Create the runs; answer ``{"seeded": [{fleet id, name, run, children, trigger}], "skipped": {name: why}}``."""
    from sqlalchemy import select

    import app.models  # noqa: F401 - every table, so the runs' foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.assignment import AssignmentRun
    from app.models.fleet import Fleet

    user = uuid.UUID(user_id)
    answer: dict = {"seeded": [], "skipped": {}}
    async with async_session() as db:
        found = {f.slug: f for f in (await db.execute(select(Fleet).where(Fleet.slug.in_(FLEETS)))).scalars()}
        now = datetime.now(UTC)
        for slug in FLEETS:
            fleet = found.get(slug)
            if fleet is None:
                answer["skipped"][slug] = "the deployment does not seed this built-in fleet"
                continue
            orchestrator, members, assignments = await _fleet_rows(db, fleet)
            trigger = pick_trigger(assignments)
            if orchestrator is None:
                answer["skipped"][fleet.name] = "no orchestrator among its home members"
                continue
            if trigger is None:
                answer["skipped"][fleet.name] = "no assignment of its members subscribes to an integration or platform event"
                continue
            # The run belongs to the agent of the assignment it starts (the orchestrator's first, by order).
            runner = trigger["agent_id"]
            others = [(agent, name) for agent, name in members if agent != runner]
            rows = plan(fleet.name, trigger, runner, others, now)
            run = AssignmentRun(**{**rows["run"], "agent_id": uuid.UUID(runner),
                                   "assignment_id": uuid.UUID(trigger["id"])}, user_id=user)
            db.add(run)
            await db.flush()
            children = [
                AssignmentRun(**{**child, "agent_id": uuid.UUID(child["agent_id"])}, parent_run_id=run.id, user_id=user)
                for child in rows["children"]
            ]
            db.add_all(children)
            await db.flush()
            answer["seeded"].append({
                "id": str(fleet.id), "name": fleet.name, "run": str(run.id),
                "children": [str(c.id) for c in children],
                "trigger": {k: v for k, v in rows["run"]["trigger_context"].items() if k != "seeded_by"},
            })
        await db.commit()
    await engine.dispose()
    return answer


def main() -> None:
    user_id, out = sys.argv[1:3]
    Path(out).write_text(json.dumps(asyncio.run(seed(user_id))))


if __name__ == "__main__":
    main()
