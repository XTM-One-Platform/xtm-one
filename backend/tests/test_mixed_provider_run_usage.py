"""A run answered by two providers records every token of every call.

The agent loop sums a run's usage key by key and the readers take the first key
of each convention they find (``langfuse_tracing._normalize_usage``). A run whose
calls two providers answered (a failover, a budget downgrade to another
provider's model) mixes Anthropic's ``input_tokens`` / ``output_tokens`` with
OpenAI's ``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``: the root
trace and the run's token totals counted the Anthropic calls only, its total was
the OpenAI calls' total only, and its cost priced that at one model.

Such a run's usage now also carries its calls per convention and model
(``USAGE_PARTS_KEY``), read part by part and priced at each part's own model.
So does a run two models served in one convention (a Gemini call next to a
Bedrock one), which one price row cannot price. A run one model served
whose sum reads back exactly carries no parts, so it records exactly what it
did.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from google.genai import types as genai_types

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse, StreamUsageSink, usage_sink_of
from app.llm.gemini_provider import _usage_from_gemini_metadata
from app.llm.openai_compat import extract_openai_response_usage
from app.llm.router import FailoverLLMProvider
from app.services import langfuse_tracing as lft
from app.services.tracing.base import SpanData, TraceData
from app.services.tracing.clickhouse_sink import ClickHouseSink
from app.services.tracing.cost_calculator import ModelPrice, calculate_costs
from app.services.tracing.local_sink import LocalPGSink
from app.tools import agent_loop as al
from app.tools.base import CallableToolExecutor, CompositeToolExecutor, ToolCall, ToolDefinition
from app.tools.run_budget import RunBudget

# An Anthropic call: 1k uncached input, 20k read from the cache, 500 out.
_ANTHROPIC_CALL = {"input_tokens": 1_000, "output_tokens": 500, "cache_read_input_tokens": 20_000}
# An OpenAI call: 10k prompt tokens, 4k of them cached, 2k out.
_OPENAI_CALL = {
    "prompt_tokens": 10_000,
    "completion_tokens": 2_000,
    "total_tokens": 12_000,
    "prompt_tokens_details": {"cached_tokens": 4_000},
}
# A Gemini call: no total, like every Gemini usage dict.
_GEMINI_CALL = _usage_from_gemini_metadata(
    genai_types.GenerateContentResponseUsageMetadata(
        prompt_token_count=3_000,
        cached_content_token_count=1_000,
        candidates_token_count=700,
        total_token_count=3_700,
    )
)


def _price(model_id: str, inp: str, out: str, cache_read: str) -> ModelPrice:
    return ModelPrice(
        provider="stub",
        model_id=model_id,
        model_pattern=None,
        input_price_per_1m=Decimal(inp),
        output_price_per_1m=Decimal(out),
        cache_read_price_per_1m=Decimal(cache_read),
        cache_write_price_per_1m=None,
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )


_PRICES = {
    "claude-test": _price("claude-test", "3", "15", "0.3"),
    "gpt-test": _price("gpt-test", "2.5", "10", "1.25"),
    "gpt-fallback": _price("gpt-fallback", "0.15", "0.6", "0.075"),
    "gemini-test": _price("gemini-test", "1.25", "10", "0.25"),
    "bedrock-test": _price("bedrock-test", "0.8", "4", "0.08"),
}


def _mixed_run_usage() -> dict:
    """The usage a run ends with after an Anthropic call then an OpenAI call."""
    parts = al._RunUsageParts()
    summed: dict = {}
    for usage, model in ((_ANTHROPIC_CALL, "claude-test"), (_OPENAI_CALL, "gpt-test")):
        for key, value in usage.items():
            if isinstance(value, (int, float)):
                summed[key] = summed.get(key, 0) + int(value)
        al._sum_usage_details_into(summed, usage)
        parts.add(dict(usage), model, "claude-test")
        parts.attach(summed)
    return summed


class _ScriptedLLM(BaseLLMProvider):
    provider_name = "stub"
    context_window = 1_000_000

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = script
        self.offered_tools: list[list[str] | None] = []

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self.offered_tools.append([t.name for t in tools] if tools is not None else None)
        return self._script.pop(0)

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.fixture
def _quiet_langfuse(monkeypatch):
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)


def _executor() -> CompositeToolExecutor:
    callable_exec = CallableToolExecutor()

    async def _lookup(**kwargs):
        return "found it"

    callable_exec.register("lookup", _lookup)
    composite = CompositeToolExecutor()
    composite.add_tools(
        [ToolDefinition(name="lookup", description="lookup", source="custom:test")],
        callable_exec,
    )
    return composite


def _tool_turn(model: str, usage: dict, call_id: str) -> ChatResponse:
    return ChatResponse(
        content="",
        model=model,
        stop_reason="tool_use",
        tool_calls=[ToolCall(id=call_id, name="lookup", arguments={"q": call_id})],
        usage=dict(usage),
    )


def _root_ctx() -> dict:
    return {
        "trace": None,
        "generation": None,
        "client": None,
        "parent_ctx": None,
        "metadata": {},
        "local_spans": [],
        "local_trace_id": uuid4(),
        "local_source": "assignment",
        "local_name": "run",
        "local_started_at": datetime.now(UTC),
        "local_metadata": {},
        "local_tags": [],
    }


def _ended_trace(usage: dict, model: str = "claude-test") -> TraceData:
    with patch("app.services.tracing.dispatcher.submit_trace_for_flush") as submit:
        lft.end_trace(_root_ctx(), output="done", usage=usage, model=model)
    trace_data, _spans = submit.call_args.args
    return trace_data


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.added_all: list[object] = []

    def add(self, row) -> None:
        self.added.append(row)

    def add_all(self, rows) -> None:
        self.added_all.extend(rows)

    async def flush(self) -> None:
        return None


@pytest.fixture
def _prices_by_model(monkeypatch):
    async def fake_resolve(_session, *, model_id, provider=None):
        return _PRICES.get(model_id)

    monkeypatch.setattr("app.services.tracing.local_sink.resolve_model_price", fake_resolve)
    monkeypatch.setattr("app.services.tracing.local_sink.resolve_model_price_any", fake_resolve)


async def test_a_run_answered_by_two_providers_keeps_every_call(_quiet_langfuse):
    """A failover after the first call: the run's usage reads both calls back."""
    composite = _executor()
    llm = _ScriptedLLM(
        [
            _tool_turn("claude-test", _ANTHROPIC_CALL, "c1"),
            ChatResponse(content="Done.", model="gpt-test", usage=dict(_OPENAI_CALL)),
        ]
    )

    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
    )

    usage = response.usage
    # The flat sum is the one every run always carried...
    assert {key: value for key, value in usage.items() if key != lft.USAGE_PARTS_KEY} == {
        "input_tokens": 1_000,
        "output_tokens": 500,
        "cache_read_input_tokens": 20_000,
        "prompt_tokens": 10_000,
        "completion_tokens": 2_000,
        "total_tokens": 12_000,
        "prompt_tokens_details": {"cached_tokens": 4_000},
    }
    # ...and the calls ride on it per model, as they came back.
    assert usage[lft.USAGE_PARTS_KEY] == [
        {"model": "claude-test", "usage": _ANTHROPIC_CALL},
        {"model": "gpt-test", "usage": _OPENAI_CALL},
    ]
    # Every prompt token of both calls; before, the OpenAI call's were dropped.
    assert lft.usage_token_totals(usage) == (31_000, 2_500)


