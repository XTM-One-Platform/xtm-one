"""Platform Health XTM One platform checks on PostgreSQL (#101 S4a, dev-docs/platform-health.md 4.2):
XTM One modes only (nothing recorded in Copilot mode), AI switched off included,
one platform-wide finding per failing check, resolved once the cause is gone."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.health_check import STATUS_OPEN, STATUS_RESOLVED, HealthCheckFinding
from app.models.xtm_hub_registration import STATUS_LOST_CONNECTIVITY, STATUS_REGISTERED, XtmHubRegistration
from app.services.health import engine, platform_checks
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)
CHECKS = [c for c in engine.registered_checks() if c.module == "xtm_one"]


def _licence(validated: bool, expires: datetime | None = None, reason: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        license_validated=validated,
        license_expiration_date=expires or T0 + timedelta(days=365),
        license_refusal_reason=reason,
    )


@pytest.fixture(autouse=True)
def _world(monkeypatch: pytest.MonkeyPatch) -> None:
    """XTM One mode, a configured token, SMTP set, no licence installed."""
    import app.core.registration_token as registration_token
    import app.services.email as email

    monkeypatch.setattr(settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(settings, "enterprise_license", "")
    monkeypatch.setattr(registration_token, "registration_token_state", lambda: "configured")
    monkeypatch.setattr(email, "is_email_configured", lambda: True)
    engine._LAST_RUN.clear()


async def _open(db: AsyncSession) -> dict[str, HealthCheckFinding]:
    rows = (await db.execute(select(HealthCheckFinding))).scalars().all()
    return {r.check_id: r for r in rows if r.status == STATUS_OPEN}


async def _pass(db: AsyncSession, now: datetime) -> dict[str, int]:
    engine._LAST_RUN.clear()
    return await engine.run_pass(db, now=now, checks=CHECKS)


def test_the_platform_checks_are_registered_on_the_engine() -> None:
    assert {c.check_id for c in CHECKS} == {
        "xtm_one.registration_token",
        "xtm_one.licence",
        "xtm_one.licence_expiry",
        "xtm_one.smtp",
        "xtm_one.hub_registration",
    }
    assert {c.interval for c in CHECKS} == {platform_checks.PLATFORM_CHECK_INTERVAL}


@pytest.mark.asyncio
async def test_a_published_token_no_smtp_and_no_hub_open_their_findings() -> None:
    import app.core.registration_token as registration_token
    import app.services.email as email

    async with _temp_database("hpc_findings") as sessions, sessions() as db:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(registration_token, "registration_token_state", lambda: "published_default")
            mp.setattr(email, "is_email_configured", lambda: False)
            totals = await _pass(db, T0)
        assert totals["checks"] == 5
        found = await _open(db)
        assert set(found) == {"xtm_one.registration_token", "xtm_one.smtp", "xtm_one.hub_registration"}
        assert found["xtm_one.registration_token"].severity == "critical"
        assert found["xtm_one.registration_token"].category == "security"
        assert found["xtm_one.registration_token"].evidence["values"] == {"state": "published_default"}
        assert found["xtm_one.hub_registration"].evidence["values"] == {"status": "unregistered"}
        assert all(r.module == "xtm_one" and r.subject == {} for r in found.values())


@pytest.mark.asyncio
async def test_fixing_the_cause_resolves_the_finding_after_two_passes() -> None:
    import app.core.registration_token as registration_token

    async with _temp_database("hpc_resolve") as sessions, sessions() as db:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(registration_token, "registration_token_state", lambda: "published_default")
            await _pass(db, T0)
        assert "xtm_one.registration_token" in await _open(db)
        db.add(
            XtmHubRegistration(
                hub_url="https://hub.example.com", platform_id=uuid.uuid4(), token_enc="x", status=STATUS_REGISTERED
            )
        )
        await db.flush()
        await _pass(db, T0 + timedelta(minutes=5))
        await _pass(db, T0 + timedelta(minutes=10))
        assert await _open(db) == {}
        rows = (await db.execute(select(HealthCheckFinding))).scalars().all()
        assert {r.check_id for r in rows if r.status == STATUS_RESOLVED} == {
            "xtm_one.registration_token",
            "xtm_one.hub_registration",
        }


@pytest.mark.asyncio
async def test_a_hub_registration_that_lost_connectivity_is_a_finding() -> None:
    async with _temp_database("hpc_hub") as sessions, sessions() as db:
        db.add(
            XtmHubRegistration(
                hub_url="https://hub.example.com",
                platform_id=uuid.uuid4(),
                token_enc="x",
                status=STATUS_LOST_CONNECTIVITY,
            )
        )
        await db.flush()
        await _pass(db, T0)
        found = await _open(db)
        assert found["xtm_one.hub_registration"].evidence["values"] == {"status": STATUS_LOST_CONNECTIVITY}


@pytest.mark.asyncio
async def test_a_refused_licence_is_critical_and_an_expiring_one_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.xtm_license as xtm_license

    async with _temp_database("hpc_licence") as sessions, sessions() as db:
        monkeypatch.setattr(settings, "enterprise_license", "-----BEGIN CERTIFICATE-----")
        monkeypatch.setattr(xtm_license, "resolve_xtm_license", lambda ps: _licence(False, reason="expired"))
        await _pass(db, T0)
        found = await _open(db)
        assert found["xtm_one.licence"].severity == "critical"
        assert found["xtm_one.licence"].evidence["reason"] == "expired"
        assert "xtm_one.licence_expiry" not in found

        monkeypatch.setattr(
            xtm_license, "resolve_xtm_license", lambda ps: _licence(True, expires=T0 + timedelta(days=10, minutes=5))
        )
        await _pass(db, T0 + timedelta(minutes=5))
        found = await _open(db)
        assert found["xtm_one.licence_expiry"].severity == "warning"
        assert found["xtm_one.licence_expiry"].evidence["values"]["days_left"] == 10


@pytest.mark.asyncio
async def test_no_licence_installed_is_community_edition_not_a_finding() -> None:
    async with _temp_database("hpc_ce") as sessions, sessions() as db:
        await _pass(db, T0)
        found = await _open(db)
        assert "xtm_one.licence" not in found
        assert "xtm_one.licence_expiry" not in found


@pytest.mark.asyncio
async def test_copilot_mode_records_no_xtm_one_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Section 1.1 and test (b): no XTM One platform finding outside the XTM One modes."""
    import app.core.registration_token as registration_token

    async with _temp_database("hpc_copilot") as sessions, sessions() as db:
        monkeypatch.setattr(settings, "platform_mode", "copilot")
        monkeypatch.setattr(registration_token, "registration_token_state", lambda: "published_default")
        await _pass(db, T0)
        assert (await db.execute(select(HealthCheckFinding))).scalars().all() == []


@pytest.mark.asyncio
async def test_the_checks_run_with_ai_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ai_disabled`` is an XTM One mode: platform health is deterministic and runs there."""
    import app.core.registration_token as registration_token
    import app.services.ai_state as ai_state

    async def off(db: AsyncSession) -> bool:
        return False

    async with _temp_database("hpc_ai_off") as sessions, sessions() as db:
        monkeypatch.setattr(settings, "platform_mode", "ai_disabled")
        monkeypatch.setattr(ai_state, "is_ai_enabled", off)
        monkeypatch.setattr(registration_token, "registration_token_state", lambda: "published_default")
        await _pass(db, T0)
        assert "xtm_one.registration_token" in await _open(db)
