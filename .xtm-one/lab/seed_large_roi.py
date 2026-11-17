"""The lab's fleet ROI figures of 1,000 and more, under ``LAB_FIGURES_SCALE=large`` (#10749).

Started by ``lab.py`` (``seed_large_roi``) with the backend's interpreter, from ``backend/`` and with
the backend's environment. Every other lab fleet stays under 1,000 on its Value figures (the Lab
sample fleet: a few hours, a few hundred USD, 3 runs, 12 units) and no fleet with ROI records
stores a quota estimate, so the K and M compaction of the fleet About Value and Quota tiles, of the
ROI tab and of the fleet PDF datasheet rendered only through hand edits. No API writes a
``roi_run_records`` row or an ``llm_traces`` row, so this writes them through the backend's own
models and writers, on the two fleets *lab.py* created (one member each):

* the large fleet: ``LARGE_RECORDS`` accepted records of ``UNITS_PER_RECORD`` units over the last
  ``RECORD_DAYS`` days, so runs and units read in K, hours saved in K and the value in M; no model
  spend, so the net value is the value;
* the negative fleet: ``NEGATIVE_RECORDS`` records of one unit, and ``SPEND_TRACES`` LLM traces of
  its member costing ``TRACE_COST`` each, so the AI cost (``roi_spend``) exceeds the value and the
  net value is negative in M; the touched daily rollups are marked stale and rebuilt;
* on both, a documentation version whose quota lines sum to 1,000 and more runs a month.

Arguments: the administrator's id, the large fleet id and its member, the negative fleet id and its
member, and the JSON file the counts are written to. The fleets are new at every lab start.
No model call.

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
#: A built-in catalog unit (20 manual minutes likely, 3 of review, the analyst rate).
UNIT = "threat_report_triaged"
RECORD_DAYS = 28
LARGE_RECORDS = 1200
UNITS_PER_RECORD = 40
NEGATIVE_RECORDS = 1050
SPEND_TRACES = 30
TRACE_COST = 50_000.0
#: One record in REJECTED_EVERY is rejected, so the review counts are not all accepted.
REJECTED_EVERY = 50

SECTIONS = {"purpose": "Seeded by the lab: a fleet whose Value figures read in thousands and millions."}


def quota_lines(index: int) -> list[dict]:
    """The quota lines of fleet *index* (0 large, 1 negative); each sums to 1,000 and more runs."""
    if index == 0:
        return [{"trigger": "Lab report triage", "basis": "Estimate: one run per tick of the schedule over 30 days",
                 "runs_min": 1440, "runs_max": 1440, "kinds": ["schedule"]}]
    return [{"trigger": "Lab enrichment sweep", "basis": "Estimate: between 1,100 and 3,600 runs over 30 days",
             "runs_min": 1100, "runs_max": 3600, "kinds": ["schedule"]}]


def record_rows(agent: str, fleet: str, user: str, count: int, units: int, now: datetime) -> list[dict]:
    """The ``RoiRunRecord`` columns of *count* records of *units* units, spread over RECORD_DAYS days."""
    step = timedelta(days=RECORD_DAYS) / count
    rows = []
    for n in range(count):
        at = now - timedelta(minutes=5) - step * n
        rows.append({
            "agent_id": uuid.UUID(agent), "fleet_id": uuid.UUID(fleet), "unit": UNIT, "units": units,
            "outcome": "rejected" if n % REJECTED_EVERY == REJECTED_EVERY - 1 else "accepted",
            "llm_cost": 0.0, "duration_s": 120.0, "created_at": at,
            "reviewed_by_user_id": uuid.UUID(user), "reviewed_at": at + timedelta(minutes=10),
        })
    return rows


def trace_rows(agent: str, user: str, now: datetime) -> list[dict]:
    """The ``LLMTrace`` columns of the negative fleet's spend: one trace a day on past days."""
    rows = []
    for n in range(SPEND_TRACES):
        started = (now - timedelta(days=1 + n % (RECORD_DAYS - 1))).replace(hour=10, minute=n, second=0, microsecond=0)
        rows.append({
            "agent_id": uuid.UUID(agent), "user_id": uuid.UUID(user), "attributed_user_id": uuid.UUID(user),
            "attribution": "user", "source": "assignment", "name": "lab large ROI spend", "model": "gpt-4o",
            "started_at": started, "ended_at": started + timedelta(seconds=2), "duration_ms": 2000.0,
            "input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200,
            "input_cost": TRACE_COST, "output_cost": 0.0, "total_cost": TRACE_COST,
            "status": "ok", "error_message": None, "level": "DEFAULT", "metadata_json": SEEDED_BY,
            "department_resolved_at": started,
        })
    return rows


