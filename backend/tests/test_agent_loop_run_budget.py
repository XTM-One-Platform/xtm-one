"""Agent-loop wiring for the per-run spending budget.

``run_agent_loop(budget=RunBudget(...))`` bounds a run along four dimensions
with one object, enforced with reserve -> call -> debit -> block:

* an LLM call whose worst-case spend no longer fits is **refused** (a hard
  stop, unlike the soft token wind-down) and the run routes to the
  Stage-3 backstop;
* the backstop is gated against the FULL limit, so the held-back
  closing-synthesis reserve turns an exhausted budget into a real answer —
  and when even that is gone, into a deterministic tool-result digest with no
  further LLM call at all;
* tool calls are counted per **batch**, not per iteration (one iteration can
  fan out into many parallel calls); and
* the duration limit is folded into the loop's existing wall-clock deadline.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.services.tracing.cost_calculator import ModelPrice
from app.tools import agent_loop as al
from app.tools.agent_loop import (
    _BUDGET_NUDGE,
    BUDGET_EXHAUSTED_MESSAGE,
    FORCED_ANSWER_TIMEOUT_MESSAGE,
)
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
)
from app.tools.run_budget import RunBudget

# The forced-answer backstop retries any synthesis shorter than 100 chars
# with a stronger prompt, so scripted final answers are realistically long.
_PADDING = (
    " The tool results were reviewed and the findings above summarise "
    "everything the run was able to establish before it stopped."
)

# What a tool-calling iteration offers: the agent's own ``lookup`` plus the
# loop's built-in ``wait`` timer, which rides along with any non-empty tool
# list.  ``None`` in an ``offered_tools`` assertion still means "every
# tool stripped" — the timer included.
_OFFERED = ["lookup", "wait"]


class _ScriptedLLM(BaseLLMProvider):
    """Stub LLM returning scripted responses, recording the tools offered."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = script
        self._idx = 0
        self.offered_tools: list[list[str] | None] = []

    @property
    def calls(self) -> int:
        return self._idx

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self.offered_tools.append(
            [t.name for t in tools] if tools is not None else None
        )
        response = self._script[self._idx]
        self._idx += 1
        return response

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.fixture
def _quiet_langfuse(monkeypatch):
    """Neutralise Langfuse plumbing so the loop runs without real tracing."""
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)


def _price(per_1m: str = "1") -> ModelPrice:
    """$1 per 1M tokens in and out — round numbers for budget arithmetic."""
    return ModelPrice(
        provider="stub",
        model_id="stub-model",
        model_pattern=None,
        input_price_per_1m=Decimal(per_1m),
        output_price_per_1m=Decimal(per_1m),
        cache_read_price_per_1m=None,
        cache_write_price_per_1m=None,
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )


def _executor() -> tuple[CompositeToolExecutor, list[str]]:
    executed: list[str] = []
    callable_exec = CallableToolExecutor()
    definitions = [
        ToolDefinition(name="lookup", description="lookup", source="custom:test")
    ]

    async def _handler(**kwargs):
        executed.append("lookup")
        return "ran lookup"

    callable_exec.register("lookup", _handler)
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)
    return composite, executed


def _tool_call(call_id: str, q: str = "x") -> ToolCall:
    return ToolCall(id=call_id, name="lookup", arguments={"q": q})


# ── Cost ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cost_budget_blocks_tools_but_still_synthesises(_quiet_langfuse):
    """An exhausted cost budget ends tool use and spends the reserved slice on
    a real final answer."""
    composite, executed = _executor()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[_tool_call("c1")],
                # $0.0895 at $1/1M in+out — the in-loop gate (90% of $0.10)
                # has room for less than a floor-sized call, but the
                # synthesis is still affordable against the full limit.
                usage={"input_tokens": 45_000, "output_tokens": 44_500},
            ),
            ChatResponse(content="Synthesised within budget." + _PADDING, model="stub", usage={}),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        budget=RunBudget(max_cost_usd=0.10, price=_price()),
    )

    assert response.content == "Synthesised within budget." + _PADDING
    assert executed == ["lookup"], "the first iteration's work must still run"
    # First call offered the tool; the synthesis call is tool-free.
    assert llm.offered_tools == [_OFFERED, None]
    assert llm.calls == 2, "the loop must not issue a third (blocked) call"


