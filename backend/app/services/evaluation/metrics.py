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

"""Per-result metric extraction and server-side aggregates.

Each evaluation result logs a flat set of numeric metrics derived from its
``execution_details``, indexed by result and run so the API can compute per-run
aggregates (mean/percentile/min/max).

Execution-level performance measurements (TTFT, agent seconds, tokens, iterations,
tool calls, cost) are **not** logged here: the metric package writes each under a
``performance.*`` key, and a second copy let an unpriced model store a fabricated
``0`` cost beside the package's ``unavailable``. Older rows carrying those
keys are kept.

This module writes only what the package cannot: ``judge_seconds`` and
``run_duration_seconds``, since the adapter drops judge steps as evaluation overhead.

A reused prediction did not re-execute the agent, so Run Duration counts only the
judge. Its ``performance.*`` metrics are kept: they measure the execution
being reused, and dropping them would remove the run from the performance comparison.
"""

from __future__ import annotations

import logging
import math
import statistics
import uuid
from collections import defaultdict

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.evaluation_metric import EvaluationMetric
from app.services.evaluation.metric_package import (
    MACRO_CLASSES,
    MACRO_COUNT_KEYS,
    POOLED_DENOMINATOR,
    POOLED_NUMERATOR,
    is_macro_key,
    is_pooled_key,
    is_quality_key,
    macro_f1_from_counts,
)

logger = logging.getLogger(__name__)

# The work this run did: agent plus judge on a fresh execution, judge alone
# on a reused or rescored one, whose agent time belongs to another run. Rows carrying
# the older ``total_seconds`` key are left alone.
RUN_DURATION_KEY = "run_duration_seconds"
JUDGE_SECONDS_KEY = "judge_seconds"

def is_platform_metric_key(key: str) -> bool:
    """Whether *key* belongs to this module rather than to the metric package.

    A package may not write the platform's keys: ``metric_results`` can come
    from an imported bundle, and an uploaded key would overwrite a platform measurement
    and show a package number as Judge time or Run Duration.

    A rule, not a list of names: a list misses the retired keys (``total_seconds``,
    ``ttft_seconds``, ``cost_usd``, ...), which the frontend still labels for old runs
    and which a package value could borrow. The package namespaces every key it emits
    (``json.``, ``stix.``, ``performance.``, ...), so an unprefixed key is the
    platform's. ``test_every_package_metric_key_is_namespaced`` pins that convention.
    """
    return "." not in key

# Read off the column so widening it cannot leave a stale number here. ``None`` when
# the type has no limit (a ``Text`` column): no length to enforce.
_MAX_METRIC_KEY_LENGTH = EvaluationMetric.__table__.c.key.type.length


def _is_storable_key(key: str) -> bool:
    """Whether *key* is worth writing and fits this table.

    A key is taken verbatim from the package's ``metric_results``, which an upload can
    supply. ``EvaluationMetric.key`` is a ``VARCHAR``, so the engine's JSON serializer
    (NUL stripping) never sees it. PostgreSQL refuses a key longer than the column
    (``StringDataRightTruncation``) and a key holding a NUL byte.

    Refused per key because the write shares ONE savepoint per run: a rejected row
    would cost every other result of the run its metrics. A savepoint per result is
    not the answer: a backend caches only 64 subtransactions before others start
    reading ``pg_subtrans``. The run savepoint stays as the backstop for an unknown
    third failure.
    """
    if not key:
        # A VARCHAR holds "", but a metric with no name is unreadable downstream.
        return False
    if _MAX_METRIC_KEY_LENGTH is not None and len(key) > _MAX_METRIC_KEY_LENGTH:
        return False
    return "\x00" not in key



