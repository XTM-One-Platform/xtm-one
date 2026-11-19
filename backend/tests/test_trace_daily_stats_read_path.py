"""The >7d rollup read path must answer the same question as the <=7d live path."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.models.llm_trace import LLM_TRACE_SOURCES
from app.services.tracing.latency_histogram import encode_histogram
from app.services.tracing.read_store import (
    PostgresTraceReadStore,
    _LatencyCells,
    _today_live_stats_query,
    build_stats_filters,
    combined_daily_stats_subquery,
    daily_stats_visible_filter,
    resolve_date_range,
    unrolled_day_ranges,
)


def _filters(**overrides):
    return SimpleNamespace(
        agent_ids=overrides.get("agent_ids"),
        models=overrides.get("models"),
        sources=overrides.get("sources"),
    )


def _user(*, is_admin: bool) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), is_admin=is_admin)


def _render(query) -> str:
    return " ".join(
        str(
            query.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).lower().split()
    )


def test_admin_rollup_reads_are_unrestricted_but_for_sandbox_copies():
    """An admin reads every rollup row except a Platform Health sandbox copy's (#8255)."""
    rendered = _render(daily_stats_visible_filter(_user(is_admin=True), []))
    assert rendered == (
        "llm_daily_stats.agent_id is null or (llm_daily_stats.agent_id not in "
        "(select agents.id from agents where agents.is_sandbox is true))"
    )


def test_non_admin_rollup_reads_include_their_own_null_agent_rows():
    # The defect: build_stats_filters dropped every agent_id IS NULL row for a
    # non-admin, so their general-chat / proxy / agentless volume vanished from
    # 30d and 90d views while the <=7d live path counted it. The rollup carries
    # user_id precisely so both sides can apply the same predicate.
    user = _user(is_admin=False)
    rendered = _render(
        build_stats_filters(
            _filters(),
            user=user,
            user_group_ids=[str(uuid.uuid4())],
            start=datetime(2026, 4, 1, tzinfo=UTC),
            end=datetime(2026, 6, 30, tzinfo=UTC),
        )
    )

    assert "general-chat" in rendered
    assert "llm_daily_stats.user_id" in rendered
    assert "llm_daily_stats.agent_id is null" in rendered
    # The visible-agent arm survives alongside the new user-scoped ones.
    assert "agents.is_company_managed" in rendered


def test_today_live_aggregation_carries_user_id_and_live_visibility():
    user = _user(is_admin=False)
    rendered = _render(
        _today_live_stats_query(
            _filters(),
            user=user,
            user_group_ids=[],
            window_start=datetime(2026, 6, 16, tzinfo=UTC),
            window_end=datetime(2026, 6, 16, 12, tzinfo=UTC),
        )
    )

    assert "group by" in rendered
    assert "user_id" in rendered
    # Built from build_trace_filters, so today's live half of the union applies
    # exactly the live path's visibility (including own general-chat traces).
    assert "general-chat" in rendered


def test_union_pairs_the_same_columns_on_both_halves():
    # UNION ALL pairs columns positionally: if the rollup half and the live half
    # disagree on column order, figures silently land in the wrong column.
    subquery = combined_daily_stats_subquery(
        _filters(),
        user=_user(is_admin=True),
        user_group_ids=[],
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime.now(UTC),
    )

    assert [column.name for column in subquery.c][:5] == [
        "date",
        "agent_id",
        "user_id",
        "model",
        "source",
    ]


def test_live_arm_serves_every_day_without_a_rollup_not_only_the_trailing_ones():
    # The live arm used to start the day after MAX(llm_daily_stats.date),
    # so a day without a rollup in the middle of the window was
    # served by neither arm and read as zero. It now covers every day of the
    # window that has no rollup row - computed in the same statement, so both
    # arms share one snapshot - one (source, started_at) range per day, and keys
    # each trace by its own day.
    subquery = combined_daily_stats_subquery(
        _filters(),
        user=_user(is_admin=True),
        user_group_ids=[],
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 6, 30, tzinfo=UTC),
    )
    rendered = _render(select(subquery))

    assert "max(llm_daily_stats.date)" not in rendered
    assert "generate_series(greatest(cast('2026-04-01' as timestamp without time zone), " in rendered
    assert "least(cast('2026-06-30' as timestamp without time zone), " in rendered
    assert "where not (exists (select * from llm_daily_stats" in rendered
    # One index range per day, fenced so the planner cannot flatten it into a
    # filter over every trace of the window.
    assert ") as unrolled_days join lateral (select llm_traces.id" in rendered
    assert (
        "llm_traces.started_at >= unrolled_days.day_start and llm_traces.started_at < unrolled_days.day_end "
        "limit all offset 0) as day_traces on true"
    ) in rendered
    assert "llm_traces.source in ('agent-chat'" in rendered
    # Keyed by the UTC day, like a rollup row - never date(), which follows
    # the session TimeZone.
    assert "group by cast(timezone('utc', anon_3.started_at) as date)" in rendered
    assert "date(anon_3.started_at)" not in rendered


