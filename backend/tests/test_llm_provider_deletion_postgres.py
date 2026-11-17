"""Deleting a provider against a real database: the preview is what gets applied.

The in-process tests pin the rule; this one pins the parts only a database can
show - which agent and user rows the orphan clauses match, that the re-seed
lands in the same transaction as the cleanup and is committed, that the audit
row records where traffic moved, and that the implicit fallback no longer
depends on the order rows happen to come back in.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 — registers every mapper
from app.api.v1 import agents as agents_api
from app.api.v1 import settings as settings_api
from app.api.v1.platform_settings_helpers import get_platform_settings, peek_platform_settings
from app.api.v1.schemas.agent import CreateAgentRequest, UpdateAgentRequest
from app.api.v1.schemas.settings import (
    GeneralAssistantConfigRequest,
    LLMProviderRequest,
    LLMProviderUpdateRequest,
)
from app.api.v1.settings import delete_llm_provider, get_llm_provider_deletion_impact
from app.core.security import encrypt_value
from app.llm import router
from app.models.activity import Activity
from app.models.agent import Agent
from app.models.audit_log import AuditLog
from app.models.embedding import Embedding
from app.models.general_assistant_config import (
    DEFAULT_GA_BUILTIN_TOOL_IDS,
    GeneralAssistantConfig,
    default_system_prompt,
)
from app.models.knowledge_base import KnowledgeBase
from app.models.llm_config import LLMProviderConfig
from app.models.platform_settings import PlatformSettings
from app.models.user import User
from app.services.llm_provider_deletion import clear_deleted_embedding_provider_references
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("provdel", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


def _config(provider: str, models: list[str], *, minutes: int, **extra) -> LLMProviderConfig:
    return LLMProviderConfig(
        provider=provider,
        api_key_enc=encrypt_value("sk-test"),
        available_models=models,
        created_at=_T0 + timedelta(minutes=minutes),
        **extra,
    )


async def _seed(session_factory):
    """An OpenAI config serving every route, and the configs that survive it."""
    async with session_factory() as db:
        admin = User(
            email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",
            full_name="Deletion Admin",
            role="admin",
            is_active=True,
        )
        tei = _config("tei", ["bge-m3"], minutes=-1)
        anthropic = _config("anthropic", ["claude-sonnet-5"], minutes=0, display_name="Claude")
        backup = _config("openai", ["gpt-4o"], minutes=1, display_name="Backup")
        doomed = _config(
            "openai",
            ["gpt-5.1", "gpt-image-1", "text-embedding-3-small"],
            minutes=2,
            display_name="Production",
            default_model="gpt-5.1",
        )
        db.add_all([admin, tei, anthropic, backup, doomed])
        await db.flush()

        ps = await get_platform_settings(db)
        ps.agent_default_provider = "openai"
        ps.agent_default_provider_id = doomed.id
        ps.agent_default_model = "gpt-5.1"
        ps.image_provider = "openai"
        ps.image_provider_id = doomed.id
        ps.image_model = "gpt-image-1"
        ps.embedding_provider = "openai"
        ps.embedding_provider_id = doomed.id
        ps.embedding_model = "text-embedding-3-small"
        ps.routing_model = "gpt-5.1"
        ps.routing_provider_id = doomed.id
        ps.decision_model = "clef-flash"
        ps.decision_provider_id = doomed.id
        db.add(
            GeneralAssistantConfig(
                llm_provider="openai",
                llm_provider_id=doomed.id,
                llm_model="gpt-5.1",
                system_prompt=default_system_prompt(),
                available_channel_types=["web"],
                builtin_tool_ids=list(DEFAULT_GA_BUILTIN_TOOL_IDS),
            )
        )
        pinned = Agent(name="Pinned", llm_provider="openai", llm_provider_id=doomed.id, llm_model="gpt-5.1")
        # A choice saved before provider references existed: the name is all it has.
        legacy = Agent(name="Legacy", llm_provider="openai", llm_model="gpt-5.1")
        elsewhere = Agent(
            name="Elsewhere", llm_provider="openai", llm_provider_id=backup.id, llm_model="gpt-4o"
        )
        user = User(
            email=f"user-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",
            full_name="Ada",
            role="user",
            is_active=True,
            preferred_llm_provider="openai",
            preferred_llm_provider_id=doomed.id,
            preferred_llm_model="gpt-5.1",
        )
        kb = KnowledgeBase(name="Wiki", user_id=None, is_company_managed=True, chunk_count=3)
        db.add_all([pinned, legacy, elsewhere, user, kb])
        await db.commit()
        return {
            "admin": admin,
            "tei": tei,
            "anthropic": anthropic,
            "backup": backup,
            "doomed": doomed,
            "pinned": pinned,
            "legacy": legacy,
            "elsewhere": elsewhere,
            "user": user,
        }


async def _preview(session_factory, rows, replacement=None):
    async with session_factory() as db:
        admin = await db.get(User, rows["admin"].id)
        return await get_llm_provider_deletion_impact(
            provider_id=str(rows["doomed"].id),
            replacement_provider_id=replacement,
            user=admin,
            db=db,
            _ai_gate=None,
        )


async def _delete(session_factory, rows, replacement=None, *, expected=None):
    await _delete_config(session_factory, rows, rows["doomed"].id, replacement, expected=expected)


async def _delete_config(session_factory, rows, config_id, replacement=None, *, expected=None):
    async with session_factory() as db:
        admin = await db.get(User, rows["admin"].id)
        await delete_llm_provider(
            provider_id=str(config_id),
            request=None,
            replacement_provider_id=str(replacement) if replacement else None,
            expected_impact=expected,
            user=admin,
            db=db,
            _ai_gate=None,
        )


async def _state(session_factory):
    async with session_factory() as db:
        ps = (await db.execute(select(PlatformSettings))).scalar_one()
        ga = (await db.execute(select(GeneralAssistantConfig))).scalar_one()
        agents = {a.name: a for a in (await db.execute(select(Agent))).scalars().all()}
        users = {u.full_name: u for u in (await db.execute(select(User))).scalars().all()}
        audit = (
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.entity_type == "llm_provider", AuditLog.action == "delete"
                    )
                )
            )
            .scalars()
            .all()
        )
        configs = (await db.execute(select(LLMProviderConfig))).scalars().all()
        return ps, ga, agents, users, audit, configs


@pytest.mark.asyncio
async def test_the_delete_applies_exactly_what_the_preview_named():
    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        anthropic, backup = rows["anthropic"], rows["backup"]

        preview = await _preview(session_factory, rows)

        # The oldest surviving chat-capable config; the older TEI config cannot chat.
        assert preview.replacement.provider_id == str(anthropic.id)
        assert preview.replacement.model == "claude-sonnet-5"
        assert preview.agent_default.action == "reseeded"
        # Claude does not offer gpt-5.1: the chat routes take its default model.
        assert preview.agent_default.model_kept is False
        assert preview.general_assistant.action == "reseeded"
        assert preview.image_default.after.provider_id == str(backup.id)
        assert preview.image_default.model_kept is True
        assert preview.embedding_default.action == "cleared"
        assert preview.embedding_default.fallback.provider_id == str(backup.id)
        assert preview.embedding_default.knowledge_base_count == 1
        assert preview.routing_model.action == "cleared"
        assert preview.decision_model.action == "cleared"
        # The pinned agent and the name-only legacy one; not the one on Backup.
        assert (preview.agents.count, preview.agents.names) == (2, ["Legacy", "Pinned"])
        assert (preview.users.count, preview.users.names) == (1, ["Ada"])

        # What the dialog does: the rule's pick previewed, confirmed by name
        # with the fingerprint it showed.
        await _delete(
            session_factory,
            rows,
            preview.replacement.provider_id,
            expected=preview.impact_fingerprint,
        )
        ps, ga, agents, users, audit, configs = await _state(session_factory)

        assert rows["doomed"].id not in {c.id for c in configs}
        # Re-seeded AND pinned by id, as previewed.
        assert (ps.agent_default_provider, ps.agent_default_provider_id, ps.agent_default_model) == (
            "anthropic",
            anthropic.id,
            "claude-sonnet-5",
        )
        assert (ga.llm_provider, ga.llm_provider_id, ga.llm_model) == (
            "anthropic",
            anthropic.id,
            "claude-sonnet-5",
        )
        assert (ps.image_provider, ps.image_provider_id, ps.image_model) == (
            "openai",
            backup.id,
            "gpt-image-1",
        )
        # The embedding default is cleared, never switched to the fallback.
        assert (ps.embedding_provider, ps.embedding_provider_id, ps.embedding_model) == (None, None, None)
        assert (ps.routing_model, ps.routing_provider_id) == (None, None)
        assert (ps.decision_model, ps.decision_provider_id) == (None, None)
        # Pinned choices are reset so they inherit the defaults; others stay.
        assert agents["Pinned"].llm_model is None and agents["Pinned"].llm_provider_id is None
        assert agents["Legacy"].llm_model is None
        assert agents["Elsewhere"].llm_provider_id == backup.id
        assert users["Ada"].preferred_llm_model is None

        [entry] = audit
        assert entry.details["replacement"]["provider_id"] == str(anthropic.id)
        assert entry.details["replacement"]["model"] == "claude-sonnet-5"
        assert entry.details["replacement"]["chosen_by"] == "admin"
        assert entry.details["routes"]["agent_default"]["after"]["provider_id"] == str(anthropic.id)
        assert entry.details["routes"]["agent_default"]["model_kept"] is False
        assert entry.details["routes"]["embedding_default"]["action"] == "cleared"
        assert entry.details["routes"]["agent_default"]["env_managed"] is False
        assert entry.details["is_from_env"] is False
        assert entry.details["agents_reset"] == 2
        assert entry.details["references_reset"]["agents"] == 2
        assert "claude-sonnet-5" in entry.summary


@pytest.mark.asyncio
async def test_reset_agents_and_users_now_resolve_to_the_new_default():
    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        await _delete(session_factory, rows)
        router.invalidate_provider_cache()

        async with session_factory() as db:
            pinned = (await db.execute(select(Agent).where(Agent.name == "Pinned"))).scalar_one()
            provider, model = await router.get_llm_for_user(
                db,
                str(rows["user"].id),
                model_override=pinned.llm_model,
                provider_id_override=pinned.llm_provider_id,
            )

        assert provider.provider_name == "anthropic"
        assert model == "claude-sonnet-5"


@pytest.mark.asyncio
async def test_an_explicit_replacement_is_honoured():
    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        backup = rows["backup"]

        preview = await _preview(session_factory, rows, str(backup.id))
        assert preview.replacement_explicit is True
        assert preview.default_replacement_id == str(rows["anthropic"].id)
        assert preview.agent_default.after.model == "gpt-4o"

        await _delete(session_factory, rows, str(backup.id))
        ps, ga, *_ = await _state(session_factory)

        assert (ps.agent_default_provider_id, ps.agent_default_model) == (backup.id, "gpt-4o")
        assert (ga.llm_provider_id, ga.llm_model) == (backup.id, "gpt-4o")
        assert ps.image_provider_id == backup.id


@pytest.mark.asyncio
async def test_a_replacement_offering_the_model_keeps_it_in_the_preview_and_the_delete():
    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        backup = rows["backup"]
        async with session_factory() as db:
            stored = await db.get(LLMProviderConfig, backup.id)
            stored.available_models = ["gpt-4o", "gpt-5.1"]
            await db.commit()

        preview = await _preview(session_factory, rows, str(backup.id))
        for route in (preview.agent_default, preview.general_assistant):
            assert (route.after.provider_id, route.after.model) == (str(backup.id), "gpt-5.1")
            assert route.model_kept is True

        await _delete(session_factory, rows, str(backup.id))
        ps, ga, _agents, _users, audit, _configs = await _state(session_factory)

        assert (ps.agent_default_provider_id, ps.agent_default_model) == (backup.id, "gpt-5.1")
        assert (ga.llm_provider_id, ga.llm_model) == (backup.id, "gpt-5.1")
        [entry] = audit
        assert entry.details["routes"]["agent_default"]["model_kept"] is True
        assert entry.details["routes"]["general_assistant"]["after"]["model"] == "gpt-5.1"
        assert "platform default keeps gpt-5.1" in entry.summary


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["embedding_only", "unknown", "itself", "malformed"])
async def test_an_unusable_replacement_is_a_422_and_deletes_nothing(which):
    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        replacement = {
            "embedding_only": str(rows["tei"].id),
            "unknown": str(uuid.uuid4()),
            "itself": str(rows["doomed"].id),
            "malformed": "not-a-uuid",
        }[which]

        with pytest.raises(HTTPException) as preview_exc:
            await _preview(session_factory, rows, replacement)
        with pytest.raises(HTTPException) as delete_exc:
            await _delete(session_factory, rows, replacement)

        assert preview_exc.value.status_code == 422
        assert delete_exc.value.status_code == 422
        *_, configs = await _state(session_factory)
        assert rows["doomed"].id in {c.id for c in configs}


@pytest.mark.asyncio
async def test_deleting_the_last_provider_reseeds_nothing_and_still_deletes():
    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        async with session_factory() as db:
            for key in ("tei", "anthropic", "backup"):
                await db.delete(await db.get(LLMProviderConfig, rows[key].id))
            await db.commit()

        preview = await _preview(session_factory, rows)
        assert preview.chat_available is False
        assert preview.replacement is None
        assert preview.agent_default.action == "cleared"

        await _delete(session_factory, rows)
        ps, ga, *_, configs = await _state(session_factory)

        assert configs == []
        assert ps.agent_default_model is None and ps.agent_default_provider_id is None
        assert ga.llm_model is None


@pytest.mark.asyncio
async def test_the_implicit_fallback_does_not_depend_on_row_order():
    """Rows inserted newest first: an unordered read returned the newest; the
    ordered read returns the oldest, the config Settings lists first."""
    async with _temp_database() as session_factory:
        async with session_factory() as db:
            db.add(_config("openai", ["gpt-5.1"], minutes=5))
            await db.flush()
            db.add(_config("anthropic", ["claude-sonnet-5"], minutes=0))
            await db.commit()
        router.invalidate_provider_cache()

        async with session_factory() as db:
            picked = await router.get_llm_provider("user", db)

        assert picked.provider_name == "anthropic"


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [True, False], ids=["admin-picked", "rule-picked"])
async def test_concurrent_deletions_never_reseed_onto_a_deleted_provider(monkeypatch, explicit):
    """Two admins delete two providers at once, each the other's replacement.

    The first deletion decides its replacement (Claude) and is held there,
    uncommitted, while the second deletes Claude. Deciding from the same
    snapshot, the second leaves the platform default alone (it still names
    Production) and the first then re-seeds it onto Claude, which no longer
    exists. Serialized, the second deletion decides from what the first
    committed: an admin-picked Production is refused (it is gone), the rule
    moves everything on to Backup.
    """
    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        doomed, anthropic, backup = rows["doomed"], rows["anthropic"], rows["backup"]
        async with session_factory() as db:
            ga = (await db.execute(select(GeneralAssistantConfig))).scalar_one()
            ga.llm_provider, ga.llm_provider_id, ga.llm_model = (
                "anthropic",
                anthropic.id,
                "claude-sonnet-5",
            )
            await db.commit()

        first_decided = asyncio.Event()
        release_first = asyncio.Event()
        cleanup = settings_api._cleanup_orphaned_model_references

        async def held_cleanup(db, provider_name, removed_models, **kwargs):
            if kwargs.get("provider_id") == doomed.id:
                first_decided.set()
                await release_first.wait()
            return await cleanup(db, provider_name, removed_models, **kwargs)

        monkeypatch.setattr(settings_api, "_cleanup_orphaned_model_references", held_cleanup)

        first = asyncio.create_task(
            _delete_config(session_factory, rows, doomed.id, anthropic.id if explicit else None)
        )
        await asyncio.wait_for(first_decided.wait(), timeout=10)
        second = asyncio.create_task(
            _delete_config(session_factory, rows, anthropic.id, doomed.id if explicit else None)
        )
        # Racing, the second deletion commits while the first is held; serialized,
        # it waits for the first to commit.
        await asyncio.wait({second}, timeout=2)
        release_first.set()
        first_result, second_result = await asyncio.gather(first, second, return_exceptions=True)

        ps, ga, *_, configs = await _state(session_factory)
        existing = {c.id for c in configs}
        references = {
            "agent_default": ps.agent_default_provider_id,
            "general_assistant": ga.llm_provider_id,
            "image_default": ps.image_provider_id,
            "embedding_default": ps.embedding_provider_id,
            "routing_model": ps.routing_provider_id,
            "fallback_model": ps.fallback_provider_id,
            "decision_model": ps.decision_provider_id,
        }
        dangling = {
            route: ref for route, ref in references.items() if ref is not None and ref not in existing
        }
        assert dangling == {}
        assert first_result is None
        if explicit:
            assert isinstance(second_result, HTTPException)
            assert second_result.status_code == 422
            assert anthropic.id in existing
            assert (ps.agent_default_provider_id, ga.llm_provider_id) == (anthropic.id, anthropic.id)
        else:
            assert second_result is None
            assert (ps.agent_default_provider_id, ga.llm_provider_id) == (backup.id, backup.id)


@pytest.mark.asyncio
async def test_a_preview_another_change_overtook_is_refused_and_nothing_is_deleted():
    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        backup, doomed = rows["backup"], rows["doomed"]
        preview = await _preview(session_factory, rows, str(backup.id))
        assert preview.agent_default.model_kept is False

        # Meanwhile Backup starts offering gpt-5.1: the defaults would now keep it.
        async with session_factory() as db:
            stored = await db.get(LLMProviderConfig, backup.id)
            stored.available_models = ["gpt-4o", "gpt-5.1"]
            await db.commit()

        with pytest.raises(HTTPException) as exc:
            await _delete(session_factory, rows, backup.id, expected=preview.impact_fingerprint)

        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "deletion_impact_changed"
        fresh = exc.value.detail["impact"]
        assert fresh["agent_default"]["model_kept"] is True
        assert fresh["impact_fingerprint"] != preview.impact_fingerprint
        _ps, _ga, _agents, _users, audit, configs = await _state(session_factory)
        assert doomed.id in {c.id for c in configs}
        assert audit == []

        # Confirming the fresh preview applies it.
        await _delete(session_factory, rows, backup.id, expected=fresh["impact_fingerprint"])
        ps, ga, *_ = await _state(session_factory)
        assert (ps.agent_default_provider_id, ps.agent_default_model) == (backup.id, "gpt-5.1")
        assert (ga.llm_provider_id, ga.llm_model) == (backup.id, "gpt-5.1")


@pytest.mark.asyncio
async def test_a_replacement_default_it_does_not_list_is_never_where_traffic_lands():
    """A gateway whose ``default_model`` (gpt-4o) is not in its catalog, next to a
    surviving OpenAI config: re-seeded on gpt-4o, the router re-routed the pinned
    default to the OpenAI config that owns the model. Re-seeded on a model the
    gateway lists, the traffic stays where the preview says."""
    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        async with session_factory() as db:
            gateway = _config(
                "openrouter",
                ["openai/gpt-4o-mini", "anthropic/claude-sonnet-5"],
                minutes=-5,
                display_name="Gateway",
                default_model="gpt-4o",
            )
            db.add(gateway)
            await db.commit()

        preview = await _preview(session_factory, rows, str(gateway.id))
        assert (preview.agent_default.after.provider_id, preview.agent_default.after.model) == (
            str(gateway.id),
            "openai/gpt-4o-mini",
        )
        assert preview.replacement.model == "openai/gpt-4o-mini"

        await _delete(session_factory, rows, gateway.id, expected=preview.impact_fingerprint)
        router.invalidate_provider_cache()
        async with session_factory() as db:
            provider, model = await router.get_llm_for_user(db, str(rows["admin"].id))
            ps = (await db.execute(select(PlatformSettings))).scalar_one()

        assert (ps.agent_default_provider_id, ps.agent_default_model) == (gateway.id, "openai/gpt-4o-mini")
        assert model == "openai/gpt-4o-mini"
        assert provider.provider_name == "openrouter"


@pytest.mark.asyncio
async def test_a_replacement_listing_no_model_is_refused_for_a_default_that_runs_on_one():
    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        async with session_factory() as db:
            catalog_less = _config("azure_openai", [], minutes=-5, display_name="Azure")
            catalog_less.available_models = None
            db.add(catalog_less)
            await db.commit()

        preview = await _preview(session_factory, rows)
        # The oldest survivor lists nothing: it is named as withheld, never picked.
        assert [t.provider_id for t in preview.unlisted] == [str(catalog_less.id)]
        assert preview.replacement.provider_id == str(rows["anthropic"].id)
        with pytest.raises(HTTPException) as exc:
            await _delete(session_factory, rows, catalog_less.id)
        assert exc.value.status_code == 422
        assert "lists no model" in exc.value.detail


@pytest.mark.asyncio
async def test_the_env_prune_applies_the_deletion_rule_and_audits_it(monkeypatch):
    """Removing a provider's environment variable deletes its config at the next
    boot (or AI re-enable): the defaults it held move by the same rule as an
    admin's delete, and a system-actor audit entry records where."""
    from app.main import _prune_orphaned_env_providers

    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        async with session_factory() as db:
            doomed = await db.get(LLMProviderConfig, rows["doomed"].id)
            doomed.is_from_env = True
            await db.commit()

        await _prune_orphaned_env_providers(set())

        ps, ga, agents, _users, _audit, configs = await _state(session_factory)
        anthropic = rows["anthropic"]
        assert rows["doomed"].id not in {c.id for c in configs}
        assert (ps.agent_default_provider_id, ps.agent_default_model) == (anthropic.id, "claude-sonnet-5")
        assert (ga.llm_provider_id, ga.llm_model) == (anthropic.id, "claude-sonnet-5")
        assert agents["Pinned"].llm_provider_id is None
        async with session_factory() as db:
            [entry] = (
                (await db.execute(select(AuditLog).where(AuditLog.entity_type == "llm_provider")))
                .scalars()
                .all()
            )
        assert entry.user_email == "system:env_provider_prune"
        assert entry.user_id == rows["admin"].id
        assert entry.details["replacement"]["provider_id"] == str(anthropic.id)
        assert entry.details["replacement"]["chosen_by"] == "rule"
        assert entry.details["is_from_env"] is True
        assert "environment variable was removed" in entry.summary