def test_the_root_trace_of_a_mixed_run_records_the_sum_of_its_calls():
    trace = _ended_trace(_mixed_run_usage())

    # Before: 21,000 / 500 / 12,000 - the Anthropic call's input and output, the
    # OpenAI call's total.
    assert (trace.input_tokens, trace.output_tokens, trace.total_tokens) == (31_000, 2_500, 33_500)
    anthropic = lft._normalize_usage(_ANTHROPIC_CALL)
    openai = lft._normalize_usage(_OPENAI_CALL)
    assert trace.usage_details == {
        "input": anthropic["input"] + openai["input"],
        "output": 2_500,
        "total": 33_500,
        "cache_read_input_tokens": 20_000,
        "input_cached_tokens": 4_000,
        lft.USAGE_PARTS_KEY: [
            {"model": "claude-test", "usage_details": anthropic},
            {"model": "gpt-test", "usage_details": openai},
        ],
    }


def test_the_langfuse_root_span_carries_the_sum_of_the_calls():
    """Metadata only: Langfuse prices the generations, not the root span."""
    root_span = MagicMock()
    ctx = {**_root_ctx(), "trace": root_span}

    with patch("app.services.tracing.dispatcher.submit_trace_for_flush"):
        lft.end_trace(ctx, output="done", usage=_mixed_run_usage(), model="claude-test")

    (metadata,) = [call.kwargs["metadata"] for call in root_span.update.call_args_list if "metadata" in call.kwargs]
    assert metadata["total_usage"] == lft._sum_normalized_usage(
        lft._normalize_usage(usage) for usage in (_ANTHROPIC_CALL, _OPENAI_CALL)
    )


def test_the_parts_ride_in_a_payload_an_older_replica_can_rebuild():
    """A trace queued in Redis is rebuilt with ``TraceData(**raw)`` by any replica."""
    from dataclasses import fields

    from app.services.tracing import dispatcher

    trace = _ended_trace(_mixed_run_usage())
    raw = dispatcher._serialize_trace(trace, [])

    # No field an older ``TraceData`` would refuse: the payload keeps main's keys.
    assert {field.name for field in fields(TraceData)} == {
        "id",
        "source",
        "name",
        "started_at",
        "ended_at",
        "duration_ms",
        "status",
        "user_id",
        "agent_id",
        "conversation_id",
        "assignment_run_id",
        "parent_trace_id",
        "model",
        "environment",
        "metadata",
        "input",
        "output",
        "tags",
        "level",
        "error_message",
        "failure_cause",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "usage_details",
        "spans",
    }
    rebuilt, _spans = dispatcher._deserialize_trace(raw)
    assert rebuilt.usage_details == trace.usage_details
    # An older sink prices the summed dict and ignores the list it does not know.
    assert calculate_costs(trace.usage_details, _PRICES["claude-test"]) == calculate_costs(
        {k: v for k, v in trace.usage_details.items() if k != lft.USAGE_PARTS_KEY}, _PRICES["claude-test"]
    )


async def test_the_sink_prices_each_part_at_the_model_that_served_it(_prices_by_model):
    trace = _ended_trace(_mixed_run_usage())
    spans = [
        SpanData(
            id=uuid4(),
            trace_id=trace.id,
            type="GENERATION",
            name="llm_call",
            position=position,
            started_at=trace.started_at,
            ended_at=trace.ended_at,
            model=model,
            usage_details=lft._normalize_usage(usage),
        )
        for position, (model, usage) in enumerate(
            (("claude-test", _ANTHROPIC_CALL), ("gpt-test", _OPENAI_CALL)), start=1
        )
    ]
    session = _FakeSession()

    await LocalPGSink(session_factory=lambda: None)._flush_trace_with_session(session, trace, spans)

    root = session.added[0]
    span_costs = [Decimal(str(span.total_cost)) for span in session.added_all]
    # 1k in @ $3 + 20k cache read @ $0.30 + 500 out @ $15, then 6k in @ $2.50
    # + 4k cached @ $1.25 + 2k out @ $10, per 1M tokens.
    assert span_costs == [Decimal("0.0165"), Decimal("0.04")]
    for column in ("input_cost", "output_cost", "total_cost"):
        assert Decimal(str(getattr(root, column))) == sum(
            Decimal(str(getattr(span, column))) for span in session.added_all
        )
    assert (root.input_tokens, root.output_tokens, root.total_tokens) == (31_000, 2_500, 33_500)


