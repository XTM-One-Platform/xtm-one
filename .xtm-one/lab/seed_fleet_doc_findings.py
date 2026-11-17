"""The lab's fleet documentation findings and a version flagged for regeneration (#10335).

Started by ``lab.py`` (``seed_fleet_doc_findings``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once the corporate fleet's documentation is
generated. The lab's stub model returns no findings and a seed regenerate never loses a block,
so the Suggestions chip, its dialog, Dismiss and the "Needs a regeneration" flag of Version
history (#10246) never render. This writes two more versions through the product's own writer
(``fleet_docs.save_documentation``: history, fingerprint, ``clean_findings``):

* an older one carrying a ``part_derived`` note only;
* the current one carrying a ``block_lost`` note (``needs_regenerate`` true) and the findings
  of :func:`findings`, one per kind the About page draws differently.

The structured parts are kept as they are: the About page keeps its showcase. Arguments: the
author (the lab administrator), the fleet id, and the JSON file the answer
(``{"version": N, "findings": [ids]}``) is written to. No model call.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

#: A member name the corporate fleet does not have: the member_without_role finding.
ABSENT_MEMBER = "Lab sample billing desk"


def findings(live_member: str, gated_member: str) -> list[dict[str, str]]:
    """Four findings of different kinds, as the drafter's answer carries them (``clean_findings`` adds the ids)."""
    return [
        {
            "kind": "missing_automation",
            "member": live_member,
            "finding": f"{live_member} is described as answering every new email, but no automation starts it.",
            "suggested_fix": "Add an automation on new emails that starts this member.",
        },
        {
            "kind": "no_gate_on_sensitive_action",
            "member": gated_member,
            "finding": (
                f"{gated_member} can merge pull requests and post to external channels on its own: neither action "
                "waits for a person's approval, although the fleet description says every outside change is reviewed "
                "before it lands."
            ),
            "suggested_fix": "Require an approval on merge and on posts to external channels.",
        },
        {
            "kind": "description_mismatch",
            "member": "",
            "finding": "The fleet description names a weekly report that no member produces.",
            "suggested_fix": "Remove the weekly report from the description, or give a member that task.",
        },
        {
            "kind": "member_without_role",
            "member": ABSENT_MEMBER,
            "finding": f"The orchestrator delegates invoices to {ABSENT_MEMBER}, which is not a member of this fleet.",
            "suggested_fix": "",
        },
    ]


def flagged_keys(structured: dict | None, block_names: dict[str, str]) -> tuple[str, str]:
    """The blocks the two notes name: (derived, lost), real keys of *block_names*, drawn ones first."""
    drawn = [key for key in block_names if isinstance(structured, dict) and structured.get(key)]
    keys = drawn or list(block_names)
    return keys[0], keys[1] if len(keys) > 1 else keys[0]


async def seed(author: str, fleet_id: str) -> dict:
    """Write the two versions on *fleet_id*; answer the current version and the stored finding ids."""
    from sqlalchemy import select

    import app.models  # noqa: F401 - every table, so the foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.agent import Agent
    from app.models.fleet import Fleet
    from app.services.fleet_doc_findings import clean_findings
    from app.services.fleet_docs import BLOCK_NAMES, doc_note, save_documentation
    from app.services.fleets import fleet_members

    async with async_session() as db:
        fleet = await db.get(Fleet, uuid.UUID(fleet_id))
        stored = fleet.documentation if fleet is not None else None
        if not isinstance(stored, dict) or not stored.get("sections"):
            raise RuntimeError(f"fleet {fleet_id} has no generated documentation")
        rows = await fleet_members(db, fleet.id)
        ids = [r.agent_id for r in rows if r.agent_id is not None and r.agent_id != fleet.orchestrator_agent_id]
        names = sorted(
            (await db.execute(select(Agent.name).where(Agent.id.in_(ids)))).scalars().all()
        ) if ids else []
        if len(names) < 2:
            raise RuntimeError(f"fleet {fleet_id} has {len(names)} member(s) besides its orchestrator, 2 needed")
        derived, lost = flagged_keys(stored.get("structured"), BLOCK_NAMES)
        sections = dict(stored["sections"])
        owner = uuid.UUID(author)
        await save_documentation(
            db, fleet, sections, author=owner, base_version=None, notes=[doc_note("part_derived", block=derived)]
        )
        await save_documentation(
            db,
            fleet,
            sections,
            author=owner,
            base_version=None,
            notes=[doc_note("block_lost", block=lost)],
            findings=clean_findings(findings(names[0], names[-1])),
        )
        answer = {
            "version": int(fleet.documentation["version"]),
            "findings": [f["id"] for f in fleet.documentation.get("findings") or []],
        }
        await db.commit()
    await engine.dispose()
    return answer


def main() -> None:
    author, fleet_id, out = sys.argv[1:4]
    Path(out).write_text(json.dumps(asyncio.run(seed(author, fleet_id))))


if __name__ == "__main__":
    main()
