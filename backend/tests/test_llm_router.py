import asyncio
import uuid
from types import SimpleNamespace

import pytest

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


def test_get_llm_for_user_routes_to_model_owner_when_override_mismatches(
    monkeypatch, caplog
):
    configs = [
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="anthropic",
            available_models=["claude-sonnet-4-20250514"],
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="openai",
            available_models=["gpt-5-mini"],
        ),
    ]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs)])

    async def _mock_platform_settings(_db):
        return SimpleNamespace(agent_default_model=None, agent_default_provider_id=None)

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _mock_platform_settings,
    )
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )

    caplog.set_level("WARNING", logger="app.llm.router")

    provider, model = asyncio.run(
        router.get_llm_for_user(
            db,
            user_id=None,
            provider_override="anthropic",
            model_override="gpt-5-mini",
        )
    )

    assert provider.provider_name == "openai"
    assert model == "gpt-5-mini"
    assert "Explicit LLM provider override" in caplog.text


def test_get_llm_for_user_skips_non_chat_model_override_and_falls_through(
    monkeypatch, caplog
):
    """A stored non-chat chat-model reference (e.g. a GA ``llm_model`` left as
    ``gpt-image-1`` by the pre-fix auto-seeding) must be skipped at resolution
    so chat keeps working via the next tier — not routed to ``chat.completions``
    with an image model."""
    configs = [
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="anthropic",
            available_models=["claude-sonnet-4-6"],
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="openai",
            available_models=["gpt-image-1"],
        ),
    ]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs)])

    async def _mock_platform_settings(_db):
        return SimpleNamespace(
            agent_default_model="claude-sonnet-4-6",
            agent_default_provider_id=None,
        )

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _mock_platform_settings,
    )
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )

    caplog.set_level("WARNING", logger="app.llm.router")

    # model_override is an image model (simulating a stored GA llm_model) — it
    # must be skipped and resolution must fall through to the chat-capable
    # platform default.
    provider, model = asyncio.run(
        router.get_llm_for_user(
            db,
            user_id=None,
            model_override="gpt-image-1",
        )
    )

    assert model == "claude-sonnet-4-6"
    assert provider.provider_name == "anthropic"
    assert "not a chat-capable model" in caplog.text


def test_get_llm_for_user_uses_model_prefix_inference_when_not_in_available_models(
    monkeypatch,
):
    configs = [
        SimpleNamespace(id=uuid.uuid4(), provider="anthropic", available_models=[]),
        SimpleNamespace(id=uuid.uuid4(), provider="openai", available_models=[]),
    ]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs)])

    async def _mock_platform_settings(_db):
        return SimpleNamespace(agent_default_model=None, agent_default_provider_id=None)

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _mock_platform_settings,
    )
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )

    provider, model = asyncio.run(
        router.get_llm_for_user(
            db,
            user_id=None,
            provider_override="anthropic",
            model_override="gpt-5-mini",
        )
    )

    assert provider.provider_name == "openai"
    assert model == "gpt-5-mini"


def test_get_llm_for_user_keeps_override_when_model_owner_unknown(monkeypatch):
    configs = [
        SimpleNamespace(id=uuid.uuid4(), provider="anthropic", available_models=[]),
        SimpleNamespace(id=uuid.uuid4(), provider="openai", available_models=[]),
    ]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs)])

    async def _mock_platform_settings(_db):
        return SimpleNamespace(agent_default_model=None, agent_default_provider_id=None)

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _mock_platform_settings,
    )
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )

    provider, model = asyncio.run(
        router.get_llm_for_user(
            db,
            user_id=None,
            provider_override="anthropic",
            model_override="model-without-known-owner",
        )
    )

    assert provider.provider_name == "anthropic"
    assert model == "model-without-known-owner"


def test_get_llm_for_user_prefers_exact_model_owner_over_prefix_inference(monkeypatch):
    configs = [
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="anthropic",
            available_models=["claude-sonnet-4-20250514"],
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="openai",
            available_models=["claude-custom-openai-wrapper"],
        ),
    ]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs)])

    async def _mock_platform_settings(_db):
        return SimpleNamespace(agent_default_model=None, agent_default_provider_id=None)

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _mock_platform_settings,
    )
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )

    provider, model = asyncio.run(
        router.get_llm_for_user(
            db,
            user_id=None,
            provider_override="anthropic",
            model_override="claude-custom-openai-wrapper",
        )
    )

    assert provider.provider_name == "openai"
    assert model == "claude-custom-openai-wrapper"


def test_get_llm_for_user_routes_to_model_owner_when_provider_id_override_mismatches(
    monkeypatch, caplog
):
    """``provider_id_override`` (UUID) mismatch must auto-correct AND
    surface the UUID in the warning. The previous wording reused the
    generic "Resolved LLM provider …" message, which made it impossible
    to tell from a log line whether the routing diverged from an explicit
    UUID override or from an implicit platform default."""
    anthropic_id = uuid.uuid4()
    configs = [
        SimpleNamespace(
            id=anthropic_id,
            provider="anthropic",
            available_models=["claude-sonnet-4-20250514"],
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="openai",
            available_models=["gpt-5-mini"],
        ),
    ]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs)])

    async def _mock_platform_settings(_db):
        return SimpleNamespace(agent_default_model=None, agent_default_provider_id=None)

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _mock_platform_settings,
    )
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )

    caplog.set_level("WARNING", logger="app.llm.router")

    provider, model = asyncio.run(
        router.get_llm_for_user(
            db,
            user_id=None,
            model_override="gpt-5-mini",
            provider_id_override=anthropic_id,
        )
    )

    assert provider.provider_name == "openai"
    assert model == "gpt-5-mini"
    # UUID-specific wording (not the generic "Resolved LLM provider …")
    # and the UUID must appear in the log so operators can grep for it.
    assert "LLM provider override id=" in caplog.text
    assert str(anthropic_id) in caplog.text


