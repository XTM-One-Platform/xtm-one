"""The Copilot dashboard each person builds, on PostgreSQL.

``PUT /settings/preferences`` saves the layout of the Copilot dashboard grid
and ``GET /auth/me`` serves it back, to its owner only; an invalid layout is a
422 that leaves the saved one in place, and an explicit null resets it.

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

LAYOUT = {
    "items": [
        {"i": "live_activity", "x": 0, "y": 0, "w": 12, "h": 10},
        {"i": "hero", "x": 0, "y": 10, "w": 8, "h": 7},
    ],
    "hidden": ["digests"],
}


@asynccontextmanager
async def _database():
    async with (
        scratch_database(
            "copilotlayout", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
        ) as db,
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


async def _me(session, user: User) -> dict:
    async with _client(session, user) as client:
        response = await client.get("/api/v1/auth/me")
    assert response.status_code == 200, response.text
    return response.json()


async def _put(session, user: User, body: dict) -> httpx.Response:
    async with _client(session, user) as client:
        return await client.put("/api/v1/settings/preferences", json=body)


async def test_a_saved_layout_is_served_back_to_its_owner_only():
    async with _database() as session:
        owner = await _user(session, "owner")
        other = await _user(session, "other")
        assert (await _me(session, owner))["copilot_dashboard_layout"] is None

        response = await _put(session, owner, {"copilot_dashboard_layout": LAYOUT})
        assert response.status_code == 200, response.text

        me = await _me(session, owner)
        assert me["copilot_dashboard_layout"] == LAYOUT
        assert me["ctem_dashboard_layouts"] is None
        assert (await _me(session, other))["copilot_dashboard_layout"] is None


@pytest.mark.parametrize(
    "layout",
    [
        pytest.param({"items": [{"i": "stage_scoping", "x": 0, "y": 0, "w": 4, "h": 8}]}, id="command-center-widget"),
        pytest.param({"items": [{"i": "hero", "x": 8, "y": 0, "w": 6, "h": 7}]}, id="past-last-column"),
        pytest.param({"items": [], "hidden": ["hero", "hero"]}, id="widget-twice"),
    ],
)
async def test_an_invalid_layout_is_a_422_that_keeps_the_saved_one(layout):
    async with _database() as session:
        user = await _user(session, "person")
        assert (await _put(session, user, {"copilot_dashboard_layout": LAYOUT})).status_code == 200

        response = await _put(session, user, {"copilot_dashboard_layout": layout})

        assert response.status_code == 422, response.text
        assert (await _me(session, user))["copilot_dashboard_layout"] == LAYOUT


async def test_null_resets_and_another_preference_leaves_the_layout_alone():
    async with _database() as session:
        user = await _user(session, "person")
        assert (await _put(session, user, {"copilot_dashboard_layout": LAYOUT})).status_code == 200

        assert (await _put(session, user, {"theme": "light"})).status_code == 200
        assert (await _me(session, user))["copilot_dashboard_layout"] == LAYOUT

        assert (await _put(session, user, {"copilot_dashboard_layout": None})).status_code == 200
        assert (await _me(session, user))["copilot_dashboard_layout"] is None
