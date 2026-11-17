"""Tests for ``app.llm.router.validate_selectable_chat_model``.

The write-path model gate: every surface that PERSISTS a chat
model reference (agent create/update via REST, the agent-builder tool,
GA / platform default / routing model updates) must only accept models
listed in a configured provider's curated ``available_models`` catalog
(env-seeded under ``MODELS_ENV_PROVIDING_ONLY``, otherwise
admin-managed).

The runtime resolver stays deliberately lenient (prefix inference keeps
newly released models working before a catalog refresh) — but that same
leniency means persisting a stale or hallucinated model id (e.g. a
retired, date-suffixed Anthropic snapshot) routes it to a provider that
404s on every call. Strict validation at write time closes that hole
without touching runtime behaviour.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.llm.router import validate_selectable_chat_model


class _FakeResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return list(self._items)


class _FakeSession:
    def __init__(self, configs):
        self._configs = configs

    async def execute(self, _statement):
        return _FakeResult(self._configs)


def _cfg(provider: str, models: list[str] | None):
    return SimpleNamespace(provider=provider, available_models=models)


@pytest.mark.asyncio
async def test_accepts_model_listed_in_a_provider_catalog():
    db = _FakeSession(
        [
            _cfg("anthropic", ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"]),
            _cfg("openai", ["gpt-image-2"]),
        ]
    )
    assert await validate_selectable_chat_model(db, "claude-sonnet-4-6") is None


@pytest.mark.asyncio
async def test_rejects_image_model_even_when_in_a_catalog():
    """An image / embedding / audio model is NOT a chat model and must be
    rejected as a chat selection even though it IS a catalog member — it backs
    the Image Generation / Embeddings sections, not chat. Persisting one as the
    agent / GA / platform default chat model is the root cause of the
    "my default model is an image model" failure."""
    db = _FakeSession(
        [
            _cfg("anthropic", ["claude-sonnet-4-6"]),
            _cfg("openai", ["gpt-image-2", "text-embedding-3-large"]),
        ]
    )
    for non_chat in ("gpt-image-2", "gpt-image-1", "dall-e-3", "text-embedding-3-large"):
        error = await validate_selectable_chat_model(db, non_chat)
        assert error is not None, f"{non_chat} must be rejected as a chat model"
        assert non_chat in error


@pytest.mark.asyncio
async def test_rejects_model_outside_every_catalog_with_suggestions():
    """A retired / hallucinated model id must be rejected, and the error
    must surface the valid catalog so a human or LLM caller can
    self-correct without a second round-trip."""
    db = _FakeSession(
        [_cfg("anthropic", ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"])]
    )

    error = await validate_selectable_chat_model(db, "claude-sonnet-4-20250514")

    assert error is not None
    assert "claude-sonnet-4-20250514" in error
    assert "claude-sonnet-4-6" in error
    assert "claude-haiku-4-5-20251001" in error


@pytest.mark.asyncio
async def test_skips_validation_when_no_provider_configured():
    """Fresh install / bootstrap import: there is no catalog to validate
    against, so the gate must not block writes (the runtime already fails
    loudly with 'No LLM provider configured' in that state)."""
    assert await validate_selectable_chat_model(_FakeSession([]), "anything") is None


@pytest.mark.asyncio
async def test_skips_validation_when_catalogs_are_empty():
    """Legacy rows can exist with NULL / empty ``available_models`` —
    same no-catalog semantics as a fresh install."""
    db = _FakeSession([_cfg("anthropic", None), _cfg("openai", [])])
    assert await validate_selectable_chat_model(db, "anything") is None


@pytest.mark.asyncio
async def test_error_suggestion_list_is_capped():
    """Installs with many providers/models must not produce a wall-of-text
    error: the suggestion list is capped and elided."""
    many = [f"model-{i:02d}" for i in range(40)]
    db = _FakeSession([_cfg("openai", many)])

    error = await validate_selectable_chat_model(db, "nope")

    assert error is not None
    assert error.endswith("...")
    # Capped at 15 suggestions (see _MODEL_SUGGESTION_LIMIT).
    assert error.count("model-") == 15
