"""The lab's trigger provisioning states: one ``integration_event`` assignment per kind of the Trigger card's line.

Started by ``lab.py`` (``seed_provisioning_states``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once the assignments exist: the assignment API
keeps ``trigger_config.<provider>_provisioning`` server-owned and never writes
``last_triggered_at`` (an event does), so this writes them through the platform's model.
Arguments: a JSON object of kind to assignment id (``KINDS``), and the JSON file the written ids
are written to.

The six kinds the ProvisioningSummary of the assignment page renders
(``frontend/src/pages/assignment-detail/provisioning-state.ts``):

* ``provisioned-opencti``: provisioned in OpenCTI, one trigger, checked and run by an event;
* ``provisioned-openaev``: provisioned in OpenAEV, two triggers (one per event);
* ``error``: the last pass failed, with its error;
* ``orphaned``: no longer declared, still held because the integration did not answer;
* ``pending``: declared and enabled, nothing created yet;
* ``disabled``: declared on a paused assignment, nothing created until it is enabled.

The lab freezes the provisioning pass (``lab_asgi``, ``LAB_FREEZE_TRIGGER_PROVISIONING``):
nothing in the lab answers OpenCTI or OpenAEV, so a pass would turn every state into an error.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: kind -> provider of its assignment.
KINDS: dict[str, str] = {
    "provisioned-opencti": "opencti",
    "provisioned-openaev": "openaev",
    "error": "opencti",
    "orphaned": "openaev",
    "pending": "opencti",
    "disabled": "openaev",
}

#: The ``provision`` block each provider's pass reads (``read_provision_spec``).
PROVISION: dict[str, dict] = {
    "opencti": {"event_types": ["create"], "entity_types": ["Report"]},
    "openaev": {"fleet": "Lab sample fleet", "purpose": "exposure validation",
                "events": ["finding_created", "simulation_finished"]},
}

#: The ``trigger_config`` key each provider's pass keeps its state under (``STATE_KEY``).
STATE_KEYS: dict[str, str] = {"opencti": "opencti_provisioning", "openaev": "openaev_provisioning"}


def plan(now: datetime) -> dict[str, dict]:
    """Per kind: ``enabled``, the ``trigger_config`` keys to set (``None`` removes one), ``last_triggered_at``."""
    checked = (now - timedelta(minutes=4)).isoformat()
    opencti_name = "XTM One - Lab sample - reports"
    openaev_name = "XTM One - Lab sample fleet - exposure validation"
    held_opencti = {"name": opencti_name, "notifier_id": "lab-notifier-opencti", "trigger_id": "lab-trigger-opencti",
                    "fingerprint": "lab", "checked_at": checked}
    held_openaev = {"name": openaev_name, "notifier_id": "lab-notifier-openaev", "fingerprint": "lab",
                    "trigger_ids": {"finding_created": "lab-trigger-finding", "simulation_finished": "lab-trigger-sim"},
                    "checked_at": checked}
    return {
        "provisioned-opencti": {
            "enabled": True, "last_triggered_at": now - timedelta(minutes=12),
            "config": {"provision": PROVISION["opencti"],
                       STATE_KEYS["opencti"]: {**held_opencti, "status": "provisioned", "error": None}},
        },
        "provisioned-openaev": {
            "enabled": True, "last_triggered_at": None,
            "config": {"provision": PROVISION["openaev"],
                       STATE_KEYS["openaev"]: {**held_openaev, "status": "provisioned", "error": None}},
        },
        "error": {
            "enabled": True, "last_triggered_at": None,
            "config": {"provision": PROVISION["opencti"], STATE_KEYS["opencti"]: {
                "status": "error", "checked_at": checked,
                "error": "Seeded by the lab: the OpenCTI integration has no webhook_secret.",
            }},
        },
        "orphaned": {
            "enabled": True, "last_triggered_at": None,
            "config": {"provision": None, STATE_KEYS["openaev"]: {
                **held_openaev, "status": "orphaned",
                "error": "Seeded by the lab: the OpenAEV integration did not answer.",
            }},
        },
        "pending": {
            "enabled": True, "last_triggered_at": None,
            "config": {"provision": PROVISION["opencti"], STATE_KEYS["opencti"]: None},
        },
        "disabled": {
            "enabled": False, "last_triggered_at": None,
            "config": {"provision": PROVISION["openaev"], STATE_KEYS["openaev"]: None},
        },
    }


def apply(trigger_config: dict, keys: dict) -> dict:
    """*trigger_config* with *keys* set, a ``None`` value removing its key."""
    updated = {**trigger_config, **{k: v for k, v in keys.items() if v is not None}}
    for key in (k for k, v in keys.items() if v is None):
        updated.pop(key, None)
    return updated


async def seed(assignments: dict[str, str]) -> list[str]:
    """Write each kind on its assignment; answer the ids written, in ``KINDS`` order."""
    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.assignment import Assignment

    rows = plan(datetime.now(UTC))
    written = []
    async with async_session() as db:
        for kind in KINDS:
            if kind not in assignments:
                continue
            assignment = await db.get(Assignment, uuid.UUID(assignments[kind]))
            if assignment is None:
                raise RuntimeError(f"assignment {assignments[kind]} ({kind}) not found")
            row = rows[kind]
            assignment.trigger_config = apply(dict(assignment.trigger_config or {}), row["config"])
            assignment.enabled = row["enabled"]
            assignment.last_triggered_at = row["last_triggered_at"]
            written.append(assignments[kind])
        await db.commit()
    await engine.dispose()
    return written


def main() -> None:
    assignments, out = sys.argv[1:3]
    Path(out).write_text(json.dumps(asyncio.run(seed(json.loads(assignments)))))


if __name__ == "__main__":
    main()
