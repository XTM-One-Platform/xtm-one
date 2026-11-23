"""The CTEM stage analysts fan out to the command center's users only, and a
run whose deadline expired before any model call returned is not retried.

Instance with seven active users, one of them using the CTEM command center:
the weekly stage-analyst fan-out makes one run per analyst, not seven, and a
run that waited in a saturated queue until its deadline was spent is not
queued again by ``retry_on_failure``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app.agents.builtin_agents import BUILTIN_ASSIGNMENTS
from app.core.config import settings
from app.services import autonomous
from app.services.assignment_engine import _deadline_failure_never_started
from app.services.failure_causes import FailureCause
from tests.agentic_gate_stub import stub_agentic_gate


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected extra execute() call")
        return self._results.pop(0)

    async def commit(self):
        pass

    async def rollback(self):
        pass


def _stage_assignment(trigger_config):
    return SimpleNamespace(id=uuid.uuid4(), name="Weekly discovery assessment", trigger_config=trigger_config)


async def test_ctem_audience_fans_out_to_command_center_users_only(monkeypatch):
    monkeypatch.setattr(settings, "fanout_spread_window_seconds", 0)
    users = [uuid.uuid4() for _ in range(7)]
    ctem_user = users[3]
    seen: list[uuid.UUID] = []

    async def fake_trigger(**kwargs):
        seen.append(kwargs["triggered_by_user_id"])
        return "run-id"

    stub_agentic_gate(monkeypatch)
    monkeypatch.setattr("app.services.assignment_engine.trigger_assignment", fake_trigger)
    db = _FakeDB(
        [
            _RowsResult([(uid, "cyber") for uid in users]),
            _ScalarsResult([ctem_user]),  # the command center's users
            _ScalarsResult([]),  # no recent run
        ]
    )

    triggered = await autonomous._fanout_assignment_to_users(
        db,
        _stage_assignment({"cron": "0 8 * * 1", "fanout_audience": "ctem_users"}),
        SimpleNamespace(name="ctem-discovery-analyst"),
        datetime.now(UTC),
        "0 8 * * 1",
    )

    assert triggered == 1
    assert seen == [ctem_user]


async def test_ctem_audience_without_any_user_makes_no_run(monkeypatch):
    async def fake_trigger(**kwargs):
        raise AssertionError("no run expected")

    stub_agentic_gate(monkeypatch)
    monkeypatch.setattr("app.services.assignment_engine.trigger_assignment", fake_trigger)
    db = _FakeDB(
        [
            _RowsResult([(uuid.uuid4(), "cyber") for _ in range(3)]),
            _ScalarsResult([]),
        ]
    )

    triggered = await autonomous._fanout_assignment_to_users(
        db,
        _stage_assignment({"cron": "0 8 * * 1", "fanout_audience": "ctem_users"}),
        SimpleNamespace(name="ctem-discovery-analyst"),
        datetime.now(UTC),
        "0 8 * * 1",
    )

    assert triggered == 0


async def test_fanout_without_audience_still_targets_every_active_user(monkeypatch):
    monkeypatch.setattr(settings, "fanout_spread_window_seconds", 0)
    users = [uuid.uuid4() for _ in range(3)]
    seen: list[uuid.UUID] = []

    async def fake_trigger(**kwargs):
        seen.append(kwargs["triggered_by_user_id"])
        return "run-id"

    stub_agentic_gate(monkeypatch)
    monkeypatch.setattr("app.services.assignment_engine.trigger_assignment", fake_trigger)
    db = _FakeDB([_RowsResult([(uid, "cyber") for uid in users]), _ScalarsResult([])])

    triggered = await autonomous._fanout_assignment_to_users(
        db,
        _stage_assignment({"cron": "0 8 1 * *"}),
        SimpleNamespace(name="exposure-briefing-analyst"),
        datetime.now(UTC),
        "0 8 1 * *",
    )

    assert triggered == 3
    assert seen == users


def test_seeded_stage_analysts_fan_out_to_ctem_users_only():
    weekly = [
        data
        for data in BUILTIN_ASSIGNMENTS
        if data["trigger_type"] == "schedule"
        and data.get("run_as_all_users")
        and str(data["name"]).startswith("Weekly ")
        and str(data["name"]).endswith(" assessment")
    ]
    assert len(weekly) == 5
    for data in weekly:
        assert data["trigger_config"].get("fanout_audience") == "ctem_users", data["name"]


def test_deadline_failure_before_any_model_call_is_not_retried():
    run = SimpleNamespace(failure_cause=FailureCause.ENGINE_DEADLINE_EXCEEDED.value)
    assert _deadline_failure_never_started(run, None) is True


def test_deadline_failure_after_a_model_call_keeps_its_retry():
    run = SimpleNamespace(failure_cause=FailureCause.ENGINE_DEADLINE_EXCEEDED.value)
    assert _deadline_failure_never_started(run, {"input_tokens": 10}) is False


def test_execution_failure_keeps_its_retry():
    run = SimpleNamespace(failure_cause="provider_error")
    assert _deadline_failure_never_started(run, None) is False
