"""Contract tests for the FIMI skills and the FIMI Infrastructure Investigator.

The FIMI Analyst's procedures are skills (``app/agents/fimi_skills.py``, bodies
in ``seed_data/skill_content/fimi-*.md``). They are only useful if the model can
load them and if every tool and skill they cite exists: a misspelled tool name
reads as an instruction the agent cannot follow. The investigator is a
read-only consulted specialist, so its deny list must cover every OpenCTI tool
that is not a read, including one added later.
"""

from __future__ import annotations

import re

import pytest

from app.agents.builtin_agents import BUILTIN_AGENTS, BUILTIN_KNOWLEDGE_BASES, BUILTIN_SKILLS
from app.agents.fimi_skills import FIMI_SKILLS
from app.agents.seed import SEED_DATA_DIR, _resolve_seed_content, seed_matches_platform

_ANALYST = "fimi-analyst"
_INVESTIGATOR = "fimi-infrastructure-investigator"

# A backticked snake_case name led by a verb: how a skill cites a tool (the
# same reading as ``tests/test_cti_pack.py``).
_TOOL_CITATION = re.compile(
    r"`((?:get|list|search|lookup|query|check|create|add|update|delete|manage|run|"
    r"submit|scan|count|find|enrich|analyse|analyze|download|export|import|map|assess|"
    r"promote|merge|upload|use|remove|bulk|identify|resolve|reverse|triage|publish|"
    r"describe|rescan|validate|request|parse|contain|lift|isolate|compute|compare|"
    r"consult|load)_[a-z0-9_]+)`"
)

#: OpenCTI tools that only read: everything else is a write the investigator
#: must not reach.
_READ_PREFIXES = ("get_", "list_", "search_", "query_", "download_", "validate_")


def _agent(slug: str) -> dict:
    return next(a for a in BUILTIN_AGENTS if a.get("slug") == slug)


def _body(skill: dict) -> str:
    return (SEED_DATA_DIR / "skill_content" / f"{skill['content']}.md").read_text(encoding="utf-8")


def _opencti_tool_names() -> set[str]:
    from app.api.v1.integration_catalog import build_dummy_token
    from app.services.integration_tools import PROVIDER_TOOLS
    from app.tools.base import CallableToolExecutor

    defs = PROVIDER_TOOLS["opencti"](build_dummy_token("opencti"), CallableToolExecutor(), refresh_fn=lambda: None)
    return {d.name for d in defs if d.name}


def _known_tool_names() -> set[str]:
    from app.services.builtin_tool_aliases import BUILTIN_GROUP_ALIASES
    from app.services.builtin_tools import build_fimi_analytics_composite
    from tests.test_cti_pack import _registered_integration_tool_names

    fimi_defs, _ = build_fimi_analytics_composite()
    # The builder and fleet functions every default assistant carries (#12343).
    platform = {name for alias in ("agent_builder", "fleets") for name in BUILTIN_GROUP_ALIASES[alias][1]}
    return (
        _registered_integration_tool_names()
        | {d.name for d in fimi_defs}
        | platform
        | {"web_fetch", "search_knowledge", "consult_agent", "load_skill", "load_tools"}
    )


@pytest.mark.parametrize("skill", FIMI_SKILLS, ids=lambda s: s["slug"])
def test_every_fimi_skill_is_seeded_loadable_and_bound(skill):
    assert skill in BUILTIN_SKILLS
    assert _body(skill).startswith("# ")
    # The catalog line the model chooses a skill from (cut at 1024 chars).
    assert 0 < len(skill["description"]) <= 1024
    assert seed_matches_platform(skill, "xtm_one")
    assert not seed_matches_platform(skill, "copilot")
    assert _ANALYST in skill["bind_to_agent_slugs"]


def test_the_investigator_carries_only_the_pivot_skills():
    bound = {s["slug"] for s in FIMI_SKILLS if _INVESTIGATOR in s["bind_to_agent_slugs"]}
    assert bound == {"fimi-infrastructure-pivoting", "fimi-coordination-indicators"}


