"""The work of a durable execution never outlives its deadline.

``deadline_at`` is fixed when an execution is created (spawn or schedule time +
budget + ``orchestration_grace_seconds``). A worker restart re-runs the
execution from the start under that same deadline, and the re-run used to be
handed its whole budget again: the agent loop believed it had time it did not
have, never wound down, and ``sweep_deadlines`` dead-lettered it mid-work with
nothing recorded ("Deadline exceeded at ...").

A delegated task also ran its agent loop on the chat formula's wall clock
(12 000 s for a 100-iteration budget) under a 30-minute bound, so a delegated
agent waiting on its own delegations was never told its budget was ending and
could not hand off: it was killed mid-wait.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

import pytest

from app.core.config import settings
from app.services import orchestration_engine
from app.services.agent_chat import chat_loop_timeout_within
from app.services.orchestration_engine import (
    MIN_RUNNABLE_SECONDS,
    bound_by_deadline,
    runnable_seconds_left,
)
from app.services.sub_agent import loop_wall_clock_deadline
from app.services.sub_agent_files import CARRIED_FILE_IDS_KEY, RESULT_FILE_IDS_KEY
from tests.agentic_gate_stub import stub_agentic_gate
from tests.test_sub_agent_result_files import (
    _exec_db,
    _FakeSessionCM,
    _incremental_writer,
    _ref,
    _task_and_agent,
)

NOW = datetime(2026, 9, 29, 6, 32, tzinfo=UTC)
GRACE = settings.orchestration_grace_seconds


@pytest.fixture(autouse=True)
def _agentic_ai_available(monkeypatch):
    """The task body asks the background gate first; these tests are
    about how long it may run once it does."""
    stub_agentic_gate(monkeypatch)


def _deadline(*, created_ago: float, budget: float) -> datetime:
    """The deadline an execution created ``created_ago`` seconds before NOW got."""
    return NOW - timedelta(seconds=created_ago) + timedelta(seconds=budget + GRACE)


# -- The shared rule --------------------------------------------------------


def test_no_deadline_leaves_the_budget_as_it_is():
    assert runnable_seconds_left(None, now=NOW) is None
    assert bound_by_deadline(1800, None, now=NOW) == 1800
    assert bound_by_deadline(None, None, now=NOW) is None


def test_a_first_execution_keeps_its_budget():
    deadline = _deadline(created_ago=0, budget=1800)

    assert runnable_seconds_left(deadline, now=NOW) == 1800
    assert bound_by_deadline(1800, deadline, now=NOW) == 1800


def test_a_re_run_gets_what_its_deadline_leaves():
    """Spawned at 06:32 for 1800 s, re-run at 06:52: ten minutes are left."""
    deadline = _deadline(created_ago=1200, budget=1800)

    assert bound_by_deadline(1800, deadline, now=NOW) == 600


def test_a_spent_deadline_times_out_at_once_instead_of_racing_the_sweep():
    deadline = _deadline(created_ago=3600, budget=1800)

    assert runnable_seconds_left(deadline, now=NOW) < 0
    assert bound_by_deadline(1800, deadline, now=NOW) == MIN_RUNNABLE_SECONDS


def test_a_deadline_without_a_budget_bounds_the_work_alone():
    deadline = _deadline(created_ago=0, budget=900)

    assert bound_by_deadline(None, deadline, now=NOW) == 900


# -- The loop's wall clock inside a task ------------------------------------


def test_the_loop_ends_a_reserve_before_the_task_bound():
    assert loop_wall_clock_deadline(None) is None
    assert loop_wall_clock_deadline(1800, now=100.0) == 100.0 + 1800 - 60
    # A short bound keeps a tenth, not a whole minute.
    assert loop_wall_clock_deadline(300, now=100.0) == pytest.approx(100.0 + 270)


def test_the_chat_formula_is_ended_sooner_by_the_caller_bound():
    # 100 iterations: the chat formula alone is 12 000 s.
    assert chat_loop_timeout_within(100, None) == 12_000
    assert chat_loop_timeout_within(100, 1_000.0 + 1_740, now=1_000.0) == 1_740
    # A caller bound further out than the formula changes nothing.
    assert chat_loop_timeout_within(15, 1_000.0 + 99_999, now=1_000.0) == 1_800
    # A spent bound still hands the loop a positive timeout.
    assert chat_loop_timeout_within(100, 1_000.0 - 5, now=1_000.0) == 1


# -- The delegated task -----------------------------------------------------


async def _run_task(
    *,
    timeout_seconds: int | None,
    deadline_at: datetime | None,
    run_chat,
):
    from app.services.sub_agent import _execute_sub_agent_task

    task, agent = _task_and_agent()
    db = _exec_db(task, agent)
    with (
        patch("app.core.database.async_session", lambda: _FakeSessionCM(db)),
        patch("app.services.agent_chat.run_agent_chat", new=AsyncMock(side_effect=run_chat)),
        patch("app.services.sub_agent._update_task_delegation", new=AsyncMock()),
        patch("app.services.sub_agent_files.write_task_result_file_ids", new=_incremental_writer()),
    ):
        outcome = await _execute_sub_agent_task(
            task_id=str(uuid.uuid4()),
            user_id=str(uuid.uuid4()),
            target_agent_id=str(uuid.uuid4()),
            task_description="triage the open alerts",
            timeout_seconds=timeout_seconds,
            deadline_at=deadline_at,
        )
    return outcome, task


@pytest.mark.asyncio
async def test_a_task_hands_its_loop_a_wall_clock_inside_its_bound():
    seen: dict = {}

    async def _chat(*_args, **kwargs):
        seen["left"] = kwargs["wall_clock_deadline"] - time.monotonic()
        return "done", None

    now = datetime.now(UTC)
    outcome, _ = await _run_task(
        timeout_seconds=1800,
        deadline_at=now + timedelta(seconds=1800 + GRACE),
        run_chat=_chat,
    )

    assert outcome.status == "completed"
    assert 1800 - 60 - 5 < seen["left"] <= 1800 - 60


@pytest.mark.asyncio
async def test_a_re_run_task_runs_on_what_its_deadline_leaves():
    """Twenty minutes of a thirty-minute delegation were spent before the
    worker restarted: the re-run's loop is told it has ten, not thirty."""
    seen: dict = {}

    async def _chat(*_args, **kwargs):
        seen["left"] = kwargs["wall_clock_deadline"] - time.monotonic()
        return "done", None

    spawned = datetime.now(UTC) - timedelta(seconds=1200)
    outcome, _ = await _run_task(
        timeout_seconds=1800,
        deadline_at=spawned + timedelta(seconds=1800 + GRACE),
        run_chat=_chat,
    )

    assert outcome.status == "completed"
    assert 600 - 60 - 5 < seen["left"] <= 600 - 60