@pytest.mark.asyncio
async def test_cost_budget_too_small_for_synthesis_returns_digest(_quiet_langfuse):
    """When not even the closing-synthesis reserve is left, the loop returns a
    deterministic digest instead of a call it cannot pay for."""
    composite, executed = _executor()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[_tool_call("c1")],
                usage={"input_tokens": 40_000, "output_tokens": 10_000},  # $0.05
            )
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        budget=RunBudget(max_cost_usd=0.05, price=_price()),
    )

    assert executed == ["lookup"]
    assert llm.calls == 1, "no LLM call may be issued once the budget is gone"
    lead_in = BUDGET_EXHAUSTED_MESSAGE.format(reason="this run reached its cost budget ($0.0500)")
    assert response.content.startswith(lead_in)
    # The work already done is still handed back, not thrown away.
    assert "ran lookup" in response.content
    # Accumulated usage survives the shortcut so run accounting is intact.
    assert response.usage.get("input_tokens") == 40_000


@pytest.mark.asyncio
async def test_cost_not_enforced_without_a_price_row(_quiet_langfuse):
    """A cost limit with no pricing data fails OPEN — the run completes."""
    composite, executed = _executor()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[_tool_call("c1")],
                usage={"input_tokens": 5_000_000, "output_tokens": 5_000_000},
            ),
            ChatResponse(content="Unpriced answer." + _PADDING, model="stub", usage={}),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        budget=RunBudget(max_cost_usd=0.000_001, price=None),
    )

    assert response.content == "Unpriced answer." + _PADDING
    assert executed == ["lookup"]
    # Tools were still offered on the second call: nothing was blocked.
    assert llm.offered_tools == [_OFFERED, _OFFERED]


# ── Tool calls ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_call_budget_counts_the_batch_not_the_iteration(_quiet_langfuse):
    """Two parallel calls in ONE iteration exhaust a 2-call budget — the gap
    ``max_iterations`` cannot close."""
    composite, executed = _executor()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[_tool_call("c1"), _tool_call("c2", "y")],
                usage={},
            ),
            ChatResponse(content="Two calls was the budget." + _PADDING, model="stub", usage={}),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        budget=RunBudget(max_tool_calls=2),
    )

    assert executed == ["lookup", "lookup"], "the requested batch still runs"
    assert response.content == "Two calls was the budget." + _PADDING
    assert llm.offered_tools == [_OFFERED, None]


@pytest.mark.asyncio
async def test_tool_call_budget_leaves_room_for_the_full_allowance(_quiet_langfuse):
    """The closing-synthesis reserve must not silently shrink a tool-call
    budget: 3 calls means 3, not 2."""
    composite, executed = _executor()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[_tool_call("c1"), _tool_call("c2", "y")],
                usage={},
            ),
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[_tool_call("c3", "z")],
                usage={},
            ),
            ChatResponse(content="Used all three." + _PADDING, model="stub", usage={}),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        budget=RunBudget(max_tool_calls=3),
    )

    assert len(executed) == 3
    assert response.content == "Used all three." + _PADDING


class _RecordingLLM(_ScriptedLLM):
    """Also keeps the transcript each request carried."""

    def __init__(self, script: list[ChatResponse]) -> None:
        super().__init__(script)
        self.requests: list[list] = []

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self.requests.append(list(messages))
        return await super().chat(messages, model=model, tools=tools, **kwargs)