async def _seed_gateway_held_defaults(session_factory, *, survivor: str, survivor_models: list[str]):
    """An env-provisioned gateway config holding the embedding, routing and
    fallback defaults, and an administrator's *survivor* row offering *survivor_models*."""
    async with session_factory() as db:
        admin = User(
            email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",
            full_name="Prune Admin",
            role="admin",
            is_active=True,
        )
        kept = _config(survivor, survivor_models, minutes=0, display_name="Survivor")
        gateway = _config(
            "filigran_gateway",
            ["claude-sonnet-5", "gpt-5.1", "text-embedding-3-large"],
            minutes=1,
            display_name="Filigran AI Gateway",
            is_from_env=True,
        )
        db.add_all([admin, kept, gateway])
        await db.flush()
        ps = await get_platform_settings(db)
        ps.embedding_provider = "filigran_gateway"
        ps.embedding_provider_id = gateway.id
        ps.embedding_model = "text-embedding-3-large"
        ps.embedding_dimensions = 3072
        ps.routing_model = "gpt-5.1"
        ps.routing_provider_id = gateway.id
        ps.fallback_model = "gpt-5.1"
        ps.fallback_provider_id = gateway.id
        db.add(KnowledgeBase(name="Wiki", user_id=None, is_company_managed=True, chunk_count=3))
        await db.commit()
        return {"admin": admin, "survivor": kept, "gateway": gateway}


