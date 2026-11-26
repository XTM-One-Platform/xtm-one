"""A hold lasts as long as its bound, not as long as its budgets.

``orchestration_engine.defer`` hands held work back to wait, and a hold may
last ``AI_UNAVAILABLE_HOLD_MAX_SECONDS`` (a week by default): about two
thousand claim / re-check / defer cycles at the 300 s cap. Nothing spent
WAITING in those cycles may be taken from the work itself:

- the runnable deadline: the reconciler claims a due row a poll interval or so
  after its ``run_at``, and that lag - plus the re-check - must not be charged
  on every cycle, or a long hold drains the timeout, falls out of
  ``claim_due``'s reach and is dead-lettered by ``sweep_deadlines`` with the
  engine's cause, days before its bound;
- the attempts: a transient read retried mid-hold or a worker lost
  during a re-check must not use up the execution's attempts one blip at a
  time, while a state that stays unreadable still ends in ``dead_letter``.

A fake clock drives the engine's ``datetime.now`` so a whole week of cycles
runs in milliseconds; ``_claim`` / ``_reclaim`` mirror what ``claim_due`` and
``reclaim_expired_leases`` do to a row.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.services import ai_hold, orchestration_engine
from tests.test_background_edition_gate import _Session

REFUSAL = "license_required"
# What a crashed worker's run gets before it is re-claimed: the engine's own
# timeout budget, 600 s plus the 120 s grace (``worker_job_timeout`` +
# ``orchestration_grace_seconds``).
BUDGET_SECONDS = 720.0
# A due row is claimed on the next reconciler cycle: up to one poll interval
# (``reconciler_interval_seconds``, 2 s) plus the cycle itself.
CLAIM_LAG_SECONDS = 1.5
RECHECK_SECONDS = 0.2


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _install(monkeypatch, clock: _Clock) -> None:
    class _ClockedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now if tz is None else clock.now.astimezone(tz)

    monkeypatch.setattr(orchestration_engine, "datetime", _ClockedDatetime)


def _claimed_execution(clock: _Clock, *, budget: float | None = BUDGET_SECONDS):
    return SimpleNamespace(
        id=uuid.uuid4(),
        adapter="assignment",
        state="running",
        payload={"run_id": str(uuid.uuid4())},
        attempt_count=1,
        max_attempts=3,
        run_at=clock.now,
        deadline_at=None if budget is None else clock.now + timedelta(seconds=budget),
        adapter_state=None,
        last_error=None,
        lease_owner="w1",
        lease_expires_at=clock.now + timedelta(seconds=60),
        updated_at=clock.now,
    )


def _claimable(execution, clock: _Clock) -> bool:
    """``claim_due``'s eligibility, field for field."""
    return (
        execution.state == "pending"
        and execution.run_at <= clock.now
        and (execution.lease_owner is None or execution.lease_expires_at < clock.now)
        and execution.attempt_count < execution.max_attempts
        and (execution.deadline_at is None or execution.deadline_at > clock.now)
    )


def _claim(execution, clock: _Clock, worker: str = "w1") -> None:
    assert _claimable(execution, clock), (
        "the held execution fell out of claim_due's reach "
        f"(deadline {execution.deadline_at}, now {clock.now}, "
        f"attempts {execution.attempt_count}/{execution.max_attempts})"
    )
    execution.state = "running"
    execution.lease_owner = worker
    execution.lease_expires_at = clock.now + timedelta(seconds=60)
    execution.attempt_count += 1


def _reclaim(execution, clock: _Clock) -> None:
    """The worker was lost mid-check: ``reclaim_expired_leases`` on the row."""
    clock.advance(61)
    assert execution.lease_expires_at < clock.now
    execution.lease_owner = None
    execution.lease_expires_at = None
    execution.adapter_state = None
    if execution.attempt_count >= execution.max_attempts:
        execution.state = "dead_letter"
    else:
        execution.state = "pending"
        execution.run_at = clock.now


async def _hold(execution, worker: str = "w1") -> None:
    """What the reconciler applies for an adapter's ``hold_result``."""
    result = ai_hold.hold_result(execution, REFUSAL)
    await orchestration_engine.defer(
        _Session(),
        execution_id=execution.id,
        worker_id=worker,
        reason=result.error,
        delay_seconds=result.backoff_seconds,
        adapter_state=result.adapter_state,
    )


async def _transient_retry(execution, worker: str = "w1") -> None:
    """The adapters' answer to a transient read, through ``fail``."""
    await orchestration_engine.fail(
        _Session(),
        execution_id=execution.id,
        worker_id=worker,
        error="Not started: whether agentic AI may run could not be read.",
        retryable=True,
        backoff_seconds=30.0,
    )


def _wait_until_due(execution, clock: _Clock) -> None:
    clock.advance(max(0.0, (execution.run_at - clock.now).total_seconds()))
    clock.advance(CLAIM_LAG_SECONDS)


