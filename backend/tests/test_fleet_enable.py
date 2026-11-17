"""Enable fleet (#9769 S4b): ``services/fleet_control.enable_fleet`` and its route.

Enable switches on every automation of the fleet that is off (seeded, paused
by a person or by the fleet's Pause all), never one an emergency stop holds;
the seeded fleet automations run as the picked user and the whole call is
refused while that user misses a module one of them needs.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api.v1 import fleets as fleets_api
from app.services import fleet_control
from app.services.assignment_pause import FLEET_PAUSE_REASON, PANIC_STOP_REASON
from app.services.fleets import FleetActionRefusedError


def _assignment(name: str, *, enabled: bool, reason: str | None = None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        name=name,
        enabled=enabled,
        trigger_type="schedule",
        trigger_config={"provider": "opencti"},
        run_as_user_id=None,
        paused_reason=reason,
        paused_at=None,
        last_paused_at=None,
        paused_by_user_id=None,
        paused_by_label=None,
        paused_details=None,
        schedule_resumed_at=None,
    )


def _agent():
    return SimpleNamespace(id=uuid.uuid4(), slug="custom-agent", is_builtin=False, integration_template_bindings=[])


def _actor(admin: bool = True):
    return SimpleNamespace(id=uuid.uuid4(), email="admin@example.com", full_name="Ada Admin", is_admin=admin)


def _fleet():
    return SimpleNamespace(id=uuid.uuid4(), name="AI security", is_builtin=False, orchestrator_agent_id=None)


def _population():
    agent = _agent()
    seeded = _assignment("Seeded", enabled=False, reason="created_disabled")
    user_paused = _assignment("Manual", enabled=False, reason="user_toggle")
    fleet_paused = _assignment("Fleet paused", enabled=False, reason=FLEET_PAUSE_REASON)
    on = _assignment("Already on", enabled=True)
    panic = _assignment("Stopped", enabled=False, reason=PANIC_STOP_REASON)
    return [(a, agent) for a in (seeded, user_paused, fleet_paused, on, panic)]


@pytest.mark.asyncio
async def test_enable_switches_on_every_off_automation_but_the_stopped_one():
    rows = _population()
    actor, run_as = _actor(), uuid.uuid4()
    with (
        patch.object(fleet_control, "_fleet_automation_rows", AsyncMock(return_value=rows)),
        patch("app.services.panic.platform_stop_open", AsyncMock(return_value=False)),
        patch("app.services.assignment_pause._log_transition", AsyncMock()) as audit,
    ):
        outcome = await fleet_control.enable_fleet(SimpleNamespace(), _fleet(), actor=actor, run_as_user_id=run_as)

    by_name = {a.name: a for a, _agent in rows}
    assert outcome.changed == 3
    assert set(outcome.names) == {"Seeded", "Manual", "Fleet paused"}
    assert all(by_name[n].enabled and by_name[n].paused_reason is None for n in outcome.names)
    assert not by_name["Stopped"].enabled and by_name["Stopped"].paused_reason == PANIC_STOP_REASON
    # One resume audit row per switched automation, none for the one already on.
    assert audit.await_count == 3


@pytest.mark.asyncio
async def test_enable_sets_the_run_as_of_seeded_fleet_automations_only():
    rows = _population()
    run_as = uuid.uuid4()
    seeded = rows[0][0]
    with (
        patch.object(fleet_control, "_fleet_automation_rows", AsyncMock(return_value=rows)),
        patch("app.services.panic.platform_stop_open", AsyncMock(return_value=False)),
        patch("app.services.assignment_pause._log_transition", AsyncMock()),
        patch("app.services.fleet_run_as.is_fleet_automation", lambda agent, a: a is seeded),
        patch("app.services.fleet_run_as.missing_providers", AsyncMock(return_value=[])),
    ):
        await fleet_control.enable_fleet(SimpleNamespace(), _fleet(), actor=_actor(), run_as_user_id=run_as)

    assert seeded.run_as_user_id == run_as
    assert rows[1][0].run_as_user_id is None


@pytest.mark.asyncio
async def test_enable_is_all_or_nothing_when_a_module_is_missing():
    rows = _population()
    seeded = rows[0][0]
    with (
        patch.object(fleet_control, "_fleet_automation_rows", AsyncMock(return_value=rows)),
        patch("app.services.panic.platform_stop_open", AsyncMock(return_value=False)),
        patch("app.services.fleet_run_as.is_fleet_automation", lambda agent, a: a is seeded),
        patch("app.services.fleet_run_as.missing_providers", AsyncMock(return_value=["opencti"])),
        pytest.raises(FleetActionRefusedError) as refused,
    ):
        await fleet_control.enable_fleet(SimpleNamespace(), _fleet(), actor=_actor(), run_as_user_id=uuid.uuid4())

    assert refused.value.status_code == 400
    assert "OpenCTI" in refused.value.detail
    assert not any(a.enabled for a, _agent in rows if a.name != "Already on")


@pytest.mark.asyncio
async def test_plan_names_each_automation_and_what_it_misses():
    rows = _population()
    seeded = rows[0][0]
    with (
        patch.object(fleet_control, "_fleet_automation_rows", AsyncMock(return_value=rows)),
        patch("app.services.fleet_run_as.is_fleet_automation", lambda agent, a: a is seeded),
        patch("app.services.fleet_run_as.missing_providers", AsyncMock(return_value=["opencti"])),
    ):
        plan = await fleet_control.plan_fleet_enable(SimpleNamespace(), _fleet(), run_as_user_id=uuid.uuid4())

    assert [i.name for i in plan.items] == ["Seeded", "Manual", "Fleet paused"]
    assert plan.items[0].providers == ("opencti",) and plan.items[0].missing == ("opencti",)
    assert plan.items[1].providers == ()
    assert plan.missing == ["opencti"] and not plan.ready
    # A dry run writes nothing.
    assert not seeded.enabled


@pytest.mark.asyncio
async def test_enable_refused_during_a_stop_and_when_nothing_is_off():
    with (
        patch.object(fleet_control, "_fleet_automation_rows", AsyncMock(return_value=_population())),
        patch("app.services.panic.platform_stop_open", AsyncMock(return_value=True)),
        pytest.raises(FleetActionRefusedError) as stopped,
    ):
        await fleet_control.enable_fleet(SimpleNamespace(), _fleet(), actor=_actor(), run_as_user_id=uuid.uuid4())
    assert stopped.value.status_code == 409

    on_only = [(_assignment("On", enabled=True), _agent())]
    with (
        patch.object(fleet_control, "_fleet_automation_rows", AsyncMock(return_value=on_only)),
        patch("app.services.panic.platform_stop_open", AsyncMock(return_value=False)),
        pytest.raises(FleetActionRefusedError) as nothing,
    ):
        await fleet_control.enable_fleet(SimpleNamespace(), _fleet(), actor=_actor(), run_as_user_id=uuid.uuid4())
    assert nothing.value.status_code == 409


@pytest.mark.asyncio
async def test_partial_enable_switches_on_only_the_picked_automations():
    """#9173 S5: the setup dialog picks some automations; the others stay off."""
    rows = _population()
    by_name = {a.name: a for a, _agent in rows}
    picked = [by_name["Seeded"].id]
    with (
        patch.object(fleet_control, "_fleet_automation_rows", AsyncMock(return_value=rows)),
        patch("app.services.panic.platform_stop_open", AsyncMock(return_value=False)),
        patch("app.services.assignment_pause._log_transition", AsyncMock()) as audit,
    ):
        outcome = await fleet_control.enable_fleet(
            SimpleNamespace(), _fleet(), actor=_actor(), run_as_user_id=uuid.uuid4(), assignment_ids=picked
        )

    assert outcome.changed == 1 and outcome.names == ("Seeded",)
    assert by_name["Seeded"].enabled
    assert not by_name["Manual"].enabled and not by_name["Fleet paused"].enabled
    assert audit.await_count == 1