async def _prune_audit(session_factory) -> AuditLog:
    async with session_factory() as db:
        [entry] = (await db.execute(select(AuditLog).where(AuditLog.entity_type == "llm_provider"))).scalars().all()
    return entry


async def _platform_settings(session_factory) -> PlatformSettings:
    async with session_factory() as db:
        return (await db.execute(select(PlatformSettings))).scalar_one()


async def _admin_notices(session_factory, admin_id) -> list[Activity]:
    async with session_factory() as db:
        return list((await db.execute(select(Activity).where(Activity.user_id == admin_id))).scalars().all())


@pytest.mark.asyncio
async def test_the_env_prune_keeps_the_embedding_routing_and_fallback_models_on_a_survivor_serving_them():
    """The gateway's variable is removed and the boot prune deletes its config:
    the embedding default keeps text-embedding-3-large and its 3072 dimensions
    on the OpenAI row (no vector-space change, no re-embed), and routing and
    fallback keep gpt-5.1 there, instead of being cleared."""
    from app.main import _prune_orphaned_env_providers

    async with _temp_database() as session_factory:
        rows = await _seed_gateway_held_defaults(
            session_factory, survivor="openai", survivor_models=["gpt-5.1", "text-embedding-3-large"]
        )
        openai = rows["survivor"]

        await _prune_orphaned_env_providers(set())

        ps = await _platform_settings(session_factory)
        assert (ps.embedding_provider, ps.embedding_provider_id, ps.embedding_model) == (
            "openai",
            openai.id,
            "text-embedding-3-large",
        )
        assert ps.embedding_dimensions == 3072
        assert (ps.routing_model, ps.routing_provider_id) == ("gpt-5.1", openai.id)
        assert (ps.fallback_model, ps.fallback_provider_id) == ("gpt-5.1", openai.id)
        async with session_factory() as db:
            kb = (await db.execute(select(KnowledgeBase))).scalar_one()
        # Not reset for a re-embed: the vectors stay comparable.
        assert kb.chunk_count == 3
        assert await _admin_notices(session_factory, rows["admin"].id) == []
        entry = await _prune_audit(session_factory)
        assert entry.user_email == "system:env_provider_prune"
        routes = entry.details["routes"]
        assert routes["embedding_default"]["action"] == "reseeded"
        assert routes["embedding_default"]["after"]["provider"] == "openai"
        assert routes["embedding_default"]["after"]["model"] == "text-embedding-3-large"
        assert routes["routing_model"]["after"]["provider_id"] == str(openai.id)
        assert routes["fallback_model"]["after"]["provider_id"] == str(openai.id)
        assert "embedding default keeps text-embedding-3-large on 'Survivor'" in entry.summary


