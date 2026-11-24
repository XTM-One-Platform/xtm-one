"""An agent's trace supervision allow-list names only agents whose chats its writer may read.

A target's traces hold every user's conversations with it, so naming one is
reading them: agent create and update refuse a target the writer may not read
the chats of (``binding_access.assert_trace_supervision_additions_allowed``),
and a duplicate leaves such a target out. The real REST handlers run against
PostgreSQL. Every writer is a NON-admin unless named otherwise.
Same scratch-database recipe as ``test_trigger_integration_writes_postgres.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api.v1 import agents as agents_api
from app.api.v1.schemas.agent import CreateAgentRequest, UpdateAgentRequest
from app.models.agent import Agent
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

REFUSED_DETAIL = "One or more trace supervision target agents were not found, or you may not read their conversations."


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "tracesup",
        tables=ALL_TABLES,
        extensions=("vector",),
        bind_app_sessions=False,
    ) as db:
        yield db.sessions


@dataclass
class _World:
    owner: uuid.UUID
    member: uuid.UUID
    admin: uuid.UUID
    shared_agent: uuid.UUID
    ids: dict[str, str]


def _user(name: str, *, role: str = "user") -> User:
    return User(
        email=f"{name}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=name.title(),
        hashed_password="x",
        role=role,
    )


async def _seed(sessions) -> _World:
    async with sessions() as session:
        owner, member, admin = _user("owner"), _user("member"), _user("admin", role="admin")
        session.add_all([owner, member, admin])
        await session.flush()

        def _shared(name: str, tier: str) -> Agent:
            return Agent(
                name=name,
                user_id=owner.id,
                shared_user_ids=[str(member.id)],
                shared_user_permissions={str(member.id): tier},
            )

        rows = {
            # Every target below is one the member can see.
            "own": Agent(name="Member's own", user_id=member.id),
            "with_chats": _shared("Chats shared", "config_with_chats"),
            "config": _shared("Config shared", "config"),
            "read_only": _shared("Read-only shared", "read_only"),
            "company": Agent(name="Company desk", user_id=owner.id, is_company_managed=True),
        }
        session.add_all(rows.values())
        await session.flush()
        ids = {key: str(row.id) for key, row in rows.items()}
        shared = Agent(
            name="Supervisor",
            user_id=owner.id,
            builtin_tool_ids=["inspect_agent_traces"],
            shared_user_ids=[str(member.id)],
            shared_user_permissions={str(member.id): "config"},
            trace_supervision_target_agent_ids=[ids["with_chats"], ids["company"]],
        )
        session.add(shared)
        await session.commit()
        return _World(owner.id, member.id, admin.id, shared.id, ids)


async def _create(sessions, user_id, targets):
    async with sessions() as session:
        user = await session.get(User, user_id)
        response = await agents_api.create_agent(
            body=CreateAgentRequest(name=f"Agent {uuid.uuid4().hex[:6]}", trace_supervision_target_agent_ids=targets),
            request=None,
            user=user,
            db=session,
            _agentic_gate=None,
        )
        await session.commit()
    async with sessions() as session:
        return await session.get(Agent, uuid.UUID(response.id))


async def _update(sessions, user_id, agent_id, targets):
    async with sessions() as session:
        user = await session.get(User, user_id)
        await agents_api.update_agent(
            agent_id=str(agent_id),
            body=UpdateAgentRequest(trace_supervision_target_agent_ids=targets),
            request=None,
            user=user,
            db=session,
            _ai_gate=None,
        )
        await session.commit()
    async with sessions() as session:
        return await session.get(Agent, agent_id)


async def _refused(call) -> None:
    with pytest.raises(HTTPException) as exc:
        await call
    assert exc.value.status_code == 400
    assert exc.value.detail == REFUSED_DETAIL


@pytest.mark.asyncio
async def test_a_create_names_only_targets_whose_chats_the_writer_may_read():
    async with _temp_database() as sessions:
        w = await _seed(sessions)
        for key in ("config", "read_only", "company"):
            await _refused(_create(sessions, w.member, [w.ids[key]]))
        await _refused(_create(sessions, w.member, [str(uuid.uuid4())]))
        await _refused(_create(sessions, w.member, ["not-a-uuid"]))
        created = await _create(sessions, w.member, [w.ids["own"], w.ids["with_chats"]])
        admin_created = await _create(sessions, w.admin, [w.ids["company"], w.ids["read_only"]])

    assert created.trace_supervision_target_agent_ids == [w.ids["own"], w.ids["with_chats"]]
    assert admin_created.trace_supervision_target_agent_ids == [w.ids["company"], w.ids["read_only"]]


@pytest.mark.asyncio
async def test_an_update_checks_only_the_targets_it_adds():
    async with _temp_database() as sessions:
        w = await _seed(sessions)
        stored = [w.ids["with_chats"], w.ids["company"]]
        # Re-saving the stored list (a target the member may not read included) saves.
        resaved = await _update(sessions, w.member, w.shared_agent, [s.upper() for s in stored])
        await _refused(_update(sessions, w.member, w.shared_agent, [*stored, w.ids["config"]]))
        extended = await _update(sessions, w.member, w.shared_agent, [*stored, w.ids["own"]])

    assert resaved.trace_supervision_target_agent_ids == [s.upper() for s in stored]
    assert extended.trace_supervision_target_agent_ids == [*stored, w.ids["own"]]


@pytest.mark.asyncio
async def test_a_member_s_copy_leaves_out_targets_whose_chats_they_may_not_read():
    async with _temp_database() as sessions:
        w = await _seed(sessions)
        async with sessions() as session:
            user = await session.get(User, w.member)
            response = await agents_api.duplicate_agent(
                agent_id=str(w.shared_agent),
                request=None,
                copy_memory=False,
                user=user,
                db=session,
                _agentic_gate=None,
            )
            await session.commit()
        async with sessions() as session:
            copy = await session.get(Agent, uuid.UUID(response.id))

    assert copy.trace_supervision_target_agent_ids == [w.ids["with_chats"]]
    assert (
        "1 trace supervision target agent of the original agent was not copied: you cannot use it." in response.warnings
    )
