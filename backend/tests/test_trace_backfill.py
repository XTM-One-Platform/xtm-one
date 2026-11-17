from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.tracing.backfill import TraceBackfillCursor, TraceBackfillService


class _SessionCtx:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _SessionFactory:
    def __call__(self):
        return _SessionCtx()


class _Sink:
    def __init__(self) -> None:
        self.calls: list[tuple[list, dict, dict]] = []

    async def bulk_import(self, *, traces, spans_by_trace, scores_by_trace) -> None:
        self.calls.append((list(traces), dict(spans_by_trace), dict(scores_by_trace)))


@pytest.mark.asyncio
async def test_trace_backfill_batches_and_returns_cursor(monkeypatch):
    sink = _Sink()
    now = datetime.now(UTC)
    t1 = SimpleNamespace(id=uuid4(), created_at=now)
    t2 = SimpleNamespace(id=uuid4(), created_at=now + timedelta(seconds=1))
    t3 = SimpleNamespace(id=uuid4(), created_at=now + timedelta(seconds=2))

    service = TraceBackfillService(
        session_factory=_SessionFactory(),
        sink=sink,  # type: ignore[arg-type]
        batch_size=2,
    )

    async def _load_trace_batch(_session, *, cursor):
        if cursor is None:
            return [t1, t2]
        if cursor.trace_id == t2.id:
            return [t3]
        return []

    async def _load_spans(_session, trace_ids):
        return [
            SimpleNamespace(id=uuid4(), trace_id=trace_id, position=1, started_at=now)
            for trace_id in trace_ids
        ]

    async def _load_scores(_session, trace_ids):
        return [
            SimpleNamespace(id=uuid4(), trace_id=trace_id, created_at=now)
            for trace_id in trace_ids
        ]

    monkeypatch.setattr(service, "_load_trace_batch", _load_trace_batch)
    monkeypatch.setattr(service, "_load_spans", _load_spans)
    monkeypatch.setattr(service, "_load_scores", _load_scores)

    first = await service.run(max_batches=1)
    assert first.imported_traces == 2
    assert first.imported_spans == 2
    assert first.imported_scores == 2
    assert first.exhausted is False
    assert first.cursor is not None
    assert first.cursor.trace_id == t2.id

    second = await service.run(cursor=first.cursor)
    assert second.imported_traces == 1
    assert second.imported_spans == 1
    assert second.imported_scores == 1
    assert second.exhausted is True
    assert second.cursor is not None
    assert second.cursor.trace_id == t3.id
    assert len(sink.calls) == 2


def test_trace_backfill_cursor_dataclass_roundtrip():
    now = datetime.now(UTC)
    trace_id = uuid4()
    cursor = TraceBackfillCursor(created_at=now, trace_id=trace_id)
    assert cursor.created_at == now
    assert cursor.trace_id == trace_id
