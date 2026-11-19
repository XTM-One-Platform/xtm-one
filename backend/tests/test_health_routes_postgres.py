"""Platform Concierge level 1 (#8254): the ``/api/v1/health`` routes against PostgreSQL.

* The improvement opt-in as owner, admin, ``read_only`` / ``config`` grantee,
  group grant, revoked share, stranger, on a company-managed agent and on a
  Concierge fleet agent, and on a flow; the opt-out as the user themself;
  Settings and budget as admin only; every route dark while the flag is off.
* A revocation (opt-in off, opt-out on, scope narrowed) runs the registered
  handlers in the same transaction; a failing handler changes nothing.

``integration``-marked; skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import platform_health as concierge_api
from app.core.config import settings
from app.core.database import get_db
from app.models.agent import Agent
from app.models.audit_log import AuditLog
from app.models.flow import Flow
from app.models.user import User
from app.services import agentic_gate
from app.services.health import consent
from app.services.health.consent import Revocation
from tests.health_support import World, health_database

pytestmark = pytest.mark.integration


@pytest.fixture
def handlers(monkeypatch) -> list[Revocation]:
    """Record every revocation, in place of the later levels' handlers."""
    seen: list[Revocation] = []
    monkeypatch.setattr(consent, "_HANDLERS", [])

    async def _record(_db, revocation: Revocation) -> None:
        seen.append(revocation)

    consent.register_revocation_handler(_record)
    return seen

@pytest.fixture
def licensed(monkeypatch):
    async def _verdict(_db):
        return agentic_gate.AgenticVerdict(refusal=None)

    monkeypatch.setattr(agentic_gate, "agentic_verdict", _verdict)


@asynccontextmanager
async def _client(sessions: async_sessionmaker, user: User) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(concierge_api.router, prefix="/api/v1/health")

    async def _get_db():
        async with sessions() as db:
            yield db
            await db.commit()

    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def _opt_in(sessions, model, row_id) -> bool:
    async with sessions() as db:
        return (await db.get(model, row_id)).health_enrolled


# ── Routes: who may do what ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("who", "status"),
    [
        ("owner", 200),
        ("admin", 200),
        ("configurer", 200),
        ("member", 200),
        ("reader", 403),
        ("revoked", 404),
        ("stranger", 404),
    ],
)
async def test_the_agent_opt_in_is_written_by_whoever_may_manage_it(who: str, status: int, handlers) -> None:
    async with health_database() as sessions:
        world = await World().build(sessions)
        async with _client(sessions, getattr(world, who)) as client:
            response = await client.put(f"/api/v1/health/agents/{world.private.id}/opt-in", json={"opt_in": True})
        assert response.status_code == status
        assert await _opt_in(sessions, Agent, world.private.id) is (status == 200)
        if status == 200:
            async with sessions() as db:
                rows = (await db.execute(select(AuditLog).where(AuditLog.entity_id == world.private.id))).scalars().all()
            assert [(r.action, r.details) for r in rows] == [("update", {"health_enrolled": True})]
            assert rows[0].user_id == getattr(world, who).id
        assert handlers == []


async def test_a_revoked_share_loses_the_opt_in_at_once(handlers) -> None:
    async with health_database() as sessions:
        world = await World().build(sessions)
        url = f"/api/v1/health/agents/{world.private.id}/opt-in"
        async with _client(sessions, world.configurer) as client:
            assert (await client.get(url)).json() == {"opt_in": False, "can_manage": True, "eligible": True}
            async with sessions() as db:
                agent = await db.get(Agent, world.private.id)
                agent.shared_user_ids = [str(world.reader.id)]
                agent.shared_user_permissions = {str(world.reader.id): "read_only"}
                await db.commit()
            assert (await client.put(url, json={"opt_in": True})).status_code == 404
        async with _client(sessions, world.reader) as client:
            assert (await client.get(url)).json() == {"opt_in": False, "can_manage": False, "eligible": True}


async def test_a_company_managed_agent_is_opted_in_by_admins_only(handlers) -> None:
    async with health_database() as sessions:
        world = await World().build(sessions)
        url = f"/api/v1/health/agents/{world.company.id}/opt-in"
        async with _client(sessions, world.stranger) as client:
            assert (await client.put(url, json={"opt_in": True})).status_code == 403
        async with _client(sessions, world.admin) as client:
            assert (await client.put(url, json={"opt_in": True})).status_code == 200
        assert await _opt_in(sessions, Agent, world.company.id) is True


async def test_a_fleet_agent_has_no_opt_in(handlers) -> None:
    async with health_database() as sessions:
        world = await World().build(sessions)
        url = f"/api/v1/health/agents/{world.fleet.id}/opt-in"
        async with _client(sessions, world.admin) as client:
            assert (await client.get(url)).json() == {"opt_in": False, "can_manage": False, "eligible": False}
            assert (await client.put(url, json={"opt_in": True})).status_code == 409
        assert await _opt_in(sessions, Agent, world.fleet.id) is False


