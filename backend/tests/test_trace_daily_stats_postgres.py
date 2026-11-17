"""Real-PostgreSQL execution of the rollup's hand-written SQL.

Every statement here is raw or semi-raw SQL that no fake-DB test can validate:
the ``width_bucket`` histogram expression shared by the aggregator and the read
path, the purge's ``AT TIME ZONE 'UTC'``day-keyed rescue probe and guarded
``DELETE``, and the re-keyed unique constraint. A typo in any of them 500s an
analytics endpoint or silently stops purging, so they are exercised against a
real database.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.agent import Agent
from app.models.agent_tombstone import AgentTombstone
from app.models.llm_trace import (
    LLM_TRACE_SOURCES,
    LLMDailyStats,
    LLMDailyStatsStaleDay,
    LLMScore,
    LLMSpan,
    LLMTrace,
)
from app.models.platform_settings import PlatformSettings
from app.models.template_container import TemplateContainer
from app.models.user import User
from app.services.trace_storage import aggregate_daily_stats, purge_expired_traces
from app.services.tracing.latency_histogram import normalize_histogram, percentile_from_histogram
from tests.scratch_database import scratch_database

_TABLES = [
    User.__table__,
    Agent.__table__,
    # A deleted agent's traces stay visible through its tombstone:
    # the trace queries join it beside ``agents``.
    AgentTombstone.__table__,
    # ``platform_settings.default_presentation_template_id`` references
    # ``template_containers``; ``create_all(tables=...)`` only creates
    # what it is given, so the referenced table has to be in the subset too.
    TemplateContainer.__table__,
    PlatformSettings.__table__,
    LLMTrace.__table__,
    LLMSpan.__table__,
    LLMScore.__table__,
    LLMDailyStats.__table__,
    LLMDailyStatsStaleDay.__table__,
]


@asynccontextmanager
async def _temp_database(prefix: str, *, extra_tables=()) -> AsyncIterator[async_sessionmaker]:
    """Isolated database with just the observability tables, dropped on exit."""
    async with scratch_database(prefix, tables=[*_TABLES, *extra_tables]) as db:
        async with db.engine.begin() as conn:
            # aggregate_daily_stats stamps last_aggregation_at on the singleton row.
            await conn.execute(
                text(
                    "INSERT INTO platform_settings (id, created_at, updated_at) "
                    "VALUES (:id, now(), now())"
                ),
                {"id": str(uuid.uuid4())},
            )
        yield db.sessions


async def _insert_trace(
    session,
    *,
    started_at: datetime,
    duration_ms: float | None = 100.0,
    user_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    model: str | None = "claude-opus-4-8",
    source: str = "agent-chat",
    status: str = "ok",
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO llm_traces (
                id, source, name, tags, started_at, status, level,
                model, user_id, agent_id, input_tokens, output_tokens, total_cost, duration_ms
            ) VALUES (
                :id, :source, :name, :tags, :started_at, :status, 'DEFAULT',
                :model, :user_id, :agent_id, 5, 7, 0.002, :duration_ms
            )
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "source": source,
            "name": "rollup-integration",
            "tags": [f"source:{source}"],
            "started_at": started_at,
            "status": status,
            "model": model,
            "user_id": str(user_id) if user_id else None,
            "agent_id": str(agent_id) if agent_id else None,
            "duration_ms": duration_ms,
        },
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_aggregate_daily_stats_writes_user_scoped_rows_and_a_usable_histogram():
    alice, bob = uuid.uuid4(), uuid.uuid4()
    day = (datetime.now(UTC) - timedelta(days=2)).date()
    noon = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)

    async with _temp_database("copilot_rollup") as session_factory:
        async with session_factory() as session:
            # Two users on the same agent/model/source: one rollup row each once
            # user_id is part of the grouping key.
            for _ in range(40):
                await _insert_trace(session, started_at=noon, duration_ms=100.0, user_id=alice)
            for _ in range(10):
                await _insert_trace(session, started_at=noon, duration_ms=8_000.0, user_id=bob)
            # A NULL duration must not break the histogram scan.
            await _insert_trace(session, started_at=noon, duration_ms=None, user_id=alice)
            await session.commit()

            inserted = await aggregate_daily_stats(session, target_date=day)

            assert inserted == 2
            rows = (
                await session.execute(
                    text(
                        "SELECT user_id, trace_count, duration_histogram "
                        "FROM llm_daily_stats WHERE date = :day ORDER BY trace_count DESC"
                    ),
                    {"day": day},
                )
            ).mappings().all()

        by_user = {row["user_id"]: row for row in rows}
        assert set(by_user) == {alice, bob}
        assert by_user[alice]["trace_count"] == 41  # includes the NULL-duration trace
        assert by_user[bob]["trace_count"] == 10

        # The stored histogram is decodable and its percentile matches the seeded
        # durations - i.e. the SQL width_bucket geometry agrees with the Python side.
        alice_histogram = normalize_histogram(by_user[alice]["duration_histogram"])
        assert alice_histogram is not None
        assert sum(alice_histogram.values()) == 40  # NULL duration contributes nothing
        assert percentile_from_histogram(alice_histogram, 0.5) == pytest.approx(100.0, rel=0.15)
        bob_histogram = normalize_histogram(by_user[bob]["duration_histogram"])
        assert bob_histogram is not None
        assert percentile_from_histogram(bob_histogram, 0.5) == pytest.approx(8_000.0, rel=0.15)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unique_constraint_separates_users_but_still_collapses_nulls():
    day = (datetime.now(UTC) - timedelta(days=3)).date()
    alice = uuid.uuid4()

    async def _insert(session, user_id):
        await session.execute(
            text(
                """
                INSERT INTO llm_daily_stats (id, date, agent_id, user_id, model, source, trace_count)
                VALUES (:id, :day, NULL, :user_id, 'm', 'agent-chat', 1)
                """
            ),
            {"id": str(uuid.uuid4()), "day": day, "user_id": str(user_id) if user_id else None},
        )

    async with _temp_database("copilot_uq") as session_factory:
        # Same (date, agent, model, source), different users -> two legal rows.
        async with session_factory() as session:
            await _insert(session, alice)
            await _insert(session, uuid.uuid4())
            await _insert(session, None)
            await session.commit()

        # A second NULL-user row is still a duplicate: NULLS NOT DISTINCT has to
        # keep covering user_id too, or null-user rows would multiply unbounded.
        async with session_factory() as session:
            with pytest.raises(IntegrityError):
                await _insert(session, None)
                await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_purge_keeps_the_boundary_day_and_rescues_unaggregated_days():
    now = datetime.now(UTC)
    today = now.date()
    retention = 7
    boundary_day = today - timedelta(days=retention)  # the oldest day still kept
    expired_day = today - timedelta(days=retention + 2)

    async with _temp_database("copilot_purge") as session_factory:
        async with session_factory() as session:
            # Boundary day, spread either side of the current wall-clock time: with
            # an instant cutoff the morning half was deleted and the rollup built
            # from the survivors under-reported permanently.
            boundary_start = datetime.combine(boundary_day, datetime.min.time(), tzinfo=UTC)
            await _insert_trace(session, started_at=boundary_start + timedelta(hours=1))
            await _insert_trace(session, started_at=boundary_start + timedelta(hours=23))
            # An expired day that was never aggregated.
            await _insert_trace(session, started_at=datetime.combine(expired_day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=5))
            await session.commit()

            deleted = await purge_expired_traces(session, retention_days=retention)

            assert deleted == 1  # only the expired day
            remaining = (
                await session.execute(
                    text(
                        "SELECT (started_at AT TIME ZONE 'UTC')::date AS d, COUNT(*) "
                        "FROM llm_traces GROUP BY 1"
                    )
                )
            ).all()
            rollups = (
                await session.execute(text("SELECT date, trace_count FROM llm_daily_stats ORDER BY date"))
            ).all()

        # The whole boundary day survives - both halves.
        assert dict((row[0], row[1]) for row in remaining) == {boundary_day: 2}
        # The expired day was aggregated BEFORE its traces were deleted, so its KPI
        # outlives them instead of being lost.
        assert [(row[0], row[1]) for row in rollups] == [(expired_day, 1)]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_purge_refuses_to_delete_a_day_whose_aggregation_failed(monkeypatch):
    now = datetime.now(UTC)
    expired_day = now.date() - timedelta(days=10)

    async def _failing_aggregate(_db, target_date=None):
        return 0  # what aggregate_daily_stats returns when it fails

    async with _temp_database("copilot_guard") as session_factory:
        async with session_factory() as session:
            await _insert_trace(
                session,
                started_at=datetime.combine(expired_day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=3),
            )
            await session.commit()

            monkeypatch.setattr("app.services.trace_storage.aggregate_daily_stats", _failing_aggregate)
            deleted = await purge_expired_traces(session, retention_days=7)

            surviving = (await session.execute(text("SELECT COUNT(*) FROM llm_traces"))).scalar()

        # No rollup, no delete: the traces are the only remaining copy of that
        # day's data, so they are kept for the next run to retry.
        assert deleted == 0
        assert surviving == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_daily_latency_merges_rollup_histograms_on_postgres(monkeypatch):
    """The daily latency branch must EXECUTE - it renders raw SQL into a select."""
    from app.api.v1 import traces as traces_api
    from app.services.tracing import read_store as read_store_module
    from app.services.tracing.read_store import PostgresTraceReadStore

    # _daily_latency derives "today" from the wall clock: days before it are
    # served from rollup rows, today from live traces. The live traces below sit
    # 5 minutes in the past, so near UTC midnight they land on a day that has
    # neither coverage and silently vanish from the merge (p99 collapses to the
    # 120ms bucket). Anchor "now" mid-day and freeze the read path's clock to it.
    now = datetime.combine(datetime.now(UTC).date(), datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)

    # Patching the module-global name rebinds every `datetime` in read_store,
    # including its six `isinstance(value, datetime)` checks. Driver-returned
    # datetimes are not instances of a subclass, so those would silently flip to
    # False; the metaclass keeps isinstance answering for the real class.
    class _FrozenDatetimeMeta(type(datetime)):
        def __instancecheck__(cls, instance):
            return isinstance(instance, datetime)

    class _FrozenDatetime(datetime, metaclass=_FrozenDatetimeMeta):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(read_store_module, "datetime", _FrozenDatetime)

    day = (now - timedelta(days=2)).date()
    noon = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)

    async with _temp_database("copilot_latency") as session_factory, session_factory() as session:
        for _ in range(30):
            await _insert_trace(session, started_at=noon, duration_ms=120.0)
        # Today's traces exercise the live-bucket arm (no rollup row exists yet).
        for _ in range(5):
            await _insert_trace(session, started_at=now - timedelta(minutes=5), duration_ms=900.0)
        await session.commit()
        await aggregate_daily_stats(session, target_date=day)

        payload = await PostgresTraceReadStore().analytics_latency(
            session,
            filters=traces_api.AnalyticsFilters(period="30d"),
            user=SimpleNamespace(id=uuid.uuid4(), is_admin=True),
            user_group_ids=[],
            start=now - timedelta(days=30),
            end=now + timedelta(minutes=1),
            use_daily=True,
        )

    # Every percentile is answerable from merged histograms - the old weighted-mean
    # path could only ever fill p50/p95.
    assert payload["approximate"] is False
    assert payload["overall"]["p50"] == pytest.approx(120.0, rel=0.2)
    assert payload["overall"]["p99"] == pytest.approx(900.0, rel=0.2)
    assert [point["date"] for point in payload["daily_median"]] == [str(day), str(now.date())]
    assert payload["by_model"][0]["trace_count"] == 35


def _freeze_read_store_clock(monkeypatch, frozen_now: datetime) -> None:
    """Pin the read path's wall clock. The regression window is real but narrow -
    the minutes between midnight UTC and the ~01:00 aggregation run, when
    yesterday has no rollup row yet - so the tests place "now" there
    deterministically. Only read_store's clock moves: seeding and aggregation
    take explicit dates."""
    from app.services.tracing import read_store

    class _AnyDatetime(type(datetime)):
        # read_store also type-checks values with ``isinstance(v, datetime)``.
        def __instancecheck__(cls, instance):
            return isinstance(instance, datetime)

    class _FrozenDatetime(datetime, metaclass=_AnyDatetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_now if tz is not None else frozen_now.replace(tzinfo=None)

    monkeypatch.setattr(read_store, "datetime", _FrozenDatetime)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_daily_latency_covers_yesterday_before_the_aggregator_runs(monkeypatch):
    """At 00:02 UTC yesterday has no rollup row yet: the rollup arm stops at the
    last aggregated day while the live arm used to start at midnight, so
    yesterday's traces fell between the two and vanished from percentiles
    presented as exact - the mechanism that made the merge test above flaky
    (p99 observed ~127 instead of ~900)."""
    from app.api.v1 import traces as traces_api
    from app.services.tracing.read_store import PostgresTraceReadStore

    today = datetime.now(UTC).date()
    frozen_now = datetime.combine(today, datetime.min.time(), tzinfo=UTC) + timedelta(minutes=2)
    _freeze_read_store_clock(monkeypatch, frozen_now)
    yesterday = today - timedelta(days=1)
    rolled_day = today - timedelta(days=2)

    async with _temp_database("copilot_latency_gap") as session_factory, session_factory() as session:
        for _ in range(30):
            await _insert_trace(
                session,
                started_at=datetime.combine(rolled_day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12),
                duration_ms=120.0,
            )
        for _ in range(5):
            await _insert_trace(
                session,
                started_at=datetime.combine(yesterday, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12),
                duration_ms=900.0,
            )
        await session.commit()
        # The nightly aggregation has not caught up yet: only day-2 is rolled up.
        assert await aggregate_daily_stats(session, target_date=rolled_day) > 0

        read = {
            "filters": traces_api.AnalyticsFilters(period="30d"),
            "user": SimpleNamespace(id=uuid.uuid4(), is_admin=True),
            "user_group_ids": [],
            "start": frozen_now - timedelta(days=30),
            "end": frozen_now,
            "use_daily": True,
        }
        before = await PostgresTraceReadStore().analytics_latency(session, **read)

        assert before["approximate"] is False
        assert [point["date"] for point in before["daily_median"]] == [str(rolled_day), str(yesterday)]
        assert before["overall"]["p99"] == pytest.approx(900.0, rel=0.2)
        assert before["by_model"][0]["trace_count"] == 35

        # Once the aggregator catches up (clock still frozen), the same window
        # must read identically: yesterday moves from the live arm to the rollup
        # arm without double counting or drift - the read is invariant to
        # whether the aggregation has run yet.
        assert await aggregate_daily_stats(session, target_date=yesterday) > 0
        after = await PostgresTraceReadStore().analytics_latency(session, **read)

        assert after == before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_combined_stats_cover_yesterday_before_the_aggregator_runs(monkeypatch):
    """combined_daily_stats_subquery shares the two-arm geometry, so cost and
    usage KPIs lost yesterday between midnight and the aggregation run too -
    and its live arm used to fold every trace onto a single "today" label."""
    from app.api.v1 import traces as traces_api
    from app.services.tracing.read_store import PostgresTraceReadStore

    today = datetime.now(UTC).date()
    frozen_now = datetime.combine(today, datetime.min.time(), tzinfo=UTC) + timedelta(minutes=2)
    _freeze_read_store_clock(monkeypatch, frozen_now)
    yesterday = today - timedelta(days=1)
    rolled_day = today - timedelta(days=2)

    async with _temp_database("copilot_stats_gap") as session_factory, session_factory() as session:
        for day, count in ((rolled_day, 3), (yesterday, 2)):
            for minute in range(count):
                await _insert_trace(
                    session,
                    started_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC)
                    + timedelta(hours=12, minutes=minute),
                )
        # One trace in the frozen "today" (00:01): live before AND after catch-up.
        await _insert_trace(session, started_at=frozen_now - timedelta(minutes=1))
        await session.commit()
        assert await aggregate_daily_stats(session, target_date=rolled_day) > 0

        store = PostgresTraceReadStore()
        read = {
            "filters": traces_api.AnalyticsFilters(period="30d"),
            "user": SimpleNamespace(id=uuid.uuid4(), is_admin=True),
            "user_group_ids": [],
            "start": frozen_now - timedelta(days=30),
            "end": frozen_now,
            "use_daily": True,
        }
        volume_before = await store.analytics_volume_over_time(session, **read)
        cost_before = await store.analytics_cost_over_time(session, **read)

        # Pre-fix: yesterday appeared in neither arm, and the live traces were
        # all labeled "today" instead of their own day.
        assert {point["date"]: point["count"] for point in volume_before} == {
            str(rolled_day): 3,
            str(yesterday): 2,
            str(today): 1,
        }
        assert {point["date"]: point["cost"] for point in cost_before} == {
            str(rolled_day): pytest.approx(0.006),
            str(yesterday): pytest.approx(0.004),
            str(today): pytest.approx(0.002),
        }

        # Aggregating yesterday (clock still frozen) must not change the read:
        # the day is served by exactly one arm before and after.
        assert await aggregate_daily_stats(session, target_date=yesterday) > 0
        assert await store.analytics_volume_over_time(session, **read) == volume_before


async def _rollup_dates(session) -> set[date]:
    return set((await session.execute(text("SELECT DISTINCT date FROM llm_daily_stats"))).scalars())


def _analytics_read(frozen_now: datetime, *, days: int) -> dict:
    from app.api.v1 import traces as traces_api

    return {
        "filters": traces_api.AnalyticsFilters(period="30d"),
        "user": SimpleNamespace(id=uuid.uuid4(), is_admin=True),
        "user_group_ids": [],
        "start": frozen_now - timedelta(days=days),
        "end": frozen_now,
        "use_daily": True,
    }


async def _hole_reads(store, session, read: dict) -> dict:
    summary = await store.analytics_summary(
        session,
        **read,
        prev_start=read["start"] - (read["end"] - read["start"]),
        prev_end=read["start"],
    )
    summary.pop("last_aggregation_at", None)
    return {
        "summary": summary,
        "volume": await store.analytics_volume_over_time(session, **read),
        "cost": await store.analytics_cost_over_time(session, **read),
        "latency": await store.analytics_latency(session, **read),
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_day_the_backfill_left_behind_is_served_live_until_it_is_rolled_up(monkeypatch):
    """After downtime the backfill builds the newest days first, so the
    older missing days sit behind later rollups for a run or more. The live arm
    used to start after MAX(date) and those days read as zero - traces, tokens,
    errors, cost and latency alike. They are served live, and once rolled up
    the same reads come back identical: no day is ever counted twice."""
    from app.services.trace_storage import mark_daily_stats_stale, run_daily_stats_backfill
    from app.services.tracing.read_store import PostgresTraceReadStore

    today = datetime.now(UTC).date()
    frozen_now = datetime.combine(today, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)
    _freeze_read_store_clock(monkeypatch, frozen_now)
    day = {n: today - timedelta(days=n) for n in range(1, 6)}
    # The hole: 4 slow traces, 2 of them failed, each costing 0.01.
    hole, older_hole = day[4], day[3]

    async with _temp_database("copilot_rollup_hole") as session_factory, session_factory() as session:
        for n, count in ((5, 3), (3, 1), (2, 2), (1, 1)):
            for minute in range(count):
                await _insert_trace(session, started_at=_noon(day[n]) + timedelta(minutes=minute))
        for minute in range(4):
            await _insert_trace(
                session,
                started_at=_noon(hole) + timedelta(minutes=minute),
                duration_ms=900.0,
                status="error" if minute < 2 else "ok",
            )
        await session.execute(
            text("UPDATE llm_traces SET total_cost = 0.01 WHERE duration_ms = 900"),
        )
        await _insert_trace(session, started_at=frozen_now - timedelta(hours=1))
        await session.commit()

        # Rolled up before the outage, then the first run after it, whose
        # budget only reaches the two newest days.
        assert await aggregate_daily_stats(session, target_date=day[5]) > 0
        assert await run_daily_stats_backfill(session, retention_days=90, now=frozen_now, max_days=2) > 0
        assert await _rollup_dates(session) == {day[5], day[2], day[1]}

        store = PostgresTraceReadStore()
        read = _analytics_read(frozen_now, days=30)
        before = await _hole_reads(store, session, read)

        assert {point["date"]: point["count"] for point in before["volume"]} == {
            str(day[5]): 3,
            str(hole): 4,
            str(older_hole): 1,
            str(day[2]): 2,
            str(day[1]): 1,
            str(today): 1,
        }
        assert {point["date"]: point["cost"] for point in before["cost"]}[str(hole)] == pytest.approx(0.04)
        summary = before["summary"]
        assert summary["trace_count"] == 12
        assert summary["total_tokens"] == 12 * 12
        assert summary["error_rate"] == pytest.approx(2 / 12)
        assert float(summary["total_cost"]) == pytest.approx(8 * 0.002 + 4 * 0.01)
        assert summary["avg_duration_ms"] == pytest.approx((8 * 100 + 4 * 900) / 12)
        latency = before["latency"]
        medians = {point["date"]: point for point in latency["daily_median"]}
        assert set(medians) == {str(d) for d in (day[5], hole, older_hole, day[2], day[1], today)}
        assert latency["by_model"][0]["trace_count"] == 12
        assert latency["overall"]["p99"] == pytest.approx(900.0, rel=0.2)

        # The next run fills the hole: each day moves from the live arm to the
        # rollup arm without a single figure changing.
        assert await run_daily_stats_backfill(session, retention_days=90, now=frozen_now, max_days=2) > 0
        assert await _rollup_dates(session) == set(day.values())
        assert await _hole_reads(store, session, read) == before

        # A day recorded stale keeps its rollup until rebuilt: served once,
        # from that rollup, never from the rollup AND the traces.
        await mark_daily_stats_stale(session, [hole])
        await session.commit()
        assert await _stale_days(session) == [hole]
        assert await _hole_reads(store, session, read) == before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_previous_window_serves_a_day_without_a_rollup_live(monkeypatch):
    """The summary's previous window read the rollup alone, so a day of it the
    backfill had not reached made the period-over-period delta a false rise."""
    from app.services.tracing.read_store import PostgresTraceReadStore

    today = datetime.now(UTC).date()
    frozen_now = datetime.combine(today, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)
    _freeze_read_store_clock(monkeypatch, frozen_now)
    rolled, hole, current = (today - timedelta(days=n) for n in (12, 15, 5))

    async with _temp_database("copilot_previous_hole") as session_factory, session_factory() as session:
        for target, count in ((hole, 3), (rolled, 2), (current, 1)):
            for minute in range(count):
                await _insert_trace(session, started_at=_noon(target) + timedelta(minutes=minute))
        await session.commit()
        for target in (rolled, current):
            assert await aggregate_daily_stats(session, target_date=target) > 0

        store = PostgresTraceReadStore()
        read = _analytics_read(frozen_now, days=10)
        before = (await _hole_reads(store, session, read))["summary"]

        assert before["trace_count"] == 1
        assert before["previous"]["trace_count"] == 5
        assert float(before["previous"]["total_cost"]) == pytest.approx(5 * 0.002)

        assert await aggregate_daily_stats(session, target_date=hole) > 0
        assert (await _hole_reads(store, session, read))["summary"] == before


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("time_zone", "utc_hour"),
    [
        # 02:00 UTC is still the previous day in New York.
        ("America/New_York", 2),
        # 10:30 UTC is already the next day at UTC+14.
        ("Pacific/Kiritimati", 10.5),
    ],
)
async def test_live_days_are_utc_days_whatever_the_session_time_zone(monkeypatch, time_zone, utc_hour):
    """``date(timestamptz)`` is computed in the session TimeZone, which the
    engine does not pin. A rollup day is a UTC day (``trace_storage`` keys it
    ``(started_at AT TIME ZONE 'UTC')::date``), so on a database, role or
    connection set to another zone the live arm filed a missing UTC day under
    its neighbour: that day read empty and the neighbour was counted twice."""
    from app.services.tracing.read_store import PostgresTraceReadStore

    today = datetime.now(UTC).date()
    frozen_now = datetime.combine(today, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)
    _freeze_read_store_clock(monkeypatch, frozen_now)
    before_hole, hole, after_hole = (today - timedelta(days=n) for n in (5, 4, 3))
    offset = timedelta(hours=utc_hour)

    async with _temp_database("copilot_live_tz") as session_factory, session_factory() as session:
        await _insert_trace(session, started_at=_noon(before_hole))
        for minute in range(2):
            await _insert_trace(session, started_at=_noon(after_hole) + timedelta(minutes=minute))
        for minute in range(3):
            await _insert_trace(
                session,
                started_at=datetime.combine(hole, datetime.min.time(), tzinfo=UTC) + offset + timedelta(minutes=minute),
            )
        await _insert_trace(
            session, started_at=datetime.combine(today, datetime.min.time(), tzinfo=UTC) + offset
        )
        await session.commit()
        for target in (before_hole, after_hole):
            assert await aggregate_daily_stats(session, target_date=target) > 0

        # Reads only from here on, in one transaction: the SET holds.
        await session.execute(text(f"SET TIME ZONE '{time_zone}'"))
        store = PostgresTraceReadStore()
        expected = {str(before_hole): 1, str(hole): 3, str(after_hole): 2, str(today): 1}

        rolled = _analytics_read(frozen_now, days=30)
        volume = await store.analytics_volume_over_time(session, **rolled)
        assert {point["date"]: point["count"] for point in volume} == expected
        latency = await store.analytics_latency(session, **rolled)
        assert {point["date"] for point in latency["daily_median"]} == set(expected)

        # The <= 7 day window is served live from end to end, keyed the same way.
        live = {**_analytics_read(frozen_now, days=7), "use_daily": False}
        live_volume = await store.analytics_volume_over_time(session, **live)
        assert {point["date"]: point["count"] for point in live_volume} == expected
        live_cost = await store.analytics_cost_over_time(session, **live)
        assert {point["date"] for point in live_cost} == set(expected)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_century_long_window_walks_only_the_traced_days(monkeypatch):
    """A custom window has no maximum span. The day series walked every day of
    it - a century is 36,500 probes in each rollup-served statement - while no
    day before the oldest trace or after the newest has anything to serve. It
    stops at them now, and the century reads exactly what the month reads."""
    from app.services.tracing.read_store import PostgresTraceReadStore, unrolled_day_ranges

    today = datetime.now(UTC).date()
    frozen_now = datetime.combine(today, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)
    _freeze_read_store_clock(monkeypatch, frozen_now)
    oldest, hole, rolled = (today - timedelta(days=n) for n in (20, 9, 3))

    async with _temp_database("copilot_century_window") as session_factory, session_factory() as session:
        for target, count in ((oldest, 1), (hole, 2), (rolled, 3)):
            for minute in range(count):
                await _insert_trace(session, started_at=_noon(target) + timedelta(minutes=minute))
        await _insert_trace(session, started_at=frozen_now - timedelta(hours=1))
        await session.commit()
        for target in (oldest, rolled):
            assert await aggregate_daily_stats(session, target_date=target) > 0

        century = _analytics_read(frozen_now, days=36_500)
        unrolled = unrolled_day_ranges(century["start"], century["end"], today)
        walked = (await session.execute(select(func.count()).select_from(unrolled))).scalar_one()
        # The 21 days from the oldest trace to today, less the 2 rolled up.
        assert walked == 19

        store = PostgresTraceReadStore()
        figures = {}
        for label, read in (("century", century), ("month", _analytics_read(frozen_now, days=30))):
            reads = await _hole_reads(store, session, read)
            reads["summary"].pop("window_starts_before_coverage")
            figures[label] = reads
        assert {point["date"]: point["count"] for point in figures["month"]["volume"]} == {
            str(oldest): 1,
            str(hole): 2,
            str(rolled): 3,
            str(today): 1,
        }
        assert figures["century"] == figures["month"]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hole_age", "opening"),
    [
        # A preset opens at the wall clock's time of day: noon UTC here.
        (30, "preset"),
        # 00:00 at UTC+14 is 10:00 UTC the day before: bounded by the caller's
        # calendar date, both arms started a UTC day late and missed the hole.
        (9, "utc+14"),
    ],
)
async def test_a_window_opening_mid_utc_day_reads_that_day_whole_before_and_after_its_rollup(
    monkeypatch, hole_age, opening
):
    """A rollup row is a whole UTC day and cannot be split, so a window opening
    in the middle of one counts that day in full once it is rolled up - and so
    does the previous window, which closes on it. Served live, the day was cut
    at the window's first instant instead: 3 of its 5 traces until the
    aggregator reached it, 5 after, and 2 then 5 in the previous window. It is
    read whole in both arms, so the reads no longer depend on the aggregator."""
    from app.services.tracing.read_store import PostgresTraceReadStore

    today = datetime.now(UTC).date()
    frozen_now = datetime.combine(today, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)
    _freeze_read_store_clock(monkeypatch, frozen_now)
    hole = today - timedelta(days=hole_age)
    rolled = hole + timedelta(days=4)
    read = _analytics_read(frozen_now, days=30)
    if opening == "utc+14":
        read["start"] = datetime.combine(hole + timedelta(days=1), datetime.min.time(), tzinfo=timezone(timedelta(hours=14)))
    # The window opens on the hole's UTC day, after its two early traces and
    # before its three late ones.
    assert _noon(hole) - timedelta(hours=5) < read["start"].astimezone(UTC) <= _noon(hole)

    async with _temp_database("copilot_mid_day_window") as session_factory, session_factory() as session:
        for hour in (6, 7, 13, 14, 15):
            await _insert_trace(
                session, started_at=datetime.combine(hole, datetime.min.time(), tzinfo=UTC) + timedelta(hours=hour)
            )
        for minute in range(2):
            await _insert_trace(session, started_at=_noon(rolled) + timedelta(minutes=minute))
        await session.commit()
        assert await aggregate_daily_stats(session, target_date=rolled) > 0

        store = PostgresTraceReadStore()
        before = await _hole_reads(store, session, read)

        assert {point["date"]: point["count"] for point in before["volume"]} == {str(hole): 5, str(rolled): 2}
        assert before["summary"]["trace_count"] == 7
        assert before["summary"]["previous"]["trace_count"] == 5
        assert {point["date"] for point in before["latency"]["daily_median"]} == {str(hole), str(rolled)}
        assert before["latency"]["by_model"][0]["trace_count"] == 7

        assert await aggregate_daily_stats(session, target_date=hole) > 0
        assert await _hole_reads(store, session, read) == before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_custom_window_closing_on_a_day_without_a_rollup_reads_that_day_whole(monkeypatch):
    """The Analytics page sends a custom window as two bare dates, so it closes
    at 00:00 of its last day. The rollup arm reads that day (a rollup row
    cannot be split), the live arm cut it at the closing instant: a last day
    the backfill had not reached read as zero until it did, on the
    window's edge. It is read whole whichever arm serves it."""
    from app.services.tracing.read_store import PostgresTraceReadStore, resolve_date_range

    today = datetime.now(UTC).date()
    frozen_now = datetime.combine(today, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)
    _freeze_read_store_clock(monkeypatch, frozen_now)
    first, rolled, last, later = (today - timedelta(days=n) for n in (20, 15, 10, 5))

    async with _temp_database("copilot_custom_last_day") as session_factory, session_factory() as session:
        for target, count in ((rolled, 2), (last, 3), (later, 1)):
            for minute in range(count):
                await _insert_trace(session, started_at=_noon(target) + timedelta(minutes=minute))
        await session.commit()
        # A later day is rolled up, so the last day is a hole behind it.
        for target in (rolled, later):
            assert await aggregate_daily_stats(session, target_date=target) > 0

        # What the API parses from ``date_from=<first>&date_to=<last>``.
        start, end, _, _ = resolve_date_range(
            "custom",
            datetime.combine(first, datetime.min.time()),
            datetime.combine(last, datetime.min.time()),
        )
        store = PostgresTraceReadStore()
        read = {**_analytics_read(frozen_now, days=30), "start": start, "end": end}
        before = await _hole_reads(store, session, read)

        assert {point["date"]: point["count"] for point in before["volume"]} == {str(rolled): 2, str(last): 3}
        assert before["summary"]["trace_count"] == 5
        assert before["summary"]["previous"]["trace_count"] == 0
        assert {point["date"] for point in before["latency"]["daily_median"]} == {str(rolled), str(last)}

        assert await aggregate_daily_stats(session, target_date=last) > 0
        assert await _hole_reads(store, session, read) == before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_rollup_served_read_turns_jit_off_for_its_own_transaction_only():
    """The live arm's run-time day set is costed blind, far past
    ``jit_above_cost``: every rollup-served read compiled for ~250 ms to run
    for a few. JIT is off for the transaction that reads, and back to the
    server's setting after it."""
    from app.services.tracing.read_store import PostgresTraceReadStore

    async with _temp_database("copilot_rollup_jit") as session_factory, session_factory() as session:
        configured = (await session.execute(text("SHOW jit"))).scalar_one()
        await session.rollback()

        await PostgresTraceReadStore().analytics_volume_over_time(
            session, **_analytics_read(datetime.now(UTC), days=30)
        )
        assert (await session.execute(text("SHOW jit"))).scalar_one() == "off"

        await session.rollback()
        assert (await session.execute(text("SHOW jit"))).scalar_one() == configured


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_window_opening_before_the_first_covered_utc_day_is_flagged(monkeypatch):
    """``window_starts_before_coverage`` compared the caller's calendar date with
    the first covered UTC day: 00:00 at UTC+14 on that day is 10:00 UTC the day
    before - before any data - and was reported as covered."""
    from app.services.tracing.read_store import PostgresTraceReadStore

    today = datetime.now(UTC).date()
    frozen_now = datetime.combine(today, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)
    _freeze_read_store_clock(monkeypatch, frozen_now)
    first = today - timedelta(days=9)

    async with _temp_database("copilot_offset_coverage") as session_factory, session_factory() as session:
        await _insert_trace(session, started_at=_noon(first))
        await session.commit()

        store = PostgresTraceReadStore()
        flags = []
        for zone in (UTC, timezone(timedelta(hours=14))):
            start = datetime.combine(first, datetime.min.time(), tzinfo=zone)
            summary = await store.analytics_summary(
                session,
                **{**_analytics_read(frozen_now, days=30), "start": start},
                prev_start=start - timedelta(days=30),
                prev_end=start,
            )
            assert summary["stats_coverage_start"] == first
            flags.append(summary["window_starts_before_coverage"])

    assert flags == [False, True]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_model_agent_and_user_breakdowns_count_a_day_without_a_rollup(monkeypatch):
    """The per-model, per-agent and per-user breakdowns read the union every
    other rollup-served figure reads: a day without a rollup is in them, and
    they read the same once the day is rolled up."""
    from app.services.tracing.read_store import PostgresTraceReadStore

    today = datetime.now(UTC).date()
    frozen_now = datetime.combine(today, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)
    _freeze_read_store_clock(monkeypatch, frozen_now)
    older, hole, newer = (today - timedelta(days=n) for n in (12, 9, 5))
    agent = uuid.uuid4()
    person = uuid.uuid4()

    async with _temp_database("copilot_breakdown_hole") as session_factory, session_factory() as session:
        for target, count, user_id in ((older, 1, person), (hole, 3, person), (newer, 2, None)):
            for minute in range(count):
                await _insert_trace(
                    session,
                    started_at=_noon(target) + timedelta(minutes=minute),
                    agent_id=agent,
                    user_id=user_id,
                )
        await session.commit()
        for target in (older, newer):
            assert await aggregate_daily_stats(session, target_date=target) > 0

        store = PostgresTraceReadStore()
        read = _analytics_read(frozen_now, days=30)

        async def _breakdowns() -> tuple[list, list, list]:
            by_user = await store.analytics_usage_by_user(session, **read)
            return (
                await store.analytics_cost_by_model(session, **read),
                await store.analytics_cost_by_agent(session, **read),
                sorted(by_user, key=lambda row: row["user_id"] or ""),
            )

        by_model, by_agent, by_user = await _breakdowns()
        assert [(row["model"], row["trace_count"]) for row in by_model] == [("claude-opus-4-8", 6)]
        assert by_model[0]["total_cost"] == pytest.approx(6 * 0.002)
        assert [(row["agent_id"], row["trace_count"]) for row in by_agent] == [(str(agent), 6)]
        assert by_agent[0]["total_cost"] == pytest.approx(6 * 0.002)
        assert [(row["user_id"], row["trace_count"], row["total_tokens"], row["agent_count"]) for row in by_user] == [
            (None, 2, 24, 1),
            (str(person), 4, 48, 1),
        ]
        assert [row["total_cost"] for row in by_user] == [pytest.approx(2 * 0.002), pytest.approx(4 * 0.002)]

        assert await aggregate_daily_stats(session, target_date=hole) > 0
        assert await _breakdowns() == (by_model, by_agent, by_user)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_user_and_group_views_name_each_user_and_count_each_group(monkeypatch):
    """The user and group views read accounts, memberships and member counts
    with queries of their own: run them on PostgreSQL over raw traces."""
    from app.models.group import Group, GroupMember
    from app.services.tracing.read_store import PostgresTraceReadStore
    from app.services.tracing.usage_breakdown import usage_by_group, usage_by_user

    frozen_now = datetime.combine(datetime.now(UTC).date(), datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)
    _freeze_read_store_clock(monkeypatch, frozen_now)
    day = frozen_now.date() - timedelta(days=2)

    async with (
        _temp_database("copilot_usage_groups", extra_tables=[Group.__table__, GroupMember.__table__]) as session_factory,
        session_factory() as session,
    ):
        alice = User(email="alice@example.com", hashed_password="x", full_name="Alice", is_active=True)
        bob = User(email="bob@example.com", hashed_password="x", full_name="Bob", is_active=False)
        idle = User(email="idle@example.com", hashed_password="x", full_name="Idle", is_active=True)
        solo = User(email="solo@example.com", hashed_password="x", full_name="Solo", is_active=True)
        session.add_all([alice, bob, idle, solo])
        await session.flush()
        support, sales, empty = Group(name="Support"), Group(name="sales"), Group(name="Empty")
        session.add_all([support, sales, empty])
        await session.flush()
        session.add_all(
            [
                GroupMember(group_id=support.id, user_id=alice.id),
                GroupMember(group_id=sales.id, user_id=alice.id),
                GroupMember(group_id=support.id, user_id=bob.id),
                GroupMember(group_id=support.id, user_id=idle.id),
            ]
        )
        for user_id, count in ((alice.id, 3), (solo.id, 2), (bob.id, 1), (None, 2)):
            for minute in range(count):
                await _insert_trace(session, started_at=_noon(day) + timedelta(minutes=minute), user_id=user_id)
        await session.commit()

        store = PostgresTraceReadStore()
        read = {**_analytics_read(frozen_now, days=7), "use_daily": False}
        rows = await store.analytics_usage_by_user(session, **read)
        by_user = await usage_by_user(session, rows, limit=10)
        top = await usage_by_user(session, rows, limit=1)
        by_group = await usage_by_group(session, rows)
        summary = await store.analytics_summary(
            session, **read, prev_start=read["start"] - timedelta(days=7), prev_end=read["start"]
        )

    assert [
        (item["user_email"], item["user_name"], item["is_active"], item["group_names"], item["trace_count"])
        for item in by_user["items"]
    ] == [
        ("alice@example.com", "Alice", True, ["sales", "Support"], 3),
        ("solo@example.com", "Solo", True, [], 2),
        ("bob@example.com", "Bob", False, ["Support"], 1),
    ]
    assert by_user["total_users"] == 3
    assert by_user["other_users"] is None
    assert by_user["unattributed"]["trace_count"] == 2

    # Past the limit the other users are counted together, so the view still
    # adds up to the dashboard summary.
    assert [item["user_email"] for item in top["items"]] == ["alice@example.com"]
    assert (top["other_users"]["user_count"], top["other_users"]["trace_count"]) == (2, 3)
    parts = [*top["items"], top["other_users"], top["unattributed"]]
    assert sum(part["trace_count"] for part in parts) == summary["trace_count"] == 8
    assert sum(part["total_cost"] for part in parts) == pytest.approx(summary["total_cost"])

    # "Empty" has no member who ran a trace, so it is neither listed nor read;
    # Support still counts its idle member.
    assert [
        (item["group_name"], item["member_count"], item["active_user_count"], item["trace_count"])
        for item in by_group["items"]
    ] == [("Support", 3, 2, 4), ("sales", 1, 1, 3)]
    assert by_group["items"][0]["total_cost"] == pytest.approx(4 * 0.002)
    assert by_group["ungrouped"]["active_user_count"] == 1
    assert by_group["ungrouped"]["trace_count"] == 2
    assert by_group["ungrouped"]["avg_cost_per_active_user"] == pytest.approx(2 * 0.002)
    assert by_group["overlapping_membership"] is True


