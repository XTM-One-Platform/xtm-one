"""Reset a built-in fleet to its factory state (#1508): ``services/fleet_reset.py``.

The reset puts back what a fresh seed leaves: automations off and not set up,
run-as cleared, the seeded level, no pins or added gates, fleet settings at
their default, and every member agent's customer layer cleared with the
seed's own bindings kept. Refused on a custom fleet and during an emergency stop.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.fleet_seed import SEEDED_FLEET_AUTONOMY_LEVEL
from app.models.fleet import FLEET_ROLE_CONNECTED, FLEET_ROLE_MEMBER
from app.services import fleet_control, fleet_reset
from app.services.fleets import FleetActionRefusedError

SEEDED_INTEGRATION = str(uuid.uuid4())
SEEDED_KB = str(uuid.uuid4())


def _agent(**overrides: Any) -> SimpleNamespace:
    values = dict(
        id=uuid.uuid4(),
        slug="ctem-orchestrator",
        is_builtin=True,
        integration_bindings={"opencti": [str(uuid.uuid4())], "xtm_one": [SEEDED_INTEGRATION]},
        integration_lending={"integration_ids": [str(uuid.uuid4())]},
        integration_allowed_tools={"opencti": ["x"]},
        integration_element_bindings={"github:repository": ["a/b"]},
        integration_element_enforcement=["github:repository"],
        integration_read_only=["opencti:1"],
        integration_signatures={"x": "on"},
        integration_live_writes={"x": "on"},
        integration_template_bindings=[],
        integration_template_read_only=None,
        mcp_server_ids=[str(uuid.uuid4())],
        mcp_allowed_tools={"m": ["t"]},
        knowledge_base_ids=[str(uuid.uuid4()), SEEDED_KB],
        knowledge_base_permissions={"k": "write"},
        skill_ids=[str(uuid.uuid4())],
        skill_load_modes={"s": "always"},
        custom_tool_ids=[str(uuid.uuid4())],
        customer_notes="Our notes",
        customer_fields=["llm_model"],
        enabled=False,
        approve_tool_calls=True,
        hide_activity_from_non_admins=True,
        llm_provider="openai",
        llm_provider_id=uuid.uuid4(),
        llm_model="gpt-x",
        llm_model_source=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _fleet(**overrides: Any) -> SimpleNamespace:
    values = dict(
        id=uuid.uuid4(),
        name="CTEM loop",
        slug="ctem-loop",
        is_builtin=True,
        orchestrator_agent_id=None,
        # Not the shipped level (full_gates since #2279): the reset moves it back.
        autonomy_level="low",
        approval_pins={"close_case": "run", "mobilise": "wait"},
        customer_gates=[{"key": "mine"}],
        enforce_gates_at_tool_level=True,
        instructions_position="medium",
        instructions_position_announced=True,
        prerequisite_confirmations={"m": {}},
        health_dismissals=[{"id": "x"}],
        health_check=None,
        health_enrolled=True,
        hide_activity_from_non_admins=False,
        budget_config=None,
        max_iterations=None,
        run_budget={"runs_per_hour": 5},
        default_context_inclusion="none",
        default_collaboration_instruction="",
        organization_scope={"mode": "list", "organization_ids": ["o"]},
        documentation={"sections": {}, "version": 2},
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _automation(*, enabled: bool, run_as=None):
    return SimpleNamespace(name="Daily", enabled=enabled, run_as_user_id=run_as, schedule_resumed_at=object())


def _actor():
    return SimpleNamespace(id=uuid.uuid4(), email="admin@example.com", full_name="Ada Admin", is_admin=True)


def _patches(
    fleet: Any,
    agent: Any,
    automations: Any,
    *,
    stop: bool = False,
    held: tuple[str, ...] = (),
    open_work: tuple[int, int] = (0, 0),
) -> tuple[Any, Any, list[Any]]:
    member = SimpleNamespace(
        role=FLEET_ROLE_MEMBER, agent_id=agent.id, context_inclusion="full", organization_scope=None
    )
    connected = SimpleNamespace(
        role=FLEET_ROLE_CONNECTED, agent_id=uuid.uuid4(), context_inclusion=None, organization_scope=None
    )

    async def _get(_model, _id):
        return agent

    db = SimpleNamespace(get=_get, flush=AsyncMock())
    return (
        db,
        member,
        [
            patch("app.services.panic.platform_stop_open", AsyncMock(return_value=stop)),
            patch.object(fleet_control, "unsettable_count", AsyncMock(return_value=(1, held))),
            patch.object(fleet_control, "_fleet_automations", AsyncMock(return_value=automations)),
            patch.object(fleet_control, "unset_fleet", AsyncMock()),
            patch.object(fleet_reset, "fleet_members", AsyncMock(return_value=[member, connected])),
            patch.object(
                fleet_reset,
                "_seeded_bindings",
                AsyncMock(return_value=({"xtm_one": [SEEDED_INTEGRATION]}, {SEEDED_KB}, set())),
            ),
            patch.object(fleet_reset, "_agent_specs", lambda: {}),
            patch("app.agents.seed._is_anthropic_enabled", AsyncMock(return_value=False)),
            patch("sqlalchemy.orm.attributes.flag_modified", lambda *_a: None),
            patch("app.services.agent_config_versioning.versioned_agent_values", lambda _a: {}),
            patch("app.services.agent_config_versioning.record_agent_version_changes", AsyncMock()),
            patch.object(fleet_reset, "_open_member_work", AsyncMock(return_value=open_work)),
        ],
    )


async def _run(
    fleet: Any,
    agent: Any,
    automations: Any,
    *,
    dry_run: bool,
    stop: bool = False,
    held: tuple[str, ...] = (),
    open_work: tuple[int, int] = (0, 0),
) -> tuple[Any, Any]:
    db, member, patches = _patches(fleet, agent, automations, stop=stop, held=held, open_work=open_work)
    for p in patches:
        p.start()
    try:
        outcome = await fleet_reset.reset_builtin_fleet(db, fleet, actor=_actor(), dry_run=dry_run)
    finally:
        for p in reversed(patches):
            p.stop()
    return outcome, member


@pytest.mark.asyncio
async def test_custom_fleet_is_refused():
    with pytest.raises(FleetActionRefusedError) as exc:
        await fleet_reset.reset_builtin_fleet(SimpleNamespace(), _fleet(is_builtin=False), actor=_actor())
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_refused_during_an_emergency_stop():
    with pytest.raises(FleetActionRefusedError) as exc:
        await _run(_fleet(), _agent(), [], dry_run=True, stop=True)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_dry_run_names_the_automations_a_scoped_stop_holds():
    fleet, agent = _fleet(), _agent()
    outcome, _member = await _run(fleet, agent, [], dry_run=True, held=("Daily", "Weekly"))
    assert outcome.held_by_stop == ("Daily", "Weekly")
    assert fleet.approval_pins  # a dry run writes nothing


@pytest.mark.asyncio
async def test_reset_is_refused_while_a_scoped_stop_holds_automations():
    # UX round 2 B1 on #1523: a stop of one person's or one fleet's scope holds automations the
    # reset cannot touch; the reset must not run and report success over them.
    fleet, agent = _fleet(), _agent()
    automation = _automation(enabled=False, run_as=uuid.uuid4())
    with pytest.raises(FleetActionRefusedError) as exc:
        await _run(fleet, agent, [automation], dry_run=False, held=("Daily",))
    assert exc.value.status_code == 409
    assert "Daily" in str(exc.value)
    assert fleet.approval_pins and fleet.autonomy_level == "low"
    assert automation.run_as_user_id is not None


@pytest.mark.asyncio
async def test_dry_run_counts_and_writes_nothing():
    fleet, agent = _fleet(), _agent()
    automations = [_automation(enabled=True, run_as=uuid.uuid4())]
    outcome, member = await _run(fleet, agent, automations, dry_run=True)

    assert outcome.pins == 2 and outcome.gates == 1
    assert outcome.autonomy_level == SEEDED_FLEET_AUTONOMY_LEVEL
    assert outcome.run_as_cleared == 1 and outcome.automations == 1
    assert outcome.agents == 1
    # opencti binding, one lent integration, one KB, one skill, one MCP server, one custom tool.
    assert outcome.bindings == 6
    assert "member_settings" in outcome.settings and "organization_scope" in outcome.settings
    # Review P2 on #1523: an administrator's run budget (#1526) is a fleet setting the reset counts.
    assert "run_budget" in outcome.settings and fleet.run_budget == {"runs_per_hour": 5}
    assert fleet.approval_pins and agent.mcp_server_ids and member.context_inclusion == "full"


@pytest.mark.asyncio
async def test_reset_returns_the_fleet_and_its_members_to_the_seed_values():
    fleet, agent = _fleet(), _agent()
    automation = _automation(enabled=True, run_as=uuid.uuid4())
    outcome, member = await _run(fleet, agent, [automation], dry_run=False)

    assert outcome.changed
    assert fleet.autonomy_level == SEEDED_FLEET_AUTONOMY_LEVEL
    assert fleet.approval_pins is None and fleet.customer_gates is None
    assert fleet.enforce_gates_at_tool_level is False and fleet.health_enrolled is False
    assert fleet.instructions_position is None and fleet.prerequisite_confirmations is None
    assert fleet.organization_scope == {"mode": "primary"}
    assert fleet.run_budget is None
    assert automation.run_as_user_id is None and automation.schedule_resumed_at is None
    assert member.context_inclusion is None
    # Only the seed's own bindings stay.
    assert agent.integration_bindings == {"xtm_one": [SEEDED_INTEGRATION]}
    assert agent.knowledge_base_ids == [SEEDED_KB] and agent.skill_ids == []
    assert agent.mcp_server_ids == [] and agent.custom_tool_ids == []
    assert agent.integration_lending is None and agent.integration_read_only is None
    assert agent.customer_notes is None and agent.customer_fields is None
    assert agent.enabled is True and agent.approve_tool_calls is None
    assert agent.llm_provider is None and agent.llm_provider_id is None


@pytest.mark.asyncio
async def test_a_second_reset_changes_nothing():
    fleet, agent = _fleet(), _agent()
    automation = _automation(enabled=True, run_as=uuid.uuid4())
    await _run(fleet, agent, [automation], dry_run=False)
    automation.enabled = False
    db, member, patches = _patches(fleet, agent, [automation])
    member.context_inclusion = None
    patches[1] = patch.object(fleet_control, "unsettable_count", AsyncMock(return_value=(0, ())))
    for p in patches:
        p.start()
    try:
        again = await fleet_reset.reset_builtin_fleet(db, fleet, actor=_actor(), dry_run=True)
    finally:
        for p in reversed(patches):
            p.stop()
    assert not again.changed, again.audit_details()


@pytest.mark.asyncio
async def test_before_records_the_fleet_as_it_stood_not_the_changes() -> None:
    # CTEM partner ticket (#2134): the audit 'before' held the change counts, so a reader saw
    # 'autonomy_level: low' (the target) as the level before. It now holds the real state.
    fleet, agent = _fleet(autonomy_level="autonomous"), _agent()
    outcome, _member = await _run(fleet, agent, [], dry_run=False, open_work=(2, 5))

    assert outcome.before["autonomy_level"] == "autonomous"
    assert outcome.before["approval_pins"] == {"close_case": "run", "mobilise": "wait"}
    assert outcome.before["customer_gates"] == 1
    assert outcome.before["home_members"] == 1 and outcome.before["connected_agents"] == 1
    assert outcome.before["open_gates"] == 2 and outcome.before["open_items"] == 5
    assert outcome.autonomy_level == SEEDED_FLEET_AUTONOMY_LEVEL
    assert fleet.autonomy_level == SEEDED_FLEET_AUTONOMY_LEVEL


@pytest.mark.asyncio
async def test_reset_restarts_the_fleet_value_and_audits_both_marks() -> None:
    # #2580: the reset stamps value_measured_since; a second reset moves it, both marks audited.
    previous = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
    fleet, agent = _fleet(value_measured_since=previous), _agent()
    outcome, _member = await _run(fleet, agent, [], dry_run=False)

    assert outcome.changed
    assert fleet.value_measured_since > previous
    assert outcome.audit_details()["value_measured_since"] == {
        "from": previous.isoformat(),
        "to": fleet.value_measured_since.isoformat(),
    }


@pytest.mark.asyncio
async def test_a_dry_run_leaves_the_value_mark() -> None:
    fleet, agent = _fleet(value_measured_since=None), _agent()
    outcome, _member = await _run(fleet, agent, [], dry_run=True)
    assert fleet.value_measured_since is None
    assert outcome.value_measured_since_to is None


@pytest.mark.asyncio
async def test_dry_run_names_the_open_work_the_reset_keeps() -> None:
    fleet, agent = _fleet(), _agent()
    outcome, _member = await _run(fleet, agent, [], dry_run=True, open_work=(1, 3))
    assert (outcome.before["open_gates"], outcome.before["open_items"]) == (1, 3)
    assert fleet.approval_pins  # still a dry run
