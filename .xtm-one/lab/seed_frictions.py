"""The lab's friction ledger: two runs whose Frictions card renders on real rows.

Started by ``lab.py`` (``seed_frictions``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once the backend answers: it
writes through the platform's own service code (``services/frictions.py``),
since no API records a friction for a run. Arguments: the agent and the user the
runs belong to, a work item the disposed friction names, and the JSON file the
two run ids are written to.

* the settled run (``completed``): four frictions, one disposed ``work_item``
  (class ``harness``) met three times, one undisposed and ``unclassified``, one
  ``accepted`` with no ref, one with no tool and no evidence; settled like a run
  that ends, so ``frictions_undisposed`` is 1;
* the live run (``running``): two frictions, one undisposed, and
  ``frictions_undisposed`` NULL, so the card counts the undisposed from its rows.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

SEEDED_BY = {"seeded_by": "lab"}


async def seed(agent_id: str, user_id: str, work_item_id: str) -> dict[str, str]:
    """Create the two runs and their frictions; answer their ids."""
    import app.models  # noqa: F401 - every table, so the runs' foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.assignment import AssignmentRun
    from app.services import frictions

    agent, user = uuid.UUID(agent_id), uuid.UUID(user_id)
    now = datetime.now(UTC)
    settled = AssignmentRun(
        agent_id=agent, user_id=user, status="completed", trigger_payload=SEEDED_BY,
        trigger_context={"type": "manual", **SEEDED_BY},
        result_summary="Seeded by the lab: a run that ended with one friction undisposed.",
        started_at=now, completed_at=now,
    )
    live = AssignmentRun(
        agent_id=agent, user_id=user, status="running", trigger_payload=SEEDED_BY,
        trigger_context={"type": "manual", **SEEDED_BY}, started_at=now,
    )
    async with async_session() as db:
        db.add_all([settled, live])
        await db.commit()

    async def record(run: AssignmentRun, kind: str, tool: str = "", evidence: str = "", times: int = 1) -> str:
        scope = frictions.make_scope(agent, user_id=user, run_id=run.id)
        if scope is None:
            raise RuntimeError(f"no friction scope for agent {agent}")
        for _ in range(times):
            row, _created = await frictions.record_friction(scope, kind=kind, tool=tool, evidence=evidence)
        return row["id"]

    async def dispose(friction_id: str, friction_class: str, disposition: str, ref: str = "") -> None:
        await frictions.dispose_friction(
            friction_id, agent_id=agent, user_id=user, friction_class=friction_class, disposition=disposition, ref=ref,
        )

    repeated = await record(
        settled, "tool_error", "sandbox_run_command", "Error: the container was not created within 130 s.", times=3,
    )
    await dispose(repeated, "harness", "work_item", work_item_id)
    await record(settled, "sandbox_failure", "materialize_repository", "Error: the checkout stopped at 4,913 files.")
    accepted = await record(settled, "approval_pause", "create_github_commit", "The run paused for an approval.")
    await dispose(accepted, "platform_acceptable", "accepted")
    bare = await record(settled, "manual")
    await dispose(bare, "procedure", "shared_state", "lab:frictions-sample")
    await frictions.settle_run_frictions(settled)

    await record(live, "tool_error", "get_github_file", "Error: 404 Not Found.")
    retried = await record(live, "retry", "list_github_pr_states", "The merge state answered unknown, read again.")
    await dispose(retried, "platform_acceptable", "accepted")
    await engine.dispose()
    return {"settled": str(settled.id), "live": str(live.id)}


def main() -> None:
    agent_id, user_id, work_item_id, out = sys.argv[1:5]
    Path(out).write_text(json.dumps(asyncio.run(seed(agent_id, user_id, work_item_id))))


if __name__ == "__main__":
    main()
