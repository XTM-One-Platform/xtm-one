"""Agent output-schema checks never fetch a document or run a regex unbounded.

The two checks - the structured-output parse and the ``finalize_on_tool``
terminal check - still enforce the owner's patterns, give the verdicts they
gave before, and only a pattern with no verdict in time or a reference to
another document is left unjudged.
"""

from __future__ import annotations

import itertools
import json
import re
import statistics
import subprocess
import sys
import textwrap
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from app.api.v1 import security_audit
from app.services.output_schema_validation import output_schema_errors, output_schema_validator
from app.services.structured_output import validate_and_parse
from app.tools.agent_loop import _terminal_tool_decision
from app.tools.base import ToolResult

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_HARVESTER = _BACKEND_DIR / "app" / "agents" / "seed_data" / "agent_output_schema" / "cti-stix-harvester.json"

# ``re`` needs minutes to refuse this input against this pattern.
_CATASTROPHIC = "^(a+)+$"
_LONG = "a" * 40 + "!"


def _object(properties: dict, **extra) -> dict:
    return {"type": "object", "properties": properties, **extra}


def _terminal(output: dict, schema: dict):
    return _terminal_tool_decision(
        tool_name="finish",
        result=ToolResult(tool_call_id="c1", content=json.dumps({"valid": True, "bundle": output})),
        finalize_on_tool={"tool_name": "finish", "validate_output_schema": True},
        tool_name_map={},
        output_schema=schema,
    )


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _refuse(*args, **kwargs):
        raise AssertionError("an output-schema check tried to retrieve a document")

    monkeypatch.setattr(urllib.request, "urlopen", _refuse)


@pytest.mark.parametrize(
    "location", ["https://example.com/schema.json", "file:///etc/passwd", "data:application/json,%7B%7D"]
)
def test_neither_check_retrieves_a_referenced_document(monkeypatch, location):
    retrieved: list[str] = []

    def _record(request, *args, **kwargs):
        retrieved.append(getattr(request, "full_url", request))
        raise AssertionError("an output-schema check tried to retrieve a document")

    monkeypatch.setattr(urllib.request, "urlopen", _record)
    for keyword in ("$ref", "$dynamicRef"):
        schema = _object({"q": {keyword: location}, "n": {"type": "integer"}})
        data, errors = validate_and_parse(json.dumps({"q": "b", "n": 1}), schema)
        assert (data, errors) == ({"q": "b", "n": 1}, [])
        assert _terminal({"q": "b", "n": 1}, schema).reason == "final_llm_call_skipped"
        # The rest of the schema is still enforced.
        assert validate_and_parse(json.dumps({"q": "b", "n": "x"}), schema)[1]
        assert _terminal({"q": "b", "n": "x"}, schema).reason == "structured_output_validation_failed"
    assert retrieved == []


class _AuditDb:
    """The count of enabled agents with an output schema, then the rows whose text holds a reference keyword."""

    def __init__(self, rows):
        with_ref = [row for row in rows if any(key in json.dumps(row[1]) for key in ("$ref", "$dynamicRef"))]
        self._results = [SimpleNamespace(scalar=lambda: len(rows)), SimpleNamespace(all=lambda: with_ref)]

    async def execute(self, *_args, **_kwargs):
        return self._results.pop(0)


async def test_the_security_audit_names_output_schemas_that_reference_another_document():
    rows = [
        ("Remote", _object({"q": {"$ref": "https://example.com/s.json"}})),
        ("Local", {"$defs": {"q": {}}, "properties": {"q": {"$ref": "#/$defs/q"}}}),
        ("Plain", _object({"q": {"type": "string"}})),
    ]
    findings: list = []
    await security_audit._check_agent_output_schemas(_AuditDb(rows), findings)
    assert [(f.id, f.severity, f.title) for f in findings] == [
        ("agents.output_schema_remote_references", "warning", "1 agent output schema(s) reference another document")
    ]
    assert findings[0].description.startswith("Remote:")
    passed: list = []
    await security_audit._check_agent_output_schemas(_AuditDb(rows[1:]), passed)
    assert [(f.id, f.severity) for f in passed] == [("agents.output_schema_references_local", "pass")]
    assert passed[0].description.startswith("2 enabled agent(s) with an output schema")
    none: list = []
    await security_audit._check_agent_output_schemas(_AuditDb([]), none)
    assert none == []


