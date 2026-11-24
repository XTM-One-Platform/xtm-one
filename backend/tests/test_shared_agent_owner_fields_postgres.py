"""What a shared agent shows the people it is shared with, and what losing it takes away.

Against PostgreSQL and the agents REST route, as the grantees themselves:

- the public widget token (``web_token``) runs the agent as its owner, so only
  someone who may manage the agent reads it, on the detail and on the list;
- an agent shared with people and no group still names, to whoever may edit
  it, the owner's personal integrations those people would use through it;
- when an agent stops reaching someone (a grant removed, company-managed made
  private, through the REST route or the agent-builder tool), that person's
  agents stop naming it as a handover target or a work-item owner, while
  agents of people who still reach it keep it.

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
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import agents as agents_api
from app.core.database import get_db
from app.models.agent import Agent
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.user import User
from app.services.builtin_tools import build_agent_builder_tool
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "agentownerfields", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
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


async def _group(session, name: str, *members: User) -> Group:
    group = Group(id=uuid.uuid4(), name=name)
    session.add(group)
    await session.flush()
    for member in members:
        session.add(GroupMember(group_id=group.id, user_id=member.id))
    await session.flush()
    return group


def _agent(owner: User | None, name: str, **fields) -> Agent:
    return Agent(
        id=uuid.uuid4(),
        user_id=owner.id if owner else None,
        name=name,
        slug=f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}",
        persona=f"You are {name}.",
        enabled=True,
        **fields,
    )


def _pointing_at(owner: User | None, name: str, target: Agent, **fields) -> Agent:
    """An agent of *owner* that hands over and files work items to *target*."""
    target_id = str(target.id)
    return _agent(
        owner,
        name,
        handover_agent_ids=[target_id],
        handover_types={target_id: "transfer"},
        handover_conditions={target_id: "When the question is about onboarding."},
        work_item_owner_agent_ids=[target_id],
        **fields,
    )


async def _references(sessions, agent_id: uuid.UUID, target_id: uuid.UUID) -> dict[str, bool]:
    """Which of *agent_id*'s fields still name *target_id*."""
    async with sessions() as session:
        row = await session.get(Agent, agent_id)
        target = str(target_id)
        return {
            "handover_agent_ids": target in (row.handover_agent_ids or []),
            "handover_types": target in (row.handover_types or {}),
            "handover_conditions": target in (row.handover_conditions or {}),
            "work_item_owner_agent_ids": target in (row.work_item_owner_agent_ids or []),
        }


ALL_KEPT = {
    "handover_agent_ids": True,
    "handover_types": True,
    "handover_conditions": True,
    "work_item_owner_agent_ids": True,
}
ALL_GONE = dict.fromkeys(ALL_KEPT, False)


async def test_only_a_manager_reads_the_web_token_of_a_shared_agent() -> None:
    async with _database() as sessions:
        async with sessions() as db:
            sam = await _user(db, "Sam")
            reader = await _user(db, "Reader")
            editor = await _user(db, "Editor")
            agent = _agent(
                sam,
                "Onboarding",
                web_enabled=True,
                web_token="public-widget-token",
                shared_user_ids=[str(reader.id), str(editor.id)],
                shared_user_permissions={str(reader.id): "read_only", str(editor.id): "config"},
            )
            db.add(agent)
            await db.commit()
            agent_id = str(agent.id)

        async with _client(sessions, reader) as client:
            opened = await client.get(f"/api/v1/agents/{agent_id}")
            assert opened.status_code == 200, opened.text
            assert opened.json()["can_manage"] is False
            assert opened.json()["web_enabled"] is True
            assert opened.json()["web_token"] is None
            listed = await client.get("/api/v1/agents")
            assert listed.status_code == 200, listed.text
            row = next(a for a in listed.json() if a["id"] == agent_id)
            assert row["web_token"] is None

        for manager in (editor, sam):
            async with _client(sessions, manager) as client:
                opened = await client.get(f"/api/v1/agents/{agent_id}")
                assert opened.status_code == 200, opened.text
                assert opened.json()["can_manage"] is True
                assert opened.json()["web_token"] == "public-widget-token"


async def test_an_agent_shared_with_people_only_names_the_owners_personal_integrations() -> None:
    async with _database() as sessions:
        async with sessions() as db:
            sam = await _user(db, "Sam")
            editor = await _user(db, "Editor")
            hubspot = Integration(
                id=uuid.uuid4(), user_id=sam.id, name="Sam's HubSpot", provider="hubspot", is_company_managed=False
            )
            # Already the editor's own through a direct share: left out.
            drata = Integration(
                id=uuid.uuid4(),
                user_id=sam.id,
                name="Sam's Drata",
                provider="drata",
                is_company_managed=False,
                shared_user_ids=[str(editor.id)],
                shared_user_permissions={str(editor.id): "read_only"},
            )
            db.add_all([hubspot, drata])
            await db.flush()
            agent = _agent(
                sam,
                "Sales desk",
                integration_bindings={"hubspot": [str(hubspot.id)], "drata": [str(drata.id)]},
                shared_user_ids=[str(editor.id)],
                shared_user_permissions={str(editor.id): "config"},
            )
            db.add(agent)
            await db.commit()
            agent_id, hubspot_id = str(agent.id), str(hubspot.id)

        async with _client(sessions, editor) as client:
            opened = await client.get(f"/api/v1/agents/{agent_id}")
            assert opened.status_code == 200, opened.text
            assert opened.json()["shared_personal_integrations"] == [
                {"id": hubspot_id, "name": "Sam's HubSpot", "provider": "hubspot"}
            ]


