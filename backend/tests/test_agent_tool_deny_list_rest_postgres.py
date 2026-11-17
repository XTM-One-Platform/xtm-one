"""The agent REST routes store ``tool_deny_list``, against PostgreSQL.

``POST /api/v1/agents`` and ``PUT /api/v1/agents/{id}`` validated the deny list
and answered 201/200, then never wrote it: an agent kept away from write tools
through the API kept them. These tests read the stored row after each call, on
the real routes, for every caller the three-tier rule names: the owner, a
non-admin ``config`` group grantee, a ``read_only`` grantee, a non-admin on a
company-managed agent, and anyone on a built-in agent in XTM One mode.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours; the temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import PropertyMock, patch

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import agents as agents_api
from app.core.database import get_db
from app.models.agent import Agent
from app.models.audit_log import AuditLog
from app.models.group import Group, GroupMember
from app.models.user import User
from app.services.builtin_tools.scheduling import (
    MAX_TOOL_DENY_LIST_ENTRIES,
    MAX_TOOL_DENY_LIST_NAME_LENGTH,
)
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "agentdeny", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
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
    app.dependency_overrides[deps.require_agentic_ai] = lambda: None
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _user(session, name: str, *, admin: bool = False) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{name.lower().replace(' ', '.')}-{uuid.uuid4().hex[:6]}@example.com",
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


async def _agent(session, owner: User, **columns) -> uuid.UUID:
    agent = Agent(
        id=uuid.uuid4(),
        user_id=owner.id,
        name=f"Investigator {uuid.uuid4().hex[:6]}",
        slug=f"investigator-{uuid.uuid4().hex[:8]}",
        persona="You investigate.",
        enabled=True,
        **columns,
    )
    session.add(agent)
    await session.flush()
    return agent.id


async def _stored_deny_list(sessions, agent_id) -> list | None:
    async with sessions() as session:
        return (await session.get(Agent, agent_id)).tool_deny_list


async def _audit_details(sessions, agent_id, action: str) -> list[dict]:
    async with sessions() as session:
        rows = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.entity_id == agent_id, AuditLog.action == action)
                .order_by(AuditLog.created_at)
            )
        ).scalars().all()
        return [row.details for row in rows]


async def test_create_stores_the_normalized_deny_list_and_audits_it() -> None:
    async with _database() as sessions:
        async with sessions() as db:
            owner = await _user(db, "Owner")
            await db.commit()

        async with _client(sessions, owner) as client:
            response = await client.post(
                "/api/v1/agents",
                json={
                    "name": "FIMI investigator",
                    "tool_deny_list": [" delete_note ", "delete_note", "", "  ", "send_email"],
                },
            )
            empty = await client.post(
                "/api/v1/agents", json={"name": "No denials", "tool_deny_list": []}
            )
            omitted = await client.post("/api/v1/agents", json={"name": "Nothing sent"})

        assert response.status_code == 201, response.text
        agent_id = uuid.UUID(response.json()["id"])
        assert response.json()["tool_deny_list"] == ["delete_note", "send_email"]
        assert await _stored_deny_list(sessions, agent_id) == ["delete_note", "send_email"]
        details = await _audit_details(sessions, agent_id, "create")
        assert [d["tool_deny_list"] for d in details] == [["delete_note", "send_email"]]

        assert empty.status_code == 201, empty.text
        assert await _stored_deny_list(sessions, uuid.UUID(empty.json()["id"])) is None
        assert omitted.status_code == 201, omitted.text
        assert await _stored_deny_list(sessions, uuid.UUID(omitted.json()["id"])) is None


async def test_owner_update_sets_keeps_and_clears_the_deny_list() -> None:
    async with _database() as sessions:
        async with sessions() as db:
            owner = await _user(db, "Owner")
            agent_id = await _agent(db, owner)
            await db.commit()
        url = f"/api/v1/agents/{agent_id}"

        async with _client(sessions, owner) as client:
            set_ = await client.put(url, json={"tool_deny_list": ["create_report", " delete_note "]})
            assert set_.status_code == 200, set_.text
            assert set_.json()["tool_deny_list"] == ["create_report", "delete_note"]
            assert await _stored_deny_list(sessions, agent_id) == ["create_report", "delete_note"]

            # Left out: another field changes, the deny list stays.
            renamed = await client.put(url, json={"description": "Read-only investigator"})
            assert renamed.status_code == 200, renamed.text
            assert await _stored_deny_list(sessions, agent_id) == ["create_report", "delete_note"]

            cleared = await client.put(url, json={"tool_deny_list": []})
            assert cleared.status_code == 200, cleared.text
            assert cleared.json()["tool_deny_list"] is None
            assert await _stored_deny_list(sessions, agent_id) is None

            await client.put(url, json={"tool_deny_list": ["delete_note"]})
            nulled = await client.put(url, json={"tool_deny_list": None})
            assert nulled.status_code == 200, nulled.text
            assert await _stored_deny_list(sessions, agent_id) is None

        details = await _audit_details(sessions, agent_id, "update")
        assert [d["tool_deny_list"] for d in details] == [
            ["create_report", "delete_note"],
            ["create_report", "delete_note"],
            [],
            ["delete_note"],
            [],
        ]


@pytest.mark.parametrize(
    ("method", "path"),
    [("post", "/api/v1/agents"), ("put", "/api/v1/agents/{agent_id}")],
)
async def test_a_core_tool_a_non_string_or_an_oversized_list_is_refused_with_a_422(method: str, path: str) -> None:
    async with _database() as sessions:
        async with sessions() as db:
            owner = await _user(db, "Owner")
            agent_id = await _agent(db, owner, tool_deny_list=["delete_note"])
            await db.commit()

        async with _client(sessions, owner) as client:
            call = getattr(client, method)
            url = path.format(agent_id=agent_id)
            core = await call(url, json={"name": "Denies scheduling", "tool_deny_list": ["schedule_work_item"]})
            not_a_list = await call(url, json={"name": "Not a list", "tool_deny_list": "delete_note"})
            not_strings = await call(url, json={"name": "Not strings", "tool_deny_list": [{"name": "x"}]})
            too_many = await call(
                url,
                json={
                    "name": "Too many",
                    "tool_deny_list": [f"tool_{i}" for i in range(MAX_TOOL_DENY_LIST_ENTRIES + 1)],
                },
            )
            too_long = await call(
                url,
                json={"name": "Too long", "tool_deny_list": ["x" * (MAX_TOOL_DENY_LIST_NAME_LENGTH + 1)]},
            )

        for response in (core, not_a_list, not_strings, too_many, too_long):
            assert response.status_code == 422, response.text
        assert await _stored_deny_list(sessions, agent_id) == ["delete_note"]
        # A refused create writes no agent: the seeded one is still the only row.
        async with sessions() as session:
            assert (await session.execute(select(func.count(Agent.id)))).scalar_one() == 1


async def test_a_config_grantee_sets_it_and_a_read_only_grantee_is_refused() -> None:
    async with _database() as sessions:
        async with sessions() as db:
            owner = await _user(db, "Owner")
            editor = await _user(db, "Editor")
            viewer = await _user(db, "Viewer")
            editors = await _group(db, "Editors", editor)
            viewers = await _group(db, "Viewers", viewer)
            agent_id = await _agent(
                db,
                owner,
                shared_group_ids=[str(editors.id), str(viewers.id)],
                shared_group_permissions={str(editors.id): "config", str(viewers.id): "read_only"},
            )
            await db.commit()
        url = f"/api/v1/agents/{agent_id}"

        async with _client(sessions, editor) as client:
            response = await client.put(url, json={"tool_deny_list": ["delete_note"]})
        assert response.status_code == 200, response.text
        assert await _stored_deny_list(sessions, agent_id) == ["delete_note"]

        async with _client(sessions, viewer) as client:
            refused = await client.put(url, json={"tool_deny_list": []})
        assert refused.status_code == 403, refused.text
        assert await _stored_deny_list(sessions, agent_id) == ["delete_note"]


async def test_a_company_managed_agent_takes_it_from_an_admin_only() -> None:
    async with _database() as sessions:
        async with sessions() as db:
            admin = await _user(db, "Admin", admin=True)
            member = await _user(db, "Member")
            agent_id = await _agent(db, admin, is_company_managed=True, tool_deny_list=["delete_note"])
            await db.commit()
        url = f"/api/v1/agents/{agent_id}"

        async with _client(sessions, member) as client:
            refused = await client.put(url, json={"tool_deny_list": []})
        assert refused.status_code == 403, refused.text
        assert await _stored_deny_list(sessions, agent_id) == ["delete_note"]

        async with _client(sessions, admin) as client:
            response = await client.put(url, json={"tool_deny_list": ["delete_note", "send_email"]})
        assert response.status_code == 200, response.text
        assert await _stored_deny_list(sessions, agent_id) == ["delete_note", "send_email"]


async def test_a_built_in_agent_refuses_it_even_beside_a_writable_field() -> None:
    async with _database() as sessions:
        async with sessions() as db:
            admin = await _user(db, "Admin", admin=True)
            agent_id = await _agent(
                db, admin, is_builtin=True, is_company_managed=True, tool_deny_list=["delete_note"]
            )
            await db.commit()
        url = f"/api/v1/agents/{agent_id}"

        with patch(
            "app.core.config.Settings.is_xtm_one", new_callable=PropertyMock, return_value=True
        ):
            async with _client(sessions, admin) as client:
                alone = await client.put(url, json={"tool_deny_list": []})
                # ``enabled`` is a built-in carve-out; the deny list must not ride along.
                beside = await client.put(url, json={"enabled": True, "tool_deny_list": []})

        assert alone.status_code == 403, alone.text
        assert beside.status_code == 403, beside.text
        assert await _stored_deny_list(sessions, agent_id) == ["delete_note"]
