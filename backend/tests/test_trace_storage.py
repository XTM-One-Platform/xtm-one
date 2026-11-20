from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.services import trace_storage
from app.services.trace_storage import (
    _PURGE_BATCH_SIZE,
    _PURGE_RESCUE_MAX_DAYS_PER_RUN,
    aggregate_daily_stats,
    purge_cutoff,
    purge_expired_traces,
    run_daily_stats_backfill,
)


class _RowcountResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _IdRows:
    """Batch of trace ids, as returned by the purge's batch-selection query."""

    def __init__(self, count: int) -> None:
        self._rows = [(uuid.uuid4(),) for _ in range(count)]

    def all(self) -> list[tuple[uuid.UUID]]:
        return list(self._rows)


class _ObjectNameRows:
    """Offloaded payload object names owned by a batch of traces."""

    def __init__(self, names: list[str]) -> None:
        self._rows = [(n,) for n in names]

    def all(self) -> list[tuple[str]]:
        return list(self._rows)


class _ScalarResult:
    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value

    def scalar_one_or_none(self) -> int | None:
        return self._value


class _MappingsResult:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def mappings(self) -> _MappingsResult:
        return self

    def all(self) -> list[dict]:
        return list(self._rows)


class _FakeDB:
    def __init__(self, results: list[object]) -> None:
        self._results = list(results)
        self.calls: list[tuple[object, object]] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, stmt, params=None):
        self.calls.append((stmt, params))
        if not self._results:
            raise AssertionError("Unexpected execute call")
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_purge_expired_traces_deletes_in_batches(monkeypatch):
    db = _FakeDB(
        results=[
            _DateRows([]),  # rescue probe: nothing expiring without a rollup
            _DateRows([]),  # nothing expiring recorded stale
            # Each batch: select the ids, resolve their offloaded payload and
            # media objects, resolve which of them still need a prefix sweep,
            # then delete exactly those ids.
            _IdRows(_PURGE_BATCH_SIZE),
            _ObjectNameRows([]),
            _ObjectNameRows([]),
            _RowcountResult(_PURGE_BATCH_SIZE),
            _IdRows(250),
            _ObjectNameRows([]),
            _ObjectNameRows([]),
            _RowcountResult(250),
            _IdRows(0),
        ]
    )
    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr("app.services.trace_storage.asyncio.sleep", _fake_sleep)

    deleted = await purge_expired_traces(db, retention_days=90)

    assert deleted == _PURGE_BATCH_SIZE + 250
    assert db.commits == 2
    assert sleep_calls == [0.1]
    batch_sql = " ".join(str(db.calls[2][0]).lower().split())
    assert "select t.id" in batch_sql
    assert "from llm_traces" in batch_sql
    delete_sql = " ".join(str(db.calls[5][0]).lower().split())
    assert "delete from llm_traces" in delete_sql
    # Deleting by resolved id, not by re-running the selection: the object names
    # collected above must correspond to exactly the rows that go.
    assert "where id = any(:ids)" in delete_sql


@pytest.mark.asyncio
async def test_offloaded_object_names_walks_nested_media_references():
    """Media references are nested; the envelope marker alone does not find them.

    A payload envelope replaces a whole field and so sits at the top level, but a
    media reference replaces one blob *inside* the payload — under
    ``messages[].content[].image_url.url``, for instance.  Testing only the top
    level, as the query first did, orphaned every media object in the bucket.
    The recursive descent is verified against real Postgres semantics; this pins
    that it stays in the query.
    """
    db = _FakeDB(results=[_ObjectNameRows(["traces/t/s/media/a.png"])])

    names = await trace_storage._offloaded_object_names(db, [uuid.uuid4()])

    assert names == ["traces/t/s/media/a.png"]
    sql = " ".join(str(db.calls[0][0]).lower().split())
    # Recursive descent over the payload, not a top-level key test.
    assert "jsonb_path_query" in sql
    assert "'$.**'" in sql
    # Both markers, so payload envelopes and media references are both collected.
    assert ":marker" in sql
    assert ":media_marker" in sql
    assert db.calls[0][1]["marker"] == trace_storage.ENVELOPE_KEY
    assert db.calls[0][1]["media_marker"] == trace_storage.MEDIA_KEY


@pytest.mark.asyncio
async def test_offloaded_object_names_survives_a_query_failure():
    """Retaining an orphaned object is cheaper than retaining an expired trace."""
    db = _FakeDB(results=[RuntimeError("jsonb blew up")])

    assert await trace_storage._offloaded_object_names(db, [uuid.uuid4()]) == []


