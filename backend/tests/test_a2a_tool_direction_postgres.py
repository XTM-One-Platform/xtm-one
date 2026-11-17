"""A tool-created A2A integration mints from the Configuration tab, on PostgreSQL.

The acceptance of #11778 end to end: the ``create_integration`` tool, then
``POST /integrations/{id}/a2a-credential`` (the "Generate credential" button)
through the real router. An ``oauth_a2a`` integration created with no
direction mints (200); an outbound one has its card URLs as targets and is
refused (422); ``entra_a2a`` and ``google_a2a`` get their settings row too.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.deps import get_current_user
from app.api.v1 import integrations as integrations_api
from app.core.database import get_db
from app.models.a2a import A2AClientTarget, A2AIntegrationSettings
from app.models.integration import Integration
from app.models.user import User
from app.services.builtin_tools import build_agent_builder_tool
from app.tools.base import ToolCall
from tests.a2a_chat_gate import stub_chat_gate
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _world() -> AsyncIterator[tuple]:
    async with scratch_database(
        "a2a_tool_dir", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            user = User(
                email=f"op-{uuid.uuid4().hex[:6]}@example.com", hashed_password="x", full_name="Op", role="admin"
            )
            db.add(user)
            await db.commit()
        application = FastAPI()
        application.include_router(integrations_api.router, prefix="/api/v1/integrations")

        async def _get_db():
            async with scratch.sessions() as db:
                yield db
                await db.commit()

        application.dependency_overrides[get_db] = _get_db
        application.dependency_overrides[get_current_user] = lambda: user
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield scratch.sessions, user, client


async def _create(user: User, **arguments) -> uuid.UUID:
    _, executor = build_agent_builder_tool(None, str(user.id))
    result = await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name="create_integration", arguments=arguments))
    assert "created successfully" in result.content, result.content
    return uuid.UUID(result.content.split("ID: `")[1].split("`")[0])


async def _direction(sessions, integration_id) -> str | None:
    async with sessions() as db:
        row = (
            await db.execute(
                select(A2AIntegrationSettings).where(A2AIntegrationSettings.integration_id == integration_id)
            )
        ).scalar_one_or_none()
        return row.direction if row is not None else None


@pytest.mark.asyncio
async def test_a_tool_created_oauth_a2a_mints_a_partner_credential(monkeypatch):
    stub_chat_gate(monkeypatch)
    async with _world() as (sessions, user, client):
        integration_id = await _create(user, provider="oauth_a2a", name="Partner")
        assert await _direction(sessions, integration_id) == "inbound"

        response = await client.post(f"/api/v1/integrations/{integration_id}/a2a-credential")

        assert response.status_code == 200, response.text
        assert response.json()["client_id"].startswith("a2a-")
        assert response.json()["client_secret"]


@pytest.mark.asyncio
async def test_a_tool_created_outbound_syncs_its_cards_and_refuses_minting(monkeypatch):
    stub_chat_gate(monkeypatch)
    async with _world() as (sessions, user, client):
        integration_id = await _create(
            user,
            provider="oauth_a2a",
            name="Partner out",
            a2a_direction="outbound",
            extra_config='{"card_urls": "https://a.example/card.json"}',
        )
        assert await _direction(sessions, integration_id) == "outbound"
        async with sessions() as db:
            targets = (
                (await db.execute(select(A2AClientTarget).where(A2AClientTarget.integration_id == integration_id)))
                .scalars()
                .all()
            )
        assert [t.card_url for t in targets] == ["https://a.example/card.json"]

        response = await client.post(f"/api/v1/integrations/{integration_id}/a2a-credential")

        assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["entra_a2a", "google_a2a"])
async def test_the_federated_providers_get_their_settings_row(monkeypatch, provider):
    stub_chat_gate(monkeypatch)
    async with _world() as (sessions, user, _client):
        integration_id = await _create(user, provider=provider, name="Federated")
        assert await _direction(sessions, integration_id) == "inbound"
        async with sessions() as db:
            assert (await db.get(Integration, integration_id)).provider == provider
