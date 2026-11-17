"""The lab's built-in fleet: one ``is_builtin`` company-managed fleet that renders on real rows.

Started by ``lab.py`` (``seed_builtin_fleet``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once the backend answers. The API
keeps built-in fleets read-only and ``BUILTIN_FLEETS`` may still be empty, so the
Built-in badge of Flows > Fleets and the read-only states of a built-in fleet had
no row to render on. This helper writes one through the platform's own seed,
:func:`app.agents.fleet_seed.seed_builtin_fleets`, given ``BUILTIN_FLEETS`` plus
one lab entry. Its orchestrator and members are built-in agents that are not yet
home in a fleet, so no other fleet is changed. When the deployment already seeds
a built-in fleet, that fleet is named and nothing is written. Arguments: the JSON
file the fleet id is written to.

The lab's backend never boots again on the same database, so the boot seed never
sweeps the lab entry as stale while the lab lives.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

LAB_FLEET_SLUG = "lab-sample-builtin-fleet"
LAB_FLEET_NAME = "Lab sample built-in fleet"
#: Members under the orchestrator: enough for the Graph tab to draw a tree.
MEMBER_COUNT = 3


def lab_entry(slugs: list[str]) -> dict:
    """The ``BUILTIN_FLEETS`` entry of the lab: the first slug orchestrates the others."""
    orchestrator, *members = slugs
    return {
        "slug": LAB_FLEET_SLUG,
        "name": LAB_FLEET_NAME,
        "description": "Seeded by the lab: a built-in fleet, read-only in the API and the fleets tools.",
        "orchestrator": orchestrator,
        "members": [{"agent": orchestrator, "role_label": "Orchestrator"}]
        + [
            {"agent": slug, "role_label": f"Member {index}", "parent": orchestrator, "exposed": index == 1}
            for index, slug in enumerate(members, start=1)
        ],
    }


async def seed(platform_mode: str) -> dict[str, str]:
    """Seed (or find) a built-in fleet; answer its id and slug."""
    from sqlalchemy import select

    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from app.agents.builtin_fleets import BUILTIN_FLEETS
    from app.agents.fleet_seed import FLEET_SEED_MODES, seed_builtin_fleets
    from app.core.database import async_session, engine
    from app.models.agent import Agent
    from app.models.fleet import FLEET_ROLE_MEMBER, Fleet, FleetMember

    if platform_mode not in FLEET_SEED_MODES:
        raise RuntimeError(f"PLATFORM_MODE {platform_mode!r} seeds no built-in fleet: use [service.lab]")
    async with async_session() as db:
        existing = (
            await db.execute(select(Fleet).where(Fleet.is_builtin.is_(True)).order_by(Fleet.slug).limit(1))
        ).scalar_one_or_none()
        if existing is None:
            homed = select(FleetMember.agent_id).where(FleetMember.role == FLEET_ROLE_MEMBER)
            free = (
                await db.execute(
                    select(Agent.slug)
                    .where(
                        Agent.is_builtin.is_(True),
                        Agent.is_company_managed.is_(True),
                        Agent.slug.is_not(None),
                        Agent.id.notin_(homed),
                    )
                    .order_by(Agent.slug)
                    .limit(MEMBER_COUNT + 1)
                )
            ).scalars().all()
            if len(free) < 2:
                raise RuntimeError(f"{len(free)} built-in agent(s) free of a fleet: two are needed")
            await seed_builtin_fleets(db, platform_mode=platform_mode, fleets=[*BUILTIN_FLEETS, lab_entry(list(free))])
            existing = (
                await db.execute(select(Fleet).where(Fleet.is_builtin.is_(True), Fleet.slug == LAB_FLEET_SLUG))
            ).scalar_one_or_none()
            if existing is None:
                raise RuntimeError("the built-in fleet seed wrote no fleet (no company-managed owner yet?)")
        answer = {"id": str(existing.id), "slug": existing.slug}
    await engine.dispose()
    return answer


def main() -> None:
    platform_mode, out = sys.argv[1:3]
    Path(out).write_text(json.dumps(asyncio.run(seed(platform_mode))))


if __name__ == "__main__":
    main()
