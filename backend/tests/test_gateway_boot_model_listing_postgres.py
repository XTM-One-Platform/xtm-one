"""A gateway seeded with no ``LLM_PROVIDER_FILIGRAN_GATEWAY_MODELS`` lists its models at boot.

Without the list the gateway was seeded with no model, so ``DEFAULT_MODEL``
resolved to nothing and, in the default env-locked mode, Settings could not
add one either. Run through the real seed against a real database.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper
from app.api.v1.platform_settings_helpers import get_platform_settings
from app.core.config import settings
from app.llm.gateway_provider import FiligranGatewayProvider
from app.models.llm_config import LLMProviderConfig
from app.models.user import User
from app.services import model_catalog_announcements as mca
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

GATEWAY_KEY = "https://gateway.example||||static-token"
# What /v1/models offers: chat models, plus ids that do not chat.
LISTED = ["claude-sonnet-5", "gpt-5.4", "text-embedding-3-large", "gpt-image-2"]
CHAT = ["claude-sonnet-5", "gpt-5.4"]


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("gwlist", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


class _Gateway:
    """Stands in for the gateway's ``/v1/models``; counts the calls."""

    def __init__(self, monkeypatch, models: list[str] | Exception, chat_default: str | None = None):
        self.models = models
        # The model the listing marks ``"default_for": ["chat"]`` (#1421).
        self.chat_default = chat_default
        self.calls = 0

        async def _list_models(provider: FiligranGatewayProvider) -> list[str]:
            self.calls += 1
            if isinstance(self.models, Exception):
                raise self.models
            return sorted(self.models)  # as the gateway's list_models does

        async def _marked_default_model(provider: FiligranGatewayProvider, kind: str) -> str | None:
            return self.chat_default if kind == "chat" else None

        monkeypatch.setattr(FiligranGatewayProvider, "list_models", _list_models)
        monkeypatch.setattr(FiligranGatewayProvider, "marked_default_model", _marked_default_model)


def _env(
    monkeypatch,
    *,
    editable: bool = False,
    models: str | None = None,
    default_model: str = "",
    extra: dict[str, str] | None = None,
) -> None:
    for key in [k for k in os.environ if k.startswith("LLM_PROVIDER_")]:
        monkeypatch.delenv(key)
    import app.main as app_main

    monkeypatch.setattr(app_main, "_load_dotenv_files", lambda: None)
    monkeypatch.setenv("LLM_PROVIDER_FILIGRAN_GATEWAY_API_KEY", GATEWAY_KEY)
    if models is not None:
        monkeypatch.setenv("LLM_PROVIDER_FILIGRAN_GATEWAY_MODELS", models)
    for key, value in (extra or {}).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(settings, "byollm_activated", False)
    monkeypatch.setattr(settings, "models_env_providing_only", False)
    monkeypatch.setattr(settings, "llm_provider_models_editable", editable)
    monkeypatch.setattr(settings, "default_model", default_model)


async def _boot() -> None:
    from app.main import _seed_llm_providers_from_env

    async with mca.model_catalog_watch():
        await _seed_llm_providers_from_env()


async def _gateway_row(sessions) -> LLMProviderConfig:
    async with sessions() as db:
        return (
            await db.execute(select(LLMProviderConfig).where(LLMProviderConfig.provider == "filigran_gateway"))
        ).scalar_one()


@pytest.mark.asyncio
async def test_unset_models_seed_the_gateway_with_its_chat_models(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch, default_model="gpt-5.4")
        _Gateway(monkeypatch, LISTED)

        await _boot()

        row = await _gateway_row(sessions)
        assert row.available_models == CHAT
        assert row.default_model in CHAT
        async with sessions() as db:
            ps = await get_platform_settings(db)
            assert (ps.agent_default_provider, ps.agent_default_model) == ("filigran_gateway", "gpt-5.4")


@pytest.mark.asyncio
async def test_env_locked_catalog_follows_the_gateway_on_every_boot(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch)
        gateway = _Gateway(monkeypatch, LISTED)
        await _boot()

        gateway.models = ["gpt-5.4", "gemini-3-pro"]
        await _boot()

        assert (await _gateway_row(sessions)).available_models == ["gemini-3-pro", "gpt-5.4"]


