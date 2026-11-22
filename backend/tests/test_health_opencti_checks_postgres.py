"""Platform Health OpenCTI checks on PostgreSQL (#101 S5, dev-docs/platform-health.md 4.3):
one result per registered OpenCTI (connectors one per connector), read from the heartbeat and
the latest fresh snapshot, XTM One modes only, resolved once the cause is gone."""

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
from app.services.health import engine, opencti_checks, platform_checks
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 20, 1, 0, tzinfo=UTC)
CHECKS = [c for c in engine.registered_checks() if c.module == opencti_checks.MODULE]

UNHEALTHY = {
    "schema_version": 1,
    "connectors": [
        {"id": "c-stale", "name": "Stale", "active": True, "updated_at": (T0 - timedelta(hours=1)).isoformat()},
        {"id": "c-errors", "name": "Errors", "active": True, "updated_at": T0.isoformat(), "works_in_error": 11},
        {"id": "c-off", "name": "Off", "active": False, "updated_at": (T0 - timedelta(days=2)).isoformat()},
    ],
    "rabbitmq": {"queue_messages": 150_000, "consumers": 0},
    "search_engine": {"status": "red", "disk_used_percent": 90},
    "ingestion_lag_seconds": 3600,
}
HEALTHY = {
    "connectors": [
        {"id": "c-stale", "name": "Stale", "active": True, "updated_at": T0.isoformat()},
        {"id": "c-errors", "name": "Errors", "active": True, "updated_at": T0.isoformat(), "works_in_error": 0},
    ],
    "rabbitmq": {"queue_messages": 10, "consumers": 4},
    "search_engine": {"status": "green", "disk_used_percent": 40.0},
    "ingestion_lag_seconds": 5,
}


@pytest.fixture(autouse=True)
def _xtm_one_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "platform_mode", "xtm_one")
    engine._LAST_RUN.clear()


async def _register(db: AsyncSession, heartbeat: datetime, snapshot: dict[str, Any] | None) -> PlatformRegistration:
    from app.services.health.snapshots import read_snapshot

    reg = PlatformRegistration(
        id=uuid.uuid4(),
        platform_identifier="opencti",
        platform_url="https://cti.example.com",
        platform_title="OpenCTI",
        platform_version="6.9.0",
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


async def _open(db: AsyncSession) -> dict[tuple[str, str], HealthCheckFinding]:
    rows = (await db.execute(select(HealthCheckFinding))).scalars().all()
    return {(r.check_id, r.subject.get("connector_id", "")): r for r in rows if r.status == STATUS_OPEN}


async def _pass(db: AsyncSession, now: datetime) -> dict[str, int]:
    engine._LAST_RUN.clear()
    return await engine.run_pass(db, now=now, checks=CHECKS)


def test_the_opencti_checks_are_registered_on_the_engine() -> None:
    assert {c.check_id for c in CHECKS} == {
        "opencti.heartbeat",
        "opencti.connector_stale",
        "opencti.connector_errors",
        "opencti.queue_depth",
        "opencti.queue_consumers",
        "opencti.search_engine",
        "opencti.search_disk",
        "opencti.ingestion_lag",
    }
    assert {c.interval for c in CHECKS} == {platform_checks.PLATFORM_CHECK_INTERVAL}


@pytest.mark.asyncio
async def test_a_restarting_opencti_opens_no_queue_consumers_finding_until_its_grace_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#958: a restart stamped on the snapshot keeps opencti.queue_consumers from opening for the grace."""
    monkeypatch.setattr(engine, "OPEN_AFTER_FAILS", 2)
    async with _temp_database("hoc_restart") as sessions, sessions() as db:
        reg = await _register(db, T0, UNHEALTHY)
        row = (
            await db.execute(select(PlatformHealthSnapshot).where(PlatformHealthSnapshot.registration_id == reg.id))
        ).scalar_one()
        row.restarted_at = T0
        await db.flush()
        for minutes in (0, 5):
            await _set_snapshot(db, reg, UNHEALTHY, T0 + timedelta(minutes=minutes))
            await _pass(db, T0 + timedelta(minutes=minutes))
        assert ("opencti.queue_consumers", "") not in await _open(db)
        # Past the grace, a sustained failure is a real outage: it opens on the second read.
        for minutes in (10, 15):
            await _set_snapshot(db, reg, UNHEALTHY, T0 + timedelta(minutes=minutes))
            await _pass(db, T0 + timedelta(minutes=minutes))
        assert ("opencti.queue_consumers", "") in await _open(db)


@pytest.mark.asyncio
async def test_an_unhealthy_snapshot_opens_one_finding_per_cause_and_resolves_once_healthy() -> None:
    async with _temp_database("hoc_findings") as sessions, sessions() as db:
        reg = await _register(db, T0, UNHEALTHY)
        totals = await _pass(db, T0)
        assert totals["checks"] == 8
        found = await _open(db)
        assert set(found) == {
            ("opencti.connector_stale", "c-stale"),
            ("opencti.connector_errors", "c-errors"),
            ("opencti.queue_depth", ""),
            ("opencti.queue_consumers", ""),
            ("opencti.search_engine", ""),
            ("opencti.search_disk", ""),
            ("opencti.ingestion_lag", ""),
        }
        assert found[("opencti.search_engine", "")].severity == "critical"
        assert found[("opencti.queue_consumers", "")].severity == "critical"
        assert all(r.module == "opencti" and r.subject["registration_id"] == str(reg.id) for r in found.values())

        await _set_snapshot(db, reg, HEALTHY, T0 + timedelta(minutes=5))
        await _pass(db, T0 + timedelta(minutes=5))
        await _set_snapshot(db, reg, HEALTHY, T0 + timedelta(minutes=10))
        await _pass(db, T0 + timedelta(minutes=10))
        assert await _open(db) == {}
        rows = (await db.execute(select(HealthCheckFinding))).scalars().all()
        assert len([r for r in rows if r.status == STATUS_RESOLVED]) == 7


@pytest.mark.asyncio
async def test_a_yellow_search_engine_is_a_warning() -> None:
    async with _temp_database("hoc_yellow") as sessions, sessions() as db:
        await _register(db, T0, {"schema_version": 1, "search_engine": {"status": "yellow"}})
        await _pass(db, T0)
        found = await _open(db)
        assert set(found) == {("opencti.search_engine", "")}
        assert found[("opencti.search_engine", "")].severity == "warning"


@pytest.mark.asyncio
async def test_a_missed_heartbeat_is_critical_and_a_stale_snapshot_is_not_read() -> None:
    async with _temp_database("hoc_heartbeat") as sessions, sessions() as db:
        await _register(db, T0 - timedelta(minutes=20), UNHEALTHY)
        await _pass(db, T0)
        found = await _open(db)
        assert set(found) == {("opencti.heartbeat", "")}
        assert found[("opencti.heartbeat", "")].severity == "critical"


@pytest.mark.asyncio
async def test_a_registration_without_a_snapshot_has_only_the_heartbeat_check() -> None:
    async with _temp_database("hoc_nosnap") as sessions, sessions() as db:
        await _register(db, T0, None)
        totals = await _pass(db, T0)
        assert totals["checks"] == 8
        assert await _open(db) == {}
        rows = (await db.execute(select(HealthCheckFinding))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_copilot_mode_records_no_opencti_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "platform_mode", "copilot")
    async with _temp_database("hoc_copilot") as sessions, sessions() as db:
        await _register(db, T0 - timedelta(hours=1), UNHEALTHY)
        await _pass(db, T0)
        assert (await db.execute(select(HealthCheckFinding))).scalars().all() == []
