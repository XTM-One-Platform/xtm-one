"""An assistant shares an agent with named people, against PostgreSQL and the REST route.

The acceptance path of per-user agent sharing from chat: the owner's assistant
shares one agent with three people outside any group by email, read-only,
through ``update_agent users=...``; each of them sees the agent (``GET
/agents/{id}``, the chat gate ``may_chat_with``) and cannot edit it; a
read-only grantee's own assistant cannot pass the agent on; an address no
account answers to changes nothing; ``get_agent`` names the people and the
groups with their member count; ``list_groups`` names groups to everyone and
their members to an admin's agent; and a ``remove`` takes the access away at
once - that person's ``GET`` is a 404 and ``may_chat_with`` says no.

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
from app.models.audit_log import AuditLog
from app.models.group import Group, GroupMember
from app.models.user import User
from app.services.builtin_tools import build_agent_builder_tool
from app.services.general_assistant_link import may_chat_with
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "agentshare", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
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
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _tool(caller: User, tool: str, /, **arguments) -> str:
    _, executor = build_agent_builder_tool(None, str(caller.id))
    result = await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=tool, arguments=arguments))
    return result.content


async def _user(session, name: str, email: str | None = None, *, admin: bool = False) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email or f"{name.lower().replace(' ', '.')}-{uuid.uuid4().hex[:6]}@example.com",
        full_name=name,
        hashed_password="x",
        role="admin" if admin else "user",
    )
    session.add(user)
    await session.flush()
    return user


async def _group(session, name: str, *members: User) -> Group:
    group = Group(id=uuid.uuid4(), name=name)
    session.add(group)
    await session.flush()
    for member in members:
        session.add(GroupMember(group_id=group.id, user_id=member.id))
    await session.flush()
    return group


async def _agent_row(sessions, agent_id) -> Agent:
    async with sessions() as session:
        return await session.get(Agent, agent_id)


async def _can_chat(sessions, agent_id, user: User) -> bool:
    async with sessions() as session:
        agent = await session.get(Agent, agent_id)
        return await may_chat_with(session, agent, user.id)


async def test_the_assistant_shares_an_agent_with_three_people_and_takes_one_back() -> None:
    async with _database() as sessions:
        async with sessions() as db:
            sam = await _user(db, "Samuel Hassine", "samuel@filigran.io")
            alice = await _user(db, "Alice Martin", "alice.martin@example.com")
            marie = await _user(db, "Marie Curie", "marie@filigran.io")
            alan = await _user(db, "Alan Turing", "alan@filigran.io")
            outsider = await _user(db, "Outsider", "outsider@filigran.io")
            admin = await _user(db, "Platform Admin", "admin@filigran.io", admin=True)
            soc = await _group(db, "SOC", marie, outsider)
            await _group(db, "Finance", alan)
            agent = Agent(
                id=uuid.uuid4(),
                user_id=sam.id,
                name="Barclays onboarding",
                slug="barclays-onboarding",
                persona="You help with the Barclays onboarding.",
                enabled=True,
            )
            db.add(agent)
            await db.commit()
            agent_id = agent.id
        url = f"/api/v1/agents/{agent_id}"

        # Nobody but the owner reaches it yet.
        assert await _can_chat(sessions, agent_id, alice) is False
        async with _client(sessions, alice) as client:
            assert (await client.get(url)).status_code == 404

        # One call, three people written as a person would: an email in the
        # wrong case, a bracketed address, a user id.
        shared = await _tool(
            sam,
            "update_agent",
            agent_id=str(agent_id),
            users=["Alice.Martin@example.com", f"Marie <{marie.email}>", str(alan.id)],
            users_permission="read_only",
            binding_mode="add",
        )
        assert "updated: users" in shared, shared
        assert "Shared with Alice Martin <alice.martin@example.com>" in shared
        assert "Marie Curie <marie@filigran.io>" in shared
        assert "Alan Turing <alan@filigran.io>" in shared
        assert "[read_only]" in shared
        assert "Shared with people: 3" in shared

        stored = await _agent_row(sessions, agent_id)
        assert sorted(stored.shared_user_ids) == sorted(str(u.id) for u in (alice, marie, alan))
        assert stored.shared_user_permissions == {str(u.id): "read_only" for u in (alice, marie, alan)}
        async with sessions() as db:
            audits = (await db.execute(select(AuditLog).where(AuditLog.entity_id == agent_id))).scalars().all()
            sharing = [a for a in audits if (a.details or {}).get("sharing_changed")]
            assert len(sharing) == 1
            assert sharing[0].user_id == sam.id
            assert sharing[0].details["via"] == "agent"
            assert sharing[0].details["permission"] == "read_only"
            assert sorted(sharing[0].details["added_users"]) == sorted(str(u.id) for u in (alice, marie, alan))

        # Each of them can chat with it and open it read-only; nobody else can.
        for person in (alice, marie, alan):
            assert await _can_chat(sessions, agent_id, person) is True
            async with _client(sessions, person) as client:
                opened = await client.get(url)
                assert opened.status_code == 200, opened.text
                assert opened.json()["can_manage"] is False
                assert opened.json()["shared_user_permissions"][str(person.id)] == "read_only"
                edited = await client.put(url, json={"name": "Changed by a reader"})
                assert edited.status_code == 403, edited.text
        async with _client(sessions, outsider) as client:
            assert (await client.get(url)).status_code == 404

        # A read-only grantee's own assistant cannot pass the agent on.
        refused = await _tool(alice, "update_agent", agent_id=str(agent_id), users=[outsider.email], binding_mode="add")
        assert "read-only" in refused.lower() or "permission" in refused.lower(), refused
        assert await _can_chat(sessions, agent_id, outsider) is False

        # An address no account answers to changes nothing, and names itself.
        unknown = await _tool(
            sam,
            "update_agent",
            agent_id=str(agent_id),
            users=[alice.email, "partner@outside.example"],
            binding_mode="add",
        )
        assert '"partner@outside.example"' in unknown, unknown
        assert "Nothing was changed" in unknown
        assert sorted((await _agent_row(sessions, agent_id)).shared_user_ids) == sorted(
            str(u.id) for u in (alice, marie, alan)
        )

        # get_agent names the people, and the groups with their member count.
        grouped = await _tool(sam, "update_agent", agent_id=str(agent_id), shared_group_ids=str(soc.id))
        assert "updated: shared_group_ids" in grouped, grouped
        shown = await _tool(sam, "get_agent", agent_id=str(agent_id))
        assert f"Shared with groups: 1 — SOC (`{soc.id}`, 2 members) [read_only]" in shown, shown
        assert "Shared with people: 3 —" in shown
        assert f"Alice Martin <alice.martin@example.com> (`{alice.id}`) [read_only]" in shown
        assert "Shared group IDs" not in shown

        # A grantee's access can be raised without re-sending the list.
        raised = await _tool(
            sam,
            "update_agent",
            agent_id=str(agent_id),
            users=marie.email,
            users_permission="config",
            binding_mode="add",
        )
        assert "Now config: Marie Curie" in raised, raised
        async with _client(sessions, marie) as client:
            assert (await client.get(url)).json()["can_manage"] is True

        # remove takes it away at once: Lucas can no longer chat with it nor
        # open it, the others keep their access.
        removed = await _tool(
            sam, "update_agent", agent_id=str(agent_id), users="alice.martin@example.com", binding_mode="remove"
        )
        assert "Access removed, they can no longer use it: Alice Martin" in removed, removed
        assert await _can_chat(sessions, agent_id, alice) is False
        async with _client(sessions, alice) as client:
            assert (await client.get(url)).status_code == 404
        assert await _can_chat(sessions, agent_id, alan) is True
        stored = await _agent_row(sessions, agent_id)
        assert sorted(stored.shared_user_ids) == sorted(str(u.id) for u in (marie, alan))
        assert str(alice.id) not in stored.shared_user_permissions

        # Removing someone not shared with changes nothing and says so.
        nothing = await _tool(sam, "update_agent", agent_id=str(agent_id), users=[alice.email], binding_mode="remove")
        assert nothing.startswith("Nothing changed on agent"), nothing
        assert "nothing to remove: Alice Martin" in nothing

        # The owner is never granted to their own agent.
        owner_named = await _tool(
            sam, "update_agent", agent_id=str(agent_id), users=[sam.email, alice.email], binding_mode="add"
        )
        assert "Skipped, they own it: Samuel Hassine" in owner_named, owner_named
        assert str(sam.id) not in (await _agent_row(sessions, agent_id)).shared_user_ids

        # 'clear' in replace mode drops every per-user share; the group stays.
        cleared = await _tool(sam, "update_agent", agent_id=str(agent_id), users="clear")
        assert "updated: users" in cleared, cleared
        stored = await _agent_row(sessions, agent_id)
        assert stored.shared_user_ids is None
        assert stored.shared_user_permissions is None
        assert stored.shared_group_ids == [str(soc.id)]
        assert await _can_chat(sessions, agent_id, marie) is True, "Marie is still reached through the SOC group"
        assert await _can_chat(sessions, agent_id, alan) is False

        # list_groups: names and member counts for everyone, members for an admin's agent.
        listed = await _tool(sam, "list_groups")
        assert f"**SOC** (`{soc.id}`) — 2 members" in listed, listed
        assert "**Finance**" in listed
        assert "Marie Curie" not in listed
        with_members = await _tool(sam, "list_groups", members="true")
        assert "Members are listed for administrators only" in with_members
        assert "Marie Curie" not in with_members
        admin_view = await _tool(admin, "list_groups", search="soc", members="true")
        assert "**SOC**" in admin_view and "**Finance**" not in admin_view, admin_view
        assert "Members: Marie Curie <marie@filigran.io>, Outsider <outsider@filigran.io>" in admin_view
        member_view = await _tool(alan, "list_groups", search="finance")
        assert "you are a member" in member_view, member_view

        # A company-managed agent is open to everyone already: not shared this way.
        async with sessions() as db:
            db.add(
                Agent(
                    id=uuid.uuid4(),
                    user_id=None,
                    name="Company helper",
                    slug="company-helper",
                    persona="x",
                    enabled=True,
                    is_company_managed=True,
                )
            )
            await db.commit()
        async with sessions() as db:
            company = (await db.execute(select(Agent).where(Agent.slug == "company-helper"))).scalar_one()
        company_refused = await _tool(
            admin, "update_agent", agent_id=str(company.id), users=[alice.email], binding_mode="add"
        )
        assert "company-managed" in company_refused, company_refused
        assert "Nothing was changed" in company_refused
        assert (await _agent_row(sessions, company.id)).shared_user_ids is None


async def test_create_agent_shares_the_new_agent_in_the_same_call() -> None:
    async with _database() as sessions:
        async with sessions() as db:
            sam = await _user(db, "Samuel Hassine", "samuel@filigran.io")
            alice = await _user(db, "Alice Martin", "alice.martin@example.com")
            await db.commit()

        created = await _tool(
            sam,
            "create_agent",
            name="Barclays onboarding",
            persona="You help with the Barclays onboarding.",
            users=[alice.email],
            users_permission="read_only",
        )
        assert "created successfully" in created, created
        assert "users" in created
        async with sessions() as db:
            agent = (await db.execute(select(Agent).where(Agent.name == "Barclays onboarding"))).scalar_one()
            assert agent.shared_user_ids == [str(alice.id)]
            assert agent.shared_user_permissions == {str(alice.id): "read_only"}
        assert await _can_chat(sessions, agent.id, alice) is True

        # A refused address creates nothing.
        refused = await _tool(sam, "create_agent", name="Half shared", persona="x", users=["nobody@outside.example"])
        assert refused.startswith("Agent not created:"), refused
        async with sessions() as db:
            assert (await db.execute(select(Agent).where(Agent.name == "Half shared"))).scalar_one_or_none() is None
