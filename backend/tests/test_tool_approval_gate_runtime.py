"""Execution-time enforcement of per-tool approval.

The gate lives in ``CompositeToolExecutor`` rather than in ``run_agent_loop``
because that is the only layer that sees every dispatch — several endpoints
execute tools directly against a composite executor without going through the
loop, and a gate placed in the loop would silently miss them.

It also sits AFTER name resolution, so a reordered or aliased tool name cannot
route around gating. That property is the point of ``test_aliased_name_*``
below: the reordered-name recovery is exactly the mechanism an attacker
or a confused model would otherwise use to reach an ungated dispatch.

These tests pin behaviour end to end through the real executor, not through a
mocked one — the whole risk here is a dispatch path that bypasses the check.
"""

from __future__ import annotations

import pytest

from app.tools.approval import evaluate_gate, fingerprint
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    HumanInputRequestedError,
    ToolCall,
    ToolDefinition,
)

SCHEMA = {"type": "object", "properties": {"target": {"type": "string"}}}
executed: list[str] = []


def _make_composite(*names: str) -> CompositeToolExecutor:
    """A composite executor whose tools record that they ran."""
    executed.clear()
    callable_exec = CallableToolExecutor()
    definitions = []
    for name in names:

        async def _handler(_name=name, **kwargs):
            executed.append(_name)
            return f"{_name} ran"

        callable_exec.register(name, _handler)
        definitions.append(
            ToolDefinition(
                name=name,
                description=f"fake tool {name}",
                input_schema=SCHEMA,
                source="test",
            )
        )
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)
    return composite


def _install_gate(composite: CompositeToolExecutor, whitelist: frozenset[str]) -> None:
    """Wire the real decision function in, the way the engine will."""

    def gate(call: ToolCall) -> None:
        decision = evaluate_gate(
            call.name,
            hitl_enabled=True,
            whitelist=whitelist,
            active_exceptions={},
            current_fingerprint=fingerprint(SCHEMA),
        )
        if decision.action == "pause":
            raise HumanInputRequestedError(
                question=f"Approve {call.name}?",
                tool_call_id=call.id,
                conversation=[],
                proposals=[{"tool_call_id": call.id, "tool_name": call.name}],
            )

    composite.set_approval_gate(gate)


async def test_gated_tool_pauses_before_execution():
    """The core guarantee: no side effect happens before the human decides."""
    composite = _make_composite("delete_entity")
    _install_gate(composite, whitelist=frozenset())

    with pytest.raises(HumanInputRequestedError):
        await composite.execute_tool(ToolCall(id="c1", name="delete_entity", arguments={}))

    assert executed == [], "tool ran despite being gated"


async def test_whitelisted_tool_executes_without_pausing():
    composite = _make_composite("search_entities")
    _install_gate(composite, whitelist=frozenset({"search_entities"}))

    result = await composite.execute_tool(
        ToolCall(id="c1", name="search_entities", arguments={})
    )

    assert executed == ["search_entities"]
    assert result.is_error is False


async def test_mixed_run_gates_only_the_non_whitelisted_tool():
    """SC-008: a read-heavy run must not prompt for its reads."""
    composite = _make_composite("search_entities", "delete_entity")
    _install_gate(composite, whitelist=frozenset({"search_entities"}))

    await composite.execute_tool(ToolCall(id="c1", name="search_entities", arguments={}))
    with pytest.raises(HumanInputRequestedError):
        await composite.execute_tool(
            ToolCall(id="c2", name="delete_entity", arguments={})
        )

    assert executed == ["search_entities"]


async def test_aliased_name_cannot_route_around_the_gate():
    """A reordered name resolves to the canonical tool — and still gates.

    Without gating placed after name resolution, ``entity_delete`` would miss a
    gate keyed on ``delete_entity`` and then be happily dispatched to it by the
    reordered-name recovery.
    """
    composite = _make_composite("delete_entity")
    _install_gate(composite, whitelist=frozenset())

    with pytest.raises(HumanInputRequestedError):
        await composite.execute_tool(
            ToolCall(id="c1", name="entity_delete", arguments={})
        )

    assert executed == []


async def test_aliased_name_of_a_whitelisted_tool_still_runs():
    """The converse: name recovery must keep working for allowed tools."""
    composite = _make_composite("search_entities")
    _install_gate(composite, whitelist=frozenset({"search_entities"}))

    await composite.execute_tool(ToolCall(id="c1", name="entities_search", arguments={}))

    assert executed == ["search_entities"]


async def test_gate_sees_the_canonical_name_not_the_supplied_one():
    seen: list[str] = []
    composite = _make_composite("delete_entity")
    composite.set_approval_gate(lambda call: seen.append(call.name))

    await composite.execute_tool(ToolCall(id="c1", name="entity_delete", arguments={}))

    assert seen == ["delete_entity"]


async def test_no_gate_installed_means_no_gating():
    """Paths that have not opted in are unaffected."""
    composite = _make_composite("delete_entity")

    result = await composite.execute_tool(
        ToolCall(id="c1", name="delete_entity", arguments={})
    )

    assert executed == ["delete_entity"]
    assert result.is_error is False


async def test_unknown_tool_still_reports_not_found_rather_than_gating():
    """An unresolvable name is a tool error, not an approval request."""
    composite = _make_composite("delete_entity")
    _install_gate(composite, whitelist=frozenset())

    result = await composite.execute_tool(
        ToolCall(id="c1", name="totally_unknown_tool", arguments={})
    )

    assert result.is_error is True
    assert "No executor registered" in result.content


async def test_resolve_canonical_name_matches_dispatch():
    """The pre-screen and the dispatcher must agree on the canonical name.

    If these ever diverge, the batch pre-screen would gate one name while the
    executor ran another.
    """
    composite = _make_composite("delete_entity")

    assert composite.resolve_canonical_name("delete_entity") == "delete_entity"
    assert composite.resolve_canonical_name("entity_delete") == "delete_entity"
    assert composite.resolve_canonical_name("no_such_tool") is None


async def test_get_tool_definition_exposes_schema_for_the_gate():
    composite = _make_composite("delete_entity")

    definition = composite.get_tool_definition("delete_entity")

    assert definition is not None
    assert definition.input_schema == SCHEMA
    assert composite.get_tool_definition("no_such_tool") is None
