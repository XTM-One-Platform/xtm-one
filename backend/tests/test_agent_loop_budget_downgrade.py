"""Budget-driven model downgrade in the agent loop.

Every run has a per-run budget whose only outcome, once exhausted, is a
**stop**.  That is the right safety behaviour but a blunt one: an operator who
caps a run at $0.50 on an expensive model usually means "not more than $0.50
*at this quality*", not "abandon the task" — and the same budget buys several
times more work on a cheaper model.

These tests pin the third outcome of the budget gate: when the COST dimension
refuses the next call and a fallback model is configured, the run switches to
it and **continues**.  What must hold:

* the switch happens on the provider that owns the fallback, and everything the
  loop had pinned to the primary (``model``, ``max_tokens``, the context
  budgets) is re-derived for it;
* the model is told, so a mid-task capability drop is not experienced as the
  model suddenly getting worse at its own job;
* the closing synthesis degrades too, rather than returning the deterministic
  digest; and
* three cases refuse the switch and keep the hard stop — a fallback whose
  context window cannot hold the system prompt, one whose provider cannot
  carry the tool surface, and a cross-provider switch that would silently drop
  native structured output.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.services.tracing.cost_calculator import ModelPrice
from app.tools import agent_loop as al
from app.tools.agent_loop import BUDGET_EXHAUSTED_MESSAGE
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
)
from app.tools.run_budget import BudgetFallbackModel, RunBudget

_PADDING = (
    " The tool results were reviewed and the findings above summarise "
    "everything the run was able to establish before the model changed."
)


class _ScriptedLLM(BaseLLMProvider):
    """Stub LLM returning scripted responses, recording every call's shape."""

    def __init__(
        self,
        script: list[ChatResponse],
        *,
        provider_name: str = "primary",
        context_window: int = 200_000,
        max_output_tokens: int = 16_384,
        max_tools: int = 128,
    ) -> None:
        self._script = script
        self._idx = 0
        self.provider_name = provider_name
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.max_tools = max_tools
        self.calls: list[dict] = []

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self.calls.append(
            {
                "model": model,
                "tools": [t.name for t in tools] if tools is not None else None,
                "max_tokens": kwargs.get("max_tokens"),
                "reasoning_effort": kwargs.get("reasoning_effort"),
                "structured_output": kwargs.get("structured_output"),
            }
        )
        response = self._script[min(self._idx, len(self._script) - 1)]
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


