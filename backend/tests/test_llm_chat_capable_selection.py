"""Implicit chat-provider selection must avoid image/embedding-only providers,
and the "no chat-capable model" warning must not spam the logs.

An OpenAI provider configured only for Image Generation / Embeddings
(``available_models=['gpt-image-2']``) must never be picked for a chat context
when a chat-capable provider exists, and ``build_provider`` must warn about it
at most once per config per process instead of on every cache rebuild.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from app.llm import router


class _MockSQLAlchemyResult:
    def __init__(self, configs):
        self._configs = configs

    def scalars(self):
        return self

    def all(self):
        return self._configs


class _MockAsyncSession:
    def __init__(self, execute_results):
        self._execute_results = list(execute_results)

    async def execute(self, _statement):
        if not self._execute_results:
            raise AssertionError("Unexpected extra execute() call")
        return self._execute_results.pop(0)


def _mock_provider_instance(provider_name: str):
    return SimpleNamespace(
        provider_name=provider_name,
        max_tools=0,
        context_window=0,
        context_windows={},
        max_output_tokens=0,
        max_output_tokens_overrides={},
    )


class _FakeProvider:
    def __init__(self, api_key=None):
        self.default_model = "gpt-4o"


# ── _config_has_chat_capable_model ───────────────────────────────────────────


def test_config_chat_capability_helper():
    f = router._config_has_chat_capable_model
    # Curated catalog with a chat model.
    assert f(SimpleNamespace(available_models=["gpt-4o"], default_model=None)) is True
    # Curated catalog with ONLY an image model.
    assert (
        f(SimpleNamespace(available_models=["gpt-image-2"], default_model=None))
        is False
    )
    # Curated catalog with ONLY an embedding model.
    assert (
        f(
            SimpleNamespace(
                available_models=["text-embedding-3-large"], default_model=None
            )
        )
        is False
    )
    # No curated catalog at all → provider uses its built-in chat default.
    assert f(SimpleNamespace(available_models=[], default_model=None)) is True
    # An image-only catalog but a chat-capable ``default_model`` is still chat.
    assert (
        f(SimpleNamespace(available_models=["gpt-image-2"], default_model="claude-3"))
        is True
    )


# ── implicit "first found" selection ─────────────────────────────────────────


def test_get_llm_provider_prefers_chat_capable_over_image_only(monkeypatch):
    image_only = SimpleNamespace(
        id=uuid.uuid4(), provider="openai", available_models=["gpt-image-2"]
    )
    chat = SimpleNamespace(
        id=uuid.uuid4(), provider="anthropic", available_models=["claude-sonnet-4-6"]
    )
    monkeypatch.setattr(router, "_get_cached_provider", lambda c: c)

    # image_only is first in the list, but selection must prefer the chat
    # provider so chat never routes to gpt-4o on an image-only OpenAI row.
    selected = asyncio.run(
        router.get_llm_provider("u", None, _all_configs=[image_only, chat])
    )
    assert selected is chat


def test_get_llm_provider_falls_back_to_only_provider_when_no_chat_capable(monkeypatch):
    image_only = SimpleNamespace(
        id=uuid.uuid4(), provider="openai", available_models=["gpt-image-2"]
    )
    monkeypatch.setattr(router, "_get_cached_provider", lambda c: c)

    # Embedding/image-only install: still return the only provider (the chat
    # call then fails with a clear provider error rather than crashing here).
    selected = asyncio.run(
        router.get_llm_provider("u", None, _all_configs=[image_only])
    )
    assert selected is image_only


def _media_only(provider: str = "gemini"):
    """A provider configured for its video and music models: a key, no model list."""
    return SimpleNamespace(
        id=uuid.uuid4(), provider=provider, available_models=None, default_model=None, api_key_enc="enc"
    )


def test_a_provider_listing_no_model_takes_no_chat_while_another_lists_chat_models(monkeypatch):
    media = _media_only()
    chat = SimpleNamespace(
        id=uuid.uuid4(), provider="anthropic", available_models=["claude-sonnet-5-5"], default_model=None
    )
    monkeypatch.setattr(router, "_get_cached_provider", lambda c: c)

    # Listed first in Settings, the media-only provider still never chats on
    # its built-in chat model: nobody offered it.
    assert router.implicit_chat_configs([media, chat]) == [chat]
    assert asyncio.run(router.get_llm_provider("u", None, _all_configs=[media, chat])) is chat


def test_an_install_configured_with_keys_alone_chats_on_the_built_in_model(monkeypatch):
    first, second = _media_only("openai"), _media_only("gemini")
    monkeypatch.setattr(router, "_get_cached_provider", lambda c: c)

    assert router.implicit_chat_configs([first, second]) == [first, second]
    assert asyncio.run(router.get_llm_provider("u", None, _all_configs=[first, second])) is first


# ── failover chain exclusion ─────────────────────────────────────────────────


def _resolve_with_default(monkeypatch, configs, default_model):
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs)])

    async def _ps(_db):
        return SimpleNamespace(
            agent_default_model=default_model,
            agent_default_provider_id=None,
            fallback_model=None,
        )

    monkeypatch.setattr("app.api.v1.platform_settings_helpers.get_platform_settings", _ps)
    monkeypatch.setattr(router, "_get_cached_provider", lambda cfg: _mock_provider_instance(cfg.provider))
    return asyncio.run(router.get_llm_for_user(db, user_id=None))


def test_failover_skips_a_provider_listing_no_model_while_another_lists_chat_models(monkeypatch):
    chat = SimpleNamespace(
        id=uuid.uuid4(), provider="anthropic", available_models=["claude-sonnet-5-5"], default_model=None
    )

    provider, model = _resolve_with_default(monkeypatch, [chat, _media_only()], "claude-sonnet-5-5")

    assert model == "claude-sonnet-5-5"
    # A wrapper may still be made for the model's own same-provider fallback;
    # the media-only provider is never in its chain.
    assert getattr(provider, "_fallback_configs", []) == []


def test_failover_between_providers_configured_with_keys_alone_is_kept(monkeypatch):
    primary, backup = _media_only("openai"), _media_only("gemini")

    provider, _model = _resolve_with_default(monkeypatch, [primary, backup], "gpt-5.1")

    assert isinstance(provider, router.FailoverLLMProvider)
    assert [c.id for c in provider._fallback_configs] == [backup.id]


def test_failover_chain_excludes_image_only_provider(monkeypatch):
    configs = [
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="anthropic",
            available_models=["claude-sonnet-4-6"],
            default_model=None,
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="openai",
            available_models=["gpt-image-2"],
            default_model=None,
        ),
    ]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs)])

    async def _ps(_db):
        return SimpleNamespace(
            agent_default_model="claude-sonnet-4-6",
            agent_default_provider_id=None,
            fallback_model=None,
        )

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings", _ps
    )
    monkeypatch.setattr(
        router, "_get_cached_provider", lambda cfg: _mock_provider_instance(cfg.provider)
    )

    provider, model = asyncio.run(router.get_llm_for_user(db, user_id=None))

    assert model == "claude-sonnet-4-6"
    # The image-only OpenAI provider is the only other config; excluding it from
    # the failover chain means NO FailoverLLMProvider wrapper is created (it
    # would otherwise 404 on gpt-4o, burning a slot + the per-attempt timeout).
    assert not isinstance(provider, router.FailoverLLMProvider)
    assert provider.provider_name == "anthropic"


# ── warning de-duplication ───────────────────────────────────────────────────


def test_build_provider_warning_deduped_and_rearmed(monkeypatch, caplog):
    router._warned_no_chat_model.clear()
    monkeypatch.setattr(router, "decrypt_value", lambda _v: "key")
    monkeypatch.setitem(router.PROVIDER_MAP, "openai", _FakeProvider)

    cfg = SimpleNamespace(
        id=uuid.uuid4(),
        provider="openai",
        api_key_enc="enc",
        default_model=None,
        available_models=["gpt-image-2"],
        context_windows=None,
        max_output_tokens=None,
    )

    caplog.set_level("WARNING", logger="app.llm.router")

    router.build_provider(cfg)
    router.build_provider(cfg)
    router.build_provider(cfg)
    # Same config rebuilt three times → the warning fires exactly once.
    assert caplog.text.count("exposes no chat-capable model") == 1

    # Invalidating the provider cache (admin changed LLM settings) re-arms the
    # warning so a config the admin just touched is re-evaluated.
    router.invalidate_provider_cache()
    router.build_provider(cfg)
    assert caplog.text.count("exposes no chat-capable model") == 2

    router._warned_no_chat_model.clear()
