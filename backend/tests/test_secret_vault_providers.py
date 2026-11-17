"""Vault providers connected in the UI, granted to groups and users, and picked from.

No vault and no database: providers are fakes or talk HTTP through
``httpx.MockTransport``, the resolver is built in memory, and the picker
routes are called directly with a user. Every non-administrator case is
tested as one (rule 19): an administrator short-circuits every grant.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from app.core.secret_refs import parse_secret_reference
from app.core.security import encrypt_value
from app.services import vault
from app.services.vault import store
from app.services.vault.base import ListedSecret, SecretListing, VaultProvider, child_entries
from app.services.vault.checks import test_connection as run_connection_test
from app.services.vault.config import (
    ADMIN_PRINCIPAL,
    GrantPolicy,
    VaultConfigError,
    VaultGrant,
    VaultPrincipal,
    VaultProviderConfig,
    database_config,
    parse_grants,
)
from app.services.vault.errors import VaultReadError
from app.services.vault.resolver import SecretResolver, get_resolver, set_resolver

SOC_GROUP = str(uuid.uuid4())
ALICE = str(uuid.uuid4())
BOB = str(uuid.uuid4())
#: Alice is in the SOC group; Bob is in no group. Neither is an administrator.
ALICE_PRINCIPAL = VaultPrincipal(is_admin=False, user_id=ALICE, group_ids=frozenset({SOC_GROUP}))
BOB_PRINCIPAL = VaultPrincipal(is_admin=False, user_id=BOB)
SECRET_VALUE = "s3cr3t-value-never-shown"


class MemoryVault(VaultProvider):
    """A database provider holding its secrets in a dict, listing them as folders by ``/``."""

    kind = "memory"
    #: The secrets every MemoryVault built from a config reads, by provider name.
    stores: dict[str, dict[str, Any]] = {}

    def __init__(self, config: VaultProviderConfig) -> None:
        super().__init__(config)
        self.reads = 0
        self.fail_login: VaultReadError | None = None
        self.fail_list: VaultReadError | None = None

    @property
    def secrets(self) -> dict[str, Any]:
        return self.stores.setdefault(self.name, {})

    async def read(self, path: str) -> Any:
        self.reads += 1
        if path not in self.secrets:
            raise VaultReadError("not_found", "HTTP 404")
        return self.secrets[path]

    async def authenticate(self) -> None:
        if self.fail_login is not None:
            raise self.fail_login

    async def list_secrets(self, prefix: str, *, page_token: str | None = None, page_size: int = 100) -> SecretListing:
        if self.fail_list is not None:
            raise self.fail_list
        return SecretListing(entries=child_entries(prefix, sorted(self.secrets)))


@pytest.fixture
def memory_vaults(monkeypatch):
    """Build every database provider as a :class:`MemoryVault` holding the secrets given per name."""
    from app.services.vault import resolver as resolver_module

    MemoryVault.stores = {}
    monkeypatch.setattr(resolver_module, "_build_provider", lambda config: MemoryVault(config))
    set_resolver(SecretResolver({}, {}, 300))
    return MemoryVault.stores


def _db_config(name: str = "corp", *, grants: Any = None, enabled: bool = True, browsable: bool = False, **settings: str):
    return database_config(
        name=name,
        type="hashicorp",
        settings={"address": "https://vault.example.com", **settings},
        credentials={"token": "hvs.platform-token"},
        id=str(uuid.uuid4()),
        display_name=name.title(),
        enabled=enabled,
        grants=grants or [],
        browsable=browsable,
    )


def _apply(*configs: VaultProviderConfig) -> SecretResolver:
    resolver = get_resolver()
    resolver.apply_database_providers(list(configs))
    return resolver


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ([{"principal": "team"}], "principal is one of"),
        ([{"principal": "group"}], "id names the group"),
        ([{"principal": "user", "id": ALICE, "paths": "secret/soc"}], "paths is a list"),
        ([{"principal": "group", "id": SOC_GROUP, "paths": ["secret/../x"]}], "not a path prefix"),
        ([{"principal": "user", "id": ALICE}, {"principal": "user", "id": ALICE}], "repeats a grant"),
        ("everyone", "grants is a list"),
    ],
)
def test_a_grant_it_cannot_use_is_refused_naming_it(raw, message):
    with pytest.raises(VaultConfigError) as exc:
        parse_grants(raw)
    assert message in str(exc.value)


def test_administrators_have_every_path_and_no_grant_means_administrators_only():
    policy = GrantPolicy(grants=())
    assert policy.usable_by(ADMIN_PRINCIPAL) and policy.admits(ADMIN_PRINCIPAL, "any/path")
    assert not policy.usable_by(ALICE_PRINCIPAL)
    assert not policy.admits(ALICE_PRINCIPAL, "any/path")
    assert policy.prefixes_for(ALICE_PRINCIPAL) == ()
    assert policy.prefixes_for(ADMIN_PRINCIPAL) is None


def test_a_group_grant_covers_its_members_at_its_prefixes_segment_by_segment():
    policy = GrantPolicy(grants=parse_grants([{"principal": "group", "id": SOC_GROUP, "paths": ["secret/soc"]}]))
    assert policy.usable_by(ALICE_PRINCIPAL)
    assert policy.admits(ALICE_PRINCIPAL, "secret/soc")
    assert policy.admits(ALICE_PRINCIPAL, "secret/soc/github")
    assert not policy.admits(ALICE_PRINCIPAL, "secret/soc-staging/github")
    assert not policy.admits(ALICE_PRINCIPAL, "secret/admin/root")
    assert policy.prefixes_for(ALICE_PRINCIPAL) == (("secret", "soc"),)
    assert not policy.usable_by(BOB_PRINCIPAL)


def test_a_user_grant_and_an_everyone_grant_add_up():
    policy = GrantPolicy(
        grants=parse_grants(
            [
                {"principal": "everyone", "paths": ["shared"]},
                {"principal": "user", "id": BOB, "paths": ["team/bob"]},
            ]
        )
    )
    assert policy.admits(BOB_PRINCIPAL, "team/bob/key") and policy.admits(BOB_PRINCIPAL, "shared/ci")
    assert not policy.admits(ALICE_PRINCIPAL, "team/bob/key")
    assert policy.admits(ALICE_PRINCIPAL, "shared/ci")
    assert set(policy.prefixes_for(BOB_PRINCIPAL)) == {("shared",), ("team", "bob")}
    assert not policy.open_to_everyone_everywhere
    assert GrantPolicy(grants=parse_grants([{"principal": "everyone"}])).open_to_everyone_everywhere


def test_a_disabled_provider_is_usable_by_nobody():
    policy = GrantPolicy(grants=(VaultGrant(principal="everyone"),), enabled=False)
    assert not policy.usable_by(ADMIN_PRINCIPAL)
    assert not policy.admits(ADMIN_PRINCIPAL, "x")
    assert policy.prefixes_for(ADMIN_PRINCIPAL) == ()


# ---------------------------------------------------------------------------
# The resolver: database providers next to the environment's
# ---------------------------------------------------------------------------


async def test_a_database_provider_is_read_like_a_declared_one(memory_vaults):
    memory_vaults["corp"] = {"secret/github": {"token": SECRET_VALUE}}
    resolver = _apply(_db_config())
    assert resolver.sources["corp"] == "database"
    assert vault.reference_problem("vault://corp/secret/github#token") is None
    assert await resolver.read(parse_secret_reference("vault://corp/secret/github#token")) == SECRET_VALUE


async def test_a_disabled_provider_refuses_new_references_and_fails_every_read(memory_vaults):
    memory_vaults["corp"] = {"secret/github": {"token": SECRET_VALUE}}
    resolver = _apply(_db_config(enabled=False))
    assert vault.reference_problem("vault://corp/secret/github#token") == "vault provider 'corp' is disabled"
    with pytest.raises(VaultReadError) as exc:
        await resolver.read(parse_secret_reference("vault://corp/secret/github#token"))
    assert exc.value.reason == "provider_disabled"
    assert "corp" not in resolver.usable_by(ADMIN_PRINCIPAL)


async def test_an_environment_declaration_hides_a_database_provider_of_the_same_name(memory_vaults):
    from tests.test_vault_references import FakeVault

    env = FakeVault("corp", secrets={"secret/github": {"token": "from-the-environment"}})
    set_resolver(SecretResolver({"corp": env}, {}, 300))
    resolver = _apply(_db_config())
    assert resolver.shadowed == {"corp": "an environment declaration of the same name takes precedence"}
    assert resolver.providers["corp"] is env
    assert await resolver.read(parse_secret_reference("vault://corp/secret/github#token")) == "from-the-environment"


async def test_a_grant_change_keeps_the_client_and_its_cache_a_settings_change_rebuilds_it(memory_vaults):
    memory_vaults["corp"] = {"secret/github": {"token": SECRET_VALUE}}
    config = _db_config()
    resolver = _apply(config)
    reference = parse_secret_reference("vault://corp/secret/github#token")
    first = resolver.providers["corp"]
    await resolver.read(reference)

    regranted = _db_config(grants=[{"principal": "everyone", "paths": ["secret"]}])
    resolver.apply_database_providers([regranted])
    assert resolver.providers["corp"] is first
    await resolver.read(reference)
    assert first.reads == 1
    assert resolver.policy("corp").usable_by(BOB_PRINCIPAL)

    moved = _db_config(address="https://vault-2.example.com")
    resolver.apply_database_providers([moved])
    assert resolver.providers["corp"] is not first
    await resolver.read(reference)
    assert resolver.providers["corp"].reads == 1

    resolver.apply_database_providers([])
    assert "corp" not in resolver.known_names()


def test_an_older_read_of_the_providers_never_undoes_a_newer_one(memory_vaults):
    resolver = get_resolver()
    stale = resolver.snapshot_ticket()
    fresh = resolver.snapshot_ticket()
    assert resolver.apply_database_providers([_db_config("corp")], ticket=fresh)
    assert not resolver.apply_database_providers([], ticket=stale)
    assert "corp" in resolver.providers


async def test_a_provider_keeps_its_own_cache_duration(memory_vaults):
    memory_vaults["corp"] = {"secret/github": {"token": SECRET_VALUE}}
    config = database_config(
        name="corp",
        type="hashicorp",
        settings={"address": "https://vault.example.com"},
        credentials={"token": "t"},
        cache_seconds=0,
    )
    resolver = _apply(config)
    reference = parse_secret_reference("vault://corp/secret/github#token")
    await resolver.read(reference)
    await resolver.read(reference)
    assert resolver.providers["corp"].reads == 2
    assert resolver.cache_seconds_for("corp") == 0


# ---------------------------------------------------------------------------
# Writing a reference: the caller's grants (403) and the provider's state (422)
# ---------------------------------------------------------------------------


def test_a_non_admin_writes_a_reference_only_inside_their_grant(memory_vaults):
    _apply(_db_config(grants=[{"principal": "group", "id": SOC_GROUP, "paths": ["secret/soc"]}]))
    inside = {"api_key": "vault://corp/secret/soc/github#token"}
    outside = {"api_key": "vault://corp/secret/admin/root#token"}
    assert vault.secret_fields_writer_problem(inside, principal=ALICE_PRINCIPAL) is None
    assert vault.secret_fields_writer_problem(outside, principal=ALICE_PRINCIPAL) == (
        "api_key: the path is outside the paths you may use on vault provider 'corp'"
    )
    assert vault.secret_fields_writer_problem(inside, principal=BOB_PRINCIPAL) == (
        "api_key: you may not use vault provider 'corp'"
    )
    assert vault.secret_fields_writer_problem(outside, principal=ADMIN_PRINCIPAL) is None


async def test_the_route_check_reads_the_callers_groups_only_when_a_group_is_granted(memory_vaults, monkeypatch):
    from app.api.v1 import helpers
    from app.api.v1.schemas.vault_references import require_vault_writer

    asked: list[Any] = []

    async def _groups(db, user_id):
        asked.append(user_id)
        return [SOC_GROUP] if str(user_id) == ALICE else []

    monkeypatch.setattr(helpers, "get_user_group_ids", _groups)
    alice, bob = SimpleNamespace(id=ALICE, is_admin=False), SimpleNamespace(id=BOB, is_admin=False)

    _apply(_db_config(grants=[{"principal": "user", "id": BOB, "paths": ["team/bob"]}]))
    await require_vault_writer(None, bob, api_key="vault://corp/team/bob/key")
    assert asked == []

    _apply(_db_config(grants=[{"principal": "group", "id": SOC_GROUP, "paths": ["secret/soc"]}]))
    await require_vault_writer(None, alice, api_key="vault://corp/secret/soc/key")
    with pytest.raises(HTTPException) as refused:
        await require_vault_writer(None, bob, api_key="vault://corp/secret/soc/key")
    assert refused.value.status_code == 403
    assert asked == [ALICE, BOB]


def test_an_edit_may_not_repoint_an_entity_keeping_a_reference_outside_the_editors_grant(memory_vaults):
    _apply(_db_config(grants=[{"principal": "group", "id": SOC_GROUP, "paths": ["secret/soc"]}]))
    row = SimpleNamespace(id=str(uuid.uuid4()), api_key_enc=encrypt_value("vault://corp/secret/admin/root#token"), client_secret_enc=None)
    row.__tablename__ = "integrations"
    problem = vault.retained_reference_writer_problem(row, principal=ALICE_PRINCIPAL)
    assert problem is not None and problem.startswith("api_key holds a reference to vault provider 'corp', you may not write")
    assert vault.retained_reference_writer_problem(row, replaced={"api_key_enc"}, principal=ALICE_PRINCIPAL) is None
    granted = SimpleNamespace(id=row.id, api_key_enc=encrypt_value("vault://corp/secret/soc/x#k"), client_secret_enc=None)
    granted.__tablename__ = "integrations"
    assert vault.retained_reference_writer_problem(granted, principal=ALICE_PRINCIPAL) is None


def test_an_entity_shows_where_its_reference_reads_only_to_a_viewer_who_may_use_that_path(memory_vaults):
    from app.api.v1.schemas.vault_references import vault_status

    _apply(_db_config(grants=[{"principal": "group", "id": SOC_GROUP, "paths": ["secret/soc"]}]))
    row = SimpleNamespace(
        id=str(uuid.uuid4()),
        api_key_enc=encrypt_value("vault://corp/secret/soc/github#token"),
        client_secret_enc=encrypt_value("vault://corp/secret/admin/root#secret"),
    )
    row.__tablename__ = "integrations"

    shown = vault_status(row, ALICE_PRINCIPAL)
    assert shown.locations["api_key"][0].model_dump() == {
        "provider": "corp",
        "provider_display_name": "Corp",
        "path": "secret/soc/github",
        "key": "token",
        "entry": None,
    }
    hidden = shown.locations["client_secret"][0]
    assert (hidden.provider, hidden.path, hidden.key) == ("corp", None, None)
    assert vault_status(row).locations == {}


# ---------------------------------------------------------------------------
# The picker routes
# ---------------------------------------------------------------------------


def _user(ident: str, *, admin: bool = False) -> SimpleNamespace:
    return SimpleNamespace(id=ident, is_admin=admin, email=f"{ident}@example.com", full_name=ident)


@pytest.fixture
def soc_vault(memory_vaults, monkeypatch):
    from app.api.v1 import helpers

    async def _groups(db, user_id):
        return [SOC_GROUP] if str(user_id) == ALICE else []

    monkeypatch.setattr(helpers, "get_user_group_ids", _groups)
    memory_vaults["corp"] = {
        "secret/soc/github": {"token": SECRET_VALUE, "user": "bot"},
        "secret/soc/slack": SECRET_VALUE,
        "secret/admin/root": {"password": SECRET_VALUE},
        "secret/shared/ci": {"token": SECRET_VALUE},
    }
    _apply(
        _db_config(grants=[{"principal": "group", "id": SOC_GROUP, "paths": ["secret/soc"]}], browsable=True),
        _db_config("hidden", grants=[]),
    )
    return memory_vaults


async def test_the_picker_offers_a_user_only_the_providers_and_paths_they_are_granted(soc_vault):
    from app.api.v1.secret_vaults import list_picker_providers

    alice = await list_picker_providers(request=None, user=_user(ALICE), db=None)
    assert [(p.name, p.browsable, p.paths) for p in alice] == [("corp", True, ["secret/soc"])]
    assert await list_picker_providers(request=None, user=_user(BOB), db=None) == []
    admin = await list_picker_providers(request=None, user=_user("admin", admin=True), db=None)
    assert {p.name: p.paths for p in admin} == {"corp": None, "hidden": None}


async def test_browsing_lists_only_the_names_inside_the_grant_and_the_folders_leading_to_it(soc_vault):
    from app.api.v1.secret_vaults import list_picker_secrets

    async def _list(user, prefix):
        return await list_picker_secrets(
            name="corp", request=None, prefix=prefix, page_token=None, page_size=100, user=user, db=None
        )

    top = await _list(_user(ALICE), "secret")
    assert [(e.path, e.folder) for e in top.entries] == [("secret/soc/", True)]
    inside = await _list(_user(ALICE), "secret/soc/")
    assert [e.path for e in inside.entries] == ["secret/soc/github", "secret/soc/slack"]
    with pytest.raises(HTTPException) as outside:
        await _list(_user(ALICE), "secret/admin")
    assert outside.value.status_code == 403
    with pytest.raises(HTTPException) as ungranted:
        await _list(_user(BOB), "")
    assert ungranted.value.status_code == 404
    everything = await _list(_user("admin", admin=True), "secret")
    assert {e.path for e in everything.entries} == {"secret/admin/", "secret/shared/", "secret/soc/"}
    assert SECRET_VALUE not in json.dumps(everything.model_dump())


async def test_a_provider_that_is_not_browsable_is_picked_by_typing_its_path(soc_vault):
    from app.api.v1.secret_vaults import list_picker_secrets

    _apply(_db_config(grants=[{"principal": "group", "id": SOC_GROUP, "paths": ["secret/soc"]}], browsable=False))
    with pytest.raises(HTTPException) as refused:
        await list_picker_secrets(name="corp", request=None, prefix="", page_token=None, page_size=100, user=_user(ALICE), db=None)
    assert refused.value.status_code == 403


async def test_a_vault_refusing_to_list_says_so_and_the_picker_falls_back_to_typing(soc_vault):
    from app.api.v1.secret_vaults import list_picker_secrets

    get_resolver().providers["corp"].fail_list = VaultReadError("access_denied", "HTTP 403")
    listing = await list_picker_secrets(
        name="corp", request=None, prefix="secret/soc", page_token=None, page_size=100, user=_user(ALICE), db=None
    )
    assert listing.entries == []
    assert listing.unavailable.reason == "access_denied"


async def test_the_keys_of_a_secret_are_its_entry_names_never_its_values(soc_vault):
    from app.api.v1.secret_vaults import list_picker_keys

    keys = await list_picker_keys(name="corp", request=None, path="secret/soc/github", user=_user(ALICE), db=None)
    assert keys.keys == ["token", "user"] and not keys.single_value
    single = await list_picker_keys(name="corp", request=None, path="secret/soc/slack", user=_user(ALICE), db=None)
    assert single.keys == [] and single.single_value
    for answer in (keys, single):
        assert SECRET_VALUE not in json.dumps(answer.model_dump())
    with pytest.raises(HTTPException) as outside:
        await list_picker_keys(name="corp", request=None, path="secret/admin/root", user=_user(ALICE), db=None)
    assert outside.value.status_code == 403


async def test_a_check_resolves_on_the_server_and_answers_without_the_value(soc_vault):
    from app.api.v1.secret_vaults import CheckRequest, check_reference

    async def _check(reference, user):
        return await check_reference(body=CheckRequest(reference=reference), request=None, user=user, db=None)

    ok = await _check("vault://corp/secret/soc/github#token", _user(ALICE))
    assert ok.ok and SECRET_VALUE not in json.dumps(ok.model_dump())
    missing = await _check("vault://corp/secret/soc/github#nope", _user(ALICE))
    assert (missing.ok, missing.reason) == (False, "not_found")
    with pytest.raises(HTTPException) as outside:
        await _check("vault://corp/secret/admin/root#password", _user(ALICE))
    assert outside.value.status_code == 403
    with pytest.raises(HTTPException) as hidden:
        await _check("vault://hidden/x#y", _user(ALICE))
    assert hidden.value.status_code == 404


# ---------------------------------------------------------------------------
# What an administrator's page shows: never a credential
# ---------------------------------------------------------------------------


async def test_the_provider_list_says_which_credentials_are_set_never_their_value(memory_vaults):
    from app.api.v1.secret_vaults import _row_out

    _apply(_db_config())

    class _NoGrantsDB:
        async def execute(self, statement):
            return SimpleNamespace(all=lambda: [])

    row = SimpleNamespace(
        id=uuid.uuid4(),
        name="corp",
        display_name="Corp",
        type="hashicorp",
        settings={"address": "https://vault.example.com", "auth_method": "approle", "role_id": "role"},
        credentials_enc=store.encrypt_credentials({"SECRET_ID": SECRET_VALUE}),
        enabled=True,
        grants=[],
        browsable=False,
        cache_seconds=None,
        last_check_at=None,
        last_check_ok=None,
        last_check_reason=None,
        last_check_detail=None,
        created_at=None,
        updated_at=None,
        updated_by_id=None,
    )
    shown = await _row_out(_NoGrantsDB(), row, {}, {})
    assert shown.credentials_set == {"secret_id": True, "token": False}
    assert shown.settings["role_id"] == "role"
    assert SECRET_VALUE not in shown.model_dump_json()
    assert shown.status == "not_checked"


def _provider_row(name: str, grants: list[dict[str, Any]]) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(), name=name, display_name=name.title(), type="hashicorp",
        settings={"address": "https://vault.example.com", "auth_method": "token"},
        credentials_enc=store.encrypt_credentials({"TOKEN": SECRET_VALUE}), enabled=True, grants=grants,
        browsable=False, cache_seconds=None, last_check_at=None, last_check_ok=None, last_check_reason=None,
        last_check_detail=None, created_at=None, updated_at=None, updated_by_id=None,
    )


async def test_the_provider_list_names_the_grants_of_every_provider_in_one_read(memory_vaults, monkeypatch):
    """The grant labels of the whole list are read once, not once per provider."""
    from app.api.v1 import secret_vaults as routes

    rows = [
        _provider_row("alpha", [{"principal": "group", "id": SOC_GROUP, "paths": []}]),
        _provider_row("beta", [{"principal": "user", "id": ALICE, "paths": ["secret/x"]}]),
        _provider_row("gamma", []),
    ]
    calls: list[list[dict[str, Any]]] = []

    async def _labels(db, grants):
        calls.append(list(grants))
        return {("group", SOC_GROUP): "SOC", ("user", ALICE): "Alice"}

    async def _no_uses(db):
        from app.services.vault.usage import ReferenceScan

        return ReferenceScan()

    async def _no_users(db, rows):
        return {}

    class _RowsDB:
        async def execute(self, statement):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    monkeypatch.setattr(routes, "_grant_labels", _labels)
    monkeypatch.setattr(routes, "recent_reference_uses", _no_uses)
    monkeypatch.setattr(routes, "_user_names", _no_users)
    listed = await routes.list_secret_vaults(db=_RowsDB())

    assert calls == [[{"principal": "group", "id": SOC_GROUP, "paths": []}, {"principal": "user", "id": ALICE, "paths": ["secret/x"]}]]
    assert [[grant.label for grant in provider.grants] for provider in listed.providers] == [["SOC"], ["Alice"], []]


async def test_the_page_counts_reuse_a_recent_scan_and_the_guards_read_a_fresh_one(monkeypatch):
    """A use count reads the last scan while it is recent; a delete or rename guard always scans again."""
    from app.services.vault import usage

    scans: list[object] = []

    async def _scan(db):
        scans.append(db)
        return usage.ReferenceScan(uses=[usage.ReferenceUse("integration", "1", "GitHub", "api_key", ("corp",))])

    monkeypatch.setattr(usage, "_scan_reference_uses", _scan)
    clock = [1000.0]
    monkeypatch.setattr(usage, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    first = await usage.recent_reference_uses("db")
    again = await usage.recent_reference_uses("db")
    assert again is first and len(scans) == 1

    assert [use.name for use in await usage.provider_uses("db", "corp")] == ["GitHub"]
    assert [use.name for use in await usage.provider_uses("db", "corp")] == ["GitHub"]
    assert len(scans) == 3

    clock[0] += usage.SCAN_CACHE_SECONDS
    await usage.recent_reference_uses("db")
    assert len(scans) == 4
    usage.clear_reference_scan_cache()
    await usage.recent_reference_uses("db")
    assert len(scans) == 5


class _PagedVault(MemoryVault):
    """A vault answering its listing a page at a time, like every real provider."""

    async def list_secrets(self, prefix: str, *, page_token: str | None = None, page_size: int = 100) -> SecretListing:
        from app.services.vault.base import page_entries

        return page_entries(child_entries(prefix, sorted(self.secrets)), page_token, page_size)


async def test_the_picker_listing_walks_every_page_of_a_large_vault(memory_vaults, monkeypatch):
    """A vault holding 150 secrets is listed whole by following ``next_page_token``."""
    from app.api.v1.secret_vaults import list_picker_secrets
    from app.services.vault import resolver as resolver_module

    monkeypatch.setattr(resolver_module, "_build_provider", lambda config: _PagedVault(config))
    memory_vaults["corp"] = {f"secret/app/s{index:03d}": {"token": SECRET_VALUE} for index in range(150)}
    _apply(_db_config(grants=[{"principal": "everyone"}], browsable=True))

    seen: list[str] = []
    token: str | None = None
    pages = 0
    while True:
        listing = await list_picker_secrets(
            name="corp", request=None, prefix="secret/app/", page_token=token, page_size=100, user=_user(BOB), db=None
        )
        pages += 1
        seen.extend(entry.path for entry in listing.entries)
        token = listing.next_page_token
        if token is None:
            break
    assert pages == 2
    assert seen == [f"secret/app/s{index:03d}" for index in range(150)]


# ---------------------------------------------------------------------------
# Test connection
# ---------------------------------------------------------------------------


async def test_a_connection_test_reports_login_list_and_read(memory_vaults):
    memory_vaults["corp"] = {"secret/xtm/github": {"token": SECRET_VALUE}}
    report = await run_connection_test(_db_config())
    assert [(step.step, step.ok) for step in report.steps] == [
        ("configuration", True),
        ("login", True),
        ("list", True),
        ("read", True),
    ]
    assert report.ok
    assert SECRET_VALUE not in repr(report)


async def test_a_refused_login_is_reported_with_its_reason_and_stops_the_test(memory_vaults):
    config = _db_config()
    provider = MemoryVault(config)
    provider.fail_login = VaultReadError("access_denied", "HTTP 403")
    report = await run_connection_test(config, provider)
    assert [(step.step, step.ok, step.reason) for step in report.steps] == [
        ("configuration", True, None),
        ("login", False, "access_denied"),
        ("list", None, None),
        ("read", None, None),
    ]
    assert not report.ok and report.failure.detail == "HTTP 403"


@pytest.mark.parametrize(
    ("stalled", "expected"),
    [
        ("authenticate", [("login", False), ("list", None), ("read", None)]),
        ("list_secrets", [("login", True), ("list", False), ("read", None)]),
        ("read", [("login", True), ("list", True), ("read", False)]),
    ],
)
async def test_a_test_that_times_out_names_the_step_that_was_running(memory_vaults, monkeypatch, stalled, expected):
    from app.services.vault import checks

    memory_vaults["corp"] = {"secret/xtm/github": {"token": SECRET_VALUE}}
    config = _db_config()
    provider = MemoryVault(config)

    async def _never(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(3600)

    monkeypatch.setattr(provider, stalled, _never)
    monkeypatch.setattr(checks, "TEST_TIMEOUT_SECONDS", 0.05)
    report = await run_connection_test(config, provider)
    assert [(step.step, step.ok) for step in report.steps] == [("configuration", True), *expected]
    assert report.failure.reason == "unreachable"


async def test_a_configuration_it_cannot_use_fails_the_first_step(monkeypatch):
    config = database_config(name="corp", type="hashicorp", settings={"address": "ftp://vault"}, credentials={"token": "t"})
    report = await run_connection_test(config)
    assert report.steps[0].step == "configuration" and report.steps[0].ok is False
    assert "http(s) URL" in report.steps[0].detail


# ---------------------------------------------------------------------------
# The provider types: database credentials and listing
# ---------------------------------------------------------------------------


def _hashicorp(handler, **settings: str):
    from app.services.vault.hashicorp import HashiCorpVaultProvider

    config = database_config(
        name="corp", type="hashicorp", settings={"address": "https://vault.example.com", **settings}, credentials={"token": "hvs.t"}
    )
    provider = HashiCorpVaultProvider(config)
    provider._client = httpx.AsyncClient(base_url=provider.address, transport=httpx.MockTransport(handler))
    return provider


async def test_hashicorp_lists_its_kv_mounts_at_the_top_then_the_metadata_of_a_folder():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}?{request.url.query.decode()}")
        assert request.headers["X-Vault-Token"] == "hvs.t"
        if request.url.path == "/v1/sys/internal/ui/mounts":
            return httpx.Response(200, json={"data": {"secret": {
                "secret/": {"type": "kv", "options": {"version": "2"}},
                "cubbyhole/": {"type": "cubbyhole"},
                "pki/": {"type": "pki"},
            }}})
        if request.url.path == "/v1/secret/metadata/xtm/":
            return httpx.Response(200, json={"data": {"keys": ["github", "teams/"]}})
        return httpx.Response(404)

    provider = _hashicorp(handler)
    top = await provider.list_secrets("")
    assert [(entry.path, entry.folder) for entry in top.entries] == [("secret/", True)]
    folder = await provider.list_secrets("secret/xtm/")
    assert [(entry.path, entry.folder) for entry in folder.entries] == [("secret/xtm/teams/", True), ("secret/xtm/github", False)]
    assert "GET /v1/secret/metadata/xtm/?list=true" in seen
    assert (await provider.list_secrets("secret/empty")).entries == []
    assert await provider.mounts() == [{"path": "secret", "version": "2"}]


@pytest.mark.parametrize(
    ("settings", "credentials", "message"),
    [
        ({"auth_method": "token"}, {}, "token setting"),
        ({"auth_method": "approle", "role_id": "r"}, {}, "secret_id setting"),
    ],
)
def test_a_database_hashicorp_provider_needs_the_credential_its_method_reads(settings, credentials, message):
    from app.services.vault.hashicorp import HashiCorpVaultProvider

    config = database_config(
        name="corp", type="hashicorp", settings={"address": "https://vault.example.com", **settings}, credentials=credentials
    )
    with pytest.raises(VaultConfigError) as exc:
        HashiCorpVaultProvider(config)
    assert message in str(exc.value)


_OP_VAULT = "vltvltvltvltvltvltvltvltvl"
_OP_ITEM = "itmitmitmitmitmitmitmitmit"
_OP_SEED = "otpauth://totp/Entra:demo?secret=GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ&digits=6&period=30"


def _onepassword(handler):
    from app.services.vault.onepassword import OnePasswordVaultProvider

    config = database_config(
        name="op", type="onepassword", settings={"address": "https://connect.example.com"}, credentials={"token": "ops.t"}
    )
    provider = OnePasswordVaultProvider(config)
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider


def _connect_handler(seen: list[str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(f"{request.method} {request.url.path}")
        assert request.headers["Authorization"] == "Bearer ops.t"
        path = request.url.path
        if path == "/v1/vaults":
            return httpx.Response(200, json=[{"id": _OP_VAULT, "name": "Engineering"}, {"id": "x" * 26, "name": "Private"}])
        if path == f"/v1/vaults/{_OP_VAULT}":
            return httpx.Response(200, json={"id": _OP_VAULT, "name": "Engineering"})
        if path == f"/v1/vaults/{_OP_VAULT}/items":
            items = [{"id": _OP_ITEM, "title": "Entra demo"}, {"id": "y" * 26, "title": "API token"}]
            wanted = request.url.params.get("filter", "")
            if wanted.startswith('title eq "'):
                items = [item for item in items if f'title eq "{item["title"]}"' == wanted]
            return httpx.Response(200, json=items)
        if path == f"/v1/vaults/{_OP_VAULT}/items/{_OP_ITEM}":
            return httpx.Response(200, json={"id": _OP_ITEM, "title": "Entra demo", "fields": [
                {"id": "username", "label": "username", "purpose": "USERNAME", "value": "demo@example.test"},
                {"id": "password", "label": "password", "purpose": "PASSWORD", "type": "CONCEALED", "value": "pw"},
                {"id": "TOTP_k3j", "label": "one-time password", "type": "OTP", "value": _OP_SEED, "totp": "123456"},
                {"id": "notesPlain", "label": "notesPlain", "purpose": "NOTES"},
            ]})
        if path == f"/v1/vaults/{_OP_VAULT}/items/{'y' * 26}":
            return httpx.Response(200, json={"id": "y" * 26, "title": "API token", "fields": [
                {"id": "credential", "label": "credential", "type": "CONCEALED", "value": "tok"},
            ]})
        return httpx.Response(404)

    return handler


async def test_onepassword_lists_vaults_as_folders_then_their_items_by_title_with_ids_for_paths():
    provider = _onepassword(_connect_handler())
    top = await provider.list_secrets("")
    assert [(entry.name, entry.path, entry.folder) for entry in top.entries] == [
        ("Engineering/", f"{_OP_VAULT}/", True),
        ("Private/", f"{'x' * 26}/", True),
    ]
    items = await provider.list_secrets(f"{_OP_VAULT}/")
    assert [(entry.name, entry.path, entry.folder) for entry in items.entries] == [
        ("API token", f"{_OP_VAULT}/{'y' * 26}", False),
        ("Entra demo", f"{_OP_VAULT}/{_OP_ITEM}", False),
    ]
    assert (await provider.list_secrets(f"{_OP_VAULT}/{_OP_ITEM}")).entries == []


async def test_onepassword_reads_an_item_as_its_field_ids_and_an_otp_field_as_its_seed():
    from app.core import totp

    provider = _onepassword(_connect_handler())
    payload = await provider.read(f"{_OP_VAULT}/{_OP_ITEM}")
    assert payload == {"username": "demo@example.test", "password": "pw", "TOTP_k3j": _OP_SEED}
    assert provider.entry_labels(f"{_OP_VAULT}/{_OP_ITEM}")["TOTP_k3j"] == "one-time password"
    # The seed computes codes with the platform's one TOTP implementation: never the server's code of the moment.
    totp.validate_secret(payload["TOTP_k3j"])


async def test_onepassword_resolves_a_hand_typed_vault_name_and_item_title():
    provider = _onepassword(_connect_handler())
    assert (await provider.read("Engineering/Entra"))["password"] == "pw"


@pytest.mark.parametrize(
    ("status", "reason"),
    [(401, "access_denied"), (403, "access_denied"), (404, "not_found"), (502, "unreachable")],
)
async def test_onepassword_maps_a_refused_read_to_its_reason(status, reason):
    provider = _onepassword(lambda request: httpx.Response(status))
    with pytest.raises(VaultReadError) as exc:
        await provider.read(f"{_OP_VAULT}/{_OP_ITEM}")
    assert exc.value.reason == reason


async def test_onepassword_answering_something_else_than_connect_is_an_invalid_response():
    provider = _onepassword(lambda request: httpx.Response(200, text="<html>sign in</html>"))
    with pytest.raises(VaultReadError) as exc:
        await provider.authenticate()
    assert exc.value.reason == "invalid_response"


def test_onepassword_paths_name_a_vault_and_an_item_and_a_database_provider_needs_its_token():
    from app.services.vault.onepassword import OnePasswordVaultProvider

    provider = _onepassword(_connect_handler())
    assert provider.path_problem(f"{_OP_VAULT}/{_OP_ITEM}") is None
    assert "a vault then an item" in (provider.path_problem(_OP_VAULT) or "")
    assert "a vault then an item" in (provider.path_problem(f"{_OP_VAULT}/{_OP_ITEM}/extra") or "")
    with pytest.raises(VaultConfigError) as exc:
        OnePasswordVaultProvider(
            database_config(name="op", type="onepassword", settings={"address": "https://connect.example.com"}, credentials={})
        )
    assert "token setting" in str(exc.value)


def test_onepassword_is_declared_in_the_environment_like_every_type():
    from app.services.vault.config import read_declarations

    declared = read_declarations(
        {"SECRET_VAULT_OP_TYPE": "onepassword", "SECRET_VAULT_OP_ADDRESS": "https://connect.example.com", "SECRET_VAULT_OP_TOKEN": "t"}
    )
    assert declared.errors == {}
    assert dict(declared.providers["op"].settings) == {"ADDRESS": "https://connect.example.com", "TOKEN": "t"}


async def test_onepassword_runs_the_generic_connection_test_down_to_an_item():
    seen: list[str] = []
    provider = _onepassword(_connect_handler(seen))
    report = await run_connection_test(provider.config, provider)
    assert report.ok, report.steps
    assert [step.step for step in report.steps] == ["configuration", "login", "list", "read"]
    assert f"GET /v1/vaults/{_OP_VAULT}/items/{'y' * 26}" in seen


async def test_the_picker_shows_a_1password_field_by_its_label_and_stores_its_id(memory_vaults, monkeypatch):
    from app.api.v1 import secret_vaults

    provider = _onepassword(_connect_handler())
    resolver = SecretResolver({"op": provider}, {}, cache_seconds=60)
    set_resolver(resolver)
    monkeypatch.setattr(secret_vaults, "enforce_rate_limit", _no_rate_limit)
    keys = await secret_vaults.list_picker_keys(
        "op", SimpleNamespace(), path=f"{_OP_VAULT}/{_OP_ITEM}", user=SimpleNamespace(id=uuid.uuid4(), is_admin=True), db=None
    )
    assert keys.keys == ["TOTP_k3j", "password", "username"]
    assert keys.labels == {"TOTP_k3j": "one-time password"}
    assert "pw" not in keys.model_dump_json() and _OP_SEED not in keys.model_dump_json()


async def _no_rate_limit(*_args, **_kwargs) -> None:
    return None


def test_a_pasted_ca_certificate_that_is_not_pem_is_a_misconfiguration():
    from app.services.vault.base import tls_verify

    config = database_config(
        name="corp", type="hashicorp", settings={"address": "https://v", "ca_cert": "not a certificate"}, credentials={"token": "t"}
    )
    with pytest.raises(VaultReadError) as exc:
        tls_verify(config)
    assert exc.value.reason == "misconfigured"


def test_flat_names_fold_into_folders_by_slash():
    names = ["prod/db/main", "prod/db/replica", "prod/github", "staging/github", "top"]
    assert [(e.path, e.folder) for e in child_entries("", names)] == [
        ("prod/", True),
        ("staging/", True),
        ("top", False),
    ]
    assert [(e.path, e.folder) for e in child_entries("prod/", names)] == [("prod/db/", True), ("prod/github", False)]


@pytest.mark.parametrize(
    ("settings", "credentials", "message"),
    [
        ({"auth_method": "assume_role", "role_arn": "not-an-arn"}, {}, "ARN of the role"),
        ({"auth_method": "access_key", "access_key_id": "AKIA"}, {}, "are required with the access_key"),
        ({"auth_method": "oidc"}, {}, "is one of default_chain"),
    ],
)
def test_an_aws_provider_needs_what_its_auth_method_reads(settings, credentials, message):
    from app.services.vault.aws import AWSSecretsManagerProvider

    with pytest.raises(VaultConfigError) as exc:
        AWSSecretsManagerProvider(database_config(name="aws", type="aws", settings=settings, credentials=credentials))
    assert message in str(exc.value)


def test_a_google_service_account_key_is_checked_when_saved_and_names_the_project():
    from app.services.vault.gcp import GoogleSecretManagerProvider

    def _gcp(key: str, **settings: str):
        return GoogleSecretManagerProvider(
            database_config(name="gcp", type="gcp", settings={"auth_method": "service_account_key", **settings}, credentials={"service_account_key": key})
        )

    with pytest.raises(VaultConfigError, match="not a JSON key file"):
        _gcp("{nope")
    with pytest.raises(VaultConfigError, match="not the JSON key of a service account"):
        _gcp(json.dumps({"type": "authorized_user"}))
    key = json.dumps({"type": "service_account", "private_key": "pk", "client_email": "x@p.iam", "project_id": "acme-prod"})
    assert _gcp(key).project == "acme-prod"
    with pytest.raises(VaultConfigError, match="project id or number"):
        _gcp(key, project="acme/../other")
    # The key's own project is a segment of the API URL too.
    odd_key = json.dumps({"type": "service_account", "private_key": "pk", "client_email": "x@p.iam", "project_id": "acme/../other"})
    with pytest.raises(VaultConfigError, match="project_id of"):
        _gcp(odd_key)


def test_azure_follows_only_a_next_page_of_its_own_listing():
    from app.services.vault.azure import _same_listing

    vault_url = "https://acme.vault.azure.net"
    assert _same_listing("https://acme.vault.azure.net:443/secrets?api-version=7.4&$skiptoken=abc", vault_url)
    assert _same_listing("https://acme.vault.azure.net/secrets?$skiptoken=abc", vault_url)
    assert not _same_listing("https://evil.example.com/secrets?x", vault_url)
    assert not _same_listing("https://acme.vault.azure.net/keys?x", vault_url)
    assert not _same_listing("http://acme.vault.azure.net/secrets?x", vault_url)


async def test_azure_signs_in_with_a_client_secret_and_lists_secret_names():
    from app.services.vault.azure import AzureKeyVaultProvider

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            form = dict(item.split("=", 1) for item in request.content.decode().split("&"))
            assert request.url.path == "/tenant-1/oauth2/v2.0/token"
            assert form["grant_type"] == "client_credentials" and form["client_secret"] == "app-secret"
            return httpx.Response(200, json={"access_token": "aad-token", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer aad-token"
        return httpx.Response(200, json={"value": [
            {"id": "https://acme.vault.azure.net/secrets/github-token", "attributes": {"enabled": True}},
            {"id": "https://acme.vault.azure.net/secrets/old", "attributes": {"enabled": False}},
        ]})

    provider = AzureKeyVaultProvider(
        database_config(
            name="az",
            type="azure",
            settings={"vault_url": "https://acme.vault.azure.net", "auth_method": "client_secret", "tenant_id": "tenant-1", "client_id": "app"},
            credentials={"client_secret": "app-secret"},
        )
    )
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await provider.authenticate()
    listing = await provider.list_secrets("")
    assert [entry.path for entry in listing.entries] == ["github-token"]


@pytest.mark.parametrize(
    "vault_url",
    [
        "https://collector.example.com",
        "https://vault.azure.net",
        "https://acme.vault.azure.net.example.com",
        "https://acme-vault.azure.net",
    ],
)
@pytest.mark.parametrize("auth_method", ["managed_identity", "client_secret"])
def test_an_azure_provider_connected_in_the_ui_reads_only_from_an_azure_key_vault(vault_url, auth_method):
    """The platform identity's Key Vault token goes to VAULT_URL: a UI session never points it at another host."""
    from app.services.vault.azure import AzureKeyVaultProvider

    config = database_config(
        name="az",
        type="azure",
        settings={"vault_url": vault_url, "auth_method": auth_method, "tenant_id": "t", "client_id": "c"},
        credentials={"client_secret": "s"},
    )
    with pytest.raises(VaultConfigError, match="URL of an Azure key vault"):
        AzureKeyVaultProvider(config)


