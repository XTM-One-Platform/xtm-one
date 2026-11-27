"""The opt-in badge of a fleet as agent tools state it (one rule with the UI).

``opt_in_state`` is the backend twin of ``fleetOptIn``
(``frontend/src/lib/ctem-fleet-activity.ts``): the cases below are the ones
``ctem-fleet-activity.test.ts`` pins, so a change to one side fails here.
``fleet_automation_state`` names every counted automation that is off and
why; ``get_fleet``, ``get_fleet_health`` and ``set_fleet_automations`` read it.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.services import fleet_views
from app.services.builtin_tools import fleets as fleet_tools
from app.services.fleet_views import (
    AutomationCounts,
    FleetAutomationState,
    OffAutomation,
    automation_counts,
    opt_in_state,
)


@pytest.mark.parametrize(
    ("total", "enabled", "paused", "state"),
    [
        (3, 0, 0, "not_set_up"),
        (3, 1, 0, "partial"),
        (3, 3, 0, "on"),
        (3, 0, 2, "paused"),
        (0, 0, 0, "not_set_up"),
    ],
)
def test_the_badge_rule_is_the_frontend_one(total, enabled, paused, state):
    assert opt_in_state(AutomationCounts(total=total, enabled=enabled, paused=paused)) == state


def test_the_weekly_health_check_born_off_makes_the_fleet_partially_on():
    rows = [
        ("orchestrator", False, "Daily triage", True, None),
        ("orchestrator", False, "Weekly fleet health check", False, "created_disabled"),
        ("member-a", False, "One", True, None),
        ("member-a", False, "Two", True, None),
    ]
    counts = automation_counts(rows, builtin_fleet=False)
    assert (counts.total, counts.enabled, counts.paused) == (4, 3, 0)
    assert opt_in_state(counts) == "partial"
    # Switched on, the same fleet reads On.
    rows[1] = ("orchestrator", False, "Weekly fleet health check", True, None)
    assert opt_in_state(automation_counts(rows, builtin_fleet=False)) == "on"


def test_a_member_with_no_assignment_never_changes_the_state():
    # Member B has no assignment: it brings no row, so it counts nowhere.
    rows = [("orchestrator", False, "Daily triage", True, None), ("member-a", False, "One", True, None)]
    counts = automation_counts(rows, builtin_fleet=False)
    assert (counts.total, counts.enabled) == (2, 2)
    assert opt_in_state(counts) == "on"


@pytest.mark.asyncio
async def test_the_state_names_each_counted_automation_off_and_why(monkeypatch):
    orchestrator, member = uuid.uuid4(), uuid.uuid4()
    health, other = uuid.uuid4(), uuid.uuid4()
    found = [
        (orchestrator, "Orchestrator", "o", False, uuid.uuid4(), "Daily triage", True, None),
        (orchestrator, "Orchestrator", "o", False, health, "Weekly fleet health check", False, "created_disabled"),
        (member, "Member A", "a", False, other, "Digest", False, "fleet_pause"),
    ]

    class _Db:
        async def execute(self, _query):
            return SimpleNamespace(all=lambda: found)

    async def _members(_db, _fleet_id):
        return []

    monkeypatch.setattr(fleet_views, "fleet_members", _members)
    monkeypatch.setattr(fleet_views, "home_member_ids", lambda _rows, _orch: [orchestrator, member])
    fleet = SimpleNamespace(id=uuid.uuid4(), orchestrator_agent_id=orchestrator, is_builtin=False)
    state = await fleet_views.fleet_automation_state(_Db(), fleet)
    assert (state.state, state.label) == ("partial", "Partially on")
    assert (state.counts.total, state.counts.enabled, state.counts.paused, state.counts.fleet_paused) == (3, 1, 1, 1)
    assert [(o.assignment_id, o.reason) for o in state.off] == [(other, "fleet_pause"), (health, "created_disabled")]
    structured = state.structured()
    assert structured["state"] == "partial" and structured["total"] == 3
    assert structured["off"][1]["assignment_name"] == "Weekly fleet health check"
    assert structured["off"][0]["switch_on"].startswith("resume")
    assert structured["off"][1]["switch_on"].startswith("enable")


# ── set_fleet_automations, without a database ──────────────────────────


class _Session:
    def __init__(self, names: dict) -> None:
        self.names = names
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def flush(self) -> None:
        return None

    async def execute(self, _query):
        return SimpleNamespace(all=lambda: list(self.names.items()))


def _setup(monkeypatch, state: FleetAutomationState, names: dict) -> tuple[SimpleNamespace, _Session]:
    from sqlalchemy import true

    db = _Session(names)

    @asynccontextmanager
    async def _factory():
        yield db

    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    fleet = SimpleNamespace(id=uuid.uuid4(), name="F", slug="f", is_company_managed=False, is_builtin=False)

    async def _load(_db, _uid):
        return user

    async def _manageable(_db, _model, _uid, _entity_uuid, **_kw):
        return fleet, None

    async def _state(_db, _fleet):
        return state

    async def _groups(_db, _uid):
        return []

    monkeypatch.setattr(fleet_tools.database, "async_session", _factory)
    monkeypatch.setattr(fleet_tools._builder, "_load_caller_user_for_builder", _load)
    monkeypatch.setattr(fleet_tools._builder, "_load_manageable_entity_for_builder", _manageable)
    monkeypatch.setattr(fleet_views, "fleet_automation_state", _state)
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", _groups)
    monkeypatch.setattr("app.api.v1.helpers.user_visible_filter", lambda *_a, **_k: true())
    monkeypatch.setattr("app.api.v1.helpers.agent_tool_admin_bypass", lambda _u: False)
    return user, db


async def _call(user_id: str, **args) -> str:
    _defs, executor = fleet_tools.build_fleet_tools(user_id)
    return await executor.handler("set_fleet_automations")(**args)


def _health_check_off() -> tuple[FleetAutomationState, uuid.UUID, uuid.UUID]:
    agent_id, assignment_id = uuid.uuid4(), uuid.uuid4()
    state = FleetAutomationState(
        counts=AutomationCounts(total=4, enabled=3),
        off=(
            OffAutomation(
                agent_id=agent_id,
                agent_name="",
                assignment_id=assignment_id,
                assignment_name="Weekly fleet health check",
                reason="created_disabled",
            ),
        ),
    )
    return state, agent_id, assignment_id


@pytest.mark.asyncio
async def test_a_resume_with_nothing_paused_says_why_and_names_what_is_still_off(monkeypatch):
    from app.services import fleet_control

    state, agent_id, assignment_id = _health_check_off()
    user, _db = _setup(monkeypatch, state, {agent_id: "Orchestrator"})

    async def _resume(_db, _fleet, **_kw):
        return fleet_control.FleetControlOutcome(changed=0, names=())

    monkeypatch.setattr(fleet_control, "resume_fleet", _resume)
    answer = await _call(str(user.id), fleet_id=str(uuid.uuid4()), action="resume")
    assert "0 automation(s) resumed" in answer
    assert "Nothing was switched off by Pause all" in answer
    assert "Automations: Partially on, 3 of 4 on" in answer
    assert f"Weekly fleet health check `{assignment_id}` of Orchestrator" in answer
    assert "reason created_disabled" in answer and "action=enable" in answer


@pytest.mark.asyncio
async def test_enable_is_previewed_until_the_user_confirms_then_switches_on_and_audits(monkeypatch):
    from app.services import fleet_control

    state, agent_id, assignment_id = _health_check_off()
    user, _db = _setup(monkeypatch, state, {agent_id: "Orchestrator"})
    plan = fleet_control.FleetEnablePlan(
        run_as_user_id=user.id,
        items=(
            fleet_control.FleetEnableItem(id=assignment_id, name="Weekly fleet health check", providers=(), missing=()),
        ),
    )
    enabled: list[tuple] = []
    audited: list[dict] = []

    async def _plan(_db, _fleet, **_kw):
        return plan

    async def _enable(_db, _fleet, *, actor, run_as_user_id, run_as_by_module=None, **_kw):
        enabled.append((actor.id, run_as_user_id, run_as_by_module))
        return fleet_control.FleetControlOutcome(changed=1, names=("Weekly fleet health check",))

    async def _log_audit(_db, **kw):
        audited.append(kw)

    monkeypatch.setattr(fleet_control, "plan_fleet_enable", _plan)
    monkeypatch.setattr(fleet_control, "enable_fleet", _enable)
    monkeypatch.setattr("app.services.audit.log_audit", _log_audit)
    fid = str(uuid.uuid4())
    preview = await _call(str(user.id), fleet_id=fid, action="enable")
    assert preview.startswith("Not applied. Enable fleet would switch on 1 automation(s)")
    assert "Weekly fleet health check" in preview and enabled == [] and audited == []
    on = FleetAutomationState(counts=AutomationCounts(total=4, enabled=4))
    monkeypatch.setattr(fleet_views, "fleet_automation_state", lambda _db, _fleet: _async(on))
    done = await _call(str(user.id), fleet_id=fid, action="enable", confirm=True)
    assert "1 automation(s) enabled: Weekly fleet health check" in done
    assert "Automations: On, 4 of 4 on" in done
    # #2650: no fleet-wide run-as default to the clicking user; a module automation needs its own choice.
    assert enabled == [(user.id, None, {})] and [a["action"] for a in audited] == ["fleet_enable"]


async def _async(value):
    return value


def test_set_fleet_automations_offers_enable():
    definitions, _executor = fleet_tools.build_fleet_tools("")
    tool = next(d for d in definitions if d.name == "set_fleet_automations")
    assert tool.input_schema["properties"]["action"]["enum"] == ["pause", "resume", "unset", "enable"]
    get_fleet = next(d for d in definitions if d.name == "get_fleet")
    assert "automations badge" in get_fleet.description