_OLD_CONSTRAINT = "uq_llm_daily_stats_date_agent_model_source"
_NEW_CONSTRAINT = "uq_llm_daily_stats_date_agent_model_source_user"

_PRE_MIGRATION_SHAPE = f"""
    CREATE TABLE llm_daily_stats (
        id uuid PRIMARY KEY,
        date date NOT NULL,
        agent_id uuid,
        model varchar(255) NOT NULL,
        source varchar(50) NOT NULL,
        trace_count integer NOT NULL DEFAULT 0,
        error_count integer NOT NULL DEFAULT 0,
        total_input_tokens bigint NOT NULL DEFAULT 0,
        total_output_tokens bigint NOT NULL DEFAULT 0,
        total_cost numeric(16, 8),
        avg_duration_ms double precision,
        p50_duration_ms double precision,
        p95_duration_ms double precision,
        avg_feedback_score double precision,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT {_OLD_CONSTRAINT} UNIQUE NULLS NOT DISTINCT (date, agent_id, model, source)
    )
"""


def _run_migration_body(sync_conn, direction: str) -> None:
    """Execute the real migration body against *sync_conn*.

    Runs ``upgrade()``/``downgrade()`` from the revision module itself rather than
    a copy of its SQL, so this test cannot drift from what deployments actually
    apply.
    """
    import importlib.util
    import pathlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    # Loaded by path: the local ``alembic/`` directory is the script location, not
    # an importable package (the installed ``alembic`` distribution owns that name).
    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "c3d4dlys0001_add_daily_stats_user_and_histogram.py"
    )
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    context = MigrationContext.configure(sync_conn)
    with Operations.context(context):
        getattr(module, direction)()


