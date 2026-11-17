"""Built-in groups loadable on demand through ``load_tools``.

A large built-in group keeps a small always-on core (its create and read
functions); its other functions are gated as ``builtin:<id>`` and revealed by
``load_tools`` like an integration.
"""

from __future__ import annotations

from app.models.general_assistant_config import DEFAULT_GA_BUILTIN_TOOL_IDS
from app.services.builtin_tool_aliases import alias_group
from app.services.builtin_tool_catalog import build_builtin_tool_catalog
from app.services.builtin_tool_groups import (
    BUILTIN_TOOL_GROUPS,
    builtin_on_demand_groups,
    builtin_source,
)
from app.tools.base import ToolDefinition
from app.tools.tool_gating import build_tool_gate, group_tools


def _owner_by_name() -> dict[str, str]:
    """Function name -> the first group listing it (the group its ``source`` names)."""
    owner: dict[str, str] = {}
    for group_id, definitions in build_builtin_tool_catalog(include_disabled=True).items():
        for definition in definitions:
            owner.setdefault(definition["name"], group_id)
    return owner


def _definitions(group_ids) -> list[ToolDefinition]:
    """The functions *group_ids* grant, stamped as the builders stamp them.

    A [DEPRECATED] former id (``generate_file`` in the default set)
    reads as the group it merged into.
    """
    catalog = build_builtin_tool_catalog(include_disabled=True)
    owner = _owner_by_name()
    seen: set[str] = set()
    tools: list[ToolDefinition] = []
    for group_id in group_ids:
        for definition in catalog[alias_group(group_id) or group_id]:
            if definition["name"] in seen:
                continue
            seen.add(definition["name"])
            tools.append(
                ToolDefinition(
                    name=definition["name"],
                    description=definition["description"],
                    input_schema=definition["input_schema"],
                    source=builtin_source(owner[definition["name"]]),
                )
            )
    return tools


def test_every_declared_core_name_is_a_function_of_its_group():
    catalog = build_builtin_tool_catalog(include_disabled=True)
    owner = _owner_by_name()
    for group in BUILTIN_TOOL_GROUPS:
        if group.core is None:
            continue
        names = {d["name"] for d in catalog[group.id]}
        assert group.core, group.id
        for name in group.core:
            assert name in names, (group.id, name)
            assert owner[name] == group.id, (group.id, name)
        # A core that is the whole group gates nothing: declare no core instead.
        assert set(group.core) != names, group.id


def test_only_the_large_groups_are_on_demand():
    on_demand = builtin_on_demand_groups()
    assert {"platform", "generate_presentation", "video_studio"} <= set(on_demand)
    for always_whole in ("files", "generate_report", "web", "coordination"):
        assert always_whole not in on_demand


def test_default_assistant_answers_presentation_report_file_and_web_without_loading():
    """The default General Assistant set needs no ``load_tools`` call for its everyday asks."""
    tools = _definitions(sorted(DEFAULT_GA_BUILTIN_TOOL_IDS))
    gate = build_tool_gate(tools, enabled=True, min_tools=16, on_demand=builtin_on_demand_groups())
    assert gate is not None
    visible = {tool.name for tool in gate.visible_tools()}
    for name in (
        "generate_presentation",
        "edit_presentation",
        "get_presentation",
        "generate_report",
        "get_report",
        "export_report",
        "generate_file",
        "read_file_content",
        "web_fetch",
        "search_templates",
        "create_agent",
        "create_assignment",
        "create_video",
    ):
        assert name in visible, name
    # The rest of the large groups waits behind load_tools.
    for gated in ("update_integration", "record_browser_demo", "share_presentation"):
        assert gated not in visible, gated


def test_load_tools_reveals_the_rest_of_a_builtin_group():
    tools = _definitions(["agent_builder", "generate_file"])
    gate = build_tool_gate(tools, enabled=True, min_tools=16, on_demand=builtin_on_demand_groups())
    assert gate is not None
    # The former id loads the platform group it merged into.
    assert "builtin:platform" in gate.catalog_prompt
    assert "update_integration" not in {t.name for t in gate.visible_tools()}
    result = gate.reveal(["builtin:platform"])
    assert result.changed
    visible = {t.name for t in gate.visible_tools()}
    assert {"update_integration", "delete_agent", "create_agent"} <= visible


def test_wire_size_of_the_default_assistant_drops():
    tools = _definitions(sorted(DEFAULT_GA_BUILTIN_TOOL_IDS))
    gate = build_tool_gate(tools, enabled=True, min_tools=16, on_demand=builtin_on_demand_groups())
    assert gate is not None
    visible = gate.visible_tools()
    # Measured on 3 Nov 2026: 211 functions without gating; the core keeps it well under 80.
    assert len(tools) > 150
    assert len(visible) < 80, len(visible)


def test_without_on_demand_builtins_stay_always_on():
    tools = _definitions(["agent_builder"])
    always_on, groups = group_tools(tools)
    assert groups == {} and len(always_on) == len(tools)


def test_a_core_flagged_tool_is_never_gated():
    tool = ToolDefinition(name="schedule_work_item", description="x", source="builtin:agent_builder", core=True)
    always_on, groups = group_tools([tool], on_demand=builtin_on_demand_groups())
    assert always_on == [tool] and groups == {}
