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

"""Two-stage metric aggregation into the Quality and Performance headlines.

This module is the **only** place that turns stored metric values into a headline.
Every surface (run rows, leader cards, ranked lists, trend charts, correlation views,
exports) reads what it persisted and none recompute: the performance headline is
comparative, so a consumer recomputing over a different run set would show a different
number for the same run.

The pipeline::

    evaluation_metrics rows        (per result, per key)
            |
            v  Stage 1 — reduce across data entries, per metric, per run
            |            (averaged, pooled or macro, as the metric declares)
    AggregatedMetric[]             (per run, per key)
            |
            v  Normalise — performance only, across the evaluation's runs
    normalised values              (0-100, comparative)
            |
            v  Stage 2 — equal-weight mean across the selected metric subset
    HeadlineValue                  (quality_score | performance_score)

The LLM Judge headline does **not** pass through here; it is written to
``EvaluationRun.score`` by its own computation.

**``None`` and ``0.0`` are different answers** and stay distinguishable end to end. A
headline of ``None`` renders as "N/A" and is excluded from ranking, leaders and charts;
it is never sorted as zero. A downstream ``or 0`` must not conflate them.

This module never imports ``evaluation_metrics``. Orientation and package-membership
questions go through ``metric_package``, the platform's sole importer of it (enforced by
``tests/test_evaluation_guards.py``).
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.evaluation import Evaluation, EvaluationResult
from app.models.evaluation_metric import EvaluationMetric
from app.services.evaluation.framing import ANOMALY_KIND_CONFIRMATION_SEEKING
from app.services.evaluation.metric_package import (
    HIGHER_IS_BETTER,
    LOWER_IS_BETTER,
    MACRO_COUNT_KEYS,
    family_is_valid,
    is_macro_key,
    is_performance_key,
    is_pooled_key,
    is_quality_key,
    macro_f1_from_counts,
    metric_catalog,
    metric_direction,
    quality_catalog,
)

logger = logging.getLogger(__name__)

# Reasons a selected metric could not contribute. ``not_applicable`` means no re-run can
# recover the metric, so the UI must not offer one; collapsing it into ``failed`` sends
# authors retrying an outcome that cannot change.
REASON_NOT_APPLICABLE = "not_applicable"
REASON_FAILED = "failed"
# Only where no selection exists: the metric scored but counts only when
# selected. Its remedy is selecting the metric, not re-running it.
REASON_NOT_A_DEFAULT_CONTRIBUTOR = "not_a_default_contributor"
# Performance only: Overall Performance counts the metrics every run has, so a
# run that measured a metric other runs lack loses it. Its record gives this reason, the
# lacking runs in `run_ids`; those runs' records carry their shared reason.
REASON_MISSING_FROM_OTHER_RUNS = "missing_from_other_runs"
# Run-level disclosures: they describe a RUN, not a metric, so they carry the
# sentinel key below.
REASON_NO_PERFORMANCE_DATA = "no_performance_data"
REASON_TOO_FEW_COMPARABLE_RUNS = "too_few_comparable_runs"
# Why Overall Performance is N/A when the runs do share a metric: the author
# selected none, or, with no selection, every metric the runs share is opt-in (time to
# first token). Without them the reader is told the runs share no metric.
REASON_NO_METRIC_SELECTED = "no_metric_selected"
REASON_ONLY_OPT_IN_METRICS_SHARED = "only_opt_in_metrics_shared"

# Not a metric key: package keys are always dotted names, so this cannot collide.
# Consumers that map exclusions onto metric rows must filter it explicitly.
RUN_LEVEL_EXCLUSION_KEY = "__run__"

# Package statuses, mirrored by value (``metric_package.MetricStatus``), read from
# ``execution_details["metric_results"]`` to classify why a selected metric is missing.
_NOT_APPLICABLE_STATUSES = frozenset({"unavailable", "skipped"})
_FAILED_STATUSES = frozenset({"invalid"})

# Quality metrics are absolute package scores on a **0-100** scale: `ScoreResult` refuses
# a `scored` result outside `0.0 <= value <= 100.0`. Do not rescale from 0-1: real values
# (`stix.entity_prf` at 35.09) would fall outside the guard and Overall Quality would read
# N/A. A value outside 0-100 cannot come from the package, so it is excluded rather than
# rendered as a plausible percentage.
_QUALITY_MIN = 0.0
_QUALITY_MAX = 100.0

_SCORE_SCALE = 100.0


def counts_in_headlines(result: object) -> bool:
    """Whether a result is one a run's headlines are computed from.

    Anomalous and judge-failed results are excluded, as in the run-level judge
    aggregation. A confirmation-seeking entry counts: inclusion is "no anomaly"
    OR "confirmation-seeking", never "not transient-error". A run none of whose results
    count measured nothing.

    *result* is an ``EvaluationResult`` or anything with its anomaly kind and judge-failure
    flag; :func:`result_counts_in_headlines` is the same rule in SQL.
    """
    kind = getattr(result, "execution_anomaly_kind", None)
    return (kind is None or kind == ANOMALY_KIND_CONFIRMATION_SEEKING) and not getattr(
        result, "is_judge_failure", False
    )


def result_counts_in_headlines() -> ColumnElement[bool]:
    """:func:`counts_in_headlines` as a SQL condition on ``EvaluationResult``."""
    return and_(
        or_(
            EvaluationResult.execution_anomaly_kind.is_(None),
            EvaluationResult.execution_anomaly_kind == ANOMALY_KIND_CONFIRMATION_SEEKING,
        ),
        EvaluationResult.is_judge_failure.is_(False),
    )


@dataclass(frozen=True)
class AggregatedMetric:
    """One metric reduced across an evaluation's data entries, for one run.

    A metric with zero contributing entries produces **no** ``AggregatedMetric``, never
    one with ``value=0``.
    """

    key: str
    value: float
    entry_count: int
    direction: str | None


@dataclass(frozen=True)
class ExcludedMetric:
    """A metric that could not contribute to a headline, and why.

    Selected by the author, or, with the default selection, dropped because some runs
    lack it; or a run-level entry under ``RUN_LEVEL_EXCLUSION_KEY``.
    """

    key: str
    reason: str
    run_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(frozen=True)
class HeadlineValue:
    """A computed headline plus the disclosure needed to read it honestly."""

    value: float | None
    contributing_keys: list[str] = field(default_factory=list)
    excluded: list[ExcludedMetric] = field(default_factory=list)
    # Per contributing key, how many data entries backed its Stage-1 aggregate.
    # Without it a headline over 2 entries reads like one over 50.
    entry_counts: dict[str, int] = field(default_factory=dict)
    # Per contributing key, the 0-100 value that entered the mean. NOT the raw mean the
    # Metrics panel shows: quality is rolled up as each metric declares (pooled, macro,
    # or averaged less the spread penalty) and performance is normalised across the
    # evaluation. Persisted, never re-derived downstream.
    contributions: dict[str, float] = field(default_factory=dict)
    # How many of the entries the mean considers it covers, and why the others are left
    # out. Quality only.
    coverage: EntryCoverage | None = None


@dataclass(frozen=True)
class EntryCoverage:
    """The entries a headline considers, those it covers, and why the others are out."""

    entries: int
    counted: int
    left_out: dict[str, int]

    def as_detail(self) -> dict[str, object]:
        return {"entries": self.entries, "counted": self.counted, "left_out": dict(self.left_out)}


# --- Stage 1: reduce across data entries -----------------------------------------------


def _std(values: list[float], mean: float) -> float:
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(variance)


def _reduce_quality(values: list[float], direction: str, penalty_factor: float) -> float:
    """Orient each entry value to a 0-100 goodness score, then penalise inconsistency.

    Values are already 0-100, so orienting is a *reflection*, not a rescale: a
    `higher_is_better` value passes through and a `lower_is_better` one becomes its
    complement.

    Orienting *before* reducing keeps the spread penalty pointing the right way:
    subtracting the deviation from a raw ``lower_is_better`` value would reward an
    inconsistent run. On goodness scores the arithmetic matches the judge path.
    """
    oriented = [
        v if direction == HIGHER_IS_BETTER else _QUALITY_MAX - v for v in values
    ]
    n = len(oriented)
    mean = sum(oriented) / n
    if n == 1:
        return round(mean, 2)
    penalised = mean - penalty_factor * _std(oriented, mean)
    return round(max(0.0, min(_SCORE_SCALE, penalised)), 2)


def _reduce_performance(
    values: list[float], direction: str, penalty_factor: float
) -> float:
    """Reduce raw performance measurements in their **native** units.

    Not ``_penalized_mean``: its ``[0, 100]`` clamp would flatten a 120-second run and a
    5000-token run to 100 before they are compared. Normalisation maps these values onto
    0-100 later, against the evaluation's other runs.

    The spread penalty always moves the value toward *worse*: for a ``lower_is_better``
    metric it adds the deviation.
    """
    n = len(values)
    mean = sum(values) / n
    if n == 1:
        return round(mean, 6)
    deviation = penalty_factor * _std(values, mean)
    penalised = mean + deviation if direction == LOWER_IS_BETTER else mean - deviation
    return round(max(0.0, penalised), 6)


async def aggregate_metrics_by_run(
    db: AsyncSession,
    run_ids: Sequence[uuid.UUID],
    penalty_factor: float = 0.5,
) -> dict[uuid.UUID, dict[str, AggregatedMetric]]:
    """Stage 1 for **several** runs in one round trip.

    Overall Performance is comparative, so every completed run is rescored on each run
    completion. A per-run fetch would cost O(R) queries per completion, serialized on one
    ``AsyncSession`` inside a transaction-scoped advisory lock.

    Grouped in Python, not SQL, so the batched and single-run paths share
    :func:`reduce_grouped_metrics` and cannot drift. Only the fetch differs.

    Every requested run gets an entry, so a run with no contributing rows is an empty
    mapping rather than a missing key -- callers distinguish "no metrics" from "not
    asked for". Within a run, a key with no values yields no entry, never
    ``value=0.0``.

    Run ids are normalised before grouping: the ``WHERE`` clause accepts a stringified
    UUID but the driver returns a :class:`uuid.UUID`, so a string id would silently lose
    every row and read as a legitimate N/A. This guards that failure; it does **not**
    widen the API to ``str``. Callers pass UUIDs.
    """
    if not run_ids:
        return {}

    # Maps the normalised id back to the exact object the caller passed, so the
    # returned mapping is keyed the way the caller can look it up.
    requested: dict[uuid.UUID, uuid.UUID] = {}
    for rid in run_ids:
        requested[rid if isinstance(rid, uuid.UUID) else uuid.UUID(str(rid))] = rid

    rows = (
        await db.execute(
            select(
                EvaluationMetric.run_id,
                EvaluationMetric.key,
                EvaluationMetric.value,
                EvaluationMetric.numerator,
                EvaluationMetric.denominator,
                EvaluationMetric.class_counts,
            )
            .join(EvaluationResult, EvaluationResult.id == EvaluationMetric.result_id)
            .where(
                EvaluationMetric.run_id.in_(run_ids),
                EvaluationMetric.value.isnot(None),
                result_counts_in_headlines(),
            )
        )
    ).all()

    grouped: dict[uuid.UUID, dict[str, list[float]]] = {
        original: {} for original in requested.values()
    }
    counts: dict[uuid.UUID, dict[str, list[tuple[float, float] | None]]] = {
        original: {} for original in requested.values()
    }
    classes: dict[uuid.UUID, dict[str, list[ClassCounts | None]]] = {
        original: {} for original in requested.values()
    }
    for run_id, key, value, numerator, denominator, class_counts in rows:
        if value is None:
            continue
        # `.in_()` cannot return a run that was not asked for, but falling back to the
        # row's own id keeps the grouping total if that ever stops being true.
        original = requested.get(run_id, run_id)
        grouped.setdefault(original, {}).setdefault(str(key), []).append(float(value))
        counts.setdefault(original, {}).setdefault(str(key), []).append(
            None if numerator is None or denominator is None else (numerator, denominator)
        )
        classes.setdefault(original, {}).setdefault(str(key), []).append(class_counts)

    return {
        run_id: reduce_grouped_metrics(
            by_key,
            penalty_factor,
            counts=counts.get(run_id),
            class_counts=classes.get(run_id),
        )
        for run_id, by_key in grouped.items()
    }


async def aggregate_run_metrics(
    db: AsyncSession,
    run_id: uuid.UUID,
    penalty_factor: float = 0.5,
) -> dict[str, AggregatedMetric]:
    """Stage 1 — reduce every package metric across one run's data entries.

    Reads the indexed ``evaluation_metrics`` table (never the ``execution_details``
    blob), joined to ``evaluation_results`` to reach the exclusion flags.

    A key with no contributing values yields **no entry** in the returned mapping,
    never ``value=0.0``.

    The single-run form of :func:`aggregate_metrics_by_run`, which it delegates to so
    the two can never diverge. Use it only where the caller has one run
    (:func:`compute_quality_headline`, which is absolute and per-run); anything scoring
    a whole evaluation must batch.
    """
    return (await aggregate_metrics_by_run(db, [run_id], penalty_factor))[run_id]


def _pool(
    entry_counts: list[tuple[float, float] | None] | None, entries: int, *, quality: bool
) -> float | None:
    """The run's pooled ratio, or ``None`` when it cannot be stated."""
    pairs = [c for c in entry_counts or [] if c is not None]
    if not pairs or len(pairs) != entries:
        return None
    numerator = sum(n for n, _ in pairs)
    denominator = sum(d for _, d in pairs)
    if denominator <= 0:
        # Every entry 0 of 0 (counts are never negative). A quality score's is an empty
        # answer, correct, so the run reads full; never a rate's: 0 errors of 0
        # calls is no rate. Assumes higher-is-better, as ``check_rollup_counts`` does.
        return 1.0 if quality else None
    return numerator / denominator