async def _constraint_columns(session, constraint: str) -> list[str] | None:
    rows = (
        await session.execute(
            text(
                """
                SELECT a.attname
                FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
                WHERE t.relname = 'llm_daily_stats' AND c.conname = :constraint
                ORDER BY k.ord
                """
            ),
            {"constraint": constraint},
        )
    ).all()
    return [row[0] for row in rows] or None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_rekeys_the_constraint_and_downgrade_merges_users_back():
    day = date(2026, 6, 29)
    alice, bob = uuid.uuid4(), uuid.uuid4()

    async with _temp_database("copilot_mig") as session_factory:
        engine = session_factory.kw["bind"]

        # Replace the model-built table with the PRE-migration shape, so the body
        # runs against what an existing deployment actually has.
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE llm_daily_stats"))
            await conn.execute(text(_PRE_MIGRATION_SHAPE))
            await conn.execute(
                text(
                    "INSERT INTO llm_daily_stats (id, date, agent_id, model, source, trace_count, "
                    "p50_duration_ms) VALUES (:id, :day, NULL, 'm', 'general-chat', 10, 100.0)"
                ),
                {"id": str(uuid.uuid4()), "day": day},
            )

        async with engine.begin() as conn:
            await conn.run_sync(_run_migration_body, "upgrade")
            # Idempotent: a retrying replica must find nothing to do.
            await conn.run_sync(_run_migration_body, "upgrade")

        async with session_factory() as session:
            assert await _constraint_columns(session, _NEW_CONSTRAINT) == [
                "date",
                "agent_id",
                "model",
                "source",
                "user_id",
            ]
            assert await _constraint_columns(session, _OLD_CONSTRAINT) is None
            # The pre-existing row keeps NULL user_id, so a non-admin sees exactly
            # what they saw before - no more, no less.
            assert (await session.execute(text("SELECT user_id FROM llm_daily_stats"))).scalar() is None

            # Two users can now coexist in one (date, agent, model, source) group.
            for user_id, count in ((alice, 4), (bob, 6)):
                await session.execute(
                    text(
                        "INSERT INTO llm_daily_stats (id, date, agent_id, user_id, model, source, "
                        "trace_count, p50_duration_ms) "
                        "VALUES (:id, :day, NULL, :user_id, 'm2', 'agent-chat', :count, 200.0)"
                    ),
                    {"id": str(uuid.uuid4()), "day": day, "user_id": str(user_id), "count": count},
                )
            await session.commit()

        async with engine.begin() as conn:
            await conn.run_sync(_run_migration_body, "downgrade")

        async with session_factory() as session:
            assert await _constraint_columns(session, _OLD_CONSTRAINT) == [
                "date",
                "agent_id",
                "model",
                "source",
            ]
            columns = {
                row[0]
                for row in (
                    await session.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'llm_daily_stats'"
                        )
                    )
                ).all()
            }
            assert "user_id" not in columns
            assert "duration_histogram" not in columns
            # The two per-user rows collapse into one, summing the additive counters,
            # because the restored constraint cannot hold both.
            rows = (
                await session.execute(
                    text("SELECT model, trace_count FROM llm_daily_stats ORDER BY model")
                )
            ).all()
            assert [(row[0], row[1]) for row in rows] == [("m", 10), ("m2", 10)]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_backfill_rebuilds_a_traced_day_past_idle_ones():
    """The day probe on real PostgreSQL: idle days are not due, so a
    one-day budget goes to the older day that has traces."""
    from app.services.trace_storage import _day_has_traces, run_daily_stats_backfill

    now = datetime.now(UTC).replace(hour=12)
    traced_day = (now - timedelta(days=4)).date()
    idle_day = (now - timedelta(days=2)).date()
    noon = datetime.combine(traced_day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)

    async with _temp_database("copilot_backfill_probe") as session_factory, session_factory() as session:
        await _insert_trace(session, started_at=noon, source="assignment")
        await session.commit()

        assert await _day_has_traces(session, traced_day) is True
        assert await _day_has_traces(session, idle_day) is False
        await session.rollback()

        inserted = await run_daily_stats_backfill(
            session, retention_days=10, now=now, max_days=1
        )

        assert inserted == 1
        days = (
            await session.execute(text("SELECT DISTINCT date FROM llm_daily_stats"))
        ).scalars().all()
        assert days == [traced_day]