@pytest.mark.asyncio
async def test_the_env_prune_clears_an_embedding_model_no_survivor_serves_and_tells_the_admins():
    """No survivor serves the models: the embedding default is cleared, never
    switched to another model; every administrator gets one bell notice naming
    the field to pick again; routing and fallback are cleared; the audit says so."""
    from app.main import _prune_orphaned_env_providers

    async with _temp_database() as session_factory:
        # Anthropic chats but embeds nothing, and does not offer gpt-5.1.
        rows = await _seed_gateway_held_defaults(
            session_factory, survivor="anthropic", survivor_models=["claude-sonnet-5"]
        )

        await _prune_orphaned_env_providers(set())

        ps = await _platform_settings(session_factory)
        assert (ps.embedding_provider, ps.embedding_provider_id, ps.embedding_model) == (None, None, None)
        assert (ps.routing_model, ps.routing_provider_id) == (None, None)
        assert (ps.fallback_model, ps.fallback_provider_id) == (None, None)
        [notice] = await _admin_notices(session_factory, rows["admin"].id)
        assert notice.activity_type == "notification"
        assert "text-embedding-3-large" in notice.title
        assert notice.extra_data["notification_source"] == "embedding_default_cleared"
        assert notice.extra_data["field"] == "embedding_model"
        entry = await _prune_audit(session_factory)
        assert entry.details["routes"]["embedding_default"]["action"] == "cleared"
        assert entry.details["admins_notified_embedding_cleared"] == 1
        assert "embedding default text-embedding-3-large cleared" in entry.summary


@pytest.mark.asyncio
async def test_an_env_survivor_is_picked_only_when_no_admin_row_serves_the_model():
    """Administrator rows first: an env row disappears again at the next boot."""
    from app.main import _prune_orphaned_env_providers

    async with _temp_database() as session_factory:
        rows = await _seed_gateway_held_defaults(
            session_factory, survivor="openai", survivor_models=["gpt-5.1", "text-embedding-3-large"]
        )
        async with session_factory() as db:
            # An older env-provisioned OpenAI row offering the same models.
            db.add(
                _config(
                    "openai",
                    ["gpt-5.1", "text-embedding-3-large"],
                    minutes=-5,
                    display_name="OpenAI (env)",
                    is_from_env=True,
                )
            )
            await db.commit()

        await _prune_orphaned_env_providers({"openai"})

        ps = await _platform_settings(session_factory)
        assert ps.embedding_provider_id == rows["survivor"].id
        assert ps.routing_provider_id == rows["survivor"].id
        assert ps.fallback_provider_id == rows["survivor"].id


@pytest.mark.asyncio
async def test_an_admin_save_of_the_routing_and_fallback_defaults_is_audited_before_and_after():
    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        backup = rows["backup"]

        await _save_general_assistant(
            session_factory,
            rows,
            GeneralAssistantConfigRequest(
                routing_model="gpt-4o",
                routing_provider_id=str(backup.id),
                fallback_model="gpt-4o",
                fallback_provider_id=str(backup.id),
            ),
        )

        async with session_factory() as db:
            [entry] = (
                (await db.execute(select(AuditLog).where(AuditLog.entity_type == "general_assistant"))).scalars().all()
            )
        changed = entry.details["model_defaults_changed"]
        assert changed["routing_model"] == {"before": "gpt-5.1", "after": "gpt-4o"}
        assert changed["routing_provider_id"] == {
            "before": str(rows["doomed"].id),
            "after": str(backup.id),
        }
        assert changed["fallback_model"] == {"before": None, "after": "gpt-4o"}
        assert "embedding_model" not in changed
        assert "model defaults changed: " in entry.summary


@pytest.mark.asyncio
async def test_a_catalog_edit_racing_a_deletion_never_leaves_a_default_on_a_dropped_model(monkeypatch):
    """The deletion keeps gpt-5.1 on Backup and is held there; an edit drops
    gpt-5.1 from Backup. Racing, the edit finds nothing on Backup to clear and
    the deletion then pins the defaults to a model Backup no longer lists."""
    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        backup, doomed = rows["backup"], rows["doomed"]
        async with session_factory() as db:
            stored = await db.get(LLMProviderConfig, backup.id)
            stored.available_models = ["gpt-4o", "gpt-5.1"]
            await db.commit()

        deletion_decided = asyncio.Event()
        release_deletion = asyncio.Event()
        cleanup = settings_api._cleanup_orphaned_model_references

        async def held_cleanup(db, provider_name, removed_models, **kwargs):
            if kwargs.get("provider_deleted"):
                deletion_decided.set()
                await release_deletion.wait()
            return await cleanup(db, provider_name, removed_models, **kwargs)

        monkeypatch.setattr(settings_api, "_cleanup_orphaned_model_references", held_cleanup)

        async def drop_gpt_5_1():
            async with session_factory() as db:
                admin = await db.get(User, rows["admin"].id)
                await settings_api.update_llm_provider(
                    provider_id=str(backup.id),
                    body=LLMProviderUpdateRequest(available_models=["gpt-4o"]),
                    request=None,
                    user=admin,
                    db=db,
                    _ai_gate=None,
                )
                await db.commit()

        deletion = asyncio.create_task(_delete(session_factory, rows, backup.id))
        await asyncio.wait_for(deletion_decided.wait(), timeout=10)
        edit = asyncio.create_task(drop_gpt_5_1())
        await asyncio.wait({edit}, timeout=2)
        release_deletion.set()
        await asyncio.gather(deletion, edit)

        ps, ga, *_, configs = await _state(session_factory)
        catalogs = {c.id: set(c.available_models or []) for c in configs}
        assert doomed.id not in catalogs
        for provider_id, model in (
            (ps.agent_default_provider_id, ps.agent_default_model),
            (ga.llm_provider_id, ga.llm_model),
        ):
            assert model is None or model in catalogs[provider_id]


# --- Provider reference writes are serialized with deletions ----------


async def _save_general_assistant(session_factory, rows, body: GeneralAssistantConfigRequest):
    async with session_factory() as db:
        admin = await db.get(User, rows["admin"].id)
        await settings_api.update_general_assistant_config(
            body=body,
            request=None,
            background_tasks=BackgroundTasks(),
            user=admin,
            db=db,
        )
        await db.commit()


async def _save_agent_model(session_factory, rows, agent_name: str, config_id, model: str):
    async with session_factory() as db:
        admin = await db.get(User, rows["admin"].id)
        agent = (await db.execute(select(Agent).where(Agent.name == agent_name))).scalar_one()
        await agents_api.update_agent(
            agent_id=str(agent.id),
            body=UpdateAgentRequest(
                llm_provider="openai", llm_provider_id=str(config_id), llm_model=model
            ),
            request=None,
            user=admin,
            db=db,
            _ai_gate=None,
        )
        await db.commit()


