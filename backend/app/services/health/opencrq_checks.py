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

"""Platform Health OpenCRQ checks (#101 S7, ``dev-docs/platform-health.md`` section 4.5).

Registered on the engine when this module is imported, one result per registered
OpenCRQ (subject ``{"registration_id": ...}``); jobs one per job kind:

==================================  ==========  ============  ==============================================
Check                               Severity    Category      Fails when
==================================  ==========  ============  ==============================================
``opencrq.heartbeat``               critical    availability  no registration heartbeat for 15 minutes
``opencrq.job_failing``             warning     availability  the last run of a job kind ended in the dead-letter queue
==================================  ==========  ============  ==============================================

The heartbeat reads ``PlatformRegistration.is_healthy``. The job check reads the
latest fresh snapshot (same rule as the OpenCTI and OpenAEV checks): an OpenCRQ
release that sends none records nothing beyond the heartbeat.

Same gate as the XTM One platform checks: XTM One modes only, AI switched off
included, no model call, never metered, no network.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.docs_links import docs_url
from app.services.health.engine import Check, CheckResult, close_out, register_check
from app.services.health.opencti_checks import _registrations, with_restart_grace
from app.services.health.platform_checks import PLATFORM_CHECK_INTERVAL, _platform_check

MODULE = "opencrq"
PLATFORM_IDENTIFIER = "opencrq"
DOC_LINK = docs_url("administration/health", "opencrq-checks")
SOURCE = "OpenCRQ health snapshot"

Read = Callable[[Any, dict[str, Any] | None, datetime], list[CheckResult]]


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


def heartbeat(registration: Any, snapshot: dict[str, Any] | None, now: datetime) -> list[CheckResult]:
    last = registration.last_heartbeat_at
    return [
        _result(
            "opencrq.heartbeat",
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
                "Check that OpenCRQ is running and reaches XTM One (XTM_ONE__URL and XTM_ONE__TOKEN on the "
                "OpenCRQ side, PLATFORM_REGISTRATION_TOKEN here)."
            ),
        )
    ]


def job_failing(registration: Any, snapshot: dict[str, Any] | None, now: datetime) -> list[CheckResult]:
    """One result per job kind OpenCRQ reports; failing when its last run reached the dead-letter queue."""
    if snapshot is None or "jobs" not in snapshot:
        return []
    return [
        _result(
            "opencrq.job_failing",
            "warning",
            "availability",
            job["health"] == "failing",
            {**_subject(registration), "job": job["kind"]},
            evidence={"values": {"job": job["kind"], "health": job["health"]}, "source": SOURCE},
            recommendation=(
                "Open the job in OpenCRQ, read the error of its last run and retry it once the cause is fixed."
            ),
        )
        for job in snapshot["jobs"]
    ]


def _check(check_id: str, read: Read) -> Callable[[AsyncSession, datetime], Awaitable[list[CheckResult]]]:
    """``read`` over every registered OpenCRQ; a registration or subject gone is closed out."""

    async def run(db: AsyncSession, now: datetime) -> list[CheckResult]:
        results: list[CheckResult] = []
        for registration, snapshot in await _registrations(db, now, PLATFORM_IDENTIFIER):
            results.extend(read(registration, snapshot, now))
        results = await with_restart_grace(db, now, results, PLATFORM_IDENTIFIER)
        return results + await close_out(db, check_id, results)

    run.__name__ = read.__name__
    run.__doc__ = read.__doc__
    return run


for _check_id, _read in (
    ("opencrq.heartbeat", heartbeat),
    ("opencrq.job_failing", job_failing),
):
    register_check(
        Check(
            check_id=_check_id,
            module=MODULE,
            interval=PLATFORM_CHECK_INTERVAL,
            run=_platform_check(_check(_check_id, _read)),
        )
    )
