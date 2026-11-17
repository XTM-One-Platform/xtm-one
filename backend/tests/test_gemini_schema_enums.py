"""Gemini refuses a non-string enum in a function declaration.

``google.genai.types.FunctionDeclaration`` accepts only string ``enum``
values; one refused declaration fails every request of the agent carrying
the tool. ``_clean_schema_for_gemini`` drops such an enum and says its values
in the description, so the declaration builds and the property keeps its type.
"""

from __future__ import annotations

import uuid

import pytest

from app.llm.gemini_provider import _clean_schema_for_gemini

genai_types = pytest.importorskip("google.genai.types")


def _declare(name: str, description: str, schema: dict) -> object:
    """The provider's own conversion (the ``tools`` branch of chat / stream)."""
    cleaned = _clean_schema_for_gemini(schema)
    return genai_types.FunctionDeclaration(
        name=name,
        description=description,
        parameters=cleaned if cleaned.get("properties") else None,
    )


def _non_string_enums(node: object, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        enum = node.get("enum")
        if isinstance(enum, list) and any(not isinstance(value, str) for value in enum):
            found.append(path)
        for key, value in node.items():
            found += _non_string_enums(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += _non_string_enums(value, f"{path}[{index}]")
    return found


def test_an_integer_enum_is_refused_by_gemini_as_written():
    """The failure the issue reports, pinned so the cleaning stays needed or goes."""
    with pytest.raises(ValueError, match="valid string"):
        genai_types.FunctionDeclaration(
            name="t",
            description="d",
            parameters={"type": "object", "properties": {"columns": {"type": "integer", "enum": [1, 2]}}},
        )


def test_an_integer_enum_becomes_its_values_in_the_description():
    schema = {
        "type": "object",
        "properties": {
            "columns": {"type": "integer", "enum": [1, 2], "description": "print in 1 or 2 columns"},
            "level": {"enum": [0, 1, 2], "type": "integer"},
            "zoom": {"type": "number", "enum": [1, 1.5, 2]},
        },
    }
    cleaned = _clean_schema_for_gemini(schema)
    columns = cleaned["properties"]["columns"]
    assert columns == {"type": "integer", "description": "print in 1 or 2 columns One of: 1, 2."}
    assert cleaned["properties"]["level"] == {"type": "integer", "description": "One of: 0, 1, 2."}
    assert cleaned["properties"]["zoom"] == {"type": "number", "description": "One of: 1, 1.5, 2."}
    _declare("t", "d", schema)


def test_a_string_enum_is_kept_and_its_empty_value_dropped():
    cleaned = _clean_schema_for_gemini({"type": "string", "enum": ["a", "", "b"], "description": "pick"})
    assert cleaned == {"type": "string", "enum": ["a", "b"], "description": "pick"}


def test_a_nested_integer_enum_in_an_array_of_objects_is_cleaned():
    schema = {
        "type": "object",
        "properties": {
            "blocks": {
                "type": "array",
                "items": {"type": "object", "properties": {"columns": {"type": "integer", "enum": [1, 2]}}},
            }
        },
    }
    cleaned = _clean_schema_for_gemini(schema)
    assert _non_string_enums(cleaned) == []
    _declare("t", "d", schema)


@pytest.mark.parametrize("build", ["report", "presentation"])
def test_every_document_and_deck_tool_declares_on_gemini(build):
    """Tripwire: the deck and document tools of the issue go through the real
    conversion, so a schema the SDK refuses cannot ship silently."""
    from app.services.builtin_tools import build_presentation_tools, build_report_tools

    if build == "report":
        # The export tool is offered on every turn: the builder reads no request.
        defs, _executor = build_report_tools(
            db=None,  # type: ignore[arg-type]
            user_id=str(uuid.uuid4()),
            conversation_id=None,
        )
    else:
        # The request still picks the template.
        defs, _executor = build_presentation_tools(
            db=None,  # type: ignore[arg-type]
            user_id=str(uuid.uuid4()),
            conversation_id=None,
            request_text="export it as a file",
        )
    assert defs
    declarations = [_declare(d.name, d.description, d.input_schema) for d in defs]
    tool = genai_types.Tool(function_declarations=declarations)
    assert len(tool.function_declarations) == len(defs)
    for definition in defs:
        assert _non_string_enums(_clean_schema_for_gemini(definition.input_schema)) == [], definition.name
