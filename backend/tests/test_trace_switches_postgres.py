"""The trace switches against a real PostgreSQL.

What ``test_trace_switches.py`` checks on fake sessions, on the real tables:
the PostgreSQL sink writes a trace whole by default, without its payload
columns while payloads are off and not at all while storage is off, and the
switches are read off a real ``platform_settings`` row.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models.llm_trace import LLMScore, LLMSpan, LLMTrace
from app.models.platform_settings import PlatformSettings
from app.services.trace_switches import DEFAULT_TRACE_SWITCHES, publish_trace_switches, read_trace_switches
from app.services.tracing.local_sink import LocalPGSink
from tests.test_provisioning_postgres import _temp_database
from tests.test_trace_switches import PAYLOADS_OFF, STORAGE_OFF, _tool_span, _trace

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_the_postgres_sink_honours_the_switches_on_the_real_tables():
    async with _temp_database("trace_switches") as sessionmaker:
        sink = LocalPGSink(session_factory=sessionmaker)
        whole, stripped, dropped = _trace(), _trace(), _trace()
        await sink.flush_trace(whole, [_tool_span(whole.id)])
        publish_trace_switches(PAYLOADS_OFF)
        await sink.flush_trace(stripped, [_tool_span(stripped.id)])
        publish_trace_switches(STORAGE_OFF)
        await sink.flush_trace(dropped, [_tool_span(dropped.id)])
        await sink.flush_score({"trace_id": str(whole.id), "name": "accuracy", "value": 1.0})

        async with sessionmaker() as db:
            traces = {row.id: row for row in (await db.execute(select(LLMTrace))).scalars()}
            spans = {row.trace_id: row for row in (await db.execute(select(LLMSpan))).scalars()}
            scores = (await db.execute(select(func.count()).select_from(LLMScore))).scalar_one()

        assert set(traces) == {whole.id, stripped.id}
        assert traces[whole.id].input == whole.input and traces[whole.id].output == whole.output
        assert spans[whole.id].tool_arguments == {"email": "jane@example.com"}

        kept, kept_span = traces[stripped.id], spans[stripped.id]
        assert (kept.input, kept.output) == (None, None)
        assert (kept_span.input, kept_span.output, kept_span.tool_arguments) == (None, None, None)
        assert (kept.model, kept.status, kept.total_tokens, kept.failure_cause) == (
            "claude-sonnet-4.6",
            "error",
            150,
            "model_rejected_request",
        )
        assert (kept_span.name, kept_span.tool_call_id, kept_span.input_tokens) == (
            "tool:lookup_customer",
            "call_1",
            12,
        )
        assert len(kept.error_message) < len(stripped.error_message)
        assert scores == 0


@pytest.mark.asyncio
async def test_the_switches_are_read_off_a_real_platform_settings_row():
    async with _temp_database("trace_switches_row") as sessionmaker:
        async with sessionmaker() as db:
            assert await read_trace_switches(db) == DEFAULT_TRACE_SWITCHES
            db.add(PlatformSettings(trace_storage_enabled=True, trace_store_payloads=False))
            await db.commit()
        async with sessionmaker() as db:
            assert await read_trace_switches(db) == PAYLOADS_OFF
