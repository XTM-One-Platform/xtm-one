"""Orphan cleanup must not wipe a choice pinned to a config that still exists.

A real database: the whole question is which stored rows a delete or a catalog
shrink touches, and the agent / user half of that is a SQL ``WHERE`` clause the
in-process tests could not see.

Two configs can advertise the same model — a gateway fronting a vendor beside
that vendor's own key — so neither the provider *name* nor the mere presence of
a model id in ``removed_models`` identifies whose choice is being orphaned.
Only the stored reference does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 — registers every mapper
from app.api.v1.platform_settings_helpers import get_platform_settings
from app.api.v1.settings import _cleanup_orphaned_model_references
from app.models.agent import Agent
from app.models.llm_config import LLMProviderConfig

SHARED = "claude-sonnet-4-6"

from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("provref", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _two_overlapping_configs(db):
    """Configs A (own key) and B (gateway), both advertising ``SHARED``."""
    a = LLMProviderConfig(
        provider="anthropic", api_key_enc="x", available_models=[SHARED]
    )
    b = LLMProviderConfig(
        provider="anthropic",
        display_name="Gateway",
        api_key_enc="y",
        available_models=[SHARED, "claude-opus-4-8"],
    )
    db.add_all([a, b])
    await db.flush()
    return a, b


@pytest.mark.asyncio
async def test_delete_keeps_a_platform_choice_pinned_to_a_surviving_config():
    """The fallback is pinned to A; deleting B must leave it alone. ``B``'s
    catalog contains the model, so the model clause fired on it regardless of
    the reference — and failover silently dropped to provider defaults."""
    async with _temp_database() as session_factory, session_factory() as db:
        a, b = await _two_overlapping_configs(db)
        ps = await get_platform_settings(db)
        ps.fallback_model = SHARED
        ps.fallback_provider_id = a.id
        await db.flush()

        await _cleanup_orphaned_model_references(
            db,
            b.provider,
            set(b.available_models),
            provider_deleted=True,
            provider_id=b.id,
        )

        assert ps.fallback_model == SHARED
        assert ps.fallback_provider_id == a.id


@pytest.mark.asyncio
async def test_delete_clears_a_platform_choice_pinned_to_the_deleted_config():
    """The other half of the same rule: the reference names the config going
    away, so the choice really is orphaned."""
    async with _temp_database() as session_factory, session_factory() as db:
        _a, b = await _two_overlapping_configs(db)
        ps = await get_platform_settings(db)
        ps.fallback_model = SHARED
        ps.fallback_provider_id = b.id
        await db.flush()

        await _cleanup_orphaned_model_references(
            db,
            b.provider,
            set(b.available_models),
            provider_deleted=True,
            provider_id=b.id,
        )

        assert ps.fallback_model is None
        assert ps.fallback_provider_id is None


@pytest.mark.asyncio
async def test_delete_keeps_an_agent_pinned_to_a_surviving_config():
    """The agent clause matched on the provider *name*, which both configs
    share, so deleting either one reset every agent of that vendor."""
    async with _temp_database() as session_factory, session_factory() as db:
        a, b = await _two_overlapping_configs(db)
        agent = Agent(
            name="pinned",
            llm_provider="anthropic",
            llm_provider_id=a.id,
            llm_model=SHARED,
        )
        db.add(agent)
        await db.flush()

        await _cleanup_orphaned_model_references(
            db,
            b.provider,
            set(b.available_models),
            provider_deleted=True,
            provider_id=b.id,
        )

        assert agent.llm_provider_id == a.id
        assert agent.llm_model == SHARED


@pytest.mark.asyncio
async def test_delete_still_clears_an_agent_with_no_reference():
    """Every choice made before the reference columns existed stores only a
    name, and must keep its pre-reference behaviour."""
    async with _temp_database() as session_factory, session_factory() as db:
        _a, b = await _two_overlapping_configs(db)
        agent = Agent(name="legacy", llm_provider="anthropic", llm_model=SHARED)
        db.add(agent)
        await db.flush()

        await _cleanup_orphaned_model_references(
            db,
            b.provider,
            set(b.available_models),
            provider_deleted=True,
            provider_id=b.id,
        )

        assert agent.llm_provider is None
        assert agent.llm_model is None


@pytest.mark.asyncio
async def test_catalog_shrink_only_touches_choices_pinned_to_that_config():
    """``removed_models`` describes one config's catalog. On a shrink there is
    no deleted provider at all, so the model clause is the only one — and
    without the reference check it reached choices pinned elsewhere."""
    async with _temp_database() as session_factory, session_factory() as db:
        a, b = await _two_overlapping_configs(db)
        ps = await get_platform_settings(db)
        ps.fallback_model = SHARED
        ps.fallback_provider_id = a.id
        pinned = Agent(
            name="pinned", llm_provider="anthropic",
            llm_provider_id=a.id, llm_model=SHARED,
        )
        dropped = Agent(
            name="dropped", llm_provider="anthropic",
            llm_provider_id=b.id, llm_model=SHARED,
        )
        db.add_all([pinned, dropped])
        await db.flush()

        # B stops advertising the shared model; A still does.
        await _cleanup_orphaned_model_references(
            db, b.provider, {SHARED}, provider_id=b.id
        )

        assert ps.fallback_model == SHARED
        assert ps.fallback_provider_id == a.id
        assert pinned.llm_model == SHARED
        assert dropped.llm_model is None
