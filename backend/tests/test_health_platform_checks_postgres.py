"""Platform Health XTM One platform checks on PostgreSQL (#101 S4a, dev-docs/platform-health.md 4.2):
XTM One modes only (nothing recorded in Copilot mode), AI switched off included,
one platform-wide finding per failing check, resolved once the cause is gone."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.health_check import STATUS_OPEN, STATUS_RESOLVED, HealthCheckFinding
from app.models.integration import Integration
from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
from app.models.mcp_server import MCPServer
from app.models.platform_settings import PlatformSettings
from app.models.xtm_hub_registration import STATUS_LOST_CONNECTIVITY, STATUS_REGISTERED, XtmHubRegistration
from app.services.health import engine, platform_checks
from app.services.quota import platform_agentic_usage as _real_platform_agentic_usage
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
    """XTM One mode, a configured token, SMTP set, no licence installed, models set, no ceiling."""
    import app.core.registration_token as registration_token
    import app.llm.router as router
    import app.services.email as email
    import app.services.quota as quota

    async def default_model(db: AsyncSession) -> str:
        return "model-a"

    async def settings_row(db: AsyncSession) -> SimpleNamespace:
        return SimpleNamespace(embedding_model="embed-a")

    async def no_ceiling(db: AsyncSession, now: datetime | None = None) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(settings, "enterprise_license", "")
    monkeypatch.setattr(registration_token, "registration_token_state", lambda: "configured")
    monkeypatch.setattr(email, "is_email_configured", lambda: True)
    monkeypatch.setattr(router, "get_platform_default_model", default_model)
    monkeypatch.setattr(platform_checks, "_platform_settings", settings_row)
    monkeypatch.setattr(quota, "platform_agentic_usage", no_ceiling)
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
        "xtm_one.integration_broken",
        "xtm_one.mcp_server_broken",
        "xtm_one.kb_indexing_stalled",
        "xtm_one.kb_stale_embeddings",
        "xtm_one.default_model",
        "xtm_one.embedding_model",
        "xtm_one.quota_near_ceiling",
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
        assert totals["checks"] == 12
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


@pytest.mark.asyncio
async def test_a_broken_company_integration_or_mcp_server_is_a_finding_and_resolves_once_fixed() -> None:
    """S4b: one finding per broken company-managed integration or MCP server; a personal one is not read."""
    async with _temp_database("hpc_integrations") as sessions, sessions() as db:
        broken = Integration(id=uuid.uuid4(), provider="github", name="org", is_company_managed=True, status="error")
        tested = Integration(
            id=uuid.uuid4(),
            provider="jira",
            name="org",
            is_company_managed=True,
            status="connected",
            connection_test_status="error",
        )
        healthy = Integration(
            id=uuid.uuid4(), provider="notion", name="org", is_company_managed=True, status="connected"
        )
        personal = Integration(id=uuid.uuid4(), provider="slack", name="mine", is_company_managed=False, status="error")
        server = MCPServer(id=uuid.uuid4(), name="tools", is_company_managed=True, enabled=True, oauth_status="error")
        db.add_all([broken, tested, healthy, personal, server])
        await db.flush()
        await _pass(db, T0)
        rows = (await db.execute(select(HealthCheckFinding))).scalars().all()
        open_rows = [r for r in rows if r.status == STATUS_OPEN]
        integration_subjects = {
            r.subject["integration_id"] for r in open_rows if r.check_id == "xtm_one.integration_broken"
        }
        assert integration_subjects == {str(broken.id), str(tested.id)}
        assert [r.subject for r in open_rows if r.check_id == "xtm_one.mcp_server_broken"] == [
            {"mcp_server_id": str(server.id)}
        ]

        broken.status = "connected"
        await db.delete(server)
        await db.flush()
        await _pass(db, T0 + timedelta(minutes=5))
        await _pass(db, T0 + timedelta(minutes=10))
        rows = (await db.execute(select(HealthCheckFinding))).scalars().all()
        still_open = {
            (r.check_id, next(iter(r.subject.values()))) for r in rows if r.status == STATUS_OPEN and r.subject
        }
        assert still_open == {("xtm_one.integration_broken", str(tested.id))}


@pytest.mark.asyncio
async def test_a_stalled_indexing_and_stale_embeddings_are_findings_per_knowledge_base() -> None:
    async with _temp_database("hpc_kb") as sessions, sessions() as db:
        ps = (await db.execute(select(PlatformSettings).limit(1))).scalar_one_or_none()
        if ps is None:
            ps = PlatformSettings()
            db.add(ps)
        ps.embedding_model = "embed-new"
        current = KnowledgeBase(id=uuid.uuid4(), name="current", embedding_model="embed-new")
        stale = KnowledgeBase(id=uuid.uuid4(), name="stale", embedding_model="embed-old")
        db.add_all([current, stale])
        await db.flush()
        db.add_all(
            [
                KnowledgeDocument(
                    knowledge_base_id=current.id,
                    filename="a.md",
                    source_type="text",
                    status="processing",
                    updated_at=T0 - timedelta(hours=2),
                ),
                KnowledgeDocument(
                    knowledge_base_id=stale.id,
                    filename="b.md",
                    source_type="text",
                    status="processing",
                    updated_at=T0 - timedelta(minutes=10),
                ),
            ]
        )
        await db.flush()
        await _pass(db, T0)
        rows = [r for r in (await db.execute(select(HealthCheckFinding))).scalars().all() if r.status == STATUS_OPEN]
        stalled = [r for r in rows if r.check_id == "xtm_one.kb_indexing_stalled"]
        assert [r.subject for r in stalled] == [{"knowledge_base_id": str(current.id)}]
        assert stalled[0].evidence["values"] == {"documents": 1}
        embeddings = [r for r in rows if r.check_id == "xtm_one.kb_stale_embeddings"]
        assert [r.subject for r in embeddings] == [{"knowledge_base_id": str(stale.id)}]
        assert embeddings[0].evidence["values"] == {"model": "embed-old", "platform_model": "embed-new"}


@pytest.mark.asyncio
async def test_an_unset_default_or_embedding_model_is_critical(monkeypatch: pytest.MonkeyPatch) -> None:
    """S4c: no default model or no embedding model opens a critical configuration finding."""
    import app.llm.router as router

    async def unset(db: AsyncSession) -> None:
        return None

    async def no_embedding(db: AsyncSession) -> SimpleNamespace:
        return SimpleNamespace(embedding_model=None)

    async with _temp_database("hpc_models") as sessions, sessions() as db:
        monkeypatch.setattr(router, "get_platform_default_model", unset)
        monkeypatch.setattr(platform_checks, "_platform_settings", no_embedding)
        await _pass(db, T0)
        found = await _open(db)
        assert found["xtm_one.default_model"].severity == "critical"
        assert found["xtm_one.embedding_model"].evidence["values"] == {"model": "unset"}


@pytest.mark.asyncio
async def test_a_ceiling_over_90_percent_is_a_warning_per_period_and_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """S4c: one finding per platform-wide ceiling over 90 %, closed out once the period is under it."""
    import app.services.quota as quota

    usage = [{"period": "daily", "used": 95, "limit": 100}, {"period": "monthly", "used": 500, "limit": 3000}]

    async def read(db: AsyncSession, now: datetime | None = None) -> list[dict[str, Any]]:
        return usage

    async with _temp_database("hpc_quota") as sessions, sessions() as db:
        monkeypatch.setattr(quota, "platform_agentic_usage", read)
        await _pass(db, T0)
        rows = [r for r in (await db.execute(select(HealthCheckFinding))).scalars().all() if r.status == STATUS_OPEN]
        quota_rows = [r for r in rows if r.check_id == "xtm_one.quota_near_ceiling"]
        assert [r.subject for r in quota_rows] == [{"period": "daily"}]
        assert quota_rows[0].category == "capacity"
        assert quota_rows[0].evidence["values"] == {"period": "daily", "used": 95, "limit": 100}

        usage = [{"period": "daily", "used": 3, "limit": 100}]
        await _pass(db, T0 + timedelta(minutes=5))
        await _pass(db, T0 + timedelta(minutes=10))
        assert "xtm_one.quota_near_ceiling" not in await _open(db)


@pytest.mark.asyncio
async def test_the_platform_usage_read_moves_no_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance 3: reading the ceilings for health changes no agentic counter."""
    import app.services.quota as quota

    counts = {"agentic": 40, "sandbox": 7}

    async def ceilings(db: AsyncSession) -> dict[str, int]:
        return {"daily": 50}

    async def global_count(redis: object, usage_type: str, period_key: str) -> int:
        return counts[usage_type]

    async def client() -> object:
        return object()

    monkeypatch.setattr(quota, "get_xtm_agentic_ceilings", ceilings)
    monkeypatch.setattr(quota, "_get_global_count", global_count)
    monkeypatch.setattr(quota, "redis_client", client)
    rows = await _real_platform_agentic_usage(None, T0)  # type: ignore[arg-type]
    assert rows == [{"period": "daily", "used": 47, "limit": 50}]
    assert counts == {"agentic": 40, "sandbox": 7}
