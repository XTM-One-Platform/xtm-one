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

"""Platform Health snapshots pushed with the registration (#101 S5, ``dev-docs/platform-health.md`` section 5).

A registered OpenCTI, OpenAEV or OpenCRQ adds an optional
``health`` object to the registration it sends every 5 minutes. It rides on the
heartbeat, so nothing about it may refuse one: a snapshot that does not read,
is too large or names another format is ignored and logged, never a 422.

:func:`read_snapshot` keeps the known keys only, each bounded (numbers clamped
to non-negative integers, strings cut, at most :data:`MAX_CONNECTORS`
connectors); :func:`record_snapshot` stores it, one row per registration,
replaced, in a savepoint so a failed write leaves the registration unharmed.
Under the published default registration token nothing is recorded: its caller
authenticates nobody, and a forged snapshot would write findings.

Snapshot format, version :data:`SCHEMA_VERSION` (every key optional)::

    {
      "schema_version": 1,
      "connectors": [{"id", "name", "active": bool, "updated_at": ISO 8601,
                      "works_in_error": int, "queue_messages": int}],
      "rabbitmq": {"queue_messages": int, "consumers": int},
      "search_engine": {"status": "green" | "yellow" | "red", "disk_used_percent": number},
      "ingestion_lag_seconds": int,
      "healthchecks": [{"type": str, "status": "ERROR" | "WARNING", "count": int}],
      "integrations": [{"id", "name", "kind": "injector" | "collector" | "executor",
                        "updated_at": ISO 8601}],
      "agents": {"total": int, "offline": int},
      "jobs": [{"kind": str, "health": "running" | "queued" | "never_ran" | "healthy" | "failing"}]
    }

OpenCTI sends the first four keys, OpenAEV the next three (#101 S6), OpenCRQ
``jobs`` (#101 S7).
"""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: The snapshot format this release reads.
SCHEMA_VERSION = 1
#: A snapshot larger than this (its JSON) is ignored.
MAX_SNAPSHOT_BYTES = 256 * 1024
#: Connectors kept from one snapshot.
MAX_CONNECTORS = 500
_MAX_TEXT = 200
_SEARCH_STATUSES = ("green", "yellow", "red")
_HEALTHCHECK_STATUSES = ("ERROR", "WARNING")
_INTEGRATION_KINDS = ("injector", "collector", "executor")
#: Healthcheck rows (type and status pairs) kept from one snapshot.
MAX_HEALTHCHECKS = 100
#: The job healths OpenCRQ derives (``module/src/modules/jobs/health.ts``).
_JOB_HEALTHS = ("running", "queued", "never_ran", "healthy", "failing")
#: Job kinds kept from one snapshot.
MAX_JOBS = 200


