"""Cron windows a starved scheduler never reached are recorded, never silent (#2562).

- the late fire serves the latest due window and is stamped on its cron slot;
- each earlier missed window is a ``skipped`` run (one summary run past three);
- a sweep on time records nothing and fires as before;
- the overlap guard still records its own ``skipped`` run, not a missed one;
- a tick past its window logs one WARNING with its duration and slowest step.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.utils import next_cron_run, previous_cron_run
from app.services import assignment_engine, autonomous, schedule_overlap


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 11, 27, hour, minute, second, tzinfo=UTC)


def _result(items):
    scalars = MagicMock()
    scalars.all.return_value = items
    res = MagicMock()
    res.scalars.return_value = scalars
    return res


def _sweep(monkeypatch, *, last_triggered_at, now, cron="*/15 * * * *", active=False, db_results=()):
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
    trigger = AsyncMock(return_value=uuid.uuid4())
    monkeypatch.setattr(autonomous, "trigger_assignment", trigger)
    return assignment, db, trigger


def _added(db):
    return [call.args[0] for call in db.add.call_args_list]


# ── the window helpers ─────────────────────────────────────────────────────


def test_previous_cron_run_is_the_slot_at_or_before():
    assert previous_cron_run("*/15 * * * *", _at(5, 42, 17)) == _at(5, 30)
    assert previous_cron_run("*/15 * * * *", _at(5, 30)) == _at(5, 30)
    assert previous_cron_run("nope", _at(5, 30)) is None
    assert previous_cron_run(None, _at(5, 30)) is None


def test_missed_windows_of_the_issue_case():
    served, missed, truncated = schedule_overlap.missed_windows("*/15 * * * *", _at(4, 45), _at(5, 42, 17))
    assert served == _at(5, 30)
    assert missed == [_at(5, 0), _at(5, 15)]
    assert not truncated


def test_a_sweep_on_time_misses_nothing():
    served, missed, truncated = schedule_overlap.missed_windows("*/15 * * * *", _at(5, 0), _at(5, 16))
    assert served == _at(5, 15)
    assert missed == [] and not truncated


def test_the_count_of_missed_windows_is_bounded():
    served, missed, truncated = schedule_overlap.missed_windows("* * * * *", _at(0, 0) - timedelta(days=3), _at(0, 0))
    assert served == _at(0, 0)
    assert len(missed) == schedule_overlap.MISSED_SCAN_LIMIT and truncated


def test_past_three_windows_one_summary_run_names_them_all():
    assignment = SimpleNamespace(id=1, agent_id=2, user_id=3, run_as_user_id=None, is_company_managed=False)
    missed = [_at(1, 0) + timedelta(minutes=15 * i) for i in range(4)]
    runs = schedule_overlap.missed_window_runs(assignment, missed, cron="*/15 * * * *", now=_at(5, 42))
    assert len(runs) == 1
    assert runs[0]["status"] == "skipped"
    assert runs[0]["trigger_context"][schedule_overlap.MISSED_WINDOW_KEY] == 4
    assert runs[0]["result_summary"] == (
        "Scheduled fire missed: 4 windows missed between 2026-11-27T01:00:00+00:00 and "
        "2026-11-27T01:45:00+00:00, the scheduler did not reach them."
    )
    one = schedule_overlap.missed_window_runs(
        assignment, missed[:1], cron="*/15 * * * *", now=_at(5, 42), reason="scheduler sweep failed: pool timeout"
    )
    assert one[0]["result_summary"] == (
        "Scheduled fire missed: the scheduler did not reach the window of 2026-11-27T01:00:00+00:00 "
        "(scheduler sweep failed: pool timeout)."
    )
    assert one[0]["trigger_context"]["scheduled_at"] == "2026-11-27T01:00:00+00:00"


# ── the sweep ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_late_sweep_fires_once_on_the_latest_slot_and_records_the_missed_windows(monkeypatch):
    assignment, db, trigger = _sweep(monkeypatch, last_triggered_at=_at(4, 45), now=_at(5, 42, 17))

    assert await autonomous._process_scheduled_assignments(db) == 1

    trigger.assert_awaited_once()
    kwargs = trigger.await_args.kwargs
    assert kwargs["payload"]["scheduled_at"] == _at(5, 30).isoformat()
    assert kwargs["trigger_context"]["scheduled_at"] == _at(5, 30).isoformat()
    assert kwargs["trigger_context"]["triggered_at"] == _at(5, 42, 17).isoformat()
    assert assignment.last_triggered_at == _at(5, 30)
    assert next_cron_run("*/15 * * * *", assignment.last_triggered_at) == _at(5, 45)
    skipped = _added(db)
    assert [run.trigger_context["scheduled_at"] for run in skipped] == [
        _at(5, 0).isoformat(),
        _at(5, 15).isoformat(),
    ]
    assert all(run.status == "skipped" for run in skipped)
    assert all(run.result_summary.startswith("Scheduled fire missed:") for run in skipped)


@pytest.mark.asyncio
async def test_the_first_fire_after_a_restart_is_stamped_on_its_slot_and_records_the_restart_gap(monkeypatch):
    # Build 6e4563d, rolling deploy: */15 last fired 05:30, missed 05:45 and
    # 06:00, first tick of the new pod at 06:13:03. A fresh process: no reason known.
    monkeypatch.setattr(autonomous, "_schedule_trouble", None)
    assignment, db, trigger = _sweep(monkeypatch, last_triggered_at=_at(5, 30), now=_at(6, 13, 3))

    assert await autonomous._process_scheduled_assignments(db) == 1

    context = trigger.await_args.kwargs["trigger_context"]
    assert context["scheduled_at"] == _at(6, 0).isoformat()
    assert context["triggered_at"] == _at(6, 13, 3).isoformat()
    assert assignment.last_triggered_at == _at(6, 0)
    skipped = _added(db)
    assert [run.trigger_context["scheduled_at"] for run in skipped] == [_at(5, 45).isoformat()]
    assert skipped[0].result_summary == (
        "Scheduled fire missed: the scheduler did not reach the window of 2026-11-27T05:45:00+00:00."
    )


@pytest.mark.asyncio
async def test_a_sweep_on_time_fires_once_and_records_nothing(monkeypatch):
    assignment, db, trigger = _sweep(monkeypatch, last_triggered_at=_at(5, 0), now=_at(5, 16))

    assert await autonomous._process_scheduled_assignments(db) == 1

    trigger.assert_awaited_once()
    assert trigger.await_args.kwargs["trigger_context"]["scheduled_at"] == _at(5, 15).isoformat()
    assert assignment.last_triggered_at == _at(5, 15)
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_a_long_outage_records_one_summary_run_and_one_fire(monkeypatch):
    # 200 windows of */15 missed: 50 hours.
    last = _at(5, 30) - timedelta(minutes=15 * 201)
    assignment, db, trigger = _sweep(monkeypatch, last_triggered_at=last, now=_at(5, 42, 17))

    assert await autonomous._process_scheduled_assignments(db) == 1

    trigger.assert_awaited_once()
    skipped = _added(db)
    assert len(skipped) == 1
    assert skipped[0].trigger_context[schedule_overlap.MISSED_WINDOW_KEY] == 200
    assert skipped[0].result_summary.startswith("Scheduled fire missed: 200 windows missed between")
    assert assignment.last_triggered_at == _at(5, 30)


@pytest.mark.asyncio
async def test_the_overlap_guard_still_records_its_own_skipped_run(monkeypatch):
    active = SimpleNamespace(
        id=uuid.uuid4(),
        status="running",
        started_at=_at(4, 50),
        created_at=None,
        trigger_context={"type": "schedule"},
    )
    _assignment, db, trigger = _sweep(
        monkeypatch,
        last_triggered_at=_at(4, 45),
        now=_at(5, 42, 17),
        active=True,
        db_results=[_result([active])],
    )
    monkeypatch.setattr(assignment_engine, "find_active_run", AsyncMock(return_value=active))

    assert await autonomous._process_scheduled_assignments(db) == 0

    trigger.assert_not_awaited()
    skipped = _added(db)
    assert len(skipped) == 1
    assert skipped[0].result_summary.startswith("Scheduled fire skipped: previous run")
    assert schedule_overlap.MISSED_WINDOW_KEY not in skipped[0].trigger_context


# ── the tick overrun ───────────────────────────────────────────────────────


def test_a_tick_past_its_window_logs_one_warning_naming_its_slowest_step(monkeypatch, caplog):
    clock = iter([0.0, 1.0, 95.0, 100.0, 100.0])
    monkeypatch.setattr(autonomous.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(autonomous, "_schedule_trouble", None)
    autonomous._tick_clock.reset()  # 0.0
    autonomous._tick_clock.mark("3")  # 1.0
    autonomous._tick_clock.mark("4")  # 95.0
    with caplog.at_level(logging.WARNING, logger=autonomous.logger.name):
        autonomous._warn_if_tick_overran()  # mark("end") 100.0, total 100.0
    lines = [r.getMessage() for r in caplog.records if "overran" in r.getMessage()]
    assert lines == ["Scheduler tick overran its 60s window: 100.0s, slowest step '3' (94.0s)"]
    assert autonomous._schedule_trouble.startswith("scheduler tick overran: 100s")


def test_a_tick_within_its_window_logs_nothing(monkeypatch, caplog):
    clock = iter([0.0, 10.0, 12.0, 12.0])
    monkeypatch.setattr(autonomous.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(autonomous, "_schedule_trouble", None)
    autonomous._tick_clock.reset()
    autonomous._tick_clock.mark("3")
    with caplog.at_level(logging.WARNING, logger=autonomous.logger.name):
        autonomous._warn_if_tick_overran()
    assert not [r for r in caplog.records if "overran" in r.getMessage()]
    assert autonomous._schedule_trouble is None
