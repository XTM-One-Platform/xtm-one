from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.services.tracing import price_loader


class _FakeDB:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.committed = False

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.committed = True


def test_load_model_prices_payload_requires_schema_version(tmp_path):
    payload = {
        "updated_at": "2026-06-14T10:00:00Z",
        "prices": [
            {
                "provider": "openai",
                "model_id": "gpt-4o-mini",
                "input_price_per_1m": 0.15,
                "output_price_per_1m": 0.6,
            }
        ],
    }
    data_file = tmp_path / "model_prices.json"
    data_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError):
        price_loader.load_model_prices_payload(data_file)


def test_load_model_prices_payload_rejects_invalid_updated_at(tmp_path):
    payload = {
        "schema_version": "1.0.0",
        "updated_at": "not-a-date",
        "prices": [],
    }
    data_file = tmp_path / "model_prices.json"
    data_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError):
        price_loader.load_model_prices_payload(data_file)


def test_load_model_prices_payload_rejects_invalid_model_pattern(tmp_path):
    payload = {
        "schema_version": "1.0.0",
        "updated_at": "2026-06-14T10:00:00Z",
        "prices": [
            {
                "provider": "openai",
                "model_id": "gpt-4o-mini",
                "model_pattern": "[unterminated(",
                "input_price_per_1m": 0.15,
                "output_price_per_1m": 0.6,
            }
        ],
    }
    data_file = tmp_path / "model_prices.json"
    data_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError):
        price_loader.load_model_prices_payload(data_file)


def test_load_model_prices_payload_normalizes_to_utc(tmp_path):
    payload = {
        "schema_version": "1.0.0",
        "updated_at": "2026-06-14T16:34:45+02:00",
        "prices": [],
    }
    data_file = tmp_path / "model_prices.json"
    data_file.write_text(json.dumps(payload), encoding="utf-8")

    parsed = price_loader.load_model_prices_payload(data_file)
    assert parsed.updated_at.isoformat() == "2026-06-14T14:34:45+00:00"


def test_load_model_prices_payload_accepts_litellm_shape(tmp_path):
    payload = {
        "sample_spec": {"mode": "chat"},
        "gpt-4o-mini": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 0.00000015,
            "output_cost_per_token": 0.00000060,
        },
        "dall-e-3": {
            "litellm_provider": "openai",
            "mode": "image_generation",
            "output_cost_per_image": 0.04,
        },
    }
    data_file = tmp_path / "model_prices_and_context_window.json"
    data_file.write_text(json.dumps(payload), encoding="utf-8")

    parsed = price_loader.load_model_prices_payload(data_file)

    assert parsed.schema_version == "litellm-1.0.0"
    assert len(parsed.prices) == 1
    seed = parsed.prices[0]
    assert seed.provider == "openai"
    assert seed.model_id == "gpt-4o-mini"
    assert seed.input_price_per_1m == Decimal("0.15000000")
    assert seed.output_price_per_1m == Decimal("0.60000000")


def test_resolve_model_prices_source_path_uses_git_target_if_present(
    tmp_path,
    monkeypatch,
):
    git_target = tmp_path / "model_prices_and_context_window.json"
    git_target.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(price_loader.settings, "model_prices_source_path", "")
    monkeypatch.setattr(price_loader.settings, "model_prices_git_sync_enabled", True)
    monkeypatch.setattr(
        price_loader.settings,
        "model_prices_git_sync_target_path",
        str(git_target),
    )

    resolved = price_loader.resolve_model_prices_source_path()
    assert resolved == git_target


def test_resolve_model_prices_source_path_falls_back_when_git_target_missing(
    tmp_path,
    monkeypatch,
):
    missing_target = tmp_path / "missing.json"

    monkeypatch.setattr(price_loader.settings, "model_prices_source_path", "")
    monkeypatch.setattr(price_loader.settings, "model_prices_git_sync_enabled", True)
    monkeypatch.setattr(
        price_loader.settings,
        "model_prices_git_sync_target_path",
        str(missing_target),
    )

    resolved = price_loader.resolve_model_prices_source_path()
    assert resolved == price_loader.MODEL_PRICES_PATH


@pytest.mark.asyncio
async def test_pull_model_prices_git_source_writes_and_parses(tmp_path, monkeypatch):
    target = tmp_path / "model_prices_and_context_window.json"
    body = {
        "sample_spec": {"mode": "chat"},
        "gpt-4o-mini": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 0.00000015,
            "output_cost_per_token": 0.00000060,
        },
    }

    class _Response:
        def __init__(self, payload: dict):
            self._payload = payload
            self.text = json.dumps(payload)

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *_args, **_kwargs):
            return _Response(body)

    monkeypatch.setattr(price_loader.httpx, "AsyncClient", _Client)

    updated, parsed = await price_loader.pull_model_prices_git_source(
        url="https://example.invalid/model_prices_and_context_window.json",
        target_path=target,
        timeout_secs=10,
    )

    assert updated is True
    assert target.exists()
    assert parsed.schema_version == "litellm-1.0.0"
    assert len(parsed.prices) == 1

    # Pulling the same body again should be a no-op on disk content.
    updated2, parsed2 = await price_loader.pull_model_prices_git_source(
        url="https://example.invalid/model_prices_and_context_window.json",
        target_path=target,
        timeout_secs=10,
    )
    assert updated2 is False
    assert len(parsed2.prices) == 1


