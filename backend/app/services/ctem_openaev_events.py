# SPDX-License-Identifier: Apache-2.0

"""OpenAEV events moving CTEM cases (CTEM-15, #918): event-driven, no poll, no model call.

A provisioned OpenAEV trigger delivers to the per-assignment webhook route
(``services/openaev_trigger_provisioning.py`` names the event of a delivery).
Two things happen there, whatever assignment the delivery reaches:

- the signal table (``services/ctem_signal_listeners.py``) records a path 8
  signal for ``scenario_score_degraded`` (a mitigation that was proven no
  longer holds);
- :func:`note_event` (this module) finds the cases a result is about, a
  case whose validation reference or OpenAEV member names the delivered
  resource, writes one ``source_event`` entry in each case's history and
  raises ``ctem_case.validation_finished`` once per case and occurrence. The
  loop fleets listen to it (routed by trigger path, as the other case
  events), read the result in OpenAEV and write it on the case with
  ``record_ctem_case_validation``, which resolves or reopens the case.

Path 4 (an attack path reaching a critical asset, OpenAEV's
``CRITICAL_ASSET_REACHED`` since CTEM-13) records a ``proven_exploitable``
signal through the signal table and, as a case event key, moves the cases
that wait on the simulation.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ctem_case import OPEN_STATES, CtemCase, CtemCaseEvent, CtemCaseIssue

logger = logging.getLogger(__name__)

PROVIDER = "openaev"
#: The platform event a case raises when an OpenAEV result it waits on arrives.
CASE_VALIDATION_FINISHED = "ctem_case.validation_finished"
#: The delivered events that can carry a case's validation or re-test result.
CASE_EVENT_KEYS: frozenset[str] = frozenset(
    {"simulation_finished", "finding_updated", "finding_deleted", "critical_asset_reached"}
)
#: The states in which a case still waits on a result (a resolved case reopens through a failed re-test).
WAITING_STATES: tuple[str, ...] = (*OPEN_STATES, "mitigated_watching", "resolved")
#: Most cases one delivery is matched against.
SCAN_LIMIT = 500
#: The history kind of the entry (``ctem_case_events.kind`` is 20 characters).
HISTORY_KIND = "source_event"


def delivered(payload: Mapping[str, Any]) -> dict[str, str]:
    """The OpenAEV facts of a normalized delivery (``_openaev_event``), as text."""
    meta = payload.get("_openaev_event") if isinstance(payload, Mapping) else None
    if not isinstance(meta, Mapping):
        return {}
    return {k: str(v).strip() for k, v in meta.items() if isinstance(v, str | int) and str(v).strip()}


#: The OpenAEV Scenario field that declares a reachability assertion (OpenAEV REST API, "Reachability assertions").
ASSERTION_FIELD = "scenario_reachability_assertion"


async def stamp_reachability_assertion(
    event: str, payload: dict[str, Any], *, integration_id: str, verified: bool
) -> bool | None:
    """CTEM-26: say on a score degradation whether its scenario is a reachability assertion.

    OpenAEV reports a failed assertion (a target that should not answer answers) as
    the existing ``SCORE_DEGRADATION`` of the scenario, and the delivery does not say
    the scenario is an assertion. The scenario is read once with the integration
    owner's credential and ``reachability_assertion`` is written on the normalized
    event, so the signal table records an outside-lens exposure (path 5) instead of a
    mitigation regression (path 8). Verified deliveries only; never raises. The flag
    read, ``None`` when it was not read (the delivery then stays path 8).
    """
    from app.core.config import settings

    meta = payload.get("_openaev_event") if isinstance(payload, dict) else None
    if not verified or settings.is_copilot_like or event != "scenario_score_degraded" or not isinstance(meta, dict):
        return None
    scenario_id = str(meta.get("resource_id") or "").strip()
    if str(meta.get("resource_type") or "").upper() != "SCENARIO" or not scenario_id:
        return None
    try:
        flag = await _read_assertion_flag(integration_id, scenario_id)
    except Exception:
        logger.warning("OpenAEV scenario %s not read for its reachability assertion flag", scenario_id, exc_info=True)
        return None
    if flag is not None:
        meta["reachability_assertion"] = flag
    return flag


async def _read_assertion_flag(integration_id: str, scenario_id: str) -> bool | None:
    from urllib.parse import quote

    body = await _read_object(integration_id, f"/api/scenarios/{quote(scenario_id, safe='')}")
    value = body.get(ASSERTION_FIELD) if isinstance(body, dict) else None
    return value if isinstance(value, bool) else None


async def _read_object(integration_id: str, path: str) -> Any:
    """One OpenAEV object read with the integration owner's credential, ``None`` when unread."""
    import uuid

    from app.core.database import async_session
    from app.integrations.providers.openaev._common import _api, _headers
    from app.models.integration import Integration
    from app.services.integration_tools import _get_token

    async with async_session() as db:
        integration = await db.get(Integration, uuid.UUID(integration_id))
        if integration is None or integration.provider != PROVIDER:
            return None
        owner = str(integration.user_id) if integration.user_id else None
        token = await _get_token(db, integration_id, PROVIDER, user_id=owner, integ=integration)
    if not token or "||" not in str(token):
        return None
    base_url, _, api_token = str(token).partition("||")
    resp, err = await _api(base_url.strip().rstrip("/"), _headers(api_token.strip()), "get", path, f"GET {path}")
    if err or resp is None:
        return None
    return resp.json()