def _finite(value: Any) -> float | int | None:
    """A number that reads (``NaN`` and the infinities, which ``json.loads`` accepts, do not)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        return value
    number: float = value
    return number if math.isfinite(number) else None


def _count(value: Any) -> int | None:
    number = _finite(value)
    return None if number is None else max(0, int(number))


def _text(value: Any) -> str | None:
    return value[:_MAX_TEXT] if isinstance(value, str) and value else None


def _moment(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _connector(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    connector_id = _text(entry.get("id"))
    if connector_id is None:
        return None
    kept: dict[str, Any] = {"id": connector_id, "name": _text(entry.get("name")) or connector_id}
    if isinstance(entry.get("active"), bool):
        kept["active"] = entry["active"]
    for key in ("works_in_error", "queue_messages"):
        number = _count(entry.get(key))
        if number is not None:
            kept[key] = number
    updated = _moment(entry.get("updated_at"))
    if updated is not None:
        kept["updated_at"] = updated
    return kept


def _healthcheck(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict) or entry.get("status") not in _HEALTHCHECK_STATUSES:
        return None
    kind = _text(entry.get("type"))
    count = _count(entry.get("count"))
    if kind is None or not count:
        return None
    return {"type": kind.lower(), "status": entry["status"], "count": count}


def _integration(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict) or entry.get("kind") not in _INTEGRATION_KINDS:
        return None
    integration_id = _text(entry.get("id"))
    if integration_id is None:
        return None
    kept: dict[str, Any] = {
        "id": integration_id,
        "name": _text(entry.get("name")) or integration_id,
        "kind": entry["kind"],
    }
    updated = _moment(entry.get("updated_at"))
    if updated is not None:
        kept["updated_at"] = updated
    return kept


def _job(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict) or entry.get("health") not in _JOB_HEALTHS:
        return None
    kind = _text(entry.get("kind"))
    return None if kind is None else {"kind": kind, "health": entry["health"]}


def read_snapshot(raw: Any) -> dict[str, Any] | None:
    """The snapshot as kept, or ``None`` when it does not read as one of :data:`SCHEMA_VERSION`."""
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        return None
    try:
        size = len(json.dumps(raw, default=str))
    except (TypeError, ValueError):
        return None
    if size > MAX_SNAPSHOT_BYTES:
        return None
    kept: dict[str, Any] = {}
    connectors = raw.get("connectors")
    if isinstance(connectors, list):
        kept["connectors"] = [c for c in (_connector(e) for e in connectors[:MAX_CONNECTORS]) if c is not None]
    rabbitmq = raw.get("rabbitmq")
    if isinstance(rabbitmq, dict):
        queue = {k: n for k in ("queue_messages", "consumers") if (n := _count(rabbitmq.get(k))) is not None}
        if queue:
            kept["rabbitmq"] = queue
    search = raw.get("search_engine")
    if isinstance(search, dict):
        engine: dict[str, Any] = {}
        if search.get("status") in _SEARCH_STATUSES:
            engine["status"] = search["status"]
        disk = _finite(search.get("disk_used_percent"))
        if disk is not None:
            engine["disk_used_percent"] = float(min(100, max(0, disk)))
        if engine:
            kept["search_engine"] = engine
    lag = _count(raw.get("ingestion_lag_seconds"))
    if lag is not None:
        kept["ingestion_lag_seconds"] = lag
    healthchecks = raw.get("healthchecks")
    if isinstance(healthchecks, list):
        kept["healthchecks"] = [h for h in (_healthcheck(e) for e in healthchecks[:MAX_HEALTHCHECKS]) if h is not None]
    integrations = raw.get("integrations")
    if isinstance(integrations, list):
        kept["integrations"] = [i for i in (_integration(e) for e in integrations[:MAX_CONNECTORS]) if i is not None]
    agents = raw.get("agents")
    if isinstance(agents, dict):
        fleet = {k: n for k in ("total", "offline") if (n := _count(agents.get(k))) is not None}
        if fleet:
            kept["agents"] = fleet
    jobs = raw.get("jobs")
    if isinstance(jobs, list):
        kept["jobs"] = [j for j in (_job(e) for e in jobs[:MAX_JOBS]) if j is not None]
    return kept


#: A heartbeat this long after the previous one (the platforms send one every 5 minutes) reads as a
#: restart of the platform (#958).
RESTART_GAP = timedelta(minutes=8)


def registration_restarted(
    previous_version: str | None, version: str | None, previous_heartbeat_at: datetime | None, now: datetime
) -> bool:
    """Whether a registration reads as a restart: its version changed, or it comes back after a gap."""
    if previous_version is None and previous_heartbeat_at is None:
        return False  # a first registration is not a restart
    if previous_version is not None and version is not None and previous_version != version:
        return True
    return previous_heartbeat_at is not None and now - previous_heartbeat_at > RESTART_GAP


async def record_snapshot(
    db: AsyncSession, registration: Any, raw: Any, *, published_default: bool, restarted: bool = False
) -> bool:
    """Store the snapshot a registration carried; whether one was stored. Never raises.

    *restarted* (:func:`registration_restarted`) stamps ``restarted_at``: the module's
    availability checks then open nothing for ``engine.RESTART_GRACE`` (#958).
    """
    from app.models.platform_health_snapshot import PlatformHealthSnapshot

    if raw is None or published_default:
        return False
    snapshot = read_snapshot(raw)
    if snapshot is None:
        logger.warning(
            "Platform '%s' sent a health snapshot that does not read as format %d or exceeds %d bytes; ignored",
            registration.platform_identifier,
            SCHEMA_VERSION,
            MAX_SNAPSHOT_BYTES,
        )
        return False
    try:
        async with db.begin_nested():
            stored = (
                await db.execute(
                    select(PlatformHealthSnapshot).where(PlatformHealthSnapshot.registration_id == registration.id)
                )
            ).scalar_one_or_none()
            now = datetime.now(UTC)
            if stored is None:
                db.add(
                    PlatformHealthSnapshot(
                        registration_id=registration.id,
                        schema_version=SCHEMA_VERSION,
                        snapshot=snapshot,
                        received_at=now,
                        restarted_at=now if restarted else None,
                    )
                )
            else:
                stored.schema_version = SCHEMA_VERSION
                stored.snapshot = snapshot
                stored.received_at = now
                if restarted:
                    stored.restarted_at = now
            await db.flush()
        return True
    except Exception as exc:
        logger.warning(
            "Could not record the health snapshot of platform '%s': %s",
            registration.platform_identifier,
            type(exc).__name__,
        )
        return False