@pytest.mark.parametrize(
    ("vault_url", "resource"),
    [
        ("https://acme.vault.azure.net", "https://vault.azure.net"),
        ("https://acme.vault.azure.cn", "https://vault.azure.cn"),
        ("https://acme.vault.usgovcloudapi.net", "https://vault.usgovcloudapi.net"),
    ],
)
async def test_an_azure_token_is_requested_for_the_key_vault_of_the_vaults_cloud(monkeypatch, vault_url, resource):
    from app.services.vault.azure import AzureKeyVaultProvider

    for variable in ("AZURE_FEDERATED_TOKEN_FILE", "IDENTITY_ENDPOINT", "IDENTITY_HEADER"):
        monkeypatch.delenv(variable, raising=False)
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.params["resource"])
        return httpx.Response(200, json={"access_token": "mi-token", "expires_in": 3600})

    provider = AzureKeyVaultProvider(database_config(name="az", type="azure", settings={"vault_url": vault_url}, credentials={}))
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await provider.authenticate()
    assert asked == [resource]


def test_an_azure_vault_on_another_host_stays_an_environment_declaration():
    from app.services.vault.azure import KEY_VAULT_RESOURCE, AzureKeyVaultProvider

    provider = AzureKeyVaultProvider(
        VaultProviderConfig(name="stack", type="azure", settings={"VAULT_URL": "https://kv.vault.local.azurestack.example"})
    )
    assert provider.resource == KEY_VAULT_RESOURCE


