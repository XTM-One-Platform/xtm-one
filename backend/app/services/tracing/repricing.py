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

"""Best-effort repricing of traces/spans whose cost was never resolved.

Only rows with ``total_cost IS NULL`` are touched — a NULL cost means the
model had no pricing entry at flush time, so filling it in completes a gap;
existing figures are never rewritten. Costs are computed with the CURRENT
pricing table: neither our seed nor LiteLLM's file carries a dated tariff
history, so when a tariff changed since the trace ran the result can differ
from the tariff then in force. ``enforce_effective_from=True`` skips rows
that started before the matched price's ``effective_from``, for callers who
prefer a missing cost over a potentially anachronistic one.

Postgres only: rows mirrored to ClickHouse (``TRACING_STORE_PROVIDER=
clickhouse``) are not updated here.

A repriced past day's ``llm_daily_stats`` rollup is stale. It is recorded
stale in the transaction that stores the costs, and rebuilt in place
(``trace_storage.rebuild_daily_stats``): its rows stay readable until the new
ones replace them, so a rollup-served read never finds the day empty.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.core.utils import format_exception_message
from app.models.llm_trace import LLMSpan, LLMTrace
from app.services.tracing.cost_calculator import (
    ModelPrice,
    calculate_costs,
    resolve_model_price,
    resolve_model_price_any,
)

logger = logging.getLogger(__name__)

_BATCH_SIZE = 500

# Days rebuilt INLINE, newest first, before the request returns: the bound
# the refresh always had. An older affected day keeps its stale rollup,
# readable, until the periodic ``run_daily_stats_backfill`` rebuilds it from
# the record the cost update left - it is no longer dropped from the
# refresh for good.
_MAX_INLINE_REBUILD_DAYS = 92

# The columns pricing reads. A batch is 500 rows, and the payload columns can
# each hold up to a MiB inline.
_TRACE_PRICING_COLUMNS = (
    LLMTrace.id,
    LLMTrace.model,
    LLMTrace.metadata_json,
    LLMTrace.input_tokens,
    LLMTrace.output_tokens,
    LLMTrace.started_at,
)
_SPAN_PRICING_COLUMNS = (
    LLMSpan.id,
    LLMSpan.model,
    LLMSpan.usage_details,
    LLMSpan.input_tokens,
    LLMSpan.output_tokens,
    LLMSpan.started_at,
)


@dataclass(slots=True)
class RepriceNullCostsResult:
    traces_updated: int = 0
    traces_skipped_no_price: int = 0
    traces_skipped_no_usage: int = 0
    traces_skipped_effective_from: int = 0
    spans_updated: int = 0
    spans_skipped_no_price: int = 0
    spans_skipped_no_usage: int = 0
    spans_skipped_effective_from: int = 0
    days_reaggregated: int = 0
    affected_dates: set[date] = field(default_factory=set)

    def absorb(self, batch: RepriceNullCostsResult) -> None:
        """Add a batch once it is committed (on a dry run, once it is counted)."""
        for counter in dataclasses.fields(self):
            if counter.name == "affected_dates":
                self.affected_dates |= batch.affected_dates
            else:
                setattr(self, counter.name, getattr(self, counter.name) + getattr(batch, counter.name))


class RepriceInterruptedError(RuntimeError):
    """A reprice stopped by a failure after some of its batches were committed.

    Those batches stay applied - their costs are stored, their days' rollups
    recorded stale for the backfill - and ``result`` counts them and nothing
    else, for the caller to audit. The session is left in the failed
    transaction for the caller to roll back.
    """

    def __init__(self, result: RepriceNullCostsResult, cause: Exception) -> None:
        super().__init__(
            f"repricing stopped after {result.traces_updated} trace(s) and "
            f"{result.spans_updated} span(s) were committed: {format_exception_message(cause)}"
        )
        self.result = result


@dataclass(frozen=True, slots=True)
class _RollupWindow:
    """The rollup days a reprice makes stale, read once when it starts.

    Every past day: today has no rollup yet. A day before ``expired_before``
    is one the retention purge may be deleting; its rebuild never replaces
    the stale rows with fewer traces than they count, and the purge keeps
    the traces of a recorded day until it is rebuilt.
    """

    expired_before: date
    today: date

    def affects(self, day: date) -> bool:
        return day < self.today


async def _rollup_window(db: AsyncSession, now: datetime) -> _RollupWindow:
    from app.api.v1.platform_settings_helpers import peek_platform_settings
    from app.services.trace_storage import purge_cutoff

    # Read like the maintenance tick that purges and backfills with it.
    ps = await peek_platform_settings(db)
    retention_days = int(getattr(ps, "trace_retention_days", None) or 90)
    return _RollupWindow(
        expired_before=purge_cutoff(retention_days, now).date(),
        today=now.date(),
    )


def _metadata_provider(metadata: dict | None) -> str | None:
    # Mirrors local_sink._trace_provider, but over the persisted metadata JSON.
    meta = metadata if isinstance(metadata, dict) else {}
    for key in ("provider", "llm_provider", "model_provider"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


class _PriceMemo:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._cache: dict[tuple[str | None, str], ModelPrice | None] = {}

    async def resolve(self, model: str, provider: str | None) -> ModelPrice | None:
        key = (provider, model)
        if key in self._cache:
            return self._cache[key]
        price: ModelPrice | None = None
        if provider:
            price = await resolve_model_price(self._db, model_id=model, provider=provider)
        if price is None:
            price = await resolve_model_price_any(self._db, model_id=model)
        self._cache[key] = price
        return price


def _usage_or_none(usage_details: dict | None, input_tokens: int | None, output_tokens: int | None) -> dict | None:
    """Reconstruct a usage payload; None when there is nothing to bill.

    Spans persist the original ``usage_details`` (exact recompute, cache
    buckets included); traces only keep the flattened token columns, so a
    trace repriced here bills its full input bucket at the input rate — the
    cache split was not persisted.
    """
    if usage_details:
        return usage_details
    if not input_tokens and not output_tokens:
        return None
    return {
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
    }


def _started_before_price(started_at: datetime, price: ModelPrice) -> bool:
    started = started_at if started_at.tzinfo else started_at.replace(tzinfo=UTC)
    return started.date() < price.effective_from


async def _reprice_traces(
    db: AsyncSession,
    memo: _PriceMemo,
    result: RepriceNullCostsResult,
    *,
    model: str | None,
    enforce_effective_from: bool,
    dry_run: bool,
    window: _RollupWindow,
) -> None:
    last_id: uuid.UUID | None = None
    while True:
        stmt = (
            select(LLMTrace)
            .options(load_only(*_TRACE_PRICING_COLUMNS))
            .where(LLMTrace.total_cost.is_(None), LLMTrace.model.is_not(None))
            .order_by(LLMTrace.id)
            .limit(_BATCH_SIZE)
        )
        if model:
            stmt = stmt.where(LLMTrace.model == model)
        if last_id is not None:
            stmt = stmt.where(LLMTrace.id > last_id)

        rows = list((await db.execute(stmt)).scalars().all())
        if not rows:
            return
        last_id = rows[-1].id
        batch = RepriceNullCostsResult()

        # A price-cache miss is a read, and its autoflush would write the costs
        # already set - locking their rows - before the rollup lock below: two
        # concurrent reprices would then each hold what the other waits for.
        with db.no_autoflush:
            for trace in rows:
                usage = _usage_or_none(None, trace.input_tokens, trace.output_tokens)
                if usage is None:
                    batch.traces_skipped_no_usage += 1
                    continue
                provider = _metadata_provider(trace.metadata_json)
                price = await memo.resolve(trace.model or "", provider)
                if price is None:
                    batch.traces_skipped_no_price += 1
                    continue
                if enforce_effective_from and _started_before_price(trace.started_at, price):
                    batch.traces_skipped_effective_from += 1
                    continue

                costs = calculate_costs(usage, price)
                batch.traces_updated += 1
                batch.affected_dates.add(trace.started_at.date())
                if dry_run:
                    continue
                trace.input_cost = costs["input_cost"]
                trace.output_cost = costs["output_cost"]
                trace.total_cost = costs["total_cost"]

        if not dry_run:
            # One transaction per batch: the costs AND the record of the
            # rollups they make stale commit together, so a crash between
            # batches leaves every committed batch consistent, the rest still
            # NULL for the next run (the ``total_cost IS NULL`` keyset resumes
            # for free), and no stale rollup that nothing will rebuild.
            await _mark_stale_rollups(db, batch.affected_dates, window)
            await db.commit()
        result.absorb(batch)


async def _reprice_spans(
    db: AsyncSession,
    memo: _PriceMemo,
    result: RepriceNullCostsResult,
    *,
    model: str | None,
    enforce_effective_from: bool,
    dry_run: bool,
) -> None:
    last_id: uuid.UUID | None = None
    while True:
        stmt = (
            select(LLMSpan, LLMTrace.model, LLMTrace.metadata_json)
            .options(load_only(*_SPAN_PRICING_COLUMNS))
            .join(LLMTrace, LLMSpan.trace_id == LLMTrace.id)
            .where(
                LLMSpan.total_cost.is_(None),
                or_(LLMSpan.model.is_not(None), LLMTrace.model.is_not(None)),
            )
            .order_by(LLMSpan.id)
            .limit(_BATCH_SIZE)
        )
        if model:
            stmt = stmt.where(
                or_(
                    LLMSpan.model == model,
                    (LLMSpan.model.is_(None)) & (LLMTrace.model == model),
                )
            )
        if last_id is not None:
            stmt = stmt.where(LLMSpan.id > last_id)

        rows = (await db.execute(stmt)).all()
        if not rows:
            return
        last_id = rows[-1][0].id
        batch = RepriceNullCostsResult()

        for span, trace_model, trace_metadata in rows:
            effective_model = span.model or trace_model
            if not effective_model:
                batch.spans_skipped_no_price += 1
                continue
            usage = _usage_or_none(span.usage_details, span.input_tokens, span.output_tokens)
            if usage is None:
                batch.spans_skipped_no_usage += 1
                continue
            price = await memo.resolve(effective_model, _metadata_provider(trace_metadata))
            if price is None:
                batch.spans_skipped_no_price += 1
                continue
            if enforce_effective_from and _started_before_price(span.started_at, price):
                batch.spans_skipped_effective_from += 1
                continue

            costs = calculate_costs(usage, price)
            batch.spans_updated += 1
            if dry_run:
                continue
            span.input_cost = costs["input_cost"]
            span.output_cost = costs["output_cost"]
            span.total_cost = costs["total_cost"]

        if not dry_run:
            # Span costs feed no rollup; committed per batch only so a long
            # backlog is never one unbounded transaction.
            await db.commit()
        result.absorb(batch)


async def _mark_stale_rollups(db: AsyncSession, dates: set[date], window: _RollupWindow) -> None:
    """Record the rollups of the past *dates* stale, in the caller's transaction.

    Nothing is deleted: the stale rows stay readable until the rebuild
    replaces them. The record is what the rebuild converges on, whatever
    happens to the inline refresh - the backfill rebuilds a recorded day in
    the retention window, the purge's rescue an expired one before its traces
    go. Its rollup lock is the batch's first statement (no autoflush before),
    so the batch writes - and locks - its traces only under it.
    """
    from app.services.trace_storage import mark_daily_stats_stale

    await mark_daily_stats_stale(db, (d for d in dates if window.affects(d)))


async def _refresh_daily_stats(affected_dates: set[date], window: _RollupWindow) -> int:
    """Rebuild the repriced days' rollups now, newest first, and count the days
    actually rebuilt.

    Each day in one transaction, so its old rows stay readable until the new
    ones replace them. A stale day rebuilt meanwhile by the backfill is left
    alone; a repriced day that had no rollup yet is built once complete, like
    the backfill would. A day not rebuilt here - past the inline bound, or a
    failed rebuild - keeps its stale rows and its record, and the periodic
    backfill rebuilds it. Nothing is counted that did not happen.

    On a session of its own: a rebuild rolls back when the day is already up
    to date, and a rollback expires every object of its session - on the
    request's, the admin its audit is written for. Never raises: every batch
    is committed by now and every stale day recorded, so a failure here costs
    latency, not data.
    """
    from app.core.database import async_session
    from app.services.trace_storage import rebuild_daily_stats

    days = sorted((d for d in affected_dates if window.affects(d)), reverse=True)
    inline = days[:_MAX_INLINE_REBUILD_DAYS]
    rebuilt = 0
    if inline:
        try:
            async with async_session() as own_db:
                for day in inline:
                    written = await rebuild_daily_stats(
                        own_db, day, expired_before=window.expired_before, build_missing=True
                    )
                    if written > 0:
                        rebuilt += 1
        except Exception as exc:
            logger.warning(
                "Repricing could not rebuild the affected days inline: %s",
                format_exception_message(exc),
                exc_info=True,
            )
    if len(days) > len(inline):
        logger.info(
            "Repricing refreshed %d of %d affected day(s) inline; the daily-stats "
            "backfill rebuilds the %d older one(s) recorded stale",
            rebuilt,
            len(days),
            len(days) - len(inline),
        )
    return rebuilt


async def reprice_null_costs(
    db: AsyncSession,
    *,
    model: str | None = None,
    enforce_effective_from: bool = False,
    dry_run: bool = False,
) -> RepriceNullCostsResult:
    """Fill in costs for traces/spans whose ``total_cost`` is NULL.

    Never overwrites a non-NULL cost. Returns per-category counters; on
    ``dry_run`` nothing is written and daily stats are left untouched.

    Every batch commits on its own, so a failure part-way through leaves the
    earlier batches applied and raises :class:`RepriceInterruptedError`, which
    counts them.
    """
    result = RepriceNullCostsResult()
    memo = _PriceMemo(db)
    window = await _rollup_window(db, datetime.now(UTC))

    try:
        await _reprice_traces(
            db,
            memo,
            result,
            model=model,
            enforce_effective_from=enforce_effective_from,
            dry_run=dry_run,
            window=window,
        )
        await _reprice_spans(
            db, memo, result, model=model, enforce_effective_from=enforce_effective_from, dry_run=dry_run
        )
        if dry_run:
            # Keyset pagination on `total_cost IS NULL` assumes updated rows leave
            # the result set; on a dry run they don't, but nothing was flushed so a
            # rollback keeps the session clean.
            await db.rollback()
            return result
        # Every batch committed itself; this only closes the read that found the
        # key set empty.
        await db.commit()
    except Exception as exc:
        if dry_run:
            raise
        raise RepriceInterruptedError(result, exc) from exc

    if result.traces_updated:
        result.days_reaggregated = await _refresh_daily_stats(result.affected_dates, window)

    logger.info(
        "Repriced NULL-cost tracing rows: %d trace(s), %d span(s); "
        "skipped (no price/no usage/effective_from) traces=%d/%d/%d spans=%d/%d/%d; "
        "%d day(s) re-aggregated",
        result.traces_updated,
        result.spans_updated,
        result.traces_skipped_no_price,
        result.traces_skipped_no_usage,
        result.traces_skipped_effective_from,
        result.spans_skipped_no_price,
        result.spans_skipped_no_usage,
        result.spans_skipped_effective_from,
        result.days_reaggregated,
    )
    return result
