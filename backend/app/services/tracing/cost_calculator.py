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

"""Pricing resolution and deterministic trace cost calculation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm_model_price import LLMModelPrice

_ONE_MILLION = Decimal("1000000")
_ZERO = Decimal("0")

# The keys ``calculate_costs`` reads, bucket by bucket, in the order it reads
# them: the first key present in a bucket is that bucket's count.
INPUT_TOKEN_KEYS = ("input_tokens", "prompt_tokens", "input")
OUTPUT_TOKEN_KEYS = ("output_tokens", "completion_tokens", "output")
CACHE_READ_TOKEN_KEYS = ("cache_read_input_tokens", "cache_read_tokens")
CACHE_WRITE_TOKEN_KEYS = (
    "cache_creation_input_tokens",
    "cache_write_input_tokens",
    "cache_write_tokens",
)


@dataclass(slots=True)
class ModelPrice:
    provider: str
    model_id: str
    model_pattern: str | None
    input_price_per_1m: Decimal
    output_price_per_1m: Decimal
    cache_read_price_per_1m: Decimal | None
    cache_write_price_per_1m: Decimal | None
    effective_from: date
    is_custom: bool


def _to_decimal(value: object | None) -> Decimal:
    if value is None:
        return _ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _token_count(usage_details: dict | None, *keys: str) -> int:
    if not usage_details:
        return 0
    for key in keys:
        value = usage_details.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0
    return 0


def _row_to_model_price(row: LLMModelPrice) -> ModelPrice:
    return ModelPrice(
        provider=row.provider,
        model_id=row.model_id,
        model_pattern=row.model_pattern,
        input_price_per_1m=_to_decimal(row.input_price_per_1m),
        output_price_per_1m=_to_decimal(row.output_price_per_1m),
        cache_read_price_per_1m=(
            None if row.cache_read_price_per_1m is None else _to_decimal(row.cache_read_price_per_1m)
        ),
        cache_write_price_per_1m=(
            None if row.cache_write_price_per_1m is None else _to_decimal(row.cache_write_price_per_1m)
        ),
        effective_from=row.effective_from,
        is_custom=bool(row.is_custom),
    )


_PRICE_ORDER = (
    desc(LLMModelPrice.is_custom),
    desc(LLMModelPrice.effective_from),
    desc(LLMModelPrice.created_at),
)


async def _load_prices(db: AsyncSession, provider: str) -> list[LLMModelPrice]:
    result = await db.execute(
        select(LLMModelPrice)
        .where(LLMModelPrice.provider == provider)
        .order_by(*_PRICE_ORDER)
    )
    return list(result.scalars().all())


async def _load_all_prices(db: AsyncSession) -> list[LLMModelPrice]:
    result = await db.execute(select(LLMModelPrice).order_by(*_PRICE_ORDER))
    return list(result.scalars().all())


def _match_price(prices: list[LLMModelPrice], model_id: str) -> ModelPrice | None:
    """Exact ``model_id`` match first, then the first matching ``model_pattern``.

    ``prices`` is assumed pre-ordered (custom first, then most recent), so the
    first match is the preferred row.
    """
    for row in prices:
        if row.model_id == model_id:
            return _row_to_model_price(row)
    for row in prices:
        pattern = row.model_pattern
        if not pattern:
            continue
        try:
            if re.search(pattern, model_id):
                return _row_to_model_price(row)
        except re.error:
            continue
    return None


async def resolve_model_price(db: AsyncSession, model_id: str, provider: str) -> ModelPrice | None:
    return _match_price(await _load_prices(db, provider), model_id)


async def resolve_model_price_any(db: AsyncSession, model_id: str) -> ModelPrice | None:
    """Resolve a price by ``model_id`` across ALL providers.

    Used when the trace/span carries a model but no provider tag in its metadata
    (the common case for chat / assignment / general-chat traces, whose producers
    do not stamp a provider) so cost is still computed. Model ids are effectively
    unique across the seeded price table; when two providers share an id the
    pre-ordering (custom, then most recent) picks the preferred row.
    """
    return _match_price(await _load_all_prices(db), model_id)


def _usage_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def reported_total_tokens(usage_details: dict | None) -> int:
    """The explicit total a usage dict carries, ``0`` when it carries none."""
    if not usage_details:
        return 0
    return _usage_int(usage_details.get("total_tokens")) or _usage_int(usage_details.get("total"))


# The Langfuse-normalized usage (``langfuse_tracing._normalize_usage``) moves
# OpenAI's ``*_tokens_details`` counts OUT of ``input`` / ``output`` into
# flattened keys: ``input_cached_tokens``, ``output_reasoning_tokens``, ... They
# are still billed, so they are priced back: cached input at the cache-read rate,
# cache writes (the AI gateway's ``cache_write_tokens``) at the cache-write rate,
# every other detail at its side's rate.
_CACHED_INPUT_DETAIL = "input_cached_tokens"
_CACHE_WRITE_INPUT_DETAIL = "input_cache_write_tokens"


def _detail_tokens(usage: dict, side: str) -> dict[str, int]:
    base = f"{side}_tokens"
    return {
        key: count
        for key, value in usage.items()
        if isinstance(key, str)
        and key.startswith(f"{side}_")
        and key != base
        and (count := _usage_int(value)) > 0
    }


def _nested_input_detail(usage: dict, name: str) -> int:
    for key in ("prompt_tokens_details", "input_tokens_details"):
        details = usage.get(key)
        if isinstance(details, dict):
            count = _usage_int(details.get(name))
            if count > 0:
                return count
    return 0


@dataclass(frozen=True, slots=True)
class _UsageTokens:
    input: int
    output: int
    cache_read: int
    cache_write: int
    cached_input_detail: int
    cache_write_detail: int
    input_detail: int
    output_detail: int
    raw_input: bool

    @property
    def priced(self) -> int:
        return (
            self.input
            + self.output
            + self.cache_read
            + self.cache_write
            + self.cached_input_detail
            + self.cache_write_detail
            + self.input_detail
            + self.output_detail
        )


def _read_usage(usage: dict) -> _UsageTokens:
    raw_input = usage.get("input_tokens") is not None or usage.get("prompt_tokens") is not None
    cache_read = _token_count(usage, *CACHE_READ_TOKEN_KEYS)
    cache_write = _token_count(usage, *CACHE_WRITE_TOKEN_KEYS)
    input_details: dict[str, int] = {}
    output_details: dict[str, int] = {}
    if not raw_input:
        # Raw provider usage keeps its details nested (``prompt_tokens_details``),
        # already inside ``prompt_tokens`` / ``completion_tokens``: only the
        # normalized shape carries them flattened and outside its buckets.
        input_details = _detail_tokens(usage, "input")
        output_details = _detail_tokens(usage, "output")
        if cache_read:
            # A cache-read bucket already prices the cached tokens.
            input_details.pop(_CACHED_INPUT_DETAIL, None)
        if cache_write:
            input_details.pop(_CACHE_WRITE_INPUT_DETAIL, None)
    cached = input_details.pop(_CACHED_INPUT_DETAIL, 0)
    written = input_details.pop(_CACHE_WRITE_INPUT_DETAIL, 0)
    input_tokens = _token_count(usage, *INPUT_TOKEN_KEYS)
    if raw_input and not cache_read:
        # Raw usage names its cached prompt tokens in the nested, OpenAI-shaped
        # ``prompt_tokens_details.cached_tokens`` (OpenAI, and Gemini's
        # ``cached_content_token_count``). They are INSIDE the prompt
        # count, so they move out of it rather than being added; a cache-read
        # bucket, when present, already prices them and wins.
        cached = min(_nested_input_detail(usage, "cached_tokens"), max(input_tokens, 0))
        input_tokens -= cached
    if raw_input and not cache_write:
        # The AI gateway's Claude writes, also inside ``prompt_tokens``.
        written = min(_nested_input_detail(usage, "cache_write_tokens"), max(input_tokens, 0))
        input_tokens -= written
    return _UsageTokens(
        input=input_tokens,
        output=_token_count(usage, *OUTPUT_TOKEN_KEYS),
        cache_read=cache_read,
        cache_write=cache_write,
        cached_input_detail=cached,
        cache_write_detail=written,
        input_detail=sum(input_details.values()),
        output_detail=sum(output_details.values()),
        raw_input=raw_input,
    )


def unaccounted_token_rate(price: ModelPrice) -> Decimal:
    """Rate for tokens only a reported total accounts for: the HIGHER one.

    With no split to go by, a cost that must never be under-counted reads every
    such token as costing the most it could.
    """
    return max(price.input_price_per_1m, price.output_price_per_1m)


@dataclass(frozen=True, slots=True)
class UsageCosts:
    """One usage priced: the costs by bucket and what had to be estimated.

    ``estimated_tokens`` are the tokens a reported total carries beyond what
    the split prices (no split at all, or xAI's reasoning tokens outside its
    ``completion_tokens``), priced at :func:`unaccounted_token_rate` into
    ``estimated_cost``. ``priced_tokens`` is what the split itself accounted for.
    """

    input_cost: Decimal
    output_cost: Decimal
    cache_read_cost: Decimal
    cache_write_cost: Decimal
    estimated_cost: Decimal
    total_cost: Decimal
    priced_tokens: int
    estimated_tokens: int


def price_usage(usage_details: dict | None, price: ModelPrice | None) -> UsageCosts | None:
    """The ONE pricing rule for a usage dict - trace costs and the run budget.

    ``None`` when there is no price row. Reads both usage conventions: raw
    provider usage (what the run budget debits) and the Langfuse-normalized
    shape (what the trace store prices).
    """
    if price is None:
        return None

    usage = usage_details or {}
    tokens = _read_usage(usage)

    # Two input conventions reach this function:
    #   * raw provider usage (``input_tokens`` / ``prompt_tokens``) keeps the
    #     non-cached prompt count separate from the cache counters (Anthropic), or
    #     already excludes cached tokens (OpenAI peels them into detail maps); and
    #   * the Langfuse-v4 normalized shape (``input``) folds cache read + creation
    #     tokens INTO the input bucket (see langfuse_tracing._normalize_usage).
    # Pricing the normalized ``input`` at the full input rate AND adding the cache
    # costs would double-bill the cache tokens, so when the count came from the
    # cache-inclusive ``input`` key we peel cache tokens back out of the full-rate
    # bucket - but only those we have a discounted cache rate to re-bill at, so a
    # model without cache pricing keeps charging them at the input rate (no loss).
    billable_input = tokens.input
    if not tokens.raw_input:
        if price.cache_read_price_per_1m is not None:
            billable_input -= tokens.cache_read
        if price.cache_write_price_per_1m is not None:
            billable_input -= tokens.cache_write
        billable_input = max(billable_input, 0)

    cached_rate = price.cache_read_price_per_1m
    if cached_rate is None:
        # No cache tariff: cached input is billed like the rest of the input.
        billable_input += tokens.cached_input_detail
    input_cost = (
        Decimal(billable_input + tokens.input_detail) * price.input_price_per_1m
    ) / _ONE_MILLION
    output_cost = (
        Decimal(tokens.output + tokens.output_detail) * price.output_price_per_1m
    ) / _ONE_MILLION

    # A missing cache tariff bills cache tokens at the input rate. The
    # normalized shape already does so by leaving them inside ``input`` (the
    # peel above only runs for a side that has a rate); raw usage keeps them in
    # their own bucket, which is therefore priced at the input rate itself.
    fallback = price.input_price_per_1m if tokens.raw_input else _ZERO
    cache_read_rate = cached_rate if cached_rate is not None else fallback
    cache_write_rate = (
        price.cache_write_price_per_1m
        if price.cache_write_price_per_1m is not None
        else fallback
    )

    cache_read_cost = (Decimal(tokens.cache_read) * cache_read_rate) / _ONE_MILLION
    if cached_rate is not None:
        cache_read_cost += (Decimal(tokens.cached_input_detail) * cached_rate) / _ONE_MILLION
    # Detail writes sit outside ``input`` in both shapes: no cache tariff, input rate.
    detail_write_rate = (
        price.cache_write_price_per_1m
        if price.cache_write_price_per_1m is not None
        else price.input_price_per_1m
    )
    cache_write_cost = (
        Decimal(tokens.cache_write) * cache_write_rate
        + Decimal(tokens.cache_write_detail) * detail_write_rate
    ) / _ONE_MILLION

    estimated_tokens = max(reported_total_tokens(usage) - tokens.priced, 0)
    estimated_cost = (
        Decimal(estimated_tokens) * unaccounted_token_rate(price)
    ) / _ONE_MILLION

    return UsageCosts(
        input_cost=input_cost,
        output_cost=output_cost,
        cache_read_cost=cache_read_cost,
        cache_write_cost=cache_write_cost,
        estimated_cost=estimated_cost,
        total_cost=input_cost + output_cost + cache_read_cost + cache_write_cost + estimated_cost,
        priced_tokens=tokens.priced,
        estimated_tokens=estimated_tokens,
    )


def calculate_costs(usage_details: dict | None, price: ModelPrice | None) -> dict[str, Decimal | None]:
    """:func:`price_usage` as the per-bucket dict the trace sinks store."""
    costs = price_usage(usage_details, price)
    if costs is None:
        return {
            "input_cost": None,
            "output_cost": None,
            "cache_read_cost": None,
            "cache_write_cost": None,
            "estimated_cost": None,
            "total_cost": None,
        }
    return {
        "input_cost": costs.input_cost,
        "output_cost": costs.output_cost,
        "cache_read_cost": costs.cache_read_cost,
        "cache_write_cost": costs.cache_write_cost,
        "estimated_cost": costs.estimated_cost,
        "total_cost": costs.total_cost,
    }


def priced_token_count(usage_details: dict | None) -> int:
    """Tokens the split of a usage dict prices, all buckets together.

    The count :func:`price_usage` compares with a reported total. Cache tokens
    folded into the Langfuse-normalized ``input`` are counted twice here, which
    can only overstate the count - never report a token the formula missed.
    """
    return _read_usage(usage_details or {}).priced


def cached_input_tokens(usage_details: dict | None) -> int:
    """The input tokens of one call's usage a prompt cache served (read), whatever its provider's shape.

    Part of the input :func:`input_output_tokens` counts, never added to it.
    """
    usage = _read_usage(usage_details or {})
    return usage.cache_read + usage.cached_input_detail


def cache_write_tokens(usage_details: dict | None) -> int:
    """The input tokens of one call's usage written to a prompt cache, whatever its provider's shape.

    Part of the input :func:`input_output_tokens` counts, never added to it.
    """
    usage = _read_usage(usage_details or {})
    return usage.cache_write + usage.cache_write_detail


def input_output_tokens(usage_details: dict | None) -> tuple[int, int]:
    """The input and output tokens of one call's usage, whatever its provider's shape.

    Input counts every prompt token the call was billed for - fresh, cache read,
    cache write and the flattened details of the normalized shape - so two calls
    sending the same prompt count the same whether or not a cache answered.
    """
    usage = _read_usage(usage_details or {})
    return (
        usage.input
        + usage.cache_read
        + usage.cache_write
        + usage.cached_input_detail
        + usage.cache_write_detail
        + usage.input_detail,
        usage.output + usage.output_detail,
    )
