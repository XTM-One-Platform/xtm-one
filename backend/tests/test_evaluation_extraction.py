"""The prediction extractor: the pure engine, the operation catalog with its chain typing and
limits, the last-successful-call source rule, and the catalog shared with the frontend.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.core.config import settings
from app.services.evaluation.extraction import failures as f
from app.services.evaluation.extraction import spec
from app.services.evaluation.extraction.engine import apply_extractor
from app.services.evaluation.extraction.failures import FAILURE_REASONS
from app.services.evaluation.extraction.limits import ExtractionLimits, current_limits
from app.services.evaluation.extraction.sources import resolve_source
from app.services.evaluation.extraction.spec import OPERATIONS
from tests.fixtures.extraction import adverse
from tests.fixtures.extraction import traces as t

# ── Engine: operations, limits, canonical bytes ──


HARVESTER_CHAIN: dict[str, Any] = {
    "source": {"kind": "agent_output"},
    "operations": [
        {"op": "parse_json"},
        {"op": "object_values"},
        {"op": "flatten"},
        {"op": "collect_keys", "path": ["predictions"]},
        {"op": "dedupe_sort"},
    ],
    "output_kind": "label_set",
}


def _limits(**overrides: int) -> ExtractionLimits:
    base = current_limits()
    values = {
        "max_operations": base.max_operations,
        "max_path_segments": base.max_path_segments,
        "max_source_bytes": base.max_source_bytes,
        "max_sample_bytes": base.max_sample_bytes,
        "max_parse_depth": base.max_parse_depth,
        "max_values": base.max_values,
        "max_value_chars": base.max_value_chars,
    }
    values.update(overrides)
    return ExtractionLimits(**values)


def test_the_empty_harvester_case_is_a_success_not_a_failure() -> None:
    """`{"ttps": []}` extracts to an empty label set, scored as an empty prediction."""
    outcome = apply_extractor(HARVESTER_CHAIN, adverse.harvester_empty())
    assert not outcome.failed
    assert outcome.prediction == "[]"


def test_key_order_does_not_change_the_prediction() -> None:
    document = json.loads(adverse.harvester_merged())
    reversed_document = dict(reversed(list(document.items())))
    first = apply_extractor(HARVESTER_CHAIN, adverse.harvester_merged())
    second = apply_extractor(HARVESTER_CHAIN, json.dumps(reversed_document))
    assert first.prediction == second.prediction


# --- Deterministic failures ---------------------------------------------------


_COLLECT_CHAIN: dict[str, Any] = {
    "source": {"kind": "agent_output"},
    "operations": [
        {"op": "parse_json"},
        {"op": "collect_keys", "path": ["predictions"]},
        {"op": "dedupe_sort"},
    ],
    "output_kind": "label_set",
}

_SELECT_THEN_KEYS = {
    "source": {"kind": "agent_output"},
    "operations": [
        {"op": "parse_json"},
        {"op": "select_path", "path": ["absent"]},
        {"op": "object_keys"},
    ],
    "output_kind": "array",
}

_COLLECT_ON_PARSED = {
    "source": {"kind": "agent_output"},
    "operations": [
        {"op": "parse_json"},
        {"op": "collect_keys", "path": ["predictions"]},
    ],
    "output_kind": "array",
}


@pytest.mark.parametrize(
    ("chain", "payload", "reason", "fields", "message"),
    [
        (
            _COLLECT_CHAIN,
            json.dumps([{"other": {"a": 1}}]),
            f.SELECTION_EMPTY,
            {"operation": "collect_keys", "path": "predictions"},
            None,
        ),
        (
            _COLLECT_CHAIN,
            json.dumps(["not-an-object"]),
            f.TYPE_MISMATCH,
            {"expected_kind": "object", "actual_kind": "str"},
            None,
        ),
        (
            _COLLECT_CHAIN,
            json.dumps([{"predictions": ["T1059"]}]),
            f.TYPE_MISMATCH,
            {"actual_kind": "array"},
            "no keys to collect",
        ),
        (_COLLECT_ON_PARSED, json.dumps({"a": 1}), f.TYPE_MISMATCH, {}, None),
        (HARVESTER_CHAIN, adverse.UNPARSEABLE, f.PAYLOAD_UNPARSEABLE, {}, None),
        # A JSON scalar parses, so it is the wrong kind rather than unparseable.
        (HARVESTER_CHAIN, adverse.SCALAR_JSON, f.TYPE_MISMATCH, {}, None),
        (_SELECT_THEN_KEYS, adverse.ABSENT_PATH, f.SELECTION_EMPTY, {}, None),
    ],
    ids=[
        "collect_keys-missing-path",
        "collect_keys-non-object-item",
        "collect_keys-non-object-selected-value",
        "collect_keys-on-a-non-array",
        "unparseable-payload",
        "json-scalar",
        "select_path-absent-path",
    ],
)
def test_a_payload_the_chain_cannot_read_fails_with_its_reason(
    chain: dict[str, Any],
    payload: str,
    reason: str,
    fields: dict[str, str],
    message: str | None,
) -> None:
    outcome = apply_extractor(chain, payload)
    assert outcome.failed
    assert outcome.failure is not None
    assert outcome.failure.reason == reason
    for name, expected in fields.items():
        assert getattr(outcome.failure, name) == expected, name
    if message is not None:
        assert message in outcome.failure.message


# --- find_json: the JSON inside an answer ----------------------------

_FIND_THEN_PARSE: dict[str, Any] = {
    "source": {"kind": "agent_output"},
    "operations": [{"op": "find_json"}, {"op": "parse_json"}],
    "output_kind": "object",
}


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param('{"b": 2, "a": 1}', id="bare"),
        pytest.param('```json\n{"b": 2, "a": 1}\n```', id="code-fence"),
        pytest.param('Here it is:\n{"b": 2, "a": 1}\nHope it helps.', id="surrounding-text"),
        pytest.param('Draft:\n```\n```\nFinal:\n```json\n{"b": 2, "a": 1}\n```', id="empty-fence-first"),
    ],
)
def test_find_json_reads_the_payload_however_it_is_wrapped(answer: str) -> None:
    outcome = apply_extractor(_FIND_THEN_PARSE, answer)
    assert not outcome.failed
    assert json.loads(outcome.prediction) == {"a": 1, "b": 2}


def test_find_json_without_a_payload_fails_with_its_reason() -> None:
    outcome = apply_extractor(_FIND_THEN_PARSE, "I found no indicators.")
    assert outcome.failure is not None
    assert outcome.failure.reason == f.PAYLOAD_UNPARSEABLE
    assert outcome.failure.operation == "find_json"
    assert "no JSON object or array" in outcome.failure.message


def test_find_json_refuses_a_value_that_is_not_text() -> None:
    """Save-time typing lets ``parse_json`` then ``find_json`` through (``parse_json``
    produces whichever kind the document is), so the run-time check is reachable."""
    chain = {
        "source": {"kind": "agent_output"},
        "operations": [{"op": "parse_json"}, {"op": "find_json"}, {"op": "parse_json"}],
        "output_kind": "object",
    }
    outcome = apply_extractor(chain, '{"a": 1}')
    assert outcome.failure is not None
    assert outcome.failure.reason == f.TYPE_MISMATCH
    assert outcome.failure.operation == "find_json"


def test_find_json_finds_but_never_repairs() -> None:
    """A broken payload in a fence is found, then refused by ``parse_json``, as before."""
    outcome = apply_extractor(_FIND_THEN_PARSE, '```json\n{"a": 1,}\n```')
    assert outcome.failure is not None
    assert outcome.failure.reason == f.PAYLOAD_UNPARSEABLE
    assert outcome.failure.operation == "parse_json"


# --- Limits enforced before accumulation -------------------------------------


def test_collect_keys_refuses_before_building_the_oversized_value() -> None:
    limits = _limits(max_values=50)
    payload = adverse.expanding_payload(keys=20, per_key=40)  # 800 labels
    chain = {
        "source": {"kind": "agent_output"},
        "operations": [
            {"op": "parse_json"},
            {"op": "object_values"},
            {"op": "collect_keys", "path": ["predictions"]},
            {"op": "dedupe_sort"},
        ],
        "output_kind": "label_set",
    }
    outcome = apply_extractor(chain, payload, limits=limits)
    assert outcome.failed
    assert outcome.failure is not None
    assert outcome.failure.reason == f.LIMIT_EXCEEDED
    assert outcome.failure.operation == "collect_keys"
    # "would produce" — the projected size, refused before the list that big exists.
    assert "would produce" in outcome.failure.message


def test_flatten_refuses_before_building_the_oversized_value() -> None:
    limits = _limits(max_values=10)
    document = {"a": [[1] * 50]}
    chain = {
        "source": {"kind": "agent_output"},
        "operations": [
            {"op": "parse_json"},
            {"op": "object_values"},
            {"op": "flatten"},
            {"op": "flatten"},
        ],
        "output_kind": "array",
    }
    outcome = apply_extractor(chain, json.dumps(document), limits=limits)
    assert outcome.failed
    assert outcome.failure is not None
    assert outcome.failure.reason == f.LIMIT_EXCEEDED


# --- Blank and non-string labels --------------------------------------------


@pytest.mark.parametrize(
    "document",
    [
        {"a": {"predictions": {"": 1.0}}},
        {"a": {"predictions": {"   ": 1.0}}},
    ],
)
def test_blank_labels_are_refused_not_dropped(document: Any) -> None:
    chain = {
        "source": {"kind": "agent_output"},
        "operations": [
            {"op": "parse_json"},
            {"op": "object_values"},
            {"op": "collect_keys", "path": ["predictions"]},
            {"op": "dedupe_sort"},
        ],
        "output_kind": "label_set",
    }
    outcome = apply_extractor(chain, json.dumps(document))
    assert outcome.failed
    assert outcome.failure is not None
    assert outcome.failure.reason == f.LABEL_INVALID


@pytest.mark.parametrize(
    "payload", [adverse.non_string_labels(), adverse.nested_object_labels()]
)
def test_non_string_labels_are_refused_not_coerced(payload: str) -> None:
    chain = {
        "source": {"kind": "agent_output"},
        "operations": [
            {"op": "parse_json"},
            {"op": "select_path", "path": ["values"]},
            {"op": "dedupe_sort"},
        ],
        "output_kind": "label_set",
    }
    outcome = apply_extractor(chain, payload)
    assert outcome.failed
    assert outcome.failure is not None
    assert outcome.failure.reason == f.LABEL_INVALID


# --- Canonical bytes, and no locale collation --------------------------------


def test_canonical_bytes_are_fixed() -> None:
    """A hard-coded expectation, not a recomputation of the same call."""
    chain = {
        "source": {"kind": "agent_output"},
        "operations": [
            {"op": "parse_json"},
            {"op": "select_path", "path": ["values"]},
            {"op": "dedupe_sort"},
        ],
        "output_kind": "label_set",
    }
    payload = json.dumps({"values": ["Zebra", "apple", "Ärger", "Éclair", "apple"]})
    outcome = apply_extractor(chain, payload)
    assert not outcome.failed
    assert outcome.prediction is not None
    # Code-point order: uppercase ASCII, lowercase ASCII, then the Latin-1 range.
    assert outcome.prediction.encode("utf-8") == (
        b'["Zebra","apple","\xc3\x84rger","\xc3\x89clair"]'
    )


def test_object_output_sorts_keys_recursively_and_keeps_array_order() -> None:
    chain = {
        "source": {"kind": "agent_output"},
        "operations": [{"op": "parse_json"}],
        "output_kind": "object",
    }
    payload = '{"b": {"z": 1, "a": [3, 1, 2]}, "a": 2}'
    outcome = apply_extractor(chain, payload)
    assert outcome.prediction == '{"a":2,"b":{"a":[3,1,2],"z":1}}'


def test_the_package_is_declarative_and_locale_free() -> None:
    """Source tripwire: no collation, no dynamic evaluation, a closed source enum.

    Collation code is the only way the canonical bytes could vary by locale:
    switching ``LC_ALL`` would prove nothing, since ``str`` comparison and
    ``json.dumps(sort_keys=True)`` are code-point ordered. Extractors are data, so
    nothing in the package evaluates a string or loads code. Scanned as an AST so
    the comments explaining the bans cannot trip or satisfy it.
    """
    import ast
    from pathlib import Path

    import app.services.evaluation.extraction as package
    from app.services.evaluation.extraction import spec as spec_module

    assert set(spec_module.SOURCE_KINDS) == {
        "agent_output",
        "tool_output",
        "tool_input",
    }

    # Any use of these names, called or referenced as an attribute.
    collation_names = {"strxfrm", "strcoll", "setlocale"}
    # Bare-name calls only: an attribute such as ``re.compile`` stays allowed.
    banned_calls = {"eval", "exec", "compile", "__import__", "getattr", "setattr"}
    banned_modules = {"locale", "importlib", "subprocess", "pickle"}
    for module in Path(package.__file__).parent.glob("*.py"):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in banned_modules, (
                        f"{module.name} imports {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in banned_modules, (
                    f"{module.name} imports from {node.module}"
                )
            elif isinstance(node, ast.Attribute):
                assert node.attr not in collation_names, (
                    f"{module.name} uses {node.attr}"
                )
            elif isinstance(node, ast.Name):
                assert node.id not in collation_names, f"{module.name} uses {node.id}"
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in banned_calls, (
                    f"{module.name} calls {node.func.id}()"
                )


# --- The canonical string parses back --------------------------------------


def test_canonical_object_parses_back_to_the_scored_document() -> None:
    chain = {
        "source": {"kind": "agent_output"},
        "operations": [{"op": "parse_json"}],
        "output_kind": "object",
    }
    payload = adverse.dashboard_payload()
    outcome = apply_extractor(chain, payload)
    assert outcome.prediction is not None
    assert json.loads(outcome.prediction) == json.loads(payload)


# --- Every limit, at the bound and one over it ------------------------------
#
# The at-bound half catches an off-by-one that refuses a legal payload and
# silently fails valid work; the over-bound half proves the bound is enforced.

_SELECT_VALUES: dict[str, Any] = {
    "source": {"kind": "agent_output"},
    "operations": [
        {"op": "parse_json"},
        {"op": "select_path", "path": ["values"]},
        {"op": "dedupe_sort"},
    ],
    "output_kind": "label_set",
}

_KEYS_OF: dict[str, Any] = {
    "source": {"kind": "agent_output"},
    "operations": [{"op": "parse_json"}, {"op": "object_keys"}],
    "output_kind": "array",
}

_AT_BYTES = json.dumps({"values": ["T1059.001"]})
_TEN_VALUES = [f"T{index:05d}" for index in range(10)]


@pytest.mark.parametrize(
    ("chain", "at_bound", "over_bound", "prediction", "message"),
    [
        (
            _SELECT_VALUES,
            (_AT_BYTES, {"max_source_bytes": len(_AT_BYTES.encode("utf-8"))}),
            (
                json.dumps({"values": ["T1059.0011"]}),
                {"max_source_bytes": len(_AT_BYTES.encode("utf-8"))},
            ),
            '["T1059.001"]',
            "before parsing",
        ),
        (
            # Depth counts the root as 1 and includes the leaf scalar, so
            # {"a": {"b": {"c": 1}}} is depth 4: three objects plus the value.
            _KEYS_OF,
            (json.dumps({"a": {"b": {"c": 1}}}), {"max_parse_depth": 4}),
            (json.dumps({"a": {"b": {"c": 1}}}), {"max_parse_depth": 3}),
            '["a"]',
            None,
        ),
        (
            _SELECT_VALUES,
            (json.dumps({"values": _TEN_VALUES}), {"max_values": 10}),
            (json.dumps({"values": _TEN_VALUES}), {"max_values": 9}),
            json.dumps(sorted(_TEN_VALUES), separators=(",", ":")),
            None,
        ),
        (
            _SELECT_VALUES,
            (json.dumps({"values": ["ABCDE"]}), {"max_value_chars": 5}),
            (json.dumps({"values": ["ABCDE"]}), {"max_value_chars": 4}),
            '["ABCDE"]',
            None,
        ),
    ],
    ids=["max_source_bytes", "max_parse_depth", "max_values", "max_value_chars"],
)
def test_a_payload_at_a_limit_is_accepted_and_one_over_is_refused(
    chain: dict[str, Any],
    at_bound: tuple[str, dict[str, int]],
    over_bound: tuple[str, dict[str, int]],
    prediction: str,
    message: str | None,
) -> None:
    payload, overrides = at_bound
    accepted = apply_extractor(chain, payload, limits=_limits(**overrides))
    assert not accepted.failed, accepted.failure
    assert accepted.prediction == prediction

    payload, overrides = over_bound
    refused = apply_extractor(chain, payload, limits=_limits(**overrides))
    assert refused.failed
    assert refused.failure is not None
    assert refused.failure.reason == f.LIMIT_EXCEEDED
    if message is not None:
        assert message in refused.failure.message


@pytest.mark.parametrize(
    ("chain", "payload", "overrides", "message"),
    [
        (
            HARVESTER_CHAIN,
            adverse.oversized_source(2_048),
            {"max_source_bytes": 2_048},
            "before parsing",
        ),
        (_KEYS_OF, adverse.deeply_nested(40), {"max_parse_depth": 8}, None),
        (
            _SELECT_VALUES,
            json.dumps({"values": ["ok", "far-too-long"]}),
            {"max_value_chars": 5},
            None,
        ),
    ],
    ids=["oversized-source", "40-level-nesting", "one-long-label-among-short"],
)
def test_a_hostile_payload_is_refused_at_its_limit(
    chain: dict[str, Any], payload: str, overrides: dict[str, int], message: str | None
) -> None:
    outcome = apply_extractor(chain, payload, limits=_limits(**overrides))
    assert outcome.failed
    assert outcome.failure is not None
    assert outcome.failure.reason == f.LIMIT_EXCEEDED
    if message is not None:
        assert message in outcome.failure.message


# --- Unknown kinds, parse bounds, failure tally ----------------------


def test_an_unknown_output_kind_fails_instead_of_serialising_as_an_array() -> None:
    """A stored extractor is never re-validated, so canonicalise is the last gate."""
    chain = {
        "source": {"kind": "agent_output"},
        "operations": [{"op": "parse_json"}, {"op": "object_keys"}],
        "output_kind": "text",
    }
    outcome = apply_extractor(chain, json.dumps({"a": 1}))
    assert outcome.failed
    assert outcome.failure is not None
    assert outcome.failure.reason == f.TYPE_MISMATCH
    assert "not a kind this version can serialise" in outcome.failure.message


def test_an_unknown_source_kind_is_a_type_mismatch_not_missing_material() -> None:
    from app.services.evaluation.extraction.sources import resolve_source

    text, failure = resolve_source(
        {"kind": "tool_call", "tool_name": "x"}, "prose", {"traces": []}
    )
    assert text is None
    assert failure is not None
    assert failure.reason == f.TYPE_MISMATCH
    assert "unknown source kind" in failure.message


def test_parse_json_bounds_the_top_level_container() -> None:
    """parse_json is an expansion point, so the cardinality bound applies there."""
    payload = json.dumps([{"a": i} for i in range(12)])
    chain = {
        "source": {"kind": "agent_output"},
        "operations": [{"op": "parse_json"}],
        "output_kind": "array",
    }
    outcome = apply_extractor(chain, payload, limits=_limits(max_values=10))
    assert outcome.failed
    assert outcome.failure is not None
    assert outcome.failure.reason == f.LIMIT_EXCEEDED
    assert outcome.failure.operation == "parse_json"

    at_bound = apply_extractor(chain, payload, limits=_limits(max_values=12))
    assert not at_bound.failed, at_bound.failure


def test_the_per_run_failure_tally_counts_by_reason() -> None:
    """The run-level log line is built from this tally, one entry at a time."""
    from app.services.evaluation.extraction.pipeline import tally_extraction_failure

    counts: dict[str, int] = {}
    tally_extraction_failure({"quality.exact_match": {"score": 1.0}}, counts)
    assert counts == {}, "a scored entry contributes nothing"

    failed = {
        f.EXTRACTION_FAILED_KEY: {"error": {"reason": f.SOURCE_NOT_PRESENT}},
    }
    tally_extraction_failure(failed, counts)
    tally_extraction_failure(failed, counts)
    tally_extraction_failure(
        {f.EXTRACTION_FAILED_KEY: {"error": {"reason": f.TYPE_MISMATCH}}}, counts
    )
    assert counts == {f.SOURCE_NOT_PRESENT: 2, f.TYPE_MISMATCH: 1}


def test_every_catalogued_operation_has_a_dispatch_branch() -> None:
    """Every operation the catalog accepts has a dispatch branch in ``_apply_one``.

    Save-time validation reads the catalog, while dispatch is a hand-written
    if-chain ending in ``raise AssertionError``. That error is not a typed failure:
    the engine's per-entry ``except Exception`` records the entry as scored, so a
    missing branch would yield a zero indistinguishable from a bad answer.
    """
    for op in OPERATIONS:
        extractor = {
            "source": {"kind": "agent_output"},
            # ``path`` is ignored by the operations that do not take one.
            "operations": [{"op": op, "path": ["a"]}],
            "output_kind": "array",
        }
        # An unhandled operation raises here rather than returning; that is the
        # detection. The assertions below only pin that what comes back is a
        # well-formed outcome either way.
        outcome = apply_extractor(extractor, '{"a": ["x"]}')
        if outcome.failure is None:
            assert outcome.prediction is not None, op
        else:
            assert outcome.failure.reason in f.FAILURE_REASONS, op


# ── Catalog, chain typing and limits ──


def _extractor(operations: list[dict[str, Any]], output_kind: str) -> dict[str, Any]:
    return {
        "source": {"kind": "agent_output"},
        "operations": operations,
        "output_kind": output_kind,
    }


LABEL_CHAIN = [
    {"op": "parse_json"},
    {"op": "object_values"},
    {"op": "flatten"},
    {"op": "collect_keys", "path": ["predictions"]},
    {"op": "dedupe_sort"},
]

# The catalog is CLOSED at eight first-order operations (``find_json`` among them).
# This literal is the tripwire: adding a ninth fails here, so admitting one is a
# deliberate act.
EXPECTED_CATALOG = {
    "find_json",
    "parse_json",
    "select_path",
    "object_keys",
    "object_values",
    "collect_keys",
    "flatten",
    "dedupe_sort",
}


def test_the_catalog_is_closed_at_eight_first_order_operations() -> None:
    assert set(spec.OPERATIONS) == EXPECTED_CATALOG
    assert len(spec.OPERATIONS) == 8


# --- Every kind transition ----------------------------------------------------


def test_every_operation_declares_both_kinds() -> None:
    for op in spec.OPERATIONS:
        accepts, produces = spec._CATALOG[op]
        assert accepts in spec.KINDS
        assert produces is None or produces in spec.KINDS


@pytest.mark.parametrize("op", sorted(spec.OPERATIONS))
def test_every_kind_transition_is_enforced(op: str) -> None:
    """Each operation is accepted after a producer of its kind and refused otherwise.

    Table-driven over the catalog, so a new operation cannot be added without being
    checked here.
    """
    accepts, _ = spec._CATALOG[op]

    # A prefix that ends in each kind, so every operation can be tried after each.
    prefixes: dict[str, list[dict[str, Any]]] = {
        spec.TEXT: [],
        spec.OBJECT: [{"op": "parse_json"}],
        spec.ARRAY: [{"op": "parse_json"}, {"op": "object_keys"}],
        spec.LABEL_SET: [
            {"op": "parse_json"},
            {"op": "object_keys"},
            {"op": "dedupe_sort"},
        ],
    }

    for kind, prefix in prefixes.items():
        chain = [*prefix, {"op": op}]
        if op in spec.PATH_OPERATIONS:
            chain[-1]["path"] = ["a"]
        problem = spec.validate_extractor_chain(_extractor(chain, "array"))

        if kind == spec.OBJECT or kind == accepts:
            # An object prefix ends in parse_json, whose produced kind is only known
            # at run time, so the fold cannot refuse what follows it. Either way the
            # only refusal left is the declared output kind.
            assert problem is None or "output_kind" in problem, (op, kind, problem)
        else:
            assert problem is not None, f"{op} must be refused after {kind}"
            assert "accepts" in problem, (op, kind, problem)


def test_a_disconnected_chain_is_refused_naming_the_position() -> None:
    problem = spec.validate_extractor_chain(
        _extractor([{"op": "parse_json"}, {"op": "flatten"}, {"op": "object_keys"}], "array")
    )
    assert problem is not None
    assert "operations[2]" in problem
    assert "object_keys" in problem
    assert "accepts object" in problem and "receives array" in problem


def test_a_valid_label_chain_passes() -> None:
    assert spec.validate_extractor_chain(_extractor(LABEL_CHAIN, "label_set")) is None


def test_the_declared_output_kind_must_match_the_chain() -> None:
    problem = spec.validate_extractor_chain(
        _extractor([{"op": "parse_json"}, {"op": "object_keys"}], "label_set")
    )
    assert problem is not None
    assert "output_kind" in problem


# --- Empty operation list -----------------------------------------------------


def test_empty_operation_list_is_rejected() -> None:
    problem = spec.validate_extractor_chain(_extractor([], "object"))
    assert problem is not None
    assert "must not be empty" in problem


# --- Sources ------------------------------------------------------------------


def test_unknown_source_kind_is_rejected() -> None:
    problem = spec.validate_extractor_chain(
        {
            "source": {"kind": "database"},
            "operations": [{"op": "parse_json"}],
            "output_kind": "object",
        }
    )
    assert problem is not None
    assert "unknown source kind" in problem


# --- Refused at authoring time ---------------------------------------------

_TOOL_SOURCE = {"kind": "tool_output", "tool_name": "extract_ttp"}


@pytest.mark.parametrize(
    ("extractor", "family", "expected"),
    [
        (
            _extractor([{"op": "parse_json"}, {"op": "transmute"}], "label_set"),
            "CLASSIFICATION",
            "unknown operation",
        ),
        (
            {
                "source": _TOOL_SOURCE,
                "operations": [{"op": "parse_json"}],
                "output_kind": "object",
            },
            "PROSE",
            "not supported in this version",
        ),
        (
            _extractor([{"op": "parse_json"}], "text"),
            "CLASSIFICATION",
            "unknown output_kind",
        ),
        (
            {"source": {"kind": "agent_output"}, "operations": [{"op": "parse_json"}]},
            "CLASSIFICATION",
            "unknown output_kind",
        ),
        (
            {"source": {"kind": "agent_output"}, "output_kind": "object"},
            None,
            "operations must be a list",
        ),
        (
            {
                "source": {"kind": "tool_output"},
                "operations": [{"op": "parse_json"}],
                "output_kind": "object",
            },
            None,
            "requires a non-empty 'tool_name'",
        ),
        (
            {
                "source": {"kind": "agent_output", "tool_name": "extract_ttp"},
                "operations": [{"op": "parse_json"}],
                "output_kind": "object",
            },
            None,
            "does not take a 'tool_name'",
        ),
        # A blank container is a configured extractor, not the absent state (NULL),
        # so it is refused rather than stored as an extractor that selects nothing.
        ({}, "CLASSIFICATION", "source must be an object"),
        ([], "CLASSIFICATION", "extractor must be an object"),
    ],
    ids=[
        "unknown-operation",
        "prose-family",
        "text-output-kind",
        "missing-output-kind",
        "missing-operations",
        "tool-source-without-tool-name",
        "agent-output-with-tool-name",
        "blank-object",
        "blank-list",
    ],
)
def test_a_malformed_extractor_is_refused_at_authoring_time(
    extractor: Any, family: str | None, expected: str
) -> None:
    problem = spec.validate_extractor_chain(extractor, family=family)
    assert problem is not None
    assert expected in problem


# --- Family compatibility -----------------------------------------------------


@pytest.mark.parametrize(
    ("family", "output_kind", "ok"),
    [
        ("CLASSIFICATION", "label_set", True),
        ("CLASSIFICATION", "object", False),
        ("STRUCTURED_JSON", "object", True),
        ("STRUCTURED_JSON", "array", True),
        ("STRUCTURED_JSON", "label_set", False),
        ("PROSE", "object", False),
        ("STIX_BUNDLE", "object", False),
    ],
)
def test_v1_support_matrix(family: str, output_kind: str, ok: bool) -> None:
    problem = spec.validate_output_kind_for_family(output_kind, family)
    assert (problem is None) is ok
    if problem and family in ("PROSE", "STIX_BUNDLE"):
        assert "not supported in this version" in problem


def test_a_missing_family_is_refused_rather_than_assumed() -> None:
    assert spec.validate_output_kind_for_family("label_set", None) is not None


# --- Limit boundaries ---------------------------------------------------------


def test_operations_boundary() -> None:
    limits = _limits(max_operations=3)
    below = [{"op": "parse_json"}, {"op": "object_keys"}]
    at = [*below, {"op": "dedupe_sort"}]
    above = [*at, {"op": "flatten"}]

    assert spec.validate_extractor_chain(_extractor(below, "array"), limits=limits) is None
    assert (
        spec.validate_extractor_chain(_extractor(at, "label_set"), limits=limits) is None
    )
    problem = spec.validate_extractor_chain(_extractor(above, "label_set"), limits=limits)
    assert problem is not None and "at most 3" in problem


def test_path_segments_boundary() -> None:
    limits = _limits(max_path_segments=2)
    for count, expect_ok in ((1, True), (2, True), (3, False)):
        chain = [
            {"op": "parse_json"},
            {"op": "select_path", "path": ["a"] * count},
            {"op": "object_keys"},
        ]
        problem = spec.validate_extractor_chain(_extractor(chain, "array"), limits=limits)
        assert (problem is None) is expect_ok


def test_collect_keys_requires_a_path() -> None:
    problem = spec.validate_extractor_chain(
        _extractor(
            [{"op": "parse_json"}, {"op": "object_keys"}, {"op": "collect_keys"}],
            "array",
        )
    )
    assert problem is not None
    assert "collect_keys" in problem and "path" in problem


def test_a_bound_below_one_is_refused_rather_than_defaulted() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        _limits(max_values=0)
    with pytest.raises(ValueError, match="positive integer"):
        _limits(max_source_bytes=-1)


def test_limits_are_configurable_through_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overriding a setting moves the boundary — the configurability claim, pinned."""
    monkeypatch.setattr(settings, "extraction_max_operations", 2)
    chain = [{"op": "parse_json"}, {"op": "object_keys"}, {"op": "dedupe_sort"}]
    problem = spec.validate_extractor_chain(_extractor(chain, "label_set"))
    assert problem is not None and "at most 2" in problem

    monkeypatch.setattr(settings, "extraction_max_operations", 8)
    assert spec.validate_extractor_chain(_extractor(chain, "label_set")) is None


