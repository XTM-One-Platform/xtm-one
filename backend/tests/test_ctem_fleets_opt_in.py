"""Seeded fleet automations are opt-in (#9160, epic #8975).

Tripwire: a seeded automation never ships enabled; the customer opts in.
Golden: the 14 dashboard and decay automations of 1.261102.0 are unchanged.
"""

from __future__ import annotations

import importlib.util
import pathlib
from typing import Any

from sqlalchemy.dialects import postgresql

from app.agents.builtin_agents import ADMIN_OPT_IN_BUILTIN_ASSIGNMENTS, BUILTIN_ASSIGNMENTS
from app.agents.fleet_opt_in import ENABLED_BY_DEFAULT_ALLOWLIST, PROVISIONING_STATE_KEYS, SEEDED_FLEET_OPT_IN

_FLEET_ASSIGNMENTS = {
    ("ctem-threat-response-orchestrator", "New relevant threat"),
    ("ctem-threat-response-orchestrator", "Weekly threat sweep"),
    ("ctem-threat-response-orchestrator", "Re-test due"),
    ("ctem-vuln-prioritization-orchestrator", "New KEV or EPSS jump"),
    ("ctem-vuln-prioritization-orchestrator", "Vulnerability sighted"),
    ("ctem-vuln-prioritization-orchestrator", "Daily backlog pass"),
    ("ctem-vuln-prioritization-orchestrator", "Re-test due"),
    # OpenAEV results move the cases (CTEM-15, #918): listen-only, seeded off.
    ("ctem-vuln-prioritization-orchestrator", "OpenAEV results into cases"),
    # The analyst's mark resumes the review gate (CTEM-16, #1089): listen-only, seeded off.
    ("ctem-vuln-prioritization-orchestrator", "Analyst review mark"),
    # The loop fleets run on cases (#891): seeded off, after migration c9160fleetoptin1.
    ("ctem-threat-response-orchestrator", "Case opened or reopened"),
    ("ctem-vuln-prioritization-orchestrator", "Case opened or reopened"),
    # A gate answer resumes the case (#1388): seeded off, takes the case listener's switch.
    ("ctem-threat-response-orchestrator", "Gate answered"),
    ("ctem-vuln-prioritization-orchestrator", "Gate answered"),
    # The loop's own timer.due listener (#1574): seeded off, takes the case listener's switch.
    ("ctem-threat-response-orchestrator", "Loop reminder due"),
    # CTEM-08 (#919): the trigger rule on software we run, seeded off, after migration c9160fleetoptin1.
    ("ctem-vuln-prioritization-orchestrator", "Vulnerability on software we run"),
    # CTEM-08 (#919): the PIR score thresholds of the policy, seeded off, after migration c9160fleetoptin1.
    ("ctem-vuln-prioritization-orchestrator", "Vulnerability flagged in a PIR"),
    ("ctem-threat-response-orchestrator", "Threat activity flagged in a PIR"),
    ("ctem-ai-security-orchestrator", "New AI-enabled report"),
    ("ctem-ai-security-orchestrator", "Weekly AI threat watch"),
    ("ctem-ai-security-orchestrator", "AI system registered"),
    ("ctem-posture-orchestrator", "Event refresh"),
    ("ctem-posture-orchestrator", "Monthly board report"),
    # F5 third-party risk (#9157 M5): seeded off, after migration c9160fleetoptin1.
    ("ctem-third-party-risk-orchestrator", "Tracked organization named"),
    ("ctem-third-party-risk-orchestrator", "Weekly third-party review"),
    # M1 OpenCTI PIR intelligence desk (#10542): seeded off, after migration c9160fleetoptin1.
    *(
        ("opencti-pir-desk-orchestrator", name)
        for name in (
            "PIR match created",
            "Weekly PIR answer",
            "Half-year threat landscape",
            "Stale indicator clean-up",
            "Gate answered",
        )
    ),
    # M2 OpenAEV detection validation desk (#10575): seeded off, after migration c9160fleetoptin1.
    *(
        ("openaev-detection-desk-orchestrator", name)
        for name in (
            "Simulation finished",
            "Weekly validation plan",
            "Weekly coverage report",
            "Re-test due",
            "Gate answered",
        )
    ),
    # M3 OpenCRQ risk budget desk (#10593): seeded off, after migration c9160fleetoptin1.
    *(
        ("opencrq-risk-desk-orchestrator", name)
        for name in (
            "Scenario re-priced",
            "Finding re-scored",
            "Control validation ingested",
            "Weekly investment review",
            "Monthly Board pack",
            "Gate answered",
        )
    ),
}