#: One entry's per-class counts of a macro metric: ``{class: {"tp", "fp", "fn"}}``.
ClassCounts = dict[str, dict[str, int]]


def _macro(
    entry_classes: list[ClassCounts | None] | None, values: list[float]
) -> float | None:
    """The run's macro F1, or ``None`` when an entry lacks its classes.

    Each class's counts are summed over the entries, then the classes' F1 averaged, each
    class seen in the reference or the answer weighing the same: a rare class missed
    everywhere pulls the run down as much as a frequent one. Averaging the entries' own
    macro F1 would score a class only where it appears, and rank a classifier that ignores
    the rare classes above one that finds them.

    A run that saw no class at all reads its entries' own values: each is an empty answer
    (100) or an answer the metric could not read where nothing was expected (0).
    """
    held = [c for c in entry_classes or [] if c is not None]
    if not held or len(held) != len(values):
        return None
    totals: dict[str, dict[str, int]] = {}
    for classes in held:
        for name, counts in classes.items():
            total = totals.setdefault(name, dict.fromkeys(MACRO_COUNT_KEYS, 0))
            for count in MACRO_COUNT_KEYS:
                total[count] += counts[count]
    seen = [counts for counts in totals.values() if any(counts.values())]
    if not seen:
        return sum(values) / len(values) / _QUALITY_MAX
    return macro_f1_from_counts(seen)