def _price(model_id: str, per_1m: str) -> ModelPrice:
    """Flat per-1M price on both directions — round numbers for arithmetic."""
    return ModelPrice(
        provider="stub",
        model_id=model_id,
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


def _tool_call(call_id: str) -> ToolCall:
    # ``lookup`` is a read, and the loop answers a repeated identical
    # read from its first result instead of running it again: each call carries
    # its own argument so every one of them really executes.
    return ToolCall(id=call_id, name="lookup", arguments={"q": call_id})


def _tool_shape(calls: list[dict]) -> list[str | None]:
    """Per call: ``"tools"`` when any were offered, ``None`` when stripped.

    The exact list is not the point here and is not ours to pin — the loop adds
    its own tools (``wait``, the ``load_tools`` meta-tool) — whereas "the
    synthesis call is tool-free" is exactly what these tests are about.
    """
    return ["tools" if c["tools"] is not None else None for c in calls]


def _expensive_first_call() -> ChatResponse:
    """$0.08 of a $0.10 budget at $1/1M — past the in-loop cost gate."""
    return ChatResponse(
        content="",
        model="expensive-model",
        stop_reason="tool_calls",
        tool_calls=[_tool_call("c1")],
        usage={"input_tokens": 40_000, "output_tokens": 40_000},
    )


def _budget(
    fallback_llm: BaseLLMProvider | None,
    *,
    max_cost_usd: float = 0.10,
    fallback_price: str = "0.01",
    fallback_model: str = "cheap-model",
) -> RunBudget:
    return RunBudget(
        max_cost_usd=max_cost_usd,
        price=_price("expensive-model", "1"),
        fallback=(
            None
            if fallback_llm is None
            else BudgetFallbackModel(
                model_id=fallback_model,
                price=_price(fallback_model, fallback_price),
                provider=fallback_llm,
            )
        ),
    )


# ── The happy path ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cost_exhaustion_continues_on_the_fallback_model(_quiet_langfuse):
    """The run keeps working on the cheap model instead of stopping."""
    composite, executed = _executor()
    primary = _ScriptedLLM([_expensive_first_call()])
    fallback = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="cheap-model",
                stop_reason="tool_calls",
                tool_calls=[_tool_call("c2")],
                usage={"input_tokens": 40_000, "output_tokens": 1_000},
            ),
            ChatResponse(
                content="Finished on the cheaper model." + _PADDING,
                model="cheap-model",
                usage={},
            ),
        ],
        provider_name="cheap",
    )
    events: list[dict] = []

    async def _on_event(event):
        events.append(event)

    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    response = await al.run_agent_loop(
        llm=primary,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        model="expensive-model",
        budget=_budget(fallback),
        on_event=_on_event,
    )

    assert response.content == "Finished on the cheaper model." + _PADDING
    # The expensive model made exactly the one call the budget could pay for;
    # everything after it ran on the fallback.
    assert len(primary.calls) == 1
    assert [c["model"] for c in fallback.calls] == ["cheap-model", "cheap-model"]
    # The work continued — tool use did NOT end at the downgrade.
    assert executed == ["lookup", "lookup"]

    downgraded = [e for e in events if e.get("status") == "model_downgraded"]
    assert len(downgraded) == 1
    assert downgraded[0]["from_model"] == "expensive-model"
    assert downgraded[0]["to_model"] == "cheap-model"
    assert downgraded[0]["provider"] == "cheap"
    assert downgraded[0]["reason"] == "cost"
    # The user sees it too, on the existing reasoning channel.
    assert any(
        e.get("status") == "thinking_text" and "cheap-model" in (e.get("content") or "")
        for e in events
    )
    # And so does the model: an unexplained capability drop mid-task reads as
    # the model getting worse at its own job.
    assert any(
        m.role == "system" and "switched to a cheaper model" in (m.content or "")
        for m in messages
    )


@pytest.mark.asyncio
async def test_the_downgrade_is_recorded_on_the_trace(_quiet_langfuse, monkeypatch):
    """The trace names the model and config the run continued on."""
    recorded: list[tuple[BaseLLMProvider, dict]] = []
    monkeypatch.setattr(
        al, "record_model_downgrade", lambda provider, **kw: recorded.append((provider, kw))
    )
    composite, _executed = _executor()
    primary = _ScriptedLLM([_expensive_first_call()])
    fallback = _ScriptedLLM(
        [ChatResponse(content="Finished on the cheaper model." + _PADDING, model="cheap-model", usage={})],
        provider_name="cheap",
    )

    await al.run_agent_loop(
        llm=primary,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        model="expensive-model",
        budget=_budget(fallback),
    )

    assert recorded == [(fallback, {"from_model": "expensive-model", "to_model": "cheap-model"})]


@pytest.mark.asyncio
async def test_the_rest_of_the_run_is_priced_at_the_fallback_rates(_quiet_langfuse):
    """Without repricing, cost accounting after the switch is fiction — a
    budget that looks respected but is not."""
    composite, _ = _executor()
    primary = _ScriptedLLM([_expensive_first_call()])
    fallback = _ScriptedLLM(
        [ChatResponse(content="Done on the cheap model." + _PADDING, model="cheap-model",
                      usage={"input_tokens": 1_000_000, "output_tokens": 0})],
        provider_name="cheap",
    )
    budget = _budget(fallback)

    await al.run_agent_loop(
        llm=primary,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        model="expensive-model",
        budget=budget,
    )

    assert budget.downgrade is not None
    assert budget.downgrade.to_model == "cheap-model"
    # $0.08 on the primary + 1M input tokens at the fallback's $0.01/1M.
    assert budget.spent_cost_usd == Decimal("0.09")


