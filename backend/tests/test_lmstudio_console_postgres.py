"""The LM Studio console on a real PostgreSQL: who may do what, the provider it feeds, and where that provider sends.

Runs the real routes of ``app/api/v1/lmstudio.py`` (and the real integration
update route, for the edits the provider follows) against an isolated temporary
database, with LM Studio itself replaced by a recording fake: no request leaves
the process. The provider's resolution (``resolve_target``) reads the same rows.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.deps import AfterCommit
from app.api.v1 import integrations as integrations_mod
from app.api.v1 import lmstudio as console
from app.api.v1.schemas.integration import UpdateIntegrationRequest
from app.api.v1.schemas.lmstudio import (
    DownloadRequest,
    LinkProviderRequest,
    LoadRequest,
    ProviderModelsRequest,
    UnloadRequest,
)
from app.core.egress import current_route
from app.core.security import decrypt_value, encrypt_value
from app.integrations.providers import lmstudio as client
from app.models.audit_log import AuditLog
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.llm_config import LLMProviderConfig
from app.models.relay import RelayGroup
from app.models.user import User
from app.services import lmstudio_llm_provider as llm_link
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_TOKEN = "lm-studio-token"


def _server_model(key: str, model_type: str = "llm", *, loaded: list[int] = (), max_context: int = 131_072) -> dict:
    return {
        "key": key,
        "type": model_type,
        "display_name": key,
        "publisher": key.split("/")[0],
        "architecture": None,
        "format": "gguf",
        "quantization": "Q4_K_M",
        "bits_per_weight": 4.0,
        "size_bytes": 1,
        "params": None,
        "max_context_length": max_context,
        "vision": False,
        "tool_use": True,
        "reasoning_options": [],
        "description": None,
        "loaded_instances": [
            {"id": f"{key}:{i}", "context_length": ctx, "flash_attention": None, "parallel": None}
            for i, ctx in enumerate(loaded, start=1)
        ],
    }


_SERVER = [
    _server_model("openai/gpt-oss-20b", loaded=[16_384, 8_192]),
    _server_model("qwen/qwen3-4b", max_context=32_768),
    _server_model("text-embedding-nomic-embed-text-v1.5", "embedding", max_context=2_048),
]


class _FakeLMStudio:
    """Stands in for the server; records every call and the route it ran on."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def install(self, monkeypatch) -> _FakeLMStudio:
        async def list_models(connection):
            self.calls.append(("list", connection, current_route()))
            return [dict(m) for m in _SERVER]

        async def load_model(connection, model, **options):
            self.calls.append(("load", model, options, current_route()))
            return {"instance_id": model, "type": "llm", "load_time_seconds": 3.0}

        async def unload_model(connection, instance_id):
            self.calls.append(("unload", instance_id, current_route()))
            return instance_id

        async def download_model(connection, model, *, quantization=None):
            self.calls.append(("download", model, quantization, current_route()))
            return {"job_id": "job_1", "status": "downloading", "total_size_bytes": 10}

        async def download_status(connection, job_id):
            self.calls.append(("status", job_id, current_route()))
            return {"job_id": job_id, "status": "completed", "downloaded_bytes": 10}

        for name, fake in list(locals().items()):
            if callable(fake) and name != "self":
                monkeypatch.setattr(client, name, fake)
        return self

    def made(self, kind: str) -> list[tuple]:
        return [call for call in self.calls if call[0] == kind]


def _user(label: str, *, role: str = "user") -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{label}-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name=label.title(),
        role=role,
    )


def _relay_group() -> RelayGroup:
    return RelayGroup(id=uuid.uuid4(), name="GPU zone", slug=f"gpu-{uuid.uuid4().hex[:6]}", is_company_managed=True)


def _lmstudio(*, owner: User | None, group: RelayGroup | None, company: bool = False, **sharing) -> Integration:
    return Integration(
        id=uuid.uuid4(),
        user_id=None if company else owner.id,
        provider="lmstudio",
        auth_type="none",
        name="GPU box",
        status="connected",
        is_company_managed=company,
        relay_group_id=group.id if group is not None else None,
        extra_config_enc=encrypt_value(json.dumps({"base_url": "http://gpu-host:1234/v1", "api_token": _TOKEN})),
        **sharing,
    )


