"""The lab's ROI records: runs and a work item whose outcome review row renders on real rows.

Started by ``lab.py`` (``seed_roi_records``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once the backend answers: a
``roi_run_records`` row is written by the platform at a run's or a work item's
end (``services/roi_records.py``) and no API creates one, so this writes them
through the platform's models. Arguments: the agent and the assignment the runs
belong to (a lab-owned pair, so the agent's and the assignment's Activity tabs
list them), the user who owns the runs and the item (the lab administrator), a
done work item created through the API (``-`` for none), the JSON file the ids
are written to and, optionally, a fleet the records carry (``fleet_id``).

* four ``completed`` runs of the assignment, unit ``threat_report_triaged``, one
  record each: unreviewed (``outcome`` NULL), ``accepted``, ``modified`` with its
  measured ``review_min`` and a note, ``rejected`` with a note;
* one ``per_work_item`` record on the done work item, unreviewed;
* with the work item (the ROI review seed, not the CTEM fleet one), the history of
  :data:`HISTORY_WEEKS` ISO weeks before the seed: :data:`HISTORY_PER_WEEK` completed runs a
  week, accepted and unreviewed mixed, no fleet, so ``GET /roi?window=90d`` answers a weekly
  ``trend`` of more than six points and the Value page's "Value over time" chart
  (``[data-testid=value-trend]``) draws with no fetch override (#10067). The seed answers
  ``trend_weeks``, the distinct weeks (Monday 00:00 UTC) its records fall in, and ``lab.py``
  logs ``seeded ROI trend weeks: N``.

So ``GET /assignments/runs/{id}`` answers a ``roi`` block with ``can_review`` true
for the administrator, ``GET /assignments/runs/all?review=unreviewed`` answers the
unreviewed run, and the work item dialog shows its outcome row, with no fetch
override (issue #9394).

With a fleet (``seed_ctem_fleet_roi``, the CTEM fleet seeded On) the four runs'
records carry it, so ``GET /roi?window=7d`` answers the fleet in ``fleets`` and
its command center tile renders the review rate and non-zero figures (#9831).

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

#: (label, minutes before the seed, units, outcome, review_min, note).
RUNS: tuple[tuple[str, int, int, str | None, float | None, str | None], ...] = (
    ("unreviewed", 40, 3, None, None, None),
    ("accepted", 30, 2, "accepted", None, None),
    ("modified", 20, 4, "modified", 12.0, "Seeded by the lab: two reports needed a second pass."),
    ("rejected", 10, 1, "rejected", None, "Seeded by the lab: the triage missed the sector."),
)

#: The weeks of back-dated history before the seed's own week, and the runs of each (#10067).
HISTORY_WEEKS = 10
HISTORY_PER_WEEK = 2


def history_plan(now: datetime) -> list[dict]:
    """The back-dated runs of the trend, oldest first: :data:`HISTORY_PER_WEEK` a week for
    :data:`HISTORY_WEEKS` weeks before *now*, units varying by week, accepted and unreviewed mixed."""
    rows = []
    for week in range(HISTORY_WEEKS, 0, -1):
        for slot in range(HISTORY_PER_WEEK - 1, -1, -1):  # the later slot is the earlier run
            started = now - timedelta(weeks=week, hours=3 + 26 * slot)
            units = 2 + (week * 3 + slot) % 5
            outcome = "accepted" if (week + slot) % 2 == 0 else None
            rows.append({
                "label": f"history-w{week}-{slot}",
                "run": {
                    "status": "completed", "trigger_payload": SEEDED_BY,
                    "trigger_context": {"type": "manual", **SEEDED_BY},
                    "result_summary": f"Seeded by the lab: {units} threat reports triaged ({week} weeks ago).",
                    "created_at": started, "started_at": started, "completed_at": started + timedelta(minutes=2),
                },
                "record": {
                    "unit": UNIT, "units": units, "outcome": outcome, "review_min": None, "review_note": None,
                    "llm_cost": 0.04 * units, "duration_s": 120.0, "created_at": started + timedelta(minutes=2),
                    "reviewed_at": None if outcome is None else started + timedelta(minutes=5),
                },
            })
    return rows


def week_of(at: datetime) -> str:
    """The Monday (UTC) of *at*'s week, as the ROI report buckets its trend."""
    at = at.astimezone(UTC)
    return (at.date() - timedelta(days=at.weekday())).isoformat()


