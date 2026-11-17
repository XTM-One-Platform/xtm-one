"""Fleet Pause all / Resume (#9769 S2): ``services/fleet_control.py``.

Pause all switches off every fleet automation that is on, stamped
``fleet_pause``; Resume switches back on exactly those, never one a person
paused before, never one an emergency stop holds.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.builtin_agents import FLEET_OPT_IN_ASSIGNMENTS
from app.services import fleet_control
from app.services.assignment_pause import FLEET_PAUSE_REASON, PANIC_STOP_REASON, PAUSE_REASONS
from app.services.fleet_views import AutomationCounts, automation_counts, is_fleet_automation
from app.services.fleets import FleetActionRefusedError

_FLEET = sorted(FLEET_OPT_IN_ASSIGNMENTS)


def _assignment(name: str, *, enabled: bool, reason: str | None = None):
    return SimpleNamespace(
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


def _actor():
    return SimpleNamespace(id=uuid.uuid4(), email="admin@example.com", full_name="Ada Admin", is_admin=True)


def _fleet():
    return SimpleNamespace(id=uuid.uuid4(), name="AI security", is_builtin=False, orchestrator_agent_id=None)


def test_fleet_pause_is_a_known_reason_a_person_decided():
    assert FLEET_PAUSE_REASON in PAUSE_REASONS


def test_is_fleet_automation_matches_automation_counts():
    slug, name = _FLEET[0]
    assert is_fleet_automation(slug, True, name, builtin_fleet=True)
    assert not is_fleet_automation(slug, True, "My own schedule", builtin_fleet=True)
    assert is_fleet_automation(None, False, "Anything", builtin_fleet=False)


def test_counts_name_the_fleet_paused_ones_resume_restores():
    rows = [
        ("a", False, "on", True, None),
        ("b", False, "fleet", False, FLEET_PAUSE_REASON),
        ("c", False, "user", False, "user_toggle"),
        ("d", False, "seed", False, "created_disabled"),
    ]
    assert automation_counts(rows, builtin_fleet=False) == AutomationCounts(
        total=4, enabled=1, paused=2, fleet_paused=1
    )


@pytest.mark.asyncio
async def test_pause_switches_off_only_what_is_on_and_resume_restores_exactly_that_set():
    on_a = _assignment("Daily triage", enabled=True)
    on_b = _assignment("Weekly report", enabled=True)
    user_paused = _assignment("Manual", enabled=False, reason="user_toggle")
    seeded_off = _assignment("Seeded", enabled=False, reason="created_disabled")
    panic = _assignment("Stopped", enabled=False, reason=PANIC_STOP_REASON)
    population = [on_a, on_b, user_paused, seeded_off, panic]
    db = SimpleNamespace()
    fleet, actor = _fleet(), _actor()

    with (
        patch.object(fleet_control, "_fleet_automations", AsyncMock(return_value=population)),
        patch.object(fleet_control, "record_pause_audits", AsyncMock()) as audits,
    ):
        paused = await fleet_control.pause_fleet(db, fleet, actor=actor)

    assert paused.changed == 2
    assert set(paused.names) == {"Daily triage", "Weekly report"}
    assert not on_a.enabled and on_a.paused_reason == FLEET_PAUSE_REASON
    assert on_a.paused_by_user_id == actor.id
    assert user_paused.paused_reason == "user_toggle"
    assert seeded_off.paused_reason == "created_disabled"
    assert audits.await_args.kwargs["assignments"] == [on_a, on_b]

    with (
        patch.object(fleet_control, "_fleet_automations", AsyncMock(return_value=population)),
        patch("app.services.panic.platform_stop_open", AsyncMock(return_value=False)),
        patch("app.services.assignment_pause._log_transition", AsyncMock()),
    ):
        resumed = await fleet_control.resume_fleet(db, fleet, actor=actor)

    assert resumed.changed == 2
    assert on_a.enabled and on_b.enabled
    assert on_a.paused_reason is None
    assert not user_paused.enabled and not seeded_off.enabled and not panic.enabled
    assert panic.paused_reason == PANIC_STOP_REASON


@pytest.mark.asyncio
async def test_pause_twice_changes_nothing_the_second_time():
    population = [_assignment("Daily triage", enabled=False, reason=FLEET_PAUSE_REASON)]
    with (
        patch.object(fleet_control, "_fleet_automations", AsyncMock(return_value=population)),
        patch.object(fleet_control, "record_pause_audits", AsyncMock()),
    ):
        outcome = await fleet_control.pause_fleet(SimpleNamespace(), _fleet(), actor=_actor())
    assert outcome.changed == 0


@pytest.mark.asyncio
async def test_resume_is_refused_while_a_platform_emergency_stop_is_in_effect():
    population = [_assignment("Daily triage", enabled=False, reason=FLEET_PAUSE_REASON)]
    with (
        patch.object(fleet_control, "_fleet_automations", AsyncMock(return_value=population)),
        patch("app.services.panic.platform_stop_open", AsyncMock(return_value=True)),
        pytest.raises(FleetActionRefusedError) as refused,
    ):
        await fleet_control.resume_fleet(SimpleNamespace(), _fleet(), actor=_actor())
    assert refused.value.status_code == 409
    assert not population[0].enabled


# ── Back to not set up (#11060) ──


def _status_of(population, *, builtin_fleet: bool = False) -> str:
    from app.services.fleet_datasheet import datasheet_status

    rows = [(None, False, a.name, a.enabled, a.paused_reason) for a in population]
    counts = automation_counts(rows, builtin_fleet=builtin_fleet)
    return datasheet_status(counts.total, counts.enabled, counts.paused)


@pytest.mark.asyncio
async def test_unset_switches_off_and_restamps_every_automation_so_the_fleet_reads_not_set_up():
    on = _assignment("Daily triage", enabled=True)
    agent_off = _assignment("Agent off", enabled=False, reason="agent_tool")
    user_off = _assignment("Manual", enabled=False, reason="user_toggle")
    fleet_paused = _assignment("Paused", enabled=False, reason=FLEET_PAUSE_REASON)
    seeded = _assignment("Seeded", enabled=False, reason="created_disabled")
    population = [on, agent_off, user_off, fleet_paused, seeded]
    fleet, actor = _fleet(), _actor()
    ids = [a.id for a in population]
    assert _status_of(population) == "partially_on"

    with (
        patch.object(fleet_control, "_fleet_automations", AsyncMock(return_value=population)),
        patch.object(fleet_control, "record_pause_audits", AsyncMock()) as audits,
    ):
        outcome = await fleet_control.unset_fleet(SimpleNamespace(), fleet, actor=actor)

    assert outcome.changed == 4
    assert set(outcome.names) == {"Daily triage", "Agent off", "Manual", "Paused"}
    assert outcome.held_by_stop == ()
    assert all(not a.enabled and a.paused_reason == "created_disabled" for a in population)
    assert on.paused_details == "Set back to not set up by Ada Admin."
    assert on.paused_by_user_id == actor.id
    # Nothing deleted, ids kept; one audit row per changed assignment, the seeded one untouched.
    assert [a.id for a in population] == ids
    assert audits.await_args.kwargs["assignments"] == [on, agent_off, user_off, fleet_paused]
    assert audits.await_args.kwargs["reason"] == "created_disabled"
    assert _status_of(population) == "not_set_up"
    # Resume has nothing to switch back on: no fleet_pause stamp is left.
    assert automation_counts(
        [(None, False, a.name, a.enabled, a.paused_reason) for a in population], builtin_fleet=False
    ) == AutomationCounts(total=5, enabled=0, paused=0, fleet_paused=0)


@pytest.mark.asyncio
async def test_unset_leaves_an_emergency_stop_untouched_and_names_it():
    stopped = _assignment("Stopped", enabled=False, reason=PANIC_STOP_REASON)
    on = _assignment("Daily triage", enabled=True)
    population = [stopped, on]
    with (
        patch.object(fleet_control, "_fleet_automations", AsyncMock(return_value=population)),
        patch.object(fleet_control, "record_pause_audits", AsyncMock()),
    ):
        count, held = await fleet_control.unsettable_count(SimpleNamespace(), _fleet())
        outcome = await fleet_control.unset_fleet(SimpleNamespace(), _fleet(), actor=_actor())

    assert (count, held) == (1, ("Stopped",))
    assert outcome.changed == 1 and outcome.held_by_stop == ("Stopped",)
    assert stopped.paused_reason == PANIC_STOP_REASON


@pytest.mark.asyncio
async def test_unset_twice_changes_nothing_the_second_time():
    population = [_assignment("Daily triage", enabled=True)]
    with (
        patch.object(fleet_control, "_fleet_automations", AsyncMock(return_value=population)),
        patch.object(fleet_control, "record_pause_audits", AsyncMock()),
    ):
        first = await fleet_control.unset_fleet(SimpleNamespace(), _fleet(), actor=_actor())
        second = await fleet_control.unset_fleet(SimpleNamespace(), _fleet(), actor=_actor())
    assert (first.changed, second.changed) == (1, 0)


def test_a_switch_off_before_the_first_run_is_stamped_created_disabled():
    from datetime import UTC, datetime

    from app.services.assignment_pause import has_never_run, switch_off_reason

    never = SimpleNamespace(execution_count=0, last_triggered_at=None)
    ran = SimpleNamespace(execution_count=3, last_triggered_at=datetime.now(UTC))
    reset_counter = SimpleNamespace(execution_count=0, last_triggered_at=datetime.now(UTC))
    unknown = SimpleNamespace(execution_count=object(), last_triggered_at=None)
    assert has_never_run(never) and not has_never_run(ran)
    assert not has_never_run(reset_counter) and not has_never_run(unknown)
    assert switch_off_reason(never, "agent_tool") == "created_disabled"
    assert switch_off_reason(never, "user_toggle") == "created_disabled"
    assert switch_off_reason(ran, "agent_tool") == "agent_tool"
    assert switch_off_reason(reset_counter, "user_toggle") == "user_toggle"
