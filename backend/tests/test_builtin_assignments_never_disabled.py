"""Built-in (seeded) assignments must never end up durably disabled.

Production incident: the auth-failure quarantine auto-disabled the
seeded CTEM stage assignments (per-user fan-outs) because ONE user's expired
OpenCTI/OpenAEV credential crossed the per-assignment threshold — then every
queued fan-out run failed with "Assignment is disabled", and the next boot's
seed sync silently re-enabled the assignment, restarting the loop.

Guarantees covered here:
  * ``seed_builtin_assignments`` re-enables a built-in assignment that was
    disabled at runtime (the seed is the source of truth).
  * Every ``BUILTIN_ASSIGNMENTS`` seed entry is enabled — a disabled seed
    would contradict the never-disabled invariant.
  * Every schedule-triggered ``BUILTIN_ASSIGNMENTS`` entry carries a valid
    cron expression, so the scheduler's invalid-cron disable path (which
    built-in assignments are now exempt from) can never be needed for them.

The quarantine exemption itself is covered in
``test_assignment_auth_quarantine.py``.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from croniter import croniter

from app.agents.builtin_agents import BUILTIN_ASSIGNMENTS
from app.agents.seed import seed_builtin_assignments

# ---------------------------------------------------------------------------
# Seed data invariants
# ---------------------------------------------------------------------------


def test_all_builtin_assignments_are_seeded_enabled():
    """A built-in assignment seed entry must never ship disabled - except an
    ``admin_opt_in`` one, which is born off for an administrator to
    turn on, and which the seed then never re-applies either way."""
    for data in BUILTIN_ASSIGNMENTS:
        if data.get("admin_opt_in"):
            assert data.get("enabled") is False, data["name"]
            continue
        assert data.get("enabled", True) is True, (
            f"Built-in assignment '{data['name']}' is seeded disabled — "
            "built-in assignments are always enabled"
        )


def test_all_builtin_schedule_assignments_have_valid_cron():
    """Seed cron expressions must parse — built-ins are exempt from the
    scheduler's invalid-cron auto-disable, so an invalid one must never ship."""
    scheduled = [
        data for data in BUILTIN_ASSIGNMENTS if data["trigger_type"] == "schedule"
    ]
    assert scheduled, "expected at least one schedule-triggered built-in assignment"
    for data in scheduled:
        cron = (data.get("trigger_config") or {}).get("cron")
        assert isinstance(cron, str) and cron.strip(), (
            f"Built-in assignment '{data['name']}' has no cron expression"
        )
        assert croniter.is_valid(cron), (
            f"Built-in assignment '{data['name']}' has an invalid cron "
            f"expression: {cron!r}"
        )


# ---------------------------------------------------------------------------
# Scheduler: invalid cron on a BUILT-IN assignment must not disable it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_invalid_cron_on_builtin_assignment_does_not_disable():
    """A built-in assignment with a broken cron is skipped (log-once), never
    disabled and no failed misconfiguration run is persisted — disabling would
    only fight the boot-time seed re-enable in a loop."""
    from app.services import autonomous

    agent_id = uuid.uuid4()
    assignment = MagicMock()
    assignment.id = uuid.uuid4()
    assignment.agent_id = agent_id
    assignment.user_id = None
    assignment.run_as_user_id = None
    assignment.is_company_managed = True
    assignment.enabled = True
    assignment.name = "Weekly discovery assessment"
    assignment.last_triggered_at = None
    assignment.trigger_config = {"cron": "not a cron"}

    agent = MagicMock()
    agent.id = agent_id
    agent.enabled = True
    agent.is_builtin = True

    def _result(items: list) -> MagicMock:
        scalars = MagicMock()
        scalars.all.return_value = items
        res = MagicMock()
        res.scalars.return_value = scalars
        return res

    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_result([assignment]), _result([agent])])
    db.commit = AsyncMock()
    db.add = MagicMock()

    snapshot = set(autonomous._cron_warned_once)
    autonomous._cron_warned_once.clear()
    try:
        count = await autonomous._process_scheduled_assignments(db)
    finally:
        autonomous._cron_warned_once.clear()
        autonomous._cron_warned_once.update(snapshot)

    assert count == 0
    assert assignment.enabled is True  # never disabled
    db.add.assert_not_called()  # no failed misconfiguration run persisted