def test_get_llm_for_user_implicit_resolution_uses_softer_wording(monkeypatch, caplog):
    """When neither ``provider_override`` nor ``provider_id_override`` is
    set and the platform default falls through to a different provider
    than the model owner, the log must use the softer ``Resolved LLM
    provider …`` wording (no caller picked this provider explicitly)."""
    default_provider_id = uuid.uuid4()
    configs = [
        SimpleNamespace(
            id=default_provider_id,
            provider="openai",
            available_models=[],  # stale: doesn't list claude
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="anthropic",
            available_models=["claude-sonnet-4-20250514"],
        ),
    ]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs)])

    async def _mock_platform_settings(_db):
        return SimpleNamespace(
            agent_default_model=None,
            agent_default_provider_id=default_provider_id,
        )

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _mock_platform_settings,
    )
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )

    caplog.set_level("WARNING", logger="app.llm.router")

    provider, model = asyncio.run(
        router.get_llm_for_user(
            db,
            user_id=None,
            # Caller passes only the model — no name or UUID override.
            model_override="claude-sonnet-4-20250514",
        )
    )

    assert provider.provider_name == "anthropic"
    assert model == "claude-sonnet-4-20250514"
    # Implicit selection → softer wording.
    assert "Resolved LLM provider" in caplog.text
    # And no "Explicit ..." / "override id=" hits so we don't accidentally
    # log a misleading "explicit" message for an implicit selection.
    assert "Explicit LLM provider" not in caplog.text
    assert "override id=" not in caplog.text


def test_resolve_model_owner_config_helper_is_canonical_source():
    """The shared helper must match the behaviour of `find_provider_for_model`
    so the two paths can't drift over time. Exercises both lookup branches:
    `available_models` membership and prefix inference."""
    anthropic_cfg = SimpleNamespace(
        id=uuid.uuid4(),
        provider="anthropic",
        available_models=["claude-sonnet-4-20250514"],
    )
    openai_cfg = SimpleNamespace(
        id=uuid.uuid4(),
        provider="openai",
        available_models=["gpt-5-mini"],
    )
    configs = [anthropic_cfg, openai_cfg]

    # Exact `available_models` membership wins.
    assert router._resolve_model_owner_config(configs, "claude-sonnet-4-20250514") is anthropic_cfg
    assert router._resolve_model_owner_config(configs, "gpt-5-mini") is openai_cfg

    # Stale `available_models` → prefix inference still resolves.
    assert router._resolve_model_owner_config(configs, "gpt-5-future-model") is openai_cfg
    assert router._resolve_model_owner_config(configs, "claude-opus-4-7") is anthropic_cfg

    # Unknown prefix + not in any `available_models` → None.
    assert router._resolve_model_owner_config(configs, "totally-unknown-model") is None

    # Empty inputs are tolerated.
    assert router._resolve_model_owner_config(configs, None) is None
    assert router._resolve_model_owner_config(configs, "") is None
    assert router._resolve_model_owner_config([], "gpt-5-mini") is None


def test_resolve_provider_for_model_uses_shared_owner_resolution(monkeypatch):
    configs = [
        SimpleNamespace(id=uuid.uuid4(), provider="anthropic", available_models=[]),
        SimpleNamespace(id=uuid.uuid4(), provider="openai", available_models=[]),
    ]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs)])
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )

    provider, model = asyncio.run(router.resolve_provider_for_model(db, "gpt-5-future-model"))

    assert provider.provider_name == "openai"
    assert model == "gpt-5-future-model"


def test_resolve_provider_for_model_raises_when_owner_not_configured():
    configs = [SimpleNamespace(id=uuid.uuid4(), provider="anthropic", available_models=[])]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs)])

    with pytest.raises(ValueError, match="Model 'gpt-5-mini' is not available"):
        asyncio.run(router.resolve_provider_for_model(db, "gpt-5-mini"))


def test_get_llm_for_user_falls_back_to_model_owner_when_explicit_override_stale(
    monkeypatch, caplog
):
    """Stale-override fallback: caller passes ``provider_override='deleted-provider'``
    that doesn't resolve to any configured row. Without the fallback,
    `primary_config` would stay None and `get_llm_provider()` would pick
    an arbitrary configured provider — reintroducing the invalid
    provider/model pair this PR is meant to prevent."""
    configs = [
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="anthropic",
            available_models=["claude-sonnet-4-20250514"],
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="openai",
            available_models=["gpt-5-mini"],
        ),
    ]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs)])

    async def _mock_platform_settings(_db):
        return SimpleNamespace(agent_default_model=None, agent_default_provider_id=None)

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _mock_platform_settings,
    )
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )

    caplog.set_level("WARNING", logger="app.llm.router")

    provider, model = asyncio.run(
        router.get_llm_for_user(
            db,
            user_id=None,
            provider_override="deleted-provider",  # not in `configs`
            model_override="gpt-5-mini",
        )
    )

    # Must route to the model owner (openai) instead of an arbitrary
    # configured provider.
    assert provider.provider_name == "openai"
    assert model == "gpt-5-mini"
    assert "did not resolve to a configured provider" in caplog.text


def test_get_llm_for_user_falls_back_to_model_owner_when_provider_id_override_stale(
    monkeypatch, caplog
):
    """Same fallback for `provider_id_override` pointing at a deleted UUID
    (no matching config) and no `provider_override` supplied."""
    configs = [
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="anthropic",
            available_models=["claude-sonnet-4-20250514"],
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="openai",
            available_models=["gpt-5-mini"],
        ),
    ]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs)])

    async def _mock_platform_settings(_db):
        return SimpleNamespace(agent_default_model=None, agent_default_provider_id=None)

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _mock_platform_settings,
    )
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )

    caplog.set_level("WARNING", logger="app.llm.router")

    stale_id = uuid.uuid4()  # not in `configs`
    provider, model = asyncio.run(
        router.get_llm_for_user(
            db,
            user_id=None,
            model_override="claude-sonnet-4-20250514",
            provider_id_override=stale_id,
        )
    )

    # Must route to the model owner (anthropic) instead of an arbitrary
    # configured provider.
    assert provider.provider_name == "anthropic"
    assert model == "claude-sonnet-4-20250514"
    assert "did not resolve to a configured provider" in caplog.text