def plan(now: datetime) -> list[dict]:
    """The run and record columns of every seeded run, oldest first, times relative to *now*."""
    rows = []
    for label, minutes, units, outcome, review_min, note in RUNS:
        started = now - timedelta(minutes=minutes)
        rows.append({
            "label": label,
            "run": {
                "status": "completed", "trigger_payload": SEEDED_BY,
                "trigger_context": {"type": "manual", **SEEDED_BY},
                "result_summary": f"Seeded by the lab: {units} threat reports triaged ({label} outcome).",
                "created_at": started, "started_at": started, "completed_at": started + timedelta(minutes=2),
            },
            "record": {
                "unit": UNIT, "units": units, "outcome": outcome, "review_min": review_min,
                "review_note": note, "llm_cost": 0.04 * units, "duration_s": 120.0,
                "created_at": started + timedelta(minutes=2),
                "reviewed_at": None if outcome is None else started + timedelta(minutes=5),
            },
        })
    return rows


async def seed(
    agent_id: str, assignment_id: str, user_id: str, work_item_id: str | None, fleet_id: str | None = None,
) -> dict[str, object]:
    """Create the runs and the records; answer the run ids by outcome and the work item's record.

    *work_item_id* ``None``: no work item record. *fleet_id*: every run record carries the fleet.
    """
    import app.models  # noqa: F401 - every table, so the foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.assignment import AssignmentRun
    from app.models.roi import RoiRunRecord

    agent, assignment, user = uuid.UUID(agent_id), uuid.UUID(assignment_id), uuid.UUID(user_id)
    fleet = uuid.UUID(fleet_id) if fleet_id else None
    now = datetime.now(UTC)
    runs: dict[str, str] = {}
    record: str | None = None
    history = history_plan(now) if work_item_id else []
    weeks = {week_of(row["record"]["created_at"]) for row in [*history, *plan(now)]}
    async with async_session() as db:
        for row in history:
            run = AssignmentRun(agent_id=agent, assignment_id=assignment, user_id=user, **row["run"])
            db.add(run)
            await db.flush()
            reviewer = user if row["record"]["outcome"] is not None else None
            db.add(RoiRunRecord(run_id=run.id, agent_id=agent, reviewed_by_user_id=reviewer, **row["record"]))
        for row in plan(now):
            run = AssignmentRun(agent_id=agent, assignment_id=assignment, user_id=user, **row["run"])
            db.add(run)
            await db.flush()
            reviewer = user if row["record"]["outcome"] is not None else None
            db.add(RoiRunRecord(run_id=run.id, agent_id=agent, fleet_id=fleet, reviewed_by_user_id=reviewer,
                                **row["record"]))
            runs[row["label"]] = str(run.id)
        item = None
        if work_item_id:
            item = RoiRunRecord(
                work_item_id=uuid.UUID(work_item_id), agent_id=agent, unit=UNIT, units=1, outcome=None,
                llm_cost=0.02, duration_s=60.0, created_at=now,
            )
            db.add(item)
        await db.commit()
        if item is not None:
            record = str(item.id)
    await engine.dispose()
    return {"runs": runs, "work_item_record": record, "trend_weeks": len(weeks)}


def main() -> None:
    agent_id, assignment_id, user_id, work_item_id, out = sys.argv[1:6]
    fleet_id = sys.argv[6] if len(sys.argv) > 6 else None
    item = None if work_item_id == "-" else work_item_id
    Path(out).write_text(json.dumps(asyncio.run(seed(agent_id, assignment_id, user_id, item, fleet_id))))


if __name__ == "__main__":
    main()
