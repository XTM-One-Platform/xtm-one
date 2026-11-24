"""CTEM seeded fleet F3, AI security (#9113): agents, assignments, fleet entry."""

from __future__ import annotations

from app.agents import ctem_ai_security as f3
from app.agents import fleet_seed
from app.agents.builtin_agents import BUILTIN_AGENTS, BUILTIN_ASSIGNMENTS
from app.agents.builtin_fleets import BUILTIN_FLEETS
from app.agents.builtin_roi_units import BUILTIN_ROI_UNITS
from app.agents.seed import SEED_DATA_DIR
from app.services.builtin_tools.agent_builder import work_item_trigger_error
from app.services.opencti_trigger_provisioning import read_provision_spec
from app.services.roi import count_units


def _agents() -> dict[str, dict]:
    return {a["slug"]: a for a in BUILTIN_AGENTS}


def test_the_two_agents_are_seeded_in_xtm_one_and_dev_with_a_persona():
    agents = _agents()
    for slug in (f3.ORCHESTRATOR, f3.THREAT_ANALYST):
        agent = agents[slug]
        assert agent["platform_targets"] == ["xtm_one", "dev"]
        assert agent["output_format"] == "json"
        assert (SEED_DATA_DIR / "agent_persona" / f"{slug}.md").is_file()
    assert [a["slug"] for a in BUILTIN_AGENTS].count(f3.ORCHESTRATOR) == 1


def test_personas_read_the_organization_context_and_keep_tests_behind_approval():
    orchestrator = (SEED_DATA_DIR / "agent_persona" / f"{f3.ORCHESTRATOR}.md").read_text(encoding="utf-8")
    assert "get_organization_context" in orchestrator
    assert "needs_human" in orchestrator
    analyst = (SEED_DATA_DIR / "agent_persona" / f"{f3.THREAT_ANALYST}.md").read_text(encoding="utf-8")
    assert "OpenCTI draft" in analyst
    assert "Never write them directly into the knowledge" in analyst


def test_the_orchestrator_delegates_to_the_analyst_and_the_shared_agents():
    orchestrator = _agents()[f3.ORCHESTRATOR]
    assert orchestrator["handover_agent_slugs"][0] == f3.THREAT_ANALYST
    assert set(f3.SHARED_AGENTS) <= set(orchestrator["handover_agent_slugs"])
    assert set(orchestrator["handover_type_slugs"].values()) == {"delegate"}
    analyst = _agents()[f3.THREAT_ANALYST]
    assert analyst["handover_type_slugs"] == {"cti-ttp-harvester-filigran-ai": "consult"}


def test_the_three_assignments_of_the_design():
    mine = {a["name"]: a for a in BUILTIN_ASSIGNMENTS if a["agent_slug"] == f3.ORCHESTRATOR}
    assert set(mine) == {"New AI-enabled report", "Weekly AI threat watch", "AI target added"}
    assert mine["Weekly AI threat watch"]["trigger_config"] == {"cron": "0 7 * * 2"}
    registered = mine["AI target added"]
    assert registered["trigger_config"]["correlation_prefix"] == "ctem:f3:ai-target:"
    # #1844: no AI system concept left in the fleet's words; AI targets are inventory assets.
    for assignment in mine.values():
        assert "AI system" not in assignment["prompt"] + assignment["description"]
    assert work_item_trigger_error(registered["trigger_type"], registered["trigger_config"]) is None
    event = mine["New AI-enabled report"]
    assert event["trigger_type"] == "integration_event"
    spec = read_provision_spec(event["trigger_config"])
    assert spec is not None
    assert spec["entity_types"] == ["Report"]
    assert spec["labels"] == ["ai"]


def test_the_fleet_entry_seeds_in_xtm_one_only_with_the_shared_agents_connected():
    assert f3.AI_SECURITY_FLEET in BUILTIN_FLEETS
    assert f3.FLEET_SLUG in [f["slug"] for f in fleet_seed.fleets_for_mode("xtm_one")]
    assert f3.FLEET_SLUG not in [f["slug"] for f in fleet_seed.fleets_for_mode("copilot")]
    fleet = f3.AI_SECURITY_FLEET
    members = {m["agent"] for m in fleet["members"]}
    connected = {c["agent"] for c in fleet["connected"]}
    assert members == {f3.ORCHESTRATOR, f3.THREAT_ANALYST}
    # Shared with F1: connected here, home in F1, never members of two fleets.
    assert set(f3.SHARED_AGENTS) <= connected
    assert not members & connected
    assert fleet["entry_handovers"] == [{"from": "ctem-assistant", "type": "delegate"}]


def test_roi_units_count_from_the_structured_output():
    analyst = BUILTIN_ROI_UNITS[f3.THREAT_ANALYST]
    assert analyst.unit == "atlas_mapping"
    output = {"atlas_techniques": [{"technique_id": "AML.T0051"}, {"technique_id": "AML.T0054"}]}
    assert count_units(analyst, output) == 2
    orchestrator = BUILTIN_ROI_UNITS[f3.ORCHESTRATOR]
    assert orchestrator.unit == "ai_test_plan"
    assert count_units(orchestrator, {"proposed_tests": [{"test": "prompt_injection"}]}) == 1
