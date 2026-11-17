"""The metric suite's primitives: the precision/recall/F1 counts, set and multiset
diagnostics, macro aggregation, and `ComparisonPolicy`.
"""

import math
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from app.evaluation_metrics.core.errors import ConfigurationError
from app.evaluation_metrics.quality.normalization.policies import (
    ArraySemantics,
    ComparisonPolicy,
    FieldMode,
)
from app.evaluation_metrics.quality.normalization.prose import ProseNormalizationPolicy
from app.evaluation_metrics.quality.primitives.aggregate import macro_average
from app.evaluation_metrics.quality.primitives.prf import precision_recall_f1
from app.evaluation_metrics.quality.primitives.sets import (
    DiagnosticSet,
    multiset_diagnostics,
    set_diagnostics,
)

# ── precision/recall/F1 ──
# Every case keeps precision and recall distinct, so swapping their denominators is
# observable; a triple with `fp == fn` would hide that mutation. F1 is asserted on its own
# because it is computed from the counts as `2 * tp / (2 * tp + fp + fn)`, not as the
# harmonic mean of precision and recall.

@pytest.mark.parametrize(
    ("tp", "fp", "fn", "precision", "recall", "f1"),
    [
        # fp != fn throughout, so precision != recall.
        (3, 1, 2, 3 / 4, 3 / 5, 6 / 9),
        (3, 2, 1, 3 / 5, 3 / 4, 6 / 9),  # the mirror: precision and recall exchange
        (1, 9, 0, 1 / 10, 1.0, 2 / 11),
        (1, 0, 9, 1.0, 1 / 10, 2 / 11),
        (5, 0, 0, 1.0, 1.0, 1.0),  # a perfect prediction
        (0, 3, 4, 0.0, 0.0, 0.0),  # nothing correct
        (0, 0, 0, 0.0, 0.0, 0.0),  # every denominator zero -> zero_division policy
        (0, 5, 0, 0.0, 0.0, 0.0),  # recall denominator zero, precision denominator not
        (0, 0, 5, 0.0, 0.0, 0.0),  # precision denominator zero, recall denominator not
    ],
)
def test_exact_values(
    tp: int, fp: int, fn: int, precision: float, recall: float, f1: float
) -> None:
    assert precision_recall_f1(tp, fp, fn) == (
        pytest.approx(precision),
        pytest.approx(recall),
        pytest.approx(f1),
    )


@pytest.mark.parametrize(
    ("tp", "fp", "fn"),
    [
        pytest.param(True, 0, 0, id="tp-true"),
        pytest.param(False, 0, 0, id="tp-false"),
        pytest.param(1.0, 0, 0, id="tp-float"),
        pytest.param("1", 0, 0, id="tp-string"),
        pytest.param(None, 0, 0, id="tp-none"),
        pytest.param(0, False, 0, id="fp-false"),
        pytest.param(2, 0.5, 0, id="fp-float"),
    ],
)
def test_non_integer_counts_are_rejected(tp: object, fp: object, fn: object) -> None:
    """A bool subclasses int but is not a count, and neither is a float or a string."""
    with pytest.raises(TypeError, match="non-boolean int"):
        precision_recall_f1(tp, fp, fn)  # type: ignore[arg-type]


@pytest.mark.parametrize(("tp", "fp", "fn"), [(-1, 0, 0), (0, -1, 0), (0, 0, -1)])
def test_negative_counts_are_rejected(tp: int, fp: int, fn: int) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        precision_recall_f1(tp, fp, fn)


# ── set and multiset diagnostics ──

def test_set_partial_overlap() -> None:
    d = set_diagnostics({"a", "b", "c"}, {"b", "c", "d"})
    assert d.tp == 2
    assert d.fp == 1
    assert d.fn == 1
    assert d.missing == ("a",)
    assert d.spurious == ("d",)


def test_set_collapses_duplicates() -> None:
    d = set_diagnostics(["a", "a", "b"], ["a"])
    assert d.tp == 1
    assert d.fn == 1
    assert d.missing == ("b",)


def test_multiset_distinguishes_duplicates() -> None:
    d = multiset_diagnostics(["a", "a", "b"], ["a", "b"])
    assert d.tp == 2
    assert d.fn == 1
    assert d.missing == ("a",)
    assert d.spurious == ()