def test_get_llm_for_user_empty_string_provider_override_uses_implicit_wording(
    monkeypatch, caplog
):
    """Regression: ``provider_override=''`` (common when a UI form sends
    an empty string for "no override") must not be treated as an explicit
    override. Otherwise the mismatch warning would use the
    ``Explicit LLM provider override ''`` wording even though the caller
    didn't actually pick a provider — misleading operators reading the
    logs into thinking a real override was supplied."""
    default_provider_id = uuid.uuid4()
    configs = [
        SimpleNamespace(
            id=default_provider_id,
            provider="openai",
            available_models=[],  # stale: doesn't list claude
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="anthropic",
            available_models=["claude-sonnet-4-20250514"],
        ),
    ]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs)])

    async def _mock_platform_settings(_db):
        return SimpleNamespace(
            agent_default_model=None,
            agent_default_provider_id=default_provider_id,
        )

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _mock_platform_settings,
    )
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )

    caplog.set_level("WARNING", logger="app.llm.router")

    provider, model = asyncio.run(
        router.get_llm_for_user(
            db,
            user_id=None,
            provider_override="",  # empty string ≠ explicit override
            model_override="claude-sonnet-4-20250514",
        )
    )

    # Must still auto-correct to the model owner.
    assert provider.provider_name == "anthropic"
    assert model == "claude-sonnet-4-20250514"
    # And the log must use the *implicit* "Resolved LLM provider" wording,
    # not the "Explicit LLM provider override" wording, because the empty
    # string was normalized to None and routing actually came from the
    # platform default ``agent_default_provider_id``.
    assert "Resolved LLM provider" in caplog.text
    assert "Explicit LLM provider override" not in caplog.text


def test_get_llm_for_user_stale_uuid_with_name_fallback_uses_name_wording(
    monkeypatch, caplog
):
    """Regression: when ``provider_id_override`` is stale (UUID doesn't
    resolve) and ``provider_override`` (name) is supplied as a soft
    fallback, the mismatch warning must use the *name-override* wording —
    NOT the UUID-specific ``LLM provider override id=…`` wording — because
    the actual routing was driven by the name lookup, not the UUID.
    Logging ``override id=<uuid> resolved to '<provider>'`` in that case
    would be a lie (the UUID did not resolve). A separate dedicated
    "stale UUID" warning is emitted at the lookup point so operators
    still see that the UUID reference is dangling."""
    configs = [
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="anthropic",
            available_models=["claude-sonnet-4-20250514"],
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="openai",
            available_models=["gpt-5-mini"],
        ),
    ]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs)])

    async def _mock_platform_settings(_db):
        return SimpleNamespace(agent_default_model=None, agent_default_provider_id=None)

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _mock_platform_settings,
    )
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )

    caplog.set_level("WARNING", logger="app.llm.router")

    stale_id = uuid.uuid4()  # not in configs
    provider, model = asyncio.run(
        router.get_llm_for_user(
            db,
            user_id=None,
            provider_override="anthropic",  # resolves; mismatches gpt-5-mini
            model_override="gpt-5-mini",
            provider_id_override=stale_id,  # stale → soft fallback to name
        )
    )

    # Routing still ends up at the model owner (openai).
    assert provider.provider_name == "openai"
    assert model == "gpt-5-mini"
    # Two warnings expected:
    #   1. Stale UUID warning at the lookup point (separate, dedicated).
    assert "stale UUID" in caplog.text
    assert str(stale_id) in caplog.text
    #   2. Mismatch warning uses the *name-override* wording, not UUID
    #      wording — because the UUID did not drive the routing.
    assert "Explicit LLM provider override 'anthropic'" in caplog.text
    # And critically, the misleading UUID-specific message MUST NOT fire:
    # logging ``override id=<uuid> resolved to '<provider>'`` would falsely
    # imply the UUID resolved to that provider.
    assert "override id=" + repr(str(stale_id)) + " resolved to" not in caplog.text


# ---------------------------------------------------------------------------
# build_provider: curated default-model wiring
#
# Every provider class ships a hardcoded ``default_model`` for ``model=None``
# calls, and hardcoded model ids rot: when the vendor retires the snapshot,
# every model-less call (memory consolidation, fact extraction, prompt
# generation — anything routed via ``get_llm_provider`` + ``llm.chat()``
# without a model) starts 404ing even though the deployment's catalog only
# contains valid models. ``build_provider`` must therefore prefer the config
# row's curated default (env-seeded / admin-set), falling back to the first
# ``available_models`` entry, and only then to the class attribute.
# ---------------------------------------------------------------------------