@pytest.mark.asyncio
async def test_a_task_past_its_deadline_times_out_before_it_starts():
    """Reclaimed inside the grace window, the task has nothing left: it records
    its own timeout without running (or charging) a turn, instead of racing
    the sweep through a setup it cannot use."""
    chat = AsyncMock(return_value=("never", None))
    spawned = datetime.now(UTC) - timedelta(seconds=1800 + 30)
    deadline = spawned + timedelta(seconds=1800 + GRACE)

    outcome, task = await _run_task(
        timeout_seconds=1800, deadline_at=deadline, run_chat=chat
    )

    chat.assert_not_awaited()
    assert outcome.status == "failed"
    assert outcome.error == orchestration_engine.deadline_spent_message(
        "Task", deadline
    )
    assert task.status == "failed"


@pytest.mark.asyncio
async def test_a_setup_that_used_what_was_left_never_starts_the_turn(monkeypatch):
    """Time was left when the task started, none once its agent was loaded:
    the task fails before its turn is charged instead of starting a loop
    that can only time out."""
    chat = AsyncMock(return_value=("never", None))

    async def _slow_budget(*_args, **_kwargs):
        await asyncio.sleep(0.6)
        return 1800

    monkeypatch.setattr(
        "app.services.sub_agent.resolve_chat_sub_agent_timeout", _slow_budget
    )
    deadline = datetime.now(UTC) + timedelta(seconds=GRACE + 0.3)

    outcome, task = await _run_task(timeout_seconds=None, deadline_at=deadline, run_chat=chat)

    chat.assert_not_awaited()
    assert outcome.status == "failed"
    assert outcome.error == orchestration_engine.deadline_spent_message(
        "Task", deadline
    )
    assert task.status == "failed"


