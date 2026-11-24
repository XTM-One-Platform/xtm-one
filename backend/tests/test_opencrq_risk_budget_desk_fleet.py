"""Single-product seeded fleet M3: the OpenCRQ risk budget desk (#10593, epic #8975).

Since CTEM-31 (#1048) the desk is the Risk desk role of the generic CTEM loop
fleet: its agents and automations are unchanged, its fleet row is retired.
"""

from __future__ import annotations

from pathlib import Path

from app.agents import opencrq_risk_budget_desk as m3
from app.agents.builtin_agents import ADMIN_OPT_IN_BUILTIN_ASSIGNMENTS, BUILTIN_AGENTS, BUILTIN_ASSIGNMENTS
from app.agents.builtin_fleet_docs import (
    BUILTIN_FLEET_STRUCTURED,
    OPENCRQ_RISK_BUDGET_DESK_DOC,
    OPENCRQ_RISK_BUDGET_DESK_STRUCTURED,
)
from app.agents.builtin_fleets import BUILTIN_FLEETS, CTEM_LOOP_SLUG, RETIRED_FLEET_SUCCESSORS
from app.agents.builtin_roi_units import BUILTIN_ROI_EXEMPT, BUILTIN_ROI_UNITS, EXEMPT_ORCHESTRATOR
from app.agents.fleet_opt_in import ENABLED_BY_DEFAULT_ALLOWLIST
from app.services import opencrq_job_events, roi
from app.services.fleet_autonomy import gate_catalog, gated_tool_rules

_NEW = {m3.ORCHESTRATOR, m3.REGISTER_ANALYST, m3.INVESTMENT_ANALYST, m3.BOARD_REPORTER}
_PERSONAS = Path(m3.__file__).resolve().parent / "seed_data" / "agent_persona"


def _agents() -> dict[str, dict]:
    return {a["slug"]: a for a in BUILTIN_AGENTS}


def _assignments() -> list[dict]:
    return [a for a in BUILTIN_ASSIGNMENTS if a["agent_slug"] == m3.ORCHESTRATOR]


def test_the_desk_is_a_role_of_the_generic_loop_seeded_in_xtm_one_and_dev_never_in_copilot():
    from app.agents import fleet_seed

    assert m3.RISK_BUDGET_DESK_FLEET not in BUILTIN_FLEETS
    assert RETIRED_FLEET_SUCCESSORS[m3.FLEET_SLUG] == CTEM_LOOP_SLUG
    loop = next(f for f in fleet_seed.fleets_for_mode("xtm_one") if f["slug"] == CTEM_LOOP_SLUG)
    roles = {m["agent"]: m for m in loop["members"]}
    assert set(roles) >= _NEW
    role = roles[m3.ORCHESTRATOR]["role_label"]
    assert role == "Risk desk"
    assert fleet_seed.fleets_for_mode("copilot") == []
    for slug in _NEW:
        assert _agents()[slug]["platform_targets"] == ["xtm_one", "dev"]


def test_four_new_agents_and_the_existing_agents_connected_never_moved():
    fleet = m3.RISK_BUDGET_DESK_FLEET
    assert fleet["orchestrator"] == m3.ORCHESTRATOR
    assert {m["agent"] for m in fleet["members"]} == _NEW
    assert [c["agent"] for c in fleet["connected"]] == list(m3.SHARED_AGENTS)
    assert m3.SHARED_AGENTS == ("opencrq-assistant", "ctem-risk-quantifier")
    assert "entry_handovers" not in fleet
    slugs = [a["slug"] for a in BUILTIN_AGENTS]
    for slug in (*m3.SHARED_AGENTS, *_NEW):
        assert slugs.count(slug) == 1, slug


def test_every_agent_works_with_opencrq_alone():
    for slug in _NEW:
        assert _agents()[slug]["integration_template_bindings"] == ["opencrq"], slug
    orch = _agents()[m3.ORCHESTRATOR]
    assert set(orch["handover_agent_slugs"]) == (_NEW - {m3.ORCHESTRATOR}) | {"ctem-risk-quantifier"}
    assert set(orch["handover_type_slugs"]) == set(orch["handover_agent_slugs"])


