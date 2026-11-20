"""An admin refreshes the env-seeded gateway's listed models without a restart.

The refresh runs the boot's provider seed, so it is tested against the same
real database and stubbed ``/v1/models`` as the boot listing.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.v1 import settings as settings_api
from app.api.v1.platform_settings_helpers import get_platform_settings
from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.llm_config import LLMProviderConfig
from app.models.model_catalog_announcement import ModelCatalogAnnouncement
from app.models.user import User
from tests.test_gateway_boot_model_listing_postgres import (
    CHAT,
    LISTED,
    _boot,
    _database,
    _env,
    _Gateway,
    _gateway_row,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _free_bootstrap_lock(monkeypatch):
    """The advisory lock opens the process-wide engine, not the scratch one."""

    @asynccontextmanager
    async def _lock(wait_timeout=None):
        yield

    monkeypatch.setattr("app.core.migrations.bootstrap_lock", _lock)


async def _admin(sessions, role: str = "admin") -> User:
    async with sessions() as db:
        admin = User(email=f"{role}@example.com", hashed_password="x", full_name=role, role=role, is_active=True)
        db.add(admin)
        await db.commit()
        await db.refresh(admin)
        return admin


async def _refresh(sessions, admin: User, provider_id) -> object:
    async with sessions() as db:
        return await settings_api.refresh_llm_provider_models(
            provider_id=str(provider_id), request=None, user=admin, db=db, _ai_gate=None
        )


async def _listed_flag(sessions, admin: User) -> bool:
    async with sessions() as db:
        providers = await settings_api.list_llm_providers(user=admin, db=db)
    return next(p.models_listed for p in providers if p.provider == "filigran_gateway")


async def _row(sessions, provider: str) -> LLMProviderConfig:
    async with sessions() as db:
        return (await db.execute(select(LLMProviderConfig).where(LLMProviderConfig.provider == provider))).scalar_one()


@pytest.mark.asyncio
async def test_a_pinned_listed_catalog_follows_the_gateway_on_refresh(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch)
        gateway = _Gateway(monkeypatch, LISTED)
        await _boot()
        admin = await _admin(sessions)
        row = await _gateway_row(sessions)
        assert await _listed_flag(sessions, admin)

        gateway.models = ["gpt-5.4", "gemini-3-pro"]
        response = await _refresh(sessions, admin, row.id)

        assert response.available_models == ["gemini-3-pro", "gpt-5.4"]
        row = await _gateway_row(sessions)
        assert row.available_models == ["gemini-3-pro", "gpt-5.4"]
        assert row.env_models == ["gemini-3-pro", "gpt-5.4"]


@pytest.mark.asyncio
async def test_an_editable_catalog_nobody_edited_is_refreshed(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch, editable=True)
        gateway = _Gateway(monkeypatch, LISTED)
        await _boot()
        admin = await _admin(sessions)
        row = await _gateway_row(sessions)

        gateway.models = ["claude-sonnet-5", "gemini-3-pro"]
        await _refresh(sessions, admin, row.id)

        assert (await _gateway_row(sessions)).available_models == ["claude-sonnet-5", "gemini-3-pro"]


@pytest.mark.asyncio
async def test_an_admin_edited_catalog_is_not_refreshed(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch, editable=True)
        gateway = _Gateway(monkeypatch, LISTED)
        await _boot()
        admin = await _admin(sessions)
        async with sessions() as db:
            row = (
                await db.execute(select(LLMProviderConfig).where(LLMProviderConfig.provider == "filigran_gateway"))
            ).scalar_one()
            row.available_models = ["gpt-5.4"]
            row.default_model = "gpt-5.4"
            await db.commit()
        assert not await _listed_flag(sessions, admin)
        gateway.calls = 0

        with pytest.raises(HTTPException) as refused:
            await _refresh(sessions, admin, row.id)

        assert refused.value.status_code == 409
        assert gateway.calls == 0
        assert (await _gateway_row(sessions)).available_models == ["gpt-5.4"]


@pytest.mark.asyncio
async def test_a_models_list_in_the_environment_is_not_refreshed(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch, models="gpt-5.4")
        gateway = _Gateway(monkeypatch, LISTED)
        await _boot()
        admin = await _admin(sessions)
        row = await _gateway_row(sessions)
        assert not await _listed_flag(sessions, admin)

        with pytest.raises(HTTPException) as refused:
            await _refresh(sessions, admin, row.id)

        assert refused.value.status_code == 409
        assert gateway.calls == 0


@pytest.mark.asyncio
async def test_a_gateway_kept_unlisted_next_to_a_chat_provider_is_not_refreshed(monkeypatch):
    async with _database() as sessions:
        _env(
            monkeypatch,
            extra={"LLM_PROVIDER_OPENAI_API_KEY": "sk-openai", "LLM_PROVIDER_OPENAI_MODELS": "gpt-5.4"},
        )
        gateway = _Gateway(monkeypatch, LISTED)
        await _boot()
        admin = await _admin(sessions)
        row = await _gateway_row(sessions)
        assert not await _listed_flag(sessions, admin)

        with pytest.raises(HTTPException) as refused:
            await _refresh(sessions, admin, row.id)

        assert refused.value.status_code == 409
        assert gateway.calls == 0


@pytest.mark.asyncio
async def test_a_failed_listing_is_reported_and_keeps_the_models(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch)
        gateway = _Gateway(monkeypatch, LISTED)
        await _boot()
        admin = await _admin(sessions)
        row = await _gateway_row(sessions)

        gateway.models = RuntimeError("gateway down")
        with pytest.raises(HTTPException) as failed:
            await _refresh(sessions, admin, row.id)

        assert failed.value.status_code == 502
        row = await _gateway_row(sessions)
        assert row.available_models == CHAT
        assert row.env_models == CHAT


@pytest.mark.asyncio
async def test_a_busy_bootstrap_lock_asks_to_retry(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch)
        gateway = _Gateway(monkeypatch, LISTED)
        await _boot()
        admin = await _admin(sessions)
        row = await _gateway_row(sessions)

        @asynccontextmanager
        async def _busy(wait_timeout=None):
            raise TimeoutError
            yield

        monkeypatch.setattr("app.core.migrations.bootstrap_lock", _busy)
        gateway.models = ["gpt-5.4"]
        with pytest.raises(HTTPException) as busy:
            await _refresh(sessions, admin, row.id)

        assert busy.value.status_code == 503
        assert (await _gateway_row(sessions)).available_models == CHAT


@pytest.mark.asyncio
async def test_only_the_gateway_list_is_refreshed(monkeypatch):
    """The rest of the env configuration waits for a restart, as before."""
    async with _database() as sessions:
        # An embedding-only provider beside it leaves the gateway the sole chat provider.
        _env(
            monkeypatch,
            extra={"LLM_PROVIDER_OPENAI_API_KEY": "sk-openai", "LLM_PROVIDER_OPENAI_MODELS": "text-embedding-3-large"},
        )
        gateway = _Gateway(monkeypatch, LISTED)
        await _boot()
        admin = await _admin(sessions)
        row = await _gateway_row(sessions)

        monkeypatch.setenv("LLM_PROVIDER_OPENAI_MODELS", "text-embedding-3-small")
        gateway.models = ["gpt-5.4"]
        await _refresh(sessions, admin, row.id)

        assert (await _gateway_row(sessions)).available_models == ["gpt-5.4"]
        assert (await _row(sessions, "openai")).available_models == ["text-embedding-3-large"]
        assert gateway.calls == 2  # the boot's listing, then the refresh's only


@pytest.mark.asyncio
async def test_a_refresh_is_audited(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch)
        gateway = _Gateway(monkeypatch, LISTED)
        await _boot()
        admin = await _admin(sessions)
        row = await _gateway_row(sessions)

        gateway.models = ["gpt-5.4"]
        await _refresh(sessions, admin, row.id)

        async with sessions() as db:
            entry = (await db.execute(select(AuditLog).where(AuditLog.entity_type == "llm_provider"))).scalar_one()
        assert entry.user_id == admin.id
        assert entry.details["removed_models"] == ["claude-sonnet-5"]


@pytest.mark.asyncio
async def test_a_refresh_records_the_catalog_change_for_the_users(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch)
        gateway = _Gateway(monkeypatch, LISTED)
        await _boot()
        admin = await _admin(sessions)
        row = await _gateway_row(sessions)

        gateway.models = [*CHAT, "gemini-3-pro"]
        await _refresh(sessions, admin, row.id)

        async with sessions() as db:
            latest = (
                (
                    await db.execute(
                        select(ModelCatalogAnnouncement).order_by(ModelCatalogAnnouncement.created_at.desc())
                    )
                )
                .scalars()
                .first()
            )
        assert {m["id"] for m in latest.models} == {*CHAT, "gemini-3-pro"}


@pytest.mark.asyncio
async def test_env_only_mode_allows_the_refresh(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch)
        monkeypatch.setattr(settings, "models_env_providing_only", True)
        gateway = _Gateway(monkeypatch, LISTED)
        await _boot()
        admin = await _admin(sessions)
        row = await _gateway_row(sessions)
        assert await _listed_flag(sessions, admin)

        gateway.models = ["gpt-5.4"]
        await _refresh(sessions, admin, row.id)

        assert (await _gateway_row(sessions)).available_models == ["gpt-5.4"]


@pytest.mark.asyncio
async def test_someone_not_an_admin_is_not_offered_the_refresh(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch)
        _Gateway(monkeypatch, LISTED)
        await _boot()
        member = await _admin(sessions, role="user")

        assert not await _listed_flag(sessions, member)


@pytest.mark.asyncio
async def test_a_refresh_drops_the_cached_providers(monkeypatch):
    """Like a catalog edit: refusals and warnings learned on the old catalog go."""
    async with _database() as sessions:
        _env(monkeypatch)
        gateway = _Gateway(monkeypatch, LISTED)
        await _boot()
        admin = await _admin(sessions)
        row = await _gateway_row(sessions)
        cleared = []
        monkeypatch.setattr("app.llm.router.invalidate_provider_cache", lambda: cleared.append(True))

        gateway.models = ["gpt-5.4"]
        await _refresh(sessions, admin, row.id)

        assert cleared


async def _agent_default(sessions) -> tuple[str | None, str | None]:
    async with sessions() as db:
        ps = await get_platform_settings(db)
        return ps.agent_default_provider, ps.agent_default_model


@pytest.mark.asyncio
async def test_a_refresh_after_a_failed_first_listing_fills_the_platform_default(monkeypatch):
    """Gateway down at first boot: the refresh pins what that boot would have."""
    async with _database() as sessions:
        _env(monkeypatch, default_model="gpt-5.4")
        gateway = _Gateway(monkeypatch, RuntimeError("gateway down"))
        await _boot()
        admin = await _admin(sessions)
        row = await _gateway_row(sessions)
        assert await _agent_default(sessions) == (None, None)

        gateway.models = LISTED
        await _refresh(sessions, admin, row.id)

        assert await _agent_default(sessions) == ("filigran_gateway", "gpt-5.4")


@pytest.mark.asyncio
async def test_the_gateway_default_alias_fills_the_platform_default_on_refresh(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch)
        gateway = _Gateway(monkeypatch, RuntimeError("gateway down"))
        await _boot()
        admin = await _admin(sessions)
        row = await _gateway_row(sessions)

        gateway.models = [*LISTED, "default"]
        await _refresh(sessions, admin, row.id)

        assert await _agent_default(sessions) == ("filigran_gateway", "default")


@pytest.mark.asyncio
async def test_a_refresh_does_not_pin_an_alphabetical_pick_platform_wide(monkeypatch):
    async with _database() as sessions:
        _env(monkeypatch)
        gateway = _Gateway(monkeypatch, RuntimeError("gateway down"))
        await _boot()
        admin = await _admin(sessions)
        row = await _gateway_row(sessions)

        gateway.models = LISTED
        await _refresh(sessions, admin, row.id)

        assert (await _agent_default(sessions))[1] is None
