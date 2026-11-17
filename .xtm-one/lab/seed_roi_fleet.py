"""The lab's fleet ROI records: counted runs of "Lab sample fleet", so the ROI report has a fleet row.

Started by ``lab.py`` (``seed_roi_fleet``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once the backend answers and
the fleet declares its ROI unit (``PUT /api/v1/roi/declarations?subject_type=fleet``).
A ``roi_run_records`` row is written by the platform at a run's end
(``services/roi_records.py``) and no API creates one, so this writes them
through the platform's models. Arguments: the fleet's orchestrator agent and a
lab assignment of it (so the agent's and the assignment's Activity tabs list the
runs), the user who owns the runs (the lab administrator), the fleet, and the
JSON file the ids are written to.

* three ``completed`` runs of the assignment, unit ``threat_report_triaged``,
  each with one record carrying the fleet (``fleet_id``) and an ``accepted``
  outcome, spread over the last days of the 30-day window.

So ``GET /api/v1/roi?window=30d`` answers the fleet in ``fleets`` and the
Analytics Outcomes table renders a fleet row with no fetch override (issue #9734).

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
UNIT = "threat_report_triaged"

#: (days before the seed, units counted by the run).
RUNS: tuple[tuple[int, int], ...] = ((6, 5), (3, 3), (1, 4))


def plan(now: datetime) -> list[dict]:
    """The run and record columns of every seeded run, oldest first, times relative to *now*."""
    rows = []
    for days, units in RUNS:
        started = now - timedelta(days=days)
        rows.append({
            "run": {
                "status": "completed", "trigger_payload": SEEDED_BY,
                "trigger_context": {"type": "manual", **SEEDED_BY},
                "result_summary": f"Seeded by the lab: {units} threat reports triaged by the fleet.",
                "created_at": started, "started_at": started, "completed_at": started + timedelta(minutes=3),
            },
            "record": {
                "unit": UNIT, "units": units, "outcome": "accepted", "llm_cost": 0.05 * units,
                "duration_s": 180.0, "created_at": started + timedelta(minutes=3),
                "reviewed_at": started + timedelta(minutes=10),
            },
        })
    return rows


async def seed(agent_id: str, assignment_id: str, user_id: str, fleet_id: str) -> dict[str, object]:
    """Create the runs and their fleet records; answer the run ids."""
    import app.models  # noqa: F401 - every table, so the foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.assignment import AssignmentRun
    from app.models.roi import RoiRunRecord

    agent, assignment, user, fleet = (uuid.UUID(v) for v in (agent_id, assignment_id, user_id, fleet_id))
    runs: list[str] = []
    async with async_session() as db:
        for row in plan(datetime.now(UTC)):
            run = AssignmentRun(agent_id=agent, assignment_id=assignment, user_id=user, **row["run"])
            db.add(run)
            await db.flush()
            db.add(RoiRunRecord(run_id=run.id, agent_id=agent, fleet_id=fleet, reviewed_by_user_id=user,
                                **row["record"]))
            runs.append(str(run.id))
        await db.commit()
    await engine.dispose()
    return {"runs": runs}


def main() -> None:
    agent_id, assignment_id, user_id, fleet_id, out = sys.argv[1:6]
    Path(out).write_text(json.dumps(asyncio.run(seed(agent_id, assignment_id, user_id, fleet_id))))


if __name__ == "__main__":
    main()
