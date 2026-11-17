"""The fleet About page's 30-day run figures (#10123): ``fleet_views.run_stats``.

Pure counting: the route reads the rows once and the KPI row shows them; an
interruption by the platform is neither a success nor a failure, a row outside
the window is ignored, and the sparkline has one bucket per day.
"""

from datetime import UTC, datetime, timedelta

from app.services.fleet_views import (
    LAST_SUMMARY_MAX_CHARS,
    RUN_STATS_DAYS,
    FleetRunStats,
    one_line_summary,
    run_stats,
)

NOW = datetime(2026, 11, 8, 18, 0, tzinfo=UTC)


def test_empty_window_has_one_zero_bucket_per_day():
    stats = run_stats((), awaiting_approval=0, now=NOW)
    zeros = (0,) * RUN_STATS_DAYS
    assert stats == FleetRunStats(by_day=zeros, succeeded_by_day=zeros, failed_by_day=zeros)


def test_counts_success_failure_and_skips_interruptions():
    rows = [
        (NOW - timedelta(hours=1), "completed", None),
        (NOW - timedelta(hours=2), "failed", "Tool crashed"),
        (NOW - timedelta(hours=3), "failed", "Run marked as failed by stale-assignment-run cleanup."),
        (NOW - timedelta(days=1), "running", None),
    ]
    stats = run_stats(rows, awaiting_approval=2, now=NOW)
    assert (stats.runs, stats.succeeded, stats.failed, stats.awaiting_approval) == (4, 1, 1, 2)
    assert stats.by_day[-1] == 3 and stats.by_day[-2] == 1
    assert sum(stats.by_day) == stats.runs
    # The home Fleets widget's stacked bars (#11996): the interruption and the run in flight are in neither.
    assert stats.succeeded_by_day[-1] == 1 and stats.failed_by_day[-1] == 1
    assert sum(stats.succeeded_by_day) == stats.succeeded and sum(stats.failed_by_day) == stats.failed
    assert stats.last_summary is None


def test_last_summary_is_one_line_and_bounded():
    assert one_line_summary(None) is None
    assert one_line_summary("  \n ") is None
    assert one_line_summary("Closed 4 items\n\nand  refreshed") == "Closed 4 items and refreshed"
    long = one_line_summary("word " * 200)
    assert long is not None and len(long) == LAST_SUMMARY_MAX_CHARS and long.endswith("\u2026")


def test_rows_outside_the_window_are_ignored_and_naive_dates_read_as_utc():
    rows = [
        (NOW - timedelta(days=RUN_STATS_DAYS), "completed", None),
        (NOW + timedelta(days=1), "completed", None),
        ((NOW - timedelta(days=RUN_STATS_DAYS - 1)).replace(tzinfo=None), "completed", None),
    ]
    stats = run_stats(rows, awaiting_approval=0, now=NOW)
    assert stats.runs == 1
    assert stats.by_day[0] == 1
