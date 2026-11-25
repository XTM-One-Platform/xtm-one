"""The Platform Health orchestrator reads carry the suite's name, the ``*_concierge_*`` names stay aliases."""

from __future__ import annotations

from app.services.builtin_allowed_tools import normalize_builtin_allowed_tools
from app.services.builtin_tool_aliases import RENAMED_BUILTIN_FUNCTIONS, current_function_name
from app.services.builtin_tools.filtering import filter_builtin_tools
from app.services.builtin_tools.health_orchestrator import GROUP_ID, TOOL_NAMES, build_health_orchestrator_tools
from app.tools.base import ToolDefinition, declared_aliases

RENAMED = {
    "list_concierge_signals": "list_health_signals",
    "list_concierge_cycles": "list_health_cycles",
    "get_concierge_cycle": "get_health_cycle",
    "request_concierge_cycle": "request_health_cycle",
}


def test_the_former_names_are_declared_renames():
    for former, current in RENAMED.items():
        assert RENAMED_BUILTIN_FUNCTIONS[former] == current
        assert current_function_name(former) == current


def test_the_group_offers_only_the_current_names():
    definitions, _executor = build_health_orchestrator_tools("")
    names = {d.name for d in definitions}
    assert names == set(TOOL_NAMES)
    assert set(RENAMED.values()) <= names
    assert not names & set(RENAMED)


def test_a_call_under_a_former_name_resolves_to_the_renamed_function():
    definitions, executor = build_health_orchestrator_tools("")
    declared = declared_aliases(definitions)
    for former, current in RENAMED.items():
        assert declared[former] == current
    assert set(RENAMED) | set(RENAMED.values()) <= set(executor.tool_names)


def test_an_assignment_run_gets_no_request_under_either_name():
    definitions, executor = build_health_orchestrator_tools("", allow_requests=False)
    assert "request_health_cycle" not in {d.name for d in definitions}
    assert not {"request_health_cycle", "request_concierge_cycle"} & set(executor.tool_names)


def test_a_stored_allow_list_with_a_former_name_keeps_the_renamed_function():
    assert normalize_builtin_allowed_tools({GROUP_ID: ["list_concierge_cycles"]}) == {GROUP_ID: ["list_health_cycles"]}
    tools = [ToolDefinition(name=name, description="x", source=f"builtin:{GROUP_ID}") for name in TOOL_NAMES]
    kept = filter_builtin_tools(tools, {GROUP_ID: ["get_concierge_cycle"]})
    assert [tool.name for tool in kept] == ["get_health_cycle"]
