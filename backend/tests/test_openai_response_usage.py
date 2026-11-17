"""A non-streaming OpenAI chat completion keeps the reported ``total_tokens``.

The streaming path has always carried it (``extract_openai_usage``), but the
non-streaming ``chat()`` of both OpenAI providers built its usage dict by hand
with only ``prompt_tokens`` / ``completion_tokens``. A run with no token
callback - an assignment, a sub-agent task, a channel reply - goes through that
path, so tokens only the total accounts for never reached the run budget: a
server that fills in nothing but the total, or xAI, whose total carries the
reasoning tokens its ``completion_tokens`` leaves out.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openai.types.chat import ChatCompletion

from app.llm.base import ChatMessage
from app.llm.openai_compat import OpenAICompatibleProvider
from app.llm.openai_provider import OpenAIProvider
from app.llm.xai_provider import XAIProvider
from app.services.tracing.cost_calculator import ModelPrice
from app.tools.run_budget import RunBudget


def _completion(usage: dict | None) -> ChatCompletion:
    """A completion built the way the SDK builds one - without validation, so a
    field the server left out reads as ``None`` instead of failing."""
    raw: dict = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
    }
    if usage is not None:
        raw["usage"] = usage
    return ChatCompletion.construct(**raw)


def _serve(provider, completion: ChatCompletion) -> None:
    class _Completions:
        async def create(self, **kwargs):
            return completion

    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))


def _price() -> ModelPrice:
    return ModelPrice(
        provider="stub",
        model_id="stub-model",
        model_pattern=None,
        input_price_per_1m=Decimal("3"),
        output_price_per_1m=Decimal("15"),
        cache_read_price_per_1m=None,
        cache_write_price_per_1m=None,
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )


# xAI's own documented shape: 32 in + 9 out, but a 135 total - the other 94
# are the reasoning tokens ``completion_tokens`` leaves out.
_XAI_USAGE = {"prompt_tokens": 32, "completion_tokens": 9, "total_tokens": 135}


@pytest.mark.parametrize(
    "provider_cls", [OpenAIProvider, OpenAICompatibleProvider, XAIProvider]
)
async def test_chat_keeps_the_reported_total(provider_cls):
    provider = provider_cls(api_key="test-key")
    _serve(provider, _completion(_XAI_USAGE))

    response = await provider.chat([ChatMessage(role="user", content="hi")], model="m")

    assert response.usage == _XAI_USAGE


@pytest.mark.parametrize("provider_cls", [OpenAIProvider, OpenAICompatibleProvider])
async def test_a_total_only_server_is_debited_on_the_non_streaming_path(provider_cls):
    """The case on the path assignment runs take: before the fix the dict
    reached the budget as ``{prompt: None, completion: None}`` - 0 tokens, $0."""
    provider = provider_cls(api_key="test-key")
    _serve(provider, _completion({"total_tokens": 200_000}))
    response = await provider.chat([ChatMessage(role="user", content="hi")], model="m")

    budget = RunBudget(max_cost_usd=10.0, price=_price())
    budget.record_llm_usage(response.usage)

    assert budget.spent_tokens == 200_000
    assert budget.spent_cost_usd == Decimal("3")


async def test_xai_reasoning_tokens_reach_the_budget_without_streaming():
    provider = XAIProvider(api_key="test-key")
    _serve(provider, _completion(_XAI_USAGE))
    response = await provider.chat([ChatMessage(role="user", content="hi")], model="m")

    budget = RunBudget(max_cost_usd=10.0, price=_price())
    budget.record_llm_usage(response.usage)

    assert budget.spent_tokens == 135
    # 32 in @ $3 + 9 out @ $15, plus the 94 reasoning tokens at $15 per 1M.
    assert budget.spent_cost_usd == (
        Decimal(32 * 3) + Decimal(9 * 15) + Decimal(94 * 15)
    ) / Decimal(1_000_000)


async def test_a_response_without_a_total_keeps_the_old_shape():
    provider = OpenAIProvider(api_key="test-key")
    _serve(provider, _completion({"prompt_tokens": 5, "completion_tokens": 2}))
    response = await provider.chat([ChatMessage(role="user", content="hi")], model="m")
    assert response.usage == {"prompt_tokens": 5, "completion_tokens": 2}


def test_the_helper_reads_the_usage_defensively():
    from app.llm.openai_compat import extract_openai_response_usage

    assert extract_openai_response_usage(None) == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }
    # A mock's auto-created attribute is not a token count.
    mocked = MagicMock(prompt_tokens=4, completion_tokens=1)
    assert extract_openai_response_usage(mocked) == {
        "prompt_tokens": 4,
        "completion_tokens": 1,
    }
    # An attribute the object does not have at all reads as absent.
    assert extract_openai_response_usage(SimpleNamespace(total_tokens=7)) == {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": 7,
    }


def test_the_helper_carries_the_prompt_details_only():
    """The cached prompt tokens are priced at the cache-read rate on both paths;
    the completion details, inside ``completion_tokens`` and priced at
    the same output rate either way, stay out."""
    from app.llm.openai_compat import extract_openai_response_usage

    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        prompt_tokens_details=SimpleNamespace(cached_tokens=5),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=15),
    )
    assert extract_openai_response_usage(usage) == {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
        "prompt_tokens_details": {"cached_tokens": 5},
    }