@pytest.mark.parametrize(
    "unjudged",
    [
        pytest.param({"$ref": "https://example.com/s.json"}, id="remote-reference"),
        pytest.param({"pattern": "^(a|aa)+$"}, id="pattern-past-its-bound"),
    ],
)
@pytest.mark.parametrize(
    "shape",
    [
        pytest.param(lambda u: {"not": u}, id="not"),
        pytest.param(lambda u: {"oneOf": [u, {"type": "string"}]}, id="oneOf"),
        pytest.param(lambda u: {"if": u, "then": {"maxLength": 1}}, id="if"),
        pytest.param(lambda u: {"not": {"not": u}}, id="nested-not"),
    ],
)
def test_an_unjudged_constraint_never_refuses_output_under_a_non_monotone_keyword(shape, unjudged):
    # Unjudged reads as satisfied, which tightens under not / oneOf / if.
    schema = _object({"q": {"type": "string", **shape(unjudged)}})
    output = {"q": "a" * 60 + "!"}
    assert validate_and_parse(json.dumps(output), schema)[1] == []
    assert _terminal(output, schema).reason == "final_llm_call_skipped"


def test_a_real_error_is_still_reported_beside_an_unjudged_constraint():
    schema = _object({"q": {"$ref": "https://example.com/s.json"}, "n": {"type": "integer"}})
    assert validate_and_parse('{"q": "b", "n": "x"}', schema)[1]


@pytest.mark.parametrize(
    ("schema", "output"),
    [
        pytest.param(
            {
                "type": "object",
                "properties": {"kind": {}},
                "if": {"properties": {"kind": {"pattern": "^(a|aa)+$"}}},
                "then": {},
                "else": {"properties": {"extra": {}}},
                "unevaluatedProperties": False,
            },
            {"kind": "a" * 60 + "!", "extra": 1},
            id="unevaluatedProperties",
        ),
        pytest.param(
            {
                "type": "array",
                "prefixItems": [{}],
                "if": {"prefixItems": [{"pattern": "^(a|aa)+$"}]},
                "then": {},
                "else": {"prefixItems": [{}, {}]},
                "unevaluatedItems": False,
            },
            ["a" * 60 + "!", 1],
            id="unevaluatedItems",
        ),
    ],
)
def test_an_unjudged_constraint_never_refuses_output_through_an_unevaluated_keyword(schema, output):
    # Both re-run if / then / else to find what was evaluated: read as met, the
    # pattern past its bound picks the branch that evaluates less.
    errors = output_schema_errors(output_schema_validator(schema, jsonschema.Draft202012Validator), output)
    assert errors == []
    if isinstance(output, dict):
        assert _terminal(output, schema).reason == "final_llm_call_skipped"


def test_only_searching_spends_the_regex_budget(monkeypatch):
    # The rest of a large output's check outlasts the whole budget: its last
    # pattern is still judged, as main judged it.
    from app.services import output_schema_validation

    monkeypatch.setattr(output_schema_validation, "OUTPUT_SCHEMA_REGEX_BUDGET_SECONDS", 0.02)
    rows = {"type": "array", "items": {"type": "object", "properties": {"n": {"type": "integer", "minimum": 0}}}}
    schema = _object({"rows": rows, "code": {"type": "string", "pattern": "^[A-Z]{3}$"}})
    output = {"rows": [{"n": index} for index in range(20000)], "code": "abc"}
    started = time.thread_time()
    errors = output_schema_errors(output_schema_validator(schema, jsonschema.Draft7Validator), output)
    assert time.thread_time() - started > 0.02
    assert [e.message for e in errors] == ["'abc' does not match '^[A-Z]{3}$'"]


