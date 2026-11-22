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

"""The module checkers' check reports in the digest and the API (#101 S15c-2, dev-docs 13.6, 13.8, 13.9 (m)).

A module-native fault per module yields a fail verdict and an open ``<module>.agentic.<check>``
finding with its recommendation; the next round with the fault removed resolves it. The cycle
digest carries the report counts, and ``GET /health/modules/{module}/checks`` the latest
report per registration, administrators only.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import platform_health_cycles
from app.core.config import settings
from app.core.database import get_db
from app.models.health import HealthCycle
from app.models.health_check import STATUS_OPEN, STATUS_RESOLVED, HealthCheckFinding
from app.models.health_module_analyst import HealthModuleCheckReport
from app.services.health.check_reports import agentic_check_id, apply_verdicts, clean_checks, counts
from app.services.health.digest import build_digest
from app.services.health.engine import finding_key
from tests.health_support import health_database, make_user

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)

#: The module-native fault of 13.9 (m) per module: the check that sees it.
FAULTS: dict[str, str] = {
    "opencti": "managers_running",
    "openaev": "scenarios_scheduled",
    "opencrq": "jobs_state",
}


@pytest.fixture
def xtm_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "platform_mode", "xtm_one")


async def _cycle(db: AsyncSession, registration: uuid.UUID, at: datetime) -> HealthCycle:
    cycle = HealthCycle(
        specialist="opencti_checker",
        trigger="schedule",
        target_type="module",
        target_id=registration,
        month=at.strftime("%Y-%m"),
        status="completed",
        opened_at=at - timedelta(minutes=5),
        closed_at=at,
    )
    db.add(cycle)
    await db.flush()
    return cycle


async def _round(
    db: AsyncSession, module: str, registration: uuid.UUID, check: str, verdict: str, at: datetime
) -> HealthCycle:
    """One checker round on *registration*: *check* with *verdict*, one other check passing."""
    entry: dict[str, Any] = {
        "id": check,
        "verdict": verdict,
        "category": "availability",
        "evidence": [{"tool": "stub", "values": {"fault": verdict == "fail"}}],
    }
    if verdict == "fail":
        entry["recommendation"] = "Restart it."
    verdicts = clean_checks([entry, {"id": "platform_errors", "verdict": "pass"}])
    cycle = await _cycle(db, registration, at)
    finding_ids, _ = await apply_verdicts(db, module=module, registration_id=registration, verdicts=verdicts, now=at)
    tally = counts(verdicts)
    db.add(
        HealthModuleCheckReport(
            registration_id=registration,
            cycle_id=cycle.id,
            module=module,
            started_at=at - timedelta(minutes=4),
            finished_at=at,
            checks=[v.as_entry(fid) for v, fid in zip(verdicts, finding_ids, strict=True)],
            **tally,
        )
    )
    await db.flush()
    return cycle


@asynccontextmanager
async def _client(sessions: async_sessionmaker[AsyncSession], user: Any) -> AsyncIterator[httpx.AsyncClient]:
    api = FastAPI()
    api.include_router(platform_health_cycles.router, prefix="/api/v1/health")

    async def _get_db() -> AsyncIterator[AsyncSession]:
        async with sessions() as db:
            yield db
            await db.commit()

    api.dependency_overrides[deps.get_current_user] = lambda: user
    api.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
@pytest.mark.parametrize("module", sorted(FAULTS))
async def test_a_module_fault_fails_its_check_and_the_next_round_resolves_it(module: str) -> None:
    registration = uuid.uuid4()
    check = FAULTS[module]
    key = finding_key(agentic_check_id(module, check), {"registration_id": str(registration)})
    async with health_database() as sessions, sessions() as db:
        faulty = await _round(db, module, registration, check, "fail", NOW - timedelta(hours=1))
        finding = await db.get(HealthCheckFinding, key)
        assert finding is not None
        assert (finding.status, finding.severity, finding.recommendation) == (STATUS_OPEN, "critical", "Restart it.")
        digest = await build_digest(db, faulty)
        assert digest.check_report is not None
        assert (digest.check_report["passed"], digest.check_report["failed"]) == (1, 1)
        assert digest.check_report["not_passed"] == [{"id": check, "verdict": "fail", "finding_id": key}]

        await _round(db, module, registration, check, "pass", NOW)
        await db.refresh(finding)
        assert finding.status == STATUS_RESOLVED


@pytest.mark.asyncio
async def test_a_round_without_a_report_is_listed_as_skipped_work() -> None:
    async with health_database() as sessions, sessions() as db:
        cycle = await _cycle(db, uuid.uuid4(), NOW)
        digest = await build_digest(db, cycle)
    assert digest.check_report == {"recorded": False}
    assert {"what": "check_report", "reason": "not_recorded"} in digest.skipped


@pytest.mark.asyncio
async def test_the_api_answers_the_latest_report_per_registration_to_an_administrator(xtm_one: None) -> None:
    admin, plain = make_user("admin", admin=True), make_user("plain")
    first, second = uuid.uuid4(), uuid.uuid4()
    async with health_database() as sessions:
        async with sessions() as db:
            db.add_all([admin, plain])
            await _round(db, "opencti", first, "managers_running", "fail", NOW - timedelta(hours=2))
            latest = await _round(db, "opencti", first, "managers_running", "pass", NOW)
            await _round(db, "opencti", second, "streams", "fail", NOW)
            await _round(db, "openaev", uuid.uuid4(), "scenarios_scheduled", "pass", NOW)
            await db.commit()
        async with _client(sessions, admin) as client:
            answer = await client.get("/api/v1/health/modules/opencti/checks")
            unknown = await client.get("/api/v1/health/modules/agents/checks")
        async with _client(sessions, plain) as client:
            refused = await client.get("/api/v1/health/modules/opencti/checks")

    assert answer.status_code == 200
    items = {item["registration_id"]: item for item in answer.json()["items"]}
    assert set(items) == {str(first), str(second)}
    assert items[str(first)]["cycle_id"] == str(latest.id)
    assert (items[str(first)]["passed"], items[str(first)]["failed"]) == (2, 0)
    assert [c["id"] for c in items[str(second)]["checks"]] == ["streams", "platform_errors"]
    assert unknown.status_code == 404
    assert refused.status_code == 403


@pytest.mark.asyncio
async def test_a_module_cycle_is_named_by_its_registration_title(xtm_one: None) -> None:
    """The Cycles tab titles a checker's round with the module and the registration (13.8)."""
    from app.models.platform_registration import PlatformRegistration

    admin = make_user("admin", admin=True)
    registered, gone = uuid.uuid4(), uuid.uuid4()
    async with health_database() as sessions:
        async with sessions() as db:
            db.add(admin)
            db.add(
                PlatformRegistration(
                    id=registered,
                    platform_identifier="opencti",
                    platform_url="https://cti.example.com",
                    platform_title="OpenCTI Europe",
                    platform_version="6.9.0",
                )
            )
            await db.flush()
            named = await _cycle(db, registered, NOW)
            orphan = await _cycle(db, gone, NOW - timedelta(hours=1))
            await db.commit()
        async with _client(sessions, admin) as client:
            listed = await client.get("/api/v1/health/cycles")
            one = await client.get(f"/api/v1/health/cycles/{named.id}")

    assert listed.status_code == 200
    names = {item["id"]: item["target_name"] for item in listed.json()}
    assert names[str(named.id)] == "OpenCTI Europe"
    assert names[str(orphan.id)] is None
    assert one.json()["target_name"] == "OpenCTI Europe"