#: The events OpenAEV delivers again on every later update of the same object: its live
#: triggers watch ``UPDATE`` filtered on a state, with no "changed to" event, so a finished
#: simulation edited later is notified again (c87c8335). The OpenAEV field that tells one
#: run of the object from the next (a relaunched simulation ends again, later).
ONCE_PER_OCCURRENCE: dict[str, tuple[str, str]] = {
    "simulation_finished": ("/api/exercises/{id}", "exercise_end_date"),
}
#: How long a delivered occurrence is remembered (a later update past it starts one more run).
REPEAT_TTL_SECONDS = 30 * 24 * 3600


async def claim_occurrence(
    event: str, payload: Mapping[str, Any], *, integration_id: str, assignment_id: str, verified: bool
) -> tuple[bool, str | None]:
    """Claim this occurrence of a once-per-occurrence event for *assignment_id*.

    Answers ``(repeat, claim)``: ``repeat`` is true when the assignment already received
    the occurrence; ``claim`` is the key this delivery claimed, or ``None``. The caller
    keeps the claim only once the delivery started or queued a run, and hands it to
    :func:`release_occurrence` on every other path (dropped, filtered, busy, failed), so
    a later delivery of the same occurrence still runs.

    ``simulation_finished`` is delivered once per simulation and end date: a later update
    of the same finished simulation is a repeat; a relaunched simulation ends again with
    a new end date and is delivered. Verified deliveries only (a forged one never claims
    an occurrence); fails open: an unread end date or an unreachable Redis delivers as
    before. The claim is one atomic ``SET NX``, so two concurrent deliveries of one
    occurrence never both run. Never raises.
    """
    read = ONCE_PER_OCCURRENCE.get(event)
    meta = payload.get("_openaev_event") if isinstance(payload, Mapping) else None
    if read is None or not verified or not isinstance(meta, Mapping):
        return False, None
    resource_id = str(meta.get("resource_id") or "").strip()
    if not resource_id:
        return False, None
    from urllib.parse import quote

    path, field = read
    try:
        body = await _read_object(integration_id, path.format(id=quote(resource_id, safe="")))
    except Exception:
        logger.warning("OpenAEV %s %s not read for its occurrence", event, resource_id, exc_info=True)
        return False, None
    occurrence = body.get(field) if isinstance(body, dict) else None
    if not isinstance(occurrence, str) or not occurrence.strip():
        return False, None
    marker = "\n".join((assignment_id, event, resource_id, occurrence.strip()))
    key = f"openaev:occurrence:{hashlib.sha256(marker.encode()).hexdigest()[:32]}"
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        claimed = await redis.set(key, "1", nx=True, ex=REPEAT_TTL_SECONDS)
    except Exception:
        logger.warning("OpenAEV %s occurrence not claimed (delivered)", event, exc_info=True)
        return False, None
    return (False, key) if claimed else (True, None)


async def release_occurrence(claim: str | None) -> None:
    """Give back an occurrence *claim* whose delivery started no run. Never raises."""
    if not claim:
        return
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        await redis.delete(claim)
    except Exception:
        logger.warning("OpenAEV occurrence claim %s not released", claim, exc_info=True)


def references(case: CtemCase, issues: list[CtemCaseIssue], resource_id: str) -> bool:
    """Whether *case* waits on the OpenAEV object *resource_id* (a validation reference or a member)."""
    if not resource_id:
        return False
    for entry in case.validations or []:
        if not isinstance(entry, Mapping) or str(entry.get("provider") or "").lower() != PROVIDER:
            continue
        if resource_id in str(entry.get("ref") or ""):
            return True
    return any(i.source == PROVIDER and i.external_id == resource_id for i in issues)


