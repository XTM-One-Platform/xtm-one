"""Who may change a private agent's memory, on PostgreSQL.

The memory write routes - clear, maintain, edit and delete of an entry - are
for the people who manage the agent (``can_manage``): its owner, an admin, or
a ``config`` / ``config_with_chats`` grantee. A ``read_only`` grantee sees the
agent and its memory, and changes nothing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

from app.api.v1 import agent_memory as routes
from app.models.agent import Agent
from app.models.agent_memory_entry import AgentMemoryEntry
from tests.memory_steward_world import entry, make_world, user, world_database

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_ROUTES = ("clear", "maintain", "update", "delete")


@pytest.fixture(autouse=True)
def _no_side_services(monkeypatch):
    """No Redis, no embedding provider, no model: the routes' own rule is under test."""
    from app.services import agent_memory, persistent_memory

    monkeypatch.setattr(agent_memory, "clear_memory", AsyncMock())
    monkeypatch.setattr(persistent_memory, "embed_text_with_model", AsyncMock(return_value=(None, None)))
    for name in ("prune_old_memories", "enforce_memory_cap", "consolidate_agent_facts", "reembed_stale_memories"):
        monkeypatch.setattr(persistent_memory, name, AsyncMock(return_value=0))


async def _share(world, tier: str) -> None:
    async with world.sessions() as session:
        await session.execute(
            update(Agent)
            .where(Agent.id == world.agent)
            .values(shared_user_ids=[str(world.other)], shared_user_permissions={str(world.other): tier})
        )
        await session.commit()


async def _call(route: str, world, caller, entry_id) -> None:
    agent_id = str(world.agent)
    async with world.sessions() as session:
        if route == "clear":
            await routes.clear_all_agent_memories(agent_id, request=None, user=caller, db=session)
        elif route == "maintain":
            await routes.trigger_memory_maintenance(agent_id, request=None, user=caller, db=session)
        elif route == "update":
            await routes.update_memory_entry(
                agent_id,
                str(entry_id),
                routes.UpdateMemoryRequest(content="Rewritten"),
                request=None,
                user=caller,
                db=session,
            )
        else:
            await routes.delete_memory_entry(agent_id, str(entry_id), request=None, user=caller, db=session)


async def _contents(world) -> list[str]:
    async with world.sessions() as session:
        return list((await session.execute(select(AgentMemoryEntry.content))).scalars().all())


@pytest.mark.parametrize("route", _ROUTES)
async def test_a_read_only_grantee_changes_no_memory(route: str) -> None:
    async with world_database() as (db, session):
        world = await make_world(session, db.sessions)
        entry_id = await entry(world, "Runs every morning at 8", user=None, days_old=1)
        await _share(world, "read_only")
        other = await user(world, world.other)

        with pytest.raises(HTTPException) as refused:
            await _call(route, world, other, entry_id)

        assert refused.value.status_code == 403
        assert await _contents(world) == ["Runs every morning at 8"]


@pytest.mark.parametrize("route", _ROUTES)
@pytest.mark.parametrize("caller", ["owner", "config", "config_with_chats"])
async def test_a_manager_changes_the_memory(route: str, caller: str) -> None:
    async with world_database() as (db, session):
        world = await make_world(session, db.sessions)
        entry_id = await entry(world, "Runs every morning at 8", user=None, days_old=1)
        if caller != "owner":
            await _share(world, caller)
        acting = await user(world, world.owner if caller == "owner" else world.other)

        await _call(route, world, acting, entry_id)

        assert await _contents(world) == {
            "clear": [],
            "maintain": ["Runs every morning at 8"],
            "update": ["Rewritten"],
            "delete": [],
        }[route]


@pytest.mark.parametrize(
    ("caller", "tier", "expected"),
    [
        ("owner", None, True),
        ("admin", None, True),
        ("other", None, False),
        ("other", "read_only", False),
        ("other", "config", True),
        ("other", "config_with_chats", True),
    ],
)
async def test_invalidate_memory_retires_the_shared_pool_for_the_same_managers(
    caller: str, tier: str | None, expected: bool
) -> None:
    from app.services.persistent_memory import may_manage_shared_memory

    async with world_database() as (db, session):
        world = await make_world(session, db.sessions)
        if tier is not None:
            await _share(world, tier)
        acting = getattr(world, caller)

        async with world.sessions() as s:
            assert await may_manage_shared_memory(s, agent_id=str(world.agent), user_id=str(acting)) is expected


async def test_nobody_retires_the_shared_pool_of_a_platform_health_agent() -> None:
    from app.services.persistent_memory import may_manage_shared_memory

    async with world_database() as (db, session):
        world = await make_world(session, db.sessions)
        async with world.sessions() as s:
            await s.execute(update(Agent).where(Agent.id == world.agent).values(health_role="orchestrator"))
            await s.commit()

        async with world.sessions() as s:
            for acting in (world.owner, world.admin):
                assert not await may_manage_shared_memory(s, agent_id=str(world.agent), user_id=str(acting))
