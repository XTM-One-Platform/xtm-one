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

    fleet = next(f for f in BUILTIN_FLEETS if f["slug"] == slug)
    names = [a["name"] for a in BUILTIN_ASSIGNMENTS if a.get("agent_slug") == fleet["orchestrator"]]
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


def test_an_entry_keeps_its_own_documentation_and_an_unknown_slug_gets_none():
    own = {"purpose": "Mine"}
    assert builtin_fleet_documentation({"slug": "ctem-posture", "documentation": own}) is own
    assert builtin_fleet_documentation({"slug": "ctem-posture"}) is BUILTIN_FLEET_DOCS["ctem-posture"]
    assert builtin_fleet_documentation({"slug": "lab-fleet"}) is None