def test_a_day_is_rolled_up_only_before_today():
    # The rollup arm stops at yesterday; a stray same-day row must not take
    # today away from the live arm.
    rendered = _render(
        select(
            unrolled_day_ranges(
                datetime(2026, 6, 1, tzinfo=UTC),
                datetime(2026, 6, 16, 12, tzinfo=UTC),
                date(2026, 6, 16),
            )
        )
    )

    assert "llm_daily_stats.date = cast(window_days.day as date)" in rendered
    assert "llm_daily_stats.date < '2026-06-16'" in rendered


def test_the_day_series_is_clamped_to_the_traced_days():
    # A custom window has no maximum span - the API and the page's date inputs
    # take any date - and every day of the series costs a probe in each
    # rollup-served statement: a century-long window walked 36,500 days, twice
    # for the summary. No day before the oldest trace or after the newest one
    # has anything to serve live, so the series stops at them, each edge one
    # (source, started_at) index probe per source. No trace at all is an empty
    # series.
    rendered = _render(
        select(
            unrolled_day_ranges(
                datetime(1926, 10, 22, tzinfo=UTC),
                datetime(2026, 9, 27, 22, tzinfo=UTC),
                date(2026, 9, 27),
            )
        )
    )

    assert (
        "generate_series(greatest(cast('1926-10-22' as timestamp without time zone), "
        "coalesce(cast(cast(timezone('utc', least("
    ) in rendered
    assert "cast('2026-09-27' as timestamp without time zone) + interval '1 day'))" in rendered
    assert (
        "least(cast('2026-09-27' as timestamp without time zone), cast(cast(timezone('utc', greatest("
    ) in rendered
    for source in LLM_TRACE_SOURCES:
        for direction in ("asc", "desc"):
            assert (
                f"from llm_traces where llm_traces.source = '{source}' "
                f"order by llm_traces.started_at {direction} limit 1)"
            ) in rendered


def test_both_arms_bound_the_window_by_utc_days():
    # 2026-06-30 23:00 at UTC-5 is 2026-07-01 04:00 UTC: the raw-trace filter
    # reaches into July 1st, so both arms must, or those hours are lost.
    minus_five = timezone(timedelta(hours=-5))
    sub = combined_daily_stats_subquery(
        _filters(),
        user=_user(is_admin=True),
        user_group_ids=[],
        start=datetime(2026, 6, 1, 20, tzinfo=minus_five),
        end=datetime(2026, 6, 30, 23, tzinfo=minus_five),
    )
    rendered = _render(select(sub))

    assert "greatest(cast('2026-06-02' as timestamp without time zone), " in rendered
    assert "least(cast('2026-07-01' as timestamp without time zone), " in rendered
    assert "llm_daily_stats.date >= '2026-06-02'" in rendered
    assert "llm_daily_stats.date <= '2026-07-01'" in rendered
    # A rollup row cannot be split, so the live arm reads the same days whole:
    # cut at the window's instants, a first or last day served live would read
    # differently the moment it is rolled up.
    assert "llm_traces.started_at >= '2026-06-02 00:00:00+00:00'" in rendered
    assert "llm_traces.started_at < '2026-07-02 00:00:00+00:00'" in rendered
    assert "-05:00'" not in rendered


