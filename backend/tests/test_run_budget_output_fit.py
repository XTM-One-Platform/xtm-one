"""A run budget sizes a call to what it can still pay for.

The reserve rule prices every model call at its worst case: the
prompt plus the model's FULL ``max_tokens``.  With output
ceilings at 128k and Bedrock Claude priced, that worst case no
longer fits small budgets, and every run would be refused before its first call.

The decision: a call the budget cannot cover in full is made with the largest
``max_tokens`` the remaining budget covers, down to ``MIN_OUTPUT_TOKENS``;
below that floor it is refused exactly as before.  What these tests pin:

* the sized allowance, when cost binds, when tokens bind, and when both are
  enforced (the stricter wins);
* the refusal below the floor is today's refusal;
* an affordable budget - and no budget - leave the reservation and the request
  exactly as they were;
* the two audit examples run again;
* across a run that ends on its budget, no call is reserved past what is left,
  and a sized answer that is cut is not continued past the budget;
* a sized call is priced on what it is sent with: the messages the loop
  appends after pricing it and the tool definitions it carries;
* the calls that follow a sized call stay priced, even once one fits in full;
* a sized answer the budget cuts, or a sized synthesis that comes back empty,
  ends with the budget message instead of reading as complete, and a cut
  answer says so in its stop reason too, which the structured-output repair
  of the chat and the assignment reads;
* a sized call is charged once, what its provider reported, under the model
  that served it;
* every provider request stays valid with a lowered ``max_tokens``.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from datetime import date
from decimal import Decimal

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.llm.bedrock_provider import _build_inference_config
from app.llm.model_capabilities import build_openai_generation_params
from app.llm.router import FailoverLLMProvider
from app.services import langfuse_tracing as lft
from app.services.tracing.cost_calculator import ModelPrice
from app.tools import agent_loop as al
from app.tools.agent_loop import BUDGET_EXHAUSTED_MESSAGE
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
)
from app.tools.run_budget import MIN_OUTPUT_TOKENS, BudgetDimension, RunBudget, count_usage_tokens
from tests.test_mixed_provider_run_usage import (
    _ANTHROPIC_CALL,
    _LONG_ANSWER,
    _OPENAI_CALL,
    _flat_sum,
    _overloaded,
    _priced,
    _recording_root,
    _reply,
    _Served,
    _tool_turn,
)
from tests.test_mixed_provider_run_usage import _PRICES as _SERVING_MODEL_PRICES

_PADDING = (
    " The tool results were reviewed and the findings above summarise "
    "everything the run was able to establish before it stopped."
)


def _price(input_per_1m: str, output_per_1m: str, model_id: str = "stub-model") -> ModelPrice:
    return ModelPrice(
        provider="stub",
        model_id=model_id,
        model_pattern=None,
        input_price_per_1m=Decimal(input_per_1m),
        output_price_per_1m=Decimal(output_per_1m),
        cache_read_price_per_1m=None,
        cache_write_price_per_1m=None,
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )


# The seeded regional Bedrock row for Claude Sonnet 5 (``model_prices.json``).
_BEDROCK_SONNET_5 = _price("2.2", "11", "anthropic.claude-sonnet-5")


# ── RunBudget ────────────────────────────────────────────────────────────


def test_a_cost_budget_sizes_the_call_to_what_it_can_pay_for():
    budget = RunBudget(max_cost_usd=0.10, price=_price("1", "1"), final_answer_reserve_fraction=0.0)
    budget.record_llm_usage({"input_tokens": 50_000, "output_tokens": 40_000})  # $0.09

    assert budget.try_reserve(estimated_input_tokens=2_000, max_output_tokens=16_384).granted is False
    sized = budget.try_reserve(
        estimated_input_tokens=2_000, max_output_tokens=16_384, min_output_tokens=MIN_OUTPUT_TOKENS
    )

    # $0.01 left at $1 per 1M: 10,000 tokens, 2,000 of them the prompt.
    assert sized.granted
    assert sized.max_output_tokens == 8_000
    assert sized.tokens == 10_000
    assert sized.cost_usd == Decimal("0.01")


def test_a_token_budget_sizes_the_call_to_what_is_left():
    budget = RunBudget(max_total_tokens=100_000, final_answer_reserve_fraction=0.1)
    budget.record_llm_usage({"total_tokens": 60_000})

    sized = budget.try_reserve(
        estimated_input_tokens=12_000, max_output_tokens=128_000, min_output_tokens=MIN_OUTPUT_TOKENS
    )

    # 90,000-token in-loop gate - 60,000 spent - 12,000 of prompt.
    assert sized.granted
    assert sized.max_output_tokens == 18_000


def test_the_stricter_of_cost_and_tokens_binds():
    budget = RunBudget(
        max_cost_usd=1.0,
        max_total_tokens=50_000,
        price=_price("1", "1"),
        final_answer_reserve_fraction=0.0,
    )
    # Cost would allow ~990k output tokens; the token limit allows 40,000.
    sized = budget.try_reserve(
        estimated_input_tokens=10_000, max_output_tokens=128_000, min_output_tokens=MIN_OUTPUT_TOKENS
    )
    assert sized.max_output_tokens == 40_000

    budget = RunBudget(
        max_cost_usd=0.02,
        max_total_tokens=1_000_000,
        price=_price("1", "1"),
        final_answer_reserve_fraction=0.0,
    )
    # Now cost is the stricter one: $0.02 is 20,000 tokens, 10,000 of prompt.
    sized = budget.try_reserve(
        estimated_input_tokens=10_000, max_output_tokens=128_000, min_output_tokens=MIN_OUTPUT_TOKENS
    )
    assert sized.max_output_tokens == 10_000


def test_below_the_floor_the_refusal_is_the_one_of_the_full_allowance():
    def budget() -> RunBudget:
        b = RunBudget(max_total_tokens=10_000, final_answer_reserve_fraction=0.0)
        b.record_llm_usage({"total_tokens": 8_500})
        return b

    unsized = budget().try_reserve(estimated_input_tokens=1_000, max_output_tokens=16_384)
    sized_budget = budget()
    sized = sized_budget.try_reserve(
        estimated_input_tokens=1_000, max_output_tokens=16_384, min_output_tokens=MIN_OUTPUT_TOKENS
    )

    # 500 tokens left: below the 1,024-token floor.
    assert sized.granted is False
    assert sized.violation == unsized.violation
    assert sized.violation.dimension is BudgetDimension.TOKENS
    assert sized.violation.user_message() == unsized.violation.user_message()
    # A refusal holds nothing.
    assert sized_budget.try_reserve(estimated_input_tokens=100, max_output_tokens=100).granted


def test_an_affordable_call_keeps_its_full_reservation():
    def reserve(**kwargs):
        b = RunBudget(max_cost_usd=5.0, max_total_tokens=1_000_000, price=_price("3", "15"))
        return b.try_reserve(estimated_input_tokens=20_000, max_output_tokens=64_000, **kwargs)

    plain = reserve()
    sized = reserve(min_output_tokens=MIN_OUTPUT_TOKENS)

    assert plain.granted and sized.granted
    assert sized.max_output_tokens == plain.max_output_tokens == 64_000
    assert (sized.cost_usd, sized.tokens) == (plain.cost_usd, plain.tokens)


def test_the_floor_never_exceeds_the_call_s_own_allowance():
    """A model whose ceiling is below the floor is never asked for more."""
    budget = RunBudget(max_total_tokens=1_500, final_answer_reserve_fraction=0.0)
    budget.record_llm_usage({"total_tokens": 1_000})

    sized = budget.try_reserve(
        estimated_input_tokens=100, max_output_tokens=600, min_output_tokens=MIN_OUTPUT_TOKENS
    )
    assert sized.granted is False, "600 + 100 do not fit in 500: nothing below 600 is offered"
    assert budget.affordable_output_tokens(estimated_input_tokens=100, max_output_tokens=300) == 300


def test_audit_example_bedrock_sonnet_5_with_a_one_dollar_budget():
    """128,000 x $11 per 1M = $1.41 of output against a $0.90 gate."""
    budget = RunBudget(max_cost_usd=1.0, price=_BEDROCK_SONNET_5)

    assert budget.try_reserve(estimated_input_tokens=10_000, max_output_tokens=128_000).granted is False
    sized = budget.try_reserve(
        estimated_input_tokens=10_000, max_output_tokens=128_000, min_output_tokens=MIN_OUTPUT_TOKENS
    )

    # ($0.90 - 10,000 x $2.2 per 1M) / $11 per 1M
    assert sized.granted
    assert sized.max_output_tokens == 79_818
    assert sized.cost_usd <= Decimal("0.90")


def test_audit_example_mythos_with_a_100k_token_budget():
    budget = RunBudget(max_total_tokens=100_000)

    assert budget.try_reserve(estimated_input_tokens=12_000, max_output_tokens=128_000).granted is False
    sized = budget.try_reserve(
        estimated_input_tokens=12_000, max_output_tokens=128_000, min_output_tokens=MIN_OUTPUT_TOKENS
    )
    assert sized.granted
    assert sized.max_output_tokens == 78_000


# ── The agent loop ───────────────────────────────────────────────────────


@pytest.fixture
def _quiet_langfuse(monkeypatch):
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)


class _ScriptedLLM(BaseLLMProvider):
    """Stub LLM recording each call's ``max_tokens`` and the tools that rode along."""

    provider_name = "stub"
    context_window = 1_000_000

    def __init__(self, script) -> None:
        self._script = script
        self.max_tokens: list[int] = []
        self.with_tools: list[bool] = []
        self.tools: list[list[ToolDefinition] | None] = []

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self.max_tokens.append(kwargs["max_tokens"])
        self.with_tools.append(tools is not None)
        self.tools.append(tools)
        step = self._script[min(len(self.max_tokens), len(self._script)) - 1]
        return step(messages, kwargs["max_tokens"]) if callable(step) else step

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


