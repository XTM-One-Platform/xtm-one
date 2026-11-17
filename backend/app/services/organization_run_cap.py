# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""The daily run cap of a tracked organization and its next-day digest (#9157, slice M6).

Design: ``dev-docs/ctem-fleets.md`` section 9.7. An OpenCTI event the
receiver resolved to tracked organizations (``services/organization_events``)
counts one run against each of them for the day (UTC). Once every concerned
organization has used its ``daily_run_cap`` (default 5), the event is not run:
it is held, and the held events of one assignment and one organization are
run the next day as ONE digest run (the autonomous tick,
:func:`dispatch_due_digests`). An event that concerns no tracked organization
(the primary's own triggers) is never counted nor held.

State lives in Redis only: a counter per organization and day, a list of held
events per (assignment, organization, day) and one set naming the lists to
dispatch. Every key expires after :data:`_TTL_SECONDS`, so nothing grows
without bound; a Redis outage runs the event (the cap never drops one).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DAILY_RUN_CAP = 5
MAX_DAILY_RUN_CAP = 100
#: Where a digest run's trigger payload carries the held events.
DIGEST_PAYLOAD_KEY = "_organization_digest"
_PENDING_KEY = "org_run_cap:digests"
_TTL_SECONDS = 3 * 86400
#: Held events kept per digest (the newest ones; the count says how many arrived).
_MAX_HELD = 50
_MAX_DIGESTS_PER_TICK = 20


def _day(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%d")


def _count_key(organization_id: str, day: str) -> str:
    return f"org_run_cap:count:{organization_id}:{day}"


def _held_key(assignment_id: str, organization_id: str, day: str) -> str:
    return f"org_run_cap:held:{assignment_id}:{organization_id}:{day}"


def held_event(payload: dict[str, Any]) -> dict[str, Any]:
    """What a digest keeps of a delivered OpenCTI event: its type, message and the instance's id and name."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return {
        "type": str(payload.get("type") or "")[:40],
        "message": str(payload.get("message") or "")[:300],
        "id": str(data.get("id") or "")[:120],
        "name": str(data.get("name") or "")[:200],
        "entity_type": str(data.get("type") or data.get("entity_type") or "")[:60],
    }


async def admit(
    redis: Any,
    *,
    assignment_id: str,
    organizations: dict[str, int],
    payload: dict[str, Any],
    now: datetime | None = None,
) -> bool:
    """Whether the event runs now; when every organization is over its cap it is held for the digest.

    *organizations* maps each concerned organization id to its cap. Each one is
    counted (``INCR``); the event runs while any of them is within its cap.
    """
    if not organizations:
        return True
    day = _day(now)
    within = False
    for organization_id, cap in organizations.items():
        key = _count_key(organization_id, day)
        used = int(await redis.incr(key))
        if used == 1:
            await redis.expire(key, _TTL_SECONDS)
        if used <= max(1, int(cap)):
            within = True
    if within:
        return True
    event = json.dumps(held_event(payload), sort_keys=True)
    for organization_id in organizations:
        key = _held_key(assignment_id, organization_id, day)
        await redis.rpush(key, event)
        await redis.ltrim(key, -_MAX_HELD, -1)
        await redis.expire(key, _TTL_SECONDS)
        await redis.sadd(_PENDING_KEY, key)
    await redis.expire(_PENDING_KEY, _TTL_SECONDS)
    return False


async def used_today(redis: Any, organization_id: str, now: datetime | None = None) -> int:
    """The runs an organization used today (UTC)."""
    raw = await redis.get(_count_key(organization_id, _day(now)))
    return int(raw or 0)


def digest_payload(organization_id: str, day: str, events: list[dict[str, Any]], held: int) -> dict[str, Any]:
    """The trigger payload of a digest run: the organization (so the run block carries it) and the events."""
    from app.services.organization_events import PAYLOAD_KEY

    return {
        "type": "organization_digest",
        "message": (
            f"{held} OpenCTI event(s) of {day[:4]}-{day[4:6]}-{day[6:]} held by the organization's daily run cap; "
            "review them together."
        ),
        PAYLOAD_KEY: [organization_id],
        DIGEST_PAYLOAD_KEY: {"day": day, "held": held, "events": events},
    }


async def due_digests(redis: Any, now: datetime | None = None) -> list[tuple[str, str, str, list[dict[str, Any]], int]]:
    """Claim the digests of past days: ``(assignment_id, organization_id, day, events, held)``.

    ``SREM`` is the claim: of two ticks, only the one that removed the member
    reads and deletes the list.
    """
    today = _day(now)
    found: list[tuple[str, str, str, list[dict[str, Any]], int]] = []
    for raw in list(await redis.smembers(_PENDING_KEY))[: _MAX_DIGESTS_PER_TICK * 5]:
        key = raw.decode() if isinstance(raw, bytes) else str(raw)
        parts = key.split(":")
        if len(parts) != 5 or parts[4] >= today:
            continue
        if not await redis.srem(_PENDING_KEY, key):
            continue
        items = await redis.lrange(key, 0, -1)
        await redis.delete(key)
        events: list[dict[str, Any]] = []
        for item in items:
            try:
                events.append(json.loads(item))
            except (TypeError, ValueError):
                continue
        if events:
            found.append((parts[2], parts[3], parts[4], events, len(events)))
        if len(found) >= _MAX_DIGESTS_PER_TICK:
            break
    return found


async def caps_of(db: Any, organization_ids: list[str]) -> dict[str, int]:
    """``{id: daily_run_cap}`` of the tracked organizations named (unknown or archived ones dropped)."""
    from sqlalchemy import select

    from app.models.organization import Organization

    ids: list[uuid.UUID] = []
    for value in organization_ids:
        try:
            ids.append(uuid.UUID(str(value)))
        except ValueError:
            continue
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(Organization.id, Organization.daily_run_cap).where(
                Organization.id.in_(ids),
                Organization.role == "tracked",
                Organization.archived_at.is_(None),
            )
        )
    ).all()
    return {str(r[0]): int(r[1] or DEFAULT_DAILY_RUN_CAP) for r in rows}


async def admit_delivery(db: Any, assignment_id: str, payload: dict[str, Any]) -> bool:
    """The receiver's gate: ``False`` when the event was held for tomorrow's digest. Redis down: runs."""
    from app.services.organization_events import PAYLOAD_KEY

    concerned = payload.get(PAYLOAD_KEY)
    if not isinstance(concerned, list) or not concerned:
        return True
    try:
        caps = await caps_of(db, [str(c) for c in concerned])
        if not caps:
            return True
        from app.core.redis import redis_client

        return await admit(await redis_client(), assignment_id=assignment_id, organizations=caps, payload=payload)
    except Exception as exc:  # the cap never drops an event
        logger.warning("Organization run cap unavailable, the event runs: %s", type(exc).__name__)
        return True


#: The window of the expected-runs estimate (section 9.7).
ESTIMATE_DAYS = 30


def estimate_filters(entity_types: list[str], identity_id: str, since_iso: str) -> dict[str, Any]:
    """The read-only count query of one purpose for one organization: created in the window, naming it."""
    filters: list[dict[str, Any]] = [{"key": ["created_at"], "values": [since_iso], "operator": "gte", "mode": "or"}]
    if entity_types:
        filters.append({"key": ["entity_type"], "values": sorted(entity_types), "operator": "eq", "mode": "or"})
    return {
        "mode": "and",
        "filters": filters,
        "filterGroups": [
            {
                "mode": "or",
                "filters": [
                    {"key": ["objects"], "values": [identity_id], "operator": "eq", "mode": "or"},
                    {
                        "key": ["regardingOf"],
                        "values": [{"key": "id", "values": [identity_id]}],
                        "operator": "eq",
                        "mode": "or",
                    },
                ],
                "filterGroups": [],
            }
        ],
    }


def serves(spec: dict[str, Any], organization: Any) -> bool:
    """Whether a provision spec's ``organizations`` scope covers this tracked organization."""
    from app.services.opencti_trigger_provisioning import PRIMARY_SCOPE, TRACKED_SCOPE

    scope = spec.get("organizations", PRIMARY_SCOPE)
    if scope == PRIMARY_SCOPE:
        return False
    return scope == TRACKED_SCOPE or getattr(organization, "relationship", None) in scope


async def estimate_runs(
    db: Any,
    gql: Any,
    organization: Any,
    now: datetime | None = None,
    *,
    agent_ids: list[uuid.UUID] | None = None,
) -> dict[str, Any]:
    """Expected runs per month of *organization*, per enabled assignment whose trigger would serve it.

    One read-only count query per assignment (``globalCount``); nothing is
    saved and no model is called. The cap bounds what is metered: at most
    ``daily_run_cap`` runs a day, plus one digest run on a day that held events.
    *agent_ids* narrows the assignments to those agents' (a fleet's members,
    ``GET /fleets/{id}/run-estimate``); ``None`` reads every agent's.
    """
    from datetime import timedelta

    from sqlalchemy import select

    from app.models.assignment import Assignment
    from app.services.opencti_trigger_provisioning import ProvisionSpecError, read_provision_spec

    since = ((now or datetime.now(UTC)) - timedelta(days=ESTIMATE_DAYS)).isoformat()
    cap = int(getattr(organization, "daily_run_cap", None) or DEFAULT_DAILY_RUN_CAP)
    identity = getattr(organization, "opencti_organization_id", None)
    query = select(Assignment).where(Assignment.enabled.is_(True))
    if agent_ids is not None:
        query = query.where(Assignment.agent_id.in_(agent_ids))
    # A fleet with no member serves nothing: no query.
    rows = (await db.execute(query)).scalars().all() if agent_ids is None or agent_ids else []
    purposes: list[dict[str, Any]] = []
    for assignment in rows:
        try:
            spec = read_provision_spec(assignment.trigger_config)
        except ProvisionSpecError:
            continue
        if spec is None or not serves(spec, organization):
            continue
        entry: dict[str, Any] = {
            "assignment_id": str(assignment.id),
            "assignment": assignment.name,
            "fleet": spec["fleet"],
            "purpose": spec["purpose"],
            "events_last_30_days": None,
            "expected_runs_per_month": None,
            "error": None,
        }
        if not identity:
            entry["error"] = "no OpenCTI identity linked"
        else:
            data, err = await gql(
                "query XtmRunEstimate($filters: FilterGroup) "
                "{ stixCoreObjects(first: 1, filters: $filters) { pageInfo { globalCount } } }",
                {"filters": estimate_filters(spec["entity_types"], identity, since)},
            )
            if err:
                entry["error"] = str(err)[:300]
            else:
                count = int((((data or {}).get("stixCoreObjects") or {}).get("pageInfo") or {}).get("globalCount") or 0)
                entry["events_last_30_days"] = count
                entry["expected_runs_per_month"] = min(count, cap * ESTIMATE_DAYS)
        purposes.append(entry)
    total = sum(p["expected_runs_per_month"] or 0 for p in purposes)
    return {
        "organization_id": str(organization.id),
        "monitoring": getattr(organization, "monitoring", "off"),
        "daily_run_cap": cap,
        "window_days": ESTIMATE_DAYS,
        "assignments": purposes,
        "expected_runs_per_month": total,
    }


async def dispatch_due_digests(db: Any) -> int:
    """Run the digests of past days, one run per (assignment, organization). Returns the runs started."""
    from app.core.redis import redis_client
    from app.models.agent import Agent
    from app.models.assignment import Assignment
    from app.services.assignment_engine import trigger_assignment

    started = 0
    for assignment_id, organization_id, day, events, held in await due_digests(await redis_client()):
        try:
            assignment = await db.get(Assignment, uuid.UUID(assignment_id))
        except ValueError:
            continue
        if assignment is None or not assignment.enabled:
            continue
        agent = await db.get(Agent, assignment.agent_id)
        if agent is None or not agent.enabled:
            continue
        payload = digest_payload(organization_id, day, events, held)
        run_id = await trigger_assignment(
            assignment=assignment,
            agent=agent,
            payload=payload,
            db=db,
            trigger_context={
                "type": "organization_digest",
                "organization_id": organization_id,
                "day": day,
                "triggered_at": datetime.now(UTC).isoformat(),
            },
        )
        if run_id:
            started += 1
    if started:
        await db.commit()
    return started