@pytest.mark.parametrize(
    "date_to",
    [datetime(2026, 6, 30), datetime(2026, 6, 30, tzinfo=UTC)],
    ids=["naive", "aware"],
)
def test_a_custom_window_reads_a_naive_bound_as_utc(date_to):
    # The Analytics page sends bare dates. The rollup's day bounds and the
    # ClickHouse store read a naive bound as UTC, but asyncpg converts one
    # through the process's local zone, and a naive bound cannot even be
    # compared with an aware one.
    start, end, prev_start, prev_end = resolve_date_range("custom", datetime(2026, 6, 1), date_to)

    assert (start, end) == (datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 6, 30, tzinfo=UTC))
    assert start.utcoffset() == end.utcoffset() == timedelta(0)
    assert (prev_start, prev_end) == (datetime(2026, 5, 3, tzinfo=UTC), start)


def test_a_custom_window_keeps_the_offset_it_was_sent_with():
    plus_fourteen = timezone(timedelta(hours=14))
    start, end, _, _ = resolve_date_range(
        "custom", datetime(2026, 6, 1, tzinfo=plus_fourteen), datetime(2026, 6, 30, tzinfo=plus_fourteen)
    )

    assert (start.tzinfo, end.tzinfo) == (plus_fourteen, plus_fourteen)


class _RecordingSession:
    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, statement):
        self.statements.append(_render(statement))
        return SimpleNamespace(all=lambda: [], scalar=lambda: None)


_JIT_OFF = "set local jit = off"


@pytest.mark.asyncio
async def test_daily_latency_reads_the_rollup_and_the_live_days_in_one_statement():
    # Two statements - the rollup capped at a pre-read MAX date, then the live
    # days after it - could not serve a hole in the middle, and a commit
    # between them could drop or double a day in percentiles shown as exact.
    db = _RecordingSession()

    payload = await PostgresTraceReadStore()._daily_latency(
        db,
        filters=_filters(),
        user=_user(is_admin=True),
        user_group_ids=[],
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 6, 30, tzinfo=UTC),
    )

    assert payload["overall"]["p50"] is None
    assert db.statements[0] == _JIT_OFF
    assert len(db.statements) == 2
    statement = db.statements[1]
    assert " union all " in statement
    assert "from llm_daily_stats" in statement
    assert ") as unrolled_days join lateral (select llm_traces.id" in statement
    assert "max(llm_daily_stats.date)" not in statement


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    [
        "analytics_summary",
        "analytics_cost_by_model",
        "analytics_cost_over_time",
        "analytics_volume_over_time",
        "analytics_latency",
        "analytics_cost_by_agent",
        "analytics_usage_by_user",
    ],
)
async def test_every_rollup_served_read_turns_jit_off_before_it_runs(method):
    # The planner costs the live arm's run-time day set blind and the estimate
    # crosses jit_above_cost by orders of magnitude: each read then compiled
    # for ~250 ms to run for a few. The setting is transaction-scoped.
    db = _RecordingSession()
    start, end = datetime(2026, 4, 1, tzinfo=UTC), datetime(2026, 6, 30, tzinfo=UTC)
    window = {"start": start, "end": end}
    if method == "analytics_summary":
        window.update(prev_start=start - (end - start), prev_end=start)

    await getattr(PostgresTraceReadStore(), method)(
        db, filters=_filters(), user=_user(is_admin=True), user_group_ids=[], use_daily=True, **window
    )

    assert db.statements[0] == _JIT_OFF
    assert any("unrolled_days" in statement for statement in db.statements[1:])


@pytest.mark.asyncio
async def test_a_short_window_keeps_jit_as_configured():
    # Served from raw traces with constant bounds, a <= 7 day read is costed
    # from the column statistics like any other query.
    db = _RecordingSession()

    await PostgresTraceReadStore().analytics_volume_over_time(
        db,
        filters=_filters(),
        user=_user(is_admin=True),
        user_group_ids=[],
        start=datetime(2026, 6, 23, tzinfo=UTC),
        end=datetime(2026, 6, 30, tzinfo=UTC),
        use_daily=False,
    )

    assert _JIT_OFF not in db.statements


def _rollup_row(cells: _LatencyCells, *, day: date, model: str, histogram, **overrides) -> None:
    cells.add_rollup_row(
        date=day,
        model=model,
        trace_count=overrides.get("trace_count", 100),
        p50=overrides.get("p50", 200.0),
        p95=overrides.get("p95", 900.0),
        histogram=histogram,
    )


