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

"""Compare two runs of one evaluation, deterministically.

The prompt self-improvement loop changes an agent, runs the same evaluation
again, and reads the difference. That difference is arithmetic, so it is done
here rather than by a model reading two lists of scores: per metric, the
candidate's score minus the baseline's, on the 0-100 scale every run score
uses, read against a noise margin of ``NOISE_MARGIN_POINTS``. Beyond the
margin it is a progression or a regression; within it (the bounds included) it
is neutral, because a judge re-scoring the same answers moves by about that
much on its own.

Which metric decides what the loop does next is the caller's decision, not
this module's: every metric is compared and reported.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

NOISE_MARGIN_POINTS = 2.0

#: The run-level scores, all 0-100 and higher is better: the two headlines,
#: Overall Quality and Overall Performance, then the LLM judge's score,
#: which ``QUALITY_METRIC`` names as an evaluation's quality score. The retired
#: ``speed_score``, ``overall_score`` and ``f1_score`` are not written on a new
#: run, so they are not compared.
COMPARED_METRICS: tuple[str, ...] = ("quality_score", "performance_score", "score")

#: The run-level score the persona self-improvement loop judges a rewrite on,
#: for every evaluation: the LLM judge's score. An evaluation still set to the
#: retired F1 (``quality_source == "f1"``, kept by an older row, an import or a
#: copy) is judged on it too, since no new run records an F1.
QUALITY_METRIC = "score"

Verdict = Literal["progression", "neutral", "regression"]


@dataclass(frozen=True, slots=True)
class MetricComparison:
    metric: str
    baseline: float | None
    candidate: float | None
    #: ``None`` when either run has no value for the metric.
    delta: float | None
    verdict: Verdict | None


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def verdict_for(delta: float, margin: float = NOISE_MARGIN_POINTS) -> Verdict:
    if delta > margin:
        return "progression"
    if delta < -margin:
        return "regression"
    return "neutral"


def compare_metric(
    metric: str,
    baseline: float | None,
    candidate: float | None,
    margin: float = NOISE_MARGIN_POINTS,
) -> MetricComparison:
    if not _finite(baseline) or not _finite(candidate):
        return MetricComparison(metric, baseline, candidate, None, None)
    delta = round(float(candidate) - float(baseline), 2)  # type: ignore[arg-type]
    return MetricComparison(metric, baseline, candidate, delta, verdict_for(delta, margin))


def compare_scores(
    baseline: Mapping[str, float | None],
    candidate: Mapping[str, float | None],
    margin: float = NOISE_MARGIN_POINTS,
) -> list[MetricComparison]:
    """Every ``COMPARED_METRICS`` entry of two runs, in that order."""
    return [
        compare_metric(metric, baseline.get(metric), candidate.get(metric), margin)
        for metric in COMPARED_METRICS
    ]