def _provider_config(**overrides):
    from app.core.security import encrypt_value

    base = dict(
        id=uuid.uuid4(),
        provider="anthropic",
        api_key_enc=encrypt_value("sk-ant-test"),
        default_model=None,
        available_models=None,
        context_windows=None,
        max_output_tokens=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_build_provider_wires_config_default_model():
    from app.llm.anthropic_provider import AnthropicProvider

    provider = router.build_provider(
        _provider_config(
            default_model="claude-sonnet-4-6",
            available_models=["claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
        )
    )

    assert provider.default_model == "claude-sonnet-4-6", (
        "model=None calls must use the curated config default, not the "
        "hardcoded class snapshot (which rots and 404s once retired)"
    )
    # Instance attribute only — the class fallback stays untouched for
    # rows that have no catalog at all.
    assert "default_model" in provider.__dict__
    assert AnthropicProvider.default_model != provider.__dict__[
        "default_model"
    ] or AnthropicProvider.default_model == "claude-sonnet-4-6"


def test_build_provider_falls_back_to_first_available_model():
    provider = router.build_provider(
        _provider_config(
            default_model=None,
            available_models=["claude-haiku-4-5-20251001", "claude-sonnet-4-6"],
        )
    )

    assert provider.default_model == "claude-haiku-4-5-20251001"


def test_build_provider_keeps_class_default_without_any_catalog():
    from app.llm.anthropic_provider import AnthropicProvider

    provider = router.build_provider(
        _provider_config(default_model=None, available_models=None)
    )

    assert provider.default_model == AnthropicProvider.default_model
    assert "default_model" not in provider.__dict__


# ---------------------------------------------------------------------------
# build_provider: chat default must never be an image / embedding model
#
# A provider configured only for image generation / embeddings (e.g. an OpenAI
# row whose ``available_models`` is just ``["gpt-image-1"]`` because it backs
# the Image Generation / Embeddings sections) must NOT have its CHAT default
# set to that non-chat model — otherwise every model-less ``llm.chat()`` call
# and every failover to this provider hits ``chat.completions`` with an image
# model and 500s, surfacing as "my default model is an image model".
# ---------------------------------------------------------------------------


def test_build_provider_skips_image_only_available_models():
    from app.llm.openai_provider import OpenAIProvider

    provider = router.build_provider(
        _provider_config(
            provider="openai",
            default_model=None,
            available_models=["gpt-image-1"],
        )
    )

    # Falls back to the class chat default (gpt-4o), NEVER the image model.
    assert provider.default_model == OpenAIProvider.default_model
    assert provider.default_model != "gpt-image-1"
    assert "default_model" not in provider.__dict__


def test_build_provider_skips_explicit_image_default_model():
    provider = router.build_provider(
        _provider_config(
            provider="openai",
            default_model="gpt-image-1",
            available_models=["gpt-image-1", "gpt-4o"],
        )
    )

    # The explicit (image) default is skipped in favour of the first
    # chat-capable catalog entry.
    assert provider.default_model == "gpt-4o"


def test_build_provider_skips_embedding_models_for_chat_default():
    provider = router.build_provider(
        _provider_config(
            provider="openai",
            default_model=None,
            available_models=["text-embedding-3-large", "gpt-4o-mini"],
        )
    )

    assert provider.default_model == "gpt-4o-mini"


def test_is_chat_capable_model():
    assert router.is_chat_capable_model("claude-sonnet-4-6") is True
    assert router.is_chat_capable_model("gpt-4o") is True
    assert router.is_chat_capable_model("o3-mini") is True
    # Non-chat models
    assert router.is_chat_capable_model("gpt-image-1") is False
    assert router.is_chat_capable_model("gpt-image-2") is False
    assert router.is_chat_capable_model("dall-e-3") is False
    assert router.is_chat_capable_model("imagen-3.0-generate-002") is False
    assert router.is_chat_capable_model("text-embedding-3-large") is False
    assert router.is_chat_capable_model("mistral-embed") is False
    assert router.is_chat_capable_model("whisper-1") is False
    assert router.is_chat_capable_model("gpt-4o-transcribe") is False
    # Markerless non-chat models caught via the authoritative catalog mode:
    # these ids contain none of the substring markers but are embedding / rerank
    # in model_prices_and_context_window.json.
    assert router.is_chat_capable_model("databricks/databricks-bge-large-en") is False
    assert router.is_chat_capable_model("databricks/databricks-gte-large-en") is False
    assert router.is_chat_capable_model("jina-reranker-v2-base-multilingual") is False
    # Catalog basename match (stored without the provider prefix).
    assert router.is_chat_capable_model("databricks-bge-large-en") is False
    # Legacy completion-only models (catalog mode="completion") 404 on
    # /v1/chat/completions ("This is not a chat model ... Did you mean to use
    # v1/completions?") — they must never be picked as a chat default or used
    # as a failover target.
    assert router.is_chat_capable_model("gpt-3.5-turbo-instruct") is False
    assert router.is_chat_capable_model("gpt-3.5-turbo-instruct-0914") is False
    assert router.is_chat_capable_model("babbage-002") is False
    assert router.is_chat_capable_model("davinci-002") is False
    # Exact-key precedence: the bare id stays excluded even though a gateway
    # re-exports the same basename with mode="chat"
    # (vercel_ai_gateway/openai/gpt-3.5-turbo-instruct) — and that gateway
    # alias itself remains chat-capable.
    assert (
        router.is_chat_capable_model("vercel_ai_gateway/openai/gpt-3.5-turbo-instruct")
        is True
    )
    # Gateway-prefixed completion entries are routing artifacts, not model
    # facts: plain "mistral" chats fine on Ollama even though the catalog has
    # ollama/mistral with mode="completion". No basename generalization.
    assert router.is_chat_capable_model("mistral") is True
    # Realtime (websocket audio) models cannot serve chat completions.
    assert (
        router.is_chat_capable_model(
            "gemini-live-2.5-flash-preview-native-audio-09-2025"
        )
        is False
    )
    # Empty / None
    assert router.is_chat_capable_model(None) is False
    assert router.is_chat_capable_model("") is False


def test_build_provider_skips_completion_only_default_model():
    # The incident shape: an OpenAI row whose configured default is a legacy
    # completion-only model. The chat default must skip it so failover to this
    # provider never calls chat.completions with a non-chat model.
    provider = router.build_provider(
        _provider_config(
            provider="openai",
            default_model="gpt-3.5-turbo-instruct",
            available_models=["gpt-3.5-turbo-instruct", "gpt-4o"],
        )
    )

    assert provider.default_model == "gpt-4o"


# ---------------------------------------------------------------------------
# Embedding-only providers (e.g. TEI) must not pollute the chat router
#
# TEI is embedding-only by design: its ``chat()`` raises NotImplementedError
# and its catalog is *expected* to contain only embedding models. The
# "exposes no chat-capable model" warning is a false positive for it, and it
# must never be picked as an implicit chat provider or a chat failover target.
# ---------------------------------------------------------------------------


def test_build_provider_suppresses_chat_warning_for_embedding_only_provider(caplog):
    caplog.set_level("WARNING", logger="app.llm.router")

    provider = router.build_provider(
        _provider_config(
            provider="tei",
            default_model="Qwen/Qwen3-Embedding-0.6B",
            available_models=["Qwen/Qwen3-Embedding-0.6B"],
        )
    )

    # No chat default is wired (all candidates are embeddings) — the class
    # default stays — and crucially NO misleading "exposes no chat-capable
    # model" warning is emitted for an embedding-only provider.
    assert "default_model" not in provider.__dict__
    assert "exposes no chat-capable model" not in caplog.text


def test_build_provider_warns_for_chat_provider_without_chat_model(caplog):
    from app.llm.openai_provider import OpenAIProvider

    caplog.set_level("WARNING", logger="app.llm.router")

    provider = router.build_provider(
        _provider_config(
            provider="openai",
            default_model=None,
            available_models=["text-embedding-3-large"],
        )
    )

    # A genuine chat provider that exposes only embedding models still warns
    # loudly so the misconfiguration is visible.
    assert "exposes no chat-capable model" in caplog.text
    assert provider.default_model == OpenAIProvider.default_model


def test_build_provider_dedupes_catalog_in_warning(caplog):
    caplog.set_level("WARNING", logger="app.llm.router")

    router.build_provider(
        _provider_config(
            provider="openai",
            default_model="text-embedding-3-large",
            available_models=["text-embedding-3-large"],
        )
    )

    # ``default_model`` + the identical ``available_models`` entry must not
    # produce a doubled catalog in the log.
    assert "exposes no chat-capable model" in caplog.text
    assert "['text-embedding-3-large']" in caplog.text


def test_validate_chat_provider_configs_flags_only_misconfigured_chat_providers(caplog):
    caplog.set_level("WARNING", logger="app.llm.router")

    configs = [
        SimpleNamespace(
            provider="anthropic",
            default_model=None,
            available_models=["claude-sonnet-4-6"],
        ),
        SimpleNamespace(
            provider="openai",
            default_model=None,
            available_models=["text-embedding-3-large"],
        ),
        SimpleNamespace(
            provider="tei",
            default_model="Qwen/Qwen3-Embedding-0.6B",
            available_models=["Qwen/Qwen3-Embedding-0.6B"],
        ),
        SimpleNamespace(
            provider="gemini",
            default_model=None,
            available_models=[],
        ),
    ]

    findings = router.validate_chat_provider_configs(configs)

    # Only the misconfigured chat provider (openai, embedding-only catalog) is
    # flagged. The embedding-only provider (tei), the chat-capable provider
    # (anthropic), and the empty-catalog provider (gemini) are all skipped.
    assert len(findings) == 1
    assert "openai" in findings[0]
    assert "tei" not in caplog.text


def test_get_llm_provider_skips_embedding_only_provider_for_implicit_default(
    monkeypatch,
):
    configs = [
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="tei",
            available_models=["Qwen/Qwen3-Embedding-0.6B"],
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="anthropic",
            available_models=["claude-sonnet-4-6"],
        ),
    ]
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )

    # tei is first in the list, but the implicit "first found" default must
    # skip it and pick the chat-capable provider instead.
    provider = asyncio.run(
        router.get_llm_provider("system", db=None, _all_configs=configs)
    )
    assert provider.provider_name == "anthropic"