@pytest.mark.asyncio
async def test_max_tokens_is_re_derived_for_the_fallback(_quiet_langfuse):
    """``max_tokens`` was resolved from the primary at loop start; leaving it
    stale would send the fallback an allowance it does not accept."""
    composite, _ = _executor()
    primary = _ScriptedLLM([_expensive_first_call()], max_output_tokens=32_000)
    fallback = _ScriptedLLM(
        [ChatResponse(content="Short answer from the cheap model." + _PADDING,
                      model="cheap-model", usage={})],
        provider_name="cheap",
        max_output_tokens=4_096,
    )

    await al.run_agent_loop(
        llm=primary,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        model="expensive-model",
        budget=_budget(fallback),
    )

    assert primary.calls[0]["max_tokens"] == 32_000
    assert fallback.calls[0]["max_tokens"] == 4_096


@pytest.mark.asyncio
async def test_reasoning_effort_is_dropped_on_the_fallback(_quiet_langfuse):
    """A reasoning knob tuned for the primary can 400 on the fallback — the
    same reason the failover path strips it."""
    composite, _ = _executor()
    primary = _ScriptedLLM([_expensive_first_call()])
    fallback = _ScriptedLLM(
        [ChatResponse(content="Answer from the cheap model." + _PADDING,
                      model="cheap-model", usage={})],
        provider_name="cheap",
    )

    await al.run_agent_loop(
        llm=primary,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        model="expensive-model",
        reasoning_effort="high",
        budget=_budget(fallback),
    )

    assert primary.calls[0]["reasoning_effort"] == "high"
    assert fallback.calls[0]["reasoning_effort"] is None


# ── The closing synthesis ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_closing_synthesis_degrades_instead_of_a_digest(_quiet_langfuse):
    """When not even the held-back reserve covers the synthesis on the primary,
    writing the final answer on the cheap model beats returning a raw digest."""
    composite, executed = _executor()
    primary = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="expensive-model",
                stop_reason="tool_calls",
                tool_calls=[_tool_call("c1")],
                usage={"input_tokens": 40_000, "output_tokens": 10_000},  # $0.05
            )
        ]
    )
    fallback = _ScriptedLLM(
        [ChatResponse(content="Synthesised on the cheap model." + _PADDING,
                      model="cheap-model", usage={})],
        provider_name="cheap",
    )

    response = await al.run_agent_loop(
        llm=primary,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        model="expensive-model",
        # $0.0505 is chosen so the repriced call ($0.0502 projected) clears the
        # FULL limit but not the 90% in-loop gate ($0.0455): the tool-calling
        # phase still hard-stops, and only the synthesis degrades.
        budget=_budget(fallback, max_cost_usd=0.0505),
    )

    assert executed == ["lookup"]
    assert len(primary.calls) == 1
    assert response.content == "Synthesised on the cheap model." + _PADDING
    assert not response.content.startswith(BUDGET_EXHAUSTED_MESSAGE[:20])


# ── Guards: when the hard stop must stand ────────────────────────────────


@pytest.mark.asyncio
async def test_no_fallback_still_hard_stops(_quiet_langfuse):
    """Opt-out is the default: with no fallback configured, the hard stop."""
    composite, _ = _executor()
    # $0.0895 of $0.10: less than a floor-sized call left at the in-loop gate
    # (at $0.08 the next call is sized down instead).
    exhausting_first_call = _expensive_first_call()
    exhausting_first_call.usage = {"input_tokens": 45_000, "output_tokens": 44_500}
    primary = _ScriptedLLM(
        [
            exhausting_first_call,
            ChatResponse(content="Synthesised on the primary." + _PADDING,
                         model="expensive-model", usage={}),
        ]
    )

    response = await al.run_agent_loop(
        llm=primary,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        model="expensive-model",
        budget=_budget(None),
    )

    assert response.content == "Synthesised on the primary." + _PADDING
    # Two calls: the work, then the tool-free synthesis. No third one.
    assert _tool_shape(primary.calls) == ["tools", None]
    assert "lookup" in (primary.calls[0]["tools"] or [])