@pytest.mark.asyncio
async def test_a_week_long_hold_keeps_its_whole_runnable_budget(monkeypatch):
    """Held for the whole default bound, re-checked on every cycle, claimed a
    poll interval late every time: still claimable on every cycle, and the
    run that finally starts has the budget it had when the hold began."""
    clock = _Clock()
    _install(monkeypatch, clock)
    execution = _claimed_execution(clock)
    monkeypatch.setattr(
        orchestration_engine, "_locked_get", AsyncMock(return_value=execution)
    )
    bound = settings.ai_unavailable_hold_max_seconds
    began = clock.now

    cycles = 0
    clock.advance(RECHECK_SECONDS)
    while ai_hold.hold_budget_left(execution, now=clock.now):
        await _hold(execution)
        _wait_until_due(execution, clock)
        _claim(execution, clock)
        clock.advance(RECHECK_SECONDS)
        cycles += 1

    assert (clock.now - began).total_seconds() >= bound
    assert cycles > 1000
    left = (execution.deadline_at - clock.now).total_seconds()
    assert left >= BUDGET_SECONDS - CLAIM_LAG_SECONDS - 2 * RECHECK_SECONDS


@pytest.mark.asyncio
async def test_a_deferral_without_a_deadline_never_gains_one(monkeypatch):
    clock = _Clock()
    _install(monkeypatch, clock)
    execution = _claimed_execution(clock, budget=None)
    monkeypatch.setattr(
        orchestration_engine, "_locked_get", AsyncMock(return_value=execution)
    )
    for _ in range(5):
        await _hold(execution)
        _wait_until_due(execution, clock)
        _claim(execution, clock)
    assert execution.deadline_at is None


@pytest.mark.asyncio
async def test_blips_during_a_hold_never_use_up_its_attempts(monkeypatch):
    """A transient read retried mid-hold and a worker lost during a re-check
    are not the work's attempts: the next hold gives them back, so a week of
    sporadic blips cannot dead-letter work that never ran."""
    clock = _Clock()
    _install(monkeypatch, clock)
    execution = _claimed_execution(clock)
    monkeypatch.setattr(
        orchestration_engine, "_locked_get", AsyncMock(return_value=execution)
    )

    await _hold(execution)
    assert execution.attempt_count == 0
    for blip in range(6):
        _wait_until_due(execution, clock)
        _claim(execution, clock)
        if blip % 2:
            await _transient_retry(execution)
        else:
            _reclaim(execution, clock)
        assert execution.state == "pending", "a blip dead-lettered held work"
        _wait_until_due(execution, clock)
        _claim(execution, clock)
        await _hold(execution)
        assert execution.state == "pending"
        assert execution.attempt_count == 0


@pytest.mark.asyncio
async def test_a_worker_lost_mid_check_never_dead_letters_held_work_at_its_last_attempt(
    monkeypatch,
):
    """Held after two transient retries (baseline 2 of 3 attempts): the next
    re-check's claim makes it 3, and a worker lost during that re-check must
    not dead-letter it - the claim was a re-check, which ``defer`` would have
    given back. The real ``reclaim_expired_leases``, on the row."""
    clock = _Clock()
    _install(monkeypatch, clock)
    execution = _claimed_execution(clock)
    execution.attempt_count = 3
    monkeypatch.setattr(
        orchestration_engine, "_locked_get", AsyncMock(return_value=execution)
    )
    await _hold(execution)
    assert execution.attempt_count == 2
    _wait_until_due(execution, clock)
    _claim(execution, clock)
    assert execution.attempt_count == execution.max_attempts

    clock.advance(61)
    dead = await orchestration_engine.reclaim_expired_leases(
        _Session([[execution]]), limit=10
    )

    assert dead == []
    assert execution.state == "pending"
    assert execution.attempt_count == 2
    assert orchestration_engine.hold_record(execution) is not None


@pytest.mark.asyncio
async def test_a_worker_lost_during_the_real_run_still_counts(monkeypatch):
    """Once the hold is closed the work is running for real: a lost worker is
    an attempt again, as on ``main``."""
    clock = _Clock()
    _install(monkeypatch, clock)
    execution = _claimed_execution(clock)
    execution.attempt_count = 3
    clock.advance(61)
    dead = await orchestration_engine.reclaim_expired_leases(
        _Session([[execution]]), limit=10
    )
    assert dead == [execution]
    assert execution.state == "dead_letter"


