"""The lab's runs paused for a person: gated tool calls and a question, on two fleets.

Started by ``lab.py`` (``seed_awaiting_human_runs``) with the backend's interpreter,
from ``backend/`` and with the backend's environment, once the backend answers and
the corporate and built-in fleets are seeded: no API pauses a run on the per-tool
approval gate or on ``request_human_input`` without a model turn, so this writes
the ``AssignmentRun`` rows through the platform's model. Arguments: the user who
owns every run (the lab administrator, so ``run_visible_filter`` shows them), the
id of "Lab sample corporate fleet" (empty when it was not seeded), and the JSON
file the answer is written to.

Five runs per fleet (``RUNS``), on its home members, every one ``awaiting_human``:

* ``two-approvals``: two ``pending_approvals`` on two integrations (GitHub, Slack);
* ``long-question``: a ``human_input_question`` over 150 characters;
* ``company-managed``: ``is_company_managed`` with one pending approval;
* ``decided``: one approval already decided (``approval_decisions`` set, so the
  API answers ``approval_answered`` true and the pending panels leave it out);
* ``older``: one pending approval, paused 30 hours ago.

The fleets: the corporate fleet, and the built-in fleet "CTEM loop"
(``ctem-loop``, CTEM-31) when the deployment seeds it, else the first
built-in fleet (the lab's own, ``seed_builtin_fleet.py``). Nothing resumes them:
a pause is resumed only by the respond endpoints, which emit the resume outbox
event, and the stale-run sweeps never touch ``awaiting_human``. No model call.

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
BUILTIN_FLEET_SLUG = "ctem-loop"
#: The question of the ``long-question`` run: over 150 characters, so the card's wrapping renders.
LONG_QUESTION = (
    "Seeded by the lab: the quarterly exposure review found 14 internet-facing hosts that match the "
    "advisory, but 3 of them belong to a subsidiary whose change window is not known to this fleet. "
    "Should I open remediation tickets for all 14 now, or only for the 11 the security team owns and "
    "ask the subsidiary's contact first?"
)


def _proposal(key: str, tool: str, source: str, description: str, arguments: dict) -> dict:
    """One ``pending_approvals`` entry, the shape ``app.tools.approval.build_action_proposal`` writes."""
    return {
        "tool_call_id": f"call_lab_{key}",
        "tool_name": tool,
        "tool_description": description,
        "arguments": arguments,
        "input_schema": {
            "type": "object",
            "properties": {name: {"type": "string", "description": f"The {name} of the call."} for name in arguments},
        },
        "source": source,
        "tool_entity_id": None,
    }


GITHUB_ISSUE = ("create_github_issue", "integration:github", "Create an issue in a GitHub repository.",
                {"repo": "acme/infrastructure", "title": "Patch the 11 exposed edge hosts"})
SLACK_POST = ("send_slack_message", "integration:slack", "Post a message to a Slack channel.",
              {"channel": "#secops", "text": "Remediation plan ready for review."})

#: (key, hours before the seed, company-managed, question, proposals as (tool, source, description, args), decided).
RUNS: tuple[tuple[str, float, bool, str | None, tuple[tuple, ...], bool], ...] = (
    ("two-approvals", 0.5, False, None, (GITHUB_ISSUE, SLACK_POST), False),
    ("long-question", 1.0, False, LONG_QUESTION, (), False),
    ("company-managed", 2.0, True, None, (SLACK_POST,), False),
    ("decided", 3.0, False, None, (GITHUB_ISSUE,), True),
    ("older", 30.0, False, None, (GITHUB_ISSUE,), False),
)


def plan(fleet: str, agents: list[str], now: datetime) -> list[dict]:
    """The ``AssignmentRun`` columns of *fleet*'s paused runs, spread over *agents* (its home members)."""
    rows = []
    for index, (key, hours, company, question, proposals, decided) in enumerate(RUNS):
        paused = now - timedelta(hours=hours)
        pending = [
            _proposal(f"{fleet[:8]}_{key}_{n}", *proposal) for n, proposal in enumerate(proposals)
        ] or None
        rows.append({
            "agent_id": agents[index % len(agents)], "status": "awaiting_human",
            "is_company_managed": company,
            "trigger_payload": {**SEEDED_BY, "case": key},
            "trigger_context": {"type": "manual", "case": key, **SEEDED_BY},
            "human_input_question": question,
            "pending_approvals": pending,
            "approval_decisions": (
                {pending[0]["tool_call_id"]: {"decision": "approve", "rejection_reason": None}}
                if decided and pending else None
            ),
            "result_summary": f"Seeded by the lab: a run paused for a person ({key}).",
            "created_at": paused - timedelta(minutes=2), "started_at": paused - timedelta(minutes=2),
            "completed_at": None,
        })
    return rows


async def _home_members(db, fleet_id: uuid.UUID) -> list[str]:
    """The fleet's home member agents, its orchestrator first."""
    from sqlalchemy import select

    from app.models.fleet import FLEET_ROLE_MEMBER, Fleet, FleetMember

    fleet = await db.get(Fleet, fleet_id)
    members = (
        await db.execute(
            select(FleetMember.agent_id).where(
                FleetMember.fleet_id == fleet_id,
                FleetMember.role == FLEET_ROLE_MEMBER,
                FleetMember.agent_id.isnot(None),
            )
        )
    ).scalars().all()
    ids = [str(agent) for agent in members]
    root = str(fleet.orchestrator_agent_id) if fleet and fleet.orchestrator_agent_id else None
    return sorted(ids, key=lambda agent: agent != root)


async def seed(user_id: str, corporate: str) -> list[dict]:
    """Create the runs on each fleet found; answer one entry per fleet: id, name, run ids by case."""
    from sqlalchemy import select

    import app.models  # noqa: F401 - every table, so the runs' foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.assignment import AssignmentRun
    from app.models.fleet import Fleet

    user = uuid.UUID(user_id)
    answer: list[dict] = []
    async with async_session() as db:
        fleets: list[Fleet] = []
        if corporate:
            row = await db.get(Fleet, uuid.UUID(corporate))
            if row is not None:
                fleets.append(row)
        builtin = (
            await db.execute(
                select(Fleet)
                .where(Fleet.is_builtin.is_(True))
                .order_by((Fleet.slug != BUILTIN_FLEET_SLUG), Fleet.slug)
                .limit(1)
            )
        ).scalar_one_or_none()
        if builtin is not None:
            fleets.append(builtin)
        now = datetime.now(UTC)
        for fleet in fleets:
            agents = await _home_members(db, fleet.id)
            if not agents:
                continue
            rows = plan(str(fleet.id), agents, now)
            runs = [
                AssignmentRun(**{**row, "agent_id": uuid.UUID(row["agent_id"])}, user_id=user) for row in rows
            ]
            db.add_all(runs)
            await db.flush()
            answer.append({
                "id": str(fleet.id), "name": fleet.name,
                "runs": {row["trigger_context"]["case"]: str(run.id) for row, run in zip(rows, runs, strict=True)},
            })
        await db.commit()
    await engine.dispose()
    if not answer:
        raise RuntimeError("no fleet with a home member: neither the corporate nor a built-in fleet")
    return answer


def main() -> None:
    user_id, corporate, out = sys.argv[1:4]
    Path(out).write_text(json.dumps(asyncio.run(seed(user_id, corporate))))


if __name__ == "__main__":
    main()
