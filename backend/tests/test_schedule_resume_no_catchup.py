"""Enabling a schedule assignment never fires a run by itself (#8899).

- a resume stamps ``schedule_resumed_at`` and keeps its audit row;
- the cron advances from the later of the last fire and the resume:
  a cron already passed today, or a week of missed daily occurrences, start
  no run at the next tick; the next run is the next future occurrence;
- an assignment that never ran and was never resumed keeps its creation
  auto-run, and ``trigger_assignment`` stays the immediate path.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.utils import describe_schedule_next_run, next_cron_run, schedule_base
from app.services import autonomous
from app.services.assignment_pause import mark_schedule_resumed, resume_assignment


def _result(items):
    scalars = MagicMock()
    scalars.all.return_value = items
    res = MagicMock()
    res.scalars.return_value = scalars
    return res


def _assignment(cron: str, *, last_triggered_at=None, schedule_resumed_at=None):
    assignment = MagicMock()
    assignment.id = uuid.uuid4()
    assignment.agent_id = uuid.uuid4()
    assignment.user_id = uuid.uuid4()
    assignment.run_as_user_id = None
    assignment.is_company_managed = False
    assignment.enabled = True
    assignment.run_as_all_users = False
    assignment.name = "Weekly report"
    assignment.trigger_type = "schedule"
    assignment.trigger_config = {"cron": cron}
    assignment.last_triggered_at = last_triggered_at
    assignment.schedule_resumed_at = schedule_resumed_at
    return assignment


async def _tick(monkeypatch, assignment) -> AsyncMock:
    agent = MagicMock()
    agent.id = assignment.agent_id
    agent.enabled = True
    agent.is_builtin = False
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_result([assignment]), _result([agent])])
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    monkeypatch.setattr(autonomous, "has_active_run", AsyncMock(return_value=False))
    trigger = AsyncMock()
    monkeypatch.setattr(autonomous, "trigger_assignment", trigger)
    await autonomous._process_scheduled_assignments(db)
    return trigger


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    async def flush(self):
        return None

    def begin_nested(self):
        return _Nested()


def _cron_minutes_ago(minutes: int, now: datetime) -> str:
    past = now - timedelta(minutes=minutes)
    return f"{past.minute} {past.hour} * * *"


# ── the resume stamps the anchor, the audit row is unchanged ─────────────


@pytest.mark.asyncio
async def test_resume_stamps_the_schedule_anchor_and_keeps_its_audit_row():
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        name="Monthly",
        trigger_type="schedule",
        enabled=False,
        paused_at=datetime.now(UTC),
        paused_reason="user_toggle",
        paused_by_user_id=None,
        paused_by_label="x",
        paused_details="d",
        schedule_resumed_at=None,
    )
    user = SimpleNamespace(id=uuid.uuid4(), email="a@example.com")
    before = datetime.now(UTC)
    with patch("app.services.audit.log_audit", new_callable=AsyncMock) as log_audit:
        await resume_assignment(_Session(), assignment=assignment, user=user)
    assert assignment.enabled is True
    assert assignment.schedule_resumed_at is not None and assignment.schedule_resumed_at >= before
    log_audit.assert_awaited_once()
    assert log_audit.await_args.kwargs["action"] == "resume"


def test_the_anchor_is_the_later_of_the_last_fire_and_the_resume():
    fire = datetime(2026, 10, 1, 7, 30, tzinfo=UTC)
    resumed = datetime(2026, 11, 5, 13, 51, tzinfo=UTC)
    a = SimpleNamespace(last_triggered_at=fire, schedule_resumed_at=None)
    mark_schedule_resumed(a, resumed)
    assert schedule_base(a) == resumed
    assert schedule_base(SimpleNamespace(last_triggered_at=resumed, schedule_resumed_at=fire)) == resumed
    assert schedule_base(SimpleNamespace(last_triggered_at=None, schedule_resumed_at=None)) is None


# ── (1) a cron already passed today, enabled: no run, next is tomorrow ────


@pytest.mark.asyncio
async def test_enabling_after_todays_occurrence_passed_starts_no_run(monkeypatch):
    now = datetime.now(UTC)
    cron = _cron_minutes_ago(10, now)
    assignment = _assignment(cron, schedule_resumed_at=now - timedelta(seconds=5))

    trigger = await _tick(monkeypatch, assignment)

    trigger.assert_not_awaited()
    nxt = describe_schedule_next_run({"cron": cron}, schedule_base(assignment), now)
    assert datetime.fromisoformat(nxt["next_run_at"]) > now
    assert datetime.fromisoformat(nxt["next_run_at"]) - now > timedelta(hours=23)


# ── (2) disabled a week with a daily cron, enabled: no catch-up ──────────


@pytest.mark.asyncio
async def test_enabling_after_a_week_off_catches_up_nothing(monkeypatch):
    now = datetime.now(UTC)
    cron = _cron_minutes_ago(10, now)
    assignment = _assignment(
        cron,
        last_triggered_at=now - timedelta(days=7, minutes=10),
        schedule_resumed_at=now - timedelta(seconds=5),
    )

    trigger = await _tick(monkeypatch, assignment)

    trigger.assert_not_awaited()
    assert next_cron_run(cron, schedule_base(assignment)) > now


@pytest.mark.asyncio
async def test_without_the_resume_anchor_the_missed_occurrence_would_fire(monkeypatch):
    """The control case: the same week-old fire with no anchor is due."""
    now = datetime.now(UTC)
    cron = _cron_minutes_ago(10, now)
    assignment = _assignment(cron, last_triggered_at=now - timedelta(days=7, minutes=10))
    monkeypatch.setattr(autonomous, "_schedule_spread_delay", lambda *a, **k: 0, raising=False)

    trigger = await _tick(monkeypatch, assignment)

    trigger.assert_awaited()


@pytest.mark.asyncio
async def test_a_due_occurrence_after_the_resume_still_fires(monkeypatch):
    now = datetime.now(UTC)
    cron = _cron_minutes_ago(1, now)
    assignment = _assignment(cron, schedule_resumed_at=now - timedelta(minutes=5))
    monkeypatch.setattr(autonomous, "_schedule_spread_delay", lambda *a, **k: 0, raising=False)

    trigger = await _tick(monkeypatch, assignment)

    trigger.assert_awaited()


# ── (3) trigger_assignment stays the immediate path ──────────────────────


def test_trigger_assignment_does_not_read_the_schedule_anchor():
    from app.services import assignment_engine

    source = inspect.getsource(assignment_engine.trigger_assignment)
    assert "schedule_resumed_at" not in source
    assert "schedule_base" not in source


def test_every_enable_path_stamps_the_anchor():
    from app.agents import seed
    from app.services import assignment_pause
    from app.services.builtin_tools import agent_builder

    assert "mark_schedule_resumed(assignment)" in inspect.getsource(assignment_pause.resume_assignment)
    assert "_mark_schedule_resumed(assignment)" in inspect.getsource(agent_builder)
    assert "mark_schedule_resumed(existing)" in inspect.getsource(seed)
    assert "last_run = schedule_base(assignment)" in inspect.getsource(autonomous._process_scheduled_assignments)
