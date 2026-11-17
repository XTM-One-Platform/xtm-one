from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.services.tracing.base import SpanData, TraceData
from app.services.tracing.clickhouse_sink import ClickHouseSink


class _FakeClient:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict]]] = []

    async def insert_json_rows(self, table: str, rows: list[dict]) -> None:
        self.inserts.append((table, rows))

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_clickhouse_sink_flush_trace_writes_trace_and_spans():
    client = _FakeClient()

    async def _client_factory():
        return client

    sink = ClickHouseSink(client_factory=_client_factory)
    now = datetime.now(UTC)
    trace = TraceData(
        id=uuid4(),
        source="assignment",
        name="trace-1",
        started_at=now,
        ended_at=now,
        input_tokens=5,
        output_tokens=7,
        total_tokens=12,
    )
    span = SpanData(
        id=uuid4(),
        trace_id=trace.id,
        type="GENERATION",
        name="span-1",
        position=1,
        started_at=now,
        ended_at=now,
    )

    await sink.flush_trace(trace, [span])

    assert [table for table, _rows in client.inserts] == ["llm_traces", "llm_spans"]
    trace_rows = client.inserts[0][1]
    span_rows = client.inserts[1][1]
    assert trace_rows[0]["id"] == str(trace.id)
    assert span_rows[0]["trace_id"] == str(trace.id)
