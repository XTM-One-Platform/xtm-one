"""The CLASSIFICATION metric family: dataset-level set metrics, the empty answer, the
label-vocabulary gate, and the golden fixture dataset.
"""

import pytest

from app.evaluation_metrics.core.contracts import MetricDirection, MetricStatus
from app.evaluation_metrics.core.errors import ConfigurationError
from app.evaluation_metrics.quality.families.classification.dataset import (
    classification_dataset,
)
from app.evaluation_metrics.quality.families.classification.vocabulary import (
    label_vocabulary_validity,
)
from app.evaluation_metrics.quality.primitives.empty_answer import (
    prf_with_empty_answer,
)
from app.evaluation_metrics.quality.primitives.prf import precision_recall_f1

# ── dataset level: micro/macro P/R/F1, samples F1, subset accuracy ──

_VOCAB = frozenset({"a", "b", "c", "d"})
_REFERENCES = [frozenset({"a", "b"}), frozenset({"b", "c"}), frozenset({"c"})]
_PREDICTIONS = [frozenset({"a", "b"}), frozenset({"b"}), frozenset({"c", "d"})]


def test_length_mismatch_raises_configuration_error() -> None:
    with pytest.raises(ConfigurationError):
        classification_dataset(
            [frozenset({"a"})], [frozenset({"a"}), frozenset({"b"})], _VOCAB
        )


def test_micro_prf() -> None:
    results = classification_dataset(_REFERENCES, _PREDICTIONS, _VOCAB)
    micro = results["classification.micro_prf"]
    # pooled tp=4 fp=1 fn=1 -> precision=recall=f1=0.8
    assert micro.status is MetricStatus.scored
    assert micro.value == pytest.approx(80.0)
    assert micro.detail["tp"] == 4
    assert micro.detail["fp"] == 1
    assert micro.detail["fn"] == 1


def test_macro_prf_observed_union_includes_predicted_only_labels() -> None:
    results = classification_dataset(_REFERENCES, _PREDICTIONS, _VOCAB)
    macro = results["classification.macro_prf"]
    # observed union {a,b,c,d}; d is predicted-only and penalizes macro
    assert macro.value == pytest.approx(200 / 3)  # (1+1+2/3+0)/4
    assert tuple(macro.detail["classes"]) == ("a", "b", "c", "d")


def test_macro_prf_ignores_a_declared_label_neither_side_used() -> None:
    """A vocabulary label no entry used tests nothing; scoring it 0 would read 25 here.

    ``_VOCAB`` declares {a,b,c,d}; only 'a' is used, and answered right.
    """
    results = classification_dataset([frozenset({"a"})], [frozenset({"a"})], _VOCAB)
    macro = results["classification.macro_prf"]
    assert tuple(macro.detail["classes"]) == ("a",)
    assert macro.value == pytest.approx(100.0)


def test_samples_f1() -> None:
    results = classification_dataset(_REFERENCES, _PREDICTIONS, _VOCAB)
    samples = results["classification.samples_f1"]
    # per-example f1: 1.0, 2/3, 2/3 -> mean = (1 + 2/3 + 2/3)/3
    assert samples.value == pytest.approx((1.0 + 2 / 3 + 2 / 3) / 3 * 100)


# ── one entry: micro, macro and samples are ways of combining entries ──
# On a single entry there is nothing to combine: the pooled and the per-entry F1 are the
# entry's own label F1, and the per-label F1 is the share of labels found among those
# expected or predicted, not a macro F1. The metadata says so, for the run page to follow.

_ONE_ENTRY_CASES = [
    pytest.param(frozenset({"a", "b", "c"}), frozenset({"a", "b", "d"}), id="found-missed-extra"),
    pytest.param(frozenset({"a"}), frozenset({"a"}), id="exact"),
    pytest.param(frozenset({"a", "b"}), frozenset(), id="nothing-predicted"),
    pytest.param(frozenset(), frozenset(), id="empty-against-empty"),
]


