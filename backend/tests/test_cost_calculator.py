from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.tracing import cost_calculator


@pytest.mark.asyncio
async def test_resolve_model_price_exact_match_first(monkeypatch):
    exact = SimpleNamespace(
        provider="openai",
        model_id="gpt-4o-mini",
        model_pattern="^gpt-4o-mini(-.*)?$",
        input_price_per_1m=Decimal("0.15"),
        output_price_per_1m=Decimal("0.60"),
        cache_read_price_per_1m=Decimal("0.08"),
        cache_write_price_per_1m=Decimal("0.20"),
        effective_from=date(2026, 1, 1),
        is_custom=True,
    )
    pattern = SimpleNamespace(
        provider="openai",
        model_id="gpt-4o",
        model_pattern="^gpt-4o.*$",
        input_price_per_1m=Decimal("1.0"),
        output_price_per_1m=Decimal("4.0"),
        cache_read_price_per_1m=None,
        cache_write_price_per_1m=None,
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )

    async def _fake_load_prices(_db, _provider):
        return [pattern, exact]

    monkeypatch.setattr(cost_calculator, "_load_prices", _fake_load_prices)

    resolved = await cost_calculator.resolve_model_price(
        db=object(),
        model_id="gpt-4o-mini",
        provider="openai",
    )
    assert resolved is not None
    assert resolved.model_id == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_resolve_model_price_regex_fallback(monkeypatch):
    pattern = SimpleNamespace(
        provider="anthropic",
        model_id="claude-sonnet-4",
        model_pattern="^claude-sonnet-4-.*$",
        input_price_per_1m=Decimal("3.0"),
        output_price_per_1m=Decimal("15.0"),
        cache_read_price_per_1m=Decimal("0.3"),
        cache_write_price_per_1m=Decimal("3.75"),
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )

    async def _fake_load_prices(_db, _provider):
        return [pattern]

    monkeypatch.setattr(cost_calculator, "_load_prices", _fake_load_prices)

    resolved = await cost_calculator.resolve_model_price(
        db=object(),
        model_id="claude-sonnet-4-20250514",
        provider="anthropic",
    )
    assert resolved is not None
    assert resolved.model_id == "claude-sonnet-4"


@pytest.mark.asyncio
async def test_resolve_model_price_returns_none_when_unmatched(monkeypatch):
    async def _fake_load_prices(_db, _provider):
        return []

    monkeypatch.setattr(cost_calculator, "_load_prices", _fake_load_prices)

    resolved = await cost_calculator.resolve_model_price(
        db=object(),
        model_id="unknown-model",
        provider="openai",
    )
    assert resolved is None


@pytest.mark.asyncio
async def test_resolve_model_price_does_not_cross_provider(monkeypatch):
    row = SimpleNamespace(
        provider="openai",
        model_id="shared-id",
        model_pattern="^shared-id$",
        input_price_per_1m=Decimal("1"),
        output_price_per_1m=Decimal("2"),
        cache_read_price_per_1m=None,
        cache_write_price_per_1m=None,
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )

    async def _fake_load_prices(_db, provider):
        return [row] if provider == "openai" else []

    monkeypatch.setattr(cost_calculator, "_load_prices", _fake_load_prices)

    resolved = await cost_calculator.resolve_model_price(
        db=object(),
        model_id="shared-id",
        provider="anthropic",
    )
    assert resolved is None


def test_calculate_costs_returns_zero_for_explicit_free_price():
    price = cost_calculator.ModelPrice(
        provider="self-hosted",
        model_id="ollama/llama3.1",
        model_pattern="^ollama/.*$",
        input_price_per_1m=Decimal("0"),
        output_price_per_1m=Decimal("0"),
        cache_read_price_per_1m=Decimal("0"),
        cache_write_price_per_1m=Decimal("0"),
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )
    usage = {
        "input_tokens": 5000,
        "output_tokens": 3000,
        "cache_read_input_tokens": 2000,
        "cache_creation_input_tokens": 1000,
    }

    result = cost_calculator.calculate_costs(usage_details=usage, price=price)
    assert result["total_cost"] == Decimal("0")