@pytest.mark.asyncio
async def test_by_default_a_batch_crossing_the_budget_runs_in_full(_quiet_langfuse, monkeypatch, caplog):
    """Without ``AGENT_LOOP_MAX_TOOL_CALLS_STRICT`` the batch that crosses the
    budget runs as it always did: every call executes and is debited, and the
    next LLM call is refused. One WARNING names the overshoot and the
    iteration's trace span records it."""
    spans: list = []
    monkeypatch.setattr(al, "_lf_end_span", lambda ctx, **kw: spans.append(kw.get("output")))
    composite, executed = _executor()
    batch = [_tool_call(f"c{i}", f"q{i}") for i in range(1, 6)]
    llm = _RecordingLLM(
        [
            ChatResponse(content="", model="stub", stop_reason="tool_calls", tool_calls=batch, usage={}),
            ChatResponse(content="All five ran." + _PADDING, model="stub", usage={}),
        ]
    )
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    budget = RunBudget(max_tool_calls=2)
    with caplog.at_level(logging.WARNING, logger=al.logger.name):
        response = await al.run_agent_loop(
            llm=llm,
            messages=[ChatMessage(role="user", content="go")],
            tools=composite.all_tools,
            executor=composite,
            max_iterations=10,
            budget=budget,
            on_event=_on_event,
        )

    assert executed == ["lookup"] * 5
    assert budget.tool_calls == 5
    assert response.content == "All five ran." + _PADDING
    assert llm.offered_tools == [_OFFERED, None], "the next call is refused exactly as before"
    answered = [m for m in llm.requests[-1] if getattr(m, "role", None) == "tool"]
    assert [(m.content, m.not_run) for m in answered] == [("ran lookup", None)] * 5
    assert not [e for e in events if e.get("budget_exhausted")]
    warnings = [r.getMessage() for r in caplog.records if "crosses the 2-call budget" in r.getMessage()]
    assert len(warnings) == 1
    assert "a batch of 5 call(s)" in warnings[0] and "3 past it" in warnings[0]
    overshoots = [o["tool_call_budget_overshoot"] for o in spans if o and "tool_call_budget_overshoot" in o]
    assert overshoots == [{"limit": 2, "batch": 5, "allowance": 2, "overshoot": 3}]


@pytest.mark.asyncio
async def test_tool_call_budget_truncates_an_oversized_batch(_quiet_langfuse):
    """In strict mode one response fanning out past the allowance runs only
    the allowance: the next-iteration gate alone let all five run."""
    composite, executed = _executor()
    batch = [_tool_call(f"c{i}", f"q{i}") for i in range(1, 6)]
    llm = _RecordingLLM(
        [
            ChatResponse(content="", model="stub", stop_reason="tool_calls", tool_calls=batch, usage={}),
            ChatResponse(content="Two calls was the budget." + _PADDING, model="stub", usage={}),
        ]
    )
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    budget = RunBudget(max_tool_calls=2, strict_tool_calls=True)
    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        budget=budget,
        on_event=_on_event,
    )

    assert executed == ["lookup", "lookup"]
    assert budget.tool_calls == 2, "only the calls that ran are debited"
    assert response.content == "Two calls was the budget." + _PADDING

    # Every tool_use of the batch is answered in the next request, so the
    # transcript stays well-formed for every provider.
    answered = {
        m.tool_call_id: m.content
        for m in llm.requests[-1]
        if getattr(m, "role", None) == "tool"
    }
    assert set(answered) == {"c1", "c2", "c3", "c4", "c5"}
    for dropped in ("c3", "c4", "c5"):
        assert answered[dropped].startswith("Not executed: this run reached its tool-call budget (2 calls)")
    assert answered["c1"] == "ran lookup"

    budget_done = [e for e in events if e.get("status") == "tool_done" and e.get("budget_exhausted")]
    assert len(budget_done) == 3
    starts = [e for e in events if e.get("status") == "tool_start"]
    assert starts and len(starts[0]["tools"]) == 5, "tool_start still lists what the model asked for"
    # Every call the UI was told about is closed exactly once, and a cut call
    # is never screened (nor refused) by the loop breaker.
    done = [e for e in events if e.get("status") == "tool_done"]
    assert len(done) == 5
    assert not [e for e in done if e.get("loop_breaker")]


class _AllowanceSpendingLLM(_RecordingLLM):
    """Spends what is left of the tool-call allowance while its tool-calling
    response is in flight: the gate admitted the call, the batch finds none."""

    def __init__(self, script: list[ChatResponse], budget: RunBudget) -> None:
        super().__init__(script)
        self._budget = budget

    async def chat(self, messages, model=None, tools=None, **kwargs):
        response = await super().chat(messages, model=model, tools=tools, **kwargs)
        if response.tool_calls:
            self._budget.record_tool_calls(self._budget.remaining_tool_calls() or 0)
        return response


@pytest.mark.asyncio
async def test_a_batch_that_finds_no_allowance_left_runs_nothing(_quiet_langfuse):
    """With nothing left the whole batch is cut: no call runs, every
    call is answered, and the run still synthesises."""
    composite, executed = _executor()
    budget = RunBudget(max_tool_calls=3, strict_tool_calls=True)
    llm = _AllowanceSpendingLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[_tool_call("c1"), _tool_call("c2", "y")],
                usage={},
            ),
            ChatResponse(content="Nothing ran." + _PADDING, model="stub", usage={}),
        ],
        budget,
    )
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        budget=budget,
        on_event=_on_event,
    )

    assert executed == []
    assert budget.tool_calls == 3, "a cut call is never debited"
    assert response.content == "Nothing ran." + _PADDING
    assert llm.offered_tools == [_OFFERED, None], "the next call is the tool-free synthesis"
    answered = [m for m in llm.requests[-1] if getattr(m, "role", None) == "tool"]
    assert [m.tool_call_id for m in answered] == ["c1", "c2"]
    assert all(m.not_run == "budget" for m in answered)
    assert len([e for e in events if e.get("budget_exhausted")]) == 2


