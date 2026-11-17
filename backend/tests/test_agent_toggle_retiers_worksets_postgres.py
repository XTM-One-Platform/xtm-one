"""An agent's worksets follow its company-managed toggle.

The corpus key a chat (``agent-{id}``) or an assignment run
(``assignment-{id}``) resolves is derived by the runtime and its tier comes from
the agent, so ``resolve_workset`` refuses a corpus left in the other tier. The
toggle therefore moves every workset the agent owns, and the internal knowledge
base of each, in its own transaction: through ``PUT /agents/{id}`` and through
the agent builder's ``update_agent``, behind the same admin gate.

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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import agents as agents_api
from app.core.database import get_db
from app.models.agent import Agent
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.models.workset import Workset
from app.services.builtin_tools import build_agent_builder_tool, chat_workset_key
from app.services.workset import resolve_workset
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "wsretier", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        yield db.sessions


@asynccontextmanager
async def _client(sessions: async_sessionmaker, user: User) -> AsyncIterator[httpx.AsyncClient]:
    """The agents router, as *user*, on its own session per request."""
    app = FastAPI()
    app.include_router(agents_api.router, prefix="/api/v1/agents")

    async def _get_db():
        async with sessions() as db:
            yield db
            await db.commit()

    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _user(session, name: str, *, admin: bool = False) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{name.lower()}-{uuid.uuid4().hex[:6]}@example.com",
        full_name=name,
        hashed_password="x",
        role="admin" if admin else "user",
    )
    session.add(user)
    await session.flush()
    return user


async def _agent(session, owner: User, name: str) -> Agent:
    agent = Agent(
        id=uuid.uuid4(),
        user_id=owner.id,
        name=name,
        slug=f"{name.lower()}-{uuid.uuid4().hex[:6]}",
        persona="x",
        enabled=True,
    )
    session.add(agent)
    await session.flush()
    return agent


async def _workset(session, user: User, agent: Agent, key: str, *, with_corpus: bool) -> Workset:
    """A personal workset the agent owns, with its internal corpus when asked
    (the shape ``ensure_corpus`` gives it)."""
    kb_id = None
    if with_corpus:
        kb = KnowledgeBase(
            id=uuid.uuid4(),
            user_id=user.id,
            is_company_managed=False,
            name=f"workset:{key}",
            is_internal=True,
            is_system_managed=True,
        )
        session.add(kb)
        await session.flush()
        kb_id = kb.id
    workset = Workset(
        id=uuid.uuid4(),
        user_id=user.id,
        is_company_managed=False,
        key=key,
        scope="global",
        owner_agent_id=agent.id,
        knowledge_base_id=kb_id,
    )
    session.add(workset)
    await session.flush()
    return workset


async def _tiers(sessions) -> dict[str, bool]:
    """Every workset's tier by key, and its corpus's as ``kb:<key>``."""
    async with sessions() as session:
        rows = (
            await session.execute(
                select(Workset.key, Workset.is_company_managed, KnowledgeBase.is_company_managed).outerjoin(
                    KnowledgeBase, KnowledgeBase.id == Workset.knowledge_base_id
                )
            )
        ).all()
    tiers: dict[str, bool] = {}
    for key, workset_tier, kb_tier in rows:
        tiers[key] = workset_tier
        if kb_tier is not None:
            tiers[f"kb:{key}"] = kb_tier
    return tiers


@asynccontextmanager
async def _scene() -> AsyncIterator[tuple]:
    """An owner, a person the agent was shared with, an admin; the agent with
    two corpora of its own (the owner's chat corpus with its knowledge base,
    an assignment's corpus without one yet, the other person's chat corpus)
    and another agent's corpus, all personal."""
    async with _database() as sessions:
        async with sessions() as db:
            owner = await _user(db, "Owner")
            colleague = await _user(db, "Colleague")
            admin = await _user(db, "Admin", admin=True)
            agent = await _agent(db, owner, "Analyst")
            other = await _agent(db, owner, "Other")
            chat_key = chat_workset_key(agent.id)
            await _workset(db, owner, agent, chat_key, with_corpus=True)
            await _workset(db, owner, agent, "assignment-1", with_corpus=False)
            await _workset(db, colleague, agent, f"{chat_key}:colleague", with_corpus=True)
            await _workset(db, owner, other, chat_workset_key(other.id), with_corpus=True)
            await db.commit()
        yield sessions, owner, admin, agent, other


def _all_personal(agent: Agent, other: Agent) -> dict[str, bool]:
    chat_key = chat_workset_key(agent.id)
    other_key = chat_workset_key(other.id)
    return {
        chat_key: False,
        f"kb:{chat_key}": False,
        "assignment-1": False,
        f"{chat_key}:colleague": False,
        f"kb:{chat_key}:colleague": False,
        other_key: False,
        f"kb:{other_key}": False,
    }


def _agent_company_managed(agent: Agent, other: Agent) -> dict[str, bool]:
    chat_key = chat_workset_key(agent.id)
    return {
        **_all_personal(agent, other),
        chat_key: True,
        f"kb:{chat_key}": True,
        "assignment-1": True,
        f"{chat_key}:colleague": True,
        f"kb:{chat_key}:colleague": True,
    }


async def test_the_rest_toggle_moves_the_agents_worksets_both_ways() -> None:
    async with _scene() as (sessions, owner, admin, agent, other):
        url = f"/api/v1/agents/{agent.id}"

        # The owner is not an admin: refused, and nothing moves.
        async with _client(sessions, owner) as client:
            refused = await client.put(url, json={"is_company_managed": True})
        assert refused.status_code == 403, refused.text
        assert await _tiers(sessions) == _all_personal(agent, other)

        async with _client(sessions, admin) as client:
            made = await client.put(url, json={"is_company_managed": True})
        assert made.status_code == 200, made.text
        assert await _tiers(sessions) == _agent_company_managed(agent, other)

        # The chat corpus resolves in the agent's new tier: the same row, no
        # refusal and no second corpus.
        async with sessions() as session:
            stored = (
                await session.execute(select(Workset).where(Workset.key == chat_workset_key(agent.id)))
            ).scalar_one()
            caller = await session.get(User, owner.id)
            resolved = await resolve_workset(
                session,
                caller,
                key=chat_workset_key(agent.id),
                scope="global",
                owner_agent_id=agent.id,
                is_company_managed=True,
            )
            assert resolved.id == stored.id
            assert len((await session.execute(select(Workset.id))).all()) == 4

        async with _client(sessions, admin) as client:
            back = await client.put(url, json={"is_company_managed": False})
        assert back.status_code == 200, back.text
        assert await _tiers(sessions) == _all_personal(agent, other)


async def test_the_agent_builder_toggle_moves_the_agents_worksets_both_ways() -> None:
    async with _scene() as (sessions, owner, admin, agent, other):

        async def _update(caller: User, value: str) -> str:
            _, executor = build_agent_builder_tool(None, str(caller.id))
            result = await executor.execute_tool(
                ToolCall(
                    id=str(uuid.uuid4()),
                    name="update_agent",
                    arguments={"agent_id": str(agent.id), "is_company_managed": value},
                )
            )
            return result.content

        refused = await _update(owner, "true")
        assert "Only admins" in refused, refused
        assert await _tiers(sessions) == _all_personal(agent, other)

        made = await _update(admin, "true")
        assert "company-managed" in made, made
        assert await _tiers(sessions) == _agent_company_managed(agent, other)

        back = await _update(admin, "false")
        assert "company-managed" in back, back
        assert await _tiers(sessions) == _all_personal(agent, other)