def test_a_hashicorp_provider_connected_in_the_ui_never_sends_the_service_account_token():
    """A Kubernetes login posts this platform's service account token to ADDRESS: only the environment declares one."""
    from app.services.vault.hashicorp import HashiCorpVaultProvider

    config = database_config(
        name="corp",
        type="hashicorp",
        settings={"address": "https://collector.example.com", "auth_method": "kubernetes", "role": "xtm-one"},
        credentials={},
    )
    with pytest.raises(VaultConfigError, match="declared in the environment"):
        HashiCorpVaultProvider(config)
    env = VaultProviderConfig(
        name="corp", type="hashicorp", settings={"ADDRESS": "https://vault.internal", "AUTH_METHOD": "kubernetes", "ROLE": "xtm-one"}
    )
    assert HashiCorpVaultProvider(env).auth_method == "kubernetes"


def test_a_provider_connected_in_the_ui_names_no_file_the_platform_would_read():
    """JWT_FILE is a path on the platform's disk: a database provider never keeps one, so no UI session picks a file to send."""
    from app.services.vault.config import DATABASE_SETTINGS

    assert not any(setting.endswith("_FILE") for settings in DATABASE_SETTINGS.values() for setting in settings)
    config = database_config(
        name="corp",
        type="hashicorp",
        settings={"address": "https://vault.internal", "auth_method": "approle", "role_id": "r", "jwt_file": "/etc/passwd"},
        credentials={"secret_id": "s"},
    )
    assert "JWT_FILE" not in config.settings