def test_calculate_costs_applies_cache_read_and_write_prices():
    price = cost_calculator.ModelPrice(
        provider="anthropic",
        model_id="claude-sonnet-4-20250514",
        model_pattern="^claude-sonnet-4-.*$",
        input_price_per_1m=Decimal("3"),
        output_price_per_1m=Decimal("15"),
        cache_read_price_per_1m=Decimal("0.3"),
        cache_write_price_per_1m=Decimal("3.75"),
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
    }

    result = cost_calculator.calculate_costs(usage_details=usage, price=price)
    assert result["input_cost"] == Decimal("3")
    assert result["output_cost"] == Decimal("15")
    assert result["cache_read_cost"] == Decimal("0.3")
    assert result["cache_write_cost"] == Decimal("3.75")
    assert result["total_cost"] == Decimal("22.05")


def test_calculate_costs_normalized_input_does_not_double_count_cache():
    """Langfuse-v4 normalized usage folds cache tokens into ``input``.

    The cache read + creation tokens are part of the ``input`` bucket (see
    ``langfuse_tracing._normalize_usage``), so billing ``input`` at the full rate
    AND adding the cache costs would double-charge them. The non-cached base here
    is 1M, so the result must match the raw-shape case (22.05), NOT 28.05.
    """
    price = cost_calculator.ModelPrice(
        provider="anthropic",
        model_id="claude-sonnet-4-20250514",
        model_pattern="^claude-sonnet-4-.*$",
        input_price_per_1m=Decimal("3"),
        output_price_per_1m=Decimal("15"),
        cache_read_price_per_1m=Decimal("0.3"),
        cache_write_price_per_1m=Decimal("3.75"),
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )
    usage = {
        # input = base(1M) + cache_creation(1M) + cache_read(1M)
        "input": 3_000_000,
        "output": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
    }

    result = cost_calculator.calculate_costs(usage_details=usage, price=price)
    assert result["input_cost"] == Decimal("3")  # 1M base only, cache peeled out
    assert result["output_cost"] == Decimal("15")
    assert result["cache_read_cost"] == Decimal("0.3")
    assert result["cache_write_cost"] == Decimal("3.75")
    assert result["total_cost"] == Decimal("22.05")


def test_calculate_costs_normalized_input_keeps_cache_at_input_rate_without_cache_price():
    """When the model has no cache pricing, cache tokens folded into ``input``
    stay billed at the input rate (no subtraction) rather than silently dropping
    to zero."""
    price = cost_calculator.ModelPrice(
        provider="openai",
        model_id="some-model",
        model_pattern=None,
        input_price_per_1m=Decimal("2"),
        output_price_per_1m=Decimal("8"),
        cache_read_price_per_1m=None,
        cache_write_price_per_1m=None,
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )
    usage = {"input": 1_000_000, "output": 0, "cache_read_input_tokens": 400_000}

    result = cost_calculator.calculate_costs(usage_details=usage, price=price)
    # No cache rate -> the 400k cache-read tokens remain in the 1M input bucket at
    # the input rate; nothing is double-billed and nothing is lost.
    assert result["input_cost"] == Decimal("2")
    assert result["cache_read_cost"] == Decimal("0")
    assert result["total_cost"] == Decimal("2")


def test_calculate_costs_handles_malformed_usage_values():
    price = cost_calculator.ModelPrice(
        provider="openai",
        model_id="gpt-4o-mini",
        model_pattern="^gpt-4o-mini$",
        input_price_per_1m=Decimal("0.15"),
        output_price_per_1m=Decimal("0.60"),
        cache_read_price_per_1m=Decimal("0.08"),
        cache_write_price_per_1m=Decimal("0.2"),
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )
    usage = {
        "input_tokens": "not-a-number",
        "output_tokens": "",
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": "oops",
    }

    result = cost_calculator.calculate_costs(usage_details=usage, price=price)
    assert result["total_cost"] == Decimal("0")


