"""Gemini tool-use prompt tokens are billed input, counted on both paths.

``tool_use_prompt_token_count`` counts the results of server-side tool
executions (grounding, code execution) that Google feeds back to the model as
input and bills at the input rate. ``prompt_token_count`` leaves them out and
``total_token_count`` counts them, so the provider dropped them from the run
budget and the traces alike. Real SDK usage types only.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from google.genai import types as genai_types

from app.llm.base import ChatMessage
from app.llm.gemini_provider import GeminiProvider, _usage_from_gemini_metadata
from app.services.langfuse_tracing import _normalize_usage
from app.services.tracing.cost_calculator import ModelPrice, calculate_costs
from app.tools.run_budget import RunBudget

_META = genai_types.GenerateContentResponseUsageMetadata(
    prompt_token_count=1_000,
    tool_use_prompt_token_count=4_000,
    candidates_token_count=500,
    thoughts_token_count=250,
    total_token_count=5_750,
)


def _flash_price() -> ModelPrice:
    """gemini-2.5-flash list prices per 1M: $0.30 in, $2.50 out."""
    return ModelPrice(
        provider="gemini",
        model_id="gemini-2.5-flash",
        model_pattern=None,
        input_price_per_1m=Decimal("0.30"),
        output_price_per_1m=Decimal("2.50"),
        cache_read_price_per_1m=Decimal("0.03"),
        cache_write_price_per_1m=None,
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )


async def _chat_usage(usage_metadata: object) -> dict:
    provider = GeminiProvider(api_key="dummy")
    part = SimpleNamespace(text="ok", thought=False, function_call=None)
    candidate = SimpleNamespace(finish_reason=None, content=SimpleNamespace(parts=[part]))
    response = SimpleNamespace(candidates=[candidate], usage_metadata=usage_metadata)

    class _FakeModels:
        async def generate_content(self, **kwargs):
            return response

    provider.client = SimpleNamespace(aio=SimpleNamespace(models=_FakeModels()))
    result = await provider.chat([ChatMessage(role="user", content="hi")])
    return result.usage


async def test_tool_use_prompt_tokens_count_as_input_on_both_paths():
    streamed = _usage_from_gemini_metadata(_META)

    assert streamed == {"prompt_tokens": 5_000, "completion_tokens": 750}
    assert await _chat_usage(_META) == streamed
    # The split now accounts for every token the reported total counts.
    assert streamed["prompt_tokens"] + streamed["completion_tokens"] == _META.total_token_count


def test_tool_use_prompt_tokens_are_priced_at_the_input_rate():
    usage = _usage_from_gemini_metadata(_META)
    budget = RunBudget(max_cost_usd=100.0, max_total_tokens=1_000_000, price=_flash_price())
    budget.record_llm_usage(usage)

    # 5k in @ $0.30 + (500 answer + 250 thinking) @ $2.50, per 1M.
    billed = (Decimal(5_000) * Decimal("0.30") + Decimal(750) * Decimal("2.50")) / Decimal(1_000_000)
    assert budget.spent_cost_usd == billed
    assert budget.spent_tokens == 5_750
    assert calculate_costs(_normalize_usage(usage), _flash_price())["total_cost"] == billed


async def test_a_response_without_tool_use_keeps_its_shape():
    meta = genai_types.GenerateContentResponseUsageMetadata(
        prompt_token_count=80, candidates_token_count=12, total_token_count=92
    )
    assert _usage_from_gemini_metadata(meta) == {"prompt_tokens": 80, "completion_tokens": 12}
    assert await _chat_usage(meta) == {"prompt_tokens": 80, "completion_tokens": 12}