@pytest.mark.asyncio
async def test_a_state_that_stays_unreadable_still_ends_in_dead_letter(monkeypatch):
    """The attempt bound still holds: consecutive transient reads with no hold in
    between use up the attempts and dead-letter, held or not."""
    clock = _Clock()
    _install(monkeypatch, clock)
    execution = _claimed_execution(clock)
    monkeypatch.setattr(
        orchestration_engine, "_locked_get", AsyncMock(return_value=execution)
    )

    await _hold(execution)
    _wait_until_due(execution, clock)
    _claim(execution, clock)
    for _ in range(execution.max_attempts - 1):
        await _transient_retry(execution)
        assert execution.state == "pending"
        _wait_until_due(execution, clock)
        _claim(execution, clock)
    await _transient_retry(execution)
    assert execution.state == "dead_letter"


async def _held_six_days_then_claimed(monkeypatch, clock: _Clock, payload: dict):
    """An execution held for six days (bound: a week), claimed with AI back."""
    from tests.test_background_edition_gate import ENTERPRISE, _pin

    execution = _claimed_execution(clock)
    execution.payload = payload
    monkeypatch.setattr(
        orchestration_engine, "_locked_get", AsyncMock(return_value=execution)
    )
    await _hold(execution)
    record = orchestration_engine.hold_record(execution)
    record["since"] = (clock.now - timedelta(days=6)).isoformat()
    _wait_until_due(execution, clock)
    _claim(execution, clock)
    _pin(monkeypatch, **ENTERPRISE)
    return execution


async def _a_later_hold_starts_its_own_bound(execution, clock: _Clock) -> None:
    """The real run crashes, agentic AI goes away again: a new hold, a new
    start - not the one that began six days ago and ends in one."""
    assert orchestration_engine.hold_record(execution) is None
    assert execution.adapter_state is None
    _reclaim(execution, clock)
    _claim(execution, clock)
    await _hold(execution)
    assert orchestration_engine.hold_started_at(execution) == clock.now
    assert ai_hold.hold_budget_left(execution, now=clock.now + timedelta(days=2))


@pytest.mark.asyncio
async def test_the_hold_ends_when_a_held_run_starts(monkeypatch):
    from app.services import assignment_engine
    from app.services.orchestration_adapters import assignment as assignment_adapter
    from app.services.orchestration_adapters.base import AdapterContext

    clock = _Clock()
    _install(monkeypatch, clock)
    execution = await _held_six_days_then_claimed(
        monkeypatch, clock, {"run_id": str(uuid.uuid4())}
    )
    execute = AsyncMock(return_value=None)
    monkeypatch.setattr(assignment_engine, "execute_assignment_run", execute)
    monkeypatch.setattr(assignment_adapter, "_bind_memory_chain", AsyncMock(return_value=None))
    monkeypatch.setattr(assignment_adapter, "_run_trigger_payload", AsyncMock(return_value=None))

    result = await assignment_adapter.AssignmentAdapter().execute(
        AdapterContext(db=_Session([SimpleNamespace(status="completed")]), worker_id="w1"),
        execution,
    )

    from app.services.agentic_gate import AgenticVerdict

    assert result.kind == "completed"
    # The run acts on the reading that closed the hold, never a second one.
    # The fast cancel event rides along (emergency stop); this context has none.
    assert execute.await_args.kwargs == {
        "hold": True,
        "verdict": AgenticVerdict(None),
        "cancel_event": None,
        "deadline_at": execution.deadline_at,
        "attempt": execution.attempt_count,
        "taken_back_on_its_word": False,
    }
    await _a_later_hold_starts_its_own_bound(execution, clock)


@pytest.mark.asyncio
async def test_the_hold_ends_when_a_held_delegation_starts(monkeypatch):
    from app.services.orchestration_adapters import sub_agent as sub_agent_adapter
    from app.services.orchestration_adapters.base import AdapterContext
    from app.services.sub_agent import SubAgentTaskOutcome

    clock = _Clock()
    _install(monkeypatch, clock)
    execution = await _held_six_days_then_claimed(
        monkeypatch,
        clock,
        {
            "task_id": str(uuid.uuid4()),
            "target_agent_id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
            "task_description": "Summarise the new exposures",
        },
    )
    body = AsyncMock(return_value=SubAgentTaskOutcome(status="completed"))
    monkeypatch.setattr("app.services.sub_agent._execute_sub_agent_task", body)

    result = await sub_agent_adapter.SubAgentAdapter().execute(
        AdapterContext(db=_Session([SimpleNamespace(status="pending")]), worker_id="w1"),
        execution,
    )

    assert result.kind == "completed"
    await _a_later_hold_starts_its_own_bound(execution, clock)


