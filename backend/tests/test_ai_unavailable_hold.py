"""Background AI work is HELD while agentic AI is unavailable.

Work that comes due while agentic AI cannot run - Community Edition, or the AI
switch off - has not started. It is held, not failed: its orchestration
execution goes back to wait through ``orchestration_engine.defer`` without
consuming an attempt, is due again after a capped backoff, and runs once AI is
available again; past ``AI_UNAVAILABLE_HOLD_MAX_SECONDS`` it is failed with the
reason. Four kinds: a delayed / queued assignment run, a delegated task not yet
picked up, the pending runs of an evaluation, and the resume of a paused (HITL)
run answered meanwhile. Enterprise Edition with AI on never meets any of it.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api import deps
from app.core.config import settings
from app.services import ai_hold, orchestration_engine
from app.services.failure_causes import FailureCause, FailureDetermination
from tests.test_background_edition_gate import (
    AI_SWITCHED_OFF,
    COMMUNITY,
    ENTERPRISE,
    _patch_task_collaborators,
    _pin,
    _pin_unreadable,
    _QuotaReached,
    _run_stubs,
    _Session,
    _task_row,
    _TaskSession,
)

LICENCE_HELD = ai_hold.held_adapter_state("license_required")


def _execution(*, deadline_in: float | None = 420.0, payload: dict | None = None):
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        state="running",
        payload=payload if payload is not None else {"run_id": str(uuid.uuid4())},
        attempt_count=1,
        max_attempts=3,
        run_at=now,
        deadline_at=None if deadline_in is None else now + timedelta(seconds=deadline_in),
        adapter_state=None,
        last_error=None,
        lease_owner="w1",
        lease_expires_at=now + timedelta(seconds=60),
        updated_at=now,
    )


def _held_for(seconds: float, *, count: int = 3) -> dict:
    since = datetime.now(UTC) - timedelta(seconds=seconds)
    return {
        "run_id": str(uuid.uuid4()),
        orchestration_engine.HOLD_PAYLOAD_KEY: {
            "since": since.isoformat(),
            "count": count,
            "reason": LICENCE_HELD,
        },
    }


def _lock(monkeypatch, execution) -> None:
    monkeypatch.setattr(
        orchestration_engine, "_locked_get", AsyncMock(return_value=execution)
    )


# ── The engine's defer step ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_deferral_gives_the_attempt_back_and_moves_the_deadline(monkeypatch):
    """Not an attempt: the claim's increment is given back, so a hold outlasts
    ``max_attempts`` claims. The deadline moves by the time spent waiting, so a
    held run that starts later keeps its whole timeout."""
    execution = _execution()
    deadline = execution.deadline_at
    _lock(monkeypatch, execution)

    before = datetime.now(UTC)
    await orchestration_engine.defer(
        _Session(),
        execution_id=execution.id,
        worker_id="w1",
        reason="Held: AI is not available",
        delay_seconds=60,
        adapter_state=LICENCE_HELD,
    )

    assert execution.state == "pending"
    assert execution.attempt_count == 0
    assert execution.lease_owner is None and execution.lease_expires_at is None
    assert execution.run_at >= before + timedelta(seconds=60)
    assert execution.deadline_at == deadline + timedelta(seconds=60)
    assert execution.adapter_state == LICENCE_HELD
    assert execution.last_error == "Held: AI is not available"
    record = orchestration_engine.hold_record(execution)
    assert record["count"] == 1 and record["reason"] == LICENCE_HELD
    since = orchestration_engine.hold_started_at(execution)

    # Claimed again and held again: the attempt is given back again, the
    # hold keeps its start.
    execution.state, execution.attempt_count, execution.lease_owner = "running", 1, "w2"
    await orchestration_engine.defer(
        _Session(),
        execution_id=execution.id,
        worker_id="w2",
        reason="Held",
        delay_seconds=120,
        adapter_state=LICENCE_HELD,
    )
    assert execution.attempt_count == 0
    assert orchestration_engine.hold_record(execution)["count"] == 2
    assert orchestration_engine.hold_started_at(execution) == since


@pytest.mark.asyncio
async def test_a_deferral_without_a_deadline_stays_without_one(monkeypatch):
    execution = _execution(deadline_in=None)
    _lock(monkeypatch, execution)
    await orchestration_engine.defer(
        _Session(),
        execution_id=execution.id,
        worker_id="w1",
        reason="Held",
        delay_seconds=30,
        adapter_state=LICENCE_HELD,
    )
    assert execution.deadline_at is None


@pytest.mark.asyncio
async def test_a_deferral_needs_the_lease(monkeypatch):
    execution = _execution()
    execution.lease_owner = "someone-else"
    _lock(monkeypatch, execution)
    with pytest.raises(orchestration_engine.LeaseLostError):
        await orchestration_engine.defer(
            _Session(),
            execution_id=execution.id,
            worker_id="w1",
            reason="Held",
            delay_seconds=30,
            adapter_state=LICENCE_HELD,
        )
    assert execution.state == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["complete", "await", "fail_final"])
async def test_the_claim_that_runs_ends_the_hold(monkeypatch, outcome):
    execution = _execution(payload=_held_for(90))
    _lock(monkeypatch, execution)
    if outcome == "complete":
        await orchestration_engine.complete(
            _Session(), execution_id=execution.id, worker_id="w1"
        )
    elif outcome == "await":
        await orchestration_engine.await_event(
            _Session(),
            execution_id=execution.id,
            worker_id="w1",
            adapter_state="awaiting_human",
        )
    else:
        await orchestration_engine.fail(
            _Session(),
            execution_id=execution.id,
            worker_id="w1",
            error="boom",
            retryable=False,
        )
    assert orchestration_engine.hold_record(execution) is None
    assert "run_id" in execution.payload


@pytest.mark.asyncio
async def test_a_transient_retry_keeps_the_hold_counting(monkeypatch):
    """A read failure mid-hold is retried and the bound keeps running."""
    execution = _execution(payload=_held_for(90))
    execution.adapter_state = LICENCE_HELD
    since = orchestration_engine.hold_started_at(execution)
    _lock(monkeypatch, execution)
    await orchestration_engine.fail(
        _Session(),
        execution_id=execution.id,
        worker_id="w1",
        error="not confirmed",
        retryable=True,
        backoff_seconds=30,
    )
    assert execution.state == "pending"
    assert orchestration_engine.hold_started_at(execution) == since
    # The adapter state is cleared by the retry: every held-state reader
    # (the stale sweep, the evaluation API) reads ``payload['hold']`` instead.
    assert execution.adapter_state is None


@pytest.mark.asyncio
async def test_a_held_task_is_listed_with_its_code_on_the_background_tasks_screen():
    """The Background Tasks screen reads ``list_background_tasks``: a held
    pending task carries the refusal code the ``Held`` badge takes, and a task
    that is not held carries no new key at all."""
    from app.services import sub_agent

    def _task(status, meta):
        return SimpleNamespace(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            parent_agent_id=uuid.uuid4(),
            target_agent_id=uuid.uuid4(),
            task_description="Summarise",
            status=status,
            result=None,
            error=None,
            conversation_id=None,
            created_at=None,
            started_at=None,
            completed_at=None,
            metadata_json=meta,
        )

    held = _task("pending", ai_hold.with_hold_marker({"assignment_run_id": "x"}, "ai_disabled"))
    plain = _task("pending", {"assignment_run_id": "y"})
    listed = await sub_agent.list_background_tasks(
        _Session([[held, plain], []]), requester_id=str(uuid.uuid4())
    )
    assert listed[0]["held_reason"] == "ai_disabled"
    assert "held_reason" not in listed[1]
    assert ai_hold.held_code_of(None) is None


@pytest.mark.asyncio
async def test_an_execution_never_held_keeps_its_payload_untouched(monkeypatch):
    """Enterprise Edition with AI on: the payload object is not even replaced."""
    execution = _execution()
    payload = execution.payload
    _lock(monkeypatch, execution)
    await orchestration_engine.complete(
        _Session(), execution_id=execution.id, worker_id="w1"
    )
    assert execution.payload is payload


@pytest.mark.asyncio
async def test_the_reconciler_applies_a_deferral_through_the_defer_step(monkeypatch):
    from app.services import orchestration_reconciler
    from app.services.orchestration_adapters.base import AdapterResult

    defer = AsyncMock()
    fail = AsyncMock()
    monkeypatch.setattr(orchestration_engine, "defer", defer)
    monkeypatch.setattr(orchestration_engine, "fail", fail)
    dead_letter = AsyncMock()
    monkeypatch.setattr(orchestration_reconciler, "run_dead_letter", dead_letter)
    execution_id = uuid.uuid4()

    session = _Session()
    await orchestration_reconciler._apply_result(
        session,
        execution_id,
        "w1",
        AdapterResult.defer("Held", delay_seconds=60.0, adapter_state=LICENCE_HELD),
    )

    fail.assert_not_awaited()
    dead_letter.assert_not_awaited()
    assert session.commits == 1
    assert defer.await_args.kwargs == {
        "execution_id": execution_id,
        "worker_id": "w1",
        "reason": "Held",
        "delay_seconds": 60.0,
        "adapter_state": LICENCE_HELD,
        "payload_update": None,
        # Behind what is due before then, as every hold waits (``keep`` and ``head`` are asked for).
        "place": "after",
    }


# ── The hold policy ─────────────────────────────────────────────────────────


def test_the_backoff_doubles_up_to_its_cap():
    delays = [ai_hold.hold_delay_seconds(n) for n in range(8)]
    assert delays == [30.0, 60.0, 120.0, 240.0, 300.0, 300.0, 300.0, 300.0]
    assert ai_hold.hold_delay_seconds(10_000) == ai_hold.HOLD_MAX_DELAY_SECONDS


def test_the_hold_is_bounded_by_the_setting(monkeypatch):
    monkeypatch.setattr(settings, "ai_unavailable_hold_max_seconds", 3600)
    assert ai_hold.hold_budget_left(_execution())
    assert ai_hold.hold_budget_left(_execution(payload=_held_for(3000)))
    assert not ai_hold.hold_budget_left(_execution(payload=_held_for(3700)))
    # 0 turns holding off: the work is failed at its start.
    monkeypatch.setattr(settings, "ai_unavailable_hold_max_seconds", 0)
    assert not ai_hold.hold_budget_left(_execution())


def test_the_default_bound_is_a_week():
    assert settings.ai_unavailable_hold_max_seconds == 7 * 24 * 3600


def test_the_held_state_is_worded_as_waiting_never_as_an_error():
    assert ai_hold.held_message("license_required").startswith(
        "Held: AI is not available on this platform (Community Edition)"
    )
    assert "(AI turned off, on the platform" in ai_hold.held_message("ai_disabled")


def test_the_hold_result_names_the_reason_and_the_next_delay():
    result = ai_hold.hold_result(_execution(payload=_held_for(10, count=2)), "ai_disabled")
    assert result.kind == "defer"
    assert result.adapter_state == "held:ai_disabled"
    assert result.backoff_seconds == 120.0
    assert result.error == ai_hold.held_message("ai_disabled")


def test_a_marker_keeps_the_first_holds_start():
    first = ai_hold.with_hold_marker({"quota_metered": True}, "license_required")
    again = ai_hold.with_hold_marker(first, "ai_disabled")
    assert again[ai_hold.HOLD_MARKER_KEY]["since"] == first[ai_hold.HOLD_MARKER_KEY]["since"]
    assert again["quota_metered"] is True
    assert ai_hold.without_hold_marker(again) == {"quota_metered": True}
    assert ai_hold.without_hold_marker({"quota_metered": True}) is None
    assert ai_hold.without_hold_marker(None) is None


# ── Kind 1: a delayed or queued assignment run ──────────────────────────────


def _quota_and_telemetry(monkeypatch):
    from app.services import assignment_engine

    quota = AsyncMock()
    monkeypatch.setattr("app.services.quota.check_agentic_quota", quota)
    monkeypatch.setattr("app.services.quota.increment_agentic_usage", quota)
    executions = AsyncMock()
    monkeypatch.setattr(assignment_engine, "record_agentic_execution", executions)
    publish = AsyncMock()
    monkeypatch.setattr(assignment_engine, "_publish_run_status", publish)
    return quota, executions, publish


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("edition", "code"),
    [(COMMUNITY, "license_required"), (AI_SWITCHED_OFF, "ai_disabled")],
)
@pytest.mark.parametrize("status", ["pending", "delayed"])
async def test_a_queued_run_is_held_uncharged_and_untouched(monkeypatch, edition, code, status):
    from app.services import assignment_engine

    _pin(monkeypatch, **edition)
    run, assignment, agent = _run_stubs()
    run.status = status
    session = _Session([run, assignment, agent])
    monkeypatch.setattr(assignment_engine, "async_session", lambda: session)
    quota, executions, publish = _quota_and_telemetry(monkeypatch)

    outcome = await assignment_engine.execute_assignment_run(str(run.id), hold=True)

    assert outcome == assignment_engine.RunHeld(code)
    assert run.status == status
    assert run.error is None and run.failure_cause is None and run.completed_at is None
    assert run.started_at is None
    assert ai_hold.held_reason_of(run.trigger_context) == ai_hold.held_message(code)
    assert session.commits == 1
    quota.assert_not_awaited()
    executions.assert_not_awaited()
    publish.assert_not_awaited()
    assert assignment.enabled is True

    # Asked again while still held: nothing is written.
    session = _Session([run, assignment, agent])
    monkeypatch.setattr(assignment_engine, "async_session", lambda: session)
    assert await assignment_engine.execute_assignment_run(str(run.id), hold=True) == (
        assignment_engine.RunHeld(code)
    )
    assert session.commits == 0


@pytest.mark.asyncio
async def test_the_assignment_adapter_holds_the_execution(monkeypatch):
    """Read before the status mapping: a held paused run must not be re-paused
    waiting for a resume that already arrived."""
    from app.services import assignment_engine
    from app.services.orchestration_adapters import assignment as assignment_adapter
    from app.services.orchestration_adapters.base import AdapterContext

    execute = AsyncMock(return_value=assignment_engine.RunHeld("license_required"))
    monkeypatch.setattr(assignment_engine, "execute_assignment_run", execute)
    monkeypatch.setattr(assignment_adapter, "_bind_memory_chain", AsyncMock(return_value=None))
    execution = _execution()

    result = await assignment_adapter.AssignmentAdapter().execute(
        AdapterContext(db=_Session(strict=True), worker_id="w1"), execution
    )

    # Never held: nothing was read before the run, which reads the gate itself.
    # The fast cancel event rides along (emergency stop); this context has none.
    assert execute.await_args.kwargs == {
        "hold": True,
        "verdict": None,
        "cancel_event": None,
        "deadline_at": execution.deadline_at,
        "attempt": execution.attempt_count,
        "taken_back_on_its_word": False,
    }
    assert result.kind == "defer"
    assert result.adapter_state == LICENCE_HELD
    assert result.backoff_seconds == ai_hold.HOLD_FIRST_DELAY_SECONDS


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["assignment", "sub_agent"])
async def test_a_held_start_decides_on_one_reading_of_the_gate(monkeypatch, kind):
    """Re-claimed while held, the start reads the gate once and the body acts
    on that reading - here one nothing could be read for, so the hold stays
    open and the work is retried. A body reading the gate again could start
    the work while the hold is still open, and a later hold would then reuse
    this one's start and budget."""
    from app.services import assignment_engine, sub_agent
    from app.services.agentic_gate import AgenticVerdict
    from app.services.orchestration_adapters import assignment as assignment_adapter
    from app.services.orchestration_adapters import sub_agent as sub_agent_adapter
    from app.services.orchestration_adapters.base import AdapterContext
    from app.services.sub_agent import SubAgentTaskOutcome

    unread = AgenticVerdict("license_required", transient=True)
    read = AsyncMock(return_value=unread)
    monkeypatch.setattr("app.services.agentic_gate.agentic_verdict", read)
    if kind == "assignment":
        body = AsyncMock(return_value=assignment_engine.RUN_DEFERRED)
        monkeypatch.setattr(assignment_engine, "execute_assignment_run", body)
        monkeypatch.setattr(assignment_adapter, "_bind_memory_chain", AsyncMock(return_value=None))
        adapter = assignment_adapter.AssignmentAdapter()
        execution = _execution(payload=_held_for(600))
        db = _Session()
    else:
        body = AsyncMock(return_value=SubAgentTaskOutcome(status="deferred", error="unread"))
        monkeypatch.setattr(sub_agent, "_execute_sub_agent_task", body)
        adapter = sub_agent_adapter.SubAgentAdapter()
        execution = _execution(
            payload={
                **_held_for(600),
                "task_id": str(uuid.uuid4()),
                "target_agent_id": str(uuid.uuid4()),
                "user_id": str(uuid.uuid4()),
                "task_description": "Summarise the new exposures",
            }
        )
        db = _Session([SimpleNamespace(status="pending")])

    result = await adapter.execute(AdapterContext(db=db, worker_id="w1"), execution)

    read.assert_awaited_once()
    assert body.await_args.kwargs["verdict"] is unread
    assert orchestration_engine.hold_record(execution) is not None
    assert result.kind == "failed" and result.retryable is True


