"""The craft skills suggested to an agent created with no skill (#12343, slice 2).

``agent_template_skills.suggested_skill_slugs`` maps the built-in groups an
agent carries to the craft skill of each; the agent-builder ``create_agent``
names them, resolved for the caller, without attaching them.
"""

from __future__ import annotations

import pytest

from app.agents.builtin_agents import BUILTIN_SKILLS
from app.services.agent_template_skills import (
    SKILL_SLUGS_BY_BUILTIN_GROUP,
    suggested_skill_slugs,
)
from app.services.builtin_tool_groups import BUILTIN_TOOL_GROUPS


def test_every_suggested_slug_is_a_global_builtin_skill() -> None:
    by_slug = {s["slug"]: s for s in BUILTIN_SKILLS}
    for group, slugs in SKILL_SLUGS_BY_BUILTIN_GROUP.items():
        for slug in slugs:
            assert slug in by_slug, (group, slug)
            assert "global" in by_slug[slug].get("platform_targets", []), (group, slug)


def test_every_mapped_group_is_a_builtin_tool_group() -> None:
    groups = {g.id for g in BUILTIN_TOOL_GROUPS}
    assert set(SKILL_SLUGS_BY_BUILTIN_GROUP) <= groups


@pytest.mark.parametrize(
    ("tools", "expected"),
    [
        (["platform"], ["fleet-design-practices", "fleet-health-advisory"]),
        # Former ids count as their group, once.
        (["agent_builder", "fleets"], ["fleet-design-practices", "fleet-health-advisory"]),
        (
            ["generate_report", "video_studio", "generate_presentation"],
            ["report-design", "video-production", "presentation-design"],
        ),
        (["web", "code_interpreter"], []),
        ([], []),
        (None, []),
        ("platform", []),
        ([1, None, "generate_report"], ["report-design"]),
    ],
)
def test_suggested_skill_slugs(tools: object, expected: list[str]) -> None:
    assert suggested_skill_slugs(tools) == expected
