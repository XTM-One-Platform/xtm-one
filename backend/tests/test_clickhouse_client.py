from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from app.services.tracing.clickhouse_client import HTTPClickHouseClient


@pytest.mark.asyncio
async def test_insert_json_rows_keeps_row_as_json_object(monkeypatch: pytest.MonkeyPatch):
    client = HTTPClickHouseClient(
        host="localhost",
        port=8123,
        username="test",
        password="test",
        database="test",
        secure=False,
        timeout_seconds=5,
    )

    captured_sql: list[str] = []
    captured_settings: list[dict[str, object] | None] = []

    async def _noop_schema() -> None:
        return None

    async def _capture_execute(
        sql: str,
        *,
        ensure_schema: bool = True,
        settings: dict[str, object] | None = None,
    ) -> str:
        captured_sql.append(sql)
        captured_settings.append(settings)
        return ""

    monkeypatch.setattr(client, "_ensure_schema_once", _noop_schema)
    monkeypatch.setattr(client, "_execute_raw", _capture_execute)

    await client.insert_json_rows(
        "llm_traces",
        [
            {
                "id": str(uuid4()),
                "metadata": {"k": "v"},
                "tags": ["alpha", "beta"],
                "created_at": datetime(2026, 6, 24, 12, 0, tzinfo=UTC),
            }
        ],
    )

    assert len(captured_sql) == 1
    sql = captured_sql[0]
    assert sql.startswith("INSERT INTO llm_traces FORMAT JSONEachRow\n")

    # The insert path must batch server-side to avoid the ClickHouse "too many
    # parts" error: async_insert on, wait_for_async_insert on for durability.
    assert captured_settings == [{"async_insert": 1, "wait_for_async_insert": 1}]

    payload_line = sql.split("\n", 1)[1].strip()
    payload = json.loads(payload_line)

    assert isinstance(payload, dict)
    assert not isinstance(payload, str)
    assert payload["metadata"] == '{"k": "v"}'
    assert payload["tags"] == '["alpha", "beta"]'

    await client.close()


_ASSIGNMENT_COLUMN_SQL = (
    "ALTER TABLE llm_traces ADD COLUMN IF NOT EXISTS "
    "assignment_id String MATERIALIZED JSONExtractString(metadata, 'assignment_id')"
)
_ASSIGNMENT_INDEXES_SQL = [
    "ALTER TABLE llm_traces ADD INDEX IF NOT EXISTS "
    "idx_llm_traces_assignment_id assignment_id TYPE bloom_filter GRANULARITY 4",
    "ALTER TABLE llm_traces ADD INDEX IF NOT EXISTS "
    "idx_llm_traces_assignment_run_id assignment_run_id TYPE bloom_filter GRANULARITY 4",
]


def test_the_assignment_scope_is_a_stored_column_with_skip_indexes_on_every_install():
    """The Trace Explorer's assignment scope must not parse every row's
    metadata JSON. The column is required - a fresh table declares it and an
    existing one gets it through an idempotent ALTER - while the two bloom
    filters are optional, never part of a statement the bootstrap needs."""
    from app.services.tracing.clickhouse_client import (
        _SCHEMA_MIGRATIONS_SQL,
        _SCHEMA_OPTIONAL_MIGRATIONS_SQL,
        _SCHEMA_SQL,
    )

    traces_ddl = next(statement for statement in _SCHEMA_SQL if "TABLE IF NOT EXISTS llm_traces" in statement)
    declared = [line.strip().rstrip(",") for line in traces_ddl.splitlines()]
    assert "assignment_id String MATERIALIZED JSONExtractString(metadata, 'assignment_id')" in declared
    assert [line for line in declared if line.startswith("INDEX ")] == []
    assert _SCHEMA_MIGRATIONS_SQL.count(_ASSIGNMENT_COLUMN_SQL) == 1
    positions = [_SCHEMA_OPTIONAL_MIGRATIONS_SQL.index(statement) for statement in _ASSIGNMENT_INDEXES_SQL]
    assert positions == sorted(positions)
    assert not set(_ASSIGNMENT_INDEXES_SQL) & set(_SCHEMA_MIGRATIONS_SQL)


def _schema_client(
    monkeypatch: pytest.MonkeyPatch,
    refused: set[str],
    *,
    unreachable: set[str] = frozenset(),
) -> tuple[HTTPClickHouseClient, list[str]]:
    client = HTTPClickHouseClient(
        host="localhost",
        port=8123,
        username="test",
        password="test",
        database="test",
        secure=False,
        timeout_seconds=5,
    )
    executed: list[str] = []
    # The ready marks are class-level, shared by every client of the process:
    # start from none, and restore whatever other tests left there.
    monkeypatch.setattr(HTTPClickHouseClient, "_schema_ready_keys", set())

    async def _execute(sql: str, *, ensure_schema: bool = True, settings: dict[str, object] | None = None) -> str:
        executed.append(sql)
        if sql in unreachable:
            raise httpx.ConnectTimeout("timed out")
        if sql in refused:
            raise RuntimeError("Code: 80. DB::Exception: refused by this server")
        return ""

    monkeypatch.setattr(client, "_execute_raw", _execute)
    return client, executed


@pytest.mark.asyncio
async def test_a_refused_skip_index_never_takes_the_trace_store_down(monkeypatch: pytest.MonkeyPatch, caplog):
    """An older server refusing a bloom filter is logged and skipped: the
    schema is still ready, so sink flushes and Explorer reads keep working."""
    client, executed = _schema_client(monkeypatch, refused=set(_ASSIGNMENT_INDEXES_SQL))

    with caplog.at_level("WARNING", logger="app.services.tracing.clickhouse_client"):
        await client._ensure_schema_once()

    assert client._schema_key in client._schema_ready_keys
    assert executed.count(_ASSIGNMENT_COLUMN_SQL) == 1
    assert [sql for sql in executed if sql in _ASSIGNMENT_INDEXES_SQL] == _ASSIGNMENT_INDEXES_SQL
    skipped = [record for record in caplog.records if "Optional ClickHouse schema statement skipped" in record.getMessage()]
    assert len(skipped) == 2


@pytest.mark.asyncio
async def test_a_refused_required_column_still_fails_the_bootstrap(monkeypatch: pytest.MonkeyPatch):
    """The scope filter reads the column: without it the schema is not ready,
    and the next call retries rather than querying a column that is absent."""
    client, executed = _schema_client(monkeypatch, refused={_ASSIGNMENT_COLUMN_SQL})

    with pytest.raises(RuntimeError):
        await client._ensure_schema_once()

    assert client._schema_key not in client._schema_ready_keys
    assert not set(_ASSIGNMENT_INDEXES_SQL) & set(executed)


@pytest.mark.asyncio
async def test_an_unreachable_server_is_not_mistaken_for_a_refused_index(monkeypatch: pytest.MonkeyPatch, caplog):
    """A timeout while adding an index says nothing about the server: the
    bootstrap is left unmarked, so the next call retries it, rather than
    skipping the index until the process restarts."""
    client, executed = _schema_client(monkeypatch, refused=set(), unreachable={_ASSIGNMENT_INDEXES_SQL[0]})

    with (
        caplog.at_level("WARNING", logger="app.services.tracing.clickhouse_client"),
        pytest.raises(httpx.ConnectTimeout),
    ):
        await client._ensure_schema_once()

    assert client._schema_key not in client._schema_ready_keys
    assert _ASSIGNMENT_INDEXES_SQL[1] not in executed
    assert [r for r in caplog.records if "Optional ClickHouse schema statement skipped" in r.getMessage()] == []
