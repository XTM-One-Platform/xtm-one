"""A built-in function outside an agent's ``builtin_allowed_tools`` is named, never hidden (#12192).

The allowlist keeps its least-privilege semantics: a function shipped in a
scoped group after the list was written is not granted. But the agent hears
why: ``load_tools`` search and reveal, and a refused call, say the function
exists outside ``builtin_allowed_tools.<group>``; ``get_agent`` lists the
group's functions outside the list.
"""

from __future__ import annotations

import pytest

from app.services import builtin_allowed_tools as bat
from app.services.builtin_tools import filter_builtin_tools
from app.services.builtin_tools.filtering import (
    builtin_tools_outside_scope,
    describe_outside_builtin_scope,
)
from app.tools.base import (
    TOOL_NOT_AVAILABLE_SENTINEL,
    CallableToolExecutor,
    CompositeToolExecutor,
    ScopedToolExecutor,
    ToolCall,
    ToolDefinition,
)
from app.tools.tool_gating import build_tool_gate

_ALLOWED = {"platform": ["list_agents", "get_agent"]}
_FIXTURE_TOOL = "fixture_new_platform_tool"


def _defs() -> list[ToolDefinition]:
    names = ["list_agents", "get_agent", _FIXTURE_TOOL, *(f"platform_tool_{i}" for i in range(12))]
    defs = [ToolDefinition(name=n, description=f"{n} description", source="builtin:platform") for n in names]
    defs.append(ToolDefinition(name="web_fetch", description="fetch a page", source="builtin:web"))
    # An integration group, so the progressive tool gate has something to gate.
    defs.extend(
        ToolDefinition(name=f"github_tool_{i}", description="a github tool", source="integration:github")
        for i in range(4)
    )
    return defs


async def _ran(**_kwargs: object) -> str:
    return "EXECUTED-OK"


def _scoped_run() -> tuple[CompositeToolExecutor, list[ToolDefinition]]:
    """What the chat and assignment paths wire: register all, record the outside, filter."""
    composite = CompositeToolExecutor()
    executor = CallableToolExecutor()
    defs = _defs()
    for d in defs:
        executor.register(d.name, _ran)
    composite.add_tools(defs, executor)
    for name, group in builtin_tools_outside_scope(composite.all_tools, _ALLOWED).items():
        composite.withhold([name], describe_outside_builtin_scope(group), registered=True)
    return composite, filter_builtin_tools(composite.all_tools, _ALLOWED)


def test_outside_scope_names_every_dropped_function_with_its_group() -> None:
    outside = builtin_tools_outside_scope(_defs(), _ALLOWED)
    assert _FIXTURE_TOOL in outside and outside[_FIXTURE_TOOL] == "platform"
    assert "list_agents" not in outside and "get_agent" not in outside
    assert "web_fetch" not in outside  # an unscoped group keeps everything
    assert builtin_tools_outside_scope(_defs(), None) == {}


def test_filter_is_unchanged_and_grants_nothing_new() -> None:
    _composite, effective = _scoped_run()
    names = {d.name for d in effective}
    assert {"list_agents", "get_agent", "web_fetch"} <= names
    assert _FIXTURE_TOOL not in names
    assert not any(n.startswith("platform_tool_") for n in names)


def test_load_tools_search_names_the_function_as_outside_the_allowlist() -> None:
    composite, effective = _scoped_run()
    gate = build_tool_gate(effective, enabled=True, min_tools=1, withheld=composite.withheld)
    assert gate is not None
    text = gate.describe_search(gate.search("fixture_new_platform"))
    assert _FIXTURE_TOOL in text
    assert "builtin_allowed_tools.platform" in text


def test_load_tools_by_name_names_it_never_unknown() -> None:
    composite, effective = _scoped_run()
    gate = build_tool_gate(effective, enabled=True, min_tools=1, withheld=composite.withheld)
    assert gate is not None
    reveal = gate.reveal([], [_FIXTURE_TOOL])
    assert _FIXTURE_TOOL in reveal.withheld_tools
    assert _FIXTURE_TOOL not in reveal.unknown_tools
    assert "builtin_allowed_tools.platform" in gate.describe_reveal(reveal)


async def test_a_call_is_refused_with_the_allowlist_named_and_never_runs() -> None:
    composite, effective = _scoped_run()
    scoped = ScopedToolExecutor(composite, effective)
    result = await scoped.execute_tool(ToolCall(id="c1", name=_FIXTURE_TOOL, arguments={}))
    assert result.is_error
    assert TOOL_NOT_AVAILABLE_SENTINEL in result.content
    assert "builtin_allowed_tools.platform" in result.content
    assert "EXECUTED-OK" not in result.content


async def test_an_allowed_function_still_runs() -> None:
    composite, effective = _scoped_run()
    scoped = ScopedToolExecutor(composite, effective)
    result = await scoped.execute_tool(ToolCall(id="c2", name="get_agent", arguments={}))
    assert not result.is_error
    assert "EXECUTED-OK" in result.content


def test_withhold_without_registered_still_skips_a_registered_name() -> None:
    composite, _effective = _scoped_run()
    composite.withhold(["get_agent"], "some reason")
    assert "get_agent" not in composite.withheld


def test_get_agent_lists_the_functions_outside_the_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bat,
        "builtin_group_functions",
        lambda group: ("list_agents", "get_agent", _FIXTURE_TOOL) if group == "platform" else (),
    )
    assert bat.builtin_functions_outside_allowlist(_ALLOWED) == {"platform": [_FIXTURE_TOOL]}
    assert bat.builtin_functions_outside_allowlist({"platform": None}) == {}
    assert bat.builtin_functions_outside_allowlist(None) == {}


@pytest.mark.parametrize("value", [{"platform": ["get_agent", "get_agent"]}, {"agent_builder": ["get_agent"]}])
def test_outside_allowlist_reads_the_stored_forms(monkeypatch: pytest.MonkeyPatch, value: dict[str, list[str]]) -> None:
    monkeypatch.setattr(
        bat,
        "builtin_group_functions",
        lambda group: ("list_agents", "get_agent") if group == "platform" else (),
    )
    assert bat.builtin_functions_outside_allowlist(value) == {"platform": ["list_agents"]}
