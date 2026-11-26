"""CTEM fleet F2, vulnerability prioritization to verified fix (#9120, dev-docs/ctem-fleets.md s.4.2)."""

from __future__ import annotations

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


def test_the_assignments_are_on_the_orchestrator() -> None:
    assert all(a in BUILTIN_ASSIGNMENTS for a in CTEM_F2_ASSIGNMENTS)
    by_name = {a["name"]: a for a in CTEM_F2_ASSIGNMENTS}
    # The four of the spec, plus the case loop (#891) and software we run (CTEM-08).
    assert set(by_name) == {
        "New KEV or EPSS jump",
        "Vulnerability on software we run",
        "Vulnerability flagged in a PIR",
        "Vulnerability sighted",
        "Daily backlog pass",
        "Re-test due",
        "OpenAEV results into cases",
        "Analyst review mark",
        "Case opened or reopened",
        "Gate answered",
    }
    assert {a["agent_slug"] for a in CTEM_F2_ASSIGNMENTS} == {_ORCH}
    assert croniter.is_valid(by_name["Daily backlog pass"]["trigger_config"]["cron"])
    assert by_name["Daily backlog pass"]["trigger_config"]["cron"] == "0 6 * * *"
    assert by_name["Re-test due"]["trigger_config"] == {"correlation_prefix": "ctem:f2:retest:"}


def _f2(name: str) -> dict:
    return next(a for a in CTEM_F2_ASSIGNMENTS if a["name"] == name)


_TRIAGE_NAMES = (
    "New KEV or EPSS jump",
    "Vulnerability on software we run",
    "Vulnerability flagged in a PIR",
    "Vulnerability sighted",
)


def test_the_triage_runs_hear_the_signal_event_and_provision_nothing():
    """#2434: the signal is the platform source's; a fleet only subscribes to ctem_signal.recorded."""
    for name in _TRIAGE_NAMES:
        a = _f2(name)
        assert a["trigger_type"] == "platform_event", name
        config = a["trigger_config"]
        assert config["event"] == "ctem_signal.recorded" and config["scope"] == "all", name
        assert {"trigger_path": "new_vulnerability", "source_provider": "opencti"}.items() <= config["filter"].items()
        assert "provision" not in config and prov.read_provision_spec(config) is None, name
        assert "record it with record_ctem_signal" not in a["prompt"], name
        assert "list_ctem_signals signal_id" in a["prompt"], name


def _takes(name: str, facts: dict, **fields) -> bool:
    from app.services import platform_events as pe
    from app.services.integration_event_filters import payload_filter_reason

    payload = {
        "trigger_path": "new_vulnerability",
        "source_provider": "opencti",
        "verdict": "confirmed",
        "pir_floor_met": False,
        "facts": {"kev": False, "epss_high": False, "sighted": False, "software_named": False, "pir_flagged": False}
        | facts,
        **fields,
    }
    assert set(_f2(name)["trigger_config"]["filter"]) - {"any"} <= {
        *pe.PLATFORM_EVENTS["ctem_signal.recorded"]["filter_keys"],
        *(f"facts.{k}" for k in payload["facts"]),
    }
    return payload_filter_reason(payload, _f2(name)["trigger_config"]["filter"]) is None


def test_kev_or_epss_takes_either_fact():
    assert _takes("New KEV or EPSS jump", {"kev": True})
    assert _takes("New KEV or EPSS jump", {"epss_high": True})
    assert not _takes("New KEV or EPSS jump", {})


def test_sighted_takes_a_sighting_only():
    assert _takes("Vulnerability sighted", {"sighted": True})
    assert not _takes("Vulnerability sighted", {"kev": True})


def test_software_we_run_needs_a_named_source_the_gate_did_not_discard():
    assert _takes("Vulnerability on software we run", {"software_named": True})
    assert not _takes("Vulnerability on software we run", {"software_named": True}, verdict="discarded")
    assert not _takes("Vulnerability on software we run", {})


def test_pir_flag_run_is_held_to_the_policy_floor():
    assert _takes("Vulnerability flagged in a PIR", {"pir_flagged": True}, pir_floor_met=True)
    assert not _takes("Vulnerability flagged in a PIR", {"pir_flagged": True})


def test_routing_facts_carry_no_value():
    from app.services.ctem_signals import routing_facts

    assert routing_facts({"cisa_kev": True, "epss_score": 0.5, "stix_type": "sighting", "software": ["x"]}) == {
        "kev": True,
        "epss_high": True,
        "sighted": True,
        "software_named": True,
        "pir_flagged": False,
    }
    assert routing_facts({"cisa_kev": "false", "epss_score": 0.49, "pir": [{"pir_id": "p"}]}) == {
        "kev": False,
        "epss_high": False,
        "sighted": False,
        "software_named": False,
        "pir_flagged": True,
    }
    assert routing_facts(None)["epss_high"] is False and routing_facts({"epss_score": True})["epss_high"] is False
