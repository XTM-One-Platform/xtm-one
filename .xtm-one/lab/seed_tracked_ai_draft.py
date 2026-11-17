"""The lab's tracked-organizations AI draft: one pending proposal with every review row state (#9761).

Started by ``lab.py`` (``seed_tracked_ai_draft``) with the backend's interpreter, from ``backend/``
and with the backend's environment, once ``seed_organizations`` wrote its rows. Without it the
review page of ``POST /api/v1/organizations/tracked/ai-draft`` (``/settings/organization/tracked/
ai-draft/<id>``, #9731) only renders error and conflict rows: the stub model answers no tracked
draft, so every batch fails. This stores one pending ``new_tracked`` proposal through the platform's
own ``organization_context_proposals.store_tracked_draft``, its rows built with
``organization_tracked_ai_draft.Row`` (the shape the draft route stores):

* pending rows: high confidence with two OpenCTI matches of the lab fixture and the exact one
  preset, medium with one exact match preset, low with no match, and a long name with no match;
* one conflict row: the monitored tracked organization ``seed_organizations`` wrote;
* one failed batch of two names: two ``error`` rows carrying the batch's message.

The OpenCTI matches are fixture identities of ``lab_asgi.LAB_OPENCTI_ORGANIZATIONS``, so the row's
picker reads them back. Arguments: the ``organizations.json`` ``seed_organizations`` wrote and the
JSON file the proposal id is written to. Idempotent: a pending proposal with the same names is kept.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

#: Fixture identities of ``lab_asgi.LAB_OPENCTI_ORGANIZATIONS``.
ACME = {"id": "identity--0b5c3e2a-lab0-4000-8000-000000000001", "name": "Acme Corporation"}
ACME_EMEA = {
    "id": "identity--0b5c3e2a-lab0-4000-8000-000000000002",
    "name": "Acme International Logistics and Maritime Freight Forwarding Holdings Limited (Europe, Middle East and Africa)",
}
INITECH = {"id": "identity--0b5c3e2a-lab0-4000-8000-000000000003", "name": "Initech"}

LONG_NAME = (
    "Lab sample regional payment processing and card issuing services provider for the Nordic and Baltic markets"
)
#: The failed batch's message: the one the draft route stores when the model answers no proposal.
BATCH_ERROR = "The model answered no proposal for this batch."
FAILED_BATCH = ("Lab sample failed batch supplier one", "Lab sample failed batch supplier two")

#: The pending rows: (name, relationship, criticality, sector, regions, website, confidence, basis, matches,
#: preset). Every relationship and criticality is one ``store_tracked_draft`` accepts.
PENDING: tuple[tuple, ...] = (
    ("Acme Corporation", "provider", "high", "Technology", ["US", "IE"], "https://acme.example", "high",
     "Lab sample: a cloud provider the name clearly designates.", [ACME, ACME_EMEA], ACME["id"]),
    ("Initech", "partner", "medium", "Software", ["US"], "https://initech.example", "medium",
     "Lab sample: probably the software publisher of that name.", [INITECH], INITECH["id"]),
    ("Lab sample unknown supplier", "provider", "low", "", [], "", "low",
     "Lab sample: an ambiguous name, nothing known.", [], ""),
    (LONG_NAME, "provider", "critical", "Financial services", ["SE", "FI", "EE"], "", "medium",
     "Lab sample: a long name, the table truncates it.", [], ""),
)


def rows(conflict_name: str, conflict_id: str) -> list[dict]:
    """The seven rows of the draft, in the order the review page lists them."""
    from app.services.organization_tracked_ai_draft import Row

    out: list[Row] = []
    for name, relationship, criticality, sector, regions, website, confidence, basis, matches, preset in PENDING:
        out.append(Row(
            name=name, relationship=relationship, criticality=criticality, sector=sector, regions=list(regions),
            website=website, confidence=confidence, basis=basis, opencti_matches=[dict(m) for m in matches],
            opencti_organization_id=preset,
        ))
    out.append(Row(name=conflict_name, relationship="", already_tracked_id=conflict_id, notes=["Already tracked."],
                   status="conflict"))
    out.extend(Row(name=name, relationship="", error=BATCH_ERROR, status="error") for name in FAILED_BATCH)
    return [r.as_dict() for r in out]


async def seed(tracked: list[str]) -> dict[str, str]:
    """Store the draft once; answer its proposal id."""
    import uuid

    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from sqlalchemy import select

    from app.core.database import async_session, engine
    from app.models.organization import Organization
    from app.models.organization_context import OrganizationContextProposal
    from app.services.organization_context_proposals import store_tracked_draft

    async with async_session() as db:
        found = [await db.get(Organization, uuid.UUID(i)) for i in tracked]
        active = [r for r in found if r is not None and r.archived_at is None]
        if not active:
            raise RuntimeError("no active tracked organization: the conflict row needs one")
        conflict = next((r for r in active if r.monitoring == "on"), active[-1])
        wanted = rows(conflict.name, str(conflict.id))
        names = [r["name"] for r in wanted]
        pending = (
            await db.execute(
                select(OrganizationContextProposal).where(
                    OrganizationContextProposal.kind == "new_tracked", OrganizationContextProposal.status == "pending"
                )
            )
        ).scalars().all()
        existing = next((p for p in pending if [r.get("name") for r in p.proposals or []] == names), None)
        if existing is None:
            existing = await store_tracked_draft(db, author_user_id=None, rows=wanted)
            await db.commit()
        proposal_id = str(existing.id)
    await engine.dispose()
    return {"proposal": proposal_id}


def main() -> None:
    organizations, out = sys.argv[1:3]
    tracked = json.loads(Path(organizations).read_text())["tracked"]
    Path(out).write_text(json.dumps(asyncio.run(seed(tracked))))


if __name__ == "__main__":
    main()
