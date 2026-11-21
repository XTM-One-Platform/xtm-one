"""The lab's blocked work items with no reason: the "No reason" chip of the work items list on real rows.

Started by ``lab.py`` (``seed_no_reason_blocked``) with the backend's interpreter, from ``backend/``
and with the backend's environment, once the backend answers (#10422). The API refuses a blocked
item that says nothing of what it waits for (``refuse_blocked_without_why``), so a lab cannot
produce the legacy state ``blocked_why`` answers ``missing`` for. This writes, through the
``WorkItem`` model, blocked items of the administrator with no ``blocked_on``, no
``blocked_on_refs``, no ``blocked_reason`` and no ``needs_human``, with no owner agent (so no inbox
dispatches them), idempotent by correlation key:

* ``operations``: unscoped, listed by ``/tasks/work-items``;
* ``fleet``: filed in the workspace of the fleet given (the lab's locked fleet) with no owner, so
  ``?fleet=<id>`` keeps it (``fleet_work_items_clause``) and the fleet's Operations > Work items
  list renders the chip too. Left out when no fleet is given.

Arguments: the administrator's user id, the fleet id (empty for none) and the JSON file the ids are
written to, ``{slot: id}``.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

#: The correlation key prefix of the rows this seed writes.
KEY_PREFIX = "lab-no-reason-blocked"
#: The slots this seed writes, in order; ``fleet`` only with a fleet.
SLOTS = ("operations", "fleet")


def plan(workspace_id: uuid.UUID | None) -> dict[str, dict]:
    """The fields of each slot's item; *workspace_id* is the fleet's workspace, ``None`` for no fleet."""
    common = {
        "status": "blocked",
        "blocked_on": None,
        "blocked_on_refs": None,
        "blocked_reason": None,
        "needs_human": False,
        "due_at": None,
        "not_before": None,
    }
    rows = {
        "operations": {
            **common,
            "title": "Seeded by the lab: blocked with no reason",
            "workspace_id": None,
        },
    }
    if workspace_id is not None:
        rows["fleet"] = {
            **common,
            "title": "Seeded by the lab: blocked with no reason, in the fleet",
            "workspace_id": workspace_id,
        }
    return rows


async def seed(admin_id: str, fleet_id: str) -> dict[str, str]:
    """Write the items; answer ``{slot: id}``."""
    from sqlalchemy import select

    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.fleet import Fleet
    from app.models.work_item import WorkItem

    admin = uuid.UUID(admin_id)
    now = datetime.now(UTC)
    async with async_session() as db:
        workspace = None
        if fleet_id:
            workspace = (await db.execute(select(Fleet.workspace_id).where(Fleet.id == uuid.UUID(fleet_id)))).scalar()
            if workspace is None:
                raise RuntimeError(f"fleet {fleet_id} not found")
        rows = plan(workspace)
        items = {}
        for slot, fields in rows.items():
            key = f"{KEY_PREFIX}:{slot}"
            item = (await db.execute(select(WorkItem).where(WorkItem.correlation_key == key))).scalars().first()
            if item is None:
                item = WorkItem(
                    user_id=admin,
                    correlation_key=key,
                    title=fields["title"],
                    description="Blocked before a reason was required: the list shows a No reason chip until it is edited.",
                    source="agent",
                )
                db.add(item)
            item.owner_agent_id = None
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
    admin_id, fleet_id, out = sys.argv[1:4]
    Path(out).write_text(json.dumps(asyncio.run(seed(admin_id, fleet_id))))


if __name__ == "__main__":
    main()
