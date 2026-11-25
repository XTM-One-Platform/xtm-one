"""#2157: a seeded fleet's Pause all also switches off its members' dashboard schedules.

The enabled-by-default allowlist (the weekly stage assessments, the monthly exposure
briefing) stays outside the Automations badge, so a fleet read "Not set up" or "Paused"
while those member schedules kept running. They are now counted as their own group,
Pause all stamps them ``fleet_pause``, Resume restores them and a boot keeps them off.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.builtin_agents import FLEET_OPT_IN_ASSIGNMENTS
from app.agents.fleet_opt_in import ENABLED_BY_DEFAULT_ALLOWLIST
from app.services import fleet_control
from app.services.assignment_pause import FLEET_PAUSE_REASON
from app.services.fleet_views import AutomationCounts, automation_counts, is_dashboard_schedule

_FLEET = sorted(FLEET_OPT_IN_ASSIGNMENTS)
_DASHBOARD = sorted(ENABLED_BY_DEFAULT_ALLOWLIST)


def test_dashboard_schedules_are_their_own_group_and_never_in_the_badge():
    rows = [(slug, True, name, True, None) for slug, name in _DASHBOARD[:3]]
    rows.append((_DASHBOARD[3][0], True, _DASHBOARD[3][1], False, FLEET_PAUSE_REASON))
    rows.append((_FLEET[0][0], True, _FLEET[0][1], False, "created_disabled"))
    counts = automation_counts(rows, builtin_fleet=True)
    assert counts == AutomationCounts(total=1, enabled=0, paused=0)
    assert (counts.dashboard_total, counts.dashboard_enabled) == (4, 3)


def test_a_custom_fleet_has_no_dashboard_group():
    slug, name = _DASHBOARD[0]
    assert not is_dashboard_schedule(slug, True, name, builtin_fleet=False)
    counts = automation_counts([(slug, True, name, True, None)], builtin_fleet=False)
    assert (counts.total, counts.dashboard_total) == (1, 0)


def test_a_user_added_assignment_is_not_a_dashboard_schedule():
    slug, _name = _DASHBOARD[0]
    assert not is_dashboard_schedule(slug, True, "My own schedule", builtin_fleet=True)
    assert not is_dashboard_schedule(slug, False, _DASHBOARD[0][1], builtin_fleet=True)


def _row(slug: str, name: str, *, enabled: bool, reason: str | None = None):
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        name=name,
        enabled=enabled,
        trigger_type="schedule",
        paused_reason=reason,
        paused_at=None,
        last_paused_at=None,
        paused_by_user_id=None,
        paused_by_label=None,
        paused_details=None,
        schedule_resumed_at=None,
    )
    agent = SimpleNamespace(slug=slug, is_builtin=True)
    return assignment, agent


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


@pytest.mark.asyncio
async def test_pause_all_switches_the_dashboard_schedules_off_and_resume_restores_them():
    weekly = _row(*_DASHBOARD[0], enabled=True)
    monthly = _row(*_DASHBOARD[1], enabled=True)
    fleet_on = _row(*_FLEET[0], enabled=True)
    own = _row(_DASHBOARD[0][0], "My own schedule", enabled=True)
    rows = [weekly, monthly, fleet_on, own]
    db = SimpleNamespace(execute=AsyncMock(return_value=_Result(rows)))
    fleet = SimpleNamespace(id=uuid.uuid4(), name="CTEM loop", is_builtin=True, orchestrator_agent_id=None)
    actor = SimpleNamespace(id=uuid.uuid4(), email="admin@example.com", full_name="Ada Admin", is_admin=True)

    with (
        patch.object(fleet_control, "fleet_members", AsyncMock(return_value=[])),
        patch("app.services.fleet_views.home_member_ids", return_value=[uuid.uuid4()]),
        patch.object(fleet_control, "record_pause_audits", AsyncMock()),
    ):
        assert await fleet_control.pausable_count(db, fleet) == 3
        paused = await fleet_control.pause_fleet(db, fleet, actor=actor)

    assert paused.changed == 3
    assert all(not a.enabled and a.paused_reason == FLEET_PAUSE_REASON for a, _ in (weekly, monthly, fleet_on))
    assert own[0].enabled, "an assignment a person added to a built-in member is not the fleet's"

    with (
        patch.object(fleet_control, "fleet_members", AsyncMock(return_value=[])),
        patch("app.services.fleet_views.home_member_ids", return_value=[uuid.uuid4()]),
        patch("app.services.panic.platform_stop_open", AsyncMock(return_value=False)),
        patch("app.services.assignment_pause._log_transition", AsyncMock()),
    ):
        resumed = await fleet_control.resume_fleet(db, fleet, actor=actor)

    assert resumed.changed == 3
    assert weekly[0].enabled and monthly[0].enabled and fleet_on[0].enabled


@pytest.mark.asyncio
async def test_unset_and_enable_keep_their_population():
    """Back to not set up and Enable fleet never take the dashboard schedules (the allowlist ships on)."""
    weekly = _row(*_DASHBOARD[0], enabled=True)
    fleet_on = _row(*_FLEET[0], enabled=True)
    db = SimpleNamespace(execute=AsyncMock(return_value=_Result([weekly, fleet_on])))
    fleet = SimpleNamespace(id=uuid.uuid4(), name="CTEM loop", is_builtin=True, orchestrator_agent_id=None)
    with (
        patch.object(fleet_control, "fleet_members", AsyncMock(return_value=[])),
        patch("app.services.fleet_views.home_member_ids", return_value=[uuid.uuid4()]),
    ):
        automations = await fleet_control._fleet_automations(db, fleet, lock=False)
    assert automations == [fleet_on[0]]