@pytest.mark.asyncio
async def test_object_sweep_is_limited_to_traces_carrying_an_envelope():
    """Every purged trace must not cost an object-storage LIST round-trip.

    Only a trace whose payload was itself offloaded can hide media inside the
    stored object, and those are the minority; the rest are fully enumerable
    from their rows.
    """
    trace_id = uuid.uuid4()
    db = _FakeDB(results=[_ObjectNameRows([str(trace_id)])])

    ids = await trace_storage._traces_needing_object_sweep(db, [trace_id])

    assert ids == [str(trace_id)]
    sql = " ".join(str(db.calls[0][0]).lower().split())
    assert "jsonb_exists(p.payload, :marker)" in sql
    assert "jsonb_typeof(p.payload) = 'object'" in sql
    # Deliberately NOT a recursive walk: this is the cheap top-level test.
    assert "jsonb_path_query" not in sql


@pytest.mark.asyncio
async def test_postgres_purge_skips_both_object_queries_under_clickhouse(monkeypatch):
    """Under ClickHouse the objects belong to that purge — including the sweep.

    Traces are mirrored to both stores and a payload object has one copy both
    rows point at, so sweeping the trace prefix here would delete media out from
    under the ClickHouse rows still serving it. The name query was already gated;
    the prefix sweep has to sit behind the same gate or it reintroduces the bug.
    """
    monkeypatch.setattr("app.services.tracing.store.get_trace_store_provider", lambda: "clickhouse")
    db = _FakeDB(
        results=[
            _DateRows([]),  # rescue probe
            _DateRows([]),  # stale expiring days
            _IdRows(3),  # the batch
            # No object-name query and no sweep query: the next statement is the
            # DELETE itself. A stray query here would raise "Unexpected execute".
            _RowcountResult(3),
            _IdRows(0),
        ]
    )
    swept: list[list] = []

    async def _fake_sweep(trace_ids):
        swept.append(list(trace_ids))
        return 0

    monkeypatch.setattr("app.services.trace_storage.delete_trace_objects", _fake_sweep)

    assert await purge_expired_traces(db, retention_days=90) == 3
    assert swept == []
    assert not any("jsonb_path_query" in str(stmt) for stmt, _ in db.calls)


def test_purge_cutoff_is_a_whole_day_boundary():
    # Cutting on the instant (now - retention) would truncate the oldest day in
    # the window mid-day, and the rollup built from that day - which is never
    # rewritten - would under-report forever. Midnight means a day is either
    # entirely kept or entirely expired.
    now = datetime(2026, 6, 16, 13, 47, 3, tzinfo=UTC)

    cutoff = purge_cutoff(7, now=now)

    assert cutoff == datetime(2026, 6, 9, 0, 0, tzinfo=UTC)
    # The oldest day the backfill aggregates (today - retention) is fully retained.
    assert cutoff.date() == now.date() - timedelta(days=7)


@pytest.mark.asyncio
async def test_purge_only_deletes_days_that_have_a_rollup():
    db = _FakeDB(results=[_DateRows([]), _DateRows([]), _IdRows(0)])

    await purge_expired_traces(db, retention_days=30)

    batch_sql = " ".join(str(db.calls[2][0]).lower().split())
    # The guard is the whole point: the rollup is the only thing that outlives
    # this purge, so a day without one must not be selected for deletion.
    assert "exists" in batch_sql
    assert "from llm_daily_stats s" in batch_sql
    assert "s.date = (t.started_at at time zone 'utc')::date" in batch_sql


@pytest.mark.asyncio
async def test_purge_keeps_the_traces_of_a_day_recorded_stale():
    """A stale rollup is rebuilt from the day's traces: the purge must
    not delete them before that rebuild, or the day keeps its stale rollup
    forever - or, rebuilt from what is left, under-reports for good."""
    db = _FakeDB(results=[_DateRows([]), _DateRows([]), _IdRows(0)])

    await purge_expired_traces(db, retention_days=30)

    batch_sql = " ".join(str(db.calls[2][0]).lower().split())
    assert "not exists" in batch_sql
    assert "from llm_daily_stats_stale_days p" in batch_sql
    assert "p.date = (t.started_at at time zone 'utc')::date" in batch_sql