@pytest.mark.asyncio
async def test_a_body_handed_the_verdict_never_reads_the_gate_again(monkeypatch):
    """The run and the delegated task act on the reading their adapter took."""
    from app.services import assignment_engine, sub_agent
    from app.services.agentic_gate import AgenticVerdict

    monkeypatch.setattr(
        "app.services.agentic_gate.agentic_verdict",
        AsyncMock(side_effect=AssertionError("read the gate again")),
    )
    refused = AgenticVerdict("ai_disabled")

    run, assignment, agent = _run_stubs()
    monkeypatch.setattr(
        assignment_engine, "async_session", lambda: _Session([run, assignment, agent])
    )
    _quota_and_telemetry(monkeypatch)
    assert await assignment_engine.execute_assignment_run(
        str(run.id), hold=True, verdict=refused
    ) == assignment_engine.RunHeld("ai_disabled")

    task = _task_row()
    monkeypatch.setattr("app.core.database.async_session", lambda: _TaskSession([task]))
    _patch_task_collaborators(monkeypatch)
    monkeypatch.setattr(sub_agent, "_mark_run_held", AsyncMock())
    outcome = await sub_agent._execute_sub_agent_task(
        task_id=str(task.id),
        user_id=str(task.user_id),
        target_agent_id=str(uuid.uuid4()),
        task_description="Summarise the new exposures",
        hold=True,
        verdict=refused,
    )
    assert outcome.held and outcome.refusal == "ai_disabled"


