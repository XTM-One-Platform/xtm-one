"""Per-generation figures on span summaries, and one trace of the calling agent by id."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.services.builtin_tools.traces as traces_module
from app.tools.base import ToolCall


def _span(**overrides):
    base = dict(
        id=uuid.uuid4(),
        trace_id=uuid.uuid4(),
        type="GENERATION",
        name="llm_call",
        status_message=None,
        started_at=None,
        ended_at=None,
        duration_ms=900.0,
        input_tokens=12_000,
        output_tokens=80,
        total_cost=None,
        tool_call_id=None,
        tool_arguments=None,
        input=None,
        output=None,
        metadata_json={"observation_kind": "llm_generation"},
        time_to_first_token=None,
        usage_details=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _trace(agent_id: str, **overrides):
    now = datetime.now(UTC)
    base = dict(
        id=uuid.uuid4(),
        agent_id=uuid.UUID(agent_id),
        source="chat",
        name="agent-chat:me",
        model="m",
        status="ok",
        started_at=now - timedelta(hours=2),
        ended_at=now - timedelta(hours=2) + timedelta(seconds=9),
        duration_ms=9000.0,
        input_tokens=24_000,
        output_tokens=160,
        total_tokens=24_160,
        total_cost=None,
        error_message=None,
        input=None,
        output=None,
        metadata_json=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_a_streamed_generation_shows_its_ttft_and_cache_split():
    span = _span(
        time_to_first_token=1234.56,
        usage_details={
            "input": 12_000,
            "output": 80,
            "cache_read_input_tokens": 11_000,
            "cache_creation_input_tokens": 500,
        },
    )
    summary = traces_module._span_summary(span)
    assert summary["ttft_ms"] == 1234.6
    assert summary["cache_read_tokens"] == 11_000
    assert summary["cache_write_tokens"] == 500


def test_a_generation_that_did_not_stream_has_a_null_ttft_and_zero_cache():
    summary = traces_module._span_summary(_span(usage_details={"input": 10, "output": 2}))
    assert summary["ttft_ms"] is None
    assert summary["cache_read_tokens"] == 0
    assert summary["cache_write_tokens"] == 0


def test_a_malformed_usage_is_read_as_no_cache():
    summary = traces_module._span_summary(
        _span(time_to_first_token=True, usage_details={"cache_read_input_tokens": "lots"})
    )
    assert summary["ttft_ms"] is None
    assert summary["cache_read_tokens"] == 0


def test_a_tool_span_carries_no_generation_figures():
    summary = traces_module._span_summary(_span(type="TOOL", name="sandbox_grep"))
    assert "ttft_ms" not in summary
    assert "cache_read_tokens" not in summary


class _SessionCtx:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_exc):
        return False


class _Store:
    def __init__(self, trace, spans):
        self.trace = trace
        self.spans = spans
        self.listed = False

    async def get_trace_detail(self, _db, **kwargs):
        if self.trace is None or kwargs["trace_id"] != self.trace.id:
            raise HTTPException(status_code=404, detail="Trace not found")
        return self.trace, None, None, None

    async def get_trace_spans(self, **_kwargs):
        return self.spans

    async def list_traces(self, **_kwargs):
        self.listed = True
        return [], 0


def _install(monkeypatch, store, runs: dict | None = None):
    """*runs* maps a run id to the trace id of that run (the run lookup)."""
    monkeypatch.setattr(traces_module, "async_session", lambda: _SessionCtx())

    async def _of_run(_db, value, *, own_agent_id):
        return (runs or {}).get(str(value).strip()), None

    monkeypatch.setattr(traces_module, "_own_trace_of_run", _of_run)

    async def _root_traces(_db, value, *, own_agent_id):
        found = (runs or {}).get(str(value).strip())
        if found is None:
            return []
        return [(tid, "") for tid in (found if isinstance(found, list) else [found])]

    monkeypatch.setattr(traces_module, "_own_run_root_traces", _root_traces)

    async def _user(_db, _uid):
        return SimpleNamespace(id=uuid.uuid4(), email="me@example.com", is_admin=False), []

    async def _none_gone(_db, _ids):
        return set()

    monkeypatch.setattr(traces_module, "_load_user_context", _user)
    monkeypatch.setattr(traces_module, "_agents_gone", _none_gone)
    monkeypatch.setattr(traces_module, "_own_live_run", lambda _agent: None)
    monkeypatch.setattr(traces_module, "build_trace_read_store", lambda: store)


async def _inspect(agent_id: str, **arguments):
    _defs, executor = traces_module.build_trace_tools(db=object(), user_id=str(uuid.uuid4()), agent_id=agent_id)
    return await executor.execute_tool(ToolCall(id="c1", name="inspect_my_traces", arguments=arguments))


@pytest.mark.asyncio
async def test_inspect_my_traces_opens_one_older_trace_by_id_with_its_generation_figures(monkeypatch):
    me = str(uuid.uuid4())
    trace = _trace(me)
    spans = [
        _span(
            trace_id=trace.id,
            time_to_first_token=4100.0,
            usage_details={"cache_creation_input_tokens": 11_000},
        ),
        _span(
            trace_id=trace.id,
            time_to_first_token=900.0,
            usage_details={"cache_read_input_tokens": 11_000},
        ),
        _span(trace_id=trace.id, type="TOOL", name="sandbox_grep"),
    ]
    store = _Store(trace, spans)
    _install(monkeypatch, store)

    result = await _inspect(me, trace_id=str(trace.id), max_spans=2, span_offset=0)

    assert result.is_error is False
    listing = json.loads(result.content)
    assert store.listed is False
    assert [t["id"] for t in listing["traces"]] == [str(trace.id)]
    shown = listing["traces"][0]
    assert [s["ttft_ms"] for s in shown["spans"]] == [4100.0, 900.0]
    assert [s["cache_write_tokens"] for s in shown["spans"]] == [11_000, 0]
    assert [s["cache_read_tokens"] for s in shown["spans"]] == [0, 11_000]
    assert shown["spans_total"] == 3
    assert shown["spans_next_offset"] == 2
    assert shown["rollup"]["generations"] == 2


@pytest.mark.asyncio
async def test_another_agents_trace_by_id_answers_not_found(monkeypatch):
    me, other = str(uuid.uuid4()), str(uuid.uuid4())
    trace = _trace(other)
    _install(monkeypatch, _Store(trace, []))

    result = await _inspect(me, trace_id=str(trace.id))

    assert "trace not found among this agent's traces" in result.content


@pytest.mark.asyncio
async def test_a_missing_trace_id_answers_not_found(monkeypatch):
    me = str(uuid.uuid4())
    _install(monkeypatch, _Store(None, []))

    result = await _inspect(me, trace_id=str(uuid.uuid4()))

    assert "trace not found among this agent's traces" in result.content


@pytest.mark.asyncio
async def test_the_id_of_a_run_of_this_agent_opens_that_runs_trace(monkeypatch):
    """A work item names its holder by run id, not trace id: that id opens
    the run's trace (dev:work-item-result-paging)."""
    me = str(uuid.uuid4())
    trace = _trace(me)
    run_id = str(uuid.uuid4())
    _install(monkeypatch, _Store(trace, []), runs={run_id: trace.id})

    result = await _inspect(me, trace_id=run_id)

    assert result.is_error is False, result.content
    assert [t["id"] for t in json.loads(result.content)["traces"]] == [str(trace.id)]


