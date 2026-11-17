"""A replaced attempt's hold writes nothing to its re-run's row.

An attempt stuck for a whole lease period before it decides whether it may run
loses its lease and is replaced by a re-run. When it then reaches a hold - AI
unavailable or the quota store out - it must not write its
stale copy of the run (``pending``, its trigger context, the charge record and
fence it loaded before the re-run started) over the re-run's row. It checks
the row under the lock like any other write before the loop; a live
attempt's hold is unchanged.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.config import settings
from app.services import ai_hold
from app.services.agentic_gate import AgenticVerdict
from app.services.attempt_charges import QUOTA_ATTEMPT_FENCE_KEY
from tests.test_background_edition_gate import (
    AI_SWITCHED_OFF,
    ENTERPRISE,
    _pin,
    _Result,
    _run_stubs,
    _Session,
)

REPLACED = {"type": "schedule", QUOTA_ATTEMPT_FENCE_KEY: [0, 2]}


def _is_guard_read(stmt) -> bool:
    descriptions = getattr(stmt, "column_descriptions", None) or []
    return [d.get("name") for d in descriptions] == ["status", "trigger_context"]


class _RowSession(_Session):
    """The edition-gate session, answering the guard's locked read of the run
    with what the row holds now (*row_status*, *row_context*)."""

    def __init__(self, rows, *, row_status: str, row_context: dict):
        super().__init__(rows)
        self.row = (row_status, row_context)
        self.guard_reads: list = []
        self.context_reads: list = []

    async def execute(self, stmt=None, *args, **kwargs):
        if _is_guard_read(stmt):
            self.guard_reads.append(stmt)
            return _GuardResult([self.row])
        if _is_context_read(stmt):
            # A re-run's own place (``fence_attempt``).
            self.context_reads.append(stmt)
            return _GuardResult([self.row[1]])
        return await super().execute(stmt, *args, **kwargs)


def _is_context_read(stmt) -> bool:
    descriptions = getattr(stmt, "column_descriptions", None) or []
    return [d.get("name") for d in descriptions] == ["trigger_context"]


class _GuardResult(_Result):
    def __init__(self, rows):
        super().__init__(None)
        self._guard_rows = rows

    def all(self):
        return list(self._guard_rows)

    def scalars(self):
        return self


def _engine(monkeypatch, session):
    from app.services import assignment_engine

    monkeypatch.setattr(assignment_engine, "async_session", lambda: session)
    spies = MagicMock()
    spies.executions = AsyncMock()
    spies.publish = AsyncMock()
    monkeypatch.setattr("app.services.quota.increment_agentic_usage", AsyncMock())
    monkeypatch.setattr(assignment_engine, "record_agentic_execution", spies.executions)
    monkeypatch.setattr(assignment_engine, "_publish_run_status", spies.publish)
    return assignment_engine, spies


def _stale_run(status: str = "running"):
    """Attempt 1's copy of the run: loaded before the re-run fenced it."""
    run, assignment, agent = _run_stubs()
    run.status = status
    run.trigger_context = {"type": "schedule"}
    return run, assignment, agent


def _for_update(stmt) -> bool:
    return getattr(stmt, "_for_update_arg", None) is not None


# -- Assignment runs: AI hold -------------------------------------------------


@pytest.mark.asyncio
async def test_a_replaced_attempts_ai_hold_writes_nothing(monkeypatch):
    _pin(monkeypatch, **AI_SWITCHED_OFF)
    run, assignment, agent = _stale_run()
    session = _RowSession([run, assignment, agent], row_status="running", row_context=REPLACED)
    engine, spies = _engine(monkeypatch, session)

    outcome = await engine.execute_assignment_run(str(run.id), hold=True, attempt=1)

    assert outcome is None, "not held: the re-run owns the run"
    assert session.commits == 0
    assert run.status == "running"
    assert run.trigger_context == {"type": "schedule"}
    assert session.guard_reads and all(_for_update(s) for s in session.guard_reads)
    spies.publish.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "running"])
