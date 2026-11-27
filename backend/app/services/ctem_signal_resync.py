# SPDX-License-Identifier: Apache-2.0

"""Step 3 of the purge of every signal (#2528): resync the signals from their sources.

Run by ``ctem_signal_purge.run_job`` only when the administrator ticked the box,
with intake still paused for every other caller (``RESYNC_BYPASS``). Each signal
goes through the same recording point as live intake, so the same exclusions,
dedupe keys and scope gate apply, with the current threshold, and the
case-opening switch stays as the administrator left it:

- **World events (paths 1, 2 and 10)** from every enabled OpenCTI source
  (``ctem_signal_sources`` rows, as its run-as): the entities of the live
  listener's types created or updated in the last :data:`WORLD_WINDOW_DAYS`
  days, each fed to ``ctem_signal_listeners.note_event`` as an ``update``
  delivery, so the live trigger's extractor and filters decide. Dedupe is the
  OpenCTI internal id, as live.
- **Scheduled passes (paths 5, 6, 7, 9, 12)**: the attack surface and the
  evidence health passes run once now (their cadence slot is given back first).

Not resynced, each named in the job's counts under ``skipped`` with its reason:
the PIR playbook's scores (OpenCTI attaches them on its playbook delivery only),
path 11 (third-party events need the organization annotation of a stream
delivery), and the inside state of paths 3, 4 and 8 (Aikido, GitHub and OpenAEV
deliver open findings by webhook; no listing of open findings is wired yet).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

logger = logging.getLogger(__name__)

#: How far back the world events are read.
WORLD_WINDOW_DAYS = 30
#: OpenCTI page size, and the most entities one source resyncs.
PAGE = 200
MAX_ENTITIES = 20000

#: The OpenCTI entity types the live listener reads as entities (relationships and sightings are events, not state).
WORLD_TYPES: tuple[str, ...] = (
    "Vulnerability",
    "Intrusion-Set",
    "Threat-Actor-Group",
    "Threat-Actor-Individual",
    "Campaign",
    "Malware",
    "User-Account",
    "Email-Addr",
)
#: The listener object type of each OpenCTI entity type (``ctem_signal_listeners.LISTENERS``).
_STIX_TYPE: dict[str, str] = {
    "Threat-Actor-Individual": "threat-actor",
}

#: What the job reports as not resynced, by source, with its reason.
SKIPPED: dict[str, str] = {
    "opencti:pir_playbook": "PIR scores come only with OpenCTI's playbook delivery; the entities resync through the listener",
    "third_party_event": "path 11 needs the organization annotation of a live OpenCTI delivery",
    "code_flaw": "Aikido and GitHub open findings arrive by webhook; no listing of open findings is wired",
    "proven_exploitable": "OpenAEV delivers simulation results by event; no listing is wired",
    "mitigation_regression": "OpenAEV delivers score degradations by event; no listing is wired",
}

_QUERY = """
query CtemResync($types: [String], $filters: FilterGroup, $first: Int, $after: ID) {
  stixCoreObjects(types: $types, filters: $filters, first: $first, after: $after, orderBy: updated_at, orderMode: asc) {
    edges { node {
      id entity_type standard_id
      ... on Vulnerability { name x_opencti_cvss_base_score x_opencti_epss_score x_opencti_cisa_kev }
      ... on IntrusionSet { name }
      ... on ThreatActor { name }
      ... on Campaign { name }
      ... on Malware { name }
      ... on UserAccount { account_login user_id display_name is_disabled }
      ... on EmailAddr { value }
    } }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def _filters(since: datetime) -> dict[str, Any]:
    return {
        "mode": "and",
        "filters": [{"key": ["updated_at"], "values": [since.isoformat()], "operator": "gt"}],
        "filterGroups": [],
    }


def delivery(node: dict[str, Any]) -> dict[str, Any] | None:
    """The live listener's delivery for one OpenCTI entity, ``None`` for a type it does not read."""
    from app.services.ctem_signal_listeners import OPENCTI_EXTENSION

    entity_type = str(node.get("entity_type") or "")
    if entity_type not in WORLD_TYPES or not node.get("id"):
        return None
    data: dict[str, Any] = {
        k: v for k, v in node.items() if k not in ("id", "entity_type", "standard_id") and v not in (None, "")
    }
    data["type"] = _STIX_TYPE.get(entity_type, entity_type.lower())
    data["id"] = node.get("standard_id") or node["id"]
    data["extensions"] = {OPENCTI_EXTENSION: {"id": node["id"]}}
    return {"type": "update", "data": data, "_opencti_event": {"message": "resync after a purge of every signal"}}


async def _opencti_sources(db: Any) -> list[Any]:
    from app.models.ctem_signal_source import CtemSignalSource

    rows = (await db.execute(select(CtemSignalSource).where(CtemSignalSource.provider == "opencti"))).scalars()
    seen: set[str] = set()
    out = []
    for row in rows:
        if row.enabled and str(row.integration_id) not in seen:
            seen.add(str(row.integration_id))
            out.append(row)
    return out


async def resync_world(
    sessions: Any, *, now: datetime | None = None, note: Callable[..., Awaitable[bool]] | None = None
) -> dict[str, Any]:
    """Paths 1, 2 and 10 from every enabled OpenCTI source; ``{read, recorded, errors}``."""
    from app.services import opencti_trigger_provisioning as prov

    if note is None:
        from app.services.ctem_signal_listeners import note_event as note
    since = (now or datetime.now(UTC)) - timedelta(days=WORLD_WINDOW_DAYS)
    read = recorded = 0
    errors: list[str] = []
    async with sessions() as db:
        sources = await _opencti_sources(db)
        connections = []
        for row in sources:
            conn = await prov.integration_connection(db, str(row.integration_id), row.run_as_user_id)
            if isinstance(conn, str):
                errors.append(conn)
            else:
                connections.append(conn[0])
    for gql in connections:
        after: str | None = None
        while read < MAX_ENTITIES:
            data, err = await gql(
                _QUERY, {"types": list(WORLD_TYPES), "filters": _filters(since), "first": PAGE, "after": after}
            )
            if err:
                errors.append(f"OpenCTI: {err}"[:300])
                break
            block = (data or {}).get("stixCoreObjects") or {}
            for edge in block.get("edges") or []:
                payload = delivery((edge or {}).get("node") or {})
                if payload is None:
                    continue
                read += 1
                if await note("opencti", "update", payload, verified=True):
                    recorded += 1
            page = block.get("pageInfo") or {}
            if not page.get("hasNextPage") or not page.get("endCursor"):
                break
            after = page["endCursor"]
    return {"read": read, "recorded": recorded, "errors": errors}


async def resync_passes(sessions: Any) -> dict[str, Any]:
    """The attack surface (paths 5, 6, 7) and evidence health (9, 12) passes, once each, now."""
    from app.services import ctem_attack_surface, ctem_evidence_health

    out: dict[str, Any] = {}
    for name, module in (("attack_surface", ctem_attack_surface), ("evidence_health", ctem_evidence_health)):
        await module.release_pass_slot()
        async with sessions() as db:
            try:
                out[name] = await module.scheduled_pass(db) or {"skipped": "off in the profile"}
            except Exception as exc:
                await db.rollback()
                logger.warning("CTEM resync: the %s pass failed", name, exc_info=True)
                out[name] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    return out


async def resync(
    *, sessions: Any, actor_id: uuid.UUID, progress: Callable[[str, int], Awaitable[None]]
) -> dict[str, Any]:
    """Step 3: the world events, then the passes; the counts per path group and what was skipped."""
    world = await resync_world(sessions)
    await progress("world", world["recorded"])
    passes = await resync_passes(sessions)
    for name, counts in passes.items():
        await progress(name, int((counts or {}).get("recorded") or 0) if isinstance(counts, dict) else 0)
    return {"world": world, "passes": passes, "skipped": dict(SKIPPED)}
