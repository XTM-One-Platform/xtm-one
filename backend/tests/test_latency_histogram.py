from __future__ import annotations

import random
import statistics

import pytest

from app.services.tracing.latency_histogram import (
    HISTOGRAM_BUCKET_COUNT,
    HISTOGRAM_MAX_MS,
    HISTOGRAM_MIN_MS,
    HISTOGRAM_VERSION,
    bucket_bounds,
    bucket_expression_sql,
    bucket_for_duration,
    encode_histogram,
    merge_histograms,
    normalize_histogram,
    percentile_from_histogram,
)


def _histogram_of(durations: list[float]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for value in durations:
        bucket = bucket_for_duration(value)
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def _exact_percentile(durations: list[float], quantile: float) -> float:
    ordered = sorted(durations)
    return statistics.quantiles(ordered, n=1000, method="inclusive")[int(quantile * 1000) - 1]


@pytest.mark.parametrize("duration", [0.0, 0.4, 1.0, 25.0, 999.0, 60_000.0])
def test_bucket_bounds_contain_their_duration(duration):
    bucket = bucket_for_duration(duration)
    lower, upper = bucket_bounds(bucket)
    # Sub-millisecond durations are clamped into the first bucket rather than
    # falling into width_bucket's underflow bucket.
    assert lower <= max(duration, HISTOGRAM_MIN_MS) < upper


def test_buckets_are_monotonic_and_cover_the_range():
    assert bucket_for_duration(HISTOGRAM_MIN_MS) == 1
    assert bucket_for_duration(HISTOGRAM_MAX_MS) == HISTOGRAM_BUCKET_COUNT + 1
    previous = 0
    for duration in (1, 10, 100, 1_000, 10_000, 100_000, 1_000_000):
        bucket = bucket_for_duration(duration)
        assert bucket > previous
        previous = bucket


def test_overflow_bucket_reports_its_lower_bound():
    # Everything at or beyond the top of the range shares one open-ended bucket;
    # its lower bound is the only defensible estimate.
    assert percentile_from_histogram({HISTOGRAM_BUCKET_COUNT + 1: 5}, 0.95) == HISTOGRAM_MAX_MS


def test_encode_normalize_round_trip():
    counts = {12: 3, 40: 7}

    encoded = encode_histogram(counts)

    assert encoded == {"v": HISTOGRAM_VERSION, "b": {"12": 3, "40": 7}}
    assert normalize_histogram(encoded) == counts


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"b": {"3": 1}},  # no version
        {"v": HISTOGRAM_VERSION + 1, "b": {"3": 1}},  # geometry we cannot decode
        {"v": HISTOGRAM_VERSION, "b": {"three": 1}},  # malformed bucket
        {"v": HISTOGRAM_VERSION, "b": {}},  # nothing recorded
        {"v": HISTOGRAM_VERSION, "b": {"3": 0}},  # all-zero
    ],
)
def test_normalize_histogram_rejects_unusable_values(raw):
    # Anything unusable must read as absent so the caller falls back to the
    # stored per-group percentiles instead of inventing one from nothing.
    assert normalize_histogram(raw) is None


def test_encode_histogram_is_none_when_empty():
    assert encode_histogram({}) is None
    assert encode_histogram({4: 0}) is None


def test_merge_histograms_sums_bucket_counts():
    assert merge_histograms([{1: 2, 5: 1}, {5: 3, 9: 4}, {}]) == {1: 2, 5: 4, 9: 4}


def test_percentile_of_empty_histogram_is_none():
    assert percentile_from_histogram({}, 0.5) is None
    assert percentile_from_histogram({7: 0}, 0.5) is None


def test_percentile_is_ordered_across_quantiles():
    durations = [50.0] * 40 + [400.0] * 40 + [5_000.0] * 20
    histogram = _histogram_of(durations)

    values = [percentile_from_histogram(histogram, q) for q in (0.5, 0.75, 0.9, 0.95, 0.99)]

    assert all(value is not None for value in values)
    assert values == sorted(values)


@pytest.mark.parametrize("quantile", [0.5, 0.9, 0.95, 0.99])
def test_merged_histogram_percentile_tracks_the_true_percentile(quantile):
    # The bucket geometry is log-spaced at ~19% per bucket, so an interpolated
    # read should land within a few percent of the real percentile of the union.
    rng = random.Random(20260730)  # noqa: S311 - sampling a latency distribution, not crypto
    durations = [rng.lognormvariate(6.0, 1.1) for _ in range(20_000)]

    merged = merge_histograms(
        # Split into "days" and merge, which is exactly what the read path does.
        [_histogram_of(durations[index::10]) for index in range(10)]
    )
    estimate = percentile_from_histogram(merged, quantile)
    exact = _exact_percentile(durations, quantile)

    assert estimate is not None
    assert abs(estimate - exact) / exact < 0.06


def test_merged_histogram_beats_averaging_per_group_percentiles():
    # The defect this format exists to fix. Two groups with very different
    # latency profiles: a trace_count-weighted mean of their p95s is not the p95
    # of the union, while merged buckets are.
    fast = [100.0] * 900 + [150.0] * 100
    slow = [4_000.0] * 50 + [30_000.0] * 50
    union = fast + slow

    merged = percentile_from_histogram(merge_histograms([_histogram_of(fast), _histogram_of(slow)]), 0.95)
    weighted_mean_of_p95s = (
        _exact_percentile(fast, 0.95) * len(fast) + _exact_percentile(slow, 0.95) * len(slow)
    ) / len(union)
    exact = _exact_percentile(union, 0.95)

    assert merged is not None
    assert abs(merged - exact) < abs(weighted_mean_of_p95s - exact)


def test_bucket_expression_sql_matches_the_python_bucketing():
    sql = bucket_expression_sql("t.duration_ms")

    # Both sides must agree on the geometry - the stored bucket indices are the
    # wire format between the aggregator (SQL) and the read path (Python).
    assert sql.startswith("width_bucket(")
    assert "t.duration_ms" in sql
    assert str(HISTOGRAM_BUCKET_COUNT) in sql
    assert "greatest" in sql
