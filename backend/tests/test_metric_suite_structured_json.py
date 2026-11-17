"""The STRUCTURED_JSON metric family: list-entry pairing, per-field accuracy and
scoring, JSON and JSON Schema validity, and the golden fixtures.
"""

from __future__ import annotations

import itertools
import json
from types import SimpleNamespace

import pytest

from app.evaluation_metrics.core.contracts import JSONValue, MetricDirection, MetricStatus
from app.evaluation_metrics.core.errors import ConfigurationError
from app.evaluation_metrics.quality.families.routing import (
    Family,
    FamilyConfig,
    run_for_family,
)
from app.evaluation_metrics.quality.families.structured_json.field_accuracy import (
    MAX_FIELD_DEPTH,
    json_field_accuracy,
)
from app.evaluation_metrics.quality.families.structured_json.pairing import (
    MAX_ENTRY_PRODUCT,
    MAX_PAIRS_SCORED,
    pair_entries,
)
from app.evaluation_metrics.quality.families.structured_json.validity import (
    json_schema_validity,
)
from app.evaluation_metrics.quality.normalization.policies import ComparisonPolicy
from app.services.evaluation.metric_package.quality_adapter import (
    PROSE_NORMALIZATION_POLICY,
    build_quality_config,
    run_quality,
)

# ── list-entry pairing ──
# Entries are paired for the most credit, so the order the model emitted them in
# never moves the score.

# The policy a real run uses: the platform's own prose policy, which is what makes a
# `free_text` field score comparably with a whole-answer prose metric.
POLICY = ComparisonPolicy(prose_policy=PROSE_NORMALIZATION_POLICY)

SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "severity": {"type": "string", "enum": ["low", "high"]},
                },
                "required": ["id", "severity"],
            },
        }
    },
    "required": ["findings"],
}

REFERENCE = {
    "findings": [
        {"id": "F-1", "severity": "high"},
        {"id": "F-2", "severity": "low"},
        {"id": "F-3", "severity": "high"},
    ]
}

# `T-a` and `T-b` differ from every reference entry in the same way, so several pairings
# earn the same credit.
TIED_PREDICTION = {
    "findings": [
        {"id": "F-2", "severity": "low"},
        {"id": "T-a", "severity": "high"},
        {"id": "T-b", "severity": "high"},
    ]
}


def test_every_permutation_scores_the_same_number():
    """Shuffling a list does not move the score.

    Asserted on the value, not the serialisation: a JSON pointer names a real position, so
    `/findings/1/id` refers to different content once the entries are reordered.
    """
    entries = TIED_PREDICTION["findings"]
    values = {
        json_field_accuracy(REFERENCE, {"findings": list(order)}, POLICY, SCHEMA).value
        for order in itertools.permutations(entries)
    }

    assert values == {200.0 / 3.0}, f"{len(values)} distinct scores across 6 permutations"


_NOISY_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "seen_at": {"type": "string"}},
            },
        }
    },
}
_NOISY_REFERENCE = {"title": "SSH brute force", "seen_at": "2024-01-01T00:00:00Z"}
#: The right finding, carrying a timestamp the author declared meaningless.
_RIGHT_ENTRY = {"title": "SSH brute force", "seen_at": "2024-06-06T00:00:00Z"}
#: The wrong finding, whose only merit is that its noise field happens to line up.
_WRONG_ENTRY = {"title": "Port scan", "seen_at": "2024-01-01T00:00:00Z"}


@pytest.mark.parametrize(
    "entries", [[_WRONG_ENTRY, _RIGHT_ENTRY], [_RIGHT_ENTRY, _WRONG_ENTRY]], ids=["wrong-first", "right-first"]
)
def test_an_ignored_path_follows_its_reference_entry(entries: list) -> None:
    """Pairing reads ``ignored_paths`` at the pointer scoring uses, and an extra entry takes
    none: the right finding is paired, in either order."""
    result = json_field_accuracy(
        {"findings": [_NOISY_REFERENCE]},
        {"findings": entries},
        ComparisonPolicy(ignored_paths=["/findings/0/seen_at"]),
        _NOISY_SCHEMA,
    )
    assert result.value == pytest.approx(100.0 / 3.0)


# ── per-field accuracy under a ComparisonPolicy ──
# Missing/extra/incorrect field diagnostics, the opt-in relaxations, and RFC 6901 pointer
# escaping in both reported and policy paths.

def test_exact_match_is_100() -> None:
    obj = {"name": "alpha", "score": 90, "tags": ["a", "b"]}
    result = json_field_accuracy(obj, dict(obj), ComparisonPolicy())
    assert result.status is MetricStatus.scored
    assert result.value == 100.0
    assert result.detail["exact_object_match"] is True
    assert result.detail["missing"] == ()
    assert result.detail["extra"] == ()
    assert result.detail["incorrect"] == ()


def test_missing_and_extra_fields() -> None:
    reference = {"name": "alpha", "score": 90}
    prediction = {"name": "alpha", "rank": 1}
    result = json_field_accuracy(reference, prediction, ComparisonPolicy())
    assert result.detail["missing"] == ("/score",)
    assert result.detail["extra"] == ("/rank",)
    assert result.detail["exact_object_match"] is False
    assert result.value is not None
    assert result.value < 100.0


def test_incorrect_value_counts_as_fp_and_fn() -> None:
    reference = {"name": "alpha", "score": 90}
    prediction = {"name": "alpha", "score": 88}
    result = json_field_accuracy(reference, prediction, ComparisonPolicy())
    assert result.detail["incorrect"] == ("/score",)
    # One of two fields correct -> 50.0
    assert result.value == 50.0


def test_numeric_tolerance_opt_in() -> None:
    reference = {"score": 90}
    prediction = {"score": 88}
    policy = ComparisonPolicy(numeric_tolerance={"/score": 5.0})
    result = json_field_accuracy(reference, prediction, policy)
    assert result.value == 100.0
    assert result.detail["incorrect"] == ()