def test_get_llm_provider_honours_explicit_embedding_only_override(monkeypatch):
    configs = [
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="anthropic",
            available_models=["claude-sonnet-4-6"],
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="tei",
            available_models=["Qwen/Qwen3-Embedding-0.6B"],
        ),
    ]
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )

    # An explicit override naming the embedding-only provider is still honoured
    # (used for embedding flows / deliberate selection).
    provider = asyncio.run(
        router.get_llm_provider(
            "system", db=None, provider_override="tei", _all_configs=configs
        )
    )
    assert provider.provider_name == "tei"


def test_get_llm_provider_falls_back_to_embedding_only_when_no_chat_provider(
    monkeypatch,
):
    configs = [
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="tei",
            available_models=["Qwen/Qwen3-Embedding-0.6B"],
        ),
    ]
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )

    # On an embedding-only install there is no chat-capable provider — prior
    # behaviour is preserved (return the only provider) rather than failing
    # differently; the chat call then raises a clear provider error.
    provider = asyncio.run(
        router.get_llm_provider("system", db=None, _all_configs=configs)
    )
    assert provider.provider_name == "tei"


def test_get_llm_for_user_excludes_embedding_only_provider_from_failover(monkeypatch):
    configs = [
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="anthropic",
            available_models=["claude-sonnet-4-6"],
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="tei",
            available_models=["Qwen/Qwen3-Embedding-0.6B"],
        ),
    ]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs)])

    async def _mock_platform_settings(_db):
        return SimpleNamespace(
            agent_default_model=None,
            agent_default_provider_id=None,
            fallback_model=None,
        )

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _mock_platform_settings,
    )
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )

    provider, model = asyncio.run(
        router.get_llm_for_user(db, user_id=None, model_override="claude-sonnet-4-6")
    )

    assert model == "claude-sonnet-4-6"
    # tei is the only other provider, but it's embedding-only and excluded from
    # the failover chain — so no failover wrapper is created at all.
    assert not isinstance(provider, router.FailoverLLMProvider)
    assert provider.provider_name == "anthropic"


# ---------------------------------------------------------------------------
# validate_fallback_model — startup diagnostic for misconfigured fallback model
# (non-chat fallback 404s on /v1/chat/completions every time)
# ---------------------------------------------------------------------------


def test_validate_fallback_model_warns_for_completion_only_model(caplog):
    """A legacy completion-only fallback model must be flagged at startup."""
    caplog.set_level("WARNING", logger="app.llm.router")

    configs = [
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="openai",
            available_models=["gpt-4o", "gpt-3.5-turbo-instruct"],
            default_model=None,
        ),
    ]

    findings = router.validate_fallback_model("gpt-3.5-turbo-instruct", configs)

    assert len(findings) == 1
    assert "gpt-3.5-turbo-instruct" in findings[0]
    assert "not chat-capable" in findings[0]
    assert "not chat-capable" in caplog.text


def test_validate_fallback_model_warns_for_embedding_fallback(caplog):
    """An embedding model as fallback must be flagged."""
    caplog.set_level("WARNING", logger="app.llm.router")

    configs = [
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="openai",
            available_models=["text-embedding-3-large"],
            default_model=None,
        ),
    ]

    findings = router.validate_fallback_model("text-embedding-3-large", configs)

    assert len(findings) == 1
    assert "text-embedding-3-large" in findings[0]
    assert "not chat-capable" in findings[0]


