"""Platform Health OpenAEV checks on PostgreSQL (#101 S6, dev-docs/platform-health.md 4.4):
one result per registered OpenAEV, read from the heartbeat and the latest fresh snapshot,
XTM One modes only, resolved once the cause is gone; and acceptance test (c) of section 1.4:
a registered OpenCTI and OpenAEV show findings after the first pass, no admin action, AI
switched off included."""

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
from app.services.health import engine, openaev_checks, opencti_checks, platform_checks
from app.services.health.snapshots import read_snapshot
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 20, 5, 0, tzinfo=UTC)
CHECKS = [c for c in engine.registered_checks() if c.module == openaev_checks.MODULE]
MODULE_CHECKS = [c for c in engine.registered_checks() if c.module in (openaev_checks.MODULE, opencti_checks.MODULE)]

UNHEALTHY = {
    "schema_version": 1,
    "healthchecks": [
        {"type": "SMTP", "status": "ERROR", "count": 3},
        {"type": "SMTP", "status": "WARNING", "count": 1},
        {"type": "AGENT_OR_EXECUTOR", "status": "WARNING", "count": 2},
        {"type": "NMAP", "status": "ERROR", "count": 0},
    ],
    "integrations": [
        {"id": "i-silent", "name": "Nuclei", "kind": "injector", "updated_at": (T0 - timedelta(hours=2)).isoformat()},
        {"id": "i-live", "name": "Crowdstrike", "kind": "collector", "updated_at": T0.isoformat()},
    ],
    "agents": {"total": 10, "offline": 8},
}
HEALTHY = {
    "healthchecks": [],
    "integrations": [
        {"id": "i-silent", "name": "Nuclei", "kind": "injector", "updated_at": T0.isoformat()},
        {"id": "i-live", "name": "Crowdstrike", "kind": "collector", "updated_at": T0.isoformat()},
    ],
    "agents": {"total": 10, "offline": 1},
}


@pytest.fixture(autouse=True)
def _xtm_one_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "platform_mode", "xtm_one")
    engine._LAST_RUN.clear()


