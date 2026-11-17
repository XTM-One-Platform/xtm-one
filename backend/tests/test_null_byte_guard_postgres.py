"""A NUL byte in a plain text column never breaks a write (PostgreSQL).

PostgreSQL refuses ``\\u0000`` in any text value
(``CharacterNotInRepertoireError``), and nothing mapped that database error to
a client answer: an agent name, a prompt, a conversation title or a knowledge
document file name carrying one answered 500. The engine now strips NUL from
every bound string (``core/database.py``), so the request gets the answer it
would have had without the NUL. These tests drive the real routes through the
ASGI app against a scratch database created from ``DATABASE_URL`` (same recipe
as ``test_flow_runner_postgres.py``; skipped without PostgreSQL).

The knowledge base's own refusal of a NUL in a document's CONTENT (a 400
naming binary data) is a deliberate contract and is pinned unchanged.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("nulguard", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


@asynccontextmanager
async def _client() -> AsyncIterator[tuple[AsyncClient, async_sessionmaker]]:
    """The real app, authenticated as an admin, on the scratch database."""
    from app.api import deps
    from app.core.database import get_db
    from app.main import application
    from app.models.user import User

    async with _temp_database() as sessions:
        user = User(
            id=uuid.uuid4(),
            email=f"nul-{uuid.uuid4().hex[:8]}@example.com",
            full_name="Nul Guard",
            hashed_password="x",
            role="admin",
        )
        async with sessions() as s:
            s.add(user)
            await s.commit()

        async def _db():
            async with sessions() as s:
                yield s
                await s.commit()

        overrides = {
            get_db: _db,
            deps.get_current_user: lambda: user,
            deps.require_agentic_ai: lambda: None,
            deps.require_ai_enabled: lambda: None,
            deps.require_chat_enabled: lambda: None,
        }
        previous = dict(application.dependency_overrides)
        application.dependency_overrides.update(overrides)
        try:
            transport = ASGITransport(app=application, raise_app_exceptions=False)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                yield client, sessions
        finally:
            application.dependency_overrides.clear()
            application.dependency_overrides.update(previous)


async def _stored(sessions, model, row_id, column: str) -> str:
    async with sessions() as s:
        row = (await s.execute(select(model).where(model.id == uuid.UUID(row_id)))).scalar_one()
        return getattr(row, column)


@pytest.mark.asyncio
async def test_an_agent_name_with_a_nul_is_created():
    from app.models.agent import Agent

    async with _client() as (client, sessions):
        r = await client.post("/api/v1/agents", json={"name": "Triage\u0000 desk"})

        assert r.status_code == 201, r.text
        assert r.json()["name"] == "Triage desk"
        assert await _stored(sessions, Agent, r.json()["id"], "name") == "Triage desk"


@pytest.mark.asyncio
async def test_a_prompt_content_with_a_nul_is_created():
    from app.models.prompt import Prompt

    async with _client() as (client, sessions):
        r = await client.post(
            "/api/v1/prompts", json={"name": "Summary", "content": "Summarise\u0000 it"}
        )

        assert r.status_code == 201, r.text
        assert r.json()["content"] == "Summarise it"
        assert await _stored(sessions, Prompt, r.json()["id"], "content") == "Summarise it"


@pytest.mark.asyncio
async def test_a_conversation_title_with_a_nul_is_created_and_renamed():
    from app.models.chat import Conversation

    async with _client() as (client, sessions):
        created = await client.post("/api/v1/chat/conversations", json={"title": "Q3\u0000 plan"})
        assert created.status_code == 201, created.text
        assert created.json()["title"] == "Q3 plan"
        conv_id = created.json()["id"]
        assert await _stored(sessions, Conversation, conv_id, "title") == "Q3 plan"

        renamed = await client.patch(
            f"/api/v1/chat/conversations/{conv_id}", json={"title": "Q4\u0000 plan"}
        )

        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["title"] == "Q4 plan"
        assert await _stored(sessions, Conversation, conv_id, "title") == "Q4 plan"


@pytest.mark.asyncio
async def test_a_knowledge_document_file_name_with_a_nul_is_created():
    from app.models.knowledge_base import KnowledgeDocument

    async with _client() as (client, sessions):
        kb = await client.post("/api/v1/knowledge-bases", json={"name": "Runbooks"})
        assert kb.status_code == 201, kb.text

        r = await client.post(
            f"/api/v1/knowledge-bases/{kb.json()['id']}/documents/text",
            json={"filename": "on\u0000call.md", "content": "Page the on-call engineer."},
        )

        assert r.status_code == 201, r.text
        assert r.json()["filename"] == "oncall.md"
        assert await _stored(sessions, KnowledgeDocument, r.json()["id"], "filename") == "oncall.md"


@pytest.mark.asyncio
async def test_a_nul_in_knowledge_document_content_is_still_refused():
    async with _client() as (client, _sessions):
        kb = await client.post("/api/v1/knowledge-bases", json={"name": "Runbooks"})

        r = await client.post(
            f"/api/v1/knowledge-bases/{kb.json()['id']}/documents/text",
            json={"filename": "binary.md", "content": "raw\u0000bytes"},
        )

        assert r.status_code == 400
        assert "binary data" in r.json()["detail"]