def test_array_set_semantics_ignores_order() -> None:
    reference = {"tags": ["a", "b"]}
    prediction = {"tags": ["b", "a"]}
    strict = json_field_accuracy(reference, prediction, ComparisonPolicy())
    # positional: both indexes differ -> 0.0
    assert strict.value == 0.0
    as_set = json_field_accuracy(
        reference, prediction, ComparisonPolicy(array_semantics={"/tags": "set"})
    )
    assert as_set.value == 100.0


def test_type_coercion_opt_in() -> None:
    reference = {"score": 90}
    prediction = {"score": "90"}
    strict = json_field_accuracy(reference, prediction, ComparisonPolicy())
    assert strict.detail["incorrect"] == ("/score",)
    coerced = json_field_accuracy(
        reference, prediction, ComparisonPolicy(type_coercion={"/score": True})
    )
    assert coerced.value == 100.0


def test_ignored_paths_excluded() -> None:
    reference = {"name": "alpha", "ts": 1}
    prediction = {"name": "alpha", "ts": 999}
    policy = ComparisonPolicy(ignored_paths=["/ts"])
    result = json_field_accuracy(reference, prediction, policy)
    assert result.value == 100.0


def test_normalization_opt_in() -> None:
    reference = {"name": "Alpha"}
    prediction = {"name": "alpha"}
    policy = ComparisonPolicy(normalization={"/name": "lowercase"})
    result = json_field_accuracy(reference, prediction, policy)
    assert result.value == 100.0


def test_pointer_escaping_in_field_paths() -> None:
    reference = {"a/b": 1, "c~d": 2}
    prediction = {"a/b": 9, "c~d": 9}
    result = json_field_accuracy(reference, prediction, ComparisonPolicy())
    # "/" -> "~1", "~" -> "~0" per RFC 6901.
    assert result.detail["incorrect"] == ("/a~1b", "/c~0d")


def test_policy_paths_use_escaped_pointers() -> None:
    reference = {"a/b": 90}
    prediction = {"a/b": 88}
    # Policy key must match the escaped pointer for the field named "a/b".
    policy = ComparisonPolicy(numeric_tolerance={"/a~1b": 5.0})
    result = json_field_accuracy(reference, prediction, policy)
    assert result.value == 100.0
    assert result.detail["incorrect"] == ()


def test_a_partial_field_score_can_round_to_a_perfect_hundred() -> None:
    """A `matched` field can score above 0.995 without reaching 1.0.

    The per-field UI marks a field correct only on `score === 1`, so a consumer that
    rounds to whole percent must clamp or it prints "100%" beside "Incorrect". A
    200-token summary missing one token is the input that needs the clamp.
    """
    from app.evaluation_metrics.quality.normalization.prose import ProseNormalizationPolicy

    words = [f"w{i}" for i in range(200)]
    reference = {"summary": " ".join(words)}
    prediction = {"summary": " ".join(words[:-1])}
    schema = {"type": "object", "properties": {"summary": {"type": "string"}}}

    result = json_field_accuracy(
        reference,
        prediction,
        ComparisonPolicy(prose_policy=ProseNormalizationPolicy()),
        schema,
    )

    (field,) = result.detail["per_field"]
    assert field["mode"] == "free_text"
    assert field["status"] == "matched"
    assert 0.995 < field["score"] < 1.0
    assert round(field["score"] * 100) == 100


# ── per-field scoring on the contract fixtures ──
# Scores are asserted exactly: the metric is deterministic, so a number that moves without
# a stated reason is a regression.

# --- Fixtures -------------------------------------------------------------

SC001_SCHEMA = {
    "type": "object",
    "required": ["stage", "headline"],
    "properties": {
        "stage": {"type": "string", "enum": ["scoping", "active"]},
        "headline": {"type": "string"},
        "note": {"type": "string"},
    },
}
SC001_REFERENCE = {
    "stage": "scoping",
    "headline": "The host was compromised through a phishing email.",
    "note": "Reviewed with the platform team on Tuesday.",
}
SC001_PREDICTION = {
    "stage": "scoping",
    "headline": "A phishing email led to the host being compromised.",
}

DOUBLE_COUNT_SCHEMA = {
    "type": "object",
    "required": ["stage", "owner", "severity"],
    "properties": {
        "stage": {"type": "string", "enum": ["scoping", "active"]},
        "owner": {"type": "string", "enum": ["soc", "ir"]},
        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
        "note": {"type": "string"},
    },
}
DOUBLE_COUNT_REFERENCE = {
    "stage": "scoping",
    "owner": "soc",
    "severity": "high",
    "note": "n",
}
DOUBLE_COUNT_PREDICTION = {"stage": "scoping", "owner": "soc", "severity": "medium"}

LABELS_SCHEMA = {
    "type": "object",
    "required": ["tags"],
    "properties": {
        "tags": {
            "type": "array",
            "items": {"enum": ["malware", "phishing", "ransomware", "trojan"]},
        }
    },
}
LABELS_REFERENCE = {"tags": ["malware", "phishing", "ransomware"]}
LABELS_REORDERED = {"tags": ["ransomware", "malware", "phishing"]}
LABELS_TWO_OF_THREE = {"tags": ["malware", "phishing", "trojan"]}

FINDINGS_SCHEMA = {
    "type": "object",
    "required": ["key_findings"],
    "properties": {
        "key_findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["title", "severity"],
                "properties": {
                    "title": {"type": "string"},
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                },
            },
        }
    },
}
FINDINGS_REFERENCE = {
    "key_findings": [
        {"title": "Unpatched perimeter appliance", "severity": "high"},
        {"title": "Excessive service account privileges", "severity": "medium"},
    ]
}
FINDINGS_PREDICTION = {
    "key_findings": [
        {"title": "Service accounts hold excessive privileges", "severity": "medium"},
        {"title": "Perimeter appliance is unpatched", "severity": "high"},
    ]
}

