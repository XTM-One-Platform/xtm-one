"""Platform Health Module Analyst on the Health page API, S14c-1 (#101, dev-docs/platform-health.md 12.7).

Pinned here, on PostgreSQL:

* a module finding carries the Module Analyst's newest recommendation (text,
  confidence, evidence, cycle) and an agent finding carries none;
* a finding whose check id reads ``<module>.agentic.<slug>`` is tagged agentic and
  the ``agentic`` filter keeps or drops it;
* an administrator's OpenCTI card carries the analyst's last cycle on its registrations;
* "Investigate now" is an administrator's, answers 404 for a module the analyst does
  not read, and refuses when the Module Analyst is off or nothing is registered.

``integration``-marked; skips when PostgreSQL is unreachable.
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
from app.api.v1 import platform_health_cycles, platform_health_findings
from app.core.config import settings
from app.core.database import get_db
from app.models.health import HealthCycle
from app.models.health_module_analyst import HealthFindingRecommendation
from app.models.platform_registration import PlatformRegistration
from app.services import agentic_gate, ai_state
from app.services.health.engine import CheckResult, finding_key, record_results
from tests.health_support import configure, health_database, make_user

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)


@pytest.fixture
def xtm_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "platform_mode", "xtm_one")

    async def _ai(_db: Any) -> bool:
        return True

    async def _verdict(_db: Any) -> agentic_gate.AgenticVerdict:
        return agentic_gate.AgenticVerdict(refusal=None)

    monkeypatch.setattr(ai_state, "is_ai_enabled", _ai)
    monkeypatch.setattr(agentic_gate, "agentic_verdict", _verdict)


@asynccontextmanager
async def _client(sessions: async_sessionmaker[AsyncSession], user: Any) -> AsyncIterator[httpx.AsyncClient]:
    api = FastAPI()
    for router in (platform_health_findings.router, platform_health_cycles.router):
        api.include_router(router, prefix="/api/v1/health")

    async def _get_db() -> AsyncIterator[AsyncSession]:
        async with sessions() as db:
            yield db
            await db.commit()

    api.dependency_overrides[deps.get_current_user] = lambda: user
    api.dependency_overrides[get_db] = _get_db
    # The AI and licence gate is pinned by test_ai_disabled_read_surface; here the route's own rules.
    api.dependency_overrides[deps.require_agentic_ai] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        yield client


def _failing(check_id: str, module: str, subject: dict[str, str]) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        module=module,
        category="availability",
        severity="warning",
        failing=True,
        subject=subject,
        evidence={"values": {"x": 1}},
        recommendation="Do it.",
    )


async def _world(sessions: async_sessionmaker[AsyncSession]) -> tuple[Any, Any, uuid.UUID, str]:
    """An admin, a plain user, an OpenCTI registration with a check finding, an agentic one, two
    recommendations on the first (the newer must win) and a completed analyst cycle."""
    admin, plain = make_user("admin", admin=True), make_user("plain")
    registration = uuid.uuid4()
    async with sessions() as db:
        db.add_all([admin, plain])
        db.add(
            PlatformRegistration(
                id=registration,
                platform_identifier="opencti",
                platform_url="https://cti.example.com",
                platform_title="OpenCTI",
                platform_version="6.9.0",
            )
        )
        await db.flush()
        subject = {"registration_id": str(registration)}
        await record_results(
            db,
            [
                _failing("opencti.heartbeat", "opencti", subject),
                _failing("opencti.agentic.connector_backlog", "opencti", subject),
            ],
            now=NOW,
        )
        cycle = HealthCycle(
            specialist="module_analyst",
            trigger="schedule",
            target_type="module",
            target_id=registration,
            month=NOW.strftime("%Y-%m"),
            status="completed",
            opened_at=NOW - timedelta(minutes=5),
            closed_at=NOW,
        )
        db.add(cycle)
        await db.flush()
        heartbeat = finding_key("opencti.heartbeat", subject)
        for text, age in (("Older advice.", 10), ("Restart the worker.", 1)):
            db.add(
                HealthFindingRecommendation(
                    finding_id=heartbeat,
                    registration_id=registration,
                    cycle_id=uuid.uuid4() if age == 10 else cycle.id,
                    module="opencti",
                    text=text,
                    evidence=[{"tool": "list_opencti_connector_states", "object_ids": ["c1"]}],
                    confidence="high",
                    created_at=NOW - timedelta(minutes=age),
                )
            )
        await db.commit()
    return admin, plain, cycle.id, heartbeat


@pytest.mark.asyncio
async def test_module_findings_carry_the_analysts_newest_recommendation_and_the_agentic_tag(xtm_one: None) -> None:
    async with health_database() as sessions:
        admin, _plain, cycle_id, heartbeat = await _world(sessions)
        async with _client(sessions, admin) as client:
            listing = (await client.get("/api/v1/health/findings")).json()
            one = (await client.get(f"/api/v1/health/findings/{heartbeat}")).json()
            agentic_only = (await client.get("/api/v1/health/findings?agentic=true")).json()
            checks_only = (await client.get("/api/v1/health/findings?agentic=false")).json()

    items = {item["check_id"]: item for item in listing["items"]}
    rec = items["opencti.heartbeat"]["analyst_recommendation"]
    assert (rec["text"], rec["confidence"], rec["cycle_id"]) == ("Restart the worker.", "high", str(cycle_id))
    assert rec["evidence"][0]["tool"] == "list_opencti_connector_states"
    assert one["analyst_recommendation"]["text"] == "Restart the worker."
    assert (items["opencti.heartbeat"]["agentic"], items["opencti.agentic.connector_backlog"]["agentic"]) == (
        False,
        True,
    )
    assert items["opencti.agentic.connector_backlog"]["analyst_recommendation"] is None
    assert [item["check_id"] for item in agentic_only["items"]] == ["opencti.agentic.connector_backlog"]
    assert [item["check_id"] for item in checks_only["items"]] == ["opencti.heartbeat"]


@pytest.mark.asyncio
async def test_the_opencti_card_shows_the_last_investigation_to_an_administrator_only(xtm_one: None) -> None:
    async with health_database() as sessions:
        admin, plain, cycle_id, _heartbeat = await _world(sessions)
        async with _client(sessions, admin) as client:
            cards = {card["module"]: card for card in (await client.get("/api/v1/health/overview")).json()["modules"]}
        async with _client(sessions, plain) as client:
            plain_cards = (await client.get("/api/v1/health/overview")).json()["modules"]

    last = cards["opencti"]["last_investigation"]
    assert (last["cycle_id"], last["status"], last["trigger"]) == (str(cycle_id), "completed", "schedule")
    assert cards["openaev"]["last_investigation"] is None
    assert cards["agents"]["last_investigation"] is None
    assert all(card["last_investigation"] is None for card in plain_cards)


@pytest.mark.asyncio
async def test_investigate_now_is_an_administrators_and_refuses_what_it_cannot_start(xtm_one: None) -> None:
    async with health_database() as sessions:
        admin, plain, _cycle_id, _heartbeat = await _world(sessions)
        async with _client(sessions, plain) as client:
            stranger = await client.post("/api/v1/health/modules/opencti/investigate")
        await configure(sessions, admin, enabled=True)
        async with _client(sessions, admin) as client:
            unknown = await client.post("/api/v1/health/modules/agents/investigate")
            nothing = await client.post("/api/v1/health/modules/openaev/investigate")
        await configure(sessions, admin, enabled=True, module_analyst={"enabled": False})
        async with _client(sessions, admin) as client:
            off = await client.post("/api/v1/health/modules/opencti/investigate")

    assert stranger.status_code == 403
    assert unknown.status_code == 404
    assert (off.status_code, off.json()["detail"]["code"]) == (409, "module_analyst_off")
    assert (nothing.status_code, nothing.json()["detail"]["code"]) == (409, "no_registration")