@pytest.mark.asyncio
async def test_a_timeout_on_a_bound_the_deadline_cut_is_credited_back_free(monkeypatch):
    """A re-run with a second and a half left times its loop out on that; the
    timeout is the engine's, not the agent's, and its action goes back free,
    exactly as the dead-letter it replaces credited it."""
    receipt = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.quota.refund_agentic_usage_by_receipt", receipt)

    async def _chat(*_args, **_kwargs):
        await asyncio.sleep(30)
        return "never", None

    # The deadline is read on a frozen clock: under a loaded runner the real
    # clock can spend the 1.5 s between building the deadline and the task
    # reading it, and the "nothing left" branch fires instead of the cut bound.
    frozen = datetime.now(UTC)
    monkeypatch.setattr(
        "app.services.sub_agent.deadline_spent",
        lambda deadline_at, **_kw: orchestration_engine.deadline_spent(deadline_at, now=frozen),
    )
    monkeypatch.setattr(
        "app.services.sub_agent.bound_by_deadline",
        lambda budget, deadline_at, **_kw: orchestration_engine.bound_by_deadline(
            budget, deadline_at, now=frozen
        ),
    )
    spawned = frozen - timedelta(seconds=1800 - 1.5)
    outcome, task = await _run_task(
        timeout_seconds=1800,
        deadline_at=spawned + timedelta(seconds=1800 + GRACE),
        run_chat=_chat,
    )

    assert outcome.status == "failed"
    assert re.fullmatch(
        r"Task timed out after [01]s, what its deadline left of its 1800s budget",
        outcome.error,
    )
    assert task.status == "failed"
    receipt.assert_awaited_once()
    assert receipt.await_args.args[0].startswith("sub_agent_task:")


@pytest.mark.asyncio
async def test_an_inner_timeout_of_a_cut_task_is_the_tasks_own(monkeypatch):
    """The deadline cut the bound, but the loop never reached it: a timeout
    raised inside the turn is the task's own, neither free nor the engine's."""
    receipt = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.quota.refund_agentic_usage_by_receipt", receipt)

    async def _chat(*_args, **_kwargs):
        raise TimeoutError

    spawned = datetime.now(UTC) - timedelta(seconds=1200)
    outcome, _ = await _run_task(
        timeout_seconds=1800,
        deadline_at=spawned + timedelta(seconds=1800 + GRACE),
        run_chat=_chat,
    )

    assert outcome.status == "failed"
    assert "what its deadline left" not in outcome.error
    receipt.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_task_that_times_out_on_its_own_budget_keeps_its_charge(monkeypatch):
    """No deadline cut the bound: the timeout is the task's own, charged as a
    timed-out task always was."""
    receipt = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.quota.refund_agentic_usage_by_receipt", receipt)

    async def _chat(*_args, **_kwargs):
        raise TimeoutError

    outcome, _ = await _run_task(timeout_seconds=1800, deadline_at=None, run_chat=_chat)

    assert outcome.status == "failed"
    assert outcome.error == "Task timed out after 1800s"
    receipt.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_turn_timing_its_loop_out_is_a_timed_out_task_not_an_answer():
    """The loop now ends inside the task's bound, so the turn's own
    ``wait_for`` can be what stops it: it hands back an apology for a chat
    reader and ``loop_timeout``, which must fail the task, never complete it
    with the apology as its result."""

    async def _chat(*_args, **kwargs):
        kwargs["error_out"]["error"] = "Agent loop timed out after 1740s"
        kwargs["error_out"]["error_code"] = "loop_timeout"
        return "I'm sorry, this request took too long and was stopped.", None

    outcome, task = await _run_task(timeout_seconds=1800, deadline_at=None, run_chat=_chat)

    assert outcome.status == "failed"
    assert outcome.error == "Task timed out after 1800s"
    assert task.status == "failed"