FR040_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["stage", "severity", "owner", "summary"],
    "properties": {
        "stage": {"type": "string"},
        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
        "owner": {"type": "string"},
        "summary": {"type": "string"},
    },
}
FR040_REFERENCE = {
    "stage": "scoping",
    "severity": "high",
    "owner": "soc",
    "summary": "Perimeter appliance unpatched",
}
FR040_STRAY_KEY = {**FR040_REFERENCE, "debug_note": "internal"}
FR040_OFF_ENUM = {**FR040_REFERENCE, "severity": "extreme"}


# --- Optional absent field + faithful paraphrase ---------------------------


def test_a_paraphrase_earns_partial_credit_once() -> None:
    """`/headline` earns 70.6 from `prose_token_prf` (`email.` matches `email`),
    charged once, never as both a false positive and a false negative; the omitted `/note`
    is a miss: 56.9."""
    result = json_field_accuracy(
        SC001_REFERENCE, SC001_PREDICTION, POLICY, SC001_SCHEMA
    )
    assert result.status is MetricStatus.scored
    assert round(result.value, 4) == 56.8627
    assert result.detail["missing"] == ("/note",)
    assert "tp" not in result.detail
    assert "fp" not in result.detail
    assert "fn" not in result.detail


# --- One mistake, one penalty ---------------------------------------------


def test_a_single_wrong_field_is_penalised_exactly_once() -> None:
    """A present but wrong field counts once: two of four fields correct is 50.0."""
    result = json_field_accuracy(
        DOUBLE_COUNT_REFERENCE, DOUBLE_COUNT_PREDICTION, POLICY, DOUBLE_COUNT_SCHEMA
    )
    assert result.value == 50.0
    assert result.detail["incorrect"] == ("/severity",)
    assert result.detail["missing"] == ("/note",)
    assert set(result.detail["correct"]) == {"/owner", "/stage"}


def test_an_omitted_field_is_a_miss_required_or_not() -> None:
    """`required` is validity's concern: optional `/note` and required `/severity`
    are both misses."""
    result = json_field_accuracy(
        DOUBLE_COUNT_REFERENCE,
        {"stage": "scoping", "owner": "soc"},
        POLICY,
        DOUBLE_COUNT_SCHEMA,
    )
    assert result.detail["missing"] == ("/note", "/severity")
    assert result.value == 50.0


# --- Multi-value closed sets ----------------------------------------------


def test_reordered_complete_label_set_scores_full_marks() -> None:
    """Every expected label returned in another order scores 100."""
    result = json_field_accuracy(
        LABELS_REFERENCE, LABELS_REORDERED, POLICY, LABELS_SCHEMA
    )
    assert result.value == 100.0


def test_enum_set_ignores_a_positional_declaration() -> None:
    """A closed value set is unordered, even under a positional `array_semantics` declaration.

    Honouring the declaration would score a reordered complete set 0.0. An author who
    needs order says so per field through `field_modes`.
    """
    result = json_field_accuracy(
        LABELS_REFERENCE,
        LABELS_REORDERED,
        ComparisonPolicy(
            prose_policy=PROSE_NORMALIZATION_POLICY,
            array_semantics={"/tags": "positional"},
        ),
        LABELS_SCHEMA,
    )
    assert result.value == 100.0


def test_two_of_three_labels_earns_partial_credit() -> None:
    """Two of three labels earn partial credit by set F1 (66.7), credited by membership, not position."""
    result = json_field_accuracy(
        LABELS_REFERENCE, LABELS_TWO_OF_THREE, POLICY, LABELS_SCHEMA
    )
    assert 0.0 < result.value < 100.0
    assert round(result.value, 4) == 66.6667


# --- List of structured findings ------------------------------------------


def test_findings_list_is_paired_then_scored_field_by_field() -> None:
    """Reordered findings with paraphrased titles are paired, then scored field by field (88.1).

    Positional comparison would demand the same order and `set` byte-identical objects,
    so only pairing reaches the fields inside the array.
    """
    result = json_field_accuracy(
        FINDINGS_REFERENCE, FINDINGS_PREDICTION, POLICY, FINDINGS_SCHEMA
    )
    assert result.value > 50.0
    assert round(result.value, 4) == 88.0952

    pairing = result.detail["pairings"][0]
    # The entries were returned in the opposite order, and were paired across it.
    assert pairing["pairs"] == ((0, 1), (1, 0))
    assert pairing["unmatched_reference"] == ()
    assert pairing["unmatched_prediction"] == ()


# --- A schema violation does not erase the per-field score ----------------


def test_stray_key_reports_what_the_answer_got_right() -> None:
    """One undeclared key under `additionalProperties: false` scores `scored`/80.0, conformance 0.

    Four correct fields beside one spurious one is 4/5 under the weighted mean. Zeroing
    the per-field score would measure the schema's strictness, not the agent's answer.
    """
    results = run_for_family(
        FR040_REFERENCE,
        FR040_STRAY_KEY,
        Family.STRUCTURED_JSON,
        FamilyConfig(json_schema=FR040_SCHEMA),
    )
    field_accuracy = results["json.field_accuracy"]
    assert field_accuracy.status is MetricStatus.scored
    assert round(field_accuracy.value, 4) == 80.0
    # Conformance fails on the metric that measures it. The JSON parsed, so this is a
    # computed failure: `scored`/0, counted in the run aggregate.
    assert results["json.schema_validity"].status is MetricStatus.scored
    assert results["json.schema_validity"].value == 0.0


def test_off_enum_value_reports_the_three_fields_that_were_right() -> None:
    """An off-enum value inside one declared field scores `scored`/75.0 for the other three."""
    results = run_for_family(
        FR040_REFERENCE,
        FR040_OFF_ENUM,
        Family.STRUCTURED_JSON,
        FamilyConfig(json_schema=FR040_SCHEMA),
    )
    field_accuracy = results["json.field_accuracy"]
    assert field_accuracy.status is MetricStatus.scored
    assert field_accuracy.value == 75.0
    assert results["json.schema_validity"].status is MetricStatus.scored
    assert results["json.schema_validity"].value == 0.0


