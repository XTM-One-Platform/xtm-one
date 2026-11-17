"""The lab's fleet runs: one run per trigger source and per outcome on the seeded fleet.

Started by ``lab.py`` (``seed_fleet_runs``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once the backend answers: no API
creates an ``AssignmentRun`` with a given ``trigger_context`` (a run is started by
its trigger), so this writes them through the platform's model. Arguments: the user
who owns every run (the lab administrator, so ``run_visible_filter`` shows them), a
JSON object of agent name to id (the lab's seeded agents, the fleet's members among
them), and the JSON file the run ids are written to.

Fourteen runs on "Lab sample fleet", spread over the last hour (``RUNS``):

* ``integration_event`` from opencti, openaev, opencrq and github, each with its
  ``provider`` and ``event``;
* ``webhook``, ``work_item``, ``chat_handover``, ``chain``, ``schedule`` and ``manual``;
* one ``failed`` run (an error of the agent's own work), one interrupted run (failed
  with an error carrying an ``INTERRUPTION_MARKERS`` entry of
  ``app/services/run_outcome.py``), one ``cancelled`` run (outcome ``other``) and one
  ``running`` run (outcome ``active``); every other run is ``completed`` (``ok``).

So ``GET /api/v1/fleets/{id}/activity`` answers every trigger and every outcome, and
the CTEM Fleet timeline on ``/`` renders them with no fetch override.

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

#: (member, minutes before the seed, status, trigger_context, error).
RUNS: tuple[tuple[str, int, str, dict, str | None], ...] = (
    ("Researcher", 58, "completed",
     {"type": "integration_event", "provider": "opencti", "event": "report.created"}, None),
    ("Analyst", 54, "completed",
     {"type": "integration_event", "provider": "openaev", "event": "simulation.completed"}, None),
    ("Writer", 50, "completed",
     {"type": "integration_event", "provider": "opencrq", "event": "scenario.updated"}, None),
    ("Researcher", 46, "completed",
     {"type": "integration_event", "provider": "github", "event": "pull_request.opened"}, None),
    ("Analyst", 42, "completed", {"type": "webhook", "scope": "assignment"}, None),
    ("Writer", 38, "completed", {"type": "work_item"}, None),
    ("Orchestrator", 34, "completed", {"type": "chat_handover"}, None),
    ("Researcher", 30, "completed", {"type": "chain"}, None),
    ("Analyst", 26, "completed", {"type": "schedule"}, None),
    ("Writer", 22, "completed", {"type": "manual"}, None),
    ("Researcher", 18, "failed", {"type": "schedule"},
     "Seeded by the lab: the agent's own work failed (the report could not be parsed)."),
    ("Analyst", 14, "failed", {"type": "work_item"},
     "Run marked as failed by stale-assignment-run cleanup. Seeded by the lab: an interruption."),
    ("Writer", 10, "cancelled", {"type": "manual"}, None),
    ("Orchestrator", 2, "running",
     {"type": "integration_event", "provider": "opencti", "event": "indicator.created"}, None),
)


def plan(agents: dict[str, str], now: datetime) -> list[dict]:
    """The ``AssignmentRun`` columns of every seeded run, oldest first, times relative to *now*."""
    rows = []
    for member, minutes, status, context, error in RUNS:
        started = now - timedelta(minutes=minutes)
        rows.append({
            "agent_id": agents[member], "status": status, "error": error,
            "trigger_payload": SEEDED_BY, "trigger_context": {**context, **SEEDED_BY},
            "result_summary": f"Seeded by the lab: a {status} run started by {context['type']}.",
            "created_at": started, "started_at": started,
            "completed_at": None if status == "running" else started + timedelta(minutes=1),
        })
    return rows


async def seed(user_id: str, agents: dict[str, str]) -> list[str]:
    """Create the runs; answer their ids, oldest first."""
    import app.models  # noqa: F401 - every table, so the runs' foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.assignment import AssignmentRun

    user = uuid.UUID(user_id)
    runs = [
        AssignmentRun(**{**row, "agent_id": uuid.UUID(row["agent_id"])}, user_id=user)
        for row in plan(agents, datetime.now(UTC))
    ]
    async with async_session() as db:
        db.add_all(runs)
        await db.commit()
    await engine.dispose()
    return [str(run.id) for run in runs]


def main() -> None:
    user_id, agents, out = sys.argv[1:4]
    Path(out).write_text(json.dumps(asyncio.run(seed(user_id, json.loads(agents)))))


if __name__ == "__main__":
    main()
