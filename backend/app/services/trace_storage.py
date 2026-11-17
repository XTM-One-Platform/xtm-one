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

"""Trace retention purge and daily statistics aggregation helpers."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import exists, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.models.llm_trace import LLM_TRACE_SOURCES, LLMDailyStats, LLMTrace
from app.models.platform_settings import PlatformSettings
from app.services.tracing.feedback_scores import (
    FEEDBACK_PER_TRACE_SQL_POSTGRES_ONE_DAY,
    FEEDBACK_SCORE_NAME,
    lock_rater_score,
    rater_score_payload,
)
from app.services.tracing.latency_histogram import (
    bucket_expression_sql,
    encode_histogram,
)
from app.services.tracing.local_sink import write_score_row
from app.services.tracing.media_offload import MEDIA_KEY
from app.services.tracing.payload_offload import (
    ENVELOPE_KEY,
    ENVELOPE_VERSION,
    delete_object_names,
    delete_trace_objects,
)

logger = logging.getLogger(__name__)

_PURGE_BATCH_SIZE = 1000

# Days rescued (aggregated) per purge run before their traces are allowed to go.
# Bounds the catch-up work when a large stretch expires at once - dropping
# ``trace_retention_days`` from 365 to 7 makes ~358 days expire in one tick.
# Un-rescued days are simply not deleted this run; the next run continues.
_PURGE_RESCUE_MAX_DAYS_PER_RUN = 31

# The transaction-scoped lock that serializes every writer of
# ``llm_daily_stats`` across replicas: the aggregation below, the rebuild of a
# stale rollup, the feedback refresh, and the repricing that records one
# (``tracing/repricing.py``).
DAILY_STATS_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('llm_daily_stats_aggregation'))"

_LAST_AGGREGATION_SQL = "UPDATE platform_settings SET last_aggregation_at = :now_utc"


def purge_cutoff(retention_days: int, now: datetime | None = None) -> datetime:
    """Start of the oldest day the retention window keeps.

    A WHOLE-DAY boundary on purpose. Cutting on the instant ``now -
    retention_days`` truncates the oldest day in the window mid-day, and the
    daily-stats aggregator - which aggregates whole UTC days down to ``today -
    retention_days`` - would then roll that day up from the surviving fraction
    and store a permanently under-reported row (``aggregate_daily_stats`` skips
    any date that already has one). Cutting on midnight means a day is either
    entirely kept or entirely expired, so the rollup is never built from a
    partially purged day.
    """
    now_utc = now or datetime.now(UTC)
    return datetime.combine(
        now_utc.date() - timedelta(days=retention_days),
        datetime.min.time(),
        tzinfo=UTC,
    )


def latest_complete_day(now: datetime | None = None) -> date:
    """Newest day whose rollup may be built now.

    Yesterday once we are at least an hour into today - a buffer for late
    async trace writes around midnight - otherwise the day before. The rollup
    is insert-only (``aggregate_daily_stats`` never rewrites a day it built),
    so a day built too early misses those writes for good.
    """
    now_utc = now or datetime.now(UTC)
    return now_utc.date() - timedelta(days=1 if now_utc.hour >= 1 else 2)


async def _rescue_unaggregated_expiring_days(db: AsyncSession, cutoff: datetime) -> int:
    """Aggregate every expiring day that still has no ``llm_daily_stats`` row.

    The rollup is meant to outlive the purge, but ``run_daily_stats_backfill``
    only ever looks *inside* the retention window - a day that leaves the window
    before it was aggregated (downtime longer than the retention period, or an
    admin lowering ``trace_retention_days``) would be deleted with its KPI never
    built, unrecoverably. So the purge rescues those days itself, immediately
    before deleting them.

    The day list comes from the traces about to be deleted rather than a calendar
    walk, so every rescued day is one that actually has traces and therefore
    always yields at least one rollup row - the work converges instead of
    re-attempting empty days forever. Returns the number of days rescued.
    """
    rows = (
        await db.execute(
            text(
                """
                SELECT DISTINCT (t.started_at AT TIME ZONE 'UTC')::date AS trace_date
                FROM llm_traces t
                WHERE t.started_at < :cutoff
                  AND NOT EXISTS (
                      SELECT 1
                      FROM llm_daily_stats s
                      WHERE s.date = (t.started_at AT TIME ZONE 'UTC')::date
                  )
                ORDER BY trace_date
                LIMIT :max_days
                """
            ),
            {"cutoff": cutoff, "max_days": _PURGE_RESCUE_MAX_DAYS_PER_RUN},
        )
    ).all()
    # Read-only probe; end its transaction so the aggregations below start clean
    # (each one takes its own advisory lock and commits).
    await db.rollback()

    if not rows:
        return 0

    rescued = 0
    for (trace_date,) in rows:
        if await aggregate_daily_stats(db, target_date=trace_date) > 0:
            rescued += 1

    logger.warning(
        "Trace retention purge rescued %d of %d expiring day(s) that had no daily-stats "
        "rollup yet (their KPIs would otherwise have been lost with the traces)",
        rescued,
        len(rows),
    )
    if len(rows) >= _PURGE_RESCUE_MAX_DAYS_PER_RUN:
        logger.warning(
            "Rescue budget of %d day(s) per run reached - traces for days beyond it are "
            "kept until a later run aggregates them",
            _PURGE_RESCUE_MAX_DAYS_PER_RUN,
        )
    return rescued


async def _rebuild_stale_expiring_days(db: AsyncSession, cutoff: datetime) -> int:
    """Rebuild every expiring day whose rollup is recorded stale, before
    its traces go.

    The purge keeps the traces of a recorded day (see the batch query), so
    this is the last moment the day can be rebuilt from all of them - the
    backfill only looks inside the retention window. A rebuild that would
    count fewer traces than the stale rows keeps them instead: the day was
    being purged before it was recorded. Oldest first and bounded like the
    rescue; a day left over, or whose rebuild failed, keeps its traces for the
    next run. Returns the number of days rebuilt.
    """
    days = await _stale_days(
        db,
        first=date.min,
        last=cutoff.date() - timedelta(days=1),
        limit=_PURGE_RESCUE_MAX_DAYS_PER_RUN,
        newest_first=False,
    )
    if not days:
        return 0

    rebuilt = 0
    for day in days:
        if await rebuild_daily_stats(db, day, expired_before=cutoff.date()) > 0:
            rebuilt += 1
    logger.info(
        "Trace retention purge rebuilt %d of %d expiring day(s) whose rollup was stale",
        rebuilt,
        len(days),
    )
    return rebuilt


def _postgres_owns_offloaded_objects() -> bool:
    """Whether the PostgreSQL purge may delete the offloaded payload objects.

    It may not under the ClickHouse provider. Traces are written to BOTH stores
    there (``MirrorTracingSink``), and a payload object has a single copy that
    both rows point at - but the trace UI reads ClickHouse. Deleting the objects
    on the PostgreSQL boundary would strip the payloads out from under the
    ClickHouse rows that still serve them, and "Load full payload" would fail on
    exactly the traces sitting at the retention boundary. So under ClickHouse the
    objects belong to :func:`purge_expired_clickhouse_traces`, which deletes them
    together with the rows that reference them, in the same maintenance cycle.
    """
    from app.services.tracing.store import get_trace_store_provider

    return get_trace_store_provider() != "clickhouse"


async def _offloaded_object_names(db: AsyncSession, trace_ids: list) -> list[str]:
    """Object-storage names of every stored object ``trace_ids`` point at.

    Covers both the trace rows and their spans, across all three payload
    columns, and both kinds of pointer:

      * a **payload envelope**, which replaces a whole field and therefore sits
        at the top level of the column;
      * a **media reference**, which replaces one blob *inside* the payload and
        so can be nested arbitrarily deep — wherever the image content part
        was.  Finding those needs the recursive ``$.**`` descent rather than a
        top-level key test, which is why looking only for the envelope marker
        left every media object orphaned in the bucket.

    ``jsonb_exists`` is used rather than the ``?`` operator so the marker test
    survives driver-level parameter interpolation.
    """
    if not trace_ids:
        return []
    try:
        rows = await db.execute(
            text(
                """
                SELECT DISTINCT node->>'object_name' AS object_name
                FROM (
                    SELECT input AS payload FROM llm_traces WHERE id = ANY(:ids)
                    UNION ALL SELECT output FROM llm_traces WHERE id = ANY(:ids)
                    UNION ALL SELECT metadata FROM llm_traces WHERE id = ANY(:ids)
                    UNION ALL SELECT input FROM llm_spans WHERE trace_id = ANY(:ids)
                    UNION ALL SELECT output FROM llm_spans WHERE trace_id = ANY(:ids)
                    UNION ALL SELECT metadata FROM llm_spans WHERE trace_id = ANY(:ids)
                ) p,
                     LATERAL jsonb_path_query(p.payload, '$.**') AS node
                WHERE p.payload IS NOT NULL
                  AND jsonb_typeof(node) = 'object'
                  AND (
                      jsonb_exists(node, :marker)
                      OR jsonb_exists(node, :media_marker)
                  )
                  AND node->>'object_name' IS NOT NULL
                """
            ),
            {"ids": trace_ids, "marker": ENVELOPE_KEY, "media_marker": MEDIA_KEY},
        )
        return [str(row[0]) for row in rows.all() if row[0]]
    except Exception as exc:
        # A failure here must not stop the purge: retaining an orphaned object is
        # a storage cost, retaining an expired trace is a compliance problem.
        logger.warning(
            "Could not resolve offloaded payload objects for purge: %s",
            format_exception_message(exc),
        )
        return []


async def _traces_needing_object_sweep(db: AsyncSession, trace_ids: list) -> list:
    """Traces whose stored objects cannot be fully enumerated from their rows.

    A payload that was itself offloaded leaves only its envelope in the row, so
    any media reference *inside* that payload lives in the stored object and is
    invisible to :func:`_offloaded_object_names`.  Those traces get a prefix
    sweep of everything they own instead.

    Restricted to traces that actually carry an envelope: the overwhelming
    majority carry none, and they must not each cost an object-storage LIST
    round-trip during a purge that deletes them a thousand at a time.
    """
    if not trace_ids:
        return []
    try:
        rows = await db.execute(
            text(
                """
                SELECT DISTINCT p.trace_id
                FROM (
                    SELECT id AS trace_id, input AS payload
                        FROM llm_traces WHERE id = ANY(:ids)
                    UNION ALL SELECT id, output FROM llm_traces WHERE id = ANY(:ids)
                    UNION ALL SELECT id, metadata FROM llm_traces WHERE id = ANY(:ids)
                    UNION ALL SELECT trace_id, input
                        FROM llm_spans WHERE trace_id = ANY(:ids)
                    UNION ALL SELECT trace_id, output
                        FROM llm_spans WHERE trace_id = ANY(:ids)
                    UNION ALL SELECT trace_id, metadata
                        FROM llm_spans WHERE trace_id = ANY(:ids)
                ) p
                WHERE p.payload IS NOT NULL
                  AND jsonb_typeof(p.payload) = 'object'
                  AND jsonb_exists(p.payload, :marker)
                """
            ),
            {"ids": trace_ids, "marker": ENVELOPE_KEY},
        )
        return [row[0] for row in rows.all() if row[0]]
    except Exception as exc:
        logger.warning(
            "Could not resolve traces needing an object sweep: %s",
            format_exception_message(exc),
        )
        return []


async def purge_expired_traces(db: AsyncSession, retention_days: int) -> int:
    """Delete expired traces in batches and return the number of deleted rows.

    Never deletes a day whose aggregate is missing: the rollup is the only thing
    that survives this purge, so deleting an un-aggregated day destroys that
    day's KPIs permanently. Expiring days without a rollup are aggregated first
    (:func:`_rescue_unaggregated_expiring_days`); any that still lack one - a
    failed aggregation, or a day beyond this run's rescue budget - are left in
    place and retried on the next run rather than silently dropped.

    That is a deliberate fail-OPEN: a day whose aggregation keeps failing keeps
    its raw traces past the configured retention period (loudly - every failure
    logs an error, and the rescue logs how many days it could not build). Losing
    a day's KPIs is irreversible; retaining its traces for another hour is not.

    The same holds for a day whose rollup is recorded stale: its traces
    are the only source of the rebuild it is owed, so they are kept until
    :func:`_rebuild_stale_expiring_days` has rebuilt it.
    """
    cutoff = purge_cutoff(retention_days)
    total_deleted = 0

    try:
        await _rescue_unaggregated_expiring_days(db, cutoff)
        await _rebuild_stale_expiring_days(db, cutoff)

        while True:
            # Resolve the batch first: the offloaded payload objects belonging to
            # these traces have to be collected while the rows still point at
            # them, or they are orphaned in the bucket forever.
            batch_rows = await db.execute(
                text(
                    """
                    SELECT t.id
                    FROM llm_traces t
                    WHERE t.started_at < :cutoff
                      AND EXISTS (
                          SELECT 1
                          FROM llm_daily_stats s
                          WHERE s.date = (t.started_at AT TIME ZONE 'UTC')::date
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM llm_daily_stats_stale_days p
                          WHERE p.date = (t.started_at AT TIME ZONE 'UTC')::date
                      )
                    ORDER BY t.started_at
                    LIMIT :batch_size
                    """
                ),
                {"cutoff": cutoff, "batch_size": _PURGE_BATCH_SIZE},
            )
            batch_ids = [row[0] for row in batch_rows.all()]
            if not batch_ids:
                break

            # Both under the same ownership gate: under the ClickHouse provider
            # the objects belong to that purge, and sweeping them here would
            # strip the payloads out from under the rows still serving them.
            owns_objects = _postgres_owns_offloaded_objects()
            object_names = (
                await _offloaded_object_names(db, batch_ids) if owns_objects else []
            )
            sweep_ids = (
                await _traces_needing_object_sweep(db, batch_ids) if owns_objects else []
            )

            result = await db.execute(
                text("DELETE FROM llm_traces WHERE id = ANY(:ids)"),
                {"ids": batch_ids},
            )
            deleted = result.rowcount or 0
            if deleted <= 0:
                break

            total_deleted += deleted
            await db.commit()
            # After the commit: a failed object deletion must not roll back a
            # purge that already succeeded, and an object without a row is
            # merely garbage, while a row without its object is a broken trace.
            if object_names:
                purged_objects = await delete_object_names(object_names)
                logger.info(
                    "Trace retention purge deleted %d/%d offloaded payload and "
                    "media object(s)",
                    purged_objects,
                    len(object_names),
                )
            if sweep_ids:
                # Traces whose payload was itself offloaded may own media
                # objects referenced only from inside that stored payload.
                swept = await delete_trace_objects(sweep_ids)
                logger.info(
                    "Trace retention purge swept %d remaining object(s) for %d "
                    "trace(s) with offloaded payloads",
                    swept,
                    len(sweep_ids),
                )

            if deleted < _PURGE_BATCH_SIZE:
                break
            await asyncio.sleep(0.1)
    except Exception as exc:
        await db.rollback()
        logger.error("Trace retention purge failed: %s", format_exception_message(exc), exc_info=True)
        return 0

    logger.info(
        "Trace retention purge deleted %d trace(s) started before %s (retention %d day(s))",
        total_deleted,
        cutoff.isoformat(),
        retention_days,
    )
    return total_deleted


async def sync_clickhouse_trace_retention(
    retention_days: int,
    *,
    client_factory: Callable[[], Any] | None = None,
) -> list[str]:
    """Make the ClickHouse trace tables expire on the configured retention window.

    The counterpart to :func:`purge_expired_traces` for the ClickHouse provider.
    ClickHouse deletes by TTL rather than by statement, so retention there is
    declarative - nothing to run per row, but the declaration has to be kept in
    step with ``trace_retention_days``, which is a runtime DB setting while the
    trace DDL runs at first use with no session to read it from. Without this the
    ClickHouse tables carry no TTL at all and traces are kept forever, silently
    ignoring the configured retention.

    Reconciled rather than applied once, from two places: the hourly trace-storage
    maintenance tick (which fires on the first tick after every startup, so fresh
    and pre-existing tables both end up with a TTL) and the observability settings
    save (so changing retention takes effect immediately rather than within the
    hour). Both paths are best-effort - a ClickHouse that is down or read-only
    must not fail an admin's save or the maintenance tick, and the next tick
    retries - so failures are logged and swallowed.

    No-op unless the ClickHouse provider is selected and its credentials are
    configured. Returns the tables whose TTL was (re)declared.
    """
    from app.services.tracing.clickhouse_client import (
        HTTPClickHouseClient,
        has_clickhouse_runtime_config,
    )
    from app.services.tracing.store import get_trace_store_provider

    if get_trace_store_provider() != "clickhouse":
        return []
    if client_factory is None and not has_clickhouse_runtime_config():
        return []

    client = (client_factory or HTTPClickHouseClient.from_settings)()
    try:
        changed = await client.apply_trace_retention_ttl(retention_days)
    except Exception as exc:
        logger.error(
            "Failed to apply the %d-day trace retention TTL to ClickHouse: %s",
            retention_days,
            format_exception_message(exc),
            exc_info=True,
        )
        return []
    finally:
        await client.close()

    if changed:
        logger.info(
            "Declared a %d-day retention TTL on ClickHouse trace table(s): %s",
            retention_days,
            ", ".join(changed),
        )
    return changed


# Days of expired backlog one ClickHouse purge run takes on. Bounds both the
# object deletions (serial, one HTTP call each) and the row mutation's span when
# a large stretch expires at once - dropping retention from 365 to 7 expires
# ~358 days in one tick. Whatever is left over is purged by the next run.
_CH_PURGE_MAX_DAYS_PER_RUN = 31

# Payload columns that can hold an offload envelope, on both trace and span rows.
# Scores carry no payload, which is why they are not swept here.
_CH_OFFLOAD_TABLES: tuple[tuple[str, str], ...] = (("llm_traces", "started_at"), ("llm_spans", "started_at"))
_CH_OFFLOAD_COLUMNS: tuple[str, ...] = ("input", "output", "metadata")

# Column naming the owning trace, per table: the sweep is keyed by trace.
_CH_TRACE_ID_COLUMNS: dict[str, str] = {"llm_traces": "id", "llm_spans": "trace_id"}


def _ch_datetime_literal(value: datetime) -> str:
    """``value`` as a ClickHouse DateTime64(3, 'UTC') literal."""
    utc = value.astimezone(UTC)
    return f"toDateTime64('{utc.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}', 3, 'UTC')"


def _ch_offloaded_object_names_sql(before: str) -> str:
    """Query returning every offloaded object name owned by expiring rows.

    Reads the envelopes out of the JSON the sink stored in ClickHouse's ``String``
    payload columns. A column holding a non-envelope payload (or no JSON at all)
    yields 0 from ``JSONExtractInt`` and is filtered out, so this only ever
    returns names an offload actually wrote.
    """
    # The suppressions below are safe: every interpolated piece is a module
    # constant (table, column, envelope marker) or a ClickHouse datetime literal
    # this module built from a datetime. Nothing comes from a request or a row.
    selects = [
        f"SELECT DISTINCT JSONExtractString({column}, 'object_name') AS object_name "  # noqa: S608
        f"FROM {table} "
        f"WHERE {time_column} < {before} "
        f"AND JSONExtractInt({column}, '{ENVELOPE_KEY}') = {ENVELOPE_VERSION}"
        for table, time_column in _CH_OFFLOAD_TABLES
        for column in _CH_OFFLOAD_COLUMNS
    ]
    return (
        "SELECT DISTINCT object_name FROM ("  # noqa: S608
        + " UNION ALL ".join(selects)
        + ") WHERE object_name != ''"
    )


def _ch_object_sweep_trace_ids_sql(before: str) -> str:
    """Query returning the expiring traces whose objects need a prefix sweep.

    The envelope query above can only find pointers sitting at the *top level* of
    a payload column, which is where a payload envelope sits.  Two things escape
    it, and neither is reachable with ``JSONExtract*``:

      * a **media reference**, which replaces one blob inside the payload and so
        nests as deep as the content part it stood in.  ClickHouse has no
        recursive JSON descent (the PostgreSQL side uses ``jsonb_path_query``
        with ``$.**``), so the marker is located with a substring test and the
        objects are then found by prefix instead of by name.
      * media referenced from inside a payload that was *itself* offloaded — the
        row holds only the envelope, so the reference is not in ClickHouse at all.

    Restricted to rows carrying one of the two markers: the overwhelming majority
    carry neither, and they must not each cost an object-storage LIST during a
    purge that expires a whole window at once.
    """
    # Same suppression rationale as ``_ch_offloaded_object_names_sql``: every
    # interpolated piece is a module constant or a datetime literal this module
    # built. Nothing comes from a request or a row.
    marker_test = " OR ".join(
        f"position({column}, '{marker}') > 0"
        for column in _CH_OFFLOAD_COLUMNS
        for marker in (MEDIA_KEY, ENVELOPE_KEY)
    )
    selects = [
        f"SELECT DISTINCT toString({_CH_TRACE_ID_COLUMNS[table]}) AS trace_id "  # noqa: S608
        f"FROM {table} "
        f"WHERE {time_column} < {before} "
        f"AND ({marker_test})"
        for table, time_column in _CH_OFFLOAD_TABLES
    ]
    return (
        "SELECT DISTINCT trace_id FROM ("  # noqa: S608
        + " UNION ALL ".join(selects)
        + ") WHERE trace_id != ''"
    )


async def purge_expired_clickhouse_traces(
    retention_days: int,
    *,
    client_factory: Callable[[], Any] | None = None,
) -> int:
    """Delete expired ClickHouse traces and their offloaded payload objects.

    Runs in the same maintenance cycle as :func:`purge_expired_traces`, and is
    the owner of the payload objects under the ClickHouse provider (see
    :func:`_postgres_owns_offloaded_objects`). The order is forced by the data:
    an object can only be found through a row that points at it, so the pointers
    are read first, the rows deleted, and the objects deleted last - the same
    ordering the PostgreSQL purge uses, and for the same reason (a row without
    its payload is a broken trace, an object without its row is only garbage).

    This is why the TTL declared by :func:`sync_clickhouse_trace_retention` sits
    a margin BEYOND the retention window instead of on it: a TTL on the exact
    boundary would let ClickHouse drop rows before this sweep read their
    pointers, orphaning the objects. In normal operation this sweep enforces the
    window and the TTL never fires; the TTL is what keeps retention honest when
    the maintenance loop is not running at all.

    Rows are deleted with ``ALTER TABLE ... DELETE`` (a mutation) rather than
    ``DELETE FROM``: ClickHouse is operator-provided here, and the lightweight
    form needs a version we cannot assume. ``mutations_sync=1`` makes the call
    return only once the rows are actually gone, so the objects are never deleted
    while their rows are still queryable.

    Bounded to ``_CH_PURGE_MAX_DAYS_PER_RUN`` days of backlog per run. Returns
    the number of payload objects deleted.
    """
    from app.services.tracing.clickhouse_client import (
        HTTPClickHouseClient,
        has_clickhouse_runtime_config,
    )
    from app.services.tracing.store import get_trace_store_provider

    if get_trace_store_provider() != "clickhouse":
        return 0
    if client_factory is None and not has_clickhouse_runtime_config():
        return 0

    cutoff = purge_cutoff(retention_days)
    client = (client_factory or HTTPClickHouseClient.from_settings)()
    purged_objects = 0
    try:
        earliest = await _clickhouse_earliest_trace(client)
        if earliest is None or earliest >= cutoff:
            return 0

        # One window per run, ending a bounded number of days after the oldest
        # trace ClickHouse still holds. A single window (rather than a mutation
        # per day) keeps this to one pass over the affected parts.
        earliest_day = datetime.combine(earliest.date(), datetime.min.time(), tzinfo=UTC)
        window_end = min(cutoff, earliest_day + timedelta(days=_CH_PURGE_MAX_DAYS_PER_RUN))
        before = _ch_datetime_literal(window_end)

        object_names = [
            str(row["object_name"])
            for row in await client.query_json_rows(_ch_offloaded_object_names_sql(before))
            if row.get("object_name")
        ]
        # Resolved before the rows go, for the same reason the names are: a trace
        # id that is no longer in ClickHouse cannot be recovered from anywhere.
        sweep_trace_ids = [
            str(row["trace_id"])
            for row in await client.query_json_rows(_ch_object_sweep_trace_ids_sql(before))
            if row.get("trace_id")
        ]

        for table, time_column in _CH_OFFLOAD_TABLES:
            await client.execute(
                f"ALTER TABLE {table} DELETE WHERE {time_column} < {before}",
                settings={"mutations_sync": 1},
            )

        if object_names:
            purged_objects = await delete_object_names(object_names)
            logger.info(
                "ClickHouse retention purge deleted %d/%d offloaded payload object(s)",
                purged_objects,
                len(object_names),
            )
        if sweep_trace_ids:
            # Media objects, which are referenced from too deep inside the
            # payload for ClickHouse to address by name.
            swept = await delete_trace_objects(sweep_trace_ids)
            purged_objects += swept
            logger.info(
                "ClickHouse retention purge swept %d media/remaining object(s) for "
                "%d trace(s)",
                swept,
                len(sweep_trace_ids),
            )

        logger.info(
            "ClickHouse retention purge deleted trace rows started before %s "
            "(retention %d day(s), cutoff %s)",
            window_end.isoformat(),
            retention_days,
            cutoff.isoformat(),
        )
        if window_end < cutoff:
            logger.warning(
                "ClickHouse retention backlog exceeds this run's %d-day budget - traces "
                "between %s and the %s cutoff are purged by a later run",
                _CH_PURGE_MAX_DAYS_PER_RUN,
                window_end.isoformat(),
                cutoff.isoformat(),
            )
    except Exception as exc:
        # Fail open and retry next tick. The rows still point at every object
        # that was not deleted, so a failure defers the purge rather than
        # orphaning anything - the one thing this function must never do.
        logger.error(
            "ClickHouse retention purge failed: %s",
            format_exception_message(exc),
            exc_info=True,
        )
    finally:
        await client.close()

    return purged_objects


async def _clickhouse_earliest_trace(client: Any) -> datetime | None:
    """Oldest ``started_at`` ClickHouse holds, or None when it holds nothing."""
    rows = await client.query_json_rows("SELECT min(started_at) AS earliest FROM llm_traces")
    earliest = (rows[0] if rows else {}).get("earliest")
    if isinstance(earliest, str):
        try:
            earliest = datetime.fromisoformat(earliest)
        except ValueError:
            return None
    if not isinstance(earliest, datetime):
        return None
    if earliest.tzinfo is None:
        earliest = earliest.replace(tzinfo=UTC)
    # ClickHouse answers an empty table with the DateTime zero value, not NULL.
    return earliest if earliest.year > 1970 else None


_HistogramKey = tuple[object, object, str, str, object, object, object]

#: The rollup's grain past the day (``uq_llm_daily_stats_date_agent_model_source_user``),
#: as the day's trace scans group by it.
_GRAIN_SQL = (
    "t.agent_id, t.user_id, COALESCE(t.model, ''), t.source, t.attributed_user_id, t.attribution, t.department_id"
)


def _grain_key(row: Any) -> _HistogramKey:
    return (
        row["agent_id"],
        row["user_id"],
        row["model"],
        row["source"],
        row["attributed_user_id"],
        row["attribution"],
        row["department_id"],
    )


async def _duration_histograms_for_day(
    db: AsyncSession, day_window: dict[str, datetime]
) -> dict[_HistogramKey, dict[str, object] | None]:
    """Per-group latency bucket counts for one day, keyed like the rollup rows.

    Bucketed in SQL (one row per group *and bucket*, so a busy day yields tens of
    rows rather than one per trace) and folded into the stored JSON shape here.
    This is the state that makes a window's percentiles computable from the
    rollup: bucket counts add across days and groups, per-group percentiles do
    not (see services/tracing/latency_histogram.py).
    """
    bucket_expr = bucket_expression_sql("t.duration_ms")
    rows = (
        await db.execute(
            text(
                f"""
                SELECT
                    t.agent_id,
                    t.user_id,
                    COALESCE(t.model, '') AS model,
                    t.source,
                    t.attributed_user_id,
                    t.attribution,
                    t.department_id,
                    {bucket_expr} AS bucket,
                    COUNT(*) AS bucket_count
                FROM llm_traces t
                WHERE t.started_at >= :day_start
                  AND t.started_at < :day_end
                  AND t.source = ANY(:sources)
                  AND t.duration_ms IS NOT NULL
                GROUP BY {_GRAIN_SQL}, {bucket_expr}
                """  # noqa: S608 - bucket_expr and the grain are module constants, not input
            ),
            day_window,
        )
    ).mappings().all()

    counts: dict[_HistogramKey, dict[int, int]] = {}
    for row in rows:
        counts.setdefault(_grain_key(row), {})[int(row["bucket"])] = int(row["bucket_count"] or 0)
    return {key: encode_histogram(buckets) for key, buckets in counts.items()}


def _day_window(target: date) -> dict[str, Any]:
    """The bounds of one UTC day's trace scans.

    Every allowed source, so each day scan is a range on
    ``ix_llm_traces_source_started_at``: with no index on ``started_at``
    alone, a bare range reads the whole of another composite index.
    """
    day_start = datetime.combine(target, datetime.min.time(), tzinfo=UTC)
    return {
        "day_start": day_start,
        "day_end": day_start + timedelta(days=1),
        "sources": list(LLM_TRACE_SOURCES),
    }


async def _day_rollup_rows(db: AsyncSession, target: date) -> list[dict[str, Any]]:
    """The ``llm_daily_stats`` rows of *target*, built from its traces as they
    are now. Writes nothing; ``[]`` for a day without traces."""
    day_window = _day_window(target)

    rows = (
        await db.execute(
            text(
                f"""
                SELECT
                    t.agent_id,
                    t.user_id,
                    COALESCE(t.model, '') AS model,
                    t.source,
                    t.attributed_user_id,
                    t.attribution,
                    t.department_id,
                    COUNT(*) AS trace_count,
                    COUNT(*) FILTER (WHERE t.status = 'error') AS error_count,
                    COALESCE(SUM(t.input_tokens), 0) AS total_input_tokens,
                    COALESCE(SUM(t.output_tokens), 0) AS total_output_tokens,
                    COALESCE(SUM(t.cached_input_tokens), 0) AS total_cached_input_tokens,
                    COALESCE(
                        SUM(t.total_cost) FILTER (WHERE t.total_cost IS NOT NULL),
                        0
                    ) AS total_cost,
                    AVG(t.duration_ms) AS avg_duration_ms,
                    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.duration_ms) AS p50_duration_ms,
                    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY t.duration_ms) AS p95_duration_ms,
                    AVG(fb.feedback_value) AS avg_feedback_score,
                    COALESCE(BOOL_OR(t.department_backfilled), false) AS department_backfilled,
                    MAX(t.agent_name_snapshot) AS agent_name_snapshot
                FROM llm_traces t
                LEFT JOIN ({FEEDBACK_PER_TRACE_SQL_POSTGRES_ONE_DAY}) fb ON fb.trace_id = t.id
                WHERE t.started_at >= :day_start
                  AND t.started_at < :day_end
                  AND t.source = ANY(:sources)
                GROUP BY {_GRAIN_SQL}
                """  # noqa: S608 - the feedback rule and the grain are module constants, not input
            ),
            day_window,
        )
    ).mappings().all()
    if not rows:
        return []

    histograms = await _duration_histograms_for_day(db, day_window)
    return [
        {
            "date": target,
            "agent_id": row["agent_id"],
            "user_id": row["user_id"],
            "model": row["model"],
            "source": row["source"],
            "attributed_user_id": row["attributed_user_id"],
            "attribution": row["attribution"],
            "department_id": row["department_id"],
            "department_backfilled": bool(row["department_backfilled"]),
            "agent_name_snapshot": row["agent_name_snapshot"],
            "trace_count": int(row["trace_count"] or 0),
            "error_count": int(row["error_count"] or 0),
            "total_input_tokens": int(row["total_input_tokens"] or 0),
            "total_output_tokens": int(row["total_output_tokens"] or 0),
            "total_cached_input_tokens": int(row["total_cached_input_tokens"] or 0),
            "total_cost": row["total_cost"],
            "avg_duration_ms": row["avg_duration_ms"],
            "p50_duration_ms": row["p50_duration_ms"],
            "p95_duration_ms": row["p95_duration_ms"],
            "duration_histogram": histograms.get(_grain_key(row)),
            "avg_feedback_score": row["avg_feedback_score"],
        }
        for row in rows
    ]


async def _insert_rollup_rows(db: AsyncSession, payload: list[dict[str, Any]]) -> None:
    # ON CONFLICT DO NOTHING is belt-and-suspenders on top of the advisory
    # lock: a retry after a partially-applied batch (rows inserted, then a
    # crash before last_aggregation_at committed) re-runs cleanly instead
    # of failing the whole batch on the unique constraint.
    await db.execute(
        pg_insert(LLMDailyStats).on_conflict_do_nothing(
            constraint="uq_llm_daily_stats_date_agent_model_source_user"
        ),
        payload,
    )


async def aggregate_daily_stats(db: AsyncSession, target_date: date | None = None) -> int:
    """Aggregate per-day stats into llm_daily_stats and update last_aggregation_at."""
    target = target_date or (datetime.now(UTC).date() - timedelta(days=1))

    try:
        # Serialize across replicas: every web replica runs this maintenance at
        # ~01:00 UTC, so without a lock they all pass the date guard and race to
        # INSERT - the loser's whole batch then rolls back on the unique
        # constraint and a busy day's aggregate can be lost. A txn-scoped advisory
        # lock (released on commit/rollback) makes exactly one replica build the
        # day; the rest see the existing row below and skip. Mirrors the
        # startup-migration advisory-lock pattern.
        await db.execute(text(DAILY_STATS_LOCK_SQL))

        existing = await db.execute(
            text(
                """
                SELECT 1
                FROM llm_daily_stats
                WHERE date = :target_date
                LIMIT 1
                """
            ),
            {"target_date": target},
        )
        existing_row = existing.scalar_one_or_none()
        if existing_row is not None:
            # Release the txn-scoped advisory lock now. Returning without a
            # commit/rollback would leave this transaction (and the lock) open
            # until the caller's session happens to end, blocking other
            # replicas' aggregation/backfill and pinning a connection. Nothing
            # was written on this path, so roll back.
            await db.rollback()
            return 0

        payload = await _day_rollup_rows(db, target)
        if payload:
            await _insert_rollup_rows(db, payload)

        await db.execute(text(_LAST_AGGREGATION_SQL), {"now_utc": datetime.now(UTC)})

        await db.commit()
        logger.info(
            "Daily trace stats aggregation complete for %s: %d row(s) inserted",
            target.isoformat(),
            len(payload),
        )
        return len(payload)
    except Exception as exc:
        await db.rollback()
        logger.error(
            "Daily trace stats aggregation failed for %s: %s",
            target.isoformat(),
            format_exception_message(exc),
            exc_info=True,
        )
        return 0


# The record a feedback writer leaves with its score, without the rollup lock
# (``record_feedback_day_stale``). A rebuild clears only the mark whose
# ``marked_at`` it read, so a mark landing while it runs is kept - as long as
# that mark never carries the value the rebuild read. The clock alone does not
# promise it (a coarse clock, two marks in one microsecond, a clock set back):
# a mark moves ``marked_at`` past the one it replaces.
_RECORD_STALE_DAY_SQL = """
    INSERT INTO llm_daily_stats_stale_days AS recorded (date, marked_at)
    VALUES (:day, clock_timestamp())
    ON CONFLICT (date) DO UPDATE
    SET marked_at = GREATEST(EXCLUDED.marked_at, recorded.marked_at + INTERVAL '1 microsecond')
