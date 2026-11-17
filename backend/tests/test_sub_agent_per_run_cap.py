"""Tests for the per-run sub-agent concurrency cap.

The cap is enforced durably by counting in-flight (pending/running) ``sub_agent``
orchestration executions for the parent run (``count_active_sub_agent_executions``)
— it replaces the old in-process counter + ``on_complete_cb`` mechanism, so it is
accurate across the iterative spawn → collect → spawn pattern and survives
restarts.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.builtin_tools import build_sub_agent_tools


def _make_agent(name: str = "Worker") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        slug=name.lower(),
        handover_types={},
    )


def _make_fake_sub_task() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), status="pending")


def _build_spawn(monkeypatch, *, parent_run_id: str | None = None):
    agent = _make_agent()
    db = MagicMock()
    result = build_sub_agent_tools(
        db,
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(uuid.uuid4()),
        handover_agents=[agent],
        parent_run_id=parent_run_id or str(uuid.uuid4()),
    )
    assert result is not None
    _defs, executor = result
    return agent, executor._handlers["spawn_background_task"]


@pytest.mark.asyncio
async def test_spawn_rejected_when_active_count_at_limit(monkeypatch):
    """spawn_background_task must return an error string (not raise) when the
    active-execution count equals the configured per-run limit."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "max_concurrent_sub_agent_tasks_per_run", 2)
    agent, spawn = _build_spawn(monkeypatch)

    create_mock = AsyncMock(side_effect=AssertionError("must not spawn at limit"))
    with (
        patch(
            "app.services.sub_agent.count_active_sub_agent_executions",
            new=AsyncMock(return_value=2),
        ),
        patch("app.services.sub_agent.create_sub_agent_task", new=create_mock),
    ):
        msg = await spawn(str(agent.id), "task at limit")

    assert "Cannot spawn task" in msg
    assert "2" in msg  # references the configured limit
    create_mock.assert_not_called()


@pytest.mark.asyncio
async def test_spawn_allowed_when_below_limit(monkeypatch):
    """When the active-execution count is below the limit, the spawn proceeds."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "max_concurrent_sub_agent_tasks_per_run", 2)
    agent, spawn = _build_spawn(monkeypatch)

    create_mock = AsyncMock(return_value=_make_fake_sub_task())
    with (
        patch(
            "app.services.sub_agent.count_active_sub_agent_executions",
            new=AsyncMock(return_value=1),
        ),
        patch("app.services.sub_agent.create_sub_agent_task", new=create_mock),
    ):
        msg = await spawn(str(agent.id), "task below limit")

    assert "spawned successfully" in msg
    create_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_spawn_rejected_when_atomic_gate_declines(monkeypatch):
    """The fast-path pre-check can pass (count below limit) yet the authoritative
    atomic gate inside ``create_sub_agent_task`` may still decline — returning
    ``None`` because a concurrent spawn for the same scope won the race under the
    advisory lock. ``spawn_background_task`` must surface the cap message and
    forward ``run_limit`` so the gate can enforce it."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "max_concurrent_sub_agent_tasks_per_run", 2)
    agent, spawn = _build_spawn(monkeypatch)

    create_mock = AsyncMock(return_value=None)  # atomic gate declined
    with (
        patch(
            "app.services.sub_agent.count_active_sub_agent_executions",
            new=AsyncMock(return_value=1),  # below limit → pre-check passes
        ),
        patch("app.services.sub_agent.create_sub_agent_task", new=create_mock),
    ):
        msg = await spawn(str(agent.id), "task that loses the race")

    assert "Cannot spawn task" in msg
    assert "2" in msg  # references the configured limit
    create_mock.assert_awaited_once()
    # The limit must be forwarded so the atomic gate can enforce it race-free.
    assert create_mock.await_args.kwargs.get("run_limit") == 2


@pytest.mark.asyncio
async def test_spawn_unknown_agent_returns_error(monkeypatch):
    """An unresolvable target agent returns an error without touching the engine."""
    _agent, spawn = _build_spawn(monkeypatch)

    count_mock = AsyncMock(return_value=0)
    with patch(
        "app.services.sub_agent.count_active_sub_agent_executions", new=count_mock
    ):
        msg = await spawn(str(uuid.uuid4()), "task for unknown agent")

    assert "not available for background tasks" in msg
    count_mock.assert_not_called()