async def _dangling_references(session_factory) -> dict[str, uuid.UUID]:
    ps, ga, agents, users, _audit, configs = await _state(session_factory)
    existing = {c.id for c in configs}
    references = {
        "agent_default": ps.agent_default_provider_id,
        "general_assistant": ga.llm_provider_id,
        "image_default": ps.image_provider_id,
        "embedding_default": ps.embedding_provider_id,
        "routing_model": ps.routing_provider_id,
        "fallback_model": ps.fallback_provider_id,
        "decision_model": ps.decision_provider_id,
        **{f"agent:{name}": agent.llm_provider_id for name, agent in agents.items()},
        **{f"user:{name}": user.preferred_llm_provider_id for name, user in users.items()},
    }
    return {
        route: ref for route, ref in references.items() if ref is not None and ref not in existing
    }


@pytest.mark.asyncio
async def test_a_general_assistant_save_racing_a_deletion_never_pins_the_deleted_config(
    monkeypatch,
):
    """A stale Settings page picks the doomed config as the fallback while its
    deletion is held. Racing, the save commits a reference the deletion never
    saw, left dangling. Serialized, the save waits, finds the config gone and
    is refused."""
    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        doomed = rows["doomed"]
        deletion_decided = asyncio.Event()
        release_deletion = asyncio.Event()
        cleanup = settings_api._cleanup_orphaned_model_references

        async def held_cleanup(db, provider_name, removed_models, **kwargs):
            if kwargs.get("provider_deleted"):
                deletion_decided.set()
                await release_deletion.wait()
            return await cleanup(db, provider_name, removed_models, **kwargs)

        monkeypatch.setattr(settings_api, "_cleanup_orphaned_model_references", held_cleanup)

        deletion = asyncio.create_task(_delete(session_factory, rows))
        await asyncio.wait_for(deletion_decided.wait(), timeout=10)
        save = asyncio.create_task(
            _save_general_assistant(
                session_factory,
                rows,
                GeneralAssistantConfigRequest(
                    fallback_model="gpt-5.1", fallback_provider_id=str(doomed.id)
                ),
            )
        )
        await asyncio.wait({save}, timeout=2)
        release_deletion.set()
        deletion_result, save_result = await asyncio.gather(
            deletion, save, return_exceptions=True
        )

        assert deletion_result is None
        assert isinstance(save_result, HTTPException)
        assert save_result.status_code == 422
        assert "no longer exists" in save_result.detail
        assert await _dangling_references(session_factory) == {}


