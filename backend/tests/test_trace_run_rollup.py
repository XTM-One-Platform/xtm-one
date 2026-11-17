"""The run rollup stored on the trace at close and its percentiles."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.platform_insights import traces as insights_traces
from app.services.tracing import run_rollup
from app.services.tracing.run_rollup import (
    LARGEST_RESULTS,
    MAX_TOOLS,
    percentile,
    rollup_from_spans,
    rollup_percentiles,
    stored_rollup,
)


def _span(kind: str, name: str = "x", **fields):
    record = {"id": uuid.uuid4(), "type": kind, "name": name, "level": "DEFAULT", "metadata": {}}
    record.update(fields)
    return record


def _tool(name: str, *, ms: float = 10.0, output=None, level: str = "DEFAULT"):
    return _span("TOOL", f"tool:{name}", duration_ms=ms, output=output, level=level, metadata={"tool_name": name})


def _run_spans():
    return [
        _span("ITERATION", "iteration-1"),
        _span(
            "GENERATION",
            "llm",
            duration_ms=1200.0,
            input_tokens=1000,
            output_tokens=50,
            usage_details={"input": 1000, "output": 50, "cache_read_input_tokens": 800, "cache_creation_input_tokens": 100},
        ),
        _tool("sandbox_grep", ms=40.0, output={"r": "a" * 300}),
        _span("ITERATION", "iteration-2"),
        _span("GENERATION", "llm", duration_ms=800.0, input_tokens=2000, output_tokens=80, usage_details={"input": 2000}),
        _tool("get_github_file", ms=600.0, output="c" * 9000),
        _tool("wait", ms=30000.0, output="waited"),
        _tool("create_github_commit", ms=900.0, output=None, level="ERROR"),
    ]


def test_the_rollup_counts_calls_tokens_and_timings_from_the_spans():
    rollup = rollup_from_spans(_run_spans(), wall_ms=34000.0)
    assert rollup["version"] == run_rollup.ROLLUP_VERSION
    assert rollup["iterations"] == 2
    assert rollup["generations"] == 2
    assert rollup["tool_calls"] == 4
    assert rollup["tool_errors"] == 1
    assert rollup["input_tokens"] == 3000
    assert rollup["output_tokens"] == 130
    assert rollup["cache_read_tokens"] == 800
    assert rollup["cache_write_tokens"] == 100
    assert rollup["wall_ms"] == 34000.0
    assert rollup["model_ms"] == 2000.0
    # ``wait`` is idle time, counted apart from the tools' work.
    assert rollup["wait_ms"] == 30000.0
    assert rollup["tool_ms"] == 1540.0
    assert rollup["tools"]["create_github_commit"]["errors"] == 1
    assert next(iter(rollup["tools"])) == "get_github_file"
    assert rollup["largest_tool_results"][0]["tool"] == "get_github_file"
    assert rollup["largest_tool_results"][0]["result_chars"] == 9000
    # Sizes are string leaves and keys, never a serialization of the result.
    assert rollup["tool_result_chars"] == 9000 + (1 + 300) + len("waited")
    assert rollup["tool_result_sizes_cut"] == 0
    assert rollup["overhead_ms"] >= 0


def test_a_delegated_task_poll_counts_as_waiting():
    rollup = rollup_from_spans([_tool("check_task_status", ms=120000.0), _tool("grep", ms=50.0)], wall_ms=1.0)
    assert rollup["wait_ms"] == 120000.0
    assert rollup["tool_ms"] == 50.0


def test_a_huge_result_is_sized_without_serializing_it():
    # Deterministic, not a wall-clock threshold (a shared CI runner measured
    # 0.73 s against a 0.5 s bound on main): the walk is proven bounded by
    # counting the rows it opens, and serialization is made to fail loudly.
    opened: list[int] = []

    class _Row(dict):
        def items(self):
            opened.append(1)
            return super().items()

        def __repr__(self):
            raise AssertionError("the size walk serialized a row")

        __str__ = __repr__

    rows = [_Row(id=i, text="z" * 10) for i in range(run_rollup.MAX_SIZE_NODES * 50)]
    huge = {"rows": rows}
    rollup = rollup_from_spans([_tool("query", output=huge)], wall_ms=1.0)
    assert rollup["tool_result_sizes_cut"] == 1
    assert rollup["tool_result_chars"] > 0
    # At most MAX_SIZE_NODES nodes are visited, whatever the result holds.
    assert 0 < len(opened) <= run_rollup.MAX_SIZE_NODES


def test_the_rollup_holds_sizes_and_timings_never_content():
    span = _tool("read_file_content", output="SECRET-PAYLOAD " * 100)
    span["tool_arguments"] = {"q": "SECRET-ARG"}
    span["input"] = {"arguments": "SECRET-ARG"}
    rollup = rollup_from_spans([span], wall_ms=5.0)
    rendered = repr(rollup)
    assert "SECRET" not in rendered


def test_a_run_without_iteration_spans_counts_its_model_calls():
    spans = [_span("GENERATION", "llm"), _span("GENERATION", "llm")]
    assert rollup_from_spans(spans, wall_ms=None)["iterations"] == 2


def test_malformed_records_are_skipped_never_raised():
    rollup = rollup_from_spans(
        [None, "x", _span("GENERATION", duration_ms="nan?", input_tokens="many"), _tool("t", ms=float("inf"))],
        wall_ms=float("nan"),
    )
    assert rollup["generations"] == 1
    assert rollup["input_tokens"] == 0
    assert rollup["tool_ms"] == 0.0
    assert rollup["wall_ms"] == 0.0


def test_the_tool_table_and_largest_results_are_bounded():
    spans = [_tool(f"t{i}", output="x" * (i + 1)) for i in range(MAX_TOOLS + 10)]
    rollup = rollup_from_spans(spans, wall_ms=1.0)
    assert len(rollup["tools"]) == MAX_TOOLS
    assert rollup["tools_omitted"] == 10
    assert len(rollup["largest_tool_results"]) == LARGEST_RESULTS
    assert rollup["largest_tool_results"][0]["tool"] == f"t{MAX_TOOLS + 9}"


def test_the_rollup_costs_under_one_millisecond_for_a_large_run():
    """The program's gate: the instrumentation weighs under 1% of a turn.

    A 200-iteration run with 600 tool calls is far above the desk's p90 (about
    60 iterations); its rollup must still cost well under a millisecond per
    span, which is under 1% of any turn that long (minutes of wall time).
    """
    spans = []
    for i in range(200):
        spans.append(_span("ITERATION", f"iteration-{i}"))
        spans.append(_span("GENERATION", "llm", duration_ms=5000.0, input_tokens=40000, output_tokens=500))
        for j in range(3):
            spans.append(_tool(f"tool{j}", ms=300.0, output={"r": "y" * 2000}))
    started = time.perf_counter()
    rollup = rollup_from_spans(spans, wall_ms=1_000_000.0)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    assert rollup["tool_calls"] == 600
    assert elapsed_ms / len(spans) < 1.0
    # 1% of this run's wall time is 10 s; the rollup costs milliseconds.
    assert elapsed_ms < 0.01 * 1_000_000.0


def test_percentile_interpolates_between_ranks():
    assert percentile([], 0.5) is None
    assert percentile([7.0], 0.9) == 7.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == pytest.approx(2.5)
    values = [float(v) for v in range(1, 11)]
    assert percentile(values, 0.9) == pytest.approx(9.1)


def test_rollup_percentiles_give_p50_and_p90_and_skip_missing_figures():
    figures = [
        {"wall_ms": float(v), "iterations": v, "total_cost": None, "flag": True} for v in range(1, 11)
    ]
    figures[0]["cache_write_tokens"] = 40
    summary = rollup_percentiles(figures)
    assert summary["runs"] == 10
    assert summary["cache_write_tokens"]["n"] == 1
    assert summary["wall_ms"] == {"p50": 5.5, "p90": 9.1, "n": 10}
    assert summary["iterations"]["n"] == 10
    # A hidden cost is absent, not zero.
    assert "total_cost" not in summary


def test_stored_rollup_reads_the_metadata_key_only():
    assert stored_rollup(None) is None
    assert stored_rollup({"rollup": "x"}) is None
    assert stored_rollup({"rollup": {"iterations": 3}}) == {"iterations": 3}


class _FakeRoot:
    def update(self, **_kwargs):
        return None

    def set_trace_io(self, **_kwargs):
        return None

    def end(self):
        return None


def _end_trace(spans, **kwargs):
    from app.services.langfuse_tracing import end_trace

    ctx = {
        "trace": _FakeRoot(),
        "generation": None,
        "parent_ctx": None,
        "metadata": {"source": "chat"},
        "local_trace_id": uuid.uuid4(),
        "local_source": "chat",
        "local_name": "chat",
        "local_metadata": {"source": "chat"},
        "local_started_at": datetime.now(UTC) - timedelta(seconds=3),
        "local_spans": spans,
    }
    with patch("app.services.tracing.dispatcher.submit_trace_for_flush") as submit:
        end_trace(ctx, **kwargs)
    assert submit.call_args is not None
    return submit.call_args.args[0], ctx


def test_end_trace_writes_the_rollup_on_the_trace_metadata():
    trace_data, ctx = _end_trace(_run_spans(), output="done")
    rollup = trace_data.metadata["rollup"]
    assert rollup["tool_calls"] == 4
    assert rollup["wait_ms"] == 30000.0
    assert rollup["wall_ms"] == pytest.approx(trace_data.duration_ms, abs=0.1)
    # The creation-time metadata is kept, and the run's own stash is not mutated.
    assert trace_data.metadata["source"] == "chat"
    assert "rollup" not in ctx["local_metadata"]


def test_a_failed_run_carries_its_partial_rollup():
    trace_data, _ctx = _end_trace(_run_spans()[:3], error="cut by a rollout")
    assert trace_data.status == "error"
    assert trace_data.metadata["rollup"]["tool_calls"] == 1


def test_a_rollup_failure_never_breaks_the_trace_close():
    with patch.object(run_rollup, "rollup_from_spans", side_effect=RuntimeError("boom")):
        trace_data, _ctx = _end_trace(_run_spans(), output="done")
    assert "rollup" not in trace_data.metadata
    assert trace_data.status == "ok"


@pytest.mark.asyncio
async def test_list_platform_traces_returns_the_rollup_and_its_percentiles(monkeypatch):
    traces = []
    for i in range(1, 11):
        trace = SimpleNamespace(
            id=uuid.uuid4(),
            started_at=datetime.now(UTC),
            name="run",
            agent_id=None,
            source="assignment",
            model="m",
            status="ok",
            failure_cause=None,
            duration_ms=float(i * 1000),
            total_tokens=10,
            total_cost=None,
            error_message=None,
            metadata_json={"rollup": {"wall_ms": float(i * 1000), "iterations": i, "tools": {"secret": {}}}},
        )
        traces.append(trace)

    class _Row(tuple):
        agent_name = None
        user_email = None

    class _Store:
        async def list_traces(self, _db, **_kwargs):
            return [_Row((trace,)) for trace in traces], len(traces)

    monkeypatch.setattr("app.services.tracing.store.build_trace_read_store", lambda: _Store())
    monkeypatch.setattr("app.services.tracing.store.ensure_trace_read_provider_supported", lambda: None)

    async def _no_filter(_db, _scope, _args):
        return None, {"users": [], "groups": []}, []

    monkeypatch.setattr(insights_traces, "_user_filter", _no_filter)
    scope = SimpleNamespace(
        without_team=lambda: scope,
        header=lambda: {},
        group_ids=[],
        user=SimpleNamespace(id=uuid.uuid4()),
    )
    result = await insights_traces.list_platform_traces(None, scope, {"period": "7d"})
    assert result["rows"][0]["rollup"]["iterations"] == 1
    # The per-tool table never reaches a platform listing.
    assert "tools" not in result["rows"][0]["rollup"]
    assert result["rollup_percentiles"]["wall_ms"]["p50"] == 5500.0
    assert result["rollup_percentiles"]["wall_ms"]["p90"] == 9100.0


def _listed_trace(rollup):
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=None,
        source="assignment",
        name="run",
        model="m",
        status="ok",
        started_at=None,
        ended_at=None,
        duration_ms=9000.0,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        total_cost=None,
        error_message=None,
        input=None,
        output=None,
        metadata_json={"rollup": rollup},
    )


async def _deep_listing(monkeypatch, trace, *, spans_fail: bool):
    import json

    import app.services.builtin_tools.traces as traces_module

    class _Row(tuple):
        agent_name = "a"

    class _Store:
        async def list_traces(self, **_kwargs):
            return [_Row((trace,))], 1

        async def get_trace_spans(self, **_kwargs):
            if spans_fail:
                raise ConnectionError("store down")
            return []

    monkeypatch.setattr(traces_module, "build_trace_read_store", lambda: _Store())
    return json.loads(
        await traces_module._fetch_traces(
            object(),
            user=SimpleNamespace(id=uuid.uuid4(), is_admin=False),
            user_group_ids=[],
            query="",
            period="24h",
            status_filter="",
            source="",
            model="",
            limit=5,
            agent_ids=None,
            include_spans=True,
        )
    )


@pytest.mark.asyncio
async def test_a_failed_span_read_keeps_the_stored_rollup(monkeypatch):
    """A span read error is not an empty run: the stored figures stay."""
    stored = {"iterations": 7, "tool_calls": 12, "wall_ms": 9000.0}
    listing = await _deep_listing(monkeypatch, _listed_trace(stored), spans_fail=True)
    assert listing["traces"][0]["rollup"] == stored
    assert listing["rollup_percentiles"]["tool_calls"]["p50"] == 12.0


@pytest.mark.asyncio
async def test_a_listing_without_spans_carries_the_stored_rollup_without_the_tool_table(monkeypatch):
    import json

    import app.services.builtin_tools.traces as traces_module

    stored = {"iterations": 3, "wall_ms": 1000.0, "tools": {"grep": {"calls": 1}}}
    trace = _listed_trace(stored)

    class _Row(tuple):
        agent_name = "a"

    class _Store:
        async def list_traces(self, **_kwargs):
            return [_Row((trace,))], 1

    monkeypatch.setattr(traces_module, "build_trace_read_store", lambda: _Store())
    listing = json.loads(
        await traces_module._fetch_traces(
            object(),
            user=SimpleNamespace(id=uuid.uuid4(), is_admin=False),
            user_group_ids=[],
            query="",
            period="24h",
            status_filter="",
            source="",
            model="",
            limit=5,
            agent_ids=None,
            include_spans=False,
        )
    )
    assert listing["traces"][0]["rollup"] == {"iterations": 3, "wall_ms": 1000.0}
    assert listing["rollup_percentiles"]["wall_ms"]["p90"] == 1000.0


def test_the_rollup_counts_gateway_prompt_cache_reads_and_writes():
    from app.services.langfuse_tracing import _normalize_usage

    # The AI gateway reports Claude's cache in OpenAI-shaped nested details.
    usage = _normalize_usage(
        {
            "prompt_tokens": 12_000,
            "completion_tokens": 80,
            "prompt_tokens_details": {"cached_tokens": 11_000, "cache_write_tokens": 500},
        }
    )
    rollup = rollup_from_spans([_span("GENERATION", "llm", usage_details=usage)], wall_ms=1000.0)
    assert rollup["cache_read_tokens"] == 11_000
    assert rollup["cache_write_tokens"] == 500
