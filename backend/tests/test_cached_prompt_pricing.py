"""Cached prompt tokens are priced at the cache rate, inclusively.

Gemini's ``prompt_token_count`` INCLUDES ``cached_content_token_count`` and
OpenAI's ``prompt_tokens`` INCLUDES ``prompt_tokens_details.cached_tokens``.
``prompt_tokens`` stays inclusive (proxy clients see no change); the cached part
travels as the OpenAI-shaped ``prompt_tokens_details.cached_tokens`` and the one
pricing rule (``price_usage``) prices it at the cache-read rate and the rest of
the prompt at the input rate - never subtracted twice. Real SDK usage types only.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from google.genai import types as genai_types
from openai.types import CompletionUsage
from openai.types.completion_usage import PromptTokensDetails

from app.llm.base import ChatMessage
from app.llm.gemini_provider import GeminiProvider, _usage_from_gemini_metadata
from app.llm.openai_compat import extract_openai_response_usage, extract_openai_usage
from app.services.evaluation_engine import _accumulate_token_categories
from app.services.langfuse_tracing import _normalize_usage
from app.services.tracing.cost_calculator import (
    ModelPrice,
    cached_input_tokens,
    calculate_costs,
    input_output_tokens,
    price_usage,
)
from app.tools.run_budget import RunBudget, count_usage_tokens


def _flash_price() -> ModelPrice:
    """gemini-2.5-flash list prices per 1M: $0.30 in, $2.50 out, $0.03 cache read."""
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


# 10k uncached @ $0.30 + 90k cached @ $0.03 per 1M = $0.0057 (not $0.030).
_BILLED = Decimal("0.0057")


def _debit(usage: dict) -> Decimal:
    budget = RunBudget(max_cost_usd=100.0, price=_flash_price())
    budget.record_llm_usage(usage)
    return budget.spent_cost_usd


def _trace(usage: dict) -> Decimal:
    return calculate_costs(_normalize_usage(usage), _flash_price())["total_cost"]


# ── Gemini ──────────────────────────────────────────────────────────────────

_GEMINI_META = genai_types.GenerateContentResponseUsageMetadata(
    prompt_token_count=100_000,
    cached_content_token_count=90_000,
    candidates_token_count=0,
    total_token_count=100_000,
)


def test_gemini_usage_carries_the_cached_prompt_tokens():
    usage = _usage_from_gemini_metadata(_GEMINI_META)
    assert usage["prompt_tokens"] == 100_000, "prompt_tokens stays inclusive"
    assert usage["prompt_tokens_details"] == {"cached_tokens": 90_000}


def test_a_cached_gemini_prompt_is_billed_at_the_cache_rate():
    usage = _usage_from_gemini_metadata(_GEMINI_META)
    assert _debit(usage) == _BILLED
    assert _trace(usage) == _BILLED
    assert count_usage_tokens(usage) == 100_000, "the token count is unchanged"


def test_gemini_chat_carries_the_cached_prompt_tokens():
    provider = GeminiProvider(api_key="dummy")
    part = SimpleNamespace(text="ok", thought=False, function_call=None)
    candidate = SimpleNamespace(finish_reason=None, content=SimpleNamespace(parts=[part]))
    response = SimpleNamespace(candidates=[candidate], usage_metadata=_GEMINI_META)

    class _FakeModels:
        async def generate_content(self, **kwargs):
            return response

    provider.client = SimpleNamespace(aio=SimpleNamespace(models=_FakeModels()))
    result = asyncio.run(provider.chat([ChatMessage(role="user", content="hi")]))

    assert result.usage["prompt_tokens"] == 100_000
    assert result.usage["prompt_tokens_details"] == {"cached_tokens": 90_000}
    assert _debit(result.usage) == _BILLED


def test_an_uncached_gemini_prompt_keeps_its_shape():
    meta = genai_types.GenerateContentResponseUsageMetadata(
        prompt_token_count=80, candidates_token_count=12, total_token_count=92
    )
    assert _usage_from_gemini_metadata(meta) == {"prompt_tokens": 80, "completion_tokens": 12}


# ── OpenAI: the same shape, the same rule ────────────────────────────────────

_OPENAI_USAGE = CompletionUsage(
    prompt_tokens=100_000,
    completion_tokens=0,
    total_tokens=100_000,
    prompt_tokens_details=PromptTokensDetails(cached_tokens=90_000),
)


def test_openai_cached_prompt_tokens_price_the_same_on_both_paths():
    streamed = extract_openai_usage(_OPENAI_USAGE)
    non_streamed = extract_openai_response_usage(_OPENAI_USAGE)
    assert non_streamed["prompt_tokens_details"] == {"cached_tokens": 90_000}
    assert _debit(streamed) == _debit(non_streamed) == _BILLED
    assert _trace(streamed) == _trace(non_streamed) == _BILLED


# ── Never subtracted twice ──────────────────────────────────────────────────


def test_a_cache_read_bucket_wins_over_the_nested_detail():
    """A dict that carries both (LiteLLM in front of Anthropic) must not have
    its cache discounted twice: the explicit bucket prices the cache."""
    usage = {
        "prompt_tokens": 10_000,
        "completion_tokens": 0,
        "cache_read_input_tokens": 90_000,
        "prompt_tokens_details": {"cached_tokens": 90_000},
    }
    costs = price_usage(usage, _flash_price())
    assert costs is not None
    # 10k @ $0.30 + the 90k cache-read bucket @ $0.03
    assert costs.total_cost == _BILLED


def test_a_nested_cached_count_above_the_prompt_is_clamped():
    usage = {"prompt_tokens": 1_000, "completion_tokens": 0, "prompt_tokens_details": {"cached_tokens": 5_000}}
    # Never more cached than the prompt: all 1k at the cache rate.
    assert price_usage(usage, _flash_price()).total_cost == Decimal("0.00003")


# ── AI gateway Claude: cache writes priced at the cache-write rate ──────────


def _claude_price(cache_write: str | None = "3.75") -> ModelPrice:
    """claude-sonnet list prices per 1M: $3 in, $15 out, $0.30 read, $3.75 write (5m TTL)."""
    return ModelPrice(
        provider="gateway",
        model_id="claude-sonnet-5-5",
        model_pattern=None,
        input_price_per_1m=Decimal("3"),
        output_price_per_1m=Decimal("15"),
        cache_read_price_per_1m=Decimal("0.30"),
        cache_write_price_per_1m=None if cache_write is None else Decimal(cache_write),
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )


# ai-gateway: prompt_tokens = fresh + cache reads + writes, both nested.
_GATEWAY_USAGE = CompletionUsage(
    prompt_tokens=100_000,
    completion_tokens=0,
    total_tokens=100_000,
    prompt_tokens_details=PromptTokensDetails(cached_tokens=60_000, cache_write_tokens=30_000),
)


def test_gateway_cache_writes_price_at_the_write_rate_on_both_paths():
    # 10k @ $3 + 60k read @ $0.30 + 30k written @ $3.75 per 1M
    billed = Decimal("0.1605")
    for usage in (extract_openai_usage(_GATEWAY_USAGE), extract_openai_response_usage(_GATEWAY_USAGE)):
        budget = RunBudget(max_cost_usd=100.0, price=_claude_price())
        budget.record_llm_usage(usage)
        assert budget.spent_cost_usd == billed
        assert calculate_costs(_normalize_usage(usage), _claude_price())["total_cost"] == billed
        assert input_output_tokens(usage) == (100_000, 0), "each prompt token counted once"
        assert cached_input_tokens(usage) == 60_000


def test_gateway_cache_writes_without_a_write_tariff_price_at_the_input_rate():
    usage = extract_openai_usage(_GATEWAY_USAGE)
    # 40k @ $3 + 60k read @ $0.30 per 1M
    billed = Decimal("0.138")
    assert price_usage(usage, _claude_price(cache_write=None)).total_cost == billed
    assert calculate_costs(_normalize_usage(usage), _claude_price(cache_write=None))["total_cost"] == billed


def test_a_cache_write_bucket_wins_over_the_nested_detail():
    """Both shapes on one usage must not bill the writes twice."""
    usage = {
        "prompt_tokens": 70_000,
        "completion_tokens": 0,
        "cache_creation_input_tokens": 30_000,
        "prompt_tokens_details": {"cache_write_tokens": 30_000},
    }
    # 70k @ $3 + the 30k write bucket @ $3.75 per 1M
    assert price_usage(usage, _claude_price()).total_cost == Decimal("0.3225")


def test_gateway_cache_writes_reach_the_eval_token_categories():
    usage = _normalize_usage(extract_openai_usage(_GATEWAY_USAGE))
    buckets, convention = _accumulate_token_categories([usage])
    assert buckets["cache_write_input_tokens"] == 30_000
    assert convention == "separate"