class _StreamedScriptedLLM(_ScriptedLLM):
    """The same script, streamed through the base provider's fallback."""

    chat_stream = BaseLLMProvider.chat_stream


def _executor(description: str = "lookup") -> CompositeToolExecutor:
    callable_exec = CallableToolExecutor()

    async def _handler(**kwargs):
        return "ran lookup"

    callable_exec.register("lookup", _handler)
    composite = CompositeToolExecutor()
    composite.add_tools(
        [ToolDefinition(name="lookup", description=description, source="custom:test")], callable_exec
    )
    return composite


def _tool_round(call_id: str, usage: dict | None = None) -> ChatResponse:
    return ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[ToolCall(id=call_id, name="lookup", arguments={"q": call_id})],
        usage=usage or {},
    )


async def _run(
    llm, budget, *, model=None, max_tokens=None, with_tools=True, tool_description="lookup", on_token=None
):
    composite = _executor(tool_description)
    return await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools if with_tools else None,
        executor=composite if with_tools else None,
        max_iterations=10,
        model=model,
        max_tokens=max_tokens,
        budget=budget,
        on_token=on_token,
    )


_ANSWER = ChatResponse(content="Answered." + _PADDING, model="stub", usage={})


@pytest.mark.asyncio
async def test_no_budget_and_an_affordable_budget_send_the_full_allowance(_quiet_langfuse):
    unbudgeted = _ScriptedLLM([_tool_round("c1"), _ANSWER])
    await _run(unbudgeted, None, max_tokens=16_384)

    affordable = _ScriptedLLM([_tool_round("c1"), _ANSWER])
    await _run(affordable, RunBudget(max_total_tokens=1_000_000), max_tokens=16_384)

    assert unbudgeted.max_tokens == affordable.max_tokens == [16_384, 16_384]
    assert unbudgeted.with_tools == affordable.with_tools == [True, True]