#: Fleet automations seeded after c9160fleetoptin1: born disabled, never switched off by it.
_SEEDED_AFTER_C9160 = {
    ("ctem-third-party-risk-orchestrator", "Tracked organization named"),
    ("ctem-third-party-risk-orchestrator", "Weekly third-party review"),
    ("ctem-threat-response-orchestrator", "Case opened or reopened"),
    ("ctem-vuln-prioritization-orchestrator", "Case opened or reopened"),
    ("ctem-threat-response-orchestrator", "Gate answered"),
    ("ctem-vuln-prioritization-orchestrator", "Gate answered"),
    ("ctem-threat-response-orchestrator", "Loop reminder due"),
    ("ctem-vuln-prioritization-orchestrator", "OpenAEV results into cases"),
    ("ctem-vuln-prioritization-orchestrator", "Analyst review mark"),
    ("ctem-vuln-prioritization-orchestrator", "Vulnerability on software we run"),
    ("ctem-vuln-prioritization-orchestrator", "Vulnerability flagged in a PIR"),
    ("ctem-threat-response-orchestrator", "Threat activity flagged in a PIR"),
    *(k for k in _FLEET_ASSIGNMENTS if k[0] == "opencti-pir-desk-orchestrator"),
    *(k for k in _FLEET_ASSIGNMENTS if k[0] == "openaev-detection-desk-orchestrator"),
    *(k for k in _FLEET_ASSIGNMENTS if k[0] == "opencrq-risk-desk-orchestrator"),
}

_EVENT_TRIGGERS = {"integration_event", "platform_event", "work_item"}


def _key(a: dict) -> tuple[str, str]:
    return a["agent_slug"], a["name"]


def test_no_seeded_assignment_ships_enabled_outside_the_allowlist():
    offenders = [
        _key(a) for a in BUILTIN_ASSIGNMENTS if a.get("enabled", True) and _key(a) not in ENABLED_BY_DEFAULT_ALLOWLIST
    ]
    assert offenders == [], (
        "A seeded automation never ships enabled; the customer opts in "
        "(dev-docs/ctem-fleets.md, backend AGENTS.md). Spread SEEDED_FLEET_OPT_IN into: "
        f"{offenders}"
    )


def test_no_seeded_event_or_work_item_assignment_ships_enabled():
    offenders = [
        _key(a) for a in BUILTIN_ASSIGNMENTS if a["trigger_type"] in _EVENT_TRIGGERS and a.get("enabled", True)
    ]
    assert offenders == []


def test_every_fleet_assignment_is_admin_opt_in_and_off():
    seeded = {_key(a): a for a in BUILTIN_ASSIGNMENTS}
    for key in _FLEET_ASSIGNMENTS:
        assert key in seeded, key
        assert seeded[key]["enabled"] is False, key
        assert seeded[key]["admin_opt_in"] is True, key
        assert key in ADMIN_OPT_IN_BUILTIN_ASSIGNMENTS, key
    assert SEEDED_FLEET_OPT_IN == {"enabled": False, "admin_opt_in": True}


def test_the_allowlist_names_exactly_the_14_pre_existing_automations():
    seeded = {_key(a) for a in BUILTIN_ASSIGNMENTS}
    assert len(ENABLED_BY_DEFAULT_ALLOWLIST) == 14
    assert ENABLED_BY_DEFAULT_ALLOWLIST.issubset(seeded)
    assert not (ENABLED_BY_DEFAULT_ALLOWLIST & _FLEET_ASSIGNMENTS)


def test_golden_dashboard_and_decay_automations_unchanged():
    """The run config of the 14 pre-existing automations, as shipped in 1.261102.0."""
    seeded = {_key(a): a for a in BUILTIN_ASSIGNMENTS}
    stages = {
        "ctem-scoping-analyst": "scoping",
        "ctem-discovery-analyst": "discovery",
        "ctem-prioritization-analyst": "prioritization",
        "ctem-validation-analyst": "validation",
        "ctem-mobilization-analyst": "remediation",
    }
    for slug, stage in stages.items():
        weekly = seeded[(slug, f"Weekly {stage} assessment")]
        assert (weekly["trigger_type"], weekly["trigger_config"], weekly["run_as_all_users"], weekly["enabled"]) == (
            "schedule",
            {"cron": "0 8 * * 1", "fanout_audience": "ctem_users"},
            True,
            True,
        )
        assert (weekly["max_iterations"], weekly["timeout_seconds"], weekly["retry_on_failure"]) == (14, 600, True)
        manual = seeded[(slug, f"Generate my {stage} assessment")]
        assert (manual["trigger_type"], manual["run_as_all_users"], manual["enabled"]) == ("manual", False, True)
        assert manual["prompt"] == weekly["prompt"]
        assert "admin_opt_in" not in weekly and "admin_opt_in" not in manual
    monthly = seeded[("exposure-briefing-analyst", "Monthly exposure briefing")]
    assert (monthly["trigger_type"], monthly["enabled"]) == ("schedule", True)
    assert seeded[("exposure-briefing-analyst", "Generate my exposure briefing")]["enabled"] is True
    assert seeded[("indicator-decay-guardian", "Re-check indicators about to decay")]["enabled"] is True
    hourly = seeded[("indicator-decay-guardian", "Hourly indicator decay re-check")]
    assert (hourly["enabled"], hourly["admin_opt_in"]) == (False, True)


