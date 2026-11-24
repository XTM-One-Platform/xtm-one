from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.services.builtin_tools.traces as traces_module
import app.services.tracing.payload_offload as payload_offload
from app.services.trace_switches import TraceSwitches, publish_trace_switches
from app.tools.base import ToolCall


class _FakeSessionCtx:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False


class _FakeDBSession:
    def add(self, _entry):
        return None

    async def flush(self):
        return None

    async def commit(self):
        return None


def _fake_async_session_factory(db):
    def _factory():
        return _FakeSessionCtx(db)

    return _factory


class _MarkerRedis:
    """The Redis the trace read audit spaces its rows with, in memory."""

    def __init__(self, *, fail: bool = False, events: list[str] | None = None):
        self.keys: dict[str, int | None] = {}
        self.fail = fail
        self.events = events if events is not None else []

    async def exists(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return int(key in self.keys)

    async def set(self, key, _value, ex=None):
        self.events.append("mark")
        self.keys[key] = ex


@pytest.fixture(autouse=True)
def marker_redis(monkeypatch):
    store = _MarkerRedis()

    async def _client():
        return store

    import app.core.redis as redis_module

    monkeypatch.setattr(redis_module, "redis_client", _client)
    return store


@pytest.fixture(autouse=True)
def readable_targets(monkeypatch):
    """Every allow-listed target is one whose chats the caller may read: the
    narrowing to readable targets is pinned by ``test_shared_agent_trace_reach.py``."""

    async def _readable(_db, _user, agent_ids, _group_ids=None):
        return {str(uuid.UUID(str(i))) for i in agent_ids}

    monkeypatch.setattr(traces_module, "agents_whose_traces_user_may_read", _readable)


@pytest.mark.asyncio
async def test_trace_tools_expose_self_and_supervision_definitions():
    defs, _exec = traces_module.build_trace_tools(
        db=object(),
        user_id=str(uuid.uuid4()),
        agent_id=str(uuid.uuid4()),
    )
    names = {d.name for d in defs}
    assert "inspect_my_traces" in names
    assert "inspect_agent_traces" in names
    # The companion fetch ships with the inspection surface: a bounded preview
    # is only honest if the full payload is reachable.
    assert traces_module.TRACE_PAYLOAD_TOOL_NAME in names


@pytest.mark.asyncio
async def test_listing_tool_schemas_default_to_including_spans():
    defs, _exec = traces_module.build_trace_tools(
        db=object(),
        user_id=str(uuid.uuid4()),
        agent_id=str(uuid.uuid4()),
    )
    listing_defs = [
        d for d in defs if d.name != traces_module.TRACE_PAYLOAD_TOOL_NAME
    ]
    assert listing_defs
    for definition in listing_defs:
        include_spans = definition.input_schema["properties"]["include_spans"]
        assert include_spans["default"] is True


def _fake_span(**overrides):
    base = dict(
        id=uuid.uuid4(),
        trace_id=uuid.uuid4(),
        type="TOOL",
        name="search_knowledge",
        status_message=None,
        started_at=None,
        ended_at=None,
        duration_ms=12.5,
        input_tokens=10,
        output_tokens=20,
        total_cost=None,
        tool_call_id="call_abc123",
        tool_arguments={"query": "why did the run fail"},
        input=None,
        output=None,
        metadata_json=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_span_summary_includes_tool_call_details():
    span = _fake_span(output={"result": "x" * 5000})
    summary = traces_module._span_summary(span)
    assert summary["tool_call_id"] == "call_abc123"
    assert "why did the run fail" in summary["tool_arguments"]
    assert "[truncated" in summary["output_preview"]
    assert len(summary["output_preview"]) <= traces_module._PREVIEW_MAX_CHARS + 40


def test_span_summary_names_the_fields_it_held_back():
    """A cut preview must point at what to fetch, or it is a dead end."""
    span = _fake_span(
        input={"messages": [{"role": "user", "content": "y" * 5000}]},
        output={"content": "z" * 5000},
    )
    summary = traces_module._span_summary(span)
    assert set(summary["payload_truncated"]) == {"input", "output"}
    # Short fields are returned whole and must not be listed.
    assert "tool_arguments" not in summary["payload_truncated"]
    assert "metadata" not in summary["payload_truncated"]


def test_span_summary_flags_an_offloaded_payload_even_when_its_excerpt_is_short():
    """An envelope is a pointer: never report its excerpt as the whole value."""
    span = _fake_span(
        output={
            payload_offload.ENVELOPE_KEY: payload_offload.ENVELOPE_VERSION,
            "size_bytes": 4_000_000,
            "sha256": "0" * 64,
            "encoding": "json",
            "preview": "short excerpt",
            "object_name": "traces/x/span/output.json",
        }
    )
    summary = traces_module._span_summary(span)
    assert "output" in summary["payload_truncated"]
    assert summary["output_preview"] == "short excerpt"
    # The digest and object name are transport bookkeeping, not payload.
    assert "sha256" not in (summary["output_preview"] or "")


def _rollup_trace(**overrides):
    base = dict(input_tokens=None, output_tokens=None, total_cost=None)
    base.update(overrides)
    return SimpleNamespace(**base)


def test_run_rollup_counts_iterations_tools_errors_and_tokens():
    """C7: the per-run figures come from every span, not the previews."""
    spans = [
        _fake_span(type="ITERATION", name="iteration 1"),
        _fake_span(type="GENERATION", name="llm", input_tokens=1000, output_tokens=50, total_cost=0.01),
        _fake_span(type="TOOL", name="sandbox_grep", output={"r": "a" * 300}),
        _fake_span(type="ITERATION", name="iteration 2"),
        _fake_span(type="GENERATION", name="llm", input_tokens=2000, output_tokens=80, total_cost=0.02),
        _fake_span(type="TOOL", name="sandbox_grep", output={"r": "b" * 30}),
        _fake_span(type="TOOL", name="get_github_file", output="c" * 9000),
        _fake_span(type="ITERATION", name="iteration 3"),
        _fake_span(type="TOOL", name="create_github_commit", output="refused", level="ERROR"),
    ]
    rollup = traces_module._run_rollup(_rollup_trace(), spans)
    assert rollup["iterations"] == 3
    assert rollup["generations"] == 2
    assert rollup["tool_calls"] == 4
    assert rollup["tool_errors"] == 1
    assert rollup["input_tokens"] == 3000
    assert rollup["output_tokens"] == 130
    assert rollup["total_cost"] == pytest.approx(0.03)
    assert rollup["tools"]["sandbox_grep"]["calls"] == 2
    assert rollup["tools"]["create_github_commit"]["errors"] == 1
    # Tools are ordered by what their results weighed.
    assert next(iter(rollup["tools"])) == "get_github_file"
    largest = rollup["largest_tool_results"]
    assert largest[0]["tool"] == "get_github_file"
    assert largest[0]["result_chars"] == 9000
    assert largest[0]["span_id"] == str(spans[6].id)
    assert [r["result_chars"] for r in largest] == sorted(
        (r["result_chars"] for r in largest), reverse=True
    )


def test_run_rollup_counts_an_offloaded_result_by_its_stored_size():
    envelope = {
        payload_offload.ENVELOPE_KEY: payload_offload.ENVELOPE_VERSION,
        "size_bytes": 4_000_000,
        "sha256": "0" * 64,
        "encoding": "json",
        "preview": "short excerpt",
        "object_name": "traces/x/span/output.json",
    }
    spans = [
        _fake_span(type="TOOL", name="read_file_content", output=envelope),
        _fake_span(type="TOOL", name="sandbox_outline", output="x" * 10),
    ]
    rollup = traces_module._run_rollup(_rollup_trace(), spans)
    assert rollup["largest_tool_results"][0]["tool"] == "read_file_content"
    assert rollup["largest_tool_results"][0]["result_chars"] == 4_000_000
    assert rollup["tool_result_chars"] == 4_000_010


def test_run_rollup_falls_back_to_trace_totals_and_model_calls():
    """A run recorded without iteration spans or span usage still has figures."""
    spans = [
        _fake_span(type="GENERATION", name="llm", input_tokens=None, output_tokens=None),
        _fake_span(type="GENERATION", name="llm", input_tokens=None, output_tokens=None),
        _fake_span(type="TOOL", name="noop", output=None),
    ]
    trace = _rollup_trace(input_tokens=500, output_tokens=40, total_cost=0.5)
    rollup = traces_module._run_rollup(trace, spans)
    assert rollup["iterations"] == 2
    assert rollup["input_tokens"] == 500
    assert rollup["output_tokens"] == 40
    assert rollup["total_cost"] == pytest.approx(0.5)
    # An empty result is not one of the "largest".
    assert rollup["largest_tool_results"] == []
    assert rollup["tools"]["noop"] == {"calls": 1, "errors": 0, "result_chars": 0}


def test_run_rollup_caps_the_largest_results():
    spans = [
        _fake_span(type="TOOL", name=f"t{i}", output="x" * (i + 1)) for i in range(12)
    ]
    rollup = traces_module._run_rollup(_rollup_trace(), spans)
    assert len(rollup["largest_tool_results"]) == traces_module._ROLLUP_LARGEST_RESULTS
    assert rollup["largest_tool_results"][0]["tool"] == "t11"


def test_span_summary_exposes_input_not_only_output():
    """The prompt a step received is half of any diagnosis."""
    span = _fake_span(input={"messages": [{"role": "system", "content": "persona"}]})
    summary = traces_module._span_summary(span)
    assert "persona" in summary["input_preview"]


_PREVIEW_KEYS = ("output_preview", "input_preview", "metadata_preview", "tool_arguments")


def _sum_preview_chars(summaries) -> int:
    return sum(len(s[key] or "") for s in summaries for key in _PREVIEW_KEYS)


def _summarize_spans(count: int, total: int):
    budget = traces_module._PreviewBudget(total=total)
    spans = [
        _fake_span(output={"result": "x" * 5000}, input={"messages": "y" * 5000})
        for _ in range(count)
    ]
    return [traces_module._span_summary(span, budget) for span in spans], budget


def test_preview_budget_does_not_scale_with_span_count():
    """A per-field floor is not a ceiling: `get_trace_spans` is unlimited.

    `_MAX_SPAN_TRACE_COUNT` bounds traces, not spans, so a floor applied on
    every field of every span multiplied by span count — 200 spans put the real
    bound near a quarter of a million characters. Ten times the spans must cost
    the same once the budget is gone.
    """
    few, _ = _summarize_spans(20, 3_000)
    many, budget = _summarize_spans(200, 3_000)
    assert _sum_preview_chars(few) == _sum_preview_chars(many)
    # Bounded by the budget plus truncation markers, not by the span count.
    assert _sum_preview_chars(many) < 3_000 + 1_000
    assert budget.previews_omitted > 0


def test_a_span_past_the_budget_says_nothing_rather_than_a_floors_worth():
    summaries, budget = _summarize_spans(200, 3_000)
    # Early spans — where a run usually goes wrong — still carry previews.
    assert summaries[0]["output_preview"]
    # Late ones carry none...
    assert summaries[-1]["output_preview"] is None
    # ...but still name what to fetch, so a null preview is never read as an
    # empty payload, and the count is reported for the same reason.
    assert "output" in summaries[-1]["payload_truncated"]
    assert budget.previews_omitted > 0


@pytest.mark.asyncio
async def test_inspect_my_traces_requires_authenticated_user():
    _defs, executor = traces_module.build_trace_tools(
        db=object(),
        user_id="anonymous",
        agent_id=str(uuid.uuid4()),
    )
    result = await executor.execute_tool(
        ToolCall(id="1", name="inspect_my_traces", arguments={})
    )
    assert result.is_error is True
    assert "requires an authenticated user context" in result.content


@pytest.mark.asyncio
async def test_inspect_agent_traces_denied_without_admin_or_allowlist(monkeypatch):
    fake_db = _FakeDBSession()
    monkeypatch.setattr(
        traces_module,
        "async_session",
        _fake_async_session_factory(fake_db),
    )

    async def _fake_user_context(_db, _uid):
        return SimpleNamespace(
            id=uuid.uuid4(),
            email="test-denied@example.com",
            is_admin=False,
        ), []

    async def _should_not_fetch(*args, **kwargs):
        raise AssertionError("_fetch_traces should not be called when access is denied")

    monkeypatch.setattr(traces_module, "_load_user_context", _fake_user_context)
    monkeypatch.setattr(traces_module, "_fetch_traces", _should_not_fetch)

    _defs, executor = traces_module.build_trace_tools(
        db=object(),
        user_id=str(uuid.uuid4()),
        agent_id=str(uuid.uuid4()),
        supervision_target_agent_ids=[],
    )

    result = await executor.execute_tool(
        ToolCall(
            id="2",
            name="inspect_agent_traces",
            arguments={"target_agent_id": str(uuid.uuid4())},
        )
    )

    assert result.is_error is True
    assert "not allowed to inspect traces" in result.content


@pytest.mark.asyncio
async def test_inspect_agent_traces_allowed_for_allowlisted_target(monkeypatch):
    fake_db = _FakeDBSession()
    monkeypatch.setattr(
        traces_module,
        "async_session",
        _fake_async_session_factory(fake_db),
    )

    target_agent_id = str(uuid.uuid4())
    captured: dict = {}

    async def _fake_user_context(_db, _uid):
        return SimpleNamespace(
            id=uuid.uuid4(),
            email="test-allowlisted@example.com",
            is_admin=False,
        ), ["group-a"]

    async def _fake_fetch(db, **kwargs):
        captured["db"] = db
        captured.update(kwargs)
        return '{"count": 0, "traces": []}'

    monkeypatch.setattr(traces_module, "_load_user_context", _fake_user_context)
    monkeypatch.setattr(traces_module, "_fetch_traces", _fake_fetch)

    _defs, executor = traces_module.build_trace_tools(
        db=object(),
        user_id=str(uuid.uuid4()),
        agent_id=str(uuid.uuid4()),
        supervision_target_agent_ids=[target_agent_id],
    )

    result = await executor.execute_tool(
        ToolCall(
            id="3",
            name="inspect_agent_traces",
            arguments={"target_agent_id": target_agent_id, "period": "30d"},
        )
    )

    assert result.is_error is False
    assert '"traces"' in result.content
    assert captured["db"] is fake_db
    assert captured["agent_ids"] == [target_agent_id]
    assert captured["period"] == "30d"
    assert captured["include_spans"] is True


@pytest.mark.asyncio
async def test_inspect_agent_traces_allowed_for_admin_without_allowlist(monkeypatch):
    fake_db = _FakeDBSession()
    monkeypatch.setattr(
        traces_module,
        "async_session",
        _fake_async_session_factory(fake_db),
    )

    target_agent_id = str(uuid.uuid4())

    async def _fake_user_context(_db, _uid):
        return SimpleNamespace(
            id=uuid.uuid4(),
            email="test-admin@example.com",
            is_admin=True,
        ), []

    async def _fake_fetch(_db, **_kwargs):
        return '{"count": 1, "traces": []}'

    monkeypatch.setattr(traces_module, "_load_user_context", _fake_user_context)
    monkeypatch.setattr(traces_module, "_fetch_traces", _fake_fetch)

    _defs, executor = traces_module.build_trace_tools(
        db=object(),
        user_id=str(uuid.uuid4()),
        agent_id=str(uuid.uuid4()),
        supervision_target_agent_ids=[],
    )

    result = await executor.execute_tool(
        ToolCall(
            id="4",
            name="inspect_agent_traces",
            arguments={"target_agent_id": target_agent_id},
        )
    )

    assert result.is_error is False
    assert '"count"' in result.content


# --- get_trace_payload -----------------------------------------------------


class _FakeReadStore:
    """Stands in for the trace read store, returning rows the tool can read."""

    def __init__(self, trace, span=None):
        self._trace = trace
        self._span = span

    async def get_trace_detail(self, _db, **_kwargs):
        if self._trace is None:
            raise HTTPException(status_code=404, detail="Trace not found")
        return self._trace, None, None, None

    async def get_trace_span(self, _db, **_kwargs):
        if self._span is None:
            raise HTTPException(status_code=404, detail="Span not found for this trace")
        return self._span


def _install_payload_fixtures(monkeypatch, *, trace, span=None, is_admin=False):
    async def _no_run(_db, _value, *, own_agent_id):
        return None, None

    async def _no_supervised_run(_db, _value, **_kwargs):
        return []

    # No run id resolves here: a trace id is what these tests pass.
    monkeypatch.setattr(traces_module, "_own_trace_of_run", _no_run)
    monkeypatch.setattr(traces_module, "_supervised_run_root_traces", _no_supervised_run)
    monkeypatch.setattr(
        traces_module,
        "async_session",
        _fake_async_session_factory(_FakeDBSession()),
    )

    async def _fake_user_context(_db, _uid):
        return SimpleNamespace(
            id=uuid.uuid4(),
            email="payload@example.com",
            is_admin=is_admin,
        ), []

    monkeypatch.setattr(traces_module, "_load_user_context", _fake_user_context)
    monkeypatch.setattr(
        traces_module,
        "build_trace_read_store",
        lambda: _FakeReadStore(trace, span),
    )


def _fake_trace(agent_id, **overrides):
    base = dict(
        id=uuid.uuid4(),
        agent_id=uuid.UUID(agent_id) if agent_id else None,
        input=None,
        output=None,
        metadata_json=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


async def _call_payload_tool(executor, **arguments):
    return await executor.execute_tool(
        ToolCall(
            id="p1",
            name=traces_module.TRACE_PAYLOAD_TOOL_NAME,
            arguments=arguments,
        )
    )


@pytest.mark.asyncio
async def test_get_trace_payload_returns_the_untruncated_value(monkeypatch):
    """The whole point: what the preview cut off must come back verbatim."""
    agent_id = str(uuid.uuid4())
    full_text = "Nous recommandons d'ouvrir " + ("x" * 5000)
    trace = _fake_trace(agent_id)
    span = _fake_span(output={"content": full_text})
    _install_payload_fixtures(monkeypatch, trace=trace, span=span)

    _defs, executor = traces_module.build_trace_tools(
        db=object(), user_id=str(uuid.uuid4()), agent_id=agent_id
    )
    result = await _call_payload_tool(
        executor,
        trace_id=str(trace.id),
        span_id=str(span.id),
        field="output",
        max_chars=40_000,
    )

    assert result.is_error is False
    page = json.loads(result.content)
    assert page["next_offset"] is None
    assert full_text in page["content"]
    assert "[truncated" not in page["content"]


@pytest.mark.asyncio
async def test_get_trace_payload_pages_through_a_large_value(monkeypatch):
    agent_id = str(uuid.uuid4())
    trace = _fake_trace(agent_id, output="a" * 60_000)
    _install_payload_fixtures(monkeypatch, trace=trace)

    _defs, executor = traces_module.build_trace_tools(
        db=object(), user_id=str(uuid.uuid4()), agent_id=agent_id
    )

    first = json.loads(
        (
            await _call_payload_tool(
                executor, trace_id=str(trace.id), field="output", max_chars=20_000
            )
        ).content
    )
    assert first["total_chars"] == 60_000
    assert first["returned_chars"] == 20_000
    assert first["next_offset"] == 20_000

    # Walking next_offset must reach the end and cover every character.
    seen = first["returned_chars"]
    offset = first["next_offset"]
    while offset is not None:
        page = json.loads(
            (
                await _call_payload_tool(
                    executor,
                    trace_id=str(trace.id),
                    field="output",
                    offset=offset,
                    max_chars=20_000,
                )
            ).content
        )
        seen += page["returned_chars"]
        offset = page["next_offset"]
    assert seen == 60_000


@pytest.mark.asyncio
async def test_get_trace_payload_refuses_another_agents_trace(monkeypatch):
    trace = _fake_trace(str(uuid.uuid4()))  # owned by some other agent
    _install_payload_fixtures(monkeypatch, trace=trace)

    _defs, executor = traces_module.build_trace_tools(
        db=object(),
        user_id=str(uuid.uuid4()),
        agent_id=str(uuid.uuid4()),
        supervision_target_agent_ids=[],
    )
    result = await _call_payload_tool(executor, trace_id=str(trace.id), field="output")

    assert result.is_error is True
    assert "not allowed to read payloads" in result.content


@pytest.mark.asyncio
async def test_get_trace_payload_allows_an_allowlisted_agents_trace(monkeypatch):
    target_agent_id = str(uuid.uuid4())
    trace = _fake_trace(target_agent_id, output="supervised output")
    _install_payload_fixtures(monkeypatch, trace=trace)

    _defs, executor = traces_module.build_trace_tools(
        db=object(),
        user_id=str(uuid.uuid4()),
        agent_id=str(uuid.uuid4()),
        supervision_target_agent_ids=[target_agent_id],
    )
    result = await _call_payload_tool(executor, trace_id=str(trace.id), field="output")

    assert result.is_error is False
    assert "supervised output" in json.loads(result.content)["content"]


@pytest.mark.asyncio
async def test_get_trace_payload_reports_an_invisible_trace_as_not_found(monkeypatch):
    _install_payload_fixtures(monkeypatch, trace=None)

    _defs, executor = traces_module.build_trace_tools(
        db=object(), user_id=str(uuid.uuid4()), agent_id=str(uuid.uuid4())
    )
    result = await _call_payload_tool(executor, trace_id=str(uuid.uuid4()))

    assert result.is_error is True
    assert "not found, or not visible" in result.content


@pytest.mark.asyncio
async def test_get_trace_payload_rejects_an_unknown_field(monkeypatch):
    agent_id = str(uuid.uuid4())
    trace = _fake_trace(agent_id)
    _install_payload_fixtures(monkeypatch, trace=trace)

    _defs, executor = traces_module.build_trace_tools(
        db=object(), user_id=str(uuid.uuid4()), agent_id=agent_id
    )
    result = await _call_payload_tool(
        executor, trace_id=str(trace.id), field="system_prompt"
    )

    assert result.is_error is True
    assert "field must be one of" in result.content


@pytest.mark.asyncio
async def test_get_trace_payload_accepts_the_synthetic_root_span_id(monkeypatch):
    """The inspector shows the trace root as ``t-<trace_id>``; a copied id must work."""
    agent_id = str(uuid.uuid4())
    trace = _fake_trace(agent_id, output="trace level output")
    _install_payload_fixtures(monkeypatch, trace=trace)

    _defs, executor = traces_module.build_trace_tools(
        db=object(), user_id=str(uuid.uuid4()), agent_id=agent_id
    )
    result = await _call_payload_tool(
        executor, trace_id=str(trace.id), span_id=f"t-{trace.id}", field="output"
    )

    assert result.is_error is False
    page = json.loads(result.content)
    assert page["span_id"] is None
    assert "trace level output" in page["content"]


@pytest.mark.asyncio
async def test_get_trace_payload_reports_a_payload_that_was_never_stored(monkeypatch):
    """A failed offload must read as a gap, not as a complete payload."""
    agent_id = str(uuid.uuid4())
    trace = _fake_trace(
        agent_id,
        output={
            payload_offload.ENVELOPE_KEY: payload_offload.ENVELOPE_VERSION,
            "size_bytes": 9_000_000,
            "sha256": "0" * 64,
            "encoding": "json",
            "preview": "the only excerpt captured",
            "offload_failed": True,
            "error": "object storage unavailable",
        },
    )
    _install_payload_fixtures(monkeypatch, trace=trace)

    _defs, executor = traces_module.build_trace_tools(
        db=object(), user_id=str(uuid.uuid4()), agent_id=agent_id
    )
    result = await _call_payload_tool(executor, trace_id=str(trace.id), field="output")

    assert result.is_error is False
    page = json.loads(result.content)
    assert page["unavailable"] is True
    assert page["error"] == "object storage unavailable"
    assert "the only excerpt captured" in page["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("switches", "field", "noted"),
    [
        (TraceSwitches(storage_enabled=True, store_payloads=False), "output", True),
        (TraceSwitches(storage_enabled=True, store_payloads=False), "metadata", False),
        (TraceSwitches(storage_enabled=True, store_payloads=True), "output", False),
    ],
)
async def test_get_trace_payload_says_a_null_payload_was_not_stored_while_payloads_are_off(
    monkeypatch, switches, field, noted
):
    """Payloads off: a null input or output is a payload the store did
    not keep, not an empty call. The metadata is kept, so its null is genuine."""
    agent_id = str(uuid.uuid4())
    trace = _fake_trace(agent_id)
    _install_payload_fixtures(monkeypatch, trace=trace)
    publish_trace_switches(switches)

    _defs, executor = traces_module.build_trace_tools(
        db=object(), user_id=str(uuid.uuid4()), agent_id=agent_id
    )
    page = json.loads((await _call_payload_tool(executor, trace_id=str(trace.id), field=field)).content)

    assert page["content"] is None
    assert ("Store full payloads is off" in page.get("trace_store_note", "")) is noted


class _ListingStore:
    async def list_traces(self, **_kwargs):
        return [], 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("switches", "note"),
    [
        (TraceSwitches(storage_enabled=False, store_payloads=True), "Trace storage is off"),
        (TraceSwitches(storage_enabled=True, store_payloads=False), "Store full payloads is off"),
        (TraceSwitches(storage_enabled=True, store_payloads=True), None),
    ],
)
async def test_a_trace_listing_says_what_the_trace_switches_leave_out(monkeypatch, switches, note):
    """Storage off, a quiet listing is not a quiet agent; payloads off, a bare
    step is not an empty one. Said in the result, from this process's switches."""
    monkeypatch.setattr(traces_module, "build_trace_read_store", lambda: _ListingStore())
    publish_trace_switches(switches)

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
            include_spans=True,
        )
    )

    assert listing["count"] == 0
    if note is None:
        assert "trace_store_note" not in listing
    else:
        assert note in listing["trace_store_note"]