@pytest.mark.asyncio
async def test_a_run_of_another_agent_still_answers_not_found(monkeypatch):
    me, other = str(uuid.uuid4()), str(uuid.uuid4())
    trace = _trace(other)
    run_id = str(uuid.uuid4())
    _install(monkeypatch, _Store(trace, []), runs={run_id: trace.id})

    result = await _inspect(me, trace_id=run_id)

    assert "trace not found among this agent's traces" in result.content


def test_the_run_lookup_reads_only_this_agents_root_traces_of_the_run():
    """The query behind a run id: this agent, root traces, the run's id."""
    import asyncio

    from sqlalchemy.dialects import postgresql

    captured: list = []

    class _Db:
        async def execute(self, stmt):
            captured.append(stmt)
            return SimpleNamespace(all=lambda: [])

    me = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    found, refusal = asyncio.run(traces_module._own_trace_of_run(_Db(), run_id, own_agent_id=me))
    assert (found, refusal) == (None, None)
    sql = str(captured[0].compile(dialect=postgresql.dialect()))
    assert "assignment_run_id" in sql and "agent_id" in sql and "parent_trace_id IS NULL" in sql

    found, refusal = asyncio.run(traces_module._own_trace_of_run(_Db(), "not-an-id", own_agent_id=me))
    assert (found, refusal) == (None, None) and len(captured) == 1


class _MultiStore(_Store):
    def __init__(self, traces):
        super().__init__(None, [])
        self.by_id = {t.id: t for t in traces}

    async def get_trace_detail(self, _db, **kwargs):
        trace = self.by_id.get(kwargs["trace_id"])
        if trace is None:
            raise HTTPException(status_code=404, detail="Trace not found")
        return trace, None, None, None


@pytest.mark.asyncio
async def test_a_run_id_with_several_traces_answers_every_trace_of_the_run(monkeypatch):
    """A run resumed after a worker stop has one trace per attempt: its run id
    answers all of them, oldest first, not an ambiguity refusal
    (dev:inspect-my-traces-own-run-id)."""
    me = str(uuid.uuid4())
    first, second = _trace(me), _trace(me)
    run_id = str(uuid.uuid4())
    _install(monkeypatch, _MultiStore([first, second]), runs={run_id: [first.id, second.id]})

    result = await _inspect(me, trace_id=run_id)

    assert result.is_error is False, result.content
    listing = json.loads(result.content)
    assert [t["id"] for t in listing["traces"]] == [str(first.id), str(second.id)]
    assert listing["total"] == 2 and "next_offset" not in listing


