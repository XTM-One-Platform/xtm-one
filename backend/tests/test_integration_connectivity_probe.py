"""Provider connectivity probes — the generic seam and Nova's implementation.

Two layers are covered:

* ``app.integrations.connectivity`` — the registry/dispatch contract every
  probe-backed provider relies on (verdict mapping, never-raises, timeout).
* Nova's probe — the first provider whose health is internal state rather than
  an authenticated HTTP call, exercised against a real Postgres schema.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

# Imported as a module: a bare ``test_provider_connectivity`` name would be
# collected by pytest as a test case.
from app.api.v1 import integration_testing
from app.integrations import connectivity
from app.integrations.connectivity import (
    ProbeContext,
    ProbeResult,
    dispatch_connectivity,
    has_connectivity_probe,
    register_connectivity,
)
from app.integrations.providers import nova as nova_provider
from app.models.integration import Integration
from app.models.nova_rule import NovaRule
from app.models.nova_rule_source import NovaRuleSource
from app.models.relay import RelayGroup
from app.models.user import User
from tests.scratch_database import scratch_database

pytestmark = pytest.mark.integration

_TABLES = [
    User.__table__,
    RelayGroup.__table__,
    Integration.__table__,
    NovaRule.__table__,
    NovaRuleSource.__table__,
]


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Keep probe registration global-state leaks out of other tests."""
    snapshot = dict(connectivity._PROBES)
    yield
    connectivity._PROBES.clear()
    connectivity._PROBES.update(snapshot)


def _ctx(provider: str = "acme") -> ProbeContext:
    return ProbeContext(provider=provider, name="Acme", token=None, extra={})


# ── Registry / dispatch contract ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unregistered_provider_is_not_our_business():
    # ``None`` (not an error verdict) is what sends the caller back to the
    # standard HTTP checks.
    assert await dispatch_connectivity(_ctx()) is None
    assert not has_connectivity_probe("acme")


@pytest.mark.parametrize(
    ("factory", "wire"),
    [
        (ProbeResult.ok, "ok"),
        (ProbeResult.degraded, "info"),
        (ProbeResult.failed, "error"),
    ],
)
def test_verdict_maps_to_the_wire_status(factory, wire):
    # "degraded" is deliberately not a wire value: the frontend already renders
    # "info" as the blue works-with-caveats banner.
    assert factory("msg").as_response() == {"status": wire, "message": "msg"}


def test_degraded_still_counts_as_healthy():
    assert ProbeResult.degraded("partial").is_healthy
    assert ProbeResult.ok("fine").is_healthy
    assert not ProbeResult.failed("broken").is_healthy


@pytest.mark.asyncio
async def test_raising_probe_becomes_an_error_verdict():
    async def boom(_ctx):
        raise RuntimeError("kaboom")

    register_connectivity("acme", boom)
    result = await dispatch_connectivity(_ctx())
    assert result.status == "error"
    # The raw exception text never reaches the user — it could carry anything.
    assert "kaboom" not in result.message


@pytest.mark.asyncio
async def test_hanging_probe_times_out_instead_of_blocking_the_request(monkeypatch):
    monkeypatch.setattr(connectivity, "PROBE_TIMEOUT", 0.05)

    async def sleeper(_ctx):
        await asyncio.sleep(5)
        return ProbeResult.ok("never")

    register_connectivity("acme", sleeper)
    result = await dispatch_connectivity(_ctx())
    assert result.status == "error"
    assert "timed out" in result.message


@pytest.mark.asyncio
async def test_probe_returning_the_wrong_type_is_rejected():
    async def sloppy(_ctx):
        return {"status": "ok"}

    register_connectivity("acme", sloppy)
    result = await dispatch_connectivity(_ctx())
    assert result.status == "error"


@pytest.mark.asyncio
async def test_probe_short_circuits_the_http_checks_table():
    """A registered probe owns the verdict even for a provider with an HTTP check."""

    async def probe(_ctx):
        return ProbeResult.degraded("local state says so")

    register_connectivity("github", probe)
    # No network: the probe answers before the checks table is consulted.
    result = await integration_testing.test_provider_connectivity(
        "github", "tok", {}, "GitHub"
    )
    assert result == {"status": "info", "message": "local state says so"}


@pytest.mark.asyncio
async def test_unprobed_provider_still_falls_back_to_appears_configured():
    result = await integration_testing.test_provider_connectivity(
        "acme", "tok", {}, "Acme"
    )
    assert result == {"status": "ok", "message": "Acme appears configured"}


# ── Nova probe (DB-backed) ───────────────────────────────────────────────────


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("novaprobe", tables=_TABLES) as db:
        yield db.sessions


def _integration(*, company: bool = True) -> Integration:
    return Integration(
        id=uuid.uuid4(),
        provider="nova",
        auth_type="none",
        name="MyNova",
        status="connected",
        is_company_managed=company,
    )


def _rule(**kw) -> NovaRule:
    return NovaRule(
        id=uuid.uuid4(),
        name=kw.pop("name", "Jailbreak"),
        action="block",
        rule_text="rule X { }",
        enabled=kw.pop("enabled", True),
        is_company_managed=kw.pop("company", True),
        **kw,
    )