def test_the_provisioning_state_keys_are_the_provisioners_own():
    from app.services import jira_webhooks, openaev_trigger_provisioning, opencti_trigger_provisioning

    assert set(PROVISIONING_STATE_KEYS) == {
        opencti_trigger_provisioning.STATE_KEY,
        openaev_trigger_provisioning.STATE_KEY,
        jira_webhooks.STATE_KEY,
    }


def _sql(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_inactive_builtin_fleet_is_no_home_fleet():
    """The home-fleet read keeps a built-in fleet only while its orchestrator has an enabled assignment."""
    import inspect

    from app.services import fleet_runs

    src = inspect.getsource(fleet_runs.home_fleet_defaults_of)
    assert "Fleet.is_builtin.is_(False)" in src and "builtin_active" in src


def test_handover_clause_drops_inactive_builtin_orchestrators():
    from app.services.fleet_runs import active_handover_target_clause

    sql = _sql(active_handover_target_clause())
    assert "fleets.orchestrator_agent_id = agents.id" in sql
    assert "fleets.is_builtin IS true" in sql
    assert "assignments.enabled IS true" in sql
    assert "NOT (EXISTS" in sql


# ── #9188: run as the enabling administrator; pre-existing members unchanged ──


def _agent(slug: str, bindings: list[str] | None = None):
    import uuid
    from types import SimpleNamespace

    return SimpleNamespace(id=uuid.uuid4(), slug=slug, is_builtin=True, integration_template_bindings=bindings or [])


def _assignment(name: str, **kw):
    from types import SimpleNamespace

    base = {"name": name, "enabled": True, "run_as_user_id": None, "user_id": "seed-owner", "trigger_config": {}}
    return SimpleNamespace(**{**base, **kw})


def _active_fleets(monkeypatch):
    """Every agent (and every caller) resolves to an active home fleet: F1 and F4 on."""
    import uuid

    from app.services import fleet_runs

    fleet = fleet_runs.FleetRunDefaults(fleet_id=uuid.uuid4(), budget_config={"max_cost_usd": 1.0}, max_iterations=3)

    async def _home(_db, agent_id):
        return fleet if agent_id is not None else fleet_runs.NO_FLEET

    monkeypatch.setattr(fleet_runs, "home_fleet_defaults", _home)
    return fleet


async def test_allowlisted_automation_prompts_unchanged_with_fleets_active(monkeypatch):
    """(a) No organization block on any of the 14 allowlisted automations, F1 and F4 active."""
    from app.services import organization_context as oc
    from app.services.fleet_runs import NO_FLEET, assignment_fleet_context

    _active_fleets(monkeypatch)
    for slug, name in sorted(ENABLED_BY_DEFAULT_ALLOWLIST):
        fleet = await assignment_fleet_context(None, _agent(slug), _assignment(name), caller_agent_id=None)  # type: ignore[arg-type]
        assert fleet is NO_FLEET, (slug, name)
        assert await oc.fleet_run_context(None, None, fleet=fleet) == ""  # type: ignore[arg-type]


async def test_pre_existing_member_gets_the_fleet_only_on_a_run_the_fleet_started(monkeypatch):
    from app.services.fleet_runs import NO_FLEET, assignment_fleet_context, fleet_run_defaults

    fleet = _active_fleets(monkeypatch)
    creator = _agent("openaev-scenario-creator", ["openaev"])
    # Intent-routed or direct: nobody in the fleet caused it.
    assert await assignment_fleet_context(None, creator, _assignment("Ad hoc"), None) is NO_FLEET  # type: ignore[arg-type]
    # Its orchestrator handed it over: the fleet started it.
    assert await assignment_fleet_context(None, creator, _assignment("Ad hoc"), "orch") == fleet  # type: ignore[arg-type]
    # Never the fleet's budget or iteration defaults.
    assert await fleet_run_defaults(None, creator) is NO_FLEET  # type: ignore[arg-type]
    # A fleet's own new agent keeps them.
    assert await fleet_run_defaults(None, _agent("ctem-threat-relevance-analyst")) == fleet  # type: ignore[arg-type]


def test_pre_existing_members_are_the_nine_agents_of_1_261102_0():
    from app.agents.builtin_fleets import BUILTIN_FLEETS
    from app.agents.fleet_opt_in import PRE_EXISTING_FLEET_MEMBERS

    assert len(PRE_EXISTING_FLEET_MEMBERS) == 9
    members = {m["agent"] for f in BUILTIN_FLEETS for m in f.get("members") or []}
    assert PRE_EXISTING_FLEET_MEMBERS.issubset(members)


def test_fleet_automations_are_the_fleet_ones_and_not_the_hourly_decay():
    from app.agents.builtin_agents import FLEET_OPT_IN_ASSIGNMENTS

    assert FLEET_OPT_IN_ASSIGNMENTS == _FLEET_ASSIGNMENTS
    assert ("indicator-decay-guardian", "Hourly indicator decay re-check") not in FLEET_OPT_IN_ASSIGNMENTS


async def test_enable_sets_run_as_to_the_caller(monkeypatch):
    """(b) Switching a fleet automation on stores the enabling user as run-as; an explicit one wins."""
    import uuid

    from app.services import fleet_run_as

    checked: list[tuple] = []

    async def _missing(_db, user_id, providers):
        checked.append((user_id, providers))
        return []

    monkeypatch.setattr(fleet_run_as, "missing_providers", _missing)
    admin = uuid.uuid4()
    orch = _agent("ctem-posture-orchestrator", ["opencti", "openaev", "opencrq"])
    row = _assignment("Monthly board report")
    db: Any = None
    await fleet_run_as.apply_fleet_run_as(
        db, agent=orch, assignment=row, actor_id=admin, was_enabled=False, run_as_changed=False
    )
    assert row.run_as_user_id == admin
    assert checked == [(admin, ["opencti", "openaev", "opencrq"])]
    explicit = uuid.uuid4()
    row2 = _assignment("Monthly board report", run_as_user_id=explicit)
    await fleet_run_as.apply_fleet_run_as(
        db, agent=orch, assignment=row2, actor_id=admin, was_enabled=False, run_as_changed=True
    )
    assert row2.run_as_user_id == explicit
    # The hourly decay re-check keeps its own rule.
    decay = _assignment("Hourly indicator decay re-check")
    guardian = _agent("indicator-decay-guardian", ["opencti"])
    await fleet_run_as.apply_fleet_run_as(
        db, agent=guardian, assignment=decay, actor_id=admin, was_enabled=False, run_as_changed=False
    )
    assert decay.run_as_user_id is None


async def test_enable_refused_without_the_module_token(monkeypatch):
    """(c) 400 naming the provider when the run-as user has no connected module."""
    import uuid

    import pytest
    from fastapi import HTTPException

    from app.services import fleet_run_as

    async def _missing(_db, _user_id, providers):
        return [p for p in providers if p == "opencti"]

    monkeypatch.setattr(fleet_run_as, "missing_providers", _missing)
    db: Any = None
    orch = _agent("ctem-threat-response-orchestrator", ["opencti", "openaev"])
    row = _assignment("New relevant threat", trigger_config={"provider": "opencti"})
    with pytest.raises(HTTPException) as exc:
        await fleet_run_as.apply_fleet_run_as(
            db, agent=orch, assignment=row, actor_id=uuid.uuid4(), was_enabled=False, run_as_changed=False
        )
    assert exc.value.status_code == 400 and "OpenCTI" in exc.value.detail
    # Same check on a run-as change of an enabled one.
    enabled = _assignment("Weekly threat sweep", run_as_user_id=uuid.uuid4())
    with pytest.raises(HTTPException):
        await fleet_run_as.apply_fleet_run_as(
            db, agent=orch, assignment=enabled, actor_id=uuid.uuid4(), was_enabled=True, run_as_changed=True
        )


async def test_provisioning_skips_a_fleet_automation_without_run_as():
    """(d) No run-as, no provisioning: never the seed owner's token."""
    from app.services.fleet_run_as import provisioning_user_id

    orch = _agent("ctem-threat-response-orchestrator", ["opencti"])
    assert provisioning_user_id(_assignment("New relevant threat"), orch) is None
    assert provisioning_user_id(_assignment("New relevant threat", run_as_user_id="admin"), orch) == "admin"
    # Any other assignment keeps run-as, then owner.
    other = _assignment("Hourly indicator decay re-check")
    assert provisioning_user_id(other, _agent("indicator-decay-guardian")) == "seed-owner"


def test_migration_switches_off_exactly_the_fleet_assignments():
    path = next(pathlib.Path(__file__).resolve().parents[1].glob("alembic/versions/c9160fleetoptin1_*.py"))
    spec = importlib.util.spec_from_file_location("c9160fleetoptin1", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert set(module._FLEET_ASSIGNMENTS) == _FLEET_ASSIGNMENTS - _SEEDED_AFTER_C9160