@pytest.mark.asyncio
async def test_audit_example_bedrock_sonnet_5_runs_again(_quiet_langfuse):
    """Refused before its first call on main; now made with a lowered allowance."""
    model = "us.anthropic.claude-sonnet-5-20260901-v1:0"
    llm = _ScriptedLLM([_ANSWER])
    assert al._resolve_effective_max_tokens(llm, model) == 128_000

    response = await _run(llm, RunBudget(max_cost_usd=1.0, price=_BEDROCK_SONNET_5), model=model)

    assert response.content == _ANSWER.content
    assert len(llm.max_tokens) == 1
    assert MIN_OUTPUT_TOKENS <= llm.max_tokens[0] < 128_000


@pytest.mark.asyncio
async def test_audit_example_mythos_runs_again_on_a_100k_token_budget(_quiet_langfuse):
    model = "claude-mythos-5-1"
    llm = _ScriptedLLM([_ANSWER])
    assert al._resolve_effective_max_tokens(llm, model) == 128_000

    response = await _run(llm, RunBudget(max_total_tokens=100_000), model=model)

    assert response.content == _ANSWER.content
    assert MIN_OUTPUT_TOKENS <= llm.max_tokens[0] < 90_000


@pytest.mark.asyncio
async def test_a_budget_below_the_floor_still_refuses_before_any_call(_quiet_langfuse):
    llm = _ScriptedLLM([_ANSWER])

    response = await _run(llm, RunBudget(max_total_tokens=900), max_tokens=16_384)

    assert llm.max_tokens == []
    assert response.content.startswith(
        BUDGET_EXHAUSTED_MESSAGE.format(reason="this run reached its token budget (900 tokens)")
    )


@pytest.mark.asyncio
async def test_a_run_that_spends_its_worst_case_never_reserves_past_its_budget(_quiet_langfuse):
    """Every call spends its whole reservation; the run ends on its budget."""
    limit = 60_000
    budget = RunBudget(max_total_tokens=limit)
    gate = limit * (1 - budget.final_answer_reserve_fraction)
    held: list[float] = []

    def step(messages, max_tokens):
        if not llm.with_tools[-1]:
            return ChatResponse(content="Synthesised." + _PADDING, model="stub", usage={"output_tokens": max_tokens})
        reserved = budget._reserved_tokens()
        held.append(budget.spent_tokens + reserved)
        return _tool_round(
            f"c{len(held)}", {"input_tokens": reserved - max_tokens, "output_tokens": max_tokens}
        )

    llm = _ScriptedLLM([step])
    response = await _run(llm, budget, max_tokens=16_384)

    tool_rounds = [m for m, tools in zip(llm.max_tokens, llm.with_tools, strict=True) if tools]
    assert tool_rounds[:3] == [16_384] * 3, "calls the budget covers keep their full allowance"
    assert MIN_OUTPUT_TOKENS <= tool_rounds[3] < 16_384, "the fourth call is sized to what is left"
    assert all(h <= gate for h in held), "no in-loop call is reserved past the gate"
    assert llm.with_tools[-1] is False and llm.max_tokens[-1] < 16_384, "a sized synthesis"
    assert budget.spent_tokens <= limit
    assert response.content == "Synthesised." + _PADDING


