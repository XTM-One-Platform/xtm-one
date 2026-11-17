"""A flow node's JSON field is stored as the value its text holds.

The builder edits a JSON field as text. Stored as text, a wired Structured
output schema never reached the agent (``wired_capabilities`` takes a dict),
a trigger filter never matched and a payload was sent empty.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from app.services.flow_graph import normalize_graph, parse_json_fields, validate_graph
from app.services.flow_runner import wired_capabilities

SCHEMA = {"type": "object", "properties": {"id": {"type": "string"}}}
SCHEMA_TEXT = '{\n  "type": "object",\n  "properties": {"id": {"type": "string"}}\n}'


def _node(node_id: str, kind: str, **config) -> dict:
    return {"id": node_id, "kind": kind, "position": {"x": 0, "y": 0}, "config": config}


def _agent_with_schema(schema) -> dict:
    return {
        "nodes": [
            _node("t", "trigger.manual"),
            _node("a", "agent", agent_id="00000000-0000-0000-0000-000000000001", prompt="Go"),
            _node("so", "capability.structured_output", schema=schema),
        ],
        "edges": [
            {"id": "e1", "source": "t", "source_handle": "out", "target": "a", "target_handle": "in", "kind": "data"},
            {"id": "e2", "source": "so", "source_handle": "provides", "target": "a", "target_handle": "output", "kind": "data"},
        ],
    }


def _migration():
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "fljsonfld0001_store_flow_json_fields_as_values.py"
    spec = importlib.util.spec_from_file_location("fljsonfld0001", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_schema_saved_as_text_reaches_the_agent():
    graph = normalize_graph(_agent_with_schema(SCHEMA_TEXT))

    assert wired_capabilities(graph, "a")["output_schema"] == SCHEMA


def test_the_text_of_every_json_field_becomes_its_value():
    config = parse_json_fields("trigger.event", {"events": ["x"], "filter": '{"status": ["open"]}'})
    assert config["filter"] == {"status": ["open"]}

    config = parse_json_fields("action.create_report", {"blocks": '[{"type": "text"}]', "header": "{}"})
    assert config == {"blocks": [{"type": "text"}], "header": {}}


def test_text_that_is_not_json_is_kept():
    for text in ('{"ids": {{n.fields.ids}}}', "{{n.fields}}", '"a string"', "", "   "):
        assert parse_json_fields("capability.structured_output", {"schema": text}) == {"schema": text}


def test_only_json_fields_are_parsed():
    config = {"agent_id": "x", "prompt": '{"a": 1}'}

    assert parse_json_fields("agent", config) == config
    assert parse_json_fields("no.such_kind", config) == config


def test_parsing_is_idempotent():
    once = parse_json_fields("capability.structured_output", {"schema": SCHEMA_TEXT})

    assert parse_json_fields("capability.structured_output", once) == once == {"schema": SCHEMA}


def _issues(graph, code):
    return [i for i in validate_graph(graph).issues if i.code == code]


def test_text_that_is_not_json_is_flagged_on_its_field():
    issues = _issues(_agent_with_schema('{"type": "object",'), "node.invalid_json")

    assert [(i.node_id, i.field, i.severity) for i in issues] == [("so", "schema", "warning")]


def test_json_text_and_values_are_not_flagged():
    assert not _issues(_agent_with_schema(SCHEMA_TEXT), "node.invalid_json")
    assert not _issues(_agent_with_schema(SCHEMA), "node.invalid_json")


def test_the_migration_converts_stored_text_and_leaves_the_rest():
    converted_graph = _migration().converted_graph
    stored = _agent_with_schema(SCHEMA_TEXT)

    graph = converted_graph(stored)

    assert graph is not None
    assert graph["nodes"][2]["config"] == {"schema": SCHEMA}
    assert graph["nodes"][:2] == stored["nodes"][:2]
    assert graph["edges"] == stored["edges"]
    assert converted_graph(graph) is None
    assert converted_graph(_agent_with_schema('{"broken": ')) is None
    assert converted_graph({}) is None
