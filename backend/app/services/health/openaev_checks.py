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

"""Platform Health OpenAEV checks (#101 S6, ``dev-docs/platform-health.md`` section 4.4).

Registered on the engine when this module is imported, one result per registered
OpenAEV (subject ``{"registration_id": ...}``); healthchecks one per type,
integrations one per injector, collector or executor:

==================================  ==========  ============  ==============================================
Check                               Severity    Category      Fails when
==================================  ==========  ============  ==============================================
``openaev.heartbeat``               critical    availability  no registration heartbeat for 15 minutes
``openaev.healthcheck``             warn/crit   availability  OpenAEV reports a failing healthcheck (ERROR critical)
``openaev.integration_silent``      warning     availability  an injector, collector or executor silent for 60 minutes (E)
``openaev.agents_offline``          warning     availability  over half of the agents are offline (E)
==================================  ==========  ============  ==============================================

The heartbeat reads ``PlatformRegistration.is_healthy``. Every other check reads
the latest fresh snapshot (same rule as the OpenCTI checks,
:mod:`app.services.health.opencti_checks`): a release that sends none records
nothing beyond the heartbeat, and a key the snapshot does not carry records
nothing for its check.

The thresholds marked (E) are the defaults of
:class:`app.services.health.settings.HealthOpenAEVThresholds`; an administrator
edits them in Settings > Platform Health and the next pass reads them (#101 S10b).

Same gate as the XTM One platform checks: XTM One modes only, AI switched off
included, no model call, never metered, no network.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.docs_links import docs_url
from app.services.health.engine import Check, CheckResult, close_out, register_check
from app.services.health.opencti_checks import _registrations, with_restart_grace
from app.services.health.platform_checks import PLATFORM_CHECK_INTERVAL, _platform_check
from app.services.health.settings import HealthOpenAEVThresholds, load_health_settings

MODULE = "openaev"
PLATFORM_IDENTIFIER = "openaev"
#: The thresholds when none is stored (Settings > Platform Health, #101 S10b).
DEFAULTS = HealthOpenAEVThresholds()
DOC_LINK = docs_url("administration/health", "openaev-checks")
SOURCE = "OpenAEV health snapshot"

Read = Callable[[Any, dict[str, Any] | None, datetime, HealthOpenAEVThresholds], list[CheckResult]]


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


def _subject(registration: Any) -> dict[str, str]:
    return {"registration_id": str(registration.id)}


def heartbeat(
    registration: Any, snapshot: dict[str, Any] | None, now: datetime, limits: HealthOpenAEVThresholds = DEFAULTS
) -> list[CheckResult]:
    last = registration.last_heartbeat_at
    return [
        _result(
            "openaev.heartbeat",
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
                "Check that OpenAEV is running and reaches XTM One (the XTM One URL and token on the "
                "OpenAEV side, PLATFORM_REGISTRATION_TOKEN here)."
            ),
        )
    ]


def healthcheck(
    registration: Any, snapshot: dict[str, Any] | None, now: datetime, limits: HealthOpenAEVThresholds = DEFAULTS
) -> list[CheckResult]:
    """One result per healthcheck type OpenAEV reports, its most severe status wins."""
    if snapshot is None or "healthchecks" not in snapshot:
        return []
    by_type: dict[str, dict[str, int]] = {}
    for row in snapshot["healthchecks"]:
        counts = by_type.setdefault(row["type"], {})
        counts[row["status"]] = counts.get(row["status"], 0) + row["count"]
    return [
        _result(
            "openaev.healthcheck",
            "critical" if "ERROR" in counts else "warning",
            "availability",
            True,
            {**_subject(registration), "type": kind},
            evidence={"values": {"type": kind, **counts}, "source": SOURCE},
            recommendation=(
                "Open the scenarios and simulations OpenAEV flags for this check and fix the service, "
                "agent, collector or content it names."
            ),
        )
        for kind, counts in sorted(by_type.items())
    ]


def integration_silent(
    registration: Any, snapshot: dict[str, Any] | None, now: datetime, limits: HealthOpenAEVThresholds = DEFAULTS
) -> list[CheckResult]:
    if snapshot is None or "integrations" not in snapshot:
        return []
    silent_after = timedelta(minutes=limits.integration_silent_minutes)
    results = []
    for integration in snapshot["integrations"]:
        if "updated_at" not in integration:
            continue
        updated = datetime.fromisoformat(integration["updated_at"])
        results.append(
            _result(
                "openaev.integration_silent",
                "warning",
                "availability",
                now - updated > silent_after,
                {**_subject(registration), "integration_id": integration["id"]},
                evidence={
                    "values": {
                        "name": integration["name"],
                        "kind": integration["kind"],
                        "updated_at": integration["updated_at"],
                    },
                    "threshold": f"{limits.integration_silent_minutes} minutes",
                    "source": SOURCE,
                },
                recommendation=f"Restart the {integration['kind']} or check its logs; it has stopped reporting.",
            )
        )
    return results


def agents_offline(
    registration: Any, snapshot: dict[str, Any] | None, now: datetime, limits: HealthOpenAEVThresholds = DEFAULTS
) -> list[CheckResult]:
    agents = (snapshot or {}).get("agents") or {}
    if "total" not in agents or "offline" not in agents:
        return []
    total, offline = agents["total"], agents["offline"]
    return [
        _result(
            "openaev.agents_offline",
            "warning",
            "availability",
            total > 0 and offline / total * 100 > limits.agents_offline_max_percent,
            _subject(registration),
            evidence={
                "values": {"total": total, "offline": offline},
                "threshold": f"{limits.agents_offline_max_percent:g} % offline",
                "source": SOURCE,
            },
            recommendation="Check the endpoints and the agent service: simulations cannot run on offline agents.",
        )
    ]


def _check(check_id: str, read: Read) -> Callable[[AsyncSession, datetime], Awaitable[list[CheckResult]]]:
    """``read`` over every registered OpenAEV with the stored thresholds; a registration or subject gone is closed out."""

    async def run(db: AsyncSession, now: datetime) -> list[CheckResult]:
        limits = (await load_health_settings(db)).openaev
        results: list[CheckResult] = []
        for registration, snapshot in await _registrations(db, now, PLATFORM_IDENTIFIER):
            results.extend(read(registration, snapshot, now, limits))
        results = await with_restart_grace(db, now, results, PLATFORM_IDENTIFIER)
        return results + await close_out(db, check_id, results)

    run.__name__ = read.__name__
    run.__doc__ = read.__doc__
    return run


for _check_id, _read in (
    ("openaev.heartbeat", heartbeat),
    ("openaev.healthcheck", healthcheck),
    ("openaev.integration_silent", integration_silent),
    ("openaev.agents_offline", agents_offline),
):
    register_check(
        Check(
            check_id=_check_id,
            module=MODULE,
            interval=PLATFORM_CHECK_INTERVAL,
            run=_platform_check(_check(_check_id, _read)),
        )
    )
