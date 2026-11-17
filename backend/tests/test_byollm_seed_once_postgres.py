"""BYOLLM: the boot seed furnishes an install once, then never writes again.

With ``BYOLLM_ACTIVATED=true`` the ``DEFAULT_*`` variables furnish an install
that has no LLM configuration of its own. The real boot seed
(``main._seed_llm_providers_from_env``) applies them to such an install and,
from the first thing an administrator stores, leaves every one of them alone
— the choice to leave a field *empty* included. Without the flag the same
environment overwrites the stored values on every boot, which is exactly why
those Settings controls are read-only.

Both halves run here against a real database, through the seed itself rather
than a source inspection, because "the admin's model survives a restart" is
the whole promise of the feature.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper
from app.api.v1.platform_settings_helpers import get_platform_settings
from app.core.config import settings
from app.core.security import decrypt_value
from app.models.general_assistant_config import GeneralAssistantConfig
from app.models.llm_config import LLMProviderConfig
from app.models.user import User
from app.services import model_catalog_announcements as mca
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

# The operator's baseline: one env-seeded provider and a default for every
# platform field the seed writes.
ENV_MODELS = "gpt-4.1,gpt-4.1-mini,gpt-image-1"
BASELINE = {
    "default_model": "gpt-4.1",
    "default_general_assistant_model": "gpt-4.1",
    "default_routing_model": "gpt-4.1-mini",
    "default_fallback_model": "gpt-4.1-mini",
    "default_reasoning_effort": "high",
    "default_embedding_provider": "openai",
    "default_embedding_model": "text-embedding-3-large",
    "default_embedding_dimensions": 3072,
    "default_image_provider": "openai",
    "default_image_model": "gpt-image-1",
}

# What an administrator picks instead, once, from Settings -> AI Models.
ADMIN_CHOICE = {
    "agent_default_model": "gpt-4.1-mini",
    "routing_model": "gpt-4.1",
    "fallback_model": "gpt-4.1",
    "default_reasoning_effort": "low",
    "embedding_provider": "mistral",
    "embedding_model": "mistral-embed",
    "embedding_dimensions": 1024,
    "image_provider": "mistral",
    "image_model": "mistral-image",
}


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("byollm", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


def _env(monkeypatch, *, byollm: bool, api_key: str = "sk-openai-baseline") -> None:
    """The deployment's environment: one provider plus the whole baseline."""
    for key in [k for k in os.environ if k.startswith("LLM_PROVIDER_")]:
        monkeypatch.delenv(key)
    import app.main as app_main

    monkeypatch.setattr(app_main, "_load_dotenv_files", lambda: None)
    monkeypatch.setenv("LLM_PROVIDER_OPENAI_API_KEY", api_key)
    monkeypatch.setenv("LLM_PROVIDER_OPENAI_MODELS", ENV_MODELS)
    monkeypatch.setattr(settings, "byollm_activated", byollm)
    monkeypatch.setattr(settings, "models_env_providing_only", False)
    monkeypatch.setattr(settings, "llm_provider_models_editable", False)
    for attr, value in BASELINE.items():
        monkeypatch.setattr(settings, attr, value)


async def _boot() -> None:
    """One restart: the real seed, watched the way provisioning runs it."""
    from app.main import _seed_llm_providers_from_env

    async with mca.model_catalog_watch():
        await _seed_llm_providers_from_env()


async def _platform(sessions) -> dict:
    async with sessions() as db:
        ps = await get_platform_settings(db)
        ga = (
            await db.execute(select(GeneralAssistantConfig).limit(1))
        ).scalar_one_or_none()
        return {
            "agent_default_model": ps.agent_default_model,
            "agent_default_provider": ps.agent_default_provider,
            "routing_model": ps.routing_model,
            "fallback_model": ps.fallback_model,
            "default_reasoning_effort": ps.default_reasoning_effort,
            "embedding_provider": ps.embedding_provider,
            "embedding_model": ps.embedding_model,
            "embedding_dimensions": ps.embedding_dimensions,
            "image_provider": ps.image_provider,
            "image_model": ps.image_model,
            "ga_model": getattr(ga, "llm_model", None),
        }


