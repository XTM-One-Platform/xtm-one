"""A deleted agent's Redis memory is cleared for good, and only once it is gone.

``consolidate_memory`` is detached from the chat turn and stores its result after
an LLM call. Every path that deletes an agent clears the agent's Redis memory
after the deletion COMMITTED, burying the agent's tombstone first, so a
consolidation still waiting on its LLM call undoes its own late write instead
of bringing the memory back (``memory_tombstones``). Clearing memory without
deleting the agent - the memory tab, the OpenAEV run purge - buries nothing:
the agent still exists and its next memories must land.

Each test drives the real route or tool and the real memory writers against an
in-memory Redis; only the database side of a deletion is stubbed.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.v1.helpers as api_helpers
import app.core.database as database
import app.services.builtin_tools as builtin_tools_module
from app.api.v1 import agent_memory as agent_memory_api
from app.api.v1 import agents as agents_module
from app.services import agent_deletion, agent_memory
from app.tools.base import ToolCall
from tests.memory_redis_double import MemoryRedis, ParkedLLM, use_memory_redis


def _request():
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})


async def _noop(*_a, **_kw):
    return None


async def _seed_lanes(agent_id: str, user_id: str) -> None:
    """The agent's own lane and one user's lane with it, ready to consolidate."""
    for scope_id in (None, user_id):
        await agent_memory.store_short_term_memory(
            agent_id, source="chat", summary="did a thing", scope_id=scope_id
        )


def _park_consolidations(agent_id: str, user_id: str, llm: ParkedLLM):
    return [
        asyncio.create_task(
            agent_memory.consolidate_memory(
                agent_id, llm, force=True, scope_id=scope_id, scope_is_user_id=False
            )
        )
        for scope_id in (None, user_id)
    ]


def _agent_keys(agent_id: str, user_id: str) -> set[str]:
    return set(agent_memory.lane_keys(agent_id)) | set(
        agent_memory.lane_keys(agent_id, user_id)
    )


def _stub_rest_delete(monkeypatch, agent, *, delete=_noop) -> None:
    """``DELETE /agents/{id}`` with its database work replaced by *delete*."""

    async def _no_groups(*_a, **_kw):
        return []

    async def _agent(*_a, **_kw):
        return agent

    async def _serialized(attempt, *, label):
        await delete()

    monkeypatch.setattr(agents_module, "get_user_group_ids", _no_groups)
    monkeypatch.setattr(agents_module, "get_user_agent", _agent)
    monkeypatch.setattr(agents_module, "assert_writable_agent", lambda *_a, **_kw: None)
    monkeypatch.setattr(agent_deletion, "_delete_runs_in_batches", _noop)
    monkeypatch.setattr(api_helpers, "run_with_deadlock_retry", _serialized)


# -- DELETE /agents/{id} --------------------------------------------------------


async def test_rest_delete_leaves_no_memory_for_a_consolidation_to_write_back(
    monkeypatch,
):
    agent_id, user_id = str(uuid.uuid4()), str(uuid.uuid4())
    redis = MemoryRedis()
    use_memory_redis(monkeypatch, redis)
    await _seed_lanes(agent_id, user_id)
    llm = ParkedLLM()
    in_flight = _park_consolidations(agent_id, user_id, llm)
    await llm.parked(len(in_flight))
    _stub_rest_delete(monkeypatch, SimpleNamespace(id=uuid.UUID(agent_id)))

    await agents_module.delete_agent(
        agent_id=agent_id,
        request=_request(),
        user=SimpleNamespace(id=uuid.uuid4(), is_admin=True),
        db=None,
        _ai_gate=None,
    )
    llm.release()
    await asyncio.gather(*in_flight)

    # Both consolidations answered after the deletion: neither brought the
    # agent's memory back.
    assert not _agent_keys(agent_id, user_id) & redis.memory()
    assert f"memory_tombstone:agent:{agent_id}" in redis.tombstones()


async def test_a_failed_rest_delete_keeps_the_agent_memory_and_sets_no_tombstone(
    monkeypatch,
):
    agent_id, user_id = str(uuid.uuid4()), str(uuid.uuid4())
    redis = MemoryRedis()
    use_memory_redis(monkeypatch, redis)
    await _seed_lanes(agent_id, user_id)
    before = redis.memory()

    async def _deadlocked():
        raise RuntimeError("the delete transaction failed")

    _stub_rest_delete(
        monkeypatch, SimpleNamespace(id=uuid.UUID(agent_id)), delete=_deadlocked
    )

    with pytest.raises(HTTPException) as exc:
        await agents_module.delete_agent(
            agent_id=agent_id,
            request=_request(),
            user=SimpleNamespace(id=uuid.uuid4(), is_admin=True),
            db=None,
            _ai_gate=None,
        )

    # The agent still exists: its memory stays, and nothing blocks the next.
    assert exc.value.status_code == 500
    assert redis.memory() == before
    assert not redis.tombstones()


# -- the agent-builder delete_agent tool -------------------------------------------


class _BuilderSession:
    """The dedicated session the tool deletes on, as far as it goes."""

    def __init__(self):
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, *_a, **_kw):
        return None

    async def delete(self, _obj):
        return None

    async def commit(self):
        self.committed = True

    async def rollback(self):
        return None


async def test_agent_builder_delete_leaves_no_memory_for_a_consolidation_to_write_back(
    monkeypatch,
):
    agent_id, user_id = str(uuid.uuid4()), str(uuid.uuid4())
    redis = MemoryRedis()
    use_memory_redis(monkeypatch, redis)
    await _seed_lanes(agent_id, user_id)
    llm = ParkedLLM()
    in_flight = _park_consolidations(agent_id, user_id, llm)
    await llm.parked(len(in_flight))

    agent = SimpleNamespace(id=uuid.UUID(agent_id), name="Scratch agent")
    sessions: list[_BuilderSession] = []

    def _session():
        sessions.append(_BuilderSession())
        return sessions[-1]

    async def _writable(_db, _uid, _agent_uuid, *, action="modify"):
        return agent, None

    async def _caller(_db, uid):
        return SimpleNamespace(id=uid, email="builder@example.com")

    monkeypatch.setattr(database, "async_session", _session)
    monkeypatch.setattr(agent_deletion, "_delete_runs_in_batches", _noop)
    monkeypatch.setattr(agent_deletion, "_delete_agent_rows", _noop)
    monkeypatch.setattr(
        builtin_tools_module, "_load_writable_agent_for_builder", _writable
    )
    monkeypatch.setattr(builtin_tools_module, "_load_caller_user_for_builder", _caller)

    _, executor = builtin_tools_module.build_agent_builder_tool(None, user_id)
    result = await executor.execute_tool(
        ToolCall(id="call-1", name="delete_agent", arguments={"agent_id": agent_id})
    )
    llm.release()
    await asyncio.gather(*in_flight)

    assert not result.is_error, result.content
    assert [session.committed for session in sessions] == [True]
    assert not _agent_keys(agent_id, user_id) & redis.memory()
    assert f"memory_tombstone:agent:{agent_id}" in redis.tombstones()


# -- the startup seed removing a stale built-in agent -----------------------------


async def test_a_removed_built_in_agent_memory_goes_once_the_seed_committed(
    monkeypatch,
):
    from app.agents import seed

    agent_id, user_id = str(uuid.uuid4()), str(uuid.uuid4())
    redis = MemoryRedis()
    use_memory_redis(monkeypatch, redis)
    await _seed_lanes(agent_id, user_id)
    # A seeded built-in: company-managed, the only kind the sweep deletes.
    stale = SimpleNamespace(
        id=uuid.UUID(agent_id), slug="retired-agent", is_company_managed=True
    )
    memory_at_commit: list[set[str]] = []

    class _SeedSession:
        async def execute(self, *_a, **_kw):
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [stale])
            )

        async def delete(self, _obj):
            return None

        async def commit(self):
            memory_at_commit.append(redis.memory() & _agent_keys(agent_id, user_id))

    # A release whose seed declares no agent: the stale one is retired.
    monkeypatch.setattr(seed, "_seeded_builtin_agents", lambda: [])
    await seed.seed_builtin_agents(_SeedSession(), platform_mode="copilot")

    # Cleared after the commit, never before it: a failed seed keeps the agent
    # and its memory.
    assert memory_at_commit and memory_at_commit[0]
    assert not _agent_keys(agent_id, user_id) & redis.memory()
    assert f"memory_tombstone:agent:{agent_id}" in redis.tombstones()


# -- clearing memory WITHOUT deleting the agent ----------------------------------


async def _later_memory_lands(agent_id: str, user_id: str) -> None:
    llm = ParkedLLM("- a new summary")
    llm.release()
    await _seed_lanes(agent_id, user_id)
    await agent_memory.consolidate_memory(
        agent_id, llm, force=True, scope_is_user_id=False
    )


async def test_the_memory_tab_clear_lets_later_memories_land(monkeypatch):
    import app.services.persistent_memory as persistent_memory

    agent_id, user_id = str(uuid.uuid4()), str(uuid.uuid4())
    redis = MemoryRedis()
    use_memory_redis(monkeypatch, redis)
    await _seed_lanes(agent_id, user_id)
    agent = SimpleNamespace(id=uuid.UUID(agent_id), name="Kept agent")

    async def _agent(*_a, **_kw):
        return agent

    async def _no_rows(*_a, **_kw):
        return 0

    monkeypatch.setattr(agent_memory_api, "_managed_agent", _agent)
    monkeypatch.setattr(agent_memory_api, "log_audit", _noop)
    monkeypatch.setattr(persistent_memory, "clear_agent_memories", _no_rows)

    await agent_memory_api.clear_all_agent_memories(
        agent_id=agent_id,
        request=_request(),
        user=SimpleNamespace(id=uuid.uuid4(), is_admin=True),
        # Every read and write is stubbed above; only a commit can reach it.
        db=SimpleNamespace(commit=_noop),
        _ai_gate=None,
    )
    assert not _agent_keys(agent_id, user_id) & redis.memory()

    await _later_memory_lands(agent_id, user_id)

    assert not redis.tombstones()
    assert set(agent_memory.lane_keys(agent_id)) <= redis.memory()
    assert {
        agent_memory._short_term_key(agent_id, user_id),
        agent_memory._counter_key(agent_id, user_id),
    } <= redis.memory()


async def test_the_autonomous_run_purge_lets_later_memories_land(monkeypatch):
    import app.services.persistent_memory as persistent_memory
    from app.services import autonomous_pentest

    agent_id, user_id = str(uuid.uuid4()), str(uuid.uuid4())
    redis = MemoryRedis()
    use_memory_redis(monkeypatch, redis)
    await _seed_lanes(agent_id, user_id)

    async def _no_rows(*_a, **_kw):
        return 0

    monkeypatch.setattr(persistent_memory, "clear_agent_memories", _no_rows)

    class _PurgeSession:
        async def execute(self, *_a, **_kw):
            return None

    await autonomous_pentest.purge_autonomous_run_state(
        _PurgeSession(), "run-1", orchestrator_agent_id=agent_id
    )
    assert not _agent_keys(agent_id, user_id) & redis.memory()

    # The orchestrator lives on: its next engagement's memory must land.
    await _later_memory_lands(agent_id, user_id)

    assert not redis.tombstones()
    assert set(agent_memory.lane_keys(agent_id)) <= redis.memory()