def test_every_tool_a_fimi_skill_or_persona_cites_is_registered():
    known = _known_tool_names()
    texts = {f"skill {s['slug']}": _body(s) for s in FIMI_SKILLS}
    for slug in (_ANALYST, _INVESTIGATOR):
        texts[f"persona {slug}"] = _resolve_seed_content("agent_persona", slug) or ""
    unknown = sorted(
        {f"{where}: {name}" for where, text in texts.items() for name in _TOOL_CITATION.findall(text) if name not in known}
    )
    assert unknown == []


def test_every_workset_tool_a_fimi_skill_cites_exists():
    """Workset tool names start with no verb, so the citation pattern above skips them."""
    from app.services.builtin_tools import build_workset_tools

    workset_tools = {d.name for d in build_workset_tools(None, "u", None)[0]}
    cited = {name for s in FIMI_SKILLS for name in re.findall(r"`(workset_[a-z_]+)`", _body(s))}
    assert cited, "the corpus review skill cites the workset tools"
    assert sorted(cited - workset_tools) == []


class TestFimiCorpus:
    def test_the_analyst_holds_a_workset_of_its_own_per_conversation(self):
        analyst = _agent(_ANALYST)
        assert "workset" in analyst["builtin_tool_ids"]
        assert analyst["config"] == {"workset_chat_scope": "conversation"}

    def test_the_corpus_review_skill_is_bound_to_the_analyst_only(self):
        skill = next(s for s in FIMI_SKILLS if s["slug"] == "fimi-corpus-review")
        assert skill["bind_to_agent_slugs"] == [_ANALYST]
        body = _body(skill)
        for step in ("to_workset", "workset_declare_total", "workset_seal", "workset_read_item", "date_order"):
            assert step in body, step

    def test_the_investigator_holds_no_workset(self):
        assert "workset" not in _agent(_INVESTIGATOR)["builtin_tool_ids"]


def test_every_fimi_skill_or_agent_a_skill_names_exists():
    """A skill sends the agent to another one by its bold display name."""
    names = {s["name"] for s in FIMI_SKILLS} | {_agent(_INVESTIGATOR)["name"]}
    unknown = sorted(
        {
            f"{s['slug']}: {name}"
            for s in FIMI_SKILLS
            for name in re.findall(r"\*\*(FIMI [^*\n]{3,60})\*\*", _body(s))
            if name not in names
        }
    )
    assert unknown == []


class TestFimiInfrastructureInvestigator:
    def test_is_consulted_by_the_analyst(self):
        analyst = _agent(_ANALYST)
        assert _INVESTIGATOR in analyst["handover_agent_slugs"]
        assert analyst["handover_type_slugs"][_INVESTIGATOR] == "consult"

    def test_denies_every_opencti_tool_that_is_not_a_read(self):
        investigator = _agent(_INVESTIGATOR)
        assert investigator["integration_template_bindings"] == ["opencti"]
        writes = {name for name in _opencti_tool_names() if not name.startswith(_READ_PREFIXES)}
        assert writes, "the OpenCTI provider exposes write tools"
        assert sorted(writes - set(investigator["tool_deny_list"])) == []

    def test_denies_no_read(self):
        """A deny-listed read would blind the investigator."""
        denied = set(_agent(_INVESTIGATOR)["tool_deny_list"])
        assert sorted(n for n in denied if n.startswith(_READ_PREFIXES)) == []

    def test_all_memory_tiers_disabled(self):
        investigator = _agent(_INVESTIGATOR)
        for field in (
            "memory_short_term_enabled",
            "memory_medium_term_enabled",
            "memory_persistent_enabled",
            "memory_user_enabled",
        ):
            assert investigator.get(field) is False, f"{_INVESTIGATOR} must disable {field}"

    def test_reads_both_viginum_knowledge_bases(self):
        bound = {kb["slug"] for kb in BUILTIN_KNOWLEDGE_BASES if _INVESTIGATOR in kb.get("bind_to_agent_slugs", [])}
        assert bound == {"viginum-fimi-doctrine", "viginum-fimi-publications"}

    def test_persona_resolves_and_forbids_writes(self):
        persona = _resolve_seed_content("agent_persona", _INVESTIGATOR) or ""
        assert "You never write to OpenCTI" in persona
        assert "You never attribute" in persona
