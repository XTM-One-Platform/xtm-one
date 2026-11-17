"""One guard serves every check against a schema the platform did not write.

Tool-argument validation, argument coercion and the two agent output-schema
checks share one registry that retrieves nothing, one bounded pattern search
and one set of regex keywords (``app.services.schema_guard``). A subschema that
names its own dialect stays on those keywords, and the bounded engine never
gives a verdict ``re`` would not give.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import json
import re
import subprocess
import sys
import textwrap
import urllib.request
from pathlib import Path

import jsonschema
import pytest

from app.services import output_schema_validation, schema_guard
from app.services.output_schema_validation import (
    output_schema_errors,
    output_schema_validator,
    references_another_document,
)
from app.services.structured_output import validate_and_parse
from app.tools import agent_loop
from app.tools.agent_loop import _already_valid, _terminal_tool_decision, coerce_tool_arguments, validate_tool_arguments
from app.tools.base import ToolResult

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_CATASTROPHIC = "^(a+)+$"
_LONG = "a" * 40 + "!"
_SENTINEL = "^sentinel-(x+)+$"
_DIALECTS = ["http://json-schema.org/draft-07/schema#", "https://json-schema.org/draft/2020-12/schema"]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _refuse(*args, **kwargs):
        raise AssertionError("a schema check tried to retrieve a document")

    monkeypatch.setattr(urllib.request, "urlopen", _refuse)


def _terminal(output: dict, schema: dict):
    return _terminal_tool_decision(
        tool_name="finish",
        result=ToolResult(tool_call_id="c1", content=json.dumps({"valid": True, "bundle": output})),
        finalize_on_tool={"tool_name": "finish", "validate_output_schema": True},
        tool_name_map={},
        output_schema=schema,
    )


def _object_schema(inner: dict, *, behind_a_reference: bool) -> dict:
    if behind_a_reference:
        return {"type": "object", "$defs": {"q": inner}, "properties": {"q": {"$ref": "#/$defs/q"}}}
    return {"type": "object", "properties": {"q": inner}}


def _union_schema(inner: dict, *, behind_a_reference: bool) -> dict:
    if behind_a_reference:
        union = {"$defs": {"q": inner}, "anyOf": [{"$ref": "#/$defs/q"}, {"type": "integer"}]}
    else:
        union = {"anyOf": [inner, {"type": "integer"}]}
    return {"type": "object", "properties": {"q": union}}


def _run_every_check(inner: dict, value: str, *, behind_a_reference: bool) -> None:
    schema = _object_schema(inner, behind_a_reference=behind_a_reference)
    validate_tool_arguments("t", {"q": value}, schema)
    coerce_tool_arguments({"q": value}, _union_schema(inner, behind_a_reference=behind_a_reference))
    validate_and_parse(json.dumps({"q": value}), schema)
    _terminal({"q": value}, schema)


def test_every_check_uses_the_one_registry_and_the_one_bounded_engine():
    # A second copy is how one check stays exposed after the other is fixed.
    assert agent_loop._LOCAL_SCHEMA_REFS_ONLY is schema_guard.LOCAL_SCHEMA_REFS_ONLY
    holding = sorted(
        path.relative_to(_BACKEND_DIR).as_posix()
        for path in sorted((_BACKEND_DIR / "app").rglob("*.py"))
        if re.search(r"^\s*import regex\b|Registry\(retrieve=", path.read_text(encoding="utf-8"), re.MULTILINE)
    )
    assert holding == ["app/services/schema_guard.py"]


@pytest.mark.parametrize("behind_a_reference", [False, True], ids=["subschema", "behind-a-reference"])
@pytest.mark.parametrize("dialect", _DIALECTS, ids=["draft-07", "2020-12"])
def test_a_subschema_naming_its_own_dialect_never_hands_its_pattern_to_re(monkeypatch, dialect, behind_a_reference):
    # jsonschema picks the stock class for a subschema's own $schema as it descends.
    reached: list[str] = []
    for name in ("search", "match", "fullmatch", "findall", "finditer", "sub", "split"):

        def spy(pattern, *args, _original=getattr(re, name), _name=name, **kwargs):
            if isinstance(pattern, str) and _SENTINEL in pattern:
                reached.append(_name)
            return _original(pattern, *args, **kwargs)

        monkeypatch.setattr(re, name, spy)
    inner = {"$schema": dialect, "type": "string", "pattern": _SENTINEL}
    _run_every_check(inner, "sentinel-x", behind_a_reference=behind_a_reference)
    assert reached == []


_DIALECT_CHILD = textwrap.dedent(
    """
    import json, sys, time
    from app.services.structured_output import validate_and_parse
    from app.tools.agent_loop import _terminal_tool_decision, coerce_tool_arguments, validate_tool_arguments
    from app.tools.base import ToolResult

    started = time.monotonic()
    for case in json.load(sys.stdin):
        value = case["value"]
        validate_tool_arguments("t", {"q": value}, case["schema"])
        coerce_tool_arguments({"q": value}, case["union"])
        validate_and_parse(json.dumps({"q": value}), case["schema"])
        _terminal_tool_decision(
            tool_name="finish",
            result=ToolResult(tool_call_id="c", content=json.dumps({"valid": True, "bundle": {"q": value}})),
            finalize_on_tool={"tool_name": "finish", "validate_output_schema": True},
            tool_name_map={},
            output_schema=case["schema"],
        )
        print("done", case["label"], flush=True)
    print(json.dumps({"elapsed": time.monotonic() - started}), flush=True)
    """
)


def test_a_catastrophic_pattern_under_its_own_dialect_cannot_hold_the_worker():
    # ``re`` keeps the GIL while it backtracks: only a child process can be stopped.
    cases = []
    for dialect in _DIALECTS:
        inner = {"$schema": dialect, "type": "string", "pattern": _CATASTROPHIC}
        for behind in (False, True):
            cases.append(
                {
                    "label": f"{dialect} {'behind-a-reference' if behind else 'subschema'}",
                    "schema": _object_schema(inner, behind_a_reference=behind),
                    "union": _union_schema(inner, behind_a_reference=behind),
                    "value": _LONG,
                }
            )
    try:
        done = subprocess.run(  # noqa: S603 - the interpreter running this test, with a fixed script
            [sys.executable, "-c", _DIALECT_CHILD],
            cwd=_BACKEND_DIR,
            input=json.dumps(cases),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        finished = (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else exc.stdout or ""
        pytest.fail(f"a pattern ran on re unbounded; finished before the timeout: {finished.splitlines()}")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.strip().splitlines()[-1])["elapsed"] < 5


_MIXED_DIALECTS = [
    # A draft-07 tuple inside a 2020-12 document: ``items`` as a list, ``additionalItems``.
    (
        {
            "type": "object",
            "properties": {
                "t": {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "array",
                    "items": [{"type": "integer"}],
                    "additionalItems": False,
                }
            },
        },
        [{"t": [1]}, {"t": [1, 2]}, {"t": ["x"]}],
    ),
    # A 2020-12 subschema inside a draft-07 document: ``prefixItems``, ``unevaluatedItems``.
    (
        {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {
                "t": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "array",
                    "prefixItems": [{"type": "string", "pattern": "^a"}],
                    "unevaluatedItems": False,
                }
            },
        },
        [{"t": ["ab"]}, {"t": ["ba"]}, {"t": ["ab", 1]}],
    ),
]


@pytest.mark.parametrize(("schema", "instances"), _MIXED_DIALECTS, ids=["draft-07-in-2020-12", "2020-12-in-draft-07"])
def test_a_mixed_dialect_document_keeps_the_verdicts_of_each_dialect(schema, instances):
    base = jsonschema.validators.validator_for(schema)
    for instance in instances:
        expected = sorted((list(e.absolute_path), e.message) for e in base(schema).iter_errors(instance))
        found = output_schema_errors(output_schema_validator(schema, base), instance)
        assert sorted((list(e.absolute_path), e.message) for e in found) == expected, instance


# Patterns ``re`` reads differently from the ``regex`` engine, with a value on
# which the two give different verdicts; ``matches`` is ``re``'s.
_READ_DIFFERENTLY = [
    pytest.param("^[[:alpha:]]$", "x", False, id="posix-class-in-a-set"),
    pytest.param("[[:alpha:]]", "[]", True, id="posix-class-literal-bracket"),
    pytest.param("^x{e}$", "zzz", False, id="fuzzy-constraint"),
    pytest.param("^a{i}$", "a", False, id="fuzzy-insertion"),
    pytest.param("^{s}$", "{s}", True, id="literal-braces-at-the-start"),
    pytest.param("^/users/{id}$", "/users/42", False, id="path-template-refused-by-regex"),
    pytest.param("^/users/{id}$", "/users/{id}", True, id="path-template-literal"),
]


@pytest.mark.parametrize(("pattern", "value", "matches"), _READ_DIFFERENTLY)
def test_every_check_reads_a_pattern_as_re_reads_it(pattern, value, matches):
    schema = {"type": "object", "properties": {"q": {"type": "string", "pattern": pattern}}}
    assert (validate_and_parse(json.dumps({"q": value}), schema)[1] == []) is matches
    assert (_terminal({"q": value}, schema).reason == "final_llm_call_skipped") is matches
    assert _already_valid(value, {"anyOf": [{"type": "string", "pattern": pattern}, {"type": "integer"}]}) is matches


# Classes and case-insensitive matching on characters the two engines classify
# or fold differently, in both directions: no verdict, never ``regex``'s.
_CLASSIFIED_DIFFERENTLY = [
    pytest.param(r"^\w$", "\u00b2", id="superscript-two-word"),
    pytest.param(r"^\W$", "\u00b2", id="superscript-two-non-word"),
    pytest.param(r"^\w+$", "e\u0301", id="combining-mark"),
    pytest.param(r"^\s$", "\x1c", id="file-separator-space"),
    pytest.param(r"^\S$", "\x1c", id="file-separator-non-space"),
    pytest.param(r"(?i)^[a-z]$", "\u0131", id="dotless-i"),
    pytest.param(r"(?i)^I$", "\u0130", id="dotted-capital-i"),
]


@pytest.mark.parametrize(("pattern", "value"), _CLASSIFIED_DIFFERENTLY)
def test_no_verdict_is_given_where_the_engines_classify_a_character_differently(pattern, value):
    with pytest.raises(schema_guard.PatternUnjudgedError) as caught:
        schema_guard.bounded_search(pattern, value, remaining=1.0)
    assert caught.value.why == schema_guard.READ_DIFFERENTLY
    # The output check lets the constraint through rather than refuse on the other engine's reading.
    schema = {"type": "object", "properties": {"q": {"type": "string", "pattern": pattern}}}
    assert validate_and_parse(json.dumps({"q": value}), schema)[1] == []


@pytest.mark.parametrize(
    ("patterns", "admitted"),
    [
        pytest.param({"^a$": {}, "(?i)^b$": {}}, "B", id="inline-flag-in-a-later-pattern"),
        pytest.param({"^(?P<k>a)$": {}, "^(?P<k>b)$": {}}, "b", id="one-group-name-in-two-patterns"),
    ],
)
def test_additional_properties_reads_each_pattern_properties_regex_on_its_own(patterns, admitted):
    # jsonschema joins the patterns with "|", and the joined expression does not compile.
    closed = {"type": "object", "patternProperties": patterns, "additionalProperties": False}
    assert _terminal({"a": 1, admitted: 1}, closed).reason == "final_llm_call_skipped"
    regexes = ", ".join(repr(each) for each in sorted(patterns))
    found = output_schema_errors(output_schema_validator(closed, jsonschema.Draft7Validator), {"a": 1, "c": 1})
    assert [e.message for e in found] == [f"'c' does not match any of the regexes: {regexes}"]
    assert _already_valid({"a": 1, admitted: 1}, {"anyOf": [closed, {"type": "integer"}]}) is True
    # The structured-output parse keeps additional keys a schema describes, and validates them.
    typed = {"type": "object", "patternProperties": patterns, "additionalProperties": {"type": "integer"}}
    assert validate_and_parse(json.dumps({"a": 1, admitted: "x"}), typed)[1] == []
    assert validate_and_parse(json.dumps({"a": 1, "c": "x"}), typed)[1] == [
        "Validation error at c: 'x' is not of type 'integer'"
    ]


def test_a_scoped_ascii_or_unicode_mode_never_yields_the_other_engines_verdict():
    # The two engines disagree on what a scoped (?a:...) / (?u:...) does to a
    # class or to case folding, beyond anything the pattern's global mode says.
    bodies = [r"\w", r"\W", r"\s", r"\S", r"\d", r"\D", r"\bx", "i", "I", "[a-z]", "[A-Z]", "k", "s", "\u0131"]
    texts = ["\u00b2", "\x1c", "\u0131", "\u0130", "i", "I", "e\u0301", "\u0663", "\u212a", "\u017f", "x", "x\u00b2"]
    for outer in ("", "(?a)", "(?u)", "(?i)", "(?ai)", "(?ui)"):
        for inner in ("a", "u", "ai", "ui"):
            for body in bodies:
                pattern = f"{outer}(?{inner}:{body})"
                try:
                    re.compile(pattern)
                except re.error:
                    continue
                for text in texts:
                    try:
                        found = schema_guard.bounded_search(pattern, text, remaining=1.0)
                    except schema_guard.PatternUnjudgedError:
                        continue
                    assert found is (re.search(pattern, text) is not None), (pattern, text)


_REMOTE = "https://example.com/other.json"


_DRAFT7 = "http://json-schema.org/draft-07/schema#"
_ROOT = "https://example.com/root.json"
_NEVER_RETRIEVED = "reference to another document, never retrieved"


@contextlib.contextmanager
def _refusals(monkeypatch):
    """The references the output checks run inside refuse to retrieve, as they refuse them."""
    met: list[str] = []
    unjudged = output_schema_validation._unjudged

    def record(what, why):
        if why == _NEVER_RETRIEVED:
            met.append(what)
        unjudged(what, why)

    with monkeypatch.context() as patch:
        patch.setattr(output_schema_validation, "_unjudged", record)
        yield met


def _refused_by_the_checks(monkeypatch, schema, instances) -> bool:
    with _refusals(monkeypatch) as met:
        for instance in instances:
            # A reference that is not a string, or an anchor the dialect does not know, makes
            # the structured-output parse raise, as stock jsonschema did: it retrieves nothing.
            with contextlib.suppress(Exception):
                validate_and_parse(json.dumps(instance), schema)
            _terminal(instance, schema)
    return bool(met)


# (schema, instances reaching every reference, does an output check refuse one).
# The checks are run on the instances too: the audit says what they do.
_AUDIT_CASES = [
    pytest.param({"properties": {"q": {"$ref": _REMOTE}}}, [{"q": 1}], True, id="a-schema-position"),
    pytest.param(
        {"$defs": {"x": {}}, "properties": {"q": {"$ref": "#/$defs/x"}}, "default": {"$ref": _REMOTE}},
        [{"q": 1}],
        False,
        id="instance-data-no-reference-reads",
    ),
    pytest.param(
        {"properties": {"q": {"$ref": "#/default"}}, "default": {"$ref": _REMOTE}},
        [{"q": 1}],
        True,
        id="instance-data-a-pointer-reads",
    ),
    pytest.param(
        {"properties": {"q": {"$ref": "#/$defs/a"}}, "$defs": {"a": {"$ref": "#/examples/0"}}, "examples": [{"$ref": _REMOTE}]},
        [{"q": 1}],
        True,
        id="read-through-a-chain-of-pointers",
    ),
    pytest.param(
        {"properties": {"q": {"$ref": "#/properties"}, "$ref": {"type": "string"}}, "enum": [{"$ref": _REMOTE}]},
        [{"q": 1}],
        False,
        id="a-map-of-names-read-as-a-schema",
    ),
    # The validator ignores an unknown keyword, and so does the audit.
    pytest.param({"type": "object", "x-custom": {"$ref": _REMOTE}}, [{"q": 1}], False, id="an-unknown-keyword"),
    pytest.param({"type": "object", "$defs": {"x": {"$ref": _REMOTE}}}, [{"q": 1}], False, id="defs-no-reference-reaches"),
    pytest.param({"type": "object", "then": {"$ref": _REMOTE}}, [{"q": 1}], False, id="then-without-if"),
    pytest.param(
        {"type": "object", "properties": {"q": {"contentSchema": {"$ref": _REMOTE}}}}, [{"q": "x"}], False, id="content-schema-an-annotation"
    ),
    pytest.param(
        {"$schema": _DRAFT7, "definitions": {"a": {}}, "$ref": "#/definitions/a", "properties": {"q": {"$ref": _REMOTE}}},
        [{"q": 1}],
        False,
        id="a-draft-7-ref-hides-its-siblings",
    ),
    pytest.param(
        {"$schema": _DRAFT7, "type": "object", "properties": {"q": {"$dynamicRef": _REMOTE}}},
        [{"q": 1}],
        False,
        id="a-keyword-draft-7-does-not-have",
    ),
    pytest.param({"type": "object", "properties": {"q": {"$dynamicRef": _REMOTE}}}, [{"q": 1}], True, id="dynamic-ref-in-2020-12"),
    pytest.param(
        {"$defs": {"x": {"$anchor": "a"}}, "properties": {"q": {"$ref": "#a"}}, "default": {"$ref": _REMOTE}},
        [{"q": 1}],
        False,
        id="an-anchor-followed",
    ),
    pytest.param(
        {"$defs": {"a/b": {"$ref": _REMOTE}, "c~d": {}}, "properties": {"q": {"$ref": "#/$defs/a~1b"}, "r": {"$ref": "#/$defs/c~0d"}}},
        [{"q": 1, "r": 1}],
        True,
        id="escaped-pointer-to-a-remote-reference",
    ),
    pytest.param(
        {"$defs": {"a b": {"type": "integer"}, "c~d": {}}, "properties": {"q": {"$ref": "#/$defs/a%20b"}, "r": {"$ref": "#/$defs/c~0d"}}},
        [{"q": 1, "r": 1}],
        False,
        id="escaped-and-percent-encoded-local-pointers",
    ),
    pytest.param(
        {"$defs": {"n": {"type": "object", "properties": {"next": {"$ref": "#/$defs/n"}}}}, "$ref": "#/$defs/n"},
        [{"next": {"next": {}}}],
        False,
        id="a-cycle-of-local-references",
    ),
    pytest.param(
        {"$defs": {"n": {"properties": {"next": {"$ref": "#/$defs/n"}, "x": {"$ref": _REMOTE}}}}, "$ref": "#/$defs/n"},
        [{"next": {}, "x": 1}],
        True,
        id="a-remote-reference-in-a-cycle",
    ),
    pytest.param(
        {"$id": "https://example.com/s.json", "$defs": {"x": {"type": "integer"}}, "properties": {"q": {"$ref": "https://example.com/s.json#/$defs/x"}}},
        [{"q": 1}],
        False,
        id="an-absolute-reference-to-the-schema-itself",
    ),
    pytest.param(
        {"$id": _ROOT, "$defs": {"o": {"$id": "other.json", "type": "integer"}}, "properties": {"q": {"$ref": "other.json"}}},
        [{"q": 1}],
        True,
        # The structured-output parse reads draft 7, whose resources are not under ``$defs``.
        id="a-nested-id-under-defs",
    ),
    pytest.param(
        {"$id": _ROOT, "definitions": {"o": {"$id": "other.json"}}, "$defs": {"o": {"$id": "other.json"}}, "properties": {"q": {"$ref": "other.json"}}},
        [{"q": 1}],
        False,
        id="a-nested-id-both-dialects-register",
    ),
    pytest.param({"properties": {"q": {"$ref": _DRAFT7}}}, [{"q": {}}], False, id="a-bundled-meta-schema"),
    pytest.param({"$id": _ROOT, "properties": {"q": {"$ref": "defs.json#/x"}}}, [{"q": 1}], True, id="a-relative-reference-to-another-document"),
    pytest.param(
        {"type": "object", "properties": {"q": {"type": "object"}}, "x-meta": {"$ref": _REMOTE}, "not": {"required": ["forbidden"]}},
        [{"q": {}}],
        False,
        id="unknown-keyword-beside-a-non-monotone-one",
    ),
    # Resolving another document crawls the whole schema first, and the crawl raises on
    # a "$schema" that is not a string: the check fails, nothing is refused.
    pytest.param(
        {"type": "object", "properties": {"a": {"$schema": {"x": 1}}, "b": {"$ref": _REMOTE}}},
        [{"b": 1}],
        False,
        id="a-schema-the-registry-cannot-crawl",
    ),
]


@pytest.mark.parametrize(("schema", "instances", "refused"), _AUDIT_CASES)
def test_the_audit_names_a_reference_exactly_when_an_output_check_refuses_it(monkeypatch, schema, instances, refused):
    assert _refused_by_the_checks(monkeypatch, schema, instances) is refused
    assert references_another_document(schema) is refused


_CONTAINED = {
    "$id": _ROOT,
    "$defs": {
        "d": {"$id": "defs.json", "type": "integer"},
        "n": {"$id": "dir/nested.json", "properties": {"q": {"$ref": "defs.json"}}},
    },
    "properties": {"n": {"$ref": "dir/nested.json"}},
}


# (schema, instances, the references a 2020-12 validator refuses): a reference is
# resolved against the base the validator has where it meets it.
_BASE_CASES = [
    # Inside dir/nested.json, "defs.json" is dir/defs.json, whatever the root registers.
    pytest.param(_CONTAINED, [{"n": {"q": 1}}], ["defs.json"], id="the-containing-resource-base"),
    pytest.param(
        {**_CONTAINED, "$defs": {**_CONTAINED["$defs"], "d": {"$id": "dir/defs.json", "type": "integer"}}},
        [{"n": {"q": 1}}],
        [],
        id="a-sibling-of-the-containing-resource",
    ),
    pytest.param(
        {"$id": _ROOT, "$defs": {"a": {"$id": "sub/a.json", "type": "integer"}}, "properties": {"q": {"$id": "sub/", "$ref": "a.json"}}},
        [{"q": 1}],
        [],
        id="descend-moves-to-the-subschemas-own-id",
    ),
    pytest.param(
        {"$id": _ROOT, "$defs": {"a": {"$id": "sub/a.json", "type": "integer"}}, "not": {"$id": "sub/", "$ref": "a.json"}},
        [{"q": 1}],
        ["a.json"],
        id="evolve-keeps-the-parents-base",
    ),
    pytest.param(
        {"$id": _ROOT, "$defs": {"a": {"$id": "sub/a.json"}}, "allOf": [{"$id": "sub/", "$ref": "a.json"}]},
        [{"z": 1}],
        [],
        id="all-of-descends",
    ),
    pytest.param(
        {"$id": _ROOT, "$defs": {"a": {"$id": "sub/a.json"}}, "allOf": [{"$id": "sub/", "$ref": "a.json"}], "unevaluatedProperties": False},
        [{"z": 1}],
        ["a.json"],
        id="unevaluated-properties-rereads-in-place",
    ),
]


@pytest.mark.parametrize(("schema", "instances", "refused"), _BASE_CASES)
def test_a_reference_is_resolved_against_the_base_the_validator_has_where_it_meets_it(monkeypatch, schema, instances, refused):
    with _refusals(monkeypatch) as met:
        for instance in instances:
            output_schema_errors(output_schema_validator(schema, jsonschema.Draft202012Validator), instance)
    # Met inside an unevaluated* keyword, a refusal is reported under that keyword.
    assert bool(met) is bool(refused)
    walk = schema_guard.refused_references(output_schema_validator(schema, jsonschema.Draft202012Validator))
    assert (sorted(set(walk.refused)), walk.complete) == (refused, True)


def test_the_reference_walk_is_bounded_and_says_when_it_stopped(monkeypatch):
    schema = {"type": "object", "properties": {f"p{index}": {"type": "string"} for index in range(50)}}
    schema["properties"]["p49"] = {"$ref": _REMOTE}
    validator = output_schema_validator(schema, jsonschema.Draft202012Validator)
    assert schema_guard.refused_references(validator, max_visits=10).complete is False
    assert schema_guard.refused_references(validator).refused == (_REMOTE,)
    # Past its budget, a reference outside "#" anywhere counts; a schema of local references stays clean.
    monkeypatch.setattr(schema_guard, "REFERENCE_WALK_MAX_VISITS", 10)
    assert references_another_document(schema) is True
    local = {"type": "object", "$defs": {"x": {}}, "properties": {f"p{index}": {"$ref": "#/$defs/x"} for index in range(50)}}
    assert references_another_document(local) is False


@pytest.mark.parametrize(
    "schema",
    [
        pytest.param([{"$ref": _REMOTE}], id="a-list"),
        pytest.param({"properties": [{"$ref": _REMOTE}]}, id="properties-a-list"),
        pytest.param({"type": "object", "properties": {"q": {"$ref": {"a": 1}}}}, id="a-reference-not-a-string"),
        pytest.param({"$schema": {"a": 1}, "properties": {"q": {"$ref": _REMOTE}}}, id="an-unhashable-schema-keyword"),
        pytest.param({"$id": 3, "properties": {"q": {"$id": [], "$ref": _REMOTE}}}, id="ids-not-strings"),
    ],
)
def test_the_audit_answers_on_a_schema_the_checks_cannot_read(schema):
    assert isinstance(references_another_document(schema), bool)


def test_the_audit_reads_the_schema_the_finalize_check_extracts_from_a_provider_wrapper():
    inner = {"type": "object", "properties": {"q": {"$dynamicRef": _REMOTE}}}
    assert references_another_document({"type": "json_schema", "json_schema": {"name": "x", "schema": inner}}) is True
    assert references_another_document({"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object"}}}) is False


def _seeded_output_schema_patterns() -> set[str]:
    found: set[str] = set()
    for path in sorted((_BACKEND_DIR / "app" / "agents" / "seed_data" / "agent_output_schema").glob("*.json")):
        pending = [json.loads(path.read_text(encoding="utf-8"))]
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


_TRICKY_PATTERNS = [
    r"a{2}",
    r"a{,2}",
    r"a{2,}",
    r"a{1,3}?",
    r"a{}",
    r"x{e<=1}",
    r"{",
    r"a{2",
    r"a{ 1}",
    r"\{{2}",
    r"[]a]",
    r"[^]a]",
    r"[[a]",
    r"[a-[]",
    r"[\[:alpha:]]",
    r"[:alpha:]",
    r"\N{LATIN SMALL LETTER A}",
    r"[\N{LATIN SMALL LETTER A}-c]",
    r"(?#{e} [[:x:]])b",
    r"(?i:ab)c",
    r"(?-i:a)",
    r"(?a)\w+",
    r"\bword\b",
    r"^\d{4}-\d{2}$",
    r"[\w.-]+@[\w-]+",
    r"^(?P<n>a)(?P=n)$",
    r"(a)?(?(1)b|c)",
    r"\x41\u0042\101",
    r"^[\\\]\[]+$",
    "^a{\u0663}$",
    r"(?x) a b # {e}",
    r"(?x:a b)c",
]
_TEXTS = [
    "",
    "a",
    "aa",
    "aaa",
    "A",
    "b",
    "ab",
    "abc",
    "Abc",
    "{",
    "a{2",
    "a{ 1}",
    "{{",
    "x{e<=1}",
    "]",
    "[",
    "[]",
    ":",
    "c",
    "word",
    "a word here",
    "2024-01",
    "x.y@host-1",
    "ABA",
    "\\][",
    "\u00e9",
    "\u00b2",
    "e\u0301",
    "\u0663",
    "a{\u0663}",
    "\x1c",
    "\u0130",
    "\u0131",
    "i",
    "I",
    "cpe:2.3:a:vendor:product:1.0:*:*:*:*:*:*:*",
    "T12:00:00Z",
    "CVE-2024-12345",
    "indicator--8e2e2d2b-17d4-4cbf-938f-98ee46b3cd3f",
]


def test_the_bounded_search_never_gives_a_verdict_re_would_not():
    judged = 0
    for pattern in sorted(_seeded_output_schema_patterns()) + _TRICKY_PATTERNS:
        for text in _TEXTS:
            try:
                expected = re.search(pattern, text) is not None
            except re.error:
                with pytest.raises(re.error):
                    schema_guard.bounded_search(pattern, text, remaining=1.0)
                continue
            try:
                found = schema_guard.bounded_search(pattern, text, remaining=1.0)
            except schema_guard.PatternUnjudgedError:
                continue
            assert found is expected, (pattern, text)
            judged += 1
    # Nearly every pair is judged: no verdict is the exception, not the way out.
    assert judged > 0.95 * (len(_seeded_output_schema_patterns()) + len(_TRICKY_PATTERNS)) * len(_TEXTS)


# -- unevaluatedProperties beside patternProperties (output checks) -----------
# The output checks count the keys a schema evaluates as jsonschema 4.26 does,
# with its one ``re.search`` asked through their judge.


def test_the_evaluated_keys_are_counted_as_the_pinned_jsonschema_counts_them():
    assert importlib.metadata.version("jsonschema") == "4.26.0", (
        "jsonschema changed: compare schema_guard._evaluated_property_keys and _judged_unevaluated_properties "
        "with its find_evaluated_property_keys_by_schema / unevaluatedProperties (2020-12 and 2019-09), and "
        "schema_guard._ReferenceWalk with where its keywords descend or evolve and what find_evaluated_* read, "
        "then update this pin"
    )


_UNEVALUATED_CASES = [
    pytest.param(
        {"type": "object", "properties": {"id": {}}, "patternProperties": {"^x-": {"type": "string"}}, "unevaluatedProperties": False},
        [{"id": 1, "x-a": "s"}, {"id": 1, "x-a": "s", "y": 2, "z": 3}, {"x-a": 3}],
        id="same-node",
    ),
    pytest.param(
        {"type": "object", "patternProperties": {"^x-": {}}, "unevaluatedProperties": {"type": "string", "minLength": 3, "pattern": "^q"}},
        [{"x-a": 1, "q1": "qqq"}, {"x-a": 1, "b": "ab", "c": 1}],
        id="unevaluated-schema-errors-per-key",
    ),
    pytest.param(
        {"type": "object", "properties": {"a": {}, "n": {"type": "object", "patternProperties": {"^x": {}}}}, "unevaluatedProperties": False},
        [{"a": 1, "n": {"xy": 1}}, {"a": 1, "zzz": 2}],
        id="unrelated-nested",
    ),
    pytest.param(
        {"type": "object", "allOf": [{"patternProperties": {"^meta_": {}}}], "anyOf": [{"patternProperties": {"^b_": {}}, "required": ["must"]}, {}], "unevaluatedProperties": False},
        [{"meta_x": 1, "b_y": 2}, {"meta_x": 1, "b_y": 2, "must": 1}],
        id="all-of-and-a-failing-any-of",
    ),
    pytest.param(
        {"type": "object", "if": {"required": ["kind"]}, "then": {"patternProperties": {"^k_": {}}}, "else": {"patternProperties": {"^e_": {}}}, "properties": {"kind": {}}, "unevaluatedProperties": False},
        [{"kind": 1, "k_a": 1}, {"kind": 1, "e_a": 1}, {"e_a": 1}, {"k_a": 1}],
        id="if-then-else",
    ),
    pytest.param(
        {"type": "object", "dependentSchemas": {"on": {"patternProperties": {"^d_": {}}}}, "properties": {"on": {}}, "unevaluatedProperties": False},
        [{"on": 1, "d_a": 1}, {"d_a": 1}],
        id="dependent-schemas",
    ),
    pytest.param(
        {"type": "object", "$defs": {"p": {"patternProperties": {"^r_": {}}}}, "$ref": "#/$defs/p", "unevaluatedProperties": False},
        [{"r_a": 1}, {"r_a": 1, "s": 2}],
        id="behind-a-reference",
    ),
    pytest.param(
        {"type": "object", "properties": {"inner": {"type": "object", "patternProperties": {"^i_": {}}, "unevaluatedProperties": False}}},
        [{"inner": {"i_a": 1}}, {"inner": {"i_a": 1, "j": 2}}],
        id="nested-unevaluated",
    ),
    pytest.param(
        {"type": "object", "patternProperties": {"^x-": {}}, "additionalProperties": {"q": {}}, "unevaluatedProperties": False},
        [{"x-a": 1, "q": 1}, {"x-a": 1, "r": 1}],
        id="additional-properties-object",
    ),
]


@pytest.mark.parametrize("base", [jsonschema.Draft201909Validator, jsonschema.Draft202012Validator])
@pytest.mark.parametrize(("schema", "instances"), _UNEVALUATED_CASES)
def test_unevaluated_properties_beside_pattern_properties_gives_jsonschema_verdicts(base, schema, instances):
    for instance in instances:
        expected = sorted((list(e.absolute_path), e.message, e.validator) for e in base(schema).iter_errors(instance))
        found = output_schema_errors(output_schema_validator(schema, base), instance)
        assert sorted((list(e.absolute_path), e.message, e.validator) for e in found) == expected, instance


@pytest.mark.parametrize("base", [jsonschema.Draft201909Validator, jsonschema.Draft202012Validator])
def test_a_pattern_properties_regex_with_no_verdict_leaves_unevaluated_properties_unjudged(base):
    schema = {"type": "object", "patternProperties": {"^(a|aa)+$": {}}, "unevaluatedProperties": False}
    assert output_schema_errors(output_schema_validator(schema, base), {"a" * 60 + "!": 1}) == []


def test_tool_argument_checks_keep_giving_unevaluated_properties_no_verdict_beside_pattern_properties():
    # Pre-validation and coercion never count keys against a regex.
    schema = {"type": "object", "properties": {"a": {}}, "patternProperties": {"^x": {}}, "unevaluatedProperties": False}
    assert validate_tool_arguments("t", {"a": 1, "z": 2}, schema) is None
    assert _already_valid({"a": 1, "z": 2}, {"anyOf": [schema, {"type": "null"}]}) is True
