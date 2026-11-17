"""Single-product seeded fleet M2: the OpenAEV detection validation desk (#10575, epic #8975)."""

from __future__ import annotations

from pathlib import Path

from app.agents import openaev_detection_desk as m2
from app.agents.builtin_agents import ADMIN_OPT_IN_BUILTIN_ASSIGNMENTS, BUILTIN_AGENTS, BUILTIN_ASSIGNMENTS
from app.agents.builtin_fleet_docs import BUILTIN_FLEET_DOCS, BUILTIN_FLEET_STRUCTURED
from app.agents.builtin_fleets import BUILTIN_FLEETS
from app.agents.builtin_roi_units import BUILTIN_ROI_EXEMPT, BUILTIN_ROI_UNITS, EXEMPT_ORCHESTRATOR
from app.agents.fleet_opt_in import ENABLED_BY_DEFAULT_ALLOWLIST
from app.services import openaev_trigger_provisioning as prov
from app.services import roi
from app.services.fleet_autonomy import gated_tool_rules

_NEW = {m2.ORCHESTRATOR, m2.GAP_ANALYST, m2.COVERAGE_REPORTER}
_PERSONAS = Path(m2.__file__).resolve().parent / "seed_data" / "agent_persona"


def _agents() -> dict[str, dict]:
    return {a["slug"]: a for a in BUILTIN_AGENTS}


def _assignments() -> list[dict]:
    return [a for a in BUILTIN_ASSIGNMENTS if a["agent_slug"] == m2.ORCHESTRATOR]


def test_the_fleet_is_seeded_in_xtm_one_and_dev_never_in_copilot():
    from app.agents import fleet_seed

    assert m2.DETECTION_DESK_FLEET in BUILTIN_FLEETS
    assert m2.FLEET_SLUG in [f["slug"] for f in fleet_seed.fleets_for_mode("xtm_one")]
    assert m2.FLEET_SLUG in [f["slug"] for f in fleet_seed.fleets_for_mode("dev")]
    assert fleet_seed.fleets_for_mode("copilot") == []
    for slug in _NEW:
        assert _agents()[slug]["platform_targets"] == ["xtm_one", "dev"]


def test_three_new_agents_and_the_existing_agents_connected_never_moved():
    fleet = m2.DETECTION_DESK_FLEET
    assert fleet["orchestrator"] == m2.ORCHESTRATOR
    assert {m["agent"] for m in fleet["members"]} == _NEW
    assert [c["agent"] for c in fleet["connected"]] == list(m2.SHARED_AGENTS)
    assert not ({c["agent"] for c in fleet["connected"]} & _NEW)
    assert "entry_handovers" not in fleet
    for slug in m2.SHARED_AGENTS:
        assert slug in _agents(), slug


def test_no_existing_agent_is_changed():
    slugs = [a["slug"] for a in BUILTIN_AGENTS]
    for slug in (*m2.SHARED_AGENTS, *_NEW):
        assert slugs.count(slug) == 1, slug
    assert not [a for a in m2.DETECTION_DESK_AGENTS if a["slug"] in m2.SHARED_AGENTS]


def test_the_orchestrator_reaches_the_rule_chain_and_the_scenario_agents():
    orch = _agents()[m2.ORCHESTRATOR]
    assert set(orch["handover_agent_slugs"]) == {m2.GAP_ANALYST, m2.COVERAGE_REPORTER} | (
        set(m2.SHARED_AGENTS) - {"openaev-assistant"}
    )
    assert set(orch["handover_type_slugs"]) == set(orch["handover_agent_slugs"])


def test_every_assignment_is_admin_opt_in_and_off():
    seeded = _assignments()
    assert [a["name"] for a in seeded] == [a["name"] for a in m2.DETECTION_DESK_ASSIGNMENTS]
    for a in seeded:
        assert a["enabled"] is False and a["admin_opt_in"] is True, a["name"]
        assert (m2.ORCHESTRATOR, a["name"]) in ADMIN_OPT_IN_BUILTIN_ASSIGNMENTS
        assert (m2.ORCHESTRATOR, a["name"]) not in ENABLED_BY_DEFAULT_ALLOWLIST


def test_the_trigger_listens_to_finished_simulations_through_the_provisioner():
    event = next(a for a in m2.DETECTION_DESK_ASSIGNMENTS if a["trigger_type"] == "integration_event")
    assert event["trigger_config"]["provider"] == "openaev"
    assert event["trigger_config"]["event"] == "simulation_finished"
    spec = prov.read_provision_spec(event["trigger_config"])
    assert spec is not None
    assert spec["events"] == ["simulation_finished"]
    assert spec["fleet"] == m2.FLEET_NAME
    body = prov.trigger_body(spec, "simulation_finished", "n1")
    assert body["notification_trigger_resource_type"] == "SIMULATION"


