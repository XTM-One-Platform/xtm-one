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
    db.execute = AsyncMock(side_effect=[_result([assignment]), _result([agent])])
    db.commit = AsyncMock()
    active = SimpleNamespace(id=uuid.uuid4(), status="running", started_at=datetime.now(UTC), created_at=None)
    monkeypatch.setattr(autonomous, "has_active_run", AsyncMock(return_value=True))
    monkeypatch.setattr(assignment_engine, "find_active_run", AsyncMock(return_value=active))
    trigger = AsyncMock()
    monkeypatch.setattr(autonomous, "trigger_assignment", trigger)

    assert await autonomous._process_scheduled_assignments(db) == 0

    trigger.assert_not_awaited()
    db.add.assert_called_once()
    skipped = db.add.call_args.args[0]
    assert isinstance(skipped, AssignmentRun)
    assert skipped.status == "skipped"
    assert f"previous run {active.id} still running since" in skipped.result_summary
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_a_skipped_fire_that_cannot_be_recorded_never_breaks_the_tick(monkeypatch):
    monkeypatch.setattr(assignment_engine, "find_active_run", AsyncMock(side_effect=RuntimeError("db blip")))
    db = MagicMock()
    await autonomous._record_skipped_fire(db, _assignment(name="x"), "*/20 * * * *", NOW)
    db.add.assert_not_called()


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
