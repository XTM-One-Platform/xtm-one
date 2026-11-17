"""A chat turn cut by the graceful shutdown timeout keeps its memory.

``run_agent_chat`` records a turn's interaction once its loop returns. A turn
whose request uvicorn's bounded drain cancels records what happened instead, in
the tiers and the scope the turn would have written, as a shutdown finalizer
the lifespan drain waits for, and without a consolidation model call. Any other
cancellation records nothing.

The tiers are unit-tested on ``_store_chat_memories``; the hook runs the real
``run_agent_chat`` against PostgreSQL with the model, the loop and the memory
stores stubbed.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.core.shutdown_finalizers import drain_shutdown_finalizers
from app.models.agent import Agent
from app.models.user import User
from app.services import agent_chat
from tests.scratch_database import ALL_TABLES, scratch_database

UVICORN_DRAIN_CANCEL_MSG = "Task cancelled, timeout graceful shutdown exceeded"
_QUESTION = "What is left before we ship?"


@pytest.fixture
def memory_stores(monkeypatch) -> SimpleNamespace:
    """The memory stores and their consolidations, recorded."""
    stores = SimpleNamespace(
        short=AsyncMock(return_value=99),
        user=AsyncMock(return_value=99),
        consolidate=AsyncMock(),
        consolidate_user=AsyncMock(),
    )
    monkeypatch.setattr("app.services.agent_memory.store_short_term_memory", stores.short)
    monkeypatch.setattr("app.services.user_memory.store_user_memory", stores.user)
    monkeypatch.setattr("app.services.agent_memory.consolidate_memory", stores.consolidate)
    monkeypatch.setattr("app.services.user_memory.consolidate_user_memory", stores.consolidate_user)
    return stores


def _tiers(**overrides) -> dict:
    tiers = dict(
        agent_id="agent-1",
        agent_name="Desk",
        user_id="user-1",
        scope_id="user-1",
        is_anonymous=False,
        is_company_managed=False,
        store_short=True,
        store_medium=True,
        store_user=True,
        llm=object(),
        model="m",
    )
    tiers.update(overrides)
    return tiers


# ---------------------------------------------------------------------------
# The tiers
# ---------------------------------------------------------------------------


async def test_an_interaction_reaches_both_tiers_in_its_scope(memory_stores):
    await agent_chat._store_chat_memories("summary", **_tiers())
    await asyncio.sleep(0)

    memory_stores.short.assert_awaited_once_with(
        "agent-1", source="chat", summary="summary", source_type="chat", scope_id="user-1"
    )
    memory_stores.user.assert_awaited_once_with(
        "user-1", agent_name="Desk", summary="summary", interaction_type="chat", agent_id="agent-1"
    )
    # Past both thresholds: each consolidation runs, in the background.
    memory_stores.consolidate.assert_called_once()
    memory_stores.consolidate_user.assert_called_once()


async def test_a_turn_the_shutdown_cut_never_starts_a_consolidation(memory_stores):
    await agent_chat._store_chat_memories("summary", **_tiers(), consolidate=False)
    await asyncio.sleep(0)

    memory_stores.short.assert_awaited_once()
    memory_stores.user.assert_awaited_once()
    memory_stores.consolidate.assert_not_called()
    memory_stores.consolidate_user.assert_not_called()


async def test_each_tier_keeps_its_switch_and_an_anonymous_user_has_no_user_memory(memory_stores):
    await agent_chat._store_chat_memories("summary", **_tiers(store_short=False))
    await agent_chat._store_chat_memories("summary", **_tiers(store_user=False, is_anonymous=True, store_short=False))
    await agent_chat._store_chat_memories("summary", **_tiers(is_anonymous=True, store_medium=False))

    memory_stores.short.assert_awaited_once()
    memory_stores.user.assert_awaited_once()
    memory_stores.consolidate.assert_not_called()


# ---------------------------------------------------------------------------
# The hook, on the real run_agent_chat
# ---------------------------------------------------------------------------


class _FakeRedis:
    async def incr(self, key):
        return 1

    async def decr(self, key):
        return 0

    async def expire(self, key, ttl):
        return True

    async def delete(self, key):
        return 1


class _DownRedis(_FakeRedis):
    """No chat activity tracking, so no event sink of its own around the loop."""

    async def incr(self, key):
        raise ConnectionError("redis down")


def _loop_cut_in_a_tool_call(started: asyncio.Event):
    async def _loop(**kwargs):
        on_event = kwargs["on_event"]
        await on_event({"status": "tool_start", "tools": ["search_web", "read_page"], "iteration": 1})
        await on_event({"status": "tool_done", "tool": "search_web", "success": True, "tool_output": "3 hits"})
        started.set()
        await asyncio.Event().wait()

    return _loop


def _loop_without_events(started: asyncio.Event):
    async def _loop(**kwargs):
        # No event sink is installed for the ledger alone: the loop runs as
        # it always has.
        assert kwargs["on_event"] is None
        started.set()
        await asyncio.Event().wait()

    return _loop


def _stub_turn(monkeypatch, loop, redis: _FakeRedis) -> None:
    """Stub everything around the agent loop."""
    for target, value in {
        "app.core.redis.redis_client": AsyncMock(return_value=redis),
        "app.services.langfuse_tracing.trace_llm_call": AsyncMock(return_value=None),
        "app.services.variable_resolver.load_variables": AsyncMock(return_value={}),
        "app.services.agent_chat.enforce_agentic_quota": AsyncMock(return_value=None),
        "app.services.builtin_tools.load_custom_and_mcp_tools": AsyncMock(return_value=None),
        "app.services.builtin_tools.build_kb_search_tool": MagicMock(return_value=None),
        "app.services.integration_tools.build_integration_tools": AsyncMock(return_value=([], None)),
        "app.services.integration_tools.build_channel_tools": AsyncMock(return_value=([], None)),
        "app.services.agent_memory.load_memory": AsyncMock(return_value={}),
        "app.services.user_memory.load_user_memory": AsyncMock(return_value={}),
        "app.services.persistent_memory.load_persistent_context": AsyncMock(return_value=""),
        "app.services.builtin_tools.memory._resolve_provider_model": AsyncMock(return_value=None),
        "app.services.agent_chat.run_agent_loop": loop,
    }.items():
        monkeypatch.setattr(target, value)


async def _cut_turn(
    monkeypatch,
    *,
    msg: str | None,
    loop=_loop_cut_in_a_tool_call,
    on_event: bool = True,
    redis: _FakeRedis | None = None,
    evaluation_mode: bool = False,
) -> None:
    """Run one turn until its loop is busy, cancel it with *msg*, drain."""
    started = asyncio.Event()
    async with (
        scratch_database("memcut", tables=ALL_TABLES, extensions=("vector",)) as db,
        db.sessions() as session,
    ):
        user = User(id=uuid.uuid4(), email="alice@test.local", full_name="Alice", hashed_password="x")
        session.add(user)
        await session.flush()
        agent = Agent(
            id=uuid.uuid4(),
            name="Release Desk",
            slug="release-desk",
            persona="You are the release desk agent of the test suite.",
            enabled=True,
            user_id=user.id,
            is_builtin=False,
            is_company_managed=False,
            builtin_tool_ids=[],
            handover_agent_ids=[],
            integration_bindings={},
            mcp_server_ids=[],
            custom_tool_ids=[],
            knowledge_base_ids=[],
            output_format="markdown",
            memory_short_term_enabled=True,
            memory_medium_term_enabled=True,
            memory_persistent_enabled=False,
            memory_user_enabled=True,
        )
        session.add(agent)
        await session.commit()
        _stub_turn(monkeypatch, loop(started), redis or _FakeRedis())
        turn = asyncio.create_task(
            agent_chat.run_agent_chat(
                session,
                str(user.id),
                agent,
                [],
                _QUESTION,
                on_event=AsyncMock() if on_event else None,
                meter_quota=False,
                evaluation_mode=evaluation_mode,
                prebuilt_llm=(SimpleNamespace(provider_name="fake"), "fake-model"),
            )
        )
        await asyncio.wait_for(started.wait(), 30)
        if msg is None:
            turn.cancel()
        else:
            turn.cancel(msg=msg)
        with pytest.raises(asyncio.CancelledError):
            await turn
        assert await drain_shutdown_finalizers(timeout=5) == 0


@pytest.mark.integration
async def test_a_turn_cut_by_the_drain_records_what_happened(monkeypatch, memory_stores):
    await _cut_turn(monkeypatch, msg=UVICORN_DRAIN_CANCEL_MSG)

    expected = (
        f"User asked: {_QUESTION} → Interrupted before the agent replied: the server "
        "process running the turn shut down. 1 tool call completed; last completed action: "
        "search_web. Still running: read_page."
    )
    [short] = memory_stores.short.await_args_list
    assert short.kwargs["summary"] == expected and short.kwargs["source_type"] == "chat"
    [user] = memory_stores.user.await_args_list
    assert user.kwargs["summary"] == expected
    memory_stores.consolidate.assert_not_called()
    memory_stores.consolidate_user.assert_not_called()


@pytest.mark.integration
async def test_a_cut_turn_whose_loop_had_no_event_sink_claims_nothing_about_its_tools(monkeypatch, memory_stores):
    await _cut_turn(
        monkeypatch,
        msg=UVICORN_DRAIN_CANCEL_MSG,
        loop=_loop_without_events,
        on_event=False,
        redis=_DownRedis(),
    )

    [short] = memory_stores.short.await_args_list
    assert short.kwargs["summary"] == (
        f"User asked: {_QUESTION} → Interrupted before the agent replied: the server "
        "process running the turn shut down."
    )


@pytest.mark.integration
@pytest.mark.parametrize("msg", [None, "the client went away"])
async def test_a_turn_cancelled_otherwise_records_nothing(monkeypatch, memory_stores, msg):
    await _cut_turn(monkeypatch, msg=msg)

    memory_stores.short.assert_not_called()
    memory_stores.user.assert_not_called()


@pytest.mark.integration
async def test_an_evaluation_turn_cut_by_the_drain_records_nothing(monkeypatch, memory_stores):
    await _cut_turn(monkeypatch, msg=UVICORN_DRAIN_CANCEL_MSG, evaluation_mode=True)

    memory_stores.short.assert_not_called()
    memory_stores.user.assert_not_called()