@pytest.mark.asyncio
async def test_a_failed_listing_keeps_the_stored_models(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch)
        gateway = _Gateway(monkeypatch, LISTED)
        await _boot()

        gateway.models = RuntimeError("gateway down")
        await _boot()

        row = await _gateway_row(sessions)
        assert row.available_models == CHAT
        # Kept, or the next listing reads as a catalog change (model announcements).
        assert row.env_models == CHAT


@pytest.mark.asyncio
async def test_editable_mode_fills_an_empty_catalog_once(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch, editable=True)
        gateway = _Gateway(monkeypatch, LISTED)
        await _boot()
        assert (await _gateway_row(sessions)).available_models == CHAT

        # The administrator curates; the next boot leaves it alone and does not ask.
        async with sessions() as db:
            row = (
                await db.execute(select(LLMProviderConfig).where(LLMProviderConfig.provider == "filigran_gateway"))
            ).scalar_one()
            row.available_models = ["gpt-5.4"]
            row.default_model = "gpt-5.4"
            await db.commit()
        gateway.calls = 0
        gateway.models = ["gpt-5.4", "gemini-3-pro"]
        await _boot()

        row = await _gateway_row(sessions)
        assert row.available_models == ["gpt-5.4"]
        assert gateway.calls == 0


@pytest.mark.asyncio
async def test_a_models_list_in_the_environment_is_not_replaced(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch, models="gpt-5.4")
        gateway = _Gateway(monkeypatch, LISTED)

        await _boot()

        assert (await _gateway_row(sessions)).available_models == ["gpt-5.4"]
        assert gateway.calls == 0



@pytest.mark.asyncio
async def test_another_chat_provider_keeps_the_gateway_unlisted(monkeypatch):
    """A gateway key set for embeddings next to a chat provider adds no chat model."""
    async with _database() as sessions:
        _env(
            monkeypatch,
            extra={"LLM_PROVIDER_OPENAI_API_KEY": "sk-openai", "LLM_PROVIDER_OPENAI_MODELS": "gpt-5.4"},
        )
        gateway = _Gateway(monkeypatch, LISTED)

        await _boot()

        assert not (await _gateway_row(sessions)).available_models
        assert gateway.calls == 0


@pytest.mark.asyncio
async def test_the_gateway_default_alias_stays_the_default(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch)
        _Gateway(monkeypatch, [*LISTED, "default"])

        await _boot()

        assert (await _gateway_row(sessions)).default_model == "default"
        async with sessions() as db:
            assert (await get_platform_settings(db)).agent_default_model == "default"


async def _agent_default(sessions) -> tuple[str | None, str | None]:
    async with sessions() as db:
        ps = await get_platform_settings(db)
        return ps.agent_default_provider, ps.agent_default_model


@pytest.mark.asyncio
async def test_the_gateway_chat_default_replaces_the_alias(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch)
        _Gateway(monkeypatch, [*LISTED, "default"], chat_default="claude-sonnet-5")

        await _boot()

        assert (await _gateway_row(sessions)).default_model == "claude-sonnet-5"
        assert await _agent_default(sessions) == ("filigran_gateway", "claude-sonnet-5")


@pytest.mark.asyncio
async def test_a_saved_alias_moves_to_the_gateway_chat_default(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch)
        gateway = _Gateway(monkeypatch, [*LISTED, "default"])
        await _boot()
        assert await _agent_default(sessions) == ("filigran_gateway", "default")

        gateway.chat_default = "gpt-5.4"
        await _boot()

        assert (await _gateway_row(sessions)).default_model == "gpt-5.4"
        assert await _agent_default(sessions) == ("filigran_gateway", "gpt-5.4")


@pytest.mark.asyncio
async def test_default_model_in_the_environment_wins_over_the_gateway_chat_default(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch, default_model="gpt-5.4")
        _Gateway(monkeypatch, [*LISTED, "default"], chat_default="claude-sonnet-5")

        await _boot()

        assert await _agent_default(sessions) == ("filigran_gateway", "gpt-5.4")


@pytest.mark.asyncio
async def test_an_admin_chat_default_wins_over_the_gateway_chat_default(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch)
        gateway = _Gateway(monkeypatch, [*LISTED, "default"])
        await _boot()
        async with sessions() as db:
            ps = await get_platform_settings(db)
            ps.agent_default_model = "gpt-5.4"
            await db.commit()

        gateway.chat_default = "claude-sonnet-5"
        await _boot()

        assert await _agent_default(sessions) == ("filigran_gateway", "gpt-5.4")


