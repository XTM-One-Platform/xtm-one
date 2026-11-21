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


async def test_learned_skill_is_owned_by_the_company_owner_not_the_author(monkeypatch):
    """Deleting the user whose run wrote the first lesson must not cascade to the skill."""
    import uuid
    from types import SimpleNamespace

    from app.api.v1 import helpers
    from app.services import builtin_customer_layer, organizations, skill_versioning

    owner_id, author_id = uuid.uuid4(), uuid.uuid4()
    versions: list = []

    async def _none(*_a, **_k):
        return None

    async def _owner(_db):
        return owner_id

    async def _version(_db, skill, *, author_id, commit_message):
        versions.append(author_id)

    monkeypatch.setattr(learned, "find_learned_skill", _none)
    monkeypatch.setattr(organizations, "get_primary", _none)
    monkeypatch.setattr(helpers, "resolve_company_managed_owner_id", _owner)
    monkeypatch.setattr(skill_versioning, "create_skill_version", _version)
    monkeypatch.setattr(builtin_customer_layer, "record_customer_fields", lambda *_a: None)

    added: list = []

    async def _flush():
        added[-1].id = uuid.uuid4()

    db = SimpleNamespace(add=added.append, flush=_flush)
    agent = SimpleNamespace(id=uuid.uuid4(), name="Triage", is_builtin=True, skill_ids=[], skill_load_modes={})
    skill = await learned.ensure_learned_skill(db, agent, author_id=author_id)
    assert skill.user_id == owner_id
    assert skill.is_company_managed
    assert versions == [author_id]