@pytest.mark.asyncio
async def test_get_trace_payload_requires_a_span_for_tool_arguments(monkeypatch):
    agent_id = str(uuid.uuid4())
    trace = _fake_trace(agent_id)
    _install_payload_fixtures(monkeypatch, trace=trace)

    _defs, executor = traces_module.build_trace_tools(
        db=object(), user_id=str(uuid.uuid4()), agent_id=agent_id
    )
    result = await _call_payload_tool(
        executor, trace_id=str(trace.id), field="tool_arguments"
    )

    assert result.is_error is True
    assert "pass the span_id" in result.content


@pytest.mark.asyncio
async def test_get_trace_payload_ignores_admin_when_only_self_inspection_granted(
    monkeypatch,
):
    """Ambient admin privilege must not widen a self-only inspection grant.

    `inspect_my_traces` is hard-scoped to this agent's own `agent_id` even for an
    admin. Honouring `user.is_admin` here regardless would let an agent granted
    only self-inspection read any trace payload in the instance — other users'
    conversations included — out of an admin's session, given a trace UUID from
    anywhere.
    """
    trace = _fake_trace(str(uuid.uuid4()), output="another agent's payload")
    _install_payload_fixtures(monkeypatch, trace=trace, is_admin=True)

    _defs, executor = traces_module.build_trace_tools(
        db=object(),
        user_id=str(uuid.uuid4()),
        agent_id=str(uuid.uuid4()),
        allow_cross_agent=False,
    )
    result = await _call_payload_tool(executor, trace_id=str(trace.id), field="output")

    assert result.is_error is True
    assert "not allowed to read payloads" in result.content
    assert "another agent's payload" not in result.content