def event_id(case: CtemCase, event: str, facts: Mapping[str, str]) -> str:
    """One platform event per case, OpenAEV object and occurrence (its message tells two updates apart)."""
    marker = "\n".join((event, facts.get("resource_id", ""), facts.get("message", "")))
    digest = hashlib.sha256(f"{CASE_VALIDATION_FINISHED}\n{case.id}\n{marker}".encode()).hexdigest()[:32]
    return f"ctem-case-{digest}"


async def matching_cases(db: AsyncSession, resource_id: str) -> list[CtemCase]:
    """The cases that wait on *resource_id*, newest first."""
    cases = (
        (
            await db.execute(
                select(CtemCase)
                .where(CtemCase.state.in_(WAITING_STATES))
                .order_by(CtemCase.updated_at.desc())
                .limit(SCAN_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    if not cases:
        return []
    ids = [c.id for c in cases]
    members = (
        (
            await db.execute(
                select(CtemCaseIssue).where(
                    CtemCaseIssue.case_id.in_(ids),
                    CtemCaseIssue.source == PROVIDER,
                    CtemCaseIssue.external_id == resource_id,
                )
            )
        )
        .scalars()
        .all()
    )
    by_case: dict[Any, list[CtemCaseIssue]] = {}
    for member in members:
        by_case.setdefault(member.case_id, []).append(member)
    return [c for c in cases if references(c, by_case.get(c.id, []), resource_id)]


async def route(
    db: AsyncSession, event: str, payload: Mapping[str, Any], *, now: datetime | None = None
) -> list[tuple[str, dict[str, Any], str]]:
    """Write the history entries of a delivery; the platform events to raise after the commit."""
    from app.services.ctem_case_loop import case_data
    from app.services.ctem_cases import SYSTEM_ACTOR

    if event not in CASE_EVENT_KEYS:
        return []
    facts = delivered(payload)
    resource_id = facts.get("resource_id", "")
    if not resource_id:
        return []
    now = now or datetime.now(UTC)
    out: list[tuple[str, dict[str, Any], str]] = []
    for case in await matching_cases(db, resource_id):
        db.add(
            CtemCaseEvent(
                case_id=case.id,
                actor=SYSTEM_ACTOR,
                kind=HISTORY_KIND,
                at=now,
                to_value=event[:300],
                evidence=f"{PROVIDER}:{resource_id}"[:300],
                reason=(facts.get("message") or None),
            )
        )
        openaev = {
            "event": event,
            "resource_type": facts.get("resource_type"),
            "resource_id": resource_id,
            "platform_url": facts.get("platform_url"),
        }
        data = {**case_data(case), "openaev": {k: v for k, v in openaev.items() if v}}
        out.append((CASE_VALIDATION_FINISHED, data, event_id(case, event, facts)))
    return out


def case_frames(pending: list[tuple[str, dict[str, Any], str]], at: datetime) -> list[dict[str, str]]:
    """One ``ctem_case_update`` frame per case a delivery moved (CTEM-15-UI, #956): ids and states only."""
    from app.services.ctem_loop_stream import case_frame

    frames: list[dict[str, str]] = []
    seen: set[str] = set()
    for _name, data, _eid in pending:
        case_id = str(data.get("case_id") or "")
        if not case_id or case_id in seen:
            continue
        seen.add(case_id)
        frames.append(case_frame(case_id, str(data.get("state") or ""), HISTORY_KIND, at.isoformat()))
    return frames


async def note_event(event: str, payload: Mapping[str, Any], *, verified: bool) -> int:
    """Move the cases an OpenAEV delivery is about; never raises. How many case events were raised.

    XTM One mode only, and only for a delivery verified against the
    integration's webhook secret (a forged delivery never moves a case).
    """
    from app.core.config import settings

    if not verified or settings.is_copilot_like or event not in CASE_EVENT_KEYS:
        return 0
    from app.core.database import async_session
    from app.services.ctem_loop_stream import publish_case_updates
    from app.services.platform_events import publish

    try:
        now = datetime.now(UTC)
        async with async_session() as db:
            pending = await route(db, event, payload, now=now)
            if not pending:
                return 0
            await db.commit()
        # The case page, the board and Act now re-read at once (live, no poll).
        await publish_case_updates(case_frames(pending, now))
        from app.services.ctem_case_routing import stamp_owners

        # Exactly one fleet hears each case event: its owner (#1390).
        await stamp_owners(data for _name, data, _eid in pending)
        raised = 0
        for name, data, eid in pending:
            if await publish(name, data, event_id=eid):
                raised += 1
        return raised
    except Exception:
        logger.warning("CTEM cases not moved by an OpenAEV %s delivery", event, exc_info=True)
        return 0