def test_the_reader_roles_bind_opencrq_read_only():
    # They read the register and recommend; a person writes it in OpenCRQ (#1797).
    for slug in (m3.REGISTER_ANALYST, m3.INVESTMENT_ANALYST, m3.BOARD_REPORTER):
        assert _agents()[slug]["integration_template_read_only"] == ["opencrq"], slug


def test_every_assignment_is_admin_opt_in_and_off():
    seeded = _assignments()
    assert [a["name"] for a in seeded] == [a["name"] for a in m3.RISK_BUDGET_DESK_ASSIGNMENTS]
    # Five triggers of the desk plus the "Gate answered" listener (#1388).
    assert len(seeded) == 6
    for a in seeded:
        assert a["enabled"] is False and a["admin_opt_in"] is True, a["name"]
        assert (m3.ORCHESTRATOR, a["name"]) in ADMIN_OPT_IN_BUILTIN_ASSIGNMENTS
        assert (m3.ORCHESTRATOR, a["name"]) not in ENABLED_BY_DEFAULT_ALLOWLIST


def test_the_register_events_are_the_existing_opencrq_job_events():
    events = [a for a in m3.RISK_BUDGET_DESK_ASSIGNMENTS if a["trigger_type"] == "integration_event"]
    assert {a["trigger_config"]["event"] for a in events} == set(opencrq_job_events.EVENTS)
    for a in events:
        assert a["trigger_config"]["provider"] == opencrq_job_events.PROVIDER
        assert a["trigger_config"]["event"] == m3.REGISTER_EVENTS[a["name"]]
        # Polled by event_poller: no provisioning on the OpenCRQ side.
        assert "provision" not in a["trigger_config"]


def test_each_register_event_keeps_only_a_succeeded_run_of_its_own_job_kinds():
    from app.services.integration_event_filters import payload_filter_reason

    events = [a for a in m3.RISK_BUDGET_DESK_ASSIGNMENTS if a["trigger_type"] == "integration_event"]
    for a in events:
        event = a["trigger_config"]["event"]
        flt = a["trigger_config"]["filter"]
        for kind in opencrq_job_events.EVENT_KINDS[event]:
            run = {"id": "r1", "kind": kind, "status": "succeeded", "finishedAt": "2026-11-10T04:00:00Z"}
            payload = opencrq_job_events.run_event(run, event, base_url="https://crq", tenant_id="t")
            assert payload_filter_reason(payload, flt) is None, (a["name"], kind)
        failed = opencrq_job_events.run_event(
            {"id": "r2", "kind": opencrq_job_events.EVENT_KINDS[event][0], "status": "failed"},
            event,
            base_url="https://crq",
            tenant_id="t",
        )
        assert payload_filter_reason(failed, flt) is not None, a["name"]
        other = opencrq_job_events.run_event(
            {"id": "r3", "kind": "some-other-job", "status": "succeeded"}, event, base_url="https://crq", tenant_id="t"
        )
        assert payload_filter_reason(other, flt) is not None, a["name"]


def test_trimmed_snapshots_are_archived_never_lost() -> None:
    """A month pushed out of keep_last goes to the archive key, so the desk state stays small (#1417)."""
    assert m3.STATE_ARCHIVE_KEY.startswith(m3.STATE_KEY)
    events = [a for a in m3.RISK_BUDGET_DESK_ASSIGNMENTS if a["trigger_type"] == "integration_event"]
    for a in events:
        assert m3.STATE_ARCHIVE_KEY in a["prompt"], a["name"]
        assert f"keep_last {m3.SNAPSHOTS_KEPT}" in a["prompt"], a["name"]
    persona = (_PERSONAS / f"{m3.ORCHESTRATOR}.md").read_text(encoding="utf-8")
    assert m3.STATE_ARCHIVE_KEY in persona