def _executor_with_write() -> tuple[CompositeToolExecutor, list[str]]:
    executed: list[str] = []
    callable_exec = CallableToolExecutor()
    definitions = [
        ToolDefinition(name="lookup", description="lookup", source="custom:test"),
        ToolDefinition(name="update_record", description="update a record", source="custom:test"),
    ]

    async def _lookup(**kwargs):
        executed.append("lookup")
        return "ran lookup"

    async def _update(**kwargs):
        executed.append("update_record")
        return "record updated"

    callable_exec.register("lookup", _lookup)
    callable_exec.register("update_record", _update)
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)
    return composite, executed


@pytest.mark.asyncio
async def test_a_write_cut_from_the_batch_never_runs_and_says_so(_quiet_langfuse):
    """A skipped write must never read as one that happened: it is
    not dispatched, its answer says it did not run, and its tool_done event is
    a failure flagged budget_exhausted."""
    composite, executed = _executor_with_write()
    write = ToolCall(id="w1", name="update_record", arguments={"id": "42"})
    llm = _RecordingLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[_tool_call("c1"), write],
                usage={},
            ),
            ChatResponse(content="The update did not run." + _PADDING, model="stub", usage={}),
        ]
    )
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        budget=RunBudget(max_tool_calls=1, strict_tool_calls=True),
        on_event=_on_event,
    )

    assert executed == ["lookup"], "the write past the allowance is never dispatched"
    answered = {m.tool_call_id: m for m in llm.requests[-1] if getattr(m, "role", None) == "tool"}
    assert (answered["c1"].content, answered["c1"].not_run) == ("ran lookup", None)
    assert answered["w1"].content == al._format_tool_budget_refusal(RunBudget(max_tool_calls=1))
    assert answered["w1"].not_run == "budget"
    (write_done,) = [e for e in events if e.get("status") == "tool_done" and e.get("tool") == "update_record"]
    assert write_done["success"] is False
    assert write_done["budget_exhausted"] is True


def test_a_tool_that_returns_the_loop_answers_verbatim_is_still_a_finding():
    """What leaves a call out of the digest is the loop's tag, never the text:
    a tool that returns the budget notice or a refusal verbatim ran, and its
    result is listed like any other."""
    notice = al._format_tool_budget_refusal(RunBudget(max_tool_calls=2))
    refusal = al._format_loop_breaker_refusal("lookup", al._LoopDecision(action="refuse", count=5))
    messages = [
        ChatMessage(role="user", content="go"),
        ChatMessage(role="assistant", content="", tool_calls=[_tool_call("c1"), _tool_call("c2", "y")]),
        ChatMessage(role="tool", content=notice, tool_call_id="c1", name="lookup"),
        ChatMessage(role="tool", content=refusal, tool_call_id="c2", name="lookup"),
    ]

    digest = al.summarise_tool_results(messages)

    assert "Not executed" in digest
    assert "Loop breaker" in digest


def test_the_digest_leaves_out_a_call_the_budget_cut():
    """A call that never ran is not something the turn did: the deterministic
    digest must not list it under what was completed."""
    notice = al._format_tool_budget_refusal(RunBudget(max_tool_calls=1))
    write = ToolCall(id="w1", name="update_record", arguments={"id": "42"})
    messages = [
        ChatMessage(role="user", content="go"),
        ChatMessage(role="assistant", content="", tool_calls=[_tool_call("c1"), write]),
        ChatMessage(role="tool", content=notice, tool_call_id="w1", name="update_record", not_run="budget"),
        ChatMessage(role="tool", content="ran lookup", tool_call_id="c1", name="lookup"),
    ]

    digest = al.summarise_tool_results(messages)

    assert "ran lookup" in digest
    assert "Not executed" not in digest
    assert "update" not in digest.lower()