def _as_finite_number(raw: object) -> float | None:
    """*raw* as a float when it is genuinely a finite number, otherwise nothing.

    Three JSON values that are not measurements, reachable from an imported package:

    * ``bool`` -- an ``int``, so a stray True would read as 1.0 measured;
    * ``NaN``/``Infinity`` -- ``json.loads`` accepts both, and one spreads through
      the key's mean, max and percentiles for the whole run;
    * an integer too large for a float -- it raises ``OverflowError`` on conversion.

    Dropped rather than raised: a bad package costs that result its metrics, never
    the whole import.
    """
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    try:
        number = float(raw)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def extract_metrics(
    execution_details: dict | None, *, is_reused: bool = False
) -> dict[str, float]:
    """Extract per-result scalar metrics from ``execution_details``.

    Returns the platform's own measurements (``judge_seconds``,
    ``run_duration_seconds``) and the package's ``scored`` metrics from
    ``metric_results``, values unchanged. The blob can come from an uploaded package,
    so a value that is not a finite number is dropped and a key restating a
    platform key is refused.

    ``is_reused`` sets what Run Duration measures: agent and judge work on a fresh
    execution, judge work alone on a reused or rescored one.
    """
    # Shape-tolerant: an uploaded package can hold anything, and a malformed
    # one must cost that result its metric rows, never the whole import.
    details = execution_details if isinstance(execution_details, dict) else {}
    judge_ms = _as_finite_number(details.get("judge_duration_ms"))
    total_ms = _as_finite_number(details.get("total_duration_ms"))

    metrics: dict[str, float] = {}
    if judge_ms is not None:
        metrics[JUDGE_SECONDS_KEY] = round(judge_ms / 1000, 3)

    # Omitted rather than zeroed when the source measurement is absent: "no
    # measurable work" is not "it took 0 seconds".
    run_duration_ms = judge_ms if is_reused else total_ms
    if run_duration_ms is not None:
        metrics[RUN_DURATION_KEY] = round(run_duration_ms / 1000, 3)

    # Package metrics. Only ``scored`` outcomes carry a value;
    # ``invalid``/``skipped``/``unavailable`` are excluded. Package keys are
    # namespaced by convention, which an uploaded package need not follow, so
    # an unprefixed key is refused. Aggregation is per key, so differing directions
    # never blend.
    package_results = details.get("metric_results")
    if not isinstance(package_results, dict):
        package_results = {}
    for key, blob in package_results.items():
        if not isinstance(blob, dict):
            continue
        # A package may not restate what this module measured.
        if is_platform_metric_key(str(key)):
            continue
        if blob.get("status") != "scored":
            continue
        # Same gate as the durations, so neither path admits a non-measurement.
        number = _as_finite_number(blob.get("value"))
        if number is None:
            continue
        metrics[str(key)] = number

    return metrics


def _is_count(value: object) -> bool:
    """A non-negative finite number: a count, or a weighted one."""
    number = _as_finite_number(value)
    return number is not None and number >= 0


def _scored_outcomes(execution_details: dict | None) -> dict[str, dict]:
    """The scored package outcomes of ``metric_results``, by key (never a platform key)."""
    details = execution_details if isinstance(execution_details, dict) else {}
    package_results = details.get("metric_results")
    if not isinstance(package_results, dict):
        return {}
    return {
        str(key): blob
        for key, blob in package_results.items()
        if isinstance(blob, dict)
        and blob.get("status") == "scored"
        and not is_platform_metric_key(str(key))
    }


def _refuse(key: str, why: str) -> None:
    """Say why a result's counts were refused: its run then has no value for *key*,
    which would otherwise read as a run recorded without counts."""
    logger.warning("Refusing the counts reported for metric %s: %s", key[:100], why)


