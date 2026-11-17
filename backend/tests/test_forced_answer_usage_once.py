"""The forced-final-answer backstop records each of its LLM calls once.

When a run ends in the backstop, the synthesis call writes the answer. Its
usage must reach every place a call's usage is recorded, exactly once, like an
in-loop call does:

* the loop's usage hook ``_merge_usage``, which debits the run budget
  (``RunBudget.record_llm_usage``) and adds up the usage the run reports (the
  root trace, ``AssignmentRun.total_*_tokens``);
* the trace generation spans: one generation per call, carrying that call's own
  usage (a Langfuse trace total is the sum of its generations).

``_continue_truncated_response`` returns the SAME response object with the
continuation's text appended, and each continuation's usage flows through
``merge_usage`` alone, so the response's ``usage`` keeps the first call's
figures. The backstop merged that usage before continuing and again at its end:
a truncated synthesis was debited twice. A synthesis too short to keep was
replaced by its stronger-prompt retry before its usage was ever merged: it was
never debited. And the backstop opened no generation at all.

The run reaches the backstop through a one-call tool budget (the next call is
refused and the run synthesises). The scripted provider implements ``chat()``
only, so the plain backstop calls it directly and the streamed backstop reaches
it through the base class's streaming fallback. The recorded numbers are read
where they land: a real ``RunBudget``, and ``LocalPGSink`` pricing the flushed
generation spans and root trace.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse, retry_llm_call, usage_sink_of
from app.services import langfuse_tracing as lft
from app.services.tracing.cost_calculator import ModelPrice, calculate_costs
from app.services.tracing.local_sink import LocalPGSink
from app.tools import agent_loop as al
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
)
from app.tools.run_budget import RunBudget, count_usage_tokens

# Long enough (>= ``_MIN_FORCED_ANSWER_CHARS``) for the backstop to keep it.
_LONG = "The lookup found what the run needed, and this is the answer. " * 3
_SHORT = "Too short."

_TOOL_CALL = {"input_tokens": 1_000, "output_tokens": 100}
_SYNTHESIS = {"input_tokens": 4_000, "output_tokens": 2_000}
_CONTINUATION = {"input_tokens": 6_100, "output_tokens": 500}
_RETRY = {"input_tokens": 4_300, "output_tokens": 900}
_RETRY_CONTINUATION = {"input_tokens": 5_300, "output_tokens": 300}
# What a streamed synthesis reported before its stream broke.
_SYNTHESIS_REPORTED = {"input_tokens": 4_000, "output_tokens": 40}

_TOOL_GENERATION = "llm_call"
_SYNTHESIS_GENERATION = "forced_final_answer"
_RETRY_GENERATION = "forced_final_answer:retry"
_CONTINUATION_GENERATION = "attempt:continuation:1"


def _phase(messages: list[ChatMessage], tools) -> str:
    contents = [m.content for m in messages]
    if tools:
        return "tools"
    if contents and contents[-1] == al._CONTINUATION_PROMPT:
        return "continuation"
    if al._FORCED_ANSWER_RETRY in contents:
        return "retry"
    if al._FORCED_FINAL_ANSWER in contents:
        return "synthesis"
    return "other"


class _ScriptedLLM(BaseLLMProvider):
    """Serves one scripted response (or raises one scripted error) per call,
    and records which phase of the run each call belongs to."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self, script: list[ChatResponse | Exception]) -> None:
        self._script = list(script)
        self.phases: list[str] = []

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self.phases.append(_phase(messages, tools))
        if not self._script:
            return ChatResponse(content="", model="stub")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


def _tool_response() -> ChatResponse:
    return ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_use",
        tool_calls=[ToolCall(id="c1", name="lookup", arguments={"q": "x"})],
        usage=dict(_TOOL_CALL),
    )


def _answer(content: str, usage: dict, *, truncated: bool = False) -> ChatResponse:
    return ChatResponse(
        content=content,
        model="stub",
        stop_reason="max_tokens" if truncated else "end_turn",
        usage=dict(usage),
    )