def test_validate_fallback_model_warns_for_unowned_model(caplog):
    """A chat-capable fallback model not owned by any provider must also warn."""
    caplog.set_level("WARNING", logger="app.llm.router")

    configs = [
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="anthropic",
            available_models=["claude-sonnet-4-6"],
            default_model=None,
        ),
    ]

    findings = router.validate_fallback_model("gpt-4o", configs)

    assert len(findings) == 1
    assert "gpt-4o" in findings[0]
    assert "not owned" in findings[0]


def test_validate_fallback_model_no_findings_for_valid_model(caplog):
    """A chat-capable fallback owned by a provider must produce no findings."""
    caplog.set_level("WARNING", logger="app.llm.router")

    configs = [
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="openai",
            available_models=["gpt-4o"],
            default_model=None,
        ),
    ]

    findings = router.validate_fallback_model("gpt-4o", configs)

    assert findings == []
    assert "gpt-4o" not in caplog.text


def test_validate_fallback_model_no_findings_when_none(caplog):
    """No fallback model configured must produce no findings."""
    caplog.set_level("WARNING", logger="app.llm.router")

    findings = router.validate_fallback_model(None, [])

    assert findings == []


# ---------------------------------------------------------------------------
# get_llm_for_user — runtime guard: non-chat fallback model is skipped
# ---------------------------------------------------------------------------


def test_get_llm_for_user_skips_non_chat_fallback_model(monkeypatch, caplog):
    """A non-chat DEFAULT_FALLBACK_MODEL must be skipped at runtime with a warning.

    The incident shape: Anthropic is overloaded, failover chain
    reaches the OpenAI provider, but the configured fallback model is a legacy
    completion-only model (e.g. ``gpt-3.5-turbo-instruct``) that 404s on
    /v1/chat/completions every time.  The runtime guard in ``get_llm_for_user``
    must detect this and not wire the non-chat model as the explicit fallback
    target."""
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
            available_models=["gpt-4o", "gpt-3.5-turbo-instruct"],
            default_model=None,
        ),
    ]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs)])

    async def _mock_platform_settings(_db):
        return SimpleNamespace(
            agent_default_model=None,
            agent_default_provider_id=None,
            fallback_model="gpt-3.5-turbo-instruct",
        )

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _mock_platform_settings,
    )
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )
    # Isolate the process-wide warning dedupe so this test never depends on
    # whether another test already warned about the same model value.
    monkeypatch.setattr(router, "_warned_fallback_model", set())

    caplog.set_level("WARNING", logger="app.llm.router")

    provider, _model = asyncio.run(
        router.get_llm_for_user(
            db,
            user_id=None,
            model_override="claude-sonnet-4-6",
        )
    )

    # The warning must surface the non-chat fallback model.
    assert "gpt-3.5-turbo-instruct" in caplog.text
    assert "not chat-capable" in caplog.text

    # The second (chat-capable) OpenAI provider keeps the generic per-provider
    # chain alive, so the failover wrapper MUST still be created — only the
    # misconfigured explicit fallback model is dropped from it.
    assert isinstance(provider, router.FailoverLLMProvider)
    assert provider._fallback_model is None


def test_get_llm_for_user_dedupes_non_chat_fallback_warning(monkeypatch, caplog):
    """The hot-path fallback warning must fire once per process per value.

    ``get_llm_for_user`` runs on every chat turn and assignment run — a single
    misconfigured ``fallback_model`` would flood the logs with an identical
    WARNING until fixed. The dedupe warns once, stays silent on subsequent
    calls, and is re-armed by ``invalidate_provider_cache`` (which every
    admin LLM-settings change triggers)."""
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
            available_models=["gpt-4o", "gpt-3.5-turbo-instruct"],
            default_model=None,
        ),
    ]

    async def _mock_platform_settings(_db):
        return SimpleNamespace(
            agent_default_model=None,
            agent_default_provider_id=None,
            fallback_model="gpt-3.5-turbo-instruct",
        )

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _mock_platform_settings,
    )
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )
    monkeypatch.setattr(router, "_warned_fallback_model", set())

    caplog.set_level("WARNING", logger="app.llm.router")

    def _resolve_once():
        db = _MockAsyncSession([_MockSQLAlchemyResult(configs)])
        return asyncio.run(
            router.get_llm_for_user(
                db, user_id=None, model_override="claude-sonnet-4-6"
            )
        )

    _resolve_once()
    assert caplog.text.count("not chat-capable") == 1

    # Second resolution with the same misconfigured value: no new warning.
    _resolve_once()
    assert caplog.text.count("not chat-capable") == 1

    # An admin LLM-settings change invalidates the provider cache, which must
    # re-arm the dedupe so the (possibly still broken) config is re-reported.
    router.invalidate_provider_cache()
    _resolve_once()
    assert caplog.text.count("not chat-capable") == 2


def test_get_llm_for_user_keeps_chat_fallback_model(monkeypatch):
    """A chat-capable DEFAULT_FALLBACK_MODEL must still be wired into failover."""
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
            available_models=["gpt-4o"],
            default_model=None,
        ),
    ]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs)])

    async def _mock_platform_settings(_db):
        return SimpleNamespace(
            agent_default_model=None,
            agent_default_provider_id=None,
            fallback_model="gpt-4o",
        )

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _mock_platform_settings,
    )
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )

    provider, _model = asyncio.run(
        router.get_llm_for_user(
            db,
            user_id=None,
            model_override="claude-sonnet-4-6",
        )
    )

    # A valid chat fallback model must be wired — a FailoverLLMProvider is created
    # with the explicit fallback model set.
    assert isinstance(provider, router.FailoverLLMProvider)
    assert provider._fallback_model == "gpt-4o"


