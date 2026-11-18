"""The lab's ROI under-k records: one agent and one fleet backed by fewer than k people.

Started by ``lab.py`` (``seed_roi_under_k``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once the backend answers, the
fleet declares its ROI unit and both are shared read-only with the peer. A
``roi_run_records`` row is written by the platform at a run's end
(``services/roi_records.py``) and no API creates one, so this writes them
through the platform's models. Arguments: the agent (the fleet's only member),
a lab assignment of it, the lab administrator, the peer, the fleet, and the
JSON file the ids are written to.

* four ``completed`` runs of the assignment, unit ``threat_report_triaged``,
  two for the administrator and two for the peer (``user_id`` and
  ``attribution`` ``assignment``), each with one ``accepted`` record carrying
  the agent and the fleet.

Two distinct people stay under k (default 5, bounds 2 to 50), so the peer reads
``GET /api/v1/roi?scope=agent|fleet&id=`` with ``figures.suppressed.own_share_only``
and his own runs above 0, with no fetch override (dev:harness-gaps
lab-seed-roi-under-k-own-share).

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

#: (who: "admin" or "peer", days before the seed, units counted by the run).
RUNS: tuple[tuple[str, int, int], ...] = (("admin", 5, 4), ("peer", 4, 3), ("admin", 2, 2), ("peer", 1, 5))


def plan(now: datetime) -> list[dict]:
    """The owner, run and record columns of every seeded run, oldest first, times relative to *now*."""
    rows = []
    for who, days, units in RUNS:
        started = now - timedelta(days=days)
        rows.append({
            "who": who,
            "run": {
                "status": "completed", "trigger_payload": SEEDED_BY,
                "trigger_context": {"type": "manual", **SEEDED_BY},
                "result_summary": f"Seeded by the lab: {units} threat reports triaged under k.",
                "created_at": started, "started_at": started, "completed_at": started + timedelta(minutes=3),
            },
            "record": {
                "unit": UNIT, "units": units, "outcome": "accepted", "llm_cost": 0.05 * units,
                "duration_s": 180.0, "attribution": "assignment", "created_at": started + timedelta(minutes=3),
                "reviewed_at": started + timedelta(minutes=10),
            },
        })
    return rows


async def seed(agent_id: str, assignment_id: str, admin_id: str, peer_id: str, fleet_id: str) -> dict[str, object]:
    """Create the runs and their records; answer the run ids per owner."""
    import app.models  # noqa: F401 - every table, so the foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.assignment import AssignmentRun
    from app.models.roi import RoiRunRecord

    agent, assignment, fleet = (uuid.UUID(v) for v in (agent_id, assignment_id, fleet_id))
    people = {"admin": uuid.UUID(admin_id), "peer": uuid.UUID(peer_id)}
    runs: dict[str, list[str]] = {"admin": [], "peer": []}
    async with async_session() as db:
        for row in plan(datetime.now(UTC)):
            person = people[row["who"]]
            run = AssignmentRun(agent_id=agent, assignment_id=assignment, user_id=person, **row["run"])
            db.add(run)
            await db.flush()
            db.add(RoiRunRecord(run_id=run.id, agent_id=agent, fleet_id=fleet, user_id=person,
                                reviewed_by_user_id=person, **row["record"]))
            runs[row["who"]].append(str(run.id))
        await db.commit()
    await engine.dispose()
    return {"runs": runs}


def main() -> None:
    agent_id, assignment_id, admin_id, peer_id, fleet_id, out = sys.argv[1:7]
    Path(out).write_text(json.dumps(asyncio.run(seed(agent_id, assignment_id, admin_id, peer_id, fleet_id))))


if __name__ == "__main__":
    main()
