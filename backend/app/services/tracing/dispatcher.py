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

"""Tracing dispatcher with bounded queue + Redis overflow."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.config import settings
from app.core.redis import redis_client
from app.core.utils import format_exception_message
from app.services.trace_switches import apply_trace_switches, discard_switched_off_objects
from app.services.tracing.base import SpanData, TraceData, TracingBackend
from app.services.tracing.langfuse_sink import LangfuseSink
from app.services.tracing.media_offload import offload_media
from app.services.tracing.payload_offload import offload_if_oversized
from app.services.tracing.store import build_trace_write_sink

logger = logging.getLogger(__name__)

OVERFLOW_KEY = "tracing:overflow"
_BACKGROUND_TRACE_TASKS: set[asyncio.Task] = set()
_STOP_SENTINEL: tuple[None, None] = (None, None)

_DISPATCHER: TracingDispatcher | None = None


def _track_background_task(task: asyncio.Task) -> None:
    _BACKGROUND_TRACE_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TRACE_TASKS.discard)


async def _offload_oversized_payloads(trace: TraceData, spans: list[SpanData]) -> None:
    """Move media blobs and oversized payloads out of trace/span rows.

    Mutates ``trace`` and ``spans`` in place: they are freshly built per trace
    by the facade and are not shared with the caller's own state.  A failure to
    offload never raises — :func:`offload_if_oversized` degrades to inline
    storage (or to a recorded-failure envelope past the hard max), because
    losing a trace payload is worse than a large row.

    Media first, then the size check.  A payload's media blobs are usually what
    makes it oversized, so replacing them with references often keeps the
    payload inline entirely; and when it is still oversized afterwards, the
    envelope written to object storage already carries the references, so each
    image stays individually addressable instead of being buried inside one
    opaque JSON object.
    """
    for holder in (trace, *spans):
        span_id = getattr(holder, "id", None) if holder is not trace else None
        for field in ("input", "output", "metadata"):
            current = getattr(holder, field, None)
            if current is None:
                continue
            try:
                replacement = await offload_media(
                    current,
                    trace_id=trace.id,
                    span_id=span_id,
                    field=field,
                )
                replacement = await offload_if_oversized(
                    replacement,
                    trace_id=trace.id,
                    span_id=span_id,
                    field=field,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "Trace payload offload raised for %s: %s",
                    field,
                    format_exception_message(exc),
                )
                continue
            if replacement is not current:
                setattr(holder, field, replacement)


def _serialize_trace(trace: TraceData, spans: list[SpanData]) -> str:
    # The spans are serialized once, as the sibling "spans" list (the canonical
    # copy that _deserialize_trace reads back). Convert the trace from a
    # spans-emptied copy so asdict() doesn't deep-convert every span just to
    # discard it - that wasted CPU/memory was the whole point of this fix - while
    # keeping asdict's exact conversion of every other (metadata/input/output)
    # field.
    trace_dict = asdict(replace(trace, spans=[]))
    trace_dict.pop("spans", None)
    payload = {
        "trace": trace_dict,
        "spans": [asdict(span) for span in spans],
    }
    return json.dumps(payload, default=str, separators=(",", ":"))


def _deserialize_trace(raw: str) -> tuple[TraceData, list[SpanData]]:
    def _as_uuid(value: Any) -> UUID | None:
        if value is None:
            return None
        if isinstance(value, UUID):
            return value
        return UUID(str(value))

    def _as_datetime(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value))

    payload = json.loads(raw)
    trace_raw = dict(payload["trace"])
    trace_raw["id"] = _as_uuid(trace_raw.get("id"))
    trace_raw["user_id"] = _as_uuid(trace_raw.get("user_id"))
    trace_raw["agent_id"] = _as_uuid(trace_raw.get("agent_id"))
    trace_raw["conversation_id"] = _as_uuid(trace_raw.get("conversation_id"))
    trace_raw["assignment_run_id"] = _as_uuid(trace_raw.get("assignment_run_id"))
    trace_raw["parent_trace_id"] = _as_uuid(trace_raw.get("parent_trace_id"))
    trace_raw["started_at"] = _as_datetime(trace_raw.get("started_at"))
    trace_raw["ended_at"] = _as_datetime(trace_raw.get("ended_at"))
    trace = TraceData(**trace_raw)

    spans: list[SpanData] = []
    for item in payload["spans"]:
        span_raw = dict(item)
        span_raw["id"] = _as_uuid(span_raw.get("id"))
        span_raw["trace_id"] = _as_uuid(span_raw.get("trace_id"))
        span_raw["parent_span_id"] = _as_uuid(span_raw.get("parent_span_id"))
        span_raw["started_at"] = _as_datetime(span_raw.get("started_at"))
        span_raw["ended_at"] = _as_datetime(span_raw.get("ended_at"))
        spans.append(SpanData(**span_raw))
    # "spans" was stripped from the trace payload (serialized separately), so
    # rehydrate the field from the parsed spans to keep the object consistent.
    trace.spans = spans
    return trace, spans


class TracingDispatcher:
    def __init__(
        self,
        *,
        sinks: list[TracingBackend] | None = None,
        queue_maxsize: int | None = None,
        overflow_ttl_seconds: int | None = None,
        overflow_max_entries: int | None = None,
        redis_provider: Callable[[], Any] | None = None,
    ) -> None:
        self._sinks = sinks or [build_trace_write_sink(), LangfuseSink()]
        self._queue = asyncio.Queue[
            tuple[TraceData, list[SpanData]] | tuple[None, None]
        ](
            maxsize=queue_maxsize or settings.tracing_queue_maxsize
        )
        self._overflow_ttl_seconds = (
            overflow_ttl_seconds or settings.tracing_overflow_ttl_seconds
        )
        self._overflow_max_entries = (
            overflow_max_entries or settings.tracing_overflow_max_entries
        )
        self._redis_provider = redis_provider or redis_client
        self._worker_task: asyncio.Task | None = None
        self.tracing_errors = 0
        self._running = False
        self._active_flushes = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        # A context of its own: started by the first trace of a worker process,
        # it would otherwise keep that run's known secrets for its whole life.
        self._worker_task = asyncio.create_task(self._worker_loop(), context=contextvars.Context())
        _track_background_task(self._worker_task)

    async def close(self, graceful_timeout_seconds: float = 5.0) -> None:
        self._running = False
        if self._worker_task is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(_STOP_SENTINEL)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._worker_task, timeout=graceful_timeout_seconds)
            self._worker_task = None
        for sink in self._sinks:
            await sink.close()

    async def record(self, trace: TraceData, spans: list[SpanData]) -> None:
        # The trace switches before anything else: a trace the store
        # may not keep, or a payload it may not keep, never reaches object
        # storage, the queue or the Redis overflow.
        switched = apply_trace_switches(trace, spans)
        if switched is None:
            return
        trace, spans = switched
        if not self._running:
            await self.start()
        # Offload media blobs and oversized payloads before the trace reaches
        # the queue, so the queue, the Redis overflow buffer and every sink
        # carry small references instead of multi-megabyte blobs.  Payloads
        # below the threshold are left untouched and stored verbatim.
        await _offload_oversized_payloads(trace, spans)
        if self._queue.full():
            await self._push_overflow(trace, spans)
            return
        self._queue.put_nowait((trace, spans))

    async def record_score(self, score: dict[str, Any]) -> None:
        for sink in self._sinks:
            task = asyncio.create_task(self._flush_score_to_sink(sink, score))
            _track_background_task(task)

    async def _flush_score_to_sink(self, sink: TracingBackend, score: dict[str, Any]) -> None:
        try:
            await sink.flush_score(score)
        except Exception as exc:
            self.tracing_errors += 1
            logger.warning(
                "Tracing score sink failed (%s): %s",
                sink.__class__.__name__,
                format_exception_message(exc),
                exc_info=True,
            )

    async def _push_overflow(self, trace: TraceData, spans: list[SpanData]) -> None:
        try:
            redis = await self._redis_provider()
            payload = _serialize_trace(trace, spans)
            pipe = redis.pipeline()
            pipe.rpush(OVERFLOW_KEY, payload)
            pipe.ltrim(OVERFLOW_KEY, -self._overflow_max_entries, -1)
            pipe.expire(OVERFLOW_KEY, self._overflow_ttl_seconds)
            await pipe.execute()
        except Exception as exc:
            self.tracing_errors += 1
            logger.warning("Tracing overflow push failed: %s", format_exception_message(exc), exc_info=True)
            # Last-resort fallback: attempt direct sink flush so traces are not
            # silently dropped when both queue and Redis overflow are unavailable.
            await self._flush_to_sinks(trace, spans)

    async def drain_overflow_to_queue(self, max_items: int = 500) -> int:
        drained = 0
        redis = await self._redis_provider()
        while drained < max_items and not self._queue.full():
            raw = await redis.lpop(OVERFLOW_KEY)
            if raw is None:
                break
            try:
                trace, spans = _deserialize_trace(raw)
            except Exception as exc:
                self.tracing_errors += 1
                logger.warning("Invalid tracing overflow payload: %s", format_exception_message(exc), exc_info=True)
                continue
            try:
                self._queue.put_nowait((trace, spans))
            except asyncio.QueueFull:
                # The `await redis.lpop` above yields the event loop, so a
                # concurrent drainer (the worker tick at the end of each flush, the
                # startup drain, and the shutdown drain can all run together) may
                # have filled the queue between the `full()` check and here. The
                # item is already popped from Redis, so put it BACK at the front of
                # the overflow list (lpop took it from the front) and stop, instead
                # of dropping the trace on a QueueFull.
                with contextlib.suppress(Exception):
                    await redis.lpush(OVERFLOW_KEY, raw)
                break
            drained += 1
        return drained

    async def _overflow_len(self) -> int:
        redis = await self._redis_provider()
        return int(await redis.llen(OVERFLOW_KEY))

    async def _spill_queue_to_overflow(self) -> int:
        spilled = 0
        while not self._queue.empty():
            item = self._queue.get_nowait()
            self._queue.task_done()
            if item == _STOP_SENTINEL:
                continue
            trace, spans = item
            await self._push_overflow(trace, spans)
            spilled += 1
        return spilled

    async def drain(self, timeout_seconds: float = 30.0) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            await self.drain_overflow_to_queue()
            if (
                self._queue.empty()
                and self._active_flushes == 0
                and await self._overflow_len() == 0
            ):
                return True
            await asyncio.sleep(0.05)
        return False

    async def _worker_loop(self) -> None:
        while True:
            item = await self._queue.get()
            if item == _STOP_SENTINEL:
                self._queue.task_done()
                return
            trace, spans = item
            self._active_flushes += 1
            try:
                await self._flush_to_sinks(trace, spans)
            finally:
                self._active_flushes = max(0, self._active_flushes - 1)
                self._queue.task_done()
            with contextlib.suppress(Exception):
                await self.drain_overflow_to_queue(max_items=1)

    async def _flush_to_sinks(self, trace: TraceData, spans: list[SpanData]) -> None:
        # Again at write time: a trace recorded before the switches changed
        # (still queued, or read back from an overflow any replica spilled) is
        # written under the switches in force now, without the objects its
        # dropped payloads were stored in.
        switched = apply_trace_switches(trace, spans)
        await discard_switched_off_objects(trace, spans, switched)
        if switched is None:
            return
        trace, spans = switched
        for sink in self._sinks:
            try:
                await sink.flush_trace(trace, spans)
            except Exception as exc:
                self.tracing_errors += 1
                logger.warning("Tracing sink failed (%s): %s", sink.__class__.__name__, format_exception_message(exc), exc_info=True)


def get_tracing_dispatcher() -> TracingDispatcher:
    global _DISPATCHER
    if _DISPATCHER is None:
        _DISPATCHER = TracingDispatcher()
    return _DISPATCHER


async def init_tracing_dispatcher() -> TracingDispatcher:
    dispatcher = get_tracing_dispatcher()
    await dispatcher.start()
    deadline = asyncio.get_running_loop().time() + 30.0
    while True:
        # A transient Redis error during the startup drain must never crash boot;
        # the worker keeps draining one item per flush afterwards regardless.
        try:
            drained = await dispatcher.drain_overflow_to_queue()
            pending = await dispatcher._overflow_len()
        except Exception as exc:
            logger.warning("Tracing startup drain error: %s", format_exception_message(exc), exc_info=True)
            break
        if pending == 0:
            break
        if drained == 0 and asyncio.get_running_loop().time() >= deadline:
            logger.warning(
                "Tracing startup drain timed out with %s overflow items pending",
                pending,
            )
            break
        await asyncio.sleep(0.05)
    return dispatcher


async def shutdown_tracing_dispatcher(timeout_seconds: float = 30.0) -> None:
    global _DISPATCHER
    if _DISPATCHER is None:
        return
    drained = await _DISPATCHER.drain(timeout_seconds)
    if not drained:
        spilled = await _DISPATCHER._spill_queue_to_overflow()
        if spilled > 0:
            logger.warning(
                "Tracing shutdown timed out; re-queued %s in-memory traces to Redis overflow",
                spilled,
            )
    await _DISPATCHER.close()
    _DISPATCHER = None


async def wait_for_submitted_traces(timeout_seconds: float = 5.0) -> None:
    """Let the traces just handed to :func:`submit_trace_for_flush` reach the queue.

    The submit is a task of its own: a drain that starts before it ran finds
    the queue empty and returns. Bounded; never raises.
    """
    pending = [task for task in _BACKGROUND_TRACE_TASKS if not task.done()]
    if not pending:
        return
    with contextlib.suppress(Exception):
        await asyncio.wait(pending, timeout=timeout_seconds)


def submit_trace_for_flush(trace: TraceData, spans: list[SpanData]) -> None:
    async def _submit() -> None:
        dispatcher = get_tracing_dispatcher()
        await dispatcher.record(trace, spans)

    task = asyncio.create_task(_submit())
    _track_background_task(task)
