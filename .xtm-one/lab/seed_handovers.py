"""The lab's handover rows: agent delegations that light the live edges of the maps.

Started by ``lab.py`` (``seed_handovers``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once the backend answers: no API
creates an ``agent_delegations`` row (an agent logs one when it consults, transfers
or spawns a background task), so this writes them through the platform's model.
Arguments: the user who owns every row (the lab administrator, so the unfiltered
``GET /api/v1/chat/delegations`` answers them), a JSON object of agent name to id
(the lab's seeded agents), and the JSON file the ids are written to.

Five rows, each along a handover edge the lab configured (``HANDOVERS``):

* ``running``, Orchestrator to Researcher, inside "Lab sample fleet": its one live edge;
* ``pending``, Triage to Responder;
* ``completed`` 5 minutes before the seed, Responder to Triage;
* ``failed`` 10 minutes before the seed, Triage to Responder;
* ``completed`` 3 hours before the seed, Orchestrator to Writer: a still edge.

The live window of the maps is 15 minutes (``LIVE_HANDOVER_WINDOW_MS``, in
``frontend/src/lib/live-handovers.ts``, shipped by #9053, slice 1 of #9050): the
running and pending rows stay live while the lab lives, the completed one for
about 10 minutes after the seed, the failed one for about 5; the old one never.
A build without that file (main before #9053) draws every returned row live and
no fleet graph draws a live edge: review the live edges on a lab of a build that
carries it. With Fleets on, the map collapses "Lab sample fleet" into one node,
so its inner edge (Orchestrator to Researcher) is read on the fleet's Graph tab
or with ``&fleets=0``.

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
SEED_NAME = "Lab sample"

#: The handover edges the lab configures (source, target, handover type), by agent name.
HANDOVERS: tuple[tuple[str, str, str], ...] = (
    ("Orchestrator", "Researcher", "delegate"),
    ("Orchestrator", "Writer", "delegate"),
    ("Triage", "Responder", "delegate"),
    ("Responder", "Triage", "consult"),
)

#: (status, source, target, description, started minutes ago, completed minutes ago)
DELEGATIONS: tuple[tuple[str, str, str, str, int | None, int | None], ...] = (
    ("running", "Orchestrator", "Researcher", "Research the sample phishing wave.", 1, None),
    ("pending", "Triage", "Responder", "Contain the sample exposed service.", None, None),
    ("completed", "Responder", "Triage", "Confirm the sample alert is a true positive.", 6, 5),
    ("failed", "Triage", "Responder", "Isolate the sample host.", 11, 10),
    ("completed", "Orchestrator", "Writer", "Write the sample weekly summary.", 181, 180),
)

#: The delegation type a handover type logs.
DELEGATION_TYPE = {"delegate": "background_task", "consult": "consult", "transfer": "transfer"}


def plan(agents: dict[str, str], now: datetime) -> list[dict]:
    """The rows to write, times relative to *now*; every edge is one of ``HANDOVERS``."""
    kinds = {(source, target): kind for source, target, kind in HANDOVERS}
    rows = []
    for status, source, target, description, started, completed in DELEGATIONS:
        kind = kinds[(source, target)]
        start = now - timedelta(minutes=started) if started is not None else None
        rows.append({
            "parent_agent_id": agents[source], "target_agent_id": agents[target],
            "type": DELEGATION_TYPE[kind], "status": status,
            "description": f"{SEED_NAME}: {description}",
            "result": "Done on the sample data." if status == "completed" else None,
            "error": "The sample host did not answer." if status == "failed" else None,
            "created_at": start or now, "started_at": start,
            "completed_at": now - timedelta(minutes=completed) if completed is not None else None,
            "duration_ms": (started - completed) * 60_000 if started is not None and completed is not None else None,
        })
    return rows


async def seed(user_id: str, agents: dict[str, str]) -> list[str]:
    """Write the delegations; answer their ids."""
    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.agent_delegation import AgentDelegation

    owner = uuid.UUID(user_id)
    ids: list[str] = []
    async with async_session() as db:
        for row in plan({name: str(uuid.UUID(agent)) for name, agent in agents.items()}, datetime.now(UTC)):
            delegation = AgentDelegation(
                user_id=owner, metadata_json=dict(SEEDED_BY),
                **{**row, "parent_agent_id": uuid.UUID(row["parent_agent_id"]),
                   "target_agent_id": uuid.UUID(row["target_agent_id"])},
            )
            db.add(delegation)
            await db.flush()
            ids.append(str(delegation.id))
        await db.commit()
    await engine.dispose()
    return ids


def main() -> None:
    user_id, agents, out = sys.argv[1:4]
    Path(out).write_text(json.dumps(asyncio.run(seed(user_id, json.loads(agents)))))


if __name__ == "__main__":
    main()
