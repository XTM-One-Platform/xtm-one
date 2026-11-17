"""The OpenRouter console on a real PostgreSQL: who may do what, and the platform provider it feeds.

Runs the real routes of ``app/api/v1/openrouter.py`` (and the real integration
update route, for the key that follows an edit) against an isolated temporary
database, with OpenRouter itself replaced by a recording fake: no request
leaves the process.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.deps import AfterCommit
from app.api.v1 import integrations as integrations_mod
from app.api.v1 import openrouter as console
from app.api.v1.schemas.integration import UpdateIntegrationRequest
from app.api.v1.schemas.openrouter import (
    CreateKeyRequest,
    LinkProviderRequest,
    ProviderModelsRequest,
    UpdateKeyRequest,
)
from app.core.security import decrypt_value, encrypt_value
from app.integrations.providers import openrouter as client
from app.models.audit_log import AuditLog
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.llm_config import LLMProviderConfig
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_API_KEY = "sk-or-v1-integration-key"
_MANAGEMENT_KEY = "sk-or-v1-management-key"
_PLATFORM_KEY = "sk-or-v1-settings-provider-key"
_OTHER_HASH = "0" * 64

_CATALOG = [
    {"id": "anthropic/claude-sonnet-5", "context_length": 200_000},
    {"id": "openai/gpt-4o", "context_length": 128_000},
    {"id": "google/gemini-3.8-flash", "context_length": 1_048_576},
]


def _model(entry: dict) -> dict:
    """A catalog entry as ``client.normalize_model`` returns it."""
    return {
        "id": entry["id"],
        "name": entry["id"],
        "author": entry["id"].split("/")[0],
        "context_length": entry["context_length"],
        "pricing": {"prompt": 1.0, "completion": 2.0},
    }


class _FakeOpenRouter:
    """Stands in for OpenRouter; records every call the routes make."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def install(self, monkeypatch) -> _FakeOpenRouter:
        async def get_key_info(api_key):
            self.calls.append(("key", api_key))
            return {"label": "sk-or-v1-int...key", "usage": 3.5, "usage_daily": 0.5}

        async def get_credits(management_key):
            self.calls.append(("credits", management_key))
            return {"total_credits": 100.0, "total_usage": 3.5, "remaining": 96.5}

        async def cached_models(api_key, *, kind="chat"):
            self.calls.append(("models", api_key, kind))
            return [_model(entry) for entry in _CATALOG] if kind == "chat" else []

        async def list_api_keys(management_key, *, include_disabled=True):
            self.calls.append(("list_keys", management_key))
            return client.KeyListing(keys=[], truncated=False)

        async def create_api_key(management_key, body):
            self.calls.append(("create_key", management_key, body))
            return _key_object(_OTHER_HASH, body["name"]), "sk-or-v1-fresh-secret"

        async def update_api_key(management_key, hash_, body):
            self.calls.append(("update_key", hash_, body))
            return _key_object(hash_, "Renamed")

        async def delete_api_key(management_key, hash_):
            self.calls.append(("delete_key", hash_))

        for name, fake in list(locals().items()):
            if callable(fake) and name != "self":
                monkeypatch.setattr(client, name, fake)
        return self

    def made(self, kind: str) -> list[tuple]:
        return [call for call in self.calls if call[0] == kind]


def _key_object(hash_: str, name: str) -> dict:
    return {
        "hash": hash_,
        "name": name,
        "label": "sk-or-v1-abc...123",
        "disabled": False,
        "limit": None,
        "limit_remaining": None,
        "limit_reset": None,
        "include_byok_in_limit": False,
        "usage": 0.0,
        "usage_daily": 0.0,
        "usage_weekly": 0.0,
        "usage_monthly": 0.0,
        "created_at": "2026-10-01T10:00:00Z",
        "updated_at": None,
        "expires_at": None,
    }


def _user(label: str, *, role: str = "user") -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{label}-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name=label.title(),
        role=role,
    )