# --------------------------------------------------------------------------------------
# The breakdown is a partition, not a sample
# --------------------------------------------------------------------------------------

_PARTITION_SCHEMA = {
    "type": "object",
    "properties": {
        "a": {"type": "string"},
        "b": {"type": "string"},
        "c": {"type": "object", "properties": {"d": {"type": "string"}}},
        "missing": {"type": "string"},
        "unset": {"type": ["string", "null"]},
        "l": {"type": "array", "items": {"type": "object", "properties": {"v": {"type": "string"}}}},
    },
}


def test_breakdown_is_exhaustive_and_mutually_exclusive() -> None:
    """Every scored pointer lands in exactly one bucket, so no field moves the number unseen.

    The fixture hits every bucket: `/a`, `/c/d` and `/l/0/v` correct, `/b` incorrect, `/zz`
    and the extra entry's `/l/1/v` extra, `/missing` missing, `/unset` excluded.
    """
    result = json_field_accuracy(
        {"a": "x", "b": "y", "c": {"d": "z"}, "missing": "m", "unset": None, "l": [{"v": "x"}]},
        {"a": "x", "b": "WRONG", "c": {"d": "z"}, "zz": 1, "l": [{"v": "y"}, {"v": "x"}]},
        POLICY,
        _PARTITION_SCHEMA,
    )
    detail = dict(result.detail)
    buckets = {
        name: set(detail[name])
        for name in ("missing", "extra", "incorrect", "correct", "excluded")
    }

    pointers = [entry["pointer"] for entry in detail["per_field"]]
    assert len(pointers) == len(set(pointers)), "a pointer is reported twice"
    reported = set(pointers)
    union: set[str] = set()
    for name, pointers in buckets.items():
        overlap = union & pointers
        assert not overlap, f"{name} also appears in an earlier bucket: {sorted(overlap)}"
        union |= pointers

    assert union == reported, "per_field and the buckets disagree about which fields exist"
    assert buckets["correct"] == {"/a", "/c/d", "/l/0/v"}
    assert buckets["incorrect"] == {"/b"}
    assert buckets["extra"] == {"/zz", "/l/1/v"}
    assert buckets["excluded"] == {"/unset"}
    assert buckets["missing"] == {"/missing"}


# --------------------------------------------------------------------------------------
# Empty and degenerate answers: 100 when nothing was expected or given, else `skipped`
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reference", "prediction"),
    [({}, {}), ({"nested": {}}, {"nested": {}}), ({"a": None}, {}), ({"a": []}, {})],
    ids=["empty-objects", "empty-nested-objects", "a-reference-null-omitted", "an-empty-list-omitted"],
)
def test_nothing_expected_and_nothing_given_scores_100(reference: dict, prediction: dict) -> None:
    """An empty answer is correct."""
    result = json_field_accuracy(reference, prediction, POLICY, None)

    assert result.status is MetricStatus.scored
    assert result.value == 100.0
    assert result.detail["empty_answer"] is True


def test_an_answer_whose_every_field_is_empty_on_one_side_only_is_skipped_never_100() -> None:
    """Nothing was expected and something given: the field is left out, so nothing
    compares, which is not the empty answer's 100."""
    schema = {"type": "object", "properties": {"a": {"type": "array", "items": {"type": "object"}}}}
    result = json_field_accuracy({"a": []}, {"a": "garbage"}, POLICY, schema)

    assert result.status is MetricStatus.skipped
    assert result.detail["reason"] == "empty_on_one_side"


@pytest.mark.parametrize(
    "policy",
    [ComparisonPolicy(field_modes={"/a": "excluded"}), ComparisonPolicy(field_weights={"/a": 0.0})],
    ids=["excluded", "weight-0"],
)
def test_an_object_whose_every_field_the_author_excluded_is_skipped(policy: ComparisonPolicy) -> None:
    """Nothing was compared, by the author's choice: neither 100 nor 0."""
    result = json_field_accuracy({"a": 1}, {"a": 1}, policy, None)

    assert result.status is MetricStatus.skipped
    assert result.detail["reason"] == "no_scorable_fields"


def test_oversized_pairing_input_is_skipped_not_invalid() -> None:
    """Exceeding the pairing compute bound is skipped with its reason, not scored as a model failure.

    The bound is a platform limit: `invalid` or 0.0 would move the run aggregate for a
    condition the model did not cause.
    """
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "object", "properties": {"v": {"type": "string"}}},
            }
        },
    }
    side = MAX_ENTRY_PRODUCT // 50  # 200 x 200 = 40_000 > 10_000
    payload = {"items": [{"v": str(index)} for index in range(side)]}

    result = json_field_accuracy(payload, payload, POLICY, schema)

    assert result.status is MetricStatus.skipped
    assert result.value is None
    assert result.detail["reason"] == "pairing_input_too_large"
    assert result.detail["entry_product"] == side * side
    assert result.detail["max_entry_product"] == MAX_ENTRY_PRODUCT


def test_an_unparseable_answer_to_a_reference_too_large_to_pair_scores_0() -> None:
    """The reference's size does not excuse an answer that is not JSON: it weighs
    one wrong item, where skipping it would let garbage leave the run aggregate."""
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "object", "properties": {"v": {"type": "string"}}},
            }
        },
    }
    side = MAX_ENTRY_PRODUCT // 50
    reference = json.dumps({"items": [{"v": str(index)} for index in range(side)]})

    results = run_for_family(reference, "not json", Family.STRUCTURED_JSON, FamilyConfig(json_schema=schema))

    field = results["json.field_accuracy"]
    assert (field.status, field.value) == (MetricStatus.scored, 0.0)
    assert field.detail["reason"] == "prediction_not_well_formed"
    assert (field.detail["numerator"], field.detail["denominator"]) == (0, 1)


