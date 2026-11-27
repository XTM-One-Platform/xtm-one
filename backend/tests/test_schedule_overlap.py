"""A scheduled assignment is never starved in silence by a run of its own.

- a due fire the overlap guard drops is recorded as a ``skipped`` run naming
  the run in its way;
- the timeout bounds a run's wall time across its resumes;
- a run with a fire of its schedule due behind it ends at its second graceful
  take-back with no tool call completed, not its third.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import assignment_engine, assignment_run_resume, autonomous, background_checkpoint, schedule_overlap
from app.services.background_checkpoint import COMPLETED_CALLS_KEY, MAX_GRACEFUL_STREAK, RESUME_CAP_REACHED

NOW = datetime(2026, 11, 2, 9, 50, tzinfo=UTC)


def _assignment(**overrides):
    values = {
        "id": uuid.uuid4(),
        "agent_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "run_as_user_id": None,
        "is_company_managed": False,
        "trigger_type": "schedule",
        "trigger_config": {"cron": "*/20 * * * *", "timezone": "Europe/Paris"},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _graceful_context(streak: int, calls: int = 7) -> dict:
    return {
        "resumes": [
            {"attempt": a, "cause": "its worker said it was stopping", "graceful": True, COMPLETED_CALLS_KEY: calls}
            for a in range(2, 2 + streak)
        ]
    }


# ── (1) a dropped fire is a skipped run with its reason ───────────────────


def test_a_skipped_fire_names_the_run_in_its_way_and_since_when():
    active = SimpleNamespace(id="140d163c", status="running", started_at=NOW - timedelta(minutes=110), created_at=None)
    fields = schedule_overlap.skipped_fire_run(_assignment(), active, cron="*/20 * * * *", now=NOW)
    assert fields["status"] == "skipped"
    assert fields["trigger_context"]["type"] == "schedule"
    assert fields["trigger_context"][schedule_overlap.SKIPPED_BEHIND_KEY] == "140d163c"
    assert fields["result_summary"] == (
        "Scheduled fire skipped: previous run 140d163c still running since 2026-11-02T08:00:00+00:00 (110 min)."
    )
    assert fields["started_at"] == fields["completed_at"] == NOW


def _result(items):
    scalars = MagicMock()
    scalars.all.return_value = items
    res = MagicMock()
    res.scalars.return_value = scalars
    return res


@pytest.mark.asyncio
async def test_the_scheduler_records_a_skipped_run_when_a_run_is_active(monkeypatch):
    from app.models.assignment import AssignmentRun

    agent_id = uuid.uuid4()
    assignment = MagicMock()
    assignment.id = uuid.uuid4()
    assignment.agent_id = agent_id
    assignment.user_id = uuid.uuid4()
    assignment.run_as_user_id = None
    assignment.is_company_managed = False
    assignment.enabled = True
    assignment.run_as_all_users = False
    assignment.name = "Sweep"
    assignment.last_triggered_at = datetime.now(UTC) - timedelta(hours=1)
    assignment.trigger_config = {"cron": "*/20 * * * *"}
    agent = MagicMock()
    agent.id = agent_id
    agent.enabled = True
    agent.is_builtin = False

    db = MagicMock()
    active = SimpleNamespace(
        id=uuid.uuid4(),
        status="running",
        started_at=datetime.now(UTC),
        created_at=None,
        trigger_context={"type": "schedule"},
    )
    # A started run is never superseded (#2040): the fire is skipped as before.
    db.execute = AsyncMock(side_effect=[_result([assignment]), _result([agent]), _result([active])])
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    monkeypatch.setattr(autonomous, "has_active_run", AsyncMock(return_value=True))
    monkeypatch.setattr(assignment_engine, "find_active_run", AsyncMock(return_value=active))
    trigger = AsyncMock()
    monkeypatch.setattr(autonomous, "trigger_assignment", trigger)

    assert await autonomous._process_scheduled_assignments(db) == 0

    trigger.assert_not_awaited()
    # The base is an hour back on a */20 cron: one row per slot it covers (#2582).
    skipped = [call.args[0] for call in db.add.call_args_list]
    assert len(skipped) == 3
    assert all(isinstance(run, AssignmentRun) and run.status == "skipped" for run in skipped)
    assert all(f"previous run {active.id} still running since" in run.result_summary for run in skipped)
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_a_skipped_fire_that_cannot_be_recorded_never_breaks_the_tick(monkeypatch):
    monkeypatch.setattr(assignment_engine, "find_active_run", AsyncMock(side_effect=RuntimeError("db blip")))
    db = MagicMock()
    await autonomous._record_skipped_fire(db, _assignment(name="x"), "*/20 * * * *", NOW)
    db.add.assert_not_called()


# ── (1bis) a fire never started is superseded by the next one (#2040) ─────


def _execution_result(execution):
    res = MagicMock()
    res.scalar_one_or_none.return_value = execution
    return res


def _stale_fire(**overrides):
    values = {
        "id": uuid.uuid4(),
        "status": "pending",
        "started_at": None,
        "created_at": NOW - timedelta(minutes=60),
        "trigger_context": {"type": "schedule"},
        "stop_reason": None,
        "result_summary": None,
        "completed_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _db(*results):
    db = MagicMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_a_fire_never_started_is_superseded_and_its_execution_cancelled():
    run = _stale_fire()
    execution = SimpleNamespace(
        state="pending",
        payload={"run_id": str(run.id)},
        lease_owner=None,
        lease_expires_at=None,
        last_error=None,
        updated_at=None,
    )
    db = _db(_execution_result(execution))
    assert await schedule_overlap.supersede_never_started(db, run, now=NOW)
    assert execution.state == "cancelled"
    assert run.status == "skipped"
    assert run.stop_reason == schedule_overlap.SUPERSEDED_STOP_REASON == "superseded"
    assert run.completed_at == NOW
    assert run.result_summary.startswith("Scheduled fire superseded: it waited for a free worker and never started")


@pytest.mark.asyncio
async def test_a_started_or_claimed_fire_is_never_superseded():
    started = _stale_fire(status="running", started_at=NOW - timedelta(minutes=5))
    assert not await schedule_overlap.supersede_never_started(_db(), started, now=NOW)
    handed_back = _stale_fire()
    execution = SimpleNamespace(state="pending", payload={"started_at": NOW.isoformat()})
    assert not await schedule_overlap.supersede_never_started(_db(_execution_result(execution)), handed_back, now=NOW)
    assert handed_back.status == "pending" and execution.state == "pending"
    claimed_meanwhile = _stale_fire()
    assert not await schedule_overlap.supersede_never_started(_db(_execution_result(None)), claimed_meanwhile, now=NOW)
    assert claimed_meanwhile.status == "pending"


def _scheduled(db_results, monkeypatch):
    agent_id = uuid.uuid4()
    assignment = MagicMock()
    assignment.id = uuid.uuid4()
    assignment.agent_id = agent_id
    assignment.user_id = uuid.uuid4()
    assignment.run_as_user_id = None
    assignment.is_company_managed = False
    assignment.enabled = True
    assignment.run_as_all_users = False
    assignment.name = "Alert triage"
    assignment.last_triggered_at = datetime.now(UTC) - timedelta(hours=1)
    assignment.trigger_config = {"cron": "0 * * * *"}
    agent = MagicMock()
    agent.id = agent_id
    agent.enabled = True
    agent.is_builtin = False
    db = _db(_result([assignment]), _result([agent]), *db_results)
    monkeypatch.setattr(autonomous, "has_active_run", AsyncMock(return_value=True))
    trigger = AsyncMock(return_value=uuid.uuid4())
    monkeypatch.setattr(autonomous, "trigger_assignment", trigger)
    return db, trigger


@pytest.mark.asyncio
async def test_the_next_fire_runs_when_the_previous_one_never_started(monkeypatch):
    stale = _stale_fire()
    execution = SimpleNamespace(
        state="pending", payload={}, lease_owner=None, lease_expires_at=None, last_error=None, updated_at=None
    )
    db, trigger = _scheduled([_result([stale]), _execution_result(execution)], monkeypatch)
    find = AsyncMock()
    monkeypatch.setattr(assignment_engine, "find_active_run", find)

    assert await autonomous._process_scheduled_assignments(db) == 1

    trigger.assert_awaited_once()
    assert stale.status == "skipped" and stale.stop_reason == "superseded"
    assert execution.state == "cancelled"
    find.assert_not_awaited()  # no skipped fire recorded: never both lost
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_an_event_run_in_the_way_still_skips_the_fire(monkeypatch):
    event_run = _stale_fire(trigger_context={"type": "integration_event"})
    db, trigger = _scheduled([_result([event_run])], monkeypatch)
    active = SimpleNamespace(id=event_run.id, status="pending", started_at=None, created_at=NOW)
    monkeypatch.setattr(assignment_engine, "find_active_run", AsyncMock(return_value=active))

    assert await autonomous._process_scheduled_assignments(db) == 0

    trigger.assert_not_awaited()
    assert event_run.status == "pending"
    assert db.add.call_args.args[0].status == "skipped"


# ── (2) the timeout bounds the wall time across resumes ───────────────────


def test_a_resume_past_the_timeout_across_resumes_is_refused():
    run = SimpleNamespace(started_at=NOW - timedelta(seconds=1885))
    reason = schedule_overlap.wall_time_refusal(run, effective_timeout=900, now=NOW)
    assert reason is not None and reason.startswith(RESUME_CAP_REACHED)
    assert schedule_overlap.WALL_TIME_EXCEEDED in reason
    assert "has run 1885s across its resumes, past its timeout of 900s" in reason
    assert "work item claims are released" in reason


def test_a_resume_within_the_timeout_or_without_one_goes_on():
    run = SimpleNamespace(started_at=NOW - timedelta(seconds=600))
    assert schedule_overlap.wall_time_refusal(run, effective_timeout=900, now=NOW) is None
    late = SimpleNamespace(started_at=NOW - timedelta(hours=5))
    assert schedule_overlap.wall_time_refusal(late, effective_timeout=None, now=NOW) is None, "timeout disabled"
    assert schedule_overlap.wall_time_refusal(SimpleNamespace(started_at=None), effective_timeout=900, now=NOW) is None
    naive = SimpleNamespace(started_at=(NOW - timedelta(seconds=1000)).replace(tzinfo=None))
    assert schedule_overlap.wall_time_refusal(naive, effective_timeout=900, now=NOW) is not None


def test_the_engine_keeps_the_first_start_and_reads_the_wall_time_before_resuming():
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    # A HITL resume keeps it too (#1645).
    assert (
        "run.started_at = (run.started_at if (_resuming or _hitl_resume) else None) or datetime.now(UTC)" in source
    )
    wall = source.index("schedule_overlap.wall_time_refusal(run, effective_timeout=_wall_timeout)")
    assert wall < source.index("assignment_run_resume.resume_refused(")
    assert wall < source.index("await record_agentic_execution(SOURCE_ASSIGNMENT)")
    assert '"wall_time" if _wall_refused else "resume_cap"' in source


# ── (3) a fire due behind a run tightens its graceful streak ──────────────


def test_a_fire_due_behind_the_run_gives_it_one_graceful_resume_in_a_row():
    run = SimpleNamespace(created_at=NOW - timedelta(minutes=30))
    assert schedule_overlap.fire_due_behind(_assignment(), run, now=NOW)
    assert schedule_overlap.graceful_streak_bound(_assignment(), run, now=NOW) == 1
    fresh = SimpleNamespace(created_at=NOW - timedelta(minutes=5))  # next fire at 10:00Z
    assert schedule_overlap.graceful_streak_bound(_assignment(), fresh, now=NOW) == MAX_GRACEFUL_STREAK
    manual = _assignment(trigger_type="manual", trigger_config={})
    assert schedule_overlap.graceful_streak_bound(manual, run, now=NOW) == MAX_GRACEFUL_STREAK
    broken = _assignment(trigger_config={"cron": ["*/20"]})
    assert schedule_overlap.graceful_streak_bound(broken, run, now=NOW) == MAX_GRACEFUL_STREAK


def test_a_starving_run_ends_at_its_second_flat_take_back_not_its_third():
    one = _graceful_context(1)
    handed = background_checkpoint.handed_back(one, attempt=3, signal="SIGTERM")
    # Without a fire due: the second take-back is resumed (bound 3).
    assert assignment_run_resume.resume_refused(handed, attempt=3, completed_calls=7) is None
    reason = assignment_run_resume.resume_refused(handed, attempt=3, completed_calls=7, graceful_streak_bound=1)
    assert reason is not None and reason.startswith(RESUME_CAP_REACHED)
    assert "A fire of its schedule is due behind it, so it was given 1 graceful resume(s) in a row" in reason
    # Progress between the take-backs restarts the streak, starving or not.
    assert assignment_run_resume.resume_refused(handed, attempt=3, completed_calls=8, graceful_streak_bound=1) is None
    # The first take-back is always resumed.
    first = background_checkpoint.handed_back({}, attempt=2, signal="SIGTERM")
    assert assignment_run_resume.resume_refused(first, attempt=2, completed_calls=7, graceful_streak_bound=1) is None


def test_the_bound_never_loosens_the_default():
    context = _graceful_context(MAX_GRACEFUL_STREAK)
    handed = background_checkpoint.handed_back(context, attempt=5, signal="SIGTERM")
    assert assignment_run_resume.resume_refused(handed, attempt=5, completed_calls=7, graceful_streak_bound=99)
