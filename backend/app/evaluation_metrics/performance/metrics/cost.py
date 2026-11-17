# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""LLM-only estimated cost from caller-supplied pricing.

Priced against the *normalized* usage, so the ``separate`` and ``inclusive`` token conventions
cost the same. Only agent-scoped usage counts; evaluation overhead is excluded.
"""

from collections.abc import Mapping
from decimal import Decimal

from app.evaluation_metrics.core.contracts import MetricDirection, MetricRollup, MetricStatus
from app.evaluation_metrics.core.errors import ConfigurationError
from app.evaluation_metrics.performance.pricing import CostBasis, PriceTable
from app.evaluation_metrics.performance.results import (
    PerformanceMetadata,
    PerformanceResult,
)
from app.evaluation_metrics.performance.telemetry import (
    ExecutionTelemetry,
    NormalizedTokenUsage,
    normalize_token_usage,
)
from app.evaluation_metrics.performance.units import MetricUnit

COST_META = PerformanceMetadata(
    key="performance.cost.llm",
    label="Estimated LLM Cost",
    kind="cost",
    primary_measure="estimated_cost",
    unit=MetricUnit.currency,
    direction=MetricDirection.lower_is_better,
    rollup=MetricRollup.mean,
)

# Bucket name -> (normalized-usage attribute, PriceTable attribute).
_BUCKETS = (
    ("uncached_input_tokens", "uncached_input_tokens", "uncached_input_per_token"),
    ("output_tokens", "output_tokens", "output_per_token"),
    ("cache_read_input_tokens", "cache_read_input_tokens", "cache_read_per_token"),
    ("cache_write_input_tokens", "cache_write_input_tokens", "cache_write_per_token"),
)


def _unavailable(reason: str) -> PerformanceResult:
    return PerformanceResult(
        status=MetricStatus.unavailable,
        value=None,
        detail={"reason": reason},
        metadata=COST_META,
    )


def _priced_buckets(
    normalized: NormalizedTokenUsage, prices: PriceTable
) -> tuple[Decimal, dict[str, str], dict[str, int]]:
    total = Decimal(0)
    applied_prices: dict[str, str] = {}
    applied_counts: dict[str, int] = {}
    for bucket_name, usage_attr, price_attr in _BUCKETS:
        count = getattr(normalized, usage_attr)
        if count is None:
            # Not captured: no contribution, no price required.
            continue
        price = getattr(prices, price_attr)
        if price is None:
            raise ConfigurationError(
                f"PriceTable is missing a price for the captured bucket {bucket_name!r}"
            )
        total += Decimal(count) * price
        applied_prices[bucket_name] = format(price, "f")
        applied_counts[bucket_name] = count
    return total, applied_prices, applied_counts


def _basis(normalized: NormalizedTokenUsage, prices: PriceTable) -> tuple[Decimal, CostBasis]:
    total, applied_prices, applied_counts = _priced_buckets(normalized, prices)
    return total, CostBasis(
        currency=prices.currency,
        prices=applied_prices,
        resolved_model=prices.resolved_model,
        resolved_provider=prices.resolved_provider,
        price_snapshot_id=prices.price_snapshot_id,
        normalized_counts=applied_counts,
    )


def _scored(total: Decimal, detail: dict) -> PerformanceResult:
    return PerformanceResult(
        status=MetricStatus.scored,
        value=float(total),
        detail=detail,
        metadata=COST_META,
    )


def estimated_llm_cost(
    telemetry: ExecutionTelemetry,
    prices: PriceTable | None = None,
    *,
    prices_by_model: Mapping[str | None, PriceTable] | None = None,
) -> PerformanceResult:
    """Report the LLM-only estimated cost of agent-scoped token usage.

    Args:
        telemetry: The execution's captured observations.
        prices: The caller-supplied per-token prices. ``None`` yields ``unavailable``.
        prices_by_model: A price table per serving model, for an execution whose usage
            is split by model (``agent_token_usage_by_model``). Each part is priced at
            its own model's table. Without it, ``prices`` prices a single part;
            it cannot price several, which are then reported ``unavailable``.

    Returns:
        ``scored`` with the summed cost and a ``detail`` echoing the applied :class:`CostBasis`
        — one per model under ``parts`` when more than one model is priced — or ``unavailable``
        when usage or pricing was not supplied, when several models were given one table, or
        when the models' tables disagree on currency.

    Raises:
        ConfigurationError: If a table omits a price for a bucket the normalized usage
            captured (even when that bucket's count is ``0``).
    """
    parts = telemetry.agent_usage_parts()
    if parts is None:
        return _unavailable("token_usage_not_captured")

    priced: list[tuple[str | None, NormalizedTokenUsage, PriceTable]] = []
    for part in parts:
        normalized = normalize_token_usage(part.usage)
        if normalized.total_tokens is None:
            continue
        table = prices_by_model.get(part.model) if prices_by_model is not None else prices
        if table is None:
            return _unavailable("pricing_not_supplied")
        priced.append((part.model, normalized, table))
    if not priced:
        return _unavailable("token_usage_not_captured")
    if prices_by_model is None and len(priced) > 1:
        # One table would price every model at one model's rates.
        return _unavailable("pricing_per_model_not_supplied")

    if len(priced) == 1:
        _model, normalized, table = priced[0]
        total, basis = _basis(normalized, table)
        return _scored(total, basis.model_dump(mode="json"))

    currency = priced[0][2].currency
    if any(table.currency != currency for _model, _normalized, table in priced):
        return _unavailable("mixed_currency")
    total = Decimal(0)
    detail_parts: list[dict] = []
    for model, normalized, table in priced:
        part_total, basis = _basis(normalized, table)
        total += part_total
        detail_parts.append({"model": model, **basis.model_dump(mode="json")})
    return _scored(total, {"currency": currency, "parts": detail_parts})


__all__ = ["COST_META", "estimated_llm_cost"]