@pytest.mark.parametrize(
    ("credit", "counts", "pairs", "unmatched_reference", "unmatched_prediction"),
    [
        pytest.param([[1.0], [5.0]], (2, 1), ((1, 0),), (0,), (), id="more-reference-entries"),
        pytest.param([[1.0, 5.0]], (1, 2), ((0, 1),), (), (0,), id="more-answer-entries"),
        pytest.param([], (0, 2), (), (), (0, 1), id="no-reference-entry"),
        pytest.param([[], []], (2, 0), (), (0, 1), (), id="no-answer-entry"),
    ],
)
def test_pair_entries_pairs_for_the_most_credit(
    credit, counts, pairs, unmatched_reference, unmatched_prediction
) -> None:
    pairing = pair_entries(credit, *counts)
    assert pairing.pairs == pairs
    assert pairing.unmatched_reference == unmatched_reference
    assert pairing.unmatched_prediction == unmatched_prediction


@pytest.mark.parametrize(("extra", "status"), [(0, "scored"), (1, "skipped")])
def test_the_bound_on_pairs_per_answer_is_inclusive(extra: int, status: str) -> None:
    """Ten 100 x 100 lists score exactly ``MAX_PAIRS_SCORED`` pairs; one more is skipped."""
    lists = MAX_PAIRS_SCORED // MAX_ENTRY_PRODUCT
    side = [{}] * 100
    payload: dict = {"v": "x", **{f"l{i}": side for i in range(lists)}, **{f"m{i}": [{}] for i in range(extra)}}
    item = {"type": "array", "items": {"type": "object"}}
    schema = {"type": "object", "properties": {key: item for key in payload if key != "v"}}

    assert json_field_accuracy(payload, payload, POLICY, schema).status.value == status


def _nested(depth: int) -> dict:
    """An object ``depth`` levels deep, a string at the bottom."""
    value: object = "leaf"
    for _ in range(depth):
        value = {"a": value}
    assert isinstance(value, dict)
    return value


def test_an_answer_nested_past_the_depth_bound_is_skipped_not_a_crash() -> None:
    """An answer nested ~500 levels deep raised RecursionError and the entry was recorded
    as the agent's failure, its answer lost. Past ``MAX_FIELD_DEPTH`` the metric is
    skipped with the depth reached and the bound, a limit of ours."""
    reference = {"b": "leaf"}
    prediction = {"b": "leaf", **_nested(500)}
    result = json_field_accuracy(reference, prediction, POLICY, {"type": "object"})

    assert result.status is MetricStatus.skipped
    assert result.value is None
    assert result.detail["reason"] == "nesting_too_deep"
    assert result.detail["max_depth"] == MAX_FIELD_DEPTH
    assert result.detail["depth"] == MAX_FIELD_DEPTH + 1


@pytest.mark.parametrize(("depth", "status"), [(MAX_FIELD_DEPTH, "scored"), (MAX_FIELD_DEPTH + 1, "skipped")])
def test_the_depth_bound_is_inclusive(depth: int, status: str) -> None:
    payload = _nested(depth)
    assert json_field_accuracy(payload, payload, POLICY, {"type": "object"}).status.value == status


def test_a_too_deep_answer_keeps_its_validity_score_through_run_quality() -> None:
    """Through the platform's seam: the entry is not failed, ``json.schema_validity`` is
    still scored, and the field metric is skipped with its reason."""
    schema = {"type": "object", "properties": {"a": {"type": "object"}, "b": {"type": "string"}}}
    config = build_quality_config(
        "STRUCTURED_JSON", SimpleNamespace(default_output_schema=schema), []
    )
    outcomes = {
        outcome.key: outcome
        for outcome in run_quality(
            family="STRUCTURED_JSON",
            reference=json.dumps({"b": "leaf"}),
            prediction=json.dumps({"b": "leaf", **_nested(500)}),
            output_schema=schema,
            config=config,
        )
    }
    assert outcomes["json.field_accuracy"].status == "skipped"
    assert "nesting_too_deep" in json.dumps(outcomes["json.field_accuracy"].serialized)
    assert outcomes["json.schema_validity"].status == "scored"


def test_nested_lists_are_bounded_in_total() -> None:
    """Pairing scores every pair, nested lists included, so one answer's total is bounded
    too (``MAX_PAIRS_SCORED``): 10 x 10 entries, each holding 40 x 40, is skipped."""
    item = {"type": "object", "properties": {"v": {"type": "string"}}}
    schema = {
        "type": "object",
        "properties": {
            "outer": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"inner": {"type": "array", "items": item}},
                },
            }
        },
    }
    payload = {"outer": [{"inner": [{"v": str(i)} for i in range(40)]} for _ in range(10)]}

    result = json_field_accuracy(payload, payload, POLICY, schema)

    assert result.detail["reason"] == "pairing_input_too_large"
    assert result.detail["pairs_scored"] > result.detail["max_pairs_scored"] == MAX_PAIRS_SCORED


# --------------------------------------------------------------------------------------
# resource_address: well-formed AND matches the reference
# --------------------------------------------------------------------------------------
#
# Well-formedness alone would score any valid URI 100% whatever resource it names; the
# first case is a measured repro: a well-formed but wrong ATT&CK URL.

_URI_SCHEMA = {
    "type": "object",
    "properties": {"url": {"type": "string", "format": "uri"}},
    "required": ["url"],
}


_T1566 = "https://attack.mitre.org/techniques/T1566/"


@pytest.mark.parametrize(
    ("reference_url", "prediction_url", "expected"),
    [
        pytest.param(
            _T1566,
            "https://attack.mitre.org/techniques/T1566/002/",
            0.0,
            id="well-formed-but-different-resource",
        ),
        pytest.param(
            # Scheme/host case, a default port, a trailing slash and query order fold together.
            "https://Attack.Mitre.org:443/techniques/T1566?b=2&a=1",
            "https://attack.mitre.org/techniques/T1566/?a=1&b=2",
            100.0,
            id="representational-differences-only",
        ),
        pytest.param(_T1566, "not a url at all", 0.0, id="malformed"),
        pytest.param(_T1566, _T1566, 100.0, id="identical"),
    ],
)
def test_a_resource_address_must_be_well_formed_and_name_the_reference(
    reference_url: str, prediction_url: str, expected: float
) -> None:
    """The well-formedness gate applies first; then only representational differences fold."""
    result = json_field_accuracy(
        {"url": reference_url}, {"url": prediction_url}, POLICY, _URI_SCHEMA
    )

    assert result.value == expected


