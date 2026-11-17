"""Partial edits of an agent's persona and an assignment's prompt (update_skill's modes)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.core import database
from app.models.agent import Agent
from app.services.builtin_tools import agent_builder as builtin_tools_module
from app.services.builtin_tools.agent_builder import build_agent_builder_tool
from app.services.skill_edits import SkillEditError, partial_mode_args, partial_text_update
from tests.test_agent_builder_crud_tools import _call, _session_factory, _stub_writable_agent
from tests.test_agent_builder_tool import (
    _DedicatedAssignmentSession,
    _SharedSession,
    _writable_assignment_from_session,
)


@pytest.fixture(autouse=True)
def version_history(monkeypatch):
    """The calls update_agent makes to the version history (these fakes have no version tables)."""
    import app.services.agent_config_versioning as versioning

    records: list[dict] = []

    async def _record(_db, **kwargs):
        records.append(kwargs)
        return None, {}

    monkeypatch.setattr(versioning, "record_agent_version_changes", _record)
    monkeypatch.setattr(versioning, "versioned_agent_values", lambda agent: {"persona": agent.persona})
    return records


PROMPT = "# Daily check\n\nRead the queue.\n\n## Steps\n\nDo it.\n\nRun summary: one line.\n"
FIELDS = ("prompt", "activation_prompt", "description")


# ── the shared helper ─────────────────────────────────────────────────────────


def test_no_mode_is_the_whole_text_replace():
    assert partial_mode_args("", "", fields=FIELDS, whole={}) == ("replace", "prompt")
    assert partial_mode_args(None, None, fields=FIELDS, whole={}) == ("replace", "prompt")


def test_mode_args_refuse_an_unknown_mode_or_field_and_a_field_passed_whole():
    with pytest.raises(SkillEditError, match="unknown mode 'patch'"):
        partial_mode_args("patch", "", fields=FIELDS, whole={})
    with pytest.raises(SkillEditError, match="unknown field 'persona'"):
        partial_mode_args("edit", "persona", fields=FIELDS, whole={})
    with pytest.raises(SkillEditError, match="do not pass 'prompt' whole"):
        partial_mode_args("append", "prompt", fields=FIELDS, whole={"prompt": "x"}, content="y")
    with pytest.raises(SkillEditError, match="takes 'edits', not 'content'"):
        partial_mode_args("edit", "", fields=FIELDS, whole={}, content="y")
    with pytest.raises(SkillEditError, match="to replace the whole text pass it as 'prompt'"):
        partial_mode_args("replace", "", fields=FIELDS, whole={}, content="y")


def test_edit_changes_one_string_and_answers_the_diff():
    new, read_back = partial_text_update(
        "edit", PROMPT, field="prompt",
        edits=[{"find": "Run summary:", "replace": "FRICTIONS block, then Run summary:"}],
    )
    assert new == PROMPT.replace("Run summary:", "FRICTIONS block, then Run summary:")
    assert "prompt (edit):" in read_back
    assert "-Run summary: one line." in read_back
    assert "+FRICTIONS block, then Run summary: one line." in read_back
    assert "Read the queue." not in read_back  # the diff, not the text


def test_a_find_absent_or_ambiguous_refuses_the_whole_edit():
    with pytest.raises(SkillEditError, match="not found"):
        partial_text_update(
            "edit", PROMPT, field="prompt",
            edits=[{"find": "Do it.", "replace": "Done."}, {"find": "absent", "replace": "x"}],
        )
    with pytest.raises(SkillEditError, match="2"):
        partial_text_update("edit", "a b a", field="prompt", edits=[{"find": "a", "replace": "c"}])


def test_append_replace_section_and_insert_after():
    appended, _ = partial_text_update("append", PROMPT, field="prompt", content="## X\n\nNew.\n")
    assert appended == PROMPT + "\n## X\n\nNew.\n"
    replaced, _ = partial_text_update(
        "replace_section", PROMPT, field="prompt", section="Steps", content="## Steps\n\nDo less.\n"
    )
    assert "Do less." in replaced and "Do it." not in replaced and "Read the queue." in replaced
    inserted, _ = partial_text_update(
        "insert_after", PROMPT, field="prompt", section="Steps", content="## After\n\nThen.\n"
    )
    assert inserted.index("## After") > inserted.index("## Steps")


def test_append_to_an_empty_field_and_a_no_op_edit():
    new, _ = partial_text_update("append", None, field="guardrails", content="Never X.")
    assert new == "Never X."
    with pytest.raises(SkillEditError, match="unchanged"):
        partial_text_update("edit", PROMPT, field="prompt", edits=[{"find": "Do it.", "replace": "Do it."}])


# ── through update_agent ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_agent_appends_to_the_persona(monkeypatch, version_history):
    agent = Agent(id=uuid.uuid4(), name="Desk", persona="# Desk\n\nYou code.")
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))

    result = await _call(
        executor, "update_agent", agent_id=str(agent.id), mode="append", field="persona",
        content="## Frictions\n\nFile each one.",
    )

    assert not result.is_error, result.content
    assert "persona (append)" in result.content and "+## Frictions" in result.content
    assert agent.persona == "# Desk\n\nYou code.\n\n## Frictions\n\nFile each one."
    assert created[0].commits == 1
    # Versioned like a whole-text edit: the value before the change is handed over.
    assert version_history and version_history[0]["before"] == {"persona": "# Desk\n\nYou code."}


@pytest.mark.asyncio
async def test_update_agent_edit_on_guardrails_refused_saves_nothing(monkeypatch):
    agent = Agent(id=uuid.uuid4(), name="Desk", persona="p", guardrails="No A. No A.")
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))

    result = await _call(
        executor, "update_agent", agent_id=str(agent.id), mode="edit", field="guardrails",
        edits=[{"find": "No A.", "replace": "No B."}],
    )

    assert result.content.startswith("Error:") and "Nothing was saved." in result.content
    assert agent.guardrails == "No A. No A."
    assert all(c.commits == 0 for c in created)


@pytest.mark.asyncio
async def test_update_agent_without_a_mode_still_replaces_the_persona(monkeypatch):
    agent = Agent(id=uuid.uuid4(), name="Desk", persona="old")
    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))

    result = await _call(executor, "update_agent", agent_id=str(agent.id), persona="new")

    assert not result.is_error, result.content
    assert agent.persona == "new" and "```diff" not in result.content


# ── through update_assignment ─────────────────────────────────────────────────


def _assignment_executor(monkeypatch, **fields):
    user_id = str(uuid.uuid4())
    assignment_id = uuid.uuid4()
    assignments = {
        assignment_id: SimpleNamespace(
            id=assignment_id, name="Daily", prompt=PROMPT, activation_prompt=None,
            description=None, trigger_type="manual", trigger_config={}, delay=None,
            enabled=True, user_id=uuid.UUID(user_id), **fields,
        )
    }
    sessions: list = []
    monkeypatch.setattr(
        database, "async_session", lambda: _DedicatedAssignmentSession(sessions, assignments)
    )
    monkeypatch.setattr(
        builtin_tools_module, "_load_writable_assignment_for_builder", _writable_assignment_from_session
    )
    _, executor = build_agent_builder_tool(_SharedSession(), user_id)
    return executor, assignments[assignment_id], sessions


@pytest.mark.asyncio
async def test_update_assignment_edit_changes_only_that_string(monkeypatch):
    executor, assignment, sessions = _assignment_executor(monkeypatch)

    result = await _call(
        executor, "update_assignment", assignment_id=str(assignment.id), mode="edit", field="prompt",
        edits=[{"find": "Run summary:", "replace": "FRICTIONS block, then Run summary:"}],
    )

    assert not result.is_error, result.content
    assert assignment.prompt == PROMPT.replace("Run summary:", "FRICTIONS block, then Run summary:")
    assert "prompt (edit)" in result.content and "```diff" in result.content
    assert sum(s.commits for s in sessions) == 1


@pytest.mark.asyncio
async def test_update_assignment_ambiguous_find_refuses_with_the_count(monkeypatch):
    executor, assignment, sessions = _assignment_executor(monkeypatch)

    result = await _call(
        executor, "update_assignment", assignment_id=str(assignment.id), mode="edit",
        edits=[{"find": "\n\n", "replace": "\n"}],
    )

    assert result.content.startswith("Error:") and "Nothing was saved." in result.content
    assert assignment.prompt == PROMPT
    assert sum(s.commits for s in sessions) == 0


@pytest.mark.asyncio
async def test_update_assignment_replace_section_of_the_prompt(monkeypatch):
    executor, assignment, _ = _assignment_executor(monkeypatch)

    result = await _call(
        executor, "update_assignment", assignment_id=str(assignment.id), mode="replace_section",
        section="Steps", content="## Steps\n\nDo less.\n",
    )

    assert not result.is_error, result.content
    assert assignment.prompt == "# Daily check\n\nRead the queue.\n\n## Steps\n\nDo less.\n"


@pytest.mark.asyncio
async def test_update_assignment_refuses_a_field_passed_whole_with_a_partial_mode(monkeypatch):
    executor, assignment, sessions = _assignment_executor(monkeypatch)

    result = await _call(
        executor, "update_assignment", assignment_id=str(assignment.id), mode="append",
        prompt="whole", content="more",
    )

    assert "do not pass 'prompt' whole" in result.content
    assert assignment.prompt == PROMPT and sessions == []


def test_both_schemas_carry_the_partial_modes():
    definitions, _ = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    by_name = {d.name: d for d in definitions}
    agent_props = by_name["update_agent"].input_schema["properties"]
    assignment_props = by_name["update_assignment"].input_schema["properties"]
    assert agent_props["field"]["enum"] == ["persona", "guardrails", "description"]
    assert assignment_props["field"]["enum"] == ["prompt", "activation_prompt", "description"]
    for props in (agent_props, assignment_props):
        assert {"mode", "edits", "section", "content"} <= set(props)
    # create_agent takes none of them: it has no stored text to edit.
    assert "edits" not in by_name["create_agent"].input_schema["properties"]
