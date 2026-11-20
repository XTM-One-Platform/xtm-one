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

A registered OpenCTI (OpenAEV and OpenCRQ in their slices) adds an optional
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
      "ingestion_lag_seconds": int
    }
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
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


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, int(value))


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
        disk = search.get("disk_used_percent")
        if isinstance(disk, (int, float)) and not isinstance(disk, bool):
            engine["disk_used_percent"] = min(100.0, max(0.0, float(disk)))
        if engine:
            kept["search_engine"] = engine
    lag = _count(raw.get("ingestion_lag_seconds"))
    if lag is not None:
        kept["ingestion_lag_seconds"] = lag
    return kept


async def record_snapshot(db: AsyncSession, registration: Any, raw: Any, *, published_default: bool) -> bool:
    """Store the snapshot a registration carried; whether one was stored. Never raises."""
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
                    )
                )
            else:
                stored.schema_version = SCHEMA_VERSION
                stored.snapshot = snapshot
                stored.received_at = now
            await db.flush()
        return True
    except Exception as exc:
        logger.warning(
            "Could not record the health snapshot of platform '%s': %s",
            registration.platform_identifier,
            type(exc).__name__,
        )
        return False
