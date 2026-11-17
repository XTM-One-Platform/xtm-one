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

"""Minimal ClickHouse HTTP client used by tracing read/write stores."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_ALLOWED_TABLES = {"llm_traces", "llm_spans", "llm_scores"}


# The Trace Explorer scopes a list to an assignment or a run. A trace
# row carries its run, and its assignment only inside the ``metadata`` JSON the
# engine stamps (``trace_llm_call``): the column below extracts it ONCE, at
# insert, so a scoped list never parses every row's JSON. ``MATERIALIZED``
# keeps it out of ``SELECT *`` and out of what an insert may write. The bloom
# filters let both scopes skip the granules that cannot hold the id; parts
# written before the index existed are read in full until retention drops them
# (``MATERIALIZE INDEX`` would rewrite every part on each bootstrap). They are
# an optimisation only, so they are never declared in the CREATE above and are
# added by ``_SCHEMA_OPTIONAL_MIGRATIONS_SQL``, which a server refusing one
# cannot turn into a tracing outage.
_ASSIGNMENT_ID_COLUMN = "assignment_id String MATERIALIZED JSONExtractString(metadata, 'assignment_id')"
_ASSIGNMENT_ID_INDEX = "idx_llm_traces_assignment_id assignment_id TYPE bloom_filter GRANULARITY 4"
_ASSIGNMENT_RUN_ID_INDEX = "idx_llm_traces_assignment_run_id assignment_run_id TYPE bloom_filter GRANULARITY 4"

_SCHEMA_SQL = [
    f"""
    CREATE TABLE IF NOT EXISTS llm_traces (
        id UUID,
        agent_id Nullable(UUID),
        user_id Nullable(UUID),
        conversation_id Nullable(UUID),
        assignment_run_id Nullable(UUID),
        parent_trace_id Nullable(UUID),
        source String,
        name String,
        model Nullable(String),
        environment String,
        metadata String,
        input String,
        output String,
        tags String,
        started_at DateTime64(3, 'UTC'),
        ended_at Nullable(DateTime64(3, 'UTC')),
        duration_ms Nullable(Float64),
        input_tokens Nullable(Int32),
        output_tokens Nullable(Int32),
        total_tokens Nullable(Int32),
        input_cost Nullable(Float64),
        output_cost Nullable(Float64),
        total_cost Nullable(Float64),
        status String,
        error_message Nullable(String),
        failure_cause Nullable(String),
        level String,
        created_at DateTime64(3, 'UTC'),
        updated_at DateTime64(3, 'UTC'),
        {_ASSIGNMENT_ID_COLUMN}
    )
    ENGINE = MergeTree
    ORDER BY (started_at, id)
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_spans (
        id UUID,
        trace_id UUID,
        parent_span_id Nullable(UUID),
        type String,
        name String,
        model Nullable(String),
        model_parameters String,
        input String,
        output String,
        metadata String,
        level String,
        status_message Nullable(String),
        started_at DateTime64(3, 'UTC'),
        ended_at Nullable(DateTime64(3, 'UTC')),
        duration_ms Nullable(Float64),
        time_to_first_token Nullable(Float64),
        input_tokens Nullable(Int32),
        output_tokens Nullable(Int32),
        input_cost Nullable(Float64),
        output_cost Nullable(Float64),
        total_cost Nullable(Float64),
        usage_details String,
        tool_call_id Nullable(String),
        tool_arguments String,
        stop_reason Nullable(String),
        position Int32,
        created_at DateTime64(3, 'UTC')
    )
    ENGINE = MergeTree
    ORDER BY (trace_id, position, started_at, id)
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_scores (
        id UUID,
        trace_id UUID,
        span_id Nullable(UUID),
        user_id Nullable(UUID),
        name String,
        source String,
        data_type String,
        value Nullable(Float64),
        string_value Nullable(String),
        comment Nullable(String),
        metadata String,
        created_at DateTime64(3, 'UTC')
    )
    ENGINE = MergeTree
    ORDER BY (trace_id, created_at, id)
    """,
]

# Columns added to a table AFTER it first shipped. The DDL above is
# ``CREATE TABLE IF NOT EXISTS``, so it never revisits a table that already
# exists — an install created by an earlier version would otherwise keep a
# schema missing every later column, and the inserts naming it would fail.
# ``ADD COLUMN IF NOT EXISTS`` is idempotent and metadata-only in ClickHouse,
# so this runs harmlessly on every schema bootstrap.
_SCHEMA_MIGRATIONS_SQL = [
    # Structured failure attribution.
    "ALTER TABLE llm_traces ADD COLUMN IF NOT EXISTS failure_cause Nullable(String)",
    # Assignment / run scope of the Trace Explorer. An existing part
    # computes the column from ``metadata`` on read; new parts store it.
    f"ALTER TABLE llm_traces ADD COLUMN IF NOT EXISTS {_ASSIGNMENT_ID_COLUMN}",
]

# Optimisations only: the schema is ready without them. A statement the
# deployed server refuses (an older release, a skip index over a type it does
# not take) is logged and skipped - never left to fail the bootstrap, which
# would drop every sink flush and every Trace Explorer read. Runs on a fresh
# table too, right after its CREATE; retried by the next process.
_SCHEMA_OPTIONAL_MIGRATIONS_SQL = [
    f"ALTER TABLE llm_traces ADD INDEX IF NOT EXISTS {_ASSIGNMENT_ID_INDEX}",
    f"ALTER TABLE llm_traces ADD INDEX IF NOT EXISTS {_ASSIGNMENT_RUN_ID_INDEX}",
]


# ── Retention (TTL) ──────────────────────────────────────────────────────────
# The DDL above declares NO TTL on purpose: it runs on first use, from a plain
# HTTP client that has no database session, while the retention window lives in
# ``platform_settings.trace_retention_days`` - a runtime setting an admin can
# change at any time. Retention is therefore reconciled separately, by
# ``apply_trace_retention_ttl``, which is also the only thing that can fix a
# table created by an earlier version: the DDL is ``CREATE TABLE IF NOT EXISTS``,
# so it never revisits a table that already exists.

# Column each table's expiry is measured from. ``llm_scores`` has no
# ``started_at`` - a score (late user feedback, say) is timestamped when it is
# written, so it can outlive the trace it belongs to by however long the feedback
# took to arrive; likewise a span that started after midnight under a trace that
# started before it. Both over-retain by a bounded margin and are invisible to
# reads (every query reaches them through ``llm_traces``), which is the safe
# direction to err in.
_TTL_COLUMNS: dict[str, str] = {
    "llm_traces": "started_at",
    "llm_spans": "started_at",
    "llm_scores": "created_at",
}

# Matches the TTL clause in ``system.tables.engine_full``, in both the form we
# send (``+ INTERVAL 91 DAY``) and the form ClickHouse normalises it to
# (``+ toIntervalDay(91)``).
_TTL_CLAUSE_PATTERN = re.compile(
    r"\bTTL\s+toStartOfDay\((?P<column>\w+)\)\s*\+\s*"
    r"(?:toIntervalDay\((?P<fn_days>\d+)\)|INTERVAL\s+(?P<days>\d+)\s+DAY)",
    re.IGNORECASE,
)


# Days the TTL is set BEYOND the retention window. The TTL is a backstop, not
# the primary mechanism: ``purge_expired_clickhouse_traces`` sweeps the exact
# window on every maintenance cycle, and it can only find a trace's offloaded
# payload objects through the row that points at them. A TTL on the exact
# boundary would race that sweep and let ClickHouse drop rows first, orphaning
# the objects in the bucket. This margin is how long the maintenance loop may be
# down before the TTL starts expiring rows on its own (retention stays honest;
# their objects are what get left behind).
TTL_BACKSTOP_MARGIN_DAYS = 7


def trace_ttl_expression(column: str, retention_days: int) -> str:
    """Backstop TTL expression for ``column``, a margin past the purge boundary.

    ``purge_expired_traces`` cuts on midnight UTC of ``today - retention_days``
    rather than on the instant ``now - retention_days`` (see
    ``trace_storage.purge_cutoff`` for why that whole-day boundary is
    load-bearing for the daily rollup), so a trace from day D leaves PostgreSQL
    at the start of day ``D + retention_days + 1``. This expresses that same
    whole-day boundary - which is also the boundary the ClickHouse sweep uses -
    plus ``TTL_BACKSTOP_MARGIN_DAYS``, so the sweep normally deletes a row before
    its TTL is ever reached.

    ``toStartOfDay`` reads the column's own ``'UTC'`` timezone, matching the
    UTC-day boundary PostgreSQL cuts on.
    """
    return f"toStartOfDay({column}) + INTERVAL {ttl_expiry_days(retention_days)} DAY DELETE"


def ttl_expiry_days(retention_days: int) -> int:
    """Days after a trace's own day that its backstop TTL expires."""
    return int(retention_days) + 1 + TTL_BACKSTOP_MARGIN_DAYS