def reduce_grouped_metrics(
    grouped: dict[str, list[float]],
    penalty_factor: float = 0.5,
    counts: dict[str, list[tuple[float, float] | None]] | None = None,
    class_counts: dict[str, list[ClassCounts | None]] | None = None,
) -> dict[str, AggregatedMetric]:
    """Reduce ``{key: [entry values]}`` into ``{key: AggregatedMetric}``.

    Split out from the query so the arithmetic is testable without a database, and so a
    caller that already holds the rows does not re-query.

    Each metric is rolled up as the package declares, with no spread
    penalty unless it is averaged:

    - pooled: the sum of its entries' numerators over the sum of their denominators
      (*counts*, one per entry, in the order of the values). It yields no entry when the
      run's total denominator is zero for a rate;
    - macro: the mean F1 of its classes, each class's counts (*class_counts*) summed over
      the run (:func:`_macro`);
    - otherwise the mean of its entries, less the spread penalty.

    A pooled or macro metric yields no entry when any entry lacks its counts (an older
    row): pooling over some entries would misstate the run, and averaging is the wrong
    arithmetic for it.
    """
    aggregated: dict[str, AggregatedMetric] = {}
    for key, values in grouped.items():
        if not values:
            # Absent, never 0.0.
            continue
        quality = is_quality_key(key)
        performance = is_performance_key(key)
        if not quality and not performance:
            # A legacy execution-level key (ttft_seconds, cost_usd, ...) an older row
            # still holds. Not a package metric, so never part of a headline.
            continue
        direction = metric_direction(key)
        if direction is None:
            # Fail-closed: a metric of unknown orientation is dropped, not assumed
            # higher-is-better. A wrong guess inverts the headline (the slowest, most
            # expensive run ranks first), which is worse than reporting nothing.
            continue
        if is_pooled_key(key) or is_macro_key(key):
            if is_macro_key(key):
                entry_counts: list | None = class_counts.get(key) if class_counts else None
                pooled = _macro(entry_counts, values)
            else:
                entry_counts = counts.get(key) if counts else None
                pooled = _pool(entry_counts, len(values), quality=quality)
            if pooled is None:
                # Not a warning: every run recorded without counts lands here. This
                # tells the logs why the metric left the headline.
                logger.debug(
                    "metric %s has counts for %d of %d entries, or a zero total; "
                    "it has no run value",
                    key,
                    sum(c is not None for c in entry_counts or []),
                    len(values),
                )
                continue
            if quality:
                score = pooled * _QUALITY_MAX
                value = round(score if direction == HIGHER_IS_BETTER else _QUALITY_MAX - score, 2)
            else:
                value = round(pooled, 6)
        elif quality:
            if any(v < _QUALITY_MIN or v > _QUALITY_MAX for v in values):
                # Outside the package's 0-100 guarantee: excluded rather than shown
                # as a valid percentage.
                continue
            value = _reduce_quality(values, direction, penalty_factor)
        else:
            value = _reduce_performance(values, direction, penalty_factor)
        aggregated[key] = AggregatedMetric(
            key=key,
            value=value,
            entry_count=len(values),
            direction=direction,
        )
    return aggregated


