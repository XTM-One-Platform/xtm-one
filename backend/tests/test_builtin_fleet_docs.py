"""Seeded documentation of the built-in fleets (#9174 slice 6a)."""

from __future__ import annotations

import re

import pytest

from app.agents.builtin_fleet_docs import BUILTIN_FLEET_DOCS, builtin_fleet_documentation
from app.agents.builtin_fleets import BUILTIN_FLEETS
from app.services.fleet_docs import DOC_SECTIONS, RETIRED_SECTIONS, SECTION_MAX_CHARS, clean_sections

_SLUGS = [f["slug"] for f in BUILTIN_FLEETS]


def test_every_built_in_fleet_ships_documentation_and_nothing_else_does():
    assert set(BUILTIN_FLEET_DOCS) == set(_SLUGS)


@pytest.mark.parametrize("slug", _SLUGS)
def test_each_documentation_fills_every_section_and_passes_the_save_rules(slug):
    doc = BUILTIN_FLEET_DOCS[slug]
    assert list(doc) == list(DOC_SECTIONS)
    assert clean_sections(doc) == {k: v.strip() for k, v in doc.items()}
    assert all(v.strip() and len(v) <= SECTION_MAX_CHARS for v in doc.values())


@pytest.mark.parametrize("slug", _SLUGS)
def test_no_retired_prose_section_is_seeded(slug):
    """#10123: members, approvals, outcomes, units, quota and switch-off live in other tabs."""
    assert not set(BUILTIN_FLEET_DOCS[slug]) & set(RETIRED_SECTIONS)


def test_a_stored_version_with_a_retired_section_is_read_without_it():
    """An old version or a fork's copy still saves: the retired sections are dropped, never refused."""
    assert clean_sections({"purpose": "P", "members": "- **A**: b", "switch_off": "Pause it."}) == {"purpose": "P"}


@pytest.mark.parametrize("slug", _SLUGS)
def test_the_documentation_names_each_seeded_automation_of_its_fleet(slug):
    from app.agents.builtin_agents import BUILTIN_ASSIGNMENTS

    # The orchestrator's automations and every lead's (CTEM-31: a role folded into the
    # generic loop keeps its automations, so the About page names them all). Read from
    # the definition: a fleet defined but not seeded (#2105) keeps its documentation.
    fleet = next(f for f in BUILTIN_FLEETS if f["slug"] == slug)
    leads = {fleet["orchestrator"], *(fleet.get("leads") or [])}
    names = [a["name"] for a in BUILTIN_ASSIGNMENTS if a.get("agent_slug") in leads]
    assert names, slug
    triggers = BUILTIN_FLEET_DOCS[slug]["triggers"]
    for name in names:
        assert f"**{name}**" in triggers, (slug, name)


@pytest.mark.parametrize("slug", _SLUGS)
def test_the_documentation_carries_no_bracketed_placeholder(slug):
    for text in BUILTIN_FLEET_DOCS[slug].values():
        assert not re.search(r"<[A-Za-z0-9][^<>]*>", text)


def test_every_built_in_fleet_ships_structured_parts_that_pass_the_save_rules():
    from app.agents.builtin_fleet_docs import BUILTIN_FLEET_STRUCTURED
    from app.services.fleet_docs import clean_structured

    assert set(BUILTIN_FLEET_STRUCTURED) == set(_SLUGS)
    for slug, parts in BUILTIN_FLEET_STRUCTURED.items():
        cleaned = clean_structured(parts)
        assert cleaned and cleaned["purpose_line"] and cleaned["triggers"] and cleaned["steps"], slug
        assert cleaned["prerequisites"] and cleaned["quota"], slug


@pytest.mark.parametrize("slug", _SLUGS)
def test_the_structured_triggers_and_quota_match_the_markdown_triggers(slug):
    from app.agents.builtin_fleet_docs import BUILTIN_FLEET_STRUCTURED

    parts = BUILTIN_FLEET_STRUCTURED[slug]
    markdown = BUILTIN_FLEET_DOCS[slug]["triggers"]
    names = [t["name"] for t in parts["triggers"]]
    for name in names:
        assert f"**{name}**" in markdown, (slug, name)
    assert {q["trigger"] for q in parts["quota"]} == set(names), slug


@pytest.mark.parametrize("slug", _SLUGS)
def test_a_step_marked_gate_has_an_approval_gate_listed(slug):
    from app.agents.builtin_fleet_docs import BUILTIN_FLEET_STRUCTURED

    parts = BUILTIN_FLEET_STRUCTURED[slug]
    if any(s.get("gate") for s in parts["steps"]):
        assert parts.get("gates"), slug


