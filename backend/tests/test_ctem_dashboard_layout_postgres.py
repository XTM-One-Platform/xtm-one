"""The command center each person builds, on PostgreSQL.

``PUT /settings/preferences`` saves one vertical's layout of the command
center grid and ``GET /auth/me`` serves the layouts back, to their owner only;
an invalid layout is a 422 that leaves the saved ones in place, and
``layout: null`` resets one vertical.

``integration``-marked and skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import auth as auth_api
from app.api.v1 import settings as settings_api
from app.core.database import get_db
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

CYBER = {
    "items": [
        {"i": "digests", "x": 0, "y": 0, "w": 12, "h": 6},
        {"i": "hero", "x": 0, "y": 6, "w": 8, "h": 8},
    ],
    "hidden": ["briefing"],
}
FRAUD = {"items": [{"i": "kpi_agents", "x": 0, "y": 0, "w": 3, "h": 3}], "hidden": []}


@asynccontextmanager
async def _database():
    async with (
        scratch_database("ctemlayout", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        yield session


@asynccontextmanager
async def _client(session, user: User) -> AsyncIterator[httpx.AsyncClient]:
    api = FastAPI()
    api.include_router(auth_api.router, prefix="/api/v1/auth")
    api.include_router(settings_api.router, prefix="/api/v1/settings")
    api.dependency_overrides[deps.get_current_user] = lambda: user
    api.dependency_overrides[deps.get_current_user_ungated] = lambda: user

    async def _get_db():
        yield session
        await session.commit()

    api.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        yield client


async def _user(session, name: str) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        full_name=name,
        hashed_password="x",
        role="user",
    )
    session.add(user)
    await session.flush()
    return user


async def _saved_layouts(session, user: User) -> dict | None:
    async with _client(session, user) as client:
        response = await client.get("/api/v1/auth/me")
    assert response.status_code == 200, response.text
    return response.json()["ctem_dashboard_layouts"]


async def _put(session, user: User, vertical: str, layout) -> httpx.Response:
    async with _client(session, user) as client:
        return await client.put(
            "/api/v1/settings/preferences",
            json={"ctem_dashboard_layout": {"vertical": vertical, "layout": layout}},
        )


async def test_each_vertical_keeps_its_layout_served_to_its_owner_only():
    async with _database() as session:
        owner = await _user(session, "owner")
        other = await _user(session, "other")
        assert await _saved_layouts(session, owner) is None

        assert (await _put(session, owner, "cyber", CYBER)).status_code == 200
        assert (await _put(session, owner, "fraud", FRAUD)).status_code == 200

        assert await _saved_layouts(session, owner) == {"cyber": CYBER, "fraud": FRAUD}
        assert await _saved_layouts(session, other) is None


@pytest.mark.parametrize(
    ("vertical", "layout"),
    [
        pytest.param("cyber", {"items": [{"i": "unknown", "x": 0, "y": 0, "w": 1, "h": 1}]}, id="unknown-widget"),
        pytest.param("cyber", {"items": [{"i": "hero", "x": 8, "y": 0, "w": 6, "h": 1}]}, id="past-last-column"),
        pytest.param("cyber", {"items": [], "hidden": ["hero", "hero"]}, id="widget-twice"),
        pytest.param("space", None, id="unknown-vertical"),
    ],
)
async def test_an_invalid_layout_is_a_422_that_keeps_the_saved_ones(vertical, layout):
    async with _database() as session:
        user = await _user(session, "person")
        assert (await _put(session, user, "cyber", CYBER)).status_code == 200

        response = await _put(session, user, vertical, layout)

        assert response.status_code == 422, response.text
        assert await _saved_layouts(session, user) == {"cyber": CYBER}


async def test_a_reset_drops_one_vertical_and_another_preference_keeps_them():
    async with _database() as session:
        user = await _user(session, "person")
        assert (await _put(session, user, "cyber", CYBER)).status_code == 200
        assert (await _put(session, user, "fraud", FRAUD)).status_code == 200

        async with _client(session, user) as client:
            kept = await client.put("/api/v1/settings/preferences", json={"ctem_vertical": "fimi"})
        assert kept.status_code == 200, kept.text
        assert await _saved_layouts(session, user) == {"cyber": CYBER, "fraud": FRAUD}

        assert (await _put(session, user, "cyber", None)).status_code == 200
        assert await _saved_layouts(session, user) == {"fraud": FRAUD}