async def test_a_part_whose_model_has_no_price_adds_nothing(_prices_by_model):
    usage = _mixed_run_usage()
    usage[lft.USAGE_PARTS_KEY][1]["model"] = "self-hosted-model"
    session = _FakeSession()

    await LocalPGSink(session_factory=lambda: None)._flush_trace_with_session(session, _ended_trace(usage), [])

    # Like a generation of that model: its tokens are counted, its cost unknown.
    assert Decimal(str(session.added[0].total_cost)) == Decimal("0.0165")
    assert session.added[0].input_tokens == 31_000


async def test_a_run_with_no_priced_part_keeps_an_unknown_cost(_prices_by_model):
    usage = _mixed_run_usage()
    for part in usage[lft.USAGE_PARTS_KEY]:
        part["model"] = "self-hosted-model"
    session = _FakeSession()

    await LocalPGSink(session_factory=lambda: None)._flush_trace_with_session(
        session, _ended_trace(usage, model="self-hosted-model"), []
    )

    assert session.added[0].total_cost is None


async def test_a_run_one_model_served_carries_no_parts_and_is_priced_as_before(_quiet_langfuse, _prices_by_model):
    """Its responses name the model two ways (the API's dated name, the name
    it was asked for): one model, nothing changes."""
    composite = _executor()
    second = {"input_tokens": 2_000, "output_tokens": 300, "cache_creation_input_tokens": 1_500}
    llm = _ScriptedLLM(
        [
            _tool_turn("claude-test", _ANTHROPIC_CALL, "c1"),
            ChatResponse(content="Done.", model="claude-test-20260101", usage=dict(second)),
        ]
    )

    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        model="claude-test",
        max_iterations=5,
    )

    assert response.usage == {
        "input_tokens": 3_000,
        "output_tokens": 800,
        "cache_read_input_tokens": 20_000,
        "cache_creation_input_tokens": 1_500,
    }
    trace = _ended_trace(response.usage)
    assert trace.usage_details == lft._normalize_usage(response.usage)
    assert lft.USAGE_PARTS_KEY not in trace.usage_details
    session = _FakeSession()
    await LocalPGSink(session_factory=lambda: None)._flush_trace_with_session(session, trace, [])
    # The whole run at the root model's price, as before.
    expected = calculate_costs(trace.usage_details, _PRICES["claude-test"])["total_cost"]
    assert Decimal(str(session.added[0].total_cost)) == expected


def _parts_of(*calls: tuple) -> dict:
    """A run's usage after *calls*: ``(usage, served_model)`` for a call made
    with the run's model, ``(usage, served_model, fallback_model)`` for a call
    a failover fallback served."""
    parts = al._RunUsageParts()
    summed: dict = {}
    for usage, model, *fallback in calls:
        for key, value in usage.items():
            if isinstance(value, (int, float)):
                summed[key] = summed.get(key, 0) + int(value)
        al._sum_usage_details_into(summed, usage)
        parts.add(dict(usage), model, "run-model", *fallback)
        parts.attach(summed)
    return summed


def test_a_call_cut_before_its_output_does_not_split_a_run():
    """An Anthropic stream stopped after ``message_start`` reports no output."""
    summed = _parts_of(
        (_ANTHROPIC_CALL, "claude-test"),
        ({"input_tokens": 700, "cache_read_input_tokens": 50}, None),
    )

    assert lft.USAGE_PARTS_KEY not in summed


def test_calls_that_report_a_total_and_calls_that_do_not_are_all_counted():
    """OpenAI reports a total, Gemini none: the run's total counted OpenAI's only."""
    summed = _parts_of((_OPENAI_CALL, "gpt-test"), (_GEMINI_CALL, "gemini-test"))

    assert lft.USAGE_PARTS_KEY in summed
    gemini_split = _GEMINI_CALL["prompt_tokens"] + _GEMINI_CALL["completion_tokens"]
    assert lft._normalize_usage(summed)["total"] == 12_000 + gemini_split
    assert lft.usage_token_totals(summed) == (13_000, 2_700)


def test_a_usage_without_its_model_goes_to_the_run_model():
    """A call that never returned and that no failover fallback was serving
    hands its usage over alone. It was made with the run's model, the one its
    generation keeps, whichever model served its convention earlier in the
    run: a fallback, or the model a budget downgrade moved away from."""
    parts = al._RunUsageParts()
    cut = {"prompt_tokens": 12_000, "completion_tokens": 400, "total_tokens": 12_400}
    # A fallback serves the OpenAI convention first.
    parts.add(dict(_OPENAI_CALL), "gpt-fallback", "gpt-test", "gpt-fallback")
    # The run moves to a cheaper model, whose stream is then cut.
    parts.add(dict(cut), None, "gpt-cheap")
    # With no run model either, the part names none: the trace's model prices
    # it, as it prices the call's generation.
    parts.add({"input": 30, "output": 5}, None, None)
    # A response that reported nothing makes no part.
    parts.add({}, "gpt-test", "gpt-test")

    assert [(part["model"], part["usage"]) for part in parts._parts.values()] == [
        ("gpt-fallback", _OPENAI_CALL),
        ("gpt-cheap", cut),
        (None, {"input": 30, "output": 5}),
    ]