@pytest.mark.asyncio
async def test_get_trace_payload_honours_admin_when_supervision_granted(monkeypatch):
    trace = _fake_trace(str(uuid.uuid4()), output="supervised payload")
    _install_payload_fixtures(monkeypatch, trace=trace, is_admin=True)

    _defs, executor = traces_module.build_trace_tools(
        db=object(),
        user_id=str(uuid.uuid4()),
        agent_id=str(uuid.uuid4()),
        allow_cross_agent=True,
    )
    result = await _call_payload_tool(executor, trace_id=str(trace.id), field="output")

    assert result.is_error is False
    assert "supervised payload" in json.loads(result.content)["content"]


def _capture_audit(monkeypatch) -> list[dict]:
    """Record audit writes instead of performing them."""
    audited: list[dict] = []

    async def _fake_log_audit(_db, **kwargs):
        audited.append(kwargs)

    import app.services.audit as audit_module

    monkeypatch.setattr(audit_module, "log_audit", _fake_log_audit)
    return audited


@pytest.mark.asyncio
async def test_get_trace_payload_treats_a_null_offset_as_the_start(monkeypatch):
    """A model sending `offset: null` means "from the beginning", not a crash."""
    agent_id = str(uuid.uuid4())
    trace = _fake_trace(agent_id, output="x" * 100)
    _install_payload_fixtures(monkeypatch, trace=trace)

    _defs, executor = traces_module.build_trace_tools(
        db=object(), user_id=str(uuid.uuid4()), agent_id=agent_id
    )
    result = await _call_payload_tool(
        executor, trace_id=str(trace.id), field="output", offset=None, max_chars=None
    )

    assert result.is_error is False
    page = json.loads(result.content)
    assert page["offset"] == 0
    # A string payload is returned as itself, not as a JSON-quoted string.
    assert page["returned_chars"] == 100
    assert page["next_offset"] is None


@pytest.mark.asyncio
async def test_get_trace_payload_rejects_a_bad_offset_before_any_side_effect(monkeypatch):
    """A bad argument must not cost an audit row for a read that never happened.

    The coercion used to run at the slice, after the ACL check, the span fetch
    and — for a cross-agent read — an audit write and its commit.
    """
    target_agent_id = str(uuid.uuid4())
    trace = _fake_trace(target_agent_id, output="x" * 100)
    _install_payload_fixtures(monkeypatch, trace=trace)
    audited = _capture_audit(monkeypatch)

    _defs, executor = traces_module.build_trace_tools(
        db=object(),
        user_id=str(uuid.uuid4()),
        agent_id=str(uuid.uuid4()),
        # A cross-agent read, so a successful call *would* audit.
        supervision_target_agent_ids=[target_agent_id],
    )
    result = await _call_payload_tool(
        executor, trace_id=str(trace.id), field="output", offset="halfway"
    )

    assert result.is_error is True
    assert "offset and max_chars must be integers" in result.content
    assert audited == []


@pytest.mark.asyncio
async def test_a_previews_truncation_count_is_an_offset_into_the_paged_text(monkeypatch):
    """The two renderings must agree, or the count the agent got means nothing.

    Pages were pretty-printed while previews were compact, so resuming at the
    preview's length landed in the wrong place.
    """
    agent_id = str(uuid.uuid4())
    payload = {"content": "z" * 6000}
    span = _fake_span(output=payload)
    trace = _fake_trace(agent_id)
    _install_payload_fixtures(monkeypatch, trace=trace, span=span)

    preview, truncated = traces_module._preview(payload, traces_module._PreviewBudget())
    assert truncated is True
    shown = preview.split("… [truncated")[0]

    _defs, executor = traces_module.build_trace_tools(
        db=object(), user_id=str(uuid.uuid4()), agent_id=agent_id
    )
    page = json.loads(
        (
            await _call_payload_tool(
                executor,
                trace_id=str(trace.id),
                span_id=str(span.id),
                field="output",
                offset=len(shown),
            )
        ).content
    )
    # Resuming at the preview's length continues exactly where it stopped.
    full = json.dumps(payload, ensure_ascii=False)
    assert page["content"] == full[len(shown) : len(shown) + page["returned_chars"]]
    assert page["total_chars"] == len(full)