@pytest.mark.parametrize(("reference", "prediction"), _ONE_ENTRY_CASES)
def test_on_one_entry_the_pooled_and_the_per_entry_f1_are_the_same(reference, prediction) -> None:
    results = classification_dataset([reference], [prediction], _VOCAB)
    assert results["classification.samples_f1"].value == pytest.approx(
        results["classification.micro_prf"].value
    )


def test_on_one_entry_the_per_label_f1_is_the_share_of_labels_found() -> None:
    # 2 labels found, 1 missed, 1 extra: 2 / 4, where the entry's label F1 is 4 / 6.
    results = classification_dataset([frozenset({"a", "b", "c"})], [frozenset({"a", "b", "d"})], _VOCAB)
    assert results["classification.macro_prf"].value == pytest.approx(50.0)
    assert results["classification.micro_prf"].value == pytest.approx(200 / 3)


def test_an_entry_shows_one_label_f1() -> None:
    """The pooled F1 is the entry's "Label F1"; the other two F1s stay at run level."""
    results = classification_dataset(_REFERENCES, _PREDICTIONS, _VOCAB)
    views = {
        key: (result.metadata.shown_on_entry, result.metadata.entry_label)
        for key, result in results.items()
    }
    assert views == {
        "classification.micro_prf": (True, "Label F1"),
        "classification.macro_prf": (False, None),
        "classification.samples_f1": (False, None),
        "classification.subset_accuracy": (True, None),
        "classification.vocabulary_validity": (True, None),
    }


def test_subset_accuracy() -> None:
    results = classification_dataset(_REFERENCES, _PREDICTIONS, _VOCAB)
    subset = results["classification.subset_accuracy"]
    # only example 0 is an exact set match -> 1/3
    assert subset.value == pytest.approx(100 / 3)


# ── empty answer: nothing applies and nothing was claimed ──
# Every metric scores it as correct. The platform shapes one entry per scoring call (`_shape_inputs`), so `micro_prf` is that
# entry's score, not a dataset aggregate; a 0.0 there would feed the penalised mean behind
# the run headline.

_VOCAB_AB = frozenset({"a", "b"})


def _values(references, predictions) -> dict[str, float]:
    results = classification_dataset(references, predictions, _VOCAB_AB)
    return {key: result.value for key, result in results.items()}


def test_every_metric_agrees_that_an_empty_answer_is_correct() -> None:
    """An empty reference and an empty prediction get one verdict from all five metrics,
    each scored rather than withheld."""
    results = classification_dataset([frozenset()], [frozenset()], _VOCAB_AB)
    for key, result in results.items():
        assert result.status is MetricStatus.scored, key
    values = {key: result.value for key, result in results.items()}
    assert values["classification.micro_prf"] == 100.0
    assert values["classification.macro_prf"] == 100.0
    assert values["classification.samples_f1"] == 100.0
    assert values["classification.subset_accuracy"] == 100.0
    assert values["classification.vocabulary_validity"] == 100.0


def test_samples_f1_credits_an_empty_example_inside_a_larger_dataset() -> None:
    """``samples_f1`` averages per-example F1, so an empty-vs-empty example contributes
    100, not a 0.0 that drags the average down.
    """
    values = _values(
        [frozenset(), frozenset({"a"})], [frozenset(), frozenset({"a"})]
    )
    assert values["classification.samples_f1"] == 100.0

    half = _values([frozenset(), frozenset({"a"})], [frozenset(), frozenset()])
    assert half["classification.samples_f1"] == 50.0


def test_a_wrong_answer_is_not_made_correct() -> None:
    """The rule is empty-vs-EMPTY. Missing everything, or inventing, still scores 0."""
    missed = _values([frozenset({"a"})], [frozenset()])
    invented = _values([frozenset()], [frozenset({"a"})])
    for values in (missed, invented):
        assert values["classification.micro_prf"] == 0.0
        assert values["classification.macro_prf"] == 0.0
        assert values["classification.samples_f1"] == 0.0
        assert values["classification.subset_accuracy"] == 0.0


