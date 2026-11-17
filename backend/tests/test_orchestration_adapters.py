"""Tests for the orchestration adapter registry + the four built-in adapters."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.services.orchestration_adapters import (
    AdapterContext,
    AdapterNotFoundError,
    OrchestrationAdapter,
    get_adapter,
    list_adapters,
    register_adapter,
)
from app.services.orchestration_adapters import registry as registry_mod
from app.services.orchestration_adapters.assignment import (
    AssignmentAdapter,
    execution_dedup_key,
)
from app.services.orchestration_adapters.base import AdapterResult
from app.services.orchestration_adapters.evaluation import EvaluationAdapter
from app.services.orchestration_adapters.sub_agent import SubAgentAdapter
from tests.agentic_gate_stub import stub_agentic_gate


@pytest.fixture(autouse=True)
def _agentic_gate_available(monkeypatch):
    """The evaluation adapter consults the agentic gate before it runs.

    These tests are about lease loss and terminal-state writes, not licensing, and
    the gate resolves a licence read against a database the unit suite does not
    have -- see ``tests/agentic_gate_stub``. Answer "AI may run" so each test
    reaches the code it is actually about; a test that wants a refusal calls
    ``stub_agentic_gate`` itself and overrides this.
    """
    stub_agentic_gate(monkeypatch)


class _FakeResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeDb:
    def __init__(self, scalar=None, *, open_transaction=False):
        self._scalar = scalar
        self.commits = 0
        self.rollbacks = 0
        # Default False because the janitors are stubbed out in these tests and
        # a stub that issues no statement leaves no transaction open. The test
        # that cares passes True to model a janitor returning while it still
        # holds run locks, which is the case ``periodic`` has to release.
        self._open_transaction = open_transaction

    def in_transaction(self):
        return self._open_transaction

    async def execute(self, *_a, **_k):
        return _FakeResult(self._scalar)

    async def commit(self):
        self.commits += 1
        self._open_transaction = False

    async def rollback(self):
        self.rollbacks += 1
        self._open_transaction = False


class _StubAdapter(OrchestrationAdapter):
    def __init__(self, name):
        self.name = name

    async def execute(self, ctx, execution):
        return AdapterResult.completed()


@pytest.fixture(autouse=True)
def _isolate_registry():
    registry_mod._reset_registry_for_tests()
    yield
    registry_mod._reset_registry_for_tests()


# ── Registry ──────────────────────────────────────────────────────────────────


def test_register_get_list_roundtrip():
    a = _StubAdapter("alpha")
    b = _StubAdapter("beta")
    register_adapter(a)
    register_adapter(b)
    assert get_adapter("alpha") is a
    assert list_adapters() == ["alpha", "beta"]


def test_register_same_instance_is_idempotent():
    a = _StubAdapter("alpha")
    register_adapter(a)
    register_adapter(a)  # no raise
    assert get_adapter("alpha") is a


def test_register_different_instance_same_name_raises():
    register_adapter(_StubAdapter("alpha"))
    with pytest.raises(registry_mod.AdapterAlreadyRegisteredError):
        register_adapter(_StubAdapter("alpha"))


def test_register_empty_name_raises():
    with pytest.raises(ValueError):
        register_adapter(_StubAdapter(""))


def test_missing_adapter_raises_keyerror_subclass():
    with pytest.raises(AdapterNotFoundError):
        get_adapter("nope")
    assert issubclass(AdapterNotFoundError, KeyError)


def test_builtin_registration_registers_all():
    from app.services.orchestration_adapters import register_builtin_adapters

    register_builtin_adapters()
    assert list_adapters() == [
        "assignment",
        "assignment_run_wake_sweep",
        "autonomous_pentest",
        "browser_profile_sessions",
        "evaluation",
        "flow",
        "sandbox_pool_reaper",
        "sandbox_workspace_sweep",
        "scheduled_work_item",
        "sub_agent",
        "video_clip",
        "video_figure_stills",
        "video_music",
        "video_preview",
        "video_render",
        "video_studio_wake_sweep",
        "video_take",
    ]


def test_builtin_registration_is_idempotent():
    # A second call (e.g. the reconciler at startup + the on-demand
    # /runs/cleanup-stale endpoint in the same process) must NOT raise
    # AdapterAlreadyRegisteredError just because it builds fresh instances.
    from app.services.orchestration_adapters import register_builtin_adapters

    register_builtin_adapters()
    register_builtin_adapters()  # no raise
    assert list_adapters() == [
        "assignment",
        "assignment_run_wake_sweep",
        "autonomous_pentest",
        "browser_profile_sessions",
        "evaluation",
        "flow",
        "sandbox_pool_reaper",
        "sandbox_workspace_sweep",
        "scheduled_work_item",
        "sub_agent",
        "video_clip",
        "video_figure_stills",
        "video_music",
        "video_preview",
        "video_render",
        "video_studio_wake_sweep",
        "video_take",
    ]


# ── AssignmentAdapter ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_assignment_adapter_maps_completed(monkeypatch):
    from app.services import assignment_engine

    async def _noop(_run_id, **_kwargs):
        return None

    monkeypatch.setattr(assignment_engine, "execute_assignment_run", _noop)
    run = SimpleNamespace(status="completed", error=None)
    ctx = AdapterContext(db=_FakeDb(scalar=run), worker_id="w1")
    execution = SimpleNamespace(payload={"run_id": str(uuid.uuid4())})

    result = await AssignmentAdapter().execute(ctx, execution)
    assert result.kind == "completed"


@pytest.mark.asyncio
async def test_assignment_adapter_forwards_the_fast_cancel_event(monkeypatch):
    """The reconciler's cancel watcher is only useful if the engine gets the
    event it fires: an emergency stop relies on the agent loop seeing it."""
    import asyncio

    from app.services import assignment_engine

    seen: dict = {}

    async def _capture(_run_id, *, cancel_event=None, **_kwargs):
        # The engine also takes the AI-hold arguments; only the
        # cancel event matters here.
        seen["cancel_event"] = cancel_event

    monkeypatch.setattr(assignment_engine, "execute_assignment_run", _capture)
    run = SimpleNamespace(status="completed", error=None)
    event = asyncio.Event()
    ctx = AdapterContext(db=_FakeDb(scalar=run), worker_id="w1", cancel_event=event)
    execution = SimpleNamespace(payload={"run_id": str(uuid.uuid4())})

    await AssignmentAdapter().execute(ctx, execution)
    assert seen["cancel_event"] is event


@pytest.mark.asyncio
async def test_assignment_adapter_maps_awaiting_human(monkeypatch):
    from app.services import assignment_engine

    async def _noop(_run_id, **_kwargs):
        return None

    monkeypatch.setattr(assignment_engine, "execute_assignment_run", _noop)
    run = SimpleNamespace(status="awaiting_human", error=None)
    ctx = AdapterContext(db=_FakeDb(scalar=run), worker_id="w1")
    execution = SimpleNamespace(payload={"run_id": str(uuid.uuid4())})

    result = await AssignmentAdapter().execute(ctx, execution)
    assert result.kind == "await"
    assert result.adapter_state == "awaiting_human"


@pytest.mark.asyncio
async def test_assignment_adapter_maps_failed_non_retryable(monkeypatch):
    from app.services import assignment_engine

    async def _noop(_run_id, **_kwargs):
        return None

    monkeypatch.setattr(assignment_engine, "execute_assignment_run", _noop)
    run = SimpleNamespace(status="failed", error="kaboom")
    ctx = AdapterContext(db=_FakeDb(scalar=run), worker_id="w1")
    execution = SimpleNamespace(payload={"run_id": str(uuid.uuid4())})

    result = await AssignmentAdapter().execute(ctx, execution)
    assert result.kind == "failed"
    assert result.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize("active_status", ["pending", "delayed", "running"])
async def test_assignment_adapter_force_fails_active_status(monkeypatch, active_status):
    from app.services import assignment_engine

    async def _noop(_run_id, **_kwargs):
        return None

    monkeypatch.setattr(assignment_engine, "execute_assignment_run", _noop)
    run = SimpleNamespace(status=active_status, error=None, completed_at=None, id=uuid.uuid4())
    ctx = AdapterContext(db=_FakeDb(scalar=run), worker_id="w1")
    execution = SimpleNamespace(payload={"run_id": str(uuid.uuid4())})

    result = await AssignmentAdapter().execute(ctx, execution)

    assert result.kind == "failed"
    assert result.retryable is False
    assert run.status == "failed"
    assert run.completed_at is not None
    assert "did not reach a terminal status" in (run.error or "")
    assert active_status in (run.error or "")


@pytest.mark.asyncio
async def test_assignment_adapter_force_fails_unknown_status(monkeypatch):
    from app.services import assignment_engine

    async def _noop(_run_id, **_kwargs):
        return None

    monkeypatch.setattr(assignment_engine, "execute_assignment_run", _noop)
    run = SimpleNamespace(status="totally-bogus", error=None, completed_at=None, id=uuid.uuid4())
    ctx = AdapterContext(db=_FakeDb(scalar=run), worker_id="w1")
    execution = SimpleNamespace(payload={"run_id": str(uuid.uuid4())})

    result = await AssignmentAdapter().execute(ctx, execution)

    assert result.kind == "failed"
    assert result.retryable is False
    assert run.status == "failed"
    assert run.completed_at is not None
    assert "unknown status" in (run.error or "")
    assert "totally-bogus" in (run.error or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["skipped", "cancelled"])
async def test_assignment_adapter_maps_reserved_terminal_statuses(monkeypatch, terminal_status):
    # "skipped" / "cancelled" are reserved terminal statuses — the dispatch
    # unit is done and the run must NOT be force-failed.
    from app.services import assignment_engine

    async def _noop(_run_id, **_kwargs):
        return None

    monkeypatch.setattr(assignment_engine, "execute_assignment_run", _noop)
    run = SimpleNamespace(status=terminal_status, error=None)
    ctx = AdapterContext(db=_FakeDb(scalar=run), worker_id="w1")
    execution = SimpleNamespace(payload={"run_id": str(uuid.uuid4())})

    result = await AssignmentAdapter().execute(ctx, execution)
    assert result.kind == "completed"
    assert run.status == terminal_status


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["completed", "failed", "skipped", "cancelled"])
async def test_assignment_adapter_dead_letter_skips_terminal_runs(terminal_status):
    # on_dead_letter must never overwrite an already-terminal run — including
    # the reserved "skipped" / "cancelled" statuses (not just completed/failed).
    run = SimpleNamespace(status=terminal_status, error=None, completed_at=None)
    ctx = AdapterContext(db=_FakeDb(scalar=run), worker_id="w1")
    execution = SimpleNamespace(payload={"run_id": str(uuid.uuid4())}, last_error="engine gave up")

    await AssignmentAdapter().on_dead_letter(ctx, execution)

    assert run.status == terminal_status
    assert run.error is None


@pytest.mark.asyncio
async def test_assignment_adapter_missing_run_id_fails():
    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(payload={})
    result = await AssignmentAdapter().execute(ctx, execution)
    assert result.kind == "failed"
    assert result.retryable is False


def test_assignment_dedup_key():
    rid = uuid.uuid4()
    assert execution_dedup_key(rid) == f"assignment_run:{rid}"


# ── EvaluationAdapter ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.usefixtures("evaluation_enabled")
async def test_evaluation_adapter_runs_full_evaluation(monkeypatch):
    from app.services import evaluation_engine

    calls = []

    async def _fake_execute(_db, evaluation_id, trigger_reason, _execution_id, **_kw):
        calls.append((evaluation_id, trigger_reason))

    monkeypatch.setattr(evaluation_engine, "execute_evaluation", _fake_execute)
    # A fresh evaluation asks the background gate first; AI may run.
    stub_agentic_gate(monkeypatch)
    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        payload={"evaluation_id": "ev1", "trigger_reason": "schedule"},
    )

    result = await EvaluationAdapter().execute(ctx, execution)
    assert result.kind == "completed"
    assert calls == [("ev1", "schedule")]


@pytest.mark.asyncio
async def test_evaluation_adapter_resume_episode(monkeypatch):
    from app.services import evaluation_continuation

    calls = []

    async def _fake_resume(_db, *, episode_id):
        calls.append(episode_id)

    monkeypatch.setattr(evaluation_continuation, "process_resume_episode", _fake_resume)
    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(payload={"kind": "resume_episode", "episode_id": "ep1"})

    result = await EvaluationAdapter().execute(ctx, execution)
    assert result.kind == "completed"
    assert calls == ["ep1"]


# ── SubAgentAdapter ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sub_agent_adapter_missing_fields_fails():
    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(payload={"task_id": "t1"})  # missing target/desc
    result = await SubAgentAdapter().execute(ctx, execution)
    assert result.kind == "failed"
    assert result.retryable is False


@pytest.mark.asyncio
async def test_sub_agent_adapter_missing_user_id_fails_cleanly():
    # ``user_id`` is required: without it, ``_execute_sub_agent_task`` would
    # raise on ``uuid.UUID("")`` deep in the body. The adapter must instead
    # reject the payload up front with a clean, non-retryable failure.
    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(
        payload={
            "task_id": str(uuid.uuid4()),
            "target_agent_id": str(uuid.uuid4()),
            "task_description": "do the thing",
            # user_id intentionally omitted
        }
    )
    result = await SubAgentAdapter().execute(ctx, execution)
    assert result.kind == "failed"
    assert result.retryable is False


@pytest.mark.asyncio
async def test_sub_agent_adapter_releases_connection_before_task(monkeypatch):
    # The adapter must commit (release) ctx.db's read transaction before the
    # long-running _execute_sub_agent_task, so a file-context read doesn't pin a
    # pooled connection for the whole delegation.
    from app.services import sub_agent as sub_agent_mod
    from app.services.sub_agent import SubAgentTaskOutcome

    seen = {}

    async def _fake_task(**_kwargs):
        # Whatever connection state existed must have been released by now.
        seen["ran"] = True
        return SubAgentTaskOutcome(status="completed")

    monkeypatch.setattr(sub_agent_mod, "_execute_sub_agent_task", _fake_task)

    db = _FakeDb()
    ctx = AdapterContext(db=db, worker_id="w1")
    execution = SimpleNamespace(
        payload={
            "task_id": str(uuid.uuid4()),
            "target_agent_id": str(uuid.uuid4()),
            "task_description": "do the thing",
            "user_id": str(uuid.uuid4()),
        }
    )

    result = await SubAgentAdapter().execute(ctx, execution)
    assert result.kind == "completed"
    assert seen.get("ran") is True
    # ctx.db was committed before the long task started (connection released).
    assert db.commits >= 1


@pytest.mark.asyncio
async def test_sub_agent_adapter_runs_the_task_for_its_user(monkeypatch):
    # Every AI call of a delegated task is for the user who delegated it, the
    # calls with no user of their own included, so that user's LLM provider
    # exclusions hold - and the binding ends with the task.
    from app.llm.provider_access import acting_user_id
    from app.services import sub_agent as sub_agent_mod
    from app.services.sub_agent import SubAgentTaskOutcome

    user_id = str(uuid.uuid4())
    seen = {}

    async def _fake_task(**_kwargs):
        seen["acting"] = acting_user_id()
        return SubAgentTaskOutcome(status="completed")

    monkeypatch.setattr(sub_agent_mod, "_execute_sub_agent_task", _fake_task)
    execution = SimpleNamespace(
        payload={
            "task_id": str(uuid.uuid4()),
            "target_agent_id": str(uuid.uuid4()),
            "task_description": "do the thing",
            "user_id": user_id,
        }
    )

    result = await SubAgentAdapter().execute(AdapterContext(db=_FakeDb(), worker_id="w1"), execution)
    assert result.kind == "completed"
    assert seen["acting"] == user_id
    assert acting_user_id() is None


# ── AutonomousPentestAdapter ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_autonomous_adapter_handle_event_noops_on_terminal(monkeypatch):
    # After an operator stop/delete cancels the execution, a self-resume the prior
    # cycle already scheduled can still fire. Re-arming a terminal row would raise
    # InvalidTransitionError; the adapter must no-op instead so the run stays dead.
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import AutonomousPentestAdapter

    called = {"rearm": False}

    async def _rearm(*_a, **_k):
        called["rearm"] = True

    monkeypatch.setattr(autonomous_mod.engine, "rearm_for_resume", _rearm)

    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(id=uuid.uuid4(), state="cancelled")
    event = SimpleNamespace(event_type="resume")

    out = await AutonomousPentestAdapter().handle_event(ctx, execution, event)
    assert out is None
    assert called["rearm"] is False  # terminal execution never re-armed


@pytest.mark.asyncio
async def test_autonomous_adapter_handle_event_rearms_when_active(monkeypatch):
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import AutonomousPentestAdapter

    called = {"rearm": False}

    async def _rearm(*_a, **_k):
        called["rearm"] = True

    monkeypatch.setattr(autonomous_mod.engine, "rearm_for_resume", _rearm)

    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(id=uuid.uuid4(), state="running")
    event = SimpleNamespace(event_type="resume")

    out = await AutonomousPentestAdapter().handle_event(ctx, execution, event)
    assert out is None
    assert called["rearm"] is True


def _cycle_outcome(decision, **kw):
    """Build a fake ``run_autonomous_cycle`` outcome (SimpleNamespace).

    ``made_progress`` defaults to True so a fake ``continue`` cycle is treated as
    productive (the forward-progress guard never trips) unless a test explicitly
    marks it a no-progress cycle with ``made_progress=False``. ``auth_failed``
    defaults to False so only a test exercising the auth-storm guard opts into
    it (the adapter reads ``outcome.auth_failed`` for the immediate dead-letter).
    """
    return SimpleNamespace(
        decision=decision,
        reason=kw.get("reason"),
        error=kw.get("error"),
        wait_seconds=kw.get("wait_seconds"),
        openaev_already_terminal=kw.get("openaev_already_terminal", False),
        made_progress=kw.get("made_progress", True),
        consumed_directive=kw.get("consumed_directive", False),
        auth_failed=kw.get("auth_failed", False),
    )


def _patch_cycle_sequence(monkeypatch, autonomous_mod, outcomes):
    """Patch ``run_autonomous_cycle`` to return ``outcomes`` in order.

    Records the ``cycle`` arg each call was made with so a test can assert the
    counter advanced across an in-process burst. Also neutralizes the mid-burst
    ``checkpoint_adapter_state`` (a real locked SELECT the fake unit-test DB
    can't satisfy) to a no-op so a ``continue`` burst isn't stopped by a spurious
    lease-loss; a test that wants to assert the checkpoint re-patches it via
    :func:`_capture_checkpoint`.
    """
    calls: list[int] = []
    seq = list(outcomes)

    async def _fake_cycle(*, payload, cycle, cancel_event=None):
        calls.append(cycle)
        # Repeat the last outcome if the sequence is exhausted (an "always
        # continue" run), so a burst-bound test never IndexErrors.
        return seq[len(calls) - 1] if len(calls) <= len(seq) else seq[-1]

    async def _noop_checkpoint(_db, *, execution_id, worker_id, adapter_state):
        return None

    monkeypatch.setattr(autonomous_mod, "run_autonomous_cycle", _fake_cycle)
    monkeypatch.setattr(
        autonomous_mod.engine, "checkpoint_adapter_state", _noop_checkpoint
    )
    return calls


def _capture_outbox(monkeypatch, autonomous_mod):
    """Capture every ``emit_outbox_event`` call the adapter makes."""
    emitted: list[dict] = []

    async def _emit(_db, *, execution_id, event_type, dedup_key, payload, available_at):
        emitted.append(
            {
                "event_type": event_type,
                "dedup_key": dedup_key,
                "payload": payload,
                "available_at": available_at,
            }
        )
        return uuid.uuid4()

    monkeypatch.setattr(autonomous_mod.engine, "emit_outbox_event", _emit)
    return emitted


def _capture_checkpoint(monkeypatch, autonomous_mod):
    """Capture every mid-burst ``checkpoint_adapter_state`` call.

    The adapter checkpoints its progress cursor after each in-burst ``continue``
    cycle (crash recovery). The real engine call does a locked SELECT the fake
    unit-test DB can't satisfy, so stub it to a no-op that records the persisted
    ``adapter_state`` - letting a test assert the cursor advanced without a live
    Postgres.
    """
    saved: list[str] = []

    async def _checkpoint(_db, *, execution_id, worker_id, adapter_state):
        saved.append(adapter_state)

    monkeypatch.setattr(autonomous_mod.engine, "checkpoint_adapter_state", _checkpoint)
    return saved


@pytest.mark.asyncio
async def test_autonomous_burst_chains_continue_cycles_in_process(monkeypatch):
    # A "continue" decision must keep cycling UNDER THE SAME LEASE (in-process),
    # not park + reschedule after every micro-decision. With ample burst budget,
    # three CONTINUE cycles then a COMPLETED must run as ONE execute() call that
    # never parks (no outbox resume emitted).
    from app.services.autonomous_pentest import DECISION_COMPLETED, DECISION_CONTINUE
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )

    calls = _patch_cycle_sequence(
        monkeypatch,
        autonomous_mod,
        [
            _cycle_outcome(DECISION_CONTINUE),
            _cycle_outcome(DECISION_CONTINUE),
            _cycle_outcome(DECISION_CONTINUE),
            _cycle_outcome(DECISION_COMPLETED, reason="done"),
        ],
    )
    emitted = _capture_outbox(monkeypatch, autonomous_mod)
    monkeypatch.setattr(
        autonomous_mod.settings, "autonomous_burst_max_cycles", 10
    )
    monkeypatch.setattr(
        autonomous_mod.settings, "autonomous_burst_budget_seconds", 3600
    )

    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(
        id=uuid.uuid4(), payload={"openaev_run_id": "run-1"}, adapter_state=None
    )
    result = await AutonomousPentestAdapter().execute(ctx, execution)

    assert result.kind == "completed"
    assert calls == [0, 1, 2, 3]  # four cycles, all in ONE execute() call
    assert emitted == []  # never parked between continue cycles


@pytest.mark.asyncio
async def test_autonomous_burst_yields_after_max_cycles(monkeypatch):
    # An "always continue" run must not loop forever in-process: once the
    # per-burst cycle cap is hit it yields back to the reconciler with a
    # short-delay self-resume, carrying the advanced cycle counter.
    from app.services.autonomous_pentest import DECISION_CONTINUE
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )

    calls = _patch_cycle_sequence(
        monkeypatch, autonomous_mod, [_cycle_outcome(DECISION_CONTINUE)]
    )
    emitted = _capture_outbox(monkeypatch, autonomous_mod)
    monkeypatch.setattr(autonomous_mod.settings, "autonomous_burst_max_cycles", 2)
    monkeypatch.setattr(
        autonomous_mod.settings, "autonomous_burst_budget_seconds", 3600
    )

    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        payload={"openaev_run_id": "run-1", "engagement_deadline": "2099-01-01T00:00:00+00:00"},
        adapter_state=None,
    )
    result = await AutonomousPentestAdapter().execute(ctx, execution)

    assert result.kind == "await"
    assert len(calls) == 2  # exactly the burst cap, then yield
    assert result.deadline_at is not None  # engagement deadline re-passed
    import json

    assert json.loads(result.adapter_state)["cycle"] == 2
    assert len(emitted) == 1
    assert emitted[0]["dedup_key"] == f"cycle-resume:{execution.id}:2"


@pytest.mark.asyncio
async def test_autonomous_burst_budget_yields_before_cycle_cap(monkeypatch):
    # The wall-clock burst budget is an independent bound: a zero-second budget
    # forces a yield after the FIRST continue cycle even though the cycle cap is
    # generous (so a single slow cycle can't monopolise a slot indefinitely).
    from app.services.autonomous_pentest import DECISION_CONTINUE
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )

    calls = _patch_cycle_sequence(
        monkeypatch, autonomous_mod, [_cycle_outcome(DECISION_CONTINUE)]
    )
    _capture_outbox(monkeypatch, autonomous_mod)
    monkeypatch.setattr(autonomous_mod.settings, "autonomous_burst_max_cycles", 10)
    monkeypatch.setattr(autonomous_mod.settings, "autonomous_burst_budget_seconds", 0)

    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(
        id=uuid.uuid4(), payload={"openaev_run_id": "run-1"}, adapter_state=None
    )
    result = await AutonomousPentestAdapter().execute(ctx, execution)

    assert result.kind == "await"
    assert len(calls) == 1  # budget exhausted after the first cycle


@pytest.mark.asyncio
async def test_autonomous_stall_guard_stops_run_after_no_progress(monkeypatch):
    # The forward-progress guard: consecutive ``continue`` cycles that changed no
    # OpenAEV state (made_progress False) must stop the run FAILED once the streak
    # hits autonomous_max_no_progress_cycles, instead of looping to MAX_CYCLES. This
    # is the fix for "20+ decisions, 0 attack-path steps, still spinning".
    from app.services.autonomous_pentest import DECISION_CONTINUE
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )

    calls = _patch_cycle_sequence(
        monkeypatch,
        autonomous_mod,
        [_cycle_outcome(DECISION_CONTINUE, made_progress=False)],
    )
    emitted = _capture_outbox(monkeypatch, autonomous_mod)
    # Cycle cap generous so the STALL guard (not the burst bound) is what fires.
    monkeypatch.setattr(autonomous_mod.settings, "autonomous_burst_max_cycles", 50)
    monkeypatch.setattr(
        autonomous_mod.settings, "autonomous_burst_budget_seconds", 3600
    )
    monkeypatch.setattr(
        autonomous_mod.settings, "autonomous_max_no_progress_cycles", 3
    )

    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(
        id=uuid.uuid4(), payload={"openaev_run_id": "run-1"}, adapter_state=None
    )
    result = await AutonomousPentestAdapter().execute(ctx, execution)

    assert result.kind == "failed"
    assert result.retryable is False
    assert "no forward progress" in (result.error or "")
    assert calls == [0, 1, 2]  # stopped exactly at the streak cap, not MAX_CYCLES
    assert emitted == []  # a stopped run parks no self-resume


@pytest.mark.asyncio
async def test_autonomous_stall_guard_resets_on_progress(monkeypatch):
    # A run that keeps AUTHORING (made_progress True) must never trip the guard,
    # however many cycles it runs: progress resets the streak every cycle.
    from app.services.autonomous_pentest import DECISION_CONTINUE
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )

    _patch_cycle_sequence(
        monkeypatch,
        autonomous_mod,
        [_cycle_outcome(DECISION_CONTINUE, made_progress=True)],
    )
    emitted = _capture_outbox(monkeypatch, autonomous_mod)
    monkeypatch.setattr(autonomous_mod.settings, "autonomous_burst_max_cycles", 4)
    monkeypatch.setattr(
        autonomous_mod.settings, "autonomous_burst_budget_seconds", 3600
    )
    monkeypatch.setattr(
        autonomous_mod.settings, "autonomous_max_no_progress_cycles", 3
    )

    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(
        id=uuid.uuid4(), payload={"openaev_run_id": "run-1"}, adapter_state=None
    )
    result = await AutonomousPentestAdapter().execute(ctx, execution)

    # Never fails: it yields at the burst cap (4 cycles) with a reset streak.
    assert result.kind == "await"
    import json

    state = json.loads(result.adapter_state)
    assert state["cycle"] == 4
    assert state["no_progress_streak"] == 0
    assert len(emitted) == 1


@pytest.mark.asyncio
async def test_autonomous_stall_guard_disabled_when_zero(monkeypatch):
    # Setting the cap to 0 disables the guard entirely: an all-no-progress run
    # falls back to the burst bound / MAX_CYCLES behaviour (never fails on stall).
    from app.services.autonomous_pentest import DECISION_CONTINUE
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )

    _patch_cycle_sequence(
        monkeypatch,
        autonomous_mod,
        [_cycle_outcome(DECISION_CONTINUE, made_progress=False)],
    )
    _capture_outbox(monkeypatch, autonomous_mod)
    monkeypatch.setattr(autonomous_mod.settings, "autonomous_burst_max_cycles", 3)
    monkeypatch.setattr(
        autonomous_mod.settings, "autonomous_burst_budget_seconds", 3600
    )
    monkeypatch.setattr(
        autonomous_mod.settings, "autonomous_max_no_progress_cycles", 0
    )

    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(
        id=uuid.uuid4(), payload={"openaev_run_id": "run-1"}, adapter_state=None
    )
    result = await AutonomousPentestAdapter().execute(ctx, execution)

    # Guard off -> yields at the burst cap instead of failing on the stall.
    assert result.kind == "await"


@pytest.mark.asyncio
async def test_autonomous_stall_streak_carried_across_parks(monkeypatch):
    # The stall streak persists across parks: a run resumed with a stored streak
    # in adapter_state continues counting from there (so a run that parks every
    # cycle still converges to the guard rather than resetting each resume). It is
    # also passed into the cycle payload so the prompt can warn the orchestrator.
    from app.services.autonomous_pentest import DECISION_CONTINUE
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )

    seen_streaks: list[int] = []

    async def _fake_cycle(*, payload, cycle, cancel_event=None):
        seen_streaks.append(int(payload.get("no_progress_streak") or 0))
        return _cycle_outcome(DECISION_CONTINUE, made_progress=False)

    monkeypatch.setattr(autonomous_mod, "run_autonomous_cycle", _fake_cycle)
    _capture_outbox(monkeypatch, autonomous_mod)
    # One cycle per burst so each execute() parks; resume feeds the streak back in.
    monkeypatch.setattr(autonomous_mod.settings, "autonomous_burst_max_cycles", 1)
    monkeypatch.setattr(
        autonomous_mod.settings, "autonomous_burst_budget_seconds", 3600
    )
    monkeypatch.setattr(
        autonomous_mod.settings, "autonomous_max_no_progress_cycles", 5
    )

    import json

    adapter = AutonomousPentestAdapter()
    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(
        id=uuid.uuid4(), payload={"openaev_run_id": "run-1"}, adapter_state=None
    )
    # First park: streak 0 -> 1.
    result = await adapter.execute(ctx, execution)
    assert result.kind == "await"
    assert json.loads(result.adapter_state)["no_progress_streak"] == 1
    # Resume with the stored state: the cycle must SEE the carried streak, then
    # advance it to 2.
    execution.adapter_state = result.adapter_state
    result = await adapter.execute(ctx, execution)
    assert json.loads(result.adapter_state)["no_progress_streak"] == 2
    # The cycle on the second execute() was told the run had already stalled once.
    assert seen_streaks == [0, 1]


@pytest.mark.asyncio
async def test_autonomous_waiting_input_schedules_recheck(monkeypatch):
    # A run parked on waiting_input has NO deadline (a human may take any time),
    # so a lost directive resume would strand it forever. The adapter must
    # schedule a periodic self-resume re-check as a belt-and-suspenders.
    from app.services.autonomous_pentest import DECISION_WAITING_INPUT
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )

    _patch_cycle_sequence(
        monkeypatch, autonomous_mod, [_cycle_outcome(DECISION_WAITING_INPUT)]
    )
    emitted = _capture_outbox(monkeypatch, autonomous_mod)
    monkeypatch.setattr(
        autonomous_mod.settings, "autonomous_waiting_input_recheck_seconds", 3600
    )

    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(
        id=uuid.uuid4(), payload={"openaev_run_id": "run-1"}, adapter_state=None
    )
    result = await AutonomousPentestAdapter().execute(ctx, execution)

    assert result.kind == "await"
    assert result.deadline_at is None  # never dead-lettered while awaiting input
    assert len(emitted) == 1
    assert emitted[0]["dedup_key"] == f"waiting-input-recheck:{execution.id}:1"


@pytest.mark.asyncio
async def test_autonomous_waiting_input_recheck_disabled_when_zero(monkeypatch):
    # Setting the re-check interval to 0 disables the periodic self-resume: the
    # run still parks with no deadline, purely directive-driven.
    from app.services.autonomous_pentest import DECISION_WAITING_INPUT
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )

    _patch_cycle_sequence(
        monkeypatch, autonomous_mod, [_cycle_outcome(DECISION_WAITING_INPUT)]
    )
    emitted = _capture_outbox(monkeypatch, autonomous_mod)
    monkeypatch.setattr(
        autonomous_mod.settings, "autonomous_waiting_input_recheck_seconds", 0
    )

    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(
        id=uuid.uuid4(), payload={"openaev_run_id": "run-1"}, adapter_state=None
    )
    result = await AutonomousPentestAdapter().execute(ctx, execution)

    assert result.kind == "await"
    assert result.deadline_at is None
    assert emitted == []


@pytest.mark.asyncio
async def test_autonomous_waiting_input_plan_mode_is_bounded_by_deadline(monkeypatch):
    # CRITICAL: a BUILD (plan / author-scenario) parked on waiting_input MUST
    # re-pass the absolute engagement deadline so a never-answered build is
    # eventually dead-lettered instead of parking forever. A LIVE run keeps NO
    # deadline (tested above) - only a build is bounded.
    from app.services.autonomous_pentest import DECISION_WAITING_INPUT
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )

    _patch_cycle_sequence(
        monkeypatch, autonomous_mod, [_cycle_outcome(DECISION_WAITING_INPUT)]
    )
    _capture_outbox(monkeypatch, autonomous_mod)
    monkeypatch.setattr(
        autonomous_mod.settings, "autonomous_waiting_input_recheck_seconds", 300
    )
    # Cap high enough that the deadline (not the recheck cap) is the bound here.
    monkeypatch.setattr(
        autonomous_mod.settings, "autonomous_max_waiting_input_rechecks", 1000
    )

    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        payload={
            "openaev_run_id": "run-1",
            "plan_mode": True,
            "engagement_deadline": "2099-01-01T00:00:00+00:00",
        },
        adapter_state=None,
    )
    result = await AutonomousPentestAdapter().execute(ctx, execution)

    assert result.kind == "await"
    assert result.deadline_at is not None  # build stays bounded while awaiting input


@pytest.mark.asyncio
async def test_autonomous_waiting_input_recheck_backs_off_and_caps(monkeypatch):
    # Each consecutive UNANSWERED recheck backs the next interval off
    # exponentially from the base up to the max, so a long human wait does not
    # burn a decision cycle every base interval forever.
    import json

    from app.services.autonomous_pentest import DECISION_WAITING_INPUT
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )

    _patch_cycle_sequence(
        monkeypatch, autonomous_mod, [_cycle_outcome(DECISION_WAITING_INPUT)]
    )
    _capture_outbox(monkeypatch, autonomous_mod)
    monkeypatch.setattr(
        autonomous_mod.settings, "autonomous_waiting_input_recheck_seconds", 300
    )
    monkeypatch.setattr(
        autonomous_mod.settings, "autonomous_waiting_input_recheck_max_seconds", 3600
    )

    delays: list[float] = []

    def _fake_next_run_at(*, delay_seconds):
        delays.append(delay_seconds)
        from datetime import UTC, datetime

        return datetime(2099, 1, 1, tzinfo=UTC)

    monkeypatch.setattr(autonomous_mod, "next_run_at", _fake_next_run_at)

    # Resume with an increasing stored unanswered-recheck count; the park bumps
    # it by one, so the scheduled delay is base * 2**(count-1), capped at max.
    for prior in (0, 1, 2, 10):
        ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
        execution = SimpleNamespace(
            id=uuid.uuid4(),
            payload={"openaev_run_id": "run-1"},
            adapter_state=json.dumps({"cycle": 0, "waiting_input_rechecks": prior}),
        )
        await AutonomousPentestAdapter().execute(ctx, execution)

    assert delays == [300, 600, 1200, 3600]  # 300*2^0, *2^1, *2^2, capped


@pytest.mark.asyncio
async def test_autonomous_plan_build_stops_after_unanswered_recheck_cap(monkeypatch):
    # A BUILD parked on an unanswered question must FAIL after the configured
    # number of unanswered rechecks (bounded planning session), with a clear
    # operator-facing reason - not park indefinitely.
    import json

    from app.services.autonomous_pentest import DECISION_WAITING_INPUT
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )

    _patch_cycle_sequence(
        monkeypatch, autonomous_mod, [_cycle_outcome(DECISION_WAITING_INPUT)]
    )
    emitted = _capture_outbox(monkeypatch, autonomous_mod)
    monkeypatch.setattr(
        autonomous_mod.settings, "autonomous_max_waiting_input_rechecks", 2
    )

    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        payload={"openaev_run_id": "run-1", "plan_mode": True},
        # Already 1 unanswered recheck; this park makes it 2 == cap -> fail.
        adapter_state=json.dumps({"cycle": 3, "waiting_input_rechecks": 1}),
    )
    result = await AutonomousPentestAdapter().execute(ctx, execution)

    assert result.kind == "failed"
    assert result.retryable is False
    assert "did not answer" in (result.error or "")
    assert emitted == []  # stopped, so no further recheck scheduled


@pytest.mark.asyncio
async def test_autonomous_waiting_input_rechecks_reset_on_operator_answer(monkeypatch):
    # A waiting_input park that CONSUMED a fresh directive this cycle is a live
    # dialogue, not an unanswered stall: the unanswered-recheck counter resets to
    # zero so a multi-question exchange never trips the build cap.
    import json

    from app.services.autonomous_pentest import DECISION_WAITING_INPUT
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )

    _patch_cycle_sequence(
        monkeypatch,
        autonomous_mod,
        [_cycle_outcome(DECISION_WAITING_INPUT, consumed_directive=True)],
    )
    _capture_outbox(monkeypatch, autonomous_mod)
    monkeypatch.setattr(
        autonomous_mod.settings, "autonomous_waiting_input_recheck_seconds", 300
    )

    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        payload={"openaev_run_id": "run-1"},
        adapter_state=json.dumps({"cycle": 0, "waiting_input_rechecks": 5}),
    )
    result = await AutonomousPentestAdapter().execute(ctx, execution)

    assert result.kind == "await"
    assert json.loads(result.adapter_state)["waiting_input_rechecks"] == 0


@pytest.mark.asyncio
async def test_autonomous_burst_checkpoints_advancing_cursor(monkeypatch):
    # Crash durability: each in-burst continue cycle persists the ADVANCING
    # progress cursor via checkpoint_adapter_state, so a worker crash mid-burst
    # resumes from the last completed cycle instead of rewinding to the last park.
    import json

    from app.services.autonomous_pentest import DECISION_COMPLETED, DECISION_CONTINUE
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )

    _patch_cycle_sequence(
        monkeypatch,
        autonomous_mod,
        [
            _cycle_outcome(DECISION_CONTINUE),
            _cycle_outcome(DECISION_CONTINUE),
            _cycle_outcome(DECISION_COMPLETED, reason="done"),
        ],
    )
    _capture_outbox(monkeypatch, autonomous_mod)
    saved = _capture_checkpoint(monkeypatch, autonomous_mod)
    monkeypatch.setattr(autonomous_mod.settings, "autonomous_burst_max_cycles", 10)
    monkeypatch.setattr(
        autonomous_mod.settings, "autonomous_burst_budget_seconds", 3600
    )

    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(
        id=uuid.uuid4(), payload={"openaev_run_id": "run-1"}, adapter_state=None
    )
    result = await AutonomousPentestAdapter().execute(ctx, execution)

    assert result.kind == "completed"
    # Checkpointed after each of the two continue cycles, with the NEXT cycle
    # number, before the completing cycle (which parks/returns, no checkpoint).
    assert [json.loads(s)["cycle"] for s in saved] == [1, 2]


@pytest.mark.asyncio
async def test_autonomous_burst_stops_when_lease_lost_mid_burst(monkeypatch):
    # The mid-burst checkpoint doubles as an ownership check: if this worker lost
    # the lease out-of-band (operator cancel / steal), the burst must stop NOW
    # rather than author another cycle against a run a second worker now drives.
    from app.services.autonomous_pentest import DECISION_CONTINUE
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )

    calls = _patch_cycle_sequence(
        monkeypatch, autonomous_mod, [_cycle_outcome(DECISION_CONTINUE)]
    )
    _capture_outbox(monkeypatch, autonomous_mod)
    monkeypatch.setattr(autonomous_mod.settings, "autonomous_burst_max_cycles", 10)
    monkeypatch.setattr(
        autonomous_mod.settings, "autonomous_burst_budget_seconds", 3600
    )

    async def _lost(_db, *, execution_id, worker_id, adapter_state):
        raise autonomous_mod.engine.LeaseLostError("stolen")

    monkeypatch.setattr(autonomous_mod.engine, "checkpoint_adapter_state", _lost)

    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(
        id=uuid.uuid4(), payload={"openaev_run_id": "run-1"}, adapter_state=None
    )
    result = await AutonomousPentestAdapter().execute(ctx, execution)

    # One cycle ran, the checkpoint reported lease loss, the burst stopped.
    assert len(calls) == 1
    assert result.kind == "completed"  # _cancelled_result: no-op terminal, lease-safe


@pytest.mark.asyncio
async def test_autonomous_await_finding_parks_with_engagement_deadline(monkeypatch):
    # await_finding must still PARK (never chain in-process): it emits a
    # finding-window self-resume and re-passes the engagement deadline.
    from app.services.autonomous_pentest import DECISION_AWAIT_FINDING
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )

    calls = _patch_cycle_sequence(
        monkeypatch,
        autonomous_mod,
        [_cycle_outcome(DECISION_AWAIT_FINDING, wait_seconds=3600)],
    )
    emitted = _capture_outbox(monkeypatch, autonomous_mod)

    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        payload={
            "openaev_run_id": "run-1",
            "engagement_deadline": "2099-01-01T00:00:00+00:00",
        },
        adapter_state=None,
    )
    result = await AutonomousPentestAdapter().execute(ctx, execution)

    assert result.kind == "await"
    assert len(calls) == 1  # a single cycle, then park
    assert result.deadline_at is not None
    assert len(emitted) == 1
    assert emitted[0]["dedup_key"] == f"cycle-resume:{execution.id}:1"


def test_autonomous_concurrency_limit_reads_settings(monkeypatch):
    # The autonomous adapter exposes a DEDICATED reconciler concurrency budget
    # from settings; 0 / negative falls back to the shared pool (None).
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )

    adapter = AutonomousPentestAdapter()
    monkeypatch.setattr(
        autonomous_mod.settings, "max_concurrent_autonomous_runs", 7
    )
    assert adapter.concurrency_limit == 7
    monkeypatch.setattr(
        autonomous_mod.settings, "max_concurrent_autonomous_runs", 0
    )
    assert adapter.concurrency_limit is None


def test_autonomous_cancel_poll_seconds_reads_settings(monkeypatch):
    # The adapter opts into the reconciler's fast cancel watcher via a positive
    # poll cadence from settings; 0 / negative disables it (None -> heartbeat).
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )

    adapter = AutonomousPentestAdapter()
    monkeypatch.setattr(autonomous_mod.settings, "autonomous_cancel_poll_seconds", 2)
    assert adapter.cancel_poll_seconds == 2.0
    monkeypatch.setattr(autonomous_mod.settings, "autonomous_cancel_poll_seconds", 0)
    assert adapter.cancel_poll_seconds is None


@pytest.mark.asyncio
async def test_autonomous_burst_stops_before_cycle_when_already_cancelled(monkeypatch):
    # A cancel_event that is ALREADY set when execute() starts must stop the
    # burst before running any cycle - no decision authored, no self-resume
    # parked (which could revive the cancelled run).
    from app.services.autonomous_pentest import DECISION_CONTINUE
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )

    calls = _patch_cycle_sequence(
        monkeypatch, autonomous_mod, [_cycle_outcome(DECISION_CONTINUE)]
    )
    emitted = _capture_outbox(monkeypatch, autonomous_mod)

    cancel_event = asyncio.Event()
    cancel_event.set()
    ctx = AdapterContext(db=_FakeDb(), worker_id="w1", cancel_event=cancel_event)
    execution = SimpleNamespace(
        id=uuid.uuid4(), payload={"openaev_run_id": "run-1"}, adapter_state=None
    )
    result = await AutonomousPentestAdapter().execute(ctx, execution)

    assert result.kind == "completed"
    assert calls == []  # never ran a cycle
    assert emitted == []  # never parked a self-resume


@pytest.mark.asyncio
async def test_autonomous_burst_stops_after_cycle_cancelled_midflight(monkeypatch):
    # A cancel that lands DURING a cycle (the fake sets the event when called,
    # simulating the agent loop aborting) must stop the burst right after that
    # cycle: the (now meaningless) "continue" decision is NOT acted on, so no
    # second cycle runs and no self-resume is parked.
    from app.services.autonomous_pentest import DECISION_CONTINUE
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )

    cancel_event = asyncio.Event()
    calls: list[int] = []

    async def _fake_cycle(*, payload, cycle, cancel_event=None):
        calls.append(cycle)
        # Operator cancels while this cycle runs; the agent loop aborts and the
        # reconciler's watcher fires the event before the cycle returns.
        if cancel_event is not None:
            cancel_event.set()
        return _cycle_outcome(DECISION_CONTINUE)

    monkeypatch.setattr(autonomous_mod, "run_autonomous_cycle", _fake_cycle)
    emitted = _capture_outbox(monkeypatch, autonomous_mod)
    monkeypatch.setattr(autonomous_mod.settings, "autonomous_burst_max_cycles", 10)
    monkeypatch.setattr(
        autonomous_mod.settings, "autonomous_burst_budget_seconds", 3600
    )

    ctx = AdapterContext(db=_FakeDb(), worker_id="w1", cancel_event=cancel_event)
    execution = SimpleNamespace(
        id=uuid.uuid4(), payload={"openaev_run_id": "run-1"}, adapter_state=None
    )
    result = await AutonomousPentestAdapter().execute(ctx, execution)

    assert result.kind == "completed"
    assert calls == [0]  # exactly one cycle - the burst did NOT continue
    assert emitted == []  # never parked a self-resume for a cancelled run


@pytest.mark.asyncio
async def test_autonomous_cycle_receives_cancel_event(monkeypatch):
    # execute() must thread its ctx.cancel_event into each cycle so the agent
    # loop (run_agent_chat) can abort between iterations on a cancel.
    from app.services.autonomous_pentest import DECISION_COMPLETED
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )

    seen: dict = {}

    async def _fake_cycle(*, payload, cycle, cancel_event=None):
        seen["cancel_event"] = cancel_event
        return _cycle_outcome(DECISION_COMPLETED, reason="done")

    monkeypatch.setattr(autonomous_mod, "run_autonomous_cycle", _fake_cycle)

    cancel_event = asyncio.Event()
    ctx = AdapterContext(db=_FakeDb(), worker_id="w1", cancel_event=cancel_event)
    execution = SimpleNamespace(
        id=uuid.uuid4(), payload={"openaev_run_id": "run-1"}, adapter_state=None
    )
    await AutonomousPentestAdapter().execute(ctx, execution)

    assert seen["cancel_event"] is cancel_event


@pytest.mark.asyncio
async def test_plan_mode_completed_backstops_planned_status(monkeypatch):
    # In plan/build mode, a "completed" decision must settle the OpenAEV run to PLANNED as a
    # best-effort backstop (the orchestrator is supposed to call update_openaev_run_status(PLANNED)
    # itself, but drops it under the oversized prompt). Without this the run strands pre-PLANNED
    # forever - the reported "stuck, header says Waiting for input" stall. It must post PLANNED
    # (never COMPLETED) for a plan run.
    from app.services.autonomous_pentest import DECISION_COMPLETED
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )

    _patch_cycle_sequence(
        monkeypatch, autonomous_mod, [_cycle_outcome(DECISION_COMPLETED, reason="plan ready")]
    )
    _capture_outbox(monkeypatch, autonomous_mod)

    planned: list[dict] = []
    completed: list[dict] = []

    async def _fake_planned(*, base_url, openaev_run_id, reason, user_id):
        planned.append({"run": openaev_run_id, "reason": reason})

    async def _fake_completed(*, base_url, openaev_run_id, reason, user_id):
        completed.append({"run": openaev_run_id})

    monkeypatch.setattr(autonomous_mod, "_report_run_planned", _fake_planned)
    monkeypatch.setattr(autonomous_mod, "_report_run_completed", _fake_completed)

    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        payload={
            "openaev_run_id": "run-1",
            "plan_mode": True,
            "callback_base_url": "https://aev.example",
            "user_id": "u-1",
        },
        adapter_state=None,
    )
    result = await AutonomousPentestAdapter().execute(ctx, execution)

    assert result.kind == "completed"
    assert planned == [{"run": "run-1", "reason": "plan ready"}]
    assert completed == []  # a plan run settles PLANNED, never COMPLETED


@pytest.mark.asyncio
async def test_plan_mode_completed_skips_backstop_when_already_terminal(monkeypatch):
    # A "completed" that came from the cross-side stop check (OpenAEV already terminal) must NOT
    # post any status - it would race a real CANCELED/FINISHED.
    from app.services.autonomous_pentest import DECISION_COMPLETED
    from app.services.orchestration_adapters import autonomous as autonomous_mod
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )

    _patch_cycle_sequence(
        monkeypatch,
        autonomous_mod,
        [_cycle_outcome(DECISION_COMPLETED, openaev_already_terminal=True)],
    )
    _capture_outbox(monkeypatch, autonomous_mod)

    planned: list[dict] = []

    async def _fake_planned(*, base_url, openaev_run_id, reason, user_id):
        planned.append({"run": openaev_run_id})

    monkeypatch.setattr(autonomous_mod, "_report_run_planned", _fake_planned)

    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        payload={
            "openaev_run_id": "run-1",
            "plan_mode": True,
            "callback_base_url": "https://aev.example",
            "user_id": "u-1",
        },
        adapter_state=None,
    )
    result = await AutonomousPentestAdapter().execute(ctx, execution)

    assert result.kind == "completed"
    assert planned == []  # never override an already-terminal OpenAEV run


def test_engagement_deadline_normalizes_naive_to_utc():
    # A stored value without an offset parses as naive; the engine compares
    # deadline_at against datetime.now(UTC) (aware), so a naive value must be
    # normalized to UTC to avoid a TypeError in sweep_deadlines / fail.
    from datetime import UTC

    from app.services.orchestration_adapters.autonomous import _engagement_deadline

    naive = _engagement_deadline({"engagement_deadline": "2026-08-05T03:00:00"})
    assert naive is not None
    assert naive.tzinfo is not None
    assert naive.utcoffset() == UTC.utcoffset(None)


def test_engagement_deadline_preserves_aware_offset():
    from app.services.orchestration_adapters.autonomous import _engagement_deadline

    aware = _engagement_deadline({"engagement_deadline": "2026-08-05T03:00:00+00:00"})
    assert aware is not None
    assert aware.tzinfo is not None


def test_engagement_deadline_missing_or_unparseable_is_none():
    from app.services.orchestration_adapters.autonomous import _engagement_deadline

    assert _engagement_deadline({}) is None
    assert _engagement_deadline({"engagement_deadline": ""}) is None
    assert _engagement_deadline({"engagement_deadline": "not-a-date"}) is None