@pytest.mark.parametrize(("who", "status"), [("owner", 200), ("member", 200), ("reader", 403), ("stranger", 404)])
async def test_the_flow_opt_in_follows_the_same_rule(who: str, status: int, handlers) -> None:
    async with health_database() as sessions:
        world = await World().build(sessions)
        async with _client(sessions, getattr(world, who)) as client:
            response = await client.put(f"/api/v1/health/flows/{world.flow.id}/opt-in", json={"opt_in": True})
        assert response.status_code == status
        assert await _opt_in(sessions, Flow, world.flow.id) is (status == 200)


async def test_turning_an_opt_in_off_revokes_in_the_same_transaction(handlers) -> None:
    async with health_database() as sessions:
        world = await World().build(sessions)
        async with _client(sessions, world.owner) as client:
            agent_url = f"/api/v1/health/agents/{world.private.id}/opt-in"
            flow_url = f"/api/v1/health/flows/{world.flow.id}/opt-in"
            await client.put(agent_url, json={"opt_in": True})
            await client.put(agent_url, json={"opt_in": False})
            await client.put(flow_url, json={"opt_in": True})
            await client.put(flow_url, json={"opt_in": False})
            await client.put(flow_url, json={"opt_in": False})
        assert handlers == [
            Revocation(kind="agent_opt_in_revoked", agent_id=world.private.id),
            Revocation(kind="flow_opt_in_revoked", flow_id=world.flow.id),
        ]


async def test_a_failing_revocation_changes_nothing(monkeypatch) -> None:
    async def _boom(_db, _revocation) -> None:
        raise RuntimeError("datasets unreachable")

    monkeypatch.setattr(consent, "_HANDLERS", [_boom])
    async with health_database() as sessions:
        world = await World().build(sessions)
        async with sessions() as db:
            (await db.get(Agent, world.private.id)).health_enrolled = True
            await db.commit()
        async with _client(sessions, world.owner) as client:
            response = await client.put(f"/api/v1/health/agents/{world.private.id}/opt-in", json={"opt_in": False})
        assert response.status_code == 500
        assert response.json() == {"detail": concierge_api.NOT_APPLIED_DETAIL}
        assert await _opt_in(sessions, Agent, world.private.id) is True


async def test_the_opt_out_is_the_users_own(handlers) -> None:
    async with health_database() as sessions:
        world = await World().build(sessions)
        async with _client(sessions, world.reader) as client:
            assert (await client.get("/api/v1/health/me/opt-out")).json() == {"opt_out": False}
            assert (await client.put("/api/v1/health/me/opt-out", json={"opt_out": True})).json() == {"opt_out": True}
            assert (await client.put("/api/v1/health/me/opt-out", json={"opt_out": True})).status_code == 200
            bad = await client.put("/api/v1/health/me/opt-out", json={"opt_out": True, "user_id": str(world.owner.id)})
            assert bad.status_code == 422
        async with sessions() as db:
            assert (await db.get(User, world.reader.id)).health_content_opt_out is True
            assert (await db.get(User, world.owner.id)).health_content_opt_out is False
        assert handlers == [Revocation(kind="user_opt_out", user_id=world.reader.id)]


async def test_settings_and_budget_are_for_admins(handlers, licensed) -> None:
    async with health_database() as sessions:
        world = await World().build(sessions)
        async with _client(sessions, world.configurer) as client:
            assert (await client.get("/api/v1/health/settings")).status_code == 403
            assert (await client.put("/api/v1/health/settings", json={"enabled": True})).status_code == 403
            assert (await client.get("/api/v1/health/budget")).status_code == 403
        async with _client(sessions, world.admin) as client:
            body = {"enabled": True, "scope_mode": "selected", "scope_agent_ids": [str(world.private.id)]}
            saved = (await client.put("/api/v1/health/settings", json=body)).json()
            assert saved["enabled"] is True and saved["scope_agent_ids"] == [str(world.private.id)]
            assert (await client.put("/api/v1/health/settings", json={**body, "typo": 1})).status_code == 422
            budget_view = (await client.get("/api/v1/health/budget")).json()
            assert budget_view["used"]["tokens"] == 0
            assert budget_view["refusal"] is None
            assert budget_view["remaining_tokens"] == 10_000_000
        # "all" then "selected" is a narrowing; enabling is not.
        assert handlers == [Revocation(kind="scope_narrowed")]


async def test_every_route_is_served_without_any_flag(monkeypatch, handlers) -> None:
    """The capability is built in: no feature flag darkens a route."""
    monkeypatch.setattr(settings, "enabled_features", "")
    async with health_database() as sessions:
        world = await World().build(sessions)
        async with _client(sessions, world.admin) as client:
            for method, url in [
                ("GET", f"/api/v1/health/agents/{world.private.id}/opt-in"),
                ("GET", f"/api/v1/health/flows/{world.flow.id}/opt-in"),
                ("GET", "/api/v1/health/me/opt-out"),
                ("GET", "/api/v1/health/settings"),
                ("GET", "/api/v1/health/budget"),
            ]:
                assert (await client.request(method, url)).status_code != 403, url