def test_priced_token_count_reads_the_usage_the_way_calculate_costs_does():
    """One reading of a usage dict for the formula and for the run budget,
    which compares it with a reported total to find tokens nothing priced -
    a second copy of the keys would drift."""
    price = cost_calculator.ModelPrice(
        provider="stub",
        model_id="stub",
        model_pattern=None,
        input_price_per_1m=Decimal("1"),
        output_price_per_1m=Decimal("1"),
        cache_read_price_per_1m=Decimal("1"),
        cache_write_price_per_1m=Decimal("1"),
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )
    buckets = (
        cost_calculator.INPUT_TOKEN_KEYS,
        cost_calculator.OUTPUT_TOKEN_KEYS,
        cost_calculator.CACHE_READ_TOKEN_KEYS,
        cost_calculator.CACHE_WRITE_TOKEN_KEYS,
    )
    for keys in buckets:
        for key in keys:
            usage = {key: 1_000_000}
            assert cost_calculator.priced_token_count(usage) == 1_000_000, key
            assert cost_calculator.calculate_costs(usage, price)["total_cost"] == Decimal("1"), key

    # A key the formula does not read is not a priced token.
    # (With no reported total: a total is priced on its own, see the tests below.)
    unread = {"reasoning_tokens": 7}
    assert cost_calculator.priced_token_count(unread) == 0
    assert cost_calculator.calculate_costs(unread, price)["total_cost"] == Decimal("0")

    # The first key present decides a bucket, zero included.
    shadowed = {"input_tokens": 0, "prompt_tokens": 9}
    assert cost_calculator.priced_token_count(shadowed) == 0
    assert cost_calculator.calculate_costs(shadowed, price)["total_cost"] == Decimal("0")

    assert cost_calculator.priced_token_count(None) == 0


# ── Trace costs: the normalized usage the trace store prices ─────────

def _trace_price() -> cost_calculator.ModelPrice:
    """$1 in / $4 out / $0.10 cache read per 1M."""
    return cost_calculator.ModelPrice(
        provider="stub",
        model_id="stub",
        model_pattern=None,
        input_price_per_1m=Decimal("1"),
        output_price_per_1m=Decimal("4"),
        cache_read_price_per_1m=Decimal("0.1"),
        cache_write_price_per_1m=None,
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )


def _trace_cost(raw_usage: dict) -> Decimal:
    """What the trace store records: the Langfuse-normalized usage, priced."""
    from app.services.langfuse_tracing import _normalize_usage

    return cost_calculator.calculate_costs(_normalize_usage(raw_usage), _trace_price())["total_cost"]


def test_a_streamed_openai_call_is_priced_with_its_cached_and_reasoning_tokens():
    """The normalizer moves cached and reasoning tokens out of ``input`` /
    ``output`` into ``input_cached_tokens`` / ``output_reasoning_tokens``; the
    trace store used to price them $0 - $0.60 recorded for a $2.28 call."""
    streamed = {
        "prompt_tokens": 1_000_000,
        "completion_tokens": 500_000,
        "total_tokens": 1_500_000,
        "prompt_tokens_details": {"cached_tokens": 800_000},
        "completion_tokens_details": {"reasoning_tokens": 400_000},
    }
    # 200k uncached @ $1 + 800k cached @ $0.10 + 500k out (reasoning included) @ $4
    assert _trace_cost(streamed) == Decimal("2.28")


def test_cached_input_without_a_cache_rate_is_priced_at_the_input_rate():
    price = _trace_price()
    price.cache_read_price_per_1m = None
    normalized = {"input": 200_000, "output": 0, "input_cached_tokens": 800_000}
    assert cost_calculator.calculate_costs(normalized, price)["total_cost"] == Decimal("1")


def test_a_streamed_xai_call_keeps_its_output_and_reasoning_tokens():
    """xAI leaves reasoning out of ``completion_tokens``, so the normalizer's
    ``output`` clamps to 0; what the split misses is priced from the total."""
    streamed = {
        "prompt_tokens": 1_000_000,
        "completion_tokens": 100_000,
        "total_tokens": 1_500_000,
        "completion_tokens_details": {"reasoning_tokens": 400_000},
    }
    # 1M in @ $1 + 500k out (100k visible + 400k reasoning) @ $4
    assert _trace_cost(streamed) == Decimal("3")


def test_a_total_only_trace_is_priced_not_recorded_as_zero():
    total_only = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 250_000}
    # No split to go by: the higher of the two rates, as the run budget does.
    assert _trace_cost(total_only) == Decimal("1")


def test_the_trace_and_the_run_budget_price_the_same_usage_the_same_way():
    """One pricing rule for both: the budget debits the raw provider usage, the
    trace store prices its normalized form."""
    from app.tools.run_budget import RunBudget

    for raw in (
        {"prompt_tokens": 1_000_000, "completion_tokens": 100_000, "total_tokens": 1_500_000},
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 250_000},
        {"prompt_tokens": 300_000, "completion_tokens": 20_000, "total_tokens": 320_000},
        {"input_tokens": 1_000, "output_tokens": 50, "cache_read_input_tokens": 9_000},
    ):
        budget = RunBudget(max_cost_usd=100.0, price=_trace_price())
        budget.record_llm_usage(raw)
        assert budget.spent_cost_usd == _trace_cost(raw), raw