# ── The last-successful-call source rule ──


TOOL_SOURCE: dict[str, Any] = {"kind": "tool_output", "tool_name": t.TOOL_NAME}
INPUT_SOURCE: dict[str, Any] = {"kind": "tool_input", "tool_name": t.TOOL_NAME}
ANSWER_SOURCE: dict[str, Any] = {"kind": "agent_output"}


def _resolve(source: dict[str, Any], traces: list[dict[str, Any]]) -> tuple[Any, Any]:
    return resolve_source(source, "raw answer", {"traces": traces})


def test_three_successful_calls_resolve_to_the_last() -> None:
    payload, failure = _resolve(
        TOOL_SOURCE, t.three_successful_calls("first", "second", "third")
    )
    assert failure is None
    assert payload == "third"


def test_only_the_first_successful_resolves_to_the_first() -> None:
    """Order is not position: the last call that *succeeded* is the deliverable."""
    payload, failure = _resolve(TOOL_SOURCE, t.only_first_successful("first", "later"))
    assert failure is None
    assert payload == "first"


@pytest.mark.parametrize(
    ("source", "raw_answer", "trace_list", "message"),
    [
        (TOOL_SOURCE, "raw answer", t.none_successful("ignored"), t.TOOL_NAME),
        (TOOL_SOURCE, "raw answer", t.no_call_at_all(), t.TOOL_NAME),
        (TOOL_SOURCE, "raw answer", t.other_tool_only("payload"), t.TOOL_NAME),
        # No recorded outcome means not successful: fail closed, never assume.
        (TOOL_SOURCE, "raw answer", t.span_reconstructed("payload"), t.TOOL_NAME),
        (INPUT_SOURCE, "raw answer", t.span_reconstructed("payload"), t.TOOL_NAME),
        (ANSWER_SOURCE, "   ", [], "no answer"),
    ],
    ids=[
        "no-successful-call",
        "no-call-at-all",
        "a-different-tool-only",
        "span-reconstructed-tool-output-fails-closed",
        "span-reconstructed-tool-input-fails-closed",
        "empty-raw-answer",
    ],
)
def test_missing_material_is_source_not_present(
    source: dict[str, Any],
    raw_answer: str,
    trace_list: list[dict[str, Any]],
    message: str,
) -> None:
    payload, failure = resolve_source(source, raw_answer, {"traces": trace_list})
    assert payload is None
    assert failure is not None
    assert failure.reason == f.SOURCE_NOT_PRESENT
    assert message in failure.message