def test_calls_that_never_ran_leave_the_digest_window_to_results():
    """A cut batch answers its dropped calls first: those answers, like a
    loop-breaker refusal, must not use up the digest's most-recent window and
    push out results that ran in an earlier round."""
    notice = al._format_tool_budget_refusal(RunBudget(max_tool_calls=5))
    refusal = al._format_loop_breaker_refusal("lookup", al._LoopDecision(action="refuse", count=5))
    earlier = [_tool_call(f"r{i}", f"q{i}") for i in (1, 2, 3)]
    fan_out = [_tool_call(f"c{i}", f"x{i}") for i in range(1, 13)]
    messages = [
        ChatMessage(role="user", content="go"),
        ChatMessage(role="assistant", content="", tool_calls=earlier),
        *[ChatMessage(role="tool", content=f"record {i}", tool_call_id=f"r{i}", name="lookup") for i in (1, 2, 3)],
        # Two calls fit the allowance left: the other ten are answered first,
        # then the loop breaker refuses c2 and c1 runs.
        ChatMessage(role="assistant", content="", tool_calls=fan_out),
        *[
            ChatMessage(role="tool", content=notice, tool_call_id=f"c{i}", name="lookup", not_run="budget")
            for i in range(3, 13)
        ],
        ChatMessage(role="tool", content=refusal, tool_call_id="c2", name="lookup", not_run="loop_breaker"),
        ChatMessage(role="tool", content="record 4", tool_call_id="c1", name="lookup"),
    ]

    digest = al.summarise_tool_results(messages)

    for n in (1, 2, 3, 4):
        assert f"record {n}" in digest


def _executor_with(*names: str, sources: dict[str, str] | None = None) -> tuple[CompositeToolExecutor, list[str]]:
    """``lookup`` plus executor tools named *names*, recording what runs."""
    executed: list[str] = []
    callable_exec = CallableToolExecutor()
    definitions = [
        ToolDefinition(name=n, description=n, source=(sources or {}).get(n, "custom:test")) for n in ("lookup", *names)
    ]

    def _handler(name: str):
        async def _run(**kwargs):
            executed.append(name)
            return f"ran {name}"

        return _run

    for d in definitions:
        callable_exec.register(d.name, _handler(d.name))
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)
    return composite, executed


async def _run_batch(composite, batch: list[ToolCall], budget: RunBudget, **kwargs):
    llm = _RecordingLLM(
        [
            ChatResponse(content="", model="stub", stop_reason="tool_calls", tool_calls=batch, usage={}),
            ChatResponse(content="Done." + _PADDING, model="stub", usage={}),
        ]
    )
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        budget=budget,
        on_event=_on_event,
        **kwargs,
    )
    return llm, events


@pytest.mark.asyncio
async def test_a_status_poll_is_not_charged_to_the_tool_call_budget(_quiet_langfuse):
    """``check_task_status`` pauses rather than acts, like ``wait``: it is
    neither debited nor cut, so two lookups still fit a 2-call budget."""
    composite, executed = _executor_with(
        "check_task_status", sources={"check_task_status": al._BUILTIN_STATUS_POLL_SOURCE}
    )
    budget = RunBudget(max_tool_calls=2, strict_tool_calls=True)
    poll = ToolCall(id="s1", name="check_task_status", arguments={})

    _llm, events = await _run_batch(composite, [_tool_call("c1"), poll, _tool_call("c2", "y")], budget)

    assert sorted(executed) == ["check_task_status", "lookup", "lookup"]
    assert budget.tool_calls == 2
    assert not [e for e in events if e.get("budget_exhausted")]


@pytest.mark.asyncio
async def test_only_one_status_poll_a_batch_rides_free(_quiet_langfuse):
    """Status polls run in the parallel gather, so a batch of them is still the
    fan-out the ceiling bounds: the first rides free, the next ones are charged
    and, in strict mode, cut."""
    composite, executed = _executor_with(
        "check_task_status", sources={"check_task_status": al._BUILTIN_STATUS_POLL_SOURCE}
    )
    budget = RunBudget(max_tool_calls=1, strict_tool_calls=True)
    polls = [ToolCall(id=f"s{i}", name="check_task_status", arguments={"task_ids": f"t{i}"}) for i in (1, 2, 3)]

    _llm, events = await _run_batch(composite, polls, budget)

    assert executed == ["check_task_status", "check_task_status"]
    assert budget.tool_calls == 1
    assert [e["tool"] for e in events if e.get("budget_exhausted")] == ["check_task_status"]