def test_raw_detail_maps_are_priced_like_their_normalized_form():
    """The budget reads raw provider usage, whose nested cached prompt tokens
    sit INSIDE ``prompt_tokens``: priced at the cache-read rate, the
    rest of the prompt at the input rate - as the trace prices the normalized
    form - and the reasoning tokens, inside ``completion_tokens``, only once."""
    raw = {
        "prompt_tokens": 1_000_000,
        "completion_tokens": 500_000,
        "total_tokens": 1_500_000,
        "prompt_tokens_details": {"cached_tokens": 800_000},
        "completion_tokens_details": {"reasoning_tokens": 400_000},
    }
    assert cost_calculator.calculate_costs(raw, _trace_price())["total_cost"] == Decimal("2.28")
    assert _trace_cost(raw) == Decimal("2.28")


def _price_without_cache_rates(cache_read: str | None = None) -> cost_calculator.ModelPrice:
    return cost_calculator.ModelPrice(
        provider="stub",
        model_id="stub",
        model_pattern=None,
        input_price_per_1m=Decimal("1"),
        output_price_per_1m=Decimal("4"),
        cache_read_price_per_1m=None if cache_read is None else Decimal(cache_read),
        cache_write_price_per_1m=None,
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )


def test_raw_cache_tokens_without_a_cache_rate_are_priced_at_the_input_rate():
    """A price row with no cache tariff (the seeded Amazon Nova rows have a
    null cache-write price) priced raw cache counters at $0, while the trace
    path keeps them at the input rate: the budget undercounted."""
    from app.tools.run_budget import RunBudget

    price = _price_without_cache_rates()
    raw = {
        "input_tokens": 1_000_000,
        "output_tokens": 0,
        "cache_read_input_tokens": 2_000_000,
        "cache_creation_input_tokens": 500_000,
    }
    # Every prompt token at the $1 input rate.
    costs = cost_calculator.price_usage(raw, price)
    assert costs is not None and costs.total_cost == Decimal("3.5")
    budget = RunBudget(max_cost_usd=100.0, price=price)
    budget.record_llm_usage(raw)
    assert budget.spent_cost_usd == Decimal("3.5")

    from app.services.langfuse_tracing import _normalize_usage

    assert cost_calculator.calculate_costs(_normalize_usage(raw), price)["total_cost"] == Decimal("3.5")


def test_a_missing_cache_write_rate_alone_falls_back_to_the_input_rate():
    """Cache reads keep their own tariff; only the side without one falls back."""
    from app.tools.run_budget import RunBudget

    price = _price_without_cache_rates(cache_read="0.1")
    raw = {
        "input_tokens": 1_000_000,
        "output_tokens": 0,
        "cache_read_input_tokens": 2_000_000,
        "cache_creation_input_tokens": 500_000,
    }
    # $1 input + 2M reads @ $0.10 + 500k writes at the $1 input rate.
    budget = RunBudget(max_cost_usd=100.0, price=price)
    budget.record_llm_usage(raw)
    assert budget.spent_cost_usd == Decimal("1.7")

    from app.services.langfuse_tracing import _normalize_usage

    assert cost_calculator.calculate_costs(_normalize_usage(raw), price)["total_cost"] == Decimal("1.7")


@pytest.mark.parametrize(
    "usage",
    [
        # Anthropic direct.
        {"input_tokens": 100, "output_tokens": 5, "cache_creation_input_tokens": 500},
        # AI gateway Claude, raw: the write is inside ``prompt_tokens``.
        {"prompt_tokens": 600, "completion_tokens": 5, "prompt_tokens_details": {"cache_write_tokens": 500}},
        # The same, Langfuse-normalized as a span stores it.
        {"input": 100, "output": 5, "input_cache_write_tokens": 500},
    ],
)
def test_cache_write_tokens_reads_every_shape_pricing_reads(usage):
    assert cost_calculator.cache_write_tokens(usage) == 500


def test_cache_write_tokens_is_zero_without_usage():
    assert cost_calculator.cache_write_tokens(None) == 0
    assert cost_calculator.cache_write_tokens({"input": 100, "output": 5}) == 0