def test_empty_vs_empty() -> None:
    d = set_diagnostics(set[str](), set[str]())
    assert (d.tp, d.fp, d.fn) == (0, 0, 0)
    assert d.missing == () and d.spurious == ()


def test_diagnostic_set_is_immutable() -> None:
    d = set_diagnostics({"a"}, {"b"})
    with pytest.raises(AttributeError):
        d.tp = 5  # type: ignore[misc]  # frozen dataclass rejects mutation


def test_diagnostic_set_rejects_boolean_counts() -> None:
    with pytest.raises(TypeError):
        DiagnosticSet[str](tp=True, fp=0, fn=0)


def test_diagnostic_set_rejects_negative_counts() -> None:
    with pytest.raises(ValueError):
        DiagnosticSet[str](tp=-1, fp=0, fn=0)


def test_as_detail_shape_and_lists() -> None:
    d = set_diagnostics({"a", "z"}, {"a"})
    detail = d.as_detail()
    assert set(detail) == {"tp", "fp", "fn", "missing", "spurious"}
    # as_detail converts the immutable tuples to fresh lists.
    assert detail["missing"] == ["z"]
    assert isinstance(detail["missing"], list)
    assert isinstance(detail["spurious"], list)


@pytest.mark.parametrize(
    ("diagnostics", "items", "expected_missing"),
    [
        pytest.param(set_diagnostics, {"a", "b", "c"}, ("a", "b", "c"), id="strings"),
        pytest.param(
            # None first, then bools (False < True), then integers, then strings.
            set_diagnostics,
            {None, False, True, 2, 10, "b", "a"},
            (None, False, True, 2, 10, "a", "b"),
            id="mixed-scalar-types",
        ),
        pytest.param(set_diagnostics, {2, 10, 1}, (1, 2, 10), id="numeric-not-lexicographic"),
        pytest.param(
            # Every int sorts before every float: distinct type ranks, no int->float coercion.
            set_diagnostics,
            {10, 2.0, 3},
            (3, 10, 2.0),
            id="int-and-float-distinct-ranks",
        ),
        pytest.param(set_diagnostics, {2.5, 1.5, 10.0}, (1.5, 2.5, 10.0), id="finite-floats"),
        pytest.param(
            set_diagnostics,
            {(1, "b"), (1, "a"), (2, "a")},
            ((1, "a"), (1, "b"), (2, "a")),
            id="tuples-recursively",
        ),
        pytest.param(
            multiset_diagnostics, [2, 1, 1, "a"], (1, 1, 2, "a"), id="multiset-mixed-types"
        ),
    ],
)
def test_diagnostics_are_sorted_deterministically(
    diagnostics: Callable[[Any, Any], DiagnosticSet[Any]],
    items: Any,
    expected_missing: tuple[Any, ...],
) -> None:
    """Missing values come out in one total order, whatever the input's iteration order."""
    assert diagnostics(items, []).missing == expected_missing


class _Opaque:
    __hash__ = object.__hash__


@pytest.mark.parametrize(
    "items",
    [
        pytest.param({float("nan")}, id="nan"),
        pytest.param({float("inf")}, id="inf"),
        pytest.param({float("-inf")}, id="minus-inf"),
        pytest.param({_Opaque(), _Opaque()}, id="opaque-object"),
    ],
)
def test_a_value_without_a_total_order_raises(items: set) -> None:
    with pytest.raises(TypeError):
        set_diagnostics(items, set())


# ── macro aggregation ──

def test_macro_averages_over_the_mapping_labels() -> None:
    per_label: dict[str, DiagnosticSet[str]] = {
        "a": DiagnosticSet(tp=1, fp=0, fn=0),  # perfect
        "b": DiagnosticSet(tp=0, fp=1, fn=0),  # predicted-only, penalized
    }
    _p, _r, f = macro_average(per_label)
    # label a f1=1.0, label b f1=0.0 -> mean 0.5
    assert f == pytest.approx(0.5)


def test_macro_empty_universe_zero() -> None:
    assert macro_average({}) == (0.0, 0.0, 0.0)


# ── ComparisonPolicy: array-semantics opt-in and immutability ──

