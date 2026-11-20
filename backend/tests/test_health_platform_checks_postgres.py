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
_real_probe = platform_checks._probe
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
    monkeypatch.setattr(platform_checks, "_probe", _probe_ok)
    monkeypatch.setattr(platform_checks, "_trace_backlog_length", _no_backlog)
    monkeypatch.setattr(platform_checks, "_running_workers", _one_worker)
    monkeypatch.setattr(platform_checks, "_released_versions", _no_release)
    monkeypatch.setattr(settings, "frontend_url", "http://localhost:3000")
    engine._LAST_RUN.clear()


async def _probe_ok(name: str) -> str:
    return "ok"


async def _no_backlog() -> int:
    return 0


async def _one_worker() -> int:
    return 1


async def _no_release() -> list[str]:
    return []


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
        "xtm_one.dependency_unreachable",
        "xtm_one.trace_backlog",
        "xtm_one.worker_absent",
        "xtm_one.provider_error_rate",
        "xtm_one.maintenance_stalled",
        "xtm_one.version_behind",
        "xtm_one.certificate_expiry",
    }
    daily = {"xtm_one.version_behind", "xtm_one.certificate_expiry"}
    assert {c.interval for c in CHECKS if c.check_id in daily} == {platform_checks.DAILY_CHECK_INTERVAL}
    assert {c.interval for c in CHECKS if c.check_id not in daily} == {platform_checks.PLATFORM_CHECK_INTERVAL}


@pytest.mark.asyncio
async def test_a_published_token_no_smtp_and_no_hub_open_their_findings() -> None:
    import app.core.registration_token as registration_token
    import app.services.email as email

    async with _temp_database("hpc_findings") as sessions, sessions() as db:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(registration_token, "registration_token_state", lambda: "published_default")
            mp.setattr(email, "is_email_configured", lambda: False)
            totals = await _pass(db, T0)
        assert totals["checks"] == 19
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