# ── schemas declared through $ref and lists of objects of several shapes ──
# A pydantic-generated schema declares its fields through `$defs` and `$ref`, and a list
# whose entries take one of several shapes as an `anyOf` of them. Such a list is a list of
# objects: each entry is scored field by field, under the shape its reference entry is.

_FILTERS_SCHEMA = {
    "type": "object",
    "properties": {
        "filters": {
            "type": "array",
            "items": {
                "anyOf": [
                    {"$ref": "#/$defs/TypeFilter"},
                    {"$ref": "#/$defs/RegardingFilter"},
                    {"$ref": "#/$defs/GenericFilter"},
                ]
            },
        },
        "mode": {"$ref": "#/$defs/Mode", "description": "How the filters combine."},
    },
    "$defs": {
        "Mode": {"type": "string", "enum": ["and", "or"]},
        "Operator": {"type": "string", "enum": ["eq", "gt", "gte", "lt"]},
        "TypeFilter": {
            "type": "object",
            "properties": {
                "key": {"const": "entity_type", "type": "string"},
                "values": {"type": "array", "items": {"type": "string"}},
                "operator": {"$ref": "#/$defs/Operator"},
            },
        },
        "RegardingFilter": {
            "type": "object",
            "properties": {
                "key": {"const": "regardingOf", "type": "string"},
                "values": {
                    "type": "array",
                    "items": {"anyOf": [{"$ref": "#/$defs/Relationship"}, {"$ref": "#/$defs/Entity"}]},
                },
                "operator": {"$ref": "#/$defs/Operator"},
            },
        },
        "GenericFilter": {
            "type": "object",
            "properties": {
                "key": {"$ref": "#/$defs/GenericKey"},
                "values": {"type": "array", "items": {"type": "string"}},
                "operator": {"$ref": "#/$defs/Operator"},
            },
        },
        "GenericKey": {"type": "string", "enum": ["x_opencti_score", "confidence"]},
        "Relationship": {
            "type": "object",
            "properties": {
                "key": {"const": "relationship_type", "type": "string"},
                "values": {"type": "array", "items": {"type": "string"}},
            },
        },
        "Entity": {
            "type": "object",
            "properties": {
                "key": {"const": "id", "type": "string"},
                "values": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}

_FILTERS = {
    "filters": [
        {"key": "entity_type", "values": ["Intrusion-Set"], "operator": "eq"},
        {"key": "x_opencti_score", "values": ["50"], "operator": "gt"},
        {
            "key": "regardingOf",
            "values": [
                {"key": "relationship_type", "values": ["targets"]},
                {"key": "id", "values": ["France"]},
            ],
            "operator": "eq",
        },
    ],
    "mode": "and",
}


def _changed(answer: dict, change) -> dict:
    copied = json.loads(json.dumps(answer))
    change(copied)
    return copied


def _inlined(node, root=None):
    """``node`` with every local ``$ref`` replaced by what it names, the keywords beside it kept."""
    root = node if root is None else root
    if isinstance(node, list):
        return [_inlined(item, root) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        target = root
        for token in node["$ref"].removeprefix("#/").split("/"):
            target = target[token]
        siblings = {key: value for key, value in node.items() if key != "$ref"}
        return _inlined({**target, **siblings}, root)
    return {key: _inlined(value, root) for key, value in node.items() if key != "$defs"}


def test_one_wrong_field_of_a_list_of_several_shapes_costs_that_field_only() -> None:
    """Each entry counts as its fields and is reported where it is wrong, not as one
    whole value lost."""
    answer = _changed(_FILTERS, lambda a: a["filters"][1].update(operator="gte"))

    result = json_field_accuracy(_FILTERS, answer, POLICY, _FILTERS_SCHEMA)

    assert result.detail["incorrect"] == ("/filters/1/operator",)
    scored = [field for field in result.detail["per_field"] if field["score"] is not None]
    assert result.value == pytest.approx(100 * (len(scored) - 1) / len(scored))
    # The entry's fields take their own branch's modes: a value list is a set, a fixed
    # operator an enum, the mode declared through a $ref an enum.
    modes = {field["pointer"]: field["mode"] for field in result.detail["per_field"]}
    assert modes["/filters/0/values"] == "enum_set"
    assert modes["/filters/1/operator"] == "enum"
    assert modes["/mode"] == "enum"
    # A key a branch fixes with `const` is that one value, not free text.
    assert modes["/filters/0/key"] == "enum"


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda a: a["filters"].reverse(), id="entries"),
        pytest.param(lambda a: a["filters"][2]["values"].reverse(), id="nested_entries"),
    ],
)
def test_the_order_of_a_list_of_several_shapes_does_not_move_the_score(change) -> None:
    result = json_field_accuracy(_FILTERS, _changed(_FILTERS, change), POLICY, _FILTERS_SCHEMA)

    assert result.value == 100.0


def test_an_unpaired_entry_counts_the_fields_of_its_own_shape() -> None:
    """A missing entry is scored under the branch it matches, so it counts its own fields."""
    answer = _changed(_FILTERS, lambda a: a["filters"].pop(2))

    result = json_field_accuracy(_FILTERS, answer, POLICY, _FILTERS_SCHEMA)

    assert result.detail["missing"] == (
        "/filters/2/key",
        "/filters/2/operator",
        "/filters/2/values/0/key",
        "/filters/2/values/0/values",
        "/filters/2/values/1/key",
        "/filters/2/values/1/values",
    )


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda a: None, id="same"),
        pytest.param(lambda a: a["filters"][1].update(operator="gte"), id="wrong_operator"),
        pytest.param(lambda a: a["filters"].pop(0), id="missing_entry"),
        pytest.param(lambda a: a.update(mode="or"), id="wrong_mode"),
        pytest.param(
            lambda a: a["filters"][2]["values"][1]["values"].append("Spain"), id="extra_value"
        ),
    ],
)
def test_a_schema_declared_through_ref_scores_as_the_same_schema_inlined(change) -> None:
    answer = _changed(_FILTERS, change)

    through_ref = json_field_accuracy(_FILTERS, answer, POLICY, _FILTERS_SCHEMA)
    inlined = json_field_accuracy(_FILTERS, answer, POLICY, _inlined(_FILTERS_SCHEMA))

    assert (through_ref.value, through_ref.detail) == (inlined.value, inlined.detail)


