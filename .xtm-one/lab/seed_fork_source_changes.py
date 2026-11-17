"""The lab's forks whose source changed: the writes the API refuses, done on the lab's database.

Started by ``lab.py`` (``seed_fork_source_changes``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once the forks exist. Two cases of the
fork source-changes notice (``GET /fleets/{id}/source-changes``) cannot be reached
through the API:

- a fork of a built-in fleet whose source changed: the API keeps built-in fleets
  read-only (403), so this helper edits the built-in source here, in the lab only
  (the fleet description and one member's role label);
- an older fork, made before the structure was kept: this helper drops
  ``forked_from.structure`` from that fork, so the endpoint answers changed with
  ``diff_available`` false.

The product's read-only rule for built-in fleets is unchanged. Arguments: the built-in
fleet id (empty: skip), the older fork id (empty: skip), the JSON file the done steps
are written to.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

#: Appended to the built-in source's description and its first member's role label.
CHANGED = "changed after the fork"


def changed_role_label(label: str) -> str:
    """*label* with the lab's change mark, within the column's 100 characters."""
    return f"{label} ({CHANGED})"[:100]


def without_structure(forked_from: dict) -> dict:
    """*forked_from* as a fork made before the structure was kept would carry it."""
    return {key: value for key, value in forked_from.items() if key != "structure"}


async def seed(builtin_id: str, older_id: str) -> dict[str, bool]:
    """Change the built-in source and age the older fork; answer what was done."""
    from sqlalchemy import select

    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.fleet import FLEET_ROLE_MEMBER, Fleet, FleetMember

    done = {"builtin": False, "older": False}
    async with async_session() as db:
        if builtin_id:
            source = await db.get(Fleet, uuid.UUID(builtin_id))
            if source is None or not source.is_builtin:
                raise RuntimeError(f"{builtin_id} is not a built-in fleet")
            source.description = f"{source.description or ''} Description {CHANGED}.".strip()
            member = (
                await db.execute(
                    select(FleetMember)
                    .where(
                        FleetMember.fleet_id == source.id,
                        FleetMember.role == FLEET_ROLE_MEMBER,
                        FleetMember.agent_id.is_not(None),
                        FleetMember.agent_id != source.orchestrator_agent_id,
                    )
                    .order_by(FleetMember.created_at, FleetMember.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if member is not None:
                member.role_label = changed_role_label(member.role_label or "Member")
            done["builtin"] = True
        if older_id:
            fork = await db.get(Fleet, uuid.UUID(older_id))
            if fork is None or not isinstance(fork.forked_from, dict):
                raise RuntimeError(f"{older_id} is not a fork")
            # Reassigned whole: an in-place change of a JSON column is not tracked.
            fork.forked_from = without_structure(fork.forked_from)
            done["older"] = True
        await db.commit()
    await engine.dispose()
    return done


def main() -> None:
    builtin_id, older_id, out = sys.argv[1:4]
    Path(out).write_text(json.dumps(asyncio.run(seed(builtin_id, older_id))))


if __name__ == "__main__":
    main()
