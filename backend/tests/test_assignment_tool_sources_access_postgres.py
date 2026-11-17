"""Only servers the writer may use can be added to an assignment's MCP scope.

``load_mcp_tools`` loads every server id of ``tool_sources`` with no visibility
check, so the rule lives where the list is written: an entry a write ADDS must
be one of the agent's own servers or a server the writer may use (the rule the
agent's own bindings follow), while entries already stored are left alone. The
three-tier predicate short-circuits for admins and reads the per-user sharing
columns otherwise, which a mock session cannot show: the real REST handlers
and the agent duplicate run here against PostgreSQL, as a NON-ADMIN writer
first. Same scratch-database recipe as
``test_assignment_stale_reference_postgres.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api.v1 import agents as agents_api
from app.api.v1 import assignments as assignments_api
from app.api.v1.schemas.assignment import (
    CreateAssignmentRequest,
    UpdateAssignmentRequest,
)
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.group import Group, GroupMember
from app.models.mcp_server import MCPServer
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_NO_SERVER = "00000000-0000-0000-0000-000000000000"


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "tsauthz",
        tables=ALL_TABLES,
        extensions=("vector",),
        bind_app_sessions=False,
    ) as db:
        yield db.sessions


@dataclass
class _World:
    writer: uuid.UUID
    admin: uuid.UUID
    writer_agent: uuid.UUID
    admin_agent: uuid.UUID
    company_agent: uuid.UUID
    servers: dict[str, str]


def _user(name: str, *, role: str = "user") -> User:
    return User(
        email=f"{name}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=name.title(),
        hashed_password="x",
        role=role,
    )


async def _seed(sessions) -> _World:
    """A writer (in a group), an admin, a server of every visibility tier, and
    one agent each: the writer's (bound to a server the writer cannot see), the
    admin's, and a company-managed one."""
    async with sessions() as session:
        owner, writer, admin = _user("owner"), _user("writer"), _user("admin", role="admin")
        session.add_all([owner, writer, admin])
        await session.flush()
        group = Group(name=f"analysts-{uuid.uuid4().hex[:8]}")
        session.add(group)
        await session.flush()
        session.add(GroupMember(group_id=group.id, user_id=writer.id))

        def _server(name: str, **visibility) -> MCPServer:
            server = MCPServer(
                name=name,
                transport="streamable_http",
                url="https://example.invalid/mcp",
                **visibility,
            )
            session.add(server)
            return server

        servers = {
            "own": _server("Own", user_id=writer.id),
            "company": _server("Company", user_id=None, is_company_managed=True),
            "group": _server(
                "Group shared", user_id=owner.id, shared_group_ids=[str(group.id)]
            ),
            "user": _server(
                "User shared", user_id=owner.id, shared_user_ids=[str(writer.id)]
            ),
            "private": _server("Private", user_id=owner.id),
            "restricted": _server(
                "Restricted",
                user_id=None,
                is_company_managed=True,
                access_restricted=True,
            ),
            # Bound to the writer's agent although the writer cannot see it
            # (it was shared once, then unshared): the agent runs with it.
            "bound": _server("Bound", user_id=owner.id),
            "admin_own": _server("Admin own", user_id=admin.id),
        }
        await session.flush()
        ids = {key: str(s.id) for key, s in servers.items()}

        writer_agent = Agent(
            name="Writer agent", user_id=writer.id, mcp_server_ids=[ids["bound"]]
        )
        admin_agent = Agent(name="Admin agent", user_id=admin.id)
        company_agent = Agent(
            name="Company agent",
            user_id=None,
            is_company_managed=True,
            mcp_server_ids=[ids["company"]],
        )
        session.add_all([writer_agent, admin_agent, company_agent])
        await session.commit()
        return _World(
            writer=writer.id,
            admin=admin.id,
            writer_agent=writer_agent.id,
            admin_agent=admin_agent.id,
            company_agent=company_agent.id,
            servers=ids,
        )


async def _create(sessions, user_id, agent_id, tool_sources) -> list[str] | None:
    async with sessions() as session:
        user = await session.get(User, user_id)
        response = await assignments_api.create_assignment(
            body=CreateAssignmentRequest(
                agent_id=str(agent_id),
                name="Scoped",
                trigger_type="manual",
                prompt="Work",
                tool_sources=tool_sources,
            ),
            request=None,
            user=user,
            db=session,
            _agentic_gate=None,
        )
        await session.commit()
        return response.tool_sources


async def _refused(sessions, user_id, agent_id, tool_sources) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        await _create(sessions, user_id, agent_id, tool_sources)
    assert exc.value.status_code == 400
    return exc.value


async def _update(sessions, user_id, assignment_id, **fields) -> list[str] | None:
    async with sessions() as session:
        user = await session.get(User, user_id)
        response = await assignments_api.update_assignment(
            assignment_id=str(assignment_id),
            body=UpdateAssignmentRequest(**fields),
            request=None,
            user=user,
            db=session,
            _ai_gate=None,
        )
        return response.tool_sources