def has_clickhouse_runtime_config() -> bool:
    """Return True when the runtime ClickHouse credentials look explicit.

    We intentionally treat untouched defaults (default user + empty password)
    as not configured, so test suites that only flip TRACING_STORE_PROVIDER
    keep the previous noop behavior.
    """

    host = (settings.clickhouse_host or "").strip()
    username = (settings.clickhouse_username or "").strip()
    database = (settings.clickhouse_database or "").strip()
    password = settings.clickhouse_password or ""

    if not host or not username or not database:
        return False

    using_placeholder_defaults = username == "default" and password == ""
    return not using_placeholder_defaults


class HTTPClickHouseClient:
    """Tiny async client over ClickHouse HTTP API."""

    _schema_lock = asyncio.Lock()
    _schema_ready_keys: set[str] = set()
    # Retention already declared by this process, keyed ``<schema_key>:<table>``.
    # Guards against an ALTER loop - see ``apply_trace_retention_ttl``.
    _ttl_applied_days: dict[str, int] = {}

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        database: str,
        secure: bool,
        timeout_seconds: int,
    ) -> None:
        scheme = "https" if secure else "http"
        self._base_url = f"{scheme}://{host}:{port}"
        self._database = database
        self._username = username
        self._password = password
        self._schema_key = f"{scheme}:{host}:{port}:{database}:{username}"
        self._http = httpx.AsyncClient(timeout=timeout_seconds)

    @classmethod
    def from_settings(cls) -> HTTPClickHouseClient:
        return cls(
            host=settings.clickhouse_host,
            port=settings.clickhouse_port,
            username=settings.clickhouse_username,
            password=settings.clickhouse_password,
            database=settings.clickhouse_database,
            secure=settings.clickhouse_secure,
            timeout_seconds=settings.clickhouse_connect_timeout_seconds,
        )

    async def query_json_rows(self, query: str) -> list[dict[str, Any]]:
        await self._ensure_schema_once()
        sql = query.strip().rstrip(";")
        if " format " not in sql.lower():
            sql = f"{sql} FORMAT JSONEachRow"
        text = await self._execute_raw(sql)
        rows: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if isinstance(payload, dict):
                rows.append(payload)
        return rows

    async def query_rows(self, query: str) -> list[dict[str, Any]]:
        return await self.query_json_rows(query)

    async def execute(self, query: str, *, settings: dict[str, Any] | None = None) -> None:
        """Run a statement. ``settings`` are passed as ClickHouse query settings
        (e.g. ``mutations_sync=1`` to wait for a mutation to finish)."""
        await self._ensure_schema_once()
        await self._execute_raw(query.strip().rstrip(";"), settings=settings)

    async def insert_json_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        if table not in _ALLOWED_TABLES:
            raise ValueError(f"Unsupported ClickHouse table: {table}")
        if not rows:
            return

        await self._ensure_schema_once()

        body_lines = [
            json.dumps(
                {key: self._json_ready(value) for key, value in row.items()},
                ensure_ascii=True,
                separators=(",", ":"),
            )
            for row in rows
        ]
        sql = f"INSERT INTO {table} FORMAT JSONEachRow\n" + "\n".join(body_lines)
        # The dispatcher flushes one trace at a time, so without batching each
        # trace becomes its own tiny part per table and ClickHouse hits the "too
        # many parts" limit under load. async_insert lets the server buffer these
        # small inserts and flush them as larger parts; wait_for_async_insert=1
        # keeps the call durable (returns only once its buffer is flushed) while
        # still coalescing inserts that arrive within the server's async window.
        await self._execute_raw(
            sql,
            settings={"async_insert": 1, "wait_for_async_insert": 1},
        )

    async def apply_trace_retention_ttl(self, retention_days: int) -> list[str]:
        """Declare the backstop TTL on every trace table; return those changed.

        This is what makes the ClickHouse provider honour
        ``platform_settings.trace_retention_days`` even when nothing is sweeping
        it (``trace_storage.purge_expired_clickhouse_traces`` is what enforces the
        exact window, a margin earlier - see ``trace_ttl_expression``). It covers
        tables created by an earlier version too, since it ALTERs whatever exists
        rather than relying on the ``CREATE TABLE IF NOT EXISTS`` DDL.
        ClickHouse's default ``materialize_ttl_after_modify`` means the ALTER also
        schedules a mutation that applies the new TTL to already-written parts, so
        existing history expires rather than only new inserts.

        Idempotent, and deliberately so: the declared TTL is read back from
        ``system.tables`` and only tables that disagree are altered, because the
        caller is a repeating maintenance tick and re-ALTERing would re-materialise
        the TTL across the whole table every hour on every replica. A second guard
        backs that up - once a table has been altered for a given retention in
        this process it is not altered again, so a normalised expression this
        module cannot parse (or a ``system.tables`` we are not allowed to read)
        degrades to one ALTER per process instead of one per tick.
        """
        days = max(1, int(retention_days))
        await self._ensure_schema_once()

        declared = await self._declared_trace_ttl()
        changed: list[str] = []
        for table, column in _TTL_COLUMNS.items():
            cache_key = f"{self._schema_key}:{table}"
            if declared.get(table) == (column, ttl_expiry_days(days)):
                self._ttl_applied_days[cache_key] = days
                continue
            if self._ttl_applied_days.get(cache_key) == days:
                logger.debug(
                    "ClickHouse %s already had a %d-day trace TTL applied by this "
                    "process; skipping re-ALTER despite an unrecognised declaration",
                    table,
                    days,
                )
                continue

            await self._execute_raw(
                f"ALTER TABLE {table} MODIFY TTL {trace_ttl_expression(column, days)}",
                ensure_schema=False,
            )
            self._ttl_applied_days[cache_key] = days
            changed.append(table)

        return changed

    async def _declared_trace_ttl(self) -> dict[str, tuple[str, int]]:
        """``(column, days)`` of the TTL each trace table currently declares.

        Tables with no TTL, or one this module does not recognise, are simply
        absent from the result - the caller then (re)declares ours. A failure to
        read ``system.tables`` is not fatal for the same reason: a restricted
        ClickHouse user must not be the difference between "retention applies"
        and "retention silently does not apply".
        """
        names = ", ".join(f"'{table}'" for table in sorted(_TTL_COLUMNS))
        try:
            rows = await self.query_json_rows(
                "SELECT name, engine_full FROM system.tables "  # noqa: S608 - names are the _TTL_COLUMNS keys, not input
                f"WHERE database = currentDatabase() AND name IN ({names})"
            )
        except Exception as exc:  # pragma: no cover - exercised in integration only
            logger.warning(
                "Could not read the declared ClickHouse trace TTL (%s); applying it blind",
                exc,
            )
            return {}

        declared: dict[str, tuple[str, int]] = {}
        for row in rows:
            match = _TTL_CLAUSE_PATTERN.search(str(row.get("engine_full") or ""))
            if match is None:
                continue
            days = match.group("fn_days") or match.group("days")
            declared[str(row.get("name"))] = (match.group("column"), int(days))
        return declared

    async def close(self) -> None:
        await self._http.aclose()

    async def _ensure_schema_once(self) -> None:
        if self._schema_key in self._schema_ready_keys:
            return

        async with self._schema_lock:
            if self._schema_key in self._schema_ready_keys:
                return

            await self._execute_raw(f"CREATE DATABASE IF NOT EXISTS {self._database}", ensure_schema=False)
            for statement in _SCHEMA_SQL:
                await self._execute_raw(statement, ensure_schema=False)
            for statement in _SCHEMA_MIGRATIONS_SQL:
                await self._execute_raw(statement, ensure_schema=False)
            for statement in _SCHEMA_OPTIONAL_MIGRATIONS_SQL:
                try:
                    await self._execute_raw(statement, ensure_schema=False)
                except httpx.HTTPError:
                    # A transport failure, not a refusal (``_execute_raw``
                    # turns a server's error answer into ``RuntimeError``):
                    # leave the schema unmarked so the next call retries the
                    # whole idempotent bootstrap instead of losing the index.
                    raise
                except Exception as exc:
                    logger.warning(
                        "Optional ClickHouse schema statement skipped: %r (%s: %r)",
                        statement[:200],
                        type(exc).__name__,
                        str(exc)[:300],
                    )

            self._schema_ready_keys.add(self._schema_key)

    async def _execute_raw(
        self,
        sql: str,
        *,
        ensure_schema: bool = True,
        settings: dict[str, Any] | None = None,
    ) -> str:
        if ensure_schema:
            await self._ensure_schema_once()

        params: dict[str, Any] = {"database": self._database}
        if settings:
            # ClickHouse accepts per-query settings as URL params (values only,
            # never credentials - those go in headers above).
            params.update(settings)

        response = await self._http.post(
            f"{self._base_url}/",
            params=params,
            content=sql.encode("utf-8"),
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                # Send credentials as headers, not URL query params: full request
                # URLs are routinely recorded by proxies / server access logs, so
                # query-string credentials leak into them.
                "X-ClickHouse-User": self._username,
                "X-ClickHouse-Key": self._password,
            },
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:  # pragma: no cover - exercised in integration only
            snippet = response.text.strip().replace("\n", " ")[:300]
            raise RuntimeError(f"ClickHouse query failed: {snippet}") from exc
        return response.text

    @classmethod
    def _json_ready(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, datetime):
            dt = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
            dt = dt.astimezone(UTC)
            return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, dict):
            return json.dumps({k: cls._json_ready(v) for k, v in value.items()}, ensure_ascii=True)
        if isinstance(value, list):
            return json.dumps([cls._json_ready(item) for item in value], ensure_ascii=True)
        if isinstance(value, tuple):
            return json.dumps([cls._json_ready(item) for item in value], ensure_ascii=True)
        return value