@pytest.mark.asyncio
async def test_paging_an_offloaded_payload_downloads_it_once(monkeypatch):
    """`resolve_payload` downloads the whole object; paging must not repeat it."""
    agent_id = str(uuid.uuid4())
    trace = _fake_trace(
        agent_id,
        output={
            payload_offload.ENVELOPE_KEY: payload_offload.ENVELOPE_VERSION,
            "size_bytes": 2_000_000,
            "sha256": "a" * 64,
            "encoding": "json",
            "preview": "head",
            "object_name": "traces/x/trace/output.json",
        },
    )
    _install_payload_fixtures(monkeypatch, trace=trace)
    traces_module._resolved_payload_cache.clear()

    downloads = {"count": 0}

    async def _fake_resolve(_raw):
        downloads["count"] += 1
        return "b" * 50_000

    monkeypatch.setattr(traces_module, "resolve_payload", _fake_resolve)

    _defs, executor = traces_module.build_trace_tools(
        db=object(), user_id=str(uuid.uuid4()), agent_id=agent_id
    )

    offset = 0
    pages = 0
    while offset is not None:
        page = json.loads(
            (
                await _call_payload_tool(
                    executor,
                    trace_id=str(trace.id),
                    field="output",
                    offset=offset,
                    max_chars=10_000,
                )
            ).content
        )
        pages += 1
        offset = page["next_offset"]

    assert pages == 5
    assert downloads["count"] == 1
    traces_module._resolved_payload_cache.clear()


@pytest.mark.asyncio
async def test_get_trace_payload_requires_an_authenticated_user():
    _defs, executor = traces_module.build_trace_tools(
        db=object(), user_id="anonymous", agent_id=str(uuid.uuid4())
    )
    result = await _call_payload_tool(executor, trace_id=str(uuid.uuid4()))

    assert result.is_error is True
    assert "requires an authenticated user context" in result.content


# --- cross-agent supervision (no target) -----------------------------------


def test_supervision_schema_makes_the_target_optional():
    """Omitting the target is the cross-agent listing, so it cannot be required."""
    defs, _exec = traces_module.build_trace_tools(
        db=object(),
        user_id=str(uuid.uuid4()),
        agent_id=str(uuid.uuid4()),
    )
    schema = next(d for d in defs if d.name == "inspect_agent_traces").input_schema
    assert "required" not in schema
    assert "target_agent_id" in schema["properties"]


@pytest.mark.asyncio
async def test_inspect_agent_traces_without_target_covers_every_agent_for_an_admin(
    monkeypatch,
):
    fake_db = _FakeDBSession()
    monkeypatch.setattr(
        traces_module, "async_session", _fake_async_session_factory(fake_db)
    )
    captured: dict = {}

    async def _fake_user_context(_db, _uid):
        return SimpleNamespace(
            id=uuid.uuid4(), email="admin@example.com", is_admin=True
        ), []

    async def _fake_fetch(_db, **kwargs):
        captured.update(kwargs)
        return '{"count": 0, "traces": []}'

    monkeypatch.setattr(traces_module, "_load_user_context", _fake_user_context)
    monkeypatch.setattr(traces_module, "_fetch_traces", _fake_fetch)

    _defs, executor = traces_module.build_trace_tools(
        db=object(), user_id=str(uuid.uuid4()), agent_id=str(uuid.uuid4())
    )

    result = await executor.execute_tool(
        ToolCall(id="10", name="inspect_agent_traces", arguments={})
    )

    assert result.is_error is False
    # ``None`` is "no agent filter" — every trace the caller can see, which for
    # an admin is the instance. A list would silently cap the listing instead.
    assert captured["agent_ids"] is None


@pytest.mark.asyncio
async def test_inspect_agent_traces_without_target_stays_inside_the_allowlist(
    monkeypatch,
):
    fake_db = _FakeDBSession()
    monkeypatch.setattr(
        traces_module, "async_session", _fake_async_session_factory(fake_db)
    )
    first, second = sorted([str(uuid.uuid4()), str(uuid.uuid4())])
    captured: dict = {}

    async def _fake_user_context(_db, _uid):
        return SimpleNamespace(
            id=uuid.uuid4(), email="supervisor@example.com", is_admin=False
        ), []

    async def _fake_fetch(_db, **kwargs):
        captured.update(kwargs)
        return '{"count": 0, "traces": []}'

    monkeypatch.setattr(traces_module, "_load_user_context", _fake_user_context)
    monkeypatch.setattr(traces_module, "_fetch_traces", _fake_fetch)

    _defs, executor = traces_module.build_trace_tools(
        db=object(),
        user_id=str(uuid.uuid4()),
        agent_id=str(uuid.uuid4()),
        supervision_target_agent_ids=[second, first],
    )

    result = await executor.execute_tool(
        ToolCall(id="11", name="inspect_agent_traces", arguments={})
    )

    assert result.is_error is False
    # Widening to "everything visible" would hand a non-admin traces its
    # allow-list never named.
    assert captured["agent_ids"] == [first, second]


