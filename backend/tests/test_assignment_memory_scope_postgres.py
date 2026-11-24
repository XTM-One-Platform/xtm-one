"""An assignment run's agent memory stays with the user the run acts as, on PostgreSQL.

A run's summary lands in its acting user's lane with the agent, and its
consolidation persists in that user's scope: another user's chat with the
same agent reads neither, through Redis nor through the durable pool. The
run's next run, as the same user, reads both.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.agent_memory_entry import AgentMemoryEntry
from app.services import agent_memory, persistent_memory
from app.services.assignment_helpers import (
    _load_memory_context,
    assignment_memory_scope,
    store_assignment_agent_memory,
)
from tests.memory_redis_double import MemoryRedis, summarizer, use_memory_redis
from tests.memory_steward_world import make_world, world_database

pytestmark = pytest.mark.integration

_SUMMARY = "Pulled the Q3 incident list from the private Jira: INC-4471 is open."
_CONSOLIDATED = "- INC-4471 is open in the private Jira."


async def _run_assignments(world, scope_id: str | None) -> None:
    """Enough runs for the lane to consolidate, the way the engine stores them."""
    tasks: set[asyncio.Task] = set()
    for _ in range(agent_memory.CONSOLIDATION_THRESHOLD):
        await store_assignment_agent_memory(
            agent_id=str(world.agent),
            scope_id=scope_id,
            source="Weekly incident digest",
            summary=_SUMMARY,
            consolidate=True,
            llm=summarizer(_CONSOLIDATED),
            model=None,
            is_company_managed=False,
            background_tasks=tasks,
        )
    await asyncio.gather(*list(tasks))


async def _durable_readable_by(world, reader: uuid.UUID | None) -> list[str]:
    """The persisted entries a reader's chat may read (``_readable_by``)."""
    async with world.sessions() as session:
        rows = await session.execute(
            select(AgentMemoryEntry.content).where(
                AgentMemoryEntry.agent_id == world.agent,
                persistent_memory._readable_by(AgentMemoryEntry, reader),
            )
        )
        return list(rows.scalars().all())


def _holds(memory: dict) -> bool:
    return bool(memory["short_term"] or memory["medium_term"])


@pytest.fixture
def redis(monkeypatch) -> MemoryRedis:
    redis = MemoryRedis()
    use_memory_redis(monkeypatch, redis)
    return redis


@pytest.mark.parametrize(("runner", "reader"), [("owner", "other"), ("other", "owner")])
async def test_another_users_chat_never_reads_a_runs_memory(monkeypatch, redis, runner: str, reader: str) -> None:
    async with world_database() as (db, session):
        world = await make_world(session, db.sessions)
        monkeypatch.setattr("app.core.database.async_session", db.sessions)
        acting, other = getattr(world, runner), getattr(world, reader)

        await _run_assignments(world, assignment_memory_scope(acting, world.owner))

        # Redis: the chat reads its own user's lane; the agent-level lane,
        # read by every assignment of the agent, holds nothing either.
        assert not _holds(await agent_memory.load_memory(str(world.agent), scope_id=str(other)))
        assert not _holds(await agent_memory.load_memory(str(world.agent)))
        # PostgreSQL: the consolidation is the acting user's.
        assert await _durable_readable_by(world, other) == []
        assert await _durable_readable_by(world, None) == []
        assert await _durable_readable_by(world, acting) == [_CONSOLIDATED]

        # The next run as the same user reads it; a run as the other does not.
        monkeypatch.setattr(
            "app.services.builtin_tools.memory._resolve_provider_model", _no_provider
        )
        async with world.sessions() as session:
            agent = await session.get(Agent, world.agent)
            mine, _ = await _load_memory_context(session, agent, acting)
            theirs, _ = await _load_memory_context(session, agent, other)
        assert _CONSOLIDATED in mine
        assert _CONSOLIDATED not in theirs and _SUMMARY not in theirs


async def _no_provider():
    return None


def test_a_run_without_a_user_acts_as_the_agents_owner() -> None:
    owner, runner = uuid.uuid4(), uuid.uuid4()
    assert assignment_memory_scope(runner, owner) == str(runner)
    assert assignment_memory_scope(None, owner) == str(owner)
    assert assignment_memory_scope(None, None) is None


async def test_a_run_with_no_user_at_all_writes_no_agent_memory(redis) -> None:
    tasks: set[asyncio.Task] = set()
    await store_assignment_agent_memory(
        agent_id=str(uuid.uuid4()),
        scope_id=None,
        source="Weekly incident digest",
        summary=_SUMMARY,
        consolidate=True,
        llm=summarizer(),
        model=None,
        is_company_managed=True,
        background_tasks=tasks,
    )
    assert (redis.data, tasks) == ({}, set())
