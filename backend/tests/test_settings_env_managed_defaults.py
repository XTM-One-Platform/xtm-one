"""Tests for env-managed default-model locking.

When LLM default models are seeded from ``DEFAULT_*`` environment variables
they are re-applied to ``PlatformSettings`` on every startup (env always
wins), so the Settings → AI and models → Models controls must be read-only — exactly like
the env-seeded LLM *provider* lock.  This applies uniformly to the agent
default, routing, reasoning, embedding, and image defaults: env seeds AND
locks every one of them.  These tests pin:

1. ``Settings.default_reasoning_effort`` validation/normalisation.
2. ``_env_managed_ga_fields`` returning the right locked field names from the
   live env settings.
3. ``update_general_assistant_config`` rejecting a mutation of a locked field
   with HTTP 403.
"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api.v1.schemas.settings import GeneralAssistantConfigRequest
from app.api.v1.settings import (
    _env_managed_ga_fields,
    update_general_assistant_config,
)
from app.core import config
from app.core.config import settings as app_settings

# ---------------------------------------------------------------------------
# 1. DEFAULT_REASONING_EFFORT validation
# ---------------------------------------------------------------------------


def test_default_reasoning_effort_normalizes_case_and_whitespace():
    assert config.Settings(default_reasoning_effort="  HIGH ").default_reasoning_effort == "high"
    assert config.Settings(default_reasoning_effort="Max").default_reasoning_effort == "max"


def test_default_reasoning_effort_empty_is_allowed():
    assert config.Settings(default_reasoning_effort="").default_reasoning_effort == ""


def test_default_reasoning_effort_rejects_unknown_level():
    with pytest.raises(ValidationError) as exc:
        config.Settings(default_reasoning_effort="hihg")
    assert "low|medium|high|max" in str(exc.value)


# ---------------------------------------------------------------------------
# 1b. DEFAULT_EMBEDDING_DIMENSIONS validation
# ---------------------------------------------------------------------------


def test_default_embedding_dimensions_accepts_positive_and_none():
    assert config.Settings(default_embedding_dimensions=3072).default_embedding_dimensions == 3072
    assert config.Settings(default_embedding_dimensions=None).default_embedding_dimensions is None


@pytest.mark.parametrize("bad", [0, -1, -3072])
def test_default_embedding_dimensions_rejects_non_positive(bad):
    """``DEFAULT_EMBEDDING_DIMENSIONS`` is now seeded unconditionally (env wins),
    so a non-positive value must fail fast at startup rather than persisting a
    corrupt vector size into ``PlatformSettings``.
    """
    with pytest.raises(ValidationError) as exc:
        config.Settings(default_embedding_dimensions=bad)
    assert "DEFAULT_EMBEDDING_DIMENSIONS must be a positive integer" in str(exc.value)


# ---------------------------------------------------------------------------
# 1c. APP_MAX_CONCURRENCY validation
# ---------------------------------------------------------------------------


def test_app_max_concurrency_accepts_zero_and_positive():
    # 0 is the documented "disable the in-app limiter" value.
    assert config.Settings(app_max_concurrency=0).app_max_concurrency == 0
    assert config.Settings(app_max_concurrency=500).app_max_concurrency == 500


@pytest.mark.parametrize("bad", [-1, -200])
def test_app_max_concurrency_rejects_negative(bad):
    """A negative ``APP_MAX_CONCURRENCY`` would silently disable overload
    shedding (the middleware treats ``<= 0`` as off), so a typo must fail fast
    at startup rather than quietly removing the health-exempt 429 backpressure.
    """
    with pytest.raises(ValidationError) as exc:
        config.Settings(app_max_concurrency=bad)
    assert "APP_MAX_CONCURRENCY must be >= 0" in str(exc.value)


# ---------------------------------------------------------------------------
# 2. _env_managed_ga_fields
# ---------------------------------------------------------------------------

# All DEFAULT_* attributes that influence the locked-field computation.  Each
# test starts from a clean slate so a value leaking in from the developer's
# real .env file cannot make an assertion flaky.
_DEFAULT_ATTRS = {
    "default_model": "",
    "default_general_assistant_model": "",
    "default_reasoning_effort": "",
    "default_routing_model": "",
    "default_fallback_model": "",
    "default_embedding_provider": "",
    "default_embedding_model": "",
    "default_embedding_dimensions": None,
    "default_image_provider": "",
    "default_image_model": "",
    "default_video_provider": "",
    "default_video_model": "",
    "default_speech_provider": "",
    "default_speech_model": "",
    "default_speech_voice": "",
    "default_music_provider": "",
    "default_music_model": "",
    "default_decision_provider": "",
    "default_decision_model": "",
}


def _reset_defaults(monkeypatch):
    for attr, value in _DEFAULT_ATTRS.items():
        monkeypatch.setattr(app_settings, attr, value)


def test_env_managed_fields_empty_when_no_defaults(monkeypatch):
    _reset_defaults(monkeypatch)
    assert _env_managed_ga_fields() == set()


def test_env_managed_fields_locks_agent_default_model(monkeypatch):
    _reset_defaults(monkeypatch)
    monkeypatch.setattr(app_settings, "default_model", "gpt-4.1")
    assert _env_managed_ga_fields() == {
        "agent_default_provider",
        "agent_default_provider_id",
        "agent_default_model",
    }


def test_env_managed_fields_locks_general_assistant_model(monkeypatch):
    """``DEFAULT_GENERAL_ASSISTANT_MODEL`` pins the GA's own chat model and is
    re-seeded on every boot (env always wins), so the GA ``llm_provider`` /
    ``llm_model`` pair must be locked. Unrelated to ``DEFAULT_MODEL``
    (the agent default), which locks the ``agent_default_*`` trio instead.
    """
    _reset_defaults(monkeypatch)
    monkeypatch.setattr(
        app_settings, "default_general_assistant_model", "claude-sonnet-4-6"
    )
    # ``llm_provider_id`` joins the pair: a provider name and its config id
    # lock together, or an admin could point an env-pinned model at a config
    # the env never chose.
    assert _env_managed_ga_fields() == {
        "llm_provider",
        "llm_provider_id",
        "llm_model",
    }


def test_env_managed_fields_ga_model_independent_of_agent_default(monkeypatch):
    """The GA-model and agent-default env vars are independent controls: when
    both ``DEFAULT_MODEL`` and ``DEFAULT_GENERAL_ASSISTANT_MODEL`` are set, each
    locks its own field set without interfering with the other — the GA
    ``llm_provider`` / ``llm_model`` pair and the ``agent_default_*`` trio both
    appear in the locked set.
    """
    _reset_defaults(monkeypatch)
    monkeypatch.setattr(app_settings, "default_model", "gpt-4.1")
    monkeypatch.setattr(
        app_settings, "default_general_assistant_model", "claude-sonnet-4-6"
    )
    locked = _env_managed_ga_fields()
    assert {"llm_provider", "llm_model"} <= locked
    assert {"agent_default_provider", "agent_default_model"} <= locked


def test_env_managed_fields_locks_reasoning_and_routing(monkeypatch):
    _reset_defaults(monkeypatch)
    monkeypatch.setattr(app_settings, "default_reasoning_effort", "high")
    monkeypatch.setattr(app_settings, "default_routing_model", "gpt-4.1-mini")
    locked = _env_managed_ga_fields()
    assert "default_reasoning_effort" in locked
    assert "routing_model" in locked


def test_env_managed_fields_locks_fallback_model(monkeypatch):
    """``DEFAULT_FALLBACK_MODEL`` is re-applied unconditionally on every boot
    (env always wins), so ``fallback_model`` must be locked just like the
    routing model.
    """
    _reset_defaults(monkeypatch)
    monkeypatch.setattr(app_settings, "default_fallback_model", "gpt-4.1")
    assert _env_managed_ga_fields() == {"fallback_model", "fallback_provider_id"}


def test_env_managed_fields_locks_routing_reference(monkeypatch):
    """A provider name and its config id lock together, so an env-pinned routing
    model cannot be pointed at a config the environment never chose."""
    _reset_defaults(monkeypatch)
    monkeypatch.setattr(app_settings, "default_routing_model", "gpt-4.1-mini")
    assert _env_managed_ga_fields() == {"routing_model", "routing_provider_id"}


def test_env_managed_fields_locks_embedding_group(monkeypatch):
    """Embedding defaults ARE env-managed: their boot seeding is
    unconditional (env always wins on restart — the ``and not ps.embedding_*``
    guards were removed from ``main._seed_llm_providers_from_env``), so the
    provider/provider_id/model trio must be locked just like the image group.
    """
    _reset_defaults(monkeypatch)
    monkeypatch.setattr(app_settings, "default_embedding_provider", "openai")
    monkeypatch.setattr(app_settings, "default_embedding_model", "text-embedding-3-large")
    assert _env_managed_ga_fields() == {
        "embedding_provider",
        "embedding_provider_id",
        "embedding_model",
    }


def test_env_managed_fields_embedding_model_only_locks_just_model(monkeypatch):
    """``DEFAULT_EMBEDDING_MODEL`` without ``DEFAULT_EMBEDDING_PROVIDER`` locks
    ONLY ``embedding_model`` — the boot seeding's ``elif`` branch re-applies
    only ``ps.embedding_model`` in that case, so locking the provider
    companions would be a phantom lock (mirrors the image-model-only rule).
    """
    _reset_defaults(monkeypatch)
    monkeypatch.setattr(app_settings, "default_embedding_model", "text-embedding-3-large")
    assert _env_managed_ga_fields() == {"embedding_model"}


def test_env_managed_fields_locks_embedding_dimensions(monkeypatch):
    """``DEFAULT_EMBEDDING_DIMENSIONS`` is re-applied unconditionally on every
    boot (env wins), so ``embedding_dimensions`` must be locked too.
    """
    _reset_defaults(monkeypatch)
    monkeypatch.setattr(app_settings, "default_embedding_dimensions", 3072)
    assert _env_managed_ga_fields() == {"embedding_dimensions"}


def test_env_managed_fields_locks_image_group(monkeypatch):
    _reset_defaults(monkeypatch)
    monkeypatch.setattr(app_settings, "default_image_provider", "openai")
    monkeypatch.setattr(app_settings, "default_image_model", "gpt-image-1")
    assert _env_managed_ga_fields() == {
        "image_provider",
        "image_provider_id",
        "image_model",
    }


def test_env_managed_fields_image_model_only_locks_just_model(monkeypatch):
    """``DEFAULT_IMAGE_MODEL`` without ``DEFAULT_IMAGE_PROVIDER`` locks ONLY
    ``image_model``. The boot seeding's ``elif`` branch re-applies only
    ``ps.image_model`` in that case, so locking ``image_provider`` /
    ``image_provider_id`` would be a phantom lock (env never re-applies them on
    restart). The frontend still renders the whole Image section read-only off
    ``image_model`` alone, so this stays consistent end-to-end.
    """
    _reset_defaults(monkeypatch)
    monkeypatch.setattr(app_settings, "default_image_model", "gpt-image-1")
    assert _env_managed_ga_fields() == {"image_model"}


@pytest.mark.parametrize(
    ("env", "locked"),
    [
        ({"default_video_provider": "gemini"}, {"video_provider", "video_provider_id", "video_model"}),
        (
            {"default_speech_provider": "gemini"},
            {"speech_provider", "speech_provider_id", "speech_model", "speech_voice"},
        ),
        ({"default_music_provider": "gemini"}, {"music_provider", "music_provider_id", "music_model"}),
        (
            {"default_decision_provider": "openai", "default_decision_model": "clef-flash"},
            {"decision_provider_id", "decision_model"},
        ),
        (
            {"default_video_provider": "litellm", "default_video_model": "veo-3.1-generate-preview"},
            {"video_provider", "video_provider_id", "video_model"},
        ),
    ],
    ids=["video", "speech", "music", "decision", "video-model-of-a-provider-without-built-in"],
)
def test_env_managed_fields_lock_a_capability_group_from_its_provider(monkeypatch, env, locked):
    """A capability's provider is what the boot seed re-applies (with the
    provider's built-in model when ``*_MODEL`` is empty), so it locks the
    whole group the Settings section saves together, the voice included."""
    _reset_defaults(monkeypatch)
    for attr, value in env.items():
        monkeypatch.setattr(app_settings, attr, value)
    assert _env_managed_ga_fields() == locked


@pytest.mark.parametrize(
    "env",
    [
        {"default_video_provider": "anthropic"},
        {"default_music_provider": "openai", "default_music_model": "lyria-3.5"},
        {"default_decision_provider": "openai"},
        {"default_decision_provider": "gemini", "default_decision_model": "clef-flash"},
        {"default_video_provider": "litellm"},
    ],
    ids=[
        "type-not-serving-video",
        "type-not-serving-music-with-a-model",
        "decision-without-model",
        "type-not-serving-decisions",
        "no-model-and-no-built-in",
    ],
)
def test_env_managed_fields_a_capability_the_seed_always_refuses_locks_nothing(monkeypatch, env):
    """The boot seed refuses a provider type that does not serve the capability,
    or one with no model to pin, whatever is configured, and keeps the stored
    choice: locking the section would leave the admin no way to change it."""
    _reset_defaults(monkeypatch)
    for attr, value in env.items():
        monkeypatch.setattr(app_settings, attr, value)
    assert _env_managed_ga_fields() == set()


@pytest.mark.parametrize(
    "attr",
    ["default_video_model", "default_speech_model", "default_speech_voice", "default_music_model", "default_decision_model"],
)
def test_env_managed_fields_a_capability_model_alone_locks_nothing(monkeypatch, attr):
    """Without its provider the seed refuses the value and stores nothing, so
    locking the section would be a phantom lock."""
    _reset_defaults(monkeypatch)
    monkeypatch.setattr(app_settings, attr, "some-model")
    assert _env_managed_ga_fields() == set()


def test_env_managed_fields_capability_groups_empty_under_byollm(monkeypatch):
    _reset_defaults(monkeypatch)
    monkeypatch.setattr(app_settings, "byollm_activated", True)
    monkeypatch.setattr(app_settings, "default_video_provider", "gemini")
    monkeypatch.setattr(app_settings, "default_decision_provider", "ollama")
    monkeypatch.setattr(app_settings, "default_decision_model", "tev1:4b")
    assert _env_managed_ga_fields() == set()


# ---------------------------------------------------------------------------
# 3. PUT /settings/general-assistant guard
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _MockSession:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, _statement):
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_update_ga_config_rejects_env_locked_field(monkeypatch):
    from fastapi import BackgroundTasks, HTTPException

    _reset_defaults(monkeypatch)
    monkeypatch.setattr(app_settings, "default_reasoning_effort", "high")

    # Existing GA config row so the endpoint skips the creation branch and
    # reaches the env-lock guard immediately after loading PlatformSettings.
    db = _MockSession([_Result(SimpleNamespace())])

    async def _fake_load_ps(_db):
        return SimpleNamespace(
            embedding_provider=None,
            embedding_provider_id=None,
            embedding_model=None,
            embedding_dimensions=None,
        )

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _fake_load_ps,
    )

    body = GeneralAssistantConfigRequest(default_reasoning_effort="low")

    with pytest.raises(HTTPException) as exc:
        await update_general_assistant_config(
            body=body,
            request=SimpleNamespace(),
            background_tasks=BackgroundTasks(),
            user=SimpleNamespace(id="admin", email="admin@filigran.io"),
            db=db,
        )

    assert exc.value.status_code == 403
    assert "default_reasoning_effort" in str(exc.value.detail)


def test_guard_only_fires_for_locked_fields(monkeypatch):
    """The guard intersects env-managed fields with the request's
    ``model_fields_set`` — an unlocked field (e.g. ``routing_model`` when no
    ``DEFAULT_ROUTING_MODEL`` is set) must never be flagged, so an admin can
    still edit non-env-managed defaults while reasoning effort is locked.
    """
    _reset_defaults(monkeypatch)
    monkeypatch.setattr(app_settings, "default_reasoning_effort", "high")

    env_locked = _env_managed_ga_fields()

    unlocked_body = GeneralAssistantConfigRequest(routing_model="gpt-4.1-mini")
    assert not (env_locked & unlocked_body.model_fields_set)

    locked_body = GeneralAssistantConfigRequest(default_reasoning_effort="low")
    assert env_locked & locked_body.model_fields_set == {"default_reasoning_effort"}