async def _register(
    db: AsyncSession, heartbeat: datetime, snapshot: dict[str, Any] | None, identifier: str = "openaev"
) -> PlatformRegistration:
    reg = PlatformRegistration(
        id=uuid.uuid4(),
        platform_identifier=identifier,
        platform_url=f"https://{identifier}.example.com",
        platform_title=identifier,
        platform_version="2.0.0",
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


async def _set_snapshot(db: AsyncSession, reg: PlatformRegistration, kept: dict[str, Any], at: datetime) -> None:
    row = (
        await db.execute(select(PlatformHealthSnapshot).where(PlatformHealthSnapshot.registration_id == reg.id))
    ).scalar_one()
    row.snapshot = kept
    row.received_at = at
    reg.last_heartbeat_at = at
    await db.flush()


def _key(row: HealthCheckFinding) -> tuple[str, str]:
    return (row.check_id, row.subject.get("type") or row.subject.get("integration_id") or "")


async def _open(db: AsyncSession) -> dict[tuple[str, str], HealthCheckFinding]:
    rows = (await db.execute(select(HealthCheckFinding))).scalars().all()
    return {_key(r): r for r in rows if r.status == STATUS_OPEN}


async def _pass(db: AsyncSession, now: datetime, checks: list[engine.Check] = CHECKS) -> dict[str, int]:
    engine._LAST_RUN.clear()
    return await engine.run_pass(db, now=now, checks=checks)


def test_the_openaev_checks_are_registered_on_the_engine() -> None:
    assert {c.check_id for c in CHECKS} == {
        "openaev.heartbeat",
        "openaev.healthcheck",
        "openaev.integration_silent",
        "openaev.agents_offline",
    }
    assert {c.interval for c in CHECKS} == {platform_checks.PLATFORM_CHECK_INTERVAL}


def test_the_snapshot_keeps_the_openaev_keys_bounded() -> None:
    kept = read_snapshot(
        {
            "schema_version": 1,
            "healthchecks": [
                {"type": "SMTP", "status": "ERROR", "count": 2},
                {"type": "SMTP", "status": "BROKEN", "count": 2},
                {"type": "IMAP", "status": "WARNING", "count": float("nan")},
                "not a row",
            ],
            "integrations": [
                {"id": "a", "kind": "executor", "updated_at": "2026-11-20T05:00:00Z"},
                {"id": "b", "kind": "printer"},
            ],
            "agents": {"total": -3, "offline": "many"},
        }
    )
    assert kept == {
        "healthchecks": [{"type": "smtp", "status": "ERROR", "count": 2}],
        "integrations": [{"id": "a", "name": "a", "kind": "executor", "updated_at": "2026-11-20T05:00:00+00:00"}],
        "agents": {"total": 0},
    }


@pytest.mark.asyncio
async def test_an_unhealthy_snapshot_opens_one_finding_per_cause_and_resolves_once_healthy() -> None:
    async with _temp_database("hoa_findings") as sessions, sessions() as db:
        reg = await _register(db, T0, UNHEALTHY)
        totals = await _pass(db, T0)
        assert totals["checks"] == 4
        found = await _open(db)
        assert set(found) == {
            ("openaev.healthcheck", "smtp"),
            ("openaev.healthcheck", "agent_or_executor"),
            ("openaev.integration_silent", "i-silent"),
            ("openaev.agents_offline", ""),
        }
        assert found[("openaev.healthcheck", "smtp")].severity == "critical"
        assert found[("openaev.healthcheck", "agent_or_executor")].severity == "warning"
        assert all(r.module == "openaev" and r.subject["registration_id"] == str(reg.id) for r in found.values())

        await _set_snapshot(db, reg, read_snapshot({"schema_version": 1, **HEALTHY}) or {}, T0 + timedelta(minutes=5))
        await _pass(db, T0 + timedelta(minutes=5))
        await _set_snapshot(db, reg, read_snapshot({"schema_version": 1, **HEALTHY}) or {}, T0 + timedelta(minutes=10))
        await _pass(db, T0 + timedelta(minutes=10))
        assert await _open(db) == {}
        rows = (await db.execute(select(HealthCheckFinding))).scalars().all()
        assert len([r for r in rows if r.status == STATUS_RESOLVED]) == 4


@pytest.mark.asyncio
async def test_a_missed_heartbeat_is_critical_and_a_stale_snapshot_is_not_read() -> None:
    async with _temp_database("hoa_heartbeat") as sessions, sessions() as db:
        await _register(db, T0 - timedelta(minutes=20), UNHEALTHY)
        await _pass(db, T0)
        found = await _open(db)
        assert set(found) == {("openaev.heartbeat", "")}
        assert found[("openaev.heartbeat", "")].severity == "critical"


@pytest.mark.asyncio
async def test_an_opencti_registration_is_not_read_by_the_openaev_checks() -> None:
    async with _temp_database("hoa_other") as sessions, sessions() as db:
        await _register(db, T0 - timedelta(hours=1), UNHEALTHY, identifier="opencti")
        await _pass(db, T0)
        assert (await db.execute(select(HealthCheckFinding))).scalars().all() == []


@pytest.mark.asyncio
async def test_copilot_mode_records_no_openaev_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "platform_mode", "copilot")
    async with _temp_database("hoa_copilot") as sessions, sessions() as db:
        await _register(db, T0 - timedelta(hours=1), UNHEALTHY)
        await _pass(db, T0)
        assert (await db.execute(select(HealthCheckFinding))).scalars().all() == []


@pytest.mark.asyncio
async def test_c_registered_opencti_and_openaev_show_findings_after_the_first_pass_ai_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance (c): no enrolment, no admin action, AI switched off, one pass."""
    monkeypatch.setattr(settings, "platform_mode", "ai_disabled")
    async with _temp_database("hoa_accept_c") as sessions, sessions() as db:
        await _register(db, T0, UNHEALTHY)
        await _register(
            db,
            T0,
            {"schema_version": 1, "rabbitmq": {"queue_messages": 5, "consumers": 0}},
            identifier="opencti",
        )
        await _pass(db, T0, MODULE_CHECKS)
        modules = {r.module for r in (await _open(db)).values()}
        assert modules == {"openaev", "opencti"}