def test_the_agents_own_nlq_schema_scores_a_wrong_operator_at_its_field() -> None:
    """The case that showed it: the OpenCTI NLQ agent's pydantic schema."""
    from app.agents.output_schema import NlqOutputSchema

    reference = {
        "filters": [
            {"key": "entity_type", "values": ["Intrusion-Set"], "operator": "eq", "mode": "or"},
            {"key": "x_opencti_score", "values": ["50"], "operator": "gt", "mode": "or"},
        ],
        "mode": "and",
    }
    answer = _changed(reference, lambda a: a["filters"][1].update(operator="gte"))

    result = json_field_accuracy(reference, answer, POLICY, NlqOutputSchema.model_json_schema())

    assert result.detail["incorrect"] == ("/filters/1/operator",)


@pytest.mark.parametrize(
    "schema",
    [
        pytest.param(
            {
                "$ref": "#/$defs/Node",
                "$defs": {
                    "Node": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "enum": ["a", "b", "c"]},
                            "children": {"type": "array", "items": {"$ref": "#/$defs/Node"}},
                        },
                    }
                },
            },
            id="a_tree",
        ),
        pytest.param(
            {
                "type": "object",
                "properties": {
                    "name": {"$ref": "#/$defs/A"},
                    "children": {"type": "array", "items": {"$ref": "#"}},
                },
                "$defs": {"A": {"$ref": "#/$defs/B"}, "B": {"$ref": "#/$defs/A"}},
            },
            id="a_reference_cycle",
        ),
    ],
)
def test_a_recursive_reference_does_not_loop(schema) -> None:
    tree = {"name": "a", "children": [{"name": "b", "children": [{"name": "c", "children": []}]}]}
    answer = _changed(tree, lambda a: a["children"][0]["children"][0].update(name="b"))

    result = json_field_accuracy(tree, answer, POLICY, schema)

    assert result.detail["incorrect"] == ("/children/0/children/0/name",)


def test_an_enum_every_shape_shares_does_not_pick_the_shape() -> None:
    """An operator outside its enum is the answer's mistake, not a sign the entry is of
    no known shape: the entry keeps its shape, and its value list stays a set."""
    reference = _changed(_FILTERS, lambda a: a["filters"][0].update(operator="equals"))
    answer = _changed(reference, lambda a: a["filters"][0]["values"].append("Malware"))
    reordered = _changed(answer, lambda a: a["filters"][0]["values"].reverse())

    result = json_field_accuracy(answer, reordered, POLICY, _FILTERS_SCHEMA)

    assert result.value == 100.0


def test_shapes_worded_differently_still_share_a_property() -> None:
    """With no branch telling an entry apart, it is scored under the properties every
    branch declares alike, whatever each says about them."""
    schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"anyOf": [
            {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}, "description": "First."}}},
            {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}, "title": "Tags"}}},
        ]}}},
    }
    reference = {"items": [{"tags": ["a", "b"]}]}

    result = json_field_accuracy(reference, {"items": [{"tags": ["b", "a"]}]}, POLICY, schema)

    assert result.value == 100.0


def test_an_object_field_of_several_shapes_is_scored_under_its_values_shape() -> None:
    """The same union scores alike whether it is one field or the entries of a list."""
    shapes = {"anyOf": [{"$ref": "#/$defs/A"}, {"$ref": "#/$defs/B"}]}
    schema = {
        "type": "object",
        "properties": {"one": shapes, "many": {"type": "array", "items": shapes}},
        "$defs": {
            "A": {"type": "object", "properties": {"kind": {"const": "a"}, "tags": {"type": "array", "items": {"type": "string"}}}},
            "B": {"type": "object", "properties": {"kind": {"const": "b"}, "size": {"type": "integer"}}},
        },
    }
    reference = {"one": {"kind": "a", "tags": ["x", "y"]}, "many": [{"kind": "a", "tags": ["x", "y"]}]}
    answer = {"one": {"kind": "a", "tags": ["y", "x"]}, "many": [{"kind": "a", "tags": ["y", "x"]}]}

    result = json_field_accuracy(reference, answer, POLICY, schema)

    assert result.value == 100.0


@pytest.mark.parametrize(
    "null",
    [{"type": "null"}, {"type": ["null"]}, {"const": None}, {"enum": [None]}],
    ids=["type", "type_list", "const", "enum"],
)
def test_a_null_shape_beside_objects_keeps_the_list_a_list_of_objects(null) -> None:
    schema = _changed(_FILTERS_SCHEMA, lambda s: s["properties"]["filters"]["items"]["anyOf"].append(null))
    answer = _changed(_FILTERS, lambda a: a["filters"][1].update(operator="gte"))

    result = json_field_accuracy(_FILTERS, answer, POLICY, schema)

    assert result.detail["incorrect"] == ("/filters/1/operator",)


