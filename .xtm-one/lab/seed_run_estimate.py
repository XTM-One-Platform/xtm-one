"""The lab's fleet run estimate: one tracked organization with a figure, one with a per-assignment error.

Started by ``lab.py`` (``seed_fleet_run_estimate``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once ``seed_organizations`` wrote its rows and
the assignment exists. Without it ``GET /api/v1/fleets/<Lab sample fleet>/run-estimate`` answers
``expected_runs_per_month: 0`` and ``assignments: []`` for every tracked organization: no member
assignment declares a ``provision`` block serving tracked organizations and no tracked
organization has an OpenCTI identity. Like ``seed_provisioning.py``, this writes both through the
platform's models (the provisioning pass is frozen by ``lab_asgi``, so the block stays as written):

* the assignment's ``trigger_config.provision`` (OpenCTI, ``organizations: tracked``), which
  ``organization_run_cap.estimate_runs`` reads with ``read_provision_spec``;
* the monitored tracked organization linked to :data:`IDENTITY`, a fixture Organization of
  ``lab_asgi.LAB_OPENCTI_ORGANIZATIONS`` whose count ``lab_asgi`` answers (``LAB_RUN_ESTIMATE_COUNT``);
* the other active tracked organization left unlinked: its entry carries ``no OpenCTI identity linked``.

Arguments: the assignment id, the ``organizations.json`` ``seed_organizations`` wrote, and the JSON
file the linked and unlinked organization ids are written to. Idempotent.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

#: The fixture Organization (``lab_asgi.LAB_OPENCTI_ORGANIZATIONS``, Acme Corporation) the linked row names.
IDENTITY = "identity--0b5c3e2a-lab0-4000-8000-000000000001"

#: The ``provision`` block of the member assignment: every tracked organization, one entity type.
PROVISION: dict = {
    "fleet": "Lab sample fleet",
    "purpose": "tracked organization reports",
    "entity_types": ["Report"],
    "event_types": ["create"],
    "organizations": "tracked",
}


async def seed(assignment_id: str, tracked: list[str]) -> dict[str, str]:
    """Write the provision block and the link; answer the linked and unlinked organization ids."""
    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.assignment import Assignment
    from app.models.organization import Organization

    async with async_session() as db:
        assignment = await db.get(Assignment, uuid.UUID(assignment_id))
        if assignment is None:
            raise RuntimeError(f"assignment {assignment_id} not found")
        assignment.trigger_config = {**dict(assignment.trigger_config or {}), "provision": PROVISION}
        assignment.enabled = True
        rows = [await db.get(Organization, uuid.UUID(i)) for i in tracked]
        active = [r for r in rows if r is not None and r.archived_at is None]
        if len(active) < 2:
            raise RuntimeError(f"two active tracked organizations needed, found {len(active)}")
        monitored = next((r for r in active if r.monitoring == "on"), active[-1])
        unlinked = next(r for r in active if r.id != monitored.id)
        monitored.opencti_organization_id = IDENTITY
        unlinked.opencti_organization_id = None
        ids = {"linked": str(monitored.id), "unlinked": str(unlinked.id)}
        await db.commit()
    await engine.dispose()
    return ids


def main() -> None:
    assignment_id, organizations, out = sys.argv[1:4]
    tracked = json.loads(Path(organizations).read_text())["tracked"]
    Path(out).write_text(json.dumps(asyncio.run(seed(assignment_id, tracked))))


if __name__ == "__main__":
    main()