@pytest.mark.asyncio
async def test_a_sized_answer_that_is_cut_is_not_continued_past_the_budget(_quiet_langfuse):
    budget = RunBudget(max_total_tokens=12_000)

    def cut(messages, max_tokens):
        reserved = budget._reserved_tokens()
        return ChatResponse(
            content="The first part of a long answer." + _PADDING,
            model="stub",
            stop_reason="max_tokens",
            usage={"input_tokens": reserved - max_tokens, "output_tokens": max_tokens},
        )

    llm = _ScriptedLLM([cut, _ANSWER])
    response = await _run(llm, budget, max_tokens=16_384, with_tools=False)

    assert len(llm.max_tokens) == 1, "the continuation would have spent past the budget"
    assert llm.max_tokens[0] < 16_384
    assert budget.spent_tokens <= 12_000 * (1 - budget.final_answer_reserve_fraction)
    assert response.content.startswith("The first part of a long answer.")
    assert response.content.endswith(_budget_cut("this run reached its token budget (12,000 tokens)"))
    assert al.answer_cut_by_budget(response)


@pytest.mark.asyncio
async def test_a_cut_answer_is_still_continued_when_the_call_was_not_sized(_quiet_langfuse):
    cut = ChatResponse(content="First part." + _PADDING, model="stub", stop_reason="max_tokens", usage={})
    llm = _ScriptedLLM([cut, _ANSWER])

    response = await _run(llm, RunBudget(max_total_tokens=1_000_000), max_tokens=16_384, with_tools=False)

    assert llm.max_tokens == [16_384, 16_384]
    assert not al.answer_cut_by_budget(response)


