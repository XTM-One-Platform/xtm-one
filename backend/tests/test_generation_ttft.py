"""Time to first token on streamed generations and on the run rollup."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.services import langfuse_tracing
from app.services.langfuse_tracing import end_generation, mark_first_output
from app.services.tracing.background_run import _span_from_payload
from app.services.tracing.run_rollup import rollup_from_spans, rollup_percentiles


def _root(started_seconds_ago: float = 3.0) -> dict:
    return {
        "trace": None,
        "generation": None,
        "parent_ctx": None,
        "metadata": {"source": "chat"},
        "local_trace_id": uuid.uuid4(),
        "local_source": "chat",
        "local_name": "chat",
        "local_metadata": {"source": "chat"},
        "local_started_at": datetime.now(UTC) - timedelta(seconds=started_seconds_ago),
        "local_spans": [],
    }


def _generation(root: dict, started_seconds_ago: float = 1.0) -> dict:
    return {
        "trace": None,
        "generation": None,
        "parent_ctx": root,
        "metadata": {"observation_kind": "llm_generation"},
        "local_span_id": uuid.uuid4(),
        "local_span_type": "GENERATION",
        "local_name": "llm_call",
        "local_metadata": {"observation_kind": "llm_generation"},
        "local_started_at": datetime.now(UTC) - timedelta(seconds=started_seconds_ago),
        "local_position": 1,
    }


def test_a_streamed_generation_records_its_time_to_first_token():
    root = _root()
    gen = _generation(root, started_seconds_ago=1.5)
    mark_first_output(gen)
    end_generation(gen, output="hello", stop_reason="end_turn")
    record = root["local_spans"][0]
    assert record["time_to_first_token"] == pytest.approx(1500.0, abs=200.0)
    assert record["metadata"]["ttft_ms"] == record["time_to_first_token"]
    assert record["stop_reason"] == "end_turn"
    # The generation's own creation metadata is not mutated.
    assert "ttft_ms" not in gen["local_metadata"]


def test_only_the_first_chunk_counts():
    root = _root()
    gen = _generation(root)
    mark_first_output(gen)
    first = gen["local_first_output_at"]
    mark_first_output(gen)
    assert gen["local_first_output_at"] is first
    assert root["local_first_output_at"] is first


def test_the_trace_keeps_the_first_output_of_its_first_streamed_generation():
    root = _root()
    first = _generation(root)
    mark_first_output(first)
    second = _generation(root)
    mark_first_output(second)
    assert root["local_first_output_at"] is first["local_first_output_at"]


def test_a_generation_without_streamed_output_records_nothing():
    root = _root()
    gen = _generation(root)
    end_generation(gen, output="non-streamed")
    record = root["local_spans"][0]
    assert record["time_to_first_token"] is None
    assert "ttft_ms" not in record["metadata"]


def test_mark_first_output_ignores_a_missing_context():
    mark_first_output(None)
    mark_first_output({})  # no parent: kept on the generation only, no error


def test_the_rollup_carries_first_token_and_the_slowest_generation_ttft():
    spans = [
        {"type": "GENERATION", "duration_ms": 900.0, "time_to_first_token": 400.0},
        {"type": "GENERATION", "duration_ms": 900.0, "time_to_first_token": 1200.0},
        {"type": "GENERATION", "duration_ms": 900.0, "time_to_first_token": None},
        {"type": "GENERATION", "duration_ms": 900.0, "time_to_first_token": True},
    ]
    rollup = rollup_from_spans(spans, wall_ms=5000.0, first_token_ms=650.0)
    assert rollup["first_token_ms"] == 650.0
    assert rollup["max_ttft_ms"] == 1200.0


def test_a_run_that_never_streamed_has_no_first_token_figure():
    rollup = rollup_from_spans([{"type": "GENERATION", "duration_ms": 10.0}], wall_ms=100.0)
    assert "first_token_ms" not in rollup
    assert rollup["max_ttft_ms"] is None
    summary = rollup_percentiles([rollup])
    assert "first_token_ms" not in summary


def test_first_token_joins_the_percentiles():
    summary = rollup_percentiles([{"first_token_ms": float(v)} for v in range(1, 11)])
    assert summary["first_token_ms"] == {"p50": 5.5, "p90": 9.1, "n": 10}


def test_end_trace_writes_first_token_on_the_rollup():
    root = _root(started_seconds_ago=4.0)
    gen = _generation(root, started_seconds_ago=2.0)
    mark_first_output(gen)
    end_generation(gen, output="hi")
    with patch("app.services.tracing.dispatcher.submit_trace_for_flush") as submit:
        langfuse_tracing.end_trace(root, output="hi")
    trace_data, spans = submit.call_args.args[0], submit.call_args.args[1]
    rollup = trace_data.metadata["rollup"]
    assert rollup["first_token_ms"] == pytest.approx(4000.0, abs=300.0)
    assert rollup["max_ttft_ms"] == pytest.approx(2000.0, abs=300.0)
    assert spans[0].time_to_first_token == pytest.approx(2000.0, abs=300.0)


def test_the_background_checkpoint_keeps_the_ttft():
    payload = json.dumps(
        {
            "id": str(uuid.uuid4()),
            "type": "GENERATION",
            "name": "llm_call",
            "started_at": datetime.now(UTC).isoformat(),
            "time_to_first_token": 812.5,
        }
    )
    span = _span_from_payload(payload, uuid.uuid4())
    assert span is not None
    assert span.time_to_first_token == 812.5


def test_streaming_chat_marks_the_first_text_chunk():
    from app.tools import agent_loop

    class _Llm:
        provider_name = "fake"

        async def chat_stream(self, **_kwargs):
            await asyncio.sleep(0.05)
            yield "a"
            yield "b"

    gen: dict = {"local_started_at": datetime.now(UTC)}

    async def _run():
        return await agent_loop._streaming_chat(_Llm(), [], None, max_tokens=10, idle_timeout=None, trace_gen_ctx=gen)

    response = asyncio.run(_run())
    assert response.content == "ab"
    elapsed = (gen["local_first_output_at"] - gen["local_started_at"]).total_seconds()
    assert 0.04 <= elapsed < 1.0


def test_the_measure_costs_well_under_one_percent_of_a_turn():
    """The program's gate: each chunk pays one dict lookup after the first.

    Checked by the work done, not by a stopwatch: a wall-clock bound fails on a
    loaded CI worker whatever the code does. After the first chunk of a
    4,000-chunk answer, no call reads the clock or walks up to the root.
    """
    root = _root()
    gen = _generation(root)
    mark_first_output(gen)
    first = gen["local_first_output_at"]
    with (
        patch.object(langfuse_tracing, "datetime") as clock,
        patch.object(langfuse_tracing, "_root_local_context") as root_walk,
    ):
        for _ in range(4000):
            mark_first_output(gen)
    assert clock.now.call_count == 0
    assert root_walk.call_count == 0
    assert gen["local_first_output_at"] is first
    assert root["local_first_output_at"] is first


def test_the_usage_sink_fires_its_first_output_callback_once():
    from app.llm.base import StreamUsageSink

    calls: list[int] = []
    sink = StreamUsageSink()
    sink.first_output()  # nothing bound: no error
    sink.bind_first_output(lambda: calls.append(1))
    sink.first_output()
    sink.first_output()
    assert calls == [1]


def test_a_failing_first_output_callback_never_breaks_the_call():
    from app.llm.base import StreamUsageSink

    def _boom() -> None:
        raise RuntimeError("measure failed")

    sink = StreamUsageSink()
    sink.bind_first_output(_boom)
    sink.first_output()


def test_a_tool_only_streamed_generation_records_its_time_to_first_token(monkeypatch):
    """A generation that answers with a tool call alone streams no text and,
    on a model whose thinking is omitted, no thinking delta: its TTFT is the
    first content block, read from the provider stream."""
    from types import SimpleNamespace

    from app.llm.anthropic_provider import AnthropicProvider
    from app.llm.base import ChatMessage, StreamUsageSink

    events = [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(usage=SimpleNamespace(input_tokens=10, output_tokens=1)),
        ),
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="tool_use", id="t1", name="lookup"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="input_json_delta", partial_json='{"q": 1}'),
        ),
        SimpleNamespace(type="content_block_stop"),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="tool_use"),
            usage=SimpleNamespace(output_tokens=5),
        ),
    ]

    yielded: list[str] = []

    class _Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def __aiter__(self):
            return self._events()

        async def _events(self):
            for event in events:
                yielded.append(event.type)
                yield event

    async def _open(_kwargs):
        return _Stream()

    provider = AnthropicProvider(api_key="test-key")
    monkeypatch.setattr(provider, "_open_event_stream", _open)
    root = _root()
    gen = _generation(root, started_seconds_ago=0.5)
    sink = StreamUsageSink()
    marked_after: list[str] = []

    def _first_output() -> None:
        marked_after.append(yielded[-1])
        mark_first_output(gen)

    sink.bind_first_output(_first_output)

    response = asyncio.run(
        provider.chat_stream_with_tools(
            messages=[ChatMessage(role="user", content="go")],
            model="claude-sonnet-5",
            usage_sink=sink,
        )
    )

    assert response.tool_calls and response.tool_calls[0].name == "lookup"
    # The first output is the tool_use block start, before its input delta
    # and the final message_delta: asserted on the event order, never on a
    # wall-clock window, which a loaded xdist worker stretches.
    assert marked_after == ["content_block_start"]
    end_generation(gen, output="", stop_reason="tool_use")
    record = root["local_spans"][0]
    expected_ms = round((gen["local_first_output_at"] - gen["local_started_at"]).total_seconds() * 1000.0, 1)
    assert record["time_to_first_token"] == expected_ms
    assert record["time_to_first_token"] >= 500.0
    assert record["metadata"]["ttft_ms"] == record["time_to_first_token"]