# --- Normalisation: performance only, across the evaluation's runs ---------------------


def _tolerance_threshold(
    best: float,
    lowest: float,
    highest: float,
    tolerance: float,
    direction: str,
) -> float:
    """Edge of the shared victory zone: how far from `best` a run still ties for the top.

    The band is a *percentage of the best value*: a 5% slower run is 5% slower whether
    the baseline is 200ms or 20s. That fails at `best == 0`, where it collapses to an
    exact tie however wide the tolerance, and at `best < 0`, where the scaled threshold
    lands on the far side of `best` and excludes the best run from its own zone (in
    both directions). A zero best is common: most performance metrics are
    lower-is-better, and retries or failure rates read `0.0` on the healthy run that
    anchors the comparison.

    There, the band is a percentage of the observed *span*, the only other length in
    the data. Not used everywhere: on ordinary positive values it disagrees with the
    multiplicative band (`[10, 20, 30]` lower-is-better: 51.28 vs 52.63) and would
    re-rank every stored evaluation.

    `tolerance` is floored at zero: a negative width also puts the threshold on the far
    side of `best`. The API schema refuses one, but bundle imports and older rows are
    read unvalidated, and this is the one place a tolerance becomes a threshold.
    """
    tolerance = max(tolerance, 0.0)
    if best > 0:
        signed = 1 + tolerance / 100 if direction == LOWER_IS_BETTER else 1 - tolerance / 100
        return best * signed
    band = (highest - lowest) * (tolerance / 100)
    return best + band if direction == LOWER_IS_BETTER else best - band


def normalise_across_runs(
    per_run: dict[uuid.UUID, AggregatedMetric],
    tolerance: float = 5.0,
) -> dict[uuid.UUID, float]:
    """Map one performance metric's per-run values onto a comparative 0-100 scale.

    Applied **only** to performance metrics. Quality metrics are absolute and are scaled
    without reference to siblings.

    Returns an empty mapping when the metric cannot be compared at all, which propagates
    as "this metric contributes to no run's headline".
    """
    if not per_run:
        return {}

    directions = {m.direction for m in per_run.values()}
    if len(directions) != 1:
        # Runs disagreeing on a metric's orientation were scored by different package
        # surfaces; comparing them is meaningless.
        return {}
    direction = directions.pop()
    if direction is None:
        # Fail-closed: normalising needs to know which end is "best", and guessing
        # could rank the run set backwards. Same guard as in `reduce_grouped_metrics`.
        return {}

    if len(per_run) < 2:
        # One run is both best and worst on every metric: no basis to normalise
        # against, so N/A, never a 100.0 for having no one to lose to.
        return {}

    values = {rid: m.value for rid, m in per_run.items()}
    lowest, highest = min(values.values()), max(values.values())

    if math.isclose(lowest, highest):
        # Two or more equal runs are a genuine tie, NOT a degeneracy: they were
        # compared and rank together at the top. A "best == worst" guard would wrongly
        # return N/A here.
        return dict.fromkeys(values, _SCORE_SCALE)

    if direction == LOWER_IS_BETTER:
        best, worst = lowest, highest
        threshold = _tolerance_threshold(best, lowest, highest, tolerance, direction)
        if worst <= threshold:
            return dict.fromkeys(values, _SCORE_SCALE)
        span = worst - threshold
        return {
            rid: _SCORE_SCALE
            if v <= threshold
            else round(_SCORE_SCALE * (worst - v) / span, 2)
            for rid, v in values.items()
        }

    best, worst = highest, lowest
    threshold = _tolerance_threshold(best, lowest, highest, tolerance, direction)
    if worst >= threshold:
        return dict.fromkeys(values, _SCORE_SCALE)
    span = threshold - worst
    return {
        rid: _SCORE_SCALE if v >= threshold else round(_SCORE_SCALE * (v - worst) / span, 2)
        for rid, v in values.items()
    }


def rankable_performance_keys(
    metrics: dict[str, AggregatedMetric],
) -> set[str]:
    """The performance metrics of one run that can actually be ranked.

    A run whose result is empty **abstains**: it has no performance opinion and must
    not shrink the shared basis for the runs that do.

    Never reads ``m.value``. A missing key already means "not measured", so a recorded
    ``0.0`` is a measurement that keeps its run in the comparison. A value-sensitive
    test would drop the most extreme observation and silently re-base every sibling.

    Shared by the intersection below and the run-level disclosure in
    :func:`compute_performance_headlines`, so they cannot disagree about who abstained.
    """
    return {k for k, m in metrics.items() if is_performance_key(k) and m.direction}