def test_the_retest_assignment_listens_to_the_desk_key_only():
    retest = next(a for a in m2.DETECTION_DESK_ASSIGNMENTS if a["trigger_type"] == "work_item")
    assert retest["trigger_config"] == {"correlation_prefix": m2.RETEST_PREFIX}
    assert m2.RETEST_PREFIX.startswith("openaev:detection-desk:")


def test_roi_on_seeded_units_and_the_orchestrator_counts_nothing_twice():
    assert BUILTIN_ROI_UNITS[m2.GAP_ANALYST].unit == "simulation_result_analysed"
    assert BUILTIN_ROI_UNITS[m2.COVERAGE_REPORTER].unit == "retest_cycle"
    assert BUILTIN_ROI_EXEMPT[m2.ORCHESTRATOR] == EXEMPT_ORCHESTRATOR
    for unit in ("simulation_result_analysed", "retest_cycle", "detection_rule_written"):
        assert unit in roi.BASELINES_BY_UNIT, unit
    # Rules drafted are counted by the existing generators the fleet consults.
    assert BUILTIN_ROI_UNITS["remediation-splunk-rule-generator"].unit == "detection_rule_written"
    assert BUILTIN_ROI_UNITS["remediation-crowdstrike-rule-generator"].unit == "detection_rule_written"


def test_one_analysis_counts_one_simulation_and_each_closed_gap_one_retest():
    analysed = roi.count_units(BUILTIN_ROI_UNITS[m2.GAP_ANALYST], {"simulation": "Weekly APT29 (sim-1)", "gaps": []})
    assert analysed == 1
    report = {"headline": "h", "closed_gaps": ["T1059 on srv-1", "T1003 on dc-1"], "open_gaps": ["T1021"]}
    assert roi.count_units(BUILTIN_ROI_UNITS[m2.COVERAGE_REPORTER], report) == 2


def test_the_simulation_launch_is_the_only_gate_and_it_waits_at_low():
    structured = BUILTIN_FLEET_STRUCTURED[m2.FLEET_SLUG]
    assert [g["key"] for g in structured["gates"]] == ["openaev.simulation.launch"]
    assert structured["gates"][0]["impact"] == "destructive"
    names = {r["name"] for r in gated_tool_rules("low", structured["gates"])}
    assert names == {"manage_openaev_simulation", "manage_openaev_atomic_testing"}


def test_the_seeded_documentation_names_every_automation():
    triggers = BUILTIN_FLEET_DOCS[m2.FLEET_SLUG]["triggers"]
    for a in m2.DETECTION_DESK_ASSIGNMENTS:
        assert f"**{a['name']}**" in triggers
    structured = BUILTIN_FLEET_STRUCTURED[m2.FLEET_SLUG]
    assert [t["name"] for t in structured["triggers"]] == [a["name"] for a in m2.DETECTION_DESK_ASSIGNMENTS]
    assert {p["check"] for p in structured["prerequisites"]} >= {"integration:openaev", "manual"}
    for section in ("purpose", "use_case", "flow", "objects_created", "prerequisites"):
        assert BUILTIN_FLEET_DOCS[m2.FLEET_SLUG][section].strip(), section


def test_the_gap_sorting_covers_prevention_detection_and_logging_and_root_causes():
    gap = m2._GAP_SCHEMA["properties"]["gaps"]["items"]["properties"]
    assert gap["verdict"]["enum"] == ["blocked", "detected", "logged", "missed"]
    assert set(gap["missing"]["items"]["enum"]) == {"prevention", "detection", "logging"}
    assert {"no_rule", "no_log", "broken_collector"} <= set(gap["root_cause"]["enum"])


def test_no_hard_dependency_on_the_new_findings_model():
    for slug in _NEW:
        assert (_PERSONAS / f"{slug}.md").read_text(encoding="utf-8").strip(), slug
    analyst = (_PERSONAS / f"{m2.GAP_ANALYST}.md").read_text(encoding="utf-8")
    assert "never require" in analyst
    orchestrator = (_PERSONAS / f"{m2.ORCHESTRATOR}.md").read_text(encoding="utf-8")
    assert "never required" in orchestrator and "needs_human" in orchestrator
