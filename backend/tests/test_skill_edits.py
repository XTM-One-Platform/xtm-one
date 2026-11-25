"""Section reads, partial updates and the wipe guard of a skill."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.core import database
from app.services import skill_versioning
from app.services.builtin_tools.agent_builder import build_agent_builder_tool
from app.services.skill_edits import (
    SkillEditError,
    format_outline,
    new_skill_content,
    partial_text_update,
    resolve_section,
)
from tests.test_agent_builder_crud_tools import _call, _session_factory, _stub_manageable_gate

SKILL = (
    "# Skill\n\nIntro.\n\n"
    "## 1. Identity\n\nAct as Sam.\n\n"
    "## 2. Merge rule\n\nSquash at green.\n\n"
    "### Holds\n\nOnly Sam's words.\n\n"
    "## 3. Reviews\n\nAnswer every thread.\n"
    "```md\n## not a heading\n```\n"
)
LONG = SKILL + ("Filler line for a long skill body.\n" * 80)


@pytest.mark.parametrize("mode", ["insert_after", "replace_section"])
def test_section_mode_on_a_headingless_prompt_names_the_field_and_the_modes_that_apply(mode: str) -> None:
    """#2193: a headingless assignment prompt refuses a section mode up front,
    naming the field (never 'skill') and the modes that work: edit, append."""
    prompt = "Read the queue.\n\nTriage each item.\n\nReport the result.\n"
    with pytest.raises(SkillEditError) as excinfo:
        partial_text_update(mode, prompt, field="prompt", content="## New\n\nMore.", section="Triage each item.")
    message = str(excinfo.value)
    assert "this prompt has no Markdown heading" in message
    assert "'edit'" in message and "'append'" in message
    assert "skill" not in message


def test_heading_first_refusal_names_the_field_edited() -> None:
    with pytest.raises(SkillEditError) as excinfo:
        partial_text_update("insert_after", SKILL, field="persona", content="no heading", section="#2")
    assert "so the persona keeps its structure" in str(excinfo.value)


def test_outline_lists_every_heading_with_offsets():
    text = format_outline(SKILL)
    assert "Outline (5 headings" in text
    assert "#3   ## 2. Merge rule [offset" in text
    assert "#4     ### Holds" in text
    assert "not a heading" not in text


@pytest.mark.parametrize("selector", ["2. Merge rule", "2", "#3", "Skill > 2. Merge rule", "merge rule"])
def test_a_selector_resolves_like_load_skill(selector):
    assert resolve_section(SKILL, selector).title == "2. Merge rule"


def test_an_unknown_or_ambiguous_section_is_refused_with_the_headings():
    with pytest.raises(SkillEditError, match="no heading matches"):
        resolve_section(SKILL, "Deploy")
    with pytest.raises(SkillEditError, match="out of range"):
        resolve_section(SKILL, "#9")
    doubled = "# A\n## Notes\nx\n# B\n## Notes\ny\n"
    with pytest.raises(SkillEditError, match="matches 2 headings"):
        resolve_section(doubled, "Notes")
    assert resolve_section(doubled, "B > Notes").start == doubled.index("## Notes\ny")


def test_replace_section_keeps_every_other_character():
    new, detail = new_skill_content(
        "replace_section", SKILL, content="## 2. Merge rule\n\nTeam approval.\n", section="2"
    )
    assert "Team approval." in new and "Squash at green." not in new
    assert "Only Sam's words." not in new  # the subsection belongs to the section
    before, after = SKILL.split("## 2. Merge rule")[0], SKILL.split("## 3. Reviews")[1]
    assert new.startswith(before) and new.endswith(after)
    assert "Section #3 '2. Merge rule'" in detail


def test_a_section_content_must_start_with_its_heading():
    with pytest.raises(SkillEditError, match="heading line"):
        new_skill_content("replace_section", SKILL, content="Team approval.", section="2")


def test_insert_after_and_append():
    new, _ = new_skill_content("insert_after", SKILL, content="## 2bis. Pools\n\nTen PRs.", section="2")
    assert new.index("## 2bis. Pools") > new.index("Only Sam's words.")
    assert new.index("## 2bis. Pools") < new.index("## 3. Reviews")
    appended, _ = new_skill_content("append", "# A\nbody", content="## B\nmore")
    assert appended == "# A\nbody\n\n## B\nmore"


def test_edit_is_all_or_nothing_and_refuses_a_non_unique_find():
    new, detail = new_skill_content("edit", SKILL, edits=[{"find": "Act as Sam.", "replace": "Act as the desk."}])
    assert "Act as the desk." in new and "Edits applied" in detail
    with pytest.raises(SkillEditError):
        new_skill_content("edit", SKILL, edits=[{"find": "Sam", "replace": "X"}])


@pytest.mark.parametrize("content", ["[[PLACEHOLDER]]", "  {{content}} ", "TODO", "...", "short body"])
def test_a_placeholder_replace_of_a_long_skill_is_refused_naming_its_length(content):
    with pytest.raises(SkillEditError, match=f"current body of {len(LONG)} characters"):
        new_skill_content("replace", LONG, content=content, confirm_shrink=True)


def test_a_shrinking_replace_needs_confirm_shrink():
    half = LONG[: len(LONG) // 3]
    with pytest.raises(SkillEditError, match="confirm_shrink=true"):
        new_skill_content("replace", LONG, content=half)
    assert new_skill_content("replace", LONG, content=half, confirm_shrink=True)[0] == half
    # A short skill is ordinary editing: no guard.
    assert new_skill_content("replace", SKILL, content="x")[0] == "x"


# ── through the update_skill / get_skill tools ──────────────────────────────


def _skill(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(), name="Skill", url=None, content=content, description="d",
        compatibility=None, tags=[], enabled=True, is_company_managed=False, user_id=None,
    )


@pytest.fixture
def versions(monkeypatch):
    written: list = []

    async def _fake_version(_db, skill, *, author_id, commit_message=None):
        written.append((skill.content, commit_message))

    monkeypatch.setattr(skill_versioning, "create_skill_version", _fake_version)
    return written


@pytest.mark.asyncio
async def test_update_skill_refuses_a_placeholder_and_keeps_the_body(monkeypatch, versions):
    skill = _skill(LONG)
    _stub_manageable_gate(monkeypatch, entity=skill)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))

    result = await _call(executor, "update_skill", skill_id=str(skill.id), content="[[PLACEHOLDER]]")

    assert f"current body of {len(LONG)} characters" in result.content
    assert "Nothing was changed." in result.content
    assert skill.content == LONG
    assert all(c.commits == 0 for c in created) and versions == []


@pytest.mark.asyncio
async def test_update_skill_replace_section_writes_a_version(monkeypatch, versions):
    skill = _skill(LONG)
    _stub_manageable_gate(monkeypatch, entity=skill)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))

    result = await _call(
        executor, "update_skill", skill_id=str(skill.id), mode="replace_section",
        section="1. Identity", content="## 1. Identity\n\nAct as the desk.\n",
    )

    assert not result.is_error, result.content
    assert "content (replace_section)" in result.content
    assert "Act as the desk." in skill.content and "Squash at green." in skill.content
    assert versions == [(skill.content, "update_skill (replace_section)")]
    assert any(c.commits == 1 for c in created)


@pytest.mark.asyncio
async def test_update_skill_edit_refuses_a_non_unique_find(monkeypatch, versions):
    skill = _skill(LONG)
    _stub_manageable_gate(monkeypatch, entity=skill)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))

    result = await _call(
        executor, "update_skill", skill_id=str(skill.id), mode="edit",
        edits=[{"find": "Filler line", "replace": "x"}],
    )

    assert result.content.startswith("Error:") and "Nothing was changed." in result.content
    assert skill.content == LONG and versions == []


@pytest.mark.asyncio
async def test_update_skill_refuses_url_with_a_partial_mode(monkeypatch):
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor, "update_skill", skill_id=str(uuid.uuid4()), mode="append",
        url="https://example.com/SKILL.md", content="## X",
    )
    assert "accepted with mode 'replace' only" in result.content


@pytest.mark.asyncio
async def test_get_skill_outline_and_section(monkeypatch):
    from app.api.v1 import helpers as api_helpers

    async def _no_groups(_db, _uid):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)
    skill = _skill(LONG)
    monkeypatch.setattr(database, "async_session", _session_factory([], execute_results=[skill]))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))

    result = await _call(executor, "get_skill", skill_id=str(skill.id), outline=True, section="#3")

    assert not result.is_error, result.content
    assert "Outline (5 headings" in result.content
    assert "## 2. Merge rule\n\nSquash at green.\n\n### Holds\n\nOnly Sam's words.\n\n" in result.content
    assert "Filler line" not in result.content
    assert "Content (full):" not in result.content