@pytest.mark.asyncio
async def test_a_held_run_goes_ahead_once_ai_is_back(monkeypatch):
    """The marker is dropped and the run reaches the quota like any other."""
    from app.services import assignment_engine

    _pin(monkeypatch, **ENTERPRISE)
    run, assignment, agent = _run_stubs()
    run.trigger_context = ai_hold.with_hold_marker({}, "license_required")
    monkeypatch.setattr(
        assignment_engine, "async_session", lambda: _Session([run, assignment, agent])
    )
    monkeypatch.setattr(
        "app.services.quota.check_agentic_quota", AsyncMock(side_effect=_QuotaReached)
    )
    with pytest.raises(_QuotaReached):
        await assignment_engine.execute_assignment_run(str(run.id), hold=True)
    assert run.status == "pending"
    assert ai_hold.HOLD_MARKER_KEY not in run.trigger_context


@pytest.mark.asyncio
async def test_a_run_held_the_maximum_is_failed_with_the_reason(monkeypatch):
    from app.services import assignment_engine

    _pin(monkeypatch, **COMMUNITY)
    run, assignment, agent = _run_stubs()
    run.trigger_context = ai_hold.with_hold_marker({}, "license_required")
    session = _Session([run, assignment, agent])
    monkeypatch.setattr(assignment_engine, "async_session", lambda: session)
    quota, _executions, _publish = _quota_and_telemetry(monkeypatch)

    assert await assignment_engine.execute_assignment_run(str(run.id), hold=False) is None

    assert run.status == "failed"
    assert run.error.startswith(deps.licence_required_detail())
    assert "held for 168 hour(s)" in run.error
    assert run.failure_cause == FailureCause.LICENSE_REQUIRED.value
    assert run.failure_determination == FailureDetermination.STRUCTURAL.value
    quota.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_adapter_stops_holding_once_the_bound_is_reached(monkeypatch):
    from app.services import assignment_engine
    from app.services.orchestration_adapters import assignment as assignment_adapter
    from app.services.orchestration_adapters.base import AdapterContext

    run, _assignment, _agent = _run_stubs()
    run.status = "failed"
    run.error = "refused"
    execute = AsyncMock(return_value=None)
    monkeypatch.setattr(assignment_engine, "execute_assignment_run", execute)
    monkeypatch.setattr(assignment_adapter, "_bind_memory_chain", AsyncMock(return_value=None))
    monkeypatch.setattr(settings, "ai_unavailable_hold_max_seconds", 3600)
    _pin(monkeypatch, **COMMUNITY)
    from app.services.agentic_gate import AgenticVerdict

    execution = _execution(payload={**_held_for(3700), "run_id": str(run.id)})
    result = await assignment_adapter.AssignmentAdapter().execute(
        AdapterContext(db=_Session([run]), worker_id="w1"), execution
    )

    assert execute.await_args.kwargs == {
        "hold": False,
        "verdict": AgenticVerdict("license_required"),
        "cancel_event": None,
        "deadline_at": execution.deadline_at,
        "attempt": execution.attempt_count,
        "taken_back_on_its_word": False,
    }
    assert result.kind == "failed" and result.retryable is False


