# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""CLASSIFICATION — dataset-level multi-label classification metrics.

Computes micro/macro precision/recall/F1, samples-F1 and subset accuracy over an ordered
collection of reference/predicted label sets.

An out-of-vocabulary label is a wrong answer, not an unscoreable one. Every
metric computes and the extra label counts as a false positive. The platform persists
only ``scored`` outcomes (``services/evaluation/metrics.py``), so withholding the metrics
would leave the entry with no quality metric at all. When the vocabulary is derived from
the dataset's reference labels, an out-of-vocabulary label is over-prediction by
definition.

Macro averaging runs over the observed union (reference ∪ prediction): a predicted-only
label is penalized, and a label absent from both sides is excluded. A declared label no
entry used tests nothing; counting it as 0 would lower every agent alike and hide an
invented label.

An entry where nothing was expected and nothing was predicted scores as correct; see
``empty_answer``.

Each metric reports what a collection of these calls combines into: micro F1 and
subset accuracy their counts (``pooled``), macro F1 its per-label counts (``macro``), so
calling this once per example and combining the results gives the same values as calling
it once on the whole collection. Samples F1 is a mean by definition. A collection with no
example has nothing to score: every metric is ``skipped`` (``no_examples``).
"""

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet

from app.evaluation_metrics.core.errors import ConfigurationError
from app.evaluation_metrics.quality.families.classification.vocabulary import (
    label_vocabulary_validity,
)
from app.evaluation_metrics.quality.primitives.aggregate import macro_average
from app.evaluation_metrics.quality.primitives.empty_answer import (
    pooled_f1_counts,
    prf_with_empty_answer,
)
from app.evaluation_metrics.quality.primitives.sets import DiagnosticSet, set_diagnostics
from app.evaluation_metrics.quality.results import (
    MACRO_CLASSES,
    POOLED_DENOMINATOR,
    POOLED_NUMERATOR,
    JSONValue,
    MetricDirection,
    MetricMetadata,
    MetricRollup,
    MetricStatus,
    ScoreResult,
)

_MICRO_META = MetricMetadata(
    key="classification.micro_prf",
    label="Label F1, All Labels Pooled (Micro)",
    kind="aggregate",
    primary_measure="f1",
    direction=MetricDirection.higher_is_better,
    rollup=MetricRollup.pooled,
    # On one entry: that entry's own label F1, the one the strip shows.
    entry_label="Label F1",
)
_MACRO_META = MetricMetadata(
    key="classification.macro_prf",
    label="Label F1, Averaged per Label (Macro)",
    kind="aggregate",
    primary_measure="f1",
    direction=MetricDirection.higher_is_better,
    rollup=MetricRollup.macro,
    # On one entry each label is found (1) or not (0), so the mean is the share of labels
    # found among those expected or predicted: not a macro F1.
    shown_on_entry=False,
)
_SAMPLES_META = MetricMetadata(
    key="classification.samples_f1",
    label="Label F1, Averaged per Entry (Samples)",
    kind="aggregate",
    primary_measure="f1",
    direction=MetricDirection.higher_is_better,
    rollup=MetricRollup.mean,
    # On one entry: the entry's own label F1, which micro already shows.
    shown_on_entry=False,
)
_SUBSET_META = MetricMetadata(
    key="classification.subset_accuracy",
    label="Exact Label Set",
    kind="aggregate",
    primary_measure="accuracy",
    direction=MetricDirection.higher_is_better,
    rollup=MetricRollup.pooled,
)


def _per_label_diagnostics(
    references: Sequence[AbstractSet[str]],
    predictions: Sequence[AbstractSet[str]],
    universe: Sequence[str],
) -> dict[str, DiagnosticSet[str]]:
    """Aggregate per-label TP/FP/FN across the dataset for the given ``universe``."""
    result: dict[str, DiagnosticSet[str]] = {}
    for label in universe:
        tp = fp = fn = 0
        for reference, prediction in zip(references, predictions, strict=True):
            in_ref = label in reference
            in_pred = label in prediction
            if in_ref and in_pred:
                tp += 1
            elif in_pred:
                fp += 1
            elif in_ref:
                fn += 1
        result[label] = DiagnosticSet[str](tp=tp, fp=fp, fn=fn)
    return result


def _observed_union(
    references: Sequence[AbstractSet[str]],
    predictions: Sequence[AbstractSet[str]],
) -> list[str]:
    labels: set[str] = set()
    for side in (references, predictions):
        for label_set in side:
            labels.update(label_set)
    return sorted(labels)


def _micro_result(diagnostics: Sequence[DiagnosticSet[str]]) -> ScoreResult:
    tp = sum(d.tp for d in diagnostics)
    fp = sum(d.fp for d in diagnostics)
    fn = sum(d.fn for d in diagnostics)
    # Scored from the pooled counts, not through the primitive's zero-division policy:
    # zero counts mean every example was empty on both sides, a correct answer.
    precision, recall, f1 = prf_with_empty_answer(tp, fp, fn)
    detail: dict[str, JSONValue] = {
        "precision": precision * 100.0,
        "recall": recall * 100.0,
        "f1": f1 * 100.0,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        **pooled_f1_counts(tp, fp, fn),
    }
    return ScoreResult(
        status=MetricStatus.scored, value=f1 * 100.0, detail=detail, metadata=_MICRO_META
    )


def _macro_result(
    references: Sequence[AbstractSet[str]],
    predictions: Sequence[AbstractSet[str]],
) -> ScoreResult:
    universe = _observed_union(references, predictions)
    per_label = _per_label_diagnostics(references, predictions, universe)
    # No label anywhere: every example was answered correctly, as the three sibling
    # metrics say.
    precision, recall, f1 = macro_average(per_label) if per_label else (1.0, 1.0, 1.0)
    detail: dict[str, JSONValue] = {
        "precision": precision * 100.0,
        "recall": recall * 100.0,
        "f1": f1 * 100.0,
        MACRO_CLASSES: {
            label: {"tp": diag.tp, "fp": diag.fp, "fn": diag.fn}
            for label, diag in per_label.items()
        },
    }
    return ScoreResult(
        status=MetricStatus.scored, value=f1 * 100.0, detail=detail, metadata=_MACRO_META
    )


def _samples_f1_result(diagnostics: Sequence[DiagnosticSet[str]]) -> ScoreResult:
    # Per example: an empty-vs-empty example counts as 1.0, even in a multi-entry dataset.
    total = sum(prf_with_empty_answer(d.tp, d.fp, d.fn)[2] for d in diagnostics)
    return ScoreResult(
        status=MetricStatus.scored,
        value=total / len(diagnostics) * 100.0,
        detail={"examples": len(diagnostics)},
        metadata=_SAMPLES_META,
    )


def _subset_accuracy_result(
    references: Sequence[AbstractSet[str]],
    predictions: Sequence[AbstractSet[str]],
) -> ScoreResult:
    total = len(references)
    matches = sum(
        1
        for reference, prediction in zip(references, predictions, strict=True)
        if set(reference) == set(prediction)
    )
    return ScoreResult(
        status=MetricStatus.scored,
        value=matches / total * 100.0,
        detail={
            "matches": matches,
            "examples": total,
            POOLED_NUMERATOR: matches,
            POOLED_DENOMINATOR: total,
        },
        metadata=_SUBSET_META,
    )


def _no_examples(meta: MetricMetadata) -> ScoreResult:
    """Nothing to score is not an empty answer, which is correct: the metric is skipped."""
    return ScoreResult(
        status=MetricStatus.skipped,
        value=None,
        detail={"reason": "no_examples"},
        metadata=meta,
    )


def classification_dataset(
    references: Sequence[AbstractSet[str]],
    predictions: Sequence[AbstractSet[str]],
    vocabulary: AbstractSet[str],
) -> Mapping[str, ScoreResult]:
    """Score a multi-label classification dataset behind stable metric keys.

    Args:
        references: Ordered reference label sets, one per example.
        predictions: Ordered predicted label sets, aligned with ``references``.
        vocabulary: The allowed label universe, read by the validity gate only.

    Returns:
        A deterministically ordered mapping keyed by stable metric key: the
        vocabulary-validity gate plus micro/macro P/R/F1, samples-F1 and subset accuracy,
        on a 0-100 scale. Every key is always present and ``scored``, except with no
        example (``skipped``): a vocabulary violation lowers the gate's value and the
        metrics, never withholds them.

    Raises:
        ConfigurationError: If the collections differ in length.
    """
    if len(references) != len(predictions):
        raise ConfigurationError(
            "references and predictions must have the same length "
            f"({len(references)} != {len(predictions)})"
        )

    # The gate reports and never withholds the other metrics. It comes first so a
    # reader meets the vocabulary verdict before the numbers.
    all_predicted = [label for prediction in predictions for label in prediction]
    gate = label_vocabulary_validity(all_predicted, vocabulary)
    results: dict[str, ScoreResult] = {gate.metadata.key: gate}

    if not references:
        for meta in (_MICRO_META, _MACRO_META, _SAMPLES_META, _SUBSET_META):
            results[meta.key] = _no_examples(meta)
        return results

    diagnostics = [
        set_diagnostics(reference, prediction)
        for reference, prediction in zip(references, predictions, strict=True)
    ]
    results[_MICRO_META.key] = _micro_result(diagnostics)
    results[_MACRO_META.key] = _macro_result(references, predictions)
    results[_SAMPLES_META.key] = _samples_f1_result(diagnostics)
    results[_SUBSET_META.key] = _subset_accuracy_result(references, predictions)
    return results


__all__ = ["classification_dataset"]
