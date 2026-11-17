"""``POST /api/v1/agents`` stores ``integration_template_bindings``, against PostgreSQL.

The create route validated ``integration_template_read_only`` against the
template bindings of the request, then never wrote the bindings: a create with
both answered ``integration_template_bindings == []`` and an orphan read-only
mark that narrowed nothing until a ``PUT`` set the bindings. These tests read
the stored row after the real route, and pin the 422 for a read-only provider
the request does not bind by template, as ``PUT`` answers it.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours; the temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import agents as agents_api
from app.core.database import get_db
from app.models.agent import Agent
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "agenttpl", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        yield db.sessions


@asynccontextmanager
async def _client(session_factory: async_sessionmaker, user: User) -> AsyncIterator[httpx.AsyncClient]:
    """The agents router, as *user*, on its own session per request."""
    app = FastAPI()
    app.include_router(agents_api.router, prefix="/api/v1/agents")

    async def _get_db():
        async with session_factory() as db:
            yield db
            await db.commit()

    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[deps.require_agentic_ai] = lambda: None
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _owner(sessions) -> User:
    async with sessions() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"owner-{uuid.uuid4().hex[:6]}@example.com",
            full_name="Owner",
            hashed_password="x",
            role="user",
        )
        session.add(user)
        await session.commit()
        return user


async def test_create_stores_the_template_bindings_and_their_read_only_marks() -> None:
    async with _database() as sessions:
        owner = await _owner(sessions)
        async with _client(sessions, owner) as client:
            response = await client.post(
                "/api/v1/agents",
                json={
                    "name": "Triage analyst",
                    "integration_template_bindings": ["opencti"],
                    "integration_template_read_only": ["opencti"],
                },
            )
            omitted = await client.post("/api/v1/agents", json={"name": "Nothing bound"})

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["integration_template_bindings"] == ["opencti"]
        assert body["integration_template_read_only"] == ["opencti"]
        async with sessions() as session:
            stored = await session.get(Agent, uuid.UUID(body["id"]))
            assert stored.integration_template_bindings == ["opencti"]
            assert stored.integration_template_read_only == ["opencti"]

        assert omitted.status_code == 201, omitted.text
        assert omitted.json()["integration_template_bindings"] in ([], None)
        assert omitted.json()["integration_template_read_only"] is None


async def test_create_refuses_a_read_only_mark_on_a_provider_not_bound_by_template() -> None:
    async with _database() as sessions:
        owner = await _owner(sessions)
        async with _client(sessions, owner) as client:
            unbound = await client.post(
                "/api/v1/agents",
                json={"name": "Orphan mark", "integration_template_read_only": ["opencti"]},
            )
        assert unbound.status_code == 422, unbound.text
        detail = unbound.json()["detail"]
        assert "opencti" in detail and "integration_template_bindings" in detail
        async with sessions() as session:
            count = await session.scalar(select(func.count()).select_from(Agent).where(Agent.name == "Orphan mark"))
        assert count == 0