def test_a_held_or_bound_expired_run_never_counts_toward_a_stale_reference_pause():
    """A held run is not terminal, so the streak never reads it; a run
    failed at the hold bound is recorded with its licence / switch cause, not
    ``stale_integration_reference``, so it never extends the streak; and the
    hold returns before the run could be tracked as a stale-reference failure."""
    from app.services import assignment_engine as engine

    now = datetime.now(UTC)

    def _row(cause: str, age: int):
        return engine._StreakRun(
            id=uuid.uuid4(),
            status="failed",
            failure_cause=cause,
            trigger_type="schedule",
            gate_skipped=False,
            created_at=now - timedelta(minutes=age),
        )

    stale = FailureCause.STALE_INTEGRATION_REFERENCE.value
    bound = FailureCause.LICENSE_REQUIRED.value
    assert engine._stale_reference_streak([_row(stale, 1), _row(stale, 2)], since=None) == 2
    assert engine._stale_reference_streak([_row(bound, 1), _row(stale, 2)], since=None) == 0
    assert '("completed", "failed")' in inspect.getsource(engine._load_streak_runs)
    source = inspect.getsource(engine.execute_assignment_run)
    assert source.index("return RunHeld(refusal)") < source.index(
        "_track_stale_reference_failure("
    )


# ── Kind 2: the resume of a paused run a person answered meanwhile ──────────


