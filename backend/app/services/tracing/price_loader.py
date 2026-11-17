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

"""Model pricing seed loading and startup synchronization."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.llm_model_price import LLMModelPrice

MODEL_PRICES_PATH = Path(__file__).resolve().parents[2] / "data" / "model_prices.json"
LITELLM_MODEL_PRICES_PATH = (
    Path(__file__).resolve().parents[2]
    / "agents"
    / "seed_data"
    / "model_prices_and_context_window.json"
)
DEFAULT_LITELLM_MODEL_PRICES_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/"
    "litellm_internal_staging/model_prices_and_context_window.json"
)
_ONE_MILLION = Decimal("1000000")


class ModelPriceSeed(BaseModel):
    provider: str
    model_id: str
    model_pattern: str | None = None
    input_price_per_1m: Decimal
    output_price_per_1m: Decimal
    cache_read_price_per_1m: Decimal | None = None
    cache_write_price_per_1m: Decimal | None = None
    effective_from: date | None = None

    @field_validator("model_pattern")
    @classmethod
    def validate_model_pattern(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"Invalid model_pattern regex: {exc}") from exc
        return value


class ModelPricesPayload(BaseModel):
    schema_version: str
    updated_at: datetime
    prices: list[ModelPriceSeed] = Field(default_factory=list)


def _coerce_path(path_value: str | None, *, default: Path) -> Path:
    raw = (path_value or "").strip()
    if not raw:
        return default

    path = Path(raw).expanduser()
    if path.is_absolute():
        return path

    # Relative paths are resolved from the backend root for deterministic
    # behavior regardless of process CWD.
    backend_root = Path(__file__).resolve().parents[3]
    return (backend_root / path).resolve()


def resolve_model_prices_source_path() -> Path:
    """Resolve the active source path for model price synchronization."""
    if settings.model_prices_source_path.strip():
        return _coerce_path(
            settings.model_prices_source_path,
            default=MODEL_PRICES_PATH,
        )

    if settings.model_prices_git_sync_enabled:
        git_target = resolve_model_prices_git_target_path()
        if git_target.exists():
            return git_target

    return MODEL_PRICES_PATH


def resolve_model_prices_git_target_path() -> Path:
    """Resolve the on-disk target file for periodic git-backed pulls."""
    return _coerce_path(
        settings.model_prices_git_sync_target_path,
        default=LITELLM_MODEL_PRICES_PATH,
    )


def _to_non_negative_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if decimal_value < 0:
        return None
    return decimal_value


def _litellm_cost_per_token(entry: dict[str, Any], *keys: str) -> Decimal:
    for key in keys:
        value = _to_non_negative_decimal(entry.get(key))
        if value is not None:
            return value
    return Decimal("0")


def _convert_litellm_payload(raw: dict[str, Any]) -> ModelPricesPayload:
    prices: list[ModelPriceSeed] = []

    for model_key, details in raw.items():
        if model_key == "sample_spec":
            continue
        if not isinstance(model_key, str) or not isinstance(details, dict):
            continue

        mode = str(details.get("mode") or "chat").strip().lower()
        if mode != "chat":
            continue

        provider = str(details.get("litellm_provider") or "").strip().lower()
        if not provider:
            continue

        input_per_token = _litellm_cost_per_token(details, "input_cost_per_token")
        output_per_token = _litellm_cost_per_token(details, "output_cost_per_token")
        cache_read_per_token = _litellm_cost_per_token(
            details,
            "cache_read_input_token_cost",
            "cache_read_cost_per_token",
        )
        cache_write_per_token = _litellm_cost_per_token(
            details,
            "cache_creation_input_token_cost",
            "cache_write_input_token_cost",
            "cache_write_cost_per_token",
        )

        # Skip rows with no token pricing signal at all.
        if (
            input_per_token == 0
            and output_per_token == 0
            and cache_read_per_token == 0
            and cache_write_per_token == 0
        ):
            continue

        prices.append(
            ModelPriceSeed(
                provider=provider,
                model_id=model_key,
                input_price_per_1m=input_per_token * _ONE_MILLION,
                output_price_per_1m=output_per_token * _ONE_MILLION,
                cache_read_price_per_1m=cache_read_per_token * _ONE_MILLION,
                cache_write_price_per_1m=cache_write_per_token * _ONE_MILLION,
            )
        )

    return ModelPricesPayload(
        schema_version="litellm-1.0.0",
        updated_at=datetime.now(UTC),
        prices=prices,
    )


def _looks_like_litellm_payload(raw: dict[str, Any]) -> bool:
    if "sample_spec" in raw:
        return True

    model_entries = 0
    for key, value in raw.items():
        if not isinstance(key, str) or key == "sample_spec":
            continue
        if not isinstance(value, dict):
            continue
        model_entries += 1
        if (
            "litellm_provider" in value
            or "input_cost_per_token" in value
            or "output_cost_per_token" in value
            or "mode" in value
        ):
            return True
        if model_entries >= 5:
            break
    return False


def _normalize_payload_datetime(payload: ModelPricesPayload) -> ModelPricesPayload:
    if payload.updated_at.tzinfo is None:
        payload.updated_at = payload.updated_at.replace(tzinfo=UTC)
    else:
        payload.updated_at = payload.updated_at.astimezone(UTC)
    return payload


def _parse_model_prices_payload(raw: Any) -> ModelPricesPayload:
    if not isinstance(raw, dict):
        raise ValueError("Model prices payload must be a JSON object")

    # Keep strict validation for native payloads (including malformed variants
    # missing schema_version), and only fallback to LiteLLM conversion when the
    # object clearly looks like LiteLLM's model map.
    if "prices" in raw or "updated_at" in raw:
        payload = ModelPricesPayload.model_validate(raw)
    elif _looks_like_litellm_payload(raw):
        payload = _convert_litellm_payload(raw)
    else:
        raise ValueError("Unsupported model prices payload format")

    return _normalize_payload_datetime(payload)


def load_model_prices_payload(path: Path | None = None) -> ModelPricesPayload:
    target = path or resolve_model_prices_source_path()
    raw = json.loads(target.read_text(encoding="utf-8"))
    return _parse_model_prices_payload(raw)


def parse_model_prices_payload(raw: Any) -> ModelPricesPayload:
    """Parse model pricing payload with native + LiteLLM format support."""
    return _parse_model_prices_payload(raw)


async def pull_model_prices_git_source(
    *,
    url: str | None = None,
    target_path: Path | None = None,
    timeout_secs: int | None = None,
) -> tuple[bool, ModelPricesPayload]:
    """Pull and validate the git-backed model-prices source file.

    Returns ``(updated, parsed_payload)`` where ``updated`` indicates whether
    the local target file content changed.
    """
    source_url = (url or settings.model_prices_git_sync_url or "").strip()
    if not source_url:
        source_url = DEFAULT_LITELLM_MODEL_PRICES_URL

    target = target_path or resolve_model_prices_git_target_path()
    timeout = timeout_secs or settings.model_prices_git_sync_timeout_secs

    async with httpx.AsyncClient(
        timeout=max(5, int(timeout)),
        follow_redirects=True,
    ) as client:
        response = await client.get(source_url, headers={"Accept": "application/json"})
        response.raise_for_status()

    raw_payload = response.json()
    parsed_payload = _parse_model_prices_payload(raw_payload)

    body_text = response.text
    if not body_text.endswith("\n"):
        body_text += "\n"
    body_bytes = body_text.encode("utf-8")
    incoming_hash = hashlib.sha256(body_bytes).hexdigest()

    existing_hash = None
    if target.exists():
        existing_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    updated = incoming_hash != existing_hash
    if updated:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_target = target.with_suffix(f"{target.suffix}.tmp")
        temp_target.write_bytes(body_bytes)
        temp_target.replace(target)

    return updated, parsed_payload


async def _load_existing_prices(db: AsyncSession) -> list[LLMModelPrice]:
    result = await db.execute(
        select(LLMModelPrice).order_by(
            desc(LLMModelPrice.is_custom),
            desc(LLMModelPrice.effective_from),
            desc(LLMModelPrice.created_at),
            desc(LLMModelPrice.id),
        )
    )
    return list(result.scalars().all())


async def sync_model_prices(
    db: AsyncSession,
    *,
    payload: ModelPricesPayload | None = None,
    path: Path | None = None,
) -> tuple[int, int]:
    """Sync seed prices into llm_model_prices.

    Returns ``(synced_count, skipped_custom_count)``.
    """
    data = payload or load_model_prices_payload(path)
    existing_prices = await _load_existing_prices(db)

    custom_keys = {
        (row.provider, row.model_id)
        for row in existing_prices
        if bool(getattr(row, "is_custom", False))
    }
    non_custom_by_key: dict[tuple[str, str], LLMModelPrice] = {}
    for row in existing_prices:
        if bool(getattr(row, "is_custom", False)):
            continue
        key = (row.provider, row.model_id)
        # Preserve first (most recent) row from ordered query.
        non_custom_by_key.setdefault(key, row)

    synced_count = 0
    skipped_custom_count = 0

    for item in data.prices:
        key = (item.provider, item.model_id)
        if key in custom_keys:
            skipped_custom_count += 1
            continue

        target = non_custom_by_key.get(key)
        effective_from = item.effective_from or datetime.now(UTC).date()

        if target is None:
            db.add(
                LLMModelPrice(
                    provider=item.provider,
                    model_id=item.model_id,
                    model_pattern=item.model_pattern,
                    input_price_per_1m=item.input_price_per_1m,
                    output_price_per_1m=item.output_price_per_1m,
                    cache_read_price_per_1m=item.cache_read_price_per_1m,
                    cache_write_price_per_1m=item.cache_write_price_per_1m,
                    effective_from=effective_from,
                    is_custom=False,
                )
            )
            synced_count += 1
            continue

        target.model_pattern = item.model_pattern
        target.input_price_per_1m = item.input_price_per_1m
        target.output_price_per_1m = item.output_price_per_1m
        target.cache_read_price_per_1m = item.cache_read_price_per_1m
        target.cache_write_price_per_1m = item.cache_write_price_per_1m
        target.effective_from = effective_from
        synced_count += 1

    await db.commit()
    return synced_count, skipped_custom_count