async def test_a_live_attempts_ai_hold_is_unchanged(monkeypatch, status):
    _pin(monkeypatch, **AI_SWITCHED_OFF)
    run, assignment, agent = _stale_run(status)
    session = _RowSession(
        [run, assignment, agent], row_status=status, row_context={"type": "schedule"}
    )
    engine, _spies = _engine(monkeypatch, session)

    outcome = await engine.execute_assignment_run(str(run.id), hold=True, attempt=1)

    assert outcome == engine.RunHeld("ai_disabled")
    assert run.status == "pending", "a reclaimed run waits as it was before it started"
    assert ai_hold.held_code_of(run.trigger_context) == "ai_disabled"
    assert QUOTA_ATTEMPT_FENCE_KEY not in run.trigger_context, "nothing new on the row"
    assert session.commits == 1


def test_a_deferral_opens_the_next_epoch_only_after_a_rerun_fenced_the_run():
    from app.services.attempt_charges import attempt_fence, fence_for_deferral

    assert fence_for_deferral({"type": "schedule"}) is None
    assert fence_for_deferral({QUOTA_ATTEMPT_FENCE_KEY: [1, 0]}) is None
    opened = fence_for_deferral({"type": "schedule", QUOTA_ATTEMPT_FENCE_KEY: [0, 2]})
    assert attempt_fence(opened) == (1, 0)
    assert opened["type"] == "schedule"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ai", "quota"])
async def test_a_hold_after_a_lost_worker_leaves_the_next_attempt_live(monkeypatch, kind):
    """A worker lost mid-check made attempt 2 a re-run, which fenced the run;
    attempt 2 holds, and the deferral restores the attempt count, so the next
    claim is attempt 1 again. It is the live attempt, not a replaced one."""
    from app.services.attempt_charges import attempt_fence

    if kind == "ai":
        _pin(monkeypatch, **AI_SWITCHED_OFF)
        held = "ai_disabled"
    else:
        monkeypatch.setattr(settings, "platform_mode", "xtm_one")
        _pin(monkeypatch, **ENTERPRISE)
        _store_down(monkeypatch)
        held = ai_hold.QUOTA_STORE_UNAVAILABLE_CODE
    fenced = {"type": "schedule", QUOTA_ATTEMPT_FENCE_KEY: [0, 2]}

    run, assignment, agent = _stale_run("pending")
    run.trigger_context = dict(fenced)
    session = _RowSession([run, assignment, agent], row_status="pending", row_context=fenced)
    engine, _spies = _engine(monkeypatch, session)
    assert await engine.execute_assignment_run(str(run.id), hold=True, attempt=2) == (
        engine.RunHeld(held)
    )
    assert attempt_fence(run.trigger_context) == (1, 0)
    assert session.commits == 2, "its place, then the hold"
    # Both reads were answered by the row, not by the queue behind them.
    assert len(session.context_reads) == 1 and _for_update(session.context_reads[0])
    assert len(session.guard_reads) == 1 and _for_update(session.guard_reads[0])

    after = dict(run.trigger_context)
    run2, assignment2, agent2 = _stale_run("pending")
    run2.trigger_context = dict(after)
    session2 = _RowSession([run2, assignment2, agent2], row_status="pending", row_context=after)
    engine, _spies = _engine(monkeypatch, session2)
    assert await engine.execute_assignment_run(str(run2.id), hold=True, attempt=1) == (
        engine.RunHeld(held)
    ), "the restored attempt 1 is live"
    assert session2.commits == 0, "held as before: nothing new to write"
    assert len(session2.guard_reads) == 1 and session2.context_reads == []


@pytest.mark.asyncio
async def test_a_replaced_attempts_expired_hold_does_not_fail_the_run(monkeypatch):
    """Past its hold bound the attempt would fail the run: not the re-run's."""
    _pin(monkeypatch, **AI_SWITCHED_OFF)
    run, assignment, agent = _stale_run()
    session = _RowSession([run, assignment, agent], row_status="running", row_context=REPLACED)
    engine, _spies = _engine(monkeypatch, session)

    outcome = await engine.execute_assignment_run(str(run.id), hold=False, attempt=1)

    assert outcome is None
    assert run.status == "running" and run.error is None and run.completed_at is None
    assert session.commits == 0


# -- Assignment runs: quota-store hold ----------------------------------------


@pytest.fixture
def xtm_one(monkeypatch):
    monkeypatch.setattr(settings, "platform_mode", "xtm_one")
    _pin(monkeypatch, **ENTERPRISE)


