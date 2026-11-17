"""ClickHouse-side trace retention: the sweep that enforces it, and the backstop TTL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.services.trace_storage import (
    _ch_datetime_literal,
    purge_cutoff,
    purge_expired_clickhouse_traces,
    sync_clickhouse_trace_retention,
)
from app.services.tracing.clickhouse_client import (
    TTL_BACKSTOP_MARGIN_DAYS,
    HTTPClickHouseClient,
    trace_ttl_expression,
    ttl_expiry_days,
)


def _client() -> HTTPClickHouseClient:
    return HTTPClickHouseClient(
        host="localhost",
        port=8123,
        username="test",
        password="test",
        database="traces_test",
        secure=False,
        timeout_seconds=5,
    )


@pytest.fixture(autouse=True)
def _reset_process_ttl_cache():
    """The applied-TTL memo is class state shared by every client instance."""
    HTTPClickHouseClient._ttl_applied_days.clear()
    yield
    HTTPClickHouseClient._ttl_applied_days.clear()


def _instrument(
    client: HTTPClickHouseClient,
    monkeypatch: pytest.MonkeyPatch,
    *,
    engine_full: dict[str, str] | None = None,
    system_tables_fails: bool = False,
) -> list[str]:
    """Capture the SQL a client would send, answering system.tables from a fake."""
    executed: list[str] = []
    tables = engine_full or {}

    async def _noop_schema() -> None:
        return None

    async def _execute_raw(sql: str, *, ensure_schema: bool = True, settings=None) -> str:
        _ = ensure_schema, settings
        executed.append(sql)
        return ""

    async def _query_json_rows(query: str) -> list[dict[str, object]]:
        assert "system.tables" in query
        assert "currentDatabase()" in query
        if system_tables_fails:
            raise RuntimeError("not enough privileges")
        return [{"name": name, "engine_full": value} for name, value in tables.items()]

    monkeypatch.setattr(client, "_ensure_schema_once", _noop_schema)
    monkeypatch.setattr(client, "_execute_raw", _execute_raw)
    monkeypatch.setattr(client, "query_json_rows", _query_json_rows)
    return executed


def test_ttl_expiry_is_the_purge_boundary_plus_the_backstop_margin():
    # purge_expired_traces cuts on midnight UTC of (today - retention), so a
    # trace from day D leaves PostgreSQL at the start of day D + retention + 1.
    # The TTL names that same whole-day boundary PLUS a margin, because the
    # ClickHouse sweep has to read a row's offload pointers before the row is
    # allowed to disappear - a TTL on the exact boundary would race it and orphan
    # the objects.
    assert ttl_expiry_days(90) == 90 + 1 + TTL_BACKSTOP_MARGIN_DAYS
    assert TTL_BACKSTOP_MARGIN_DAYS > 0

    assert trace_ttl_expression("started_at", 90) == (
        f"toStartOfDay(started_at) + INTERVAL {91 + TTL_BACKSTOP_MARGIN_DAYS} DAY DELETE"
    )
    assert trace_ttl_expression("created_at", 1) == (
        f"toStartOfDay(created_at) + INTERVAL {2 + TTL_BACKSTOP_MARGIN_DAYS} DAY DELETE"
    )


@pytest.mark.asyncio
async def test_ttl_is_declared_on_every_trace_table(monkeypatch: pytest.MonkeyPatch):
    client = _client()
    executed = _instrument(client, monkeypatch)

    changed = await client.apply_trace_retention_ttl(45)

    assert sorted(changed) == ["llm_scores", "llm_spans", "llm_traces"]
    expiry = ttl_expiry_days(45)
    assert executed == [
        f"ALTER TABLE llm_traces MODIFY TTL toStartOfDay(started_at) + INTERVAL {expiry} DAY DELETE",
        f"ALTER TABLE llm_spans MODIFY TTL toStartOfDay(started_at) + INTERVAL {expiry} DAY DELETE",
        # llm_scores has no started_at - it expires on its own write time.
        f"ALTER TABLE llm_scores MODIFY TTL toStartOfDay(created_at) + INTERVAL {expiry} DAY DELETE",
    ]

    await client.close()


@pytest.mark.asyncio
async def test_existing_table_without_a_ttl_is_altered(monkeypatch: pytest.MonkeyPatch):
    # The DDL is CREATE TABLE IF NOT EXISTS, so a table created before retention
    # existed is never revisited by it - reconciliation is what fixes those.
    client = _client()
    executed = _instrument(
        client,
        monkeypatch,
        engine_full={
            "llm_traces": "MergeTree ORDER BY (started_at, id) SETTINGS index_granularity = 8192",
            "llm_spans": "MergeTree ORDER BY (trace_id, position, started_at, id)",
            "llm_scores": "MergeTree ORDER BY (trace_id, created_at, id)",
        },
    )

    changed = await client.apply_trace_retention_ttl(90)

    assert sorted(changed) == ["llm_scores", "llm_spans", "llm_traces"]
    assert all(f"INTERVAL {ttl_expiry_days(90)} DAY" in sql for sql in executed)

    await client.close()


@pytest.mark.asyncio
async def test_matching_ttl_is_not_re_altered(monkeypatch: pytest.MonkeyPatch):
    # Re-issuing MODIFY TTL re-materialises the TTL across the whole table (the
    # default materialize_ttl_after_modify), so an hourly tick must not do it
    # when the declaration is already right. ClickHouse normalises the interval
    # we send into toIntervalDay(N), which is what we have to recognise.
    client = _client()
    expiry = ttl_expiry_days(90)
    executed = _instrument(
        client,
        monkeypatch,
        engine_full={
            "llm_traces": (
                "MergeTree ORDER BY (started_at, id) "
                f"TTL toStartOfDay(started_at) + toIntervalDay({expiry}) "
                "SETTINGS index_granularity = 8192"
            ),
            "llm_spans": (
                "MergeTree ORDER BY (trace_id, position, started_at, id) "
                f"TTL toStartOfDay(started_at) + toIntervalDay({expiry})"
            ),
            "llm_scores": (
                "MergeTree ORDER BY (trace_id, created_at, id) "
                f"TTL toStartOfDay(created_at) + toIntervalDay({expiry})"
            ),
        },
    )

    assert await client.apply_trace_retention_ttl(90) == []
    assert executed == []

    await client.close()


@pytest.mark.asyncio
async def test_lowering_retention_re_declares_the_ttl(monkeypatch: pytest.MonkeyPatch):
    client = _client()
    previous = ttl_expiry_days(90)
    executed = _instrument(
        client,
        monkeypatch,
        engine_full={
            "llm_traces": f"MergeTree TTL toStartOfDay(started_at) + toIntervalDay({previous})",
            "llm_spans": f"MergeTree TTL toStartOfDay(started_at) + toIntervalDay({previous})",
            "llm_scores": f"MergeTree TTL toStartOfDay(created_at) + toIntervalDay({previous})",
        },
    )

    changed = await client.apply_trace_retention_ttl(7)

    assert sorted(changed) == ["llm_scores", "llm_spans", "llm_traces"]
    assert all(f"INTERVAL {ttl_expiry_days(7)} DAY" in sql for sql in executed)

    await client.close()


@pytest.mark.asyncio
async def test_unparseable_declaration_alters_once_per_process(
    monkeypatch: pytest.MonkeyPatch,
):
    # If a normalised expression ever stops matching our pattern, the read-back
    # would report "no TTL" forever. The per-process memo bounds that to a single
    # ALTER instead of one on every maintenance tick.
    client = _client()
    executed = _instrument(
        client,
        monkeypatch,
        engine_full={"llm_traces": "MergeTree TTL some_future_normalisation(started_at)"},
    )

    first = await client.apply_trace_retention_ttl(30)
    second = await client.apply_trace_retention_ttl(30)

    assert "llm_traces" in first
    assert second == []
    assert len(executed) == 3  # one per table, from the first call only

    await client.close()


@pytest.mark.asyncio
async def test_unreadable_system_tables_still_applies_the_ttl(
    monkeypatch: pytest.MonkeyPatch,
):
    # A ClickHouse user without system.tables access must not be the difference
    # between "retention applies" and "retention silently does not apply".
    client = _client()
    executed = _instrument(client, monkeypatch, system_tables_fails=True)

    changed = await client.apply_trace_retention_ttl(90)

    assert sorted(changed) == ["llm_scores", "llm_spans", "llm_traces"]
    assert len(executed) == 3

    await client.close()


@pytest.mark.asyncio
async def test_retention_days_is_floored_at_one_day(monkeypatch: pytest.MonkeyPatch):
    client = _client()
    executed = _instrument(client, monkeypatch)

    await client.apply_trace_retention_ttl(0)

    assert all(f"INTERVAL {ttl_expiry_days(1)} DAY" in sql for sql in executed)

    await client.close()


class _RecordingClient:
    def __init__(self) -> None:
        self.applied: list[int] = []
        self.closed = False

    async def apply_trace_retention_ttl(self, retention_days: int) -> list[str]:
        self.applied.append(retention_days)
        return ["llm_traces"]

    async def close(self) -> None:
        self.closed = True


class _FailingClient(_RecordingClient):
    async def apply_trace_retention_ttl(self, retention_days: int) -> list[str]:
        self.applied.append(retention_days)
        raise RuntimeError("clickhouse unreachable")


@pytest.mark.asyncio
async def test_sync_is_a_noop_under_the_postgres_provider(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "tracing_store_provider", "postgres")
    client = _RecordingClient()

    assert await sync_clickhouse_trace_retention(90, client_factory=lambda: client) == []
    assert client.applied == []


@pytest.mark.asyncio
async def test_sync_applies_the_configured_retention(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "tracing_store_provider", "clickhouse")
    client = _RecordingClient()

    changed = await sync_clickhouse_trace_retention(30, client_factory=lambda: client)

    assert changed == ["llm_traces"]
    assert client.applied == [30]
    assert client.closed


class _SweepClient:
    """Fake ClickHouse answering the sweep's four query shapes."""

    def __init__(
        self,
        *,
        earliest: str | None = "2026-01-01 08:30:00.000",
        object_names: list[str] | None = None,
        media_trace_ids: list[str] | None = None,
        delete_fails: bool = False,
    ) -> None:
        self.queries: list[str] = []
        self.statements: list[tuple[str, dict | None]] = []
        self.closed = False
        self._earliest = earliest
        self._object_names = object_names or []
        self._media_trace_ids = media_trace_ids or []
        self._delete_fails = delete_fails

    async def query_json_rows(self, query: str) -> list[dict[str, object]]:
        self.queries.append(query)
        if "min(started_at)" in query:
            return [{"earliest": self._earliest}] if self._earliest else [{"earliest": None}]
        # The marker-position query asks which traces need a prefix sweep; the
        # JSONExtract one asks for object names it can address directly.
        if "position(" in query:
            return [{"trace_id": tid} for tid in self._media_trace_ids]
        return [{"object_name": name} for name in self._object_names]

    async def execute(self, query: str, *, settings: dict | None = None) -> None:
        if self._delete_fails:
            raise RuntimeError("mutation rejected")
        self.statements.append((query, settings))

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def _clickhouse_provider(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "tracing_store_provider", "clickhouse")