async def test_removing_a_grant_unlinks_the_agents_of_whoever_lost_it() -> None:
    async with _database() as sessions:
        async with sessions() as db:
            sam = await _user(db, "Sam")
            alice = await _user(db, "Alice")
            marie = await _user(db, "Marie")
            admin = await _user(db, "Admin", admin=True)
            soc = await _group(db, "SOC", marie)
            target = _agent(
                sam,
                "Onboarding",
                shared_group_ids=[str(soc.id)],
                shared_group_permissions={str(soc.id): "read_only"},
                shared_user_ids=[str(alice.id)],
                shared_user_permissions={str(alice.id): "read_only"},
            )
            db.add(target)
            await db.flush()
            other = _agent(sam, "Other")
            db.add(other)
            await db.flush()
            alices = _pointing_at(alice, "Alice desk", target)
            # Alice's agent also names another agent she keeps.
            alices.handover_agent_ids = [str(target.id), str(other.id)]
            alices.handover_types = {str(target.id): "transfer", str(other.id): "delegate"}
            other.shared_user_ids = [str(alice.id)]
            other.shared_user_permissions = {str(alice.id): "read_only"}
            maries = _pointing_at(marie, "Marie desk", target)
            sams = _pointing_at(sam, "Sam desk", target)
            admins = _pointing_at(admin, "Admin desk", target)
            db.add_all([alices, maries, sams, admins])
            await db.commit()
            target_id = target.id
            ids = {"alice": alices.id, "marie": maries.id, "sam": sams.id, "admin": admins.id}
            other_id = str(other.id)

        # Sam takes Alice's grant away: only Alice's agent is unlinked.
        async with _client(sessions, sam) as client:
            removed = await client.put(f"/api/v1/agents/{target_id}", json={"shared_user_ids": None})
            assert removed.status_code == 200, removed.text
        assert await _references(sessions, ids["alice"], target_id) == ALL_GONE
        async with sessions() as session:
            row = await session.get(Agent, ids["alice"])
            assert row.handover_agent_ids == [other_id]
            assert row.handover_types == {other_id: "delegate"}
        for kept in ("marie", "sam", "admin"):
            assert await _references(sessions, ids[kept], target_id) == ALL_KEPT, kept

        # Then the group's: Marie's agent follows; the owner's and an
        # administrator's (who reaches every agent) keep it.
        async with _client(sessions, sam) as client:
            removed = await client.put(f"/api/v1/agents/{target_id}", json={"shared_group_ids": None})
            assert removed.status_code == 200, removed.text
        assert await _references(sessions, ids["marie"], target_id) == ALL_GONE
        for kept in ("sam", "admin"):
            assert await _references(sessions, ids[kept], target_id) == ALL_KEPT, kept


async def test_the_agent_builder_removing_a_person_unlinks_their_agents() -> None:
    async with _database() as sessions:
        async with sessions() as db:
            sam = await _user(db, "Sam")
            alice = await _user(db, "Alice")
            target = _agent(
                sam,
                "Onboarding",
                shared_user_ids=[str(alice.id)],
                shared_user_permissions={str(alice.id): "read_only"},
            )
            db.add(target)
            await db.flush()
            alices = _pointing_at(alice, "Alice desk", target)
            db.add(alices)
            await db.commit()
            target_id, alices_id = target.id, alices.id

        answer = await _tool(
            sam, "update_agent", agent_id=str(target_id), users=[str(alice.id)], binding_mode="remove"
        )
        assert "updated: users" in answer, answer
        assert await _references(sessions, alices_id, target_id) == ALL_GONE


async def test_making_a_company_managed_agent_private_unlinks_everyone_else() -> None:
    async with _database() as sessions:
        async with sessions() as db:
            admin = await _user(db, "Admin", admin=True)
            marie = await _user(db, "Marie")
            target = _agent(admin, "Onboarding", is_company_managed=True)
            db.add(target)
            await db.flush()
            maries = _pointing_at(marie, "Marie desk", target)
            # A company-managed agent hands over to company-managed agents only.
            company = _pointing_at(admin, "Company desk", target, is_company_managed=True)
            admins = _pointing_at(admin, "Admin desk", target)
            db.add_all([maries, company, admins])
            await db.commit()
            target_id = target.id
            maries_id, company_id, admins_id = maries.id, company.id, admins.id

        async with _client(sessions, admin) as client:
            made_private = await client.put(f"/api/v1/agents/{target_id}", json={"is_company_managed": False})
            assert made_private.status_code == 200, made_private.text
        assert await _references(sessions, maries_id, target_id) == ALL_GONE
        assert await _references(sessions, company_id, target_id) == {
            "handover_agent_ids": False,
            "handover_types": False,
            "handover_conditions": False,
            # The roster follows who the agent's owner reaches: an admin.
            "work_item_owner_agent_ids": True,
        }
        assert await _references(sessions, admins_id, target_id) == ALL_KEPT