@pytest.mark.asyncio
async def test_partial_plan_lists_every_off_automation_and_reads_only_the_pick():
    """A module missing for an automation left out does not block the pick."""
    rows = _population()
    by_name = {a.name: a for a, _agent in rows}
    seeded = by_name["Seeded"]
    with (
        patch.object(fleet_control, "_fleet_automation_rows", AsyncMock(return_value=rows)),
        patch("app.services.fleet_run_as.is_fleet_automation", lambda agent, a: a is seeded),
        patch("app.services.fleet_run_as.missing_providers", AsyncMock(return_value=["opencti"])),
    ):
        plan = await fleet_control.plan_fleet_enable(
            SimpleNamespace(), _fleet(), run_as_user_id=uuid.uuid4(), assignment_ids=[by_name["Manual"].id]
        )

    assert [(i.name, i.selected) for i in plan.items] == [("Seeded", False), ("Manual", True), ("Fleet paused", False)]
    assert plan.changed == 1 and plan.missing == [] and plan.ready


@pytest.mark.asyncio
async def test_partial_enable_refuses_an_empty_pick_and_an_id_that_is_not_off():
    rows = _population()
    by_name = {a.name: a for a, _agent in rows}
    for pick in ([], [by_name["Already on"].id], [by_name["Stopped"].id], [uuid.uuid4()]):
        with (
            patch.object(fleet_control, "_fleet_automation_rows", AsyncMock(return_value=rows)),
            patch("app.services.panic.platform_stop_open", AsyncMock(return_value=False)),
            pytest.raises(FleetActionRefusedError) as refused,
        ):
            await fleet_control.enable_fleet(
                SimpleNamespace(), _fleet(), actor=_actor(), run_as_user_id=uuid.uuid4(), assignment_ids=pick
            )
        assert refused.value.status_code == 422
    assert not any(a.enabled for a, _agent in rows if a.name != "Already on")


@pytest.mark.asyncio
async def test_only_an_admin_picks_another_run_as_user():
    user = _actor(admin=False)
    assert await fleets_api._enable_run_as(SimpleNamespace(), user, None) == user.id
    assert await fleets_api._enable_run_as(SimpleNamespace(), user, str(user.id)) == user.id
    with pytest.raises(HTTPException) as refused:
        await fleets_api._enable_run_as(SimpleNamespace(), user, str(uuid.uuid4()))
    assert refused.value.status_code == 403