@pytest.mark.asyncio
async def test_a_default_model_on_another_provider_is_not_the_gateway_alias(monkeypatch):
    """Pins today's behaviour: only a saved alias on the gateway counts as unset."""
    async with _database() as sessions:
        _env(monkeypatch)
        gateway = _Gateway(monkeypatch, [*LISTED, "default"])
        await _boot()
        async with sessions() as db:
            other = LLMProviderConfig(
                user_id=None,
                provider="custom_openai",
                api_key_enc="x",
                default_model="default",
                available_models=["default"],
            )
            db.add(other)
            await db.flush()
            ps = await get_platform_settings(db)
            ps.agent_default_provider, ps.agent_default_provider_id = "custom_openai", other.id
            ps.agent_default_model = "default"
            await db.commit()

        gateway.chat_default = "claude-sonnet-5"
        await _boot()

        assert await _agent_default(sessions) == ("custom_openai", "default")


@pytest.mark.asyncio
async def test_a_chat_default_the_gateway_does_not_list_keeps_the_alias(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch)
        _Gateway(monkeypatch, [*LISTED, "default"], chat_default="gpt-retired")

        await _boot()

        assert (await _gateway_row(sessions)).default_model == "default"
        assert await _agent_default(sessions) == ("filigran_gateway", "default")


@pytest.mark.asyncio
async def test_an_alphabetical_pick_is_not_pinned_as_the_platform_default(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch)
        _Gateway(monkeypatch, LISTED)

        await _boot()

        async with sessions() as db:
            assert (await get_platform_settings(db)).agent_default_model is None


@pytest.mark.asyncio
async def test_a_shorter_listing_keeps_the_choices_made_on_a_hidden_model(monkeypatch):
    """A partial listing (an upstream down) must not clear what users picked."""
    async with _database() as sessions:
        _env(monkeypatch)
        gateway = _Gateway(monkeypatch, LISTED)
        await _boot()
        row = await _gateway_row(sessions)
        async with sessions() as db:
            user = User(
                email="ada@example.com",
                hashed_password="x",
                full_name="Ada",
                role="user",
                is_active=True,
                preferred_llm_provider="filigran_gateway",
                preferred_llm_provider_id=row.id,
                preferred_llm_model="gpt-5.4",
            )
            db.add(user)
            await db.commit()

        gateway.models = ["claude-sonnet-5"]
        await _boot()

        assert (await _gateway_row(sessions)).available_models == ["claude-sonnet-5"]
        async with sessions() as db:
            kept = (await db.execute(select(User).where(User.email == "ada@example.com"))).scalar_one()
            assert kept.preferred_llm_model == "gpt-5.4"


@pytest.mark.asyncio
async def test_editable_mode_does_not_refill_a_catalog_curated_to_empty(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch, editable=True)
        gateway = _Gateway(monkeypatch, LISTED)
        await _boot()
        async with sessions() as db:
            row = (
                await db.execute(select(LLMProviderConfig).where(LLMProviderConfig.provider == "filigran_gateway"))
            ).scalar_one()
            row.available_models = []
            row.default_model = None
            await db.commit()
        gateway.calls = 0

        await _boot()

        assert (await _gateway_row(sessions)).available_models == []
        assert gateway.calls == 0


@pytest.mark.asyncio
async def test_a_listed_gateway_keeps_refreshing_after_another_chat_provider(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch)
        gateway = _Gateway(monkeypatch, LISTED)
        await _boot()

        monkeypatch.setenv("LLM_PROVIDER_OPENAI_API_KEY", "sk-openai")
        monkeypatch.setenv("LLM_PROVIDER_OPENAI_MODELS", "gpt-5.4")
        gateway.models = ["claude-sonnet-5", "gemini-3-pro"]
        await _boot()

        assert (await _gateway_row(sessions)).available_models == ["claude-sonnet-5", "gemini-3-pro"]


@pytest.mark.asyncio
async def test_editable_mode_refreshes_a_listed_catalog_nobody_edited(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch, editable=True)
        gateway = _Gateway(monkeypatch, LISTED)
        await _boot()

        gateway.models = ["claude-sonnet-5", "gemini-3-pro"]
        await _boot()

        assert (await _gateway_row(sessions)).available_models == ["claude-sonnet-5", "gemini-3-pro"]