async def _administrator_picks(sessions) -> None:
    """Stand in for the Settings -> AI Models saves, which the controls now
    accept because nothing is env-managed under BYOLLM."""
    async with sessions() as db:
        ps = await get_platform_settings(db)
        for field, value in ADMIN_CHOICE.items():
            setattr(ps, field, value)
        ga = (
            await db.execute(select(GeneralAssistantConfig).limit(1))
        ).scalar_one_or_none()
        if ga is not None:
            ga.llm_model = "gpt-4.1-mini"
        await db.commit()


@pytest.mark.asyncio
async def test_byollm_seeds_an_empty_install_from_the_operator_baseline(monkeypatch):
    """A fresh install still starts configured: seed-once is "once", not
    "never", so the operator's DEFAULT_* values are what the first boot
    writes."""
    async with _database() as sessions:
        _env(monkeypatch, byollm=True)
        await _boot()

        after = await _platform(sessions)
        assert after["agent_default_model"] == "gpt-4.1"
        assert after["routing_model"] == "gpt-4.1-mini"
        assert after["fallback_model"] == "gpt-4.1-mini"
        assert after["default_reasoning_effort"] == "high"
        assert after["embedding_model"] == "text-embedding-3-large"
        assert after["embedding_dimensions"] == 3072
        assert after["image_model"] == "gpt-image-1"
        assert after["ga_model"] == "gpt-4.1"


@pytest.mark.asyncio
async def test_byollm_keeps_every_administrator_choice_across_a_restart(monkeypatch):
    """The feature: once an admin has chosen, a restart leaves all of it
    alone — agent default, General Assistant model, routing, fallback,
    reasoning effort, and the embedding / image groups."""
    async with _database() as sessions:
        _env(monkeypatch, byollm=True)
        await _boot()
        await _administrator_picks(sessions)

        await _boot()

        after = await _platform(sessions)
        assert after["agent_default_model"] == "gpt-4.1-mini"
        assert after["routing_model"] == "gpt-4.1"
        assert after["fallback_model"] == "gpt-4.1"
        assert after["default_reasoning_effort"] == "low"
        assert after["embedding_provider"] == "mistral"
        assert after["embedding_model"] == "mistral-embed"
        assert after["embedding_dimensions"] == 1024
        assert after["image_provider"] == "mistral"
        assert after["image_model"] == "mistral-image"
        assert after["ga_model"] == "gpt-4.1-mini"


@pytest.mark.asyncio
async def test_without_byollm_the_restart_takes_every_choice_back(monkeypatch):
    """The contrast that makes the lock legitimate: with the flag off the same
    restart overwrites all of it, which is why those controls are read-only."""
    async with _database() as sessions:
        _env(monkeypatch, byollm=False)
        await _boot()
        await _administrator_picks(sessions)

        await _boot()

        after = await _platform(sessions)
        assert after["agent_default_model"] == "gpt-4.1"
        assert after["routing_model"] == "gpt-4.1-mini"
        assert after["fallback_model"] == "gpt-4.1-mini"
        assert after["default_reasoning_effort"] == "high"
        assert after["embedding_provider"] == "openai"
        assert after["embedding_model"] == "text-embedding-3-large"
        assert after["embedding_dimensions"] == 3072
        assert after["image_provider"] == "openai"
        assert after["image_model"] == "gpt-image-1"
        assert after["ga_model"] == "gpt-4.1"