@pytest.mark.asyncio
async def test_a_wait_is_never_cut_and_never_debited(_quiet_langfuse):
    """The loop's own ``wait`` rides through a cut batch: the cut keeps it,
    drops the lookup past the allowance, and ``tool_start`` still lists the
    batch in the model's order."""
    from app.tools.wait_policy import WaitPolicy

    composite, executed = _executor_with()
    budget = RunBudget(max_tool_calls=1, strict_tool_calls=True)
    pause = ToolCall(id="w1", name="wait", arguments={"duration": "1s"})

    llm, events = await _run_batch(
        composite,
        [_tool_call("c1"), pause, _tool_call("c2", "y")],
        budget,
        wait_policy=WaitPolicy(per_call_seconds=1, total_seconds=600),
    )

    assert executed == ["lookup"]
    assert budget.tool_calls == 1
    answered = {m.tool_call_id: m for m in llm.requests[-1] if getattr(m, "role", None) == "tool"}
    assert "Waited" in answered["w1"].content
    assert answered["w1"].not_run is None
    assert answered["c2"].not_run == "budget"
    assert [e["tools"] for e in events if e.get("status") == "tool_start"] == [["lookup", "wait", "lookup"]]


@pytest.mark.asyncio
async def test_a_tool_of_the_run_named_check_task_status_is_charged(_quiet_langfuse):
    """Only the built-in status poll is a pause: a custom or MCP tool that
    carries its name is work, debited and cut like any other."""
    composite, executed = _executor_with("check_task_status")
    budget = RunBudget(max_tool_calls=1, strict_tool_calls=True)
    poll = ToolCall(id="s1", name="check_task_status", arguments={})

    _llm, events = await _run_batch(composite, [_tool_call("c1"), poll], budget)

    assert executed == ["lookup"]
    assert [e["tool"] for e in events if e.get("budget_exhausted")] == ["check_task_status"]


def test_the_status_poll_exemption_names_the_built_in_source():
    """The exemption keys on the built-in definition's ``source``: pinned to
    what the sub-agent tool builder declares."""
    import ast
    from pathlib import Path

    from app.services.builtin_tools import handover

    tree = ast.parse(Path(handover.__file__).read_text(encoding="utf-8"))
    sources = [
        {kw.arg: kw.value.value for kw in node.keywords if isinstance(kw.value, ast.Constant)}.get("source")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "ToolDefinition"
        and any(kw.arg == "name" and getattr(kw.value, "value", None) == "check_task_status" for kw in node.keywords)
    ]
    assert sources == [al._BUILTIN_STATUS_POLL_SOURCE]


@pytest.mark.asyncio
async def test_a_tool_of_the_run_named_wait_is_charged(_quiet_langfuse):
    """When the run carries its own ``wait`` tool the loop offers no timer, and
    that tool is work like any other."""
    composite, executed = _executor_with("wait")
    budget = RunBudget(max_tool_calls=1, strict_tool_calls=True)

    _llm, events = await _run_batch(
        composite, [_tool_call("c1"), ToolCall(id="w1", name="wait", arguments={})], budget
    )

    assert executed == ["lookup"]
    assert [e["tool"] for e in events if e.get("budget_exhausted")] == ["wait"]


def test_the_digest_leaves_out_a_call_the_loop_breaker_refused():
    """A refused call never ran: the digest must not list the refusal among
    what the turn completed."""
    refusal = al._format_loop_breaker_refusal("lookup", al._LoopDecision(action="refuse", count=5))
    messages = [
        ChatMessage(role="user", content="go"),
        ChatMessage(role="assistant", content="", tool_calls=[_tool_call("c1"), _tool_call("c2")]),
        ChatMessage(role="tool", content="ran lookup", tool_call_id="c1", name="lookup"),
        ChatMessage(role="tool", content=refusal, tool_call_id="c2", name="lookup", not_run="loop_breaker"),
    ]

    digest = al.summarise_tool_results(messages)

    assert "ran lookup" in digest
    assert "Loop breaker" not in digest


