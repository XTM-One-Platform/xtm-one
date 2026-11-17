"""Per-user fan-out reliability.

Production incident: the weekly CTEM stage assessments fan out one run per
active user, all created with the same ``run_at``. The reconciler caps
concurrency at ``max_concurrent_runs + max_concurrent_sub_agent_tasks``
(default 15), so with 80+ users x 5 stage assignments due at Monday 08:00:

  * queued runs burn their orchestration deadline (``run_at + timeout +
    grace``) while WAITING for a slot and are dead-lettered as timeouts
    without ever executing, and
  * the concurrent agentic loops trip the LLM provider's rate limits, whose
    in-call 429/529 backoff inflates run durations and pushes even more
    queued runs past their deadline.

Fix under test: ``_fanout_spread_delay_seconds`` spreads the per-user runs
evenly across ``settings.fanout_spread_window_seconds`` (default 1h), passed
into ``trigger_assignment(extra_delay_seconds=...)`` so each run's deadline
starts at its own scheduled start; the seeded fan-out assignments also enable
``retry_on_failure`` so residual transient failures re-run instead of leaving
a hole until the next cadence tick.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app.agents.builtin_agents import BUILTIN_ASSIGNMENTS
from app.core.config import settings
from app.services import autonomous
from app.services.autonomous import _fanout_spread_delay_seconds
from tests.agentic_gate_stub import stub_agentic_gate

# ---------------------------------------------------------------------------
# _fanout_spread_delay_seconds — spread math
# ---------------------------------------------------------------------------


def test_spread_is_even_across_the_window(monkeypatch):
    monkeypatch.setattr(settings, "fanout_spread_window_seconds", 3600)
    delays = [_fanout_spread_delay_seconds(i, 4) for i in range(4)]
    assert delays == [0, 900, 1800, 2700]


def test_spread_first_run_is_always_immediate(monkeypatch):
    monkeypatch.setattr(settings, "fanout_spread_window_seconds", 3600)
    assert _fanout_spread_delay_seconds(0, 500) == 0


def test_spread_is_bounded_by_the_window_regardless_of_fleet_size(monkeypatch):
    monkeypatch.setattr(settings, "fanout_spread_window_seconds", 3600)
    assert _fanout_spread_delay_seconds(999, 1000) < 3600


def test_spread_disabled_window_returns_zero(monkeypatch):
    monkeypatch.setattr(settings, "fanout_spread_window_seconds", 0)
    assert _fanout_spread_delay_seconds(3, 10) == 0


def test_spread_single_user_returns_zero(monkeypatch):
    monkeypatch.setattr(settings, "fanout_spread_window_seconds", 3600)
    assert _fanout_spread_delay_seconds(0, 1) == 0


# ---------------------------------------------------------------------------
# _fanout_assignment_to_users — wiring into trigger_assignment
# ---------------------------------------------------------------------------


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
        return self._results.pop(0)

    async def commit(self):
        pass

    async def rollback(self):
        pass


def _patch_fanout(monkeypatch, *, trigger):
    stub_agentic_gate(monkeypatch)
    monkeypatch.setattr("app.services.assignment_engine.trigger_assignment", trigger)


async def test_fanout_passes_staggered_extra_delay_per_user(monkeypatch):
    """Each eligible user's run gets an increasing, evenly-spaced delay."""
    monkeypatch.setattr(settings, "fanout_spread_window_seconds", 300)
    users = [uuid.uuid4() for _ in range(3)]
    seen: list[tuple[uuid.UUID, int]] = []

    async def fake_trigger(**kwargs):
        seen.append(
            (kwargs["triggered_by_user_id"], kwargs["extra_delay_seconds"])
        )
        return "run-id"

    _patch_fanout(monkeypatch, trigger=fake_trigger)
    db = _FakeDB(
        [
            _RowsResult([(uid, "cyber") for uid in users]),
            _ScalarsResult([]),  # no recent runs — everyone eligible
        ]
    )
    assignment = SimpleNamespace(id=uuid.uuid4(), name="Weekly discovery assessment")
    agent = SimpleNamespace(name="ctem-discovery-analyst")

    triggered = await autonomous._fanout_assignment_to_users(
        db, assignment, agent, datetime.now(UTC), "0 8 * * 1"
    )

    assert triggered == 3
    assert [d for _, d in seen] == [0, 100, 200]


async def test_fanout_spread_ignores_deduped_users(monkeypatch):
    """The spread is computed over ELIGIBLE users only — dedup'd users must
    not leave holes that stretch the tail of the window."""
    monkeypatch.setattr(settings, "fanout_spread_window_seconds", 300)
    users = [uuid.uuid4() for _ in range(4)]
    seen: list[int] = []

    async def fake_trigger(**kwargs):
        seen.append(kwargs["extra_delay_seconds"])
        return "run-id"

    _patch_fanout(monkeypatch, trigger=fake_trigger)
    db = _FakeDB(
        [
            _RowsResult([(uid, "cyber") for uid in users]),
            _ScalarsResult(users[:2]),  # first two already ran recently
        ]
    )
    assignment = SimpleNamespace(id=uuid.uuid4(), name="Weekly discovery assessment")
    agent = SimpleNamespace(name="ctem-discovery-analyst")

    triggered = await autonomous._fanout_assignment_to_users(
        db, assignment, agent, datetime.now(UTC), "0 8 * * 1"
    )

    assert triggered == 2
    assert seen == [0, 150]


# ---------------------------------------------------------------------------
# Seed data — fan-out assignments retry transient failures
# ---------------------------------------------------------------------------


def test_all_seeded_fanout_assignments_retry_on_failure():
    """Every scheduled ``run_as_all_users`` built-in assignment must enable
    ``retry_on_failure`` — a transient LLM rate-limit / timeout failure must
    re-run instead of leaving that user without an assessment until the next
    weekly/monthly tick."""
    fanouts = [
        data
        for data in BUILTIN_ASSIGNMENTS
        if data["trigger_type"] == "schedule" and data.get("run_as_all_users")
    ]
    assert fanouts, "expected at least one seeded fan-out assignment"
    for data in fanouts:
        assert data.get("retry_on_failure") is True, (
            f"Seeded fan-out assignment '{data['name']}' must set "
            "retry_on_failure"
        )
        assert data.get("max_retries"), (
            f"Seeded fan-out assignment '{data['name']}' must set max_retries"
        )
