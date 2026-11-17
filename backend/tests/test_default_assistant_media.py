"""The default assistants make reports, decks and videos out of the box.

``DEFAULT_ASSISTANT_SLUGS`` names the assistants a person reaches without
choosing an agent: the CTEM Assistant (the General Assistant in XTM One mode),
the XTM One assistant, and the assistant ``global.assistant`` resolves to on
each vertical, which is what the embedded chat of OpenCTI, OpenAEV and OpenCRQ
talks to. Each carries the media tools and the skills that teach them; the
General Assistant carries the same skills until its admin chooses.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from app.agents.builtin_agents import (
    BUILTIN_AGENTS,
    BUILTIN_SKILLS,
    DEFAULT_ASSISTANT_MEDIA_TOOL_IDS,
    DEFAULT_ASSISTANT_SLUGS,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPORT_SKILL_PATH = BACKEND_ROOT / "app" / "agents" / "seed_data" / "skill_content" / "report-design.md"
_MIGRATION_PATH = BACKEND_ROOT / "alembic" / "versions" / "r5931gareport_add_generate_report_to_ga_tools.py"

#: The craft skills of the media tools: decks, videos, reports.
MEDIA_SKILL_SLUGS = ("presentation-design", "video-production", "report-design")


def _agent(slug: str) -> dict:
    return next(agent for agent in BUILTIN_AGENTS if agent["slug"] == slug)


def _skill(slug: str) -> dict:
    return next(skill for skill in BUILTIN_SKILLS if skill["slug"] == slug)


@pytest.mark.parametrize("slug", DEFAULT_ASSISTANT_SLUGS)
def test_every_default_assistant_carries_the_media_tools(slug):
    tools = _agent(slug)["builtin_tool_ids"]
    missing = [tool for tool in DEFAULT_ASSISTANT_MEDIA_TOOL_IDS if tool not in tools]
    assert not missing, f"{slug} lacks {missing!r}"
    assert len(tools) == len(set(tools)), f"{slug} lists a tool twice"


def test_the_media_tools_are_the_ones_the_general_assistant_may_carry():
    from app.models.general_assistant_config import GENERAL_ASSISTANT_BUILTIN_TOOL_IDS

    assert set(DEFAULT_ASSISTANT_MEDIA_TOOL_IDS) <= set(GENERAL_ASSISTANT_BUILTIN_TOOL_IDS)


def test_every_assistant_a_vertical_routes_to_is_a_default_assistant():
    """An agent bound to ``global.assistant`` on a vertical answers that product's embedded chat."""
    vertical_assistants = {
        agent["slug"]
        for agent in BUILTIN_AGENTS
        if isinstance((agent.get("supported_intents") or {}).get("global.assistant"), dict)
    }
    assert vertical_assistants, "the premise: the product assistants are bound per vertical"
    assert vertical_assistants <= set(DEFAULT_ASSISTANT_SLUGS)


@pytest.mark.parametrize("skill_slug", MEDIA_SKILL_SLUGS)
def test_every_default_assistant_and_the_general_assistant_carry_the_media_skills(skill_slug):
    from app.agents.seed import _resolve_seed_content, seed_matches_platform
    from app.services.builtin_tools.skills import MAX_CATALOG_DESCRIPTION_CHARS
    from app.services.general_assistant_skills import GENERAL_ASSISTANT_SKILL_SLUGS

    skill = _skill(skill_slug)
    assert set(DEFAULT_ASSISTANT_SLUGS) <= set(skill["bind_to_agent_slugs"])
    assert skill_slug in GENERAL_ASSISTANT_SKILL_SLUGS
    for mode in ("copilot", "xtm_one", "dev"):
        assert seed_matches_platform(skill, mode), mode
    assert len(skill["description"]) <= MAX_CATALOG_DESCRIPTION_CHARS
    assert _resolve_seed_content("skill_content", skill["content"]).strip()


def test_the_ctem_assistant_carries_every_skill_of_the_general_assistant():
    """In XTM One mode the General Assistant is linked to the CTEM Assistant and answers with its skills."""
    from app.services.general_assistant_skills import GENERAL_ASSISTANT_SKILL_SLUGS

    missing = [slug for slug in GENERAL_ASSISTANT_SKILL_SLUGS if "ctem-assistant" not in _skill(slug)["bind_to_agent_slugs"]]
    assert not missing, f"the CTEM Assistant lacks the General Assistant's skills {missing!r}"


def test_a_new_general_assistant_writes_reports():
    from app.models.general_assistant_config import DEFAULT_GA_BUILTIN_TOOL_IDS

    assert set(DEFAULT_ASSISTANT_MEDIA_TOOL_IDS) <= set(DEFAULT_GA_BUILTIN_TOOL_IDS)


def _migration():
    spec = importlib.util.spec_from_file_location("r5931gareport", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_adds_report_generation_once_and_leaves_anything_else_alone():
    from app.models.general_assistant_config import DEFAULT_GA_BUILTIN_TOOL_IDS

    migration = _migration()
    assert set(migration.ADDED_TOOL_IDS) <= set(DEFAULT_GA_BUILTIN_TOOL_IDS)
    assert json.loads(migration.with_added_tools('["generate_file"]')) == ["generate_file", "generate_report"]
    assert json.loads(migration.with_added_tools(None)) == ["generate_report"]
    assert json.loads(migration.with_added_tools("null")) == ["generate_report"]
    assert migration.with_added_tools('["generate_report", "web_fetch"]') is None
    assert migration.with_added_tools('{"not": "a list"}') is None
    assert migration.with_added_tools("not json") is None


# ── The Report Design skill ───────────────────────────────────────────────


def _report_skill() -> str:
    return REPORT_SKILL_PATH.read_text(encoding="utf-8")


def test_every_example_of_the_report_skill_is_a_valid_report_piece():
    from app.services.report_spec import validate_spec

    examples = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", _report_skill(), re.S)]
    assert len(examples) >= 6
    for example in examples:
        if "block" in example:
            validate_spec({"title": "Skill", "header": {"title": "Skill"}, "blocks": [example]})
        else:
            validate_spec(
                {"title": "Skill", "header": example, "blocks": [{"block": "prose", "rich_text": "<p>Body.</p>"}]}
            )


def test_the_report_skill_teaches_every_block_the_tool_accepts():
    from app.services.report_spec import REPORT_BLOCKS

    text = _report_skill()
    missing = [block for block in REPORT_BLOCKS if f"`{block}`" not in text]
    assert not missing, f"the skill never names {missing!r}"


def test_the_report_skill_teaches_the_arcs_and_the_revision_in_place():
    text = _report_skill()
    for words in (
        "Threat brief",
        "validation readout",
        "Compliance gap report",
        "Post-incident review",
        "Weekly CTEM briefing",
        'search_templates(kind="report")',
        "report_id",
        "get_report",
        "export_report",
        "[[REPORT:...]]",
    ):
        assert words in text, words