def test_the_primitive_keeps_its_zero_division_policy() -> None:
    """``precision_recall_f1`` still scores all-zero counts 0; only the empty-answer
    wrapper scores them 1.0.

    ``macro_average`` calls the primitive PER LABEL, where 1.0 would score every unused
    label in the universe as perfect.
    """
    assert precision_recall_f1(0, 0, 0) == (0.0, 0.0, 0.0)
    assert prf_with_empty_answer(0, 0, 0) == (1.0, 1.0, 1.0)
    assert prf_with_empty_answer(1, 1, 1) == precision_recall_f1(1, 1, 1)


_AGREEING_KEYS = (
    "classification.micro_prf",
    "classification.macro_prf",
    "classification.samples_f1",
    "classification.subset_accuracy",
)


def test_no_examples_is_not_a_perfect_answer() -> None:
    """"Nothing was scored" must not read as "everything scored perfectly".

    An empty dataset is not an empty ANSWER, which is correct: all four metrics are
    skipped. Unreachable from the platform (``_shape_inputs`` hands over one entry per
    call), but ``classification_dataset`` is public API.
    """
    results = classification_dataset([], [], _VOCAB)
    assert [results[key].status for key in _AGREEING_KEYS] == [MetricStatus.skipped] * 4
    assert {results[key].detail["reason"] for key in _AGREEING_KEYS} == {"no_examples"}


def test_scoring_examples_one_at_a_time_combines_into_the_dataset_values() -> None:
    """Each metric reports what a run combines: the counts of each example, pooled
    or summed per label, give the values of the whole collection."""
    whole = classification_dataset(_REFERENCES, _PREDICTIONS, _VOCAB)
    parts = [
        classification_dataset([reference], [prediction], _VOCAB)
        for reference, prediction in zip(_REFERENCES, _PREDICTIONS, strict=True)
    ]
    for key in ("classification.micro_prf", "classification.subset_accuracy"):
        numerator = sum(part[key].detail["numerator"] for part in parts)
        denominator = sum(part[key].detail["denominator"] for part in parts)
        assert 100.0 * numerator / denominator == pytest.approx(whole[key].value)
    summed: dict[str, list[int]] = {}
    for part in parts:
        for label, counts in part["classification.macro_prf"].detail["classes"].items():
            total = summed.setdefault(label, [0, 0, 0])
            for index, count in enumerate((counts["tp"], counts["fp"], counts["fn"])):
                total[index] += count
    assert summed == {
        label: [counts["tp"], counts["fp"], counts["fn"]]
        for label, counts in whole["classification.macro_prf"].detail["classes"].items()
    }


# ── label-vocabulary gate ──
# A violation is reported without withholding the metrics: `services/evaluation/metrics.py`
# persists only `scored` outcomes, so an `invalid` gate would leave the entry with nothing,
# indistinguishable from one never scored. Over-prediction is what precision measures.

_VOCAB_ABC = frozenset({"a", "b", "c"})


def test_vocabulary_valid() -> None:
    result = label_vocabulary_validity(["a", "b"], _VOCAB_ABC)
    assert result.status is MetricStatus.scored
    assert result.value == 100.0
    assert result.detail["valid"] is True
    assert list(result.detail["offending_labels"]) == []
    assert result.metadata.key == "classification.vocabulary_validity"
    assert result.metadata.direction is MetricDirection.higher_is_better


def test_vocabulary_violation_is_a_value_not_a_status() -> None:
    """``scored``/0.0, so the violation survives into the persisted metrics."""
    result = label_vocabulary_validity(["a", "z", "y", "z"], _VOCAB_ABC)
    assert result.status is MetricStatus.scored
    assert result.value == 0.0
    # A scored result may not carry a MetricError, so the offenders ride in detail --
    # still every one of them, deterministically ordered and de-duplicated.
    assert result.error is None
    assert result.detail["valid"] is False
    assert list(result.detail["offending_labels"]) == ["y", "z"]
    assert result.detail["offending_label_count"] == 2


