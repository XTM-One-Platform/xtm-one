"""Gemini refuses an array without ``items`` (#145).

Google answers ``...items: missing field`` and fails the whole request, so one
untyped array in any tool sends the agent's every turn to the fallback. An
empty ``items`` is accepted (checked live) and keeps "any element".
"""

from __future__ import annotations

from typing import Any

import pytest

from app.llm.gemini_provider import _clean_schema_for_gemini, gemini_wire_tools
from app.llm.openai_compat import openai_function_tools
from app.tools.base import ToolDefinition

genai_types = pytest.importorskip("google.genai.types")


def _untyped_arrays(node: Any, path: str = "") -> list[str]:
    """Every array of a cleaned schema whose ``items`` is not a schema."""
    found: list[str] = []
    if not isinstance(node, dict):
        return found
    if node.get("type") == "array" and not isinstance(node.get("items"), dict):
        found.append(path or "/")
    for name, prop in (node.get("properties") or {}).items():
        found += _untyped_arrays(prop, f"{path}.{name}")
    found += _untyped_arrays(node.get("items"), f"{path}[]")
    return found


def test_an_array_without_items_gets_empty_items() -> None:
    cleaned = _clean_schema_for_gemini({"type": "array", "description": "First set."})
    assert cleaned == {"type": "array", "items": {}, "description": "First set."}


def test_an_array_or_string_union_gets_empty_items() -> None:
    """``organization_context_categories``: the union keeps ``array``."""
    cleaned = _clean_schema_for_gemini({"type": ["array", "string"]})
    assert cleaned == {"type": "array", "items": {}}


def test_a_nullable_array_without_items_gets_empty_items() -> None:
    cleaned = _clean_schema_for_gemini({"type": ["array", "null"]})
    assert cleaned == {"type": "array", "items": {}, "nullable": True}


def test_a_nested_array_without_items_gets_empty_items() -> None:
    """``generate_report``'s ``blocks[].items``."""
    schema = {
        "type": "object",
        "properties": {
            "blocks": {
                "type": "array",
                "items": {"type": "object", "properties": {"items": {"type": "array"}}},
            }
        },
    }
    assert _untyped_arrays(_clean_schema_for_gemini(schema)) == []


def test_typed_items_are_kept() -> None:
    cleaned = _clean_schema_for_gemini({"type": "array", "items": {"type": "string", "default": "x"}})
    assert cleaned == {"type": "array", "items": {"type": "string"}}


def test_the_gateway_sends_gemini_empty_items() -> None:
    tool = ToolDefinition(
        name="compute",
        description="d",
        input_schema={"type": "object", "properties": {"set_a": {"type": "array"}}},
    )
    (wire,) = openai_function_tools([tool], "gemini-4-argon", upstream="gemini")
    assert wire["function"]["parameters"]["properties"]["set_a"] == {"type": "array", "items": {}}


def test_the_sdk_keeps_empty_items_on_the_wire() -> None:
    """The direct provider: ``exclude_none`` must not drop the empty schema."""
    ((name, description, parameters),) = gemini_wire_tools(
        [
            ToolDefinition(
                name="t", description="d", input_schema={"type": "object", "properties": {"v": {"type": "array"}}}
            )
        ]
    )
    declaration = genai_types.FunctionDeclaration(name=name, description=description, parameters=parameters)
    dumped = declaration.model_dump(mode="json", exclude_none=True)
    assert dumped["parameters"]["properties"]["v"]["items"] == {}


def test_every_builtin_tool_declares_typed_arrays_on_gemini() -> None:
    """Tripwire: no builtin tool reaches Gemini with an array Google refuses."""
    from app.services.builtin_tool_catalog import build_builtin_tool_catalog

    catalog = build_builtin_tool_catalog(include_disabled=True)
    tools = [tool for group in catalog.values() for tool in group if tool.get("input_schema")]
    assert tools
    refused = {
        tool["name"]: paths
        for tool in tools
        if (paths := _untyped_arrays(_clean_schema_for_gemini(tool["input_schema"])))
    }
    assert refused == {}