@pytest.mark.asyncio
async def test_a_run_id_with_eight_traces_answers_all_eight(monkeypatch):
    """More root traces than the id-prefix candidate cap (5): every one is
    listed, the newest included (dev:supervised-run-id-trace-lookup-retest)."""
    me = str(uuid.uuid4())
    attempts = [_trace(me) for _ in range(8)]
    run_id = str(uuid.uuid4())
    _install(monkeypatch, _MultiStore(attempts), runs={run_id: [t.id for t in attempts]})

    result = await _inspect(me, trace_id=run_id)

    assert result.is_error is False, result.content
    listing = json.loads(result.content)
    assert [t["id"] for t in listing["traces"]] == [str(t.id) for t in attempts]
    assert listing["total"] == 8 and "run_note" not in listing


@pytest.mark.asyncio
async def test_the_calling_runs_own_run_id_answers_its_live_summary(monkeypatch):
    """The run id a work item shows as "held by run" names the calling run while
    it is in progress: its live rollup, never "trace not found"."""
    me = str(uuid.uuid4())
    run_id = uuid.uuid4()
    store = _Store(None, [])
    _install(monkeypatch, store)
    root = {
        "local_trace_id": uuid.uuid4(),
        "local_assignment_run_id": run_id,
        "local_agent_id": me,
        "local_started_at": datetime.now(UTC) - timedelta(minutes=3),
        "local_spans": [],
    }
    monkeypatch.setattr(traces_module, "_own_live_run", lambda _agent: root)

    async def _none(*_args, **_kwargs):
        return None

    async def _no_earlier(*_args, **_kwargs):
        return []

    monkeypatch.setattr(traces_module, "_earlier_segment_rollups", _no_earlier)
    monkeypatch.setattr(traces_module, "_recorded_resumes", _none)
    monkeypatch.setattr(traces_module, "_run_frictions", _none)

    for value in (str(run_id), str(run_id)[:8], str(run_id).upper()):
        result = await _inspect(me, trace_id=value)
        assert result.is_error is False, result.content
        shown = json.loads(result.content)["traces"]
        assert len(shown) == 1 and shown[0]["in_progress"] is True
        assert shown[0]["run_id"] == str(run_id)


def test_names_own_run_matches_the_trace_id_or_the_run_id_only():
    root = {"local_trace_id": uuid.uuid4(), "local_assignment_run_id": uuid.uuid4()}
    assert traces_module._names_own_run(root, str(root["local_trace_id"]))
    assert traces_module._names_own_run(root, str(root["local_assignment_run_id"])[:8])
    assert not traces_module._names_own_run(root, str(root["local_assignment_run_id"])[:7])
    assert not traces_module._names_own_run(root, str(uuid.uuid4()))
    assert not traces_module._names_own_run(None, str(uuid.uuid4()))


def test_a_run_with_several_root_traces_is_refused_naming_them():
    import asyncio

    first, second = uuid.uuid4(), uuid.uuid4()

    class _Db:
        async def execute(self, _stmt):
            return SimpleNamespace(all=lambda: [(first, "attempt 1"), (second, "attempt 2")])

    found, refusal = asyncio.run(
        traces_module._own_trace_of_run(_Db(), str(uuid.uuid4()), own_agent_id=str(uuid.uuid4()))
    )
    assert found is None
    assert str(first) in refusal and str(second) in refusal


@pytest.mark.asyncio
async def test_a_trace_id_that_is_no_uuid_is_refused(monkeypatch):
    me = str(uuid.uuid4())
    _install(monkeypatch, _Store(None, []))

    result = await _inspect(me, trace_id="not-an-id")

    assert "trace_id must be a valid UUID" in result.content


def test_both_listings_offer_trace_id():
    """The supervision listing takes a trace or run id too
    (dev:supervised-run-id-trace-lookup), within its supervision scope."""
    defs, _exec = traces_module.build_trace_tools(db=object(), user_id=str(uuid.uuid4()), agent_id=str(uuid.uuid4()))
    by_name = {d.name: d for d in defs}
    assert "trace_id" in by_name["inspect_my_traces"].input_schema["properties"]
    assert "trace_id" in by_name["inspect_agent_traces"].input_schema["properties"]


def test_a_gateway_generation_shows_its_cache_split():
    from app.services.langfuse_tracing import _normalize_usage

    usage = _normalize_usage(
        {
            "prompt_tokens": 12_000,
            "completion_tokens": 80,
            "prompt_tokens_details": {"cached_tokens": 11_000, "cache_write_tokens": 500},
        }
    )
    summary = traces_module._span_summary(_span(usage_details=usage))
    assert summary["cache_read_tokens"] == 11_000
    assert summary["cache_write_tokens"] == 500