def test_a_model_that_is_not_a_name_is_attributed_like_a_missing_one():
    parts = al._RunUsageParts()
    parts.add(dict(_ANTHROPIC_CALL), "claude-test", "run-model")
    parts.add(dict(_ANTHROPIC_CALL), {"not": "a name"}, "run-model")

    assert [(part["model"], part["usage"]["input_tokens"]) for part in parts._parts.values()] == [
        ("claude-test", 1_000),
        ("run-model", 1_000),
    ]


@pytest.mark.parametrize(
    "garbage",
    ["not a list", [], [5], [{"model": 1, "usage": {}}], [{"model": "m", "usage": "x"}]],
)
def test_malformed_parts_are_read_as_the_flat_sum(garbage):
    flat = {**_ANTHROPIC_CALL, "total_tokens": 3}

    assert lft._normalize_usage({**flat, lft.USAGE_PARTS_KEY: garbage}) == lft._normalize_usage(flat)
    assert lft.usage_token_totals({**flat, lft.USAGE_PARTS_KEY: garbage}) == lft.usage_token_totals(flat)


@pytest.mark.parametrize(
    ("usage", "convention"),
    [
        (_ANTHROPIC_CALL, ("input_tokens", False)),
        (_OPENAI_CALL, ("prompt_tokens", True)),
        (_GEMINI_CALL, ("prompt_tokens", False)),
        ({"prompt_tokens": 5, "completion_tokens": 1, "cache_read_input_tokens": 9}, ("prompt_tokens", False)),
        ({"total_tokens": 40}, (None, True)),
        ({"input": 3, "output": 1, "total": 4}, ("input", True)),
        ({"output_tokens": 7}, ("input_tokens", False)),
        ({"input_tokens": True, "prompt_tokens": 4}, ("prompt_tokens", False)),
    ],
)
def test_usage_convention(usage, convention):
    assert lft.usage_convention(usage) == convention


def test_the_run_token_totals_of_a_mixed_run_count_both_providers():
    from app.services.assignment_engine import apply_run_usage_accounting

    run = SimpleNamespace(
        id=uuid4(), langfuse_trace_id=None, llm_trace_id=None, total_input_tokens=None, total_output_tokens=None
    )

    apply_run_usage_accounting(run, _mixed_run_usage(), None)

    # Before: 21,000 in / 500 out.
    assert (run.total_input_tokens, run.total_output_tokens) == (31_000, 2_500)


@pytest.mark.parametrize(
    ("second", "budget", "spent"),
    [
        # A mixed run: the flat sum reads the OpenAI call's total alone
        # (12,000), with or without the parts, exactly as on main.
        pytest.param(("gpt-test", _OPENAI_CALL), 12_000, 12_000, id="mixed-crosses"),
        pytest.param(("gpt-test", _OPENAI_CALL), 12_001, None, id="mixed-stays-under"),
        # A single-convention run: every input and output token, as always.
        pytest.param(("claude-other", _ANTHROPIC_CALL), 3_000, 3_000, id="single-crosses"),
        pytest.param(("claude-other", _ANTHROPIC_CALL), 3_001, None, id="single-stays-under"),
    ],
)
async def test_the_token_wind_down_reads_the_flat_sum_as_on_main(_quiet_langfuse, caplog, second, budget, spent):
    """The legacy ``max_total_tokens`` wind-down is a loop guard: it reads the
    run's flat summed usage, never the parts."""
    composite = _executor()
    second_model, second_usage = second
    llm = _ScriptedLLM(
        [
            _tool_turn("claude-test", _ANTHROPIC_CALL, "c1"),
            _tool_turn(second_model, second_usage, "c2"),
            ChatResponse(content="Budget answer.", model=second_model, usage={}),
        ]
    )

    with caplog.at_level(logging.WARNING, logger="app.tools.agent_loop"):
        response = await al.run_agent_loop(
            llm=llm,
            messages=[ChatMessage(role="user", content="go")],
            tools=composite.all_tools,
            executor=composite,
            max_iterations=10,
            max_total_tokens=budget,
        )

    assert response.content == "Budget answer."
    assert (lft.USAGE_PARTS_KEY in response.usage) is (second_model == "gpt-test")
    assert llm.offered_tools[1] == ["lookup", "wait"]
    wind_downs = [r.getMessage() for r in caplog.records if "token budget exceeded" in r.getMessage()]
    if spent is None:
        assert llm.offered_tools[2] == ["lookup", "wait"]
        assert wind_downs == []
    else:
        assert llm.offered_tools[2] is None
        assert len(wind_downs) == 1
        assert f"token budget exceeded: {spent} " in wind_downs[0]


# ---------------------------------------------------------------------------
# A call a failover fallback answered goes to the fallback's model wherever the
# loop hands its usage over: the compaction summariser, a max-tokens
# continuation, the forced answer, a stream cut while the fallback answered.
# ---------------------------------------------------------------------------

_SUMMARY_TEXT = "Prior work: the failed deploy of build 4.2.1 was isolated and rolled back. " * 35
_LONG_ANSWER = "The incident was traced to a failed deploy, and this is the full report. " * 3


