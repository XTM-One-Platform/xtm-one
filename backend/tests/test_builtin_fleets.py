"""Built-in fleets (#8988): mode selection, parent resolution, the write guard."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents import fleet_seed
from app.agents.builtin_fleets import (
    BUILTIN_FLEETS,
    SEEDED_BUILTIN_FLEET_SLUGS,
    SEEDED_BUILTIN_FLEETS,
    UNSEEDED_FLEET_AGENT_SLUGS,
)
from app.services.fleets import (
    BUILTIN_FLEET_WRITABLE_FIELDS,
    FleetActionRefusedError,
    ensure_builtin_fleet_writable,
)

_FLEETS = [
    {"slug": "default-modes", "name": "A"},
    {"slug": "copilot-only", "name": "B", "platform_targets": ["copilot"]},
    {"slug": "global", "name": "C", "platform_targets": ["global"]},
    {"slug": "dev-only", "name": "D", "platform_targets": ["dev"]},
]


def _slugs(mode: str) -> list[str]:
    return [f["slug"] for f in fleet_seed.fleets_for_mode(mode, _FLEETS)]


def test_xtm_one_and_dev_seed_their_entries():
    assert _slugs("xtm_one") == ["default-modes", "global"]
    assert _slugs("dev") == ["default-modes", "global", "dev-only"]


def test_copilot_mode_seeds_no_fleet_whatever_the_targets():
    assert _slugs("copilot") == []


def test_builtin_fleets_seed_in_xtm_one_and_never_in_copilot():
    assert [f["slug"] for f in fleet_seed.fleets_for_mode("xtm_one")] == [f["slug"] for f in SEEDED_BUILTIN_FLEETS]
    assert fleet_seed.fleets_for_mode("copilot") == []


def test_four_defined_fleets_one_generic_and_three_use_cases():
    # CTEM-31 (#1048, Sam, 21 Nov 2026): a fifth defined fleet is a product decision, never a fix.
    assert [f["slug"] for f in BUILTIN_FLEETS] == [
        "ctem-loop",
        "ctem-vuln-prioritization",
        "ctem-ai-security",
        "ctem-third-party-risk",
    ]


def test_only_the_ctem_loop_is_seeded_until_the_use_cases_are_tested():
    # Sam, 25 Nov 2026 (#2105): the three use cases stay defined, not seeded.
    assert {"ctem-loop"} == SEEDED_BUILTIN_FLEET_SLUGS
    for mode in ("xtm_one", "dev"):
        assert [f["slug"] for f in fleet_seed.fleets_for_mode(mode)] == ["ctem-loop"]
    assert fleet_seed.fleets_for_mode("copilot") == []


def test_the_agents_only_the_unseeded_fleets_hold_are_withheld_with_their_assignments():
    from app.agents import seed
    from app.agents.builtin_agents import BUILTIN_ASSIGNMENTS
    from app.agents.ctem_ai_security import ORCHESTRATOR as AI_ORCH
    from app.agents.ctem_ai_security import THREAT_ANALYST
    from app.agents.ctem_third_party_risk import ORCHESTRATOR as TPR_ORCH

    exclusive = {
        "ctem-vuln-prioritization-orchestrator",
        "ctem-vuln-triage-analyst",
        AI_ORCH,
        THREAT_ANALYST,
        TPR_ORCH,
    }
    assert exclusive == UNSEEDED_FLEET_AGENT_SLUGS
    # Agents the CTEM loop names stay seeded.
    loop = next(f for f in BUILTIN_FLEETS if f["slug"] == "ctem-loop")
    loop_named = {
        loop["orchestrator"],
        *(m["agent"] for m in loop["members"]),
        *(c["agent"] for c in loop["connected"]),
    }
    assert not loop_named & UNSEEDED_FLEET_AGENT_SLUGS
    for shared in ("ctem-risk-quantifier", "ctem-remediation-coordinator", "opencti-detection-rule-converter"):
        assert shared not in UNSEEDED_FLEET_AGENT_SLUGS
    seeded = {a["slug"] for a in seed._seeded_builtin_agents()}
    assert not seeded & exclusive
    assert exclusive <= seed._withheld_builtin_agent_slugs()
    # The declarations are untouched: the switch decides what is seeded.
    assert any(a["agent_slug"] == AI_ORCH for a in BUILTIN_ASSIGNMENTS)
    for agent in seed._seeded_builtin_agents():
        assert not set(agent.get("handover_agent_slugs") or []) & exclusive, agent["slug"]


def test_switching_a_use_case_back_on_seeds_it_exactly_as_its_definition():
    from app.agents.builtin_fleets import (
        CTEM_VULN_PRIORITIZATION_FLEET,
        seeded_fleets,
        unseeded_fleet_agent_slugs,
    )

    both = seeded_fleets({"ctem-loop", "ctem-vuln-prioritization"})
    assert [f["slug"] for f in fleet_seed.fleets_for_mode("xtm_one", both)] == ["ctem-loop", "ctem-vuln-prioritization"]
    assert both[1] is CTEM_VULN_PRIORITIZATION_FLEET
    withheld = unseeded_fleet_agent_slugs(both)
    assert "ctem-vuln-prioritization-orchestrator" not in withheld
    assert "ctem-vuln-triage-analyst" not in withheld
    assert unseeded_fleet_agent_slugs(list(BUILTIN_FLEETS)) == frozenset()


def test_a_fork_of_an_unseeded_fleet_names_the_ctem_loop_and_no_case_path_is_ownerless():
    from app.agents.builtin_fleets import RETIRED_FLEET_SUCCESSORS

    for slug in ("ctem-vuln-prioritization", "ctem-ai-security", "ctem-third-party-risk"):
        assert RETIRED_FLEET_SUCCESSORS[slug] == "ctem-loop"
    loop = next(f for f in SEEDED_BUILTIN_FLEETS if f["slug"] == "ctem-loop")
    # Every path, new_vulnerability included, falls back to the loop.
    assert loop["case_ownership"]["paths"] is None


def test_the_generic_loop_folds_the_posture_stage_analysts_under_its_posture_role():
    from app.agents.builtin_agents import CTEM_POSTURE_ORCHESTRATOR_SLUG
    from app.services.ctem_verticals import CTEM_STAGE_AGENT_SLUGS

    fleet = next(f for f in BUILTIN_FLEETS if f["slug"] == "ctem-loop")
    parents = {m["agent"]: m.get("parent") for m in fleet["members"]}
    assert parents[CTEM_POSTURE_ORCHESTRATOR_SLUG] == fleet["orchestrator"]
    for slug in [*CTEM_STAGE_AGENT_SLUGS.values(), "exposure-briefing-analyst"]:
        assert parents[slug] == CTEM_POSTURE_ORCHESTRATOR_SLUG, slug
    assert "opencti-dashboard-widget-creator" in {c["agent"] for c in fleet["connected"]}
    assert fleet["entry_handovers"] == [{"from": "ctem-assistant", "type": "delegate"}]


def test_the_generic_loop_keeps_every_agent_of_the_fleets_it_absorbs():
    """No agent of a retired fleet is left orphaned: each is a member of the loop."""
    from app.agents.openaev_detection_desk import DETECTION_DESK_FLEET
    from app.agents.opencrq_risk_budget_desk import RISK_BUDGET_DESK_FLEET
    from app.agents.opencti_pir_desk import PIR_DESK_FLEET

    loop = next(f for f in BUILTIN_FLEETS if f["slug"] == "ctem-loop")
    members = {m["agent"] for m in loop["members"]}
    for retired in (PIR_DESK_FLEET, DETECTION_DESK_FLEET, RISK_BUDGET_DESK_FLEET):
        assert {m["agent"] for m in retired["members"]} <= members, retired["slug"]
        assert retired["slug"] in loop["absorbs"]
        assert retired["orchestrator"] in loop["leads"]
    assert "ctem-posture" in loop["absorbs"] and loop["previous_slugs"] == ["ctem-threat-response"]
    # One home fleet per agent: no member of the loop is a member of another seeded fleet.
    for other in BUILTIN_FLEETS[1:]:
        assert not members & {m["agent"] for m in other["members"]}, other["slug"]


def test_every_retired_fleet_names_its_successor_and_every_lead_is_a_member():
    from app.agents.builtin_fleets import BUILTIN_FLEET_LEADS, RETIRED_FLEET_SUCCESSORS

    assert RETIRED_FLEET_SUCCESSORS == {
        "ctem-threat-response": "ctem-loop",
        "ctem-posture": "ctem-loop",
        "opencti-pir-desk": "ctem-loop",
        "openaev-detection-desk": "ctem-loop",
        "opencrq-risk-budget-desk": "ctem-loop",
        # Defined, not seeded (#2105): a fork of one names the loop.
        "ctem-vuln-prioritization": "ctem-loop",
        "ctem-ai-security": "ctem-loop",
        "ctem-third-party-risk": "ctem-loop",
    }
    assert set(BUILTIN_FLEET_LEADS) == SEEDED_BUILTIN_FLEET_SLUGS
    for fleet in SEEDED_BUILTIN_FLEETS:
        members = {m["agent"] for m in fleet["members"]}
        assert BUILTIN_FLEET_LEADS[fleet["slug"]] <= members, fleet["slug"]


def test_every_agent_a_builtin_fleet_names_is_a_builtin_agent():
    from app.agents.builtin_agents import BUILTIN_AGENTS
    from app.agents.ctem_ai_security import SHARED_AGENTS

    # F3 connects F1's shared agents before F1 seeds them; the seeder skips a
    # connected agent that is not seeded yet.
    seeded = {a["slug"] for a in BUILTIN_AGENTS} | set(SHARED_AGENTS)
    for fleet in BUILTIN_FLEETS:
        named = (
            [fleet["orchestrator"]]
            + [m["agent"] for m in fleet["members"]]
            + [c["agent"] for c in fleet.get("connected") or []]
            + [h["from"] for h in fleet.get("entry_handovers") or []]
        )
        assert set(named) <= seeded, set(named) - seeded


def test_f4_keeps_every_existing_stage_and_briefing_assignment_name():
    # A renamed seed is swept and born again: F4 renames nothing.
    from app.agents.builtin_agents import BUILTIN_ASSIGNMENTS

    names = {a["name"] for a in BUILTIN_ASSIGNMENTS}
    for stage in ("scoping", "discovery", "prioritization", "validation", "remediation"):
        assert f"Weekly {stage} assessment" in names
        assert f"Generate my {stage} assessment" in names
    assert "Generate my exposure briefing" in names


def _posture_assignment(name: str) -> dict:
    from app.agents.builtin_agents import BUILTIN_ASSIGNMENTS, CTEM_POSTURE_ORCHESTRATOR_SLUG

    found = [a for a in BUILTIN_ASSIGNMENTS if a["name"] == name]
    assert len(found) == 1, name
    assert found[0]["agent_slug"] == CTEM_POSTURE_ORCHESTRATOR_SLUG
    return found[0]


def test_f4_event_refresh_hears_closed_f1_f3_cases_and_is_a_valid_trigger():
    from app.services.integration_event_filters import payload_filter_reason
    from app.services.platform_events import trigger_config_error

    a = _posture_assignment("Event refresh")
    assert a["trigger_type"] == "platform_event"
    assert trigger_config_error(a["trigger_type"], a["trigger_config"]) is None
    assert a["trigger_config"]["event"] == "work_item.completed"
    assert a["trigger_config"]["scope"] == "all"
    flt = a["trigger_config"]["filter"]
    for key in ("ctem:f1:case:1", "ctem:f2:retest:x", "ctem:f3:approval:bot:jailbreak"):
        assert payload_filter_reason({"correlation_key": key}, flt) is None, key
    for key in ("ctem:f4:state", "dev:ctem", "mail:ctem:f1:"):
        assert payload_filter_reason({"correlation_key": key}, flt) is not None, key
    assert not a["run_as_all_users"]


def test_f4_event_refresh_debounces_per_stage_per_hour_in_its_state():
    from app.agents.builtin_agents import CTEM_POSTURE_REFRESH_DEBOUNCE_SECONDS, CTEM_POSTURE_STATE_KEY

    prompt = _posture_assignment("Event refresh")["prompt"]
    assert CTEM_POSTURE_REFRESH_DEBOUNCE_SECONDS == 3600
    assert CTEM_POSTURE_STATE_KEY in prompt
    assert "60 minutes" in prompt
    # The template placeholders survive the f-string; no stray braces.
    assert "{{trigger.correlation_key}}" in prompt
    assert "{{{" not in prompt


def test_f4_monthly_board_report_reads_f1_f3_outcomes_and_roi():
    a = _posture_assignment("Monthly board report")
    assert a["trigger_type"] == "schedule"
    # After the briefing (08:00) and the OpenCRQ Board pack (10:00), #1832.
    assert a["trigger_config"] == {"cron": "0 11 1 * *"}
    for key in ("ctem:f1:state", "ctem:f2:state", "ctem:f3:state"):
        assert key in a["prompt"]
    assert "ROI" in a["prompt"]
    # The cases are the record (#1832): counted with the case reads, the states hold counters only.
    assert "list_ctem_cases" in a["prompt"]
    assert "open cases, closed cases" not in a["prompt"]
    assert "loss avoided" in a["prompt"]


def test_f4_orchestrator_has_the_shared_state_tool_its_prompts_use():
    from app.agents.builtin_agents import BUILTIN_AGENTS, CTEM_POSTURE_ORCHESTRATOR_SLUG

    agent = next(a for a in BUILTIN_AGENTS if a["slug"] == CTEM_POSTURE_ORCHESTRATOR_SLUG)
    assert "shared_state" in agent["builtin_tool_ids"]


def test_builtin_fleets_are_unique_and_never_in_copilot():
    # F1-F4 add their entries, one slice each.
    slugs = [f["slug"] for f in BUILTIN_FLEETS]
    assert len(slugs) == len(set(slugs))
    assert fleet_seed.fleets_for_mode("copilot") == []
    assert [f["slug"] for f in fleet_seed.fleets_for_mode("xtm_one")] == [f["slug"] for f in SEEDED_BUILTIN_FLEETS]


def test_every_builtin_member_is_a_handover_target_of_its_parent():
    """No member floats on the fleet graph (#9757): its parent hands over to it."""
    from app.agents.builtin_agents import BUILTIN_AGENTS

    handovers = {a["slug"]: set(a.get("handover_agent_slugs") or []) for a in BUILTIN_AGENTS}
    floating = [
        (fleet["slug"], member["parent"], member["agent"])
        for fleet in BUILTIN_FLEETS
        for member in fleet["members"]
        if member.get("parent") and member["agent"] not in handovers.get(member["parent"], set())
    ]
    assert floating == []


def test_a_missing_parent_moves_its_reports_up():
    parents = {"orch": None, "lead": "orch", "hunt": "lead", "risk": "orch"}
    assert fleet_seed.resolve_parent("hunt", parents, {"orch", "hunt", "risk"}) == "orch"
    assert fleet_seed.resolve_parent("hunt", parents, {"hunt"}) is None
    assert fleet_seed.resolve_parent("hunt", parents, {"lead", "hunt"}) == "lead"


def test_a_parent_loop_ends_at_the_top_level():
    parents = {"a": "b", "b": "a"}
    assert fleet_seed.resolve_parent("a", parents, {"a"}) is None


def test_a_user_fleet_is_always_writable():
    ensure_builtin_fleet_writable(SimpleNamespace(is_builtin=False), None)
    ensure_builtin_fleet_writable(SimpleNamespace(is_builtin=False), {"name"})


def test_a_builtin_fleet_takes_only_its_switches():
    fleet = SimpleNamespace(is_builtin=True)
    ensure_builtin_fleet_writable(fleet, {"is_archived"})
    ensure_builtin_fleet_writable(fleet, set(BUILTIN_FLEET_WRITABLE_FIELDS))
    for fields in ({"name"}, {"is_archived", "orchestrator_agent_id"}, None):
        with pytest.raises(FleetActionRefusedError) as refused:
            ensure_builtin_fleet_writable(fleet, fields)
        assert refused.value.status_code == 403