class _SearchClock:
    """The clock the regex budget reads, on which every search takes exactly its allowance.

    ``_search`` starts ``schema_guard.regex_stopwatch()`` when a search starts
    and reads what it charged when it ends. On the runner a search is also
    charged the CPU time of a garbage collection that lands in it, so a few
    searches could spend a budget thousands of microsecond searches never would.
    What each search really took is kept in ``searches``: no collection moves
    their median.
    """

    def __init__(self, allowance: float) -> None:
        self._allowance = allowance
        self._started = 0.0
        self.elapsed = 0.0
        self.searches: list[float] = []

    def __call__(self) -> _SearchClock:
        self._started = time.perf_counter()
        return self

    def charged(self) -> float:
        self.searches.append(time.perf_counter() - self._started)
        self.elapsed += self._allowance
        return self._allowance


@pytest.fixture
def search_clock(monkeypatch) -> _SearchClock:
    from app.services import output_schema_validation, schema_guard

    clock = _SearchClock(output_schema_validation.OUTPUT_SCHEMA_SEARCH_ALLOWANCE_SECONDS)
    monkeypatch.setattr(schema_guard, "regex_stopwatch", clock)
    return clock


def test_the_quick_searches_of_a_large_output_never_exhaust_the_budget(monkeypatch, search_clock):
    # Each takes its whole allowance on the budget's clock: together they outlast this budget many times over.
    from app.services import output_schema_validation

    budget = 0.03
    monkeypatch.setattr(output_schema_validation, "OUTPUT_SCHEMA_REGEX_BUDGET_SECONDS", budget)
    schema = {"type": "array", "items": {"type": "string", "pattern": "^[a-z]+$"}}
    output = ["abc"] * 30000 + ["ABC"]
    errors = output_schema_errors(output_schema_validator(schema, jsonschema.Draft7Validator), output)
    assert [(list(e.absolute_path), e.message) for e in errors] == [([30000], "'ABC' does not match '^[a-z]+$'")]
    assert len(search_clock.searches) == len(output)
    assert search_clock.elapsed > 10 * budget
    # And each is quick on the runner's clock too.
    assert statistics.median(search_clock.searches) < output_schema_validation.OUTPUT_SCHEMA_SEARCH_ALLOWANCE_SECONDS


def test_the_terminal_check_stops_at_the_first_error_as_validate_did():
    schema = _object({"a": {"type": "integer"}, "b": {"type": "integer"}})
    errors = output_schema_errors(output_schema_validator(schema, jsonschema.Draft202012Validator), {"a": "x", "b": "y"}, limit=1)
    assert [e.message for e in errors] == ["'x' is not of type 'integer'"]
    assert _terminal({"a": "x", "b": "y"}, schema).detail.startswith("'x' is not of type 'integer'\n")


def test_owner_patterns_are_still_enforced():
    schema = _object({"id": {"type": "string", "pattern": "^bundle--"}}, required=["id"])
    assert validate_and_parse('{"id": "bundle--1"}', schema)[1] == []
    errors = validate_and_parse('{"id": "report--1"}', schema)[1]
    assert errors == ["Validation error at id: 'report--1' does not match '^bundle--'"]
    assert _terminal({"id": "bundle--1"}, schema).reason == "final_llm_call_skipped"
    assert _terminal({"id": "report--1"}, schema).reason == "structured_output_validation_failed"


_SAME_VERDICT_CASES = [
    (_object({"q": {"pattern": "^a"}}), {"q": "ab"}),
    (_object({"q": {"pattern": "^a"}}), {"q": "ba"}),
    ({"type": "object", "patternProperties": {"^x_": {"type": "integer"}}}, {"x_a": 1, "y": "s"}),
    ({"type": "object", "patternProperties": {"^x_": {"type": "integer"}}}, {"x_a": "s"}),
    ({"type": "object", "patternProperties": {"^x_": {}}, "additionalProperties": False}, {"x_a": 1}),
    ({"type": "object", "patternProperties": {"^x_": {}}, "additionalProperties": False}, {"x_a": 1, "b": 2, "c": 3}),
    ({"type": "object", "properties": {"a": {}}, "additionalProperties": False}, {"a": 1, "b": 2}),
    ({"type": "object", "patternProperties": {"^x_": {}}, "additionalProperties": {"type": "string"}}, {"b": 2}),
    ({"type": "object", "propertyNames": {"pattern": "^[a-z]+$"}}, {"ok": 1, "Not": 2}),
    ({"definitions": {"id": {"pattern": "^T\\d{4}$"}}, "properties": {"t": {"$ref": "#/definitions/id"}}}, {"t": "T1"}),
    ({"type": "array", "items": {"pattern": "^[0-9a-f]{2}$"}}, ["0a", "zz", "ff"]),
]