def extract_pooled_counts(execution_details: dict | None) -> dict[str, tuple[float, float]]:
    """Each scored pooled metric's counts, as its result reports them.

    A pooled metric's value is the ratio of two counts its result reports in ``detail``
    (``POOLED_NUMERATOR`` / ``POOLED_DENOMINATOR``); the run adds them up rather than
    averaging the entries' values. A count is a non-negative real: a weighted share is not
    a whole number. Taken only for a metric the registry declares pooled, and only
    when well-formed -- a numerator no larger than a positive denominator (or 0 of 0, a
    quality score's empty answer) -- AND consistent with the value stored beside them: this
    blob can arrive from an uploaded package without the package's own check, and
    counts that disagree with the value would make the run's figure contradict its entries'.
    Such an entry stores no counts, so the run has no value for the metric, and a warning
    says so; a result that reports no counts at all (one scored before them) is silent.
    """
    counts: dict[str, tuple[float, float]] = {}
    for key, blob in _scored_outcomes(execution_details).items():
        if not is_pooled_key(key):
            continue
        detail = blob.get("detail")
        if not isinstance(detail, dict):
            continue
        numerator, denominator = detail.get(POOLED_NUMERATOR), detail.get(POOLED_DENOMINATOR)
        if numerator is None and denominator is None:
            continue
        if not (_is_count(numerator) and _is_count(denominator)):
            _refuse(key, "not two non-negative numbers")
            continue
        numerator, denominator = float(numerator), float(denominator)
        quality = is_quality_key(key)
        # 0 of 0 is a quality score's empty answer, correct.
        empty = quality and numerator == denominator == 0
        if not (empty or denominator > 0) or numerator > denominator:
            _refuse(key, "the numerator exceeds the denominator, or the denominator is 0")
            continue
        value = _as_finite_number(blob.get("value"))
        scale = 100.0 if quality else 1.0  # a quality value is a 0-100 score
        expected = scale if empty else scale * (numerator / denominator)
        if value is None or abs(value - expected) > 1e-9 * scale:
            _refuse(key, "their ratio is not the value stored beside them")
            continue
        counts[key] = (numerator, denominator)
    return counts


def extract_class_counts(
    execution_details: dict | None,
) -> dict[str, dict[str, dict[str, int]]]:
    """Each scored macro metric's per-class counts, as its result reports them.

    A macro metric's run value is the mean F1 of its classes over the run, so each entry
    reports its classes' counts under ``MACRO_CLASSES``. Taken only for a metric the
    registry declares macro, only when every class holds non-negative integer counts, and,
    as pooled counts are, only when they agree with the value stored beside them: the mean
    F1 of the classes (``macro_f1_from_counts``, the package's formula). An empty mapping
    is taken whatever the value: it adds no class to the run (nothing on either side, or an
    answer the metric could not read where nothing was expected). Refused counts are
    warned about, as pooled ones are.
    """
    counts: dict[str, dict[str, dict[str, int]]] = {}
    for key, blob in _scored_outcomes(execution_details).items():
        if not is_macro_key(key):
            continue
        detail = blob.get("detail")
        classes = detail.get(MACRO_CLASSES) if isinstance(detail, dict) else None
        if classes is None:
            continue
        value = _as_finite_number(blob.get("value"))
        if value is None or not isinstance(classes, dict):
            _refuse(key, "no value, or classes that are not a mapping")
            continue
        parsed = {
            str(name): {name_: held.get(name_) for name_ in MACRO_COUNT_KEYS}
            for name, held in classes.items()
            if isinstance(held, dict)
        }
        if len(parsed) != len(classes) or not all(
            isinstance(n, int) and not isinstance(n, bool) and n >= 0
            for held in parsed.values()
            for n in held.values()
        ):
            _refuse(key, "a class without non-negative integer tp, fp and fn")
            continue
        if parsed and abs(value - 100.0 * macro_f1_from_counts(parsed.values())) > 1e-7:
            _refuse(key, "the classes' mean F1 is not the value stored beside them")
            continue
        counts[key] = parsed
    return counts


async def _upsert_metric(
    db: AsyncSession,
    result_id: uuid.UUID,
    run_id: uuid.UUID,
    key: str,
    value: float | None,
    counts: tuple[float, float] | None = None,
    class_counts: dict[str, dict[str, int]] | None = None,
) -> None:
    """Insert or update one metric honouring the ``(result_id, key)`` uniqueness."""
    existing = (
        await db.execute(
            select(EvaluationMetric).where(
                EvaluationMetric.result_id == result_id,
                EvaluationMetric.key == key,
            )
        )
    ).scalar_one_or_none()
    numerator, denominator = counts if counts is not None else (None, None)
    if existing is None:
        db.add(
            EvaluationMetric(
                result_id=result_id,
                run_id=run_id,
                key=key,
                value=value,
                numerator=numerator,
                denominator=denominator,
                class_counts=class_counts,
            )
        )
    else:
        existing.value = value
        existing.numerator = numerator
        existing.denominator = denominator
        existing.class_counts = class_counts