def _price() -> ModelPrice:
    return ModelPrice(
        provider="stub",
        model_id="stub",
        model_pattern=None,
        input_price_per_1m=Decimal("3"),
        output_price_per_1m=Decimal("15"),
        cache_read_price_per_1m=None,
        cache_write_price_per_1m=None,
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )


class _RecordingBudget(RunBudget):
    """A real run budget that also keeps every debit, in order.

    A usage with nothing in it debits nothing and is not kept: when a
    synthesis call fails, the backstop goes on with an empty placeholder
    response, and records it like any response it holds.
    """

    def __init__(self) -> None:
        # One tool call: the next call is refused and the run synthesises.
        super().__init__(max_tool_calls=1, price=_price())
        self.debits: list[dict] = []

    def record_llm_usage(self, usage: dict | None) -> None:
        if usage:
            self.debits.append(dict(usage))
        super().record_llm_usage(usage)


def _root_trace_ctx() -> dict:
    return {
        "trace": None,
        "generation": None,
        "client": None,
        "parent_ctx": None,
        "metadata": {"source": "test"},
        "input_data": {"messages": []},
        "local_spans": [],
        "local_span_counter": 0,
        "local_trace_id": uuid.uuid4(),
        "local_source": "agent_chat",
        "local_name": "trace",
        "local_started_at": datetime.now(UTC),
        "local_metadata": {"source": "test"},
        "local_tags": [],
    }


def _lookup_tools() -> tuple[list[ToolDefinition], CompositeToolExecutor]:
    executor = CallableToolExecutor()

    async def _lookup(**_kwargs) -> str:
        return "found it"

    executor.register("lookup", _lookup)
    definitions = [ToolDefinition(name="lookup", description="lookup", source="custom:test")]
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, executor)
    return definitions, composite


async def _collect_token(_chunk: str) -> None:
    return None


_PLAIN_AND_STREAMED = pytest.mark.parametrize("on_token", [None, _collect_token], ids=["plain", "streamed"])


async def _run(llm, budget: RunBudget, root: dict, on_token, cancel_event: asyncio.Event | None = None) -> ChatResponse:
    definitions, composite = _lookup_tools()
    token = lft.set_current_trace_ctx(root)
    try:
        return await al.run_agent_loop(
            llm=llm,
            messages=[ChatMessage(role="user", content="go")],
            tools=definitions,
            executor=composite,
            max_iterations=10,
            max_tokens=1_024,
            progressive_tools=False,
            on_token=on_token,
            budget=budget,
            cancel_event=cancel_event,
        )
    finally:
        lft.reset_current_trace_ctx(token)


class _FakeSession:
    def __init__(self) -> None:
        self.added: list = []
        self.added_all: list = []

    def add(self, row) -> None:
        self.added.append(row)

    def add_all(self, rows) -> None:
        self.added_all.extend(rows)

    async def flush(self) -> None:
        return None


async def _flush(root: dict, response: ChatResponse):
    """End the root trace like a caller does and price it in ``LocalPGSink``."""
    with patch("app.services.tracing.dispatcher.submit_trace_for_flush") as submit:
        lft.end_trace(root, output=response.content, usage=response.usage, model="stub")
    trace, spans = submit.call_args.args
    session = _FakeSession()
    price = AsyncMock(return_value=_price())
    with (
        patch("app.services.tracing.local_sink.resolve_model_price", price),
        patch("app.services.tracing.local_sink.resolve_model_price_any", price),
    ):
        await LocalPGSink(session_factory=lambda: None)._flush_trace_with_session(session, trace, spans)
    trace_row = session.added[0]
    generations = [row for row in session.added_all if row.type == "GENERATION"]
    return trace_row, generations


