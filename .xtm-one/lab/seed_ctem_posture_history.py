"""Ninety days of recorded CTEM posture of the administrator, for every XTM One lab.

Started by ``lab.py`` (``seed_ctem_posture_history``) with the backend's interpreter, from
``backend/`` and with the backend's environment. No API writes a past snapshot: ``GET
/ctem-metrics/posture`` records today only, so a fresh lab answers one day of history and every
trend surface (the stage History card with its 30 / 90 days switch, the command center trend and
deltas) shows only its "no change yet" state. This writes one ``ctem_posture_snapshots`` row per
day of ``DAYS_AGO`` (every 3 days over 90 days, plus the last 5 and today), scored by the
platform's own :func:`app.services.ctem_scoring.compute_posture`, one shape per stage:

- Discovery rises (``/ctem/discovery``: "Stage score +N points since <date>");
- Prioritization falls;
- Scoping holds (``/ctem/scoping``: "unchanged");
- Validation is scored only before day 35 (30 days: no change to show; 90 days: a trend);
- Remediation is scored on one recorded day only (``/ctem/remediation``: "No change to show yet").

A day already recorded is left as it is (insert on conflict do nothing), except today: the live
collection answers no fact in a lab, so a posture read before the seed recorded an empty today,
which the seeded one replaces; ``lab_asgi`` (``LAB_CTEM_POSTURE_HISTORY=1``) then keeps it at every
later read. Arguments: the administrator's id and the JSON file the written dates are written to.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, timedelta
from pathlib import Path

#: The vertical the history feeds: the default one the command center opens on.
VERTICAL = "cyber"
#: The products the recorded days report present.
PRESENT = ("opencti",)
#: The recorded days, in days before today: every 3 days over 90 days, plus the last 5 and today.
DAYS_AGO: tuple[int, ...] = tuple(sorted(set(range(90, 0, -3)) | {0, 1, 2, 3, 4, 5}, reverse=True))
#: Validation is scored on the days at least this old only.
VALIDATION_UNTIL = 35
#: The one recorded day Remediation is scored on.
REMEDIATION_DAY = 1


def stage_facts(days_ago: int) -> dict[str, dict[str, float]]:
    """The facts of one recorded day, read by the cyber scorers; a stage with no fact is not scored."""
    elapsed = 90 - days_ago
    facts: dict[str, dict[str, float]] = {
        "scoping": {"scope_completeness_pct": 70.0},
        "discovery": {"detection_coverage_pct": round(40 + elapsed * 0.3, 1)},
        "prioritization": {"noncritical_share_pct": round(80 - elapsed * 0.25, 1)},
    }
    if days_ago >= VALIDATION_UNTIL:
        facts["validation"] = {"detection_rule_coverage_pct": round(50 + elapsed * 0.4, 1)}
    if days_ago == REMEDIATION_DAY:
        facts["remediation"] = {"playbook_automation_pct": 62.0}
    return facts


def plan(today: date) -> list[dict]:
    """The ``CtemPostureSnapshot`` columns of every recorded day, oldest first."""
    from app.services.ctem_scoring import compute_posture

    rows = []
    for days_ago in DAYS_AGO:
        facts = stage_facts(days_ago)
        posture = compute_posture(facts, set(PRESENT), VERTICAL)
        rows.append({
            "vertical": VERTICAL,
            "snapshot_date": today - timedelta(days=days_ago),
            "posture_score": posture["posture_score"],
            "stage_metrics": {
                stage["key"]: {"score": stage["score"], **facts.get(stage["key"], {})}
                for stage in posture["stages"]
            },
        })
    return rows


async def seed(user_id: str) -> list[str]:
    """Write the recorded days a day already recorded leaves out; answer the dates written, oldest first."""
    import uuid
    from datetime import UTC, datetime

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.ctem_posture_snapshot import CtemPostureSnapshot

    user = uuid.UUID(user_id)
    written: list[str] = []
    async with async_session() as db:
        today = datetime.now(UTC).date()
        for row in plan(today):
            insert = pg_insert(CtemPostureSnapshot).values(id=uuid.uuid4(), user_id=user, **row)
            if row["snapshot_date"] == today:
                insert = insert.on_conflict_do_update(
                    constraint="uq_ctem_snapshot_user_vertical_date",
                    set_={"posture_score": row["posture_score"], "stage_metrics": row["stage_metrics"],
                          "updated_at": datetime.now(UTC)},
                )
            else:
                insert = insert.on_conflict_do_nothing(constraint="uq_ctem_snapshot_user_vertical_date")
            result = await db.execute(insert.returning(CtemPostureSnapshot.id))
            if result.scalar_one_or_none() is not None:
                written.append(row["snapshot_date"].isoformat())
        await db.commit()
    await engine.dispose()
    return written


def main() -> None:
    user_id, out = sys.argv[1:3]
    Path(out).write_text(json.dumps(asyncio.run(seed(user_id))))


if __name__ == "__main__":
    main()
