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

"""An answer a metric cannot read scores 0 and counts like the expected answer.

An unparseable answer, one whose STIX bundle is malformed, or one the platform could not
extract, is the agent's failure: ``scored``/0, never ``invalid``, which the run would drop.
What it adds to a run that pools or macro-averages is one rule, whatever made it
unreadable: it earns nothing of what the expected answer earns, and weighs as much.

- ``pooled``: 0 of the expected answer's denominator, or 0 of 1 when nothing was expected
  (an unreadable answer is never the correct empty one). For an F1 that is an answer as
  large as the expected one with every item wrong.
- ``macro``: the expected answer's classes, every one missed.
- ``mean``: 0.

The expected answer is the metric scored with the reference as the answer, so the rule
needs no knowledge of the metric. A metric the expected answer does not score stays as it
is when the author left nothing to compare; when only a limit kept the expected answer from
being scored (``limited``), the unreadable answer is at fault all the same and weighs one
item.
"""

from collections.abc import Mapping

from app.evaluation_metrics.quality.results import (
    MACRO_CLASSES,
    POOLED_DENOMINATOR,
    POOLED_NUMERATOR,
    JSONValue,
    MetricRollup,
    MetricStatus,
    ScoreResult,
)


def unreadable_answer(
    expected: ScoreResult, detail: Mapping[str, JSONValue], *, limited: bool = False
) -> ScoreResult:
    """*expected*'s metric scored 0 for an answer it cannot read, *detail* saying why.

    Args:
        expected: The metric's result for the reference given as the answer.
        detail: Why the answer cannot be read (``reason`` and its message); the counts the
            roll-up needs are added to it.
        limited: *expected* is not scored only because the reference exceeds a compute
            limit, not because the author left nothing to compare.

    Returns:
        ``scored``/0 with the counts of the rule above; *expected* unchanged when it is not
        ``scored`` and not *limited*.
    """
    if expected.status is not MetricStatus.scored and not limited:
        return expected
    counts: dict[str, JSONValue] = {}
    rollup = expected.metadata.rollup
    if expected.status is not MetricStatus.scored:
        # Nothing to weigh the expected answer by: one wrong item, or no class.
        if rollup is MetricRollup.pooled:
            counts = {POOLED_NUMERATOR: 0, POOLED_DENOMINATOR: 1}
        elif rollup is MetricRollup.macro:
            counts = {MACRO_CLASSES: {}}
    elif rollup is MetricRollup.pooled:
        weight = expected.detail.get(POOLED_DENOMINATOR)
        counts = {POOLED_NUMERATOR: 0, POOLED_DENOMINATOR: weight or 1}
    elif rollup is MetricRollup.macro:
        classes = expected.detail.get(MACRO_CLASSES) or {}
        counts = {
            MACRO_CLASSES: {
                name: {"tp": 0, "fp": 0, "fn": held["tp"] + held["fn"]}
                for name, held in classes.items()
            }
        }
    return ScoreResult(
        status=MetricStatus.scored,
        value=0.0,
        detail={**detail, **counts},
        metadata=expected.metadata,
    )


__all__ = ["unreadable_answer"]