# -- A re-run of an attempt the engine lost ---------------------------------


async def _run_rerun(
    monkeypatch,
    *,
    status: str,
    run_chat,
    metadata: dict | None = None,
    hold: bool = False,
    delegation: AsyncMock | None = None,
):
    from app.services.sub_agent import _execute_sub_agent_task

    receipt = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.quota.refund_agentic_usage_by_receipt", receipt)
    task, agent = _task_and_agent(metadata)
    task.status = status
    db = _exec_db(task, agent)
    task_id = str(uuid.uuid4())
    with (
        patch("app.core.database.async_session", lambda: _FakeSessionCM(db)),
        patch("app.services.agent_chat.run_agent_chat", new=AsyncMock(side_effect=run_chat)),
        patch(
            "app.services.sub_agent._update_task_delegation",
            new=delegation or AsyncMock(),
        ),
        patch("app.services.sub_agent_files.write_task_result_file_ids", new=_incremental_writer()),
        # A re-run looks up the files its lost attempts produced in a
        # session of its own; keep that read off the sequenced fake session
        # (its answers are counted), the lookup has its own tests.
        patch("app.services.sub_agent_files.load_attachment_rows", new=AsyncMock(return_value=[])),
    ):
        outcome = await _execute_sub_agent_task(
            task_id=task_id,
            user_id=str(uuid.uuid4()),
            target_agent_id=str(uuid.uuid4()),
            task_description="triage the open alerts",
            timeout_seconds=1800,
            hold=hold,
        )
    return outcome, task, task_id, receipt


@pytest.mark.asyncio
async def test_a_re_run_keeps_the_files_of_the_lost_attempt(monkeypatch):
    """The lost attempt wrote a batch before its worker went away; the re-run's
    own record must not drop it: the parent is told of both attempts' files."""
    earlier, later = _ref("q1.csv"), _ref("q2.csv")

    async def _chat(*_args, **kwargs):
        kwargs["file_context"].add(later)
        return "done", None

    outcome, task, _, _ = await _run_rerun(
        monkeypatch,
        status="running",
        run_chat=_chat,
        metadata={RESULT_FILE_IDS_KEY: [earlier.file_id]},
    )

    assert outcome.status == "completed"
    assert task.metadata_json[RESULT_FILE_IDS_KEY] == [earlier.file_id, later.file_id]
    assert task.metadata_json[CARRIED_FILE_IDS_KEY] == [earlier.file_id]


@pytest.mark.asyncio
async def test_a_reclaimed_task_held_waits_as_it_was_before_its_lost_attempt(
    monkeypatch,
):
    """Agentic AI is unavailable when the engine re-runs a task a lost attempt
    left ``running``: the task is held back in ``pending`` with its
    delegation, not shown running for the whole hold."""
    stub_agentic_gate(monkeypatch, "ai_disabled")
    delegation = AsyncMock()
    chat = AsyncMock(return_value=("never", None))

    outcome, task, task_id, _ = await _run_rerun(
        monkeypatch, status="running", run_chat=chat, hold=True, delegation=delegation
    )

    assert outcome.held
    assert task.status == "pending"
    assert task.started_at is None
    delegation.assert_awaited_once_with(ANY, task_id, "pending")
    chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_first_attempt_carries_nothing(monkeypatch):
    outcome, task, _, _ = await _run_rerun(
        monkeypatch, status="pending", run_chat=AsyncMock(return_value=("done", None))
    )

    assert outcome.status == "completed"
    assert CARRIED_FILE_IDS_KEY not in task.metadata_json