@pytest.mark.asyncio
async def test_a_fallback_too_small_for_the_system_prompt_is_refused(_quiet_langfuse):
    """Switching would force the emergency truncation of the persona and the
    security policy — worse than stopping."""
    composite, _ = _executor()
    primary = _ScriptedLLM(
        [
            _expensive_first_call(),
            ChatResponse(content="Synthesised on the primary." + _PADDING,
                         model="expensive-model", usage={}),
        ]
    )
    tiny = _ScriptedLLM([], provider_name="cheap", context_window=8_000)

    messages: list[ChatMessage] = [
        # Over half of the tiny fallback's 14,400-char session budget.
        ChatMessage(role="system", content="P" * 10_000),
        ChatMessage(role="user", content="go"),
    ]
    response = await al.run_agent_loop(
        llm=primary,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        model="expensive-model",
        budget=_budget(tiny),
    )

    assert tiny.calls == [], "no call may be placed on a fallback that cannot hold the prompt"
    assert response.content == "Synthesised on the primary." + _PADDING


@pytest.mark.asyncio
async def test_a_fallback_that_cannot_carry_the_tool_surface_is_refused(_quiet_langfuse):
    """Dropping tools the model has already been told about mid-run is worse
    than stopping."""
    composite, _ = _executor()
    primary = _ScriptedLLM(
        [
            _expensive_first_call(),
            ChatResponse(content="Synthesised on the primary." + _PADDING,
                         model="expensive-model", usage={}),
        ]
    )
    no_tools = _ScriptedLLM([], provider_name="cheap", max_tools=0)

    response = await al.run_agent_loop(
        llm=primary,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        model="expensive-model",
        budget=_budget(no_tools),
    )

    assert no_tools.calls == []
    assert response.content == "Synthesised on the primary." + _PADDING


@pytest.mark.asyncio
async def test_a_gated_run_moves_to_a_tighter_provider_by_re_capping_the_gate(
    _quiet_langfuse,
):
    """With progressive disclosure active the fallback only has to carry the
    gate's REQUIRED budget (core + ``load_tools`` + one gated slot; the
    optional timer is not counted, it yields its slot when the list is full)
    — the inventory is never on the wire.  A fallback whose cap is far below the agent's tool
    count is therefore accepted; the gate then unloads the least recently used
    group to fit the new cap and the model is told which.  Before, the
    same run was refused for "offering 41 tools" to a provider capping at 24."""
    executed: list[str] = []
    callable_exec = CallableToolExecutor()
    definitions = [
        ToolDefinition(name=f"{p}_tool_{i}", description="d", source=f"integration:{p}")
        for p in ("a", "b")
        for i in range(20)
    ]

    def _make_handler(tool_name: str):
        async def _handler(**kwargs):
            executed.append(tool_name)
            return f"ran {tool_name}"

        return _handler

    for definition in definitions:
        callable_exec.register(definition.name, _make_handler(definition.name))
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)

    def _load(call_id: str, group: str) -> ChatResponse:
        return ChatResponse(
            content="",
            model="expensive-model",
            stop_reason="tool_calls",
            tool_calls=[
                ToolCall(id=call_id, name="load_tools", arguments={"groups": [group]})
            ],
            usage={"input_tokens": 1_000, "output_tokens": 100},
        )

    primary = _ScriptedLLM(
        [
            _load("c1", "integration:a"),
            _load("c2", "integration:b"),
            # Calling into group A makes it the most recently used one, then
            # the cost of this call trips the gate for the NEXT call.
            ChatResponse(
                content="",
                model="expensive-model",
                stop_reason="tool_calls",
                tool_calls=[ToolCall(id="c3", name="a_tool_0", arguments={})],
                usage={"input_tokens": 40_000, "output_tokens": 40_000},
            ),
        ],
        max_tools=1024,
    )
    tight = _ScriptedLLM(
        [
            ChatResponse(
                content="Finished on the cheaper model." + _PADDING,
                model="cheap-model",
                usage={},
            )
        ],
        provider_name="cheap",
        max_tools=24,
    )

    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    response = await al.run_agent_loop(
        llm=primary,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        model="expensive-model",
        budget=_budget(tight),
        progressive_tools=True,
    )

    assert response.content == "Finished on the cheaper model." + _PADDING
    assert executed == ["a_tool_0"]
    # Both groups were on the wire on the primary (41 + the timer)...
    assert sum(1 for n in primary.calls[2]["tools"] if n.startswith("b_tool_")) == 20
    # ...and the fallback got a list that fits its cap: group B (least
    # recently used) was unloaded, group A (just called into) survived.
    assert len(tight.calls) == 1
    offered = tight.calls[0]["tools"] or []
    assert len(offered) <= 24
    assert sum(1 for n in offered if n.startswith("a_tool_")) == 20
    assert not any(n.startswith("b_tool_") for n in offered)
    assert "load_tools" in offered
    assert any(
        m.role == "system"
        and "least recently used tool group(s) were unloaded: integration:b" in (m.content or "")
        for m in messages
    )
    # The catalogue the model reads was rewritten IN PLACE for the new cap:
    # exactly one catalogue message, carrying the fallback's budget paragraph.
    catalogues = [
        m for m in messages if m.role == "system" and "Available tool groups" in (m.content or "")
    ]
    assert len(catalogues) == 1
    assert "Tool budget: this model accepts at most 24 tool schemas" in catalogues[0].content