@pytest.mark.parametrize("slug", _SLUGS)
def test_the_steps_flagged_at_low_are_the_steps_whose_gate_waits(slug: str) -> None:
    """#1074: the About page badges a step exactly when the gate it names waits.

    The static flags are Low's (the level the shipped structure was written for);
    the About response derives the live badges from the fleet's level, Full
    human gates out of the box since #2279.
    """
    from app.agents.builtin_fleet_docs import BUILTIN_FLEET_STRUCTURED
    from app.services.fleet_autonomy import gate_catalog, with_live_step_gates

    parts = BUILTIN_FLEET_STRUCTURED[slug]
    catalog = gate_catalog("low", parts.get("gates"))
    keys = {g["key"] for g in catalog}
    for step in parts["steps"]:
        # A step that carries a badge names the gate behind it, and that gate is in the catalog.
        if step.get("gate") or step.get("gate_key"):
            assert step.get("gate_key") in keys, (slug, step["title"])
    derived = with_live_step_gates(parts, catalog)
    static = [bool(s.get("gate")) for s in parts["steps"]]
    live = [bool(s.get("gate")) for s in derived["steps"]]
    assert static == live, slug
    # One step per waiting gate the steps name: no waiting gate is left without its badge.
    waiting = {g["key"] for g in catalog if g["gated"]}
    named = {s.get("gate_key") for s in parts["steps"] if s.get("gate_key")}
    assert {s["gate_key"] for s in derived["steps"] if s.get("gate")} == waiting & named, slug


def test_a_step_badge_follows_the_level_and_the_pins_of_the_fleet():
    """#1074: a level or a pin that moves a gate moves the badge of the step that names it."""
    from app.agents.builtin_fleet_docs import BUILTIN_FLEET_STRUCTURED
    from app.services.fleet_autonomy import PIN_RUN, gate_catalog, with_live_step_gates

    parts = BUILTIN_FLEET_STRUCTURED["ctem-vuln-prioritization"]

    def badged(level: str, pins: dict[str, str] | None = None) -> list[str]:
        gates = [dict(g, pin=(pins or {}).get(g["key"])) for g in parts["gates"]]
        derived = with_live_step_gates(parts, gate_catalog(level, gates))
        return [s["title"] for s in derived["steps"] if s.get("gate")]

    # #1339: the OpenAEV validation waits out of the box with Mobilize.
    assert badged("low") == ["Exploitability", "Ticket"]
    assert badged("medium") == ["Exploitability", "Ticket"]
    assert badged("autonomous") == []
    assert badged("low", {"ctem.validation.non_readonly": PIN_RUN}) == ["Ticket"]
    assert badged("medium", {"ctem.mobilize": PIN_RUN}) == ["Exploitability"]
    # The stored structure is never written by the derivation.
    assert [s["title"] for s in parts["steps"] if s.get("gate")] == ["Exploitability", "Ticket"]


def test_a_step_without_a_known_gate_key_keeps_its_stored_flag():
    from app.services.fleet_autonomy import gate_catalog, with_live_step_gates

    structured = {
        "steps": [
            {"title": "A", "gate": True},
            {"title": "B", "gate": True, "gate_key": "not.in.catalog"},
            {"title": "C", "gate": True, "gate_key": "k"},
        ]
    }
    catalog = gate_catalog("autonomous", [{"key": "k", "impact": "destructive"}])
    assert [s["gate"] for s in with_live_step_gates(structured, catalog)["steps"]] == [True, True, False]
    assert with_live_step_gates(None, catalog) is None


_PROFILE_ROUTES = {
    "profile:sector": "/settings/organization?card=identity",
    "profile:regions": "/settings/organization?card=identity",
    "profile:primary_assets": "/settings/organization/profile/business?card=primary_assets",
    "profile:tracked_organizations": "/settings/organization/profile/business?card=tracked_organizations",
    "profile:software": "/settings/organization/profile/technology?card=software",
}


def test_a_profile_prerequisite_opens_the_tab_and_card_of_its_field_never_the_bare_profile():
    from app.agents.builtin_fleet_docs import BUILTIN_FLEET_STRUCTURED, profile_fix_route
    from app.services.fleet_docs import clean_structured

    seen = set()
    for slug, parts in BUILTIN_FLEET_STRUCTURED.items():
        for item in parts["prerequisites"]:
            if not item["check"].startswith("profile:"):
                continue
            seen.add(item["check"])
            if item["check"] == "profile:ai_targets":
                # #1844: AI targets are assets, fixed on the CTEM Inventory, never a profile card.
                assert item["fix_route"] == "/ctem/inventory", (slug, item)
                continue
            assert item["fix_route"] == _PROFILE_ROUTES[item["check"]], (slug, item)
            assert item["fix_route"] not in ("/settings/organization", "/settings/organization/profile"), slug
        # The routes pass the save rules (fix_route pattern).
        assert clean_structured(parts)["prerequisites"], slug
    assert "profile:primary_assets" in seen and "profile:ai_targets" in seen
    for check, route in _PROFILE_ROUTES.items():
        assert profile_fix_route(check.split(":", 1)[1]) == route


def test_an_entry_keeps_its_own_documentation_and_an_unknown_slug_gets_none():
    own = {"purpose": "Mine"}
    assert builtin_fleet_documentation({"slug": "ctem-loop", "documentation": own}) is own
    assert builtin_fleet_documentation({"slug": "ctem-loop"}) is BUILTIN_FLEET_DOCS["ctem-loop"]
    # A retired fleet (CTEM-31) gets no documentation of its own any more.
    assert builtin_fleet_documentation({"slug": "ctem-posture"}) is None
    assert builtin_fleet_documentation({"slug": "lab-fleet"}) is None