def _billed_in_full(messages, max_tokens, tools=None) -> dict:
    """A call that runs to its cap: billed for what it was sent - its transcript
    and the tool definitions it carried - and its whole allowance."""
    sent_chars = al._transcript_chars(messages) + sum(al._tool_definition_chars(tool) for tool in tools or ())
    return {"input_tokens": sent_chars // al._CHARS_PER_TOKEN, "output_tokens": max_tokens}


def _budget_cut(reason: str) -> str:
    """How a sized answer the budget could not continue ends."""
    return "\n\n" + BUDGET_EXHAUSTED_MESSAGE.format(reason=reason)


@pytest.mark.asyncio
async def test_a_sized_synthesis_is_priced_with_the_instructions_it_is_sent_with(_quiet_langfuse):
    limit = 40_000
    budget = RunBudget(max_total_tokens=limit)
    sent: list[list[ChatMessage]] = []

    def step(messages, max_tokens):
        sent.append(list(messages))
        if llm.with_tools[-1]:
            # Too much spent for a floor-sized call at the in-loop gate.
            return _tool_round("c1", {"input_tokens": 35_000, "output_tokens": 500})
        return ChatResponse(
            content="Synthesised." + _PADDING, model="stub", usage=_billed_in_full(messages, max_tokens)
        )

    llm = _ScriptedLLM([step])
    response = await _run(llm, budget, max_tokens=16_384)

    assert llm.with_tools == [True, False]
    assert any(m.content == al._FORCED_FINAL_ANSWER for m in sent[-1])
    assert MIN_OUTPUT_TOKENS <= llm.max_tokens[-1] < 16_384, "a sized synthesis"
    assert budget.spent_tokens <= limit
    assert response.content == "Synthesised." + _PADDING


@pytest.mark.asyncio
async def test_a_sized_call_is_priced_with_the_nudge_it_is_sent_with(_quiet_langfuse):
    limit = 30_000
    # No closing reserve: the in-loop gate is the configured limit itself.
    budget = RunBudget(max_total_tokens=limit, final_answer_reserve_fraction=0.0)
    sent: list[list[ChatMessage]] = []

    def step(messages, max_tokens):
        sent.append(list(messages))
        if len(sent) == 1:
            # Past the warning threshold, and too much spent for the full allowance.
            return _tool_round("c1", {"input_tokens": 24_500, "output_tokens": 500})
        return ChatResponse(
            content="Answered." + _PADDING,
            model="stub",
            usage=_billed_in_full(messages, max_tokens, llm.tools[-1]),
        )

    llm = _ScriptedLLM([step])
    response = await _run(llm, budget, max_tokens=16_384)

    assert llm.with_tools == [True, True]
    assert any(m.content == al._BUDGET_NUDGE for m in sent[-1])
    assert MIN_OUTPUT_TOKENS <= llm.max_tokens[-1] < 16_384, "a sized call"
    assert budget.spent_tokens <= limit
    assert response.content == "Answered." + _PADDING


@pytest.mark.asyncio
async def test_a_sized_call_is_priced_with_the_tools_it_is_sent_with(_quiet_langfuse):
    limit = 40_000
    budget = RunBudget(max_total_tokens=limit, final_answer_reserve_fraction=0.0)

    def step(messages, max_tokens):
        if len(llm.max_tokens) == 1:
            return _tool_round("c1", {"input_tokens": 19_500, "output_tokens": 500})
        return ChatResponse(
            content="Answered." + _PADDING,
            model="stub",
            usage=_billed_in_full(messages, max_tokens, llm.tools[-1]),
        )

    llm = _ScriptedLLM([step])
    # A definition of about 10,000 tokens, what a heavily-wired agent carries.
    response = await _run(llm, budget, max_tokens=32_000, tool_description="Look it up. " * 2_500)

    assert llm.with_tools == [True, True]
    assert MIN_OUTPUT_TOKENS <= llm.max_tokens[-1] < 32_000, "a sized call"
    assert budget.spent_tokens <= limit
    assert response.content == "Answered." + _PADDING


@pytest.mark.asyncio
async def test_a_sized_synthesis_that_comes_back_empty_ends_with_the_budget_message(_quiet_langfuse):
    """Reasoning can spend a lowered allowance whole: the run then ends as a
    run whose synthesis the budget cannot pay for does, not with a digest
    that reads like an answer."""
    budget = RunBudget(max_total_tokens=40_000)

    def step(messages, max_tokens):
        if llm.with_tools[-1]:
            return _tool_round("c1", {"input_tokens": 35_000, "output_tokens": 500})
        return ChatResponse(content="", model="stub", stop_reason="max_tokens", usage={"output_tokens": max_tokens})

    llm = _ScriptedLLM([step])
    response = await _run(llm, budget, max_tokens=16_384)

    assert llm.with_tools == [True, False]
    assert MIN_OUTPUT_TOKENS <= llm.max_tokens[-1] < 16_384, "a sized synthesis"
    assert response.content.startswith(
        BUDGET_EXHAUSTED_MESSAGE.format(reason="this run reached its token budget (40,000 tokens)")
    )
    assert budget.spent_tokens <= 40_000


def _cut(content: str):
    """A step that fills its whole reservation and is cut at ``max_tokens``."""

    def step(messages, max_tokens):
        return ChatResponse(
            content=content, model="stub", stop_reason="max_tokens", usage=_billed_in_full(messages, max_tokens)
        )

    return step


@pytest.mark.asyncio
async def test_a_streamed_sized_answer_cut_by_the_budget_says_so(_quiet_langfuse):
    budget = RunBudget(max_total_tokens=12_000)
    streamed: list[str] = []

    async def on_token(chunk: str) -> None:
        streamed.append(chunk)

    llm = _StreamedScriptedLLM([_cut("The first part of a long answer." + _PADDING), _ANSWER])
    response = await _run(llm, budget, max_tokens=16_384, with_tools=False, on_token=on_token)

    note = _budget_cut("this run reached its token budget (12,000 tokens)")
    assert len(llm.max_tokens) == 1, "the continuation would have spent past the budget"
    assert response.content == "The first part of a long answer." + _PADDING + note
    assert al.answer_cut_by_budget(response)
    assert "".join(streamed).endswith(note)
    assert budget.spent_tokens <= 12_000


@pytest.mark.asyncio
async def test_a_sized_retry_cut_by_the_budget_says_so(_quiet_langfuse):
    budget = RunBudget(max_total_tokens=12_000)

    def empty(messages, max_tokens):
        return ChatResponse(content="", model="stub", usage={"output_tokens": 100})

    llm = _StreamedScriptedLLM([empty, _cut("The retried answer." + _PADDING), _ANSWER])

    async def on_token(chunk: str) -> None:
        return None

    response = await _run(llm, budget, max_tokens=16_384, with_tools=False, on_token=on_token)

    assert len(llm.max_tokens) == 2, "the empty answer's retry, then no continuation"
    assert MIN_OUTPUT_TOKENS <= llm.max_tokens[1] < llm.max_tokens[0] < 16_384
    assert response.content == (
        "The retried answer." + _PADDING + _budget_cut("this run reached its token budget (12,000 tokens)")
    )
    assert al.answer_cut_by_budget(response)
    assert budget.spent_tokens <= 12_000


def _history_with_a_bulky_old_result(bulky_chars: int) -> list[ChatMessage]:
    """Five earlier lookups, the oldest one bulky: the session prune stubs it."""
    history = [ChatMessage(role="user", content="Look into it.")]
    for i in range(5):
        call_id = f"h{i}"
        history.append(
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id=call_id, name="lookup", arguments={"q": call_id})],
            )
        )
        history.append(
            ChatMessage(
                role="tool",
                content="x" * bulky_chars if i == 0 else "ran lookup",
                tool_call_id=call_id,
                name="lookup",
            )
        )
    history.append(ChatMessage(role="user", content="Now write the report."))
    return history


