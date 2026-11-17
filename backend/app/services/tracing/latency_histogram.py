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

"""Mergeable latency histograms for the ``llm_daily_stats`` rollup.

Why this exists
---------------
``llm_daily_stats`` stores one row per ``(date, agent, model, source, user)``
group. A percentile stored per group cannot be recombined into the percentile of
the union: the old read path averaged the per-group ``p50``/``p95`` weighted by
``trace_count``, and a weighted mean of percentiles is not a percentile - so
latency KPIs shifted, sometimes materially, the moment a window crossed the
7-day boundary onto the rollup.

A histogram *is* mergeable: bucket counts add. Summing the histograms of every
row in a window and reading the quantile off the merged counts yields the
percentile of the union, with an error bounded by the bucket width instead of an
unbounded error from averaging percentiles.

Bucket scheme
-------------
Log-spaced buckets over ``[HISTOGRAM_MIN_MS, HISTOGRAM_MAX_MS)``, which is what
latency distributions want: constant *relative* resolution across the four
orders of magnitude an LLM call spans (sub-10 ms cache hit to a multi-minute
agentic turn). Bucket ``k`` (1-based) covers durations
``[MIN * r**(k-1), MIN * r**k)`` with ``r = exp(LN_SPAN / HISTOGRAM_BUCKET_COUNT)``
~= 1.19, i.e. each bucket is ~19% wide; geometric interpolation inside the
bucket brings the realistic error well under that.

Bucket indices come from PostgreSQL's ``width_bucket`` over ``ln(duration_ms)``
(see :func:`bucket_expression_sql`) so the aggregator computes them in the same
scan that builds the rest of the row, and this module decodes them on the read
side. Both sides MUST agree on the constants below - they are the wire format of
the stored ``duration_histogram`` JSON, so changing them invalidates already
written rollups (see :data:`HISTOGRAM_VERSION`).

``width_bucket`` returns ``0`` below the low bound and ``count + 1`` above the
high bound; with ``greatest(duration_ms, MIN)`` clamping the input, only the
``count + 1`` overflow bucket is reachable, and it is read as the open-ended
``[HISTOGRAM_MAX_MS, inf)`` tail.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

# Bump when the bucket geometry changes: already-stored histograms were written
# with the previous geometry and must not be decoded with a new one. Readers
# treat a histogram of an unknown version as absent (falling back to the stored
# per-group percentiles) rather than silently misreading it.
HISTOGRAM_VERSION = 1

# 1 ms .. 10_000_000 ms (~2.8 h) spans every realistic LLM call duration; 93
# buckets over that span give r ~= 1.19 (~19% per bucket).
HISTOGRAM_MIN_MS = 1.0
HISTOGRAM_MAX_MS = 10_000_000.0
HISTOGRAM_BUCKET_COUNT = 93

_LN_MIN = math.log(HISTOGRAM_MIN_MS)
_LN_MAX = math.log(HISTOGRAM_MAX_MS)
_LN_SPAN = _LN_MAX - _LN_MIN
_BUCKET_LN_WIDTH = _LN_SPAN / HISTOGRAM_BUCKET_COUNT

# The reachable overflow bucket: everything at or above HISTOGRAM_MAX_MS.
_OVERFLOW_BUCKET = HISTOGRAM_BUCKET_COUNT + 1


def bucket_expression_sql(duration_column: str) -> str:
    """SQL for the bucket index of *duration_column*, matching this module.

    ``greatest`` clamps sub-millisecond and zero durations into the first
    bucket so the unreachable ``width_bucket`` underflow bucket (``0``) never
    appears in stored data. The bounds are inlined as literals rather than bound
    parameters so the expression can be reused inside a ``GROUP BY`` without the
    caller having to thread extra binds through every call site; both values are
    module constants formatted with ``repr``, never user input.
    """
    return (
        "width_bucket("
        f"ln(greatest({duration_column}, {HISTOGRAM_MIN_MS!r})::double precision), "
        f"{_LN_MIN!r}, {_LN_MAX!r}, {HISTOGRAM_BUCKET_COUNT}"
        ")"
    )


def bucket_bounds(bucket: int) -> tuple[float, float]:
    """Inclusive-lower / exclusive-upper duration bounds of *bucket* in ms.

    The overflow bucket is open-ended, so it is reported as a degenerate
    ``(HISTOGRAM_MAX_MS, HISTOGRAM_MAX_MS)`` range: interpolation inside it
    collapses to its lower bound, which is the only defensible estimate without
    storing the tail's actual values.
    """
    if bucket >= _OVERFLOW_BUCKET:
        return HISTOGRAM_MAX_MS, HISTOGRAM_MAX_MS
    index = max(1, bucket)
    lower = math.exp(_LN_MIN + (index - 1) * _BUCKET_LN_WIDTH)
    upper = math.exp(_LN_MIN + index * _BUCKET_LN_WIDTH)
    return lower, upper


def bucket_for_duration(duration_ms: float) -> int:
    """Bucket index for *duration_ms* - the Python twin of the SQL expression.

    Only used by tests and by any Python-side bucketing; the aggregator buckets
    in SQL so it never materializes one row per trace.
    """
    clamped = max(float(duration_ms), HISTOGRAM_MIN_MS)
    if clamped >= HISTOGRAM_MAX_MS:
        return _OVERFLOW_BUCKET
    offset = (math.log(clamped) - _LN_MIN) / _BUCKET_LN_WIDTH
    return min(HISTOGRAM_BUCKET_COUNT, int(offset) + 1)


def normalize_histogram(raw: Any) -> dict[int, int] | None:
    """Coerce a stored ``duration_histogram`` value into ``{bucket: count}``.

    Stored shape is ``{"v": HISTOGRAM_VERSION, "b": {"<bucket>": count}}``.
    Returns ``None`` for anything unusable - absent, wrong version, malformed, or
    all-zero - so callers fall back to the per-group percentile columns instead
    of reporting a percentile derived from nothing.
    """
    if not isinstance(raw, Mapping):
        return None
    if raw.get("v") != HISTOGRAM_VERSION:
        return None
    buckets = raw.get("b")
    if not isinstance(buckets, Mapping):
        return None

    counts: dict[int, int] = {}
    for key, value in buckets.items():
        try:
            bucket = int(key)
            count = int(value)
        except (TypeError, ValueError):
            return None
        if count <= 0:
            continue
        counts[bucket] = counts.get(bucket, 0) + count
    return counts or None


def encode_histogram(counts: Mapping[int, int]) -> dict[str, Any] | None:
    """Build the stored JSON shape from ``{bucket: count}`` (``None`` if empty)."""
    buckets = {str(bucket): int(count) for bucket, count in counts.items() if count > 0}
    if not buckets:
        return None
    return {"v": HISTOGRAM_VERSION, "b": buckets}


def merge_histograms(histograms: Iterable[Mapping[int, int]]) -> dict[int, int]:
    """Sum bucket counts across histograms - the whole point of the format."""
    merged: dict[int, int] = {}
    for histogram in histograms:
        for bucket, count in histogram.items():
            merged[bucket] = merged.get(bucket, 0) + int(count)
    return merged


def percentile_from_histogram(counts: Mapping[int, int], quantile: float) -> float | None:
    """Interpolated *quantile* (0..1) of the distribution described by *counts*.

    Walks buckets in ascending order to the one containing rank
    ``quantile * total``, then interpolates geometrically inside it (the buckets
    are log-spaced, so a geometric position is the natural one). Returns ``None``
    for an empty histogram.
    """
    total = sum(int(count) for count in counts.values() if count > 0)
    if total <= 0:
        return None

    target = quantile * total
    cumulative = 0.0
    for bucket in sorted(counts):
        count = int(counts[bucket])
        if count <= 0:
            continue
        if cumulative + count >= target:
            lower, upper = bucket_bounds(bucket)
            if upper <= lower:
                return lower
            fraction = (target - cumulative) / count
            fraction = min(1.0, max(0.0, fraction))
            return lower * (upper / lower) ** fraction
        cumulative += count

    # Only reachable through float rounding at quantile ~= 1.0.
    highest = max(counts)
    return bucket_bounds(highest)[1]