def _total(*usages: dict) -> dict:
    return {
        "input_tokens": sum(u["input_tokens"] for u in usages),
        "output_tokens": sum(u["output_tokens"] for u in usages),
    }


def _assert_debited_once(response: ChatResponse, budget: _RecordingBudget, *usages: dict) -> None:
    """Each call went through the usage hook once: one debit per call, in
    order, and the usage the run reports is their sum."""
    total = _total(*usages)
    assert budget.debits == list(usages)
    assert budget.spent_tokens == count_usage_tokens(total)
    assert budget.spent_cost_usd == calculate_costs(total, _price())["total_cost"]
    assert response.usage == total


# ---------------------------------------------------------------------------
# The usage hook: run budget and the usage the run reports
# ---------------------------------------------------------------------------


@_PLAIN_AND_STREAMED
async def test_a_truncated_synthesis_is_debited_once(on_token):
    llm = _ScriptedLLM(
        [
            _tool_response(),
            _answer(_LONG, _SYNTHESIS, truncated=True),
            _answer(" And the rest of it.", _CONTINUATION),
        ]
    )
    budget = _RecordingBudget()

    response = await _run(llm, budget, _root_trace_ctx(), on_token)

    assert llm.phases == ["tools", "synthesis", "continuation"]
    assert response.content.endswith("And the rest of it.")
    _assert_debited_once(response, budget, _TOOL_CALL, _SYNTHESIS, _CONTINUATION)


@_PLAIN_AND_STREAMED
async def test_a_synthesis_replaced_by_its_retry_is_still_debited(on_token):
    llm = _ScriptedLLM(
        [
            _tool_response(),
            _answer(_SHORT, _SYNTHESIS),
            _answer(_LONG, _RETRY),
        ]
    )
    budget = _RecordingBudget()

    response = await _run(llm, budget, _root_trace_ctx(), on_token)

    assert llm.phases == ["tools", "synthesis", "retry"]
    assert response.content.strip() == _LONG.strip()
    # The short synthesis was billed, so it is debited too.
    _assert_debited_once(response, budget, _TOOL_CALL, _SYNTHESIS, _RETRY)


@_PLAIN_AND_STREAMED
async def test_a_truncated_retry_is_debited_once(on_token):
    llm = _ScriptedLLM(
        [
            _tool_response(),
            _answer(_SHORT, _SYNTHESIS),
            _answer(_LONG, _RETRY, truncated=True),
            _answer(" And the rest of it.", _RETRY_CONTINUATION),
        ]
    )
    budget = _RecordingBudget()

    response = await _run(llm, budget, _root_trace_ctx(), on_token)

    assert llm.phases == ["tools", "synthesis", "retry", "continuation"]
    _assert_debited_once(response, budget, _TOOL_CALL, _SYNTHESIS, _RETRY, _RETRY_CONTINUATION)


@_PLAIN_AND_STREAMED
async def test_a_plain_synthesis_records_what_it_recorded_before(on_token):
    """The common case, a synthesis kept as it came: the same numbers as
    before the fix, recorded the same way."""
    llm = _ScriptedLLM([_tool_response(), _answer(_LONG, _SYNTHESIS)])
    budget = _RecordingBudget()

    response = await _run(llm, budget, _root_trace_ctx(), on_token)

    assert llm.phases == ["tools", "synthesis"]
    assert response.content.strip() == _LONG.strip()
    _assert_debited_once(response, budget, _TOOL_CALL, _SYNTHESIS)


# ---------------------------------------------------------------------------
# The trace: one generation per call, and the root trace agreeing with them
# ---------------------------------------------------------------------------