@pytest.mark.asyncio
async def test_a_call_sized_at_the_gate_keeps_its_continuations_priced(_quiet_langfuse):
    """The gate sizes the call on a bulky transcript; the session prune that
    runs after it shrinks that transcript until the call fits in full again.
    The call is made at its full allowance, and its continuation is still
    priced: it follows a call the budget sized, so it may not spend past what
    is left."""
    limit = 25_000
    budget = RunBudget(max_total_tokens=limit, final_answer_reserve_fraction=0.0)

    def step(messages, max_tokens):
        if len(llm.max_tokens) == 1:
            return ChatResponse(
                content="The first part of the report." + _PADDING,
                model="stub",
                stop_reason="max_tokens",
                usage=_billed_in_full(messages, max_tokens, llm.tools[-1]),
            )
        return ChatResponse(content=" The end.", model="stub", usage=_billed_in_full(messages, max_tokens))

    llm = _ScriptedLLM([step])
    composite = _executor()
    response = await al.run_agent_loop(
        llm=llm,
        messages=_history_with_a_bulky_old_result(36_000),
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        max_tokens=16_384,
        max_session_chars=20_000,
        budget=budget,
    )

    assert llm.with_tools == [True, False]
    assert llm.max_tokens[0] == 16_384, "the pruned transcript fits the full allowance"
    assert MIN_OUTPUT_TOKENS <= llm.max_tokens[1] < 16_384, "the continuation is sized to what is left"
    assert budget.spent_tokens <= limit
    assert response.content == "The first part of the report." + _PADDING + " The end."
    assert not al.answer_cut_by_budget(response), "the continuation finished the answer"


@pytest.mark.asyncio
async def test_a_sized_synthesis_cut_by_the_budget_says_so(_quiet_langfuse):
    budget = RunBudget(max_total_tokens=40_000)
    streamed: list[str] = []

    async def on_token(chunk: str) -> None:
        streamed.append(chunk)

    def step(messages, max_tokens):
        if llm.with_tools[-1]:
            return _tool_round("c1", {"input_tokens": 35_000, "output_tokens": 500})
        return _cut("The first part of the report." + _PADDING)(messages, max_tokens)

    llm = _StreamedScriptedLLM([step])
    response = await _run(llm, budget, max_tokens=16_384, on_token=on_token)

    note = _budget_cut("this run reached its token budget (40,000 tokens)")
    assert llm.with_tools == [True, False], "no room left to continue the synthesis"
    assert response.content == "The first part of the report." + _PADDING + note
    assert al.answer_cut_by_budget(response)
    assert "".join(streamed).endswith(note)
    assert budget.spent_tokens <= 40_000


# ── A structured payload the budget cut ──────────────────────────────────


def test_a_repair_would_pass_a_payload_the_budget_cut_off_as_complete():
    """Why the budget cut is carried outside the text: the structured-output
    repair closes a cut payload into one that parses, and folds the budget
    message into its last value."""
    from app.services.structured_output import emit_bare_json, emit_repaired_json

    note = _budget_cut("this run reached its token budget (12,000 tokens)")
    cut = '{"summary": "The first part of the findings", "items": ["a", "b' + note
    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}, "items": {"type": "array", "items": {"type": "string"}}},
    }

    for repaired in (emit_repaired_json(cut, schema, log_context="test"), emit_bare_json(cut, log_context="test")):
        assert repaired is not None
        assert json.loads(repaired)["items"][-1].endswith(note.strip())


def _guarded_by_the_budget_cut(module) -> tuple[list[int], int]:
    """Lines of the structured-output repairs in *module* that no enclosing
    condition keeps away from an answer the budget cut, and how many repairs it
    found: none found means the scan no longer sees the repair, not that it is safe."""
    tree = ast.parse(inspect.getsource(module))
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    assigned: dict[ast.AST, dict[str, list[ast.AST]]] = {}

    def calls_the_check(expr: ast.AST) -> bool:
        return any(
            isinstance(node, ast.Call) and getattr(node.func, "id", None) == "answer_cut_by_budget"
            for node in ast.walk(expr)
        )

    def reads_the_cut(expr: ast.AST, scope: ast.AST) -> bool:
        """The condition calls the check, or names a variable of its function
        assigned from an expression that does."""
        if scope not in assigned:
            values: dict[str, list[ast.AST]] = {}
            for node in ast.walk(scope):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            values.setdefault(target.id, []).append(node.value)
            assigned[scope] = values
        names = {node.id for node in ast.walk(expr) if isinstance(node, ast.Name)}
        return calls_the_check(expr) or any(
            calls_the_check(value) for name in names for value in assigned[scope].get(name, [])
        )

    unguarded: list[int] = []
    found = 0
    for call in ast.walk(tree):
        # ``deliver_json_answer`` repairs through the two: the chat turn calls it.
        if not (
            isinstance(call, ast.Call)
            and getattr(call.func, "id", None)
            in {
                "emit_repaired_json",
                "emit_bare_json",
                "deliver_json_answer",
                "emit_repaired_json_in_thread",
                "deliver_json_answer_in_thread",
                "deliver_json_answer_with_contract",
            }
        ):
            continue
        found += 1
        conditions: list[ast.AST] = []
        scope: ast.AST = tree
        node: ast.AST = call
        while node in parents:
            node = parents[node]
            if isinstance(node, ast.If):
                conditions.append(node.test)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                scope = node
                break
        if not any(reads_the_cut(condition, scope) for condition in conditions):
            unguarded.append(call.lineno)
    return unguarded, found


