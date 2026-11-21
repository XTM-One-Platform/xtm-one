"""CTEM fleet F2, vulnerability prioritization to verified fix (#9120, dev-docs/ctem-fleets.md s.4.2)."""

from __future__ import annotations

import json

from croniter import croniter

from app.agents.builtin_agents import BUILTIN_AGENTS, BUILTIN_ASSIGNMENTS, CTEM_F2_ASSIGNMENTS
from app.agents.builtin_fleets import BUILTIN_FLEETS, CTEM_VULN_PRIORITIZATION_FLEET
from app.agents.builtin_roi_units import BUILTIN_ROI_UNITS
from app.agents.seed import _resolve_seed_content, _resolve_seed_schema
from app.services import opencti_trigger_provisioning as prov

_ORCH = "ctem-vuln-prioritization-orchestrator"
_TRIAGE = "ctem-vuln-triage-analyst"
# The CTEM role agents F1 (#9114) seeds; F2 reuses them by slug.
_SHARED_WITH_F1 = {"ctem-risk-quantifier", "ctem-remediation-coordinator"}


def _agents() -> dict[str, dict]:
    return {a["slug"]: a for a in BUILTIN_AGENTS}


def test_the_fleet_is_registered_and_xtm_one_only():
    assert CTEM_VULN_PRIORITIZATION_FLEET in BUILTIN_FLEETS
    assert CTEM_VULN_PRIORITIZATION_FLEET["platform_targets"] == ["xtm_one", "dev"]
    assert CTEM_VULN_PRIORITIZATION_FLEET["orchestrator"] == _ORCH
    assert {"from": "ctem-assistant", "type": "delegate"} in CTEM_VULN_PRIORITIZATION_FLEET["entry_handovers"]


def test_members_are_the_two_new_agents_and_shared_agents_are_connected():
    fleet = CTEM_VULN_PRIORITIZATION_FLEET
    members = {m["agent"] for m in fleet["members"]}
    connected = {c["agent"] for c in fleet["connected"]}
    assert members == {_ORCH, _TRIAGE}
    # One home fleet per agent: a shared or folded agent is never a member here.
    assert connected >= _SHARED_WITH_F1
    assert not members & connected


def test_every_named_agent_is_a_builtin_or_a_shared_f1_agent():
    agents = _agents()
    fleet = CTEM_VULN_PRIORITIZATION_FLEET
    named = (
        {m["agent"] for m in fleet["members"]}
        | {c["agent"] for c in fleet["connected"]}
        | {h["from"] for h in fleet["entry_handovers"]}
    )
    unknown = named - set(agents) - _SHARED_WITH_F1
    assert not unknown, unknown
    for slug in (_ORCH, _TRIAGE):
        for target in agents[slug].get("handover_agent_slugs", []):
            assert target in agents or target in _SHARED_WITH_F1, f"{slug} -> {target}"


def test_the_new_agents_ship_persona_and_schema():
    agents = _agents()
    for slug in (_ORCH, _TRIAGE):
        assert agents[slug]["platform_targets"] == ["xtm_one", "dev"]
        assert _resolve_seed_content("agent_persona", agents[slug]["persona"])
    schema = _resolve_seed_schema(agents[_TRIAGE]["output_schema"])
    assert schema and "findings" in schema["properties"]
    match = schema["properties"]["findings"]["items"]["properties"]["match"]
    assert match["enum"] == ["matched", "not_applicable", "unknown"]


def test_the_triage_analyst_counts_cve_assessed():
    decl = BUILTIN_ROI_UNITS[_TRIAGE]
    assert (decl.unit, decl.count_path) == ("cve_assessed", "findings")


def test_the_assignments_are_on_the_orchestrator():
    assert all(a in BUILTIN_ASSIGNMENTS for a in CTEM_F2_ASSIGNMENTS)
    by_name = {a["name"]: a for a in CTEM_F2_ASSIGNMENTS}
    # The four of the spec, plus the case loop (#891).
    assert set(by_name) == {
        "New KEV or EPSS jump",
        "Vulnerability sighted",
        "Daily backlog pass",
        "Re-test due",
        "Case opened or reopened",
    }
    assert {a["agent_slug"] for a in CTEM_F2_ASSIGNMENTS} == {_ORCH}
    assert croniter.is_valid(by_name["Daily backlog pass"]["trigger_config"]["cron"])
    assert by_name["Daily backlog pass"]["trigger_config"]["cron"] == "0 6 * * *"
    assert by_name["Re-test due"]["trigger_config"] == {"correlation_prefix": "ctem:f2:retest:"}


def test_kev_or_epss_trigger_provisions_one_or_group():
    config = next(a for a in CTEM_F2_ASSIGNMENTS if a["name"] == "New KEV or EPSS jump")["trigger_config"]
    spec = prov.read_provision_spec(config)
    assert spec["entity_types"] == ["Vulnerability"]
    filters = json.loads(prov.trigger_filters(spec, []))
    assert {f["key"][0] for f in filters["filters"]} == {"entity_type"}
    (group,) = filters["filterGroups"]
    assert group["mode"] == "or"
    assert {f["key"][0]: f["values"] for f in group["filters"]} == {
        "x_opencti_cisa_kev": ["true"],
        "x_opencti_epss_score": ["0.5"],
    }


def test_sighting_trigger_filters_on_the_sighted_type():
    config = next(a for a in CTEM_F2_ASSIGNMENTS if a["name"] == "Vulnerability sighted")["trigger_config"]
    spec = prov.read_provision_spec(config)
    keys = {f["key"][0]: f for f in json.loads(prov.trigger_filters(spec, []))["filters"]}
    assert keys["entity_type"]["values"] == ["stix-sighting-relationship"]
    assert keys["fromTypes"]["values"] == ["Vulnerability"]