# ── Independent-session safety ───────────────────────────────────────
# The sub-agent tools execute concurrently under ``asyncio.gather`` (one LLM
# turn can request several spawns at once). A SQLAlchemy ``AsyncSession`` cannot
# serve concurrent operations, so every sub-agent tool DB access must run on its
# own session — never the shared chat/assignment session. Sharing it raised
# "This session is provisioning a new connection; concurrent operations are not
# permitted" when an orchestrator fanned out background tasks.


def _fake_session_factory(monkeypatch, own_session):
    """Patch ``app.core.database.async_session`` to yield *own_session*."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=own_session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)
    monkeypatch.setattr("app.core.database.async_session", factory)
    return factory


@pytest.mark.asyncio
async def test_count_active_executions_opens_own_session_when_db_omitted(monkeypatch):
    """Calling the count without a ``db`` opens an independent session.

    The ``spawn_background_task`` fast-path pre-check no
    longer borrows the shared session — it relies on the count opening its own.
    """
    from app.services import sub_agent

    fake_result = MagicMock()
    fake_result.scalar = MagicMock(return_value=3)
    own_session = AsyncMock()
    own_session.execute = AsyncMock(return_value=fake_result)
    factory = _fake_session_factory(monkeypatch, own_session)

    count = await sub_agent.count_active_sub_agent_executions(
        parent_run_id=str(uuid.uuid4())
    )

    assert count == 3
    factory.assert_called_once()  # opened its own session
    own_session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_spawn_precheck_never_touches_shared_session(monkeypatch):
    """The spawn cap pre-check must not run on the shared chat/assignment session.

    Parallel spawns under ``asyncio.gather`` collided on
    the shared ``AsyncSession``. The pre-check must call the count WITHOUT a
    positional session argument (so the count opens its own).
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "max_concurrent_sub_agent_tasks_per_run", 5)

    agent = _make_agent()
    shared_db = MagicMock()
    shared_db.execute = AsyncMock(side_effect=AssertionError("shared session used"))

    result = build_sub_agent_tools(
        shared_db,
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(uuid.uuid4()),
        handover_agents=[agent],
        parent_run_id=str(uuid.uuid4()),
    )
    assert result is not None
    _defs, executor = result
    spawn = executor._handlers["spawn_background_task"]

    count_mock = AsyncMock(return_value=0)
    create_mock = AsyncMock(return_value=_make_fake_sub_task())
    with (
        patch(
            "app.services.sub_agent.count_active_sub_agent_executions", new=count_mock
        ),
        patch("app.services.sub_agent.create_sub_agent_task", new=create_mock),
    ):
        msg = await spawn(str(agent.id), "fan-out task")

    assert "spawned successfully" in msg
    # No positional session arg — only the scope kwargs are forwarded.
    assert count_mock.await_args is not None
    assert count_mock.await_args.args == ()
    assert set(count_mock.await_args.kwargs) == {"parent_run_id", "conversation_id", "cap_scope"}
    shared_db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_list_user_tasks_opens_own_session(monkeypatch):
    """``list_user_tasks`` (the ``list_background_tasks`` tool) opens its own
    session instead of the shared one passed for API compatibility.

    Same root cause as the spawn pre-check.
    """
    from app.services import sub_agent

    scalars = MagicMock()
    scalars.all = MagicMock(return_value=[])
    fake_result = MagicMock()
    fake_result.scalars = MagicMock(return_value=scalars)
    own_session = AsyncMock()
    own_session.execute = AsyncMock(return_value=fake_result)
    factory = _fake_session_factory(monkeypatch, own_session)

    shared_db = MagicMock()
    shared_db.execute = AsyncMock(side_effect=AssertionError("shared session used"))

    tasks = await sub_agent.list_user_tasks(shared_db, str(uuid.uuid4()))

    assert tasks == []
    factory.assert_called_once()
    own_session.execute.assert_awaited_once()
    shared_db.execute.assert_not_called()


# ── A run with neither a parent run nor a conversation ───────────────
# An evaluated turn has no parent run (evaluation mode creates none) and no
# conversation, so its spawns had no scope and escaped the cap. It is given a
# scope of its own, ``cap_scope``, counted when the other two are absent: a
# lookup on the execution payload that only a real database can check.