@pytest.mark.parametrize("base", [jsonschema.Draft7Validator, jsonschema.Draft202012Validator])
@pytest.mark.parametrize(("schema", "instance"), _SAME_VERDICT_CASES)
def test_the_bounded_check_gives_the_verdicts_jsonschema_gave(base, schema, instance):
    def verdicts(errors):
        return sorted((list(e.absolute_path), e.message, e.validator) for e in errors)

    expected = verdicts(base(schema).iter_errors(instance))
    assert verdicts(output_schema_errors(output_schema_validator(schema, base), instance)) == expected


def test_no_owner_pattern_is_matched_by_re(monkeypatch):
    sentinel = "^sentinel-(x+)+$"
    reached: list[str] = []
    for name in ("search", "match", "fullmatch", "findall", "finditer", "sub", "split"):

        def spy(pattern, *args, _original=getattr(re, name), _name=name, **kwargs):
            if isinstance(pattern, str) and sentinel in pattern:
                reached.append(_name)
            return _original(pattern, *args, **kwargs)

        monkeypatch.setattr(re, name, spy)
    schema = {
        "type": "object",
        "properties": {"q": {"pattern": sentinel}, "r": {"$ref": "#/definitions/r"}},
        "patternProperties": {sentinel: {}},
        "additionalProperties": False,
        "propertyNames": {"pattern": sentinel},
        "definitions": {"r": {"pattern": sentinel}},
    }
    output = {"q": "sentinel-x", "r": "sentinel-x", "sentinel-xx": 1}
    validate_and_parse(json.dumps(output), schema)
    _terminal(output, schema)
    assert reached == []


@pytest.mark.parametrize("base", [jsonschema.Draft7Validator, jsonschema.Draft202012Validator])
def test_the_harvester_schema_judges_real_bundles_as_before_and_within_its_budget(
    base, caplog, monkeypatch, search_clock
):
    from app.services import output_schema_validation
    from tests.test_cti_stix_bundle_schema import _CUSTOM_SDOS, _MUST_REJECT, _STANDARD_OBJECTS, _bundle

    # Stock jsonschema has no bounds: none a paused search can reach here, so every constraint gets its verdict.
    monkeypatch.setattr(output_schema_validation, "OUTPUT_SCHEMA_REGEX_BUDGET_SECONDS", 600.0)
    monkeypatch.setattr(output_schema_validation, "OUTPUT_SCHEMA_PATTERN_TIMEOUT_SECONDS", 60.0)
    schema = json.loads(_HARVESTER.read_text(encoding="utf-8"))
    bundles = [_bundle(*_STANDARD_OBJECTS, *_CUSTOM_SDOS)] + [_bundle(case.values[0]) for case in _MUST_REJECT]
    with caplog.at_level("WARNING", logger="app.services.output_schema_validation"):
        for bundle in bundles:
            expected = sorted((list(e.absolute_path), e.message) for e in base(schema).iter_errors(bundle))
            found = output_schema_errors(output_schema_validator(schema, base), bundle)
            assert sorted((list(e.absolute_path), e.message) for e in found) == expected
    assert [r.getMessage() for r in caplog.records] == []
    # Within its budget: a search of a real bundle takes less than its allowance, so it spends none of it.
    assert statistics.median(search_clock.searches) < output_schema_validation.OUTPUT_SCHEMA_SEARCH_ALLOWANCE_SECONDS