def _store_down(monkeypatch):
    check = AsyncMock(
        side_effect=RedisConnectionError("Error 111 connecting to redis:6379. Connection refused.")
    )
    monkeypatch.setattr("app.services.quota.check_agentic_quota", check)
    return check


@pytest.mark.asyncio
async def test_a_replaced_attempts_quota_hold_writes_nothing(monkeypatch, xtm_one):
    run, assignment, agent = _stale_run()
    session = _RowSession([run, assignment, agent], row_status="running", row_context=REPLACED)
    engine, _spies = _engine(monkeypatch, session)
    check = _store_down(monkeypatch)

    outcome = await engine.execute_assignment_run(str(run.id), hold=True, attempt=1)

    assert outcome is None
    check.assert_not_awaited()
    assert session.commits == 0
    assert run.status == "running"
    assert run.trigger_context == {"type": "schedule"}


@pytest.mark.asyncio
async def test_a_live_attempts_quota_hold_is_unchanged(monkeypatch, xtm_one):
    run, assignment, agent = _stale_run("pending")
    session = _RowSession(
        [run, assignment, agent], row_status="pending", row_context={"type": "schedule"}
    )
    engine, _spies = _engine(monkeypatch, session)
    _store_down(monkeypatch)

    outcome = await engine.execute_assignment_run(str(run.id), hold=True, attempt=1)

    assert outcome == engine.RunHeld(ai_hold.QUOTA_STORE_UNAVAILABLE_CODE)
    assert ai_hold.held_code_of(run.trigger_context) == ai_hold.QUOTA_STORE_UNAVAILABLE_CODE
    assert run.status == "pending"
    assert session.commits == 1


# -- Assignment runs: the other writes before the loop -------------------------


@pytest.mark.asyncio
async def test_a_replaced_attempt_does_not_fail_the_run_for_a_disabled_assignment(monkeypatch):
    _pin(monkeypatch, **ENTERPRISE)
    run, assignment, agent = _stale_run()
    assignment.enabled = False
    session = _RowSession([run, assignment, agent], row_status="running", row_context=REPLACED)
    engine, _spies = _engine(monkeypatch, session)

    assert await engine.execute_assignment_run(str(run.id), hold=True, attempt=1) is None
    assert run.status == "running" and run.error is None
    assert session.commits == 0


@pytest.mark.asyncio
async def test_a_first_attempt_checks_under_the_lock_before_any_write(monkeypatch):
    """Even with no re-run in sight: a re-run can replace it at any point
    before its first commit, so the lock is taken before that commit."""
    _pin(monkeypatch, **AI_SWITCHED_OFF)
    run, assignment, agent = _stale_run("pending")
    session = _RowSession(
        [run, assignment, agent], row_status="pending", row_context={"type": "schedule"}
    )
    engine, _spies = _engine(monkeypatch, session)

    await engine.execute_assignment_run(str(run.id), hold=True, attempt=1)

    assert len(session.guard_reads) == 1
    assert _for_update(session.guard_reads[0])


# -- Delegated tasks: the first read is locked ---------------------


class _SessionCM:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_exc):
        return False


def _task(metadata: dict) -> MagicMock:
    task = MagicMock()
    task.id = uuid.uuid4()
    task.status = "running"
    task.conversation_id = None
    task.metadata_json = dict(metadata)
    task.error = None
    return task


async def _run_held_task(task, *, attempt: int):
    from app.services import sub_agent

    db = MagicMock()
    db.execute = AsyncMock(return_value=_Result(task))
    db.commit = AsyncMock()
    refuse = AsyncMock(return_value=sub_agent.SubAgentTaskOutcome(status="held"))
    with (
        patch("app.core.database.async_session", lambda: _SessionCM(db)),
        patch("app.services.sub_agent._refuse_task_before_turn", new=refuse),
        patch("app.services.sub_agent.credit_lost_attempts", new=AsyncMock(return_value=0)),
        patch("app.services.sub_agent._update_task_delegation", new=AsyncMock()),
    ):
        outcome = await sub_agent._execute_sub_agent_task(
            task_id=str(task.id),
            user_id=str(uuid.uuid4()),
            target_agent_id=str(uuid.uuid4()),
            task_description="Summarise the new exposures",
            hold=True,
            verdict=AgenticVerdict("ai_disabled"),
            attempt=attempt,
        )
    return outcome, db, refuse


