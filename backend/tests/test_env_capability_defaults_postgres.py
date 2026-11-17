"""The video, speech, music and decision models set from the environment.

``DEFAULT_<ROLE>_PROVIDER`` / ``DEFAULT_<ROLE>_MODEL`` (and
``DEFAULT_SPEECH_VOICE``) are applied by the real boot seed
(``main._seed_llm_providers_from_env``) the way ``DEFAULT_IMAGE_*`` is: on every
boot, the environment winning over a Settings edit, once under
``BYOLLM_ACTIVATED``, and never when the platform could not serve the value.
Run against a real database, through the seed itself.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.main as app_main  # its import resets the root log handlers: never inside a test using caplog
import app.models  # noqa: F401 - registers every mapper
from app.api.v1.platform_settings_helpers import get_platform_settings
from app.core.config import settings
from app.core.security import encrypt_value
from app.llm.env_capability_defaults import ROLES, env_locked_fields
from app.llm.router import VIDEO_DEFAULTS
from app.models.llm_config import LLMProviderConfig
from app.models.user import User
from app.services.flow_classifier import decision_provider_types
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

# Every DEFAULT_* the seed reads, emptied so a developer's .env cannot leak in.
_EMPTY_DEFAULTS: dict[str, object] = {
    "default_model": "",
    "default_general_assistant_model": "",
    "default_routing_model": "",
    "default_fallback_model": "",
    "default_reasoning_effort": "",
    "default_embedding_provider": "",
    "default_embedding_model": "",
    "default_embedding_provider_base_url": "",
    "default_embedding_dimensions": None,
    "default_image_provider": "",
    "default_image_model": "",
    "default_image_provider_base_url": "",
    **{role.provider_env.lower(): "" for role in ROLES},
    **{role.model_env.lower(): "" for role in ROLES},
    **{role.voice_env.lower(): "" for role in ROLES if role.voice_env},
}

# The SaaS baseline: Veo and Lyria on Gemini, OpenAI's HD voice.
SAAS = {
    "default_video_provider": "gemini",
    "default_speech_provider": "openai",
    "default_speech_model": "tts-1-hd",
    "default_speech_voice": "alloy",
    "default_music_provider": "gemini",
    "default_music_model": "lyria-3.5",
}


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("envcap", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


def _env(monkeypatch, *, byollm: bool = False, **defaults: object) -> None:
    for key in [k for k in os.environ if k.startswith("LLM_PROVIDER_")]:
        monkeypatch.delenv(key)
    monkeypatch.setattr(app_main, "_load_dotenv_files", lambda: None)
    monkeypatch.setenv("LLM_PROVIDER_OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("LLM_PROVIDER_OPENAI_MODELS", "gpt-4.1,gpt-image-2")
    monkeypatch.setenv("LLM_PROVIDER_GEMINI_API_KEY", "AIza-gemini")
    monkeypatch.setenv("LLM_PROVIDER_GEMINI_MODELS", "gemini-3-flash")
    monkeypatch.setenv("LLM_PROVIDER_ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setattr(settings, "byollm_activated", byollm)
    monkeypatch.setattr(settings, "models_env_providing_only", False)
    monkeypatch.setattr(settings, "llm_provider_models_editable", False)
    for attr, value in {**_EMPTY_DEFAULTS, **defaults}.items():
        monkeypatch.setattr(settings, attr, value)


async def _boot() -> None:
    await app_main._seed_llm_providers_from_env()


async def _config_id(sessions, provider: str):
    async with sessions() as db:
        return (
            await db.execute(select(LLMProviderConfig.id).where(LLMProviderConfig.provider == provider))
        ).scalar_one()


async def _stored(sessions) -> dict:
    async with sessions() as db:
        ps = await get_platform_settings(db)
        return {attr: getattr(ps, attr) for role in ROLES for attr in role.fields}


@pytest.mark.asyncio
async def test_without_default_video_the_seed_stores_no_video_model(monkeypatch):
    """The fill-in stored the first seeded provider's built-in model - OpenAI's retired ``sora-2``."""
    async with _database() as sessions:
        _env(monkeypatch)
        await _boot()
        stored = await _stored(sessions)
        # "Auto": the generation takes the first provider whose built-in model is live.
        assert (stored["video_provider"], stored["video_provider_id"], stored["video_model"]) == (None, None, None)
        assert stored["speech_model"] is None and stored["music_model"] is None
        async with sessions() as db:
            ps = await get_platform_settings(db)
            assert ps.agent_default_model and ps.image_model  # the other fill-ins still happen


@pytest.mark.asyncio
async def test_the_environment_pins_video_speech_and_music(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch, **SAAS)
        await _boot()

        stored = await _stored(sessions)
        gemini, openai = await _config_id(sessions, "gemini"), await _config_id(sessions, "openai")
        # No DEFAULT_VIDEO_MODEL: the provider's built-in video model.
        assert (stored["video_provider"], stored["video_provider_id"], stored["video_model"]) == (
            "gemini",
            gemini,
            VIDEO_DEFAULTS["gemini"],
        )
        assert (stored["speech_provider"], stored["speech_provider_id"], stored["speech_model"]) == (
            "openai",
            openai,
            "tts-1-hd",
        )
        assert stored["speech_voice"] == "alloy"
        assert (stored["music_provider"], stored["music_provider_id"], stored["music_model"]) == (
            "gemini",
            gemini,
            "lyria-3.5",
        )


@pytest.mark.asyncio
async def test_the_environment_wins_over_a_settings_edit_at_the_next_boot(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch, **SAAS)
        await _boot()
        async with sessions() as db:
            ps = await get_platform_settings(db)
            ps.video_model = "veo-2.0-generate-001"
            ps.speech_voice = "nova"
            await db.commit()

        await _boot()

        stored = await _stored(sessions)
        assert stored["video_model"] == VIDEO_DEFAULTS["gemini"]
        assert stored["speech_voice"] == "alloy"


@pytest.mark.asyncio
async def test_the_decision_model_is_pinned_with_its_provider(monkeypatch):
    assert "openai" in decision_provider_types()
    async with _database() as sessions:
        _env(monkeypatch, default_decision_provider="openai", default_decision_model="clef-flash")
        await _boot()

        stored = await _stored(sessions)
        assert stored["decision_model"] == "clef-flash"
        assert stored["decision_provider_id"] == await _config_id(sessions, "openai")


@pytest.mark.asyncio
async def test_a_value_the_platform_cannot_serve_is_never_stored(monkeypatch, caplog):
    """A provider that does not serve the capability, one that is not
    configured, a decision provider named without its model and a model named
    without its provider each keep the stored choice, with a warning."""
    assert "anthropic" not in decision_provider_types()
    async with _database() as sessions:
        _env(monkeypatch)
        await _boot()
        async with sessions() as db:
            ps = await get_platform_settings(db)
            ps.music_model = "lyria-3-clip-preview"
            await db.commit()
        before = await _stored(sessions)

        _env(
            monkeypatch,
            default_video_provider="anthropic",
            default_music_provider="elevenlabs",
            default_speech_model="tts-1-hd",
            default_decision_provider="openai",
        )
        with caplog.at_level(logging.WARNING, logger="app.llm.env_capability_defaults"):
            await _boot()

        assert await _stored(sessions) == before
        assert before["music_model"] == "lyria-3-clip-preview"
        warnings = " ".join(r.getMessage() for r in caplog.records)
        assert "DEFAULT_VIDEO_PROVIDER=anthropic is not a provider type that serves video generation" in warnings
        assert "DEFAULT_MUSIC_PROVIDER=elevenlabs names no configured provider" in warnings
        assert "DEFAULT_SPEECH_MODEL=tts-1-hd is set without DEFAULT_SPEECH_PROVIDER" in warnings
        assert "DEFAULT_DECISION_PROVIDER=openai has no built-in model for decision models" in warnings
        # What no configuration could make the seed apply leaves its section
        # editable; a provider type that serves music but is not configured
        # keeps it locked until that provider is configured.
        music = next(role for role in ROLES if role.key == "music")
        assert env_locked_fields() == music.fields


@pytest.mark.asyncio
async def test_the_environments_own_config_serves_a_role_over_one_an_admin_added(monkeypatch):
    """Another config of the provider type, added in Settings and made the
    default one, never takes the role from the env-seeded config, which holds
    the key the environment names."""
    async with _database() as sessions:
        _env(monkeypatch, **SAAS)
        await _boot()
        env_gemini = await _config_id(sessions, "gemini")
        async with sessions() as db:
            admin = User(email="admin@acme.test", full_name="Ann Admin", role="admin", is_active=True)
            db.add(admin)
            await db.flush()
            db.add(
                LLMProviderConfig(
                    provider="gemini",
                    display_name="Gemini (admin)",
                    api_key_enc=encrypt_value("AIza-admin"),
                    user_id=admin.id,
                    is_default=True,
                )
            )
            await db.commit()

        await _boot()

        stored = await _stored(sessions)
        assert stored["video_provider_id"] == env_gemini
        assert stored["music_provider_id"] == env_gemini


@pytest.mark.asyncio
async def test_byollm_furnishes_an_empty_install_once(monkeypatch):
    """Under BYOLLM the environment furnishes an install that has no LLM
    configuration yet; once an administrator chose, a restart leaves every
    capability alone."""
    async with _database() as sessions:
        _env(monkeypatch, byollm=True, **SAAS)
        await _boot()
        assert (await _stored(sessions))["video_model"] == VIDEO_DEFAULTS["gemini"]

        async with sessions() as db:
            ps = await get_platform_settings(db)
            ps.video_model = "veo-2.0-generate-001"
            ps.speech_model = "gpt-4o-mini-tts"
            ps.speech_voice = None
            await db.commit()

        await _boot()

        stored = await _stored(sessions)
        assert stored["video_model"] == "veo-2.0-generate-001"
        assert stored["speech_model"] == "gpt-4o-mini-tts"
        assert stored["speech_voice"] is None