def test_tool_input_is_read_when_recorded() -> None:
    payload, failure = _resolve(INPUT_SOURCE, t.one_successful_call("out"))
    assert failure is None
    assert payload == "{}"


def test_the_raw_answer_is_returned_verbatim() -> None:
    payload, failure = resolve_source(ANSWER_SOURCE, '{"a": 1}', {"traces": []})
    assert failure is None
    assert payload == '{"a": 1}'


def test_no_configuration_field_can_change_the_selection() -> None:
    """The rule is fixed by the system, not exposed to the author.

    A source carries only ``kind`` and ``tool_name``; any other key is refused at
    save time, so no field can ask for the first call or an unsuccessful one.
    """
    from app.services.evaluation.extraction.spec import validate_extractor_chain

    problem = validate_extractor_chain(
        {
            "source": {
                "kind": "tool_output",
                "tool_name": t.TOOL_NAME,
                "occurrence": "first",
            },
            "operations": [{"op": "parse_json"}],
            "output_kind": "object",
        }
    )
    assert problem is not None
    assert "unknown source keys" in problem


def test_traces_may_be_passed_bare_or_inside_execution_details() -> None:
    bare, _ = resolve_source(TOOL_SOURCE, None, t.one_successful_call("payload"))
    wrapped, _ = resolve_source(
        TOOL_SOURCE, None, {"traces": t.one_successful_call("payload")}
    )
    assert bare == wrapped == "payload"