@pytest.mark.asyncio
async def test_an_answered_paused_run_is_held_with_its_answer_kept(monkeypatch):
    """The respond route stays ungated: the answer is recorded, and
    the resumed run is held - still ``awaiting_human``, the answer intact -
    until agentic AI is available again, when it resumes from the answer."""
    from app.api.v1 import assignments
    from app.services import assignment_engine

    assert "require_agentic_ai" not in inspect.getsource(assignments.respond_to_human_input)

    _pin(monkeypatch, **COMMUNITY)
    run, assignment, agent = _run_stubs()
    run.status = "awaiting_human"
    run.human_input_response = "Approved, go ahead"
    monkeypatch.setattr(
        assignment_engine, "async_session", lambda: _Session([run, assignment, agent])
    )
    quota, executions, _publish = _quota_and_telemetry(monkeypatch)

    outcome = await assignment_engine.execute_assignment_run(str(run.id), hold=True)

    assert outcome == assignment_engine.RunHeld("license_required")
    assert run.status == "awaiting_human"
    assert run.human_input_response == "Approved, go ahead"
    assert run.failure_cause is None
    quota.assert_not_awaited()
    executions.assert_not_awaited()


# ── Kind 3: a delegated task not yet picked up ──────────────────────────────


@pytest.mark.asyncio
async def test_a_queued_delegation_is_held_pending_and_its_parent_not_woken(monkeypatch):
    """Its parent is woken when the task finally runs and ends - the ordinary
    completion path - not now: nothing ended."""
    from app.services import sub_agent

    _pin(monkeypatch, **COMMUNITY)
    task = _task_row()
    session = _TaskSession([task])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    collaborators = _patch_task_collaborators(monkeypatch)
    mark_run = AsyncMock()
    monkeypatch.setattr(sub_agent, "_mark_run_held", mark_run)
    run_id = str(uuid.uuid4())

    outcome = await sub_agent._execute_sub_agent_task(
        task_id=str(task.id),
        user_id=str(task.user_id),
        target_agent_id=str(uuid.uuid4()),
        task_description="Summarise the new exposures",
        assignment_run_id=run_id,
        hold=True,
    )

    assert outcome.held and outcome.refusal == "license_required"
    assert task.status == "pending"
    assert task.error is None and task.started_at is None and task.completed_at is None
    assert ai_hold.held_reason_of(task.metadata_json) == ai_hold.held_message(
        "license_required"
    )
    mark_run.assert_awaited_once()
    assert mark_run.await_args.args[1:] == (run_id, "license_required")
    assert session.commits == 1
    collaborators.run_agent_chat.assert_not_awaited()
    collaborators.update_run.assert_not_awaited()
    collaborators.delegation.assert_not_awaited()
    collaborators.publish.assert_not_awaited()
    collaborators.wake.assert_not_called()


@pytest.mark.asyncio
async def test_the_sub_agent_adapter_holds_the_execution(monkeypatch):
    from app.services.orchestration_adapters import sub_agent as sub_agent_adapter
    from app.services.orchestration_adapters.base import AdapterContext
    from app.services.sub_agent import SubAgentTaskOutcome

    task_body = AsyncMock(
        return_value=SubAgentTaskOutcome(
            status="held", error="Held", refusal="ai_disabled"
        )
    )
    monkeypatch.setattr("app.services.sub_agent._execute_sub_agent_task", task_body)
    execution = _execution(
        payload={
            "task_id": str(uuid.uuid4()),
            "target_agent_id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
            "task_description": "Summarise the new exposures",
        }
    )

    result = await sub_agent_adapter.SubAgentAdapter().execute(
        AdapterContext(db=_Session([SimpleNamespace(status="pending")]), worker_id="w1"),
        execution,
    )

    assert task_body.await_args.kwargs["hold"] is True
    assert result.kind == "defer"
    assert result.adapter_state == "held:ai_disabled"


