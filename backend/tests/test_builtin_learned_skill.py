# SPDX-License-Identifier: Apache-2.0
"""The learned skill of a built-in agent (#914, slice 3): section rules and tool gating."""

from __future__ import annotations

import pytest

from app.services import builtin_learned_skill as learned


def test_append_creates_then_extends_a_section():
    body = learned.apply_lesson(None, "Asset owners", "portal.example: platform team")
    assert "## Asset owners\nportal.example: platform team" in body
    body = learned.apply_lesson(body, "asset owners", "db.example: data team")
    assert body.count("## Asset owners") == 1
    assert "db.example: data team" in body


def test_revise_replaces_and_remove_drops_only_that_section():
    body = learned.apply_lesson(None, "A", "first-rule")
    body = learned.apply_lesson(body, "B", "two")
    body = learned.apply_lesson(body, "A", "second-rule", mode="revise")
    assert "## A\nsecond-rule" in body and "first-rule" not in body and "## B\ntwo" in body
    body = learned.apply_lesson(body, "A", "", mode="remove")
    assert "## A" not in body and "## B\ntwo" in body


@pytest.mark.parametrize(
    ("title", "text", "mode"),
    [("", "x", "append"), ("T", "", "append"), ("T", "x", "rewrite"), ("Missing", "", "remove")],
)
def test_refusals(title, text, mode):
    with pytest.raises(learned.LessonError):
        learned.apply_lesson(None, title, text, mode=mode)


def test_bounds_are_enforced():
    with pytest.raises(learned.LessonError):
        learned.apply_lesson(None, "T", "x" * (learned.SECTION_MAX_CHARS + 1))
    body = None
    with pytest.raises(learned.LessonError):
        for i in range(100):
            body = learned.apply_lesson(body, f"Topic {i}", "y" * 1000)


def test_name_tags_and_freeze():
    assert learned.skill_name("Triage", "Acme") == "Triage learned for Acme"
    assert learned.skill_name("Triage", "") == "Triage learned for this organization"
    assert learned.agent_tag("abc") == "learned-for-agent:abc"

    class _Skill:
        tags = [learned.LEARNED_TAG, learned.FROZEN_TAG]

    assert learned.is_frozen(_Skill())


def test_tool_is_offered_to_built_ins_only(monkeypatch):
    from app.core.config import settings
    from app.services.builtin_tools import LEARNED_LESSON_TOOL, build_learned_lesson_tool, offers_learned_lesson_tool

    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda self: False), raising=False)
    assert offers_learned_lesson_tool(is_builtin=True)
    assert not offers_learned_lesson_tool(is_builtin=False)
    definitions, _ = build_learned_lesson_tool(agent_id="00000000-0000-0000-0000-000000000001", user_id=None)
    assert [d.name for d in definitions] == [LEARNED_LESSON_TOOL]
    assert len(definitions[0].description) <= 300
