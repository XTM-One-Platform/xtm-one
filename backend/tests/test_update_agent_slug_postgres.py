"""The ``update_agent`` builder tool changes an agent's slug.

The slug branch of ``_update_agent`` ran its uniqueness query on ``Agent``,
which the module only imports under ``TYPE_CHECKING``: every call naming a
slug answered ``name 'Agent' is not defined`` and dropped the other fields of
the call. The real tool runs here against PostgreSQL, so the uniqueness query
is PostgreSQL's own answer. Same scratch-database recipe as
``test_trigger_integration_writes_postgres.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.user import User
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "agentslug",
        tables=ALL_TABLES,
        extensions=("vector",),
        bind_app_sessions=False,
    ) as db:
        yield db.sessions


async def _seed(sessions) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with sessions() as session:
        owner = User(
            email=f"owner-{uuid.uuid4().hex[:8]}@test.local",
            full_name="Owner",
            hashed_password="x",
            role="user",
        )
        session.add(owner)
        await session.flush()
        agent = Agent(name="Office orchestrator", slug="office-orchestrator", user_id=owner.id)
        other = Agent(name="Other agent", slug="taken-slug", user_id=owner.id)
        session.add_all([agent, other])
        await session.commit()
        return owner.id, agent.id, other.id


async def _call(monkeypatch, sessions, user_id, tool, **arguments):
    import app.core.database as database
    import app.services.builtin_tools as builtin_tools_module

    monkeypatch.setattr(database, "async_session", sessions)
    _, executor = builtin_tools_module.build_agent_builder_tool(SimpleNamespace(), str(user_id))
    return await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name=tool, arguments=arguments)
    )


async def _stored(sessions, agent_id) -> Agent:
    async with sessions() as session:
        return await session.get(Agent, agent_id)


@pytest.mark.asyncio
async def test_update_agent_applies_a_slug_alone(monkeypatch):
    async with _temp_database() as sessions:
        owner, agent_id, _ = await _seed(sessions)
        result = await _call(
            monkeypatch, sessions, owner, "update_agent",
            agent_id=str(agent_id), slug="security-office-orchestrator",
        )
        stored = await _stored(sessions, agent_id)
        shown = await _call(monkeypatch, sessions, owner, "get_agent", agent_id=str(agent_id))

    assert "is not defined" not in str(result.content), result.content
    assert not result.is_error, result.content
    assert stored.slug == "security-office-orchestrator"
    assert "security-office-orchestrator" in str(shown.content), shown.content


@pytest.mark.asyncio
async def test_update_agent_applies_a_slug_with_other_fields(monkeypatch):
    async with _temp_database() as sessions:
        owner, agent_id, _ = await _seed(sessions)
        result = await _call(
            monkeypatch, sessions, owner, "update_agent",
            agent_id=str(agent_id),
            slug="renamed-office",
            name="Renamed office",
            description="Runs the renamed office.",
            tags="office, renamed",
        )
        stored = await _stored(sessions, agent_id)

    assert not result.is_error, result.content
    assert stored.slug == "renamed-office"
    assert stored.name == "Renamed office"
    assert stored.description == "Runs the renamed office."
    assert stored.tags == ["office", "renamed"]


@pytest.mark.asyncio
async def test_update_agent_refuses_a_taken_or_invalid_slug(monkeypatch):
    async with _temp_database() as sessions:
        owner, agent_id, _ = await _seed(sessions)
        taken = await _call(
            monkeypatch, sessions, owner, "update_agent",
            agent_id=str(agent_id), slug="taken-slug", name="Should not land",
        )
        invalid = await _call(
            monkeypatch, sessions, owner, "update_agent",
            agent_id=str(agent_id), slug="Not A Slug!",
        )
        stored = await _stored(sessions, agent_id)

    assert "already taken" in str(taken.content), taken.content
    assert "is not defined" not in str(taken.content), taken.content
    assert "Invalid slug" in str(invalid.content), invalid.content
    assert stored.slug == "office-orchestrator"
    assert stored.name == "Office orchestrator"