async def seed(author: str, large: tuple[str, str], negative: tuple[str, str]) -> dict:
    """Write the documentation, the records, the traces and the rollups; answer the counts."""
    import app.models  # noqa: F401 - every table, so the foreign keys resolve
    from sqlalchemy import insert

    from app.core.database import async_session, engine
    from app.models.fleet import Fleet
    from app.models.llm_trace import LLMSpan, LLMTrace
    from app.models.roi import RoiRunRecord
    from app.models.user import User
    from app.services.fleet_docs import save_documentation
    from app.services.trace_storage import mark_daily_stats_stale, run_daily_stats_backfill

    now = datetime.now(UTC)
    answer: dict = {"quota": {}}
    traces = trace_rows(negative[1], author, now)
    async with async_session() as db:
        user = await db.get(User, uuid.UUID(author))
        if user is None:
            raise RuntimeError(f"no user {author}")
        for index, (fleet_id, _member) in enumerate((large, negative)):
            fleet = await db.get(Fleet, uuid.UUID(fleet_id))
            if fleet is None:
                raise RuntimeError(f"no fleet {fleet_id}")
            lines = quota_lines(index)
            await save_documentation(
                db, fleet, dict(SECTIONS), author=user.id, base_version=None, structured={"quota": lines}
            )
            answer["quota"][fleet_id] = sum(q["runs_max"] for q in lines)
        await db.execute(insert(RoiRunRecord), [
            *record_rows(large[1], large[0], author, LARGE_RECORDS, UNITS_PER_RECORD, now),
            *record_rows(negative[1], negative[0], author, NEGATIVE_RECORDS, 1, now),
        ])
        for row in traces:
            trace = LLMTrace(**row)
            db.add(trace)
            await db.flush()
            db.add(LLMSpan(
                trace_id=trace.id, type="GENERATION", name="llm", model=row["model"], level=row["level"],
                status_message=None, started_at=row["started_at"], ended_at=row["ended_at"],
                duration_ms=row["duration_ms"], input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"], input_cost=row["input_cost"], output_cost=row["output_cost"],
                total_cost=row["total_cost"],
            ))
        # A day another seed already rolled up is rebuilt with these traces; a day without is built.
        await mark_daily_stats_stale(db, {r["started_at"].date() for r in traces})
        await db.commit()
    async with async_session() as db:
        rollups = await run_daily_stats_backfill(db, retention_days=90, max_days=RECORD_DAYS + 2)
    await engine.dispose()
    answer.update(
        records=LARGE_RECORDS + NEGATIVE_RECORDS, units=LARGE_RECORDS * UNITS_PER_RECORD + NEGATIVE_RECORDS,
        traces=len(traces), spend=TRACE_COST * len(traces), daily_stats_rows=rollups,
    )
    return answer


def main() -> None:
    author, large_fleet, large_member, negative_fleet, negative_member, out = sys.argv[1:7]
    result = asyncio.run(seed(author, (large_fleet, large_member), (negative_fleet, negative_member)))
    Path(out).write_text(json.dumps(result))


if __name__ == "__main__":
    main()