# ── The catalog shared with the frontend ──
# `frontend/src/components/evaluation/extractor-catalog.ts` mirrors
# `app/services/evaluation/extraction/spec.py`: the same operations with the same kind
# signatures, source kinds, output kinds, closed failure reasons and v1 support matrix.
# Both halves are checked against ONE table, `frontend/tests_e2e/fixtures/extractor-catalog.json`,
# which these tests write from the Python source and `tests_e2e/evaluation-extractor.spec.ts`
# asserts the TypeScript side against. A drift in either direction fails a test rather
# than a run.


FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "frontend"
    / "tests_e2e"
    / "fixtures"
    / "extractor-catalog.json"
)


def _catalog() -> dict:
    return {
        "operations": [
            {
                "op": op,
                "accepts": spec._CATALOG[op][0],
                "produces": spec._CATALOG[op][1],
            }
            for op in spec.OPERATIONS
        ],
        "source_kinds": list(spec.SOURCE_KINDS),
        "output_kinds": list(spec.OUTPUT_KINDS),
        "failure_reasons": sorted(FAILURE_REASONS),
        "supported_output_kinds": {
            family: sorted(spec.supported_output_kinds(family))
            for family in ("CLASSIFICATION", "STRUCTURED_JSON")
        },
        "path_operations": sorted(spec.PATH_OPERATIONS),
    }


def test_the_shared_fixture_matches_the_python_catalog() -> None:
    """Regenerate the table when the catalog changes, deliberately.

    The fixture is written here rather than by hand so it cannot lag the source;
    the failure message says exactly what to do.
    """
    current = _catalog()
    if not FIXTURE.exists() or json.loads(FIXTURE.read_text()) != current:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        raise AssertionError(
            f"{FIXTURE} was stale and has been regenerated. Re-run the suite, and "
            "update frontend/src/components/evaluation/extractor-catalog.ts to match."
        )