# ---------------------------------------------------------------------------
# An MCP server's OAuth client secret read from a vault
# ---------------------------------------------------------------------------


async def test_an_mcp_refresh_whose_client_secret_vault_fails_keeps_the_server_and_asks_nothing(memory_vaults, monkeypatch, fake_redis):
    from unittest.mock import AsyncMock

    from app.services import token_refresh

    _apply(_db_config(enabled=False))
    srv = SimpleNamespace(
        id=uuid.uuid4(),
        name="Company MCP",
        oauth_status="connected",
        client_id_enc=encrypt_value("client-id"),
        client_secret_enc=encrypt_value("vault://corp/secret/mcp/oauth#client_secret"),
        refresh_token_enc=encrypt_value("refresh-token"),
        access_token_enc=encrypt_value("access-token"),
        token_url="https://mcp.example.com/oauth/token",
        scopes=None,
        oauth_resource=None,
        token_expires_at=None,
    )
    srv.__tablename__ = "mcp_servers"
    http = AsyncMock()
    cooldown = AsyncMock()
    monkeypatch.setattr(token_refresh, "start_refresh_cooldown", cooldown)

    assert await token_refresh._do_refresh_mcp(srv, http) is False
    assert srv.oauth_status == "connected"
    http.post.assert_not_called()
    cooldown.assert_not_called()