async def _status(coro) -> int:
    try:
        await coro
    except HTTPException as exc:
        return exc.status_code
    return 200


def _database():
    return scratch_database("lmstudioconsole", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True)


# ---------------------------------------------------------------------------
# Who may open the console, and who may change the server
# ---------------------------------------------------------------------------


async def test_the_console_follows_the_manage_rule_and_the_server_holder_changes_it(monkeypatch):
    fake = _FakeLMStudio().install(monkeypatch)
    async with _database() as db, db.sessions() as session:
        owner, admin = _user("owner"), _user("admin", role="admin")
        editor, group_editor, reader, stranger = (_user(n) for n in ("editor", "groupeditor", "reader", "stranger"))
        group = Group(id=uuid.uuid4(), name=f"editors-{uuid.uuid4().hex[:6]}")
        relays = _relay_group()
        session.add_all([owner, admin, editor, group_editor, reader, stranger, group, relays])
        await session.flush()
        session.add(GroupMember(group_id=group.id, user_id=group_editor.id))
        await session.flush()
        integration = _lmstudio(
            owner=owner,
            group=relays,
            shared_user_ids=[str(editor.id), str(reader.id)],
            shared_user_permissions={str(editor.id): "config", str(reader.id): "read_only"},
            shared_group_ids=[str(group.id)],
            shared_group_permissions={str(group.id): "config"},
        )
        session.add(integration)
        await session.commit()
        iid = str(integration.id)

        for user, holder in ((owner, True), (admin, True), (editor, False), (group_editor, False)):
            overview = await console.get_overview(iid, user=user, db=session, _ai_gate=None)
            assert (overview.can_manage_server, overview.can_link_provider) == (holder, False), user.full_name
            assert (overview.routed, overview.base_url, overview.has_api_token) == (True, "http://gpu-host:1234", True)
            listed = await console.get_models(iid, user=user, db=session, _ai_gate=None)
            assert [m.key for m in listed.models] == [m["key"] for m in _SERVER]
        assert overview.link_refusal.startswith("Make this integration company-managed")
        # Every listing ran on the integration's relay group, with its URL and token.
        assert {(c[1].base_url, c[1].token, c[2].group_id) for c in fake.made("list")} == {
            ("http://gpu-host:1234", _TOKEN, relays.id)
        }
        # A read-only grantee sees the integration, not its server: 403. A
        # stranger does not see it at all: the unknown id's 404.
        assert await _status(console.get_overview(iid, user=reader, db=session, _ai_gate=None)) == 403
        assert await _status(console.get_models(iid, user=stranger, db=session, _ai_gate=None)) == 404

        load = LoadRequest(model="qwen/qwen3-4b", context_length=8192)
        for user in (editor, group_editor):
            assert await _status(console.load_model(iid, load, None, user=user, db=session, _ai_gate=None)) == 403
            assert await _status(
                console.unload_model(iid, UnloadRequest(instance_id="qwen/qwen3-4b"), None, user=user, db=session, _ai_gate=None)
            ) == 403
            assert await _status(
                console.start_download(iid, DownloadRequest(model="qwen/qwen3-4b"), None, user=user, db=session, _ai_gate=None)
            ) == 403
        assert fake.made("load") == fake.made("unload") == fake.made("download") == []

        await console.load_model(iid, load, None, user=owner, db=session, _ai_gate=None)
        await console.unload_model(iid, UnloadRequest(instance_id="qwen/qwen3-4b"), None, user=owner, db=session, _ai_gate=None)
        job = await console.start_download(
            iid, DownloadRequest(model="https://huggingface.co/lmstudio-community/Qwen3-4B-GGUF", quantization="Q4_K_M"),
            None, user=owner, db=session, _ai_gate=None,
        )
        # A grantee follows a download it can see.
        status = await console.get_download(iid, job.job_id, user=editor, db=session, _ai_gate=None)
        assert (job.status, status.status) == ("downloading", "completed")
        assert [c[:3] for c in fake.made("load")] == [("load", "qwen/qwen3-4b", {"context_length": 8192})]
        assert all(c[-1].group_id == relays.id for c in fake.calls)

        audits = (
            await session.execute(select(AuditLog).where(AuditLog.entity_id == integration.id).order_by(AuditLog.created_at))
        ).scalars().all()
        assert [a.summary for a in audits] == [
            "Loaded model 'qwen/qwen3-4b' on LM Studio integration 'GPU box'",
            "Unloaded model instance 'qwen/qwen3-4b' on LM Studio integration 'GPU box'",
            "Started downloading model 'https://huggingface.co/lmstudio-community/Qwen3-4B-GGUF' on LM Studio integration 'GPU box'",
        ]
        assert {a.user_id for a in audits} == {owner.id}
        assert _TOKEN not in json.dumps([a.details for a in audits])

        # A revoked share takes the console away at once.
        integration.shared_user_ids = [str(reader.id)]
        integration.shared_user_permissions = {str(reader.id): "read_only"}
        integration.shared_group_ids = []
        await session.commit()
        assert await _status(console.get_models(iid, user=editor, db=session, _ai_gate=None)) == 404
        assert await _status(console.get_models(iid, user=group_editor, db=session, _ai_gate=None)) == 404