@pytest.mark.asyncio
async def test_inspect_agent_traces_without_target_refused_without_a_grant(
    monkeypatch,
):
    fake_db = _FakeDBSession()
    monkeypatch.setattr(
        traces_module, "async_session", _fake_async_session_factory(fake_db)
    )

    async def _fake_user_context(_db, _uid):
        return SimpleNamespace(
            id=uuid.uuid4(), email="plain@example.com", is_admin=False
        ), []

    async def _fake_fetch(_db, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("traces were listed without a supervision grant")

    monkeypatch.setattr(traces_module, "_load_user_context", _fake_user_context)
    monkeypatch.setattr(traces_module, "_fetch_traces", _fake_fetch)

    _defs, executor = traces_module.build_trace_tools(
        db=object(),
        user_id=str(uuid.uuid4()),
        agent_id=str(uuid.uuid4()),
        supervision_target_agent_ids=[],
    )

    result = await executor.execute_tool(
        ToolCall(id="12", name="inspect_agent_traces", arguments={})
    )

    assert "admin privileges" in result.content


@pytest.mark.asyncio
async def test_inspect_my_traces_refuses_without_a_calling_agent():
    """``/mcp/xtm`` builds these tools with no agent behind the call."""
    _defs, executor = traces_module.build_trace_tools(
        db=object(), user_id=str(uuid.uuid4()), agent_id=None
    )

    result = await executor.execute_tool(
        ToolCall(id="13", name="inspect_my_traces", arguments={})
    )

    assert "only available inside an agent run" in result.content


def test_a_supervision_refusal_cannot_be_mistaken_for_a_wide_scope():
    """The denial is an exception, not a value.

    ``None`` legitimately means "no agent filter — every trace the caller can
    see". When the refusal was also a return value, a call site that forgot to
    check it would have widened to exactly that.
    """
    with pytest.raises(traces_module.SupervisionDeniedError):
        traces_module._supervision_scope(
            admin_bypass=False, target_uuid=None, supervision_allowlist=set()
        )
    with pytest.raises(traces_module.SupervisionDeniedError):
        traces_module._supervision_scope(
            admin_bypass=False, target_uuid=str(uuid.uuid4()), supervision_allowlist=set()
        )
    assert (
        traces_module._supervision_scope(
            admin_bypass=True, target_uuid=None, supervision_allowlist=set()
        )
        is None
    )


# --- the inspect_traces audit trail ---------------------------------


class _CommitLog(_FakeDBSession):
    def __init__(self, events: list[str], *, fail: bool = False):
        self.events = events
        self.fail = fail

    async def commit(self):
        if self.fail:
            raise RuntimeError("commit failed")
        self.events.append("commit")


def _supervisor(monkeypatch, *, db, trace):
    """One supervising reader (the same user on every call) of *trace*'s agent."""
    reader = SimpleNamespace(id=uuid.uuid4(), email="supervisor@example.com", is_admin=False)

    async def _user_context(_db, _uid):
        return reader, []

    async def _fetch(_db, **_kwargs):
        return '{"count": 0, "traces": []}'

    monkeypatch.setattr(traces_module, "async_session", _fake_async_session_factory(db))
    monkeypatch.setattr(traces_module, "_load_user_context", _user_context)
    monkeypatch.setattr(traces_module, "_fetch_traces", _fetch)
    monkeypatch.setattr(traces_module, "build_trace_read_store", lambda: _FakeReadStore(trace))
    _defs, executor = traces_module.build_trace_tools(
        db=object(),
        user_id=str(reader.id),
        agent_id=str(uuid.uuid4()),
        supervision_target_agent_ids=[str(trace.agent_id)],
    )
    return executor


@pytest.mark.asyncio
async def test_paging_one_payload_of_another_agent_is_audited_once_per_window(monkeypatch, marker_redis):
    trace = _fake_trace(str(uuid.uuid4()), output="p" * 50_000, input="i" * 10)
    executor = _supervisor(monkeypatch, db=_FakeDBSession(), trace=trace)
    audited = _capture_audit(monkeypatch)

    for offset in (0, 20_000, 40_000):
        result = await _call_payload_tool(executor, trace_id=str(trace.id), field="output", offset=offset)
        assert result.is_error is False

    assert len(audited) == 1
    assert audited[0]["action"] == "inspect_traces"
    assert audited[0]["details"]["field"] == "output"
    assert audited[0]["details"]["window_seconds"] == traces_module.TRACE_READ_AUDIT_WINDOW_SECONDS
    assert list(marker_redis.keys.values()) == [traces_module.TRACE_READ_AUDIT_WINDOW_SECONDS]

    # Another field of the same trace is another read, and has its own row.
    await _call_payload_tool(executor, trace_id=str(trace.id), field="input")
    assert [row["details"]["field"] for row in audited] == ["output", "input"]


@pytest.mark.asyncio
async def test_listing_the_same_agents_again_is_audited_once_per_window(monkeypatch):
    trace = _fake_trace(str(uuid.uuid4()))
    executor = _supervisor(monkeypatch, db=_FakeDBSession(), trace=trace)
    audited = _capture_audit(monkeypatch)
    target = str(trace.agent_id)

    for query in ("timeout", "error", ""):
        result = await executor.execute_tool(
            ToolCall(id="l", name="inspect_agent_traces", arguments={"target_agent_id": target, "query": query})
        )
        assert result.is_error is False

    assert len(audited) == 1
    assert audited[0]["entity_id"] == target
    assert audited[0]["details"]["target_agent_ids"] == [target]


@pytest.mark.asyncio
async def test_the_read_marker_is_written_only_after_the_row_is_committed(monkeypatch, marker_redis):
    events: list[str] = []
    marker_redis.events = events
    trace = _fake_trace(str(uuid.uuid4()), output="payload")
    db = _CommitLog(events, fail=True)
    executor = _supervisor(monkeypatch, db=db, trace=trace)
    audited = _capture_audit(monkeypatch)

    failed = await _call_payload_tool(executor, trace_id=str(trace.id), field="output")
    assert failed.is_error is True
    assert "failed to read the trace payload" in failed.content
    assert marker_redis.keys == {}

    db.fail = False
    served = await _call_payload_tool(executor, trace_id=str(trace.id), field="output")
    assert served.is_error is False
    assert len(audited) == 2
    assert events == ["commit", "mark"]


@pytest.mark.asyncio
async def test_an_unreachable_marker_store_audits_every_read(monkeypatch, marker_redis):
    marker_redis.fail = True
    trace = _fake_trace(str(uuid.uuid4()), output="payload")
    executor = _supervisor(monkeypatch, db=_FakeDBSession(), trace=trace)
    audited = _capture_audit(monkeypatch)

    for _ in range(2):
        result = await _call_payload_tool(executor, trace_id=str(trace.id), field="output")
        assert result.is_error is False

    assert len(audited) == 2
    assert marker_redis.keys == {}


def test_the_read_marker_names_the_reader_and_the_whole_row():
    reader = uuid.uuid4()
    details = {"calling_agent_id": "a", "trace_id": "t", "span_id": None, "field": "output"}
    key = traces_module.trace_read_audit_key(reader, "agent", details)

    assert key.startswith(f"trace:inspect:audit:{reader}:")
    assert key == traces_module.trace_read_audit_key(reader, "agent", dict(reversed(details.items())))
    assert key != traces_module.trace_read_audit_key(uuid.uuid4(), "agent", details)
    assert key != traces_module.trace_read_audit_key(reader, "agent", {**details, "field": "input"})
    assert key != traces_module.trace_read_audit_key(reader, "other", details)


# --- The calling run first -------------------------------------------


def _stored_trace(agent_id: str, **overrides):
    from datetime import UTC, datetime

    base = dict(
        id=uuid.uuid4(),
        agent_id=uuid.UUID(agent_id),
        source="assignment",
        name="agent-chat:me",
        model="m",
        status="ok",
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
        duration_ms=10.0,
        input_tokens=1,
        output_tokens=1,
        total_tokens=2,
        total_cost=None,
        error_message=None,
        input=None,
        output=None,
        metadata_json={"rollup": {"iterations": 4, "wall_ms": 100.0, "tool_calls": 2}},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _RowsStore:
    def __init__(self, rows):
        self.rows = rows
        self.limits: list[int] = []

    async def list_traces(self, **kwargs):
        self.limits.append(kwargs["limit"])
        return [(row,) for row in self.rows[: kwargs["limit"]]], len(self.rows)

    async def get_trace_spans(self, **_kwargs):
        return []


def _live_root(agent_id: str, **overrides):
    from datetime import UTC, datetime, timedelta

    base = {
        "local_trace_id": uuid.uuid4(),
        "local_agent_id": uuid.UUID(agent_id),
        "local_source": "assignment",
        "local_name": "agent-chat:me",
        "local_model": "m",
        "local_started_at": datetime.now(UTC) - timedelta(seconds=30),
        "local_spans": [
            {"type": "ITERATION"},
            {"type": "GENERATION", "duration_ms": 900.0, "input_tokens": 1200, "output_tokens": 40},
            {"type": "TOOL", "name": "sandbox_grep", "duration_ms": 50.0, "output": "x" * 500},
        ],
    }
    base.update(overrides)
    return base


async def _list(monkeypatch, store, *, limit, own_run=None, include_spans=False, agent_id=None):
    monkeypatch.setattr(traces_module, "build_trace_read_store", lambda: store)

    async def _none_gone(_db, _ids):
        return set()

    monkeypatch.setattr(traces_module, "_agents_gone", _none_gone)
    extra = {"own_run": own_run} if own_run is not None else {}
    return json.loads(
        await traces_module._fetch_traces(
            object(),
            user=SimpleNamespace(id=uuid.uuid4(), is_admin=True),
            user_group_ids=[],
            query="",
            period="24h",
            status_filter="",
            source="",
            model="",
            limit=limit,
            agent_ids=[agent_id] if agent_id else None,
            include_spans=include_spans,
            **extra,
        )
    )


@pytest.mark.asyncio
async def test_the_calling_run_comes_first_with_its_rollup_so_far(monkeypatch):
    me = str(uuid.uuid4())
    earlier = [_stored_trace(me), _stored_trace(me)]
    root = _live_root(me)

    listing = await _list(monkeypatch, _RowsStore(earlier), limit=3, own_run=root, agent_id=me)

    first = listing["traces"][0]
    assert first["id"] == str(root["local_trace_id"])
    assert first["in_progress"] is True
    assert first["status"] == "in_progress"
    assert first["rollup"]["iterations"] == 1
    assert first["rollup"]["tool_calls"] == 1
    assert first["rollup"]["input_tokens"] == 1200
    assert first["rollup"]["wall_ms"] >= 30_000
    # The per-tool table stays for the span-inclusive mode.
    assert "tools" not in first["rollup"]
    assert [t["id"] for t in listing["traces"][1:]] == [str(t.id) for t in earlier]
    assert listing["count"] == 3


@pytest.mark.asyncio
async def test_limit_one_answers_the_calling_run_and_no_sibling(monkeypatch):
    me = str(uuid.uuid4())
    sibling = _stored_trace(me)
    root = _live_root(me)

    listing = await _list(monkeypatch, _RowsStore([sibling]), limit=1, own_run=root, agent_id=me)

    assert [t["id"] for t in listing["traces"]] == [str(root["local_trace_id"])]
    # A partial run never weighs in the percentiles of the runs listed.
    assert "rollup_percentiles" not in listing


@pytest.mark.asyncio
async def test_the_calling_run_is_never_listed_twice_once_stored(monkeypatch):
    me = str(uuid.uuid4())
    root = _live_root(me)
    stored_self = _stored_trace(me, id=root["local_trace_id"])
    others = [_stored_trace(me), _stored_trace(me)]
    store = _RowsStore([stored_self, *others])

    listing = await _list(monkeypatch, store, limit=3, own_run=root, agent_id=me)

    ids = [t["id"] for t in listing["traces"]]
    assert ids == [str(root["local_trace_id"]), *(str(t.id) for t in others)]
    # One row more than shown, so the stored copy of this run never shortens the page.
    assert store.limits == [3]


class _SegmentsStore(_RowsStore):
    """Answers the listing with *rows* and a run-id read with *segments*."""

    def __init__(self, rows, segments):
        super().__init__(rows)
        self.segments = segments
        self.run_ids: list = []

    async def list_traces(self, **kwargs):
        if kwargs.get("assignment_run_id") is not None:
            self.run_ids.append(kwargs["assignment_run_id"])
            return [(row,) for row in self.segments], len(self.segments)
        return await super().list_traces(**kwargs)


@pytest.mark.asyncio
async def test_a_resumed_run_sums_every_segment_and_counts_its_resumes(monkeypatch):
    from datetime import UTC, datetime, timedelta

    me = str(uuid.uuid4())
    run_id = uuid.uuid4()
    root = _live_root(me, local_assignment_run_id=run_id)
    now = datetime.now(UTC)
    segments = [
        _stored_trace(
            me,
            started_at=now - timedelta(minutes=20 - i),
            metadata_json={
                "rollup": {
                    "iterations": 10,
                    "generations": 10,
                    "tool_calls": 12,
                    "input_tokens": 50_000,
                    "output_tokens": 2_000,
                    "wall_ms": 60_000.0,
                    "tools": {"sandbox_read_file": {"calls": 12, "errors": 0, "result_chars": 9_000, "ms": 5.0}},
                    "largest_tool_results": [
                        {"span_id": f"s{i}", "tool": "sandbox_read_file", "result_chars": 4_000 + i}
                    ],
                }
            },
        )
        for i in range(4)
    ]
    # The stored copy of this run (once closed) is never counted as an earlier segment.
    segments.append(_stored_trace(me, id=root["local_trace_id"]))
    store = _SegmentsStore([], segments)

    listing = await _list(monkeypatch, store, limit=1, own_run=root, agent_id=me, include_spans=True)

    rollup = listing["traces"][0]["rollup"]
    assert store.run_ids == [run_id]
    assert rollup["resumes"] == 4
    assert rollup["segments"] == 5
    assert rollup["iterations"] == 41
    assert rollup["tool_calls"] == 49
    assert rollup["input_tokens"] == 201_200
    assert rollup["output_tokens"] == 8_040
    assert rollup["wall_ms"] >= 270_000
    assert rollup["tools"]["sandbox_read_file"]["calls"] == 48
    assert rollup["tools"]["sandbox_grep"]["calls"] == 1
    assert rollup["largest_tool_results"][0]["span_id"] == "s3"
    assert "resumed 4 time(s)" in listing["traces"][0]["note"]


@pytest.mark.asyncio
async def test_a_run_never_resumed_reads_no_segment_and_keeps_its_figures(monkeypatch):
    me = str(uuid.uuid4())
    root = _live_root(me, local_assignment_run_id=uuid.uuid4())
    store = _SegmentsStore([], [])

    listing = await _list(monkeypatch, store, limit=1, own_run=root, agent_id=me)

    rollup = listing["traces"][0]["rollup"]
    assert rollup["iterations"] == 1
    assert rollup["input_tokens"] == 1200
    assert "resumes" not in rollup
    assert "summed over" not in listing["traces"][0]["note"]


@pytest.mark.asyncio
async def test_a_failed_segment_read_keeps_the_live_figures(monkeypatch):
    me = str(uuid.uuid4())
    root = _live_root(me, local_assignment_run_id=uuid.uuid4())

    class _FailingSegments(_RowsStore):
        async def list_traces(self, **kwargs):
            if kwargs.get("assignment_run_id") is not None:
                raise RuntimeError("store down")
            return await super().list_traces(**kwargs)

    listing = await _list(monkeypatch, _FailingSegments([]), limit=1, own_run=root, agent_id=me)

    assert listing["traces"][0]["rollup"]["iterations"] == 1


def _recorded(monkeypatch, count):
    async def _resumes(_db, _root):
        return count

    monkeypatch.setattr(traces_module, "_recorded_resumes", _resumes)


@pytest.mark.asyncio
async def test_a_segment_lost_with_its_worker_still_counts_as_a_resume(monkeypatch):
    me = str(uuid.uuid4())
    root = _live_root(me, local_assignment_run_id=uuid.uuid4())
    # Two resumes on the run's record (a take-back, then a hand-back): only the hand-back stored its trace.
    store = _SegmentsStore([], [_stored_trace(me)])
    _recorded(monkeypatch, 2)

    listing = await _list(monkeypatch, store, limit=1, own_run=root, agent_id=me)

    rollup = listing["traces"][0]["rollup"]
    assert rollup["resumes"] == 2
    assert rollup["segments"] == 3
    assert rollup["untraced_segments"] == 1
    # The figures are those of the segments that stored a trace: the live one plus the stored one.
    assert rollup["iterations"] == 5
    note = listing["traces"][0]["note"]
    assert "summed over its 2 segments with a stored trace (resumed 2 time(s))" in note
    assert "1 earlier segment(s) were lost with their worker" in note


@pytest.mark.asyncio
async def test_a_run_whose_every_earlier_segment_left_no_trace_reports_its_resumes(monkeypatch):
    me = str(uuid.uuid4())
    root = _live_root(me, local_assignment_run_id=uuid.uuid4())
    _recorded(monkeypatch, 1)

    listing = await _list(monkeypatch, _SegmentsStore([], []), limit=1, own_run=root, agent_id=me)

    rollup = listing["traces"][0]["rollup"]
    assert (rollup["resumes"], rollup["segments"], rollup["untraced_segments"]) == (1, 2, 1)
    assert rollup["iterations"] == 1


@pytest.mark.asyncio
async def test_a_record_agreeing_with_the_stored_traces_adds_no_untraced_segment(monkeypatch):
    me = str(uuid.uuid4())
    root = _live_root(me, local_assignment_run_id=uuid.uuid4())
    _recorded(monkeypatch, 1)

    listing = await _list(monkeypatch, _SegmentsStore([], [_stored_trace(me)]), limit=1, own_run=root, agent_id=me)

    rollup = listing["traces"][0]["rollup"]
    assert (rollup["resumes"], rollup["segments"]) == (1, 2)
    assert "untraced_segments" not in rollup
    assert "lost with their worker" not in listing["traces"][0]["note"]


@pytest.mark.asyncio
async def test_the_recorded_resumes_are_read_from_the_run_trigger_context():
    run_id = uuid.uuid4()

    class _Result:
        def scalar_one_or_none(self):
            return {"resumes": [{"cause": "taken back"}, {"cause": "handed back"}, "not an entry"]}

    class _Db:
        def __init__(self):
            self.statements: list = []

        async def execute(self, statement):
            self.statements.append(statement)
            return _Result()

    db = _Db()
    assert await traces_module._recorded_resumes(db, {"local_assignment_run_id": str(run_id)}) == 2
    assert len(db.statements) == 1
    # No run (a chat turn) and an unreadable record answer None: the stored traces count alone.
    assert await traces_module._recorded_resumes(db, {}) is None
    assert await traces_module._recorded_resumes(object(), {"local_assignment_run_id": run_id}) is None


@pytest.mark.asyncio
async def test_without_a_calling_run_the_listing_is_unchanged(monkeypatch):
    me = str(uuid.uuid4())
    rows = [_stored_trace(me), _stored_trace(me)]
    store = _RowsStore(rows)

    listing = await _list(monkeypatch, store, limit=2, agent_id=me)

    assert [t["id"] for t in listing["traces"]] == [str(t.id) for t in rows]
    assert store.limits == [2]
    assert all("in_progress" not in t for t in listing["traces"])
    assert "rollup_percentiles" in listing


def test_the_calling_run_is_read_from_the_current_trace_context(monkeypatch):
    import app.services.langfuse_tracing as lft

    me = str(uuid.uuid4())
    root = _live_root(me)
    child = {"trace": None, "parent_ctx": root}
    token = lft.set_current_trace_ctx(child)
    try:
        assert traces_module._own_live_run(me) is root
        # A context of another agent (a delegating run's) is not this run.
        assert traces_module._own_live_run(str(uuid.uuid4())) is None
        assert traces_module._own_live_run(None) is None
    finally:
        lft.reset_current_trace_ctx(token)
    assert traces_module._own_live_run(me) is None


@pytest.mark.asyncio
async def test_inspect_my_traces_passes_the_calling_run(monkeypatch):
    import app.services.langfuse_tracing as lft

    me = str(uuid.uuid4())
    root = _live_root(me)
    captured: dict = {}

    async def _user_context(_db, _uid):
        return SimpleNamespace(id=uuid.uuid4(), email="me@example.com", is_admin=False), []

    async def _fetch(_db, **kwargs):
        captured.update(kwargs)
        return '{"count": 0, "traces": []}'

    monkeypatch.setattr(traces_module, "async_session", _fake_async_session_factory(_FakeDBSession()))
    monkeypatch.setattr(traces_module, "_load_user_context", _user_context)
    monkeypatch.setattr(traces_module, "_fetch_traces", _fetch)
    _definitions, executor = traces_module.build_trace_tools(object(), str(uuid.uuid4()), me)

    token = lft.set_current_trace_ctx(root)
    try:
        result = await executor.execute_tool(ToolCall(id="c1", name="inspect_my_traces", arguments={"limit": 1}))
    finally:
        lft.reset_current_trace_ctx(token)

    assert result.is_error is False
    assert captured["own_run"] is root
    assert captured["agent_ids"] == [me]


@pytest.mark.asyncio
async def test_the_calling_run_rollup_carries_its_frictions(monkeypatch):
    me = str(uuid.uuid4())
    run_id = uuid.uuid4()
    root = _live_root(me, local_assignment_run_id=run_id)
    _recorded(monkeypatch, 0)
    asked: list = []
    summary = {"recorded": 2, "undisposed": 1, "by_kind": {"tool_error": 2}}

    async def _frictions(_db, run):
        asked.append(run)
        return summary

    import app.services.frictions as frictions_module

    monkeypatch.setattr(frictions_module, "run_friction_summary", _frictions)

    listing = await _list(monkeypatch, _SegmentsStore([], []), limit=1, own_run=root, agent_id=me)

    assert asked == [run_id]
    assert listing["traces"][0]["rollup"]["frictions"] == summary


@pytest.mark.asyncio
async def test_a_chat_turn_or_a_run_without_frictions_has_no_frictions_key(monkeypatch):
    me = str(uuid.uuid4())
    listing = await _list(monkeypatch, _RowsStore([]), limit=1, own_run=_live_root(me), agent_id=me)
    assert "frictions" not in listing["traces"][0]["rollup"]


class _SpansStore(_RowsStore):
    """Answers the listing with *rows* and every trace's spans with *spans*."""

    def __init__(self, rows, spans):
        super().__init__(rows)
        self.spans = spans

    async def get_trace_spans(self, **_kwargs):
        return list(self.spans)


async def _list_window(monkeypatch, store, **window):
    monkeypatch.setattr(traces_module, "build_trace_read_store", lambda: store)

    async def _none_gone(_db, _ids):
        return set()

    monkeypatch.setattr(traces_module, "_agents_gone", _none_gone)
    return await traces_module._fetch_traces(
        object(),
        user=SimpleNamespace(id=uuid.uuid4(), is_admin=True),
        user_group_ids=[],
        query="",
        period="24h",
        status_filter="",
        source="",
        model="",
        limit=1,
        agent_ids=None,
        include_spans=window.pop("include_spans", True),
        **window,
    )


def _ninety_tool_spans():
    return [
        _fake_span(name=f"tool_{i % 7}", output="r" * (500 + i), tool_arguments={"path": "p" * 300})
        for i in range(90)
    ]


@pytest.mark.asyncio
async def test_a_long_run_lists_the_span_cap_and_names_the_next_offset(monkeypatch):
    me = str(uuid.uuid4())
    store = _SpansStore([_stored_trace(me)], _ninety_tool_spans())

    raw = await _list_window(monkeypatch, store)
    trace = json.loads(raw)["traces"][0]

    # At most the span cap, fewer when the listing's size budget stops the page first.
    shown = len(trace["spans"])
    assert 0 < shown <= traces_module._DEFAULT_MAX_SPANS
    assert trace["spans_total"] == 90
    assert trace["spans_next_offset"] == shown
    assert f"span_offset={shown}" in trace["spans_note"]
    # The rollup counts every span, not the page shown.
    assert trace["rollup"]["tool_calls"] == 90
    assert len(raw) < 30_000


@pytest.mark.asyncio
async def test_span_offset_pages_to_the_last_spans(monkeypatch):
    me = str(uuid.uuid4())
    spans = _ninety_tool_spans()
    store = _SpansStore([_stored_trace(me)], spans)

    trace = json.loads(await _list_window(monkeypatch, store, max_spans=40, span_offset=80))["traces"][0]

    assert [s["id"] for s in trace["spans"]] == [str(s.id) for s in spans[80:]]
    assert trace["spans_offset"] == 80
    assert trace["spans_next_offset"] is None


@pytest.mark.asyncio
async def test_a_short_run_lists_every_span_without_a_note(monkeypatch):
    me = str(uuid.uuid4())
    store = _SpansStore([_stored_trace(me)], _ninety_tool_spans()[:5])

    trace = json.loads(await _list_window(monkeypatch, store))["traces"][0]

    assert len(trace["spans"]) == 5
    assert trace["spans_next_offset"] is None
    assert "spans_note" not in trace


@pytest.mark.asyncio
async def test_rollup_only_answers_the_whole_rollup_and_no_span(monkeypatch):
    me = str(uuid.uuid4())
    store = _SpansStore([_stored_trace(me)], _ninety_tool_spans())

    raw = await _list_window(monkeypatch, store, include_spans=False, rollup_only=True)
    listing = json.loads(raw)
    trace = listing["traces"][0]

    assert listing["rollup_only"] is True
    assert listing["include_spans"] is False
    assert "spans" not in trace
    assert "output_preview" not in trace
    rollup = trace["rollup"]
    for key in ("iterations", "tool_calls", "tool_errors", "input_tokens", "output_tokens", "largest_tool_results"):
        assert key in rollup
    assert rollup["tool_calls"] == 90
    assert sum(entry["calls"] for entry in rollup["tools"].values()) == 90
    assert len(raw) < 6_000


def _long_run_spans(count: int = 300):
    """A run shaped like the retest of #10603: iteration spans with a 2,200-character
    input, generation spans, and failed tool spans with a 7,000-character error."""
    spans = []
    for i in range(count):
        kind = ("ITERATION", "GENERATION", "TOOL")[i % 3]
        if kind == "TOOL":
            spans.append(
                _fake_span(
                    name=f"tool_{i % 40}",
                    status_message="E" * 7_000,
                    level="ERROR",
                    output="o" * 3_000,
                    tool_arguments={"path": "p" * 300},
                )
            )
        else:
            spans.append(
                _fake_span(
                    type=kind,
                    name=f"{kind.lower()} {i}",
                    input="t" * 2_200,
                    output="g" * 1_500,
                    tool_call_id=None,
                    tool_arguments=None,
                )
            )
    return spans


@pytest.mark.asyncio
async def test_a_long_run_listing_stays_under_30k_and_pages_every_span(monkeypatch):
    me = str(uuid.uuid4())
    spans = _long_run_spans()
    store = _SpansStore([_stored_trace(me)], spans)

    raw = await _list_window(monkeypatch, store)
    trace = json.loads(raw)["traces"][0]

    assert len(raw) < 30_000
    assert trace["spans_total"] == 300
    assert 0 < len(trace["spans"]) <= traces_module._DEFAULT_MAX_SPANS
    assert trace["spans_next_offset"] == len(trace["spans"])
    assert f"span_offset={trace['spans_next_offset']}" in trace["spans_note"]

    # Walking spans_next_offset reaches every span once, in order.
    seen = [s["id"] for s in trace["spans"]]
    offset = trace["spans_next_offset"]
    while offset is not None:
        page = json.loads(await _list_window(monkeypatch, store, span_offset=offset))
        page_trace = page["traces"][0]
        assert page_trace["spans_offset"] == offset
        seen.extend(s["id"] for s in page_trace["spans"])
        offset = page_trace["spans_next_offset"]
    assert seen == [str(s.id) for s in spans]


def test_a_long_status_message_is_cut_and_names_get_trace_payload():
    span = _fake_span(status_message="E" * 7_000, level="ERROR")

    message = traces_module._span_summary(span)["status_message"]

    assert len(message) < 700
    assert message.startswith("E" * traces_module._STATUS_MESSAGE_MAX_CHARS)
    assert "[truncated 6600 chars]" in message
    assert f"{traces_module.TRACE_PAYLOAD_TOOL_NAME}(trace_id={span.trace_id}, span_id={span.id}" in message


def test_a_short_status_message_is_kept_whole():
    assert traces_module._span_summary(_fake_span(status_message="refused"))["status_message"] == "refused"


@pytest.mark.asyncio
async def test_rollup_only_of_a_run_of_many_tools_stays_under_6k(monkeypatch):
    me = str(uuid.uuid4())
    spans = [
        _fake_span(name=f"a_rather_long_builtin_tool_name_{i}", output="r" * (100 + i)) for i in range(40)
    ]
    store = _SpansStore([_stored_trace(me)], spans)

    raw = await _list_window(monkeypatch, store, include_spans=False, rollup_only=True)
    rollup = json.loads(raw)["traces"][0]["rollup"]

    assert len(raw) < 6_000
    assert rollup["tool_calls"] == 40
    assert len(rollup["tools"]) == traces_module.MAX_TOOLS
    assert rollup["tools_omitted"] == 40 - traces_module.MAX_TOOLS


def test_span_window_defaults_and_bounds():
    assert traces_module._span_window(None, None) == (traces_module._DEFAULT_MAX_SPANS, 0)
    assert traces_module._span_window("x", "y") == (traces_module._DEFAULT_MAX_SPANS, 0)
    assert traces_module._span_window(0, -3) == (1, 0)
    assert traces_module._span_window(10_000, 7) == (traces_module._MAX_SPANS, 7)


def test_both_listing_tools_declare_the_span_window():
    defs, _ = traces_module.build_trace_tools(None, str(uuid.uuid4()), str(uuid.uuid4()), allow_cross_agent=True)
    for definition in defs:
        if definition.name in {"inspect_my_traces", "inspect_agent_traces"}:
            props = definition.input_schema["properties"]
            assert props["max_spans"]["default"] == traces_module._DEFAULT_MAX_SPANS
            assert props["span_offset"]["default"] == 0
            assert props["rollup_only"]["default"] is False
# --- Platform Health ---------------------------------------------------------


@pytest.fixture(params=["principal", "sandbox_turn"])
def as_concierge(monkeypatch, request):
    """The work acts as Platform Health (flag on), through a fleet agent an admin
    owns; or it is a sandbox copy's turn with no principal (an evaluation case
    of an admin-owned agent's copy, which keeps that agent's supervision list)."""
    from app.services.health.identity import HealthPrincipal, acting_as_health_principal
    from app.services.health.sandbox.session import SandboxSession, sandbox_session

    if request.param == "sandbox_turn":
        with sandbox_session(SandboxSession(mode="sandbox")):
            yield
        return
    with acting_as_health_principal(HealthPrincipal(agent_id=uuid.uuid4(), role="quality_analyst")):
        yield


@pytest.mark.asyncio
@pytest.mark.usefixtures("as_concierge")
async def test_the_concierge_never_lists_another_agents_traces_here(monkeypatch):
    """No consent rule applies here: neither the owner's admin bypass nor an
    allow-list opens another agent's traces to the Concierge."""
    target_agent_id = str(uuid.uuid4())
    monkeypatch.setattr(traces_module, "async_session", _fake_async_session_factory(_FakeDBSession()))

    async def _admin(_db, _uid):
        return SimpleNamespace(id=uuid.uuid4(), email="owner@example.com", is_admin=True), []

    async def _should_not_fetch(*_args, **_kwargs):
        raise AssertionError("another agent's traces were read")

    monkeypatch.setattr(traces_module, "_load_user_context", _admin)
    monkeypatch.setattr(traces_module, "_fetch_traces", _should_not_fetch)
    _defs, executor = traces_module.build_trace_tools(
        db=object(),
        user_id=str(uuid.uuid4()),
        agent_id=str(uuid.uuid4()),
        supervision_target_agent_ids=[target_agent_id],
        allow_cross_agent=True,
    )

    for arguments in ({"target_agent_id": target_agent_id}, {}):
        result = await executor.execute_tool(ToolCall(id="c1", name="inspect_agent_traces", arguments=arguments))
        assert result.content == traces_module.HEALTH_TRACES_REFUSED


@pytest.mark.asyncio
@pytest.mark.usefixtures("as_concierge")
async def test_the_concierge_reads_no_other_agents_payload_but_its_own(monkeypatch):
    me, target = str(uuid.uuid4()), str(uuid.uuid4())
    for owner, expected in ((target, None), (me, "own output")):
        trace = _fake_trace(owner, output="own output" if owner == me else "their conversation")
        _install_payload_fixtures(monkeypatch, trace=trace, is_admin=True)
        _defs, executor = traces_module.build_trace_tools(
            db=object(),
            user_id=str(uuid.uuid4()),
            agent_id=me,
            supervision_target_agent_ids=[target],
            allow_cross_agent=True,
        )
        result = await _call_payload_tool(executor, trace_id=str(trace.id), field="output")
        if expected is None:
            assert result.content == traces_module.HEALTH_TRACES_REFUSED
        else:
            assert expected in json.loads(result.content)["content"]


# --- Time window, rollup filters, paging, conversation id (#9272) -----


class _WindowStore(_RowsStore):
    """Pages ``rows`` by offset and records every list call's window."""

    def __init__(self, rows):
        super().__init__(rows)
        self.calls: list[dict] = []

    async def list_traces(self, **kwargs):
        self.calls.append(kwargs)
        first = kwargs.get("offset", 0)
        return [(row,) for row in self.rows[first : first + kwargs["limit"]]], len(self.rows)


async def _window_list(monkeypatch, store, **kwargs):
    monkeypatch.setattr(traces_module, "build_trace_read_store", lambda: store)

    async def _none_gone(_db, _ids):
        return set()

    monkeypatch.setattr(traces_module, "_agents_gone", _none_gone)
    params = dict(query="", period="24h", status_filter="", source="", model="", limit=10, include_spans=False)
    params.update(kwargs)
    return json.loads(
        await traces_module._fetch_traces(
            object(),
            user=SimpleNamespace(id=uuid.uuid4(), is_admin=True),
            user_group_ids=[],
            agent_ids=None,
            **params,
        )
    )


def _turn(me: str, generations: int, *, streamed: bool = True, conversation_id=None):
    rollup = {"iterations": generations, "generations": generations}
    if streamed:
        rollup["turn_first_token_ms"] = 800.0
    return _stored_trace(me, metadata_json={"rollup": rollup}, conversation_id=conversation_id)


@pytest.mark.asyncio
async def test_started_from_and_to_replace_the_period_bound(monkeypatch):
    store = _WindowStore([])

    listing = await _window_list(
        monkeypatch, store, started_from="2026-11-06T11:00:00Z", started_to="2026-11-06T14:00:00"
    )

    call = store.calls[0]
    assert call["date_from"].isoformat() == "2026-11-06T11:00:00+00:00"
    # A naive time is read as UTC.
    assert call["date_to"].isoformat() == "2026-11-06T14:00:00+00:00"
    assert listing["started_from"] == "2026-11-06T11:00:00+00:00"


@pytest.mark.asyncio
async def test_a_bad_or_inverted_window_is_refused_before_any_read(monkeypatch):
    store = _WindowStore([])
    monkeypatch.setattr(traces_module, "build_trace_read_store", lambda: store)
    common = dict(
        user=SimpleNamespace(id=uuid.uuid4(), is_admin=True),
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

    bad = await traces_module._fetch_traces(object(), started_from="yesterday", **common)
    inverted = await traces_module._fetch_traces(
        object(), started_from="2026-11-06T12:00:00Z", started_to="2026-11-06T11:00:00Z", **common
    )

    assert bad.startswith("Error: started_from is not an ISO 8601 time")
    assert inverted == "Error: started_to is before started_from."
    assert store.calls == []


@pytest.mark.asyncio
async def test_the_generations_band_and_streamed_only_filter_on_the_stored_rollup(monkeypatch):
    me = str(uuid.uuid4())
    conv = uuid.uuid4()
    one = _turn(me, 1, conversation_id=conv)
    three = _turn(me, 3)
    five = _turn(me, 5)
    unstreamed = _turn(me, 2, streamed=False)
    no_rollup = _stored_trace(me, metadata_json={})
    store = _WindowStore([one, five, unstreamed, no_rollup, three])

    listing = await _window_list(monkeypatch, store, max_generations=3, streamed_only=True)

    assert [t["id"] for t in listing["traces"]] == [str(one.id), str(three.id)]
    assert listing["total"] == 2
    assert listing["filtered_on_rollup"] is True
    assert listing["traces"][0]["conversation_id"] == str(conv)
    assert "conversation_id" not in listing["traces"][1]
    assert "next_offset" not in listing


@pytest.mark.asyncio
async def test_min_generations_keeps_the_heavier_runs(monkeypatch):
    me = str(uuid.uuid4())
    light, heavy = _turn(me, 1, streamed=False), _turn(me, 6, streamed=False)

    listing = await _window_list(monkeypatch, _WindowStore([light, heavy]), min_generations=2)

    assert [t["id"] for t in listing["traces"]] == [str(heavy.id)]


@pytest.mark.asyncio
async def test_offset_pages_over_the_filtered_set(monkeypatch):
    me = str(uuid.uuid4())
    turns = [_turn(me, 1) if i % 2 == 0 else _turn(me, 9) for i in range(10)]
    matching = [str(t.id) for t in turns if t.metadata_json["rollup"]["generations"] == 1]

    first = await _window_list(monkeypatch, _WindowStore(turns), max_generations=3, limit=3)
    second = await _window_list(
        monkeypatch, _WindowStore(turns), max_generations=3, limit=3, offset=first["next_offset"]
    )

    assert [t["id"] for t in first["traces"]] == matching[:3]
    assert first["next_offset"] == 3
    assert [t["id"] for t in second["traces"]] == matching[3:]
    assert "next_offset" not in second


@pytest.mark.asyncio
async def test_offset_pages_an_unfiltered_listing_in_the_store(monkeypatch):
    me = str(uuid.uuid4())
    rows = [_stored_trace(me) for _ in range(5)]
    store = _WindowStore(rows)

    listing = await _window_list(monkeypatch, store, limit=2, offset=2)

    assert store.calls[0]["offset"] == 2
    assert [t["id"] for t in listing["traces"]] == [str(r.id) for r in rows[2:4]]
    assert listing["next_offset"] == 4


@pytest.mark.asyncio
async def test_the_rollup_scan_is_bounded_and_says_so(monkeypatch):
    me = str(uuid.uuid4())
    monkeypatch.setattr(traces_module, "_ROLLUP_FILTER_SCAN_MAX", 4)
    monkeypatch.setattr(traces_module, "_ROLLUP_FILTER_SCAN_PAGE", 2)
    store = _WindowStore([_turn(me, 9) for _ in range(10)])

    listing = await _window_list(monkeypatch, store, max_generations=3)

    assert listing["traces"] == []
    assert len(store.calls) == 2
    assert "newest 4 traces" in listing["filter_note"]


@pytest.mark.asyncio
async def test_inspect_my_traces_forwards_the_window_and_filters(monkeypatch):
    me = str(uuid.uuid4())
    captured: dict = {}

    async def _user_context(_db, _uid):
        return SimpleNamespace(id=uuid.uuid4(), email="me@example.com", is_admin=False), []

    async def _fetch(_db, **kwargs):
        captured.update(kwargs)
        return '{"count": 0, "traces": []}'

    monkeypatch.setattr(traces_module, "async_session", _fake_async_session_factory(_FakeDBSession()))
    monkeypatch.setattr(traces_module, "_load_user_context", _user_context)
    monkeypatch.setattr(traces_module, "_fetch_traces", _fetch)
    _definitions, executor = traces_module.build_trace_tools(object(), str(uuid.uuid4()), me)
    arguments = {
        "started_from": "2026-11-06T11:00:00Z",
        "max_generations": 3,
        "streamed_only": True,
        "offset": 25,
    }

    result = await executor.execute_tool(ToolCall(id="c1", name="inspect_my_traces", arguments=arguments))

    assert result.is_error is False
    assert {key: captured[key] for key in arguments} == arguments
    # Own traces only: the window never widens the scope.
    assert captured["agent_ids"] == [me]
    schema = next(d for d in _definitions if d.name == "inspect_my_traces").input_schema["properties"]
    assert set(arguments) | {"started_to", "min_generations"} <= set(schema)


@pytest.mark.asyncio
async def test_inspect_agent_traces_forwards_the_window_inside_the_supervision_scope(monkeypatch):
    """A supervisor reads another agent's runs of a given hour and pages past the newest (#740)."""
    target = str(uuid.uuid4())
    captured: dict = {}

    async def _user_context(_db, _uid):
        return SimpleNamespace(id=uuid.uuid4(), email="me@example.com", is_admin=False), []

    async def _fetch(_db, **kwargs):
        captured.update(kwargs)
        return '{"count": 0, "traces": []}'

    async def _no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(traces_module, "async_session", _fake_async_session_factory(_FakeDBSession()))
    monkeypatch.setattr(traces_module, "_load_user_context", _user_context)
    monkeypatch.setattr(traces_module, "_fetch_traces", _fetch)
    monkeypatch.setattr(traces_module, "_audit_trace_read", _no_audit)
    definitions, executor = traces_module.build_trace_tools(
        object(),
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        supervision_target_agent_ids=[target],
        allow_cross_agent=True,
    )
    window = {"started_from": "2026-11-06T11:00:00Z", "started_to": "2026-11-06T14:00:00Z", "offset": 25}

    result = await executor.execute_tool(
        ToolCall(id="c2", name="inspect_agent_traces", arguments={"target_agent_id": target, **window})
    )

    assert result.is_error is False
    assert {key: captured[key] for key in window} == window
    # The window narrows the time, never the agents read.
    assert captured["agent_ids"] == [target]
    schema = next(d for d in definitions if d.name == "inspect_agent_traces").input_schema["properties"]
    assert set(window) <= set(schema)


class _OneTraceStore:
    """A trace store answering one trace's spans."""

    def __init__(self, spans):
        self.spans = spans

    async def get_trace_spans(self, **_kwargs):
        return self.spans


def _searched_trace():
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=None,
        source="assignment",
        name="run",
        model="m",
        status="ok",
        started_at=None,
        ended_at=None,
        duration_ms=1,
        input_tokens=1,
        output_tokens=1,
        total_tokens=2,
        total_cost=None,
        error_message=None,
        input="x" * 300_000,
        output="done",
        metadata_json=None,
    )


def _search_spans():
    spans = [_fake_span(type="GENERATION", name="llm", output="lost" * 50, tool_arguments=None)]
    for index in range(46):
        # Odd indexes are command runs: 5 and 21 match, 40 is a file read.
        word = "lost" if index in (5, 21, 40) else "cached"
        spans.append(
            _fake_span(
                # Stored as the tracer writes a tool span: "tool:<name>".
                name="tool:sandbox_run_command" if index % 2 else "tool:sandbox_read_file",
                tool_arguments={"check": "backend-unit", "n": index},
                output="y" * 6_000 + f" container {word} " + "z" * 6_000,
            )
        )
    # The arguments are searched too, and a file read is not a command run.
    spans.append(_fake_span(name="tool:sandbox_run_command", tool_arguments={"note": "LOST"}, output="ok"))
    return spans


async def _search(monkeypatch, spans, **arguments):
    monkeypatch.setattr(traces_module, "build_trace_read_store", lambda: _OneTraceStore(spans))

    async def _nobody_gone(_db, _ids):
        return set()

    monkeypatch.setattr(traces_module, "_agents_gone", _nobody_gone)
    return await traces_module._fetch_traces(
        object(),
        user=SimpleNamespace(id=uuid.uuid4(), is_admin=False),
        user_group_ids=[],
        query="",
        period="7d",
        status_filter="",
        source="",
        model="",
        limit=1,
        agent_ids=None,
        include_spans=True,
        only_trace=_searched_trace(),
        **arguments,
    )


@pytest.mark.asyncio
async def test_a_span_search_answers_the_matching_tool_spans_alone(monkeypatch):
    """Three outputs of a fifty-span run in one compact answer: no rollup, no
    trace preview, no span that does not match (dev:trace-span-filter)."""
    text = await _search(monkeypatch, _search_spans(), span_tool="SANDBOX_RUN_COMMAND", span_match="Lost")

    assert len(text) < 3_000
    answer = json.loads(text)
    trace = answer["traces"][0]
    assert "rollup" not in trace and "spans" not in trace and "input_preview" not in trace
    assert answer["span_search"] == {"tool": "SANDBOX_RUN_COMMAND", "match": "lost"}
    matches = trace["span_matches"]
    assert trace["span_matches_total"] == 3 and trace["spans_searched"] == 48
    assert [m["tool"] for m in matches] == ["sandbox_run_command"] * 3
    # The 6,000 characters before the hit are cut: the excerpt opens near it.
    assert all(" container lost " in m["output"] for m in matches[:2])
    assert all(m["output"].startswith("…") and "[truncated" in m["output"] for m in matches[:2])
    assert '"LOST"' in matches[2]["tool_arguments"]
    assert trace["spans_next_offset"] is None


@pytest.mark.asyncio
async def test_a_span_search_pages_over_its_matches(monkeypatch):
    first = json.loads(await _search(monkeypatch, _search_spans(), span_match="cached", max_spans=10))
    trace = first["traces"][0]
    assert trace["span_matches_total"] == 43
    assert len(trace["span_matches"]) == 10 and trace["spans_next_offset"] == 10

    last = json.loads(await _search(monkeypatch, _search_spans(), span_match="cached", max_spans=10, span_offset=40))
    assert len(last["traces"][0]["span_matches"]) == 3
    assert last["traces"][0]["spans_next_offset"] is None


@pytest.mark.asyncio
async def test_a_span_search_by_tool_alone_lists_every_call_of_it(monkeypatch):
    answer = json.loads(await _search(monkeypatch, _search_spans(), span_tool="sandbox_read_file"))
    assert answer["traces"][0]["span_matches_total"] == 23


@pytest.mark.asyncio
@pytest.mark.parametrize("span_tool", ["sandbox_run_command", "tool:sandbox_run_command", " Tool:Sandbox_Run_Command "])
async def test_a_span_search_matches_the_bare_and_the_stored_tool_name(monkeypatch, span_tool):
    """The tracer stores "tool:<name>"; callers pass the bare name: both match
    (dev:trace-span-tool-bare-name, retest of #10938 answered 0 on the bare name)."""
    answer = json.loads(await _search(monkeypatch, _search_spans(), span_tool=span_tool, span_match="lost"))
    trace = answer["traces"][0]
    assert trace["span_matches_total"] == 3
    assert {m["tool"] for m in trace["span_matches"]} == {"sandbox_run_command"}
    assert "span_tool_note" not in trace


@pytest.mark.asyncio
async def test_a_span_search_on_a_tool_the_trace_never_called_names_the_tools_it_did(monkeypatch):
    answer = json.loads(await _search(monkeypatch, _search_spans(), span_tool="sandbox_write_file"))
    trace = answer["traces"][0]
    assert trace["span_matches_total"] == 0
    assert trace["span_tool_note"] == (
        "No span of tool sandbox_write_file in this trace; tools in it: sandbox_read_file, sandbox_run_command"
    )


@pytest.mark.asyncio
async def test_a_span_search_of_a_called_tool_without_a_hit_adds_no_note(monkeypatch):
    answer = json.loads(await _search(monkeypatch, _search_spans(), span_tool="sandbox_read_file", span_match="absent"))
    trace = answer["traces"][0]
    assert trace["span_matches_total"] == 0 and "span_tool_note" not in trace


def test_both_listing_tools_take_the_span_search():
    definitions, _executor = traces_module.build_trace_tools(object(), str(uuid.uuid4()), str(uuid.uuid4()))
    for name in ("inspect_my_traces", "inspect_agent_traces"):
        properties = next(d for d in definitions if d.name == name).input_schema["properties"]
        assert {"span_tool", "span_match"} <= set(properties)