@pytest.mark.asyncio
async def test_purge_rebuilds_the_stale_expiring_days_first(monkeypatch):
    """The last moment an expiring stale day can be rebuilt from all of its
    traces: oldest first, bounded like the rescue, with the guard that never
    lets a rebuild replace the rollup with fewer traces than it counts."""
    now = datetime.now(UTC)
    cutoff = purge_cutoff(7, now=now).date()
    stale = [cutoff - timedelta(days=3), cutoff - timedelta(days=1)]
    db = _FakeDB(results=[_DateRows([]), _DateRows(stale), _IdRows(0)])
    rebuilt: list[tuple[date, date | None]] = []

    async def _fake_rebuild(_db, target, *, now=None, expired_before=None, build_missing=False):
        rebuilt.append((target, expired_before))
        return 1

    monkeypatch.setattr(trace_storage, "rebuild_daily_stats", _fake_rebuild)

    await purge_expired_traces(db, retention_days=7)

    assert rebuilt == [(day, cutoff) for day in stale]
    probe_sql = " ".join(str(db.calls[1][0]).lower().split())
    assert "from llm_daily_stats_stale_days" in probe_sql
    assert "order by date asc" in probe_sql
    assert db.calls[1][1]["last"] == cutoff - timedelta(days=1)
    assert db.calls[1][1]["limit"] == _PURGE_RESCUE_MAX_DAYS_PER_RUN


@pytest.mark.asyncio
async def test_purge_rescues_expiring_days_missing_a_rollup(monkeypatch):
    # A day that leaves the retention window before it was ever aggregated (long
    # downtime, or an admin lowering the retention period) used to be deleted with
    # its KPIs never built. The purge now aggregates it first.
    expiring = [date(2026, 5, 30), date(2026, 5, 31)]
    db = _FakeDB(results=[_DateRows(expiring), _DateRows([]), _IdRows(0)])
    aggregated: list[date] = []

    async def _fake_aggregate(_db, target_date=None):
        aggregated.append(target_date)
        return 2

    monkeypatch.setattr(trace_storage, "aggregate_daily_stats", _fake_aggregate)

    await purge_expired_traces(db, retention_days=7)

    assert aggregated == expiring
    probe_sql = " ".join(str(db.calls[0][0]).lower().split())
    assert "not exists" in probe_sql
    # Oldest first and bounded, so a 365 -> 7 retention change catches up over
    # several runs instead of aggregating hundreds of days in one tick.
    assert "order by trace_date" in probe_sql
    assert db.calls[0][1]["max_days"] == _PURGE_RESCUE_MAX_DAYS_PER_RUN


@pytest.mark.asyncio
async def test_purge_expired_traces_swallows_errors():
    db = _FakeDB(results=[RuntimeError("boom")])

    deleted = await purge_expired_traces(db, retention_days=30)

    assert deleted == 0
    assert db.rollbacks == 1


@pytest.mark.asyncio
async def test_aggregate_daily_stats_inserts_rows_and_updates_last_aggregation_at():
    user_id = uuid.uuid4()
    department_id = uuid.uuid4()
    # The grain past the day: who the usage counts for and its department.
    grain = {
        "agent_id": None,
        "user_id": user_id,
        "model": "gpt-5.3-codex",
        "source": "agent-chat",
        "attributed_user_id": user_id,
        "attribution": "user",
        "department_id": department_id,
    }
    db = _FakeDB(
        results=[
            _ScalarResult(1),  # pg_advisory_xact_lock
            _ScalarResult(None),
            _MappingsResult(
                [
                    {
                        **grain,
                        "trace_count": 10,
                        "error_count": 2,
                        "total_input_tokens": 1000,
                        "total_output_tokens": 500,
                        "total_cached_input_tokens": 200,
                        "total_cost": Decimal("1.23000000"),
                        "avg_duration_ms": 220.5,
                        "p50_duration_ms": 200.0,
                        "p95_duration_ms": 480.0,
                        "avg_feedback_score": 0.8,
                        "department_backfilled": False,
                        "agent_name_snapshot": None,
                    }
                ]
            ),
            _MappingsResult(
                [
                    {**grain, "bucket": 31, "bucket_count": 7},
                    {**grain, "bucket": 36, "bucket_count": 3},
                ]
            ),
            _RowcountResult(1),
            _RowcountResult(1),
        ]
    )

    inserted = await aggregate_daily_stats(db, target_date=date(2026, 6, 15))

    assert inserted == 1
    assert db.commits == 1
    lock_sql = str(db.calls[0][0]).lower()
    assert "pg_advisory_xact_lock" in lock_sql
    aggregate_sql = str(db.calls[2][0]).lower()
    assert "coalesce" in aggregate_sql
    assert "filter (where" in aggregate_sql
    # user_id is part of the grouping key so the rollup can answer the same
    # question as the live path for a non-admin viewer.
    assert "group by t.agent_id, t.user_id" in " ".join(aggregate_sql.split())
    histogram_sql = str(db.calls[3][0]).lower()
    assert "width_bucket" in histogram_sql
    # The bucket counts land on the matching row, keyed the same way.
    payload = db.calls[4][1]
    assert payload[0]["user_id"] == user_id
    assert payload[0]["duration_histogram"] == {"v": 1, "b": {"31": 7, "36": 3}}
    assert payload[0]["department_id"] == department_id
    assert payload[0]["total_cached_input_tokens"] == 200
    update_sql = str(db.calls[-1][0]).lower()
    assert "update platform_settings" in update_sql
    assert "last_aggregation_at" in update_sql