_CHILD = textwrap.dedent(
    """
    import json, sys, time
    from app.services.structured_output import validate_and_parse
    from app.tools.agent_loop import _terminal_tool_decision
    from app.tools.base import ToolResult

    cases = json.load(sys.stdin)
    start = time.monotonic()
    for case in cases:
        validate_and_parse(json.dumps(case["output"]), case["schema"])
        _terminal_tool_decision(
            tool_name="finish",
            result=ToolResult(tool_call_id="c", content=json.dumps({"valid": True, "bundle": case["output"]})),
            finalize_on_tool={"tool_name": "finish", "validate_output_schema": True},
            tool_name_map={},
            output_schema=case["schema"],
        )
    print(json.dumps({"elapsed": time.monotonic() - start}))
    """
)


def test_a_catastrophic_owner_pattern_cannot_hold_the_worker():
    # ``re`` keeps the GIL while it backtracks, so only a child process can be
    # stopped whatever it is doing.
    cases = [
        {"schema": _object({"q": {"type": "string", "pattern": _CATASTROPHIC}}), "output": {"q": _LONG}},
        {
            "schema": {"type": "object", "patternProperties": {_CATASTROPHIC: {}}, "additionalProperties": False},
            "output": {_LONG: 1},
        },
        # Each search runs to its own bound: together they stop at the budget.
        {"schema": {"type": "array", "items": {"type": "string", "pattern": "^(a|aa)+$"}}, "output": ["a" * 60 + "!"] * 50},
    ]
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
    assert json.loads(done.stdout.strip().splitlines()[-1])["elapsed"] < 5


# The seeded STIX harvester schema: its two identifier patterns, before the fix.
_OLD_TYPE = r"^([a-z][a-z0-9]*)+(-[a-z0-9]+)*\-?$"
_OLD_EXTENSION = r"^([a-z][a-z0-9]*)+(-[a-z0-9]+)*\-ext$"


def _harvester_patterns() -> set[str]:
    found: set[str] = set()
    pending = [json.loads(_HARVESTER.read_text(encoding="utf-8"))]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if isinstance(node.get("pattern"), str):
                found.add(node["pattern"])
            if isinstance(node.get("patternProperties"), dict):
                found.update(node["patternProperties"])
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return found


def _replacement(old: str) -> str:
    matches = [p for p in _harvester_patterns() if p.endswith(old[-6:]) and p.startswith("^[a-z][a-z0-9]*")]
    assert len(matches) == 1, matches
    return matches[0]


@pytest.mark.parametrize("old", [_OLD_TYPE, _OLD_EXTENSION], ids=["type", "extension-key"])
def test_the_seeded_identifier_patterns_accept_exactly_what_they_accepted(old):
    new = _replacement(old)
    assert old not in _harvester_patterns()
    alphabet = "a0-zeExt_"
    for length in range(0, 7):
        for chars in itertools.product(alphabet, repeat=length):
            text = "".join(chars)
            assert bool(re.search(new, text)) == bool(re.search(old, text)), text
    for text in (
        "indicator", "x-mitre-tactic", "ipv4-addr", "x509-certificate", "a-", "a--b", "-a", "A", "a_b",
        "x-acme-ext", "extension-definition--ext", "a-ext", "ext", "1a", "a-b-c-d-e-f-g-h-", "a\n",
    ):
        assert bool(re.search(new, text)) == bool(re.search(old, text)), text


def test_the_seeded_harvester_patterns_run_in_linear_time():
    probe = textwrap.dedent(
        """
        import json, re, sys, time
        units = ["a", "a1", "a-", "-a", "aa", "0", "-abcde", "x"]
        start = time.monotonic()
        for pattern in json.load(sys.stdin):
            for unit in units:
                for tail in ("!", "-!", "\\n!"):
                    re.search(pattern, "a" + unit * 40 + tail)
        print(json.dumps({"elapsed": time.monotonic() - start}))
        """
    )
    done = subprocess.run(  # noqa: S603 - the interpreter running this test, with a fixed script
        [sys.executable, "-c", probe],
        input=json.dumps(sorted(_harvester_patterns())),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.strip().splitlines()[-1])["elapsed"] < 5
