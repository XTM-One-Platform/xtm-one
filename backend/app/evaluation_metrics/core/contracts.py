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

"""Contracts shared by the quality and performance suites.

A symbol belongs here only if both suites depend on it. The result types do not:
``ScoreResult`` is a 0-100 score and ``PerformanceResult`` carries a unit.
"""

import math
from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.evaluation_metrics.core.serialization import (
    FrozenMapping,
    FrozenStrList,
    _empty_locations,
    _empty_mapping,
)

# A JSON-compatible value. Canonical serialization rejects NaN/Infinity at the boundary.
type JSONValue = (
    None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
)


class MetricStatus(StrEnum):
    """Exactly one of these describes every result."""

    scored = "scored"
    invalid = "invalid"
    skipped = "skipped"
    unavailable = "unavailable"


class MetricDirection(StrEnum):
    """Whether a higher or lower value is better for a metric."""

    higher_is_better = "higher_is_better"
    lower_is_better = "lower_is_better"


class MetricRollup(StrEnum):
    """How a metric's entry values make one run value; declared by every metric.

    ``mean``: the value grades the whole entry (a score, a time, tokens, a cost, a count), so
    the run averages entries, less the spread penalty. ``pooled``: the value is a share of
    things counted inside the entry (calls, labels, entities, fields), so the run sums every
    entry's counts and divides, with no spread penalty; averaging would weigh an entry of 1
    item like one of 100. A pass/fail metric is pooled over one item per entry, so the run
    reports its share of passes: on a pass/fail value the spread penalty is a function of
    that share, not of consistency. A scored pooled result carries its counts in ``detail``
    under ``POOLED_NUMERATOR`` and ``POOLED_DENOMINATOR``. ``macro``: the value is the
    unweighted mean of a per-class F1, so the run sums each class's counts over its entries
    and averages the classes' F1 over the run; a scored macro result carries its per-class
    counts in ``detail[MACRO_CLASSES]``.
    """

    mean = "mean"
    pooled = "pooled"
    macro = "macro"


#: The ``detail`` keys of a scored ``pooled`` result's counts: its value is their ratio.
POOLED_NUMERATOR = "numerator"
POOLED_DENOMINATOR = "denominator"
#: The ``detail`` key of a scored ``macro`` result's per-class counts:
#: ``{class: {"tp": int, "fp": int, "fn": int}}``, the classes seen on either side. A class
#: may carry more keys (``stix.entity_prf.by_type`` adds its ``f1``); a run reads only these.
MACRO_CLASSES = "classes"
MACRO_COUNT_KEYS = ("tp", "fp", "fn")


def _is_count(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(value)
        and value >= 0
    )


def check_rollup_counts(
    rollup: MetricRollup, scored: bool, value: float | None, detail, scale: float = 1.0
) -> None:
    """Raise unless a scored result carries what its ``rollup`` combines over the run.

    ``scale`` is the result's scale: 1 for a performance rate, 100 for a quality score.

    - ``pooled``: non-negative real counts whose ratio is the value (a weighted share
      is not a whole number). A quality score may report 0 of 0, an empty answer, which is
      correct (``empty_answer``), at its full value: the metric must be higher-is-better.
    - ``macro``: non-negative integer counts per class. The value is the entry's own macro
      F1, over the classes this entry saw, and is not re-derived here.
    """
    if not scored:
        return
    if rollup is MetricRollup.macro:
        _check_macro_classes(detail)
        return
    if rollup is not MetricRollup.pooled:
        return
    numerator = detail.get(POOLED_NUMERATOR)
    denominator = detail.get(POOLED_DENOMINATOR)
    for name, count in ((POOLED_NUMERATOR, numerator), (POOLED_DENOMINATOR, denominator)):
        if not _is_count(count):
            raise ValueError(f"a scored pooled result requires a non-negative detail[{name!r}]")
    if numerator > denominator or (denominator == 0 and (numerator or scale != 100.0)):
        raise ValueError(
            "a scored pooled result requires 0 <= numerator <= denominator > 0, "
            "or 0 of 0 for an empty answer on a quality score"
        )
    expected = scale if denominator == 0 else scale * (numerator / denominator)
    if value is None or abs(value - expected) > 1e-9 * scale:
        raise ValueError("a scored pooled result's value must be numerator / denominator")


def _check_macro_classes(detail) -> None:
    classes = detail.get(MACRO_CLASSES)
    if not isinstance(classes, Mapping):
        raise ValueError(f"a scored macro result requires a detail[{MACRO_CLASSES!r}] mapping")
    for name, counts in classes.items():
        if not isinstance(name, str) or not isinstance(counts, Mapping):
            raise ValueError(f"detail[{MACRO_CLASSES!r}] maps a class name to its counts")
        for key in MACRO_COUNT_KEYS:
            count = counts.get(key)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError(
                    f"a macro class's {key!r} must be a non-negative integer ({name!r})"
                )


class MetricError(BaseModel):
    """Structured description of a failure attached to ``invalid`` (and diagnostic) results."""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    locations: FrozenStrList = Field(default_factory=_empty_locations)
    details: FrozenMapping = Field(default_factory=_empty_mapping)
