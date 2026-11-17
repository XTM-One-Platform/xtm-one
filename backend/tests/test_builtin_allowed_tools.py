"""Per-function scoping of built-in tool groups: ``builtin_allowed_tools``.

Same semantics as ``mcp_allowed_tools``: a group absent or mapped to null keeps
every function, a list keeps only the functions it names. The filter is keyed
on the definition's ``source`` (``builtin:<group id>``), never removes a
``core`` tool, and leaves every non-built-in tool alone.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.models.agent_copy import (
    AGENT_BUNDLE_PORTABLE_FIELDS,
    AGENT_COPY_FIELDS,
    ASSIGNMENT_COPY_FIELDS,
)
from app.services import builtin_allowed_tools as bat
from app.services.builtin_tools import (
    apply_tool_deny_list,
    filter_builtin_tools,
    merge_tool_allow_lists,
)


@dataclass
class _Def:
    name: str
    source: str
    core: bool = False


def _tools() -> list[_Def]:
    return [
        # The platform group's source; lists keyed by the former
        # ``agent_builder`` id fold onto it.
        _Def("list_agents", "builtin:platform"),
        _Def("get_agent", "builtin:platform"),
        _Def("delete_agent", "builtin:platform"),
        _Def("web_fetch", "builtin:web_fetch"),
        _Def("schedule_work_item", "builtin:work_items", core=True),
        _Def("search_github_issues", "integration:github"),
        _Def("custom_tool", "custom:1234"),
    ]


def _names(defs) -> list[str]:
    return [d.name for d in defs]


def test_no_map_keeps_everything():
    tools = _tools()
    assert filter_builtin_tools(tools, None) is tools
    assert filter_builtin_tools(tools, {}) is tools


def test_list_keeps_only_named_functions_of_that_group():
    kept = filter_builtin_tools(_tools(), {"agent_builder": ["list_agents", "get_agent"]})
    assert _names(kept) == [
        "list_agents",
        "get_agent",
        "web_fetch",
        "schedule_work_item",
        "search_github_issues",
        "custom_tool",
    ]


def test_null_group_keeps_every_function():
    kept = filter_builtin_tools(_tools(), {"agent_builder": None})
    assert "delete_agent" in _names(kept)


def test_empty_list_removes_the_group_but_not_core_tools():
    kept = filter_builtin_tools(_tools(), {"agent_builder": [], "work_items": []})
    assert "list_agents" not in _names(kept)
    assert "schedule_work_item" in _names(kept)


def test_non_builtin_sources_are_untouched():
    kept = filter_builtin_tools(_tools(), {"github": [], "1234": []})
    assert "search_github_issues" in _names(kept)
    assert "custom_tool" in _names(kept)


def test_deny_list_still_applies_after_the_allow_list():
    allowed = filter_builtin_tools(_tools(), {"agent_builder": ["list_agents", "delete_agent"]})
    kept = apply_tool_deny_list(allowed, ["delete_agent"])
    assert "delete_agent" not in _names(kept)
    assert "list_agents" in _names(kept)


def test_agent_and_assignment_lists_intersect():
    merged = merge_tool_allow_lists(
        {"agent_builder": ["list_agents", "get_agent"]},
        {"agent_builder": ["get_agent", "delete_agent"]},
    )
    assert _names(filter_builtin_tools(_tools(), merged))[:1] == ["get_agent"]
    assert "list_agents" not in _names(filter_builtin_tools(_tools(), merged))


def test_normalize_drops_null_groups_and_dedupes(monkeypatch):
    monkeypatch.setattr(bat, "builtin_group_functions", lambda g: ("a", "b") if g == "grp" else ())
    assert bat.normalize_builtin_allowed_tools(None) is None
    assert bat.normalize_builtin_allowed_tools({}) is None
    assert bat.normalize_builtin_allowed_tools({"grp": None}) is None
    assert bat.normalize_builtin_allowed_tools({"grp": ["b", "a", "b"]}) == {"grp": ["b", "a"]}
    assert bat.normalize_builtin_allowed_tools({"grp": []}) == {"grp": []}


def test_normalize_names_the_valid_functions(monkeypatch):
    monkeypatch.setattr(bat, "builtin_group_functions", lambda g: ("a", "b") if g == "grp" else ())
    with pytest.raises(bat.BuiltinAllowedToolsError) as exc:
        bat.normalize_builtin_allowed_tools({"grp": ["a", "zz"]})
    assert "zz" in str(exc.value) and "Valid functions: a, b" in str(exc.value)
    with pytest.raises(bat.BuiltinAllowedToolsError, match="unknown built-in group 'nope'"):
        bat.normalize_builtin_allowed_tools({"nope": ["a"]})
    with pytest.raises(bat.BuiltinAllowedToolsError):
        bat.normalize_builtin_allowed_tools({"grp": "a"})
    with pytest.raises(bat.BuiltinAllowedToolsError):
        bat.normalize_builtin_allowed_tools(["grp"])


def test_the_field_travels_with_copies_and_bundles():
    assert "builtin_allowed_tools" in AGENT_COPY_FIELDS
    assert "builtin_allowed_tools" in AGENT_BUNDLE_PORTABLE_FIELDS
    assert "builtin_allowed_tools" in ASSIGNMENT_COPY_FIELDS