def normalise_performance(
    per_run_metrics: dict[uuid.UUID, dict[str, AggregatedMetric]],
    tolerance: float = 5.0,
) -> dict[uuid.UUID, dict[str, float]]:
    """Normalise every performance metric over the **intersection** of keys across runs.

    Not per run: a run holding only its most favourable metric would score near the top
    beside a run averaged over five, and the two would be shown as comparable.

    A run contributing NO rankable key abstains rather than vetoing: intersecting
    with its empty set would empty the basis for the whole evaluation, and since the
    basis is rebuilt from full history, permanently (a run predating the metric package,
    typically).

    A run with a *partial* key set does not abstain: dropping the keys it lacks is the
    rule above, which keeps every headline built from like inputs.
    """
    if not per_run_metrics:
        return {}

    key_sets = [
        keys
        for keys in (
            rankable_performance_keys(metrics) for metrics in per_run_metrics.values()
        )
        if keys
    ]
    shared: set[str] = set.intersection(*key_sets) if key_sets else set()

    normalised: dict[uuid.UUID, dict[str, float]] = {rid: {} for rid in per_run_metrics}
    for key in sorted(shared):
        per_run = {
            rid: metrics[key]
            for rid, metrics in per_run_metrics.items()
            if key in metrics
        }
        for run_id, value in normalise_across_runs(per_run, tolerance).items():
            normalised[run_id][key] = value
    return normalised


def scale_quality(metrics: dict[str, AggregatedMetric]) -> dict[str, float]:
    """Select the quality half, already reduced to absolute 0-100 goodness scores."""
    return {k: m.value for k, m in metrics.items() if is_quality_key(k)}


# --- Stage 2: combine into a headline ---------------------------------------------------


def combine_headline(
    metrics: dict[str, float],
    selection: list[str] | None,
    excluded: list[ExcludedMetric] | None = None,
    entry_counts: dict[str, int] | None = None,
) -> HeadlineValue:
    """Stage 2 — equal-weight mean over the selected metric subset.

    Selection resolution:

    * ``None`` (unconfigured) → every key present in ``metrics``.
    * ``[]`` (explicitly emptied) → **not applicable**. Never ``0.0``. ``None`` and
      ``[]`` are not interchangeable — collapsing them makes "select none" silently mean
      "select all".
    * ``[...]`` → intersect with the keys present; selected keys unavailable for this
      family are silently ignored, not treated as zero.
    """
    disclosure = list(excluded or [])

    if selection is None:
        resolved = sorted(metrics)
    elif len(selection) == 0:
        # An explicit empty selection is an author choice to compose the headline from
        # nothing, which reads as not applicable.
        return HeadlineValue(value=None, contributing_keys=[], excluded=disclosure)
    else:
        resolved = sorted(set(selection) & set(metrics))

    if not resolved:
        return HeadlineValue(value=None, contributing_keys=[], excluded=disclosure)

    # Equal weight by design: a weighted blend makes the headline uninterpretable.
    mean = sum(metrics[k] for k in resolved) / len(resolved)
    counts = entry_counts or {}
    return HeadlineValue(
        value=round(mean, 2),
        contributing_keys=resolved,
        excluded=disclosure,
        entry_counts={k: counts[k] for k in resolved if k in counts},
        contributions={k: metrics[k] for k in resolved},
    )


# --- Exclusion reasons ------------------------------------------------------------------


def classify_exclusions(
    selection: list[str] | None,
    available: set[str],
    statuses_by_run: dict[uuid.UUID, dict[str, str]],
    family: str | None = None,
) -> list[ExcludedMetric]:
    """Explain which selected metrics could not contribute, and whether a re-run helps.

    ``statuses_by_run`` maps each run to ``{metric key: package status}``, read from the
    per-result ``execution_details["metric_results"]`` blob, and only that part of it
    (:func:`load_metric_statuses_by_run`). ``evaluation_metrics`` cannot answer this by
    construction — it stores only ``scored`` outcomes, so an excluded metric has no row
    there.

    ``selection`` is the metrics that should have contributed: the author's selection,
    or, with the default performance selection, the metrics the shared-key rule dropped.

    ``family``, when given, is consulted before the blob-absent fallback: no write
    path validates ``overall_quality_metrics`` / ``overall_performance_metrics`` against
    the family, and the family can change after a selection is set, so a selected key
    outside the family's catalogue is reachable. It can never be scored, so it reads
    ``not_applicable``, not ``failed``, which would suggest a re-run.

    Skipped for an unknown family, whose catalogue would hold no quality key and mark
    every missing one ``not_applicable`` even when it failed.
    """
    if not selection:
        # No explicit selection: nothing was asked for and missed.
        return []

    family_keys = (
        {m["key"] for m in metric_catalog(family)}
        if family and family_is_valid(family)
        else None
    )

    excluded: list[ExcludedMetric] = []
    for key in sorted(set(selection) - available):
        offenders = [
            run_id
            for run_id, statuses in statuses_by_run.items()
            if statuses.get(key) != "scored"
        ]
        observed = {
            statuses.get(key)
            for run_id, statuses in statuses_by_run.items()
            if run_id in offenders
        }
        # "Failed" wins a mixed set: if any run could recover the metric by re-running,
        # "not applicable" would tell the author to give up on something reachable.
        if observed & _FAILED_STATUSES:
            reason = REASON_FAILED
        elif observed & _NOT_APPLICABLE_STATUSES:
            reason = REASON_NOT_APPLICABLE
        elif family_keys is not None and key not in family_keys:
            # Absent from the blob AND outside the family's catalogue: no run could
            # ever score this key for this family.
            reason = REASON_NOT_APPLICABLE
        else:
            # Absent from the blob: the metric never reported, which follows an
            # execution error rather than a deliberate skip.
            reason = REASON_FAILED
        excluded.append(
            ExcludedMetric(key=key, reason=reason, run_ids=sorted(offenders, key=str))
        )
    return excluded