@pytest.mark.asyncio
async def test_a_batch_within_the_allowance_is_not_cut(_quiet_langfuse):
    composite, executed = _executor()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[_tool_call("c1"), _tool_call("c2", "y")],
                usage={},
            ),
            ChatResponse(content="Done." + _PADDING, model="stub", usage={}),
        ]
    )
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        budget=RunBudget(max_tool_calls=5, strict_tool_calls=True),
        on_event=_on_event,
    )

    assert executed == ["lookup", "lookup"]
    assert not [e for e in events if e.get("budget_exhausted")]


# ── Tokens ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_token_budget_is_a_hard_block_when_the_budget_is_active(_quiet_langfuse):
    """Unlike the legacy soft wind-down, the budget's token limit stops
    the run rather than stripping tools and continuing."""
    composite, executed = _executor()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[_tool_call("c1")],
                usage={"total_tokens": 200_000},
            ),
            ChatResponse(content="Hard-stopped answer." + _PADDING, model="stub", usage={}),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        budget=RunBudget(max_total_tokens=220_000),
    )

    assert executed == ["lookup"]
    assert response.content == "Hard-stopped answer." + _PADDING
    assert llm.offered_tools == [_OFFERED, None]


@pytest.mark.asyncio
async def test_reservations_are_released_so_a_healthy_run_completes(_quiet_langfuse):
    """Worst-case reservations must not accumulate: three cheap iterations run
    to completion under a budget that only fits ~2 outstanding reservations."""
    composite, executed = _executor()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[_tool_call(f"c{i}", str(i))],
                usage={"total_tokens": 100},
            )
            for i in range(3)
        ]
        + [ChatResponse(content="Done cheaply." + _PADDING, model="stub", usage={"total_tokens": 100})]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        # ~16.4k of worst-case output allowance per call: if reservations
        # leaked, the third iteration would be refused.
        budget=RunBudget(max_total_tokens=40_000),
    )

    assert response.content == "Done cheaply." + _PADDING
    assert len(executed) == 3
    assert llm.offered_tools == [_OFFERED] * 4, "no iteration was blocked"


@pytest.mark.asyncio
async def test_budget_warning_nudges_before_it_blocks(_quiet_langfuse):
    """Crossing the warning threshold injects a consolidation nudge and leaves
    tools in place — the model gets a chance to land before the hard stop."""
    composite, _ = _executor()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[_tool_call("c1")],
                usage={"total_tokens": 800_000},
            ),
            ChatResponse(content="Landed after the nudge." + _PADDING, model="stub", usage={}),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        budget=RunBudget(max_total_tokens=1_000_000),
    )

    assert response.content == "Landed after the nudge." + _PADDING
    assert any(
        getattr(m, "role", None) == "system" and m.content == _BUDGET_NUDGE
        for m in messages
    )
    assert llm.offered_tools == [_OFFERED, _OFFERED], "a nudge must not strip tools"


# ── Duration ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duration_limit_is_folded_into_the_wall_clock_deadline(_quiet_langfuse):
    """The duration dimension reuses the loop's existing wall-clock path, whose
    guard refuses to start a doomed call and returns the graceful fallback."""
    composite, executed = _executor()
    llm = _ScriptedLLM([ChatResponse(content="never reached", model="stub")])
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        budget=RunBudget(max_duration_seconds=0.001),
    )

    assert llm.calls == 0
    assert executed == []
    assert response.content.startswith(FORCED_ANSWER_TIMEOUT_MESSAGE)


@pytest.mark.asyncio
async def test_caller_deadline_wins_when_tighter(_quiet_langfuse):
    """``wall_clock_deadline`` and the budget's duration limit resolve to the
    tighter bound, not the last one set."""
    import time

    composite, _ = _executor()
    llm = _ScriptedLLM([ChatResponse(content="never reached", model="stub")])
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        wall_clock_deadline=time.monotonic() + 0.001,
        budget=RunBudget(max_duration_seconds=3_600),
    )

    assert llm.calls == 0
    assert response.content.startswith(FORCED_ANSWER_TIMEOUT_MESSAGE)


# ── Backward compatibility ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inactive_budget_changes_nothing(_quiet_langfuse):
    """An all-zero budget is indistinguishable from no budget at all."""
    composite, executed = _executor()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[_tool_call("c1")],
                usage={"total_tokens": 10_000_000},
            ),
            ChatResponse(content="Unbounded answer." + _PADDING, model="stub", usage={}),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        budget=RunBudget(),
    )

    assert response.content == "Unbounded answer." + _PADDING
    assert executed == ["lookup"]
    assert llm.offered_tools == [_OFFERED, _OFFERED]
