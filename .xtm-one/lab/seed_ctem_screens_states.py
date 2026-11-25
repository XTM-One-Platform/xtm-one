"""The CTEM screens states a review used to build by hand, for ``[service.lab-ctem-screens]`` (#2316).

Started by ``lab.py`` (``seed_ctem_screens_states``) with the backend's interpreter, from ``backend/``
and with the backend's environment, once ``seed_ctem_screens`` has read the cases back. The recompute
opens cases from in-scope issues only; these states need rows no API writes in a chosen state, so a UX
round or a retest set them through lab-sql, ``browser evaluate`` or a ``window.fetch`` override:

* a watchlisted signal of the administrator (the scope gate left it under the threshold), the
  Unmatched signals tab (gap lab-ctem-screens-no-watchlist-signal);
* two signals of the lab's non-admin peer, one watchlisted and one new: a non-admin reads only the
  signals recorded for them (``ctem_access.signal_reader``), so the peer's tab was empty
  (gap lab-ctem-screens-peer-no-signals);
* the running versions history of the first upgrade case: a ``baseline`` then a change event,
  written by ``ctem_case_version_history.event_fields`` (gap lab-ctem-screens-no-version-states);
* a stage run of the peer skipped by the module account preflight (``no_module_account``, both
  modules missing), so the peer's Scoping card shows the sign-in hint of #2075
  (gap lab-ctem-no-module-account-seed).

The OpenAEV re-test states ride on the sample itself (``ctem_sample.SCREENS_ISSUES``) and the primary
asset links go through the organization context API in ``lab.py``. A restart finds the rows by their
stable keys and writes nothing twice. Arguments: the administrator's id, the peer's email (``-`` when the
lab has none), the upgrade case id (``-`` when none) and the JSON file the answer is written to.

dev-docs/reference/agentic-labs.md, "The xtm-one lab". Lab-only.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: What every row this writes carries, so it reads as seeded.
SEEDED_BY = {"seeded_by": "lab"}
#: The vertical the screens lab seeds.
VERTICAL = "cyber"
#: The stage whose run the preflight skipped for the peer.
SKIPPED_STAGE_SLUG = "ctem-scoping-analyst"


def _watchlist_decision(cve: str, criticality: str) -> dict:
    """A scope gate decision record left under the threshold (``ctem_scope_gate`` ``below_scope_threshold``)."""
    return {
        "outcome": "watchlist", "state": "watchlist",
        "reason": f"criticality {criticality} below the scope threshold high",
        "reason_code": "below_scope_threshold",
        "reason_params": {"criticality": criticality, "threshold": "high"},
        "inputs": {"cves": [cve]}, "policy_checks": [],
    }


#: (dedupe key, owner ``admin`` or ``peer``, state, cve, product, criticality) of every seeded signal.
SIGNALS: tuple[tuple[str, str, str, str, str, str], ...] = (
    ("lab-screens-watchlist-admin-1", "admin", "watchlist", "CVE-2024-3400", "PAN-OS", "medium"),
    ("lab-screens-watchlist-peer-1", "peer", "watchlist", "CVE-2023-4966", "NetScaler ADC", "low"),
    ("lab-screens-new-peer-1", "peer", "new", "CVE-2024-6387", "OpenSSH", ""),
)

#: The per-device snapshots of the upgrade case's history, oldest first (``ctem_case_version_history``).
VERSION_SNAPSHOTS: tuple[dict, ...] = (
    {
        "lab-device-1": {"versions": ["7.2.4"], "status": "below_fixed", "sources": ["lab"]},
        "lab-device-2": {"versions": ["7.2.5"], "status": "below_fixed", "sources": ["lab"]},
    },
    {
        "lab-device-1": {"versions": ["7.2.7"], "status": "fixed", "sources": ["lab"]},
        "lab-device-2": {"versions": ["unknown"], "status": "not_comparable", "sources": ["lab"]},
    },
)


def signal_rows(admin_id: str, peer_id: str | None, now: datetime) -> list[dict]:
    """The ``CtemSignal`` columns of :data:`SIGNALS` (a peer row is left out when the lab has no peer)."""
    rows = []
    for index, (key, owner, state, cve, product, criticality) in enumerate(SIGNALS):
        recorder = admin_id if owner == "admin" else peer_id
        if not recorder:
            continue
        seen = now - timedelta(hours=6 + index)
        rows.append({
            "trigger_path": "new_vulnerability", "lens": "world", "source_provider": "opencti",
            "source_external_id": f"vulnerability--lab-signal-{index + 1:04d}", "subject_kind": "vulnerability",
            "subject_ref": cve, "subject_label": f"{cve} ({product})", "observed_at": seen,
            "first_seen_at": seen, "last_seen_at": seen, "evidence": {"product": product, **SEEDED_BY},
            "dedupe_key": key, "state": state,
            "decision": _watchlist_decision(cve, criticality) if state == "watchlist" else None,
            "recorded_via": "api", "recorded_by": recorder,
        })
    return rows


def version_events(now: datetime) -> list[dict]:
    """The ``versions`` case events of :data:`VERSION_SNAPSHOTS`: a baseline, then one change a day later."""
    from app.services import ctem_case_version_history as history

    events = []
    before = None
    for index, snapshot in enumerate(VERSION_SNAPSHOTS):
        fields = history.event_fields(before, snapshot)
        if fields is not None:
            at = now - timedelta(days=len(VERSION_SNAPSHOTS) - index)
            events.append({"kind": history.KIND, "at": at, **fields})
        before = snapshot
    return events


def skipped_run(agent_id: str, peer_id: str, now: datetime) -> dict:
    """The ``AssignmentRun`` columns of the peer's stage run the preflight skipped (#2042)."""
    from app.services.ctem_module_preflight import NO_MODULE_ACCOUNT

    at = now - timedelta(minutes=10)
    return {
        "agent_id": agent_id, "user_id": peer_id, "status": "skipped", "error": NO_MODULE_ACCOUNT,
        "trigger_payload": SEEDED_BY,
        "trigger_context": {"type": "manual", "ctem_vertical": VERTICAL,
                            "module_accounts": {"opencti": "missing", "openaev": "missing"}, **SEEDED_BY},
        "created_at": at, "started_at": at, "completed_at": at,
    }