@pytest.mark.asyncio
async def test_aggregate_daily_stats_is_idempotent():
    db = _FakeDB(results=[_ScalarResult(1), _ScalarResult(1)])

    inserted = await aggregate_daily_stats(db, target_date=date(2026, 6, 15))

    assert inserted == 0
    assert db.commits == 0
    # Idempotent short-circuit must still roll back to release the txn-scoped
    # advisory lock (returning with the transaction open would pin the lock).
    assert db.rollbacks == 1
    # advisory lock + existence check, then short-circuit (no aggregate/insert).
    assert len(db.calls) == 2


@pytest.mark.asyncio
async def test_aggregate_daily_stats_handles_empty_day():
    db = _FakeDB(
        results=[
            _ScalarResult(1),  # pg_advisory_xact_lock
            _ScalarResult(None),
            _MappingsResult([]),
            _RowcountResult(1),
        ]
    )

    inserted = await aggregate_daily_stats(db, target_date=date(2026, 6, 15))

    assert inserted == 0
    assert db.commits == 1
    # advisory lock + existence check + aggregate select + settings update. The
    # histogram scan is skipped entirely: with no groups there is no row to
    # attach buckets to, and the backfill sweeps plenty of idle days.
    assert len(db.calls) == 4


@pytest.mark.asyncio
async def test_aggregate_daily_stats_swallows_errors():
    db = _FakeDB(results=[RuntimeError("aggregation failed")])

    inserted = await aggregate_daily_stats(db, target_date=date(2026, 6, 15))

    assert inserted == 0
    assert db.rollbacks == 1


class _DateRows:
    def __init__(self, dates: list[date]) -> None:
        self._rows = [(d,) for d in dates]

    def all(self) -> list[tuple[date]]:
        return list(self._rows)


@pytest.fixture(autouse=True)
def _feedback_refresh_calls(monkeypatch) -> list[tuple[date, date]]:
    """The backfill's feedback refresh is covered in test_feedback_scores*."""
    calls: list[tuple[date, date]] = []

    async def _refresh(_db, *, earliest, latest):
        calls.append((earliest, latest))
        return 0

    monkeypatch.setattr(trace_storage, "refresh_rollup_feedback", _refresh)
    return calls


class _Scalar:
    def __init__(self, value) -> None:
        self._value = value

    def scalar(self):
        return self._value


class _BackfillFakeDB:
    """Answers the backfill's DISTINCT-date and stale-day queries; UPDATE/other
    are no-ops."""

    def __init__(self, existing: list[date], stale: list[date] | None = None) -> None:
        self._existing = existing
        self._stale = stale or []
        self.stale_queries: list[dict] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, stmt, params=None):
        sql = str(stmt).upper()
        if "LLM_DAILY_STATS_STALE_DAYS" in sql:
            self.stale_queries.append(dict(params or {}))
            return _DateRows(self._stale)
        if "DISTINCT DATE" in sql:
            return _DateRows(self._existing)
        if "EXISTS" in sql:
            # ``_day_has_traces``: every day has traces unless a test says
            # otherwise by patching the probe.
            return _Scalar(True)
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_run_daily_stats_backfill_aggregates_missing_days_newest_first(monkeypatch):
    # now is well into the day, so latest aggregatable day is yesterday (06-15).
    now = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)
    # retention 5 -> window 06-11..06-15; only 06-13 already aggregated.
    db = _BackfillFakeDB(existing=[date(2026, 6, 13)])
    aggregated: list[date] = []

    async def _fake_aggregate(_db, target_date=None):
        aggregated.append(target_date)
        return 1

    monkeypatch.setattr(trace_storage, "aggregate_daily_stats", _fake_aggregate)

    total = await run_daily_stats_backfill(db, retention_days=5, now=now, max_days=10)

    assert total == 4
    # Newest-first, skipping the already-aggregated 06-13.
    assert aggregated == [
        date(2026, 6, 15),
        date(2026, 6, 14),
        date(2026, 6, 12),
        date(2026, 6, 11),
    ]


