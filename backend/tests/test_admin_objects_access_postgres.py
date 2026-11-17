"""An admin opens and modifies every Administration > Objects type.

Administration > Objects lists colleagues' private copies of every library type
(``api/v1/admin_objects.py`` ``ENTITY_MAP``) and each row opens that type's own
detail page. Agent tools deliberately keep three-tier visibility for an admin;
the REST routes behind those pages must not: the admin opens, edits and
deletes an object owned by another user and shared with nobody. Driven through
the real routers against real PostgreSQL, as the admin and, for contrast, as a
non-admin stranger who must see nothing.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours; the temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import (
    agent_skills,
    agents,
    custom_tools,
    integrations,
    knowledge_bases,
    mcp_servers,
    presentations,
    prompts,
    runners,
    template_containers,
)
from app.core.config import settings
from app.core.database import get_db
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.custom_tool import CustomTool
from app.models.integration import Integration
from app.models.knowledge_base import KnowledgeBase
from app.models.mcp_server import MCPServer
from app.models.presentation import Presentation
from app.models.prompt import Prompt
from app.models.runner import Runner
from app.models.template_container import TemplateContainer
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_DECK_SPEC = {"title": "Deck", "slides": [{"layout": "title", "title": "Deck"}]}


@dataclass(frozen=True)
class ObjectType:
    """One Objects type: its route, how to seed it, and how to modify it."""

    key: str
    prefix: str
    router: Any
    build: Callable[[uuid.UUID], Any]
    modify_method: str
    modify_body: Callable[[dict], dict]


OBJECT_TYPES = (
    ObjectType(
        "agent",
        "/api/v1/agents",
        agents.router,
        lambda owner: Agent(name="Private agent", slug=f"a-{uuid.uuid4().hex[:8]}", user_id=owner),
        "PUT",
        lambda _: {"name": "Renamed by admin"},
    ),
    ObjectType(
        "knowledge_base",
        "/api/v1/knowledge-bases",
        knowledge_bases.router,
        lambda owner: KnowledgeBase(name="Private KB", user_id=owner),
        "PUT",
        lambda _: {"name": "Renamed by admin"},
    ),
    ObjectType(
        "prompt",
        "/api/v1/prompts",
        prompts.router,
        lambda owner: Prompt(name="Private prompt", content="Say hello.", user_id=owner),
        "PUT",
        lambda _: {"name": "Renamed by admin"},
    ),
    ObjectType(
        "skill",
        "/api/v1/skills",
        agent_skills.router,
        lambda owner: AgentSkill(name="Private skill", user_id=owner),
        "PUT",
        lambda _: {"name": "Renamed by admin"},
    ),
    ObjectType(
        "mcp_server",
        "/api/v1/mcp-servers",
        mcp_servers.router,
        lambda owner: MCPServer(name="Private MCP", user_id=owner),
        "PUT",
        lambda _: {"name": "Renamed by admin"},
    ),
    ObjectType(
        "custom_tool",
        "/api/v1/custom-tools",
        custom_tools.router,
        lambda owner: CustomTool(name="private_tool", user_id=owner),
        "PUT",
        lambda _: {"name": "renamed_by_admin"},
    ),
    ObjectType(
        "integration",
        "/api/v1/integrations",
        integrations.router,
        lambda owner: Integration(provider="rss", name="Private feed", user_id=owner),
        "PUT",
        lambda _: {"name": "Renamed by admin"},
    ),
    ObjectType(
        "presentation",
        "/api/v1/presentations",
        presentations.router,
        lambda owner: Presentation(title="Deck", spec=dict(_DECK_SPEC), revision=1, user_id=owner),
        "PATCH",
        lambda opened: {"title": "Renamed by admin", "expected_revision": opened["revision"]},
    ),
    ObjectType(
        "template_container",
        "/api/v1/template-containers",
        template_containers.router,
        lambda owner: TemplateContainer(name="Private template", user_id=owner),
        "PUT",
        lambda _: {"name": "Renamed by admin"},
    ),
    ObjectType(
        "runner",
        "/api/v1/runners",
        runners.router,
        lambda owner: Runner(
            name="Private machine",
            platform="linux",
            key_prefix=uuid.uuid4().hex[:12],
            key_hash="not-a-real-hash",
            user_id=owner,
        ),
        "PATCH",
        lambda _: {"name": "Renamed by admin"},
    ),
)


@asynccontextmanager
async def _client(session_factory: async_sessionmaker, user: User) -> AsyncIterator[httpx.AsyncClient]:
    """Every Objects router, as *user*, on its own session per request."""
    app = FastAPI()
    for object_type in OBJECT_TYPES:
        app.include_router(object_type.router, prefix=object_type.prefix)

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


@pytest.fixture
def runners_enabled(monkeypatch):
    if hasattr(settings, "runners_enabled"):
        monkeypatch.setattr(settings, "runners_enabled", True)


@pytest.mark.parametrize("object_type", OBJECT_TYPES, ids=lambda t: t.key)
async def test_an_admin_opens_modifies_and_deletes_a_colleague_s_private_object(
    object_type: ObjectType, runners_enabled
) -> None:
    async with scratch_database(
        f"objects_{object_type.key}",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
    ) as scratch:
        async with scratch.sessions() as db:
            colleague, stranger, admin = (
                User(email=f"{name}@example.com", hashed_password="x", full_name=name, role=role)
                for name, role in (("colleague", "user"), ("stranger", "user"), ("admin", "admin"))
            )
            db.add_all([colleague, stranger, admin])
            await db.flush()
            row = object_type.build(colleague.id)
            db.add(row)
            await db.commit()
            object_id = str(row.id)
        url = f"{object_type.prefix}/{object_id}"

        async with _client(scratch.sessions, stranger) as client:
            response = await client.get(url)
            assert response.status_code in {403, 404}, response.text

        async with _client(scratch.sessions, admin) as client:
            opened = await client.get(url)
            assert opened.status_code == 200, opened.text

            modified = await client.request(object_type.modify_method, url, json=object_type.modify_body(opened.json()))
            assert modified.status_code == 200, modified.text

            deleted = await client.delete(url)
            assert deleted.status_code == 204, deleted.text
            assert (await client.get(url)).status_code == 404