@pytest.mark.asyncio
async def test_an_unreachable_dependency_is_critical_per_dependency_and_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S4d: a probe that fails opens one critical finding for that dependency, resolved once it answers."""
    down = {"object_storage"}

    async def probe(name: str) -> str:
        return "timed out after 4 s" if name in down else "ok"

    async with _temp_database("hpc_probes") as sessions, sessions() as db:
        monkeypatch.setattr(platform_checks, "_probe", probe)
        await _pass(db, T0)
        rows = [r for r in (await db.execute(select(HealthCheckFinding))).scalars().all() if r.status == STATUS_OPEN]
        probe_rows = [r for r in rows if r.check_id == "xtm_one.dependency_unreachable"]
        assert [r.subject for r in probe_rows] == [{"dependency": "object_storage"}]
        assert probe_rows[0].severity == "critical"
        assert probe_rows[0].evidence["values"] == {"probe": "timed out after 4 s"}

        down = set()
        await _pass(db, T0 + timedelta(minutes=5))
        await _pass(db, T0 + timedelta(minutes=10))
        assert "xtm_one.dependency_unreachable" not in await _open(db)


@pytest.mark.asyncio
async def test_the_probe_reports_a_timeout_and_an_error() -> None:
    """S4d: the probe is bounded and turns any failure into its text, never raises."""
    import asyncio

    async def hang() -> None:
        await asyncio.sleep(60)

    async def boom() -> None:
        raise ConnectionError("refused")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(platform_checks, "PROBE_TIMEOUT_SECONDS", 0.01)
        mp.setattr(platform_checks, "_PROBES", {"redis": hang, "object_storage": boom})
        assert (await _real_probe("redis")).startswith("timed out")
        assert await _real_probe("object_storage") == "ConnectionError: refused"


@pytest.mark.asyncio
async def test_a_trace_backlog_over_1000_is_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """S4d: over 1,000 traces in the overflow list opens a capacity warning; an unreadable Redis records nothing."""

    async def backlog() -> int:
        return 1500

    async def unreadable() -> int:
        raise ConnectionError("down")

    async with _temp_database("hpc_traces") as sessions, sessions() as db:
        monkeypatch.setattr(platform_checks, "_trace_backlog_length", backlog)
        await _pass(db, T0)
        found = await _open(db)
        assert found["xtm_one.trace_backlog"].category == "capacity"
        assert found["xtm_one.trace_backlog"].evidence["values"] == {"waiting": 1500}

    monkeypatch.setattr(platform_checks, "_trace_backlog_length", unreadable)
    assert await platform_checks.trace_backlog(None, T0) == []  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_no_running_worker_is_critical_and_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """S4e: no worker presence opens a critical finding, closed once a worker runs; unreadable Redis records nothing."""
    running: int | None = 0

    async def workers() -> int | None:
        return running

    async with _temp_database("hpc_workers") as sessions, sessions() as db:
        monkeypatch.setattr(platform_checks, "_running_workers", workers)
        await _pass(db, T0)
        found = await _open(db)
        assert found["xtm_one.worker_absent"].severity == "critical"
        assert found["xtm_one.worker_absent"].evidence["values"] == {"running": 0}

        running = 2
        await _pass(db, T0 + timedelta(minutes=5))
        await _pass(db, T0 + timedelta(minutes=10))
        assert "xtm_one.worker_absent" not in await _open(db)

    running = None
    assert await platform_checks.worker_absent(None, T0) == []  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_failing_model_is_a_warning_per_model_and_resolves() -> None:
    """S4e: over 5 % failed traces of a model in the last hour warns for that model; too few traces are not judged."""
    from app.models.llm_trace import LLMTrace

    def traces(model: str, ok: int, failed: int, at: datetime) -> list[LLMTrace]:
        return [
            LLMTrace(id=uuid.uuid4(), source="agent-chat", name="chat", model=model, started_at=at, status=status)
            for status, n in (("ok", ok), ("error", failed))
            for _ in range(n)
        ]

    recent = T0 - timedelta(minutes=10)
    async with _temp_database("hpc_providers") as sessions, sessions() as db:
        db.add_all(
            traces("model-bad", 18, 2, recent)  # 10 % of 20: a finding
            + traces("model-good", 99, 1, recent)  # 1 %: none
            + traces("model-rare", 1, 4, recent)  # 5 traces: not judged
            + traces("model-old", 0, 30, T0 - timedelta(hours=3))  # outside the window
        )
        await db.commit()
        await _pass(db, T0)
        rows = [
            r
            for r in (await db.execute(select(HealthCheckFinding))).scalars().all()
            if r.status == STATUS_OPEN and r.check_id == "xtm_one.provider_error_rate"
        ]
        assert [r.subject for r in rows] == [{"model": "model-bad"}]
        assert rows[0].evidence["values"] == {"traces": 20, "failed": 2, "rate": 0.1}

        # An hour later the failures left the window: resolved after two passes.
        await _pass(db, T0 + timedelta(hours=1))
        await _pass(db, T0 + timedelta(hours=1, minutes=5))
        assert "xtm_one.provider_error_rate" not in await _open(db)


@pytest.mark.asyncio
async def test_newer_releases_are_info_then_warning_and_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    """S4f: one newer release is info, two a warning; drafts, pre-releases and older tags do not count."""
    import app.services.xtm_hub_registration as hub

    released: list[str] | None = ["1.261119.1", "1.261117.3"]

    async def releases() -> list[str] | None:
        return released

    monkeypatch.setattr(hub, "registration_platform_version", lambda: "1.261119.0")
    async with _temp_database("hpc_version") as sessions, sessions() as db:
        monkeypatch.setattr(platform_checks, "_released_versions", releases)
        await _pass(db, T0)
        found = (await _open(db))["xtm_one.version_behind"]
        assert (found.severity, found.category) == ("info", "configuration")
        assert found.evidence["values"] == {"running": "1.261119.0", "newer": ["1.261119.1"], "behind": 1}

        released = ["1.261120.0", "1.261119.1"]
        await _pass(db, T0 + timedelta(days=1))
        assert (await _open(db))["xtm_one.version_behind"].severity == "warning"

        monkeypatch.setattr(hub, "registration_platform_version", lambda: "1.261120.0")
        await _pass(db, T0 + timedelta(days=2))
        await _pass(db, T0 + timedelta(days=3))
        assert "xtm_one.version_behind" not in await _open(db)

    released = None  # the release list cannot be read: nothing judged
    assert await platform_checks.version_behind(None, T0) == []  # type: ignore[arg-type]
    monkeypatch.setattr(hub, "registration_platform_version", lambda: "0.0.0")
    released = ["9.9.9"]  # a local build has no release to compare
    assert await platform_checks.version_behind(None, T0) == []  # type: ignore[arg-type]


def test_a_release_tag_core_is_read_whatever_its_prefix_or_suffix() -> None:
    assert platform_checks._version_core("v1.2.3") == (1, 2, 3)
    assert platform_checks._version_core("1.261105.0-dev.2ffcbd3") == (1, 261105, 0)
    assert platform_checks._version_core("latest") is None


@pytest.mark.asyncio
async def test_a_certificate_expiring_soon_or_refused_is_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """S4f: a certificate of the public URL expiring within 21 days or refused warns; unreachable or http records nothing."""
    import ssl

    expires = T0 + timedelta(days=10)
    calls: list[tuple[str, int]] = []

    async def handshake(host: str, port: int) -> datetime:
        calls.append((host, port))
        return expires

    monkeypatch.setattr(settings, "frontend_url", "https://one.example.com:8443/app")
    monkeypatch.setattr(platform_checks, "_certificate_expiry", handshake)
    async with _temp_database("hpc_certificate") as sessions, sessions() as db:
        await _pass(db, T0)
        found = (await _open(db))["xtm_one.certificate_expiry"]
        assert (found.severity, found.category, found.subject) == ("warning", "security", {"host": "one.example.com"})
        assert found.evidence["values"]["days_left"] == 10
        assert calls[-1] == ("one.example.com", 8443)

        expires = T0 + timedelta(days=90)
        await _pass(db, T0 + timedelta(days=1))
        await _pass(db, T0 + timedelta(days=2))
        assert "xtm_one.certificate_expiry" not in await _open(db)

    async def refused(host: str, port: int) -> datetime:
        raise ssl.SSLCertVerificationError("certificate has expired")

    async def unreachable(host: str, port: int) -> datetime:
        raise OSError("no route to host")

    async with _temp_database("hpc_certificate_read") as sessions, sessions() as db:
        monkeypatch.setattr(platform_checks, "_certificate_expiry", refused)
        [result] = await platform_checks.certificate_expiry(db, T0)
        assert result.failing and "expired" in result.evidence["values"]["error"]

        monkeypatch.setattr(platform_checks, "_certificate_expiry", unreachable)
        assert await platform_checks.certificate_expiry(db, T0) == []
        monkeypatch.setattr(settings, "frontend_url", "http://one.example.com")
        assert await platform_checks.certificate_expiry(db, T0) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("moved_to", ["https://two.example.com", "http://one.example.com", "unreachable"])
async def test_a_certificate_finding_of_a_former_host_resolves(monkeypatch: pytest.MonkeyPatch, moved_to: str) -> None:
    """S4f review: FRONTEND_URL moved to another host, to http or to a host unreachable from here closes the old host out."""

    async def expiring(host: str, port: int) -> datetime:
        return T0 + timedelta(days=5)

    async def unreachable(host: str, port: int) -> datetime:
        raise OSError("no route to host")

    monkeypatch.setattr(settings, "frontend_url", "https://one.example.com")
    monkeypatch.setattr(platform_checks, "_certificate_expiry", expiring)
    async with _temp_database("hpc_certificate_moved") as sessions, sessions() as db:
        await _pass(db, T0)
        assert (await _open(db))["xtm_one.certificate_expiry"].subject == {"host": "one.example.com"}

        if moved_to == "unreachable":
            monkeypatch.setattr(settings, "frontend_url", "https://two.example.com")
            monkeypatch.setattr(platform_checks, "_certificate_expiry", unreachable)
        else:
            monkeypatch.setattr(settings, "frontend_url", moved_to)
        await _pass(db, T0 + timedelta(days=1))
        await _pass(db, T0 + timedelta(days=2))
        still_open = {
            r.subject.get("host")
            for r in (await db.execute(select(HealthCheckFinding))).scalars()
            if r.check_id == "xtm_one.certificate_expiry" and r.status == STATUS_OPEN
        }
        assert "one.example.com" not in still_open


@pytest.mark.asyncio
async def test_an_unreachable_host_keeps_its_own_certificate_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    """S4f review: a host that stops answering keeps its open finding: nothing was read to resolve it."""

    async def expiring(host: str, port: int) -> datetime:
        return T0 + timedelta(days=5)

    async def unreachable(host: str, port: int) -> datetime:
        raise OSError("no route to host")

    monkeypatch.setattr(settings, "frontend_url", "https://one.example.com")
    monkeypatch.setattr(platform_checks, "_certificate_expiry", expiring)
    async with _temp_database("hpc_certificate_unreachable") as sessions, sessions() as db:
        await _pass(db, T0)
        monkeypatch.setattr(platform_checks, "_certificate_expiry", unreachable)
        await _pass(db, T0 + timedelta(days=1))
        await _pass(db, T0 + timedelta(days=2))
        assert (await _open(db))["xtm_one.certificate_expiry"].subject == {"host": "one.example.com"}