@pytest.mark.asyncio
async def test_run_daily_stats_backfill_is_bounded_per_run(monkeypatch):
    now = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)
    db = _BackfillFakeDB(existing=[])
    aggregated: list[date] = []

    async def _fake_aggregate(_db, target_date=None):
        aggregated.append(target_date)
        return 1

    monkeypatch.setattr(trace_storage, "aggregate_daily_stats", _fake_aggregate)

    total = await run_daily_stats_backfill(db, retention_days=90, now=now, max_days=3)

    assert total == 3
    assert aggregated == [date(2026, 6, 15), date(2026, 6, 14), date(2026, 6, 13)]


@pytest.mark.asyncio
async def test_run_daily_stats_backfill_touches_timestamp_when_caught_up(monkeypatch, _feedback_refresh_calls):
    now = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)
    # retention 2 -> window 06-14..06-15; both already aggregated -> nothing due.
    db = _BackfillFakeDB(existing=[date(2026, 6, 14), date(2026, 6, 15)])

    async def _fake_aggregate(_db, target_date=None):
        raise AssertionError("must not aggregate when already caught up")

    monkeypatch.setattr(trace_storage, "aggregate_daily_stats", _fake_aggregate)

    total = await run_daily_stats_backfill(db, retention_days=2, now=now)

    assert total == 0
    # last_aggregation_at refreshed (so the staleness banner clears) -> one commit.
    assert db.commits == 1
    # The rollups' feedback is re-read even when nothing was due, over the days
    # whose traces are all still retained (06-14 may be partly purged).
    assert _feedback_refresh_calls == [(date(2026, 6, 15), date(2026, 6, 15))]


@pytest.mark.asyncio
async def test_run_daily_stats_backfill_rebuilds_the_stale_days_first(monkeypatch):
    """The days a repricing recorded stale are rebuilt in place, newest
    first and bounded like the missing days, before those are aggregated; only
    complete days inside the retention window (an expired one is the purge's)."""
    now = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)
    stale = [date(2026, 6, 10), date(2026, 6, 2)]
    db = _BackfillFakeDB(existing=[date(2026, 6, 15), date(2026, 6, 10), date(2026, 6, 2)], stale=stale)
    order: list[tuple[str, date]] = []

    async def _fake_rebuild(_db, target, *, now=None, expired_before=None, build_missing=False):
        assert not build_missing
        order.append(("rebuild", target))
        return 3

    async def _fake_aggregate(_db, target_date=None):
        order.append(("aggregate", target_date))
        return 1

    monkeypatch.setattr(trace_storage, "rebuild_daily_stats", _fake_rebuild)
    monkeypatch.setattr(trace_storage, "aggregate_daily_stats", _fake_aggregate)

    total = await run_daily_stats_backfill(db, retention_days=30, now=now, max_days=2)

    assert order == [
        ("rebuild", date(2026, 6, 10)),
        ("rebuild", date(2026, 6, 2)),
        ("aggregate", date(2026, 6, 14)),
        ("aggregate", date(2026, 6, 13)),
    ]
    assert total == 3 + 3 + 1 + 1
    assert db.stale_queries == [{"first": date(2026, 5, 17), "last": date(2026, 6, 15), "limit": 2}]


@pytest.mark.asyncio
async def test_a_backfill_with_only_stale_days_does_not_touch_the_timestamp_again(monkeypatch):
    """A rebuild stamps ``last_aggregation_at`` in its own transaction."""
    now = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)
    db = _BackfillFakeDB(existing=[date(2026, 6, 14), date(2026, 6, 15)], stale=[date(2026, 6, 15)])

    async def _fake_rebuild(_db, target, *, now=None, expired_before=None, build_missing=False):
        return 2

    monkeypatch.setattr(trace_storage, "rebuild_daily_stats", _fake_rebuild)

    assert await run_daily_stats_backfill(db, retention_days=2, now=now) == 2
    assert db.commits == 0