# Each scenario builds its script afresh: the loop appends a continuation to
# the response object it was handed and replaces its usage.
_SCENARIOS = {
    "plain": (
        lambda: [_tool_response(), _answer(_LONG, _SYNTHESIS)],
        [(_TOOL_GENERATION, _TOOL_CALL), (_SYNTHESIS_GENERATION, _SYNTHESIS)],
    ),
    "truncated": (
        lambda: [
            _tool_response(),
            _answer(_LONG, _SYNTHESIS, truncated=True),
            _answer(" And the rest of it.", _CONTINUATION),
        ],
        [
            (_TOOL_GENERATION, _TOOL_CALL),
            (_SYNTHESIS_GENERATION, _SYNTHESIS),
            (_CONTINUATION_GENERATION, _CONTINUATION),
        ],
    ),
    "retried": (
        lambda: [_tool_response(), _answer(_SHORT, _SYNTHESIS), _answer(_LONG, _RETRY)],
        [
            (_TOOL_GENERATION, _TOOL_CALL),
            (_SYNTHESIS_GENERATION, _SYNTHESIS),
            (_RETRY_GENERATION, _RETRY),
        ],
    ),
    "retried_and_truncated": (
        lambda: [
            _tool_response(),
            _answer(_SHORT, _SYNTHESIS),
            _answer(_LONG, _RETRY, truncated=True),
            _answer(" And the rest of it.", _RETRY_CONTINUATION),
        ],
        [
            (_TOOL_GENERATION, _TOOL_CALL),
            (_SYNTHESIS_GENERATION, _SYNTHESIS),
            (_RETRY_GENERATION, _RETRY),
            (_CONTINUATION_GENERATION, _RETRY_CONTINUATION),
        ],
    ),
}


@_PLAIN_AND_STREAMED
@pytest.mark.parametrize("scenario", list(_SCENARIOS))
async def test_each_backstop_call_is_one_generation_carrying_its_own_usage(scenario, on_token):
    build_script, calls = _SCENARIOS[scenario]
    llm = _ScriptedLLM(build_script())
    budget = _RecordingBudget()
    root = _root_trace_ctx()

    response = await _run(llm, budget, root, on_token)
    trace_row, generations = await _flush(root, response)

    # The backstop opens a generation too, not only the tool call's.
    assert [(g.name, g.input_tokens, g.output_tokens) for g in generations] == [
        (name, usage["input_tokens"], usage["output_tokens"]) for name, usage in calls
    ]
    for generation, (_name, usage) in zip(generations, calls, strict=True):
        assert generation.total_cost == calculate_costs(lft._normalize_usage(usage), _price())["total_cost"]
    # The root trace is priced from the run's usage, which now adds up to
    # exactly the generations: the trace and its spans agree.
    total = _total(*(usage for _name, usage in calls))
    assert (trace_row.input_tokens, trace_row.output_tokens) == (
        total["input_tokens"],
        total["output_tokens"],
    )
    assert trace_row.total_cost == sum(g.total_cost for g in generations)


@pytest.mark.parametrize(
    "error",
    [RuntimeError("provider down"), TimeoutError("synthesis timed out")],
    ids=["failure", "timeout"],
)
async def test_a_failed_synthesis_closes_its_generation_with_the_error(error):
    """A synthesis that raises or times out records nothing it never
    received, and its generation is closed with the error rather than left
    open."""
    llm = _ScriptedLLM([_tool_response(), error, _answer(_LONG, _RETRY)])
    budget = _RecordingBudget()
    root = _root_trace_ctx()

    response = await _run(llm, budget, root, None)
    _trace_row, generations = await _flush(root, response)

    assert llm.phases == ["tools", "synthesis", "retry"]
    _assert_debited_once(response, budget, _TOOL_CALL, _RETRY)
    assert [g.name for g in generations] == [
        _TOOL_GENERATION,
        _SYNTHESIS_GENERATION,
        _RETRY_GENERATION,
    ]
    _tool, failed, retry = generations
    assert failed.level == "ERROR"
    assert failed.status_message == f"{type(error).__name__}: {error}"
    assert failed.usage_details is None
    assert (retry.input_tokens, retry.output_tokens) == (
        _RETRY["input_tokens"],
        _RETRY["output_tokens"],
    )


