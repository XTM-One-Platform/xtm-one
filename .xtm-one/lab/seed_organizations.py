"""The lab's organizations: a named primary and three tracked ones (#9389).

Started by ``lab.py`` (``seed_organizations``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once the backend answers. Without
it ``GET /api/v1/organizations`` answers ``[]``: the primary never renders (the fleet
organization scope reads the fallback "Own organization") and every organization-scope
review posts tracked organizations by hand. No API switches a tracked organization's
monitoring on (``dev-docs/ctem-fleets.md`` section 9.7), so this writes the rows through
``services/organizations.py`` (``ensure_primary``, ``create_tracked``, ``archive``) and
sets ``monitoring`` on the model. Argument: the JSON file the ids are written to.

* the primary, ``Lab sample organization``;
* a provider with a long name (the list and the fleet columns truncate it);
* a customer, archived (listed with ``include_archived=true`` only);
* a partner with monitoring ``on`` (the organizations the CTEM fleets cover).

Idempotent: a restarted lab finds the rows by name and writes nothing twice.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

PRIMARY_NAME = "Lab sample organization"

#: (name, relationship, criticality, monitoring, archived)
TRACKED: tuple[tuple[str, str, str, str, bool], ...] = (
    (
        "Lab sample managed cloud hosting and security operations provider for the European subsidiaries",
        "provider", "high", "off", False,
    ),
    ("Lab sample archived customer", "customer", "low", "off", True),
    ("Lab sample monitored partner", "partner", "critical", "on", False),
)


async def seed() -> dict[str, object]:
    """Write the primary and the tracked rows (each once); answer their ids."""
    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from sqlalchemy import select

    from app.core.database import async_session, engine
    from app.models.organization import Organization
    from app.services import organizations as orgs

    async with async_session() as db:
        primary = await orgs.ensure_primary(db, name=PRIMARY_NAME)
        if primary.name in ("", "Primary"):
            primary.name = PRIMARY_NAME
        tracked: list[str] = []
        for name, relationship, criticality, monitoring, archived in TRACKED:
            row = (
                await db.execute(select(Organization).where(Organization.role == "tracked", Organization.name == name))
            ).scalar_one_or_none()
            if row is None:
                row = await orgs.create_tracked(db, name=name, relationship=relationship, criticality=criticality)
            row.monitoring = monitoring
            if archived:
                orgs.archive(row)
            await db.flush()
            tracked.append(str(row.id))
        ids = {"primary": str(primary.id), "tracked": tracked}
        await db.commit()
    await engine.dispose()
    return ids


def main() -> None:
    (out,) = sys.argv[1:2]
    Path(out).write_text(json.dumps(asyncio.run(seed())))


if __name__ == "__main__":
    main()
