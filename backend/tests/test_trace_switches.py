"""The trace storage and payload switches of Settings > Usage and quotas > Observability.

Both switches were stored and served, and read by nothing: every trace was
written with its prompts, answers and tool arguments whatever an administrator
set. These tests pin what each sink writes under each position, what the
dispatcher lets reach object storage, the queue and the Redis overflow, and
how a change reaches every process.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.models.llm_trace import LLMScore, LLMSpan, LLMTrace
from app.services import trace_switches as switches_module
from app.services.trace_switches import (
    DEFAULT_TRACE_SWITCHES,
    PAYLOAD_OFF_ERROR_CHARS,
    TraceSwitches,
    apply_trace_switches,
    discard_switched_off_objects,
    publish_trace_switches,
    read_trace_switches,
    switches_from_row,
    trace_switches,
    without_payloads,
)
from app.services.tracing.base import SpanData, TraceData
from app.services.tracing.clickhouse_sink import ClickHouseSink
from app.services.tracing.dispatcher import TracingDispatcher, _serialize_trace
from app.services.tracing.local_sink import LocalPGSink
from app.services.tracing.media_offload import MEDIA_KEY, MEDIA_VERSION
from app.services.tracing.payload_offload import ENVELOPE_KEY, ENVELOPE_VERSION
from app.services.tracing.store import MirrorTracingSink

STORAGE_OFF = TraceSwitches(storage_enabled=False, store_payloads=True)
PAYLOADS_OFF = TraceSwitches(storage_enabled=True, store_payloads=False)


def _trace(**overrides) -> TraceData:
    now = datetime.now(UTC)
    fields = {
        "id": uuid4(),
        "source": "agent-chat",
        "name": "chat:support",
        "started_at": now,
        "ended_at": now,
        "duration_ms": 1200.0,
        "status": "error",
        "model": "claude-sonnet-4.6",
        "metadata": {"agent_name": "Support", "tool_count": 2},
        "input": {"messages": [{"role": "user", "content": "my IBAN is FR76 3000 6000 0112"}]},
        "output": {"content": "Your IBAN FR76 3000 6000 0112 is saved."},
        "tags": ["source:agent-chat"],
        "level": "ERROR",
        "error_message": "BadRequestError: " + "the prompt quoted back " * 40,
        "failure_cause": "model_rejected_request",
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "usage_details": {"input": 120, "output": 30, "total": 150},
    }
    fields.update(overrides)
    return TraceData(**fields)


def _tool_span(trace_id, **overrides) -> SpanData:
    now = datetime.now(UTC)
    fields = {
        "id": uuid4(),
        "trace_id": trace_id,
        "type": "TOOL",
        "name": "tool:lookup_customer",
        "position": 1,
        "started_at": now,
        "ended_at": now,
        "duration_ms": 40.0,
        "model": None,
        "model_parameters": {"temperature": 0.2},
        "input": {"tool": "lookup_customer", "arguments": '{"email": "jane@example.com"}'},
        "output": {"name": "Jane Doe", "phone": "+33 6 12 34 56 78"},
        "metadata": {"observation_kind": "tool", "tool_name": "lookup_customer"},
        "level": "DEFAULT",
        "status_message": None,
        "input_tokens": 12,
        "output_tokens": 4,
        "usage_details": {"input": 12, "output": 4, "total": 16},
        "tool_call_id": "call_1",
        "tool_arguments": {"email": "jane@example.com"},
        "stop_reason": "tool_use",
    }
    fields.update(overrides)
    return SpanData(**fields)


def _envelope(name: str) -> dict:
    return {
        ENVELOPE_KEY: ENVELOPE_VERSION,
        "size_bytes": 2_000_000,
        "sha256": "0" * 64,
        "preview": "...",
        "object_name": name,
    }


def _media(name: str) -> dict:
    return {
        MEDIA_KEY: MEDIA_VERSION,
        "content_type": "image/png",
        "size_bytes": 5000,
        "sha256": "1" * 64,
        "object_name": name,
    }


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_the_defaults_keep_the_trace_exactly_as_recorded():
    trace = _trace()
    spans = [_tool_span(trace.id)]
    assert trace_switches() == DEFAULT_TRACE_SWITCHES == TraceSwitches(True, True)
    kept = apply_trace_switches(trace, spans)
    assert kept is not None
    assert kept[0] is trace and kept[1][0] is spans[0]


def test_storage_off_keeps_nothing():
    trace = _trace()
    assert apply_trace_switches(trace, [_tool_span(trace.id)], STORAGE_OFF) is None
    assert apply_trace_switches(trace, [], TraceSwitches(False, False)) is None


def test_payloads_off_drops_what_was_said_and_keeps_the_rest():
    trace = _trace()
    span = _tool_span(trace.id, status_message="ToolError: " + "a result excerpt " * 30)
    kept = apply_trace_switches(trace, [span], PAYLOADS_OFF)
    assert kept is not None
    kept_trace, [kept_span] = kept

    assert (kept_trace.input, kept_trace.output) == (None, None)
    assert (kept_span.input, kept_span.output, kept_span.tool_arguments) == (None, None, None)
    # Timings, token counts, model, tool names and status survive.
    for name in (
        "id",
        "source",
        "name",
        "started_at",
        "ended_at",
        "duration_ms",
        "status",
        "model",
        "metadata",
        "tags",
        "level",
        "failure_cause",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "usage_details",
    ):
        assert getattr(kept_trace, name) == getattr(trace, name), name
    for name in (
        "id",
        "trace_id",
        "type",
        "name",
        "position",
        "duration_ms",
        "model_parameters",
        "metadata",
        "level",
        "input_tokens",
        "output_tokens",
        "usage_details",
        "tool_call_id",
        "stop_reason",
    ):
        assert getattr(kept_span, name) == getattr(span, name), name
    # An error keeps its head: a provider error can quote the request.
    marker = switches_module._ERROR_CUT_MARKER
    assert kept_trace.error_message == trace.error_message[:PAYLOAD_OFF_ERROR_CHARS] + marker
    assert kept_span.status_message == span.status_message[:PAYLOAD_OFF_ERROR_CHARS] + marker
    # The objects passed in are never modified: the caller may still hold them.
    assert trace.input is not None and span.tool_arguments is not None


def test_payloads_off_is_idempotent_and_leaves_a_stripped_trace_as_it_is():
    trace = _trace(error_message="RateLimitError: 429")
    once = without_payloads(trace, [_tool_span(trace.id)])
    twice = without_payloads(*once)
    assert twice[0] is once[0] and twice[1][0] is once[1][0]
    assert once[0].error_message == "RateLimitError: 429"


def test_a_row_reads_null_as_the_column_default():
    assert switches_from_row(None, None) == DEFAULT_TRACE_SWITCHES
    assert switches_from_row(False, None) == STORAGE_OFF
    assert switches_from_row(True, False) == PAYLOADS_OFF


# ---------------------------------------------------------------------------
# The PostgreSQL sink
# ---------------------------------------------------------------------------


class _Session:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, row) -> None:
        self.added.append(row)

    def add_all(self, rows) -> None:
        self.added.extend(rows)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def local_sink(monkeypatch):
    async def _no_price(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.tracing.local_sink.resolve_model_price", _no_price)
    monkeypatch.setattr("app.services.tracing.local_sink.resolve_model_price_any", _no_price)
    sessions: list[_Session] = []

    def _factory():
        session = _Session()
        sessions.append(session)
        return session

    return LocalPGSink(session_factory=_factory), sessions


def _rows(sessions, kind):
    return [row for session in sessions for row in session.added if isinstance(row, kind)]


@pytest.mark.asyncio
async def test_the_local_sink_writes_payloads_by_default(local_sink):
    sink, sessions = local_sink
    trace = _trace()
    await sink.flush_trace(trace, [_tool_span(trace.id)])
    [trace_row] = _rows(sessions, LLMTrace)
    [span_row] = _rows(sessions, LLMSpan)
    assert trace_row.input == trace.input and trace_row.output == trace.output
    assert trace_row.error_message == trace.error_message
    assert span_row.tool_arguments == {"email": "jane@example.com"}


@pytest.mark.asyncio
async def test_the_local_sink_writes_nothing_while_storage_is_off(local_sink):
    sink, sessions = local_sink
    publish_trace_switches(STORAGE_OFF)
    trace = _trace()
    await sink.flush_trace(trace, [_tool_span(trace.id)])
    await sink.flush_score({"trace_id": str(trace.id), "name": "accuracy", "value": 1.0})
    assert sessions == []


@pytest.mark.asyncio
async def test_the_local_sink_writes_no_payload_while_payloads_are_off(local_sink):
    sink, sessions = local_sink
    publish_trace_switches(PAYLOADS_OFF)
    trace = _trace()
    await sink.flush_trace(trace, [_tool_span(trace.id)])
    [trace_row] = _rows(sessions, LLMTrace)
    [span_row] = _rows(sessions, LLMSpan)
    assert (trace_row.input, trace_row.output) == (None, None)
    assert (span_row.input, span_row.output, span_row.tool_arguments) == (None, None, None)
    assert (trace_row.model, trace_row.status, trace_row.total_tokens, trace_row.duration_ms) == (
        "claude-sonnet-4.6",
        "error",
        150,
        1200.0,
    )
    assert (span_row.name, span_row.tool_call_id, span_row.input_tokens) == ("tool:lookup_customer", "call_1", 12)
    assert len(trace_row.error_message) < len(trace.error_message)
    assert sessions[0].commits == 1


@pytest.mark.asyncio
async def test_the_local_sink_keeps_scores_while_payloads_are_off(local_sink):
    sink, sessions = local_sink
    score = {"trace_id": str(uuid4()), "name": "accuracy", "value": 1.0, "comment": "spot on"}
    publish_trace_switches(PAYLOADS_OFF)
    await sink.flush_score(score)
    [score_row] = _rows(sessions, LLMScore)
    assert (score_row.value, score_row.comment) == (1.0, "spot on")


# ---------------------------------------------------------------------------
# The ClickHouse sink
# ---------------------------------------------------------------------------


class _ClickHouse:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict]]] = []

    async def insert_json_rows(self, table: str, rows: list[dict]) -> None:
        self.inserts.append((table, rows))

    async def close(self) -> None:
        return None


def _clickhouse_sink() -> tuple[ClickHouseSink, _ClickHouse]:
    client = _ClickHouse()

    async def _factory():
        return client

    return ClickHouseSink(client_factory=_factory), client


@pytest.mark.asyncio
async def test_the_clickhouse_sink_writes_payloads_by_default():
    sink, client = _clickhouse_sink()
    trace = _trace()
    await sink.flush_trace(trace, [_tool_span(trace.id)])
    tables = dict(client.inserts)
    assert tables["llm_traces"][0]["input"] == trace.input
    assert tables["llm_spans"][0]["tool_arguments"] == {"email": "jane@example.com"}


@pytest.mark.asyncio
async def test_the_clickhouse_sink_writes_nothing_while_storage_is_off():
    sink, client = _clickhouse_sink()
    publish_trace_switches(STORAGE_OFF)
    trace = _trace()
    await sink.flush_trace(trace, [_tool_span(trace.id)])
    await sink.flush_score({"trace_id": str(trace.id), "name": "accuracy", "value": 1.0})
    assert client.inserts == []


@pytest.mark.asyncio
async def test_the_clickhouse_sink_writes_no_payload_while_payloads_are_off():
    sink, client = _clickhouse_sink()
    publish_trace_switches(PAYLOADS_OFF)
    trace = _trace()
    await sink.flush_trace(trace, [_tool_span(trace.id)])
    tables = dict(client.inserts)
    [trace_row] = tables["llm_traces"]
    [span_row] = tables["llm_spans"]
    assert (trace_row["input"], trace_row["output"]) == (None, None)
    assert (span_row["input"], span_row["output"], span_row["tool_arguments"]) == (None, None, None)
    assert (trace_row["model"], trace_row["status"], trace_row["total_tokens"]) == ("claude-sonnet-4.6", "error", 150)
    assert (span_row["name"], span_row["input_tokens"], span_row["tool_call_id"]) == (
        "tool:lookup_customer",
        12,
        "call_1",
    )
    await sink.flush_score({"trace_id": str(trace.id), "name": "accuracy", "value": 1.0})
    assert "llm_scores" in dict(client.inserts)


@pytest.mark.asyncio
async def test_the_clickhouse_provider_mirror_honours_the_switches_on_both_stores(local_sink):
    local, sessions = local_sink
    clickhouse, client = _clickhouse_sink()
    mirror = MirrorTracingSink([clickhouse, local])
    publish_trace_switches(STORAGE_OFF)
    trace = _trace()
    await mirror.flush_trace(trace, [_tool_span(trace.id)])
    assert (client.inserts, sessions) == ([], [])


@pytest.mark.asyncio
async def test_the_rewrite_of_old_chat_scores_still_reaches_clickhouse_while_storage_is_off(monkeypatch):
    """The one-time rewrite of the older chat scores copies what
    PostgreSQL already holds: the storage switch stops new scores, not this
    copy. Gated, it wrote nothing while reporting success, and ClickHouse kept
    the misread score of every pair rewritten while storage was off."""
    from app.services import trace_storage

    client = _ClickHouse()

    class _RecordingClickHouseSink(ClickHouseSink):
        def __init__(self) -> None:
            async def _factory():
                return client

            super().__init__(client_factory=_factory)

    monkeypatch.setattr("app.services.tracing.store.get_trace_store_provider", lambda: "clickhouse")
    monkeypatch.setattr("app.services.tracing.clickhouse_sink.ClickHouseSink", _RecordingClickHouseSink)
    publish_trace_switches(STORAGE_OFF)
    score = {"trace_id": str(uuid4()), "user_id": str(uuid4()), "name": "user_feedback", "value": 1.0}
    async with trace_storage._clickhouse_score_copy() as copy_to_clickhouse:
        assert copy_to_clickhouse is not None
        assert await copy_to_clickhouse(score) is True
    [(table, [row])] = client.inserts
    assert (table, row["trace_id"], row["value"]) == ("llm_scores", score["trace_id"], 1.0)


# ---------------------------------------------------------------------------
# The dispatcher: object storage, the queue, the Redis overflow
# ---------------------------------------------------------------------------


class _Sink:
    def __init__(self) -> None:
        self.flushed: list[tuple[TraceData, list[SpanData]]] = []

    async def flush_trace(self, trace, spans) -> None:
        self.flushed.append((trace, spans))

    async def flush_score(self, score) -> None:
        return None

    async def close(self) -> None:
        return None


class _Pipeline:
    def __init__(self, redis) -> None:
        self.redis = redis
        self.payload = None

    def rpush(self, _key, payload):
        self.payload = payload
        return self

    def ltrim(self, *_args):
        return self

    def expire(self, *_args):
        return self

    async def execute(self):
        self.redis.values.append(self.payload)
        return True


class _Redis:
    def __init__(self) -> None:
        self.values: list[str] = []

    def pipeline(self):
        return _Pipeline(self)

    async def lpop(self, _key):
        return self.values.pop(0) if self.values else None

    async def lpush(self, _key, value):
        self.values.insert(0, value)

    async def llen(self, _key):
        return len(self.values)


def _dispatcher(sink, redis=None, queue_maxsize=5) -> TracingDispatcher:
    redis = redis or _Redis()

    async def _provider():
        return redis

    return TracingDispatcher(sinks=[sink], queue_maxsize=queue_maxsize, redis_provider=_provider)


@pytest.fixture
def offloaded(monkeypatch):
    """Every (field, value) the dispatcher hands to object storage."""
    seen: list[tuple[str, object]] = []

    async def _media(value, *, trace_id, span_id, field):
        seen.append((field, value))
        return value

    async def _oversized(value, *, trace_id, span_id, field):
        return value

    monkeypatch.setattr("app.services.tracing.dispatcher.offload_media", _media)
    monkeypatch.setattr("app.services.tracing.dispatcher.offload_if_oversized", _oversized)
    return seen


@pytest.mark.asyncio
async def test_storage_off_records_nothing_and_offloads_nothing(offloaded):
    sink = _Sink()
    redis = _Redis()
    dispatcher = _dispatcher(sink, redis, queue_maxsize=1)
    publish_trace_switches(STORAGE_OFF)
    trace = _trace()
    await dispatcher.record(trace, [_tool_span(trace.id)])
    assert dispatcher._queue.empty() and redis.values == [] and offloaded == []
    await dispatcher.close()
    assert sink.flushed == []


@pytest.mark.asyncio
async def test_payloads_off_never_hand_a_payload_to_object_storage_or_the_overflow(offloaded):
    sink = _Sink()
    redis = _Redis()
    dispatcher = _dispatcher(sink, redis, queue_maxsize=1)
    publish_trace_switches(PAYLOADS_OFF)
    dispatcher._running = True
    dispatcher._queue.put_nowait((_trace(), []))  # full: the next record spills to Redis
    trace = _trace()
    await dispatcher.record(trace, [_tool_span(trace.id)])
    assert {field for field, _value in offloaded} == {"metadata"}
    [spilled] = redis.values
    assert "FR76" not in spilled and "jane@example.com" not in spilled and "Jane Doe" not in spilled
    await dispatcher.close()


@pytest.mark.asyncio
async def test_a_trace_an_older_replica_spilled_is_written_under_the_switches_in_force(offloaded):
    sink = _Sink()
    redis = _Redis()
    trace = _trace()
    # Serialized with its payloads, as a replica without the switches spills it.
    redis.values.append(_serialize_trace(trace, [_tool_span(trace.id)]))
    publish_trace_switches(PAYLOADS_OFF)
    dispatcher = _dispatcher(sink, redis)
    await dispatcher.start()
    await dispatcher.drain(timeout_seconds=1.0)
    await dispatcher.close()
    [(written, [written_span])] = sink.flushed
    assert (written.input, written.output, written_span.tool_arguments) == (None, None, None)
    assert (written.id, written.total_tokens, written_span.name) == (trace.id, 150, "tool:lookup_customer")


@pytest.mark.asyncio
async def test_a_stripped_trace_still_reads_back_from_the_overflow_on_any_replica():
    from app.services.tracing.dispatcher import _deserialize_trace

    trace = _trace()
    stripped, spans = without_payloads(trace, [_tool_span(trace.id)])
    restored, [restored_span] = _deserialize_trace(_serialize_trace(stripped, spans))
    assert (restored.id, restored.input, restored_span.tool_arguments, restored_span.tool_call_id) == (
        trace.id,
        None,
        None,
        "call_1",
    )


@pytest.mark.asyncio
async def test_a_queued_trace_is_written_under_a_switch_changed_since_it_was_recorded(offloaded):
    sink = _Sink()
    dispatcher = _dispatcher(sink)
    trace = _trace()
    dispatcher._running = True
    await dispatcher.record(trace, [_tool_span(trace.id)])  # recorded while payloads were on
    publish_trace_switches(STORAGE_OFF)
    dispatcher._running = False
    await dispatcher.start()
    await dispatcher.drain(timeout_seconds=1.0)
    await dispatcher.close()
    assert sink.flushed == []


# ---------------------------------------------------------------------------
# The objects an in-flight trace was offloaded to
# ---------------------------------------------------------------------------


class _Deletions(list):
    """The deletions asked for, in order, over a bucket holding ``listed``."""

    def __init__(self) -> None:
        super().__init__()
        self.listed: list[str] = []
        self.resolved: dict[str, object] = {}
        self.unreadable = False
        self.unlistable = False


@pytest.fixture
def deletions(monkeypatch):
    calls = _Deletions()

    async def _by_prefix(trace_ids):
        calls.append(("prefix", list(trace_ids)))
        return 1

    async def _by_name(names):
        calls.append(("names", list(names)))
        return len(names)

    async def _list(_trace_id):
        if calls.unlistable:
            raise OSError("bucket unreachable")
        return list(calls.listed)

    async def _resolve(envelope):
        if calls.unreadable:
            raise OSError("bucket unreachable")
        return calls.resolved.get(envelope["object_name"], {})

    monkeypatch.setattr("app.services.tracing.payload_offload.delete_trace_objects", _by_prefix)
    monkeypatch.setattr("app.services.tracing.payload_offload.delete_object_names", _by_name)
    monkeypatch.setattr("app.services.tracing.payload_offload.list_trace_objects", _list)
    monkeypatch.setattr("app.services.tracing.payload_offload.resolve_payload", _resolve)
    return calls


def _offloaded_trace():
    trace = _trace(input=_envelope("traces/t/trace/a-input.json"))
    span = _tool_span(trace.id, output={"content": [_media("traces/t/s/media/b-media.png")]})
    return trace, [span]


@pytest.mark.asyncio
async def test_a_dropped_trace_takes_its_objects_with_it(deletions):
    trace, spans = _offloaded_trace()
    await discard_switched_off_objects(trace, spans, apply_trace_switches(trace, spans, STORAGE_OFF))
    assert deletions == [("prefix", [trace.id])]


@pytest.mark.asyncio
async def test_dropped_payloads_take_their_objects_with_them(deletions):
    trace, spans = _offloaded_trace()
    await discard_switched_off_objects(trace, spans, apply_trace_switches(trace, spans, PAYLOADS_OFF))
    # No kept field points at an object: the whole prefix is the dropped payloads'.
    assert deletions == [("prefix", [trace.id])]

    deletions.clear()
    trace.metadata = _envelope("traces/t/trace/c-metadata.json")
    deletions.resolved["traces/t/trace/c-metadata.json"] = {"shot": _media("traces/t/trace/media/d-metadata.png")}
    deletions.listed = [
        "traces/t/trace/a-input.json",
        # Media inside the offloaded input: the row never shows it.
        "traces/t/trace/media/e-input.png",
        "traces/t/s/media/b-media.png",
        "traces/t/trace/c-metadata.json",
        "traces/t/trace/media/d-metadata.png",
    ]
    await discard_switched_off_objects(trace, spans, apply_trace_switches(trace, spans, PAYLOADS_OFF))
    # The offloaded metadata is kept with the media inside it; everything else under the prefix goes.
    assert deletions == [
        ("names", ["traces/t/s/media/b-media.png", "traces/t/trace/a-input.json", "traces/t/trace/media/e-input.png"])
    ]


@pytest.mark.asyncio
async def test_the_media_inside_a_dropped_payload_goes_when_the_metadata_keeps_an_object(deletions):
    """Kept metadata holding media of its own must not shield the media stored
    inside a dropped offloaded payload, which only a listing of the prefix finds."""
    trace, spans = _offloaded_trace()
    trace.metadata = {"attachment": _media("traces/t/trace/media/d-metadata.png")}
    deletions.listed = [
        "traces/t/trace/a-input.json",
        "traces/t/trace/media/e-input.png",
        "traces/t/s/media/b-media.png",
        "traces/t/trace/media/d-metadata.png",
    ]
    await discard_switched_off_objects(trace, spans, apply_trace_switches(trace, spans, PAYLOADS_OFF))
    assert deletions == [
        ("names", ["traces/t/s/media/b-media.png", "traces/t/trace/a-input.json", "traces/t/trace/media/e-input.png"])
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unreadable", "unlistable"])
async def test_an_unreadable_bucket_limits_the_cleanup_to_what_the_dropped_fields_name(deletions, failure):
    """What the kept metadata points at, or what the prefix holds, is unknown:
    deleting past what the dropped fields name could take a kept object."""
    trace, spans = _offloaded_trace()
    trace.metadata = _envelope("traces/t/trace/c-metadata.json")
    deletions.listed = ["traces/t/trace/c-metadata.json", "traces/t/trace/media/d-metadata.png"]
    setattr(deletions, failure, True)
    await discard_switched_off_objects(trace, spans, apply_trace_switches(trace, spans, PAYLOADS_OFF))
    assert deletions == [("names", ["traces/t/s/media/b-media.png", "traces/t/trace/a-input.json"])]


@pytest.mark.asyncio
async def test_nothing_is_deleted_for_a_trace_kept_whole_or_holding_no_object(deletions):
    trace, spans = _offloaded_trace()
    await discard_switched_off_objects(trace, spans, apply_trace_switches(trace, spans))
    inline = _trace()
    await discard_switched_off_objects(inline, [], apply_trace_switches(inline, [], STORAGE_OFF))
    await discard_switched_off_objects(inline, [], apply_trace_switches(inline, [], PAYLOADS_OFF))
    assert deletions == []


@pytest.mark.asyncio
async def test_a_bucket_failure_never_stops_the_trace(monkeypatch):
    async def _down(_trace_ids):
        raise OSError("bucket unreachable")

    monkeypatch.setattr("app.services.tracing.payload_offload.delete_trace_objects", _down)
    trace, spans = _offloaded_trace()
    assert await discard_switched_off_objects(trace, spans, None) == 0


# ---------------------------------------------------------------------------
# Propagation: every process, within the security options poll
# ---------------------------------------------------------------------------


class _Row:
    def __init__(self, row) -> None:
        self._row = row

    def first(self):
        return self._row

    def scalar_one_or_none(self):
        return None


class _Database:
    """The ``platform_settings`` row as another process may have just changed it."""

    def __init__(self, storage=True, payloads=True) -> None:
        self.row: tuple | None = (storage, payloads)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        text = str(statement)
        if "trace_storage_enabled" in text:
            return _Row(self.row)
        return _Row(None)


@pytest.mark.asyncio
async def test_the_switches_are_read_off_the_platform_settings_row():
    database = _Database(storage=True, payloads=False)
    assert await read_trace_switches(database) == PAYLOADS_OFF
    database.row = None
    assert await read_trace_switches(database) == DEFAULT_TRACE_SWITCHES


@pytest.mark.asyncio
async def test_the_security_options_refresh_puts_the_stored_switches_in_force(monkeypatch):
    from app.services import security_options as service

    database = _Database(storage=True, payloads=False)
    monkeypatch.setattr("app.core.database.async_session", lambda: database)
    assert not switches_module.snapshot_loaded()
    assert await service.refresh_security_options_cache() is True
    assert trace_switches() == PAYLOADS_OFF and switches_module.snapshot_loaded()


@pytest.mark.asyncio
async def test_a_change_made_on_another_process_applies_here_within_one_poll(monkeypatch):
    from app.services import security_options as service

    database = _Database()
    monkeypatch.setattr("app.core.database.async_session", lambda: database)
    poll = asyncio.create_task(service.run_security_options_refresh_loop(interval=0.01))
    try:
        database.row = (False, True)  # an administrator saved on another replica
        async with asyncio.timeout(2):
            while trace_switches() != STORAGE_OFF:
                await asyncio.sleep(0.01)
        database.row = (True, True)
        async with asyncio.timeout(2):
            while trace_switches() != DEFAULT_TRACE_SWITCHES:
                await asyncio.sleep(0.01)
    finally:
        poll.cancel()
        with pytest.raises(asyncio.CancelledError):
            await poll


@pytest.mark.asyncio
async def test_a_failed_read_keeps_the_switches_in_force(monkeypatch):
    from app.services import security_options as service

    class _Down:
        async def __aenter__(self):
            raise OSError("database down")

        async def __aexit__(self, *exc):
            return False

    publish_trace_switches(PAYLOADS_OFF)
    monkeypatch.setattr("app.core.database.async_session", lambda: _Down())
    assert await service.refresh_security_options_cache() is False
    assert trace_switches() == PAYLOADS_OFF


@pytest.mark.asyncio
async def test_a_poll_that_read_before_a_save_never_puts_the_old_switches_back(monkeypatch):
    from app.services import security_options as service

    started, release = asyncio.Event(), asyncio.Event()

    async def _read(_db):
        started.set()
        await release.wait()
        return DEFAULT_TRACE_SWITCHES  # what the row held when this poll began

    monkeypatch.setattr("app.core.database.async_session", lambda: _Database())
    monkeypatch.setattr(service, "read_trace_switches", _read)
    poll = asyncio.create_task(service.refresh_security_options_cache())
    await started.wait()
    # The administrator's save commits and publishes while that read is out.
    assert publish_trace_switches(STORAGE_OFF) is True
    release.set()
    assert await poll is True
    assert trace_switches() == STORAGE_OFF