@pytest.mark.asyncio
async def test_a_task_whose_timeout_is_disabled_has_no_loop_bound():
    seen: dict = {}

    async def _chat(*_args, **kwargs):
        seen["deadline"] = kwargs["wall_clock_deadline"]
        return "done", None

    outcome, _ = await _run_task(timeout_seconds=0, deadline_at=None, run_chat=_chat)

    assert outcome.status == "completed"
    assert seen["deadline"] is None


# -- The adapters hand the deadline to the work ------------------------------


def _sub_agent_execution(deadline_at: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        payload={
            "task_id": str(uuid.uuid4()),
            "target_agent_id": str(uuid.uuid4()),
            "task_description": "triage the open alerts",
            "user_id": str(uuid.uuid4()),
            "timeout_seconds": 1800,
        },
        deadline_at=deadline_at,
    )


@pytest.mark.asyncio
async def test_the_sub_agent_adapter_hands_the_deadline_to_the_task(monkeypatch):
    from app.services import sub_agent
    from app.services.orchestration_adapters.base import AdapterContext
    from app.services.orchestration_adapters.sub_agent import SubAgentAdapter
    from app.services.sub_agent import SubAgentTaskOutcome
    from tests.test_sub_agent_failure_propagation import _FakeDb

    body = AsyncMock(return_value=SubAgentTaskOutcome(status="completed"))
    monkeypatch.setattr(sub_agent, "_execute_sub_agent_task", body)
    deadline = datetime.now(UTC) + timedelta(seconds=600)

    result = await SubAgentAdapter().execute(
        AdapterContext(db=_FakeDb(), worker_id="w1"), _sub_agent_execution(deadline)
    )

    assert result.kind == "completed"
    assert body.await_args.kwargs["deadline_at"] == deadline


def test_the_assignment_run_bounds_its_own_clock_and_nothing_else():
    """The loop's ``wait_for`` and wall clock take the deadline-bounded value;
    the per-tool cap and the budget spawned workers inherit stay those of the
    whole run, so a re-run never shrinks what its delegations get."""
    from app.services import assignment_engine
    from app.services.orchestration_adapters import assignment as assignment_adapter

    source = inspect.getsource(assignment_engine.execute_assignment_run)
    assert "run_timeout = bound_by_deadline(timeout, deadline_at)" in source
    assert 'loop_kwargs["wall_clock_deadline"] = _loop_started + run_timeout' in source
    assert "timeout=run_timeout," in source
    assert "_loop_started + timeout" not in source
    assert re.search(r"resolve_assignment_tool_timeout\(\s*timeout,", source)
    assert "else int(timeout)" in source

    adapter_source = inspect.getsource(assignment_adapter.AssignmentAdapter.execute)
    assert 'deadline_at=getattr(execution, "deadline_at", None)' in adapter_source


# -- Whose timeout it is ------------------------------------------------------


@pytest.mark.parametrize(
    ("budget", "bound", "cut"),
    [
        (1800, 1800, False),
        (1800, 1800 - GRACE, False),
        (1800, 1800 - GRACE - 1, True),
        (1800, MIN_RUNNABLE_SECONDS, True),
        (None, 600, False),
        (1800, None, False),
    ],
    ids=[
        "first-execution",
        "claim-lag-within-the-grace",
        "lost-more-than-the-grace",
        "setup-spent-the-rest",
        "no-budget",
        "no-bound",
    ],
)
def test_the_deadline_cut_the_budget_only_past_the_grace(budget, bound, cut):
    """Within the grace the bound is the budget less the claim latency, as a
    first execution always ran; past it the engine would have dead-lettered the
    work, so the timeout is the engine's."""
    assert orchestration_engine.deadline_cut_budget(budget, bound) is cut


