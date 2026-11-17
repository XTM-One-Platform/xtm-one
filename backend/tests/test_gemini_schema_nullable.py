"""Gemini refuses a JSON Schema type union in a function declaration.

``google.genai.types.FunctionDeclaration`` takes one ``type`` per property:
``"type": ["string", "null"]`` fails validation, and one refused declaration
fails every request of the agent carrying it, so a failover onto the Gemini
leg could not serve the run. ``_clean_schema_for_gemini`` turns a nullable
union into the non-null type with ``nullable: true``, at every depth.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.llm.gemini_provider import _clean_schema_for_gemini

genai_types = pytest.importorskip("google.genai.types")


def _declare(name: str, description: str, schema: dict[str, Any]) -> object:
    """The provider's own conversion (the ``tools`` branch of chat / stream)."""
    cleaned = _clean_schema_for_gemini(schema)
    return genai_types.FunctionDeclaration(
        name=name,
        description=description,
        parameters=cleaned if cleaned.get("properties") else None,
    )


def _unions(node: object, path: str = "") -> list[str]:
    """Every place a cleaned schema still declares a type list or an anyOf."""
    found: list[str] = []
    if isinstance(node, dict):
        if isinstance(node.get("type"), list) or "anyOf" in node:
            found.append(path or "/")
        for key, value in node.items():
            found += _unions(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += _unions(value, f"{path}[{index}]")
    return found


def test_a_nullable_type_list_is_refused_by_gemini_as_written() -> None:
    """The failure the gap reports, pinned so the cleaning stays needed or goes."""
    with pytest.raises(ValueError):
        genai_types.FunctionDeclaration(
            name="t",
            description="d",
            parameters={"type": "object", "properties": {"due_at": {"type": ["string", "null"]}}},
        )


def test_a_nullable_type_list_becomes_the_type_with_nullable() -> None:
    cleaned = _clean_schema_for_gemini({"type": ["string", "null"], "description": "ISO-8601 or null"})
    assert cleaned == {"type": "string", "description": "ISO-8601 or null", "nullable": True}


def test_an_any_of_with_null_becomes_the_type_with_nullable() -> None:
    cleaned = _clean_schema_for_gemini(
        {"anyOf": [{"type": "null"}, {"type": "integer", "description": "inner"}], "description": "a count"}
    )
    assert cleaned == {"type": "integer", "description": "a count", "nullable": True}


def test_a_union_without_null_keeps_its_first_type_and_is_not_nullable() -> None:
    assert _clean_schema_for_gemini({"type": ["integer", "string"]}) == {"type": "integer"}
    assert _clean_schema_for_gemini({"anyOf": [{"type": "string"}, {"type": "number"}]}) == {"type": "string"}


def test_a_plain_type_is_unchanged() -> None:
    schema = {"type": "string", "description": "plain"}
    assert _clean_schema_for_gemini(schema) == schema


def test_nested_properties_and_array_items_are_normalised() -> None:
    schema = {
        "type": "object",
        "properties": {
            "due_at": {"type": ["string", "null"]},
            "owner": {
                "type": "object",
                "properties": {"email": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
            },
            "tags": {"type": "array", "items": {"type": ["string", "null"]}},
            "rows": {
                "type": ["array", "null"],
                "items": {"type": "object", "properties": {"score": {"type": ["integer", "null"]}}},
            },
        },
        "required": ["owner"],
    }
    cleaned = _clean_schema_for_gemini(schema)
    assert _unions(cleaned) == []
    props = cleaned["properties"]
    assert props["due_at"] == {"type": "string", "nullable": True}
    assert props["owner"]["properties"]["email"] == {"type": "string", "nullable": True}
    assert props["tags"]["items"] == {"type": "string", "nullable": True}
    assert props["rows"]["type"] == "array" and props["rows"]["nullable"] is True
    assert props["rows"]["items"]["properties"]["score"] == {"type": "integer", "nullable": True}
    _declare("t", "d", schema)


def test_the_source_schema_is_not_mutated() -> None:
    """The same definition feeds the anthropic and openai legs: they keep the union."""
    schema = {"type": "object", "properties": {"due_at": {"type": ["string", "null"]}}}
    _clean_schema_for_gemini(schema)
    assert schema == {"type": "object", "properties": {"due_at": {"type": ["string", "null"]}}}


def test_every_builtin_tool_declares_on_gemini() -> None:
    """Tripwire over the whole built-in catalog, disabled groups included."""
    from app.services.builtin_tool_catalog import build_builtin_tool_catalog

    declared = 0
    failures: list[str] = []
    for suite, definitions in build_builtin_tool_catalog(include_disabled=True).items():
        for definition in definitions:
            try:
                _declare(definition["name"], definition["description"], definition["input_schema"])
            except ValueError as exc:
                failures.append(f"{suite}.{definition['name']}: {str(exc).splitlines()[0]}")
            declared += 1
    assert declared > 100
    assert failures == [], "\n".join(failures)


def test_every_integration_tool_declares_on_gemini() -> None:
    """Tripwire over every provider's tool builder (a dummy token suffices)."""
    from app.api.v1.integration_catalog import build_dummy_token
    from app.services.integration_tools import PROVIDER_TOOLS
    from app.tools.base import CallableToolExecutor

    declared = 0
    failures: list[str] = []
    unbuilt: list[str] = []  # a builder that needs more than a token is covered by its own tests
    builders: dict[str, Any] = dict(PROVIDER_TOOLS)
    for provider, builder in builders.items():
        try:
            definitions = builder(build_dummy_token(provider), CallableToolExecutor(), refresh_fn=lambda: None)
        except Exception as exc:
            unbuilt.append(f"{provider}: {type(exc).__name__}")
            definitions = []
        for definition in definitions or []:
            try:
                _declare(definition.name, definition.description, definition.input_schema)
            except ValueError as exc:
                failures.append(f"{provider}.{definition.name}: {str(exc).splitlines()[0]}")
            declared += 1
    assert declared > 500, unbuilt
    assert failures == [], "\n".join(failures)
