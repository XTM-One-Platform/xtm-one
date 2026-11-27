"""#2677: the resume notice, the cut-result marker and the capabilities line
name ``code_interpreter`` (and its ``attachments/`` mount) only when the run
can call it."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.llm.base import ChatMessage
from app.services import turn_checkpoint
from app.services.code_interpreter import callable_builtin_tool_ids, code_interpreter_callable
from app.services.turn_checkpoint import ResumedWork, resume_note
from app.tools.base import ToolCall, ToolDefinition, offering_tools


@pytest.fixture(autouse=True)
def _code_interpreter_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "disable_code_interpreter", False)


def _resumed_with_a_spill() -> ResumedWork:
    call = ToolCall(id="c1", name="load_skill", arguments={"name": "x"})
    return ResumedWork(
        found=True,
        messages=[ChatMessage(role="assistant", content="", tool_calls=[call])],
        spilled=[{"call_id": "c1", "name": "load_skill", "chars": 40_000, "file_id": "f-1"}],
    )


def test_resume_note_names_read_file_content_alone_without_the_code_interpreter() -> None:
    note = resume_note(_resumed_with_a_spill())
    assert note is not None
    assert "(read_file_content): load_skill" in note.content
    assert "code_interpreter" not in note.content
    assert "attachments/" not in note.content


def test_resume_note_keeps_the_mount_when_the_code_interpreter_is_callable() -> None:
    note = resume_note(_resumed_with_a_spill(), code_interpreter=True)
    assert note is not None
    assert "(read_file_content, or attachments/ in code_interpreter): load_skill" in note.content


def _builtin_code_interpreter() -> ToolDefinition:
    return ToolDefinition(
        name="code_interpreter",
        description="d",
        input_schema={"type": "object", "properties": {}},
        source="builtin:code_interpreter",
    )


def test_cut_marker_names_the_mount_only_on_a_surface_offering_the_code_interpreter() -> None:
    with offering_tools([]):
        bare = turn_checkpoint._cut_note(40_000, "f-1", "r.txt")
    assert 'read_file_content(file_id="f-1") reads it.' in bare
    assert "code_interpreter" not in bare and "attachments/" not in bare

    with offering_tools([_builtin_code_interpreter()]):
        mounted = turn_checkpoint._cut_note(40_000, "f-1", "r.txt")
    assert "and code_interpreter opens it as attachments/" in mounted


def test_cut_marker_outside_a_loop_names_no_mount() -> None:
    text = turn_checkpoint._cut_note(40_000, "f-1", "r.txt")
    assert "code_interpreter" not in text


def test_code_interpreter_callable_reads_the_switch_the_selection_and_the_deny_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import settings

    assert code_interpreter_callable(["code_interpreter", "files"]) is True
    assert code_interpreter_callable(["files"]) is False
    assert code_interpreter_callable(["code_interpreter"], ["code_interpreter"]) is False
    monkeypatch.setattr(settings, "disable_code_interpreter", True)
    assert code_interpreter_callable(["code_interpreter"]) is False


def test_capabilities_list_omits_what_the_run_cannot_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    ids = ["code_interpreter", "files", "web"]
    assert callable_builtin_tool_ids(ids) == ids, "callable: unchanged"
    assert callable_builtin_tool_ids(ids, deny_list=["code_interpreter"]) == ["files", "web"]
    assert callable_builtin_tool_ids(ids, allowed_map={"web": []}) == ["code_interpreter", "files"]
    monkeypatch.setattr(settings, "disable_code_interpreter", True)
    assert callable_builtin_tool_ids(ids) == ["files", "web"]


def test_every_capabilities_line_goes_through_the_callable_builder() -> None:
    """The three entry points that write the line build it from the callable groups."""
    import inspect

    from app.services import agent_chat, assignment_helpers, general_chat

    for module in (agent_chat, assignment_helpers, general_chat):
        source = inspect.getsource(module)
        assert "callable_builtin_tool_ids(" in source, module.__name__
        assert "', '.join(agent.builtin_tool_ids)" not in source, module.__name__
        assert "', '.join(ga_builtin_tool_ids)" not in source, module.__name__


def test_every_resume_entry_point_passes_the_callable_decision() -> None:
    """Review of #2682: the web, REST, channel and cut-turn (parent wake)
    resumes, and the A2A hand-back, resolve the flag instead of taking the
    no-mount default."""
    import inspect
    import re

    from app.a2a import tasks
    from app.api.v1 import chat
    from app.channels import router
    from app.services import parent_wake

    for module in (chat, router, parent_wake, tasks):
        source = inspect.getsource(module)
        calls = re.findall(r"\.load_replay\((.*?)\n\s*\)", source, flags=re.S)
        assert calls, module.__name__
        for call in calls:
            assert "code_interpreter=" in call, f"{module.__name__}: load_replay({call.strip()})"


def test_capabilities_line_of_an_agent_with_a_platform_disabled_code_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "disable_code_interpreter", True)
    agent = SimpleNamespace(
        builtin_tool_ids=["code_interpreter", "files"], tool_deny_list=None, builtin_allowed_tools=None
    )
    line = "built-in tools: " + ", ".join(
        callable_builtin_tool_ids(
            agent.builtin_tool_ids, deny_list=agent.tool_deny_list, allowed_map=agent.builtin_allowed_tools
        )
    )
    assert line == "built-in tools: files"
