"""A run names its parent only to a caller who could open it.

``GET /runs/{id}/descendants`` never names a parent the caller cannot open:
no id, no agent. The other run reads follow the same rule - the run
page, the run lists, the actions answering with a run and the agent-builder
``get_run`` tool - including the delegation ``trigger_payload`` keys that carry
the parent's id and agent. Same scratch-database recipe as
``test_run_descendants_postgres.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api.v1.assignments import get_run_descendants, get_run_detail, list_all_runs
from app.models.assignment import AssignmentRun
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("runparent", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _seed_user(session, name: str, *, role: str = "user"):
    from app.models.agent import Agent
    from app.models.user import User

    user = User(
        id=uuid.uuid4(),
        email=f"{name}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=name,
        hashed_password="x",
        role=role,
    )
    session.add(user)
    await session.flush()
    agent = Agent(id=uuid.uuid4(), name=f"{name} agent", user_id=user.id)
    session.add(agent)
    await session.flush()
    user.agent_id = agent.id
    return user


async def _run(session, user, *, parent=None, company=False):
    row = AssignmentRun(
        id=uuid.uuid4(),
        assignment_id=None,
        agent_id=user.agent_id,
        user_id=user.id,
        parent_run_id=parent.id if parent else None,
        status="running",
        is_company_managed=company,
        started_at=datetime.now(UTC),
        trigger_payload=(
            {
                "trigger_type": "delegation",
                "parent_agent_id": str(parent.agent_id),
                "parent_run_id": str(parent.id),
                "task": "look into it",
            }
            if parent
            # What every run writer stores for a run with no payload of its own.
            else {}
        ),
    )
    session.add(row)
    await session.flush()
    return row


def _caller(user, *, admin: bool = False):
    return SimpleNamespace(id=user.id, is_admin=admin)


async def _counted(caller, session, parent_run_id: str) -> int:
    from app.api.v1.assignments import count_runs

    result = await count_runs(
        status=None,
        search=None,
        notifications=False,
        digests=False,
        ownership=None,
        agent_id=None,
        agent_ids=None,
        assignment_id=None,
        parent_run_id=parent_run_id,
        a2a_integration_id=None,
        scope="visible",
        user=caller,
        db=session,
    )
    return result.count


async def _listed(caller, session, parent_run_id: str = "") -> dict:
    rows = await list_all_runs(
        limit=50,
        offset=0,
        status=None,
        search=None,
        notifications=False,
        digests=False,
        ownership=None,
        agent_id=None,
        agent_ids=None,
        parent_run_id=parent_run_id,
        a2a_integration_id=None,
        scope="visible",
        user=caller,
        db=session,
    )
    return {row.id: row for row in rows}


async def _builder_tool(user, tool: str, **arguments: str) -> str:
    from app.services.builtin_tools import build_agent_builder_tool

    _, executor = build_agent_builder_tool(None, str(user.id))
    result = await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=tool, arguments=arguments))
    return result.content


async def _get_run_tool(user, run_id: str) -> str:
    return await _builder_tool(user, "get_run", run_id=run_id)


def _names_parent(row, parent) -> bool:
    payload = row.trigger_payload or {}
    return (
        row.parent_run_id == str(parent.id)
        and row.parent_agent_name == "owner agent"
        and payload.get("parent_run_id") == str(parent.id)
        and payload.get("parent_agent_id") == str(parent.agent_id)
    )


def _names_nothing(row) -> bool:
    payload = row.trigger_payload or {}
    return (
        row.parent_run_id is None
        and row.parent_agent_name is None
        and row.parent_agent_icon is None
        and "parent_run_id" not in payload
        and "parent_agent_id" not in payload
        # The rest of the payload is the run's own input and stays.
        and payload.get("task") == "look into it"
    )


async def test_a_hidden_parent_is_neither_linked_nor_named():
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner = await _seed_user(session, "owner")
        stranger = await _seed_user(session, "stranger")
        admin = await _seed_user(session, "admin", role="admin")
        root = await _run(session, owner, company=True)
        shared = await _run(session, owner, parent=root, company=True)
        private = await _run(session, owner, parent=shared)
        below = await _run(session, owner, parent=private, company=True)
        await session.commit()

        # The stranger opens ``below`` but not the private run above it.
        detail = await get_run_detail(str(below.id), _caller(stranger), session)
        assert _names_nothing(detail)
        listed = await _listed(_caller(stranger), session)
        assert str(private.id) not in listed
        assert _names_nothing(listed[str(below.id)])
        # A parent they can open is still named.
        assert _names_parent(await get_run_detail(str(shared.id), _caller(stranger), session), root)
        assert _names_parent(listed[str(shared.id)], root)
        # A filter naming the hidden parent lists and counts nothing: the
        # children it would return confirm the link. One they can open works.
        assert await _listed(_caller(stranger), session, str(private.id)) == {}
        assert await _counted(_caller(stranger), session, str(private.id)) == 0
        assert set(await _listed(_caller(stranger), session, str(root.id))) == {str(shared.id)}
        assert await _counted(_caller(stranger), session, str(root.id)) == 1
        assert await _listed(_caller(stranger), session, "not-a-run-id") == {}
        # The descendants list says the same about its payload.
        rows = {r.id: r for r in await get_run_descendants(str(root.id), _caller(stranger), session)}
        assert _names_nothing(rows[str(below.id)])
        assert "Parent run:" not in await _get_run_tool(stranger, str(below.id))
        assert f"Parent run: `{root.id}`" in await _get_run_tool(stranger, str(shared.id))
        # Nor is a run found by the id of the parent it hides: the cross-run
        # search does not read the parent keys of a payload.
        assert str(below.id) in await _builder_tool(stranger, "list_runs", search="look into it")
        assert str(below.id) not in await _builder_tool(stranger, "list_runs", search=str(private.id))
        # Nor does the search inside the run read them.
        assert "look into it" in await _builder_tool(stranger, "get_run", run_id=str(below.id), search="look into")
        for hidden in (str(private.id), str(private.agent_id)):
            found = await _builder_tool(stranger, "get_run", run_id=str(below.id), search=hidden)
            assert found.startswith(f"No matches for `{hidden}`")

        # The owner and an admin may open it: named everywhere.
        for caller, user in ((_caller(owner), owner), (_caller(admin, admin=True), admin)):
            assert _names_parent(await get_run_detail(str(below.id), caller, session), private)
            assert _names_parent((await _listed(caller, session))[str(below.id)], private)
            assert set(await _listed(caller, session, str(private.id))) == {str(below.id)}
            assert await _counted(caller, session, str(private.id)) == 1
            assert await _listed(caller, session, "not-a-run-id") == {}
            assert await _counted(caller, session, "not-a-run-id") == 0
            rows = {r.id: r for r in await get_run_descendants(str(root.id), caller, session)}
            assert _names_parent(rows[str(below.id)], private)
            assert f"Parent run: `{private.id}`" in await _get_run_tool(user, str(below.id))
            for shown in (str(private.id), str(private.agent_id)):
                found = await _builder_tool(user, "get_run", run_id=str(below.id), search=shown)
                assert shown in found and not found.startswith("No matches")