def test_the_chat_and_the_assignment_never_repair_a_payload_the_budget_cut():
    """``agent_chat`` and ``assignment_engine`` read the stop reason before
    they repair a structured answer, so a payload the budget cut short keeps
    the budget message it ends with instead of being delivered as complete."""
    from app.services import agent_chat, assignment_engine

    for module in (agent_chat, assignment_engine):
        unguarded, found = _guarded_by_the_budget_cut(module)
        assert found, f"{module.__name__}: the scan found no repair to check"
        assert unguarded == [], module.__name__


# ── What a sized call is charged ─────────────────────────────────────────


class _RecordingServed(_Served):
    """A provider of a failover chain that records each call's ``max_tokens``."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.max_tokens: list[int] = []

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self.max_tokens.append(kwargs["max_tokens"])
        return await super().chat(messages, model=model, tools=tools, **kwargs)

    async def chat_stream(self, messages, model=None, **kwargs):
        self.max_tokens.append(kwargs["max_tokens"])
        async for chunk in super().chat_stream(messages, model=model, **kwargs):
            yield chunk


@pytest.fixture
def _serving_model_prices(monkeypatch):
    async def fake_resolve(_session, *, model_id, provider=None):
        return _SERVING_MODEL_PRICES.get(model_id)

    monkeypatch.setattr("app.services.tracing.local_sink.resolve_model_price", fake_resolve)
    monkeypatch.setattr("app.services.tracing.local_sink.resolve_model_price_any", fake_resolve)


async def _run_traced(root: dict, **kwargs) -> ChatResponse:
    """The loop on a 64,000-token model, recording its generations on *root*."""
    token = lft.set_current_trace_ctx(root)
    try:
        return await al.run_agent_loop(model="claude-test", max_tokens=64_000, **kwargs)
    finally:
        lft.reset_current_trace_ctx(token)


@pytest.mark.asyncio
async def test_a_sized_call_cut_at_its_allowance_is_charged_once_at_the_model_that_served_it(_serving_model_prices):
    """The budget sizes the answer; it stops at that lowered ``max_tokens`` and
    its continuation, priced again, fails over to another provider. Each call
    is charged once, what its provider reported and under the model that
    served it - never the reservation's estimate - and the root trace costs
    what its generations cost."""
    primary = _RecordingServed(
        "anthropic",
        "claude-test",
        chats=[_reply("claude-test", _ANTHROPIC_CALL, "Part one", "max_tokens"), _overloaded()],
    )
    fallback = _RecordingServed("openai", "gpt-test", chats=[_reply("gpt-test", _OPENAI_CALL, " and part two.")])
    budget = RunBudget(max_total_tokens=40_000, final_answer_reserve_fraction=0.0)
    root = _recording_root()

    response = await _run_traced(
        root,
        llm=FailoverLLMProvider(primary=primary, fallbacks=[fallback]),
        messages=[ChatMessage(role="user", content="Write the report.")],
        max_iterations=3,
        budget=budget,
    )

    assert response.content == "Part one and part two."
    assert MIN_OUTPUT_TOKENS <= primary.max_tokens[0] < 64_000, "the answer is sized"
    assert fallback.max_tokens[0] < 64_000, "and so is its continuation"
    assert response.usage[lft.USAGE_PARTS_KEY] == [
        {"model": "claude-test", "usage": _ANTHROPIC_CALL},
        {"model": "gpt-test", "usage": _OPENAI_CALL},
    ]
    assert budget.spent_tokens == count_usage_tokens(_ANTHROPIC_CALL) + count_usage_tokens(_OPENAI_CALL)
    assert budget._reserved_tokens() == 0
    trace_row, generations = await _priced(root, response)
    assert sorted(generation.model for generation in generations) == ["claude-test", "gpt-test"]
    assert trace_row.total_cost == sum(generation.total_cost for generation in generations)


@pytest.mark.asyncio
async def test_a_sized_synthesis_cut_at_its_allowance_is_charged_once_at_the_model_that_served_it(
    _serving_model_prices,
):
    """The budget sizes the closing synthesis; a fallback writes it, is cut at
    the lowered ``max_tokens`` and continues it within the full limit. The
    synthesis and its continuation are each charged once, under the
    fallback's model, and the run spends exactly what its providers reported."""
    continuation = {"prompt_tokens": 3_000, "completion_tokens": 400, "total_tokens": 3_400}
    primary = _RecordingServed(
        "anthropic",
        "claude-test",
        chats=[_tool_turn("claude-test", _ANTHROPIC_CALL, "c1"), _overloaded(), _overloaded()],
    )
    fallback = _RecordingServed(
        "openai",
        "gpt-test",
        chats=[_reply("gpt-test", _OPENAI_CALL, _LONG_ANSWER, "max_tokens"), _reply("gpt-test", continuation, " The end.")],
    )
    # One tool call, then the next call is refused and the run synthesises.
    budget = RunBudget(max_total_tokens=40_000, max_tool_calls=1)
    composite = _executor()
    root = _recording_root()

    response = await _run_traced(
        root,
        llm=FailoverLLMProvider(primary=primary, fallbacks=[fallback]),
        messages=[ChatMessage(role="user", content="Investigate.")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        budget=budget,
    )

    assert response.content.endswith(" The end.")
    assert len(fallback.max_tokens) == 2 and all(m < 64_000 for m in fallback.max_tokens), "a sized synthesis"
    assert response.usage[lft.USAGE_PARTS_KEY] == [
        {"model": "claude-test", "usage": _ANTHROPIC_CALL},
        {"model": "gpt-test", "usage": _flat_sum(_OPENAI_CALL, continuation)},
    ]
    assert budget.spent_tokens == sum(map(count_usage_tokens, (_ANTHROPIC_CALL, _OPENAI_CALL, continuation)))
    trace_row, generations = await _priced(root, response)
    assert [generation.model for generation in generations] == ["claude-test", "gpt-test", "gpt-test"]
    assert trace_row.total_cost == sum(generation.total_cost for generation in generations)


@pytest.mark.asyncio
async def test_a_sized_stream_the_user_stops_is_charged_once_what_it_reported(_quiet_langfuse):
    """A sized answer stopped mid-stream never returned: it is charged once,
    the usage its stream reported before the stop, and its reservation is
    handed back."""
    cancel = asyncio.Event()

    async def _stop_mid_answer(chunk: str) -> None:
        if chunk == "The answer starts":
            cancel.set()

    llm = _RecordingServed(
        "anthropic", "claude-test", streams=[(["The answer starts", " and goes on."], _ANTHROPIC_CALL, "end_turn")]
    )
    budget = RunBudget(max_total_tokens=40_000, final_answer_reserve_fraction=0.0)

    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="Write the report.")],
        model="claude-test",
        max_tokens=64_000,
        max_iterations=3,
        budget=budget,
        on_token=_stop_mid_answer,
        cancel_event=cancel,
    )

    assert response.stop_reason == "cancelled"
    assert MIN_OUTPUT_TOKENS <= llm.max_tokens[0] < 64_000, "the answer is sized"
    assert response.usage == _ANTHROPIC_CALL
    assert budget.spent_tokens == count_usage_tokens(_ANTHROPIC_CALL)
    assert budget._reserved_tokens() == 0