def test_roi_on_seeded_units_and_the_orchestrator_counts_nothing_twice():
    assert BUILTIN_ROI_UNITS[m3.REGISTER_ANALYST].unit == "risk_scenario_quantified"
    assert BUILTIN_ROI_UNITS[m3.INVESTMENT_ANALYST].unit == "investment_case_written"
    assert BUILTIN_ROI_UNITS[m3.BOARD_REPORTER].unit == "briefing_written"
    assert BUILTIN_ROI_EXEMPT[m3.ORCHESTRATOR] == EXEMPT_ORCHESTRATOR
    for unit in ("risk_scenario_quantified", "investment_case_written", "briefing_written"):
        assert unit in roi.BASELINES_BY_UNIT, unit
    case = roi.BASELINES_BY_UNIT["investment_case_written"]
    assert case.manual_min_low <= case.manual_min_likely <= case.manual_min_high
    assert case.minutes_label == roi.LABEL_ASSUMPTION


def test_units_count_each_scenario_each_case_and_one_pack():
    register = {"headline": "h", "scenarios": [{"name": "a"}, {"name": "b"}], "missing_context": []}
    assert roi.count_units(BUILTIN_ROI_UNITS[m3.REGISTER_ANALYST], register) == 2
    cases = {"headline": "h", "cases": [{"control": "MFA"}, {"control": "EDR"}, {"control": "Backup"}]}
    assert roi.count_units(BUILTIN_ROI_UNITS[m3.INVESTMENT_ANALYST], cases) == 3
    pack = {"headline": "Register down 8%", "ale_trend": [], "top_controls": [], "plan_progress": []}
    assert roi.count_units(BUILTIN_ROI_UNITS[m3.BOARD_REPORTER], pack) == 1


def test_register_writes_are_a_decision_and_nothing_waits_at_low() -> None:
    # In the loop the register writes belong to ctem.remediation.decision (impact high),
    # by the persona: the gate names no tool (#1675), so no write pauses at any level.
    gates = BUILTIN_FLEET_STRUCTURED[CTEM_LOOP_SLUG]["gates"]
    decision = next(g for g in gates if g["key"] == "ctem.remediation.decision")
    assert decision["impact"] == "high"
    catalog = {e["key"]: e["gated"] for e in gate_catalog("low", gates)}
    assert catalog["ctem.remediation.decision"] is False
    assert "tools" not in decision
    assert gated_tool_rules("full_gates", [decision]) == []


def test_the_seeded_documentation_names_every_automation_and_the_known_limits():
    doc = OPENCRQ_RISK_BUDGET_DESK_DOC
    for a in m3.RISK_BUDGET_DESK_ASSIGNMENTS:
        assert f"**{a['name']}**" in doc["triggers"]
    structured = OPENCRQ_RISK_BUDGET_DESK_STRUCTURED
    assert [t["name"] for t in structured["triggers"]] == [a["name"] for a in m3.RISK_BUDGET_DESK_ASSIGNMENTS]
    assert {p["check"] for p in structured["prerequisites"]} == {"integration:opencrq", "manual"}
    for section in ("purpose", "use_case", "flow", "objects_created", "prerequisites"):
        assert doc[section].strip(), section
    assert "beta" in doc["prerequisites"] and "February 2027" in doc["prerequisites"]
    assert "writes a treatment plan only through the register write gate" in doc["prerequisites"]
    assert "OpenCTI is never required" in doc["purpose"]


def test_the_personas_read_and_recommend_never_write_plans():
    for slug in _NEW:
        assert (_PERSONAS / f"{slug}.md").read_text(encoding="utf-8").strip(), slug
    orchestrator = (_PERSONAS / f"{m3.ORCHESTRATOR}.md").read_text(encoding="utf-8")
    assert "needs_human" in orchestrator and "read only" in orchestrator
    assert "OpenCTI is never required" in orchestrator
    investment = (_PERSONAS / f"{m3.INVESTMENT_ANALYST}.md").read_text(encoding="utf-8")
    assert "get_opencrq_crq_what_if" in investment and "never write" in investment