@pytest.mark.asyncio
async def test_run_daily_stats_backfill_skips_days_without_traces(monkeypatch):
    """Idle days never get a rollup row, so a calendar walk re-collected them
    on every run and spent the per-run budget on nothing: an older day whose
    aggregation had failed was never reached. Only days that have
    traces are due now."""
    now = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)
    db = _BackfillFakeDB(existing=[])
    traced = {date(2026, 6, 3), date(2026, 5, 30)}

    async def _has_traces(_db, day):
        return day in traced

    aggregated: list[date] = []

    async def _fake_aggregate(_db, target_date=None):
        aggregated.append(target_date)
        return 1

    monkeypatch.setattr(trace_storage, "_day_has_traces", _has_traces)
    monkeypatch.setattr(trace_storage, "aggregate_daily_stats", _fake_aggregate)

    total = await run_daily_stats_backfill(db, retention_days=90, now=now, max_days=2)

    assert total == 2
    assert aggregated == [date(2026, 6, 3), date(2026, 5, 30)]


@pytest.mark.asyncio
async def test_a_backfill_with_only_idle_days_still_clears_the_banner(monkeypatch):
    now = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)
    db = _BackfillFakeDB(existing=[])

    async def _no_traces(_db, _day):
        return False

    async def _fake_aggregate(_db, target_date=None):
        raise AssertionError("an idle day has nothing to aggregate")

    monkeypatch.setattr(trace_storage, "_day_has_traces", _no_traces)
    monkeypatch.setattr(trace_storage, "aggregate_daily_stats", _fake_aggregate)

    assert await run_daily_stats_backfill(db, retention_days=10, now=now) == 0
    assert db.commits == 1


def test_the_trace_probe_rides_the_source_index():
    """``llm_traces`` has no index on ``started_at`` alone: the day probe must
    filter on every allowed source to use ``(source, started_at)``, and the
    CHECK constraint is built from the same tuple so the two cannot drift."""
    import inspect

    from app.models.llm_trace import LLM_TRACE_SOURCES, LLMTrace

    src = inspect.getsource(trace_storage._day_has_traces)
    assert "LLMTrace.source.in_(LLM_TRACE_SOURCES)" in src
    check = next(
        c for c in LLMTrace.__table__.constraints if c.name == "ck_llm_traces_source_valid"
    )
    assert str(check.sqltext) == (
        "source IN ('agent-chat', 'general-chat', 'assignment', 'flow', 'memory', 'evaluation', 'proxy', "
        "'platform_health')"
    )
    assert len(LLM_TRACE_SOURCES) == 8


def test_the_tracing_sink_accepts_exactly_the_stored_sources():
    """The sink kept its own copy of the list, so a source
    added to the tuple would be accepted by the store and folded to ``proxy``
    before it got there."""
    from app.models.llm_trace import LLM_TRACE_SOURCES
    from app.services.langfuse_tracing import _resolve_local_trace_source

    for source in LLM_TRACE_SOURCES:
        assert _resolve_local_trace_source("anything", source) == source
    assert _resolve_local_trace_source("anything", "not-a-source") == "proxy"


@pytest.mark.asyncio
async def test_marking_a_day_stale_records_only_days_with_a_rollup_under_the_lock():
    """The record is taken under the rollup lock, from the rollup itself (a day
    without one is the backfill's anyway), and deletes nothing."""
    db = _FakeDB(results=[None, None])

    await trace_storage.mark_daily_stats_stale(db, [date(2026, 6, 3), date(2026, 6, 1), date(2026, 6, 3)])

    assert "pg_advisory_xact_lock" in str(db.calls[0][0])
    insert_sql = " ".join(str(db.calls[1][0]).lower().split())
    assert insert_sql.startswith("insert into llm_daily_stats_stale_days (date)")
    assert "from llm_daily_stats s where s.date = any(:days)" in insert_sql
    assert "on conflict (date) do nothing" in insert_sql
    assert db.calls[1][1] == {"days": [date(2026, 6, 1), date(2026, 6, 3)]}
    assert db.commits == 0 and db.rollbacks == 0


@pytest.mark.asyncio
async def test_marking_no_day_takes_no_lock():
    db = _FakeDB(results=[])

    await trace_storage.mark_daily_stats_stale(db, [])

    assert db.calls == []