def _flat_price(monkeypatch, *, reads_through_session: bool = False) -> None:
    """Price every model at $1 per token. ``reads_through_session`` makes the
    lookup an ORM read on the caller's session, as the real one is."""
    from decimal import Decimal

    from sqlalchemy import select

    from app.services.tracing import repricing
    from app.services.tracing.cost_calculator import ModelPrice

    price = ModelPrice(
        provider="anthropic",
        model_id="claude-opus-4-8",
        model_pattern=None,
        input_price_per_1m=Decimal("1000000"),
        output_price_per_1m=Decimal("1000000"),
        cache_read_price_per_1m=None,
        cache_write_price_per_1m=None,
        effective_from=date(2020, 1, 1),
        is_custom=False,
    )

    async def _price(db, *_args, **_kwargs):
        if reads_through_session:
            await db.execute(select(LLMTrace.id).limit(1))
        return price

    monkeypatch.setattr(repricing, "resolve_model_price_any", _price)
    monkeypatch.setattr(repricing, "resolve_model_price", _price)


def _noon(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)


async def _stale_days(session) -> list[date]:
    return list(
        (await session.execute(text("SELECT date FROM llm_daily_stats_stale_days ORDER BY date"))).scalars()
    )


async def _rollup_figures(session_factory, *, days: int = 90) -> dict:
    """What a rollup-served analytics window (``use_daily``) reads right now:
    per day, the traces and their cost; over the window and the one before it,
    the summary KPIs. A day missing its rollup reads as no day at all."""
    from app.api.v1 import traces as traces_api
    from app.services.tracing.read_store import PostgresTraceReadStore

    now = datetime.now(UTC)
    read = {
        "filters": traces_api.AnalyticsFilters(period=f"{days}d"),
        "user": SimpleNamespace(id=uuid.uuid4(), is_admin=True),
        "user_group_ids": [],
        "start": now - timedelta(days=days),
        "end": now,
        "use_daily": True,
    }
    store = PostgresTraceReadStore()
    async with session_factory() as session:
        volume = await store.analytics_volume_over_time(session, **read)
        cost = await store.analytics_cost_over_time(session, **read)
        summary = await store.analytics_summary(
            session, **read, prev_start=now - timedelta(days=2 * days), prev_end=now - timedelta(days=days)
        )
    kpis = ("trace_count", "total_tokens", "avg_duration_ms", "error_rate")
    return {
        "count": {point["date"]: point["count"] for point in volume},
        "cost": {point["date"]: round(point["cost"], 6) for point in cost},
        "current": {kpi: summary[kpi] for kpi in kpis},
        "previous": {kpi: summary["previous"][kpi] for kpi in kpis},
        "total_cost": round(summary["total_cost"], 6),
    }