@pytest.fixture
def _captured_object_deletes(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    async def _fake_delete(object_names: list[str]) -> int:
        calls.append(list(object_names))
        return len(object_names)

    monkeypatch.setattr("app.services.trace_storage.delete_object_names", _fake_delete)
    return calls


@pytest.mark.asyncio
async def test_sweep_deletes_rows_then_their_offloaded_objects(
    _clickhouse_provider, _captured_object_deletes, monkeypatch: pytest.MonkeyPatch
):
    # The whole point of the sweep: an offloaded object can only be found through
    # a row that points at it, so the pointers are read first, the rows deleted,
    # and the objects deleted last - in the same cycle as the PostgreSQL purge.
    cutoff = purge_cutoff(90)
    client = _SweepClient(
        # Recent enough that the retention cutoff, not the day budget, bounds the
        # window - so the sweep enforces exactly the PostgreSQL purge boundary.
        earliest=(cutoff - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S.000"),
        object_names=["traces/abc/trace/input.json", "traces/abc/span-1/output.json"],
    )

    purged = await purge_expired_clickhouse_traces(90, client_factory=lambda: client)

    assert purged == 2
    assert _captured_object_deletes == [
        ["traces/abc/trace/input.json", "traces/abc/span-1/output.json"]
    ]

    # Pointers were read before any row was deleted.
    enumeration = [q for q in client.queries if "object_name" in q]
    assert len(enumeration) == 1
    assert "JSONExtractString(input, 'object_name')" in enumeration[0]
    assert "__xtm_offload__" in enumeration[0]
    # Both payload-bearing tables, all three payload columns.
    for table in ("llm_traces", "llm_spans"):
        assert f"FROM {table}" in enumeration[0]
    for column in ("input", "output", "metadata"):
        assert f"JSONExtractString({column}, 'object_name')" in enumeration[0]

    # Rows deleted with a mutation the call waits for, so an object is never
    # deleted while its row is still queryable.
    boundary = _ch_datetime_literal(cutoff)
    assert [sql for sql, _ in client.statements] == [
        f"ALTER TABLE llm_traces DELETE WHERE started_at < {boundary}",
        f"ALTER TABLE llm_spans DELETE WHERE started_at < {boundary}",
    ]
    assert all(opts == {"mutations_sync": 1} for _, opts in client.statements)
    assert client.closed


@pytest.mark.asyncio
async def test_sweep_is_bounded_to_a_day_budget_per_run(
    _clickhouse_provider, _captured_object_deletes
):
    # Dropping retention from 365 to 7 expires ~358 days at once. One run takes a
    # bounded slice of that backlog; the rest waits for the next tick rather than
    # deleting a year of objects one HTTP call at a time inside one tick.
    from app.services.trace_storage import _CH_PURGE_MAX_DAYS_PER_RUN

    oldest = purge_cutoff(7) - timedelta(days=400)
    client = _SweepClient(earliest=oldest.strftime("%Y-%m-%d %H:%M:%S.000"))

    await purge_expired_clickhouse_traces(7, client_factory=lambda: client)

    expected_end = datetime.combine(
        oldest.date() + timedelta(days=_CH_PURGE_MAX_DAYS_PER_RUN),
        datetime.min.time(),
        tzinfo=UTC,
    )
    assert {sql.split("started_at < ")[1] for sql, _ in client.statements} == {
        _ch_datetime_literal(expected_end)
    }
    assert expected_end < purge_cutoff(7)  # backlog remains, for the next run


@pytest.mark.asyncio
async def test_sweep_does_nothing_when_nothing_is_expired(
    _clickhouse_provider, _captured_object_deletes
):
    # An empty table answers min(started_at) with the DateTime zero value, not
    # NULL - purging "everything before 1970 + 31 days" would be a no-op mutation
    # on every tick.
    for earliest in ("1970-01-01 00:00:00.000", None, datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.000")):
        client = _SweepClient(earliest=earliest)

        purged = await purge_expired_clickhouse_traces(
            90, client_factory=lambda bound=client: bound
        )

        assert purged == 0
        assert client.statements == []
        assert _captured_object_deletes == []
        assert client.closed


@pytest.mark.asyncio
async def test_sweep_leaves_objects_alone_when_the_row_deletion_fails(
    _clickhouse_provider, _captured_object_deletes
):
    # Fail open: the rows still point at every object, so a failure defers the
    # purge to the next tick instead of orphaning anything.
    client = _SweepClient(object_names=["traces/abc/trace/input.json"], delete_fails=True)

    assert await purge_expired_clickhouse_traces(90, client_factory=lambda: client) == 0

    assert _captured_object_deletes == []
    assert client.closed


@pytest.mark.asyncio
async def test_sweep_is_a_noop_under_the_postgres_provider(
    monkeypatch: pytest.MonkeyPatch, _captured_object_deletes
):
    monkeypatch.setattr(settings, "tracing_store_provider", "postgres")
    client = _SweepClient()

    assert await purge_expired_clickhouse_traces(90, client_factory=lambda: client) == 0
    assert client.queries == []


@pytest.mark.asyncio
async def test_sweep_also_clears_media_objects_by_prefix(
    _clickhouse_provider, _captured_object_deletes, monkeypatch: pytest.MonkeyPatch
):
    """Media is referenced too deep in the payload for ClickHouse to name it.

    A media reference sits wherever its content part was — inside
    ``messages[].content[]`` — and ClickHouse has no recursive JSON descent, so
    ``JSONExtractString`` cannot reach it. The rows carrying the marker are found
    with a substring test and their objects are then deleted by trace prefix.
    Without this, every media object outlived its trace in the bucket.
    """
    cutoff = purge_cutoff(90)
    trace_id = "11111111-1111-1111-1111-111111111111"
    client = _SweepClient(
        earliest=(cutoff - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S.000"),
        object_names=[],
        media_trace_ids=[trace_id],
    )

    swept: list[list[str]] = []

    async def _fake_sweep(trace_ids: list[str]) -> int:
        swept.append(list(trace_ids))
        return 3

    monkeypatch.setattr("app.services.trace_storage.delete_trace_objects", _fake_sweep)

    purged = await purge_expired_clickhouse_traces(90, client_factory=lambda: client)

    assert purged == 3
    assert swept == [[trace_id]]

    # The marker query looks for both kinds of pointer, on both tables and every
    # payload column, and runs before any row is deleted.
    marker_query = next(q for q in client.queries if "position(" in q)
    assert "__xtm_media__" in marker_query
    # Payload envelopes too: media referenced from inside an offloaded payload is
    # not in ClickHouse at all, so those traces need the sweep as well.
    assert "__xtm_offload__" in marker_query
    for table in ("llm_traces", "llm_spans"):
        assert f"FROM {table}" in marker_query
    for column in ("input", "output", "metadata"):
        assert f"position({column}, '__xtm_media__')" in marker_query
    # Keyed by the owning trace, which is a different column on each table.
    assert "toString(id) AS trace_id" in marker_query
    assert "toString(trace_id) AS trace_id" in marker_query


@pytest.mark.asyncio
async def test_sweep_does_not_list_objects_for_traces_carrying_no_marker(
    _clickhouse_provider, _captured_object_deletes, monkeypatch: pytest.MonkeyPatch
):
    """A purge expiring a whole window must not cost one LIST per trace."""
    cutoff = purge_cutoff(90)
    client = _SweepClient(
        earliest=(cutoff - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S.000"),
        object_names=["traces/abc/trace/input.json"],
        media_trace_ids=[],
    )

    swept: list[list[str]] = []

    async def _fake_sweep(trace_ids: list[str]) -> int:
        swept.append(list(trace_ids))
        return 0

    monkeypatch.setattr("app.services.trace_storage.delete_trace_objects", _fake_sweep)

    await purge_expired_clickhouse_traces(90, client_factory=lambda: client)

    assert swept == [], "no marker, no prefix walk"


def test_postgres_purge_defers_objects_to_the_sweep_under_clickhouse(
    monkeypatch: pytest.MonkeyPatch,
):
    # Traces are mirrored to both stores, and a payload object has one copy that
    # both rows point at. The trace UI reads ClickHouse, so deleting objects on
    # the PostgreSQL boundary would break "Load full payload" for the traces
    # sitting at that boundary. Exactly one owner: the ClickHouse sweep.
    from app.services.trace_storage import _postgres_owns_offloaded_objects

    monkeypatch.setattr(settings, "tracing_store_provider", "postgres")
    assert _postgres_owns_offloaded_objects() is True

    monkeypatch.setattr(settings, "tracing_store_provider", "clickhouse")
    assert _postgres_owns_offloaded_objects() is False


@pytest.mark.asyncio
async def test_sync_swallows_clickhouse_failures(monkeypatch: pytest.MonkeyPatch):
    # Called from the maintenance tick and from an admin's settings save; neither
    # may fail because ClickHouse is down. The next tick retries.
    monkeypatch.setattr(settings, "tracing_store_provider", "clickhouse")
    client = _FailingClient()

    assert await sync_clickhouse_trace_retention(30, client_factory=lambda: client) == []
    assert client.closed