def _hold_deletion_after_cleanup(monkeypatch) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold the deletion once it has reset the pinned agents, before it commits."""
    deletion_applied = asyncio.Event()
    release_deletion = asyncio.Event()
    cleanup = settings_api._cleanup_orphaned_model_references

    async def held_cleanup(db, provider_name, removed_models, **kwargs):
        counts = await cleanup(db, provider_name, removed_models, **kwargs)
        if kwargs.get("provider_deleted"):
            deletion_applied.set()
            await release_deletion.wait()
        return counts

    monkeypatch.setattr(settings_api, "_cleanup_orphaned_model_references", held_cleanup)
    return deletion_applied, release_deletion


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_name", "kept_provider"),
    [("Elsewhere", "backup"), ("Pinned", None)],
    ids=["pinned-elsewhere", "pinned-to-the-doomed-config"],
)
async def test_an_agent_save_racing_a_deletion_never_pins_the_deleted_config(
    monkeypatch, agent_name, kept_provider
):
    """The deletion has reset the agents pinned to the doomed config and is held
    before it commits; meanwhile an agent is pinned to that config. Racing, the
    pin lands after the reset and outlives the config. Serialized, the save
    waits for the deletion and is refused - including when the session already
    held the agent as it stood before the reset ("Pinned")."""
    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        doomed = rows["doomed"]
        deletion_applied, release_deletion = _hold_deletion_after_cleanup(monkeypatch)

        deletion = asyncio.create_task(_delete(session_factory, rows))
        await asyncio.wait_for(deletion_applied.wait(), timeout=10)
        save = asyncio.create_task(
            _save_agent_model(session_factory, rows, agent_name, doomed.id, "gpt-5.1")
        )
        await asyncio.wait({save}, timeout=2)
        release_deletion.set()
        deletion_result, save_result = await asyncio.gather(
            deletion, save, return_exceptions=True
        )

        assert deletion_result is None
        assert isinstance(save_result, HTTPException)
        assert save_result.status_code == 422
        assert await _dangling_references(session_factory) == {}
        *_, agents, _users, _audit, _configs = await _state(session_factory)
        expected = rows[kept_provider].id if kept_provider else None
        assert agents[agent_name].llm_provider_id == expected


@pytest.mark.asyncio
async def test_a_duplicate_racing_a_deletion_never_copies_the_deleted_config(monkeypatch):
    """The copy carries the source's provider reference. Racing, it copied the
    pin the deletion was resetting; serialized, it copies the reset."""
    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        deletion_applied, release_deletion = _hold_deletion_after_cleanup(monkeypatch)

        async def duplicate_pinned():
            async with session_factory() as db:
                admin = await db.get(User, rows["admin"].id)
                await agents_api.duplicate_agent(
                    agent_id=str(rows["pinned"].id),
                    request=None,
                    user=admin,
                    db=db,
                    _agentic_gate=None,
                )
                await db.commit()

        deletion = asyncio.create_task(_delete(session_factory, rows))
        await asyncio.wait_for(deletion_applied.wait(), timeout=10)
        duplicate = asyncio.create_task(duplicate_pinned())
        await asyncio.wait({duplicate}, timeout=2)
        release_deletion.set()
        await asyncio.gather(deletion, duplicate)

        assert await _dangling_references(session_factory) == {}
        *_, agents, _users, _audit, _configs = await _state(session_factory)
        # The whole reset, not just the id: the existence check alone drops the
        # pin but keeps the model only the deleted config served.
        copy = agents["Copy of Pinned"]
        assert (copy.llm_provider, copy.llm_provider_id, copy.llm_model) == (None, None, None)


@pytest.mark.asyncio
async def test_a_deletion_waits_for_a_writer_even_on_a_config_created_after_its_scan():
    """Row locks only cover the rows a scan saw. The writer scanned an empty
    table; a config is then created. With row locks alone the deletion of that
    config locks it at once and can commit under a model-only write validated
    against it. The advisory lock makes the deletion wait for the writer, and
    then decide from what it committed."""
    from app.services.llm_provider_deletion import lock_provider_configs

    async with (
        _temp_database() as session_factory,
        session_factory() as writer,
        session_factory() as deleter,
    ):
        await lock_provider_configs(writer, shared=True)

        async with session_factory() as creator:
            await lock_provider_configs(creator, shared=True)
            creator.add(_config("openai", ["gpt-4o"], minutes=0))
            await creator.commit()

        deletion_locked = asyncio.create_task(lock_provider_configs(deleter))
        await asyncio.wait({deletion_locked}, timeout=2)
        assert not deletion_locked.done()

        await writer.commit()
        await asyncio.wait_for(deletion_locked, timeout=10)
        await deleter.rollback()


@pytest.mark.asyncio
async def test_a_catalog_install_racing_a_deletion_never_copies_the_deleted_config(
    monkeypatch,
):
    """The catalog clone carries the published agent's provider reference.
    Racing, it copied the pin the deletion was resetting, left
    dangling once both committed; serialized, it copies the reset."""
    from app.api.v1 import public_catalog

    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        async with session_factory() as db:
            published = await db.get(Agent, rows["pinned"].id)
            published.is_public_catalog = True
            published.is_company_managed = True
            await db.commit()
        deletion_applied, release_deletion = _hold_deletion_after_cleanup(monkeypatch)

        async def install() -> None:
            async with session_factory() as db:
                admin = await db.get(User, rows["admin"].id)
                await public_catalog.add_catalog_item_to_account(
                    entity_type="agents",
                    entity_id=str(rows["pinned"].id),
                    body=None,
                    user=admin,
                    db=db,
                    _ai_gate=None,
                )

        deletion = asyncio.create_task(_delete(session_factory, rows))
        await asyncio.wait_for(deletion_applied.wait(), timeout=10)
        installing = asyncio.create_task(install())
        await asyncio.wait({installing}, timeout=2)
        release_deletion.set()
        await asyncio.gather(deletion, installing)

        assert await _dangling_references(session_factory) == {}
        *_, agents, _users, _audit, _configs = await _state(session_factory)
        [clone] = [
            agent for name, agent in agents.items() if name not in {"Pinned", "Legacy", "Elsewhere"}
        ]
        # The whole reset, not just the id: the existence check alone drops the
        # pin but keeps the model only the deleted config served.
        assert (clone.llm_provider, clone.llm_provider_id, clone.llm_model) == (None, None, None)


@pytest.mark.asyncio
async def test_a_duplicate_never_copies_a_pin_to_a_config_that_is_gone():
    """A pin left dangling before this fix stays on its agent, but the copy is a
    new reference: it keeps the model and drops the pin."""
    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        async with session_factory() as db:
            db.add(
                Agent(
                    name="Stale",
                    llm_provider="openai",
                    llm_provider_id=uuid.uuid4(),
                    llm_model="gpt-4o",
                )
            )
            await db.commit()
            stale = (await db.execute(select(Agent).where(Agent.name == "Stale"))).scalar_one()

        async with session_factory() as db:
            admin = await db.get(User, rows["admin"].id)
            await agents_api.duplicate_agent(
                agent_id=str(stale.id), request=None, user=admin, db=db, _agentic_gate=None
            )
            await db.commit()

        *_, agents, _users, _audit, _configs = await _state(session_factory)
        copy = agents["Copy of Stale"]
        assert copy.llm_provider_id is None
        assert (copy.llm_provider, copy.llm_model) == ("openai", "gpt-4o")


@pytest.mark.asyncio
async def test_a_deletion_queued_behind_another_reads_what_it_committed():
    """A PlatformSettings copy already in the second request's session when its
    deletion starts waiting on the lock is stale once the first deletion
    commits: re-seeded onto Claude, the platform default then stayed on Claude
    when the second deletion removed it. The identity map keeps an unmodified
    copy only while something references it, so the test holds one."""
    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        anthropic, backup = rows["anthropic"], rows["backup"]

        async with session_factory() as first, session_factory() as second:
            admin_first = await first.get(User, rows["admin"].id)
            admin_second = await second.get(User, rows["admin"].id)
            # A copy loaded before the lock wait, still referenced.
            held = await peek_platform_settings(second)
            assert held is not None
            await settings_api.delete_llm_provider(
                provider_id=str(rows["doomed"].id),
                request=None,
                replacement_provider_id=None,
                expected_impact=None,
                user=admin_first,
                db=first,
                _ai_gate=None,
            )
            await settings_api.delete_llm_provider(
                provider_id=str(anthropic.id),
                request=None,
                replacement_provider_id=None,
                expected_impact=None,
                user=admin_second,
                db=second,
                _ai_gate=None,
            )

        assert await _dangling_references(session_factory) == {}
        ps, ga, *_ = await _state(session_factory)
        assert ps.agent_default_provider_id == backup.id
        assert ga.llm_provider_id == backup.id


@pytest.mark.asyncio
async def test_a_save_naming_a_config_that_does_not_exist_is_refused():
    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        ghost = uuid.uuid4()

        with pytest.raises(HTTPException) as ga_exc:
            await _save_general_assistant(
                session_factory,
                rows,
                GeneralAssistantConfigRequest(image_model="gpt-image-1", image_provider_id=str(ghost)),
            )
        with pytest.raises(HTTPException) as agent_exc:
            await _save_agent_model(session_factory, rows, "Elsewhere", ghost, "gpt-4o")

        assert ga_exc.value.status_code == 422
        assert agent_exc.value.status_code == 422
        ps, _ga, agents, *_ = await _state(session_factory)
        assert ps.image_provider_id == rows["doomed"].id
        assert agents["Elsewhere"].llm_provider_id == rows["backup"].id

        # A reference already stored and sent back unchanged is not re-checked.
        await _save_general_assistant(
            session_factory,
            rows,
            GeneralAssistantConfigRequest(image_provider_id=str(rows["doomed"].id)),
        )


@pytest.mark.asyncio
async def test_a_provider_is_committed_before_its_creation_is_answered():
    """The get_db teardown commits after the response is sent. Left to it, a
    new config was still uncommitted when the 201 reached the client: an agent
    pinned to it at once took the shared lock beside the creation's, could not
    see the row, and was refused."""
    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        async with session_factory() as creating:
            admin = await creating.get(User, rows["admin"].id)
            created = await settings_api.create_llm_provider(
                body=LLMProviderRequest(
                    provider="openai", api_key="sk-new", available_models=["gpt-4o"]
                ),
                request=None,
                user=admin,
                db=creating,
                _ai_gate=None,
            )
            # Still open, as the request's session is while the response is sent.
            await _save_agent_model(session_factory, rows, "Elsewhere", created.id, "gpt-4o")

        *_, agents, _users, _audit, _configs = await _state(session_factory)
        assert str(agents["Elsewhere"].llm_provider_id) == str(created.id)


@pytest.mark.asyncio
async def test_a_refusal_the_agent_routes_answered_first_still_comes_first():
    """The provider check runs where the routes read the id, after the checks
    they made before it: a save refused for another reason (a slug already
    taken) keeps that answer instead of the provider's 422."""
    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        ghost = str(uuid.uuid4())
        async with session_factory() as db:
            db.add(Agent(name="Taken", slug="taken"))
            await db.commit()

        async with session_factory() as db:
            admin = await db.get(User, rows["admin"].id)
            with pytest.raises(HTTPException) as created:
                await agents_api.create_agent(
                    body=CreateAgentRequest(name="Twin", slug="taken", llm_provider_id=ghost),
                    request=None,
                    user=admin,
                    db=db,
                    _agentic_gate=None,
                )
        async with session_factory() as db:
            admin = await db.get(User, rows["admin"].id)
            with pytest.raises(HTTPException) as updated:
                await agents_api.update_agent(
                    agent_id=str(rows["elsewhere"].id),
                    body=UpdateAgentRequest(slug="taken", llm_provider_id=ghost),
                    request=None,
                    user=admin,
                    db=db,
                    _ai_gate=None,
                )

        assert (created.value.status_code, updated.value.status_code) == (409, 409)


@pytest.mark.asyncio
async def test_a_general_assistant_save_releases_the_lock_before_its_embedding_reset(
    monkeypatch,
):
    """A save choosing the embedding model holds the provider lock exclusive and
    hands the knowledge base reset to a background task, which can run for
    minutes. FastAPI runs a background task before the ``get_db`` teardown
    commits: committed there, the save held the lock for the whole reset, and
    every agent model save, provider creation and deletion queued behind it
    until its statement timeout. Driven through FastAPI so the order is the
    framework's own; the reset asks for the lock the way an agent save does."""
    import httpx
    from fastapi import FastAPI
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.api.deps import require_admin
    from app.services.llm_provider_deletion import lock_provider_configs

    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        async with session_factory() as db:
            admin = await db.get(User, rows["admin"].id)
        lock_granted_to_the_reset: list[bool] = []

        async def reset_asking_for_the_lock(_target_embedding_model):
            async with session_factory() as other:
                await other.execute(text("SET LOCAL lock_timeout = '3s'"))
                try:
                    await lock_provider_configs(other, shared=True)
                except DBAPIError:
                    lock_granted_to_the_reset.append(False)
                else:
                    lock_granted_to_the_reset.append(True)

        monkeypatch.setattr(
            settings_api, "_run_embedding_reset_background", reset_asking_for_the_lock
        )
        app = FastAPI()
        app.include_router(settings_api.router, prefix="/api/v1/settings")
        app.dependency_overrides[require_admin] = lambda: admin

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.put(
                "/api/v1/settings/general-assistant",
                json={
                    "embedding_provider": "tei",
                    "embedding_provider_id": str(rows["tei"].id),
                    "embedding_model": "bge-m3",
                },
            )

        assert response.status_code == 200, response.text
        assert lock_granted_to_the_reset == [True]
        ps, *_ = await _state(session_factory)
        assert (ps.embedding_provider_id, ps.embedding_model) == (rows["tei"].id, "bge-m3")