class _Served(BaseLLMProvider):
    """One provider of a ``FailoverLLMProvider`` chain, answering from scripts.

    An exception in a script fails the call over to the next provider: that is
    how a response the loop receives comes to name another provider's model.
    """

    context_window = 1_000_000

    def __init__(self, name: str, model: str, *, chats=(), streams=()) -> None:
        self.provider_name = name
        self.default_model = model
        self.chats = list(chats)
        self.streams = list(streams)

    async def chat(self, messages, model=None, tools=None, **kwargs):
        item = self.chats.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def chat_stream(self, messages, model=None, **kwargs):
        item = self.streams.pop(0)
        if isinstance(item, BaseException):
            raise item
        chunks, usage, stop_reason = item
        # Like a provider: the attempt it opens, what it learns while it
        # streams, then what the finished stream reported.
        sink = usage_sink_of(kwargs)
        if sink is not None:
            sink.restart()
            sink.report(dict(usage))
        self.last_usage = dict(usage)
        self.last_stop_reason = stop_reason
        for chunk in chunks:
            yield chunk

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


def _reply(model: str, usage: dict, content: str = "Done.", stop_reason: str = "end_turn") -> ChatResponse:
    return ChatResponse(content=content, model=model, stop_reason=stop_reason, usage=dict(usage))


def _chain(primary: _Served, fallback: _Served) -> FailoverLLMProvider:
    return FailoverLLMProvider(primary=primary, fallbacks=[fallback])


def _overloaded() -> RuntimeError:
    return RuntimeError("primary overloaded")


def _recording_root() -> dict:
    """A root trace context the loop records its generations on."""
    return {**_root_ctx(), "local_span_counter": 0, "input_data": {"messages": []}}


async def _run_recorded(root: dict, *, model: str = "claude-test", **kwargs) -> ChatResponse:
    token = lft.set_current_trace_ctx(root)
    try:
        return await al.run_agent_loop(model=model, max_tokens=1_024, **kwargs)
    finally:
        lft.reset_current_trace_ctx(token)


async def _priced(root: dict, response: ChatResponse) -> tuple[object, list]:
    """The root trace and its generations, priced by the local sink."""
    with patch("app.services.tracing.dispatcher.submit_trace_for_flush") as submit:
        lft.end_trace(root, output=response.content, usage=response.usage, model=response.model)
    trace, spans = submit.call_args.args
    session = _FakeSession()
    await LocalPGSink(session_factory=lambda: None)._flush_trace_with_session(session, trace, spans)
    return session.added[0], [row for row in session.added_all if row.type == "GENERATION"]


async def _collect(_chunk: str) -> None:
    return None


async def test_a_compaction_a_fallback_answered_is_priced_at_the_fallback(_prices_by_model):
    """The summariser call fails over to an OpenAI-schema provider before any
    call of the run named a model of that convention. Its usage goes to the
    model its response names, the one its generation is priced at; it went to
    the run's model, and the root trace priced OpenAI tokens at Anthropic's
    rates."""
    primary = _Served("anthropic", "claude-test", chats=[_overloaded(), _reply("claude-test", _ANTHROPIC_CALL)])
    fallback = _Served("openai", "gpt-test", chats=[_reply("gpt-test", _OPENAI_CALL, _SUMMARY_TEXT)])
    history = [
        ChatMessage(role="system", content="You are a careful incident analyst."),
        ChatMessage(role="user", content="Investigate the incident and report."),
        *(ChatMessage(role="assistant", content=f"turn-{i}: " + "x" * 3_500) for i in range(20)),
    ]
    root = _recording_root()

    response = await _run_recorded(
        root, llm=_chain(primary, fallback), messages=history, max_iterations=5, max_session_chars=20_000
    )

    assert any(al._SEMANTIC_COMPACTION_MARKER in (message.content or "") for message in history)
    assert response.usage[lft.USAGE_PARTS_KEY] == [
        {"model": "gpt-test", "usage": _OPENAI_CALL},
        {"model": "claude-test", "usage": _ANTHROPIC_CALL},
    ]
    trace_row, generations = await _priced(root, response)
    assert [generation.model for generation in generations] == ["gpt-test", "claude-test"]
    assert trace_row.total_cost == sum(generation.total_cost for generation in generations)


@pytest.mark.parametrize("streamed", [False, True], ids=["plain", "streamed"])
async def test_a_continuation_a_fallback_answered_is_priced_at_the_fallback(streamed, _prices_by_model):
    """The answer stops at ``max_tokens`` and its continuation fails over: the
    continuation's usage goes to the model its response names (a text stream's
    from the failover wrapper), not to the run's model."""
    if streamed:
        primary = _Served(
            "anthropic", "claude-test", streams=[(["Part one"], _ANTHROPIC_CALL, "max_tokens"), _overloaded()]
        )
        fallback = _Served("openai", "gpt-test", streams=[([" and part two."], _OPENAI_CALL, "end_turn")])
    else:
        primary = _Served(
            "anthropic", "claude-test", chats=[_reply("claude-test", _ANTHROPIC_CALL, "Part one", "max_tokens"), _overloaded()]
        )
        fallback = _Served("openai", "gpt-test", chats=[_reply("gpt-test", _OPENAI_CALL, " and part two.")])
    root = _recording_root()

    response = await _run_recorded(
        root,
        llm=_chain(primary, fallback),
        messages=[ChatMessage(role="user", content="Write the report.")],
        max_iterations=3,
        on_token=_collect if streamed else None,
    )

    assert response.content == "Part one and part two."
    assert response.usage[lft.USAGE_PARTS_KEY] == [
        {"model": "claude-test", "usage": _ANTHROPIC_CALL},
        {"model": "gpt-test", "usage": _OPENAI_CALL},
    ]
    trace_row, generations = await _priced(root, response)
    assert sorted(generation.model for generation in generations) == ["claude-test", "gpt-test"]
    assert trace_row.total_cost == sum(generation.total_cost for generation in generations)


