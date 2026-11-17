"""Background tasks inherit the parent run's budget and chat
spawns derive theirs from the target agent's max_iterations.

Without the inheritance a sub-agent task runs under the hardcoded 900s
``_SUB_AGENT_TASK_TIMEOUT`` and the target agent's own ``max_iterations``
resolution — so an assignment configured with ``timeout_seconds=3600`` watches
its fan-out workers die with "Task timed out after 900s" while the parent run
still has 45 minutes of budget left.  A chat spawn on that 900s default kills
a specialist configured for 100 iterations (12 000 s of chat budget) at 15
minutes.

Covered here:

* ``resolve_sub_agent_timeout`` — the single source of truth shared by the
  orchestration ``deadline_at`` (``create_sub_agent_task``) and the
  ``asyncio.wait_for`` bound (``_execute_sub_agent_task``).
* ``resolve_chat_sub_agent_timeout`` / ``chat_loop_timeout_seconds`` — chat
  spawns derive ``max(1800, iterations * 120)`` from the target agent.
* The spawn tools forward the inherited budget to ``create_sub_agent_task``
  (functional, mocked spawn).
* Source contracts (the repo's established pattern for the engine's giant
  ``execute_assignment_run`` body): the engine passes the assignment's
  EFFECTIVE (iteration-floored) timeout + resolved
  ``effective_max_iterations`` to ``build_sub_agent_tools``; the payload
  carries the budget; the adapter forwards it; the worker applies
  ``max_iterations_override``.
"""

import inspect
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import agent_chat, assignment_engine
from app.services import sub_agent as sub_agent_module
from app.services.builtin_tools import build_sub_agent_tools
from app.services.orchestration_adapters.sub_agent import SubAgentAdapter
from app.services.sub_agent import (
    _SUB_AGENT_TASK_TIMEOUT,
    resolve_chat_sub_agent_timeout,
    resolve_sub_agent_timeout,
)

# ── resolve_sub_agent_timeout (pure) ─────────────────────────────────────────


def test_none_keeps_standalone_default():
    """Last-resort when iterations cannot be resolved (lookup failed /
    pre-derivation payload). Chat spawns must not land here: they go
    through ``resolve_chat_sub_agent_timeout`` first."""
    assert resolve_sub_agent_timeout(None) == _SUB_AGENT_TASK_TIMEOUT


def test_positive_value_is_inherited():
    assert resolve_sub_agent_timeout(3600) == 3600


def test_zero_disables_the_per_task_bound():
    """A parent with ``timeout_seconds=0`` (disabled) propagates as no
    per-task deadline — mirroring the assignment-run contract."""
    assert resolve_sub_agent_timeout(0) is None


def test_negative_value_disables_the_per_task_bound():
    assert resolve_sub_agent_timeout(-5) is None


# ── Spawn tools forward the budget (functional) ──────────────────────────────


def _make_agent(name: str = "Worker") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        slug=name.lower(),
        is_company_managed=False,
        description="",
        persona="",
    )


@pytest.mark.asyncio
async def test_spawn_forwards_inherited_budget_to_create_task():
    peer = _make_agent("Peer")
    result = build_sub_agent_tools(
        MagicMock(),
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(uuid.uuid4()),
        handover_agents=[peer],
        parent_run_id=str(uuid.uuid4()),
        task_timeout_seconds=3600,
        task_max_iterations=100,
    )
    assert result is not None
    _, executor = result

    fake_task = SimpleNamespace(id=uuid.uuid4(), status="pending")
    with (
        patch.object(
            sub_agent_module,
            "count_active_sub_agent_executions",
            new=AsyncMock(return_value=0),
        ),
        patch.object(
            sub_agent_module,
            "create_sub_agent_task",
            new=AsyncMock(return_value=fake_task),
        ) as create_mock,
    ):
        spawn = executor._handlers["spawn_background_task"]
        out = await spawn(agent_id=str(peer.id), task="do the thing")

    assert "Background task spawned successfully" in out
    kwargs = create_mock.await_args.kwargs
    assert kwargs["timeout_seconds"] == 3600
    assert kwargs["max_iterations"] == 100


@pytest.mark.asyncio
async def test_spawn_without_budget_keeps_defaults():
    """Chat spawns (no assignment scope) pass None at the tool layer —
    ``create_sub_agent_task`` then derives the wall-clock from the target
    agent's ``max_iterations``."""
    peer = _make_agent("Peer")
    result = build_sub_agent_tools(
        MagicMock(),
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(uuid.uuid4()),
        handover_agents=[peer],
        parent_run_id=str(uuid.uuid4()),
    )
    assert result is not None
    _, executor = result

    fake_task = SimpleNamespace(id=uuid.uuid4(), status="pending")
    with (
        patch.object(
            sub_agent_module,
            "count_active_sub_agent_executions",
            new=AsyncMock(return_value=0),
        ),
        patch.object(
            sub_agent_module,
            "create_sub_agent_task",
            new=AsyncMock(return_value=fake_task),
        ) as create_mock,
    ):
        spawn = executor._handlers["spawn_background_task"]
        await spawn(agent_id=str(peer.id), task="do the thing")

    kwargs = create_mock.await_args.kwargs
    assert kwargs["timeout_seconds"] is None
    assert kwargs["max_iterations"] is None