def test_get_llm_for_user_reference_wins_over_the_recency_guess(monkeypatch, caplog):
    """A gateway fronting Anthropic beside a direct Anthropic key both
    advertise ``claude-sonnet-4-6``. ``model_owner_config`` is the newest-row
    guess, so the mismatch check — which compares provider *names* — used to
    auto-correct the admin's explicit gateway reference away to the vendor row.
    A reference that advertises the model is not a mismatch.
    """
    from datetime import UTC, datetime

    gateway_id = uuid.uuid4()
    configs = [
        SimpleNamespace(
            id=gateway_id,
            provider="litellm",
            available_models=["claude-sonnet-4-6"],
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="anthropic",
            available_models=["claude-sonnet-4-6"],
            created_at=datetime(2026, 6, 1, tzinfo=UTC),  # newest → recency winner
        ),
    ]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs)])

    async def _mock_platform_settings(_db):
        return SimpleNamespace(agent_default_model=None, agent_default_provider_id=None)

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _mock_platform_settings,
    )
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )
    caplog.set_level("WARNING", logger="app.llm.router")

    provider, model = asyncio.run(
        router.get_llm_for_user(
            db,
            user_id=None,
            model_override="claude-sonnet-4-6",
            provider_id_override=gateway_id,
        )
    )

    assert provider.provider_name == "litellm"
    assert model == "claude-sonnet-4-6"
    # Nothing diverged, so there is nothing to warn about.
    assert "Auto-correcting" not in caplog.text


def test_get_llm_for_user_reference_still_auto_corrects_when_it_cannot_serve(
    monkeypatch, caplog
):
    """The safety net stays: a reference to a config that does NOT advertise the
    model would 404 on the provider, so the model owner still wins there."""
    anthropic_id = uuid.uuid4()
    configs = [
        SimpleNamespace(
            id=anthropic_id,
            provider="anthropic",
            available_models=["claude-sonnet-4-6"],
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="openai",
            available_models=["gpt-5-mini"],
        ),
    ]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs)])

    async def _mock_platform_settings(_db):
        return SimpleNamespace(agent_default_model=None, agent_default_provider_id=None)

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _mock_platform_settings,
    )
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )
    caplog.set_level("WARNING", logger="app.llm.router")

    provider, _model = asyncio.run(
        router.get_llm_for_user(
            db,
            user_id=None,
            model_override="gpt-5-mini",
            provider_id_override=anthropic_id,
        )
    )

    assert provider.provider_name == "openai"
    assert "Auto-correcting to the model owner provider" in caplog.text


def test_get_llm_for_user_platform_default_reference_wins_over_recency(monkeypatch):
    """Same precedence for the platform default's stored reference, which is a
    config-id channel too — not only the per-call override."""
    from datetime import UTC, datetime

    gateway_id = uuid.uuid4()
    configs = [
        SimpleNamespace(
            id=gateway_id,
            provider="litellm",
            available_models=["claude-sonnet-4-6"],
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="anthropic",
            available_models=["claude-sonnet-4-6"],
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
        ),
    ]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs)])

    async def _mock_platform_settings(_db):
        return SimpleNamespace(
            agent_default_model="claude-sonnet-4-6",
            agent_default_provider_id=gateway_id,
        )

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _mock_platform_settings,
    )
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )

    provider, model = asyncio.run(router.get_llm_for_user(db, user_id=None))
    assert provider.provider_name == "litellm"
    assert model == "claude-sonnet-4-6"


def test_platform_default_reference_does_not_claim_someone_elses_model(monkeypatch):
    """A reference speaks only for the model it was saved with. The platform
    default's reference must not move a user-preferred or agent-chosen model onto
    the platform default's credentials — that would change whose key and bill
    serve the turn."""
    from datetime import UTC, datetime

    gateway_id = uuid.uuid4()
    gateway = SimpleNamespace(
        id=gateway_id,
        provider="litellm",
        # The gateway's catalog also lists the model the user asked for.
        available_models=["claude-sonnet-4-6", "gpt-4o"],
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    openai_cfg = SimpleNamespace(
        id=uuid.uuid4(),
        provider="openai",
        available_models=["gpt-4o"],
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    db = _MockAsyncSession([_MockSQLAlchemyResult([gateway, openai_cfg])])

    async def _mock_platform_settings(_db):
        return SimpleNamespace(
            # The reference belongs to THIS model, not to gpt-4o.
            agent_default_model="claude-sonnet-4-6",
            agent_default_provider_id=gateway_id,
        )

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _mock_platform_settings,
    )
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )

    provider, model = asyncio.run(
        router.get_llm_for_user(db, user_id=None, model_override="gpt-4o")
    )
    assert provider.provider_name == "openai"
    assert model == "gpt-4o"


def test_general_assistant_model_reference_is_honoured_by_the_router(monkeypatch):
    """The GA's own model is the router's last tier. Its stored reference must
    apply there too, or the plain-user-chat path serves the GA's model from
    another provider config."""
    from datetime import UTC, datetime

    gateway_id = uuid.uuid4()

    class _GAResult:
        def scalar_one_or_none(self):
            return SimpleNamespace(
                llm_model="claude-sonnet-4-6", llm_provider_id=gateway_id
            )

    configs = [
        SimpleNamespace(
            id=gateway_id,
            provider="litellm",
            available_models=["claude-sonnet-4-6"],
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="anthropic",
            available_models=["claude-sonnet-4-6"],
            created_at=datetime(2026, 6, 1, tzinfo=UTC),  # recency winner
        ),
    ]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs), _GAResult()])

    async def _mock_platform_settings(_db):
        return SimpleNamespace(agent_default_model=None, agent_default_provider_id=None)

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _mock_platform_settings,
    )
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )

    provider, model = asyncio.run(router.get_llm_for_user(db, user_id=None))
    assert provider.provider_name == "litellm"
    assert model == "claude-sonnet-4-6"


