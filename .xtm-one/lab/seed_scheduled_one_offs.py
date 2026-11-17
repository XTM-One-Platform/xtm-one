"""The lab's scheduled one-offs: the Reschedule dialog of ``ScheduleActions`` on real rows (#11252).

Started by ``lab.py`` (``seed_scheduled_one_offs``) with the backend's interpreter, from ``backend/``
and with the backend's environment, once the backend answers. A scheduled one-off is created by the
``schedule_work_item`` agent tool, never by a REST route, so a lab with no model has none and the
Reschedule dialog (work item dialog, Operations > Work items, the fleet Schedule view) needs a
hand-made call. This writes, through the ``WorkItem`` model, two one-offs of the administrator owned
by the agent given (a member of "Lab sample fleet"), idempotent by correlation key:

* ``upcoming``: an instruction one-off due in 24 hours (``scheduled.state`` scheduled);
* ``missed``: an exact one-off due 26 hours ago, blocked with ``outcome_code`` missed, the state
  the scheduler leaves an item it could not run in its grace window.

Each restart moves the upcoming one 24 hours ahead again, so it never turns due in a long lab.
No owner inbox dispatches them: an ``action_mode`` item is the scheduler's alone, and the scheduler
never runs a blocked item nor an open one before its due time.

Arguments: the administrator's user id, the agent id and the JSON file the ids are written to,
``{slot: id}``.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: The correlation key prefix of the rows this seed writes.
KEY_PREFIX = "lab-scheduled-one-off"
#: The slots this seed writes, in order.
SLOTS = ("upcoming", "missed")


def plan(now: datetime) -> dict[str, dict]:
    """The fields of each slot's item, relative to *now*."""
    return {
        "upcoming": {
            "title": "Seeded by the lab: weekly digest of the sample fleet",
            "status": "open",
            "outcome_code": None,
            "result": None,
            "action_mode": "instruction",
            "due_at": now + timedelta(hours=24),
            "payload": {"action": {
                "mode": "instruction",
                "prompt": "Read the sample fleet's runs of the last week and post a five-line digest.",
                "target": "Lab sample fleet",
                "notify": True,
            }},
        },
        "missed": {
            "title": "Seeded by the lab: post the sample reminder",
            "status": "blocked",
            "outcome_code": "missed",
            "result": "Not run: the platform was down past the grace window of its due time.",
            "action_mode": "exact",
            "due_at": now - timedelta(hours=26),
            "payload": {"action": {
                "mode": "exact",
                "tool": "store_memory",
                "arguments": {"content": "Sample reminder seeded by the lab."},
                "target": "Agent memory",
            }},
        },
    }


async def seed(admin_id: str, agent_id: str) -> dict[str, str]:
    """Write the items; answer ``{slot: id}``."""
    from sqlalchemy import select

    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.work_item import WorkItem

    admin = uuid.UUID(admin_id)
    agent = uuid.UUID(agent_id)
    now = datetime.now(UTC)
    async with async_session() as db:
        items = {}
        for slot, fields in plan(now).items():
            key = f"{KEY_PREFIX}:{slot}"
            item = (await db.execute(select(WorkItem).where(WorkItem.correlation_key == key))).scalars().first()
            if item is None:
                item = WorkItem(user_id=admin, correlation_key=key, title=fields["title"], source="user",
                                priority="medium")
                db.add(item)
            item.user_id = admin
            item.owner_agent_id = agent
            item.claimed_by_agent_id = None
            item.claimed_by_run_id = None
            item.claimed_at = None
            for field, value in fields.items():
                setattr(item, field, value)
            item.last_activity_at = now
            items[slot] = item
        await db.commit()
        answer = {slot: str(item.id) for slot, item in items.items()}
    await engine.dispose()
    return answer


def main() -> None:
    admin_id, agent_id, out = sys.argv[1:4]
    Path(out).write_text(json.dumps(asyncio.run(seed(admin_id, agent_id))))


if __name__ == "__main__":
    main()
