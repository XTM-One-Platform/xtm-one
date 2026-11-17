"""Seeded agents declare their ROI unit (CTEM seeded fleets S4 slice 4, issue #8990)."""

import json

import pytest
from jsonschema import Draft202012Validator

from app.agents.builtin_agents import BUILTIN_AGENTS
from app.agents.builtin_fleets import BUILTIN_FLEETS
from app.agents.builtin_roi_units import (
    BUILTIN_ROI_EXEMPT,
    BUILTIN_ROI_UNITS,
    EXEMPT_ORCHESTRATOR,
    EXEMPT_REASONS,
    with_builtin_roi_unit,
)
from app.agents.seed import _resolve_seed_schema
from app.services import roi
from app.services.structured_output import (
    build_anthropic_structured_output,
    build_openai_response_format,
    build_schema_prompt,
    strip_platform_keywords,
)


def _all_agents() -> dict[str, dict]:
    return {a["slug"]: a for a in BUILTIN_AGENTS}


def _seeded_schema(slug: str) -> dict:
    agent = _all_agents()[slug]
    return with_builtin_roi_unit(slug, _resolve_seed_schema(agent.get("output_schema")))


@pytest.mark.parametrize("slug", sorted(BUILTIN_ROI_UNITS))
def test_every_declaring_agent_is_a_json_built_in_with_a_known_unit(slug):
    agents = _all_agents()
    assert slug in agents, f"{slug} is not a built-in agent"
    assert agents[slug].get("output_format") == "json"
    schema = _seeded_schema(slug)
    assert roi.declaration_error(schema) is None
    assert roi.unit_declaration(schema) == BUILTIN_ROI_UNITS[slug]


@pytest.mark.parametrize("slug", sorted(BUILTIN_ROI_UNITS))
def test_the_count_path_is_a_property_of_the_output(slug):
    schema = _seeded_schema(slug)
    first = BUILTIN_ROI_UNITS[slug].count_path.split(".")[0]
    assert first in schema.get("properties", {}), f"{slug}: '{first}' is not in its output schema"


def test_the_declaration_never_changes_validation():
    schema = _seeded_schema("remediation-crowdstrike-rule-generator")
    Draft202012Validator.check_schema(schema)
    sample = {"rules": [{"name": "r"}, {"name": "s"}]}
    stripped = strip_platform_keywords(schema)

    def messages(s: dict) -> list[str]:
        return sorted(e.message for e in Draft202012Validator(s).iter_errors(sample))

    assert messages(schema) == messages(stripped)
    assert messages(schema)


def test_a_seeded_run_output_counts_its_units():
    decl = roi.unit_declaration(_seeded_schema("remediation-crowdstrike-rule-generator"))
    assert roi.count_units(decl, {"rules": [{}, {}, {}]}) == 3
    decl = roi.unit_declaration(_seeded_schema("cti-stix-harvester"))
    assert roi.count_units(decl, {"type": "bundle", "id": "bundle--1", "objects": []}) == 1
    decl = roi.unit_declaration(_seeded_schema("exposure-briefing-analyst"))
    assert roi.count_units(decl, {"headline": "  "}) == 0


def test_every_built_in_agent_declares_a_unit_or_says_why_not():
    """#9836 R2: a seeded agent is in exactly one of the two tables."""
    slugs = set(_all_agents())
    assert not set(BUILTIN_ROI_UNITS) & set(BUILTIN_ROI_EXEMPT)
    missing = sorted(slugs - set(BUILTIN_ROI_UNITS) - set(BUILTIN_ROI_EXEMPT))
    assert not missing, f"declare a unit in BUILTIN_ROI_UNITS or a reason in BUILTIN_ROI_EXEMPT for: {missing}"
    stale = sorted((set(BUILTIN_ROI_UNITS) | set(BUILTIN_ROI_EXEMPT)) - slugs)
    assert not stale, f"not built-in agents any more: {stale}"
    assert set(BUILTIN_ROI_EXEMPT.values()) <= set(EXEMPT_REASONS)


def test_every_built_in_assignment_runs_a_covered_agent():
    from app.agents.builtin_agents import BUILTIN_ASSIGNMENTS

    covered = set(BUILTIN_ROI_UNITS) | set(BUILTIN_ROI_EXEMPT)
    assert {a["agent_slug"] for a in BUILTIN_ASSIGNMENTS} <= covered


@pytest.mark.parametrize("fleet", BUILTIN_FLEETS, ids=lambda f: f["slug"])
def test_every_seeded_fleet_counts_a_unit_after_one_run(fleet):
    """A seeded fleet's members produce a unit, so its value is never 0 by construction."""
    agents = {m["agent"] for m in fleet.get("members", [])} | {c["agent"] for c in fleet.get("connected", [])}
    assert agents & set(BUILTIN_ROI_UNITS), f"{fleet['slug']}: no agent of the fleet declares a unit"


def test_an_exempt_orchestrator_never_counts_its_members_twice():
    """No double counting: an orchestrator listed exempt records nothing of its own."""
    orchestrators = {f["orchestrator"] for f in BUILTIN_FLEETS}
    for slug in orchestrators - set(BUILTIN_ROI_UNITS):
        assert BUILTIN_ROI_EXEMPT[slug] == EXEMPT_ORCHESTRATOR
        assert roi.unit_declaration(_seeded_schema(slug) or {}) is None


def test_an_agent_without_a_row_keeps_its_schema_object():
    schema = {"type": "object"}
    assert with_builtin_roi_unit("not-a-declaring-agent", schema) is schema
    assert with_builtin_roi_unit("cti-stix-harvester", None) is None


def test_the_declaration_is_a_copy():
    source = {"type": "object", "properties": {"rules": {"type": "array"}}}
    out = with_builtin_roi_unit("remediation-crowdstrike-rule-generator", source)
    assert roi.ROI_SCHEMA_KEYWORD in out
    assert roi.ROI_SCHEMA_KEYWORD not in source


def test_platform_keywords_never_reach_a_provider_or_a_prompt():
    schema = _seeded_schema("remediation-crowdstrike-rule-generator")
    assert roi.ROI_SCHEMA_KEYWORD in schema
    assert "x-xtm-" not in json.dumps(build_openai_response_format(strip_platform_keywords(schema)))
    assert "x-xtm-" not in json.dumps(build_anthropic_structured_output(strip_platform_keywords(schema)))
    assert "x-xtm-" not in build_schema_prompt(schema)


def test_strip_keeps_a_property_named_like_a_keyword():
    schema = {
        "type": "object",
        "x-xtm-roi": {"unit": "cve_assessed", "count": "x-xtm-count"},
        "properties": {"x-xtm-count": {"type": "integer", "x-xtm-note": "dropped"}},
    }
    out = strip_platform_keywords(schema)
    assert "x-xtm-roi" not in out
    assert out["properties"] == {"x-xtm-count": {"type": "integer"}}
    assert "x-xtm-roi" in schema  # input untouched
