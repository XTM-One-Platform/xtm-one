"""The lab's Operations rows: background tasks and worksets that render on real rows.

Started by ``lab.py`` (``seed_operations``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once the backend answers: it
writes through the platform's own models and services, since no API creates a
background task (a sub-agent task is spawned by a delegating agent) and no API
creates a workset (an agent builds it with the workset tools). Arguments: the
agent the tasks belong to (parent and target), the user who owns every row, and
the JSON file the ids are written to.

* five background tasks (``sub_agent_tasks``), Operations > Background Tasks:
  ``running`` with a ``running`` orchestration execution holding a live lease
  (dedup key ``sub_agent_task:<id>``, the liveness column reads its
  ``updated_at`` as the last progress); ``pending`` with no execution (the
  "No live execution" chip; the orphan sweep fails it once it is older than
  the recovery grace, about 20 minutes after the lab starts); ``completed`` with
  a result line; ``failed`` with an error line; ``completed`` with an empty
  result;
* two worksets (``worksets``), Operations > Worksets: one ``active`` with three
  metadata-only items (summary and priority given, so they settle ``ready``
  with no embedding and no model call), one ``resolved``.

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

#: (status, description, result, error, with a live execution)
TASKS: tuple[tuple[str, str, str | None, str | None, bool], ...] = (
    ("running", "Summarise the open findings of the sample workspace.", None, None, True),
    ("pending", "Collect the sample reports of the last week.", None, None, False),
    ("completed", "List the sample relays.", "Two relays answered: both healthy.", None, False),
    ("failed", "Read the sample dataset.", None, "The sample dataset was not found.", False),
    ("completed", "Check the sample template for changes.", "", None, False),
)

#: (title, summary, priority) of the active workset's items.
ITEMS: tuple[tuple[str, str, int], ...] = (
    ("Sample report A", "A short sample report on a phishing wave.", 80),
    ("Sample report B", "A sample advisory on an exposed service.", 55),
    ("Sample report C", "A sample note with no follow-up needed.", 10),
)


async def seed(agent_id: str, user_id: str) -> dict[str, list[str]]:
    """Create the tasks, the execution and the worksets; answer their ids."""
    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.orchestration import OrchestrationExecution
    from app.models.sub_agent_task import SubAgentTask
    from app.models.user import User
    from app.services import workset as worksets

    agent, owner = uuid.UUID(agent_id), uuid.UUID(user_id)
    now = datetime.now(UTC)
    tasks: list[str] = []
    async with async_session() as db:
        for index, (status, description, result, error, live) in enumerate(TASKS):
            created = now - timedelta(minutes=len(TASKS) - index)
            task = SubAgentTask(
                user_id=owner, parent_agent_id=agent, target_agent_id=agent,
                task_description=f"{SEED_NAME}: {description}", status=status,
                result=result, error=error, metadata_json=dict(SEEDED_BY), created_at=created,
                started_at=created if status != "pending" else None,
                completed_at=now if status in ("completed", "failed") else None,
            )
            db.add(task)
            await db.flush()
            if live:
                db.add(OrchestrationExecution(
                    adapter="sub_agent", state="running", payload={"task_id": str(task.id), **SEEDED_BY},
                    dedup_key=f"sub_agent_task:{task.id}", run_at=created, attempt_count=1,
                    # A lease far ahead: no reconciler takes the execution back while the lab lives.
                    lease_owner="lab-seed", lease_expires_at=now + timedelta(days=1),
                    created_at=created, updated_at=now,
                ))
            tasks.append(str(task.id))
        await db.commit()

        user = await db.get(User, owner)
        if user is None:
            raise RuntimeError(f"no user {owner}")
        active = await worksets.resolve_workset(
            db, user, key="lab-sample-reports", scope="global", label=f"{SEED_NAME} reports",
        )
        for title, summary, priority in ITEMS:
            outcome = await worksets.add_item(
                db, active, user, title=title, summary=summary, priority=priority,
                external_ref=f"lab:{title}", tags=["lab"],
            )
            if outcome.action == "refused":
                raise RuntimeError(f"workset item {title!r} refused: {outcome.error}")
        resolved = await worksets.resolve_workset(
            db, user, key="lab-sample-archive", scope="global", label=f"{SEED_NAME} archive",
        )
        await worksets.resolve(db, resolved, note="concluded")
        await db.commit()
        workset_ids = [str(active.id), str(resolved.id)]
    await engine.dispose()
    return {"tasks": tasks, "worksets": workset_ids}


def main() -> None:
    agent_id, user_id, out = sys.argv[1:4]
    Path(out).write_text(json.dumps(asyncio.run(seed(agent_id, user_id))))


if __name__ == "__main__":
    main()