async def test_a_company_managed_server_is_an_admins_console(monkeypatch):
    _FakeLMStudio().install(monkeypatch)
    async with _database() as db, db.sessions() as session:
        member, admin = _user("member"), _user("admin", role="admin")
        relays = _relay_group()
        session.add_all([member, admin, relays])
        await session.flush()
        integration = _lmstudio(owner=None, group=relays, company=True)
        session.add(integration)
        await session.commit()

        assert await _status(console.get_overview(str(integration.id), user=member, db=session, _ai_gate=None)) == 403
        overview = await console.get_overview(str(integration.id), user=admin, db=session, _ai_gate=None)
        assert (overview.can_manage_server, overview.can_link_provider, overview.link_refusal) == (True, True, None)


async def test_an_unrouted_integration_reaches_its_server_directly(monkeypatch):
    fake = _FakeLMStudio().install(monkeypatch)
    async with _database() as db, db.sessions() as session:
        admin = _user("admin", role="admin")
        session.add(admin)
        integration = _lmstudio(owner=None, group=None, company=True)
        session.add(integration)
        await session.commit()
        iid = str(integration.id)

        overview = await console.get_overview(iid, user=admin, db=session, _ai_gate=None)
        assert (overview.routed, overview.relay_group_id, overview.server_refusal) == (False, None, None)
        assert (overview.can_link_provider, overview.link_refusal) == (True, None)
        models = await console.get_models(iid, user=admin, db=session, _ai_gate=None)
        assert models.models
        [(kind, connection, route)] = fake.calls
        assert (kind, connection.base_url, route) == ("list", "http://gpu-host:1234", None)


# ---------------------------------------------------------------------------
# Feeding the platform LLM provider, and where the provider sends
# ---------------------------------------------------------------------------