async def test_a_forced_answer_a_fallback_answered_is_priced_at_the_fallback(_prices_by_model):
    """The synthesis and its continuation fail over. Each backstop call is
    recorded once and under the model its response names, the one its
    generation records, never the run's: the root trace costs its generations."""
    composite = _executor()
    continuation = {"prompt_tokens": 12_000, "completion_tokens": 400, "total_tokens": 12_400}
    primary = _Served(
        "anthropic",
        "claude-test",
        chats=[_tool_turn("claude-test", _ANTHROPIC_CALL, "c1"), _overloaded(), _overloaded()],
    )
    fallback = _Served(
        "openai",
        "gpt-test",
        chats=[_reply("gpt-test", _OPENAI_CALL, _LONG_ANSWER, "max_tokens"), _reply("gpt-test", continuation, " The end.")],
    )
    root = _recording_root()

    response = await _run_recorded(
        root,
        llm=_chain(primary, fallback),
        messages=[ChatMessage(role="user", content="Investigate.")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        # One tool call: the next call is refused and the run synthesises.
        budget=RunBudget(max_tool_calls=1),
    )

    assert response.content.endswith(" The end.")
    assert response.usage[lft.USAGE_PARTS_KEY] == [
        {"model": "claude-test", "usage": _ANTHROPIC_CALL},
        {"model": "gpt-test", "usage": _flat_sum(_OPENAI_CALL, continuation)},
    ]
    trace_row, generations = await _priced(root, response)
    assert [generation.model for generation in generations] == ["claude-test", "gpt-test", "gpt-test"]
    assert trace_row.total_cost == sum(generation.total_cost for generation in generations)


async def test_a_stream_cut_while_a_fallback_answered_goes_to_the_fallback(_quiet_langfuse):
    """The continuation fails over and the user stops it mid-answer: the
    wrapper noted the fallback's model on the call's usage sink, and
    the usage it reported goes to that model, not to the run's."""
    cancel = asyncio.Event()

    async def _stop_in_the_fallback(chunk: str) -> None:
        if chunk == "The fallback starts":
            cancel.set()

    primary = _Served(
        "anthropic", "claude-test", streams=[(["Part one"], _ANTHROPIC_CALL, "max_tokens"), _overloaded()]
    )
    fallback = _Served("openai", "gpt-test", streams=[(["The fallback starts", " and goes on."], _OPENAI_CALL, "end_turn")])

    response = await al.run_agent_loop(
        llm=_chain(primary, fallback),
        messages=[ChatMessage(role="user", content="Write the report.")],
        model="claude-test",
        max_iterations=3,
        on_token=_stop_in_the_fallback,
        cancel_event=cancel,
    )

    assert response.stop_reason == "cancelled"
    assert response.usage[lft.USAGE_PARTS_KEY] == [
        {"model": "claude-test", "usage": _ANTHROPIC_CALL},
        {"model": "gpt-test", "usage": _OPENAI_CALL},
    ]


async def test_a_stream_cut_while_the_primary_answered_goes_to_the_run_model(_prices_by_model):
    """A fallback answered the first part, the primary serves the continuation
    and the user stops it mid-answer: no fallback noted a model, so its usage
    goes to the run's model, the one the continuation's generation keeps. It
    went to the fallback, the last model of that usage convention, and the
    root trace priced every call at the run's model."""
    cancel = asyncio.Event()
    cut = {"prompt_tokens": 12_000, "completion_tokens": 400, "total_tokens": 12_400}

    async def _stop_in_the_primary(chunk: str) -> None:
        if chunk == "The primary resumes":
            cancel.set()

    primary = _Served(
        "openai", "gpt-test", streams=[_overloaded(), (["The primary resumes", " and goes on."], cut, "end_turn")]
    )
    fallback = _Served("azure_openai", "gpt-fallback", streams=[(["Part one"], _OPENAI_CALL, "max_tokens")])
    root = _recording_root()

    response = await _run_recorded(
        root,
        model="gpt-test",
        llm=_chain(primary, fallback),
        messages=[ChatMessage(role="user", content="Write the report.")],
        max_iterations=3,
        on_token=_stop_in_the_primary,
        cancel_event=cancel,
    )

    assert response.stop_reason == "cancelled"
    assert response.usage[lft.USAGE_PARTS_KEY] == [
        {"model": "gpt-fallback", "usage": _OPENAI_CALL},
        {"model": "gpt-test", "usage": cut},
    ]
    trace_row, generations = await _priced(root, response)
    assert [generation.model for generation in generations] == ["gpt-fallback", "gpt-test"]
    assert trace_row.total_cost == sum(generation.total_cost for generation in generations)


@pytest.mark.parametrize(
    ("noted", "usage"),
    [(None, _ANTHROPIC_CALL), ("gpt-test", _OPENAI_CALL)],
    ids=["no-model-known", "fallback-noted"],
)
def test_an_aborted_call_hands_its_usage_over_with_the_model_its_sink_knows(noted, usage):
    """A call that never returned names its model only when a failover
    fallback noted it on the call's sink; otherwise the hook gets ``None``
    and the run's model is used, the one the call's generation keeps."""
    sink = StreamUsageSink()
    sink.report(dict(usage))
    if noted:
        sink.note_served_model(noted)
    handed: list[tuple[dict, str | None]] = []

    al._end_aborted_generation(
        None, TimeoutError("slow"), sink, lambda usage, served_model=None: handed.append((usage, served_model))
    )

    assert handed == [(usage, noted)]


# ---------------------------------------------------------------------------
# A run one model served in one usage convention records what it always did
# ---------------------------------------------------------------------------

# A compatible server reporting more cached tokens than prompt tokens: its sum
# with another call does not read back like the two calls read apart.
_QUIRKY_OPENAI_CALL = {
    "prompt_tokens": 100,
    "completion_tokens": 10,
    "total_tokens": 110,
    "prompt_tokens_details": {"cached_tokens": 150},
}
# xAI leaves its reasoning tokens out of ``completion_tokens``.
_XAI_CALL = {"prompt_tokens": 800, "completion_tokens": 50, "total_tokens": 1_450}
_OPENAI_REASONING_CALL = {
    "prompt_tokens": 3_000,
    "completion_tokens": 900,
    "total_tokens": 3_900,
    "completion_tokens_details": {"reasoning_tokens": 600},
}
_BEDROCK_CALL = {"prompt_tokens": 1_200, "completion_tokens": 90, "cache_read_input_tokens": 4_000}
_ANTHROPIC_CACHE_WRITE_CALL = {"input_tokens": 2_000, "output_tokens": 300, "cache_creation_input_tokens": 1_500}
# A non-streamed response a compatible server sent without a usage object: zero
# prompt and completion tokens and no total, so another key shape than the
# calls of its run that report one.
_NO_USAGE_RESPONSE = extract_openai_response_usage(None)


def _flat_sum(*calls: dict) -> dict:
    summed: dict = {}
    for usage in calls:
        for key, value in usage.items():
            if isinstance(value, (int, float)):
                summed[key] = summed.get(key, 0) + int(value)
        al._sum_usage_details_into(summed, usage)
    return summed


@pytest.mark.parametrize(
    "calls",
    [
        pytest.param(((_ANTHROPIC_CALL, "claude-a"), (_ANTHROPIC_CACHE_WRITE_CALL, "claude-b")), id="anthropic"),
        pytest.param(((_OPENAI_CALL, "gpt-a"), (_OPENAI_REASONING_CALL, "gpt-b"), (_XAI_CALL, "grok")), id="openai"),
        pytest.param(((_GEMINI_CALL, "gemini"), (_BEDROCK_CALL, "bedrock")), id="gemini-bedrock"),
        pytest.param(((_QUIRKY_OPENAI_CALL, "compat"), (_OPENAI_CALL, "gpt-a")), id="details-above-the-count"),
        pytest.param(
            ((_QUIRKY_OPENAI_CALL, "compat"), (_OPENAI_CALL, "gpt-a"), (_NO_USAGE_RESPONSE, "gpt-a")),
            id="a-response-that-reported-no-usage",
        ),
    ],
)
def test_a_run_one_model_served_in_one_convention_never_carries_parts(calls):
    """Whatever model names its responses carry, and even with usage its flat
    sum reads back differently: its usage is the flat sum, exactly as on main."""
    summed = _parts_of(*calls)

    assert lft.USAGE_PARTS_KEY not in summed
    assert summed == _flat_sum(*(usage for usage, _model in calls))


def test_a_response_that_reported_no_usage_leaves_a_mixed_run_split():
    """Its zeros count toward no convention; the two providers' calls still do."""
    summed = _parts_of((_ANTHROPIC_CALL, "claude-test"), (_OPENAI_CALL, "gpt-test"), (_NO_USAGE_RESPONSE, "gpt-test"))

    assert lft.USAGE_PARTS_KEY in summed
    assert lft.usage_token_totals(summed) == (31_000, 2_500)


@pytest.mark.parametrize(
    ("anthropic", "has_parts"),
    [
        # Copilot's example: a prompt read entirely from the cache still
        # produced output, which the flat sum reads instead of Gemini's.
        ({"input_tokens": 0, "output_tokens": 400, "cache_read_input_tokens": 20_000}, True),
        # A call cut before any output with its whole prompt cached shadows
        # nothing: the flat sum reads back exactly and is recorded as on main.
        ({"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 20_000}, False),
    ],
)
def test_a_mixed_run_carries_parts_exactly_when_its_flat_sum_loses_tokens(anthropic, has_parts):
    summed = _parts_of((anthropic, "claude-test"), (_GEMINI_CALL, "gemini-test"))

    assert (lft.USAGE_PARTS_KEY in summed) is has_parts
    # Either way the run reads back as its two calls read apart.
    assert lft._normalize_usage(summed) == lft._sum_normalized_usage(
        lft._normalize_usage(usage) for usage in (anthropic, _GEMINI_CALL)
    )


async def test_a_payload_an_older_replica_queued_is_priced_as_before(_prices_by_model):
    """A trace an older replica ended during a rolling deploy carries no parts:
    the sink prices its summed usage at its model, as it always did."""
    flat = lft._normalize_usage(_flat_sum(_ANTHROPIC_CALL, _OPENAI_CALL))
    trace = _ended_trace(_mixed_run_usage())
    trace.usage_details = flat
    session = _FakeSession()

    await LocalPGSink(session_factory=lambda: None)._flush_trace_with_session(session, trace, [])

    assert session.added[0].total_cost == calculate_costs(flat, _PRICES["claude-test"])["total_cost"]


async def test_the_clickhouse_mirror_writes_the_summed_token_columns():
    """ClickHouse prices no live trace (no price row): a mixed root is written
    with NULL costs like every live root, and its token columns are the sums."""
    inserted: dict[str, list[dict]] = {}

    class _Client:
        async def insert_json_rows(self, table: str, rows: list[dict]) -> None:
            inserted.setdefault(table, []).extend(rows)

        async def close(self) -> None:
            return None

    async def _factory() -> _Client:
        return _Client()

    sink = ClickHouseSink(client_factory=_factory)
    await sink.flush_trace(_ended_trace(_mixed_run_usage()), [])

    assert sink.tracing_errors == 0
    (row,) = inserted["llm_traces"]
    assert (row["input_tokens"], row["output_tokens"], row["total_tokens"]) == (31_000, 2_500, 33_500)
    assert (row["input_cost"], row["output_cost"], row["total_cost"]) == (None, None, None)


# ---------------------------------------------------------------------------
# A run two models served in one usage convention is priced per model
# ---------------------------------------------------------------------------


async def test_a_gemini_and_bedrock_run_costs_what_its_generations_cost(_prices_by_model):
    """Gemini answers with cached prompt tokens, then fails over to Bedrock.

    Both report prompt and completion tokens without a total: one convention,
    so the run's usage was only its flat sum, priced at the root model's price
    row, where Bedrock's cache-read bucket also dropped the cached tokens Gemini
    counts inside its prompt tokens."""
    composite = _executor()
    primary = _Served("gemini", "gemini-test", chats=[_tool_turn("gemini-test", _GEMINI_CALL, "c1"), _overloaded()])
    fallback = _Served("bedrock", "bedrock-test", chats=[_reply("bedrock-test", _BEDROCK_CALL)])
    root = _recording_root()

    response = await _run_recorded(
        root,
        model="gemini-test",
        llm=_chain(primary, fallback),
        messages=[ChatMessage(role="user", content="Investigate.")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
    )

    trace_row, generations = await _priced(root, response)
    # 2k in @ $1.25 + 1k cached @ $0.25 + 700 out @ $10, then 1.2k in @ $0.80
    # + 4k cache read @ $0.08 + 90 out @ $4, per 1M tokens.
    assert [(generation.model, generation.total_cost) for generation in generations] == [
        ("gemini-test", Decimal("0.00975")),
        ("bedrock-test", Decimal("0.00164")),
    ]
    # Before: $0.00604, every token at Bedrock's rates and Gemini's cached ones
    # at $0.
    assert trace_row.total_cost == sum(generation.total_cost for generation in generations)
    # The tokens it records are the flat sum's: only the price changed.
    assert (trace_row.input_tokens, trace_row.output_tokens, trace_row.total_tokens) == (8_200, 790, 8_990)
    assert response.usage[lft.USAGE_PARTS_KEY] == [
        {"model": "gemini-test", "usage": _GEMINI_CALL},
        {"model": "bedrock-test", "usage": _BEDROCK_CALL},
    ]


async def test_a_fallback_serving_the_run_model_leaves_the_run_whole(_quiet_langfuse):
    """Another provider serves the run's own model: one model, one price row,
    whatever dated name the fallback's response gives it."""
    composite = _executor()
    primary = _Served("openai", "gpt-test", chats=[_tool_turn("gpt-test", _OPENAI_CALL, "c1"), _overloaded()])
    fallback = _Served("azure_openai", "gpt-test", chats=[_reply("gpt-test-2026-01-01", _OPENAI_CALL)])

    response = await al.run_agent_loop(
        llm=_chain(primary, fallback),
        messages=[ChatMessage(role="user", content="Investigate.")],
        tools=composite.all_tools,
        executor=composite,
        model="gpt-test",
        max_iterations=5,
    )

    assert response.usage == _flat_sum(_OPENAI_CALL, _OPENAI_CALL)


@pytest.mark.parametrize(
    ("calls", "split"),
    [
        # A non-streamed call names the API's dated model, a streamed one the
        # model it was asked for.
        pytest.param(
            ((_OPENAI_CALL, "gpt-test-2026-01-01"), (_OPENAI_CALL, "gpt-test")), False, id="one-model-two-names"
        ),
        pytest.param(
            ((_OPENAI_CALL, "gpt-test"), (_OPENAI_CALL, "gpt-test-2026-01-01", "run-model")),
            False,
            id="the-run-model-on-a-fallback",
        ),
        # A fallback whose response reported no token served none.
        pytest.param(
            (
                (_GEMINI_CALL, "gemini-test"),
                (_GEMINI_CALL, "gemini-test-001"),
                (_NO_USAGE_RESPONSE, "gpt-test", "gpt-test"),
            ),
            False,
            id="a-fallback-that-reported-no-usage",
        ),
        pytest.param(
            ((_GEMINI_CALL, "gemini-test"), (_BEDROCK_CALL, "bedrock-test", "bedrock-test")),
            True,
            id="a-fallback-on-another-model",
        ),
    ],
)
def test_a_call_counts_toward_the_model_it_was_made_with(calls, split):
    """Never toward the name its response carries."""
    summed = _parts_of(*calls)

    assert (lft.USAGE_PARTS_KEY in summed) is split
    assert {key: value for key, value in summed.items() if key != lft.USAGE_PARTS_KEY} == _flat_sum(
        *(usage for usage, *_models in calls)
    )


def test_a_budget_downgrade_splits_a_run_of_one_convention():
    """The run's model is replaced mid-run: the calls made after it
    count toward the model the run moved to."""
    parts = al._RunUsageParts()
    summed = _flat_sum(_ANTHROPIC_CALL, _ANTHROPIC_CACHE_WRITE_CALL)
    parts.add(dict(_ANTHROPIC_CALL), "claude-test", "claude-test")
    parts.add(dict(_ANTHROPIC_CACHE_WRITE_CALL), "claude-other", "claude-other")
    parts.attach(summed)

    assert summed[lft.USAGE_PARTS_KEY] == [
        {"model": "claude-test", "usage": _ANTHROPIC_CALL},
        {"model": "claude-other", "usage": _ANTHROPIC_CACHE_WRITE_CALL},
    ]
