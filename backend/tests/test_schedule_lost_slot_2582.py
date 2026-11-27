"""A cron slot is never consumed without a run or a ``skipped`` row (#2582).

- a fire whose run creation is stopped (``CancelledError``) or raises after the
  schedule base was committed records the slot as a ``skipped`` lost fire;
- behind an active run, each due slot is a ``skipped`` row stamped on it, and
  the base moves to the latest slot with the rows, never without them;
- an active run that ended between ``has_active_run`` and ``find_active_run``
  lets the fire go through instead of consuming the slot in silence;
- the claim and hold releases of the tick stop at their time budget, and a
  release step stuck past it is cancelled without holding the schedules.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.work_item as wi_service
from app.services import assignment_engine, autonomous, schedule_overlap


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 11, 27, hour, minute, second, tzinfo=UTC)


def _result(items):
    scalars = MagicMock()
    scalars.all.return_value = items
    res = MagicMock()
    res.scalars.return_value = scalars
    return res


def _sweep(monkeypatch, *, last_triggered_at, now, cron="*/15 * * * *", active=False, db_results=(), trigger=None):
    agent_id = uuid.uuid4()
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        run_as_user_id=None,
        is_company_managed=False,
        enabled=True,
        run_as_all_users=False,
        name="Sweep",
        last_triggered_at=last_triggered_at,
        schedule_resumed_at=None,
        trigger_config={"cron": cron},
        trigger_type="schedule",
    )
    agent = SimpleNamespace(id=agent_id, enabled=True, is_builtin=False, name="Agent")
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_result([assignment]), _result([agent]), *db_results])
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    monkeypatch.setattr(autonomous, "_is_expired", lambda _obj: False)
    monkeypatch.setattr(autonomous, "has_active_run", AsyncMock(return_value=active))

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(autonomous, "datetime", _Clock)
    trigger = trigger or AsyncMock(return_value=uuid.uuid4())
    monkeypatch.setattr(autonomous, "trigger_assignment", trigger)
    return assignment, db, trigger


class _OwnSession:
    """The session ``_record_lost_fire`` opens: counts the runs created since the tick, then writes."""

    def __init__(self, landed: int = 0):
        self.landed = landed
        self.added: list = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _statement):
        res = MagicMock()
        res.scalar.return_value = self.landed
        return res

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


def _own_session(monkeypatch, landed: int = 0) -> _OwnSession:
    session = _OwnSession(landed)
    monkeypatch.setattr(autonomous, "async_session", lambda: session)
    return session


def _active_run():
    return SimpleNamespace(
        id=uuid.uuid4(), status="running", started_at=_at(6, 50), created_at=None, trigger_context={"type": "schedule"}
    )


# ── (1) the base never moves past a slot without a row ────────────────────


@pytest.mark.asyncio
async def test_a_fire_stopped_after_the_base_commit_records_its_slot(monkeypatch):
    own = _own_session(monkeypatch)
    assignment, db, trigger = _sweep(
        monkeypatch,
        last_triggered_at=_at(7, 0),
        now=_at(7, 16),
        trigger=AsyncMock(side_effect=asyncio.CancelledError()),
    )

    with pytest.raises(asyncio.CancelledError):
        await autonomous._process_scheduled_assignments(db)

    trigger.assert_awaited_once()
    assert assignment.last_triggered_at == _at(7, 15)
    assert own.committed and len(own.added) == 1
    lost = own.added[0]
    assert lost.status == "skipped"
    assert lost.trigger_context["scheduled_at"] == _at(7, 15).isoformat()
    assert lost.trigger_context[schedule_overlap.LOST_FIRE_KEY] is True
    assert lost.result_summary == (
        "Scheduled fire lost: the run of the window of 2026-11-27T07:15:00+00:00 was not created "
        "(the scheduler tick was stopped)."
    )


@pytest.mark.asyncio
async def test_a_fire_whose_creation_raises_records_its_slot(monkeypatch):
    own = _own_session(monkeypatch)
    _assignment, db, _trigger = _sweep(
        monkeypatch,
        last_triggered_at=_at(7, 0),
        now=_at(7, 16),
        trigger=AsyncMock(side_effect=RuntimeError("pool timeout")),
    )

    assert await autonomous._process_scheduled_assignments(db) == 0

    assert len(own.added) == 1
    assert own.added[0].result_summary.endswith("was not created (pool timeout).")


@pytest.mark.asyncio
async def test_a_run_created_before_the_stop_needs_no_lost_record(monkeypatch):
    own = _own_session(monkeypatch, landed=1)
    _assignment, db, _trigger = _sweep(
        monkeypatch,
        last_triggered_at=_at(7, 0),
        now=_at(7, 16),
        trigger=AsyncMock(side_effect=asyncio.CancelledError()),
    )

    with pytest.raises(asyncio.CancelledError):
        await autonomous._process_scheduled_assignments(db)

    assert own.added == [] and not own.committed


# ── (2) an active run that vanished between the two reads ─────────────────


@pytest.mark.asyncio
async def test_an_active_run_gone_between_the_reads_lets_the_fire_through(monkeypatch):
    active = _active_run()
    assignment, db, trigger = _sweep(
        monkeypatch,
        last_triggered_at=_at(7, 0),
        now=_at(7, 16),
        active=True,
        db_results=[_result([active])],  # the supersede read: a started run, kept
    )
    monkeypatch.setattr(assignment_engine, "find_active_run", AsyncMock(return_value=None))

    assert await autonomous._process_scheduled_assignments(db) == 1

    trigger.assert_awaited_once()
    assert trigger.await_args.kwargs["trigger_context"]["scheduled_at"] == _at(7, 15).isoformat()
    assert assignment.last_triggered_at == _at(7, 15)
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_a_skipped_fire_that_cannot_be_recorded_leaves_the_base(monkeypatch):
    active = _active_run()
    assignment, db, trigger = _sweep(
        monkeypatch,
        last_triggered_at=_at(7, 0),
        now=_at(7, 16),
        active=True,
        db_results=[_result([active])],
    )
    monkeypatch.setattr(assignment_engine, "find_active_run", AsyncMock(side_effect=RuntimeError("db blip")))

    assert await autonomous._process_scheduled_assignments(db) == 0

    trigger.assert_not_awaited()
    assert assignment.last_triggered_at == _at(7, 0)
    db.rollback.assert_awaited()


# ── (3) one skipped row per slot, stamped on the slot ─────────────────────


@pytest.mark.asyncio
async def test_each_slot_behind_an_active_run_is_a_skipped_row_on_its_slot(monkeypatch):
    active = _active_run()
    assignment, db, trigger = _sweep(
        monkeypatch,
        last_triggered_at=_at(7, 0),
        now=_at(7, 46),
        active=True,
        db_results=[_result([active])],
    )
    monkeypatch.setattr(assignment_engine, "find_active_run", AsyncMock(return_value=active))

    assert await autonomous._process_scheduled_assignments(db) == 0

    trigger.assert_not_awaited()
    skipped = [call.args[0] for call in db.add.call_args_list]
    assert [run.trigger_context["scheduled_at"] for run in skipped] == [
        _at(7, 15).isoformat(),
        _at(7, 30).isoformat(),
        _at(7, 45).isoformat(),
    ]
    assert all(run.status == "skipped" for run in skipped)
    assert all(run.trigger_context[schedule_overlap.SKIPPED_BEHIND_KEY] == str(active.id) for run in skipped)
    assert assignment.last_triggered_at == _at(7, 45)
    db.commit.assert_awaited()


def test_past_the_cap_one_summary_row_names_the_slots():
    assignment = SimpleNamespace(id=1, agent_id=2, user_id=3, run_as_user_id=None, is_company_managed=False)
    active = SimpleNamespace(id="140d163c", status="running", started_at=_at(1, 0), created_at=None)
    slots = [_at(2, 0), _at(2, 15), _at(2, 30), _at(2, 45)]
    runs = schedule_overlap.skipped_fire_runs(assignment, active, cron="*/15 * * * *", now=_at(2, 50), slots=slots)
    assert len(runs) == 1
    assert runs[0]["trigger_context"]["scheduled_at"] == _at(2, 0).isoformat()
    assert runs[0]["trigger_context"][schedule_overlap.SKIPPED_WINDOWS_KEY] == 4
    assert runs[0]["result_summary"].startswith(
        "Scheduled fire skipped: 4 windows between 2026-11-27T02:00:00+00:00 and 2026-11-27T02:45:00+00:00; "
        "previous run 140d163c still running since"
    )


def test_due_slots_with_no_base_answers_nothing():
    assert schedule_overlap.due_slots("*/15 * * * *", None, _at(7, 46)) == (None, [], False)
    assert schedule_overlap.due_slots("*/15 * * * *", _at(7, 45), _at(7, 46)) == (None, [], False)


# ── (4) the housekeeping steps are time-budgeted ──────────────────────────


@pytest.mark.asyncio
async def test_a_release_step_stuck_past_its_budget_is_cancelled_and_logged(monkeypatch, caplog):
    monkeypatch.setattr(autonomous, "_HOUSEKEEPING_BUDGET_SECONDS", 0.01)
    monkeypatch.setattr(autonomous, "_HOUSEKEEPING_GRACE_SECONDS", 0.01)
    stuck = asyncio.Event()

    async def slow(_deadline):
        await stuck.wait()  # a pool that never answers
        return 7

    with caplog.at_level(logging.WARNING, logger=autonomous.logger.name):
        assert await autonomous._within_housekeeping_budget("work item claim release", slow) == 0

    assert any("'work item claim release' cancelled past its" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_release_step_within_its_budget_answers_its_count():
    seen = {}

    async def quick(deadline):
        seen["deadline"] = deadline
        return 3

    assert await autonomous._within_housekeeping_budget("shared-state hold release", quick) == 3
    assert seen["deadline"] > 0


class _ReleaseSession:
    def __init__(self, rows):
        self.rows = rows
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _statement):
        res = MagicMock()
        res.all.return_value = self.rows
        return res

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_the_claim_release_stops_at_its_deadline_and_commits_what_it_did(monkeypatch):
    run_id = uuid.uuid4()
    session = _ReleaseSession([(uuid.uuid4(), run_id, [])])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    monkeypatch.setattr(wi_service, "holding_runs", AsyncMock(return_value={run_id: SimpleNamespace(alive=False)}))

    assert await wi_service.release_ended_run_claims(deadline=0.0) == 0
    assert session.committed


def test_the_tick_releases_within_a_budget_and_publishes_off_the_tick():
    source = inspect.getsource(autonomous._run_autonomous_tick_inner)
    assert "release_ended_run_claims(deadline=deadline, background_events=True)" in source
    assert "release_ended_run_holds(deadline=deadline)" in source