# ---------------------------------------------------------------------------
# seed_builtin_assignments — re-enable at boot
# ---------------------------------------------------------------------------


class _Scalars:
    def __init__(self, items: list):
        self._items = items

    def all(self) -> list:
        return self._items

    def first(self):
        return self._items[0] if self._items else None


class _Result:
    """Minimal stand-in for a SQLAlchemy ``Result``."""

    def __init__(self, *, scalar=None, items: list | None = None):
        self._scalar = scalar
        self._items = items if items is not None else ([scalar] if scalar else [])

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self) -> _Scalars:
        return _Scalars(self._items)


@pytest.mark.asyncio
async def test_seed_sync_reenables_disabled_builtin_assignment(monkeypatch):
    """The boot-time seed sync must flip a disabled built-in assignment back
    to enabled — whatever disabled it at runtime (manual toggle, legacy
    auto-quarantine), the seed remains the source of truth."""
    agent = MagicMock()
    agent.id = uuid.uuid4()
    agent.slug = "ctem-discovery-analyst"
    agent.user_id = None
    agent.is_company_managed = True

    existing = MagicMock()
    existing.enabled = False  # disabled at runtime (e.g. legacy quarantine)
    existing.user_id = None

    seed_entry = {
        "agent_slug": agent.slug,
        "name": "Weekly discovery assessment",
        "description": "Test entry",
        "trigger_type": "schedule",
        "trigger_config": {"cron": "0 8 * * 1"},
        "run_as_all_users": True,
        "prompt": "Assess the discovery stage.",
        "enabled": True,
    }
    monkeypatch.setattr(
        "app.agents.seed.BUILTIN_ASSIGNMENTS", [seed_entry], raising=True
    )

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _Result(scalar=agent),  # phase 1: agent lookup for stale sweep
            _Result(items=[]),  # phase 1: no stale assignments
            _Result(scalar=agent),  # phase 2: agent lookup
            _Result(scalar=existing, items=[existing]),  # phase 2: existing row
        ]
    )
    db.commit = AsyncMock()
    db.flush = AsyncMock()

    inserted = await seed_builtin_assignments(db, platform_mode="xtm_one")

    assert inserted == 0  # updated in place, nothing inserted
    assert existing.enabled is True
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_seed_sync_keeps_an_emergency_stop_pause(monkeypatch):
    """A restart during an emergency stop must not undo it: the seed leaves a
    built-in assignment the stop paused paused, stamp included, so only
    lifting the stop re-enables it."""
    from datetime import UTC, datetime

    from app.services.assignment_pause import PANIC_STOP_REASON

    agent = MagicMock()
    agent.id = uuid.uuid4()
    agent.slug = "ctem-discovery-analyst"
    agent.user_id = None
    agent.is_company_managed = True

    stopped_at = datetime.now(UTC)
    existing = MagicMock()
    existing.enabled = False
    existing.user_id = None
    existing.paused_reason = PANIC_STOP_REASON
    existing.paused_at = stopped_at

    seed_entry = {
        "agent_slug": agent.slug,
        "name": "Weekly discovery assessment",
        "description": "Test entry",
        "trigger_type": "schedule",
        "trigger_config": {"cron": "0 8 * * 1"},
        "run_as_all_users": True,
        "prompt": "Assess the discovery stage.",
        "enabled": True,
    }
    monkeypatch.setattr(
        "app.agents.seed.BUILTIN_ASSIGNMENTS", [seed_entry], raising=True
    )

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _Result(scalar=agent),
            _Result(items=[]),
            _Result(scalar=agent),
            _Result(scalar=existing, items=[existing]),
        ]
    )
    db.commit = AsyncMock()
    db.flush = AsyncMock()

    await seed_builtin_assignments(db, platform_mode="xtm_one")

    assert existing.enabled is False
    assert existing.paused_reason == PANIC_STOP_REASON
    assert existing.paused_at == stopped_at