def test_a_deadline_is_spent_once_the_grace_is_all_that_is_left():
    assert not orchestration_engine.deadline_spent(None, now=NOW)
    assert not orchestration_engine.deadline_spent(
        NOW + timedelta(seconds=GRACE + 1), now=NOW
    )
    assert orchestration_engine.deadline_spent(NOW + timedelta(seconds=GRACE), now=NOW)
    assert orchestration_engine.deadline_spent(NOW + timedelta(seconds=GRACE - 90), now=NOW)


@pytest.mark.parametrize(
    ("elapsed", "cut", "expected"),
    [
        (600.5, True, "DeadlineCutTimeoutError"),
        (30.0, True, "TimeoutError"),
        # The run's own budget fired: a timeout stop (#1695).
        (1800.5, False, "AssignmentTimeoutError"),
    ],
    ids=["the-cut-bound-fired", "a-setup-call-timed-out", "the-budget-fired"],
)
def test_the_loop_timeout_is_the_deadlines_only_when_its_cut_bound_fired(
    elapsed, cut, expected
):
    from app.services import assignment_engine

    error = assignment_engine._loop_timeout_error(
        600.0 if cut else 1800.0, 1800.0, elapsed, 50, deadline_cut=cut
    )

    assert type(error).__name__ == expected
    assert isinstance(error, TimeoutError)
    if expected == "DeadlineCutTimeoutError":
        assert str(error) == (
            "Assignment timed out after 600s (max_iterations=50): what its "
            "deadline left of its 1800s budget"
        )
    else:
        assert "what its deadline left" not in str(error)


def test_an_assignment_timeout_the_deadline_cut_is_credited_free_and_classified():
    """The engine's timeout is free (never a pool slot) and
    ``engine_deadline_exceeded``, as the dead-letter it replaces was; a setup
    that used what the deadline left never starts the loop."""
    from app.services import assignment_engine

    source = inspect.getsource(assignment_engine.execute_assignment_run)
    cut_at = source.index("deadline_cut = deadline_cut_budget(timeout, run_timeout)")
    spent_at = source.index("if deadline_spent(deadline_at):", cut_at)
    loop_at = source.index("run_agent_loop(**loop_kwargs)")
    assert spent_at < loop_at
    # Read again once the last await before the loop is done: the loop is
    # bounded by what the deadline leaves when it starts, not before.
    released_at = source.index("await release_db_connection_before_loop(db)", spent_at)
    last_bound_at = source.rindex("run_timeout = bound_by_deadline(timeout, deadline_at)")
    assert released_at < last_bound_at < loop_at
    assert (
        released_at
        < source.rindex("deadline_cut = deadline_cut_budget(timeout, run_timeout)")
        < loop_at
    )
    assert "raise _loop_timeout_error(" in source
    assert "if isinstance(e, DeadlineCutTimeoutError)" in source
    assert "_free_credit = isinstance(e, DeadlineCutTimeoutError) or (" in source


# -- The assignment run -------------------------------------------------------


def _assignment_engine(monkeypatch, run):
    from tests.test_background_edition_gate import _run_stubs
    from tests.test_quota_store_hold import _check, _engine

    _, assignment, agent = _run_stubs()
    engine, spies = _engine(monkeypatch, run, assignment, agent)
    _check(monkeypatch, down=False)
    return engine, spies


def _assignment_run(*, status: str = "pending", context: dict | None = None):
    from tests.test_background_edition_gate import _run_stubs

    run, _, _ = _run_stubs()
    run.status = status
    run.trigger_payload = {}
    run.trigger_context = dict(context or {"type": "schedule"})
    return run


@pytest.mark.asyncio
async def test_an_assignment_run_past_its_deadline_times_out_before_it_starts(monkeypatch):
    from app.services.failure_causes import FailureCause

    run = _assignment_run()
    engine, spies = _assignment_engine(monkeypatch, run)
    deadline = datetime.now(UTC) + timedelta(seconds=GRACE - 30)

    assert await engine.execute_assignment_run(str(run.id), deadline_at=deadline) is None

    assert run.status == "failed"
    assert run.error == orchestration_engine.deadline_spent_message(
        "Assignment run", deadline
    )
    assert run.failure_cause == FailureCause.ENGINE_DEADLINE_EXCEEDED.value
    assert run.completed_at is not None
    spies.increment.assert_not_awaited()
    spies.executions.assert_not_awaited()
    spies.free.assert_not_awaited()
    spies.pool.assert_not_awaited()
    assert spies.publish.await_args.args[2] == "failed"