def test_latency_cells_merge_histograms_exactly_when_every_row_has_one():
    cells = _LatencyCells()
    # 200 traces at ~100ms on day one, 200 at ~10s on day two: the p95 of the
    # union sits in the slow group, which a weighted mean of per-day p95s cannot
    # express.
    fast = {31: 200}  # ~100 ms
    slow = {57: 200}  # ~10 s
    _rollup_row(cells, day=date(2026, 6, 1), model="gpt-5.3-codex", histogram=fast, trace_count=200)
    _rollup_row(cells, day=date(2026, 6, 2), model="gpt-5.3-codex", histogram=slow, trace_count=200)

    payload = cells.to_payload()

    assert payload["approximate"] is False
    # All five percentiles are answerable from a histogram, where the old
    # weighted-mean path could only ever produce p50 and p95.
    assert all(payload["overall"][name] is not None for name in ("p50", "p75", "p90", "p95", "p99"))
    assert payload["overall"]["p50"] < payload["overall"]["p95"]
    assert [point["date"] for point in payload["daily_median"]] == ["2026-06-01", "2026-06-02"]
    assert payload["by_model"][0]["model"] == "gpt-5.3-codex"
    assert payload["by_model"][0]["trace_count"] == 400


def test_latency_cells_fall_back_and_flag_when_a_row_predates_histograms():
    cells = _LatencyCells()
    _rollup_row(cells, day=date(2026, 6, 1), model="m", histogram={31: 100})
    # A row written before duration_histogram existed: latency data, no buckets.
    _rollup_row(cells, day=date(2026, 6, 2), model="m", histogram=None, p50=400.0, p95=1200.0)

    payload = cells.to_payload()

    assert payload["approximate"] is True
    # The fallback can only reproduce the two percentiles that were stored.
    assert payload["overall"]["p50"] is not None
    assert payload["overall"]["p75"] is None
    assert payload["overall"]["p99"] is None
    # The day that DOES have a histogram is still merged exactly.
    day_one = next(point for point in payload["daily_median"] if point["date"] == "2026-06-01")
    assert day_one["p50"] is not None


def test_latency_cells_do_not_flag_a_window_with_no_latency_data():
    cells = _LatencyCells()
    # A group whose traces all had NULL duration: no histogram AND no percentiles.
    # That is missing data, not an approximation.
    _rollup_row(cells, day=date(2026, 6, 1), model="m", histogram=None, p50=None, p95=None)

    payload = cells.to_payload()

    assert payload["approximate"] is False
    assert payload["overall"]["p50"] is None


def test_latency_cells_include_today_from_live_buckets():
    cells = _LatencyCells()
    _rollup_row(cells, day=date(2026, 6, 15), model="m", histogram={31: 10}, trace_count=10)
    cells.add_live_bucket(date=date(2026, 6, 16), model="m", bucket=57, count=5)

    payload = cells.to_payload()

    assert payload["approximate"] is False
    assert [point["date"] for point in payload["daily_median"]] == ["2026-06-15", "2026-06-16"]
    assert payload["by_model"][0]["trace_count"] == 15


def test_latency_cells_represent_today_in_the_legacy_fallback_too():
    # Mixed window: a legacy row forces the weighted-mean fallback, and today's
    # live buckets must still contribute to it rather than being dropped.
    with_today = _LatencyCells()
    _rollup_row(with_today, day=date(2026, 6, 1), model="m", histogram=None, p50=100.0, p95=200.0, trace_count=10)
    with_today.add_live_bucket(date=date(2026, 6, 16), model="m", bucket=57, count=10)

    without_today = _LatencyCells()
    _rollup_row(without_today, day=date(2026, 6, 1), model="m", histogram=None, p50=100.0, p95=200.0, trace_count=10)

    mixed = with_today.to_payload()
    legacy_only = without_today.to_payload()

    assert mixed["approximate"] is True
    assert mixed["overall"]["p50"] > legacy_only["overall"]["p50"]


def test_normalized_histogram_from_storage_feeds_the_merge():
    # End-to-end of the stored wire format: what the aggregator encodes is what
    # the read path merges.
    stored = encode_histogram({31: 3, 40: 1})
    cells = _LatencyCells()
    cells.add_rollup_row(
        date=date(2026, 6, 1),
        model="m",
        trace_count=4,
        p50=None,
        p95=None,
        histogram={int(bucket): count for bucket, count in stored["b"].items()},
    )

    payload = cells.to_payload()

    assert payload["approximate"] is False
    assert payload["overall"]["p50"] is not None
