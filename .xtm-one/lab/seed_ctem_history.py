"""Recorded CTEM posture days of the administrator, for ``[service.lab-ctem]``.

Started by ``lab.py`` (``seed_ctem_sample``) with the backend's interpreter, from ``backend/``
and with the backend's environment. No API writes a past snapshot: ``GET /ctem-metrics/posture``
records today only. This writes one ``ctem_posture_snapshots`` row per earlier day of
``ctem_sample.DAYS_AGO``, scored by the platform's own :func:`app.services.ctem_scoring.compute_posture`
with the sample's present products, so the history and the live answer agree. Arguments: the
administrator's id and the JSON file the row ids are written to.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import ctem_sample  # beside this file


def plan(today: date) -> list[dict]:
    """The ``CtemPostureSnapshot`` columns of every recorded day, oldest first."""
    from app.services.ctem_scoring import compute_posture

    rows = []
    for days_ago in sorted((d for d in ctem_sample.DAYS_AGO if d > 0), reverse=True):
        facts = ctem_sample.stage_facts(days_ago)
        posture = compute_posture(facts, set(ctem_sample.PRESENT), ctem_sample.VERTICAL)
        rows.append({
            "vertical": ctem_sample.VERTICAL,
            "snapshot_date": today - timedelta(days=days_ago),
            "posture_score": posture["posture_score"],
            "stage_metrics": {
                stage["key"]: {"score": stage["score"], **facts.get(stage["key"], {})}
                for stage in posture["stages"]
            },
        })
    return rows


async def seed(user_id: str) -> list[str]:
    """Write the recorded days; answer their ids, oldest first."""
    import uuid
    from datetime import UTC, datetime

    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.ctem_posture_snapshot import CtemPostureSnapshot

    user = uuid.UUID(user_id)
    rows = [CtemPostureSnapshot(user_id=user, **row) for row in plan(datetime.now(UTC).date())]
    async with async_session() as db:
        db.add_all(rows)
        await db.commit()
    await engine.dispose()
    return [str(row.id) for row in rows]


def main() -> None:
    user_id, out = sys.argv[1:3]
    Path(out).write_text(json.dumps(asyncio.run(seed(user_id))))


if __name__ == "__main__":
    main()