# ---------------------------------------------------------------------------
# The Security Audit covers the providers connected in the UI
# ---------------------------------------------------------------------------


async def test_the_audit_reports_static_keys_failing_unused_and_open_database_providers(memory_vaults, monkeypatch, fake_redis):
    from app.services.vault import audit

    rows = [
        SimpleNamespace(name="open", type="hashicorp", settings={}, enabled=True, last_check_ok=True, last_check_reason=None),
        SimpleNamespace(name="keyed", type="aws", settings={"auth_method": "access_key"}, enabled=True, last_check_ok=False, last_check_reason="access_denied"),
    ]

    async def _rows(db):
        return rows

    monkeypatch.setattr(audit, "_database_rows", _rows)
    _apply(
        _db_config("open", grants=[{"principal": "everyone"}]),
        _db_config("keyed"),
    )

    class _EmptyDB:
        async def execute(self, statement):
            return SimpleNamespace(all=lambda: [])

    facts = await audit.gather_vault_facts(_EmptyDB())
    assert facts.open_writers == ["open"]
    assert facts.static_keys == ["keyed: access_key"]
    assert facts.failing == [f"keyed: {vault.REASON_DESCRIPTIONS['access_denied']}"]
    assert sorted(facts.unused) == ["keyed", "open"]


@pytest.fixture
def fake_redis(monkeypatch):
    from app.services.vault import failures
    from tests.test_vault_references import FakeRedis

    redis = FakeRedis()

    async def _client():
        return redis

    monkeypatch.setattr(failures, "_redis", _client)
    return redis


def test_listed_secret_is_a_name_and_a_path_only():
    assert set(ListedSecret.__dataclass_fields__) == {"name", "path", "folder"}