@pytest.mark.asyncio
@pytest.mark.usefixtures("evaluation_enabled")
async def test_the_hold_ends_when_a_held_evaluation_starts(monkeypatch):
    from app.services.orchestration_adapters import evaluation as evaluation_adapter
    from app.services.orchestration_adapters.base import AdapterContext

    clock = _Clock()
    _install(monkeypatch, clock)
    execution = await _held_six_days_then_claimed(
        monkeypatch, clock, {"evaluation_id": str(uuid.uuid4())}
    )
    execute = AsyncMock()
    monkeypatch.setattr("app.services.evaluation_engine.execute_evaluation", execute)
    monkeypatch.setattr(evaluation_adapter, "_flush_langfuse", AsyncMock())

    result = await evaluation_adapter.EvaluationAdapter().execute(
        AdapterContext(db=_Session(), worker_id="w1"), execution
    )

    assert result.kind == "completed"
    execute.assert_awaited_once()
    await _a_later_hold_starts_its_own_bound(execution, clock)


@pytest.mark.asyncio
async def test_an_execution_never_held_pays_nothing_to_close_a_hold(monkeypatch):
    """Enterprise Edition with AI on: no gate read, no lock, no commit."""
    clock = _Clock()
    _install(monkeypatch, clock)
    execution = _claimed_execution(clock)
    locked = AsyncMock(side_effect=AssertionError("locked a never-held row"))
    monkeypatch.setattr(orchestration_engine, "_locked_get", locked)
    monkeypatch.setattr(
        "app.services.agentic_gate.agentic_verdict",
        AsyncMock(side_effect=AssertionError("asked the gate")),
    )
    session = _Session(strict=True)
    await ai_hold.close_hold_if_ai_is_back(
        SimpleNamespace(db=session, worker_id="w1"), execution
    )
    assert session.commits == 0


@pytest.mark.asyncio
async def test_a_user_with_a_held_fanout_run_gets_no_catch_up_run(monkeypatch):
    """A per-user fan-out skips a user whose previous run is held - for days,
    older than the dedup window: the held run IS the catch-up. Same single
    dedup statement; which old runs count is checked against PostgreSQL in
    test_ai_hold_readers_postgres."""
    from sqlalchemy.dialects import postgresql

    from app.services import assignment_engine, autonomous
    from tests.test_background_edition_gate import ENTERPRISE, _pin

    _pin(monkeypatch, **ENTERPRISE)
    held_user, fresh_user = uuid.uuid4(), uuid.uuid4()
    statements: list = []

    class _Db(_Session):
        async def execute(self, statement, *_a, **_k):
            statements.append(statement)
            return await super().execute(statement)

    db = _Db([[(held_user, None), (fresh_user, None)], [held_user]])
    trigger = AsyncMock(return_value="run")
    monkeypatch.setattr(assignment_engine, "trigger_assignment", trigger)
    assignment = SimpleNamespace(id=uuid.uuid4(), name="Daily briefing")

    fanned = await autonomous._fanout_assignment_to_users(
        db, assignment, SimpleNamespace(name="Briefer"), datetime.now(UTC), "0 8 * * *"
    )

    assert fanned == 1
    assert trigger.await_args.kwargs["triggered_by_user_id"] == fresh_user
    dedup = " ".join(
        str(statements[-1].compile(dialect=postgresql.dialect())).split()
    ).lower()
    assert "assignment_runs.created_at >=" in dedup
    assert "assignment_runs.status in" in dedup
    assert "assignment_runs.trigger_context ->" in dedup
    assert " or " in dedup
    assert len(statements) == 2


def test_work_whose_hold_was_turned_off_is_not_said_to_have_waited_zero_hours(monkeypatch):
    """``AI_UNAVAILABLE_HOLD_MAX_SECONDS=0`` set while work is held ends every
    hold at the next re-check: the failure keeps the reason and says the hold
    was turned off, never that the work waited "0 hour(s)"."""
    from app.services.agentic_gate import refusal_message

    monkeypatch.setattr(settings, "ai_unavailable_hold_max_seconds", 0)
    message = ai_hold.expired_message(REFUSAL, was_held=True)
    assert message.startswith(refusal_message(REFUSAL))
    assert "0 hour" not in message
    assert "turned off" in message
    assert ai_hold.expired_message(REFUSAL, was_held=False) == refusal_message(REFUSAL)


@pytest.mark.asyncio
async def test_a_deferral_on_the_outbox_path_gives_back_no_attempt(monkeypatch):
    """The outbox path defers a row no claim took (``require_lease=False``):
    there is no claim to give back, so the attempts stay as they are."""
    clock = _Clock()
    _install(monkeypatch, clock)
    execution = _claimed_execution(clock)
    execution.lease_owner = None
    execution.lease_expires_at = None
    monkeypatch.setattr(
        orchestration_engine, "_locked_get", AsyncMock(return_value=execution)
    )

    await orchestration_engine.defer(
        _Session(),
        execution_id=execution.id,
        require_lease=False,
        reason=ai_hold.held_message(REFUSAL),
        delay_seconds=30.0,
        adapter_state=ai_hold.held_adapter_state(REFUSAL),
    )

    assert execution.state == "pending"
    assert execution.attempt_count == 1