async def _seed_repriceable_days(session_factory, *, priced: list[date], unpriced: list[date], per_day: int = 3):
    """``per_day`` traces a day, one of them failed, every day rolled up. The
    ``unpriced`` days' traces carry no cost - a model the pricing table did not
    know yet - so their rollups count $0."""
    async with session_factory() as session:
        for day in [*priced, *unpriced]:
            for index in range(per_day):
                await _insert_trace(
                    session,
                    started_at=_noon(day) + timedelta(minutes=index),
                    duration_ms=100.0 * (index + 1),
                    status="error" if index == 0 else "ok",
                )
        if unpriced:
            await session.execute(
                text(
                    "UPDATE llm_traces SET total_cost = NULL "
                    "WHERE (started_at AT TIME ZONE 'UTC')::date = ANY(:days)"
                ),
                {"days": unpriced},
            )
        await session.commit()
        for day in [*priced, *unpriced]:
            assert await aggregate_daily_stats(session, target_date=day) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_repricing_replaces_a_stale_rollup_and_leaves_no_row_behind(monkeypatch):
    """The costs and the record of the stale rollup commit together, then the
    day is rebuilt in place from the repriced traces, and the record goes with
    the stale row."""
    from decimal import Decimal

    from app.services.tracing import repricing

    _flat_price(monkeypatch)

    day = (datetime.now(UTC) - timedelta(days=3)).date()
    noon = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)

    async with _temp_database("copilot_reprice_rollup") as session_factory, session_factory() as session:
        await _insert_trace(session, started_at=noon)
        await session.execute(text("UPDATE llm_traces SET total_cost = NULL"))
        await session.execute(
            text(
                "INSERT INTO llm_spans (id, trace_id, type, name, started_at, input_tokens, output_tokens) "
                "SELECT :id, id, 'GENERATION', 'call', started_at, 2, 3 FROM llm_traces"
            ),
            {"id": str(uuid.uuid4())},
        )
        await session.commit()
        assert await aggregate_daily_stats(session, target_date=day) == 1
        stale = (
            await session.execute(text("SELECT total_cost FROM llm_daily_stats"))
        ).scalar_one()
        assert stale == 0

        result = await repricing.reprice_null_costs(session)

        assert result.traces_updated == 1
        assert result.spans_updated == 1
        assert result.days_reaggregated == 1
        fresh = (
            await session.execute(text("SELECT total_cost FROM llm_daily_stats"))
        ).scalars().all()
        # 5 input + 7 output tokens at $1 per token.
        assert fresh == [Decimal("12")]
        span_cost = (await session.execute(text("SELECT total_cost FROM llm_spans"))).scalar_one()
        assert span_cost == Decimal("5")
        assert await _stale_days(session) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_repricing_leaves_an_expired_days_rollup_alone(monkeypatch):
    """A trace older than the retention window can
    still be there - the purge deletes a day in batches, and only once it has a
    rollup. A rebuild must never replace that rollup with what the purge had not
    deleted yet: the day would be under-reported for good. So an expired day
    whose traces count fewer than its rollup keeps it, and is not recorded
    stale any more (the purge may finish it)."""
    from app.services.tracing import repricing

    _flat_price(monkeypatch)
    day = (datetime.now(UTC) - timedelta(days=20)).date()

    async with _temp_database("copilot_reprice_expired") as session_factory, session_factory() as session:
        await session.execute(text("UPDATE platform_settings SET trace_retention_days = 7"))
        await _insert_trace(session, started_at=_noon(day))
        await _insert_trace(session, started_at=_noon(day))
        await session.commit()
        assert await aggregate_daily_stats(session, target_date=day) == 1
        # The purge got half-way through the day; what is left has no cost.
        await session.execute(
            text("DELETE FROM llm_traces WHERE id = (SELECT id FROM llm_traces LIMIT 1)")
        )
        await session.execute(text("UPDATE llm_traces SET total_cost = NULL"))
        await session.commit()

        result = await repricing.reprice_null_costs(session)

        assert result.traces_updated == 1
        assert result.days_reaggregated == 0
        counts = (
            await session.execute(
                text("SELECT trace_count FROM llm_daily_stats WHERE date = :day"), {"day": day}
            )
        ).scalars().all()
        assert counts == [2]
        assert await _stale_days(session) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_reprice_batch_locks_no_trace_before_the_rollup_lock(monkeypatch):
    """Two reprices over the same NULL-cost traces could
    deadlock. A price-cache miss part-way through a batch autoflushed the costs
    already set, so the batch held trace row locks when it asked for the rollup
    lock - which a concurrent batch could hold while waiting for those rows."""
    from sqlalchemy.exc import DBAPIError

    from app.services.tracing import repricing

    _flat_price(monkeypatch, reads_through_session=True)
    noon = _noon((datetime.now(UTC) - timedelta(days=2)).date())

    async with _temp_database("copilot_reprice_lock_order") as session_factory:
        async with session_factory() as session:
            # Two models: the second one's price is a cache miss mid-batch.
            await _insert_trace(session, started_at=noon, model="model-a")
            await _insert_trace(session, started_at=noon, model="model-b")
            await session.execute(text("UPDATE llm_traces SET total_cost = NULL"))
            await session.commit()

        locked_first: list[bool] = []
        mark = repricing._mark_stale_rollups

        async def _probe_then_mark(db, *args, **kwargs):
            async with session_factory() as other:
                try:
                    await other.execute(text("SELECT id FROM llm_traces FOR UPDATE NOWAIT"))
                    locked_first.append(False)
                except DBAPIError:
                    locked_first.append(True)
                await other.rollback()
            return await mark(db, *args, **kwargs)

        monkeypatch.setattr(repricing, "_mark_stale_rollups", _probe_then_mark)
        async with session_factory() as session:
            result = await repricing.reprice_null_costs(session)

    assert result.traces_updated == 2
    assert locked_first == [False]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_reprice_route_audits_a_run_whose_day_was_rebuilt_meanwhile(monkeypatch):
    """The backfill can rebuild a day a batch dropped before the
    inline refresh reaches it, and ``aggregate_daily_stats`` then rolls back.
    On the request session that rollback expired the admin loaded for the
    request: the audit's read of ``user.id`` raised ``MissingGreenlet`` - every
    cost committed, a 500, and no audit entry."""
    from app.api.v1.settings import ModelPricingRepriceRequest, reprice_null_cost_traces
    from app.models.audit_log import AuditLog
    from app.services.tracing import repricing

    _flat_price(monkeypatch)
    day = (datetime.now(UTC) - timedelta(days=3)).date()

    async with _temp_database(
        "copilot_reprice_route", extra_tables=[AuditLog.__table__]
    ) as session_factory:
        async with session_factory() as session:
            admin = User(
                email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
                hashed_password="x",
                full_name="Reprice Admin",
                role="admin",
                is_active=True,
            )
            session.add(admin)
            await _insert_trace(session, started_at=_noon(day))
            await session.execute(text("UPDATE llm_traces SET total_cost = NULL"))
            await session.commit()
            admin_id = admin.id

        refresh = repricing._refresh_daily_stats

        async def _backfill_gets_there_first(*args, **kwargs):
            async with session_factory() as backfill:
                assert await aggregate_daily_stats(backfill, target_date=day) == 1
            return await refresh(*args, **kwargs)

        monkeypatch.setattr(repricing, "_refresh_daily_stats", _backfill_gets_there_first)

        async with session_factory() as session:
            user = await session.get(User, admin_id)
            response = await reprice_null_cost_traces(
                body=ModelPricingRepriceRequest(),
                request=SimpleNamespace(headers={}, client=None),
                user=user,
                db=session,
                _ai_gate=None,
            )

        assert response.traces_updated == 1
        assert response.days_reaggregated == 0
        async with session_factory() as session:
            summaries = (await session.execute(text("SELECT summary FROM audit_logs"))).scalars().all()
        assert summaries == ["Repriced NULL-cost traces (1 traces, 0 spans)"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_day_aggregation_rides_the_source_index():
    """``llm_traces`` has no index on ``started_at`` alone, and
    without a ``source`` predicate a day scan reads the whole of another
    composite index - once per rebuilt day, and a repricing hands up to 92
    days to this rebuild. Both the first aggregation of a day and the rebuild
    of a stale one."""
    from sqlalchemy import event

    from app.services.trace_storage import mark_daily_stats_stale, rebuild_daily_stats

    day = (datetime.now(UTC) - timedelta(days=5)).date()
    sources = "ARRAY[" + ", ".join(f"'{s}'" for s in LLM_TRACE_SOURCES) + "]"

    async with _temp_database("copilot_rollup_plan") as session_factory, session_factory() as session:
        await session.execute(
            text(
                f"""
                INSERT INTO llm_traces (
                    id, source, name, model, started_at, status, level,
                    input_tokens, output_tokens, total_cost, duration_ms
                )
                SELECT gen_random_uuid(), ({sources})[1 + (i % 7)], 'plan', 'model-' || (i % 5),
                       now() - (i % 60) * interval '1 day' - (i % 1440) * interval '1 minute',
                       'ok', 'DEFAULT', 10, 5, 0.001, 100 + (i % 900)
                FROM generate_series(1, 20000) i
                """  # noqa: S608 - sources is built from a module constant
            )
        )
        await session.commit()
        await session.execute(text("ANALYZE llm_traces"))
        await session.commit()

        async def _stale_then_rebuild():
            await mark_daily_stats_stale(session, [day])
            await session.commit()
            return await rebuild_daily_stats(session, day)

        sync_engine = session.bind.sync_engine
        for build in (lambda: aggregate_daily_stats(session, target_date=day), _stale_then_rebuild):
            captured: list[tuple[str, object]] = []

            def _capture(_conn, _cursor, statement, parameters, _context, _executemany, into=captured):
                if "FROM llm_traces t" in statement:
                    into.append((statement, parameters))

            event.listen(sync_engine, "before_cursor_execute", _capture)
            try:
                assert await build() > 0
            finally:
                event.remove(sync_engine, "before_cursor_execute", _capture)

            # The day's groups, and its latency buckets.
            assert len(captured) == 2
            conn = await session.connection()
            await conn.exec_driver_sql("SET LOCAL enable_seqscan = off")
            for statement, parameters in captured:
                plan = "\n".join(
                    row[0]
                    for row in (await conn.exec_driver_sql("EXPLAIN " + statement, parameters)).all()
                )
                index_conds = [line for line in plan.splitlines() if "Index Cond" in line]
                assert any("source" in cond and "started_at" in cond for cond in index_conds), plan
            await session.rollback()


# -- No day without its rollup -------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_reprice_never_leaves_a_repriced_day_without_its_rollup(monkeypatch):
    """Each batch deleted the rollups
    of its days, and only the newest 14 were rebuilt before the request
    answered. While a newer day's rollup kept the range open, every other
    repriced day read 0 in every rollup-served figure - traces, tokens,
    errors, latency, cost, in the window and in the one before it - until the
    backfill reached it, and even the newest 14 did until the request's own
    rebuild did. The rows now stay until the rebuild replaces them, and the
    request rebuilds the 92 days it always did: no figure reads lower than
    before, at any point, and each repriced day's cost goes from stale to new."""
    from app.services.tracing import repricing

    _flat_price(monkeypatch)
    monkeypatch.setattr(repricing, "_BATCH_SIZE", 7)
    today = datetime.now(UTC).date()
    priced = [today - timedelta(days=2)]
    unpriced = [today - timedelta(days=n) for n in (*range(10, 41), *range(95, 100))]

    async with _temp_database("copilot_reprice_no_hole") as session_factory:
        async with session_factory() as session:
            await session.execute(text("UPDATE platform_settings SET trace_retention_days = 200"))
            await session.commit()
        await _seed_repriceable_days(session_factory, priced=priced, unpriced=unpriced)
        before = await _rollup_figures(session_factory)

        refresh = repricing._refresh_daily_stats
        between: list[dict] = []

        async def _read_then_refresh(*args, **kwargs):
            # Every batch has committed its costs; the rebuild has not started.
            between.append(await _rollup_figures(session_factory))
            return await refresh(*args, **kwargs)

        monkeypatch.setattr(repricing, "_refresh_daily_stats", _read_then_refresh)
        async with session_factory() as session:
            result = await repricing.reprice_null_costs(session)
        after = await _rollup_figures(session_factory)
        async with session_factory() as session:
            recorded = await _stale_days(session)

    assert result.traces_updated == 3 * len(unpriced)
    assert result.days_reaggregated == len(unpriced)
    assert len(before["count"]) == 32 and before["current"]["trace_count"] == 96
    assert before["previous"]["trace_count"] == 15
    assert between == [before]
    assert after["count"] == before["count"]
    assert after["current"] == before["current"]
    assert after["previous"] == before["previous"]
    # 3 traces of 5 + 7 tokens at $1 per token; the priced day is untouched.
    assert after["cost"] == {
        **before["cost"],
        **{str(day): 36.0 for day in unpriced if day > today - timedelta(days=90)},
    }
    assert recorded == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_days_past_the_inline_bound_keep_their_rollup_until_the_backfill_rebuilds_them(monkeypatch):
    """A reprice reaching further back than the request rebuilds (a retention
    over 92 days) leaves the older days' stale rollups readable and recorded;
    the backfill rebuilds them, a bounded number per run, until none is left."""
    from app.services.trace_storage import run_daily_stats_backfill
    from app.services.tracing import repricing

    _flat_price(monkeypatch)
    monkeypatch.setattr(repricing, "_MAX_INLINE_REBUILD_DAYS", 3)
    today = datetime.now(UTC).date()
    priced = [today - timedelta(days=2)]
    unpriced = [today - timedelta(days=n) for n in range(10, 20)]
    newest = sorted(unpriced, reverse=True)[:3]

    async with _temp_database("copilot_reprice_past_bound") as session_factory:
        async with session_factory() as session:
            await session.execute(text("UPDATE platform_settings SET trace_retention_days = 200"))
            await session.commit()
        await _seed_repriceable_days(session_factory, priced=priced, unpriced=unpriced)
        before = await _rollup_figures(session_factory)

        async with session_factory() as session:
            result = await repricing.reprice_null_costs(session)
        answered = await _rollup_figures(session_factory)
        async with session_factory() as session:
            recorded = await _stale_days(session)

        runs = []
        while True:
            async with session_factory() as session:
                if not await _stale_days(session):
                    break
                await run_daily_stats_backfill(session, retention_days=200, max_days=2)
            runs.append(await _rollup_figures(session_factory))
            assert len(runs) < 10
        converged = await _rollup_figures(session_factory)

    assert result.days_reaggregated == 3
    assert answered["count"] == before["count"]
    assert answered["current"] == before["current"]
    assert answered["cost"] == {**before["cost"], **{str(day): 36.0 for day in newest}}
    assert recorded == sorted(set(unpriced) - set(newest))
    # 7 recorded days, 2 per run - and no run reads a figure lower than before.
    assert len(runs) == 4
    assert all(run["count"] == before["count"] and run["current"] == before["current"] for run in runs)
    assert converged["cost"] == {**before["cost"], **{str(day): 36.0 for day in unpriced}}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_reprice_that_dies_part_way_keeps_every_rollup_and_converges(monkeypatch):
    """A failure after some batches committed: their days keep their
    stale rows, readable, and are recorded; the backfill rebuilds them from
    the costs that were committed, and the next reprice finishes the rest -
    the ``total_cost IS NULL`` keyset resumes where the dead one stopped."""
    from app.services.trace_storage import run_daily_stats_backfill
    from app.services.tracing import repricing

    _flat_price(monkeypatch)
    monkeypatch.setattr(repricing, "_BATCH_SIZE", 4)
    today = datetime.now(UTC).date()
    priced = [today - timedelta(days=2)]
    unpriced = [today - timedelta(days=n) for n in range(10, 14)]

    resolve = repricing._PriceMemo.resolve
    calls = {"n": 0}

    async def _dies_in_the_second_batch(self, model, provider):
        calls["n"] += 1
        if calls["n"] == 5:
            raise RuntimeError("database went away")
        return await resolve(self, model, provider)

    async with _temp_database("copilot_reprice_crash") as session_factory:
        await _seed_repriceable_days(session_factory, priced=priced, unpriced=unpriced)
        before = await _rollup_figures(session_factory)

        monkeypatch.setattr(repricing._PriceMemo, "resolve", _dies_in_the_second_batch)
        async with session_factory() as session:
            with pytest.raises(repricing.RepriceInterruptedError) as excinfo:
                await repricing.reprice_null_costs(session)
            await session.rollback()
        monkeypatch.setattr(repricing._PriceMemo, "resolve", resolve)
        died = await _rollup_figures(session_factory)
        async with session_factory() as session:
            recorded = await _stale_days(session)
            repriced_days = sorted(
                (
                    await session.execute(
                        text(
                            "SELECT DISTINCT (started_at AT TIME ZONE 'UTC')::date "
                            "FROM llm_traces WHERE total_cost = 12"
                        )
                    )
                ).scalars()
            )

        async with session_factory() as session:
            await run_daily_stats_backfill(session, retention_days=90)
        async with session_factory() as session:
            left_by_backfill = await _stale_days(session)
            result = await repricing.reprice_null_costs(session)
        converged = await _rollup_figures(session_factory)

    assert excinfo.value.result.traces_updated == 4
    assert died == before
    assert recorded == repriced_days
    assert left_by_backfill == []
    assert result.traces_updated == 3 * len(unpriced) - 4
    assert converged["count"] == before["count"]
    assert converged["current"] == before["current"]
    assert converged["cost"] == {**before["cost"], **{str(day): 36.0 for day in unpriced}}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_failed_rebuild_keeps_the_stale_rollup_and_its_record(monkeypatch):
    """A rebuild that fails rolls its delete back with it: the stale rows stay
    readable, the record stays, and the next run rebuilds the day."""
    from app.services import trace_storage

    day = (datetime.now(UTC) - timedelta(days=5)).date()
    async with _temp_database("copilot_rebuild_fails") as session_factory:
        await _seed_repriceable_days(session_factory, priced=[], unpriced=[day])
        async with session_factory() as session:
            await session.execute(text("UPDATE llm_traces SET total_cost = 12"))
            await trace_storage.mark_daily_stats_stale(session, [day])
            await session.commit()
        before = await _rollup_figures(session_factory)

        build = trace_storage._day_rollup_rows

        async def _statement_timeout(*_args, **_kwargs):
            raise RuntimeError("canceling statement due to statement timeout")

        monkeypatch.setattr(trace_storage, "_day_rollup_rows", _statement_timeout)
        async with session_factory() as session:
            assert await trace_storage.rebuild_daily_stats(session, day) == 0
            still_recorded = await _stale_days(session)
        failed = await _rollup_figures(session_factory)

        monkeypatch.setattr(trace_storage, "_day_rollup_rows", build)
        async with session_factory() as session:
            assert await trace_storage.rebuild_daily_stats(session, day) == 1
            recorded_after = await _stale_days(session)
        rebuilt = await _rollup_figures(session_factory)

    assert failed == before
    assert still_recorded == [day]
    assert recorded_after == []
    assert rebuilt["count"] == before["count"]
    assert rebuilt["cost"] == {str(day): 36.0}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_rebuild_with_no_trace_left_keeps_the_rollup():
    """Nothing to rebuild from (every trace of the day deleted since): the day
    keeps its rollup rather than losing it, and the record goes."""
    from app.services import trace_storage

    day = (datetime.now(UTC) - timedelta(days=5)).date()
    async with _temp_database("copilot_rebuild_empty") as session_factory:
        await _seed_repriceable_days(session_factory, priced=[day], unpriced=[])
        async with session_factory() as session:
            await trace_storage.mark_daily_stats_stale(session, [day])
            await session.execute(text("DELETE FROM llm_traces"))
            await session.commit()
        before = await _rollup_figures(session_factory)
        async with session_factory() as session:
            assert await trace_storage.rebuild_daily_stats(session, day) == 0
            recorded = await _stale_days(session)
        after = await _rollup_figures(session_factory)

    assert before["count"] == {str(day): 3}
    assert after == before
    assert recorded == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_purge_keeps_a_stale_days_traces_until_the_day_is_rebuilt(monkeypatch):
    """An expiring day recorded stale is rebuilt from all of its traces before
    they go. While its rebuild keeps failing the traces are kept - loudly, like
    a day whose aggregation fails - instead of leaving a stale rollup forever."""
    from app.services import trace_storage

    retention = 7
    day = (datetime.now(UTC) - timedelta(days=retention + 3)).date()

    async def _rollup(session):
        return (
            await session.execute(
                text("SELECT trace_count, total_cost FROM llm_daily_stats WHERE date = :day"),
                {"day": day},
            )
        ).all()

    async def _traces(session):
        return (await session.execute(text("SELECT count(*) FROM llm_traces"))).scalar_one()

    async with _temp_database("copilot_purge_stale") as session_factory:
        await _seed_repriceable_days(session_factory, priced=[], unpriced=[day])
        async with session_factory() as session:
            await session.execute(text("UPDATE llm_traces SET total_cost = 12"))
            await trace_storage.mark_daily_stats_stale(session, [day])
            await session.commit()

        build = trace_storage._day_rollup_rows

        async def _statement_timeout(*_args, **_kwargs):
            raise RuntimeError("canceling statement due to statement timeout")

        monkeypatch.setattr(trace_storage, "_day_rollup_rows", _statement_timeout)
        async with session_factory() as session:
            kept = await purge_expired_traces(session, retention_days=retention)
            kept_traces, kept_rollup, kept_record = await _traces(session), await _rollup(session), await _stale_days(session)

        monkeypatch.setattr(trace_storage, "_day_rollup_rows", build)
        async with session_factory() as session:
            purged = await purge_expired_traces(session, retention_days=retention)
            left, rollup, record = await _traces(session), await _rollup(session), await _stale_days(session)

    assert kept == 0
    assert kept_traces == 3
    assert [(count, float(cost)) for count, cost in kept_rollup] == [(3, 0.0)]
    assert kept_record == [day]
    assert purged == 3
    assert left == 0
    assert [(count, float(cost)) for count, cost in rollup] == [(3, 36.0)]
    assert record == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_concurrent_reprices_converge_without_double_counting(monkeypatch):
    """Two admins reprice at once: neither deadlocks the other, both record and
    rebuild under the rollup lock, and every rebuilt day counts each trace
    once, at its new cost."""
    import asyncio

    from app.services.tracing import repricing

    _flat_price(monkeypatch, reads_through_session=True)
    monkeypatch.setattr(repricing, "_BATCH_SIZE", 5)
    today = datetime.now(UTC).date()
    unpriced = [today - timedelta(days=n) for n in range(3, 11)]

    async with _temp_database("copilot_reprice_concurrent") as session_factory:
        await _seed_repriceable_days(session_factory, priced=[], unpriced=unpriced)
        before = await _rollup_figures(session_factory)

        async def _reprice():
            async with session_factory() as session:
                return await repricing.reprice_null_costs(session)

        first, second = await asyncio.gather(_reprice(), _reprice())
        after = await _rollup_figures(session_factory)
        async with session_factory() as session:
            recorded = await _stale_days(session)

    assert first.traces_updated + second.traces_updated >= 3 * len(unpriced)
    assert after["count"] == before["count"]
    assert after["current"] == before["current"]
    assert after["cost"] == {str(day): 36.0 for day in unpriced}
    assert recorded == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_rollup_rebuilt_before_its_day_is_complete_stays_recorded():
    """Before 01:00 UTC yesterday's writes may still be landing, and the rollup
    is insert-only. A day without a rollup is not built that early (the read
    path serves it from its traces); one that already has a stale rollup is
    rebuilt at once - its new cost is not held back - and stays recorded, so
    the backfill rebuilds it once more when it is complete."""
    from datetime import time

    from app.services import trace_storage

    today = datetime.now(UTC).date()
    yesterday = today - timedelta(days=1)
    before_one = datetime.combine(today, time(0, 30), tzinfo=UTC)
    after_one = datetime.combine(today, time(1, 30), tzinfo=UTC)

    async with _temp_database("copilot_rebuild_incomplete") as session_factory, session_factory() as session:
        await _insert_trace(session, started_at=_noon(yesterday))
        await session.execute(text("UPDATE llm_traces SET total_cost = NULL"))
        await session.commit()

        built_early = await trace_storage.rebuild_daily_stats(
            session, yesterday, now=before_one, build_missing=True
        )
        rows_before_one = (await session.execute(text("SELECT count(*) FROM llm_daily_stats"))).scalar_one()

        # Built early anyway - an admin's manual refresh, an older release.
        assert await aggregate_daily_stats(session, target_date=yesterday) == 1
        await session.execute(text("UPDATE llm_traces SET total_cost = 12"))
        await trace_storage.mark_daily_stats_stale(session, [yesterday])
        await session.commit()
        rebuilt_early = await trace_storage.rebuild_daily_stats(session, yesterday, now=before_one)
        recorded_early = await _stale_days(session)

        # A late write lands; the day is complete an hour later.
        await _insert_trace(session, started_at=_noon(yesterday) + timedelta(hours=11))
        await session.commit()
        rebuilt_complete = await trace_storage.rebuild_daily_stats(session, yesterday, now=after_one)
        recorded_complete = await _stale_days(session)
        rollup = (await session.execute(text("SELECT trace_count, total_cost FROM llm_daily_stats"))).all()

    assert built_early == 0
    assert rows_before_one == 0
    assert rebuilt_early == 1
    assert recorded_early == [yesterday]
    assert rebuilt_complete == 1
    assert recorded_complete == []
    assert [(count, float(cost)) for count, cost in rollup] == [(2, 12.002)]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_inline_refresh_of_92_days_is_bounded_by_the_days_it_rebuilds():
    """The request rebuilds up to 92 days inline - the bound it always had.
    Each day is two range scans of that day's traces on (source, started_at)
    (see the plan test above), not a pass over the table, so the request's
    rebuild grows with the 92 days' traces and nothing else. Measured here at
    500 traces a day over 92 days."""
    import time as time_module

    from app.services.tracing import repricing

    per_day, days = 500, repricing._MAX_INLINE_REBUILD_DAYS
    today = datetime.now(UTC).date()
    # Ends the day before yesterday: complete whatever the time of day.
    first_day = today - timedelta(days=days + 1)
    sources = "ARRAY[" + ", ".join(f"'{s}'" for s in LLM_TRACE_SOURCES) + "]"

    async with _temp_database("copilot_reprice_bounded") as session_factory:
        async with session_factory() as session:
            await session.execute(
                text(
                    f"""
                    INSERT INTO llm_traces (
                        id, source, name, model, started_at, status, level,
                        input_tokens, output_tokens, total_cost, duration_ms
                    )
                    SELECT gen_random_uuid(), ({sources})[1 + (i % 7)], 'bounded', 'model-' || (i % 5),
                           CAST(:first_noon AS timestamptz) + (i % :days) * interval '1 day'
                               + (i % 600) * interval '1 second',
                           'ok', 'DEFAULT', 10, 5, NULL, 100 + (i % 900)
                    FROM generate_series(1, :total) i
                    """  # noqa: S608 - sources is built from a module constant
                ),
                {"first_noon": _noon(first_day), "days": days, "total": per_day * days},
            )
            await session.commit()
            await session.execute(text("ANALYZE llm_traces"))
            await session.commit()
            day_list = [first_day + timedelta(days=n) for n in range(days)]
            for day in day_list:
                assert await aggregate_daily_stats(session, target_date=day) > 0
            # The repricing committed its costs and recorded every day stale.
            await session.execute(text("UPDATE llm_traces SET total_cost = 0.001"))
            await session.execute(
                text("INSERT INTO llm_daily_stats_stale_days (date) SELECT DISTINCT date FROM llm_daily_stats")
            )
            await session.commit()

        window = repricing._RollupWindow(expired_before=today - timedelta(days=200), today=today)
        started = time_module.perf_counter()
        rebuilt = await repricing._refresh_daily_stats(set(day_list), window)
        elapsed = time_module.perf_counter() - started

        async with session_factory() as session:
            recorded = await _stale_days(session)
            totals = (
                await session.execute(
                    text("SELECT SUM(trace_count), ROUND(SUM(total_cost), 3) FROM llm_daily_stats")
                )
            ).one()

    logging.getLogger(__name__).warning(
        "inline refresh of %d days x %d traces: %.2fs", days, per_day, elapsed
    )
    assert rebuilt == days
    assert recorded == []
    assert (int(totals[0]), float(totals[1])) == (per_day * days, round(0.001 * per_day * days, 3))
    # Generous: a regression to a pass over the table per day is what this catches.
    assert elapsed < 60, elapsed
