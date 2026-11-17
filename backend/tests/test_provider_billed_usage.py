"""Provider usage carries every token the vendor bills.

Bedrock dropped the Converse ``cacheReadInputTokens`` / ``cacheWriteInputTokens``
counters and Gemini dropped ``thoughts_token_count`` (thinking tokens, billed at
the output rate), so those tokens reached neither the run budget nor the
traces, both of which price the provider's usage dict. Fake SDK responses only.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from app.llm.base import ChatMessage
from app.llm.bedrock_provider import BedrockProvider, _usage_from_bedrock
from app.llm.gemini_provider import GeminiProvider, _usage_from_gemini_metadata
from app.services.tracing.cost_calculator import ModelPrice, calculate_costs
from app.tools.run_budget import RunBudget


def _price(cache_read: str | None = "0.3", cache_write: str | None = "3.75") -> ModelPrice:
    return ModelPrice(
        provider="stub",
        model_id="stub",
        model_pattern=None,
        input_price_per_1m=Decimal("3"),
        output_price_per_1m=Decimal("15"),
        cache_read_price_per_1m=None if cache_read is None else Decimal(cache_read),
        cache_write_price_per_1m=None if cache_write is None else Decimal(cache_write),
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )


# ── Bedrock: the Converse cache counters ────────────────────────────────────

_CONVERSE_USAGE = {
    "inputTokens": 1_000_000,
    "outputTokens": 100_000,
    "cacheReadInputTokens": 2_000_000,
    "cacheWriteInputTokens": 400_000,
    "totalTokens": 3_500_000,
}
# 1M in @ $3 + 100k out @ $15 + 2M cache read @ $0.30 + 400k cache write @ $3.75
_CONVERSE_COST = Decimal("3") + Decimal("1.5") + Decimal("0.6") + Decimal("1.5")


def test_bedrock_streaming_usage_carries_the_cache_counters():
    assert _usage_from_bedrock(_CONVERSE_USAGE) == {
        "prompt_tokens": 1_000_000,
        "completion_tokens": 100_000,
        "cache_read_input_tokens": 2_000_000,
        "cache_creation_input_tokens": 400_000,
    }


def test_bedrock_chat_usage_carries_the_cache_counters_and_is_priced():
    provider = BedrockProvider(api_key="AKIA||secret||us-east-1")
    response = {
        "output": {"message": {"content": [{"text": "ok"}]}},
        "stopReason": "end_turn",
        "usage": _CONVERSE_USAGE,
    }
    provider._client = SimpleNamespace(converse=lambda **_kwargs: response)

    result = asyncio.run(
        provider.chat([ChatMessage(role="user", content="hi")], model="anthropic.claude-sonnet-4")
    )

    assert result.usage["cache_read_input_tokens"] == 2_000_000
    assert result.usage["cache_creation_input_tokens"] == 400_000
    assert calculate_costs(result.usage, _price())["total_cost"] == _CONVERSE_COST
    budget = RunBudget(max_cost_usd=100.0, price=_price())
    budget.record_llm_usage(result.usage)
    assert budget.spent_cost_usd == _CONVERSE_COST


def test_a_cache_only_bedrock_record_is_kept():
    """A prompt read entirely from the cache reports 0 ``inputTokens``; the
    record must still reach the budget and the traces, not collapse to ``{}``."""
    assert _usage_from_bedrock(
        {"inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 5_000}
    ) == {"prompt_tokens": 0, "completion_tokens": 0, "cache_read_input_tokens": 5_000}
    assert _usage_from_bedrock({"inputTokens": 0, "outputTokens": 0}) == {}


def test_bedrock_usage_without_caching_keeps_its_shape():
    assert _usage_from_bedrock({"inputTokens": 200, "outputTokens": 50, "totalTokens": 250}) == {
        "prompt_tokens": 200,
        "completion_tokens": 50,
    }


# ── Gemini: thinking tokens ─────────────────────────────────────────────────

_GEMINI_META = SimpleNamespace(
    prompt_token_count=100_000,
    candidates_token_count=10_000,
    thoughts_token_count=90_000,
    total_token_count=200_000,
)


def test_gemini_streaming_usage_counts_thinking_as_output():
    assert _usage_from_gemini_metadata(_GEMINI_META) == {
        "prompt_tokens": 100_000,
        "completion_tokens": 100_000,
    }


def test_gemini_chat_usage_counts_thinking_as_output_and_is_priced():
    provider = GeminiProvider(api_key="dummy")
    part = SimpleNamespace(text="ok", thought=False, function_call=None)
    candidate = SimpleNamespace(finish_reason=None, content=SimpleNamespace(parts=[part]))
    response = SimpleNamespace(candidates=[candidate], usage_metadata=_GEMINI_META)

    class _FakeModels:
        async def generate_content(self, **kwargs):
            return response

    provider.client = SimpleNamespace(aio=SimpleNamespace(models=_FakeModels()))
    result = asyncio.run(provider.chat([ChatMessage(role="user", content="hi")]))

    assert result.usage == {"prompt_tokens": 100_000, "completion_tokens": 100_000}
    budget = RunBudget(max_cost_usd=100.0, max_total_tokens=1_000_000, price=_price())
    budget.record_llm_usage(result.usage)
    # 100k in @ $3 + (10k answer + 90k thinking) @ $15
    assert budget.spent_cost_usd == Decimal("0.3") + Decimal("1.5")
    assert budget.spent_tokens == 200_000


def test_gemini_usage_without_thinking_keeps_its_shape():
    meta = SimpleNamespace(prompt_token_count=80, candidates_token_count=12, thoughts_token_count=None)
    assert _usage_from_gemini_metadata(meta) == {"prompt_tokens": 80, "completion_tokens": 12}