@pytest.mark.asyncio
async def test_a_gated_run_is_not_moved_onto_a_provider_that_cannot_hold_a_gate(
    _quiet_langfuse,
):
    """A gate always sends ``load_tools`` and needs room for one real tool
    (``MIN_GATED_BUDGET``): a fallback capping at 1 could never reveal
    anything, and at 0 it would be over-cap.  The gate's required budget
    (core 0 + meta-tool + one gated slot = 2) carries that floor, so guard 2
    refuses such a fallback for the tool-calling phase instead of re-capping
    the gate to it; the run hard-stops on the primary as for any surface the
    fallback cannot carry."""
    callable_exec = CallableToolExecutor()
    definitions = [
        ToolDefinition(name=f"a_tool_{i}", description="d", source="integration:a")
        for i in range(20)
    ]
    for definition in definitions:
        callable_exec.register(definition.name, lambda **kwargs: "ran")
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)

    primary = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="expensive-model",
                stop_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="c1", name="load_tools", arguments={"groups": ["integration:a"]}
                    )
                ],
                usage={"input_tokens": 40_000, "output_tokens": 40_000},
            ),
            ChatResponse(
                content="Synthesised on the primary." + _PADDING,
                model="expensive-model",
                usage={},
            ),
        ],
        max_tools=1024,
    )
    one_tool = _ScriptedLLM(
        [ChatResponse(content="Finished on the cheaper model." + _PADDING,
                      model="cheap-model", usage={})],
        provider_name="cheap",
        max_tools=1,
    )

    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    response = await al.run_agent_loop(
        llm=primary,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        model="expensive-model",
        budget=_budget(one_tool),
        progressive_tools=True,
    )

    assert "load_tools" in primary.calls[0]["tools"], "the run was gated"
    assert one_tool.calls == [], "no tool-calling request may land on a 1-tool provider"
    assert response.content == "Synthesised on the primary." + _PADDING


@pytest.mark.asyncio
async def test_the_iteration_span_reports_the_re_capped_surface_not_the_snapshot(
    _quiet_langfuse, monkeypatch
):
    """The iteration span is opened with the tool list the loop is ABOUT to
    offer; a budget downgrade inside the same iteration then re-caps the gate
    and unloads a group before the request is placed.  The span must report
    what actually went out - the post-eviction list - not the pre-downgrade
    snapshot naming the evicted group."""
    spans: list[dict] = []

    def _record_span(**kwargs):
        ctx = {
            "trace": None,
            "local_input": {
                "iteration": kwargs["iteration"],
                "available_tools": kwargs.get("available_tools"),
            },
        }
        spans.append(ctx)
        return ctx

    monkeypatch.setattr(al, "_lf_start_iter_span", _record_span)

    callable_exec = CallableToolExecutor()
    definitions = [
        ToolDefinition(name=f"{p}_tool_{i}", description="d", source=f"integration:{p}")
        for p in ("a", "b")
        for i in range(20)
    ]
    for definition in definitions:
        callable_exec.register(definition.name, lambda **kwargs: "ran")
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)

    def _load(call_id: str, group: str) -> ChatResponse:
        return ChatResponse(
            content="",
            model="expensive-model",
            stop_reason="tool_calls",
            tool_calls=[
                ToolCall(id=call_id, name="load_tools", arguments={"groups": [group]})
            ],
            usage={"input_tokens": 1_000, "output_tokens": 100},
        )

    primary = _ScriptedLLM(
        [
            _load("c1", "integration:a"),
            _load("c2", "integration:b"),
            ChatResponse(
                content="",
                model="expensive-model",
                stop_reason="tool_calls",
                tool_calls=[ToolCall(id="c3", name="a_tool_0", arguments={})],
                usage={"input_tokens": 40_000, "output_tokens": 40_000},
            ),
        ],
        max_tools=1024,
    )
    tight = _ScriptedLLM(
        [ChatResponse(content="Finished." + _PADDING, model="cheap-model", usage={})],
        provider_name="cheap",
        max_tools=24,
    )

    await al.run_agent_loop(
        llm=primary,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        model="expensive-model",
        budget=_budget(tight),
        progressive_tools=True,
    )

    assert len(tight.calls) == 1
    # Iteration 4 is the one that downgraded mid-flight: its span must match
    # the fallback's request exactly - group B gone, group A kept.
    span_tools = spans[3]["local_input"]["available_tools"]
    assert span_tools == tight.calls[0]["tools"]
    assert not any(n.startswith("b_tool_") for n in span_tools)
    assert sum(1 for n in span_tools if n.startswith("a_tool_")) == 20
    # The earlier iterations still report what THEY offered (both groups).
    assert sum(1 for n in spans[2]["local_input"]["available_tools"] if n.startswith("b_tool_")) == 20