async def load_metric_statuses_by_run(
    db: AsyncSession, run_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, dict[str, str]]:
    """Read per-metric package statuses for **several** runs in one round trip.

    The second per-run pass of a headline computation: it runs when a selected key
    turned out to be unavailable, and, with the default performance selection, whenever
    some runs measured a metric others lack. One query whatever the run count,
    reading only each result's ``metric_results``.

    Every requested run gets an entry, so "no statuses recorded" and "not asked for"
    stay distinguishable. Run ids are normalised before grouping, for the same reason
    as :func:`aggregate_metrics_by_run` -- and, as there, the annotation is the
    contract rather than the widest input the guard survives.
    """
    if not run_ids:
        return {}

    requested: dict[uuid.UUID, uuid.UUID] = {}
    for rid in run_ids:
        requested[rid if isinstance(rid, uuid.UUID) else uuid.UUID(str(rid))] = rid

    rows = (
        await db.execute(
            # Only ``metric_results`` leaves the database: the rest of the blob
            # (traces, prompts, raw responses) is by far its largest part.
            select(
                EvaluationResult.run_id,
                EvaluationResult.execution_details["metric_results"],
            ).where(
                EvaluationResult.run_id.in_(run_ids),
                # Same inclusion rule as aggregate_metrics_by_run.
                result_counts_in_headlines(),
            )
        )
    ).all()

    statuses_by_run: dict[uuid.UUID, dict[str, str]] = {
        original: {} for original in requested.values()
    }
    for run_id, results in rows:
        if not isinstance(results, dict):
            continue
        statuses = statuses_by_run.setdefault(requested.get(run_id, run_id), {})
        for key, outcome in results.items():
            status = outcome.get("status") if isinstance(outcome, dict) else None
            if not isinstance(status, str):
                continue
            if statuses.get(str(key)) == "scored":
                continue
            statuses[str(key)] = status
    return statuses_by_run


async def load_metric_statuses(db: AsyncSession, run_id: uuid.UUID) -> dict[str, str]:
    """Read one run's per-metric package statuses from ``execution_details``.

    The only blob read in this module: it answers *why* a metric is missing, which
    ``evaluation_metrics`` cannot — that table stores ``scored`` outcomes only, so an
    excluded metric has no row to explain itself.

    A key seen as ``scored`` on any entry stays ``scored``: the metric did report, and a
    later entry skipping it is an entry-level gap, not the metric being unavailable.

    The single-run form of :func:`load_metric_statuses_by_run`, which it delegates to so
    the two can never diverge.
    """
    return (await load_metric_statuses_by_run(db, [run_id]))[run_id]


def resolve_default_quality_selection(available: set[str]) -> list[str]:
    """Which metrics compose the headline when the author configured no selection.

    The available keys in ``catalog.DEFAULT_QUALITY_KEYS`` (which explains why the others
    are not). Resolved here, not in :func:`combine_headline`, which the performance
    headline shares: family knowledge stays out of shared arithmetic.

    An EMPTY result must be passed through as an empty list: "no default contributor
    scored" reads as *not applicable*. Widening it to ``available`` would put the excluded
    metric back on exactly the runs where it is the only signal.
    """
    from app.services.evaluation.metric_package.catalog import DEFAULT_QUALITY_KEYS

    return sorted(available & DEFAULT_QUALITY_KEYS)


def classify_default_omissions(
    available: set[str], non_default: frozenset[str] | None = None
) -> list[ExcludedMetric]:
    """Explain the metrics the DEFAULT selection left out.

    The counterpart of :func:`classify_exclusions`, which returns nothing without a
    stored selection, exactly when :func:`resolve_default_quality_selection` narrows.
    Without it a headline could read N/A while a metric visibly scored, with nothing
    saying why.

    Candidates are drawn from ``available``, the metrics that PRODUCED a value, which
    keeps this reason off a metric that failed or was inapplicable. The guarantee is the
    intersection, so widening the candidates breaks it (``test_evaluation_aggregation``
    pins both branches).

    Callers MUST invoke this only when no selection is configured. An explicit selection --
    including an explicitly empty one -- is an author choice and keeps its own reporting.

    ``non_default`` is the performance half's list, from
    :func:`compute_performance_headlines`. When omitted, the quality half's
    omissions are the available quality metrics outside ``catalog.DEFAULT_QUALITY_KEYS``;
    a key of the other half is never disclosed here.
    """
    from app.services.evaluation.metric_package.catalog import DEFAULT_QUALITY_KEYS

    if non_default is None:
        omitted = {key for key in available if is_quality_key(key)} - DEFAULT_QUALITY_KEYS
    else:
        omitted = available & non_default
    return [
        ExcludedMetric(key=key, reason=REASON_NOT_A_DEFAULT_CONTRIBUTOR, run_ids=[])
        for key in sorted(omitted)
    ]


def classify_run_level_exclusions(
    per_run_metrics: dict[uuid.UUID, dict[str, AggregatedMetric]],
) -> list[ExcludedMetric]:
    """Disclose the runs the comparison basis set aside, and why.

    Nothing else in the stored headline says that a run was excluded from the basis,
    or that too few remained to rank.

    Not routed through :func:`classify_exclusions`, which returns ``[]`` when no
    selection is configured, the ordinary case.

    Derived from aggregates already in memory: no query, however many runs abstain.
    """
    abstaining: list[uuid.UUID] = []
    eligible: list[uuid.UUID] = []
    for rid, metrics in per_run_metrics.items():
        target = eligible if rankable_performance_keys(metrics) else abstaining
        target.append(rid)

    disclosure: list[ExcludedMetric] = []
    if abstaining:
        disclosure.append(
            ExcludedMetric(
                key=RUN_LEVEL_EXCLUSION_KEY,
                reason=REASON_NO_PERFORMANCE_DATA,
                run_ids=sorted(abstaining, key=str),
            )
        )
    if len(eligible) < 2:
        # Distinct from abstention: an evaluation with no abstainers is still
        # unrankable with only one run.
        disclosure.append(
            ExcludedMetric(
                key=RUN_LEVEL_EXCLUSION_KEY,
                reason=REASON_TOO_FEW_COMPARABLE_RUNS,
                run_ids=sorted(eligible, key=str),
            )
        )
    return disclosure


