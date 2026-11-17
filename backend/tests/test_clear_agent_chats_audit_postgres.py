"""Clearing an agent's chats answers 204 and leaves an audit row.

``DELETE /api/v1/agents/{id}/chats`` called ``log_audit`` without the required
``summary`` and with a string ``details``: the conversations were deleted and
committed, then the audit call raised and the client got a 500. The audit row
now commits with the delete, in one transaction. The other
tests of the route stub ``log_audit`` out; this one runs the real route, the
real ``get_db`` and the real ``log_audit`` on PostgreSQL.

``integration``-marked and skipped when PostgreSQL is unreachable, like its
neighbours.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.deps import get_current_user
from app.api.v1 import agent_chats
from app.models.agent import Agent
from app.models.audit_log import AuditLog
from app.models.chat import Conversation
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_workspace_collection(monkeypatch):
    """The sandbox workspaces of the deleted conversations are not under test."""
    monkeypatch.setattr(
        "app.services.sandbox_workspace.gc.collect_conversations_soon",
        lambda *_args, **_kwargs: None,
    )


async def _seed(sessions) -> tuple[User, Agent]:
    """An owner and their agent with two conversations."""
    async with sessions() as db:
        owner = User(email="owner@example.com", full_name="Owner", role="user")
        db.add(owner)
        await db.flush()
        agent = Agent(user_id=owner.id, name="Mail Desk")
        db.add(agent)
        await db.flush()
        db.add_all(
            Conversation(user_id=owner.id, agent_id=agent.id, channel="web") for _ in range(2)
        )
        await db.commit()
    return owner, agent


async def _clear(owner: User, agent: Agent) -> httpx.Response:
    app = FastAPI()
    app.include_router(agent_chats.router, prefix="/api/v1/agents")
    app.dependency_overrides[get_current_user] = lambda: owner
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.delete(f"/api/v1/agents/{agent.id}/chats")


async def _state(sessions, agent: Agent) -> tuple[int, list[AuditLog]]:
    """The agent's conversations left, and every audit row."""
    async with sessions() as db:
        remaining = await db.scalar(
            select(func.count()).select_from(Conversation).where(Conversation.agent_id == agent.id)
        )
        return remaining, list((await db.scalars(select(AuditLog))).all())


async def test_clearing_an_agents_chats_answers_204_and_writes_an_audit_row():
    async with scratch_database(
        "clear_agent_chats", tables=ALL_TABLES, extensions=("vector",)
    ) as scratch:
        owner, agent = await _seed(scratch.sessions)

        response = await _clear(owner, agent)

        assert response.status_code == 204, response.text
        remaining, (entry,) = await _state(scratch.sessions, agent)

    assert remaining == 0
    assert (entry.user_id, entry.action, entry.entity_type, entry.entity_id) == (
        owner.id,
        "delete",
        "conversation",
        agent.id,
    )
    assert entry.summary == "Cleared 2 conversation(s) of agent 'Mail Desk'"
    assert entry.details == {"deleted_count": 2}
    assert entry.ip_address == "127.0.0.1"


async def test_a_failed_audit_write_keeps_the_chats(monkeypatch):
    """The delete and its audit row are one transaction: neither lands alone."""

    async def _fail(*_args, **_kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(agent_chats, "log_audit", _fail)
    async with scratch_database(
        "clear_agent_chats", tables=ALL_TABLES, extensions=("vector",)
    ) as scratch:
        owner, agent = await _seed(scratch.sessions)

        response = await _clear(owner, agent)

        assert response.status_code == 500
        assert await _state(scratch.sessions, agent) == (2, [])
