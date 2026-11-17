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

"""CLASSIFICATION — label-vocabulary validity gate.

Validates predicted labels against a caller-supplied allowed vocabulary and lists every
offending label deterministically.

A violation reports ``scored``/0.0, not ``invalid`` (the same shape as the STIX
envelope gate). ``services/evaluation/metrics.py`` persists only ``scored``
outcomes, so an ``invalid`` gate would drop the entry from the run aggregate and the
run's quality would read higher than the agent earned.

The offenders travel in ``detail`` rather than in a ``MetricError``, because a ``scored``
result may not carry one (``ScoreResult._validate_invariants``).
"""

from collections.abc import Iterable
from collections.abc import Set as AbstractSet

from app.evaluation_metrics.quality.primitives.pass_fail import pass_fail_counts
from app.evaluation_metrics.quality.results import (
    MetricDirection,
    MetricMetadata,
    MetricRollup,
    MetricStatus,
    ScoreResult,
)

_META = MetricMetadata(
    key="classification.vocabulary_validity",
    label="Only Known Labels",
    kind="validity",
    primary_measure="valid",
    direction=MetricDirection.higher_is_better,
    rollup=MetricRollup.pooled,
)


def _offenders(prediction: Iterable[str], vocabulary: AbstractSet[str]) -> list[str]:
    """Return the deterministically ordered, de-duplicated out-of-vocabulary labels."""
    return sorted({label for label in prediction if label not in vocabulary})


def label_vocabulary_validity(
    prediction: Iterable[str], vocabulary: AbstractSet[str]
) -> ScoreResult:
    """Gate predicted labels against an allowed ``vocabulary``.

    Args:
        prediction: The predicted labels (duplicates are collapsed for reporting).
        vocabulary: The allowed label universe.

    Returns:
        ``scored``/100.0 when every predicted label is in the vocabulary, otherwise
        ``scored``/0.0, so the violation survives into the persisted metrics.
        Both carry the same ``detail`` shape.
    """
    offenders = _offenders(prediction, vocabulary)
    return ScoreResult(
        status=MetricStatus.scored,
        value=0.0 if offenders else 100.0,
        detail={
            "valid": not offenders,
            "offending_labels": offenders,
            "offending_label_count": len(offenders),
            **pass_fail_counts(not offenders),
        },
        metadata=_META,
    )


__all__ = ["label_vocabulary_validity"]