def _openrouter(*, owner: User | None, company: bool = False, **sharing) -> Integration:
    return Integration(
        id=uuid.uuid4(),
        user_id=None if company else owner.id,
        provider="openrouter",
        auth_type="api_key",
        name="OpenRouter",
        status="connected",
        is_company_managed=company,
        api_key_enc=encrypt_value(_API_KEY),
        extra_config_enc=encrypt_value(json.dumps({"management_key": _MANAGEMENT_KEY})),
        **sharing,
    )


async def _overview(user, integration, session):
    return await console.get_overview(str(integration.id), user=user, db=session, _ai_gate=None)


async def _status(coro) -> int:
    try:
        await coro
    except HTTPException as exc:
        return exc.status_code
    return 200


# ---------------------------------------------------------------------------
# Who may open the console, and who may mint keys
# ---------------------------------------------------------------------------


async def test_the_console_follows_the_manage_rule_and_keys_the_account_holder(monkeypatch):
    fake = _FakeOpenRouter().install(monkeypatch)
    async with (
        scratch_database("openrouterconsole", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        owner, admin = _user("owner"), _user("admin", role="admin")
        editor, group_editor, reader, stranger = (_user(n) for n in ("editor", "groupeditor", "reader", "stranger"))
        group = Group(id=uuid.uuid4(), name=f"editors-{uuid.uuid4().hex[:6]}")
        session.add_all([owner, admin, editor, group_editor, reader, stranger, group])
        await session.flush()
        session.add(GroupMember(group_id=group.id, user_id=group_editor.id))
        await session.flush()
        integration = _openrouter(
            owner=owner,
            shared_user_ids=[str(editor.id), str(reader.id)],
            shared_user_permissions={str(editor.id): "config", str(reader.id): "read_only"},
            shared_group_ids=[str(group.id)],
            shared_group_permissions={str(group.id): "config"},
        )
        session.add(integration)
        await session.commit()

        # Whoever manages the integration opens the console; only the account
        # holder (the owner, an admin) may mint keys; nobody may feed the
        # platform provider from a personal integration.
        for user, may_mint in ((owner, True), (admin, True), (editor, False), (group_editor, False)):
            overview = await _overview(user, integration, session)
            assert (overview.can_manage_keys, overview.can_link_provider) == (may_mint, False), user.full_name
            assert overview.key.usage == 3.5
            assert overview.credits.remaining == 96.5
        assert overview.link_refusal.startswith("Make this integration company-managed")
        # A read-only grantee sees the integration, not its account: 403. A
        # stranger does not see it at all: the unknown id's 404.
        assert await _status(_overview(reader, integration, session)) == 403
        assert await _status(_overview(stranger, integration, session)) == 404

        body = CreateKeyRequest(name="CI runner", limit=5, limit_reset="daily")
        for user in (editor, group_editor):
            assert await _status(
                console.create_key(str(integration.id), body, None, Response(), user=user, db=session, _ai_gate=None)
            ) == 403
        assert fake.made("create_key") == []

        created = await console.create_key(
            str(integration.id), body, None, Response(), user=owner, db=session, _ai_gate=None
        )
        assert created.secret == "sk-or-v1-fresh-secret"
        assert fake.made("create_key") == [
            ("create_key", _MANAGEMENT_KEY, {"name": "CI runner", "include_byok_in_limit": False, "limit": 5.0, "limit_reset": "daily"})
        ]
        [audit] = (
            await session.execute(select(AuditLog).where(AuditLog.entity_id == integration.id))
        ).scalars().all()
        assert audit.user_id == owner.id
        assert audit.details["key_hash"] == _OTHER_HASH
        assert "fresh-secret" not in json.dumps(audit.details) + audit.summary

        # A revoked share takes the console away at once.
        integration.shared_user_ids = [str(reader.id)]
        integration.shared_user_permissions = {str(reader.id): "read_only"}
        await session.commit()
        assert await _status(_overview(editor, integration, session)) == 404


async def test_a_company_managed_integration_is_an_admins_console(monkeypatch):
    _FakeOpenRouter().install(monkeypatch)
    async with (
        scratch_database("openrouterconsole", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        member, admin = _user("member"), _user("admin", role="admin")
        session.add_all([member, admin])
        integration = _openrouter(owner=None, company=True)
        session.add(integration)
        await session.commit()

        assert await _status(_overview(member, integration, session)) == 403
        assert await _status(
            console.get_models(str(integration.id), kind="chat", user=member, db=session, _ai_gate=None)
        ) == 403
        overview = await _overview(admin, integration, session)
        assert (overview.can_manage_keys, overview.can_link_provider, overview.link_refusal) == (True, True, None)


async def test_a_key_the_platform_runs_on_is_neither_disabled_nor_deleted(monkeypatch):
    fake = _FakeOpenRouter().install(monkeypatch)
    async with (
        scratch_database("openrouterconsole", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        admin = _user("admin", role="admin")
        session.add(admin)
        integration = _openrouter(owner=None, company=True)
        settings_provider = LLMProviderConfig(provider="openrouter", api_key_enc=encrypt_value(_PLATFORM_KEY))
        session.add_all([integration, settings_provider])
        await session.commit()

        for used_key in (_API_KEY, _PLATFORM_KEY):
            hash_ = client.key_hash(used_key)
            delete = console.delete_key(str(integration.id), hash_, None, user=admin, db=session, _ai_gate=None)
            disable = console.update_key(
                str(integration.id), hash_, UpdateKeyRequest(disabled=True), None, user=admin, db=session, _ai_gate=None
            )
            assert (await _status(delete), await _status(disable)) == (409, 409)
        assert fake.made("delete_key") == fake.made("update_key") == []

        # Renaming the integration's own key is fine; disabling another key too.
        await console.update_key(
            str(integration.id), client.key_hash(_API_KEY), UpdateKeyRequest(name="Renamed"), None,
            user=admin, db=session, _ai_gate=None,
        )
        await console.delete_key(str(integration.id), _OTHER_HASH, None, user=admin, db=session, _ai_gate=None)
        assert fake.made("update_key") == [("update_key", client.key_hash(_API_KEY), {"name": "Renamed"})]
        assert fake.made("delete_key") == [("delete_key", _OTHER_HASH)]
        # A malformed hash names no key: the unknown key's 404, nothing sent.
        assert await _status(
            console.delete_key(str(integration.id), "../credits", None, user=admin, db=session, _ai_gate=None)
        ) == 404
        assert len(fake.made("delete_key")) == 1


# ---------------------------------------------------------------------------
# Feeding the platform LLM provider
# ---------------------------------------------------------------------------


async def test_an_admin_feeds_the_platform_provider_from_a_company_managed_integration(monkeypatch):
    _FakeOpenRouter().install(monkeypatch)
    async with (
        scratch_database("openrouterconsole", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        owner, admin = _user("owner"), _user("admin", role="admin")
        session.add_all([owner, admin])
        await session.flush()
        company = _openrouter(owner=None, company=True)
        personal = _openrouter(owner=admin)
        session.add_all([company, personal])
        await session.commit()

        selection = LinkProviderRequest(
            available_models=["anthropic/claude-sonnet-5", "openai/gpt-4o"],
            default_model="anthropic/claude-sonnet-5",
        )
        # A personal integration's key is its owner's, even an admin's.
        assert await _status(
            console.link_provider(str(personal.id), selection, None, user=admin, db=session, _ai_gate=None)
        ) == 409
        # A model the catalog does not list is refused before anything is written.
        unknown = LinkProviderRequest(available_models=["mistralai/not-a-model"])
        assert await _status(
            console.link_provider(str(company.id), unknown, None, user=admin, db=session, _ai_gate=None)
        ) == 422
        assert (await session.execute(select(LLMProviderConfig))).scalars().all() == []

        created = await console.link_provider(str(company.id), selection, None, user=admin, db=session, _ai_gate=None)
        config = await session.get(LLMProviderConfig, uuid.UUID(created.id))
        assert created.source_integration_id == str(company.id)
        assert config.provider == "openrouter"
        assert decrypt_value(config.api_key_enc) == _API_KEY
        assert config.available_models == ["anthropic/claude-sonnet-5", "openai/gpt-4o"]
        assert config.default_model == "anthropic/claude-sonnet-5"
        assert config.context_windows == {"anthropic/claude-sonnet-5": 200_000, "openai/gpt-4o": 128_000}

        # One provider per integration.
        assert await _status(
            console.link_provider(str(company.id), selection, None, user=admin, db=session, _ai_gate=None)
        ) == 409

        # Picking models: an admin's window is kept, a new model gets
        # OpenRouter's, and the default follows the selection.
        config.context_windows = {**config.context_windows, "openai/gpt-4o": 64_000}
        await session.commit()
        updated = await console.update_provider_models(
            str(company.id), created.id,
            ProviderModelsRequest(available_models=["openai/gpt-4o", "google/gemini-3.8-flash"]),
            None, user=admin, db=session, _ai_gate=None,
        )
        assert updated.available_models == ["openai/gpt-4o", "google/gemini-3.8-flash"]
        assert updated.default_model == "openai/gpt-4o"
        assert updated.context_windows == {
            "anthropic/claude-sonnet-5": 200_000,
            "openai/gpt-4o": 64_000,
            "google/gemini-3.8-flash": 1_048_576,
        }
        # A model already offered may stay after OpenRouter retired it; a new
        # one outside the catalog may not be added.
        retired = await session.get(LLMProviderConfig, uuid.UUID(created.id))
        retired.available_models = [*retired.available_models, "retired/old-model"]
        await session.commit()
        kept = await console.update_provider_models(
            str(company.id), created.id,
            ProviderModelsRequest(available_models=["openai/gpt-4o", "retired/old-model"]),
            None, user=admin, db=session, _ai_gate=None,
        )
        assert kept.available_models == ["openai/gpt-4o", "retired/old-model"]
        assert await _status(
            console.update_provider_models(
                str(company.id), created.id, ProviderModelsRequest(available_models=["openai/gpt-4o", "brand/new-unknown"]),
                None, user=admin, db=session, _ai_gate=None,
            )
        ) == 422

        # Unlinking keeps the provider running on the key it holds.
        await console.unlink_provider(str(company.id), created.id, None, user=admin, db=session, _ai_gate=None)
        await session.refresh(config)
        assert (config.source_integration_id, decrypt_value(config.api_key_enc)) == (None, _API_KEY)


async def test_a_settings_provider_is_adopted_unless_the_environment_owns_it(monkeypatch):
    _FakeOpenRouter().install(monkeypatch)
    async with (
        scratch_database("openrouterconsole", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        admin = _user("admin", role="admin")
        integration = _openrouter(owner=None, company=True)
        made_in_settings = LLMProviderConfig(
            provider="openrouter", api_key_enc=encrypt_value(_PLATFORM_KEY), available_models=["openai/gpt-4o"]
        )
        from_env = LLMProviderConfig(provider="openrouter", api_key_enc=encrypt_value("sk-or-env"), is_from_env=True)
        not_openrouter = LLMProviderConfig(provider="openai", api_key_enc=encrypt_value("sk-openai"))
        session.add_all([admin, integration, made_in_settings, from_env, not_openrouter])
        await session.commit()

        overview = await _overview(admin, integration, session)
        assert [p.id for p in overview.adoptable_providers] == [str(made_in_settings.id)]
        for refused in (from_env, not_openrouter):
            assert await _status(
                console.link_provider(
                    str(integration.id), LinkProviderRequest(provider_config_id=refused.id), None,
                    user=admin, db=session, _ai_gate=None,
                )
            ) == 409

        adopted = await console.link_provider(
            str(integration.id), LinkProviderRequest(provider_config_id=made_in_settings.id), None,
            user=admin, db=session, _ai_gate=None,
        )
        await session.refresh(made_in_settings)
        assert adopted.source_integration_id == str(integration.id)
        assert decrypt_value(made_in_settings.api_key_enc) == _API_KEY
        assert made_in_settings.available_models == ["openai/gpt-4o"]
        assert (await session.get(LLMProviderConfig, from_env.id)).source_integration_id is None


async def test_an_adopted_provider_saves_with_the_models_it_had_in_settings(monkeypatch):
    _FakeOpenRouter().install(monkeypatch)
    async with (
        scratch_database("openrouterconsole", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        admin = _user("admin", role="admin")
        integration = _openrouter(owner=None, company=True)
        # Settings takes any id: an OpenRouter alias, a bare id.
        made_in_settings = LLMProviderConfig(
            provider="openrouter",
            api_key_enc=encrypt_value(_PLATFORM_KEY),
            available_models=["openai/gpt-4o", "~google/gemini-pro-latest", "gpt-4o"],
            default_model="~google/gemini-pro-latest",
        )
        session.add_all([admin, integration, made_in_settings])
        await session.commit()
        await console.link_provider(
            str(integration.id), LinkProviderRequest(provider_config_id=made_in_settings.id), None,
            user=admin, db=session, _ai_gate=None,
        )

        # The console sends the saved models back with the one picked.
        saved = await console.update_provider_models(
            str(integration.id), str(made_in_settings.id),
            ProviderModelsRequest(
                available_models=["openai/gpt-4o", "~google/gemini-pro-latest", "gpt-4o", "anthropic/claude-sonnet-5"],
                default_model="~google/gemini-pro-latest",
            ),
            None, user=admin, db=session, _ai_gate=None,
        )

        await session.refresh(made_in_settings)
        assert made_in_settings.available_models == [
            "openai/gpt-4o", "~google/gemini-pro-latest", "gpt-4o", "anthropic/claude-sonnet-5",
        ]
        assert made_in_settings.default_model == "~google/gemini-pro-latest"
        assert saved.available_models == made_in_settings.available_models
        # A model the save adds still comes from the catalog.
        assert await _status(
            console.update_provider_models(
                str(integration.id), str(made_in_settings.id),
                ProviderModelsRequest(available_models=["openai/gpt-4o", "gpt-5-typed"]),
                None, user=admin, db=session, _ai_gate=None,
            )
        ) == 422
        await session.refresh(made_in_settings)
        assert "gpt-5-typed" not in made_in_settings.available_models


async def test_an_edit_of_the_integration_reaches_the_provider_it_feeds(monkeypatch):
    _FakeOpenRouter().install(monkeypatch)
    async with (
        scratch_database("openrouterconsole", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        admin = _user("admin", role="admin")
        integration = _openrouter(owner=None, company=True)
        session.add_all([admin, integration])
        await session.commit()
        created = await console.link_provider(
            str(integration.id), LinkProviderRequest(available_models=["openai/gpt-4o"]), None,
            user=admin, db=session, _ai_gate=None,
        )
        config_id = uuid.UUID(created.id)

        # A replaced key is copied into the provider, audited, and the provider
        # cache is dropped once the edit is committed.
        after_commit = AfterCommit()
        dropped: list[bool] = []
        monkeypatch.setattr("app.llm.router.invalidate_provider_cache", lambda: dropped.append(True))
        await integrations_mod.update_integration(
            str(integration.id), UpdateIntegrationRequest(api_key="sk-or-v1-rotated"), None,
            user=admin, db=session, after_commit=after_commit,
        )
        config = await session.get(LLMProviderConfig, config_id)
        await session.refresh(config)
        assert decrypt_value(config.api_key_enc) == "sk-or-v1-rotated"
        [audit] = (
            await session.execute(
                select(AuditLog).where(AuditLog.entity_type == "llm_provider", AuditLog.entity_id == config_id,
                                       AuditLog.summary.like("Replaced the API key%"))
            )
        ).scalars().all()
        assert audit.user_id == admin.id
        assert dropped == []
        await after_commit.run()
        assert dropped == [True]

        # An edit that changes no key replaces nothing and drops nothing.
        quiet = AfterCommit()
        await integrations_mod.update_integration(
            str(integration.id), UpdateIntegrationRequest(description="Billing account"), None,
            user=admin, db=session, after_commit=quiet,
        )
        await quiet.run()
        assert dropped == [True]

        # Made private again, the integration stops feeding the provider,
        # which keeps the key it holds.
        await integrations_mod.update_integration(
            str(integration.id), UpdateIntegrationRequest(is_company_managed=False), None,
            user=admin, db=session, after_commit=AfterCommit(),
        )
        await session.refresh(config)
        assert config.source_integration_id is None
        assert decrypt_value(config.api_key_enc) == "sk-or-v1-rotated"

        # Deleting an integration that feeds a provider leaves the provider.
        relinked = _openrouter(owner=None, company=True)
        session.add(relinked)
        config.source_integration_id = relinked.id
        await session.commit()
        await session.delete(relinked)
        await session.commit()
        await session.refresh(config)
        assert config.source_integration_id is None


# ---------------------------------------------------------------------------
# Races and consistency
# ---------------------------------------------------------------------------


async def test_the_in_use_check_and_the_upstream_delete_hold_the_locks_that_change_it(monkeypatch):
    fake = _FakeOpenRouter().install(monkeypatch)
    order: list[str] = []
    from app.services import llm_provider_deletion

    real_lock = llm_provider_deletion.lock_provider_configs

    async def _recording_lock(db, *, shared=False):
        order.append(f"provider configs ({'shared' if shared else 'exclusive'})")
        await real_lock(db, shared=shared)

    monkeypatch.setattr(llm_provider_deletion, "lock_provider_configs", _recording_lock)
    real_delete = client.delete_api_key

    async def _delete(management_key, hash_):
        order.append("upstream delete")
        await real_delete(management_key, hash_)

    monkeypatch.setattr(client, "delete_api_key", _delete)
    async with (
        scratch_database("openrouterconsole", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        admin = _user("admin", role="admin")
        integration = _openrouter(owner=None, company=True)
        session.add_all([admin, integration])
        await session.commit()

        await console.delete_key(str(integration.id), _OTHER_HASH, None, user=admin, db=session, _ai_gate=None)

        # Exclusive: a provider creation (shared) or adoption waits on it.
        assert order == ["provider configs (exclusive)", "upstream delete"]
        assert fake.made("delete_key") == [("delete_key", _OTHER_HASH)]


async def test_a_second_link_is_refused_by_the_database_too(monkeypatch):
    _FakeOpenRouter().install(monkeypatch)
    async with (
        scratch_database("openrouterconsole", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        from sqlalchemy.exc import IntegrityError

        from app.services import openrouter_llm_provider as llm_link

        admin = _user("admin", role="admin")
        integration = _openrouter(owner=None, company=True)
        session.add_all([admin, integration])
        await session.commit()
        # The refused create rolls the session back, which expires the rows.
        integration_id = integration.id
        first = await console.link_provider(
            str(integration_id), LinkProviderRequest(available_models=["openai/gpt-4o"]), None,
            user=admin, db=session, _ai_gate=None,
        )

        # Two creations racing: the second one's check saw no linked config.
        async def _raced(db_, integration_id):
            return []

        monkeypatch.setattr(llm_link, "linked_configs", _raced)
        with pytest.raises(HTTPException) as refused:
            await console.link_provider(
                str(integration_id), LinkProviderRequest(available_models=["openai/gpt-4o"]), None,
                user=admin, db=session, _ai_gate=None,
            )
        assert refused.value.status_code == 409
        configs = (await session.execute(select(LLMProviderConfig))).scalars().all()
        assert [str(c.id) for c in configs] == [first.id]

        # The rule is the database's: a second row naming the integration fails.
        session.add(
            LLMProviderConfig(
                provider="openrouter", api_key_enc=encrypt_value("sk-or-x"), source_integration_id=integration_id
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


async def test_an_update_hook_that_fails_fails_the_edit(monkeypatch):
    _FakeOpenRouter().install(monkeypatch)
    async with (
        scratch_database("openrouterconsole", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        from app.integrations import lifecycle

        admin = _user("admin", role="admin")
        integration = _openrouter(owner=None, company=True)
        session.add_all([admin, integration])
        await session.commit()
        created = await console.link_provider(
            str(integration.id), LinkProviderRequest(available_models=["openai/gpt-4o"]), None,
            user=admin, db=session, _ai_gate=None,
        )

        integration_id, config_id = integration.id, uuid.UUID(created.id)

        async def _broken(db_, integration_, update):
            raise RuntimeError("encryption backend down")

        monkeypatch.setitem(lifecycle._ON_UPDATE, "openrouter", _broken)
        with pytest.raises(HTTPException) as refused:
            await integrations_mod.update_integration(
                str(integration_id), UpdateIntegrationRequest(api_key="sk-or-v1-rotated"), None,
                user=admin, db=session, after_commit=AfterCommit(),
            )
        assert refused.value.status_code == 409
        assert "nothing was saved" in refused.value.detail
        # What get_db does with a raising route: nothing of the edit is kept.
        await session.rollback()
        stored = await session.get(Integration, integration_id)
        config = await session.get(LLMProviderConfig, config_id)
        assert decrypt_value(stored.api_key_enc) == _API_KEY
        assert decrypt_value(config.api_key_enc) == _API_KEY


async def test_an_unreadable_linked_key_is_replaced_by_the_edit_not_refused(monkeypatch):
    """A fed config whose stored key no longer decrypts takes the integration's new key."""
    _FakeOpenRouter().install(monkeypatch)
    async with (
        scratch_database("openrouterconsole", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        admin = _user("admin", role="admin")
        integration = _openrouter(owner=None, company=True)
        session.add_all([admin, integration])
        await session.commit()
        created = await console.link_provider(
            str(integration.id), LinkProviderRequest(available_models=["openai/gpt-4o"]), None,
            user=admin, db=session, _ai_gate=None,
        )
        integration_id, config_id = integration.id, uuid.UUID(created.id)
        # A row encrypted under another Fernet key (a rotated key, a corrupted row).
        config = await session.get(LLMProviderConfig, config_id)
        config.api_key_enc = "gAAAAABunreadable-token"
        await session.commit()

        after_commit = AfterCommit()
        dropped: list[bool] = []
        monkeypatch.setattr("app.llm.router.invalidate_provider_cache", lambda: dropped.append(True))
        await integrations_mod.update_integration(
            str(integration_id), UpdateIntegrationRequest(api_key="sk-or-v1-rotated"), None,
            user=admin, db=session, after_commit=after_commit,
        )
        await session.commit()

        stored = await session.get(Integration, integration_id)
        config = await session.get(LLMProviderConfig, config_id)
        await session.refresh(stored)
        await session.refresh(config)
        assert decrypt_value(stored.api_key_enc) == "sk-or-v1-rotated"
        assert decrypt_value(config.api_key_enc) == "sk-or-v1-rotated"
        assert config.source_integration_id == integration_id
        audits = (
            await session.execute(
                select(AuditLog).where(AuditLog.entity_type == "llm_provider", AuditLog.entity_id == config_id,
                                       AuditLog.summary.like("Replaced the API key%"))
            )
        ).scalars().all()
        assert len(audits) == 1
        assert audits[0].user_id == admin.id
        await after_commit.run()
        assert dropped == [True]