async def test_a_cancelled_synthesis_closes_its_generation_and_propagates():
    started = asyncio.Event()

    class _HangingSynthesis(_ScriptedLLM):
        async def chat(self, messages, model=None, tools=None, **kwargs):
            if _phase(messages, tools) == "synthesis":
                self.phases.append("synthesis")
                started.set()
                await asyncio.Event().wait()
            return await super().chat(messages, model=model, tools=tools, **kwargs)

    llm = _HangingSynthesis([_tool_response()])
    budget = _RecordingBudget()
    root = _root_trace_ctx()

    task = asyncio.create_task(_run(llm, budget, root, None))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert budget.debits == [_TOOL_CALL]
    synthesis = [
        span for span in root["local_spans"] if span["type"] == "GENERATION" and span["name"] == _SYNTHESIS_GENERATION
    ]
    assert len(synthesis) == 1
    assert synthesis[0]["level"] == "ERROR"
    assert synthesis[0]["status_message"].startswith("CancelledError")


async def test_a_failed_retry_keeps_the_first_synthesis_once():
    """The retry that was to replace a short synthesis fails: the synthesis
    it kept is debited once, and the retry's generation is closed with the
    error and no usage."""
    llm = _ScriptedLLM([_tool_response(), _answer(_SHORT, _SYNTHESIS), RuntimeError("provider down")])
    budget = _RecordingBudget()
    root = _root_trace_ctx()

    response = await _run(llm, budget, root, None)
    trace_row, generations = await _flush(root, response)

    assert llm.phases == ["tools", "synthesis", "retry"]
    _assert_debited_once(response, budget, _TOOL_CALL, _SYNTHESIS)
    assert [(g.name, g.level) for g in generations] == [
        (_TOOL_GENERATION, "DEFAULT"),
        (_SYNTHESIS_GENERATION, "DEFAULT"),
        (_RETRY_GENERATION, "ERROR"),
    ]
    _tool, synthesis, failed = generations
    assert (synthesis.input_tokens, synthesis.output_tokens) == (
        _SYNTHESIS["input_tokens"],
        _SYNTHESIS["output_tokens"],
    )
    assert failed.status_message == "RuntimeError: provider down"
    assert failed.usage_details is None
    assert trace_row.total_cost == sum(g.total_cost for g in generations)


async def test_an_aborted_streamed_synthesis_closes_its_generation_with_what_it_reported():
    """A streamed synthesis whose stream breaks after its provider reported
    usage: its generation is closed with the error and that usage, and the
    usage is debited once."""

    class _BrokenSynthesisStream(_ScriptedLLM):
        async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
            if _phase(messages, tools) != "synthesis":
                async for chunk in super().chat_stream(
                    messages, model=model, temperature=temperature, max_tokens=max_tokens, tools=tools, **kwargs
                ):
                    yield chunk
                return
            self.phases.append("synthesis")
            usage_sink_of(kwargs).report(dict(_SYNTHESIS_REPORTED))
            yield "Partial "
            raise RuntimeError("stream dropped")

    llm = _BrokenSynthesisStream([_tool_response(), _answer(_LONG, _RETRY)])
    budget = _RecordingBudget()
    root = _root_trace_ctx()

    response = await _run(llm, budget, root, _collect_token)
    trace_row, generations = await _flush(root, response)

    assert llm.phases == ["tools", "synthesis", "retry"]
    assert response.content.strip() == _LONG.strip()
    _assert_debited_once(response, budget, _TOOL_CALL, _SYNTHESIS_REPORTED, _RETRY)
    assert [(g.name, g.input_tokens, g.output_tokens) for g in generations] == [
        (_TOOL_GENERATION, _TOOL_CALL["input_tokens"], _TOOL_CALL["output_tokens"]),
        (_SYNTHESIS_GENERATION, _SYNTHESIS_REPORTED["input_tokens"], _SYNTHESIS_REPORTED["output_tokens"]),
        (_RETRY_GENERATION, _RETRY["input_tokens"], _RETRY["output_tokens"]),
    ]
    broken = generations[1]
    assert broken.level == "ERROR"
    assert broken.status_message == "RuntimeError: stream dropped"
    assert trace_row.total_cost == sum(g.total_cost for g in generations)


