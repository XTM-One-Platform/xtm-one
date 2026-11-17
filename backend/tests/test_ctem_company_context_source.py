"""The CTEM analysts read the organization context of Settings, never the Company Context knowledge base (#11464)."""

from __future__ import annotations

from app.agents.builtin_agents import _STAGE_ASSIGNMENTS, _STAGE_DEFS, _stage_persona, _stage_prompt

_CTEM_SLUGS = {
    "ctem-assistant",
    "exposure-briefing-analyst",
    "ctem-scoping-analyst",
    "ctem-discovery-analyst",
    "ctem-prioritization-analyst",
    "ctem-validation-analyst",
    "ctem-mobilization-analyst",
}


def _company_context_kb() -> dict:
    from app.agents.builtin_agents import BUILTIN_KNOWLEDGE_BASES

    return next(kb for kb in BUILTIN_KNOWLEDGE_BASES if kb["slug"] == "company-context")


def test_stage_prompts_read_the_organization_context_tool():
    for d in _STAGE_DEFS:
        text = _stage_prompt(d) + _stage_persona(d) + " ".join(str(v) for v in d.values())
        assert "get_organization_context" in _stage_prompt(d), d["key"]
        assert "search_knowledge" not in text, d["key"]
        assert "Company Context" not in text, d["key"]


def test_stage_assignments_carry_the_new_prompt():
    for item in _STAGE_ASSIGNMENTS:
        assert "search_knowledge" not in str(item.get("prompt", ""))


def test_company_context_kb_is_not_bound_to_ctem_agents():
    bound = set(_company_context_kb()["bind_to_agent_slugs"])
    assert not bound & _CTEM_SLUGS
