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
* four worksets (``worksets``), Operations > Worksets: one ``active`` with three
  metadata-only items (summary and priority given, so they settle ``ready``
  with no embedding and no model call), one ``resolved``, the seeded agent's
  implicit chat corpus (the runtime's key, ``chat_workset_key``: the "Rolling"
  chip) holding three items after a fourth evicted the lowest one (``max_items``
  3, evicted count 1), and one named workset sealed after two items (the
  "Sealed" chip) (#73).

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

#: The implicit corpus holds this many items; the last of ROLLING_ITEMS evicts the lowest.
ROLLING_MAX_ITEMS = 3

#: (title, summary, priority) of the implicit corpus, in the order they are added.
ROLLING_ITEMS: tuple[tuple[str, str, int], ...] = (
    ("Chat note A", "A sample finding the agent kept from a chat.", 75),
    ("Chat note B", "A sample lead the agent kept for later.", 40),
    ("Chat note C", "A sample aside of little value.", 5),
    ("Chat note D", "A sample finding that pushes the aside out.", 60),
)

#: (title, summary, priority) of the sealed workset's items.
SEALED_ITEMS: tuple[tuple[str, str, int], ...] = (
    ("Weekly report 1", "The first sample report of a closed week.", 50),
    ("Weekly report 2", "The second sample report of a closed week.", 30),
)


async def _add_items(worksets, db, workset, user, items, prefix: str) -> int:
    """Add metadata-only items; answer how many items they evicted. Raise on a refusal."""
    evicted = 0
    for title, summary, priority in items:
        outcome = await worksets.add_item(
            db, workset, user, title=title, summary=summary, priority=priority,
            external_ref=f"{prefix}:{title}", tags=["lab"],
        )
        if outcome.action == "refused":
            raise RuntimeError(f"workset item {title!r} refused: {outcome.error}")
        evicted += len(outcome.evicted)
    return evicted


async def seed(agent_id: str, user_id: str) -> dict[str, list[str]]:
    """Create the tasks, the execution and the worksets; answer their ids."""
    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.orchestration import OrchestrationExecution
    from app.models.sub_agent_task import SubAgentTask
    from app.models.user import User
    from app.services import workset as worksets
    from app.services.builtin_tools.workset import chat_workset_key

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
        await _add_items(worksets, db, active, user, ITEMS, "lab")
        resolved = await worksets.resolve_workset(
            db, user, key="lab-sample-archive", scope="global", label=f"{SEED_NAME} archive",
        )
        await worksets.resolve(db, resolved, note="concluded")
        # The runtime's own corpus of the seeded agent's chat: implicit, never sealed, rolling.
        rolling = await worksets.resolve_workset(
            db, user, key=chat_workset_key(agent), scope="global", label=f"{SEED_NAME} chat corpus",
            owner_agent_id=agent, max_items=ROLLING_MAX_ITEMS, implicit=True, ingest=True,
        )
        if await _add_items(worksets, db, rolling, user, ROLLING_ITEMS, "lab-chat") < 1:
            raise RuntimeError("the implicit corpus evicted nothing")
        sealed = await worksets.resolve_workset(
            db, user, key="lab-sample-week", scope="global", label=f"{SEED_NAME} closed week",
        )
        await _add_items(worksets, db, sealed, user, SEALED_ITEMS, "lab-week")
        await worksets.seal(db, sealed)
        await db.commit()
        workset_ids = [str(active.id), str(resolved.id), str(rolling.id), str(sealed.id)]
    await engine.dispose()
    return {"tasks": tasks, "worksets": workset_ids}


def main() -> None:
    agent_id, user_id, out = sys.argv[1:4]
    Path(out).write_text(json.dumps(asyncio.run(seed(agent_id, user_id))))


if __name__ == "__main__":
    main()
