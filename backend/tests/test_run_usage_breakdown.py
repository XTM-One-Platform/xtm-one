"""A run's usage keeps its calls' breakdown maps, so its root trace prices like them.

The root trace of a run is priced from the run's summed usage (``end_trace``,
then the trace sink). The agent loop summed only the flat counts, so the nested
``prompt_tokens_details.cached_tokens`` (OpenAI and Gemini) was
lost there: a run whose calls read most of their prompt from the cache had its
root trace priced as if nothing was cached, above the sum of its own
generations. The token counts the root trace and the run record show stay what
they were: every prompt token, the cached ones included.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from google.genai import types as genai_types

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.llm.gemini_provider import _usage_from_gemini_metadata
from app.services import langfuse_tracing as lft
from app.services.tracing.cost_calculator import ModelPrice, calculate_costs
from app.tools import agent_loop as al
from app.tools.base import CallableToolExecutor, CompositeToolExecutor, ToolCall, ToolDefinition
from app.tools.run_budget import RunBudget


def _price() -> ModelPrice:
    """$1 in / $4 out / $0.10 cache read per 1M tokens."""
    return ModelPrice(
        provider="stub",
        model_id="stub-model",
        model_pattern=None,
        input_price_per_1m=Decimal("1"),
        output_price_per_1m=Decimal("4"),
        cache_read_price_per_1m=Decimal("0.1"),
        cache_write_price_per_1m=None,
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )


# One call: 100k prompt tokens, 90k of them read from the cache, 1k out.
_OPENAI_CALL = {
    "prompt_tokens": 100_000,
    "completion_tokens": 1_000,
    "total_tokens": 101_000,
    "prompt_tokens_details": {"cached_tokens": 90_000, "audio_tokens": 0},
}
_GEMINI_CALL = _usage_from_gemini_metadata(
    genai_types.GenerateContentResponseUsageMetadata(
        prompt_token_count=100_000,
        cached_content_token_count=90_000,
        candidates_token_count=1_000,
        total_token_count=101_000,
    )
)
# 10k uncached @ $1 + 90k cached @ $0.10 + 1k out @ $4, per 1M.
_CALL_COST = Decimal("0.023")


def _root_cost(usage: dict) -> Decimal:
    """What the trace sink records for a root trace ended with *usage*."""
    return calculate_costs(lft._normalize_usage(usage), _price())["total_cost"]


class _ScriptedLLM(BaseLLMProvider):
    provider_name = "stub"
    context_window = 1_000_000

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = script

    async def chat(self, messages, model=None, tools=None, **kwargs):
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


@pytest.mark.parametrize("call_usage", [_OPENAI_CALL, _GEMINI_CALL], ids=["openai", "gemini"])
async def test_a_run_prices_its_root_trace_like_its_calls(_quiet_langfuse, call_usage):
    composite = _executor()
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub-model",
                stop_reason="tool_use",
                tool_calls=[ToolCall(id="c1", name="lookup", arguments={"q": "x"})],
                usage=dict(call_usage),
            ),
            ChatResponse(content="Done: found it.", model="stub-model", usage=dict(call_usage)),
        ]
    )
    budget = RunBudget(max_cost_usd=100.0, price=_price())

    response = await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=5,
        budget=budget,
    )

    usage = response.usage
    assert usage["prompt_tokens"] == 200_000
    assert usage["prompt_tokens_details"]["cached_tokens"] == 180_000
    # Before, the summed usage priced all 200k prompt tokens at the input rate.
    assert _root_cost(usage) == 2 * _CALL_COST
    assert budget.spent_cost_usd == 2 * _CALL_COST


def _root_ctx() -> dict:
    return {
        "trace": None,
        "generation": None,
        "client": None,
        "parent_ctx": None,
        "metadata": {},
        "local_spans": [],
        "local_trace_id": lft.uuid.uuid4(),
        "local_source": "assignment",
        "local_name": "run",
        "local_started_at": lft.datetime.now(lft.UTC),
        "local_metadata": {},
        "local_tags": [],
    }


_RUN_USAGE = {
    "prompt_tokens": 200_000,
    "completion_tokens": 2_000,
    "total_tokens": 202_000,
    "prompt_tokens_details": {"cached_tokens": 180_000},
}


def test_the_root_trace_counts_every_token_and_prices_the_breakdown():
    with patch("app.services.tracing.dispatcher.submit_trace_for_flush") as submit:
        lft.end_trace(_root_ctx(), output="done", usage=dict(_RUN_USAGE), model="stub-model")

    trace_data, _spans = submit.call_args.args
    # The columns the dashboards sum count the cached prompt tokens, as before.
    assert (trace_data.input_tokens, trace_data.output_tokens, trace_data.total_tokens) == (
        200_000,
        2_000,
        202_000,
    )
    # The sink prices the breakdown: the cached share at the cache-read rate.
    assert calculate_costs(trace_data.usage_details, _price())["total_cost"] == 2 * _CALL_COST


def test_the_run_token_totals_count_the_cached_prompt_tokens():
    assert lft.usage_token_totals(dict(_RUN_USAGE)) == (200_000, 2_000)


def test_the_breakdown_is_summed_leaf_by_leaf():
    run: dict = {}
    al._sum_usage_details_into(run, {"prompt_tokens_details": {"cached_tokens": 5, "audio_tokens": 1}})
    al._sum_usage_details_into(
        run,
        {
            "prompt_tokens_details": {"cached_tokens": 7, "flag": True},
            "completion_tokens_details": {"reasoning_tokens": 3},
            "details": {"cached_tokens": 100},
            "input_tokens_details": None,
        },
    )
    assert run == {
        "prompt_tokens_details": {"cached_tokens": 12, "audio_tokens": 1},
        "completion_tokens_details": {"reasoning_tokens": 3},
    }
