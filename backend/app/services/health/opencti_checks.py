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

"""Platform Health OpenCTI checks (#101 S5, ``dev-docs/platform-health.md`` section 4.3).

Registered on the engine when this module is imported, one result per registered
OpenCTI (subject ``{"registration_id": ...}``), connectors one per connector:

==================================  ==========  ============  ==============================================
Check                               Severity    Category      Fails when
==================================  ==========  ============  ==============================================
``opencti.heartbeat``               critical    availability  no registration heartbeat for 15 minutes
``opencti.connector_stale``         warning     availability  an active connector has not reported for 30 minutes (E)
``opencti.connector_errors``        warning     availability  a connector has over 10 works in error (E)
``opencti.queue_depth``             warning     capacity      over 100,000 messages wait in the broker (E)
``opencti.queue_consumers``         critical    availability  messages wait in the broker and no consumer reads them
``opencti.search_engine``           warn/crit   availability  the search engine is yellow (warning) or red (critical)
``opencti.search_disk``             warning     capacity      the search engine disk is over 85 % used (E)
``opencti.ingestion_lag``           warning     performance   ingestion lags by over 15 minutes (E)
==================================  ==========  ============  ==============================================

The heartbeat reads ``PlatformRegistration.is_healthy``. Every other check reads
the latest snapshot the platform pushed with its registration
(:mod:`app.services.health.snapshots`), and only while that snapshot is fresh
(:data:`SNAPSHOT_FRESH_FOR`): a platform that stopped sending, or a release that
sends none, records nothing beyond the heartbeat. A key the snapshot does not
carry records nothing for its check.

The thresholds marked (E) are the defaults of
:class:`app.services.health.settings.HealthOpenCTIThresholds`; an administrator
edits them in Settings > Platform Health and the next pass reads them (#101 S10b).

Same gate as the XTM One platform checks: XTM One modes only, AI switched off
included, no model call, never metered, no network.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.docs_links import docs_url
from app.services.health.engine import RESTART_GRACE, Check, CheckResult, apply_grace, close_out, register_check
from app.services.health.platform_checks import PLATFORM_CHECK_INTERVAL, _platform_check
from app.services.health.settings import HealthOpenCTIThresholds, load_health_settings

MODULE = "opencti"
PLATFORM_IDENTIFIER = "opencti"
#: A snapshot older than this is not read (three missed heartbeats).
SNAPSHOT_FRESH_FOR = timedelta(minutes=15)
#: The thresholds when none is stored (Settings > Platform Health, #101 S10b).
DEFAULTS = HealthOpenCTIThresholds()
DOC_LINK = docs_url("administration/health", "opencti-checks")

Read = Callable[[Any, dict[str, Any] | None, datetime, HealthOpenCTIThresholds], list[CheckResult]]


def _result(
    check_id: str, severity: str, category: str, failing: bool, subject: dict[str, str], **kw: Any
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        module=MODULE,
        category=category,
        severity=severity,
        failing=failing,
        subject=subject,
        doc_link=DOC_LINK,
        **kw,
    )


async def _registrations(
    db: AsyncSession, now: datetime, platform_identifier: str = PLATFORM_IDENTIFIER
) -> list[tuple[Any, dict[str, Any] | None]]:
    """Each registered platform of that kind with its snapshot, ``None`` when it has none or it is stale."""
    from app.models.platform_health_snapshot import PlatformHealthSnapshot
    from app.models.platform_registration import PlatformRegistration

    rows = (
        await db.execute(
            select(PlatformRegistration, PlatformHealthSnapshot)
            .outerjoin(PlatformHealthSnapshot, PlatformHealthSnapshot.registration_id == PlatformRegistration.id)
            .where(PlatformRegistration.platform_identifier == platform_identifier)
        )
    ).all()
    out: list[tuple[Any, dict[str, Any] | None]] = []
    for registration, snapshot in rows:
        fresh = snapshot is not None and now - snapshot.received_at <= SNAPSHOT_FRESH_FOR
        out.append((registration, snapshot.snapshot if fresh else None))
    return out


def _subject(registration: Any) -> dict[str, str]:
    return {"registration_id": str(registration.id)}


async def restarting_registrations(
    db: AsyncSession, now: datetime, platform_identifier: str = PLATFORM_IDENTIFIER
) -> frozenset[str]:
    """The ids of the registrations of that kind that restarted less than ``engine.RESTART_GRACE`` ago (#958)."""
    from app.models.platform_health_snapshot import PlatformHealthSnapshot
    from app.models.platform_registration import PlatformRegistration

    rows = await db.execute(
        select(PlatformHealthSnapshot.registration_id)
        .join(PlatformRegistration, PlatformRegistration.id == PlatformHealthSnapshot.registration_id)
        .where(
            PlatformRegistration.platform_identifier == platform_identifier,
            PlatformHealthSnapshot.restarted_at.is_not(None),
            PlatformHealthSnapshot.restarted_at > now - RESTART_GRACE,
        )
    )
    return frozenset(str(rid) for rid in rows.scalars())


async def with_restart_grace(
    db: AsyncSession, now: datetime, results: list[CheckResult], platform_identifier: str = PLATFORM_IDENTIFIER
) -> list[CheckResult]:
    """*results* with the failing availability reads of a restarting registration marked ``in_grace`` (#958)."""
    restarting = await restarting_registrations(db, now, platform_identifier)
    if not restarting:
        return results
    return apply_grace(results, lambda r: (r.subject or {}).get("registration_id") in restarting)


def heartbeat(
    registration: Any, snapshot: dict[str, Any] | None, now: datetime, limits: HealthOpenCTIThresholds = DEFAULTS
) -> list[CheckResult]:
    last = registration.last_heartbeat_at
    return [
        _result(
            "opencti.heartbeat",
            "critical",
            "availability",
            not registration.is_healthy(now),
            _subject(registration),
            evidence={
                "values": {"status": registration.status, "last_heartbeat_at": last.isoformat() if last else None},
                "threshold": "15 minutes",
                "source": "platform registration",
            },
            recommendation=(
                "Check that OpenCTI is running and reaches XTM One (XTM_ONE_URL and XTM_ONE_TOKEN on the "
                "OpenCTI side, PLATFORM_REGISTRATION_TOKEN here)."
            ),
        )
    ]


def _connectors(snapshot: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    if snapshot is None or "connectors" not in snapshot:
        return None
    connectors: list[dict[str, Any]] = snapshot["connectors"]
    return connectors


def connector_stale(
    registration: Any, snapshot: dict[str, Any] | None, now: datetime, limits: HealthOpenCTIThresholds = DEFAULTS
) -> list[CheckResult]:
    connectors = _connectors(snapshot)
    if connectors is None:
        return []
    stale_after = timedelta(minutes=limits.connector_stale_minutes)
    results = []
    for connector in connectors:
        if connector.get("active") is not True or "updated_at" not in connector:
            continue
        updated = datetime.fromisoformat(connector["updated_at"])
        results.append(
            _result(
                "opencti.connector_stale",
                "warning",
                "availability",
                now - updated > stale_after,
                {**_subject(registration), "connector_id": connector["id"]},
                evidence={
                    "values": {"name": connector["name"], "updated_at": connector["updated_at"]},
                    "threshold": f"{limits.connector_stale_minutes} minutes",
                    "source": "OpenCTI health snapshot",
                },
                recommendation="Restart the connector or check its logs; it has stopped reporting to OpenCTI.",
            )
        )
    return results


def connector_errors(
    registration: Any, snapshot: dict[str, Any] | None, now: datetime, limits: HealthOpenCTIThresholds = DEFAULTS
) -> list[CheckResult]:
    connectors = _connectors(snapshot)
    if connectors is None:
        return []
    return [
        _result(
            "opencti.connector_errors",
            "warning",
            "availability",
            connector["works_in_error"] > limits.connector_errors_max,
            {**_subject(registration), "connector_id": connector["id"]},
            evidence={
                "values": {"name": connector["name"], "works_in_error": connector["works_in_error"]},
                "threshold": limits.connector_errors_max,
                "source": "OpenCTI health snapshot",
            },
            recommendation="Open the connector's works in OpenCTI and fix the cause of the errors.",
        )
        for connector in connectors
        if "works_in_error" in connector
    ]


def queue_depth(
    registration: Any, snapshot: dict[str, Any] | None, now: datetime, limits: HealthOpenCTIThresholds = DEFAULTS
) -> list[CheckResult]:
    queue = (snapshot or {}).get("rabbitmq") or {}
    if "queue_messages" not in queue:
        return []
    return [
        _result(
            "opencti.queue_depth",
            "warning",
            "capacity",
            queue["queue_messages"] > limits.queue_depth_max,
            _subject(registration),
            evidence={
                "values": {"queue_messages": queue["queue_messages"]},
                "threshold": limits.queue_depth_max,
                "source": "OpenCTI health snapshot",
            },
            recommendation="Add OpenCTI workers or slow the connectors that fill the queue.",
        )
    ]


def queue_consumers(
    registration: Any, snapshot: dict[str, Any] | None, now: datetime, limits: HealthOpenCTIThresholds = DEFAULTS
) -> list[CheckResult]:
    queue = (snapshot or {}).get("rabbitmq") or {}
    if "consumers" not in queue or "queue_messages" not in queue:
        return []
    return [
        _result(
            "opencti.queue_consumers",
            "critical",
            "availability",
            queue["consumers"] == 0 and queue["queue_messages"] > 0,
            _subject(registration),
            evidence={"values": dict(queue), "source": "OpenCTI health snapshot"},
            recommendation="Start the OpenCTI workers: messages wait in the broker and nothing reads them.",
        )
    ]


def search_engine(
    registration: Any, snapshot: dict[str, Any] | None, now: datetime, limits: HealthOpenCTIThresholds = DEFAULTS
) -> list[CheckResult]:
    status = ((snapshot or {}).get("search_engine") or {}).get("status")
    if status is None:
        return []
    return [
        _result(
            "opencti.search_engine",
            "critical" if status == "red" else "warning",
            "availability",
            status != "green",
            _subject(registration),
            evidence={"values": {"status": status}, "source": "OpenCTI health snapshot"},
            recommendation="Check the Elasticsearch or OpenSearch cluster health: unassigned shards or a lost node.",
        )
    ]


def search_disk(
    registration: Any, snapshot: dict[str, Any] | None, now: datetime, limits: HealthOpenCTIThresholds = DEFAULTS
) -> list[CheckResult]:
    used = ((snapshot or {}).get("search_engine") or {}).get("disk_used_percent")
    if used is None:
        return []
    return [
        _result(
            "opencti.search_disk",
            "warning",
            "capacity",
            used > limits.search_disk_max_percent,
            _subject(registration),
            evidence={
                "values": {"disk_used_percent": used},
                "threshold": limits.search_disk_max_percent,
                "source": "OpenCTI health snapshot",
            },
            recommendation="Free or add disk on the search engine nodes, or set retention rules in OpenCTI.",
        )
    ]


def ingestion_lag(
    registration: Any, snapshot: dict[str, Any] | None, now: datetime, limits: HealthOpenCTIThresholds = DEFAULTS
) -> list[CheckResult]:
    lag = (snapshot or {}).get("ingestion_lag_seconds")
    if lag is None:
        return []
    return [
        _result(
            "opencti.ingestion_lag",
            "warning",
            "performance",
            lag > limits.ingestion_lag_minutes * 60,
            _subject(registration),
            evidence={
                "values": {"ingestion_lag_seconds": lag},
                "threshold": limits.ingestion_lag_minutes * 60,
                "source": "OpenCTI health snapshot",
            },
            recommendation="Add OpenCTI workers or check the broker: ingestion is falling behind.",
        )
    ]


def _check(check_id: str, read: Read) -> Callable[[AsyncSession, datetime], Awaitable[list[CheckResult]]]:
    """``read`` over every registered OpenCTI with the stored thresholds; a registration or subject gone is closed out."""

    async def run(db: AsyncSession, now: datetime) -> list[CheckResult]:
        limits = (await load_health_settings(db)).opencti
        results: list[CheckResult] = []
        for registration, snapshot in await _registrations(db, now):
            results.extend(read(registration, snapshot, now, limits))
        results = await with_restart_grace(db, now, results)
        return results + await close_out(db, check_id, results)

    run.__name__ = read.__name__
    run.__doc__ = read.__doc__
    return run


for _check_id, _read in (
    ("opencti.heartbeat", heartbeat),
    ("opencti.connector_stale", connector_stale),
    ("opencti.connector_errors", connector_errors),
    ("opencti.queue_depth", queue_depth),
    ("opencti.queue_consumers", queue_consumers),
    ("opencti.search_engine", search_engine),
    ("opencti.search_disk", search_disk),
    ("opencti.ingestion_lag", ingestion_lag),
):
    register_check(
        Check(
            check_id=_check_id,
            module=MODULE,
            interval=PLATFORM_CHECK_INTERVAL,
            run=_platform_check(_check(_check_id, _read)),
        )
    )
