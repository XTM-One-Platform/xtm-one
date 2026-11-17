"""Tests for LangfuseSink no-op compatibility behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.services.tracing.base import SpanData, TraceData
from app.services.tracing.langfuse_sink import LangfuseSink


@pytest.mark.asyncio
async def test_flush_trace_is_noop():
    sink = LangfuseSink()
    now = datetime.now(UTC)
    trace = TraceData(
        id=uuid4(),
        source="agent_chat",
        name="trace",
        started_at=now,
    )
    spans = [
        SpanData(
            id=uuid4(),
            trace_id=trace.id,
            type="generation",
            name="span",
            position=0,
            started_at=now,
        )
    ]

    assert await sink.flush_trace(trace, spans) is None


@pytest.mark.asyncio
async def test_flush_score_is_noop():
    sink = LangfuseSink()

    assert await sink.flush_score({"name": "feedback", "value": 1.0}) is None


@pytest.mark.asyncio
async def test_close_is_noop():
    sink = LangfuseSink()

    result = await sink.close()
    assert result is None
