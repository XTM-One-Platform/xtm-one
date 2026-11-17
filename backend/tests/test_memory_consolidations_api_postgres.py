"""Memory consolidation history routes on PostgreSQL (#8258).

``/settings/me/memory/consolidations``: the caller's own lanes, nobody
else's, an administrator included. ``/agents/{id}/memory/consolidations``:
the agent's own lane, for the people who manage the agent. Rows are recorded
the way a pass records them (``tests/memory_steward_world.applied``).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import update

from app.api.v1 import memory_consolidations as routes
from app.models.agent import Agent
from app.models.memory_consolidation import MemoryConsolidation
from tests.memory_steward_world import applied, entry, make_world, state, user, world_database

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_REFUSED = "This consolidation can no longer be undone."


@asynccontextmanager
async def _world():
    async with world_database() as (db, session):
        world = await make_world(session, db.sessions)
        old = await entry(world, "Prefers answers in French", user=world.owner, days_old=9)
        new = await entry(world, "prefers answers in french", user=world.owner, days_old=1)
        world.merge = await applied(world, "merge", user=world.owner, before=(old,), after=new)
        world.old, world.new = old, new
        yield world


async def test_the_owner_lists_and_undoes_their_consolidation() -> None:
    async with _world() as world:
        owner = await user(world, world.owner)
        async with world.sessions() as session:
            listed = await routes.list_my_memory_consolidations(user=owner, db=session)

        [item] = listed["consolidations"]
        assert (listed["retention_days"], item["agent_name"], item["operation"], item["undoable"]) == (
            30,
            "Analyst",
            "merge",
            True,
        )
        assert [(e["role"], e["content"], e["exists"]) for e in item["entries"]] == [
            ("merged", "Prefers answers in French", True),
            ("kept", "prefers answers in french", True),
        ]

        async with world.sessions() as session:
            undone = await routes.undo_my_memory_consolidation(item["id"], user=owner, db=session)

        assert (undone["undoable"], undone["undone_at"] is not None) == (False, True)
        assert (await state(world))[world.old] == (False, None)

        async with world.sessions() as session:
            with pytest.raises(HTTPException) as twice:
                await routes.undo_my_memory_consolidation(item["id"], user=owner, db=session)
        assert (twice.value.status_code, twice.value.detail) == (409, _REFUSED)


@pytest.mark.parametrize("who", ["other", "admin"])
async def test_nobody_else_sees_or_undoes_a_users_consolidations(who: str) -> None:
    async with _world() as world:
        caller = await user(world, getattr(world, who))
        async with world.sessions() as session:
            listed = await routes.list_my_memory_consolidations(user=caller, db=session)
            with pytest.raises(HTTPException) as mine:
                await routes.undo_my_memory_consolidation(str(world.merge), user=caller, db=session)
        assert (listed["consolidations"], mine.value.status_code) == ([], 404)

        if who == "admin":
            # Nor through the agent lane route: the row is a user lane's.
            async with world.sessions() as session:
                with pytest.raises(HTTPException) as agent_route:
                    await routes.undo_agent_memory_consolidation(str(world.agent), str(world.merge), user=caller, db=session)
            assert agent_route.value.status_code == 404
        async with world.sessions() as session:
            assert (await session.get(MemoryConsolidation, world.merge)).undone_at is None


@pytest.mark.parametrize("raw", ["not-a-uuid", "00000000-0000-0000-0000-000000000000"])
async def test_an_unknown_id_is_a_404(raw: str) -> None:
    async with _world() as world:
        owner = await user(world, world.owner)
        async with world.sessions() as session:
            with pytest.raises(HTTPException) as unknown:
                await routes.undo_my_memory_consolidation(raw, user=owner, db=session)
        assert unknown.value.status_code == 404


async def test_the_agent_lane_history_is_for_its_managers() -> None:
    async with _world() as world:
        old = await entry(world, "Runs every morning at 8", user=None, days_old=6)
        new = await entry(world, "runs every morning at 8", user=None, days_old=1)
        row = await applied(world, "merge", user=None, before=(old,), after=new)
        owner, other = await user(world, world.owner), await user(world, world.other)

        async with world.sessions() as session:
            with pytest.raises(HTTPException) as hidden:
                await routes.list_agent_memory_consolidations(str(world.agent), user=other, db=session)
        assert hidden.value.status_code == 404

        async with world.sessions() as session:
            listed = await routes.list_agent_memory_consolidations(str(world.agent), user=owner, db=session)
        [item] = listed["consolidations"]
        assert (item["id"], [e["id"] for e in item["entries"]]) == (str(row), [str(old), str(new)])

        async with world.sessions() as session:
            await routes.undo_agent_memory_consolidation(str(world.agent), item["id"], user=owner, db=session)
        assert (await state(world))[old] == (False, None)


@pytest.mark.parametrize("tier", ["read_only", "config"])
async def test_a_shared_user_manages_the_agent_lane_only_with_config(tier: str) -> None:
    async with _world() as world:
        old = await entry(world, "Runs every morning at 8", user=None, days_old=6)
        new = await entry(world, "runs every morning at 8", user=None, days_old=1)
        row = await applied(world, "merge", user=None, before=(old,), after=new)
        async with world.sessions() as session:
            await session.execute(
                update(Agent)
                .where(Agent.id == world.agent)
                .values(shared_user_ids=[str(world.other)], shared_user_permissions={str(world.other): tier})
            )
            await session.commit()
        other = await user(world, world.other)

        async with world.sessions() as session:
            if tier == "read_only":
                with pytest.raises(HTTPException) as refused:
                    await routes.undo_agent_memory_consolidation(str(world.agent), str(row), user=other, db=session)
                assert refused.value.status_code == 403
            else:
                await routes.undo_agent_memory_consolidation(str(world.agent), str(row), user=other, db=session)

        assert (await state(world))[old] == ((True, new) if tier == "read_only" else (False, None))