def test_an_out_of_vocabulary_label_is_penalised_not_suppressed() -> None:
    """An invented label still produces every metric, and precision carries the cost."""
    references = [frozenset({"a"}), frozenset({"b"})]
    predictions = [frozenset({"a"}), frozenset({"b", "zzz"})]
    results = classification_dataset(references, predictions, _VOCAB_ABC)

    gate = results["classification.vocabulary_validity"]
    assert gate.status is MetricStatus.scored
    assert gate.value == 0.0
    assert list(gate.detail["offending_labels"]) == ["zzz"]

    for key in (
        "classification.micro_prf",
        "classification.macro_prf",
        "classification.samples_f1",
        "classification.subset_accuracy",
    ):
        assert results[key].status is MetricStatus.scored, key

    # Both references were found and one label was invented: recall is perfect,
    # precision is not.
    micro = results["classification.micro_prf"]
    assert micro.detail["recall"] == 100.0
    assert micro.detail["precision"] < 100.0
    assert micro.detail["fp"] == 1


# ── golden fixture dataset ──
# Every averaging decision is observable: micro precision (66.67) differs from micro recall
# (80.0), and macro precision (75.0) from macro recall (62.5), so a swap shows. The
# vocabulary carries a label ("e") absent from references and predictions: macro averaging
# runs over the 4 observed labels, not the 5 declared ones. Expected values come from
# scikit-learn.

# Each call builds a fresh copy, so no test can change another's input.


def _golden_dataset() -> tuple[list[frozenset[str]], list[frozenset[str]], frozenset[str]]:
    return (
        [frozenset({"a", "b"}), frozenset({"b", "c"}), frozenset({"c"})],
        [frozenset({"a", "b"}), frozenset({"b", "d"}), frozenset({"c", "d"})],
        frozenset({"a", "b", "c", "d", "e"}),
    )


def _zero_division_dataset() -> tuple[list[frozenset[str]], list[frozenset[str]], frozenset[str]]:
    # The first example predicts no label, so "a" has no predicted support and its
    # per-label precision divides by zero.
    return (
        [frozenset({"a"}), frozenset({"b", "c"})],
        [frozenset(), frozenset({"b", "c"})],
        frozenset({"a", "b", "c"}),
    )


def test_dataset_golden_scores() -> None:
    references, predictions, vocabulary = _golden_dataset()
    results = classification_dataset(references, predictions, vocabulary)

    assert results["classification.vocabulary_validity"].status is MetricStatus.scored

    micro = results["classification.micro_prf"]
    assert micro.detail is not None
    assert micro.detail["precision"] == pytest.approx(200 / 3)
    assert micro.detail["recall"] == pytest.approx(80.0)
    assert micro.value == pytest.approx(800 / 11)

    macro = results["classification.macro_prf"]
    assert macro.detail is not None
    assert macro.detail["precision"] == pytest.approx(75.0)
    assert macro.detail["recall"] == pytest.approx(62.5)
    assert macro.value == pytest.approx(200 / 3)

    assert results["classification.samples_f1"].value == pytest.approx(650 / 9)
    assert results["classification.subset_accuracy"].value == pytest.approx(100 / 3)


def test_zero_division_golden_scores() -> None:
    """An example with no predicted labels drives a per-label zero denominator.

    Under the zero-division policy the affected label scores 0 rather than raising,
    which pulls macro precision below micro precision.
    """
    references, predictions, vocabulary = _zero_division_dataset()
    results = classification_dataset(references, predictions, vocabulary)

    micro = results["classification.micro_prf"]
    assert micro.detail is not None
    assert micro.detail["precision"] == pytest.approx(100.0)
    assert micro.detail["recall"] == pytest.approx(200 / 3)
    assert micro.value == pytest.approx(80.0)

    macro = results["classification.macro_prf"]
    assert macro.detail is not None
    assert macro.detail["precision"] == pytest.approx(200 / 3)
    assert macro.detail["recall"] == pytest.approx(200 / 3)
    assert macro.value == pytest.approx(200 / 3)

    assert results["classification.samples_f1"].value == pytest.approx(50.0)
    assert results["classification.subset_accuracy"].value == pytest.approx(50.0)
