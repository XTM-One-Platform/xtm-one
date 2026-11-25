"""Single-product seeded fleet M2: the OpenAEV detection validation desk (#10575, epic #8975).

Since CTEM-31 (#1048) the desk is the Validation desk role of the generic CTEM loop
fleet: its agents and automations are unchanged, its fleet row is retired.
"""

from __future__ import annotations

from pathlib import Path

from app.agents import openaev_detection_desk as m2
from app.agents.builtin_agents import ADMIN_OPT_IN_BUILTIN_ASSIGNMENTS, BUILTIN_AGENTS, BUILTIN_ASSIGNMENTS
from app.agents.builtin_fleet_docs import (
    BUILTIN_FLEET_STRUCTURED,
    OPENAEV_DETECTION_DESK_DOC,
    OPENAEV_DETECTION_DESK_STRUCTURED,
)
from app.agents.builtin_fleets import BUILTIN_FLEETS, CTEM_LOOP_SLUG, RETIRED_FLEET_SUCCESSORS
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


def test_the_desk_is_a_role_of_the_generic_loop_seeded_in_xtm_one_and_dev_never_in_copilot():
    from app.agents import fleet_seed

    assert m2.DETECTION_DESK_FLEET not in BUILTIN_FLEETS
    assert RETIRED_FLEET_SUCCESSORS[m2.FLEET_SLUG] == CTEM_LOOP_SLUG
    loop = next(f for f in fleet_seed.fleets_for_mode("dev") if f["slug"] == CTEM_LOOP_SLUG)
    roles = {m["agent"]: m for m in loop["members"]}
    assert set(roles) >= _NEW
    role = roles[m2.ORCHESTRATOR]["role_label"]
    assert role == "Validation desk"
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


def test_in_the_loop_a_simulation_launch_is_the_validation_decision_never_a_tool_pause() -> None:
    # Sam, 23 Nov 2026 (#1339): out of the box two CTEM gates wait, ctem.mobilize and the
    # OpenAEV validation. Sam, 24 Nov 2026 (#1675): the wait is a decision item, no tool pauses.
    gates = BUILTIN_FLEET_STRUCTURED[CTEM_LOOP_SLUG]["gates"]
    validation = next(g for g in gates if g["key"] == "ctem.validation.non_readonly")
    assert "tools" not in validation
    assert gated_tool_rules("low", gates) == []
    assert gated_tool_rules("full_gates", gates) == []


def test_the_seeded_documentation_names_every_automation():
    triggers = OPENAEV_DETECTION_DESK_DOC["triggers"]
    for a in m2.DETECTION_DESK_ASSIGNMENTS:
        assert f"**{a['name']}**" in triggers
    structured = OPENAEV_DETECTION_DESK_STRUCTURED
    assert [t["name"] for t in structured["triggers"]] == [a["name"] for a in m2.DETECTION_DESK_ASSIGNMENTS]
    loop = BUILTIN_FLEET_STRUCTURED[CTEM_LOOP_SLUG]
    assert "Simulation finished" in {t["name"] for t in loop["triggers"]}
    assert {p["check"] for p in loop["prerequisites"]} >= {"integration:openaev", "manual"}
    for section in ("purpose", "use_case", "flow", "objects_created", "prerequisites"):
        assert OPENAEV_DETECTION_DESK_DOC[section].strip(), section


def test_the_gap_sorting_covers_prevention_detection_and_logging_and_root_causes():
    gap = m2._GAP_SCHEMA["properties"]["gaps"]["items"]["properties"]
    assert gap["verdict"]["enum"] == ["blocked", "detected", "logged", "missed"]
    assert set(gap["missing"]["items"]["enum"]) == {"prevention", "detection", "logging"}
    assert {"no_rule", "no_log", "broken_collector"} <= set(gap["root_cause"]["enum"])


