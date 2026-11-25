"""CTEM seeded fleet F5, third-party risk (#9157 slice M5): agent, assignments, fleet entry."""

from __future__ import annotations

from app.agents import ctem_third_party_risk as f5
from app.agents import fleet_seed
from app.agents.builtin_agents import ADMIN_OPT_IN_BUILTIN_ASSIGNMENTS, BUILTIN_AGENTS, BUILTIN_ASSIGNMENTS
from app.agents.builtin_fleets import BUILTIN_FLEETS
from app.agents.seed import SEED_DATA_DIR
from app.services.opencti_trigger_provisioning import TRACKED_SCOPE, read_provision_spec


def _agents() -> dict[str, dict]:
    return {a["slug"]: a for a in BUILTIN_AGENTS}


def _persona() -> str:
    return (SEED_DATA_DIR / "agent_persona" / f"{f5.ORCHESTRATOR}.md").read_text(encoding="utf-8")


def test_the_orchestrator_is_seeded_once_in_xtm_one_and_dev_with_a_persona():
    agent = _agents()[f5.ORCHESTRATOR]
    assert agent["platform_targets"] == ["xtm_one", "dev"]
    assert agent["output_format"] == "json"
    assert agent["integration_template_bindings"] == ["opencti", "opencrq"]
    assert [a["slug"] for a in BUILTIN_AGENTS].count(f5.ORCHESTRATOR) == 1
    assert (SEED_DATA_DIR / "agent_persona" / f"{f5.ORCHESTRATOR}.md").is_file()


def test_the_persona_never_contacts_a_third_party_and_keeps_actions_behind_approval():
    persona = _persona()
    assert "Never contact a third party automatically" in persona
    assert "needs_human" in persona
    assert f5.APPROVAL_PREFIX in persona
    assert "get_organization_context" in persona
    # D3 = A: the loss stays the primary's, read through a supporting asset.
    assert "supporting asset" in persona
    assert "never create an OpenCRQ scope for a third party" in persona


def test_the_orchestrator_has_no_tool_that_writes_to_a_third_party():
    agent = _agents()[f5.ORCHESTRATOR]
    assert agent["builtin_tool_ids"] == ["work_items", "shared_state"]
    assert set(agent["handover_type_slugs"].values()) == {"delegate"}
    assert set(agent["handover_agent_slugs"]) == set(f5.SHARED_AGENTS)


def test_every_proposed_action_is_an_approval_item_in_the_case_schema():
    schema = _agents()[f5.ORCHESTRATOR]["output_schema"]
    item = schema["properties"]["proposed_actions"]["items"]
    assert "approval_item" in item["required"]
    assert item["properties"]["action"]["enum"] == list(f5.PROPOSED_ACTIONS)


def test_the_assignments_are_opt_in_and_off_by_default():
    mine = {a["name"]: a for a in BUILTIN_ASSIGNMENTS if a["agent_slug"] == f5.ORCHESTRATOR}
    assert set(mine) == {"Tracked organization named", "Weekly third-party review"}
    for name, assignment in mine.items():
        assert assignment["enabled"] is False, name
        assert assignment["admin_opt_in"] is True, name
        assert (f5.ORCHESTRATOR, name) in ADMIN_OPT_IN_BUILTIN_ASSIGNMENTS


def test_the_event_assignment_provisions_on_tracked_organizations():
    event = next(
        a
        for a in BUILTIN_ASSIGNMENTS
        if a["agent_slug"] == f5.ORCHESTRATOR and a["name"] == "Tracked organization named"
    )
    assert event["trigger_type"] == "integration_event"
    spec = read_provision_spec(event["trigger_config"])
    assert spec is not None
    assert spec["organizations"] == TRACKED_SCOPE
    assert sorted(spec["entity_types"]) == ["Incident", "Report"]
    assert spec["organization_context"] == []


def test_the_fleet_entry_seeds_in_xtm_one_only_with_the_shared_agents_connected():
    assert f5.THIRD_PARTY_RISK_FLEET in BUILTIN_FLEETS
    # Defined, not seeded until tested (#2105): switched on, it seeds in xtm_one only.
    assert f5.FLEET_SLUG in [f["slug"] for f in fleet_seed.fleets_for_mode("xtm_one", BUILTIN_FLEETS)]
    assert f5.FLEET_SLUG not in [f["slug"] for f in fleet_seed.fleets_for_mode("copilot", BUILTIN_FLEETS)]
    fleet = f5.THIRD_PARTY_RISK_FLEET
    members = {m["agent"] for m in fleet["members"]}
    connected = {c["agent"] for c in fleet["connected"]}
    assert members == {f5.ORCHESTRATOR}
    # D1 = A: F1's role agents are reused by slug, connected here, home in F1.
    assert set(f5.SHARED_AGENTS) <= connected
    assert not members & connected
    assert fleet["entry_handovers"] == [{"from": "ctem-assistant", "type": "delegate"}]