@pytest.mark.asyncio
async def test_the_exclusive_provider_lock_lets_embedding_writes_through():
    """The exclusive row locks exclude no foreign key check onto the configs.

    A migration dropped the embeddings foreign key, so the schema built here
    has none; the test puts back the one an install carried before it, whose
    check reads the config ``FOR KEY SHARE``. With the configs locked ``FOR
    UPDATE``, every embedding write stalled for as long as a deletion, a catalog
    edit or a platform defaults save was open. ``FOR NO KEY UPDATE`` lets it
    through and still stops an update of the config row itself."""
    from sqlalchemy import text, update
    from sqlalchemy.exc import DBAPIError

    from app.services.llm_provider_deletion import lock_provider_configs

    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        backup = rows["backup"]
        async with session_factory() as db:
            await db.execute(
                text(
                    "ALTER TABLE embeddings ADD CONSTRAINT "
                    "embeddings_embedding_provider_id_fkey FOREIGN KEY "
                    "(embedding_provider_id) REFERENCES llm_provider_configs (id) "
                    "ON DELETE SET NULL"
                )
            )
            await db.commit()
        async with session_factory() as holder:
            await lock_provider_configs(holder)
            async with session_factory() as writer:
                await writer.execute(text("SET LOCAL lock_timeout = '2s'"))
                writer.add(
                    Embedding(
                        source_type="document",
                        content="chunk",
                        embedding=[0.1, 0.2, 0.3],
                        embedding_provider_id=backup.id,
                    )
                )
                await writer.commit()
            async with session_factory() as editor:
                await editor.execute(text("SET LOCAL lock_timeout = '1s'"))
                with pytest.raises(DBAPIError):
                    await editor.execute(
                        update(LLMProviderConfig)
                        .where(LLMProviderConfig.id == backup.id)
                        .values(display_name="Renamed")
                    )
            await holder.rollback()


@pytest.mark.asyncio
async def test_a_lock_wait_past_the_statement_timeout_is_a_clear_refusal():
    """A write that waited for the provider lock past the statement timeout
    failed with the raw cancellation, a 500. It is refused with a 503 saying
    another provider change is in progress, and a retry once that change has
    committed goes through."""
    from sqlalchemy import text

    from app.services.llm_provider_deletion import (
        ProviderLockBusyError,
        lock_provider_configs,
    )

    async with _temp_database() as session_factory:
        await _seed(session_factory)
        async with session_factory() as holder:
            await lock_provider_configs(holder)
            async with session_factory() as waiter:
                await waiter.execute(text("SET LOCAL statement_timeout = '1s'"))
                with pytest.raises(ProviderLockBusyError) as refused:
                    await lock_provider_configs(waiter, shared=True)
                await waiter.rollback()
            await holder.rollback()
        async with session_factory() as retry:
            await lock_provider_configs(retry, shared=True)

        assert refused.value.status_code == 503
        assert "another llm provider change is in progress" in refused.value.detail.lower()


@pytest.mark.asyncio
async def test_a_save_completing_a_stored_default_waits_for_a_deletion(monkeypatch):
    """A platform default stored by provider name only (saved before the
    reference columns) gets an id from any General Assistant save, one touching
    no model field included. Unlocked, a save racing a deletion completed the
    default it had read before the deletion committed: the replacement's name
    and model with another config's id. Serialized, it reads the re-seeded
    default, which has nothing left to complete."""
    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        async with session_factory() as db:
            ps = await get_platform_settings(db)
            ps.agent_default_provider_id = None
            await db.commit()
        deletion_applied, release_deletion = _hold_deletion_after_cleanup(monkeypatch)

        deletion = asyncio.create_task(_delete(session_factory, rows))
        await asyncio.wait_for(deletion_applied.wait(), timeout=10)
        save = asyncio.create_task(
            _save_general_assistant(
                session_factory, rows, GeneralAssistantConfigRequest(system_prompt="Triage first.")
            )
        )
        await asyncio.wait({save}, timeout=2)
        release_deletion.set()
        await asyncio.gather(deletion, save)

        ps, ga, *_ = await _state(session_factory)
        default = (ps.agent_default_provider, ps.agent_default_provider_id, ps.agent_default_model)
        assert default == ("anthropic", rows["anthropic"].id, "claude-sonnet-5")
        assert ga.system_prompt == "Triage first."


@pytest.mark.asyncio
async def test_an_import_racing_a_deletion_never_keeps_a_model_nothing_serves(monkeypatch):
    """An imported agent keeps its bundle's model only when a configured
    provider serves it. Racing, the import checked the model against the doomed
    config and created the agent once the deletion's cleanup had run, keeping a
    model nothing serves. Serialized, it waits, finds the model gone and drops
    it, as for any model this platform does not serve."""
    from app.api.v1.agent_import import create_agent_from_bundle

    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        deletion_applied, release_deletion = _hold_deletion_after_cleanup(monkeypatch)

        async def import_bundle() -> list[str]:
            async with session_factory() as db:
                admin = await db.get(User, rows["admin"].id)
                imported = await create_agent_from_bundle(
                    db,
                    user=admin,
                    bundle={
                        "version": 3,
                        "export_type": "agent",
                        "agent": {
                            "name": "Imported",
                            "llm_provider": "openai",
                            "llm_model": "gpt-5.1",
                        },
                    },
                )
                await db.commit()
                return imported.warnings

        deletion = asyncio.create_task(_delete(session_factory, rows))
        await asyncio.wait_for(deletion_applied.wait(), timeout=10)
        importing = asyncio.create_task(import_bundle())
        await asyncio.wait({importing}, timeout=2)
        release_deletion.set()
        _, warnings = await asyncio.gather(deletion, importing)

        *_, agents, _users, _audit, _configs = await _state(session_factory)
        assert (agents["Imported"].llm_provider, agents["Imported"].llm_model) == (None, None)
        assert any("gpt-5.1" in warning for warning in warnings)


@pytest.mark.asyncio
async def test_a_catalog_edit_waits_for_a_writer_even_on_a_config_created_after_its_scan():
    """A writer's row locks only cover the configs its scan saw. Here it picks a
    model only a config created after that scan lists, and an edit then drops
    that model. With its own row lock alone the edit went through at once and
    its cleanup missed the pick, left on a model nothing lists. The edit waits
    for the writer, like a deletion, and resets the pick."""
    from app.services.llm_provider_deletion import lock_provider_configs

    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)

        async def drop_gpt_4_1(config_id) -> None:
            async with session_factory() as db:
                admin = await db.get(User, rows["admin"].id)
                await settings_api.update_llm_provider(
                    provider_id=str(config_id),
                    body=LLMProviderUpdateRequest(available_models=["gpt-4.1-mini"]),
                    request=None,
                    user=admin,
                    db=db,
                    _ai_gate=None,
                )
                await db.commit()

        async with session_factory() as writer:
            await lock_provider_configs(writer, shared=True)
            async with session_factory() as creator:
                await lock_provider_configs(creator, shared=True)
                late = _config("openai", ["gpt-4.1", "gpt-4.1-mini"], minutes=5)
                creator.add(late)
                await creator.commit()
            legacy = (await writer.execute(select(Agent).where(Agent.name == "Legacy"))).scalar_one()
            legacy.llm_model = "gpt-4.1"
            await writer.flush()

            edit = asyncio.create_task(drop_gpt_4_1(late.id))
            await asyncio.wait({edit}, timeout=2)
            assert not edit.done()
            await writer.commit()
            await asyncio.wait_for(edit, timeout=10)

        *_, agents, _users, _audit, _configs = await _state(session_factory)
        assert agents["Legacy"].llm_model is None


@pytest.mark.asyncio
async def test_a_deletion_never_misses_a_provider_only_import_in_flight(monkeypatch):
    """A bundle can name a provider without a model (an agent exported with a
    provider and no model override): the import stores the name, a choice a
    deletion resets by name. Unlocked, a deletion that started while such an
    import was in flight read past its uncommitted agent, and the import then
    committed the deleted provider's name. Serialized, the deletion waits for
    the import and resets the agent it created."""
    from app.api.v1.agent_import import create_agent_from_bundle

    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        deletion_applied, release_deletion = _hold_deletion_after_cleanup(monkeypatch)

        async with session_factory() as importer:
            admin = await importer.get(User, rows["admin"].id)
            await create_agent_from_bundle(
                importer,
                user=admin,
                bundle={
                    "version": 3,
                    "export_type": "agent",
                    "agent": {"name": "Imported", "llm_provider": "openai"},
                },
            )
            deletion = asyncio.create_task(_delete(session_factory, rows))
            decided = asyncio.create_task(deletion_applied.wait())
            await asyncio.wait({decided}, timeout=2)
            waited_for_the_import = not decided.done()
            await importer.commit()
        release_deletion.set()
        await asyncio.wait_for(deletion, timeout=10)
        await asyncio.wait_for(decided, timeout=10)

        assert waited_for_the_import
        *_, agents, _users, _audit, _configs = await _state(session_factory)
        assert (agents["Imported"].llm_provider, agents["Imported"].llm_model) == (None, None)


