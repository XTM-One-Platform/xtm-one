"""A background task cut by a rollout runs again instead of dying with its item held.

Three behaviours, each driven through the code that has it:

- a task its stopping worker handed back is a deferral (see
  ``test_background_task_resume``), so the engine gives its attempt back;
- an execution taken back from a worker that said it was stopping gives its
  attempt back; one taken back from a worker gone without a word spends it;
- a run of the agent holding a legacy claim (no run recorded) takes it over
  without waiting for a quiet grace its own notes keep refreshing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from app.services import orchestration_engine, work_item
from app.services.orchestration_engine import _take_back, stopped_on_its_word


def _row(*, attempt_count: int, max_attempts: int = 3) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        adapter="sub_agent",
        lease_owner="worker-a",
        lease_expires_at=datetime.now(UTC),
        adapter_state=None,
        updated_at=None,
        payload={},
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        state="running",
        run_at=None,
        last_error=None,
    )


@pytest.fixture(autouse=True)
def _queue(monkeypatch):
    monkeypatch.setattr(orchestration_engine, "head_of_queue", AsyncMock(return_value=datetime.now(UTC)))
    monkeypatch.setattr(orchestration_engine, "last_heard_at", lambda row: None)


@pytest.mark.parametrize("state", ["draining", "stopped"])
def test_a_worker_that_said_it_was_stopping_stopped_on_its_word(state):
    assert stopped_on_its_word({"state": state})


@pytest.mark.parametrize("record", [None, {}, {"state": "running"}, {"lapsed": True, "state": "running"}])
def test_a_worker_gone_without_a_word_did_not(record):
    assert not stopped_on_its_word(record)


async def test_the_last_attempt_taken_back_from_a_stopping_worker_runs_again():
    row = _row(attempt_count=3)

    dead = await _take_back(
        AsyncMock(), row, datetime.now(UTC), why="Its worker stopped (SIGTERM)", head=True,
        owner_record={"state": "stopped"},
    )

    assert dead is False
    assert row.state == "pending" and row.attempt_count == 2
    assert "run again (2 of 3 attempts used)" in row.last_error


async def test_the_last_attempt_taken_back_from_a_crashed_worker_is_dead_lettered():
    row = _row(attempt_count=3)

    dead = await _take_back(
        AsyncMock(), row, datetime.now(UTC), why="Its worker is gone", head=True,
        owner_record={"state": "running", "lapsed": True},
    )

    assert dead is True
    assert row.state == "dead_letter" and row.attempt_count == 3
    assert "with no attempt left" in row.last_error


async def test_a_lease_that_simply_expired_still_spends_its_attempt():
    row = _row(attempt_count=1)

    await _take_back(AsyncMock(), row, datetime.now(UTC), why="Lease expired", head=False)

    assert row.state == "pending" and row.attempt_count == 1


async def test_the_claim_resuming_a_handed_back_task_ends_its_deferral_record(monkeypatch):
    # The hand-back deferral is no AI hold: its record
    # ends when the next claim starts, so a later hold counts from its own start.
    from app.services.orchestration_adapters import sub_agent as adapter

    ended = SimpleNamespace(payload={}, adapter_state=None)
    end_hold = AsyncMock(return_value=ended)
    monkeypatch.setattr(orchestration_engine, "end_hold", end_hold)
    ctx = SimpleNamespace(db=SimpleNamespace(commit=AsyncMock()), worker_id="worker-b")
    execution = SimpleNamespace(
        id=uuid.uuid4(), adapter_state=adapter.HANDED_BACK_STATE,
        payload={"hold": {"since": "t", "count": 1, "reason": adapter.HANDED_BACK_STATE, "attempts": 1}},
    )

    assert await adapter._end_hand_back(ctx, execution) is ended

    end_hold.assert_awaited_once_with(
        ctx.db, execution_id=execution.id, worker_id="worker-b", held_adapter_state=True
    )
    ctx.db.commit.assert_awaited_once()


async def test_a_hand_back_reclaimed_from_a_gone_worker_still_ends_its_deferral(monkeypatch):
    # The claim that resumed the hand-back lost its worker before reaching
    # ``_end_hand_back``: ``_take_back`` wrote
    # ``worker_gone`` over ``handed_back`` and kept the record. The record
    # decides, so the next claim still ends the deferral and its state.
    from app.services.orchestration_adapters import sub_agent as adapter

    end_hold = AsyncMock(return_value=SimpleNamespace(payload={}, adapter_state=None))
    monkeypatch.setattr(orchestration_engine, "end_hold", end_hold)
    ctx = SimpleNamespace(db=SimpleNamespace(commit=AsyncMock()), worker_id="worker-c")
    execution = SimpleNamespace(
        id=uuid.uuid4(), adapter_state=orchestration_engine.OWNER_GONE_STATE,
        payload={"hold": {"since": "t", "count": 1, "reason": adapter.HANDED_BACK_STATE, "attempts": 1}},
    )

    await adapter._end_hand_back(ctx, execution)

    end_hold.assert_awaited_once_with(
        ctx.db, execution_id=execution.id, worker_id="worker-c", held_adapter_state=True
    )


@pytest.mark.parametrize("hold", [None, {"since": "t", "count": 2, "reason": "held:ai_disabled"}])
async def test_an_ai_hold_or_no_hold_is_left_to_the_ai_gate(monkeypatch, hold):
    from app.services.orchestration_adapters import sub_agent as adapter

    end_hold = AsyncMock()
    monkeypatch.setattr(orchestration_engine, "end_hold", end_hold)
    execution = SimpleNamespace(id=uuid.uuid4(), adapter_state=None, payload={"hold": hold} if hold else {})

    assert await adapter._end_hand_back(SimpleNamespace(db=None, worker_id="w"), execution) is execution
    end_hold.assert_not_awaited()


def _sql(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))


def _bound_values(clause) -> list:
    compiled = clause.compile(dialect=postgresql.dialect())
    return list(compiled.params.values())


def test_the_legacy_claim_clause_keeps_its_grace_by_default():
    now = datetime.now(UTC)
    with_grace = work_item._orphaned_legacy_claim_clause(now)
    assert now - work_item.LEGACY_CLAIM_GRACE in _bound_values(with_grace)
    assert now - work_item.LEGACY_CLAIM_SAME_AGENT_AGE not in _bound_values(with_grace)


def test_a_same_agent_takeover_waits_for_the_claim_age_instead():
    # Not "no condition": a chat turn or dispatcher still working on a claim it
    # made recently keeps it.
    now = datetime.now(UTC)
    same_agent = work_item._orphaned_legacy_claim_clause(now, quiet=False)
    assert now - work_item.LEGACY_CLAIM_SAME_AGENT_AGE in _bound_values(same_agent)
    assert now - work_item.LEGACY_CLAIM_GRACE not in _bound_values(same_agent)
    assert work_item.LEGACY_CLAIM_SAME_AGENT_AGE > work_item.LEGACY_CLAIM_GRACE


@pytest.mark.parametrize("quiet", [True, False])
def test_every_legacy_takeover_keeps_the_live_holder_guards(quiet):
    clause = work_item._orphaned_legacy_claim_clause(datetime.now(UTC), quiet=quiet)
    sql = _sql(clause)
    # A live run or background task of the agent, or an active flow run
    # started for the item by its work-item trigger, is never displaced.
    assert "assignment_runs" in sql and "sub_agent_tasks" in sql
    assert "flow_runs.trigger_context" in sql and "work_item_id" in _bound_values(clause)


class _Db:
    def __init__(self):
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return SimpleNamespace(scalar_one_or_none=lambda: uuid.uuid4())


def _legacy_item(agent: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(), claimed_by_agent_id=agent, owner_agent_id=agent, claim_notes=None,
        status="in_progress", claimed_by_run_id=None,
    )


async def test_a_run_of_the_holding_agent_takes_its_legacy_claim_over_without_the_grace(monkeypatch):
    agent = uuid.uuid4()
    seen = []
    real = work_item._orphaned_legacy_claim_clause
    monkeypatch.setattr(
        work_item, "_orphaned_legacy_claim_clause", lambda now, **kw: seen.append(kw) or real(now, **kw)
    )
    db = _Db()

    won = await work_item._take_over_orphaned_legacy_claim(
        db, _legacy_item(agent), agent_id=agent, run_id=uuid.uuid4(), now=datetime.now(UTC)
    )

    assert won is True
    assert seen == [{"quiet": False}]


@pytest.mark.parametrize("same_agent,run", [(False, True), (True, False)])
async def test_another_agent_or_a_claim_outside_a_run_still_waits_the_grace(monkeypatch, same_agent, run):
    holder = uuid.uuid4()
    seen = []
    real = work_item._orphaned_legacy_claim_clause
    monkeypatch.setattr(
        work_item, "_orphaned_legacy_claim_clause", lambda now, **kw: seen.append(kw) or real(now, **kw)
    )

    await work_item._take_over_orphaned_legacy_claim(
        _Db(), _legacy_item(holder), agent_id=holder if same_agent else uuid.uuid4(),
        run_id=uuid.uuid4() if run else None, now=datetime.now(UTC),
    )

    assert seen == [{"quiet": True}]