async def log_judge_seconds(
    db: AsyncSession, *, result_id: uuid.UUID, run_id: uuid.UUID, judge_duration_ms: int
) -> None:
    """Upsert one result's Judge Time alone, for a judge-only replay: the agent did not
    re-run, so its other measurements stand. No commit."""
    await _upsert_metric(
        db, result_id, run_id, JUDGE_SECONDS_KEY, round(judge_duration_ms / 1000, 3)
    )


async def log_result_metrics(
    db: AsyncSession,
    *,
    result_id: uuid.UUID,
    run_id: uuid.UUID,
    execution_details: dict | None,
    is_reused: bool = False,
    replace: bool = False,
) -> None:
    """Extract + upsert all metrics for one result. No commit.

    ``replace`` (a rescore) also deletes the result's quality metric rows that are no
    longer scored, or a metric the rescore leaves out would keep the previous pass's
    value in the run's figures. The rest is kept: a rescore does not re-run the
    agent, and the score on display may still come from the pass the timings measured.
    """
    # ``performance.*`` metrics are kept on a reused result: they describe
    # the execution being reused, and without them the shared-key intersection drops
    # every key and the run's headline reads N/A. Provenance is disclosed on the run.
    metrics = extract_metrics(execution_details, is_reused=is_reused)
    counts = extract_pooled_counts(execution_details)
    class_counts = extract_class_counts(execution_details)
    for key, value in metrics.items():
        if not _is_storable_key(key):
            # The key itself, truncated, not its length: a NUL byte is the other reason.
            logger.warning(
                "Result %s: dropping an unstorable metric key %r",
                result_id,
                key[:60],
            )
            continue
        await _upsert_metric(
            db, result_id, run_id, key, value, counts.get(key), class_counts.get(key)
        )
    if replace:
        # Quality keys only: a package key is namespaced (an unprefixed one is the
        # platform's, ``is_platform_metric_key``), and a rescore keeps ``performance.*``.
        await db.execute(
            delete(EvaluationMetric).where(
                EvaluationMetric.result_id == result_id,
                EvaluationMetric.key.contains("."),
                EvaluationMetric.key.not_like("performance.%"),
                EvaluationMetric.key.not_in(list(metrics)),
            )
        )


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    """Linear-interpolated percentile over an already-sorted list."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (p / 100)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    if lo == hi:
        return sorted_vals[lo]
    return round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo), 6)


def aggregate_metrics(rows: list[tuple[str, float | None]]) -> dict[str, dict]:
    """Aggregate ``[(key, value)]`` into per-key stats.

    Returns ``{key: {mean, min, max, p50, p95, count}}``; NULL values are
    skipped. An empty input yields an empty dict.
    """
    buckets: dict[str, list[float]] = defaultdict(list)
    for key, value in rows:
        if value is not None:
            buckets[key].append(float(value))
    out: dict[str, dict] = {}
    for key, vals in buckets.items():
        s = sorted(vals)
        out[key] = {
            "mean": round(statistics.fmean(s), 6),
            "min": s[0],
            "max": s[-1],
            "p50": _percentile(s, 50),
            "p95": _percentile(s, 95),
            "count": len(s),
        }
    return out


async def get_run_aggregates(db: AsyncSession, run_id: uuid.UUID) -> dict[str, dict]:
    """Compute per-key metric aggregates for one run."""
    result = await db.execute(
        select(EvaluationMetric.key, EvaluationMetric.value).where(
            EvaluationMetric.run_id == run_id
        )
    )
    return aggregate_metrics([(k, v) for k, v in result.all()])