def _isolate_env_seed(monkeypatch, **overrides) -> None:
    """Run the env provider seed on these settings alone, whatever the machine's environment holds."""
    import dotenv

    from app.core.config import settings as app_settings

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    for key in [key for key in os.environ if key.startswith("LLM_PROVIDER_")]:
        monkeypatch.delenv(key)
    unset = {
        "default_model": "",
        "default_general_assistant_model": "",
        "default_reasoning_effort": "",
        "default_routing_model": "",
        "default_fallback_model": "",
        "default_embedding_provider": "",
        "default_embedding_model": "",
        "default_embedding_provider_base_url": "",
        "default_embedding_dimensions": None,
        "default_image_provider": "",
        "default_image_model": "",
        "default_image_provider_base_url": "",
    }
    for attr, value in {**unset, **overrides}.items():
        monkeypatch.setattr(app_settings, attr, value)


@pytest.mark.asyncio
async def test_the_env_seed_racing_a_deletion_never_pins_the_deleted_config(monkeypatch):
    """Switching AI on at runtime runs the env provider seed next to whatever an
    admin does meanwhile. ``DEFAULT_MODEL`` names a model only the doomed config
    lists. Racing, the seed found it there and its pin landed once the deletion
    had committed, left dangling. Serialized, the seed waits, finds no config
    listing the model and leaves the platform default where it was."""
    from app import main as app_main

    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        backup = rows["backup"]
        async with session_factory() as db:
            ps = await get_platform_settings(db)
            ps.agent_default_provider_id = backup.id
            ps.agent_default_model = "gpt-4o"
            await db.commit()
        _isolate_env_seed(monkeypatch, default_model="gpt-5.1")
        deletion_applied, release_deletion = _hold_deletion_after_cleanup(monkeypatch)

        deletion = asyncio.create_task(_delete(session_factory, rows))
        await asyncio.wait_for(deletion_applied.wait(), timeout=10)
        seed = asyncio.create_task(app_main._seed_llm_providers_from_env())
        await asyncio.wait({seed}, timeout=2)
        release_deletion.set()
        await asyncio.gather(deletion, seed)

        assert await _dangling_references(session_factory) == {}
        ps, *_ = await _state(session_factory)
        assert (ps.agent_default_provider_id, ps.agent_default_model) == (backup.id, "gpt-4o")


@pytest.mark.asyncio
async def test_the_embedding_seed_racing_a_deletion_of_its_config_provisions_it_again(monkeypatch):
    """The built-in embedding endpoint seed rewrites its config, also when AI
    is switched on at runtime. Racing an admin's delete of that config, its
    update waited on the deleted row, then matched nothing and failed the seed.
    Serialized, it waits for the deletion and provisions the endpoint again."""
    from app import main as app_main
    from app.core.config import settings as app_settings

    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        tei = rows["tei"]
        monkeypatch.setattr(app_settings, "platform_mode", "xtm_one")
        monkeypatch.setattr(app_settings, "builtin_embedding_server_base_url", "http://tei.test")
        monkeypatch.setattr(app_settings, "builtin_embedding_server_model", "bge-m3")
        deletion_applied, release_deletion = _hold_deletion_after_cleanup(monkeypatch)

        deletion = asyncio.create_task(_delete_config(session_factory, rows, tei.id))
        await asyncio.wait_for(deletion_applied.wait(), timeout=10)
        seed = asyncio.create_task(app_main._seed_xtm_one_embedding_server_from_env())
        await asyncio.wait({seed}, timeout=2)
        release_deletion.set()
        await asyncio.gather(deletion, seed)

        *_, configs = await _state(session_factory)
        [endpoint] = [config for config in configs if config.provider == "tei"]
        assert endpoint.id != tei.id
        assert await _dangling_references(session_factory) == {}


async def _seed_chunks(session_factory, rows) -> dict[str, list[uuid.UUID]]:
    """Three chunks embedded by the doomed config, one by Backup."""
    async with session_factory() as db:
        chunks = {
            "doomed": [
                Embedding(
                    source_type="document",
                    embedding_provider="openai",
                    embedding_provider_id=rows["doomed"].id,
                    embedding_model="text-embedding-3-small",
                    embedding_dimensions=3,
                    content=f"chunk {i}",
                    embedding=[0.1, 0.2, 0.3],
                )
                for i in range(3)
            ],
            "backup": [
                Embedding(
                    source_type="document",
                    embedding_provider="openai",
                    embedding_provider_id=rows["backup"].id,
                    embedding_model="text-embedding-3-small",
                    embedding_dimensions=3,
                    content="kept",
                    embedding=[0.3, 0.2, 0.1],
                )
            ],
        }
        db.add_all([*chunks["doomed"], *chunks["backup"]])
        await db.commit()
        return {key: [chunk.id for chunk in group] for key, group in chunks.items()}


async def _chunk_providers(session_factory) -> dict[uuid.UUID, tuple[str | None, uuid.UUID | None]]:
    async with session_factory() as db:
        result = await db.execute(
            select(Embedding.id, Embedding.embedding_provider, Embedding.embedding_provider_id)
        )
        return {row.id: (row.embedding_provider, row.embedding_provider_id) for row in result}


@asynccontextmanager
async def _chunk_held(session_factory, chunk_id: uuid.UUID) -> AsyncIterator[None]:
    """Another transaction holding one chunk row, as an ingestion rewriting it does."""
    async with session_factory() as holder:
        await holder.execute(
            select(Embedding.id).where(Embedding.id == chunk_id).with_for_update()
        )
        try:
            yield
        finally:
            await holder.rollback()


@pytest.mark.asyncio
async def test_the_deletion_does_not_wait_on_the_chunks_the_config_embedded():
    """The ON DELETE SET NULL cascade ran inside the locked deletion, so a
    chunk row held elsewhere held the deletion (and every provider edit queued
    behind its lock). The deletion now commits without touching a chunk, and
    the sweep ends in the state the cascade produced."""
    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        chunks = await _seed_chunks(session_factory, rows)
        doomed, backup = rows["doomed"], rows["backup"]

        async with _chunk_held(session_factory, chunks["doomed"][0]):
            await asyncio.wait_for(_delete(session_factory, rows), timeout=10)

        *_, configs = await _state(session_factory)
        assert doomed.id not in {c.id for c in configs}

        async with session_factory() as db:
            cleared = await clear_deleted_embedding_provider_references(db)
        assert cleared == 3
        providers = await _chunk_providers(session_factory)
        for chunk_id in chunks["doomed"]:
            # The id is nulled, the provider name kept: what the cascade left.
            assert providers[chunk_id] == ("openai", None)
        assert providers[chunks["backup"][0]] == ("openai", backup.id)

        async with session_factory() as db:
            assert await clear_deleted_embedding_provider_references(db) == 0


@pytest.mark.asyncio
async def test_the_sweep_skips_a_chunk_another_transaction_holds():
    async with _temp_database() as session_factory:
        rows = await _seed(session_factory)
        chunks = await _seed_chunks(session_factory, rows)
        await _delete(session_factory, rows)
        held = chunks["doomed"][0]

        async with _chunk_held(session_factory, held):
            async with session_factory() as db:
                cleared = await asyncio.wait_for(
                    clear_deleted_embedding_provider_references(db, batch_size=1),
                    timeout=10,
                )
            assert cleared == 2
            assert (await _chunk_providers(session_factory))[held][1] == rows["doomed"].id

        async with session_factory() as db:
            assert await clear_deleted_embedding_provider_references(db) == 1
        providers = await _chunk_providers(session_factory)
        assert all(providers[chunk_id][1] is None for chunk_id in chunks["doomed"])