@pytest.mark.asyncio
async def test_re_capping_the_gate_rewrites_the_catalogue_even_when_nothing_is_evicted(
    _quiet_langfuse,
):
    """The primary's cap (1024) does not bind, so the catalogue the run started
    with has no budget paragraph.  The run moves to a 24-tool fallback before
    any group is loaded: nothing is evicted, so no eviction notice is due —
    but the cap now binds, and a model left with the original catalogue would
    learn it only from a surprising partial reveal.  The catalogue message is
    rewritten in place, not left stale next to an appended note."""
    executed: list[str] = []
    callable_exec = CallableToolExecutor()
    definitions = [
        ToolDefinition(name="lookup", description="lookup", source="builtin:test"),
        *(
            ToolDefinition(name=f"{p}_tool_{i}", description="d", source=f"integration:{p}")
            for p in ("a", "b")
            for i in range(20)
        ),
    ]

    def _make_handler(tool_name: str):
        async def _handler(**kwargs):
            executed.append(tool_name)
            return f"ran {tool_name}"

        return _handler

    for definition in definitions:
        callable_exec.register(definition.name, _make_handler(definition.name))
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)

    # The always-on ``lookup`` is called (no group loaded) and the cost of
    # that call trips the gate for the next one.
    primary = _ScriptedLLM([_expensive_first_call()], max_tools=1024)
    tight = _ScriptedLLM(
        [
            ChatResponse(
                content="Finished on the cheaper model." + _PADDING,
                model="cheap-model",
                usage={},
            )
        ],
        provider_name="cheap",
        max_tools=24,
    )

    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    response = await al.run_agent_loop(
        llm=primary,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        model="expensive-model",
        budget=_budget(tight),
        progressive_tools=True,
    )

    assert response.content == "Finished on the cheaper model." + _PADDING
    assert executed == ["lookup"]
    assert len(tight.calls) == 1
    catalogues = [
        m for m in messages if m.role == "system" and "Available tool groups" in (m.content or "")
    ]
    assert len(catalogues) == 1, "rewritten in place, never duplicated"
    assert "Tool budget: this model accepts at most 24 tool schemas" in catalogues[0].content
    assert not any(
        "tool group(s) were unloaded" in (m.content or "") for m in messages
    ), "nothing was loaded, so nothing was evicted and no eviction notice is due"