async def test_an_admin_feeds_the_platform_provider_which_follows_the_integration(monkeypatch):
    _FakeLMStudio().install(monkeypatch)
    async with _database() as db, db.sessions() as session:
        monkeypatch.setattr("app.core.database.async_session", db.sessions)
        owner, admin = _user("owner"), _user("admin", role="admin")
        relays, other_relays = _relay_group(), _relay_group()
        session.add_all([owner, admin, relays, other_relays])
        await session.flush()
        company = _lmstudio(owner=None, group=relays, company=True)
        personal = _lmstudio(owner=admin, group=relays)
        session.add_all([company, personal])
        await session.commit()
        cid = str(company.id)

        selection = LinkProviderRequest(
            available_models=["openai/gpt-oss-20b", "qwen/qwen3-4b"], default_model="openai/gpt-oss-20b"
        )
        # A personal integration is its owner's, even an admin's.
        assert await _status(
            console.link_provider(str(personal.id), selection, None, user=admin, db=session, _ai_gate=None)
        ) == 409
        # A model the server does not hold is refused before anything is written.
        assert await _status(
            console.link_provider(cid, LinkProviderRequest(available_models=["meta/not-there"]), None, user=admin, db=session, _ai_gate=None)
        ) == 422
        assert (await session.execute(select(LLMProviderConfig))).scalars().all() == []

        created = await console.link_provider(cid, selection, None, user=admin, db=session, _ai_gate=None)
        config_id = uuid.UUID(created.id)
        config = await session.get(LLMProviderConfig, config_id)
        assert (config.provider, config.source_integration_id) == ("lmstudio", company.id)
        # No URL, no token in the config: the integration keeps them.
        assert decrypt_value(config.api_key_enc) == llm_link.STORED_CREDENTIAL
        assert config.available_models == ["openai/gpt-oss-20b", "qwen/qwen3-4b"]
        # The context a loaded model is served with (its smallest instance),
        # else the model's maximum.
        assert config.context_windows == {"openai/gpt-oss-20b": 8_192, "qwen/qwen3-4b": 32_768}
        listed = await console.get_models(cid, user=admin, db=session, _ai_gate=None)
        assert {m.key for m in listed.models if m.offered} == {"openai/gpt-oss-20b", "qwen/qwen3-4b"}

        # The provider reads the integration through its config: the relay
        # group, the server's root and the token.
        target = await llm_link.resolve_target(config_id)
        assert (target.integration_id, target.route.group_id, target.route.source_type, target.route.source_id) == (
            company.id, relays.id, "integration", str(company.id)
        )
        assert (target.connection.base_url, target.connection.token) == ("http://gpu-host:1234", _TOKEN)

        # Picking models: a new one gets its window, the default follows.
        updated = await console.update_provider_models(
            cid, created.id, ProviderModelsRequest(available_models=["qwen/qwen3-4b", "text-embedding-nomic-embed-text-v1.5"]),
            None, user=admin, db=session, _ai_gate=None,
        )
        assert (updated.default_model, updated.context_windows["text-embedding-nomic-embed-text-v1.5"]) == ("qwen/qwen3-4b", 2_048)
        assert await _status(
            console.update_provider_models(
                cid, created.id, ProviderModelsRequest(available_models=["meta/not-there"]), None, user=admin, db=session, _ai_gate=None,
            )
        ) == 422

        # An edit of the integration is followed on the next request: another
        # relay group, another token.
        after_commit = AfterCommit()
        await integrations_mod.update_integration(
            cid, UpdateIntegrationRequest(
                extra_config={"base_url": "http://gpu-host:1234", "api_token": "rotated-token"},
                relay_group_id=str(other_relays.id),
            ), None,
            user=admin, db=session, after_commit=after_commit,
        )
        await after_commit.run()
        target = await llm_link.resolve_target(config_id)
        assert (target.route.group_id, target.connection.token) == (other_relays.id, "rotated-token")
        await session.refresh(config)
        assert decrypt_value(config.api_key_enc) == llm_link.STORED_CREDENTIAL

        # Taken off its relay group, the integration sends the provider's
        # requests to the server directly.
        after_commit = AfterCommit()
        await integrations_mod.update_integration(
            cid, UpdateIntegrationRequest(relay_group_id=None), None, user=admin, db=session, after_commit=after_commit,
        )
        await after_commit.run()
        await session.refresh(company)
        assert company.relay_group_id is None
        target = await llm_link.resolve_target(config_id)
        assert (target.route, target.connection.base_url) == (None, "http://gpu-host:1234")

        # Made private, the integration stops feeding the provider.
        company.relay_group_id = relays.id
        await session.commit()
        await integrations_mod.update_integration(
            cid, UpdateIntegrationRequest(is_company_managed=False), None, user=admin, db=session, after_commit=AfterCommit(),
        )
        await session.refresh(config)
        assert config.source_integration_id is None
        llm_link.forget_target()
        with pytest.raises(llm_link.TargetRefusedError) as refused:
            await llm_link.resolve_target(config_id)
        assert "not fed by an LM Studio integration" in refused.value.message