@pytest.mark.asyncio
async def test_a_held_delegation_goes_ahead_once_ai_is_back(monkeypatch):
    from app.services import sub_agent

    _pin(monkeypatch, **ENTERPRISE)
    task = _task_row()
    task.metadata_json = ai_hold.with_hold_marker({"assignment_run_id": "x"}, "ai_disabled")
    monkeypatch.setattr(
        "app.core.database.async_session", lambda: _TaskSession([task, None])
    )
    _patch_task_collaborators(monkeypatch)
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", AsyncMock(return_value=[]))

    await sub_agent._execute_sub_agent_task(
        task_id=str(task.id),
        user_id=str(task.user_id),
        target_agent_id=str(uuid.uuid4()),
        task_description="Summarise the new exposures",
        hold=True,
    )
    assert task.started_at is not None
    assert task.metadata_json == {"assignment_run_id": "x"}


@pytest.mark.asyncio
async def test_a_delegation_held_the_maximum_fails_and_wakes_its_parent(monkeypatch):
    from app.services import sub_agent

    _pin(monkeypatch, **COMMUNITY)
    task = _task_row()
    task.metadata_json = ai_hold.with_hold_marker({}, "license_required")
    monkeypatch.setattr(
        "app.core.database.async_session",
        lambda: _TaskSession([task, "Exposure analyst"]),
    )
    collaborators = _patch_task_collaborators(monkeypatch)

    outcome = await sub_agent._execute_sub_agent_task(
        task_id=str(task.id),
        user_id=str(task.user_id),
        target_agent_id=str(uuid.uuid4()),
        task_description="Summarise the new exposures",
        hold=False,
    )

    assert task.status == "failed"
    assert "held for" in task.error and not outcome.succeeded
    collaborators.wake.assert_called_once()
    assert collaborators.wake.call_args.kwargs["agent_name"] == "Exposure analyst"


@pytest.mark.asyncio
async def test_a_held_delegation_is_listed_as_held(monkeypatch):
    from app.services import sub_agent

    held = SimpleNamespace(
        id=uuid.uuid4(),
        parent_agent_id=uuid.uuid4(),
        target_agent_id=uuid.uuid4(),
        task_description="Summarise",
        status="pending",
        result=None,
        error=None,
        created_at=None,
        started_at=None,
        completed_at=None,
        metadata_json=ai_hold.with_hold_marker({}, "ai_disabled"),
    )
    plain = SimpleNamespace(**{**held.__dict__, "metadata_json": None})
    session = _Session([[held, plain]])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    listed = await sub_agent.list_user_tasks(None, str(uuid.uuid4()))

    assert listed[0]["held_reason"] == ai_hold.held_message("ai_disabled")
    assert "held_reason" not in listed[1]


@pytest.mark.asyncio
async def test_the_agent_reads_that_a_listed_delegation_is_held(monkeypatch):
    """``list_background_tasks`` is what an agent calls to learn what is still
    running: a held task that reads as plain ``pending`` looks stuck, so the
    line says why it waits (and a task that is not held reads as before)."""
    from unittest.mock import MagicMock

    from app.a2a.client import delegation as delegation_mod
    from app.services import sub_agent
    from app.services.builtin_tools import handover

    message = ai_hold.held_message("ai_disabled")
    held_id, plain_id = str(uuid.uuid4()), str(uuid.uuid4())

    async def _tasks(*_a, **_k):
        return [
            {"task_id": held_id, "status": "pending", "task_description": "Summarise", "held_reason": message},
            {"task_id": plain_id, "status": "pending", "task_description": "Translate"},
        ]

    async def _partner(*_a, **_k):
        return []

    monkeypatch.setattr(sub_agent, "list_user_tasks", _tasks)
    monkeypatch.setattr(delegation_mod, "outstanding_for_listing", _partner)
    built = handover.build_sub_agent_tools(
        MagicMock(),
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(uuid.uuid4()),
        handover_agents=[
            SimpleNamespace(
                id=uuid.uuid4(),
                name="Peer",
                slug="peer",
                description="",
                persona="",
                is_company_managed=False,
            )
        ],
        conversation_id=str(uuid.uuid4()),
        parent_run_id=str(uuid.uuid4()),
    )
    assert built is not None

    lines = (await built[1]._handlers["list_background_tasks"]()).splitlines()

    assert lines[0] == f"• {held_id} | pending | Summarise | {message}"
    assert lines[1] == f"• {plain_id} | pending | Translate"


# ── Kind 4: the pending runs of an evaluation ───────────────────────────────


@pytest.mark.asyncio
@pytest.mark.usefixtures("evaluation_enabled")
async def test_a_queued_evaluation_is_held_with_its_runs_pending(monkeypatch):
    from app.services.orchestration_adapters import evaluation as evaluation_adapter
    from app.services.orchestration_adapters.base import AdapterContext

    _pin(monkeypatch, **COMMUNITY)
    failed_runs = AsyncMock()
    monkeypatch.setattr(evaluation_adapter, "_fail_active_runs", failed_runs)
    execute = AsyncMock()
    monkeypatch.setattr("app.services.evaluation_engine.execute_evaluation", execute)

    result = await evaluation_adapter.EvaluationAdapter().execute(
        AdapterContext(db=_Session(strict=True), worker_id="w1"),
        _execution(payload={"evaluation_id": str(uuid.uuid4())}),
    )

    assert result.kind == "defer" and result.adapter_state == LICENCE_HELD
    failed_runs.assert_not_awaited()
    execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("evaluation_enabled")