async def seed(admin_id: str, peer_email: str | None, case_id: str | None) -> dict[str, int]:
    """Write the rows; answer state -> rows written (0 for a state a restart found)."""
    from sqlalchemy import select

    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.agent import Agent
    from app.models.assignment import AssignmentRun
    from app.models.ctem_case import CtemCase, CtemCaseEvent
    from app.models.ctem_signal import CtemSignal
    from app.models.user import User
    from app.services.ctem_cases import SYSTEM_ACTOR
    from app.services.organizations import get_primary

    now = datetime.now(UTC)
    written = {"signals": 0, "version_events": 0, "skipped_run": 0}
    async with async_session() as db:
        primary = await get_primary(db)
        if primary is None:
            raise SystemExit("no primary organization")
        peer_id = None
        if peer_email:
            peer = (await db.execute(select(User.id).where(User.email == peer_email))).scalar()
            peer_id = str(peer) if peer is not None else None
        have = set((await db.execute(
            select(CtemSignal.dedupe_key).where(CtemSignal.dedupe_key.in_([s[0] for s in SIGNALS]))
        )).scalars())
        for row in signal_rows(admin_id, peer_id, now):
            if row["dedupe_key"] not in have:
                db.add(CtemSignal(organization_id=primary.id, **row))
                written["signals"] += 1
        if case_id:
            case = await db.get(CtemCase, uuid.UUID(case_id))
            if case is None:
                raise SystemExit(f"case {case_id} not found")
            recorded = (await db.execute(
                select(CtemCaseEvent.id).where(CtemCaseEvent.case_id == case.id, CtemCaseEvent.kind == "versions")
            )).first()
            if recorded is None:
                for event in version_events(now):
                    db.add(CtemCaseEvent(case_id=case.id, actor=SYSTEM_ACTOR, **event))
                    written["version_events"] += 1
        if peer_id:
            agent = (await db.execute(select(Agent.id).where(Agent.slug == SKIPPED_STAGE_SLUG))).scalar()
            found = (await db.execute(
                select(AssignmentRun.id).where(
                    AssignmentRun.user_id == uuid.UUID(peer_id),
                    AssignmentRun.trigger_context["seeded_by"].astext == "lab",
                    AssignmentRun.status == "skipped",
                )
            )).first()
            if agent is not None and found is None:
                row = skipped_run(str(agent), peer_id, now)
                db.add(AssignmentRun(**{k: v for k, v in row.items() if k not in ("agent_id", "user_id")},
                                     agent_id=uuid.UUID(row["agent_id"]), user_id=uuid.UUID(row["user_id"])))
                written["skipped_run"] += 1
        await db.commit()
    await engine.dispose()
    return written


def _arg(value: str) -> str | None:
    return None if value in ("", "-") else value


def main() -> None:
    admin_id, peer_email, case_id, out = sys.argv[1:5]
    Path(out).write_text(json.dumps(asyncio.run(seed(admin_id, _arg(peer_email), _arg(case_id)))))


if __name__ == "__main__":
    main()