@pytest.mark.asyncio
async def test_a_run_scoped_by_cap_scope_alone_is_capped(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "max_concurrent_sub_agent_tasks_per_run", 2)
    agent = _make_agent()
    result = build_sub_agent_tools(
        MagicMock(),
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(uuid.uuid4()),
        handover_agents=[agent],
        cap_scope="evaluation_turn:t-1",
    )
    assert result is not None
    spawn = result[1]._handlers["spawn_background_task"]

    count_mock = AsyncMock(return_value=1)
    create_mock = AsyncMock(return_value=_make_fake_sub_task())
    with (
        patch("app.services.sub_agent.count_active_sub_agent_executions", new=count_mock),
        patch("app.services.sub_agent.create_sub_agent_task", new=create_mock),
    ):
        assert "spawned successfully" in await spawn(str(agent.id), "below the cap")
        count_mock.return_value = 2
        assert "Cannot spawn task" in await spawn(str(agent.id), "at the cap")

    assert count_mock.await_args.kwargs == {
        "parent_run_id": None,
        "conversation_id": None,
        "cap_scope": "evaluation_turn:t-1",
    }
    create_mock.assert_awaited_once()
    assert create_mock.await_args.kwargs["cap_scope"] == "evaluation_turn:t-1"
    assert create_mock.await_args.kwargs["run_limit"] == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_cap_scope_bounds_its_in_flight_tasks_on_the_database(monkeypatch):
    from sqlalchemy import update

    import app.models  # noqa: F401 - register every mapper on Base.metadata
    from app.models.agent import Agent
    from app.models.orchestration import OrchestrationExecution
    from app.models.sub_agent_task import SubAgentTask
    from app.models.user import User
    from app.services import sub_agent
    from tests.scratch_database import ALL_TABLES, scratch_database

    # What a spawn sends elsewhere: the worker wake-up, the audit row, the UI event.
    monkeypatch.setattr("app.services.orchestration_engine.notify_orchestration_wake", AsyncMock())
    monkeypatch.setattr("app.services.delegation.log_delegation", AsyncMock())
    monkeypatch.setattr(sub_agent, "publish_task_event", AsyncMock())

    async with scratch_database("cap_scope", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            user = User(email="cap@test.local", full_name="Cap", hashed_password="x")
            session.add(user)
            await session.flush()
            agent = Agent(name="Worker", user_id=user.id)
            session.add(agent)
            await session.commit()

        async def _spawn(scope: str):
            return await sub_agent.create_sub_agent_task(
                None,
                user_id=str(user.id),
                parent_agent_id=str(agent.id),
                target_agent_id=str(agent.id),
                allow_self=True,
                task_description="a part",
                run_limit=2,
                cap_scope=scope,
            )

        first, second = await _spawn("turn-a"), await _spawn("turn-a")
        assert first is not None and second is not None
        assert await _spawn("turn-a") is None
        # Another turn's scope is its own.
        assert await _spawn("turn-b") is not None

        # A task that ends frees its place.
        async with db.sessions() as session:
            await session.execute(
                update(OrchestrationExecution)
                .where(OrchestrationExecution.payload["task_id"].astext == str(first.id))
                .values(state="completed")
            )
            await session.commit()
        assert await _spawn("turn-a") is not None

        # The task keeps its scope: a wait continuation reads it to stay in it.
        async with db.sessions() as session:
            stored = await session.get(SubAgentTask, second.id)
            assert stored.metadata_json["cap_scope"] == "turn-a"

        # A run with a parent run or a conversation is counted by it, whatever
        # cap_scope it is given: chat and assignments are unchanged.
        async with db.sessions() as session:
            session.add_all(
                OrchestrationExecution(adapter="sub_agent", state="running", payload=payload)
                for payload in (
                    {"parent_run_id": "run-1", "cap_scope": "turn-a"},
                    {"parent_run_id": "run-1"},
                    {"conversation_id": "conv-1"},
                )
            )
            await session.commit()
        count = sub_agent.count_active_sub_agent_executions
        assert await count(parent_run_id="run-1", cap_scope="turn-z") == 2
        assert await count(conversation_id="conv-1", cap_scope="turn-a") == 1