def _not_executed_replay() -> dict:
    """An analyst answer for one simulation where some targets never ran the payload (#2380)."""
    return {
        "simulation": "Weekly APT29 (sim-1)",
        "gaps": [
            {
                "technique": "T1059",
                "asset": "srv-1",
                "verdict": "logged",
                "missing": ["prevention", "detection"],
                "root_cause": "no_rule",
                "evidence": "Logging expectation met, detection expectation failed.",
            }
        ],
        "not_executed": [
            {"technique": "T1059", "asset": f"host-{status.lower()}", "status": status, "evidence": status}
            for status in ("AGENT_INACTIVE", "TIMEOUT", "ERROR", "ASSET_AGENTLESS")
        ],
        "coverage": {"prevention": 0, "detection": 0, "logging": 100},
        "missing_context": [],
    }


def test_a_payload_that_never_ran_is_listed_apart_never_a_gap():
    import jsonschema

    schema = m2._GAP_SCHEMA
    assert "not_executed" in schema["required"]
    item = schema["properties"]["not_executed"]["items"]
    assert set(item["required"]) == {"technique", "asset", "status", "evidence"}
    assert {"AGENT_INACTIVE", "TIMEOUT", "ERROR", "ASSET_AGENTLESS"} <= set(item["properties"]["status"]["enum"])
    # A gap is a pair that ran: no verdict or root cause stands for an untested pair.
    gap = schema["properties"]["gaps"]["items"]["properties"]
    assert "not_executed" not in gap["verdict"]["enum"] and "not_executed" not in gap["root_cause"]["enum"]
    assert "not_executed" in schema["properties"]["gaps"]["description"]
    assert "not_executed" in schema["properties"]["coverage"]["description"]

    answer = _not_executed_replay()
    jsonschema.validate(answer, schema)
    untested = {p["asset"] for p in answer["not_executed"]}
    assert not untested & {g["asset"] for g in answer["gaps"]}
    # An untested pair can no longer pass as a gap row.
    bad = {**answer["not_executed"][0], "verdict": "not_executed", "missing": [], "root_cause": "not_executed"}
    try:
        jsonschema.validate({**answer, "gaps": [bad]}, schema)
    except jsonschema.ValidationError:
        pass
    else:
        raise AssertionError("an untested pair validated as a gap")
    # One simulation still counts one analysis, the untested pairs included.
    assert roi.count_units(BUILTIN_ROI_UNITS[m2.GAP_ANALYST], answer) == 1


def test_both_personas_keep_an_untested_pair_out_of_the_gaps_and_the_remediation():
    analyst = (_PERSONAS / f"{m2.GAP_ANALYST}.md").read_text(encoding="utf-8")
    for status in ("AGENT_INACTIVE", "TIMEOUT", "ERROR", "ASSET_AGENTLESS"):
        assert f"`{status}`" in analyst, status
    assert "`not_executed`" in analyst and "never a gap" in analyst
    assert "count in no denominator" in analyst
    orchestrator = (_PERSONAS / f"{m2.ORCHESTRATOR}.md").read_text(encoding="utf-8")
    sort_step = next(line for line in orchestrator.splitlines() if line.startswith("3. **Sort**"))
    assert "`not_executed`" in sort_step
    gap_step = next(line for line in orchestrator.splitlines() if m2.REMEDIATION_PREFIX in line)
    assert f"A `not_executed` pair gets no `{m2.REMEDIATION_PREFIX}` item" in gap_step
    assert "at most one per asset group and simulation" in gap_step


def test_no_hard_dependency_on_the_new_findings_model():
    for slug in _NEW:
        assert (_PERSONAS / f"{slug}.md").read_text(encoding="utf-8").strip(), slug
    analyst = (_PERSONAS / f"{m2.GAP_ANALYST}.md").read_text(encoding="utf-8")
    assert "never require" in analyst
    orchestrator = (_PERSONAS / f"{m2.ORCHESTRATOR}.md").read_text(encoding="utf-8")
    assert "never required" in orchestrator and "needs_human" in orchestrator
