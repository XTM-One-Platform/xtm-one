"""The Concierge's name (#8261) against PostgreSQL: the routes, the seed and the rename.

* ``GET /concierge/name`` answers every signed-in person, admin or not; a
  hostile name is a 422 from ``PUT /concierge/settings``, never a 500.
* The fleet is seeded as "Platform Health" in every mode (#101); a row still
  carrying a former default ("Alfred", "Concierge") is renamed; a saved name
  renames the orchestrator, its fleet and its workspace; a row an
  administrator renamed by hand keeps its name.

``integration``-marked; skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import concierge as concierge_api
from app.core.config import settings
from app.core.database import get_db
from app.models.agent import Agent
from app.models.fleet import Fleet
from app.models.user import User
from app.models.workspace import Workspace
from app.services.health import fleet_seed
from app.services.health.settings import load_concierge_settings
from tests.concierge_support import World, concierge_database, configure

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setattr(settings, "enabled_features", "CONCIERGE")


@asynccontextmanager
async def _client(sessions, user: User) -> AsyncIterator[httpx.AsyncClient]:
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


async def _names(sessions) -> tuple[str, str, str]:
    """The orchestrator's, its fleet's and its workspace's names."""
    async with sessions() as db:
        orchestrator = (await db.execute(select(Agent).where(Agent.slug == fleet_seed.ORCHESTRATOR_SLUG))).scalar_one()
        fleet = (await db.execute(select(Fleet).where(Fleet.orchestrator_agent_id == orchestrator.id))).scalar_one()
        workspace = await db.get(Workspace, fleet.workspace_id)
        return orchestrator.name, fleet.name, workspace.name


async def test_every_signed_in_person_reads_the_name(monkeypatch) -> None:
    monkeypatch.setattr(settings, "platform_mode", "copilot")
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        async with _client(sessions, world.stranger) as client:
            default = await client.get("/api/v1/health/name")
        await configure(sessions, world.admin, name="Jeeves")
        async with _client(sessions, world.reader) as client:
            named = await client.get("/api/v1/health/name")
    assert default.json() == {"name": "Platform Health", "default_name": "Platform Health"}
    assert named.json() == {"name": "Jeeves", "default_name": "Platform Health"}


async def test_the_name_route_is_served_without_any_flag(monkeypatch) -> None:
    """The capability is built in: no feature flag darkens the route."""
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        monkeypatch.setattr(settings, "enabled_features", "")
        async with _client(sessions, world.admin) as client:
            assert (await client.get("/api/v1/health/name")).status_code == 200


@pytest.mark.parametrize("name", ["Al\nfred", "x" * 41, "Al‮fred", "Al\x00fred", 7])
async def test_a_hostile_name_is_a_422_and_nothing_is_saved(name) -> None:
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        async with _client(sessions, world.admin) as client:
            response = await client.put("/api/v1/health/settings", json={"enabled": True, "name": name})
        async with sessions() as db:
            stored = await load_concierge_settings(db)
    assert response.status_code == 422
    assert (stored.enabled, stored.name) == (False, None)


async def test_a_non_admin_cannot_name_the_concierge() -> None:
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        async with _client(sessions, world.configurer) as client:
            response = await client.put("/api/v1/health/settings", json={"name": "Mine"})
        async with sessions() as db:
            stored = await load_concierge_settings(db)
    assert response.status_code == 403
    assert stored.name is None


async def test_the_fleet_is_seeded_as_platform_health_and_a_former_default_is_renamed(monkeypatch) -> None:
    async with concierge_database() as sessions:
        await World().build(sessions)
        monkeypatch.setattr(settings, "platform_mode", "copilot")
        async with sessions() as db:
            await fleet_seed.seed_concierge_fleet(db)
        seeded = await _names(sessions)
        # A deployment seeded before #101 carries the former copilot default.
        async with sessions() as db:
            orchestrator = (
                await db.execute(select(Agent).where(Agent.slug == fleet_seed.ORCHESTRATOR_SLUG))
            ).scalar_one()
            fleet = (await db.execute(select(Fleet))).scalar_one()
            workspace = await db.get(Workspace, fleet.workspace_id)
            orchestrator.name = fleet.name = workspace.name = "Alfred"
            await db.commit()
        monkeypatch.setattr(settings, "platform_mode", "xtm_one")
        async with sessions() as db:
            await fleet_seed.seed_concierge_fleet(db)
        reseeded = await _names(sessions)
    assert seeded == ("Platform Health", "Platform Health", "Platform Health")
    assert reseeded == ("Platform Health", "Platform Health", "Platform Health")


async def test_a_saved_name_renames_the_fleet_but_not_a_hand_renamed_row(monkeypatch) -> None:
    monkeypatch.setattr(settings, "platform_mode", "copilot")
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        async with sessions() as db:
            await fleet_seed.seed_concierge_fleet(db)
            fleet = (await db.execute(select(Fleet))).scalar_one()
            fleet.name = "Improvement crew"
            await db.commit()
        async with _client(sessions, world.admin) as client:
            saved = await client.put("/api/v1/health/settings", json={"enabled": True, "name": "  Jeeves "})
        renamed = await _names(sessions)
        async with sessions() as db:
            await fleet_seed.seed_concierge_fleet(db)
        reseeded = await _names(sessions)
        async with _client(sessions, world.admin) as client:
            await client.put("/api/v1/health/settings", json={"enabled": True, "name": None})
        back_to_default = await _names(sessions)
    assert saved.status_code == 200 and saved.json()["name"] == "Jeeves"
    assert renamed == ("Jeeves", "Improvement crew", "Jeeves")
    assert reseeded == renamed
    assert back_to_default == ("Platform Health", "Improvement crew", "Platform Health")


async def test_the_budget_line_names_the_concierge_as_its_administrator_named_it(monkeypatch) -> None:
    from app.services import agentic_gate

    async def _verdict(_db):
        return agentic_gate.AgenticVerdict(refusal=None)

    monkeypatch.setattr(agentic_gate, "agentic_verdict", _verdict)
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        await configure(sessions, world.admin, enabled=False, name="{Jeeves}")
        async with _client(sessions, world.admin) as client:
            budget = (await client.get("/api/v1/health/budget")).json()
    assert (budget["refusal"], budget["refusal_message"]) == ("concierge_disabled", "{Jeeves} is switched off.")