# ── Source contracts ─────────────────────────────────────────────────────────


def test_engine_passes_assignment_budget_to_spawn_tools():
    """execute_assignment_run must hand the assignment's EFFECTIVE budget to
    build_sub_agent_tools so spawned workers inherit it — the FLOORED
    wall-clock timeout (not the raw stored value: a max_iterations=100 /
    timeout_seconds=300 parent runs for 2000 s and its workers must not die
    at 300 s), with the raw value only for the disabled (<= 0 -> ``timeout is
    None``) case so the worker resolves it to "no per-task deadline" instead
    of the standalone 900 s default."""
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    call = source.split("build_sub_agent_tools(", 1)[1].split(
        "composite.add_tools", 1
    )[0]
    assert "task_timeout_seconds=(" in call
    assert "assignment.timeout_seconds" in call
    assert "if timeout is None" in call
    assert "else int(timeout)" in call
    assert "task_max_iterations=effective_max_iterations" in call


def test_engine_floors_timeout_before_spawn_tools():
    """The floored effective timeout must be computed BEFORE the
    background-task block so spawned workers inherit the floored value —
    not recomputed after, next to the agentic loop."""
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    floor_idx = source.index("_apply_iteration_timeout_floor(")
    spawn_idx = source.index("build_sub_agent_tools(")
    assert floor_idx < spawn_idx


def test_engine_resolves_max_iterations_before_spawn_tools():
    """effective_max_iterations must be resolved BEFORE the background-task
    block (it used to live after, next to the agentic loop). The resolution
    now goes through the shared ``resolve_run_max_iterations`` helper (which
    wraps ``resolve_effective_max_iterations`` with the platform-default
    lookup) so the deadline-floor call sites share the same value."""
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    resolve_idx = source.index("resolve_run_max_iterations(")
    spawn_idx = source.index("build_sub_agent_tools(")
    assert resolve_idx < spawn_idx


def test_create_task_stores_raw_budget_in_payload():
    """The payload carries the RAW inherited values so the worker re-resolves
    them with the shared helper and the two bounds can't drift."""
    source = inspect.getsource(sub_agent_module.create_sub_agent_task)
    assert '"timeout_seconds": timeout_seconds' in source
    assert '"max_iterations": max_iterations' in source
    assert "resolve_sub_agent_timeout(timeout_seconds)" in source


def test_create_task_disabled_timeout_sets_no_deadline():
    """timeout 0/negative (parent timeout disabled) must NOT set an
    orchestration deadline — sweep_deadlines would dead-letter the task."""
    source = inspect.getsource(sub_agent_module.create_sub_agent_task)
    segment = source.split("_effective_timeout = ", 1)[1].split(
        "await create_execution", 1
    )[0]
    assert "None" in segment and "deadline_at" in segment


def test_adapter_forwards_budget_from_payload():
    source = inspect.getsource(SubAgentAdapter.execute)
    assert 'timeout_seconds=payload.get("timeout_seconds")' in source
    assert 'max_iterations=payload.get("max_iterations")' in source


def test_worker_applies_inherited_budget():
    """_execute_sub_agent_task must bound run_agent_chat with the resolved
    inherited timeout and pin the loop cap via max_iterations_override."""
    source = inspect.getsource(sub_agent_module._execute_sub_agent_task)
    assert "resolve_sub_agent_timeout(timeout_seconds)" in source
    assert "timeout=effective_timeout" in source
    assert "max_iterations_override=max_iterations" in source
    # The hardcoded constant must no longer bound the agentic loop directly.
    assert "timeout=_SUB_AGENT_TASK_TIMEOUT" not in source


def test_create_task_derives_chat_timeout_from_target_agent():
    """Chat spawns (timeout_seconds=None) must call the chat resolver
    before the shared helper, so a 100-iteration specialist is not
    pinned to the historical 900 s default at deadline_at time."""
    source = inspect.getsource(sub_agent_module.create_sub_agent_task)
    segment = source.split("_effective_timeout = ", 1)[0]
    assert "if timeout_seconds is None:" in segment
    assert "resolve_chat_sub_agent_timeout(" in segment
    assert "resolve_sub_agent_timeout(timeout_seconds)" in source


def test_worker_derives_chat_timeout_when_payload_is_none():
    """_execute_sub_agent_task must re-derive a None payload (chat /
    pre-fix in-flight) from the loaded target agent, not fall through
    to the 900 s last-resort before the agent is known."""
    source = inspect.getsource(sub_agent_module._execute_sub_agent_task)
    assert "resolve_chat_sub_agent_timeout(" in source
    assert "if timeout_seconds is None:" in source
    # The sync helper must not be called with None (that is the 900 s
    # last-resort). Assignment budgets still go through it.
    assert "else resolve_sub_agent_timeout(timeout_seconds)" in source