@pytest.mark.asyncio
async def test_the_tool_surface_counted_excludes_the_optional_timer(_quiet_langfuse):
    """The loop offers its own ``wait`` tool on top of the agent's —
    but only while the offered list is below the provider's cap
    (``_with_wait_tool``), so the timer is optional and yields its slot.  A
    fallback with room for exactly the MANDATORY surface (here: one agent
    tool) must therefore be accepted, and the request it gets carries exactly
    ``max_tools`` tools with ``wait`` omitted.  Counting the timer refused
    this exact-fit fallback for a slot the timer would never have taken.
    """
    composite, executed = _executor()
    primary = _ScriptedLLM([_expensive_first_call()])
    one_slot = _ScriptedLLM(
        [
            ChatResponse(
                content="Finished on the cheaper model." + _PADDING,
                model="cheap-model",
                usage={},
            )
        ],
        provider_name="cheap",
        max_tools=1,
    )

    response = await al.run_agent_loop(
        llm=primary,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        model="expensive-model",
        budget=_budget(one_slot),
    )

    # The primary (cap 128) was offered the tool AND the timer...
    assert primary.calls[0]["tools"] == ["lookup", "wait"]
    assert executed == ["lookup"]
    # ...the exact-fit fallback was accepted and got exactly its cap, timer
    # omitted — the same shape ``_with_wait_tool`` produces on any full list.
    assert len(one_slot.calls) == 1
    assert one_slot.calls[0]["tools"] == ["lookup"]
    assert response.content == "Finished on the cheaper model." + _PADDING


def _gated_core_plus_one_tool_group(
    group_size: int,
) -> tuple[CompositeToolExecutor, list[str]]:
    """One always-on ``lookup`` plus one gated group of *group_size* tools."""
    executed: list[str] = []
    callable_exec = CallableToolExecutor()
    definitions = [
        ToolDefinition(name="lookup", description="lookup", source="builtin:test"),
        *(
            ToolDefinition(name=f"a_tool_{i}", description="d", source="integration:a")
            for i in range(group_size)
        ),
    ]

    def _make_handler(tool_name: str):
        async def _handler(**kwargs):
            executed.append(tool_name)
            return f"ran {tool_name}"

        return _handler

    for definition in definitions:
        callable_exec.register(definition.name, _make_handler(definition.name))
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)
    return composite, executed


def _gated_primary_that_loads_a_then_trips_the_budget() -> _ScriptedLLM:
    return _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="expensive-model",
                stop_reason="tool_calls",
                tool_calls=[
                    ToolCall(id="c1", name="load_tools", arguments={"groups": ["integration:a"]})
                ],
                usage={"input_tokens": 1_000, "output_tokens": 100},
            ),
            ChatResponse(
                content="",
                model="expensive-model",
                stop_reason="tool_calls",
                tool_calls=[ToolCall(id="c2", name="a_tool_0", arguments={})],
                usage={"input_tokens": 40_000, "output_tokens": 40_000},
            ),
            ChatResponse(
                content="Synthesised on the primary." + _PADDING,
                model="expensive-model",
                usage={},
            ),
        ],
        max_tools=1024,
    )


@pytest.mark.asyncio
async def test_a_gated_exact_fit_fallback_is_accepted_and_the_timer_omitted(
    _quiet_langfuse,
):
    """Same exact-fit rule with progressive disclosure: the gate's required
    budget is the always-on core + ``load_tools`` + one slot for a gated tool
    (3 here), so a fallback capping at 22 is accepted; the 20-tool group
    already loaded then fills the list exactly (1 + 1 + 20), nothing is
    unloaded, and the request carries ``lookup``, ``load_tools`` and the
    group — no timer."""
    composite, executed = _gated_core_plus_one_tool_group(20)
    primary = _gated_primary_that_loads_a_then_trips_the_budget()
    exact = _ScriptedLLM(
        [
            ChatResponse(
                content="Finished on the cheaper model." + _PADDING,
                model="cheap-model",
                usage={},
            )
        ],
        provider_name="cheap",
        max_tools=22,
    )

    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    response = await al.run_agent_loop(
        llm=primary,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        model="expensive-model",
        budget=_budget(exact),
        progressive_tools=True,
    )

    assert response.content == "Finished on the cheaper model." + _PADDING
    assert executed == ["a_tool_0"]
    assert "wait" in primary.calls[1]["tools"], "the primary had room for the timer"
    assert len(exact.calls) == 1
    sent = exact.calls[0]["tools"]
    assert len(sent) == 22 and "wait" not in sent
    assert {"lookup", "load_tools", *(f"a_tool_{i}" for i in range(20))} == set(sent)
    assert not any(
        "were unloaded" in (m.content or "") or "moved to" in (m.content or "")
        for m in messages
        if m.role == "system"
    ), "an exact fit changes nothing the model was told"