@pytest.mark.asyncio
async def test_a_replaced_task_attempts_hold_writes_nothing():
    from app.services.sub_agent import REPLACED_ATTEMPT_ERROR
    from app.services.sub_agent_files import TASK_ATTEMPT_KEY

    task = _task({TASK_ATTEMPT_KEY: 2})

    outcome, db, refuse = await _run_held_task(task, attempt=1)

    assert (outcome.status, outcome.error) == ("failed", REPLACED_ATTEMPT_ERROR)
    refuse.assert_not_awaited()
    db.commit.assert_not_awaited()
    assert task.status == "running"
    assert task.metadata_json == {TASK_ATTEMPT_KEY: 2}
    assert _for_update(db.execute.await_args_list[0].args[0])


@pytest.mark.asyncio
async def test_a_live_task_attempts_hold_is_unchanged():
    task = _task({})

    outcome, _db, refuse = await _run_held_task(task, attempt=1)

    assert outcome.status == "held"
    refuse.assert_awaited_once()
    assert refuse.await_args.kwargs["refusal"] == "ai_disabled"
    assert refuse.await_args.kwargs["hold"] is True


def test_the_task_fence_keeps_the_attempt_a_bare_number_and_opens_an_epoch_on_deferral():
    """The previous release reads ``orchestration_attempt`` as a bare number
    only: it stays one, and the epoch has a key of its own."""
    from app.services.sub_agent_files import (
        TASK_ATTEMPT_EPOCH_KEY,
        TASK_ATTEMPT_KEY,
        task_attempt_fence,
        task_attempt_place,
        task_fence_for_deferral,
        with_task_attempt_fence,
    )

    assert task_attempt_fence({}) == (0, 0)
    assert task_attempt_fence({TASK_ATTEMPT_KEY: 2}) == (0, 2), "a row written before epochs"
    assert task_attempt_fence({TASK_ATTEMPT_KEY: 3, TASK_ATTEMPT_EPOCH_KEY: 1}) == (1, 3)
    for broken in (None, {TASK_ATTEMPT_KEY: True}, {TASK_ATTEMPT_KEY: "2"}, {TASK_ATTEMPT_KEY: -1}):
        assert task_attempt_fence(broken) == (0, 0)
    written = with_task_attempt_fence({"k": 1}, (0, 2))
    assert written == {"k": 1, TASK_ATTEMPT_KEY: 2, TASK_ATTEMPT_EPOCH_KEY: 0}
    assert isinstance(written[TASK_ATTEMPT_KEY], int)
    assert task_attempt_place({TASK_ATTEMPT_KEY: 0, TASK_ATTEMPT_EPOCH_KEY: 1}, 1) == (1, 1)
    assert task_attempt_place({}, None) is None
    assert task_fence_for_deferral({}) is None
    assert task_fence_for_deferral({TASK_ATTEMPT_KEY: 0, TASK_ATTEMPT_EPOCH_KEY: 1}) is None
    assert task_fence_for_deferral({"k": 1, TASK_ATTEMPT_KEY: 2}) == {
        "k": 1,
        TASK_ATTEMPT_KEY: 0,
        TASK_ATTEMPT_EPOCH_KEY: 1,
    }


@pytest.mark.asyncio
async def test_a_task_hold_after_a_lost_worker_leaves_the_next_attempt_live():
    """Attempt 2 re-ran the task and fenced it, then held; the deferral
    restores the count, so the next claim is attempt 1 again - live."""
    from app.services import sub_agent
    from app.services.sub_agent_files import TASK_ATTEMPT_KEY, task_attempt_fence

    task = _task({TASK_ATTEMPT_KEY: 2})
    task.status = "pending"
    db = MagicMock()
    db.commit = AsyncMock()
    outcome = await sub_agent._refuse_task_before_turn(
        db,
        task,
        task_id=str(task.id),
        user_id=str(uuid.uuid4()),
        assignment_run_id=None,
        refusal="ai_disabled",
        hold=True,
        was_held=False,
        agent_name="Specialist",
    )
    assert outcome.status == "held"
    assert task_attempt_fence(task.metadata_json) == (1, 0)
    db.commit.assert_awaited_once()

    after = _task(task.metadata_json)
    outcome, _db, refuse = await _run_held_task(after, attempt=1)
    assert outcome.status == "held", "the restored attempt 1 is live"
    refuse.assert_awaited_once()