def test_agent_chat_uses_shared_chat_loop_timeout_helper():
    """The parent chat turn and chat-spawned workers must share one
    formula so they cannot drift."""
    source = inspect.getsource(agent_chat.run_agent_chat)
    assert "chat_loop_timeout_within(" in source
    assert "chat_loop_timeout_seconds(" in inspect.getsource(
        agent_chat.chat_loop_timeout_within
    )
    assert "chat_loop_max_iterations(" in source
    assert "max(600, effective_max_iterations * 120)" not in source


def test_chat_override_is_clamped_to_platform_ceiling():
    """The explicit caller pin must go through clamp_max_iterations so it
    can never exceed MAX_ITERATIONS_CEILING (a raw ``max(1, ...)`` would let
    a future caller bypass the platform safety ceiling)."""
    source = inspect.getsource(agent_chat.run_agent_chat)
    branch = source.split("elif max_iterations_override is not None:", 1)[1]
    branch = branch.split("else:", 1)[0]
    assert "clamp_max_iterations(" in branch


# ── resolve_chat_sub_agent_timeout (functional) ──────────────────────────────


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


@pytest.fixture
def cap_is_the_reach(monkeypatch):
    """Which iterations the budget is derived from, with no extension."""
    from app.core import config as config_module

    monkeypatch.setattr(config_module.settings, "agent_loop_auto_extend_iterations", False)


@pytest.mark.asyncio
async def test_chat_timeout_is_sized_for_the_iterations_a_task_may_reach(monkeypatch):
    """A task's turn keeps going in blocks while it makes progress."""
    from app.core import config as config_module
    from app.models.general_assistant_config import MAX_ITERATIONS_CEILING

    monkeypatch.setattr(config_module.settings, "agent_loop_auto_extend_iterations", True)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarResult(25))
    timeout = await resolve_chat_sub_agent_timeout(db, str(uuid.uuid4()))
    assert timeout == MAX_ITERATIONS_CEILING * 120


@pytest.mark.asyncio
async def test_an_evaluation_chain_task_is_sized_for_its_cap_alone(monkeypatch):
    """A task in an evaluation's chain is never extended: its budget is its cap's."""
    from app.core import config as config_module

    monkeypatch.setattr(config_module.settings, "agent_loop_auto_extend_iterations", True)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarResult(25))
    timeout = await resolve_chat_sub_agent_timeout(
        db, str(uuid.uuid4()), evaluation_chain=True
    )
    assert timeout == 25 * 120


def test_both_task_paths_size_an_evaluation_chain_task_on_its_cap():
    """The create path reads the chain from the task's metadata, the execute path from the row."""
    create_source = inspect.getsource(sub_agent_module.create_sub_agent_task)
    assert "evaluation_chain=bool((extra_metadata or {}).get(EVALUATION_CHAIN_KEY))" in create_source
    execute_source = inspect.getsource(sub_agent_module._execute_sub_agent_task)
    assert "agent=agent, evaluation_chain=_evaluation_chain" in execute_source


@pytest.mark.asyncio
@pytest.mark.usefixtures("cap_is_the_reach")
async def test_chat_timeout_uses_target_agent_iterations():
    """A 100-iteration specialist gets 12 000 s, not 900 s."""
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarResult(100))
    timeout = await resolve_chat_sub_agent_timeout(db, str(uuid.uuid4()))
    assert timeout == 12_000
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.usefixtures("cap_is_the_reach")
async def test_chat_timeout_floors_five_iteration_specialist():
    """A 5-iteration specialist still gets the 1800 s floor (15 * 120)."""
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarResult(5))
    timeout = await resolve_chat_sub_agent_timeout(db, str(uuid.uuid4()))
    assert timeout == 1800
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.usefixtures("cap_is_the_reach")
async def test_chat_timeout_falls_back_to_platform_default():
    """When the target agent has no override, use the GA platform default."""
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_ScalarResult(None), _ScalarResult(50)])
    timeout = await resolve_chat_sub_agent_timeout(db, str(uuid.uuid4()))
    assert timeout == 50 * 120  # 6 000 s
    assert db.execute.await_count == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("cap_is_the_reach")
async def test_chat_timeout_uses_already_loaded_agent():
    """The execute path passes the loaded agent so we don't re-query it."""
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=AssertionError("must not hit the DB for the agent"))
    agent = SimpleNamespace(max_iterations=100)
    timeout = await resolve_chat_sub_agent_timeout(
        db, str(uuid.uuid4()), agent=agent
    )
    assert timeout == 12_000
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("cap_is_the_reach")
async def test_chat_timeout_loaded_agent_without_override_queries_platform():
    """An already-loaded agent with max_iterations=None still needs the
    platform default — don't skip the GA lookup just because a row was
    passed in."""
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ScalarResult(25))
    agent = SimpleNamespace(max_iterations=None)
    timeout = await resolve_chat_sub_agent_timeout(
        db, str(uuid.uuid4()), agent=agent
    )
    assert timeout == 3_000
    db.execute.assert_awaited_once()