# ── Provider requests with a lowered max_tokens ──────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude-mythos-5-1", "claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5"])
async def test_an_anthropic_request_at_the_floor_stays_valid_with_thinking(model: str):
    """XTM One sends adaptive thinking (no ``budget_tokens``), which
    ``max_tokens`` caps together with the answer: a lowered cap needs no
    thinking budget lowered with it, and none can exceed it."""
    from app.llm.anthropic_provider import AnthropicProvider
    from tests.test_anthropic_provider import _CACHE_TOOLS, _discard_thinking, _patch_stream

    provider = AnthropicProvider(api_key="test-key")
    stream_mock = _patch_stream(provider)

    await provider.chat_stream_with_tools(
        messages=[ChatMessage(role="user", content="hi")],
        tools=_CACHE_TOOLS,
        model=model,
        max_tokens=MIN_OUTPUT_TOKENS,
        on_thinking=_discard_thinking,
    )

    sent = stream_mock.call_args.kwargs
    assert sent["max_tokens"] == MIN_OUTPUT_TOKENS
    assert sent["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "budget_tokens" not in sent["thinking"]


def test_openai_reasoning_bedrock_and_compatible_requests_carry_the_lowered_cap():
    assert build_openai_generation_params("gpt-5", temperature=None, max_tokens=2_048) == {
        "max_completion_tokens": 2_048
    }
    assert build_openai_generation_params("mistral-large-latest", temperature=0.2, max_tokens=2_048) == {
        "max_tokens": 2_048,
        "temperature": 0.2,
    }
    assert _build_inference_config("us.anthropic.claude-sonnet-5", 0.2, 2_048)["maxTokens"] == 2_048