async def test_an_unlinked_provider_refuses_at_once_and_can_be_adopted_again(monkeypatch):
    _FakeLMStudio().install(monkeypatch)
    async with _database() as db, db.sessions() as session:
        monkeypatch.setattr("app.core.database.async_session", db.sessions)
        admin = _user("admin", role="admin")
        relays = _relay_group()
        session.add_all([admin, relays])
        await session.flush()
        integration = _lmstudio(owner=None, group=relays, company=True)
        session.add(integration)
        await session.commit()
        iid = str(integration.id)

        created = await console.link_provider(
            iid, LinkProviderRequest(available_models=["qwen/qwen3-4b"]), None, user=admin, db=session, _ai_gate=None
        )
        config_id = uuid.UUID(created.id)
        assert (await llm_link.resolve_target(config_id)).integration_id == integration.id

        # Unlinked: the target this process held is dropped with it.
        await console.unlink_provider(iid, created.id, None, user=admin, db=session, _ai_gate=None)
        with pytest.raises(llm_link.TargetRefusedError):
            await llm_link.resolve_target(config_id)
        overview = await console.get_overview(iid, user=admin, db=session, _ai_gate=None)
        assert [p.id for p in overview.adoptable_providers] == [created.id]

        adopted = await console.link_provider(
            iid, LinkProviderRequest(provider_config_id=config_id), None, user=admin, db=session, _ai_gate=None
        )
        assert adopted.source_integration_id == iid
        assert (await llm_link.resolve_target(config_id)).integration_id == integration.id

        # Deleting the integration unlinks it at the database level.
        await session.delete(integration)
        await session.commit()
        llm_link.forget_target()
        with pytest.raises(llm_link.TargetRefusedError):
            await llm_link.resolve_target(config_id)


async def test_an_lm_studio_integration_is_created_direct_or_routed_and_connected_without_a_token(monkeypatch):
    from app.api.v1.schemas.integration import CreateIntegrationRequest

    async with _database() as db, db.sessions() as session:
        admin = _user("admin", role="admin")
        relays = _relay_group()
        session.add_all([admin, relays])
        await session.commit()

        direct = CreateIntegrationRequest(provider="lmstudio", name="Local box", extra_config={"base_url": "http://localhost:1234"})
        await integrations_mod.create_integration(direct, None, user=admin, db=session, _agentic_gate=None)
        routed = CreateIntegrationRequest(
            provider="lmstudio", name="GPU box", extra_config={"base_url": "http://gpu-host:1234"}, relay_group_id=str(relays.id)
        )
        await integrations_mod.create_integration(routed, None, user=admin, db=session, _agentic_gate=None)
        rows = {r.name: r for r in (await session.execute(select(Integration))).scalars().all()}
        assert {name: (r.status, r.auth_type, r.relay_group_id, r.api_key_enc) for name, r in rows.items()} == {
            "Local box": ("connected", "none", None, None),
            "GPU box": ("connected", "none", relays.id, None),
        }


async def test_settings_never_creates_the_provider_by_hand():
    from app.api.v1.schemas.settings import LLMProviderRequest
    from app.api.v1.settings import create_provider_config

    async with _database() as db, db.sessions() as session:
        admin = _user("admin", role="admin")
        session.add(admin)
        await session.commit()
        with pytest.raises(HTTPException) as refused:
            await create_provider_config(
                session,
                body=LLMProviderRequest(provider="lmstudio", api_key="none||http://10.0.0.5:1234"),
                user=admin,
                request=None,
            )
        assert refused.value.status_code == 400
        assert (await session.execute(select(LLMProviderConfig))).scalars().all() == []