@pytest.mark.parametrize("stopped", ["synthesis", "retry"])
async def test_a_user_stop_closes_the_stopped_call_and_keeps_what_the_run_spent(stopped):
    """The user stops the run during the synthesis or its retry: that call's
    generation is closed with the cancellation, and the cancelled response
    carries what the run spent until then, each call once."""
    cancel = asyncio.Event()

    class _StoppedByTheUser(_ScriptedLLM):
        async def chat(self, messages, model=None, tools=None, **kwargs):
            if _phase(messages, tools) != stopped:
                return await super().chat(messages, model=model, tools=tools, **kwargs)
            self.phases.append(stopped)
            cancel.set()
            await asyncio.Event().wait()

    before = [_tool_response()] if stopped == "synthesis" else [_tool_response(), _answer(_SHORT, _SYNTHESIS)]
    llm = _StoppedByTheUser(before)
    budget = _RecordingBudget()
    root = _root_trace_ctx()

    response = await _run(llm, budget, root, None, cancel_event=cancel)

    assert response.stop_reason == "cancelled"
    spent = [_TOOL_CALL] if stopped == "synthesis" else [_TOOL_CALL, _SYNTHESIS]
    _assert_debited_once(response, budget, *spent)
    name = _SYNTHESIS_GENERATION if stopped == "synthesis" else _RETRY_GENERATION
    (closed,) = [span for span in root["local_spans"] if span["type"] == "GENERATION" and span["name"] == name]
    assert closed["level"] == "ERROR"
    assert closed["status_message"].startswith("_CancelledMidCallError")


class _TransientError(Exception):
    pass


def _observation(name: str, registry: list[MagicMock]) -> MagicMock:
    """A Langfuse observation stand-in whose children are recorded by name."""
    observation = MagicMock()
    observation.observation_name = name

    def _start_observation(**kwargs):
        child = _observation(kwargs.get("name"), registry)
        registry.append(child)
        return child

    observation.start_observation.side_effect = _start_observation
    return observation


async def test_a_synthesis_retried_by_its_provider_is_one_call_with_an_attempt_event():
    """A provider retry inside the synthesis call is an attempt event on that
    call's generation, and nowhere else; the call is debited once, with the
    usage of the response it returned."""

    class _RetriedSynthesis(_ScriptedLLM):
        attempts = 0

        async def chat(self, messages, model=None, tools=None, **kwargs):
            if _phase(messages, tools) != "synthesis":
                return await super().chat(messages, model=model, tools=tools, **kwargs)
            self.phases.append("synthesis")

            async def _attempt() -> ChatResponse:
                self.attempts += 1
                if self.attempts == 1:
                    raise _TransientError("overloaded")
                return _answer(_LONG, _SYNTHESIS)

            return await retry_llm_call(
                _attempt, is_retryable=lambda exc: isinstance(exc, _TransientError), base_delay=0.0
            )

    llm = _RetriedSynthesis([_tool_response()])
    budget = _RecordingBudget()
    observations: list[MagicMock] = []
    root = {**_root_trace_ctx(), "trace": _observation("trace", observations)}

    response = await _run(llm, budget, root, None)

    assert llm.phases == ["tools", "synthesis"]
    assert llm.attempts == 2
    _assert_debited_once(response, budget, _TOOL_CALL, _SYNTHESIS)
    retried = {o.observation_name: [c.kwargs["name"] for c in o.create_event.call_args_list] for o in observations}
    assert retried.pop(_SYNTHESIS_GENERATION) == ["attempt:retry:1"]
    assert all(events == [] for events in retried.values())
