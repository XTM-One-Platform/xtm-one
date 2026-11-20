"""Platform Health OpenCRQ checks on PostgreSQL (#101 S7, dev-docs/platform-health.md 4.5):
one result per registered OpenCRQ, read from the heartbeat and the latest fresh snapshot,
XTM One modes only, resolved once the cause is gone."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.health_check import STATUS_OPEN, STATUS_RESOLVED, HealthCheckFinding
from app.models.platform_health_snapshot import PlatformHealthSnapshot
from app.models.platform_registration import PlatformRegistration
from app.services.health import engine, opencrq_checks, platform_checks
from app.services.health.snapshots import read_snapshot
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 20, 6, 0, tzinfo=UTC)
CHECKS = [c for c in engine.registered_checks() if c.module == opencrq_checks.MODULE]

UNHEALTHY = {
    "schema_version": 1,
    "jobs": [
        {"kind": "risk-quantification", "health": "failing"},
        {"kind": "asset-sync", "health": "healthy"},
        {"kind": "report-export", "health": "never_ran"},
    ],
}
HEALTHY = {
    "schema_version": 1,
    "jobs": [
        {"kind": "risk-quantification", "health": "running"},
        {"kind": "asset-sync", "health": "healthy"},
    ],
}


@pytest.fixture(autouse=True)
def _xtm_one_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "platform_mode", "xtm_one")
    engine._LAST_RUN.clear()


async def _register(
    db: AsyncSession, heartbeat: datetime, snapshot: dict[str, Any] | None, identifier: str = "opencrq"
) -> PlatformRegistration:
    reg = PlatformRegistration(
        id=uuid.uuid4(),
        platform_identifier=identifier,
        platform_url=f"https://{identifier}.example.com",
        platform_title=identifier,
        platform_version="1.0.0",
        status="active",
        last_heartbeat_at=heartbeat,
    )
    db.add(reg)
    await db.flush()
    if snapshot is not None:
        kept = read_snapshot(snapshot)
        assert kept is not None
        db.add(PlatformHealthSnapshot(registration_id=reg.id, schema_version=1, snapshot=kept, received_at=heartbeat))
        await db.flush()
    return reg


async def _open(db: AsyncSession) -> dict[tuple[str, str], HealthCheckFinding]:
    rows = (await db.execute(select(HealthCheckFinding))).scalars().all()
    return {(r.check_id, r.subject.get("job") or ""): r for r in rows if r.status == STATUS_OPEN}


async def _pass(db: AsyncSession, now: datetime) -> dict[str, int]:
    engine._LAST_RUN.clear()
    return await engine.run_pass(db, now=now, checks=CHECKS)


def test_the_opencrq_checks_are_registered_on_the_engine() -> None:
    assert {c.check_id for c in CHECKS} == {"opencrq.heartbeat", "opencrq.job_failing"}
    assert {c.interval for c in CHECKS} == {platform_checks.PLATFORM_CHECK_INTERVAL}


def test_the_snapshot_keeps_the_opencrq_jobs_bounded() -> None:
    kept = read_snapshot(
        {
            "schema_version": 1,
            "jobs": [
                {"kind": "risk-quantification", "health": "failing"},
                {"kind": "asset-sync", "health": "broken"},
                {"kind": "", "health": "healthy"},
                {"health": "queued"},
                "not a row",
            ],
        }
    )
    assert kept == {"jobs": [{"kind": "risk-quantification", "health": "failing"}]}


@pytest.mark.asyncio
async def test_a_failing_job_opens_one_finding_and_resolves_once_it_runs_again() -> None:
    async with _temp_database("hcq_findings") as sessions, sessions() as db:
        reg = await _register(db, T0, UNHEALTHY)
        totals = await _pass(db, T0)
        assert totals["checks"] == 2
        found = await _open(db)
        assert set(found) == {("opencrq.job_failing", "risk-quantification")}
        finding = found[("opencrq.job_failing", "risk-quantification")]
        assert finding.severity == "warning"
        assert finding.module == "opencrq" and finding.subject["registration_id"] == str(reg.id)

        row = (
            await db.execute(select(PlatformHealthSnapshot).where(PlatformHealthSnapshot.registration_id == reg.id))
        ).scalar_one()
        for minutes in (5, 10):  # auto-resolve takes two healthy passes
            later = T0 + timedelta(minutes=minutes)
            row.snapshot = read_snapshot(HEALTHY) or {}
            row.received_at = later
            reg.last_heartbeat_at = later
            await db.flush()
            await _pass(db, later)
        assert await _open(db) == {}
        rows = (await db.execute(select(HealthCheckFinding))).scalars().all()
        assert [r.status for r in rows] == [STATUS_RESOLVED]


@pytest.mark.asyncio
async def test_a_missed_heartbeat_is_critical_and_a_stale_snapshot_is_not_read() -> None:
    async with _temp_database("hcq_heartbeat") as sessions, sessions() as db:
        await _register(db, T0 - timedelta(minutes=20), UNHEALTHY)
        await _pass(db, T0)
        found = await _open(db)
        assert set(found) == {("opencrq.heartbeat", "")}
        assert found[("opencrq.heartbeat", "")].severity == "critical"


@pytest.mark.asyncio
async def test_an_openaev_registration_is_not_read_by_the_opencrq_checks() -> None:
    async with _temp_database("hcq_other") as sessions, sessions() as db:
        await _register(db, T0 - timedelta(hours=1), UNHEALTHY, identifier="openaev")
        await _pass(db, T0)
        assert (await db.execute(select(HealthCheckFinding))).scalars().all() == []


@pytest.mark.asyncio
async def test_ai_disabled_mode_still_records_the_opencrq_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "platform_mode", "ai_disabled")
    async with _temp_database("hcq_ai_off") as sessions, sessions() as db:
        await _register(db, T0, UNHEALTHY)
        await _pass(db, T0)
        assert set(await _open(db)) == {("opencrq.job_failing", "risk-quantification")}


@pytest.mark.asyncio
async def test_copilot_mode_records_no_opencrq_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "platform_mode", "copilot")
    async with _temp_database("hcq_copilot") as sessions, sessions() as db:
        await _register(db, T0 - timedelta(hours=1), UNHEALTHY)
        await _pass(db, T0)
        assert (await db.execute(select(HealthCheckFinding))).scalars().all() == []
