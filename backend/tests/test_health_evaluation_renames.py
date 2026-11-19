"""The Platform Health evaluation tools carry the suite's name, the ``*_concierge_*`` names stay aliases."""

from __future__ import annotations

from app.services.builtin_allowed_tools import normalize_builtin_allowed_tools
from app.services.builtin_tool_aliases import RENAMED_BUILTIN_FUNCTIONS, current_function_name
from app.services.builtin_tools.filtering import filter_builtin_tools
from app.services.health.evaluation_tools import EVALUATION_SUITE_ID, build_health_evaluation_tools
from app.services.mcp_tool_metadata import _READ_ONLY_NAMES
from app.tools.base import ToolDefinition, declared_aliases

RENAMED = {
    "run_concierge_evaluation": "run_health_evaluation",
    "get_concierge_evaluation": "get_health_evaluation",
    "compare_concierge_runs": "compare_health_runs",
}
CURRENT = ("build_dataset_from_traces", *RENAMED.values())


def test_the_former_names_are_declared_renames():
    for former, current in RENAMED.items():
        assert RENAMED_BUILTIN_FUNCTIONS[former] == current
        assert current_function_name(former) == current


def test_the_group_offers_only_the_current_names():
    definitions, _executor = build_health_evaluation_tools(None, None)  # type: ignore[arg-type]
    names = {d.name for d in definitions}
    assert names == set(CURRENT)
    assert not names & set(RENAMED)


def test_a_call_under_a_former_name_resolves_to_the_renamed_function():
    definitions, executor = build_health_evaluation_tools(None, None)  # type: ignore[arg-type]
    declared = declared_aliases(definitions)
    for former, current in RENAMED.items():
        assert declared[former] == current
        assert executor._handlers[former] is executor._handlers[current]


def test_a_stored_allow_list_with_a_former_name_keeps_the_renamed_function():
    assert normalize_builtin_allowed_tools({EVALUATION_SUITE_ID: ["compare_concierge_runs"]}) == {
        EVALUATION_SUITE_ID: ["compare_health_runs"]
    }
    tools = [ToolDefinition(name=name, description="x", source=f"builtin:{EVALUATION_SUITE_ID}") for name in CURRENT]
    kept = filter_builtin_tools(tools, {EVALUATION_SUITE_ID: ["get_concierge_evaluation"]})
    assert [tool.name for tool in kept] == ["get_health_evaluation"]


def test_the_comparison_reads_as_read_only_under_both_names():
    assert {"compare_health_runs", "compare_concierge_runs"} <= _READ_ONLY_NAMES