def _charged(user_id) -> dict:
    return {"type": "schedule", "quota_metered": True, "quota_metered_user_id": str(user_id)}


@pytest.mark.asyncio
async def test_a_re_run_past_its_deadline_charges_nothing_more(monkeypatch):
    """The lost attempt's charge stands, as its dead-letter left it before;
    the re-run with nothing left adds none."""
    run = _assignment_run(status="running")
    run.trigger_context = _charged(run.user_id)
    engine, spies = _assignment_engine(monkeypatch, run)

    await engine.execute_assignment_run(
        str(run.id), deadline_at=datetime.now(UTC) + timedelta(seconds=GRACE - 30)
    )

    assert run.status == "failed"
    spies.increment.assert_not_awaited()
    spies.pool.assert_not_awaited()


@pytest.mark.parametrize(
    "held_for",
    ["gate", "quota_store"],
    ids=["agentic-ai-unavailable", "quota-store-unreachable"],
)
@pytest.mark.asyncio
async def test_a_reclaimed_run_held_waits_as_it_was_before_its_lost_attempt(
    monkeypatch, held_for
):
    """Held when the engine re-runs it, a run a lost attempt left ``running``
    waits in ``pending``, not shown running for the whole hold."""
    from app.services import ai_hold

    run = _assignment_run(status="running")
    run.trigger_context = _charged(run.user_id)
    engine, spies = _assignment_engine(monkeypatch, run)
    if held_for == "gate":
        stub_agentic_gate(monkeypatch, "ai_disabled")
        code = "ai_disabled"
    else:
        from tests.test_quota_store_hold import _check

        monkeypatch.setattr(settings, "platform_mode", "xtm_one")
        _check(monkeypatch, down=True)
        code = ai_hold.QUOTA_STORE_UNAVAILABLE_CODE

    outcome = await engine.execute_assignment_run(str(run.id), hold=True)

    assert outcome == engine.RunHeld(code)
    assert run.status == "pending"
    assert ai_hold.held_code_of(run.trigger_context) == code
    spies.increment.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_first_attempt_held_keeps_its_status(monkeypatch):
    run = _assignment_run(status="delayed")
    engine, _ = _assignment_engine(monkeypatch, run)
    stub_agentic_gate(monkeypatch, "ai_disabled")

    assert await engine.execute_assignment_run(str(run.id), hold=True) == (
        engine.RunHeld("ai_disabled")
    )
    assert run.status == "delayed"


def test_a_retry_does_not_inherit_the_charge_of_the_run_it_retries():
    """A retry carries the failed run's trigger context, not its charge record:
    abandoned before it charges, it would be credited an action it never
    spent."""
    from app.services import assignment_engine, attempt_charges

    context = {
        "type": "schedule",
        "ctem_vertical": "cti",
        **_charged(uuid.uuid4()),
        attempt_charges.QUOTA_METERED_CHARGE_KEY: str(uuid.uuid4()),
        attempt_charges.QUOTA_METERED_ATTEMPT_KEY: 2,
        attempt_charges.QUOTA_METERED_EPOCH_KEY: 0,
        attempt_charges.QUOTA_ATTEMPT_FENCE_KEY: [0, 2],
    }

    assert attempt_charges.without_charge_state(context) == {
        "type": "schedule",
        "ctem_vertical": "cti",
    }
    assert attempt_charges.without_charge_state(None) is None
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    # The retry also starts without the resumes of the run it retries.
    assert "without_charge_state(run.trigger_context)" in source
    assert "trigger_context=assignment_run_resume.without_resume_state(" in source
