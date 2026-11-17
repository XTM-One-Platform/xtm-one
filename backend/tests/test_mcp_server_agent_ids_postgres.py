"""An MCP server carries no list of agents.

``MCPServer.agent_ids`` was accepted by ``POST`` / ``PUT /mcp-servers``, stored
and served back, but nothing ever read it: an agent uses the servers its own
``Agent.mcp_server_ids`` names. The API now neither takes nor serves it, and
the model no longer maps it. A migrated database still holds the nullable
column, unread (the previous release selects it during a rolling deploy), so
the routes are driven here against a schema that has it, as a non-admin owner,
through the real router on PostgreSQL.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import mcp_servers as mcp_servers_api
from app.core.database import get_db
from app.models.mcp_server import MCPServer
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

PREFIX = "/api/v1/mcp-servers"
LEGACY_AGENT_ID = str(uuid.uuid4())


def test_the_model_does_not_map_agent_ids():
    assert "agent_ids" not in MCPServer.__table__.c


@asynccontextmanager
async def _migrated_database() -> AsyncIterator[async_sessionmaker]:
    """A scratch database shaped like a migrated install: the column is still there."""
    async with scratch_database(
        "mcp_agent_ids",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
        refuse_other_databases=True,
    ) as db:
        async with db.sessions() as session:
            await session.execute(text("ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS agent_ids JSON"))
            await session.commit()
        yield db.sessions


async def _user(sessions: async_sessionmaker) -> User:
    async with sessions() as db:
        user = User(
            email=f"user-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",
            full_name="User",
            role="user",
        )
        db.add(user)
        await db.commit()
        return user


@asynccontextmanager
async def _client(sessions: async_sessionmaker, user: User) -> AsyncIterator[httpx.AsyncClient]:
    """The real MCP server router, as *user*, one session per request."""
    app = FastAPI()
    app.include_router(mcp_servers_api.router, prefix=PREFIX)

    async def _get_db():
        async with sessions() as db:
            yield db
            await db.commit()

    async def _current_user(db: AsyncSession = Depends(get_db)) -> User:
        return await db.get(User, user.id)

    app.dependency_overrides[deps.get_current_user] = _current_user
    app.dependency_overrides[get_db] = _get_db
    for gate in (deps.require_agentic_ai, deps.require_ai_enabled, deps.require_agentic_ai_licensed):
        app.dependency_overrides[gate] = lambda: None
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _stored_agent_ids(sessions: async_sessionmaker, server_id: str):
    async with sessions() as db:
        return (
            await db.execute(
                text("SELECT agent_ids FROM mcp_servers WHERE id = :id"), {"id": uuid.UUID(server_id)}
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_create_ignores_agent_ids_and_does_not_serve_them():
    async with _migrated_database() as sessions:
        owner = await _user(sessions)
        async with _client(sessions, owner) as client:
            created = await client.post(
                PREFIX,
                json={"name": "Local", "transport": "stdio", "command": "npx", "agent_ids": [LEGACY_AGENT_ID]},
            )
        assert created.status_code == 201, created.text
        assert "agent_ids" not in created.json()
        assert await _stored_agent_ids(sessions, created.json()["id"]) is None


@pytest.mark.asyncio
async def test_update_leaves_a_stored_value_alone_and_reads_never_serve_it():
    async with _migrated_database() as sessions:
        owner = await _user(sessions)
        async with _client(sessions, owner) as client:
            created = await client.post(PREFIX, json={"name": "Local", "transport": "stdio", "command": "npx"})
        assert created.status_code == 201, created.text
        server_id = created.json()["id"]
        # A value an earlier release stored.
        async with sessions() as db:
            await db.execute(
                text("UPDATE mcp_servers SET agent_ids = CAST(:ids AS JSON) WHERE id = :id"),
                {"ids": f'["{LEGACY_AGENT_ID}"]', "id": uuid.UUID(server_id)},
            )
            await db.commit()

        async with _client(sessions, owner) as client:
            updated = await client.put(f"{PREFIX}/{server_id}", json={"name": "Renamed", "agent_ids": []})
            detail = await client.get(f"{PREFIX}/{server_id}")
            listed = await client.get(PREFIX)

        assert updated.status_code == 200, updated.text
        assert updated.json()["name"] == "Renamed"
        assert detail.status_code == 200, detail.text
        assert listed.status_code == 200, listed.text
        [row] = [s for s in listed.json() if s["id"] == server_id]
        for shown in (updated.json(), detail.json(), row):
            assert "agent_ids" not in shown
        assert await _stored_agent_ids(sessions, server_id) == [LEGACY_AGENT_ID]
