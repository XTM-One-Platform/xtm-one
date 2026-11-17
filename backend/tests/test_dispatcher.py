from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.services.tracing.base import SpanData, TraceData
from app.services.tracing.dispatcher import (
    OVERFLOW_KEY,
    TracingDispatcher,
    _deserialize_trace,
    _serialize_trace,
)


def _trace(name: str) -> TraceData:
    now = datetime.now(UTC)
    return TraceData(
        id=uuid4(),
        source="assignment",
        name=name,
        started_at=now,
        ended_at=now,
        status="ok",
    )


def _span(trace_id, name: str, position: int) -> SpanData:
    now = datetime.now(UTC)
    return SpanData(
        id=uuid4(),
        trace_id=trace_id,
        type="llm",
        name=name,
        position=position,
        started_at=now,
        ended_at=now,
    )


class FakeSink:
    def __init__(self) -> None:
        self.flushed: list[str] = []
        self.closed = False

    async def flush_trace(self, trace, spans) -> None:
        self.flushed.append(trace.name)

    async def flush_score(self, score) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class _ScoreSink(FakeSink):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail
        self.scores: list[dict] = []

    async def flush_score(self, score) -> None:
        if self.fail:
            raise RuntimeError("score sink failed")
        self.scores.append(score)


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self._payload = None
        self._trim = None
        self._ttl = None

    def rpush(self, key, payload):
        assert key == OVERFLOW_KEY
        self._payload = payload
        return self

    def ltrim(self, key, start, end):
        assert key == OVERFLOW_KEY
        self._trim = (start, end)
        return self

    def expire(self, key, ttl):
        assert key == OVERFLOW_KEY
        self._ttl = ttl
        return self

    async def execute(self):
        if self._payload is not None:
            self.redis.values.append(self._payload)
        if self._trim is not None:
            start, end = self._trim
            self.redis.values = self.redis.values[start : end + 1 if end != -1 else None]
        return True


class FakeRedis:
    def __init__(self):
        self.values: list[str] = []

    def pipeline(self):
        return FakePipeline(self)

    async def lpop(self, _key):
        if not self.values:
            return None
        return self.values.pop(0)

    async def llen(self, _key):
        return len(self.values)


@pytest.mark.asyncio
async def test_dispatcher_flushes_queue_to_sink():
    sink = FakeSink()
    redis = FakeRedis()

    async def _redis_provider():
        return redis

    dispatcher = TracingDispatcher(
        sinks=[sink],
        queue_maxsize=5,
        redis_provider=_redis_provider,
    )
    await dispatcher.start()
    await dispatcher.record(_trace("trace-1"), [])
    await dispatcher.drain(timeout_seconds=1.0)
    await dispatcher.close()

    assert sink.flushed == ["trace-1"]
    assert sink.closed is True


def test_serialize_trace_roundtrips_spans_without_duplication():
    # The producer leaves trace.spans populated; serialization must still write
    # the spans exactly once (the sibling "spans" list), never nested under
    # "trace" - that double-conversion is what this path was fixed to avoid.
    trace = _trace("with-spans")
    spans = [_span(trace.id, "span-a", 0), _span(trace.id, "span-b", 1)]
    trace.spans = spans

    raw = _serialize_trace(trace, spans)
    payload = json.loads(raw)
    assert not payload["trace"].get("spans")  # no spans nested inside the trace
    assert len(payload["spans"]) == 2

    restored_trace, restored_spans = _deserialize_trace(raw)
    assert restored_trace.name == "with-spans"
    assert [s.name for s in restored_spans] == ["span-a", "span-b"]
    # trace.spans is rehydrated from the parsed spans so the object stays consistent.
    assert [s.name for s in restored_trace.spans] == ["span-a", "span-b"]


@pytest.mark.asyncio
async def test_dispatcher_overflow_roundtrip():
    sink = FakeSink()
    redis = FakeRedis()

    async def _redis_provider():
        return redis

    dispatcher = TracingDispatcher(
        sinks=[sink],
        queue_maxsize=1,
        overflow_max_entries=10,
        redis_provider=_redis_provider,
    )

    dispatcher._running = True
    dispatcher._queue.put_nowait((_trace("already-full"), []))
    await dispatcher.record(_trace("overflowed"), [])
    assert len(redis.values) == 1

    dispatcher._queue.get_nowait()
    dispatcher._queue.task_done()
    drained = await dispatcher.drain_overflow_to_queue()
    assert drained == 1
    trace, _spans = dispatcher._queue.get_nowait()
    assert trace.name == "overflowed"


@pytest.mark.asyncio
async def test_dispatcher_worker_drains_runtime_overflow():
    sink = FakeSink()
    redis = FakeRedis()

    async def _redis_provider():
        return redis

    dispatcher = TracingDispatcher(
        sinks=[sink],
        queue_maxsize=1,
        overflow_max_entries=10,
        redis_provider=_redis_provider,
    )

    # Pre-fill queue without worker, forcing next record() into Redis overflow.
    dispatcher._running = True
    dispatcher._queue.put_nowait((_trace("q-1"), []))
    await dispatcher.record(_trace("overflow-1"), [])
    assert len(redis.values) == 1

    # Start worker and ensure it drains both in-memory and overflow payloads.
    dispatcher._running = False
    await dispatcher.start()
    await dispatcher.drain(timeout_seconds=1.0)
    await dispatcher.close()

    assert sink.flushed == ["q-1", "overflow-1"]
    assert len(redis.values) == 0


@pytest.mark.asyncio
async def test_dispatcher_record_score_dispatches_independently():
    sink_ok_1 = _ScoreSink()
    sink_fail = _ScoreSink(fail=True)
    sink_ok_2 = _ScoreSink()
    dispatcher = TracingDispatcher(
        sinks=[sink_ok_1, sink_fail, sink_ok_2],
        queue_maxsize=1,
    )

    score = {
        "trace_id": str(uuid4()),
        "name": "user_feedback",
        "source": "USER",
        "data_type": "BOOLEAN",
        "value": 1.0,
    }
    await dispatcher.record_score(score)
    await asyncio.sleep(0)

    assert sink_ok_1.scores == [score]
    assert sink_ok_2.scores == [score]
    assert dispatcher.tracing_errors == 1