def test_general_assistant_stale_reference_falls_back_to_its_provider_name(
    monkeypatch, caplog
):
    """FR-007 on the GA tier: a dangling reference must fall back to the stored
    provider name and log the id, not drop silently to the model-owner guess.
    """
    from datetime import UTC, datetime

    stale_id = uuid.uuid4()

    class _GAResult:
        def scalar_one_or_none(self):
            return SimpleNamespace(
                llm_model="claude-sonnet-4-6",
                llm_provider_id=stale_id,  # points at a deleted config
                llm_provider="litellm",
            )

    # Both configs are the stored provider name, so the mismatch auto-correct
    # (pre-existing, and tested separately) cannot fire and obscure which path
    # resolved the call.
    configs = [
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="litellm",
            available_models=["claude-sonnet-4-6"],
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="litellm",
            available_models=["claude-sonnet-4-6"],
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
        ),
    ]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs), _GAResult()])

    async def _mock_platform_settings(_db):
        return SimpleNamespace(agent_default_model=None, agent_default_provider_id=None)

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _mock_platform_settings,
    )
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )
    caplog.set_level("WARNING", logger="app.llm.router")

    provider, model = asyncio.run(router.get_llm_for_user(db, user_id=None))

    # What this pins: the tier consulted ``llm_provider`` instead of dropping
    # straight to the model-owner guess, and the dangling id is on the record.
    assert provider.provider_name == "litellm"
    assert model == "claude-sonnet-4-6"
    assert "stale UUID" in caplog.text
    assert str(stale_id) in caplog.text
    assert "falling back to its stored provider name 'litellm'" in caplog.text


def test_general_assistant_reference_outranks_an_unscoped_platform_default(
    monkeypatch,
):
    """A reference speaks only for the model it was saved with.

    ``agent_default_provider_id`` can be set while ``agent_default_model`` is
    empty — the Settings save retains the provider name when the admin picks
    "None", and the name/id alignment loop re-derives the id from it. The GA
    tier then supplies the model, so the GA's reference is the scoped one. While
    this branch sat *after* the platform-default branch, the GA's model was
    served from the platform default's config — the wrong key and bill, which is
    the substitution the router must avoid.
    """
    from datetime import UTC, datetime

    ga_config_id = uuid.uuid4()
    other_config_id = uuid.uuid4()

    class _GAResult:
        def scalar_one_or_none(self):
            return SimpleNamespace(
                llm_model="claude-sonnet-4-6",
                llm_provider_id=ga_config_id,
                llm_provider="litellm",
            )

    # Both are ``litellm``, so the mismatch auto-correct cannot mask the choice.
    configs = [
        SimpleNamespace(
            id=ga_config_id,
            provider="litellm",
            available_models=["claude-sonnet-4-6"],
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        SimpleNamespace(
            id=other_config_id,
            provider="litellm",
            available_models=["claude-sonnet-4-6"],
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
        ),
    ]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs), _GAResult()])

    async def _mock_platform_settings(_db):
        return SimpleNamespace(
            agent_default_model=None,  # no platform default model…
            agent_default_provider_id=other_config_id,  # …but a stale id
            agent_default_provider="litellm",
        )

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _mock_platform_settings,
    )
    built: list = []

    def _capture(cfg):
        built.append(cfg.id)
        return _mock_provider_instance(cfg.provider)

    monkeypatch.setattr(router, "_get_cached_provider", _capture)

    provider, model = asyncio.run(router.get_llm_for_user(db, user_id=None))
    assert model == "claude-sonnet-4-6"
    assert provider.provider_name == "litellm"
    assert built == [ga_config_id]  # not other_config_id


def test_get_llm_for_user_honours_a_prefix_inferred_reference(monkeypatch, caplog):
    """One rule for "can this config serve the model".

    The write path and ``resolve_configured_fallback`` accept a reference whose
    config does not list the model, resolved by name prefix alone — that is how
    an operator pins a resilient fallback without exposing it in the picker. The
    router required catalog membership, so it auto-corrected the very setup the
    other helper documents as supported.
    """
    from datetime import UTC, datetime

    anthropic_id = uuid.uuid4()
    configs = [
        SimpleNamespace(
            id=anthropic_id,
            provider="anthropic",
            # Does not advertise the model; ``claude-`` infers to anthropic.
            available_models=["claude-sonnet-4-6"],
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="litellm",
            available_models=["claude-opus-4-8"],  # explicit → the owner guess
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
        ),
    ]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs)])

    async def _mock_platform_settings(_db):
        return SimpleNamespace(agent_default_model=None, agent_default_provider_id=None)

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _mock_platform_settings,
    )
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )
    caplog.set_level("WARNING", logger="app.llm.router")

    provider, model = asyncio.run(
        router.get_llm_for_user(
            db,
            user_id=None,
            model_override="claude-opus-4-8",
            provider_id_override=anthropic_id,
        )
    )

    assert provider.provider_name == "anthropic"
    assert model == "claude-opus-4-8"
    assert "Auto-correcting" not in caplog.text


def test_general_assistant_dangling_reference_is_logged_without_a_name(
    monkeypatch, caplog
):
    """The endpoint writes ``llm_provider`` and ``llm_provider_id`` independently,
    so a dangling id with no stored name is reachable — and it is the case with
    the least evidence, because routing silently drops to the model-owner guess.
    The warning used to be conditional on the name being present."""
    from datetime import UTC, datetime

    stale_id = uuid.uuid4()

    class _GAResult:
        def scalar_one_or_none(self):
            return SimpleNamespace(
                llm_model="claude-sonnet-4-6",
                llm_provider_id=stale_id,
                llm_provider=None,  # never stored, or cleared since
            )

    configs = [
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="anthropic",
            available_models=["claude-sonnet-4-6"],
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    ]
    db = _MockAsyncSession([_MockSQLAlchemyResult(configs), _GAResult()])

    async def _mock_platform_settings(_db):
        return SimpleNamespace(agent_default_model=None, agent_default_provider_id=None)

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _mock_platform_settings,
    )
    monkeypatch.setattr(
        router,
        "_get_cached_provider",
        lambda cfg: _mock_provider_instance(cfg.provider),
    )
    caplog.set_level("WARNING", logger="app.llm.router")

    provider, _model = asyncio.run(router.get_llm_for_user(db, user_id=None))

    assert provider.provider_name == "anthropic"  # the model-owner guess
    assert "stale UUID" in caplog.text
    assert str(stale_id) in caplog.text
    assert "falling back to the model owner" in caplog.text
