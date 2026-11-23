"""CTEM fleet F1, Threat-informed exposure response (dev-docs/ctem-fleets.md s.4.1, #9114).

Since CTEM-31 (#1048) F1 is the core of the generic CTEM loop fleet (``ctem-loop``):
its orchestrator is the loop root, its roles are the loop's own roles, and the
posture and module-desk roles hang below it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.builtin_agents import BUILTIN_AGENTS, BUILTIN_ASSIGNMENTS, CTEM_FLEET_AGENTS, CTEM_LOOP_ROLE_LEADS
from app.agents.builtin_fleets import BUILTIN_FLEETS
from app.agents.builtin_roi_units import BUILTIN_ROI_UNITS
from app.services.opencti_trigger_provisioning import read_provision_spec

_SEED_DATA = Path(__file__).resolve().parents[1] / "app" / "agents" / "seed_data"
_ORCH = "ctem-threat-response-orchestrator"
_SHARED = ("ctem-threat-relevance-analyst", "ctem-risk-quantifier", "ctem-remediation-coordinator")
_CORE_ROLES = {
    "ctem-threat-relevance-analyst",
    "openaev-scenario-creator",
    "ctem-risk-quantifier",
    "ctem-remediation-coordinator",
    "opencti-threat-hunt-creator",
    "opencti-detection-rule-converter",
}


def _fleet() -> dict:
    (fleet,) = [f for f in BUILTIN_FLEETS if f["slug"] == "ctem-loop"]
    return fleet


def _agents() -> dict[str, dict]:
    return {a["slug"]: a for a in BUILTIN_AGENTS}


def _f1_assignments() -> dict[str, dict]:
    return {a["name"]: a for a in BUILTIN_ASSIGNMENTS if a["agent_slug"] == _ORCH}


def test_every_member_and_connected_agent_is_a_builtin():
    fleet, agents = _fleet(), _agents()
    named = [m["agent"] for m in fleet["members"]] + [c["agent"] for c in fleet["connected"]]
    named += [h["from"] for h in fleet["entry_handovers"]]
    assert [slug for slug in named if slug not in agents] == []


def test_the_orchestrator_is_the_single_top_level_member():
    fleet = _fleet()
    assert fleet["orchestrator"] == _ORCH
    top = [m["agent"] for m in fleet["members"] if not m.get("parent")]
    assert top == [_ORCH]
    members = {m["agent"] for m in fleet["members"]}
    assert all(m["parent"] in members for m in fleet["members"] if m.get("parent"))


def test_the_spec_members_are_seeded_with_their_roles():
    roles = {m["agent"]: m["role_label"] for m in _fleet()["members"] if m["agent"] in {_ORCH, *_CORE_ROLES}}
    assert roles == {
        _ORCH: "Orchestrator",
        "ctem-threat-relevance-analyst": "Relevance",
        "openaev-scenario-creator": "Validation",
        "ctem-risk-quantifier": "Risk",
        "ctem-remediation-coordinator": "Mobilization",
        "opencti-threat-hunt-creator": "Hunt",
        "opencti-detection-rule-converter": "Detections",
    }


def test_the_orchestrator_delegates_to_its_direct_reports():
    orch = _agents()[_ORCH]
    direct = {m["agent"] for m in _fleet()["members"] if m.get("parent") == _ORCH}
    assert set(orch["handover_agent_slugs"]) == direct
    assert set(CTEM_LOOP_ROLE_LEADS) <= direct
    assert set(orch["handover_type_slugs"].values()) == {"delegate"}
    # The blackboard and the approval queue of every environment write.
    assert {"shared_state", "work_items"} <= set(orch["builtin_tool_ids"])


@pytest.mark.parametrize("slug", [_ORCH, *_SHARED])
def test_each_new_agent_has_a_persona_file(slug):
    persona = (_SEED_DATA / "agent_persona" / f"{slug}.md").read_text(encoding="utf-8")
    assert "get_organization_context" in persona


@pytest.mark.parametrize("slug", _SHARED)
def test_the_shared_role_agents_declare_their_roi_unit_and_schema(slug):
    agent = _agents()[slug]
    assert agent["output_format"] == "json"
    assert (_SEED_DATA / "agent_output_schema" / f"{agent['output_schema']}.json").is_file()
    assert slug in BUILTIN_ROI_UNITS


def test_the_new_agents_are_seeded_once():
    slugs = [a["slug"] for a in BUILTIN_AGENTS]
    assert all(slugs.count(a["slug"]) == 1 for a in CTEM_FLEET_AGENTS)


def test_the_spec_assignments_live_on_the_orchestrator() -> None:
    assignments = _f1_assignments()
    # The three of the spec, plus the case loop (#891) and the PIR score trigger (CTEM-08).
    assert set(assignments) == {
        "New relevant threat",
        "Threat activity flagged in a PIR",
        "Weekly threat sweep",
        "Re-test due",
        "Case opened or reopened",
        "Gate answered",
        "Loop reminder due",
    }
    assert assignments["Weekly threat sweep"]["trigger_config"] == {"cron": "0 7 * * 1"}
    assert assignments["Re-test due"]["trigger_type"] == "work_item"
    assert assignments["Re-test due"]["trigger_config"] == {"correlation_prefix": "ctem:f1:retest:"}


def test_the_new_threat_trigger_is_provisioned_from_the_organization_profile():
    trigger = _f1_assignments()["New relevant threat"]
    assert trigger["trigger_type"] == "integration_event"
    spec = read_provision_spec(trigger["trigger_config"])
    assert spec is not None
    assert set(spec["entity_types"]) == {"Intrusion-Set", "Campaign", "Report"}
    assert spec["event_types"] == ["create"]
    assert set(spec["organization_context"]) == {"sector", "regions"}


def test_the_pir_threat_trigger_is_a_pir_playbook_held_to_the_policy_threshold() -> None:
    trigger = _f1_assignments()["Threat activity flagged in a PIR"]
    assert trigger["trigger_config"]["pir_min_score"] == "policy"
    spec = read_provision_spec(trigger["trigger_config"])
    assert spec is not None
    assert spec["mode"] == "pir_playbook"
    assert spec["organization_context"] == ["pir_ids"]
    assert "Vulnerability" not in spec["entity_types"]