"""
_CLEAR_STALE_DAY_SQL = "DELETE FROM llm_daily_stats_stale_days WHERE date = :day AND marked_at = :marked_at"


async def mark_daily_stats_stale(db: AsyncSession, days: Iterable[date]) -> None:
    """Record, in the caller's transaction, that the rollups of *days* are stale.

    Only a day that has a rollup is recorded: one without is built from the
    traces as they are when the backfill (or the purge's rescue) reaches it.
    Its rows stay readable until :func:`rebuild_daily_stats` replaces them, so
    committing this with the change that made them stale leaves no day without
    its rollup, and no rebuild the change owes that a crash could lose.

    Under the rollup lock: a rebuild running concurrently either committed
    before this reads the day's rows (and read the change) or starts after
    this commits (and finds the record).
    """
    targets = sorted(set(days))
    if not targets:
        return
    await db.execute(text(DAILY_STATS_LOCK_SQL))
    await db.execute(
        text(
            """
            INSERT INTO llm_daily_stats_stale_days (date)
            SELECT DISTINCT s.date FROM llm_daily_stats s WHERE s.date = ANY(:days)
            ON CONFLICT (date) DO NOTHING
            """
        ),
        {"days": targets},
    )


async def rebuild_daily_stats(
    db: AsyncSession,
    target: date,
    *,
    now: datetime | None = None,
    expired_before: date | None = None,
    build_missing: bool = False,
) -> int:
    """Bring the rollup of *target* up to date with its traces, in one transaction.

    - A day recorded stale has its rows replaced by rows built from its traces
      as they are now. The delete and the insert commit together, under the
      rollup lock, so a rollup-served read sees the old rows or the new ones -
      never a day without its rollup - and the record is cleared with them. It
      is kept when the day is not complete yet (:func:`latest_complete_day`),
      so the backfill rebuilds the day once more when it is, and when it was
      marked again after this read it (:func:`record_feedback_day_stale`), so
      the next run reads the change that mark records.
    - The stale rows are kept, and the record cleared, when the rebuild would
      replace them with less: no trace left to build from, or, for a day the
      retention purge may be deleting (before *expired_before*), fewer traces
      than the rows count.
    - A day with no rollup is built only with *build_missing*, once complete:
      until then the read path serves it from its traces.
    - A day that is not recorded stale is up to date and left alone.

    Returns the number of rollup rows written, 0 when nothing was. A failure
    rolls back - the stale rows and their record stay for a later run - and is
    logged, never raised.
    """
    now_utc = now or datetime.now(UTC)
    complete = target <= latest_complete_day(now_utc)
    day = {"day": target}
    try:
        await db.execute(text(DAILY_STATS_LOCK_SQL))
        has_rows = (
            await db.execute(text("SELECT 1 FROM llm_daily_stats WHERE date = :day LIMIT 1"), day)
        ).scalar_one_or_none() is not None
        marked_at = (
            await db.execute(text("SELECT marked_at FROM llm_daily_stats_stale_days WHERE date = :day"), day)
        ).scalar_one_or_none()
        stale = marked_at is not None
        record = {**day, "marked_at": marked_at}

        if has_rows and not stale:
            await db.rollback()
            return 0
        if not has_rows and not (build_missing and complete):
            # A record without rows (removed by hand, or left by a feedback
            # writer before the day was built) leaves a plain missing day,
            # which is the backfill's.
            if stale:
                await db.execute(text(_CLEAR_STALE_DAY_SQL), record)
                await db.commit()
            else:
                await db.rollback()
            return 0

        payload = await _day_rollup_rows(db, target)
        if has_rows:
            kept = None
            if not payload:
                kept = "no trace of the day is left"
            elif expired_before is not None and target < expired_before:
                stored = (
                    await db.execute(
                        text("SELECT COALESCE(SUM(trace_count), 0) FROM llm_daily_stats WHERE date = :day"),
                        day,
                    )
                ).scalar_one()
                if sum(row["trace_count"] for row in payload) < int(stored or 0):
                    kept = "the retention purge has deleted part of the day"
            if kept is not None:
                await db.execute(text(_CLEAR_STALE_DAY_SQL), record)
                await db.commit()
                logger.warning(
                    "Kept the stale daily-stats rollup of %s: %s", target.isoformat(), kept
                )
                return 0
            await db.execute(text("DELETE FROM llm_daily_stats WHERE date = :day"), day)
        if payload:
            await _insert_rollup_rows(db, payload)
        if stale and complete:
            await db.execute(text(_CLEAR_STALE_DAY_SQL), record)
        await db.execute(text(_LAST_AGGREGATION_SQL), {"now_utc": now_utc})
        await db.commit()
        logger.info(
            "Daily trace stats %s for %s: %d row(s) written",
            "rebuild" if has_rows else "aggregation",
            target.isoformat(),
            len(payload),
        )
        return len(payload)
    except Exception as exc:
        await db.rollback()
        logger.error(
            "Daily trace stats rebuild failed for %s: %s",
            target.isoformat(),
            format_exception_message(exc),
            exc_info=True,
        )
        return 0


_STALE_DAYS_SQL = {
    True: (
        "SELECT date FROM llm_daily_stats_stale_days "
        "WHERE date BETWEEN :first AND :last ORDER BY date DESC LIMIT :limit"
    ),
    False: (
        "SELECT date FROM llm_daily_stats_stale_days "
        "WHERE date BETWEEN :first AND :last ORDER BY date ASC LIMIT :limit"
    ),
}


async def _stale_days(
    db: AsyncSession, *, first: date, last: date, limit: int, newest_first: bool
) -> list[date]:
    """The days recorded stale between *first* and *last*, at most *limit*.

    Ends the read's transaction: every rebuild takes its own lock and commits.
    """
    rows = (
        await db.execute(
            text(_STALE_DAYS_SQL[newest_first]),
            {"first": first, "last": last, "limit": limit},
        )
    ).all()
    await db.rollback()
    return [row[0] for row in rows]


_BACKFILL_MAX_DAYS_PER_RUN = 14

# A rollup's feedback can go stale after the day is built: a rating given or
# changed later. Each run re-reads the feedback of the last few complete days,
# and an older day is recorded stale when its feedback changes
# (``record_feedback_day_stale``); once, it re-reads every retained day, which is
# what corrects the rollups built before feedback counted one rating per rater -
# after the chat scores of that time that the rule may misread are
# rewritten (below).
FEEDBACK_REFRESH_RECENT_DAYS = 7


def feedback_refresh_earliest(retention_days: int | None, now: datetime) -> date:
    """The oldest day the feedback refresh may read: the day after the oldest
    retained one, which the retention purge may already be deleting."""
    return now.date() - timedelta(days=max(1, int(retention_days or 90)) - 1)


def feedback_refresh_floor(*, earliest: date, latest: date) -> date:
    """The oldest day the recent feedback refresh reads: the last
    ``FEEDBACK_REFRESH_RECENT_DAYS`` complete days up to *latest*, none before
    *earliest*. It moves a day a day at most: *latest* at 01:00 UTC
    (:func:`latest_complete_day`), *earliest* at midnight UTC."""
    return max(earliest, latest - timedelta(days=FEEDBACK_REFRESH_RECENT_DAYS - 1))


# The retention the backfill runs with, off the settings singleton as
# ``get_platform_settings`` reads it.
_TRACE_RETENTION_DAYS = (
    select(PlatformSettings.trace_retention_days)
    .order_by(PlatformSettings.created_at.asc(), PlatformSettings.id.asc())
    .limit(1)
)

FEEDBACK_RULE_REFRESH_MARKER = "llm_daily_stats:feedback_rule_refreshed:4308"
# Held by the one process running the full pass: every web process runs the
# backfill, and a pass that outlasts the scheduler's 55 s window would start
# again in the next one. Outlives any real pass; a crashed owner's claim lapses.
FEEDBACK_RULE_REFRESH_CLAIM = "llm_daily_stats:feedback_rule_refresh_claim:4308"
_FEEDBACK_RULE_REFRESH_CLAIM_TTL_SECONDS = 3600
_RELEASE_IF_OWNER_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


async def refresh_daily_feedback(db: AsyncSession, target: date) -> int:
    """Recompute ``avg_feedback_score`` of ``target``'s rollup rows from its raw traces.

    Only ``avg_feedback_score`` is touched, and only on rows whose group still
    has raw traces, so a rollup that outlived its traces keeps its value.
    Returns the number of rows whose value changed.
    """
    await db.execute(text(DAILY_STATS_LOCK_SQL))
    result = await db.execute(
        text(
            f"""
            UPDATE llm_daily_stats d
            SET avg_feedback_score = f.avg_feedback_score
            FROM (
                SELECT
                    t.agent_id,
                    t.user_id,
                    COALESCE(t.model, '') AS model,
                    t.source,
                    t.attributed_user_id,
                    t.attribution,
                    t.department_id,
                    AVG(fb.feedback_value) AS avg_feedback_score
                FROM llm_traces t
                LEFT JOIN ({FEEDBACK_PER_TRACE_SQL_POSTGRES_ONE_DAY}) fb ON fb.trace_id = t.id
                WHERE t.started_at >= :day_start
                  AND t.started_at < :day_end
                  AND t.source = ANY(:sources)
                GROUP BY {_GRAIN_SQL}
            ) f
            WHERE d.date = :target_date
              AND d.agent_id IS NOT DISTINCT FROM f.agent_id
              AND d.user_id IS NOT DISTINCT FROM f.user_id
              AND d.model = f.model
              AND d.source = f.source
              AND d.attributed_user_id IS NOT DISTINCT FROM f.attributed_user_id
              AND d.attribution IS NOT DISTINCT FROM f.attribution
              AND d.department_id IS NOT DISTINCT FROM f.department_id
              AND d.avg_feedback_score IS DISTINCT FROM f.avg_feedback_score
            """  # noqa: S608 - the feedback rule and the grain are module constants, not input
        ),
        {**_day_window(target), "target_date": target},
    )
    await db.commit()
    return int(result.rowcount or 0)


async def _claim_full_feedback_refresh() -> str | None:
    """Claim the one pass over every retained day.

    ``None`` when it is done, runs in another process, or Redis cannot say:
    this run then refreshes the recent days only.
    """
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        if await redis.exists(FEEDBACK_RULE_REFRESH_MARKER):
            return None
        token = uuid.uuid4().hex
        claimed = await redis.set(
            FEEDBACK_RULE_REFRESH_CLAIM, token, nx=True, ex=_FEEDBACK_RULE_REFRESH_CLAIM_TTL_SECONDS
        )
        return token if claimed else None
    except Exception as exc:
        logger.warning("Feedback rollup refresh: marker unreadable, recent days only: %s", format_exception_message(exc))
        return None


async def _finish_full_feedback_refresh(token: str, *, done: bool) -> None:
    """Record a clean full pass, and release the claim either way."""
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        if done:
            await redis.set(FEEDBACK_RULE_REFRESH_MARKER, "1")
        await redis.eval(_RELEASE_IF_OWNER_LUA, 1, FEEDBACK_RULE_REFRESH_CLAIM, token)
    except Exception as exc:
        logger.warning("Feedback rollup refresh: marker not written: %s", format_exception_message(exc))


# The older chat writer appended a row per action, with no message
# identity: one message re-rated and two messages of one turn rated differently
# leave the same rows, and the latest row reads only the first right. So a
# rater's score the rule may misread - several such rows, the latest of them one
# too - is rebuilt from their ratings of the trace's messages that still stand,
# as a chat action writes it now. A pair left alone (its conversation deleted,
# every rating retracted) keeps the latest-row reading.
_LEGACY_CHAT_SCORES_PER_RUN = 500
_LEGACY_CHAT_SCORES_SQL = f"""
    SELECT held.trace_id, held.user_id, held.created_at
    FROM (
        SELECT
            trace_id,
            user_id,
            created_at,
            COALESCE(jsonb_typeof(metadata), 'null') AS metadata_type,
            ROW_NUMBER() OVER (
                PARTITION BY trace_id, user_id ORDER BY created_at DESC, id DESC
            ) AS rank,
            COUNT(*) OVER (PARTITION BY trace_id, user_id) AS row_count
        FROM llm_scores
        WHERE name = '{FEEDBACK_SCORE_NAME}' AND user_id IS NOT NULL
    ) held
    WHERE held.rank = 1
      AND held.row_count > 1
      AND held.metadata_type = 'null'
      AND EXISTS (
          SELECT 1
          FROM message_feedback f
          JOIN messages m ON m.id = f.message_id
          WHERE m.llm_trace_id = held.trace_id AND f.user_id = held.user_id
      )
    ORDER BY held.trace_id, held.user_id
    LIMIT :limit
"""  # noqa: S608 - built from module constants, not input


@asynccontextmanager
async def _clickhouse_score_copy() -> AsyncIterator[Callable[[dict[str, Any]], Awaitable[bool]] | None]:
    """Copies a rewritten score to ClickHouse, whose rows keep a rater's latest
    row; ``None`` when PostgreSQL is the store.

    The copy says whether ClickHouse took the score: the sink logs a failed
    insert and carries on, counting it in ``tracing_errors``.
    """
    from app.services.tracing.store import get_trace_store_provider

    if get_trace_store_provider() != "clickhouse":
        yield None
        return
    from app.services.tracing.clickhouse_sink import ClickHouseSink

    sink = ClickHouseSink()

    async def _copy(payload: dict[str, Any]) -> bool:
        errors = sink.tracing_errors
        await sink.write_score(payload)
        return sink.tracing_errors == errors

    try:
        yield _copy
    finally:
        await sink.close()


async def rewrite_legacy_chat_scores(db: AsyncSession, *, limit: int = _LEGACY_CHAT_SCORES_PER_RUN) -> bool:
    """Rewrite the chat scores of an older writer the rule may misread (see above).

    Each is stored like a chat action (``rater_score_payload``, the sink's
    ``write_score_row``, under the rater's lock) and timed one millisecond after
    the rater's last old action, so any later action - a rating since, an admin
    REST score written to ClickHouse alone - still wins, in either store. At
    most *limit* per run; True once none is left.
    """
    try:
        pairs = (await db.execute(text(_LEGACY_CHAT_SCORES_SQL), {"limit": limit + 1})).all()
    except Exception as exc:
        await db.rollback()
        logger.error("Old chat feedback rewrite: scores unreadable: %s", format_exception_message(exc), exc_info=True)
        return False
    await db.rollback()

    rewritten = 0
    failed = False
    async with _clickhouse_score_copy() as copy_to_clickhouse:
        for trace_id, user_id, last_action_at in pairs[:limit]:
            try:
                await lock_rater_score(db, trace_id, user_id, FEEDBACK_SCORE_NAME)
                payload = await rater_score_payload(
                    db,
                    trace_id=trace_id,
                    user_id=user_id,
                    created_at=last_action_at + timedelta(milliseconds=1),
                )
                # From ratings that still stand only: one retracted since the read
                # sent its own score, one deleted with its conversation sent none.
                applied = not payload.get("retract") and await write_score_row(db, payload, trace_id)
                # Copied before the commit: a pair rewritten here is never selected
                # again, so a score ClickHouse did not take would stay misread there.
                copied = not applied or copy_to_clickhouse is None or await copy_to_clickhouse(payload)
                if copied:
                    await db.commit()
                else:
                    await db.rollback()
            except Exception as exc:
                failed = True
                await db.rollback()
                logger.error(
                    "Old chat feedback rewrite failed for trace %s: %s",
                    trace_id,
                    format_exception_message(exc),
                    exc_info=True,
                )
                continue
            if not copied:
                # ClickHouse is likely down: the rest waits for the next run.
                failed = True
                logger.error("Old chat feedback rewrite stopped: ClickHouse did not take the score of trace %s", trace_id)
                break
            rewritten += int(applied)
    if rewritten:
        logger.info("Rewrote %d chat feedback score(s) written by an older version", rewritten)
    return not failed and len(pairs) <= limit


async def refresh_rollup_feedback(db: AsyncSession, *, earliest: date, latest: date) -> int:
    """Bring the rollups' feedback in line with their raw traces (see above).

    The pass over every retained day reads the old chat scores rewritten: until
    :func:`rewrite_legacy_chat_scores` is done, its owner rewrites a batch per
    run and refreshes the recent days only.
    """
    token = await _claim_full_feedback_refresh()
    full = token is not None and await rewrite_legacy_chat_scores(db)
    floor = earliest if full else feedback_refresh_floor(earliest=earliest, latest=latest)
    changed = 0
    failed = False
    day = latest
    while day >= floor:
        try:
            changed += await refresh_daily_feedback(db, day)
        except Exception as exc:
            failed = True
            await db.rollback()
            logger.error(
                "Feedback rollup refresh failed for %s: %s",
                day.isoformat(),
                format_exception_message(exc),
                exc_info=True,
            )
        day -= timedelta(days=1)
    if token:
        await _finish_full_feedback_refresh(token, done=full and not failed)
    return changed


async def record_feedback_day_stale(
    db: AsyncSession, trace_id: uuid.UUID, *, now: datetime | None = None
) -> date | None:
    """Record, with the caller's score write, the rollup of *trace_id*'s day stale.

    Every run re-reads the feedback of the last ``FEEDBACK_REFRESH_RECENT_DAYS``
    complete days only, so a rating given, changed or retracted on an older
    trace left its day's rollup stale for good. That day is recorded like a
    repriced one: the backfill rebuilds it from its traces, feedback
    included, and the purge keeps its traces until then. Every day the recent
    refresh does not read is recorded, and so is the oldest one it does
    (:func:`feedback_refresh_floor`), which may leave the window before the
    next run: the last complete day moves at 01:00 UTC, and under a retention
    shorter than the window the retention's oldest day moves at midnight UTC.
    A day that is not complete yet has no rollup: it is built from its traces,
    this score included. If no run comes for more than a day, a day the
    refresh read at the time of the rating can still leave the window unread.

    In the caller's transaction, so a crash cannot separate the record from
    the score, and without the rollup lock, so a score write never waits on a
    rollup being built: a rebuild keeps a record marked after it read it
    (``_CLEAR_STALE_DAY_SQL``), and a day not built yet is built from its
    traces. In a savepoint: a failure is logged and never fails the score.
    Returns the day recorded, ``None`` when none was.
    """
    now_utc = now or datetime.now(UTC)
    # The caller's writes stay out of the savepoint: rolling it back must not undo them.
    await db.flush()
    try:
        async with db.begin_nested():
            started_at = await db.scalar(select(LLMTrace.started_at).where(LLMTrace.id == trace_id))
            if started_at is None:
                return None
            day = (started_at if started_at.tzinfo else started_at.replace(tzinfo=UTC)).astimezone(UTC).date()
            latest = latest_complete_day(now_utc)
            if day > latest:
                return None
            if day > latest - timedelta(days=FEEDBACK_REFRESH_RECENT_DAYS - 1):
                # In the recent window, unless the retention is shorter than it.
                retention_days = await db.scalar(_TRACE_RETENTION_DAYS)
                earliest = feedback_refresh_earliest(retention_days, now_utc)
                if day > feedback_refresh_floor(earliest=earliest, latest=latest):
                    return None
            await db.execute(text(_RECORD_STALE_DAY_SQL), {"day": day})
            return day
    except Exception as exc:
        logger.warning(
            "Could not record the rollup of trace %s stale after a feedback change: %s",
            trace_id,
            format_exception_message(exc),
            exc_info=True,
        )
        return None


async def _day_has_traces(db: AsyncSession, target: date) -> bool:
    """True when at least one trace started on *target* (UTC).

    Filtered on every allowed ``source`` so the probe is an index range scan on
    ``ix_llm_traces_source_started_at`` - ``llm_traces`` has no index on
    ``started_at`` alone, and an unfiltered probe would scan the table.
    """
    day_start = datetime.combine(target, datetime.min.time(), tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    result = await db.execute(
        select(
            exists().where(
                LLMTrace.source.in_(LLM_TRACE_SOURCES),
                LLMTrace.started_at >= day_start,
                LLMTrace.started_at < day_end,
            )
        )
    )
    return bool(result.scalar())


async def _touch_last_aggregation(db: AsyncSession, now_utc: datetime) -> None:
    """Record that daily-stats maintenance ran. Refreshing the timestamp even
    when nothing needed aggregating is what clears the 'stats may be incomplete'
    banner on a healthy, idle, or brand-new platform."""
    try:
        await db.execute(
            text("UPDATE platform_settings SET last_aggregation_at = :now_utc"),
            {"now_utc": now_utc},
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.error("Failed to record last_aggregation_at: %s", format_exception_message(exc), exc_info=True)


async def run_daily_stats_backfill(
    db: AsyncSession,
    *,
    retention_days: int,
    now: datetime | None = None,
    max_days: int = _BACKFILL_MAX_DAYS_PER_RUN,
) -> int:
    """Aggregate every complete, un-aggregated day in the retention window, and
    rebuild the complete days whose rollup is recorded stale.

    Restart-resilient + self-healing: the set of days to build is derived from
    the persistent ``llm_daily_stats`` table, NOT an in-memory schedule, so a
    frequently-restarting replica (whose tick counters reset on every boot) still
    backfills, and gaps left by downtime are filled - newest day first, bounded
    to ``max_days`` per run so a cold start cannot stampede. The stale days come
    from ``llm_daily_stats_stale_days``, with the same order and bound.
    ``last_aggregation_at`` is refreshed on every successful run (even when
    nothing was due) so the "stats may be incomplete" banner never sticks on a
    healthy platform. Returns the number of daily-stats rows written.
    """
    now_utc = now or datetime.now(UTC)
    today = now_utc.date()
    retention_days = max(1, int(retention_days or 90))
    # Most recent COMPLETE day safe to aggregate. Older days are always complete.
    latest = latest_complete_day(now_utc)
    earliest = today - timedelta(days=retention_days)
    if latest < earliest:
        await _touch_last_aggregation(db, now_utc)
        return 0

    total = 0
    # Stale rollups first: a repricing recorded them with the costs it
    # filled in, and each is rebuilt in place - its rows stay readable until
    # the new ones replace them. An expired day's record is the purge's.
    stale = await _stale_days(db, first=earliest, last=latest, limit=max_days, newest_first=True)
    for target in stale:
        total += await rebuild_daily_stats(db, target, now=now_utc)

    existing_result = await db.execute(
        text(
            "SELECT DISTINCT date FROM llm_daily_stats "
            "WHERE date BETWEEN :earliest AND :latest"
        ),
        {"earliest": earliest, "latest": latest},
    )
    existing = {row[0] for row in existing_result.all()}

    # Only days that HAVE traces are due. A day with no trace never
    # gets a rollup row, so a plain calendar walk re-collected every idle day
    # on every run: on a platform with idle days the per-run budget went to
    # days with nothing to build, and an older day whose aggregation had
    # failed was only built by the retention purge's rescue.
    missing: list[date] = []
    day = latest
    while day >= earliest and len(missing) < max_days:
        if day not in existing and await _day_has_traces(db, day):
            missing.append(day)
        day -= timedelta(days=1)

    for target in missing:
        # Each call acquires the advisory lock + commits, so concurrent replicas
        # and per-day failures are isolated; the in-function existence check makes
        # it idempotent against a racing backfill.
        total += await aggregate_daily_stats(db, target_date=target)

    # The oldest retained day may already be partly purged: only days whose
    # traces are all still there are re-read.
    refreshed = await refresh_rollup_feedback(
        db, earliest=feedback_refresh_earliest(retention_days, now_utc), latest=latest
    )
    if refreshed:
        logger.info("Refreshed the feedback of %d daily stats row(s)", refreshed)

    if not missing and not stale:
        # Caught up: just record that we checked so the staleness banner clears.
        await _touch_last_aggregation(db, now_utc)
    return total