def test_a_reference_that_names_nothing_scores_the_field_exact() -> None:
    """A reference into nothing (another document, a token no list index reads) is no
    declaration: the field is compared as it is, and scoring goes on."""
    schema = {
        "type": "object",
        "properties": {
            "remote": {"$ref": "https://example.invalid/schema.json"},
            "digit": {"$ref": "#/$defs/list/\u00b2"},
        },
        "$defs": {"list": [{"type": "string"}]},
    }
    reference = {"remote": ["a", "b"], "digit": ["a", "b"]}

    result = json_field_accuracy(reference, {"remote": ["b", "a"], "digit": ["b", "a"]}, POLICY, schema)

    assert result.detail["incorrect"] == ("/digit", "/remote")


# ── JSON and JSON Schema validity ──
# Strict JSON (no NaN/Infinity, valid UTF-8 only) and JSON Schema Draft 2020-12, with
# deterministic RFC 6901 violation locations.

_SCHEMA: dict[str, JSONValue] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["name", "score"],
    "properties": {
        "name": {"type": "string"},
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
    },
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param('{"name": "x", "score": 10}', id="text"),
        pytest.param(b'{"name": "x", "score": 10}', id="bytes"),
        pytest.param({"name": "x", "score": 10}, id="parsed"),
    ],
)
def test_an_answer_that_parses_is_scored_on_the_two_json_metrics(payload: object) -> None:
    """Raw text, bytes and an already-parsed value score alike, on these two keys only."""
    results = run_for_family(
        {"name": "x", "score": 10},
        payload,
        Family.STRUCTURED_JSON,
        FamilyConfig(json_schema=_SCHEMA),
    )
    assert list(results) == ["json.schema_validity", "json.field_accuracy"]
    assert results["json.schema_validity"].value == 100.0
    assert results["json.field_accuracy"].value == 100.0
    assert results["json.schema_validity"].metadata.direction is MetricDirection.higher_is_better


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param('{"a": }', id="syntax-error"),
        pytest.param("NaN", id="nan"),
        pytest.param("Infinity", id="infinity"),
        pytest.param("-Infinity", id="minus-infinity"),
        pytest.param('{"x": NaN}', id="nested-nan"),
        pytest.param(b'{"a": "\xff\xfe"}', id="invalid-utf8"),
    ],
)
def test_an_answer_that_is_not_strict_json_scores_0_on_both_metrics(
    payload: str | bytes,
) -> None:
    """An answer that is not strict JSON is a computed 0, not an invalid result.

    Both metrics carry the parse diagnostic, which tells "not JSON" from a schema
    violation.
    """
    results = run_for_family(
        {"name": "x", "score": 10},
        payload,
        Family.STRUCTURED_JSON,
        FamilyConfig(json_schema=_SCHEMA),
    )
    assert set(results) == {"json.schema_validity", "json.field_accuracy"}
    for result in results.values():
        assert result.status is MetricStatus.scored
        assert result.value == 0.0
        assert result.detail["reason"] == "prediction_not_well_formed"
        assert result.detail["error_code"] == "json_parse_error"


def test_schema_valid() -> None:
    result = json_schema_validity({"name": "x", "score": 10}, _SCHEMA)
    assert result.status is MetricStatus.scored
    assert result.value == 100.0


def test_schema_violation_reports_paths() -> None:
    result = json_schema_validity({"name": "x", "score": 999}, _SCHEMA)
    assert result.status is MetricStatus.scored
    assert result.value == 0.0
    assert result.detail["error_code"] == "schema_invalid"
    assert "/score" in result.detail["locations"]


def test_schema_locations_use_rfc6901_escaping() -> None:
    schema: dict[str, JSONValue] = {
        "type": "object",
        "properties": {"a/b": {"type": "integer"}},
    }
    result = json_schema_validity({"a/b": "not-int"}, schema)
    assert result.status is MetricStatus.scored
    assert result.value == 0.0
    # "/" inside the key is escaped to "~1".
    assert "/a~1b" in result.detail["locations"]


def test_schema_errors_sorted_across_indices_and_keys() -> None:
    schema: dict[str, JSONValue] = {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"type": "integer"}},
            "name": {"type": "integer"},
        },
    }
    payload = {"items": ["bad", "worse"], "name": "also-bad"}
    first = json_schema_validity(payload, schema)
    second = json_schema_validity(payload, schema)
    # Deterministic: sorting heterogeneous paths (ints + strs) must not raise and
    # must be byte-identical across runs.
    assert first.to_json() == second.to_json()
    assert list(first.detail["locations"]) == sorted(first.detail["locations"])


def test_invalid_schema_raises_configuration_error() -> None:
    with pytest.raises(ConfigurationError):
        json_schema_validity({"a": 1}, {"type": "not-a-real-type"})


# ── golden fixtures ──

# Each call builds a fresh copy, so no test can change another's input.


def _golden_reference() -> dict[str, JSONValue]:
    return {"name": "alpha", "score": 90, "tags": ["a", "b"]}


def _golden_prediction() -> dict[str, JSONValue]:
    return {"name": "alpha", "score": 88, "tags": ["b", "a"]}


def _golden_schema() -> dict[str, JSONValue]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["name", "score"],
        "properties": {
            "name": {"type": "string"},
            "score": {"type": "integer", "minimum": 0, "maximum": 100},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": False,
    }


def test_reference_prediction_field_accuracy_golden() -> None:
    result = json_field_accuracy(_golden_reference(), _golden_prediction(), ComparisonPolicy())
    # One of three declared fields correct: /name. /score and /tags are each wrong once,
    # an array counting as one field, not one mistake per leaf.
    assert result.value == pytest.approx(100 / 3)
    assert result.detail["incorrect"] == ("/score", "/tags")


def test_field_accuracy_with_set_semantics_golden() -> None:
    policy = ComparisonPolicy(array_semantics={"/tags": "set"})
    result = json_field_accuracy(_golden_reference(), _golden_prediction(), policy)
    # /name and /tags (as a set) correct; only /score differs: two of three fields.
    assert result.value == pytest.approx(200 / 3)
    assert result.detail["incorrect"] == ("/score",)


def test_schema_validity_golden() -> None:
    result = json_schema_validity(_golden_prediction(), _golden_schema())
    assert result.status is MetricStatus.scored
    assert result.value == 100.0