async def test_an_evaluation_held_the_maximum_fails_its_pending_runs(monkeypatch):
    from app.services.orchestration_adapters import evaluation as evaluation_adapter
    from app.services.orchestration_adapters.base import AdapterContext

    _pin(monkeypatch, **COMMUNITY)
    monkeypatch.setattr(settings, "ai_unavailable_hold_max_seconds", 3600)
    failed_runs = AsyncMock()
    monkeypatch.setattr(evaluation_adapter, "_fail_active_runs", failed_runs)
    evaluation_id = str(uuid.uuid4())

    execution = _execution(payload={**_held_for(3700), "evaluation_id": evaluation_id})

    result = await evaluation_adapter.EvaluationAdapter().execute(
        AdapterContext(db=_Session(strict=True), worker_id="w1"), execution
    )

    assert result.kind == "failed" and result.retryable is False
    assert "held for 1 hour(s)" in result.error
    # The sweep is scoped to the runs THIS execution owns, so it names it.
    failed_runs.assert_awaited_once_with(
        evaluation_id, execution.id, result.error, statuses=("pending",)
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("evaluation_enabled")
async def test_an_evaluation_on_enterprise_edition_runs(monkeypatch):
    from app.services.orchestration_adapters import evaluation as evaluation_adapter
    from app.services.orchestration_adapters.base import AdapterContext

    _pin(monkeypatch, **ENTERPRISE)
    execute = AsyncMock()
    monkeypatch.setattr("app.services.evaluation_engine.execute_evaluation", execute)
    monkeypatch.setattr(evaluation_adapter, "_flush_langfuse", AsyncMock())

    result = await evaluation_adapter.EvaluationAdapter().execute(
        AdapterContext(db=_Session(strict=True), worker_id="w1"),
        _execution(payload={"evaluation_id": str(uuid.uuid4())}),
    )
    assert result.kind == "completed"
    execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_held_evaluation_is_read_off_its_execution():
    """An evaluation run has no JSON column for a marker: the API's
    ``held_reason`` is the refusal code read off the held execution."""
    evaluation_id = uuid.uuid4()
    session = _Session([[(str(evaluation_id), "held:ai_disabled"), ("not-a-uuid", "held:x")]])
    assert await ai_hold.held_evaluations(session, [evaluation_id]) == {
        evaluation_id: "ai_disabled"
    }
    assert await ai_hold.held_evaluations(_Session(strict=True), []) == {}


def _evaluation_run(evaluation_id, status):
    return SimpleNamespace(id=uuid.uuid4(), evaluation_id=evaluation_id, status=status)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("statuses", "expected_queries"),
    [
        (["pending", "pending", "pending", "completed"], 1),
        (["completed", "failed", "running"], 0),
        ([], 0),
    ],
)
async def test_the_runs_list_reads_the_held_state_in_one_query(
    monkeypatch, statuses, expected_queries
):
    """A page of evaluation runs costs ONE held-state query whatever its size,
    and none when no run is pending - what ``main`` pays on Enterprise."""
    from app.api.v1 import evaluations as evaluations_api

    first, second = uuid.uuid4(), uuid.uuid4()
    runs = [
        _evaluation_run(first if i % 2 else second, status)
        for i, status in enumerate(statuses)
    ]
    held_query = AsyncMock(return_value={first: "license_required"})
    monkeypatch.setattr(ai_hold, "held_evaluations", held_query)
    monkeypatch.setattr(evaluations_api, "get_user_group_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(evaluations_api, "_get_user_evaluation", AsyncMock())
    monkeypatch.setattr(
        "app.services.evaluation_engine.batch_compute_run_replayability",
        AsyncMock(return_value={}),
    )
    built: list = []

    async def _build(_db, run, *, missing_snapshots=None, held_reasons=None, run_modes=None):
        built.append(held_reasons)
        return {"id": str(run.id)}

    monkeypatch.setattr(evaluations_api, "_build_run_response", _build)
    # The page's run modes are read once too; what they say is not this test's.
    run_mode_query = AsyncMock(return_value={})
    monkeypatch.setattr(evaluations_api, "load_run_modes", run_mode_query)

    await evaluations_api.list_runs(
        evaluation_id=str(first),
        agent_id=None,
        model_id=None,
        limit=20,
        offset=0,
        user=SimpleNamespace(id=uuid.uuid4(), is_admin=True),
        db=_Session([runs]),
    )

    assert held_query.await_count == expected_queries
    assert run_mode_query.await_count == 1
    if expected_queries:
        assert held_query.await_args.args[1] == {first, second}
    # Every row is built from the page's answer: none asks on its own.
    assert all(reasons is not None for reasons in built)


@pytest.mark.asyncio
async def test_a_run_built_from_a_page_answer_asks_nothing_itself(monkeypatch):
    from app.api.v1 import evaluations as evaluations_api

    monkeypatch.setattr(
        evaluations_api,
        "_held_reasons_for",
        AsyncMock(side_effect=AssertionError("asked again")),
    )
    # The reuse and judge-failure tallies come back alongside the averages.
    monkeypatch.setattr(
        evaluations_api, "_get_avg_sub_scores", AsyncMock(return_value=({}, 0, 0, 0))
    )

    class _Rows:
        def one_or_none(self):
            return None

    class _Db:
        async def execute(self, *_a, **_k):
            return _Rows()

    eid = uuid.uuid4()
    run = SimpleNamespace(
        id=uuid.uuid4(),
        evaluation_id=eid,
        agent_id=uuid.uuid4(),
        flow_id=None,
        model_id="m",
        is_default_model=False,
        status="pending",
        trigger_reason="manual",
        score=None,
        speed_score=None,
        overall_score=None,
        f1_score=None,
        duration_seconds=None,
        completed_entries=0,
        total_entries=0,
        error=None,
        rescore_error=None,
        started_at=None,
        completed_at=None,
        created_at=datetime.now(UTC),
        persona_version_number=None,
        prompt_versions=None,
        agent_config_versions=None,
        skill_versions=None,
        # A run's evaluation fields: its own tags, the headline scores
        # the metric package writes, the pinned dataset versions, the scorer
        # selection and the anomaly tally.
        tags=None,
        quality_score=None,
        performance_score=None,
        metric_suite_version=None,
        pinned_dataset_versions=None,
        scorer_selection=None,
        anomaly_count=0,
        headline_details=None,
        # The mode the run recorded when it started.
        execution_mode=None,
        apply_prompt_guards=None,
        require_tool_approval=None,
        # Outside any placebo comparison.
        arm="real",
        trial_index=0,
        placebo_target=None,
        placebo_comparison_id=None,
    )
    response = await evaluations_api._build_run_response(
        _Db(), run, missing_snapshots=[], held_reasons={eid: "ai_disabled"}
    )
    assert response["held_reason"] == "ai_disabled"
    assert response["error"] is None
    # A run this request just created (the trigger endpoints) is not looked up
    # at all: the read cost is exactly main's.
    response = await evaluations_api._build_run_response(_Db(), run, missing_snapshots=[])
    assert response["held_reason"] is None


# ── Transient reads keep the retry ──────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("half", ["licence", "switch"])
async def test_a_transient_read_is_still_retried_not_held(monkeypatch, half):
    from app.services import assignment_engine

    _pin_unreadable(monkeypatch, half=half)
    run, assignment, agent = _run_stubs()
    session = _Session([run, assignment, agent])
    monkeypatch.setattr(assignment_engine, "async_session", lambda: session)
    _quota_and_telemetry(monkeypatch)

    outcome = await assignment_engine.execute_assignment_run(str(run.id), hold=True)
    assert outcome == assignment_engine.RUN_DEFERRED
    assert ai_hold.HOLD_MARKER_KEY not in run.trigger_context
    assert session.commits == 0


# ── What a hold says, and what keeps it alive ───────────────────────────────


def test_the_held_message_names_what_is_missing_in_each_mode(monkeypatch):
    """Copilot mode has no Community Edition: ``license_required`` there means
    the Filigran license is missing (``deps.licence_required_detail``)."""
    assert "Community Edition" in ai_hold.held_message("license_required")
    monkeypatch.setattr(settings, "platform_mode", "copilot")
    assert settings.is_copilot_like
    copilot = ai_hold.held_message("license_required")
    assert "Community Edition" not in copilot
    assert "Filigran license" in copilot
    assert "AI turned off" in ai_hold.held_message("ai_disabled")


@pytest.mark.asyncio
async def test_the_deadline_sweep_rebases_held_work_instead_of_dead_lettering_it():
    """Worker downtime longer than a held row's runnable budget: the sweep
    must not dead-letter work that is waiting on purpose (``claim_due`` would
    refuse it past its deadline too). It gets its runnable budget again, and
    the hold's own bound decides at the next re-check. A row that is not held
    is dead-lettered as on ``main``."""
    now = datetime.now(UTC)
    held = _execution(payload=_held_for(3600))
    held.state = "pending"
    held.lease_owner = held.lease_expires_at = None
    held.payload[orchestration_engine.HOLD_PAYLOAD_KEY]["budget_seconds"] = 720.0
    held.deadline_at = now - timedelta(minutes=30)
    plain = _execution()
    plain.state = "pending"
    plain.deadline_at = now - timedelta(minutes=30)
    # Held past the bound: the hold is over, so the deadline is the deadline.
    expired = _execution(payload=_held_for(settings.ai_unavailable_hold_max_seconds + 60))
    expired.state = "pending"
    expired.payload[orchestration_engine.HOLD_PAYLOAD_KEY]["budget_seconds"] = 720.0
    expired.deadline_at = now - timedelta(minutes=30)

    dead = await orchestration_engine.sweep_deadlines(
        _Session([[held, plain, expired]]), limit=10
    )

    assert dead == [plain, expired]
    assert plain.state == "dead_letter" and expired.state == "dead_letter"
    assert held.state == "pending"
    assert (held.deadline_at - now).total_seconds() == pytest.approx(720, abs=5)


@pytest.mark.asyncio
async def test_a_held_delegation_retries_its_linked_run_marker_on_every_recheck(monkeypatch):
    """The linked run's marker is best effort (a savepoint): one that failed
    is written at the next re-check, even though the task already carries
    its own marker. Nothing is committed when neither changed."""
    from app.services import sub_agent

    _pin(monkeypatch, **COMMUNITY)
    for wrote, commits in ((True, 1), (False, 0)):
        task = _task_row()
        task.metadata_json = ai_hold.with_hold_marker({}, "license_required")
        session = _TaskSession([task])
        monkeypatch.setattr("app.core.database.async_session", lambda s=session: s)
        _patch_task_collaborators(monkeypatch)
        mark_run = AsyncMock(return_value=wrote)
        monkeypatch.setattr(sub_agent, "_mark_run_held", mark_run)

        outcome = await sub_agent._execute_sub_agent_task(
            task_id=str(task.id),
            user_id=str(task.user_id),
            target_agent_id=str(uuid.uuid4()),
            task_description="Summarise the new exposures",
            assignment_run_id=str(uuid.uuid4()),
            hold=True,
        )

        assert outcome.held
        mark_run.assert_awaited_once()
        assert session.commits == commits
