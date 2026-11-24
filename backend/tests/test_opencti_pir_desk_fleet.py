"""Single-product seeded fleet M1: the OpenCTI PIR intelligence desk (#10542, epic #8975).

Since CTEM-31 (#1048) the desk is the Intelligence role of the generic CTEM loop
fleet: its agents and automations are unchanged, its fleet row is retired.
"""

from __future__ import annotations

from app.agents import opencti_pir_desk as m1
from app.agents.builtin_agents import ADMIN_OPT_IN_BUILTIN_ASSIGNMENTS, BUILTIN_AGENTS, BUILTIN_ASSIGNMENTS
from app.agents.builtin_fleet_docs import BUILTIN_FLEET_STRUCTURED, OPENCTI_PIR_DESK_DOC
from app.agents.builtin_fleets import BUILTIN_FLEETS, CTEM_LOOP_SLUG, RETIRED_FLEET_SUCCESSORS
from app.agents.builtin_roi_units import BUILTIN_ROI_EXEMPT, BUILTIN_ROI_UNITS, EXEMPT_ORCHESTRATOR
from app.agents.fleet_opt_in import ENABLED_BY_DEFAULT_ALLOWLIST
from app.services.opencti_trigger_provisioning import read_provision_spec

_NEW = {m1.ORCHESTRATOR, m1.TRIAGE_ANALYST, m1.LANDSCAPE_REPORTER}


def _agents() -> dict[str, dict]:
    return {a["slug"]: a for a in BUILTIN_AGENTS}


def _assignments() -> list[dict]:
    return [a for a in BUILTIN_ASSIGNMENTS if a["agent_slug"] == m1.ORCHESTRATOR]


def test_the_desk_is_a_role_of_the_generic_loop_seeded_in_xtm_one_and_dev_never_in_copilot():
    from app.agents import fleet_seed

    assert m1.PIR_DESK_FLEET not in BUILTIN_FLEETS
    assert RETIRED_FLEET_SUCCESSORS[m1.FLEET_SLUG] == CTEM_LOOP_SLUG
    loop = next(f for f in fleet_seed.fleets_for_mode("xtm_one") if f["slug"] == CTEM_LOOP_SLUG)
    roles = {m["agent"]: m for m in loop["members"]}
    assert set(roles) >= _NEW
    role = roles[m1.ORCHESTRATOR]["role_label"]
    assert role == "Intelligence desk"
    assert fleet_seed.fleets_for_mode("copilot") == []
    for slug in _NEW:
        assert _agents()[slug]["platform_targets"] == ["xtm_one", "dev"]


def test_three_new_agents_and_the_existing_opencti_agents_connected_never_moved():
    fleet = m1.PIR_DESK_FLEET
    assert fleet["orchestrator"] == m1.ORCHESTRATOR
    assert {m["agent"] for m in fleet["members"]} == _NEW
    assert [c["agent"] for c in fleet["connected"]] == list(m1.SHARED_AGENTS)
    assert not ({c["agent"] for c in fleet["connected"]} & _NEW)
    assert "entry_handovers" not in fleet
    for slug in m1.SHARED_AGENTS:
        assert slug in _agents(), slug


def test_no_existing_agent_is_changed():
    """The connected OpenCTI agents keep their own seed entry: none is redeclared here."""
    slugs = [a["slug"] for a in BUILTIN_AGENTS]
    for slug in m1.SHARED_AGENTS:
        assert slugs.count(slug) == 1, slug
    assert not [a for a in m1.PIR_DESK_AGENTS if a["slug"] in m1.SHARED_AGENTS]


def test_every_assignment_is_admin_opt_in_and_off():
    seeded = _assignments()
    assert [a["name"] for a in seeded] == [a["name"] for a in m1.PIR_DESK_ASSIGNMENTS]
    for a in seeded:
        assert a["enabled"] is False and a["admin_opt_in"] is True, a["name"]
        assert (m1.ORCHESTRATOR, a["name"]) in ADMIN_OPT_IN_BUILTIN_ASSIGNMENTS
        assert (m1.ORCHESTRATOR, a["name"]) not in ENABLED_BY_DEFAULT_ALLOWLIST


def test_the_trigger_listens_to_pir_matches_on_four_entity_types():
    event = next(a for a in m1.PIR_DESK_ASSIGNMENTS if a["trigger_type"] == "integration_event")
    spec = read_provision_spec(event["trigger_config"])
    assert spec is not None
    assert sorted(spec["entity_types"]) == ["Campaign", "Intrusion-Set", "Report", "Vulnerability"]
    assert spec["event_types"] == ["create"]
    assert spec["organization_context"] == ["pir_ids"]
    assert spec["fleet"] == m1.FLEET_NAME


def test_roi_on_seeded_units_and_the_orchestrator_counts_nothing_twice():
    assert BUILTIN_ROI_UNITS[m1.TRIAGE_ANALYST].unit == "threat_report_triaged"
    assert BUILTIN_ROI_UNITS[m1.LANDSCAPE_REPORTER].unit == "briefing_written"
    assert BUILTIN_ROI_EXEMPT[m1.ORCHESTRATOR] == EXEMPT_ORCHESTRATOR
    # Hunt packages and stale indicators are counted by the existing agents they reach.
    assert BUILTIN_ROI_UNITS["indicator-decay-judge"].unit == "indicator_decay_reviewed"


def test_the_seeded_documentation_names_every_automation_and_the_revocation_gate():
    triggers = OPENCTI_PIR_DESK_DOC["triggers"]
    for a in m1.PIR_DESK_ASSIGNMENTS:
        assert f"**{a['name']}**" in triggers
    # The revocation is folded into the loop's one waiting gate, ctem.mobilize (CTEM-31).
    loop = BUILTIN_FLEET_STRUCTURED[CTEM_LOOP_SLUG]
    mobilize = next(g for g in loop["gates"] if g["key"] == "ctem.mobilize")
    assert "revoke a stale indicator" in mobilize["action"] and mobilize["impact"] == "destructive"
    assert {p["check"] for p in loop["prerequisites"]} >= {"integration:opencti", "manual"}


def test_no_hard_dependency_on_new_pir_criteria_types():
    from pathlib import Path

    personas = Path(m1.__file__).resolve().parent / "seed_data" / "agent_persona"
    for slug in _NEW:
        text = (personas / f"{slug}.md").read_text(encoding="utf-8")
        assert text.strip(), slug
    triage = (personas / f"{m1.TRIAGE_ANALYST}.md").read_text(encoding="utf-8")
    assert "never require" in triage