def test_array_mode_opt_in() -> None:
    policy = ComparisonPolicy(array_semantics={"/tags": "set", "/log": "multiset"})
    assert policy.array_mode("/tags") is ArraySemantics.set
    assert policy.array_mode("/log") is ArraySemantics.multiset
    assert policy.array_mode("/other") is ArraySemantics.positional


def test_frozen() -> None:
    policy = ComparisonPolicy()
    with pytest.raises(ValidationError):
        policy.ignored_paths = ["/x"]


# ── ComparisonPolicy: contradictory configurations ──
# Caller misconfiguration raises `ConfigurationError` and is never turned into a number: a
# score from a bad configuration is a silent wrong answer, indistinguishable downstream from
# a badly performing agent. A pointer matching no schema field, and the free-text rule for
# derived modes, need the schema, which `ComparisonPolicy` does not carry: mode derivation
# enforces and tests them.

@pytest.mark.parametrize(
    ("configuration", "match"),
    [
        pytest.param(
            {"field_modes": {"/status": "approximately_right"}},
            "unknown field_mode",
            id="unknown-field-mode",
        ),
        *(
            pytest.param({"field_weights": {"/verdict": weight}}, "invalid field_weight", id=f"weight-{name}")
            for name, weight in (
                ("negative", -1.0),
                ("barely-negative", -0.0001),
                ("nan", math.nan),
                ("inf", math.inf),
                ("minus-inf", -math.inf),
            )
        ),
        pytest.param(
            # The two settings state opposite intents, and honouring either hides the other.
            {"field_weights": {"/notes": 2.0}, "ignored_paths": ["/notes"]},
            "field_weight while also being ignored",
            id="weight-on-an-ignored-path",
        ),
        pytest.param(
            # A default would silently score the field under other normalisation than the
            # whole-answer prose metric it is read beside.
            {"field_modes": {"/summary": "free_text"}},
            "prose_policy is required",
            id="free-text-without-a-prose-policy",
        ),
        pytest.param(
            {"array_semantics": {"/tags": "unordered"}},
            "unknown array_semantics",
            id="unknown-array-semantics",
        ),
        pytest.param(
            {"normalization": {"/name": "shout"}},
            "unknown normalization",
            id="unknown-normalization",
        ),
        *(
            pytest.param(
                {"numeric_tolerance": {"/score": tolerance}},
                "invalid numeric_tolerance",
                id=f"tolerance-{name}",
            )
            for name, tolerance in (("negative", -1.0), ("nan", math.nan), ("inf", math.inf))
        ),
    ],
)
def test_an_invalid_configuration_raises(configuration: dict, match: str) -> None:
    with pytest.raises(ConfigurationError, match=match):
        ComparisonPolicy(**configuration)


@pytest.mark.parametrize(
    ("configuration", "read"),
    [
        pytest.param(
            # The field counts for nothing but stays in the breakdown; rejecting zero would
            # push the author to `ignored_paths`, which hides the field.
            {"field_weights": {"/notes": 0.0}},
            lambda policy: policy.field_weight("/notes"),
            id="zero-weight",
        ),
        pytest.param(
            {"numeric_tolerance": {"/score": 0.0}},
            lambda policy: policy.numeric_tolerance["/score"],
            id="zero-tolerance",
        ),
    ],
)
def test_zero_is_accepted(
    configuration: dict, read: Callable[[ComparisonPolicy], float]
) -> None:
    assert read(ComparisonPolicy(**configuration)) == 0.0


def test_free_text_override_with_a_prose_policy_is_accepted() -> None:
    policy = ComparisonPolicy(
        field_modes={"/summary": "free_text"},
        prose_policy=ProseNormalizationPolicy(),
    )
    assert policy.field_mode("/summary") is FieldMode.free_text


def test_defaults_are_strict_and_unset_fields_weigh_equally() -> None:
    """An author who sets no option gets strict comparison and a plain mean: positional
    arrays, no tolerance, no coercion, no normalization."""
    policy = ComparisonPolicy()
    assert policy.field_weight("/anything") == 1.0
    assert policy.field_mode("/anything") is None
    assert policy.array_mode("/anything") is ArraySemantics.positional
    assert policy.prose_policy is None
    assert policy.array_semantics == {}
    assert policy.numeric_tolerance == {}
    assert policy.type_coercion == {}
    assert policy.normalization == {}
    assert policy.ignored_paths == []
