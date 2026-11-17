"""The administrators' vault provider routes on a real PostgreSQL.

Create, edit, disable, test and delete a provider entirely through the API:
the row is what the resolver then uses, every change is audited, a credential
is written once and never read back, and a provider a secret field uses can
neither be deleted nor renamed.
"""

from __future__ import annotations

import socket
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1 import secret_vaults as routes
from app.core.security import encrypt_value
from app.models.audit_log import AuditLog
from app.models.group import Group
from app.models.integration import Integration
from app.models.secret_vault_provider import SecretVaultProvider
from app.models.user import User
from app.services.vault import store
from app.services.vault.resolver import SecretResolver, get_resolver, set_resolver
from tests.scratch_database import ALL_TABLES, scratch_database
from tests.test_secret_vault_providers import MemoryVault

pytestmark = pytest.mark.integration

TOKEN = "hvs.never-read-back"


@pytest.fixture
def vault_dns(monkeypatch):
    """Stand in for the destination policy's DNS: name -> addresses, any other name does not resolve."""
    from app.services.vault import destinations

    table: dict[str, list[str]] = {}

    async def _resolve(host: str, port: int) -> list:
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "unknown")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port)) for address in table[host]]

    monkeypatch.setattr(destinations, "_resolve", _resolve)
    return table


@pytest.fixture
def memory_providers(monkeypatch, vault_dns):
    from app.services.vault import resolver as resolver_module

    MemoryVault.stores = {"corp": {"secret/xtm/github": {"token": "value"}}}
    monkeypatch.setattr(resolver_module, "_build_provider", lambda config: MemoryVault(config))
    monkeypatch.setattr(routes, "test_connection", _memory_test)
    set_resolver(SecretResolver({}, {}, 300))
    yield
    set_resolver(None)


async def _memory_test(config, provider=None):
    from app.services.vault.checks import test_connection

    return await test_connection(config, MemoryVault(config))


def _admin() -> User:
    return User(id=uuid.uuid4(), email=f"admin-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x", full_name="Admin", role="admin")