async def compute_quality_headline(
    db: AsyncSession,
    run_id: uuid.UUID,
    evaluation: Evaluation,
    entry_metric_results: Sequence[object] | None = None,
) -> HeadlineValue:
    """Overall Quality for one run — Stage 1 then Stage 2.

    No cross-run normalisation: quality metrics are absolute 0-100 scores, so a run's
    quality reads the same alone or among twenty, unlike performance.

    Returns a ``HeadlineValue`` whose ``value`` is ``None`` when nothing contributed,
    never ``0.0``; a caller must not substitute a zero for a missing measurement. With
    *entry_metric_results*, the ``metric_results`` of the entries it considers (the
    caller holds them), it also says which entries it covers (``coverage``).
    """
    penalty_factor = evaluation.penalty_factor if evaluation is not None else 0.5
    aggregates = await aggregate_run_metrics(db, run_id, penalty_factor)
    metrics = scale_quality(aggregates)

    selection = evaluation.overall_quality_metrics if evaluation is not None else None
    excluded: list[ExcludedMetric] = []
    if selection and set(selection) - set(metrics):
        statuses = await load_metric_statuses(db, run_id)
        family = evaluation.family if evaluation is not None else None
        excluded = classify_exclusions(
            selection, set(metrics), {run_id: statuses}, family
        )

    # An unconfigured selection resolves to the DEFAULT contributors. The
    # disclosure above keys on the stored value, so no choice is attributed to an author
    # who made none; the default's narrowing gets its own reason below.
    if selection is None:
        effective = resolve_default_quality_selection(set(metrics))
        excluded = classify_default_omissions(set(metrics))
    else:
        effective = selection

    headline = combine_headline(
        metrics,
        effective,
        excluded,
        entry_counts={k: m.entry_count for k, m in aggregates.items()},
    )
    if entry_metric_results is None:
        return headline
    family = evaluation.family if evaluation is not None else None
    covered_keys = headline.contributing_keys or (
        list(selection) if selection else _default_quality_keys(family)
    )
    return replace(headline, coverage=entry_coverage(entry_metric_results, covered_keys))


def _default_quality_keys(family: str | None) -> list[str]:
    """The family's default contributors, for a headline none of them composed."""
    return sorted(m["key"] for m in quality_catalog(family) if m["default_contributor"])


#: The reason of an entry the headline does not cover and whose metrics say nothing.
NOT_MEASURED = "not_measured"


def entry_coverage(
    entry_metric_results: Sequence[object], keys: Sequence[str]
) -> EntryCoverage | None:
    """How many entries the mean considers, how many it covers, and why the others are
    left out.

    *entry_metric_results* holds each considered entry's ``metric_results``. An entry is
    covered when every one of *keys* scored on it. One that is not counts under the reason
    its first unscored metric gives, the reason stored with an ``unavailable`` or
    ``skipped`` outcome, or ``NOT_MEASURED`` when none was stored (an entry whose run
    failed). ``None`` without *keys*: no headline to cover.
    """
    if not keys:
        return None
    counted = 0
    left_out: dict[str, int] = {}
    for results in entry_metric_results:
        outcomes = results if isinstance(results, dict) else {}
        reason = next(
            (_left_out_reason(outcomes.get(key)) for key in keys if not _scored(outcomes.get(key))),
            None,
        )
        if reason is None:
            counted += 1
        else:
            left_out[reason] = left_out.get(reason, 0) + 1
    return EntryCoverage(entries=len(entry_metric_results), counted=counted, left_out=left_out)


def _scored(outcome: object) -> bool:
    return isinstance(outcome, dict) and outcome.get("status") == "scored"


def _left_out_reason(outcome: object) -> str:
    detail = outcome.get("detail") if isinstance(outcome, dict) else None
    reason = detail.get("reason") if isinstance(detail, dict) else None
    return reason if isinstance(reason, str) and reason else NOT_MEASURED


def _with_measured_as_scored(
    statuses_by_run: dict[uuid.UUID, dict[str, str]],
    measuring: dict[uuid.UUID, set[str]],
) -> dict[uuid.UUID, dict[str, str]]:
    """A copy of ``statuses_by_run`` with every metric a run measured read as scored."""
    merged = {run_id: dict(statuses) for run_id, statuses in statuses_by_run.items()}
    for run_id, keys in measuring.items():
        merged.setdefault(run_id, {}).update(dict.fromkeys(keys, "scored"))
    return merged


def _excluded_for_run(
    excluded: list[ExcludedMetric], metrics: dict[str, AggregatedMetric]
) -> list[ExcludedMetric]:
    """The exclusions as one run's record states them.

    The list is the evaluation's; each run's record says what is true for that run:

    * a metric this run measured and other runs lack: ``REASON_MISSING_FROM_OTHER_RUNS``,
      with the lacking runs; those runs keep their shared reason;
    * a metric this run measured that no run lacks (a lone run ranks nothing): left to
      the run-level entry;
    * a run that measured nothing abstains: its run-level entry says so, no metric
      is listed.

    Run-level entries are kept as they are.
    """
    measured = rankable_performance_keys(metrics)
    for_run: list[ExcludedMetric] = []
    for e in excluded:
        if e.key == RUN_LEVEL_EXCLUSION_KEY:
            for_run.append(e)
        elif not measured:
            continue
        elif e.key not in measured:
            for_run.append(e)
        elif e.run_ids:
            for_run.append(
                ExcludedMetric(
                    key=e.key, reason=REASON_MISSING_FROM_OTHER_RUNS, run_ids=e.run_ids
                )
            )
    return for_run