@pytest.mark.asyncio
async def test_a_non_admin_cannot_add_a_server_they_cannot_see():
    async with _temp_database() as sessions:
        world = await _seed(sessions)
        ids = world.servers

        private = await _refused(sessions, world.writer, world.writer_agent, [ids["private"]])
        missing = await _refused(
            sessions, world.writer, world.writer_agent, [str(uuid.uuid4())]
        )
        await _refused(sessions, world.writer, world.writer_agent, [ids["restricted"]])
        await _refused(
            sessions, world.writer, world.writer_agent, [ids["own"], ids["private"]]
        )

        # Kept: own, company-managed, group-shared, user-shared, and the
        # agent's own server the writer cannot see.
        kept = [ids["own"], ids["company"], ids["group"], ids["user"], ids["bound"]]
        assert await _create(sessions, world.writer, world.writer_agent, kept) == kept
        async with sessions() as session:
            rows = await session.execute(select(Assignment.tool_sources))
            assert rows.scalars().all() == [kept]

    # The refusal never says whether the server exists.
    assert private.detail == missing.detail
    assert ids["private"] not in private.detail
    assert "Private" not in private.detail


@pytest.mark.asyncio
async def test_a_row_holding_a_stale_id_still_saves():
    async with _temp_database() as sessions:
        world = await _seed(sessions)
        ids = world.servers
        async with sessions() as session:
            stale = Assignment(
                agent_id=world.writer_agent,
                user_id=world.writer,
                name="Legacy",
                trigger_type="manual",
                prompt="Work",
                tool_sources=[ids["private"], ids["own"]],
            )
            session.add(stale)
            await session.commit()

        # Unrelated edit, the stored list sent back as is, and an addition of a
        # server the writer may use: all save, the stale entry included.
        assert await _update(sessions, world.writer, stale.id, name="Renamed") == [
            ids["private"],
            ids["own"],
        ]
        assert await _update(
            sessions, world.writer, stale.id, tool_sources=[ids["private"].upper(), ids["own"]]
        ) == [ids["private"].upper(), ids["own"]]
        assert await _update(
            sessions,
            world.writer,
            stale.id,
            tool_sources=[ids["private"], ids["own"], ids["company"]],
        ) == [ids["private"], ids["own"], ids["company"]]
        assert await _update(sessions, world.writer, stale.id, tool_sources=[ids["own"]]) == [
            ids["own"]
        ]

        # Once removed, the stale server is an addition like any other.
        with pytest.raises(HTTPException) as exc:
            await _update(
                sessions, world.writer, stale.id, tool_sources=[ids["own"], ids["private"]]
            )
        assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_admins_follow_the_agent_binding_rules():
    """A personal agent's bindings are unchecked for admins, a company-managed
    agent's must be company-managed: the scope follows the same rules."""
    async with _temp_database() as sessions:
        world = await _seed(sessions)
        ids = world.servers

        assert await _create(sessions, world.admin, world.admin_agent, [ids["private"]]) == [
            ids["private"]
        ]
        await _refused(sessions, world.admin, world.company_agent, [ids["admin_own"]])
        assert await _create(
            sessions, world.admin, world.company_agent, [ids["company"]]
        ) == [ids["company"]]


@pytest.mark.asyncio
async def test_a_duplicate_never_carries_a_server_the_copier_cannot_use():
    async with _temp_database() as sessions:
        world = await _seed(sessions)
        ids = world.servers
        async with sessions() as session:
            session.add_all(
                [
                    Assignment(
                        agent_id=world.company_agent,
                        name="Mixed",
                        trigger_type="manual",
                        prompt="Work",
                        is_company_managed=True,
                        tool_sources=[ids["company"], ids["private"]],
                    ),
                    Assignment(
                        agent_id=world.company_agent,
                        name="Foreign only",
                        trigger_type="manual",
                        prompt="Work",
                        is_company_managed=True,
                        tool_sources=[ids["private"]],
                    ),
                ]
            )
            await session.commit()

        async with sessions() as session:
            writer = await session.get(User, world.writer)
            copy = await agents_api.duplicate_agent(
                agent_id=str(world.company_agent),
                request=None,
                user=writer,
                db=session,
                _agentic_gate=None,
            )
        async with sessions() as session:
            rows = await session.execute(
                select(Assignment).where(Assignment.agent_id == uuid.UUID(copy.id))
            )
            scopes = {wf.name: wf.tool_sources for wf in rows.scalars().all()}

    assert scopes == {"Mixed": [ids["company"]], "Foreign only": [_NO_SERVER]}
    # The copier is told, by count only.
    assert copy.warnings == [
        "2 assignment MCP servers of the original agent were not copied: you cannot use them."
    ]