async def _probe(session, integ, *, can_manage: bool = True) -> ProbeResult:
    # The admins who manage the company-managed connection, unless a test says
    # otherwise.
    return await nova_provider.check_connectivity(
        ProbeContext(
            provider="nova",
            name=integ.name,
            token=None,
            extra={},
            db=session,
            integration=integ,
            caller_can_manage=can_manage,
        )
    )


@pytest.mark.asyncio
async def test_nova_is_registered_as_probe_backed():
    # The endpoint keys its "skip credential resolution" branch off this.
    assert has_connectivity_probe("nova")


@pytest.mark.asyncio
async def test_nova_without_the_integration_record_says_so():
    result = await nova_provider.check_connectivity(
        ProbeContext(provider="nova", name="MyNova", token=None, extra={})
    )
    assert result.status == "error"


@pytest.mark.asyncio
async def test_nova_personal_connection_is_degraded_not_ok():
    async with _temp_database() as sessions, sessions() as session:
        integ = _integration(company=False)
        session.add(integ)
        await session.commit()
        result = await _probe(session, integ)
    assert result.status == "degraded"
    assert "platform-wide" in result.message


@pytest.mark.asyncio
async def test_nova_without_a_rule_source_fails():
    async with _temp_database() as sessions, sessions() as session:
        integ = _integration()
        session.add(integ)
        await session.commit()
        result = await _probe(session, integ)
    assert result.status == "error"
    assert "No rule source" in result.message


@pytest.mark.asyncio
async def test_nova_reports_a_failed_sync():
    async with _temp_database() as sessions, sessions() as session:
        integ = _integration()
        session.add(integ)
        session.add(
            NovaRuleSource(
                id=uuid.uuid4(),
                integration_id=integ.id,
                repo="acme/rules",
                branch="main",
                sync_interval="daily",
                last_sync_status="error",
                last_sync_error="404 Not Found",
                sync_failure_count=3,
            )
        )
        await session.commit()
        result = await _probe(session, integ)
        hidden = await _probe(session, integ, can_manage=False)
    assert result.status == "error"
    assert "acme/rules" in result.message
    assert "404 Not Found" in result.message
    # Whoever cannot manage the connection gets the verdict, not the error.
    assert hidden.status == "error"
    assert "acme/rules" in hidden.message
    assert "404 Not Found" not in hidden.message
    assert "manage this integration" in hidden.message


@pytest.mark.asyncio
async def test_nova_never_synced_is_degraded():
    async with _temp_database() as sessions, sessions() as session:
        integ = _integration()
        session.add(integ)
        session.add(
            NovaRuleSource(
                id=uuid.uuid4(),
                integration_id=integ.id,
                repo="acme/rules",
                branch="main",
                sync_interval="daily",
            )
        )
        await session.commit()
        result = await _probe(session, integ)
    assert result.status == "degraded"
    assert "never synced" in result.message


@pytest.mark.asyncio
async def test_nova_with_zero_active_rules_fails():
    """A guard with no rule allows everything — a green badge would lie."""
    from datetime import UTC, datetime

    async with _temp_database() as sessions, sessions() as session:
        integ = _integration()
        session.add(integ)
        session.add(
            NovaRuleSource(
                id=uuid.uuid4(),
                integration_id=integ.id,
                repo="acme/rules",
                branch="main",
                sync_interval="daily",
                last_synced_at=datetime.now(UTC),
                last_sync_status="ok",
                last_sync_rules=0,
            )
        )
        session.add(_rule(enabled=False))  # disabled rules do not count
        await session.commit()
        result = await _probe(session, integ)
    assert result.status == "error"
    assert "allows every prompt" in result.message


@pytest.mark.asyncio
async def test_nova_without_the_engine_is_degraded_with_the_rule_count(monkeypatch):
    from datetime import UTC, datetime

    from app.services import nova_engine

    monkeypatch.setattr(nova_engine, "NOVA_HUNTING_AVAILABLE", False)
    async with _temp_database() as sessions, sessions() as session:
        integ = _integration()
        session.add(integ)
        session.add(
            NovaRuleSource(
                id=uuid.uuid4(),
                integration_id=integ.id,
                repo="acme/rules",
                branch="main",
                sync_interval="daily",
                last_synced_at=datetime.now(UTC),
                last_sync_status="ok",
                last_sync_rules=2,
            )
        )
        session.add(_rule(name="A"))
        session.add(_rule(name="B"))
        await session.commit()
        result = await _probe(session, integ)
    assert result.status == "degraded"
    assert "2 active rules" in result.message
    assert "nova-hunting" in result.message


@pytest.mark.asyncio
async def test_nova_fully_operational_is_ok(monkeypatch):
    from datetime import UTC, datetime

    from app.services import nova_engine

    monkeypatch.setattr(nova_engine, "NOVA_HUNTING_AVAILABLE", True)
    async with _temp_database() as sessions, sessions() as session:
        integ = _integration()
        session.add(integ)
        session.add(
            NovaRuleSource(
                id=uuid.uuid4(),
                integration_id=integ.id,
                repo="acme/rules",
                branch="main",
                sync_interval="daily",
                last_synced_at=datetime.now(UTC),
                last_sync_status="ok",
                last_sync_rules=1,
            )
        )
        session.add(_rule())
        await session.commit()
        result = await _probe(session, integ)
    assert result.status == "ok"
    assert "1 active rule," in result.message