async def test_an_administrator_connects_edits_tests_disables_and_deletes_a_provider(memory_providers, monkeypatch):
    async with scratch_database(
        "secretvaultapi", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        admin = _admin()
        group = Group(id=uuid.uuid4(), name="SOC")
        async with db.sessions() as seed:
            seed.add_all([admin, group])
            await seed.commit()

        async def _refresh():
            async with db.sessions() as session:
                get_resolver().apply_database_providers(await store.load_database_configs(session))
            return True

        monkeypatch.setattr(routes, "refresh_vault_providers", _refresh)

        async with db.sessions() as session:
            created = await routes.create_secret_vault(
                routes.SecretVaultProviderCreate(
                    name="corp",
                    display_name="Corporate Vault",
                    type="hashicorp",
                    settings={"address": "https://vault.example.com", "auth_method": "token"},
                    credentials={"token": TOKEN},
                    grants=[routes.VaultGrantModel(principal="group", id=str(group.id), paths=["secret/xtm"])],
                ),
                request=None,
                user=admin,
                db=session,
            )
        assert created.credentials_set == {"secret_id": False, "token": True}
        assert TOKEN not in created.model_dump_json()
        assert created.grants[0].label == "SOC"
        assert "corp" in get_resolver().providers
        assert get_resolver().policy("corp").admits(
            store.VaultPrincipal(is_admin=False, group_ids=frozenset({str(group.id)})), "secret/xtm/github"
        )

        async with db.sessions() as session:
            with pytest.raises(HTTPException) as taken:
                await routes.create_secret_vault(
                    routes.SecretVaultProviderCreate(
                        name="corp", display_name="Again", type="aws", settings={}, credentials={}
                    ),
                    request=None,
                    user=admin,
                    db=session,
                )
        assert taken.value.status_code == 409

        async with db.sessions() as session:
            report = await routes.test_secret_vault(str(created.id), request=None, user=admin, db=session)
        assert report.ok and [step.step for step in report.steps] == ["configuration", "login", "list", "read"]

        async with db.sessions() as session:
            listed = await routes.list_secret_vaults(db=session)
        [shown] = listed.providers
        assert shown.status == "connected" and shown.last_check_at is not None
        assert TOKEN not in listed.model_dump_json()

        # A secret field now reads from it: neither a rename nor a delete is accepted.
        async with db.sessions() as session:
            session.add(
                Integration(
                    name="GitHub",
                    provider="github",
                    user_id=admin.id,
                    api_key_enc=encrypt_value("vault://corp/secret/xtm/github#token"),
                )
            )
            await session.commit()
        async with db.sessions() as session:
            with pytest.raises(HTTPException) as renamed:
                await routes.update_secret_vault(
                    str(created.id), routes.SecretVaultProviderUpdate(name="corporate"), request=None, user=admin, db=session
                )
        assert renamed.value.status_code == 409 and renamed.value.detail["uses"][0]["kind"] == "integration"
        async with db.sessions() as session:
            with pytest.raises(HTTPException) as deleted:
                await routes.delete_secret_vault(str(created.id), request=None, user=admin, db=session)
        assert deleted.value.status_code == 409
        async with db.sessions() as session:
            uses = await routes.secret_vault_usage("corp", db=session)
        assert [(use.kind, use.field) for use in uses] == [("integration", "api_key")]

        # Edit: the credential is kept when not sent, its settings change, then the provider is disabled.
        async with db.sessions() as session:
            edited = await routes.update_secret_vault(
                str(created.id),
                routes.SecretVaultProviderUpdate(display_name="Vault", settings={"address": "https://vault-2.example.com"}),
                request=None,
                user=admin,
                db=session,
            )
        assert edited.credentials_set["token"] and edited.settings["address"] == "https://vault-2.example.com"
        assert edited.status == "not_checked"
        async with db.sessions() as session:
            disabled = await routes.update_secret_vault(
                str(created.id), routes.SecretVaultProviderUpdate(enabled=False), request=None, user=admin, db=session
            )
        assert disabled.status == "disabled" and "corp" in get_resolver().disabled

        # Unused again, it can be deleted.
        async with db.sessions() as session:
            for row in (await session.execute(select(Integration))).scalars():
                await session.delete(row)
            await session.commit()
        async with db.sessions() as session:
            await routes.delete_secret_vault(str(created.id), request=None, user=admin, db=session)
        assert "corp" not in get_resolver().known_names()

        async with db.sessions() as session:
            remaining = (await session.execute(select(SecretVaultProvider))).scalars().all()
            actions = [entry.action for entry in (await session.execute(select(AuditLog).order_by(AuditLog.created_at))).scalars()]
        assert remaining == []
        assert actions == ["create", "test", "update", "disable", "delete"]


async def test_an_edit_answers_its_use_count_without_scanning_every_secret_again(memory_providers, monkeypatch):
    """The list and an edit share one recent scan; a delete still scans now, so a new use is never missed."""
    from app.services.vault import usage

    scans = 0
    real_scan = usage._scan_reference_uses

    async def _counted(session):
        nonlocal scans
        scans += 1
        return await real_scan(session)

    monkeypatch.setattr(usage, "_scan_reference_uses", _counted)
    async with scratch_database(
        "secretvaultscan", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        admin = _admin()
        async with db.sessions() as seed:
            seed.add(admin)
            await seed.commit()

        async def _refresh():
            async with db.sessions() as session:
                get_resolver().apply_database_providers(await store.load_database_configs(session))
            return True

        monkeypatch.setattr(routes, "refresh_vault_providers", _refresh)
        async with db.sessions() as session:
            created = await routes.create_secret_vault(
                routes.SecretVaultProviderCreate(
                    name="corp",
                    display_name="Corp",
                    type="hashicorp",
                    settings={"address": "https://vault.example.com", "auth_method": "token"},
                    credentials={"token": TOKEN},
                ),
                request=None,
                user=admin,
                db=session,
            )
            session.add(
                Integration(
                    name="GitHub", provider="github", user_id=admin.id, api_key_enc=encrypt_value("vault://corp/secret/xtm/github#token")
                )
            )
            await session.commit()
        assert scans == 0

        async with db.sessions() as session:
            listed = await routes.list_secret_vaults(db=session)
        assert [provider.usage_count for provider in listed.providers] == [1]
        assert scans == 1

        async with db.sessions() as session:
            edited = await routes.update_secret_vault(
                str(created.id), routes.SecretVaultProviderUpdate(display_name="Corporate"), request=None, user=admin, db=session
            )
        assert edited.usage_count == 1
        assert scans == 1

        async with db.sessions() as session:
            with pytest.raises(HTTPException) as refused:
                await routes.delete_secret_vault(str(created.id), request=None, user=admin, db=session)
        assert refused.value.status_code == 409
        assert scans == 2


async def test_a_database_provider_cannot_take_the_name_of_an_environment_one(memory_providers):
    from tests.test_vault_references import FakeVault

    set_resolver(SecretResolver({"corp": FakeVault("corp")}, {}, 300))
    async with scratch_database(
        "secretvaultname", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        admin = _admin()
        async with db.sessions() as seed:
            seed.add(admin)
            await seed.commit()
        async with db.sessions() as session:
            with pytest.raises(HTTPException) as refused:
                await routes.create_secret_vault(
                    routes.SecretVaultProviderCreate(
                        name="corp",
                        display_name="Corp",
                        type="hashicorp",
                        settings={"address": "https://vault.example.com"},
                        credentials={"token": TOKEN},
                    ),
                    request=None,
                    user=admin,
                    db=session,
                )
        assert refused.value.status_code == 409
        assert "declared in the environment" in refused.value.detail


async def test_a_provider_connected_in_the_ui_may_not_point_at_an_internal_or_metadata_address(memory_providers, vault_dns, monkeypatch):
    """Create, update, the draft test and the stored test refuse it (422), a private address is accepted."""
    from app.services.vault.destinations import REFUSED_DETAIL

    async with scratch_database(
        "secretvaultdest", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        admin = _admin()
        async with db.sessions() as seed:
            seed.add(admin)
            await seed.commit()

        async def _refresh():
            async with db.sessions() as session:
                get_resolver().apply_database_providers(await store.load_database_configs(session))
            return True

        monkeypatch.setattr(routes, "refresh_vault_providers", _refresh)
        vault_dns["metadata.example.com"] = ["169.254.169.254"]
        vault_dns["vault.corp.internal"] = ["10.0.0.6"]

        def _create(name: str, kind: str, values: dict[str, str]) -> routes.SecretVaultProviderCreate:
            credentials = {"hashicorp": {"token": TOKEN}, "onepassword": {"token": TOKEN}}.get(kind, {})
            return routes.SecretVaultProviderCreate(
                name=name, display_name=name, type=kind, settings=values, credentials=credentials
            )

        refused_drafts = [
            ("hashicorp", {"address": "http://169.254.169.254"}),
            ("hashicorp", {"address": "https://metadata.example.com:8200"}),
            ("hashicorp", {"address": "http://127.0.0.1:8200"}),
            ("onepassword", {"address": "http://[::ffff:169.254.169.254]"}),
            ("aws", {"region": "eu-west-1", "endpoint_url": "http://169.254.169.254"}),
        ]
        for kind, values in refused_drafts:
            async with db.sessions() as session:
                with pytest.raises(HTTPException) as created:
                    await routes.create_secret_vault(_create("refused", kind, values), request=None, user=admin, db=session)
            assert (created.value.status_code, created.value.detail) == (422, REFUSED_DETAIL)
            async with db.sessions() as session:
                with pytest.raises(HTTPException) as tested:
                    await routes.test_secret_vault_draft(
                        routes.SecretVaultDraftTest(**_create("draft", kind, values).model_dump()),
                        request=None,
                        user=admin,
                        db=session,
                    )
            assert (tested.value.status_code, tested.value.detail) == (422, REFUSED_DETAIL)

        # A private address, spelled or resolved, is where a vault normally lives.
        async with db.sessions() as session:
            private = await routes.create_secret_vault(
                _create("corp", "hashicorp", {"address": "https://10.0.0.5:8200"}), request=None, user=admin, db=session
            )
        async with db.sessions() as session:
            named = await routes.create_secret_vault(
                _create("named", "hashicorp", {"address": "https://vault.corp.internal:8200"}), request=None, user=admin, db=session
            )

        async with db.sessions() as session:
            with pytest.raises(HTTPException) as updated:
                await routes.update_secret_vault(
                    str(private.id),
                    routes.SecretVaultProviderUpdate(settings={"address": "http://169.254.169.254"}),
                    request=None,
                    user=admin,
                    db=session,
                )
        assert (updated.value.status_code, updated.value.detail) == (422, REFUSED_DETAIL)

        # The name resolved to a private address when it was saved; by the test it answers a metadata one.
        vault_dns["vault.corp.internal"] = ["169.254.169.254"]
        async with db.sessions() as session:
            with pytest.raises(HTTPException) as stored_test:
                await routes.test_secret_vault(str(named.id), request=None, user=admin, db=session)
        assert (stored_test.value.status_code, stored_test.value.detail) == (422, REFUSED_DETAIL)
        # An edit that keeps the address is not judged again: the provider can still be disabled.
        async with db.sessions() as session:
            disabled = await routes.update_secret_vault(
                str(named.id), routes.SecretVaultProviderUpdate(enabled=False), request=None, user=admin, db=session
            )
        assert disabled.status == "disabled"

        async with db.sessions() as session:
            rows = {row.name: row for row in (await session.execute(select(SecretVaultProvider))).scalars()}
            actions = [entry.action for entry in (await session.execute(select(AuditLog).order_by(AuditLog.created_at))).scalars()]
        assert sorted(rows) == ["corp", "named"]
        assert rows["corp"].settings == {"address": "https://10.0.0.5:8200"}
        assert rows["named"].last_check_at is None
        assert actions == ["create", "create", "disable"]