@pytest.mark.asyncio
async def test_byollm_applies_default_model_from_the_provider_seeded_second(
    monkeypatch,
):
    """The platform default must come from DEFAULT_MODEL, not from whichever
    provider the environment happens to list first.

    ``_ensure_platform_default_model`` fills an empty ``agent_default_model``
    from each provider as the loop seeds it, so by the time the DEFAULT_MODEL
    pass runs the field already holds the *first* provider's chat model.
    Reading seedability there would mistake it for an administrator's choice
    and strand a fresh install on the wrong model.
    """
    async with _database() as sessions:
        _env(monkeypatch, byollm=True)
        # The seed iterates ``os.environ`` in insertion order, so clear the
        # OpenAI pair and re-add it AFTER Anthropic: DEFAULT_MODEL (gpt-4.1)
        # is then served only by the provider seeded second, which is the
        # shape that exposed the bug.
        monkeypatch.delenv("LLM_PROVIDER_OPENAI_API_KEY")
        monkeypatch.delenv("LLM_PROVIDER_OPENAI_MODELS")
        monkeypatch.setenv("LLM_PROVIDER_ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("LLM_PROVIDER_ANTHROPIC_MODELS", "claude-sonnet-5")
        monkeypatch.setenv("LLM_PROVIDER_OPENAI_API_KEY", "sk-openai-baseline")
        monkeypatch.setenv("LLM_PROVIDER_OPENAI_MODELS", ENV_MODELS)

        import os as _os

        seeded_order = [
            k
            for k in _os.environ
            if k.startswith("LLM_PROVIDER_") and k.endswith("_API_KEY")
        ]
        assert seeded_order == [
            "LLM_PROVIDER_ANTHROPIC_API_KEY",
            "LLM_PROVIDER_OPENAI_API_KEY",
        ], seeded_order

        await _boot()

        after = await _platform(sessions)
        assert after["agent_default_model"] == "gpt-4.1"
        assert after["agent_default_provider"] == "openai"


@pytest.mark.asyncio
async def test_byollm_leaves_an_embedding_model_without_the_baseline_dimensions(
    monkeypatch,
):
    """Dimensions belong to the model they were measured on.

    An administrator on a 1024-d model who leaves the dimension unset must
    not get DEFAULT_EMBEDDING_DIMENSIONS=3072 stamped onto it at the next
    restart: the env-seed path runs no re-embedding, so the mismatch would
    corrupt every later vector read.
    """
    async with _database() as sessions:
        _env(monkeypatch, byollm=True)
        await _boot()

        async with sessions() as db:
            ps = await get_platform_settings(db)
            ps.embedding_provider = "mistral"
            ps.embedding_model = "mistral-embed"
            ps.embedding_dimensions = None
            await db.commit()

        await _boot()

        after = await _platform(sessions)
        assert after["embedding_model"] == "mistral-embed"
        assert after["embedding_dimensions"] is None


@pytest.mark.asyncio
async def test_byollm_lets_the_general_assistant_stay_on_agent_default(monkeypatch):
    """NULL is the "Agent default" entry of the chat model picker, not a gap.

    It is the configuration BYOLLM exists to allow - the assistant follows
    the agent default the administrator picked - so a restart must not
    re-pin DEFAULT_GENERAL_ASSISTANT_MODEL over it.
    """
    async with _database() as sessions:
        _env(monkeypatch, byollm=True)
        await _boot()

        async with sessions() as db:
            ga = (
                await db.execute(select(GeneralAssistantConfig).limit(1))
            ).scalar_one()
            ga.llm_model = None
            ga.llm_provider = None
            ga.llm_provider_id = None
            await db.commit()

        await _boot()

        assert (await _platform(sessions))["ga_model"] is None


@pytest.mark.asyncio
async def test_byollm_leaves_a_cleared_routing_and_fallback_cleared(monkeypatch):
    """Clearing them is a choice the router reads: no explicit fallback means
    "fail over across the providers' own defaults", and no routing model
    means "use the platform default". Neither may be silently re-pinned."""
    async with _database() as sessions:
        _env(monkeypatch, byollm=True)
        await _boot()

        async with sessions() as db:
            ps = await get_platform_settings(db)
            ps.routing_model = None
            ps.fallback_model = None
            await db.commit()

        await _boot()

        after = await _platform(sessions)
        assert after["routing_model"] is None
        assert after["fallback_model"] is None


@pytest.mark.asyncio
async def test_the_general_assistant_route_accepts_a_locked_field_under_byollm(
    monkeypatch,
):
    """The lock lives in the route, not only in ``_env_managed_ga_fields``.

    Pinning it here means a later re-ordering of the route cannot keep
    refusing the save while the helper reports nothing as env-managed.
    """
    from fastapi import BackgroundTasks, HTTPException

    from app.api.v1.schemas.settings import GeneralAssistantConfigRequest
    from app.api.v1.settings import update_general_assistant_config

    async def _save(db, admin, effort: str):
        await update_general_assistant_config(
            body=GeneralAssistantConfigRequest(default_reasoning_effort=effort),
            request=SimpleNamespace(),
            background_tasks=BackgroundTasks(),
            user=admin,
            db=db,
        )

    async with _database() as sessions:
        _env(monkeypatch, byollm=True)
        await _boot()

        # A real row: the save writes an audit log that references it.
        async with sessions() as db:
            admin = User(
                email="admin@acme.one.test", full_name="Admin", role="admin"
            )
            db.add(admin)
            await db.commit()
            admin_id = admin.id

        admin = SimpleNamespace(id=admin_id, email="admin@acme.one.test")
        async with sessions() as db:
            await _save(db, admin, "low")

        assert (await _platform(sessions))["default_reasoning_effort"] == "low"

        # Same environment, flag off: DEFAULT_REASONING_EFFORT is re-applied
        # on every boot, so the route must refuse the save instead of letting
        # it revert at the next restart.
        monkeypatch.setattr(settings, "byollm_activated", False)
        async with sessions() as db:
            with pytest.raises(HTTPException) as exc:
                await _save(db, admin, "medium")
        assert exc.value.status_code == 403
        assert "default_reasoning_effort" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_byollm_leaves_provider_credentials_env_managed(monkeypatch):
    """BYOLLM is about who chooses the models, not about who holds the
    operator's key: an ``LLM_PROVIDER_*_API_KEY`` is still re-applied on every
    boot, so rotating it in the environment still reaches the install."""
    async with _database() as sessions:
        _env(monkeypatch, byollm=True)
        await _boot()

        _env(monkeypatch, byollm=True, api_key="sk-openai-rotated")
        await _boot()

        async with sessions() as db:
            row = (
                await db.execute(
                    select(LLMProviderConfig).where(
                        LLMProviderConfig.provider == "openai"
                    )
                )
            ).scalar_one()
            assert decrypt_value(row.api_key_enc) == "sk-openai-rotated"
            assert row.is_from_env is True


@pytest.mark.asyncio
async def test_byollm_lets_an_admin_curate_the_env_seeded_catalog(monkeypatch):
    """BYOLLM implies ``LLM_PROVIDER_MODELS_EDITABLE``: the env list is a
    baseline, so a model the admin added to the catalog survives the restart
    while a model the operator retires still goes away."""
    async with _database() as sessions:
        _env(monkeypatch, byollm=True)
        await _boot()

        async with sessions() as db:
            row = (
                await db.execute(
                    select(LLMProviderConfig).where(
                        LLMProviderConfig.provider == "openai"
                    )
                )
            ).scalar_one()
            row.available_models = [*(row.available_models or []), "o4-mini"]
            await db.commit()

        # The operator retires gpt-4.1-mini from the baseline.
        _env(monkeypatch, byollm=True)
        monkeypatch.setenv("LLM_PROVIDER_OPENAI_MODELS", "gpt-4.1,gpt-image-1")
        await _boot()

        async with sessions() as db:
            row = (
                await db.execute(
                    select(LLMProviderConfig).where(
                        LLMProviderConfig.provider == "openai"
                    )
                )
            ).scalar_one()
            assert "o4-mini" in (row.available_models or [])
            assert "gpt-4.1-mini" not in (row.available_models or [])


def _keys_only(monkeypatch) -> None:
    """The deployment's first shape: a provider key and its models, no DEFAULT_*."""
    _env(monkeypatch, byollm=True)
    for attr, value in BASELINE.items():
        monkeypatch.setattr(settings, attr, None if isinstance(value, int) else "")


@pytest.mark.asyncio
async def test_byollm_applies_a_default_model_added_after_a_keys_only_boot(
    monkeypatch,
):
    """The platform picking a chat model from the provider is not an
    administrator configuring the install: a DEFAULT_MODEL the operator adds on
    the second boot is still applied."""
    async with _database() as sessions:
        _keys_only(monkeypatch)
        await _boot()

        first = await _platform(sessions)
        # Filled by the platform from the env-seeded row, nobody's choice.
        assert first["agent_default_model"] == "gpt-4.1"
        assert first["embedding_model"] == "text-embedding-3-large"
        assert first["image_model"] == "gpt-image-1"

        monkeypatch.setattr(settings, "default_model", "gpt-4.1-mini")
        await _boot()

        assert (await _platform(sessions))["agent_default_model"] == "gpt-4.1-mini"


@pytest.mark.asyncio
async def test_byollm_keeps_an_agent_default_picked_from_the_env_provider(
    monkeypatch,
):
    """The env-seeded provider is the administrator's to choose from too: a
    model picked from its catalog, other than the one the platform fills,
    configures the install, so the DEFAULT_MODEL added later is inert."""
    async with _database() as sessions:
        _keys_only(monkeypatch)
        await _boot()

        async with sessions() as db:
            ps = await get_platform_settings(db)
            ps.agent_default_model = "gpt-4.1-mini"
            await db.commit()

        monkeypatch.setattr(settings, "default_model", "gpt-4.1")
        await _boot()

        assert (await _platform(sessions))["agent_default_model"] == "gpt-4.1-mini"


@pytest.mark.asyncio
async def test_byollm_leaves_a_cleared_embedding_and_image_model_cleared(
    monkeypatch,
):
    """The provider loop fills an empty embedding and image default from the
    env-seeded OpenAI row; on a configured install that would take the
    administrator's choice back at every restart."""
    async with _database() as sessions:
        _env(monkeypatch, byollm=True)
        await _boot()

        async with sessions() as db:
            ps = await get_platform_settings(db)
            ps.embedding_provider = None
            ps.embedding_provider_id = None
            ps.embedding_model = None
            ps.image_provider = None
            ps.image_provider_id = None
            ps.image_model = None
            await db.commit()

        await _boot()

        after = await _platform(sessions)
        assert after["embedding_model"] is None
        assert after["image_model"] is None
        # Still configured from the rest: nothing else came back either.
        assert after["routing_model"] == "gpt-4.1-mini"


@pytest.mark.asyncio
async def test_byollm_builtin_embedding_furnishes_once_then_stays_cleared(
    monkeypatch,
):
    """The XTM One embedding endpoint fills an empty embedding default. It
    furnishes a fresh install even though the LLM seed of the same boot has
    just stored the operator's reasoning effort, and leaves a cleared default
    of a configured install alone."""
    from app.services.provisioning import provision_env_providers

    async with _database() as sessions:
        _keys_only(monkeypatch)
        # A chat provider with no built-in embedding model, so the endpoint
        # is the only thing that can fill the embedding default.
        monkeypatch.delenv("LLM_PROVIDER_OPENAI_API_KEY")
        monkeypatch.delenv("LLM_PROVIDER_OPENAI_MODELS")
        monkeypatch.setenv("LLM_PROVIDER_ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("LLM_PROVIDER_ANTHROPIC_MODELS", "claude-sonnet-5")
        monkeypatch.setattr(settings, "default_reasoning_effort", "high")
        monkeypatch.setattr(settings, "platform_mode", "xtm_one")
        monkeypatch.setattr(settings, "builtin_embedding_server_base_url", "http://tei.test")
        monkeypatch.setattr(settings, "builtin_embedding_server_model", "bge-m3")
        monkeypatch.setattr(settings, "builtin_embedding_server_dimensions", None)

        await provision_env_providers()

        first = await _platform(sessions)
        assert first["default_reasoning_effort"] == "high"
        assert first["embedding_provider"] == "tei"
        assert first["embedding_model"] == "bge-m3"

        async with sessions() as db:
            ps = await get_platform_settings(db)
            ps.embedding_provider = None
            ps.embedding_provider_id = None
            ps.embedding_model = None
            await db.commit()

        await provision_env_providers()

        after = await _platform(sessions)
        assert after["embedding_provider"] is None
        assert after["embedding_model"] is None


@pytest.mark.asyncio
async def test_a_model_the_seed_retires_stays_on_a_choice_pinned_to_another_config(
    monkeypatch,
):
    """Retiring a model from the env-seeded catalog cleans the choices that
    config served, not a choice pinned to another config listing the same
    model id - a gateway next to a direct provider."""
    from app.core.security import encrypt_value

    async with _database() as sessions:
        _env(monkeypatch, byollm=True)
        await _boot()

        async with sessions() as db:
            gateway = LLMProviderConfig(
                user_id=None,
                provider="litellm",
                api_key_enc=encrypt_value("sk-gateway||http://gateway.test"),
                default_model="gpt-4.1-mini",
                available_models=["gpt-4.1-mini"],
                is_from_env=False,
            )
            db.add(gateway)
            await db.flush()
            ps = await get_platform_settings(db)
            ps.agent_default_provider = "litellm"
            ps.agent_default_provider_id = gateway.id
            ps.agent_default_model = "gpt-4.1-mini"
            await db.commit()

        # The operator retires gpt-4.1-mini from the OpenAI baseline.
        monkeypatch.setenv("LLM_PROVIDER_OPENAI_MODELS", "gpt-4.1,gpt-image-1")
        await _boot()

        after = await _platform(sessions)
        assert after["agent_default_provider"] == "litellm"
        assert after["agent_default_model"] == "gpt-4.1-mini"