@pytest.mark.asyncio
async def test_a_fallback_one_slot_short_of_a_gated_tool_is_refused(_quiet_langfuse):
    """A fallback that holds the core and ``load_tools`` but no gated tool
    (cap 2 for a core of 1) is refused: re-capping the gate to it would have
    to demote ``lookup`` — a tool the model has been using as always-on —
    into the loadable overflow group mid-run, or leave a gate that can never
    reveal anything.  Either changes what the model was told, so the run
    hard-stops on the primary as for any surface the fallback cannot carry.
    """
    composite, executed = _gated_core_plus_one_tool_group(20)
    primary = _gated_primary_that_loads_a_then_trips_the_budget()
    short = _ScriptedLLM(
        [ChatResponse(content="Finished on the cheaper model." + _PADDING,
                      model="cheap-model", usage={})],
        provider_name="cheap",
        max_tools=2,
    )

    response = await al.run_agent_loop(
        llm=primary,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        model="expensive-model",
        budget=_budget(short),
        progressive_tools=True,
    )

    assert executed == ["a_tool_0"]
    assert short.calls == [], "no tool-calling request may land on a 2-tool provider"
    assert response.content == "Synthesised on the primary." + _PADDING


@pytest.mark.asyncio
async def test_cross_provider_structured_output_is_refused(_quiet_langfuse):
    """The structured-output payload is formatted for the primary's provider;
    dropping it would silently break the run's output contract."""
    composite, _ = _executor()
    primary = _ScriptedLLM(
        [
            _expensive_first_call(),
            ChatResponse(content='{"ok": true}' + _PADDING, model="expensive-model",
                         usage={}),
        ]
    )
    other_provider = _ScriptedLLM([], provider_name="cheap")

    response = await al.run_agent_loop(
        llm=primary,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        model="expensive-model",
        structured_output={"type": "json_schema", "json_schema": {"name": "out"}},
        budget=_budget(other_provider),
    )

    assert other_provider.calls == []
    assert response.content == '{"ok": true}' + _PADDING


@pytest.mark.asyncio
async def test_a_fallback_that_is_also_unaffordable_hard_stops(_quiet_langfuse):
    """Repricing is a real check: a fallback that is barely cheaper cannot save
    a budget that is simply gone."""
    composite, _ = _executor()
    primary = _ScriptedLLM(
        [
            _expensive_first_call(),
            ChatResponse(content="Synthesised on the primary." + _PADDING,
                         model="expensive-model", usage={}),
        ]
    )
    barely_cheaper = _ScriptedLLM([], provider_name="cheap")

    response = await al.run_agent_loop(
        llm=primary,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        model="expensive-model",
        budget=_budget(barely_cheaper, fallback_price="1"),
    )

    assert barely_cheaper.calls == []
    assert response.content == "Synthesised on the primary." + _PADDING


@pytest.mark.asyncio
async def test_only_one_downgrade_then_the_run_stops(_quiet_langfuse):
    """Primary -> fallback -> stop.  Never a ladder down through progressively
    worse models."""
    composite, _ = _executor()
    primary = _ScriptedLLM([_expensive_first_call()])
    fallback = _ScriptedLLM(
        [
            # Burns the rest of the budget on the fallback too.
            ChatResponse(
                content="",
                model="cheap-model",
                stop_reason="tool_calls",
                tool_calls=[_tool_call("c2")],
                usage={"input_tokens": 1_000_000, "output_tokens": 0},
            ),
            ChatResponse(content="Wrapped up on the cheap model." + _PADDING,
                         model="cheap-model", usage={}),
        ],
        provider_name="cheap",
    )
    budget = _budget(fallback)

    response = await al.run_agent_loop(
        llm=primary,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        model="expensive-model",
        budget=budget,
    )

    assert budget.fallback_available is False, "the one downgrade is spent"
    # Two calls on the fallback: the work, then the tool-free synthesis it can
    # still afford against the full limit. No third downgrade attempt.
    assert _tool_shape(fallback.calls) == ["tools", None]
    assert response.content == "Wrapped up on the cheap model." + _PADDING
