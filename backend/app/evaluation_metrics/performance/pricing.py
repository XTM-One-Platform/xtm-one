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

"""Caller-supplied LLM pricing, the only source of cost data.

The package holds no provider price table and never infers one: without a
:class:`PriceTable` the cost metric reports ``unavailable``. Only LLM token pricing is in
scope; tool and service fees are not priced, so there is no ``tool_prices`` field.
"""

from collections.abc import Mapping
from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


def _non_negative_price(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    if not value.is_finite() or value < 0:
        raise ValueError("a price must be finite and non-negative")
    return value


class PriceTable(BaseModel):
    """Caller-supplied per-token prices, echoed back for reproducibility.

    Prices apply to the *normalized* usage (:func:`normalize_token_usage`), never the raw
    provider buckets, so the ``separate`` and ``inclusive`` conventions cost the same.
    """

    model_config = ConfigDict(frozen=True)

    currency: str
    uncached_input_per_token: Decimal | None = None
    output_per_token: Decimal | None = None
    cache_read_per_token: Decimal | None = None
    cache_write_per_token: Decimal | None = None
    resolved_model: str | None = None
    resolved_provider: str | None = None
    price_snapshot_id: str | None = None

    @field_validator(
        "uncached_input_per_token",
        "output_per_token",
        "cache_read_per_token",
        "cache_write_per_token",
    )
    @classmethod
    def _check_price(cls, value: Decimal | None) -> Decimal | None:
        return _non_negative_price(value)

    @model_validator(mode="after")
    def _check_currency(self) -> Self:
        if not self.currency:
            raise ValueError("PriceTable.currency must be non-empty")
        return self


class CostBasis(BaseModel):
    """The pricing inputs echoed on a cost result so the calculation can be reproduced."""

    model_config = ConfigDict(frozen=True)

    currency: str
    prices: Mapping[str, str]
    resolved_model: str | None = None
    resolved_provider: str | None = None
    price_snapshot_id: str | None = None
    normalized_counts: Mapping[str, int]


__all__ = ["CostBasis", "PriceTable"]
