"""Trace payloads are stored whole; only oversized ones move to object storage.

These tests pin the property the trace layer previously violated: nothing on the
durable path is truncated.  The regression they guard against is specific — the
old code capped both the reasoning string and the whole serialized payload at
20 000 characters, so a long reasoning block pushed the generation output past
the limit, ``json.loads`` failed on the clipped text, and the payload was stored
as an unparseable string.  The span inspector reads ``output.reasoning``, so the
reasoning pane simply disappeared for exactly the models that produce the most
of it.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.core.config import settings
from app.services import langfuse_tracing as lft
from app.services.tracing import payload_offload as po
from app.tools import agent_loop as al

# ---------------------------------------------------------------------------
# No truncation on the durable path
# ---------------------------------------------------------------------------


def test_to_jsonable_keeps_a_huge_payload_whole_and_parseable():
    reasoning = "step " * 40_000  # 200k chars — far past every legacy cap
    payload = {"content": "Final answer.", "reasoning": reasoning}

    stored = lft._to_jsonable(payload)

    assert isinstance(stored, dict), "the object must not collapse into a string"
    assert stored["reasoning"] == reasoning
    assert "truncated" not in json.dumps(stored)


def test_to_jsonable_preserves_reasoning_alongside_tool_calls():
    """The tool-call envelope used to push the payload over the cap sooner."""
    payload = {
        "content": "x" * 30_000,
        "reasoning": "y" * 30_000,
        "tool_calls": [
            {
                "id": "t1",
                "type": "function",
                "function": {"name": "search_kb", "arguments": json.dumps({"q": "z" * 20_000})},
            }
        ],
    }

    stored = lft._to_jsonable(payload)

    assert set(stored) == {"content", "reasoning", "tool_calls"}
    assert len(stored["reasoning"]) == 30_000
    assert stored["tool_calls"][0]["function"]["name"] == "search_kb"


def test_to_jsonable_normalizes_non_json_types():
    """The JSON round-trip is load-bearing: JSONB rejects datetimes and UUIDs."""
    trace_id = uuid.uuid4()
    stored = lft._to_jsonable({"id": trace_id, "nested": {"n": {1, 2}}})

    assert stored["id"] == str(trace_id)
    assert isinstance(stored["nested"]["n"], str)


def test_to_jsonable_strips_null_bytes():
    """Postgres rejects \\u0000 in JSONB, so sanitization must survive."""
    stored = lft._to_jsonable({"content": "before\x00after"})

    assert "\x00" not in json.dumps(stored)


def test_bound_for_langfuse_still_caps_the_external_export():
    """The external sink keeps its caps: its ingestion rejects oversized events."""
    bounded = lft._bound_for_langfuse({"reasoning": "r" * 50_000})

    assert len(bounded["reasoning"]) < 50_000
    assert "truncated" in bounded["reasoning"]


def test_merge_trace_reasoning_does_not_truncate():
    captured = "a" * 60_000
    merged = al._merge_trace_reasoning(captured, None)

    assert merged == captured


def test_merge_trace_reasoning_deduplicates_identical_sources():
    """Streamed deltas and ``response.thinking`` often carry the same text."""
    assert al._merge_trace_reasoning("same", "same") == "same"
    assert al._merge_trace_reasoning("streamed", "final") == "streamed\n\nfinal"


# ---------------------------------------------------------------------------
# Display previews keep their structure
# ---------------------------------------------------------------------------


def test_display_preview_keeps_the_object_shape():
    """The whole point: a bounded preview must still expose ``reasoning``."""
    payload = {"content": "answer", "reasoning": "r" * 100_000}

    preview, truncated = po.build_display_preview(payload, limit=20_000)

    assert truncated is True
    assert isinstance(preview, dict)
    assert set(preview) == {"content", "reasoning"}
    assert preview["content"] == "answer"
    assert "truncated" in preview["reasoning"]
    assert po.payload_size_bytes(preview) <= 20_000


def test_display_preview_leaves_small_payloads_untouched():
    payload = {"content": "answer", "reasoning": "short"}

    preview, truncated = po.build_display_preview(payload, limit=20_000)

    assert truncated is False
    assert preview == payload


def test_display_preview_bounds_long_lists_by_item_count():
    """Per-string shrinking alone cannot bound 100k tiny messages."""
    payload = {"messages": [{"role": "user", "content": "hi"} for _ in range(100_000)]}

    preview, truncated = po.build_display_preview(payload, limit=20_000)

    assert truncated is True
    assert po.payload_size_bytes(preview) <= 20_000
    assert isinstance(preview["messages"][-1], str)
    assert "items omitted" in preview["messages"][-1]


def test_display_preview_passes_an_offload_envelope_through():
    envelope = {po.ENVELOPE_KEY: 1, "object_name": "traces/x/out.json", "preview": "head"}

    preview, truncated = po.build_display_preview(envelope, limit=10)

    assert preview is envelope
    assert truncated is False


# ---------------------------------------------------------------------------
# Offload / resolve round-trip
# ---------------------------------------------------------------------------


class _FakeStorage:
    def __init__(self, *, fail: bool = False) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.fail = fail

    async def upload(self, data, filename, content_type=None, prefix=""):
        if self.fail:
            raise RuntimeError("object storage unreachable")
        name = f"{prefix}/{filename}"
        self.objects[name] = data
        return name

    async def download(self, object_name):
        return self.objects[object_name]

    async def delete(self, object_name):
        self.deleted.append(object_name)
        self.objects.pop(object_name, None)


@pytest.fixture
def storage(monkeypatch):
    fake = _FakeStorage()
    monkeypatch.setattr("app.services.file_storage.file_storage", fake)
    return fake


@pytest.mark.asyncio
async def test_small_payload_is_stored_inline(storage, monkeypatch):
    monkeypatch.setattr(settings, "trace_payload_offload_threshold_bytes", 1_000_000)
    payload = {"content": "small"}

    result = await po.offload_if_oversized(
        payload, trace_id=uuid.uuid4(), span_id=uuid.uuid4(), field="output"
    )

    assert result is payload
    assert storage.objects == {}


@pytest.mark.asyncio
async def test_oversized_payload_round_trips_through_object_storage(storage, monkeypatch):
    monkeypatch.setattr(settings, "trace_payload_offload_threshold_bytes", 1_000)
    trace_id, span_id = uuid.uuid4(), uuid.uuid4()
    payload = {"content": "answer", "reasoning": "r" * 5_000}

    envelope = await po.offload_if_oversized(
        payload, trace_id=trace_id, span_id=span_id, field="output"
    )

    assert po.is_offload_envelope(envelope)
    assert envelope["size_bytes"] == po.payload_size_bytes(payload)
    assert envelope["preview"].startswith('{"content"')
    assert str(trace_id) in envelope["object_name"]
    # Nothing is lost: the stored object rebuilds the payload byte for byte.
    assert await po.resolve_payload(envelope) == payload


@pytest.mark.asyncio
async def test_resolve_passes_inline_payloads_through(storage):
    payload = {"content": "inline"}
    assert await po.resolve_payload(payload) is payload


@pytest.mark.asyncio
async def test_offload_failure_falls_back_to_inline_storage(monkeypatch):
    """An S3 outage must not cost us the payload — fidelity beats tidiness."""
    monkeypatch.setattr("app.services.file_storage.file_storage", _FakeStorage(fail=True))
    monkeypatch.setattr(settings, "trace_payload_offload_threshold_bytes", 1_000)
    monkeypatch.setattr(settings, "trace_payload_hard_max_bytes", 10_000_000)
    payload = {"reasoning": "r" * 5_000}

    result = await po.offload_if_oversized(
        payload, trace_id=uuid.uuid4(), span_id=None, field="output"
    )

    assert result is payload


@pytest.mark.asyncio
async def test_offload_failure_past_the_hard_max_records_the_loss(monkeypatch):
    """Past the inline ceiling the gap is reported, never silently swallowed."""
    monkeypatch.setattr("app.services.file_storage.file_storage", _FakeStorage(fail=True))
    monkeypatch.setattr(settings, "trace_payload_offload_threshold_bytes", 1_000)
    monkeypatch.setattr(settings, "trace_payload_hard_max_bytes", 2_000)
    payload = {"reasoning": "r" * 50_000}

    result = await po.offload_if_oversized(
        payload, trace_id=uuid.uuid4(), span_id=None, field="output"
    )

    assert po.is_offload_envelope(result)
    assert result["offload_failed"] is True
    assert result["error"]
    assert "object_name" not in result
    assert result["preview"]


@pytest.mark.asyncio
async def test_disabled_threshold_always_stores_inline(storage, monkeypatch):
    monkeypatch.setattr(settings, "trace_payload_offload_threshold_bytes", 0)
    payload = {"reasoning": "r" * 500_000}

    result = await po.offload_if_oversized(
        payload, trace_id=uuid.uuid4(), span_id=None, field="output"
    )

    assert result is payload
    assert storage.objects == {}


@pytest.mark.asyncio
async def test_delete_object_names_survives_a_bucket_error(monkeypatch):
    class _Failing(_FakeStorage):
        async def delete(self, object_name):
            raise RuntimeError("bucket gone")

    monkeypatch.setattr("app.services.file_storage.file_storage", _Failing())

    # A storage failure must never block the row deletion the caller is doing.
    assert await po.delete_object_names(["traces/a/out.json"]) == 0


# ---------------------------------------------------------------------------
# payload_meta — what the API tells the UI
# ---------------------------------------------------------------------------


def test_payload_meta_is_absent_for_a_complete_small_payload():
    assert po.payload_meta({"content": "x"}, preview_truncated=False) is None


def test_payload_meta_reports_a_truncated_preview():
    meta = po.payload_meta({"content": "x" * 100}, preview_truncated=True)

    assert meta == {"truncated": True, "offloaded": False, "size_bytes": meta["size_bytes"]}
    assert meta["size_bytes"] > 100


def test_payload_meta_reports_an_offloaded_payload():
    envelope = {
        po.ENVELOPE_KEY: 1,
        "object_name": "traces/a/out.json",
        "size_bytes": 4_000_000,
        "sha256": "abc",
    }

    meta = po.payload_meta(envelope, preview_truncated=False)

    assert meta["offloaded"] is True
    assert meta["truncated"] is True
    assert meta["size_bytes"] == 4_000_000
    assert meta["sha256"] == "abc"


def test_payload_meta_surfaces_a_failed_offload():
    envelope = {
        po.ENVELOPE_KEY: 1,
        "size_bytes": 90_000_000,
        "offload_failed": True,
        "error": "boom",
    }

    meta = po.payload_meta(envelope, preview_truncated=False)

    assert meta["offload_failed"] is True
    assert meta["error"] == "boom"


# ---------------------------------------------------------------------------
# _build_generation_trace_output — reasoning reaches the span payload
# ---------------------------------------------------------------------------


def _response(content="answer", *, thinking=None, tool_calls=None):
    from app.llm.base import ChatResponse

    return ChatResponse(
        content=content,
        model="claude-opus-5",
        thinking=thinking,
        tool_calls=tool_calls,
    )


def test_generation_output_carries_streamed_reasoning():
    payload = al._build_generation_trace_output(
        _response(), captured_reasoning="thought about it"
    )

    assert payload["content"] == "answer"
    assert payload["reasoning"] == "thought about it"


def test_generation_output_carries_non_streamed_reasoning():
    """Providers that return thinking in one shot must not be treated as empty."""
    payload = al._build_generation_trace_output(_response(thinking="one-shot"))

    assert payload["reasoning"] == "one-shot"


def test_generation_output_keeps_reasoning_next_to_tool_calls():
    from app.llm.base import ToolCall

    payload = al._build_generation_trace_output(
        _response(content="", tool_calls=[ToolCall(id="t1", name="search", arguments={"q": "x"})]),
        captured_reasoning="why I called the tool",
    )

    assert payload["reasoning"] == "why I called the tool"
    assert payload["tool_calls"][0]["function"]["name"] == "search"


def test_generation_output_is_plain_content_without_reasoning():
    """No reasoning, no tool calls: the payload stays the bare string it was."""
    assert al._build_generation_trace_output(_response()) == "answer"


def test_generation_output_keeps_a_huge_reasoning_block_whole():
    reasoning = "r" * 250_000
    payload = al._build_generation_trace_output(_response(), captured_reasoning=reasoning)

    assert payload["reasoning"] == reasoning


def test_empty_response_retries_no_longer_discard_reasoning():
    """The four empty-response retry closes used to hardcode {"content": ...}.

    The reasoning was streamed to the user's screen and then dropped, so the one
    record of what the model did during a failed call was lost.
    """
    import inspect

    source = inspect.getsource(al)
    assert 'output={"content": retry_resp.content or ""}' not in source
    assert 'output={"content": ""}' not in source


# ---------------------------------------------------------------------------
# Dispatcher wiring — the offload runs before anything is enqueued
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_offloads_before_enqueueing(storage, monkeypatch):
    """The queue, the Redis overflow buffer and both sinks must carry the pointer.

    Offloading later would put the multi-megabyte blob through all of them.
    """
    from app.services.tracing.base import SpanData, TraceData
    from app.services.tracing.dispatcher import TracingDispatcher

    monkeypatch.setattr(settings, "trace_payload_offload_threshold_bytes", 1_000)

    trace_id = uuid.uuid4()
    now = __import__("datetime").datetime.now(__import__("datetime").UTC)
    trace = TraceData(
        id=trace_id,
        source="test",
        name="t",
        started_at=now,
    )
    span = SpanData(
        id=uuid.uuid4(),
        trace_id=trace_id,
        type="GENERATION",
        name="llm",
        position=0,
        started_at=now,
        output={"content": "answer", "reasoning": "r" * 5_000},
    )

    dispatcher = TracingDispatcher(sinks=[])
    dispatcher._running = True  # avoid starting the worker task
    try:
        await dispatcher.record(trace, [span])
    finally:
        dispatcher._running = False

    queued_trace, queued_spans = dispatcher._queue.get_nowait()
    assert queued_trace is trace
    assert po.is_offload_envelope(queued_spans[0].output)
    # And the payload behind the pointer is intact.
    assert await po.resolve_payload(queued_spans[0].output) == {
        "content": "answer",
        "reasoning": "r" * 5_000,
    }


@pytest.mark.asyncio
async def test_dispatcher_leaves_small_payloads_alone(storage, monkeypatch):
    from app.services.tracing.base import SpanData, TraceData
    from app.services.tracing.dispatcher import TracingDispatcher

    monkeypatch.setattr(settings, "trace_payload_offload_threshold_bytes", 1_000_000)

    trace_id = uuid.uuid4()
    now = __import__("datetime").datetime.now(__import__("datetime").UTC)
    trace = TraceData(id=trace_id, source="test", name="t", started_at=now)
    output = {"content": "answer", "reasoning": "short"}
    span = SpanData(
        id=uuid.uuid4(),
        trace_id=trace_id,
        type="GENERATION",
        name="llm",
        position=0,
        started_at=now,
        output=output,
    )

    dispatcher = TracingDispatcher(sinks=[])
    dispatcher._running = True
    try:
        await dispatcher.record(trace, [span])
    finally:
        dispatcher._running = False

    _, queued_spans = dispatcher._queue.get_nowait()
    assert queued_spans[0].output is output
    assert storage.objects == {}
