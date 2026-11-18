"""The lab's A2A partner runs: work one inbound OAuth A2A partner submitted.

Started by ``lab.py`` (``seed_a2a_partner``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once the backend answers, the
``oauth_a2a`` integration exists, its partner credential is minted and an agent
binds it. A partner's run is created by ``a2a.tasks.create_task`` when the
partner submits a message over A2A, and no API creates one with a given
registration, so this writes them through the platform's model. Arguments: the
bound agent, the lab administrator (who owns every run, so
``run_visible_filter`` shows them), the partner's minted client id, and the JSON
file the run ids are written to.

Three assignment-less runs, the shape ``create_task`` writes (``trigger_context``
``{source: a2a, client_id, a2a_provider: oauth_a2a}``): one ``completed``, one
``failed`` and one ``running``. So the integration's Activity tab in A2A mode
(``GET /api/v1/assignments/runs/all?a2a_integration_id=``) lists partner rows
of mixed status with no fetch override (dev:harness-gaps lab-seed-a2a, #71).

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
PROVIDER = "oauth_a2a"

#: (status, minutes before the seed, the partner's message, error).
RUNS: tuple[tuple[str, int, str, str | None], ...] = (
    ("completed", 45, "Summarise the sample phishing wave for our SOC.", None),
    ("failed", 25, "Enrich the sample indicator list we shared.",
     "Seeded by the lab: the partner's attachment could not be read."),
    ("running", 3, "Draft a takedown request for the sample domain.", None),
)


def plan(client_id: str, now: datetime) -> list[dict]:
    """The ``AssignmentRun`` columns of every seeded partner run, oldest first, times relative to *now*."""
    rows = []
    for index, (status, minutes, message, error) in enumerate(RUNS):
        started = now - timedelta(minutes=minutes)
        rows.append({
            "status": status, "error": error,
            "trigger_payload": {"a2a_message_id": f"lab-a2a-{index}", "message": message, **SEEDED_BY},
            "trigger_context": {"source": "a2a", "client_id": client_id, "a2a_provider": PROVIDER, **SEEDED_BY},
            "result_summary": None if status == "running" else f"Seeded by the lab: a {status} A2A partner run.",
            "created_at": started, "started_at": started,
            "completed_at": None if status == "running" else started + timedelta(minutes=2),
        })
    return rows


async def seed(agent_id: str, admin_id: str, client_id: str) -> list[str]:
    """Create the runs; answer their ids, oldest first."""
    import app.models  # noqa: F401 - every table, so the runs' foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.assignment import AssignmentRun

    agent, admin = uuid.UUID(agent_id), uuid.UUID(admin_id)
    runs = [
        AssignmentRun(assignment_id=None, agent_id=agent, user_id=admin, **row)
        for row in plan(client_id, datetime.now(UTC))
    ]
    async with async_session() as db:
        db.add_all(runs)
        await db.commit()
    await engine.dispose()
    return [str(run.id) for run in runs]


def main() -> None:
    agent_id, admin_id, client_id, out = sys.argv[1:5]
    Path(out).write_text(json.dumps(asyncio.run(seed(agent_id, admin_id, client_id))))


if __name__ == "__main__":
    main()
