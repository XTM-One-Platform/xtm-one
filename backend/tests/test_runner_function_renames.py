"""The Computer use file functions carry the ``runner_`` prefix, the former names stay aliases."""

from __future__ import annotations

import uuid

from app.services.builtin_allowed_tools import normalize_builtin_allowed_tools
from app.services.builtin_tool_aliases import RENAMED_BUILTIN_FUNCTIONS, current_function_name
from app.services.builtin_tools.computer_use import _tool_definitions, build_computer_use_tools
from app.services.builtin_tools.filtering import apply_tool_deny_list, filter_builtin_tools
from app.tools.base import ToolDefinition, declared_aliases

RENAMED = {"read_file": "runner_read_file", "write_file": "runner_write_file", "list_dir": "runner_list_dir"}


def test_the_runner_renames_are_the_three_file_functions():
    runner = {former: current for former, current in RENAMED_BUILTIN_FUNCTIONS.items() if current.startswith("runner_")}
    assert runner == RENAMED


def test_a_call_under_a_former_name_resolves_to_the_renamed_function():
    declared = declared_aliases(_tool_definitions())
    for former, current in RENAMED.items():
        assert declared[former] == current


def test_the_group_offers_only_the_current_names():
    names = {tool.name for tool in _tool_definitions()}
    assert set(RENAMED.values()) <= names
    assert not set(RENAMED) & names


def test_the_executor_runs_both_names():
    _defs, executor = build_computer_use_tools(
        user_id=str(uuid.uuid4()),
        is_admin=False,
        user_group_ids=[],
        agent_runner_ids=None,
        agent_runner_labels=None,
        conversation_id=str(uuid.uuid4()),
    )
    registered = set(executor.tool_names)
    assert set(RENAMED) | set(RENAMED.values()) <= registered


def test_an_allow_list_naming_a_former_function_is_stored_renamed():
    assert normalize_builtin_allowed_tools({"computer_use": ["read_file", "run_shell"]}) == {
        "computer_use": ["runner_read_file", "run_shell"]
    }


def _computer_use_tools() -> list[ToolDefinition]:
    return [ToolDefinition(name=name, description="x", source="builtin:computer_use") for name in ("run_shell", *RENAMED.values())]


def test_a_stored_allow_list_with_a_former_name_keeps_the_renamed_function():
    kept = filter_builtin_tools(_computer_use_tools(), {"computer_use": ["read_file"]})
    assert [tool.name for tool in kept] == ["runner_read_file"]


def test_a_deny_list_with_a_former_name_still_denies():
    kept = apply_tool_deny_list(_computer_use_tools(), ["write_file"])
    assert "runner_write_file" not in {tool.name for tool in kept}
    assert "runner_read_file" in {tool.name for tool in kept}


def test_a_current_name_is_unchanged():
    assert current_function_name("run_shell") == "run_shell"
    assert current_function_name("runner_read_file") == "runner_read_file"

