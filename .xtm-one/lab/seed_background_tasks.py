"""The lab's background tasks: running, pending, completed and failed sub-agent tasks with a live execution.

Started by ``lab.py`` (``seed_background_tasks``) with the backend's interpreter, from ``backend/`` and
with the backend's environment, once the backend answers. A ``sub_agent_tasks`` row is written by the
platform when an agent delegates work in the background (``services/sub_agent.py``) and running one
needs a model call, so this writes them through the platform's models.

``seed ADMIN PARENT TARGET OUT``: the tasks of :data:`TASKS` from the parent agent to the target agent,
owned by the lab administrator, their ids written to ``OUT``. Each open task (pending, running) gets a
``sub_agent`` orchestration execution in ``pending`` with ``run_at`` :data:`PARKED_DAYS` ahead and no
deadline: no worker claims it before then, the orphan recovery leaves a task whose execution is
pending, and the listing shows it with a live execution.

``restamp OUT``: sets ``completed_at`` of the seeded ended tasks to now. The chat background tasks bar
shows a completion of the last 60 seconds only (``COMPLETED_RECENCY_MS``), so a capture restamps first.

dev-docs/reference/agentic-labs.md, "The xtm-one lab" (dev:harness-gaps lab-seed-running-background-tasks).
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: Days ahead of now the execution of an open task becomes claimable.
PARKED_DAYS = 30

#: The tasks' ``task_description`` prefix, how a re-seed or a restamp finds them.
PREFIX = "[lab]"

#: (label, status, minutes since creation, minutes it ran, description, result or error).
TASKS: tuple[tuple[str, str, float, float, str, str | None], ...] = (
    ("running-long-1", "running", 12, 0,
     "Map every exposed service of the three acquisitions to their owners, cross-check the asset inventory "
     "against the last two external scans and list the gaps with the team that should close each one", None),
    ("running-long-2", "running", 7, 0,
     "Draft the quarterly threat landscape for the board: top five intrusion sets targeting the sector, "
     "their recent campaigns, the techniques we have no detection for, and the three investments that close most of it",
     None),
    ("running-3", "running", 3, 0, "Summarise the overnight phishing reports and group them by campaign", None),
    ("pending-1", "pending", 1, 0, "Check the new vendor questionnaire against the security policy", None),
    ("completed-1", "completed", 25, 9, "Collect the indicators of the latest ransomware advisory",
     "Collected 42 indicators (31 domains, 11 hashes) and attached them to the advisory report."),
    ("completed-2", "completed", 18, 4, "List the open critical vulnerabilities on internet-facing hosts",
     "Seven open critical vulnerabilities on four internet-facing hosts; owners notified."),
    ("failed-1", "failed", 15, 6, "Export the detection coverage matrix to the shared workspace",
     "The shared workspace refused the upload: the file is over the workspace's size limit."),
)

OPEN = ("pending", "running")


def plan(now: datetime, *, admin: str, parent: str, target: str) -> list[dict]:
    """The ``sub_agent_tasks`` columns of every seeded task, and its execution when open, relative to *now*."""
    rows = []
    for label, status, age, ran, description, outcome in TASKS:
        created = now - timedelta(minutes=age)
        started = None if status == "pending" else created + timedelta(seconds=20)
        ended = None if status in OPEN else started + timedelta(minutes=ran)
        task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"xtm-one-lab:background-task:{label}:{now.isoformat()}"))
        task = {
            "id": task_id, "user_id": admin, "parent_agent_id": parent, "target_agent_id": target,
            "conversation_id": None, "task_description": f"{PREFIX} {description}", "status": status,
            "result": outcome if status == "completed" else None, "error": outcome if status == "failed" else None,
            "metadata_json": {"lab_seed": label}, "created_at": created, "started_at": started, "completed_at": ended,
        }
        execution = None
        if status in OPEN:
            execution = {
                "adapter": "sub_agent", "state": "pending", "dedup_key": f"sub_agent_task:{task_id}",
                "payload": {"task_id": task_id, "user_id": admin, "target_agent_id": target, "lab_seed": label},
                "run_at": now + timedelta(days=PARKED_DAYS), "deadline_at": None, "max_attempts": 1,
            }
        rows.append({"label": label, "task": task, "execution": execution})
    return rows


async def seed(admin: str, parent: str, target: str) -> dict[str, object]:
    """Write the tasks and their executions; answer the task ids by label."""
    import app.models  # noqa: F401 - every table, so the foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.orchestration import OrchestrationExecution
    from app.models.sub_agent_task import SubAgentTask

    keyed = {"id", "user_id", "parent_agent_id", "target_agent_id"}
    async with async_session() as db:
        written = {}
        for row in plan(datetime.now(UTC), admin=admin, parent=parent, target=target):
            values = {k: uuid.UUID(v) if k in keyed and isinstance(v, str) else v for k, v in row["task"].items()}
            db.add(SubAgentTask(**values))
            if row["execution"]:
                db.add(OrchestrationExecution(**row["execution"]))
            written[row["label"]] = row["task"]["id"]
        await db.commit()
    await engine.dispose()
    return {"tasks": written}


async def restamp(ids: list[str]) -> int:
    """Set ``completed_at`` of the ended tasks among *ids* to now; answer how many moved."""
    import app.models  # noqa: F401
    from sqlalchemy import update

    from app.core.database import async_session, engine
    from app.models.sub_agent_task import SubAgentTask

    async with async_session() as db:
        result = await db.execute(
            update(SubAgentTask)
            .where(SubAgentTask.id.in_([uuid.UUID(i) for i in ids]), SubAgentTask.status.notin_(OPEN))
            .values(completed_at=datetime.now(UTC))
        )
        await db.commit()
    await engine.dispose()
    return int(result.rowcount or 0)


def main() -> None:
    mode, *args = sys.argv[1:]
    if mode == "seed":
        admin, parent, target, out = args
        Path(out).write_text(json.dumps(asyncio.run(seed(admin, parent, target))))
    elif mode == "restamp":
        ids = list(json.loads(Path(args[0]).read_text())["tasks"].values())
        print(f"restamped {asyncio.run(restamp(ids))} ended background task(s)")
    else:
        raise SystemExit(f"unknown mode {mode!r}: seed ADMIN PARENT TARGET OUT | restamp OUT")


if __name__ == "__main__":
    main()