async def compute_performance_headlines(
    db: AsyncSession,
    evaluation: Evaluation,
    run_ids: list[uuid.UUID],
) -> dict[uuid.UUID, HeadlineValue]:
    """Overall Performance for **every** run in one comparison basis.

    Raw units (seconds, tokens) only mean something relative to the evaluation's other
    runs, so the basis is the whole evaluation and every run is rescored together.
    Scoping it to a trigger batch or a single run would score a run against whichever
    siblings ran alongside it, and give a lone run the top of the scale.

    Runs absent from the returned mapping, and entries whose ``value`` is ``None``, both
    mean *not applicable* — never zero.
    """
    from app.services.evaluation.metric_package.catalog import NON_DEFAULT_PERFORMANCE_KEYS

    if not run_ids:
        return {}

    penalty_factor = evaluation.penalty_factor if evaluation is not None else 0.5
    tolerance = evaluation.performance_tolerance if evaluation is not None else 5.0
    selection = (
        evaluation.overall_performance_metrics if evaluation is not None else None
    )

    aggregates_by_run = await aggregate_metrics_by_run(db, run_ids, penalty_factor)
    normalised = normalise_performance(aggregates_by_run, tolerance)

    # The usable set is what normalisation produced, so the disclosure names what left
    # the EVALUATION's headline: a key dropped by the shared-key rule is dropped
    # everywhere.
    usable: set[str] = set()
    for per_run in normalised.values():
        usable |= set(per_run)

    # What each run measured. It is what kept a metric in that run's aggregates, so
    # it counts as scored whatever the run's results' blob says (an older result may
    # carry none): the blob then explains only the runs that lack a metric.
    measuring = {
        run_id: keys
        for run_id, metrics in aggregates_by_run.items()
        if (keys := rankable_performance_keys(metrics))
    }

    excluded: list[ExcludedMetric] = []
    if selection and set(selection) - usable:
        # Over the runs that measured anything, as below: a run that abstains is
        # disclosed once, at run level, never as lacking a selected metric.
        statuses_by_run = _with_measured_as_scored(
            await load_metric_statuses_by_run(db, list(measuring)), measuring
        )
        family = evaluation.family if evaluation is not None else None
        excluded = classify_exclusions(selection, usable, statuses_by_run, family)
    elif selection is None:
        # No selection: the headline uses the metrics all runs share, so one some
        # runs lack leaves every headline. Recorded as an explicit selection's is,
        # over the runs that measured anything. Only what the shared-key rule
        # dropped: a lone run's unranked metrics are covered by the run-level entry,
        # and a metric the default leaves out anyway gets that reason per run, below.
        dropped = {
            key
            for key in set().union(*measuring.values())
            if any(key not in keys for keys in measuring.values())
        } - NON_DEFAULT_PERFORMANCE_KEYS
        if dropped:
            statuses_by_run = _with_measured_as_scored(
                await load_metric_statuses_by_run(db, list(measuring)), measuring
            )
            family = evaluation.family if evaluation is not None else None
            excluded = classify_exclusions(
                sorted(dropped), usable, statuses_by_run, family
            )

    # Appended AFTER the branch above, which REASSIGNS `excluded`: entries added first
    # would be lost when a selected key is unavailable.
    excluded = excluded + classify_run_level_exclusions(aggregates_by_run)
    # `[]` only: an explicit "select none". `None` is the default selection, handled
    # per run below; the two must never be confused (see `combine_headline`).
    if selection is not None and not selection:
        # Concerns every run alike, so it names none.
        excluded.append(
            ExcludedMetric(key=RUN_LEVEL_EXCLUSION_KEY, reason=REASON_NO_METRIC_SELECTED)
        )

    headlines: dict[uuid.UUID, HeadlineValue] = {}
    for run_id in run_ids:
        run_metrics = normalised.get(run_id, {})
        run_excluded = _excluded_for_run(excluded, aggregates_by_run.get(run_id, {}))
        effective = selection
        if selection is None:
            # An unconfigured selection counts the default contributors only, as
            # on the quality side (:func:`resolve_default_quality_selection`): an empty
            # result reads as not applicable, never as "everything". What the default left
            # out of a run is disclosed on that run, from what it measured.
            effective = sorted(set(run_metrics) - NON_DEFAULT_PERFORMANCE_KEYS)
            run_excluded = (
                classify_default_omissions(
                    measuring.get(run_id, set()), NON_DEFAULT_PERFORMANCE_KEYS
                )
                + run_excluded
            )
            if run_metrics and not effective:
                run_excluded.append(
                    ExcludedMetric(
                        key=RUN_LEVEL_EXCLUSION_KEY,
                        reason=REASON_ONLY_OPT_IN_METRICS_SHARED,
                        run_ids=[run_id],
                    )
                )
        headlines[run_id] = combine_headline(
            run_metrics,
            effective,
            run_excluded,
            entry_counts={
                k: m.entry_count for k, m in aggregates_by_run.get(run_id, {}).items()
            },
        )
    return headlines


__all__ = [
    "REASON_FAILED",
    "REASON_MISSING_FROM_OTHER_RUNS",
    "REASON_NOT_APPLICABLE",
    "REASON_NOT_A_DEFAULT_CONTRIBUTOR",
    "REASON_NO_METRIC_SELECTED",
    "REASON_NO_PERFORMANCE_DATA",
    "REASON_ONLY_OPT_IN_METRICS_SHARED",
    "REASON_TOO_FEW_COMPARABLE_RUNS",
    "RUN_LEVEL_EXCLUSION_KEY",
    "AggregatedMetric",
    "ExcludedMetric",
    "HeadlineValue",
    "aggregate_metrics_by_run",
    "aggregate_run_metrics",
    "classify_default_omissions",
    "classify_exclusions",
    "classify_run_level_exclusions",
    "combine_headline",
    "compute_performance_headlines",
    "compute_quality_headline",
    "counts_in_headlines",
    "load_metric_statuses",
    "load_metric_statuses_by_run",
    "normalise_across_runs",
    "normalise_performance",
    "reduce_grouped_metrics",
    "resolve_default_quality_selection",
    "result_counts_in_headlines",
    "scale_quality",
]
