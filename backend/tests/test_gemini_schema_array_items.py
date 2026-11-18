"""Gemini refuses an array without ``items`` (#145).

Google answers ``...items: missing field`` and fails the whole request, so one
untyped array in any tool sends the agent's every turn to the fallback. An
empty ``items`` is accepted (checked live) and keeps "any element".
"""

from __future__ import annotations

from typing import Any

import pytest

from app.llm.gemini_provider import _clean_schema_for_gemini, _gemini_tool
from app.llm.openai_compat import openai_function_tools
from app.tools.base import ToolDefinition

genai_types = pytest.importorskip("google.genai.types")


def _refused_arrays(node: Any, path: str = "") -> list[str]:
    """Every array of a cleaned schema without an ``items`` schema, which Google refuses."""
    found: list[str] = []
    if not isinstance(node, dict):
        return found
    items = node.get("items")
    if node.get("type") == "array" and not isinstance(items, dict):
        found.append(path or "/")
    for name, prop in (node.get("properties") or {}).items():
        found += _refused_arrays(prop, f"{path}.{name}")
    found += _refused_arrays(items, f"{path}[]")
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
    assert _refused_arrays(_clean_schema_for_gemini(schema)) == []


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
    """The direct provider, serialized as the SDK's request path does: the empty schema stays."""
    from google.genai import _common, models

    tool = _gemini_tool(
        [
            ToolDefinition(
                name="t", description="d", input_schema={"type": "object", "properties": {"v": {"type": "array"}}}
            )
        ]
    )
    wire = _common.convert_to_dict(models._Tool_to_mldev(tool))
    assert wire["functionDeclarations"][0]["parameters"]["properties"]["v"] == {"items": {}, "type": "ARRAY"}


def test_no_builtin_tool_declares_an_array_gemini_refuses() -> None:
    """Tripwire: no builtin tool reaches Gemini with an array Google refuses."""
    from app.services.builtin_tool_catalog import build_builtin_tool_catalog

    catalog = build_builtin_tool_catalog(include_disabled=True)
    tools = [tool for group in catalog.values() for tool in group if tool.get("input_schema")]
    assert tools
    refused = {
        tool["name"]: paths
        for tool in tools
        if (paths := _refused_arrays(_clean_schema_for_gemini(tool["input_schema"])))
    }
    assert refused == {}