@pytest.mark.asyncio
async def test_sync_model_prices_preserves_custom_and_updates_non_custom(monkeypatch):
    payload = price_loader.ModelPricesPayload.model_validate(
        {
            "schema_version": "1.0.0",
            "updated_at": "2026-06-14T10:00:00Z",
            "prices": [
                {
                    "provider": "openai",
                    "model_id": "gpt-4o-mini",
                    "model_pattern": "^gpt-4o-mini(-.*)?$",
                    "input_price_per_1m": 0.16,
                    "output_price_per_1m": 0.62,
                    "cache_read_price_per_1m": 0.08,
                    "cache_write_price_per_1m": 0.2,
                },
                {
                    "provider": "anthropic",
                    "model_id": "claude-sonnet-4-20250514",
                    "model_pattern": "^claude-sonnet-4-.*$",
                    "input_price_per_1m": 3.0,
                    "output_price_per_1m": 15.0,
                    "cache_read_price_per_1m": 0.3,
                    "cache_write_price_per_1m": 3.75,
                    "effective_from": "2026-06-14",
                },
            ],
        }
    )

    custom_row = SimpleNamespace(
        provider="openai",
        model_id="gpt-4o-mini",
        model_pattern="^gpt-4o-mini$",
        input_price_per_1m=0.14,
        output_price_per_1m=0.55,
        cache_read_price_per_1m=0.07,
        cache_write_price_per_1m=0.18,
        effective_from=date(2026, 1, 1),
        is_custom=True,
    )
    non_custom_row = SimpleNamespace(
        provider="anthropic",
        model_id="claude-sonnet-4-20250514",
        model_pattern="^claude-sonnet-4$",
        input_price_per_1m=2.0,
        output_price_per_1m=10.0,
        cache_read_price_per_1m=0.2,
        cache_write_price_per_1m=2.5,
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )

    async def _fake_load_existing_prices(_db):
        return [custom_row, non_custom_row]

    monkeypatch.setattr(price_loader, "_load_existing_prices", _fake_load_existing_prices)

    db = _FakeDB()
    synced, skipped = await price_loader.sync_model_prices(db, payload=payload)

    assert synced == 1
    assert skipped == 1
    assert db.committed is True

    # Custom row must be preserved unchanged.
    assert custom_row.input_price_per_1m == 0.14
    assert custom_row.output_price_per_1m == 0.55

    # Non-custom row must be updated.
    assert non_custom_row.input_price_per_1m == 3.0
    assert non_custom_row.output_price_per_1m == 15.0
    assert non_custom_row.cache_read_price_per_1m == Decimal("0.3")
    assert non_custom_row.cache_write_price_per_1m == Decimal("3.75")
    assert non_custom_row.model_pattern == "^claude-sonnet-4-.*$"
    assert non_custom_row.effective_from == date(2026, 6, 14)


@pytest.mark.asyncio
async def test_sync_model_prices_updates_most_recent_duplicate_non_custom(monkeypatch):
    payload = price_loader.ModelPricesPayload.model_validate(
        {
            "schema_version": "1.0.0",
            "updated_at": "2026-06-14T10:00:00Z",
            "prices": [
                {
                    "provider": "openai",
                    "model_id": "gpt-4o-mini",
                    "input_price_per_1m": 0.2,
                    "output_price_per_1m": 0.8,
                }
            ],
        }
    )

    newer_row = SimpleNamespace(
        provider="openai",
        model_id="gpt-4o-mini",
        model_pattern="^gpt-4o-mini$",
        input_price_per_1m=0.14,
        output_price_per_1m=0.55,
        cache_read_price_per_1m=None,
        cache_write_price_per_1m=None,
        effective_from=date(2026, 6, 1),
        is_custom=False,
    )
    older_row = SimpleNamespace(
        provider="openai",
        model_id="gpt-4o-mini",
        model_pattern="^gpt-4o-mini$",
        input_price_per_1m=0.13,
        output_price_per_1m=0.5,
        cache_read_price_per_1m=None,
        cache_write_price_per_1m=None,
        effective_from=date(2026, 5, 1),
        is_custom=False,
    )

    async def _fake_load_existing_prices(_db):
        # Ordered newest first, matching loader contract.
        return [newer_row, older_row]

    monkeypatch.setattr(price_loader, "_load_existing_prices", _fake_load_existing_prices)

    db = _FakeDB()
    synced, skipped = await price_loader.sync_model_prices(db, payload=payload)

    assert synced == 1
    assert skipped == 0
    assert newer_row.input_price_per_1m == Decimal("0.2")
    assert newer_row.output_price_per_1m == Decimal("0.8")
    assert older_row.input_price_per_1m == 0.13
    assert older_row.output_price_per_1m == 0.5


@pytest.mark.asyncio
async def test_sync_model_prices_creates_new_rows(monkeypatch):
    payload = price_loader.ModelPricesPayload.model_validate(
        {
            "schema_version": "1.0.0",
            "updated_at": datetime.now(UTC).isoformat(),
            "prices": [
                {
                    "provider": "google",
                    "model_id": "gemini-2.5-flash",
                    "model_pattern": "^gemini-2.5-flash(-.*)?$",
                    "input_price_per_1m": 0.3,
                    "output_price_per_1m": 2.5,
                }
            ],
        }
    )

    async def _fake_load_existing_prices(_db):
        return []

    monkeypatch.setattr(price_loader, "_load_existing_prices", _fake_load_existing_prices)

    db = _FakeDB()
    synced, skipped = await price_loader.sync_model_prices(db, payload=payload)

    assert synced == 1
    assert skipped == 0
    assert db.committed is True
    assert len(db.added) == 1
