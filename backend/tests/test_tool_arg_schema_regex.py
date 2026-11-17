"""Tool-argument validation never evaluates a schema's own regexes.

Tool schemas come from MCP servers, custom tools and imported bundles. Their
``pattern`` / ``patternProperties`` are left to the tool; the platform still
enforces structure (required, types, enums) and still shows the model the
schema with its patterns. Dropping a regex must never refuse a call the
schema accepts, and validation never retrieves a referenced document. An MCP
server's ``outputSchema`` is not checked at all: the platform never reads the
``structuredContent`` it describes.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import subprocess
import sys
import textwrap
import urllib.parse
import urllib.request
from pathlib import Path

import anyio
import jsonschema
import pytest
from mcp import types as mcp_types

from app.tools import agent_loop
from app.tools.agent_loop import (
    _already_valid,
    coerce_tool_arguments,
    validate_tool_arguments,
)
from app.tools.mcp_client import ClientSession

# A pattern the argument below can never match: a refusal would prove the
# pattern was evaluated.
_NEVER_MATCHES = {"type": "string", "pattern": "^a$"}

# ``re`` needs minutes to refuse this input against this pattern.
_CATASTROPHIC = "^(a+)+$"
_LONG = "a" * 40 + "!"
# Exponential for ``re`` and for the bounded engine alike.
_BUDGET_BREAKER = "^(a|aa)+$"
_LONGER = "a" * 60 + "!"

_BACKEND_DIR = Path(__file__).resolve().parents[1]


def _object(properties: dict) -> dict:
    return {"type": "object", "properties": properties}


def _referenced_under_nested_lists(pattern: str) -> dict:
    hidden: object = {"pattern": pattern}
    for _ in range(70):
        hidden = [hidden]
    return {"type": "object", "x-ext": hidden, "properties": {"q": {"$ref": "#/x-ext" + "/0" * 70}}}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    # A regression must fail here, never reach the network.
    def _refuse(*args, **kwargs):
        raise AssertionError("argument validation tried to retrieve a schema")

    monkeypatch.setattr(urllib.request, "urlopen", _refuse)


def test_a_schema_pattern_is_not_evaluated():
    schema = {"type": "object", "properties": {"q": _NEVER_MATCHES}}
    assert validate_tool_arguments("t", {"q": "b"}, schema) is None


def test_structure_is_still_enforced_and_the_echo_keeps_the_pattern():
    schema = {"type": "object", "properties": {"q": _NEVER_MATCHES}, "required": ["q"]}
    feedback = validate_tool_arguments("t", {}, schema)
    assert feedback is not None
    assert "'q' is a required property" in feedback
    assert "pattern" in feedback  # the model still sees the constraint
    wrong_type = validate_tool_arguments("t", {"q": 3}, schema)
    assert wrong_type is not None and "is not of type 'string'" in wrong_type


def test_the_schema_given_to_the_model_is_not_modified():
    schema = {"type": "object", "properties": {"q": _NEVER_MATCHES}}
    before = copy.deepcopy(schema)
    validate_tool_arguments("t", {"q": "b"}, schema)
    assert schema == before


def test_dropping_pattern_properties_does_not_refuse_what_they_admitted():
    schema = {
        "type": "object",
        "patternProperties": {"^x_": {"type": "string"}},
        "additionalProperties": False,
    }
    assert validate_tool_arguments("t", {"x_a": "v"}, schema) is None


def test_an_empty_pattern_properties_keeps_additional_properties():
    schema = {"type": "object", "patternProperties": {}, "additionalProperties": False}
    assert validate_tool_arguments("t", {"x": 1}, schema) is not None


def test_a_property_names_pattern_is_not_evaluated():
    schema = {"type": "object", "propertyNames": {"pattern": "^a"}}
    assert validate_tool_arguments("t", {"b": 1}, schema) is None


def test_a_property_named_pattern_is_kept():
    schema = {
        "type": "object",
        "properties": {"pattern": {"type": "string"}},
        "required": ["pattern"],
    }
    missing = validate_tool_arguments("t", {}, schema)
    assert missing is not None and "'pattern' is a required property" in missing
    assert validate_tool_arguments("t", {"pattern": "x"}, schema) is None


def test_instance_data_that_looks_like_a_schema_is_compared_as_it_is():
    schema = _object({"q": {"enum": [{"pattern": "a"}]}})
    assert validate_tool_arguments("t", {"q": {"pattern": "a"}}, schema) is None
    assert validate_tool_arguments("t", {"q": {"pattern": "b"}}, schema) is not None


def test_a_very_deep_schema_still_reports_top_level_errors():
    node: dict = {"type": "string"}
    for _ in range(80):
        node = {"type": "object", "properties": {"x": node}, "required": ["x"]}
    feedback = validate_tool_arguments("t", {}, node)
    assert feedback is not None and "'x' is a required property" in feedback


def test_the_union_check_keeps_its_verdict_patterns_included():
    union = {"anyOf": [_NEVER_MATCHES, {"type": "integer"}]}
    assert _already_valid("b", union) is False
    assert _already_valid("a", union) is True


def _deep_integer_chain() -> dict:
    node: dict = {"type": "integer"}
    for _ in range(70):
        node = {"allOf": [node]}
    return node


@pytest.mark.parametrize(
    ("schema", "args"),
    [
        pytest.param(_object({"q": {"type": "string", "not": {"pattern": "^a$"}}}), {"q": "b"}, id="not"),
        pytest.param(
            _object({"q": {"oneOf": [{"type": "string", "pattern": "^a"}, {"type": "string", "pattern": "^b"}]}}),
            {"q": "b"},
            id="oneOf",
        ),
        pytest.param(
            {
                **_object({"q": {"type": "string"}}),
                "if": {"properties": {"q": {"pattern": "^z"}}},
                "then": {"required": ["z"]},
            },
            {"q": "b"},
            id="if",
        ),
        pytest.param(
            _object({"q": {"type": "array", "contains": {"pattern": "^a"}, "maxContains": 1}}),
            {"q": ["a", "b"]},
            id="maxContains",
        ),
        pytest.param(
            {"type": "object", "patternProperties": {"^x_": {"type": "string"}}, "unevaluatedProperties": False},
            {"x_a": "v"},
            id="unevaluatedProperties",
        ),
        pytest.param(
            {
                "type": "object",
                "allOf": [{"patternProperties": {"^x_": {"type": "string"}}}],
                "unevaluatedProperties": False,
            },
            {"x_a": "v"},
            id="unevaluatedProperties-through-allOf",
        ),
        pytest.param(
            {**_object({"q": {"not": {"$ref": "#/$defs/p"}}}), "$defs": {"p": {"pattern": "^a$"}}},
            {"q": "b"},
            id="not-through-ref",
        ),
        pytest.param(_object({"q": {"not": _deep_integer_chain()}}), {"q": "b"}, id="not-over-a-deep-subschema"),
    ],
)
def test_dropping_a_regex_never_refuses_a_valid_call(schema, args):
    assert validate_tool_arguments("t", args, schema) is None


@pytest.mark.parametrize(
    "schema",
    [
        pytest.param({**_object({"q": {"$ref": "#/default"}}), "default": {"pattern": "^a$"}}, id="default"),
        pytest.param(
            {**_object({"q": {"$ref": "#/$defs/e/enum/0"}}), "$defs": {"e": {"enum": [{"pattern": "^a$"}]}}},
            id="enum",
        ),
        pytest.param(
            {"type": "object", "properties": {"q": {"$ref": "#/properties"}, "not": {"pattern": "^a$"}}},
            id="map-of-names",
        ),
        pytest.param(
            {
                "$schema": "http://json-schema.org/draft-03/schema#",
                "type": "object",
                "dependencies": {"pattern": "^a$"},
                "properties": {"q": {"$ref": "#/dependencies"}},
            },
            id="draft-03-dependencies",
        ),
        pytest.param(_referenced_under_nested_lists("^a$"), id="deeply-nested-lists"),
    ],
)
def test_a_pattern_reached_only_through_a_reference_is_not_evaluated(schema):
    assert validate_tool_arguments("t", {"q": "b"}, schema) is None


@pytest.mark.parametrize(
    ("schema", "args", "coerced"),
    [
        pytest.param(
            _object({"n": {"type": "integer"}, "q": {"type": "string", "not": {"pattern": "^x"}}}),
            {"n": "5", "q": "b"},
            {"n": 5, "q": "b"},
            id="property-beside-a-regex-under-not",
        ),
        pytest.param(
            _object({"n": {"type": "integer"}, "q": {"oneOf": [{"pattern": "^0"}, {"type": "integer"}]}}),
            {"n": "5", "q": "5"},
            {"n": 5, "q": 5},
            id="oneOf-with-a-pattern-it-refuses",
        ),
        pytest.param(
            _object({"q": {"oneOf": [{"pattern": "^0"}, {"type": "integer"}]}}),
            {"q": "007"},
            {"q": "007"},
            id="oneOf-with-a-pattern-it-admits",
        ),
        pytest.param(
            _object({"q": {"anyOf": [{"pattern": "^0"}, {"type": "integer"}]}}),
            {"q": "5"},
            {"q": 5},
            id="anyOf-with-a-pattern-it-refuses",
        ),
        pytest.param(
            _object({"id": {"anyOf": [{"enum": ["001"]}, {"type": "integer"}], "not": {"pattern": "^x$"}}}),
            {"id": "001"},
            {"id": "001"},
            id="valid-union-value-beside-a-regex-under-not",
        ),
        pytest.param(
            {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "$ref": "https://example.com/tool.json",
                "properties": {"id": {"type": "integer"}},
            },
            {"id": "001"},
            {"id": 1},
            id="draft-07-sibling-of-a-remote-reference",
        ),
        pytest.param(
            {
                **_object({"m": {"anyOf": [{"$ref": "#/$defs/M"}, {"type": "null"}]}}),
                "$defs": {"M": {"type": "object"}},
            },
            {"m": "null"},
            {"m": None},
            id="pydantic-optional-model",
        ),
    ],
)
def test_coercion_is_what_it_was_before_this_change(schema, args, coerced):
    # Expected values are what the coercion gave before the validator was
    # bounded, computed with the unbounded validator it used then.
    assert coerce_tool_arguments(args, schema) == coerced


def test_a_union_value_behind_a_remote_reference_is_left_as_sent():
    # The only verdict that needed the remote document: it is never fetched.
    args = {"id": "001"}
    schema = _object({"id": {"anyOf": [{"$ref": "https://example.com/id.json"}, {"type": "integer"}]}})
    assert coerce_tool_arguments(args, schema) is args


def test_the_cached_validator_is_the_bounded_one():
    # One validator is kept per schema object: it must be this one, never
    # a plain validator with the default (fetching) registry.
    schema = _object({"q": _NEVER_MATCHES})
    for _ in range(2):
        assert validate_tool_arguments("t", {"q": "b"}, schema) is None
    cached = agent_loop._schema_validators[id(schema)][1]
    assert cached is agent_loop._checked_validator(schema)
    assert cached._registry is agent_loop._LOCAL_SCHEMA_REFS_ONLY
    assert type(cached).VALIDATORS["pattern"] is not jsonschema.Draft202012Validator.VALIDATORS["pattern"]
    skipped = _object({"q": {"type": "string", "not": {"pattern": "^a$"}}})
    for _ in range(2):
        assert validate_tool_arguments("t", {"q": "b"}, skipped) is None
    assert agent_loop._schema_validators[id(skipped)][1] is None


def test_each_coercion_call_has_its_own_pattern_budget():
    slow = _object({"q": {"anyOf": [{"pattern": _BUDGET_BREAKER}, {"type": "integer"}]}})
    fast = _object({"q": {"anyOf": [{"pattern": "^0"}, {"type": "integer"}]}})
    for _ in range(2):
        args = {"q": _LONGER}
        assert coerce_tool_arguments(args, slow) is args
        assert coerce_tool_arguments({"q": "5"}, fast) == {"q": 5}
    assert agent_loop._coercion_regex_stopwatch.get() is None


def test_a_reference_into_a_regex_keyword_still_resolves():
    # Resolved from the property schema itself, as the union check reads it.
    id_schema = {
        "anyOf": [{"enum": ["001"]}, {"type": "integer"}],
        "allOf": [{"$ref": "#/patternProperties/^x_"}],
        "patternProperties": {"^x_": {"type": "string"}},
    }
    args = {"id": "001"}
    assert coerce_tool_arguments(args, _object({"id": id_schema})) is args
    # Resolved from the root: the referenced subschema is still enforced.
    schema = {
        "type": "object",
        "patternProperties": {"^x_": {"type": "string"}},
        "properties": {"id": {"allOf": [{"$ref": "#/patternProperties/^x_"}]}},
    }
    assert validate_tool_arguments("t", {"id": "001"}, schema) is None
    assert validate_tool_arguments("t", {"id": 1}, schema) is not None


@pytest.mark.parametrize(
    "location",
    ["https://example.com/schema.json", "file:///etc/passwd", "data:application/json,%7B%7D"],
)
def test_a_remote_reference_is_never_retrieved(monkeypatch, location):
    retrieved: list[str] = []

    def _record(request, *args, **kwargs):
        retrieved.append(getattr(request, "full_url", request))
        raise AssertionError("argument validation tried to retrieve a schema")

    monkeypatch.setattr(urllib.request, "urlopen", _record)
    for keyword in ("$ref", "$dynamicRef"):
        schema = _object({"q": {keyword: location}})
        assert validate_tool_arguments("t", {"q": "b"}, schema) is None
    assert retrieved == []


@pytest.mark.parametrize("keyword", ["type", "disallow"])
def test_a_reference_rebased_by_an_id_is_never_retrieved(monkeypatch, keyword):
    # Every reference reads local, so the census lets the schema through: only
    # the registry stops the draft-03 id on a schema jsonschema descends into,
    # but referencing never crawls, from rebasing "#/y" onto another document.
    retrieved: list[str] = []

    def _record(request, *args, **kwargs):
        retrieved.append(getattr(request, "full_url", request))
        raise AssertionError("argument validation tried to retrieve a schema")

    monkeypatch.setattr(urllib.request, "urlopen", _record)
    rebased = {"id": "https://example.com/rebased.json", "properties": {"z": {"$ref": "#/y"}}}
    schema = {
        "$schema": "http://json-schema.org/draft-03/schema#",
        "type": "object",
        "properties": {"q": {keyword: [rebased]}},
    }
    assert validate_tool_arguments("t", {"q": {"z": 1}}, schema) is None
    assert retrieved == []


def test_an_mcp_result_is_never_checked_against_the_servers_output_schema(monkeypatch):
    # Driven through the SDK's own call_tool, so a check an SDK upgrade moves
    # out of _validate_tool_result fails here too.
    sentinel = "^sentinel-(x+)+$"
    reached = _patterns_reaching_re(monkeypatch, sentinel)
    retrieved: list[str] = []

    def _record(request, *args, **kwargs):
        retrieved.append(getattr(request, "full_url", request))
        raise AssertionError("an MCP result check tried to retrieve a schema")

    monkeypatch.setattr(urllib.request, "urlopen", _record)
    result = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="done")],
        structuredContent={"x": "sentinel-x", "y": 1},
        isError=False,
    )

    async def _send_request(*args, **kwargs):
        return result

    send, receive = anyio.create_memory_object_stream(0)
    try:
        session = ClientSession(receive, send)
        session._tool_output_schemas["t"] = _object(
            {"x": {"type": "string", "pattern": sentinel}, "y": {"$ref": "https://example.com/out.json"}}
        )
        monkeypatch.setattr(session, "send_request", _send_request)
        assert asyncio.run(session.call_tool("t", {})) is result
    finally:
        send.close()
        receive.close()
    assert reached == []
    assert retrieved == []


def test_only_the_mcp_client_opens_an_mcp_session():
    # Its ClientSession is the one that skips the check: a module opening the
    # SDK's own session would validate results against the server's schema.
    naming_it = [
        path.relative_to(_BACKEND_DIR).as_posix()
        for path in sorted((_BACKEND_DIR / "app").rglob("*.py"))
        if "ClientSession" in path.read_text(encoding="utf-8")
    ]
    assert naming_it == ["app/tools/mcp_client.py"]


def test_structure_behind_a_local_reference_is_still_enforced():
    schema = {
        **_object({"q": {"$ref": "#/$defs/M"}}),
        "$defs": {
            "M": {"type": "object", "properties": {"d": {"type": "string", "pattern": "^\\d+$"}}, "required": ["d"]}
        },
    }
    feedback = validate_tool_arguments("t", {"q": {}}, schema)
    assert feedback is not None and "'d' is a required property" in feedback
    assert validate_tool_arguments("t", {"q": {"d": "x"}}, schema) is None


def _patterns_reaching_re(monkeypatch, sentinel: str) -> list[str]:
    reached: list[str] = []
    for name in ("search", "match", "fullmatch", "compile", "findall", "finditer", "sub", "split"):

        def spy(pattern, *args, _original=getattr(re, name), _name=name, **kwargs):
            if isinstance(pattern, str) and sentinel in pattern:
                reached.append(_name)
            return _original(pattern, *args, **kwargs)

        monkeypatch.setattr(re, name, spy)
    return reached


def test_no_schema_pattern_ever_reaches_re(monkeypatch):
    # Every placement jsonschema reads: an upgrade adding a regex path fails here.
    sentinel = "^sentinel-(x+)+$"
    reached = _patterns_reaching_re(monkeypatch, sentinel)
    schema = {
        "type": "object",
        "properties": {
            "q": {"type": "string", "pattern": sentinel},
            "r": {"$ref": "#/$defs/r"},
            "s": {"type": "array", "items": {"pattern": sentinel}},
            "t": {"$ref": "#/default"},
            "u": {"anyOf": [{"type": "string", "pattern": sentinel}, {"type": "integer"}]},
        },
        "patternProperties": {sentinel: {}},
        "additionalProperties": False,
        "unevaluatedProperties": False,
        "propertyNames": {"pattern": sentinel},
        "dependentSchemas": {"q": {"properties": {"q": {"pattern": sentinel}}}},
        "$defs": {"r": {"pattern": sentinel}},
        "default": {"pattern": sentinel},
    }
    args = {"q": "x", "r": "x", "s": ["x"], "t": "x", "u": "5", "sentinel-x": 1}
    coerce_tool_arguments(args, schema)
    # The union check only compiles (``re`` rejects what it always rejected);
    # its matching runs on the bounded engine.
    assert [name for name in reached if name != "compile"] == []
    reached.clear()
    validate_tool_arguments("t", args, schema)
    assert reached == []


@pytest.mark.parametrize("keyword_first", [False, True], ids=["patterns-first", "keyword-first"])
@pytest.mark.parametrize("keyword", ["additionalProperties", "unevaluatedProperties"])
def test_pattern_properties_of_any_shape_never_reach_re(monkeypatch, keyword, keyword_first):
    # jsonschema joins or iterates a patternProperties LIST as surely as a
    # mapping, and check_schema never vets the instance data a $ref reads as a
    # schema, nor the union branches the coercion check validates. Keywords
    # run in schema order, so the keyword is also tried before the patterns.
    sentinel = "^sentinel-(x+)+$"
    reached = _patterns_reaching_re(monkeypatch, sentinel)
    if keyword_first:
        listed = {"type": "object", keyword: False, "patternProperties": [sentinel]}
    else:
        listed = {"type": "object", "patternProperties": [sentinel], keyword: False}
    args = {"q": {"sentinel-x": 1}}
    behind_a_reference = {**_object({"q": {"$ref": "#/default"}}), "default": listed}
    assert validate_tool_arguments("t", args, behind_a_reference) is None
    in_a_union = _object({"q": {"anyOf": [listed, {"type": "integer"}]}})
    assert coerce_tool_arguments(args, in_a_union) is args
    assert reached == []


def test_a_schema_without_regexes_keeps_every_constraint():
    not_b = _object({"q": {"type": "string", "not": {"const": "b"}}})
    assert validate_tool_arguments("t", {"q": "b"}, not_b) is not None
    both = _object({"q": {"oneOf": [{"type": "string"}, {"type": "string"}]}})
    assert validate_tool_arguments("t", {"q": "b"}, both) is not None
    # A property named like a keyword, next to a reference, changes nothing.
    named_if = {
        **_object({"if": {"type": "string"}, "m": {"$ref": "#/$defs/M"}}),
        "required": ["if"],
        "$defs": {"M": {"type": "integer"}},
    }
    missing = validate_tool_arguments("t", {}, named_if)
    assert missing is not None and "'if' is a required property" in missing


_CHILD = textwrap.dedent(
    """
    import json, sys, time
    from app.tools.agent_loop import _already_valid, coerce_tool_arguments, validate_tool_arguments

    cases = json.load(sys.stdin)
    start = time.monotonic()
    results = [validate_tool_arguments("t", case["args"], case["schema"]) for case in cases["validate"]]
    results += [_already_valid(case["value"], case["schema"]) for case in cases["union"]]
    results += [coerce_tool_arguments(case["args"], case["schema"]) for case in cases["coerce"]]
    print(json.dumps({"elapsed": time.monotonic() - start, "results": results}))
    """
)


def test_a_catastrophic_pattern_is_never_run_wherever_the_schema_puts_it():
    # ``re`` keeps the GIL while it backtracks, so no thread could stop it: a
    # child process is killed at the timeout whatever it is doing.
    data_uri = "data:application/json," + urllib.parse.quote(json.dumps({"pattern": _CATASTROPHIC}))
    listed_patterns = {"type": "object", "additionalProperties": False, "patternProperties": [_CATASTROPHIC]}
    cases = {
        "validate": [
            {"schema": _object({"q": {"type": "string", "pattern": _CATASTROPHIC}}), "args": {"q": _LONG}},
            {
                "schema": {"type": "object", "patternProperties": {_CATASTROPHIC: {}}, "additionalProperties": False},
                "args": {_LONG: 1},
            },
            {
                "schema": {**_object({"q": {"$ref": "#/default"}}), "default": {"pattern": _CATASTROPHIC}},
                "args": {"q": _LONG},
            },
            {"schema": _object({"q": {"$ref": data_uri}}), "args": {"q": _LONG}},
            {"schema": _referenced_under_nested_lists(_CATASTROPHIC), "args": {"q": _LONG}},
            {
                "schema": {**_object({"q": {"$ref": "#/default"}}), "default": listed_patterns},
                "args": {"q": {_LONG: 1}},
            },
        ],
        "union": [
            {"schema": {"anyOf": [{"type": "string", "pattern": _CATASTROPHIC}, {"type": "integer"}]}, "value": _LONG},
            {"schema": {"anyOf": [listed_patterns, {"type": "integer"}]}, "value": {_LONG: 1}},
            # Hard for the bounded engine too: it gives up at its budget.
            {"schema": {"anyOf": [{"pattern": _BUDGET_BREAKER}, {"type": "integer"}]}, "value": _LONGER},
        ],
        "coerce": [
            {
                "schema": _object({"q": {"anyOf": [{"pattern": _BUDGET_BREAKER}, {"type": "integer"}]}}),
                "args": {"q": _LONGER},
            }
        ],
    }
    done = subprocess.run(  # noqa: S603 - the interpreter running this test, with a fixed script
        [sys.executable, "-c", _CHILD],
        cwd=_BACKEND_DIR,
        input=json.dumps(cases),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout.strip().splitlines()[-1])
    assert report["results"] == [None, None, None, None, None, None, False, True, True, {"q": _LONGER}]
    assert report["elapsed"] < 5
