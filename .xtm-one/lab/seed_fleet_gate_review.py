"""The lab's fleet gate review: one custom fleet whose computed approval catalog is off (#11001).

Started by ``lab.py`` (``seed_fleet_gate_review``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once the fleet exists with its documentation,
an empty stored catalog and a member that writes to the lab GitHub integration. Every new custom
fleet acts on its computed catalog (``Fleet.approval_pins`` ``{}``, ``fleets.create_fleet``), so a
member with a write tool always gives it an effective catalog and the Approval tab never offers
"Review actions" (``FleetGateReview``). No route sets ``approval_pins`` back to ``NULL``, the legacy
state a migrated fleet keeps until a person applies a level: this helper writes it through the
backend's own model, on the one fleet named, and nothing else. Arguments: the fleet id.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import sys
import uuid


async def seed(fleet_id: str) -> None:
    """Set ``approval_pins`` of *fleet_id* to ``NULL``; refuse a built-in or missing fleet."""
    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.fleet import Fleet

    async with async_session() as db:
        fleet = await db.get(Fleet, uuid.UUID(fleet_id))
        if fleet is None:
            raise RuntimeError(f"no fleet {fleet_id}")
        if fleet.is_builtin:
            raise RuntimeError(f"fleet {fleet_id} is built-in: its catalog is the seed's")
        fleet.approval_pins = None
        await db.commit()
    await engine.dispose()


def main() -> None:
    asyncio.run(seed(sys.argv[1]))


if __name__ == "__main__":
    main()
