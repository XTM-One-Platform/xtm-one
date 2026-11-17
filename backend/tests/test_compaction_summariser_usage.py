"""The in-loop compaction summariser records its usage once.

Semantic compaction (``_semantic_compact_session_context``) distils the older
turns of a run into one summary with a single ``llm.chat`` call, the
summariser. The loop makes it from the proactive near-window gate, from the
empty-response recovery (``_compact_for_empty_recovery``) and from the
forced-answer backstop. The provider bills that call like any other, so its
usage must reach every place a call's usage is recorded, exactly once, like an
in-loop call:

* the loop's usage hook ``_merge_usage``, which debits the run budget
  (``RunBudget.record_llm_usage``) and adds up the usage the run reports (the
  root trace, ``AssignmentRun.total_*_tokens``);
* a generation span of its own carrying that call's usage (a Langfuse trace
  total is the sum of its generations).

Before the fix the summariser's usage was dropped: no debit, no generation, a
run total short of what the provider billed.

The provider is scripted (no network, no key): ``chat()`` answers the
summariser request, recognised by the compaction system prompt, and the run's
own calls from two separate scripts. The numbers are read where they land: a
real ``RunBudget`` (every debit recorded, in order) and ``LocalPGSink`` pricing
the flushed generation spans and root trace.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse, retry_llm_call
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
from app.tools.run_budget import MIN_OUTPUT_TOKENS, BudgetFallbackModel, RunBudget, count_usage_tokens

# The generation the summariser call is recorded as.
_COMPACTION = "context_compaction"

_SUMMARY = {"input_tokens": 7_000, "output_tokens": 600}
_SECOND_SUMMARY = {"input_tokens": 5_500, "output_tokens": 450}
_EMPTY_SUMMARY = {"input_tokens": 6_800, "output_tokens": 3}
_ANSWER = {"input_tokens": 9_000, "output_tokens": 700}
_TOOL_CALL = {"input_tokens": 2_000, "output_tokens": 150}
_SYNTHESIS = {"input_tokens": 4_000, "output_tokens": 20}
_RETRY = {"input_tokens": 4_300, "output_tokens": 900}

# Long enough for the backstop to keep it (>= ``_MIN_FORCED_ANSWER_CHARS``).
_LONG = "The incident was traced to a failed deploy, and this is the full report. " * 3
_SHORT = "Too short."
_SUMMARY_TEXT = "Prior work: the failed deploy of build 4.2.1 was isolated and rolled back. " * 35

# Small enough that the proactive gate compacts a history of a few dozen
# 3,500-character turns; the window stays far away so nothing else fires.
_SESSION_BUDGET = 20_000


def _phase(messages: list[ChatMessage], tools) -> str:
    if messages and messages[0].role == "system" and messages[0].content == al._IN_LOOP_COMPACTION_SYSTEM_PROMPT:
        return "compaction"
    contents = [m.content for m in messages]
    if tools:
        return "tools"
    if al._FORCED_ANSWER_RETRY in contents:
        return "retry"
    if al._FORCED_FINAL_ANSWER in contents:
        return "synthesis"
    return "answer"


class _ScriptedLLM(BaseLLMProvider):
    """Answers the summariser from one script and the run's own calls from
    another, and records which phase of the run each call belongs to."""

    provider_name = "stub"
    context_window = 200_000

    def __init__(
        self,
        *,
        calls: list[ChatResponse | BaseException],
        summaries: list[ChatResponse | BaseException],
    ) -> None:
        self._calls = list(calls)
        self._summaries = list(summaries)
        self.phases: list[str] = []
        self.max_tokens: list[int | None] = []

    async def chat(self, messages, model=None, tools=None, **kwargs):
        phase = _phase(messages, tools)
        self.phases.append(phase)
        self.max_tokens.append(kwargs.get("max_tokens"))
        script = self._summaries if phase == "compaction" else self._calls
        if not script:
            return ChatResponse(content="", model="stub")
        item = script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


def _summary(usage: dict, content: str = _SUMMARY_TEXT) -> ChatResponse:
    return ChatResponse(content=content, model="stub", stop_reason="end_turn", usage=dict(usage))


def _answer(content: str, usage: dict) -> ChatResponse:
    return ChatResponse(content=content, model="stub", stop_reason="end_turn", usage=dict(usage))


def _tool_response() -> ChatResponse:
    return ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_use",
        tool_calls=[ToolCall(id="c1", name="lookup", arguments={"q": "deploys"})],
        usage=dict(_TOOL_CALL),
    )


def _history(turns: int, size: int = 3_500) -> list[ChatMessage]:
    """A persona, the task, then *turns* older assistant turns to compact."""
    history = [
        ChatMessage(role="system", content="You are a careful incident analyst."),
        ChatMessage(role="user", content="Investigate the incident and report."),
    ]
    history.extend(ChatMessage(role="assistant", content=f"turn-{i}: " + "x" * size) for i in range(turns))
    return history


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
    """A real run budget that also keeps every debit, in order."""

    def __init__(self, **limits) -> None:
        super().__init__(price=_price(), **(limits or {"max_total_tokens": 10_000_000}))
        self.debits: list[dict] = []

    def record_llm_usage(self, usage: dict | None) -> None:
        self.debits.append(dict(usage or {}))
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
        return "found it: " + "y" * 3_000

    executor.register("lookup", _lookup)
    definitions = [ToolDefinition(name="lookup", description="lookup", source="custom:test")]
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, executor)
    return definitions, composite


async def _collect_token(_chunk: str) -> None:
    return None


_PLAIN_AND_STREAMED = pytest.mark.parametrize("on_token", [None, _collect_token], ids=["plain", "streamed"])


async def _run(
    llm: BaseLLMProvider,
    budget: RunBudget | None,
    root: dict,
    messages: list[ChatMessage],
    *,
    on_token=None,
    on_event=None,
    with_tools: bool = False,
    max_session_chars: int | None = _SESSION_BUDGET,
    max_total_tokens: int | None = None,
    max_tokens: int = 1_024,
) -> ChatResponse:
    definitions, composite = _lookup_tools() if with_tools else (None, None)
    token = lft.set_current_trace_ctx(root)
    try:
        return await al.run_agent_loop(
            llm=llm,
            messages=messages,
            tools=definitions,
            executor=composite,
            max_iterations=10,
            max_tokens=max_tokens,
            max_session_chars=max_session_chars,
            progressive_tools=False,
            on_token=on_token,
            on_event=on_event,
            max_total_tokens=max_total_tokens,
            budget=budget,
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
    return session.added[0], session.added_all


def _total(*usages: dict) -> dict:
    return {
        "input_tokens": sum(u["input_tokens"] for u in usages),
        "output_tokens": sum(u["output_tokens"] for u in usages),
    }


def _compaction_spans(root: dict) -> list[dict]:
    return [s for s in root["local_spans"] if s["type"] == "GENERATION" and s["name"] == _COMPACTION]


def _compacted(messages: list[ChatMessage]) -> bool:
    return any(al._SEMANTIC_COMPACTION_MARKER in (m.content or "") for m in messages)


# ---------------------------------------------------------------------------
# A run that compacts once
# ---------------------------------------------------------------------------


@_PLAIN_AND_STREAMED
async def test_a_run_that_compacts_once_records_the_summariser_once(on_token):
    llm = _ScriptedLLM(calls=[_answer(_LONG, _ANSWER)], summaries=[_summary(_SUMMARY)])
    budget = _RecordingBudget()
    root = _root_trace_ctx()
    messages = _history(turns=20)

    response = await _run(llm, budget, root, messages, on_token=on_token)

    assert llm.phases == ["compaction", "answer"]
    assert _compacted(messages)
    assert response.content.strip() == _LONG.strip()
    # The run budget: one debit per call, the summariser's first.
    assert budget.debits == [_SUMMARY, _ANSWER]
    total = _total(_SUMMARY, _ANSWER)
    assert budget.spent_tokens == count_usage_tokens(total)
    assert budget.spent_cost_usd == calculate_costs(total, _price())["total_cost"]
    # The usage the run reports.
    assert response.usage == total

    trace_row, rows = await _flush(root, response)
    generations = [row for row in rows if row.type == "GENERATION"]
    assert [(g.name, g.input_tokens, g.output_tokens) for g in generations] == [
        (_COMPACTION, _SUMMARY["input_tokens"], _SUMMARY["output_tokens"]),
        ("llm_call", _ANSWER["input_tokens"], _ANSWER["output_tokens"]),
    ]
    compaction = generations[0]
    # A generation of the iteration it ran in, carrying what was sent.
    rows_by_id = {row.id: row for row in rows}
    assert rows_by_id[compaction.parent_span_id].name == "iteration-1"
    assert compaction.metadata_json["observation_kind"] == _COMPACTION
    assert compaction.input["messages"][0]["content"] == al._IN_LOOP_COMPACTION_SYSTEM_PROMPT
    assert compaction.output == _SUMMARY_TEXT
    assert compaction.total_cost == calculate_costs(lft._normalize_usage(_SUMMARY), _price())["total_cost"]
    # The root trace and its generations agree.
    assert (trace_row.input_tokens, trace_row.output_tokens) == (total["input_tokens"], total["output_tokens"])
    assert trace_row.total_cost == sum(g.total_cost for g in generations)


# ---------------------------------------------------------------------------
# The forced-answer backstop
# ---------------------------------------------------------------------------


@_PLAIN_AND_STREAMED
async def test_a_compaction_in_the_backstop_is_recorded_once(on_token):
    """A synthesis too short to keep is retried after an empty-response
    compaction of the whole history: the summariser runs in the backstop,
    after the iteration spans closed."""
    llm = _ScriptedLLM(
        calls=[_tool_response(), _answer(_SHORT, _SYNTHESIS), _answer(_LONG, _RETRY)],
        summaries=[_summary(_SUMMARY)],
    )
    # One tool call: the next call is refused and the run synthesises.
    budget = _RecordingBudget(max_tool_calls=1)
    root = _root_trace_ctx()
    messages = _history(turns=22)

    # The session budget is the model's own, so nothing compacts in the loop.
    response = await _run(llm, budget, root, messages, on_token=on_token, with_tools=True, max_session_chars=None)

    assert llm.phases == ["tools", "synthesis", "compaction", "retry"]
    assert _compacted(messages)
    assert response.content.strip() == _LONG.strip()
    # Debited once, when it returned: after the calls before it, before the
    # retry it made room for. (What the backstop records for its own calls is
    # not this test's.)
    assert budget.debits.count(_SUMMARY) == 1
    assert budget.debits[0] == _TOOL_CALL
    assert budget.debits[-2:] == [_SUMMARY, _RETRY]
    assert response.usage == _total(*budget.debits)

    spans = _compaction_spans(root)
    assert len(spans) == 1
    (span,) = spans
    assert (span["input_tokens"], span["output_tokens"]) == (_SUMMARY["input_tokens"], _SUMMARY["output_tokens"])
    # No iteration span is open any more: the generation hangs off the run.
    assert span["parent_span_id"] is None
    assert span["level"] == "DEFAULT"


# ---------------------------------------------------------------------------
# A summariser that fails
# ---------------------------------------------------------------------------


async def test_a_failed_summariser_records_nothing_and_keeps_its_error(caplog):
    """The summariser raises: nothing was reported, nothing is debited, the
    generation is closed with the error and the loop falls back as before."""
    llm = _ScriptedLLM(calls=[_answer(_LONG, _ANSWER)], summaries=[RuntimeError("summariser down")])
    budget = _RecordingBudget()
    root = _root_trace_ctx()
    messages = _history(turns=20)

    with caplog.at_level(logging.WARNING, logger="app.tools.agent_loop"):
        response = await _run(llm, budget, root, messages)

    assert llm.phases == ["compaction", "answer"]
    assert not _compacted(messages)
    assert response.content.strip() == _LONG.strip()
    assert budget.debits == [_ANSWER]
    assert response.usage == _ANSWER
    # The original error is what the log reports, unchanged.
    failures = [r for r in caplog.records if r.getMessage().startswith("Semantic compaction summary call failed")]
    assert len(failures) == 1
    assert isinstance(failures[0].exc_info[1], RuntimeError)
    assert str(failures[0].exc_info[1]) == "summariser down"

    (span,) = _compaction_spans(root)
    assert span["level"] == "ERROR"
    assert span["status_message"] == "RuntimeError: summariser down"
    assert span["usage_details"] is None
    assert span["input_tokens"] is None


async def test_a_summariser_that_returns_no_summary_is_still_debited():
    """A call that returned was billed, whether or not its summary is usable:
    its usage is recorded and the loop falls back as before."""
    llm = _ScriptedLLM(calls=[_answer(_LONG, _ANSWER)], summaries=[_summary(_EMPTY_SUMMARY, content="   ")])
    budget = _RecordingBudget()
    root = _root_trace_ctx()
    messages = _history(turns=20)

    response = await _run(llm, budget, root, messages)

    assert llm.phases == ["compaction", "answer"]
    assert not _compacted(messages)
    assert budget.debits == [_EMPTY_SUMMARY, _ANSWER]
    assert response.usage == _total(_EMPTY_SUMMARY, _ANSWER)
    (span,) = _compaction_spans(root)
    assert (span["input_tokens"], span["output_tokens"]) == (
        _EMPTY_SUMMARY["input_tokens"],
        _EMPTY_SUMMARY["output_tokens"],
    )
    assert span["level"] == "WARNING"


async def test_a_cancelled_summariser_closes_its_generation_and_propagates():
    started = asyncio.Event()

    class _HangingSummariser(_ScriptedLLM):
        async def chat(self, messages, model=None, tools=None, **kwargs):
            if _phase(messages, tools) == "compaction":
                self.phases.append("compaction")
                started.set()
                await asyncio.Event().wait()
            return await super().chat(messages, model=model, tools=tools, **kwargs)

    llm = _HangingSummariser(calls=[_answer(_LONG, _ANSWER)], summaries=[])
    budget = _RecordingBudget()
    root = _root_trace_ctx()

    task = asyncio.create_task(_run(llm, budget, root, _history(turns=20)))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert llm.phases == ["compaction"]
    assert budget.debits == []
    (span,) = _compaction_spans(root)
    assert span["level"] == "ERROR"
    assert span["status_message"].startswith("CancelledError")
    assert span["usage_details"] is None


async def test_a_failing_usage_hook_never_breaks_the_compaction(caplog):
    """The usage hook raising on the summariser's usage is logged: the summary
    is still applied, its generation keeps its usage and the run answers."""

    class _FailingBudget(_RecordingBudget):
        def record_llm_usage(self, usage: dict | None) -> None:
            if usage == _SUMMARY:
                raise RuntimeError("budget store down")
            super().record_llm_usage(usage)

    llm = _ScriptedLLM(calls=[_answer(_LONG, _ANSWER)], summaries=[_summary(_SUMMARY)])
    budget = _FailingBudget()
    root = _root_trace_ctx()
    messages = _history(turns=20)

    with caplog.at_level(logging.ERROR, logger="app.tools.agent_loop"):
        response = await _run(llm, budget, root, messages)

    assert llm.phases == ["compaction", "answer"]
    assert _compacted(messages)
    assert response.content.strip() == _LONG.strip()
    assert budget.debits == [_ANSWER]
    logged = [r for r in caplog.records if r.levelno == logging.ERROR and r.exc_info]
    assert [str(r.exc_info[1]) for r in logged] == ["budget store down"]
    (span,) = _compaction_spans(root)
    assert (span["input_tokens"], span["output_tokens"]) == (_SUMMARY["input_tokens"], _SUMMARY["output_tokens"])


async def test_a_user_cancel_during_the_summariser_records_nothing():
    """A user cancel aborts the summariser like any miss: ``""`` for the
    deterministic fallback, the generation closed with the cancel, no usage
    (the call never returned)."""
    started = asyncio.Event()
    cancel = asyncio.Event()

    class _Hanging(BaseLLMProvider):
        provider_name = "stub"

        async def chat(self, messages, **kwargs):
            started.set()
            await asyncio.Event().wait()

        async def embed(self, text, model=None):
            return [0.0]

        async def list_models(self):
            return []

    root = _root_trace_ctx()
    merged: list[dict] = []
    token = lft.set_current_trace_ctx(root)
    try:
        task = asyncio.create_task(
            al._summarize_messages_for_context(
                _Hanging(),
                _history(turns=4)[1:],
                model=None,
                cancel_event=cancel,
                merge_usage=lambda usage, served_model=None: merged.append(usage),
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        cancel.set()
        summary = await asyncio.wait_for(task, timeout=5)
    finally:
        lft.reset_current_trace_ctx(token)

    assert summary == ""
    assert merged == []
    (span,) = _compaction_spans(root)
    assert span["level"] == "ERROR"
    assert span["status_message"].startswith("_CancelledMidCallError")


# ---------------------------------------------------------------------------
# Compaction again: no double count
# ---------------------------------------------------------------------------


async def test_a_second_compaction_records_its_own_call_and_never_the_first_again():
    """The history grows back over the gate after a first compaction and the
    next iteration compacts again: each summariser call is debited and traced
    once, with its own usage."""
    llm = _ScriptedLLM(
        calls=[_tool_response(), _answer(_LONG, _ANSWER)],
        summaries=[_summary(_SUMMARY), _summary(_SECOND_SUMMARY)],
    )
    budget = _RecordingBudget()
    root = _root_trace_ctx()
    messages = _history(turns=20)

    response = await _run(llm, budget, root, messages, with_tools=True)

    assert llm.phases == ["compaction", "tools", "compaction", "tools"]
    assert _compacted(messages)
    assert response.content.strip() == _LONG.strip()
    assert budget.debits == [_SUMMARY, _TOOL_CALL, _SECOND_SUMMARY, _ANSWER]
    total = _total(_SUMMARY, _TOOL_CALL, _SECOND_SUMMARY, _ANSWER)
    assert response.usage == total

    trace_row, rows = await _flush(root, response)
    generations = [row for row in rows if row.type == "GENERATION"]
    assert [(g.name, g.input_tokens, g.output_tokens) for g in generations] == [
        (_COMPACTION, _SUMMARY["input_tokens"], _SUMMARY["output_tokens"]),
        ("llm_call", _TOOL_CALL["input_tokens"], _TOOL_CALL["output_tokens"]),
        (_COMPACTION, _SECOND_SUMMARY["input_tokens"], _SECOND_SUMMARY["output_tokens"]),
        ("llm_call", _ANSWER["input_tokens"], _ANSWER["output_tokens"]),
    ]
    rows_by_id = {row.id: row for row in rows}
    assert [rows_by_id[g.parent_span_id].name for g in generations if g.name == _COMPACTION] == [
        "iteration-1",
        "iteration-2",
    ]
    assert (trace_row.input_tokens, trace_row.output_tokens) == (total["input_tokens"], total["output_tokens"])
    assert trace_row.total_cost == sum(g.total_cost for g in generations)


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


async def test_a_summariser_retried_by_its_provider_is_recorded_once():
    """A provider retry inside the summariser call is an attempt event on
    that call's generation; the call is debited once, with the usage of the
    response it returned."""

    class _RetriedSummariser(_ScriptedLLM):
        attempts = 0

        async def chat(self, messages, model=None, tools=None, **kwargs):
            if _phase(messages, tools) != "compaction":
                return await super().chat(messages, model=model, tools=tools, **kwargs)
            self.phases.append("compaction")

            async def _attempt() -> ChatResponse:
                self.attempts += 1
                if self.attempts == 1:
                    raise _TransientError("overloaded")
                return _summary(_SUMMARY)

            return await retry_llm_call(
                _attempt, is_retryable=lambda exc: isinstance(exc, _TransientError), base_delay=0.0
            )

    llm = _RetriedSummariser(calls=[_answer(_LONG, _ANSWER)], summaries=[])
    budget = _RecordingBudget()
    observations: list[MagicMock] = []
    root = {**_root_trace_ctx(), "trace": _observation("trace", observations)}

    response = await _run(llm, budget, root, _history(turns=20))

    assert llm.phases == ["compaction", "answer"]
    assert llm.attempts == 2
    assert budget.debits == [_SUMMARY, _ANSWER]
    assert response.usage == _total(_SUMMARY, _ANSWER)
    (span,) = _compaction_spans(root)
    assert (span["input_tokens"], span["output_tokens"]) == (_SUMMARY["input_tokens"], _SUMMARY["output_tokens"])
    # The retry is on the summariser's generation, and nowhere else.
    retried = {o.observation_name: [c.kwargs["name"] for c in o.create_event.call_args_list] for o in observations}
    assert retried.pop(_COMPACTION) == ["attempt:retry:1"]
    assert all(events == [] for events in retried.values())


# ---------------------------------------------------------------------------
# The budget, checked again after a compaction that spent
# ---------------------------------------------------------------------------

# A summariser call large enough to push a small budget over its limit on its
# own: the call that follows it was priced before it was debited.
_HUGE_SUMMARY = {"input_tokens": 100_000, "output_tokens": 600}
_STOPPED_LEAD_IN = al.BUDGET_EXHAUSTED_MESSAGE.split("{reason}")[0]
_STOP_NOTICE_LEAD_IN = al._BUDGET_STOP_NOTICE.split("{reason}")[0]


class _Events:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def __call__(self, event: dict) -> None:
        self.events.append(event)

    def statuses(self) -> list[str]:
        return [e.get("status") for e in self.events]

    def budget_stops(self) -> int:
        return sum(
            1
            for e in self.events
            if e.get("status") == "thinking_text" and "Stopping tool use" in str(e.get("content"))
        )


def _count(messages: list[ChatMessage], lead_in: str) -> int:
    return sum(1 for m in messages if (m.content or "").startswith(lead_in))


async def test_a_compaction_that_exhausts_the_budget_takes_the_exhaustion_path_without_the_call():
    """The summariser pushes the run over its token limit: the main call it
    preceded is not made, and the run stops the way a run that reaches its
    budget at an iteration boundary stops (one stop warning, the backstop's
    guard, the budget digest)."""
    llm = _ScriptedLLM(calls=[_answer(_LONG, _ANSWER)], summaries=[_summary(_HUGE_SUMMARY)])
    budget = _RecordingBudget(max_total_tokens=40_000)
    events = _Events()

    response = await _run(llm, budget, _root_trace_ctx(), _history(turns=20), on_event=events)

    # Before the check point: ["compaction", "answer"], an answer the budget
    # had already refused.
    assert llm.phases == ["compaction"]
    assert budget.debits == [_HUGE_SUMMARY]
    assert response.content.startswith(_STOPPED_LEAD_IN)
    assert response.usage == _HUGE_SUMMARY
    assert events.budget_stops() == 1


# A summariser call that leaves the call it preceded less than its full
# allowance, but more than the floor.
_LARGE_SUMMARY = {"input_tokens": 34_000, "output_tokens": 600}


async def test_a_compaction_that_leaves_less_than_a_full_call_is_followed_by_a_sized_one():
    """The call the summariser left short is not made at its full allowance:
    the gate at the next iteration boundary sizes it, as it sizes any call the
    budget cannot cover in full."""
    llm = _ScriptedLLM(calls=[_answer(_LONG, _ANSWER)], summaries=[_summary(_LARGE_SUMMARY)])
    budget = _RecordingBudget(max_total_tokens=60_000)

    response = await _run(llm, budget, _root_trace_ctx(), _history(turns=20), max_tokens=16_384)

    assert llm.phases == ["compaction", "answer"]
    assert MIN_OUTPUT_TOKENS <= llm.max_tokens[-1] < 16_384
    assert response.content == _LONG
    assert budget.debits == [_LARGE_SUMMARY, _ANSWER]


async def test_after_a_compaction_the_downgrade_still_comes_before_sizing():
    """With a fallback model configured, the next gate switches the call the
    summariser left short to the fallback, as it did before calls
    were sized, rather than sizing it down on the primary."""
    fallback = _ScriptedLLM(calls=[_answer(_LONG, _ANSWER)], summaries=[])
    llm = _ScriptedLLM(calls=[], summaries=[_summary(_LARGE_SUMMARY)])
    cheap = ModelPrice(
        provider="stub",
        model_id="cheap-model",
        model_pattern=None,
        input_price_per_1m=Decimal("0.01"),
        output_price_per_1m=Decimal("0.01"),
        cache_read_price_per_1m=None,
        cache_write_price_per_1m=None,
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )
    budget = _RecordingBudget(
        max_cost_usd=0.40,
        fallback=BudgetFallbackModel(model_id="cheap-model", price=cheap, provider=fallback),
    )

    response = await _run(llm, budget, _root_trace_ctx(), _history(turns=20), max_tokens=16_384)

    assert llm.phases == ["compaction"]
    assert fallback.phases == ["answer"]
    assert budget.downgrade is not None and budget.downgrade.to_model == "cheap-model"
    assert response.content == _LONG


async def test_a_run_still_under_budget_after_compaction_is_unchanged():
    """Under its limit, a budgeted run that compacts makes the same calls and
    emits the same events as the same run with no budget at all."""

    async def _one(budget: RunBudget | None) -> tuple[list[str], list[str], ChatResponse]:
        llm = _ScriptedLLM(calls=[_answer(_LONG, _ANSWER)], summaries=[_summary(_SUMMARY)])
        events = _Events()
        response = await _run(llm, budget, _root_trace_ctx(), _history(turns=20), on_event=events)
        return llm.phases, events.statuses(), response

    budget = _RecordingBudget(max_total_tokens=1_000_000)
    phases, statuses, response = await _one(budget)
    unbudgeted_phases, unbudgeted_statuses, unbudgeted = await _one(None)

    assert phases == unbudgeted_phases == ["compaction", "answer"]
    assert statuses == unbudgeted_statuses
    assert response.content == unbudgeted.content
    assert response.usage == unbudgeted.usage == _total(_SUMMARY, _ANSWER)
    assert budget.debits == [_SUMMARY, _ANSWER]


async def test_an_empty_response_recovery_that_exhausts_the_budget_makes_no_retry():
    """The empty-response recovery compacts before its retry: a summariser
    that exhausts the budget there leaves the retry unmade."""
    llm = _ScriptedLLM(
        calls=[_answer("", _TOOL_CALL), _answer(_LONG, _RETRY)],
        summaries=[_summary(_HUGE_SUMMARY)],
    )
    budget = _RecordingBudget(max_total_tokens=40_000)
    events = _Events()

    response = await _run(
        llm,
        budget,
        _root_trace_ctx(),
        _history(turns=22),
        on_event=events,
        with_tools=True,
        max_session_chars=None,
    )

    # Before the check point: the retry ran ("answer") on a spent budget.
    assert llm.phases == ["tools", "compaction"]
    assert budget.debits == [_TOOL_CALL, _HUGE_SUMMARY]
    assert response.content.startswith(_STOPPED_LEAD_IN)
    assert events.budget_stops() == 1


async def test_a_backstop_compaction_that_exhausts_the_budget_makes_no_retry():
    """A synthesis too short to keep is retried after a compaction: when that
    compaction exhausts the budget, the backstop's own guard answers with the
    budget digest instead of the retry."""
    llm = _ScriptedLLM(
        calls=[_tool_response(), _answer(_SHORT, _SYNTHESIS), _answer(_LONG, _RETRY)],
        summaries=[_summary(_HUGE_SUMMARY)],
    )
    budget = _RecordingBudget(max_tool_calls=1, max_total_tokens=60_000)
    events = _Events()
    messages = _history(turns=22)

    response = await _run(
        llm,
        budget,
        _root_trace_ctx(),
        messages,
        on_event=events,
        with_tools=True,
        max_session_chars=None,
    )

    # Before the check point: [..., "compaction", "retry"].
    assert llm.phases == ["tools", "synthesis", "compaction"]
    assert budget.debits[0] == _TOOL_CALL
    assert budget.debits[-1] == _HUGE_SUMMARY
    assert response.content.startswith(_STOPPED_LEAD_IN)
    # One stop, at the iteration boundary that ended the tool calls; the
    # backstop's notice once.
    assert events.budget_stops() == 1
    assert _count(messages, _STOP_NOTICE_LEAD_IN) == 1


async def test_a_compaction_that_crosses_the_token_wind_down_winds_down_once():
    """The summariser crosses the token wind-down while tools are offered:
    the call is made tool-free at the next boundary, with the wind-down
    prompt once, not a tool call first and not the prompt twice."""
    llm = _ScriptedLLM(calls=[_answer(_LONG, _ANSWER)], summaries=[_summary(_HUGE_SUMMARY)])
    events = _Events()
    messages = _history(turns=20)

    response = await _run(
        llm,
        None,
        _root_trace_ctx(),
        messages,
        on_event=events,
        with_tools=True,
        max_total_tokens=30_000,
    )

    # Before the check point: ["compaction", "tools"], a call that still
    # offered every tool past the wind-down.
    assert llm.phases == ["compaction", "answer"]
    assert response.content.strip() == _LONG.strip()
    assert _count(messages, al._LAST_CHANCE_PROMPT) == 1
    assert events.statuses().count("composing") == 1
