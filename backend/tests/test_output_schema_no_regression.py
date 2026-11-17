"""What main validated is validated the same way.

Both agent output checks - the structured-output parse and the
``finalize_on_tool`` terminal check - and the tool-argument pre-validation
and coercion, over schemas main judged in full: local references, owner
patterns (linear, case-insensitive, non-ASCII text), ``patternProperties``
beside ``additionalProperties`` / ``unevaluatedProperties``, every ``$schema``
spelling, unknown keywords, and large STIX bundles. main checks with stock
``jsonschema``, so each case is compared with it; the file imports only what
main has and passes on main unchanged. Not here: what this tree changes on
purpose (a reference to another document is never retrieved, a regex never
runs unbounded), tested in ``test_output_schema_regex.py`` and
``test_schema_guard.py``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import jsonschema
import pytest

from app.services.structured_output import validate_and_parse
from app.tools.agent_loop import _terminal_tool_decision, coerce_tool_arguments, validate_tool_arguments
from app.tools.base import ToolResult
from tests.test_cti_stix_bundle_schema import _CUSTOM_SDOS, _MUST_REJECT, _STANDARD_OBJECTS, _bundle

_HARVESTER = Path(__file__).resolve().parents[1] / "app" / "agents" / "seed_data" / "agent_output_schema" / "cti-stix-harvester.json"
_DRAFT_2020 = "https://json-schema.org/draft/2020-12/schema"


@pytest.fixture(autouse=True)
def _every_regex_gets_its_verdict(monkeypatch):
    """Stock ``jsonschema`` has no regex budget, so a comparison with it holds
    only while the regex budget is not reached - and on a loaded runner the
    280-object bundle reached it. The budget is the subject of
    ``test_output_schema_regex.py``; here every constraint is judged."""
    try:
        from app.services import output_schema_validation
    except ImportError:  # an older tree has no budget
        return
    monkeypatch.setattr(output_schema_validation, "OUTPUT_SCHEMA_REGEX_BUDGET_SECONDS", 600.0)
    monkeypatch.setattr(output_schema_validation, "OUTPUT_SCHEMA_PATTERN_TIMEOUT_SECONDS", 60.0)


def _object(properties: dict, **extra) -> dict:
    return {"type": "object", "properties": properties, **extra}


def _parse(schema: dict, output):
    return validate_and_parse(json.dumps(output), schema)


def _stock_parse_errors(schema: dict, data) -> list[str]:
    # What the parse validates: the data after its own type coercion.
    errors = sorted(jsonschema.Draft7Validator(schema).iter_errors(data), key=lambda e: list(e.absolute_path))
    return [f"Validation error at {'.'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}" for e in errors]


def _terminal(schema: dict, output):
    return _terminal_tool_decision(
        tool_name="finish",
        result=ToolResult(tool_call_id="c", content=json.dumps({"valid": True, "bundle": output})),
        finalize_on_tool={"tool_name": "finish", "validate_output_schema": True},
        tool_name_map={},
        output_schema=schema,
    )


def _stock_first_error(schema: dict, output) -> str | None:
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    return next((error.message for error in validator_cls(schema).iter_errors(output)), None)


_STIX_ID = "^[a-z][a-z0-9-]+[a-z0-9]--[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_TIMESTAMP = "^[0-9]{4}-(0[1-9]|1[012])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\\.[0-9]+)?Z$"

# (schema, outputs): every output is judged by both checks.
_CASES = {
    "local-definitions-ref": (
        {"definitions": {"t": {"type": "string", "maxLength": 3}}, **_object({"q": {"$ref": "#/definitions/t"}})},
        [{"q": "ab"}, {"q": "abcdef"}, {"q": 1}],
    ),
    "local-defs-ref-chain": (
        {"$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"type": "integer", "minimum": 2}}, **_object({"n": {"$ref": "#/$defs/a"}})},
        [{"n": 3}, {"n": 1}, {"n": "x"}],
    ),
    "stix-id-and-timestamp-patterns": (
        _object({"id": {"type": "string", "pattern": _STIX_ID}, "created": {"type": "string", "pattern": _TIMESTAMP}}),
        [
            {"id": "indicator--8e2e2d2b-17d4-4cbf-938f-98ee46b3cd3f", "created": "2026-09-28T10:00:00.000Z"},
            {"id": "indicator--8e2e2d2b-17d4-4cbf-938f-98ee46b3cd3f", "created": "2026-13-28T10:00:00Z"},
            {"id": "Indicator--x", "created": "2026-09-28T10:00:00Z"},
        ],
    ),
    "case-insensitive-pattern": (
        _object({"tlp": {"type": "string", "pattern": "(?i)^tlp:(clear|green|amber|red)$"}}),
        [{"tlp": "TLP:AMBER"}, {"tlp": "tlp:white"}],
    ),
    "word-class-on-accented-text": (
        _object({"title": {"type": "string", "pattern": "^[\\w .,'-]+$"}}),
        [{"title": "Opération Sandworm, café"}, {"title": "rm -rf /"}],
    ),
    "pattern-properties-and-additional-false": (
        {"type": "object", "properties": {"id": {}}, "patternProperties": {"^x_": {"type": "string"}}, "additionalProperties": False},
        [{"id": 1, "x_a": "s"}, {"id": 1, "x_a": 2}, {"id": 1, "extra": True}],
    ),
    "pattern-properties-and-additional-schema": (
        {"type": "object", "patternProperties": {"^x_": {}}, "additionalProperties": {"type": "integer"}},
        [{"x_a": "s", "b": 2}, {"b": "no"}],
    ),
    "property-names-pattern": (
        {"type": "object", "propertyNames": {"pattern": "^[a-z_]+$"}},
        [{"ok_key": 1}, {"Bad-Key": 1}],
    ),
    "unevaluated-beside-same-node-pattern-properties": (
        {"$schema": _DRAFT_2020, "type": "object", "properties": {"id": {}}, "patternProperties": {"^x-": {"type": "string"}}, "unevaluatedProperties": False},
        [{"id": 1, "x-a": "s"}, {"id": 1, "x-a": "s", "y": 2}, {"x-a": 3}],
    ),
    "unevaluated-beside-unrelated-pattern-properties": (
        {"$schema": _DRAFT_2020, "type": "object", "properties": {"a": {}, "n": {"type": "object", "patternProperties": {"^x": {}}}}, "unevaluatedProperties": False},
        [{"a": 1}, {"a": 1, "zzz": 2}],
    ),
    "unevaluated-through-all-of-and-pattern-properties": (
        {"$schema": _DRAFT_2020, "type": "object", "allOf": [{"patternProperties": {"^meta_": {}}}], "properties": {"a": {}}, "unevaluatedProperties": {"type": "string"}},
        [{"a": 1, "meta_x": 1, "b": "s"}, {"a": 1, "b": 2}],
    ),
    "unevaluated-draft-2019": (
        {"$schema": "https://json-schema.org/draft/2019-09/schema", "type": "object", "patternProperties": {"^x-": {}}, "unevaluatedProperties": False},
        [{"x-a": 1}, {"x-a": 1, "y": 2}],
    ),
    "draft-07-dialect": (
        {"$schema": "http://json-schema.org/draft-07/schema#", **_object({"q": {"type": "integer"}}, required=["q"])},
        [{"q": 1}, {"q": "x"}, {}],
    ),
    "draft-2019-dialect": (
        {"$schema": "https://json-schema.org/draft/2019-09/schema", **_object({"q": {"type": "integer"}})},
        [{"q": 1}, {"q": "x"}],
    ),
    "unknown-dialect": (
        {"$schema": "https://example.com/my-dialect", **_object({"q": {"type": "integer"}})},
        [{"q": 1}, {"q": "x"}],
    ),
    "nested-dialect-with-a-pattern": (
        _object({"q": {"$schema": "http://json-schema.org/draft-07/schema#", "type": "string", "pattern": "^a"}}),
        [{"q": "ab"}, {"q": "b"}],
    ),
    "unknown-keywords": (
        _object({"q": {"type": "string", "x-custom": 1, "discriminator": {"propertyName": "k"}, "format": "not-a-format"}}),
        [{"q": "s"}, {"q": 1}],
    ),
    "structure-only": (
        _object({"items": {"type": "array", "items": {"enum": ["a", "b"]}, "minItems": 1}}, required=["items"]),
        [{"items": ["a"]}, {"items": []}, {"items": ["c"]}, {}],
    ),
}
_PAIRS = [pytest.param(schema, output, id=f"{name}-{index}") for name, (schema, outputs) in _CASES.items() for index, output in enumerate(outputs)]


@pytest.mark.parametrize(("schema", "output"), _PAIRS)
def test_the_structured_output_parse_reports_what_main_reported(schema, output):
    data, errors = _parse(schema, output)
    assert errors == _stock_parse_errors(schema, data)


@pytest.mark.parametrize(("schema", "output"), _PAIRS)
def test_the_finalize_check_accepts_and_refuses_what_main_did(schema, output):
    expected = _stock_first_error(schema, output)
    decision = _terminal(schema, output)
    if expected is None:
        assert decision.reason == "final_llm_call_skipped"
    else:
        assert decision.reason == "structured_output_validation_failed"
        assert expected in decision.detail


def _harvester_bundles() -> list:
    valid = _bundle(*([*_STANDARD_OBJECTS, *_CUSTOM_SDOS] * 40))
    late = copy.deepcopy(valid)
    # The last object of a large bundle, judged by the timestamp pattern.
    next(o for o in reversed(late["objects"]) if o.get("type") == "malware")["created"] = "2026-99-01T00:00:00Z"
    return [pytest.param(valid, False, id="valid-280-objects"), pytest.param(late, True, id="invalid-late-object")] + [
        pytest.param(_bundle(case.values[0]), True, id=f"must-reject-{index}") for index, case in enumerate(_MUST_REJECT)
    ]


@pytest.mark.parametrize(("bundle", "refused"), _harvester_bundles())
def test_the_harvester_schema_judges_bundles_as_main_did(bundle, refused):
    # The seeded patterns' text differs between the trees (two are linear here),
    # so each check is compared on where it refuses and whether it does.
    schema = json.loads(_HARVESTER.read_text(encoding="utf-8"))
    assert (_stock_first_error(schema, bundle) is not None) is refused
    data, errors = _parse(schema, bundle)
    assert sorted({e.split(":")[0] for e in errors}) == sorted({e.split(":")[0] for e in _stock_parse_errors(schema, data)})
    assert (_terminal(schema, bundle).reason == "final_llm_call_skipped") is not refused


# -- tool-argument pre-validation and coercion ------------------------

_PATTERN = {"type": "object", "properties": {"q": {"type": "string", "pattern": "^a"}}, "required": ["q"]}

# (schema, arguments, refused): main never pre-validates a pattern, leaves a
# call to the tool under a remote reference or a regex beside ``not``, and
# refuses structure as jsonschema does.
_TOOL_CASES = [
    pytest.param(_PATTERN, {"q": "b"}, False, id="pattern-never-pre-validated"),
    pytest.param(_PATTERN, {}, True, id="required"),
    pytest.param({"type": "object", "properties": {"n": {"type": "integer"}}}, {"n": "x"}, True, id="type"),
    pytest.param(
        {"type": "object", "patternProperties": {"^x_": {"type": "integer"}}, "additionalProperties": False},
        {"x_a": "s", "y": 1},
        False,
        id="additional-beside-pattern-properties",
    ),
    pytest.param(
        {"type": "object", "properties": {"a": {}}, "patternProperties": {"^x": {}}, "unevaluatedProperties": False},
        {"a": 1, "z": 2},
        False,
        id="unevaluated-beside-pattern-properties",
    ),
    pytest.param(
        {"type": "object", "properties": {"a": {"$ref": "#/$defs/n"}}, "$defs": {"n": {"type": "integer"}}},
        {"a": "x"},
        True,
        id="local-reference",
    ),
    pytest.param({"type": "object", "properties": {"a": {"not": {"pattern": "^x"}}}}, {"a": "xy"}, False, id="regex-beside-not"),
    pytest.param({"type": "object", "properties": {"a": {"$ref": "https://example.com/s.json"}}}, {"a": 1}, False, id="remote-reference"),
]


@pytest.mark.parametrize(("schema", "arguments", "refused"), _TOOL_CASES)
def test_tool_argument_pre_validation_answers_as_on_main(schema, arguments, refused):
    assert (validate_tool_arguments("tool", arguments, schema) is not None) is refused


# (union, value, coerced): a value an alternative's pattern admits is left as sent.
_UNIONS = [
    pytest.param({"anyOf": [{"pattern": "^[0-9]+$"}, {"type": "integer"}]}, "42", "42", id="pattern-admits"),
    pytest.param({"anyOf": [{"pattern": "^x"}, {"type": "integer"}]}, "42", 42, id="pattern-refuses"),
    pytest.param({"anyOf": [{"pattern": "(?i)^ID-[0-9]+$"}, {"type": "integer"}]}, "id-7", "id-7", id="case-insensitive"),
    pytest.param({"anyOf": [{"pattern": r"^\w+$"}, {"type": "integer"}]}, "café", "café", id="word-class-accented"),
    pytest.param({"anyOf": [{"pattern": r"^\w+$"}, {"type": "integer"}]}, "42", "42", id="word-class-digits"),
    pytest.param({"anyOf": [{"pattern": "^T"}, {"type": "boolean"}]}, "true", True, id="boolean"),
    pytest.param({"anyOf": [{"type": "string", "pattern": "^[a-z]+$"}, {"type": "integer"}]}, "5", "5", id="string-alternative"),
    pytest.param({"anyOf": [{"type": "integer"}, {"type": "null"}]}, "42", 42, id="no-pattern"),
]


@pytest.mark.parametrize(("union", "value", "coerced"), _UNIONS)
def test_tool_argument_coercion_of_a_union_answers_as_on_main(union, value, coerced):
    schema = {"type": "object", "properties": {"u": union}}
    assert coerce_tool_arguments({"u": value}, schema) == {"u": coerced}
