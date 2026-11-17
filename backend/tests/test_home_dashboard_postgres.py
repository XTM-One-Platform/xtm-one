"""A custom dashboard as one's dashboard, against PostgreSQL, through the real routes.

The owner chooses one of their own dashboards (``PUT /settings/preferences``
``home_dashboard_id``) and ``/auth/me`` carries it; null brings back the
platform's dashboard. Another person's dashboard is the 404 of an unknown id,
an administrator's included (an admin reads others' dashboards, never makes one
theirs); a malformed id is a 422 from the body model. Deleting the dashboard
brings back the platform's (``ON DELETE SET NULL``).

``integration``-marked; the temporary database is ``tests/scratch_database.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import auth, custom_dashboards, settings
from app.core.database import get_db
from app.models.audit_log import AuditLog
from app.models.custom_dashboard import CustomDashboard
from app.models.user import User
from app.services.custom_dashboard_spec import validate_spec
from tests.scratch_database import ALL_TABLES, scratch_database

# Every dashboard route is behind DASHBOARD_STUDIO: on for the module.
pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("dashboard_studio_enabled")]

_SPEC = {"title": "Board brief", "widgets": [{"kind": "score", "id": "score", "label": "Exposure score", "value": 58}]}


@asynccontextmanager
async def _client(session_factory, user_id: uuid.UUID) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(settings.router, prefix="/api/v1/settings")
    app.include_router(auth.router, prefix="/api/v1/auth")
    app.include_router(custom_dashboards.router, prefix="/api/v1/custom-dashboards")

    async def _get_db():
        async with session_factory() as db:
            yield db
            await db.commit()

    async def _signed_in(db: AsyncSession = Depends(get_db)) -> User:
        # Loaded in the request's own session, as the real dependency does: the
        # route's writes on it are the ones committed.
        return await db.get(User, user_id)

    for dependency in (deps.get_current_user, deps.get_current_user_ungated):
        app.dependency_overrides[dependency] = _signed_in
    app.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@asynccontextmanager
async def _world():
    async with scratch_database(
        "home_dashboard", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = User(email="owner@example.com", hashed_password="x", full_name="Owner", role="user")
            other = User(email="other@example.com", hashed_password="x", full_name="Other", role="user")
            admin = User(email="admin@example.com", hashed_password="x", full_name="Admin", role="admin")
            db.add_all([owner, other, admin])
            await db.flush()
            dashboard = CustomDashboard(
                user_id=owner.id, title="Board brief", widget_count=1, revision=1, spec=validate_spec(_SPEC)
            )
            db.add(dashboard)
            await db.commit()
        yield scratch, owner.id, other.id, admin.id, dashboard.id


async def _home(scratch, user_id: uuid.UUID) -> uuid.UUID | None:
    async with scratch.sessions() as db:
        return (await db.get(User, user_id)).home_dashboard_id


async def test_the_owner_chooses_their_dashboard_and_clears_it() -> None:
    async with _world() as (scratch, owner, _other, _admin, dashboard_id), _client(scratch.sessions, owner) as client:
        chosen = await client.put("/api/v1/settings/preferences", json={"home_dashboard_id": str(dashboard_id)})
        assert chosen.status_code == 200, chosen.text
        assert await _home(scratch, owner) == dashboard_id
        me = await client.get("/api/v1/auth/me")
        assert me.json()["home_dashboard_id"] == str(dashboard_id)

        # Another preference leaves it as it is; an explicit null clears it.
        assert (await client.put("/api/v1/settings/preferences", json={"theme": "light"})).status_code == 200
        assert await _home(scratch, owner) == dashboard_id
        cleared = await client.put("/api/v1/settings/preferences", json={"home_dashboard_id": None})
        assert cleared.status_code == 200
        assert await _home(scratch, owner) is None
        assert (await client.get("/api/v1/auth/me")).json()["home_dashboard_id"] is None

        async with scratch.sessions() as db:
            from sqlalchemy import select

            details = (
                (await db.execute(select(AuditLog.details).where(AuditLog.entity_type == "preferences"))).scalars().all()
            )
            assert [d.get("home_dashboard_id", "absent") for d in details] == [str(dashboard_id), "absent", None]


@pytest.mark.parametrize("who", ["other", "admin"])
async def test_someone_elses_dashboard_is_not_found(who) -> None:
    async with _world() as (scratch, _owner, other, admin, dashboard_id):
        user_id = other if who == "other" else admin
        async with _client(scratch.sessions, user_id) as client:
            refused = await client.put("/api/v1/settings/preferences", json={"home_dashboard_id": str(dashboard_id)})
            assert (refused.status_code, refused.json()["detail"]) == (404, "Dashboard not found")
            unknown = await client.put("/api/v1/settings/preferences", json={"home_dashboard_id": str(uuid.uuid4())})
            assert unknown.status_code == 404
        assert await _home(scratch, user_id) is None


@pytest.mark.parametrize("value", ["not-a-uuid", "", 42, ["x"]])
async def test_a_malformed_id_is_a_422_that_writes_nothing(value) -> None:
    async with _world() as (scratch, owner, _other, _admin, _dashboard_id), _client(scratch.sessions, owner) as client:
        refused = await client.put("/api/v1/settings/preferences", json={"home_dashboard_id": value, "theme": "light"})
        assert refused.status_code == 422
        async with scratch.sessions() as db:
            stored = await db.get(User, owner)
            assert (stored.home_dashboard_id, stored.theme) == (None, None)


async def test_deleting_the_dashboard_brings_back_the_platforms() -> None:
    async with _world() as (scratch, owner, _other, _admin, dashboard_id), _client(scratch.sessions, owner) as client:
        await client.put("/api/v1/settings/preferences", json={"home_dashboard_id": str(dashboard_id)})
        assert (await client.delete(f"/api/v1/custom-dashboards/{dashboard_id}")).status_code == 204
        assert await _home(scratch, owner) is None
        assert (await client.get("/api/v1/auth/me")).json()["home_dashboard_id"] is None


@pytest.mark.parametrize("who", ["owner", "admin"])
async def test_with_the_studio_off_no_dashboard_is_home_and_none_is_listed(who, monkeypatch) -> None:
    """Off, a stored home dashboard is kept but not served, none can be chosen,
    and the dashboard routes answer like unknown ones, for an admin too."""
    from app.core.config import settings as app_settings

    async with _world() as (scratch, owner, _other, admin, dashboard_id):
        async with scratch.sessions() as db:
            (await db.get(User, owner)).home_dashboard_id = dashboard_id
            await db.commit()
        monkeypatch.setattr(app_settings, "enabled_features", "")
        user_id = owner if who == "owner" else admin
        async with _client(scratch.sessions, user_id) as client:
            assert (await client.get("/api/v1/auth/me")).json()["home_dashboard_id"] is None
            chosen = await client.put("/api/v1/settings/preferences", json={"home_dashboard_id": str(dashboard_id)})
            assert (chosen.status_code, chosen.json()["detail"]) == (404, "Dashboard not found")
            assert (await client.get("/api/v1/custom-dashboards")).status_code == 404
            assert (await client.get(f"/api/v1/custom-dashboards/{dashboard_id}")).status_code == 404
            assert (await client.delete(f"/api/v1/custom-dashboards/{dashboard_id}")).status_code == 404
        # The stored choice is kept: turning the studio back on restores it.
        assert await _home(scratch, owner) == dashboard_id
        async with scratch.sessions() as db:
            assert await db.get(CustomDashboard, dashboard_id) is not None
