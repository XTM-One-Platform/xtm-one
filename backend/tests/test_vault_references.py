"""Secrets read from enterprise vaults: references, providers, resolution, rotation, masking.

No vault is needed: providers talk HTTP through ``httpx.MockTransport`` (or a
stubbed SDK client), and the failure record goes to an in-memory stand-in for
Redis.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from app.core.secret_refs import (
    SecretReference,
    SecretReferenceSyntaxError,
    holds_secret_reference,
    parse_secret_reference,
    secret_reference_problem,
    secret_references_in,
)
from app.core.security import (
    SecretReferenceError,
    decrypt_json,
    decrypt_stored_value,
    decrypt_value,
    encrypt_value,
)
from app.services import vault
from app.services.vault import failures
from app.services.vault.base import VaultProvider
from app.services.vault.config import ADMIN_PRINCIPAL, VaultPrincipal, VaultProviderConfig, read_declarations
from app.services.vault.errors import VAULT_UNAVAILABLE_DETAIL, SecretUnavailableError, VaultReadError
from app.services.vault.resolver import FAILURE_RETRY_SECONDS, SecretResolver, select_value, set_resolver

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: A user who is not an administrator, in no group.
USER_PRINCIPAL = VaultPrincipal(is_admin=False)


class FakeVault(VaultProvider):
    """A provider holding secrets in a dict, counting its reads."""

    kind = "fake"

    def __init__(self, name: str = "corp", secrets: dict[str, Any] | None = None, **settings: str) -> None:
        super().__init__(VaultProviderConfig(name=name, type="hashicorp", settings=settings))
        self.secrets = dict(secrets or {})
        self.reads = 0
        self.gate: asyncio.Event | None = None

    async def read(self, path: str) -> Any:
        self.reads += 1
        if self.gate is not None:
            await self.gate.wait()
        if path not in self.secrets:
            raise VaultReadError("not_found", "HTTP 404")
        value = self.secrets[path]
        if isinstance(value, VaultReadError):
            raise value
        return value


class FakeRedis:
    """The few Redis commands the failure record uses."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}

    def pipeline(self, transaction: bool = False) -> FakePipeline:
        return FakePipeline(self)

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    async def hdel(self, key, field):
        self.hashes.get(key, {}).pop(field, None)
        if key in self.hashes and not self.hashes[key]:
            del self.hashes[key]

    async def exists(self, key):
        return int(key in self.hashes)

    async def zrem(self, key, member):
        self.zsets.get(key, {}).pop(member, None)

    async def eval(self, script, numkeys, *args):
        """The one script the failure record runs, as Redis runs it: atomically."""
        keys, argv = args[:numkeys], args[numkeys:]
        assert script == failures._CLEAR_FIELD_SCRIPT
        await self.hdel(keys[0], argv[0])
        if not await self.exists(keys[0]):
            await self.zrem(keys[1], argv[1])
        return 1

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def zrevrangebyscore(self, key, high, low, start=0, num=None):
        members = sorted(self.zsets.get(key, {}).items(), key=lambda item: -item[1])
        return [member for member, score in members if score >= low][start : start + (num or len(members))]


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.calls: list[tuple[str, tuple]] = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.calls.append((name, args))
            return self

        return _record

    async def execute(self):
        results = []
        for name, args in self.calls:
            if name == "hset":
                await self.redis.hset(*args)
            elif name == "zadd":
                self.redis.zsets.setdefault(args[0], {}).update(args[1])
            elif name == "hgetall":
                results.append(await self.redis.hgetall(*args))
        return results


@pytest.fixture
def fake_redis(monkeypatch):
    redis = FakeRedis()

    async def _client():
        return redis

    monkeypatch.setattr(failures, "_redis", _client)
    return redis


def _use(*providers: VaultProvider, cache_seconds: float = 300, errors: dict[str, str] | None = None) -> SecretResolver:
    resolver = SecretResolver({p.name: p for p in providers}, errors or {}, cache_seconds)
    set_resolver(resolver)
    return resolver


#: The reference most tests store, for the failures recorded against it.
_GITHUB_REF = parse_secret_reference("vault://corp/secret/github#token")


def _integration(**columns: str | None) -> SimpleNamespace:
    row = SimpleNamespace(id="11111111-1111-1111-1111-111111111111", **columns)
    row.__tablename__ = "integrations"
    return row


# ---------------------------------------------------------------------------
# Reference syntax
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("vault://corp/secret/github#token", SecretReference("corp", "secret/github", "token")),
        ("  vault://aws/prod/github-token  ", SecretReference("aws", "prod/github-token", None)),
        (
            "vault://aws/arn:aws:secretsmanager:eu-west-1:123:secret:gh-AbC#api_key",
            SecretReference("aws", "arn:aws:secretsmanager:eu-west-1:123:secret:gh-AbC", "api_key"),
        ),
        ("VAULT://corp/kv/a.b_c-d#x", SecretReference("corp", "kv/a.b_c-d", "x")),
    ],
)
def test_a_reference_parses_into_provider_path_and_key(text, expected):
    assert parse_secret_reference(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "vault://",
        "vault://Corp/x",
        "vault://corp",
        "vault://corp/",
        "vault://corp/a//b",
        "vault://corp/../etc/passwd",
        "vault://corp/a b",
        "vault://corp/a#",
        "vault://corp/a#bad key",
        "vault://" + "a" * 40 + "/x",
        # A newline inside the reference (outer whitespace is stripped): a "$" anchor would admit it.
        "vault://corp/secret/github\n#token",
        "vault://corp\n/secret/github",
    ],
)
def test_a_malformed_reference_is_refused_with_a_reason(text):
    with pytest.raises(SecretReferenceSyntaxError) as exc:
        parse_secret_reference(text)
    assert str(exc.value)


def test_a_plain_secret_is_never_taken_for_a_reference():
    for value in ("sk-live-abc", "vaultkeeper", "https://vault.example.com", "", None, 42):
        assert not holds_secret_reference(value)
        assert secret_reference_problem(value, {"corp"}) is None


def test_a_compound_value_holds_a_reference_in_a_segment():
    text = "vault://corp/llm#openai||https://gateway.example.com/v1"
    assert holds_secret_reference(text)
    assert secret_references_in(text) == [SecretReference("corp", "llm", "openai")]
    assert holds_secret_reference("none||vault://corp/x#y")


def test_a_reference_to_an_undeclared_provider_is_refused_on_write():
    assert "No vault provider named 'nope'" in secret_reference_problem("vault://nope/x", {"corp"})
    assert "no vault provider is configured" in secret_reference_problem("vault://corp/x", set())
    assert secret_reference_problem("vault://corp/x#k", {"corp"}) is None
    assert secret_reference_problem("vault://corp/x#k||https://h", {"corp"}) is None


# ---------------------------------------------------------------------------
# Provider declarations
# ---------------------------------------------------------------------------


def test_providers_are_read_from_the_environment_by_name():
    declarations = read_declarations(
        {
            "SECRET_VAULT_CORP_TYPE": "HashiCorp",
            "SECRET_VAULT_CORP_ADDRESS": "https://vault.example.com:8200",
            "SECRET_VAULT_CORP_TOKEN": "s.secret",
            "SECRET_VAULT_CORP_UNKNOWN": "ignored",
            "SECRET_VAULT_AWS_PROD_TYPE": "aws",
            "SECRET_VAULT_AWS_PROD_REGION": "eu-west-1",
            "SECRET_VAULT_BAD_TYPE": "keepass",
            "SECRET_VAULT_CACHE_SECONDS": "60",
        }
    )
    assert set(declarations.providers) == {"corp", "aws_prod"}
    corp = declarations.providers["corp"]
    assert corp.type == "hashicorp"
    assert dict(corp.settings) == {"ADDRESS": "https://vault.example.com:8200", "TOKEN": "s.secret"}
    assert corp.variable("TOKEN") == "SECRET_VAULT_CORP_TOKEN"
    assert declarations.providers["aws_prod"].get("REGION") == "eu-west-1"
    # Named by its variable, never by its value (logged, shown in the Security Audit).
    assert declarations.errors["bad"].startswith("SECRET_VAULT_BAD_TYPE is not a known type")
    assert "keepass" not in declarations.errors["bad"]


def test_a_refused_provider_settings_are_reported_and_never_built():
    declarations = read_declarations({"SECRET_VAULT_CORP_TYPE": "hashicorp", "SECRET_VAULT_CORP_ADDRESS": "vault:8200"})
    resolver = SecretResolver.from_declarations(declarations, 300)
    assert resolver.providers == {}
    assert "SECRET_VAULT_CORP_ADDRESS" in resolver.errors["corp"]


def test_a_provider_description_never_shows_a_credential():
    declarations = read_declarations(
        {
            "SECRET_VAULT_CORP_TYPE": "hashicorp",
            "SECRET_VAULT_CORP_ADDRESS": "https://vault:8200",
            "SECRET_VAULT_CORP_AUTH_METHOD": "approle",
            "SECRET_VAULT_CORP_ROLE_ID": "role",
            "SECRET_VAULT_CORP_SECRET_ID": "super-secret-id",
        }
    )
    resolver = SecretResolver.from_declarations(declarations, 300)
    assert "super-secret-id" not in json.dumps(resolver.providers["corp"].describe())


# ---------------------------------------------------------------------------
# Provider policy: who writes references, which paths they read
# ---------------------------------------------------------------------------


def test_a_provider_is_for_administrators_and_every_path_by_default():
    policy = _use(FakeVault()).policy("corp")
    assert policy.usable_by(ADMIN_PRINCIPAL)
    assert not policy.usable_by(USER_PRINCIPAL)
    assert policy.allows_path("anything/at/all")


def test_paths_are_prefixes_matched_segment_by_segment():
    policy = _use(FakeVault(PATHS=" xtm-one/ , shared/ci ,")).policy("corp")
    assert policy.allows_path("xtm-one")
    assert policy.allows_path("xtm-one/github")
    assert policy.allows_path("shared/ci/token")
    assert not policy.allows_path("xtm-one-staging/github")
    assert not policy.allows_path("shared/cid")
    assert not policy.allows_path("shared")
    assert not policy.allows_path("XTM-ONE/github")


@pytest.mark.parametrize(
    ("settings", "variable"),
    [
        ({"WRITERS": "everyone"}, "SECRET_VAULT_CORP_WRITERS"),
        ({"PATHS": "ok,a//b"}, "SECRET_VAULT_CORP_PATHS"),
        ({"PATHS": "../secret"}, "SECRET_VAULT_CORP_PATHS"),
    ],
)
def test_a_policy_it_cannot_read_refuses_the_provider(settings, variable):
    resolver = _use(FakeVault(**settings))
    assert "corp" not in resolver.providers
    assert variable in resolver.errors["corp"]
    assert not resolver.policy("corp").usable_by(USER_PRINCIPAL)


def test_the_provider_description_shows_its_policy():
    description = _use(FakeVault(WRITERS="ALL", PATHS="xtm-one")).providers["corp"].describe()
    assert description["writers"] == "all"
    assert description["paths"] == "xtm-one"


async def test_a_path_outside_the_allowed_prefixes_is_never_read():
    fake = FakeVault(secrets={"other/github": {"token": "t"}, "xtm-one/github": {"token": "ok"}}, PATHS="xtm-one")
    resolver = _use(fake)
    with pytest.raises(VaultReadError) as exc:
        await resolver.read(parse_secret_reference("vault://corp/other/github#token"))
    assert exc.value.reason == "path_not_allowed"
    assert fake.reads == 0
    assert await resolver.read(parse_secret_reference("vault://corp/xtm-one/github#token")) == "ok"


def test_a_reference_outside_the_allowed_paths_is_refused_on_write():
    _use(FakeVault(PATHS="xtm-one"))
    assert vault.reference_problem("vault://corp/xtm-one/github#token") is None
    assert "outside the paths" in vault.reference_problem("vault://corp/prod/github#token")
    assert "outside the paths" in vault.reference_problem({"Authorization": "vault://corp/prod/x"})
    assert "outside the paths" in vault.reference_problem("vault://corp/prod/x||https://gateway")


def test_only_the_roles_a_provider_admits_may_write_a_reference_to_it():
    _use(FakeVault("corp"), FakeVault("team", WRITERS="all"))
    fields = {"api_key": "vault://corp/x#k", "client_secret": "plain"}
    assert vault.secret_fields_writer_problem(fields, principal=ADMIN_PRINCIPAL) is None
    assert vault.secret_fields_writer_problem(fields, principal=USER_PRINCIPAL) == (
        "api_key: only administrators may store a reference to vault provider 'corp'"
    )
    assert vault.secret_fields_writer_problem({"api_key": "vault://team/x#k"}, principal=USER_PRINCIPAL) is None
    assert "Authorization" in vault.secret_fields_writer_problem(
        {"headers": {"Authorization": "vault://corp/x"}}, principal=USER_PRINCIPAL
    )
    assert vault.secret_fields_writer_problem({"api_key": "sk-plain", "headers": None}, principal=USER_PRINCIPAL) is None


# ---------------------------------------------------------------------------
# Value selection, cache, rotation
# ---------------------------------------------------------------------------


def test_the_value_is_the_named_entry_or_the_only_one():
    assert select_value("plain", None) == "plain"
    assert select_value('{"token": "t", "user": "u"}', "token") == "t"
    assert select_value({"token": "t"}, None) == "t"
    assert select_value({"port": 5432}, "port") == "5432"
    with pytest.raises(VaultReadError) as many:
        select_value({"a": "1", "b": "2"}, None)
    assert many.value.reason == "key_required"
    with pytest.raises(VaultReadError) as missing:
        select_value({"a": "1"}, "b")
    assert missing.value.reason == "not_found"
    with pytest.raises(VaultReadError):
        select_value("not json", "key")


@pytest.mark.parametrize(("payload", "key"), [("", None), ({"token": ""}, "token"), ({"token": ""}, None), ('{"t": ""}', "t")])
def test_an_empty_value_is_never_a_credential(payload, key):
    """A caller would fall back to another credential it holds: an empty secret fails like an unreadable one."""
    with pytest.raises(VaultReadError) as empty:
        select_value(payload, key)
    assert empty.value.reason == "invalid_response"


def test_a_provider_declaration_never_shows_its_credentials():
    config = VaultProviderConfig(name="corp", type="hashicorp", settings={"TOKEN": "s.inline-root-token", "SECRET_ID": "sid-1"})
    assert "s.inline-root-token" not in repr(config) and "sid-1" not in repr(config)


async def test_a_cached_failure_is_raised_as_a_fresh_error():
    resolver = _use(FakeVault(secrets={}))
    reference = parse_secret_reference("vault://corp/secret/missing#k")
    raised = []
    for _ in range(2):
        with pytest.raises(VaultReadError) as failed:
            await resolver.read(reference)
        raised.append(failed.value)
    assert raised[0] is not raised[1] and raised[1].reason == "not_found"


def test_secret_fields_outside_the_reference_columns_refuse_a_reference():
    """decrypt_value refuses a stored reference: a field that cannot read one must not store one (422)."""
    from app.api.v1.schemas.settings import LangfuseConfigRequest, OAuthDefaultRequest, SSOConfigRequest

    reference = "vault://corp/x#k"
    for build in (
        lambda: SSOConfigRequest(oidc_client_secret=reference),
        lambda: SSOConfigRequest(oidc_client_id=reference),
        lambda: OAuthDefaultRequest(provider="github", client_id="id", client_secret=reference),
        lambda: OAuthDefaultRequest(provider="azure", client_id="id", client_secret="s", extra_config={"tenant_id": reference}),
        lambda: LangfuseConfigRequest(secret_key=reference),
    ):
        with pytest.raises(ValidationError):
            build()
    assert SSOConfigRequest(oidc_client_secret="plain").oidc_client_secret == "plain"


async def test_a_reference_in_the_oidc_secret_costs_the_oidc_sign_in_only():
    """Every login reads the SSO configuration first: a stored reference there must not take the login page down."""
    from app.core.sso_helpers import get_sso_config

    def _row(secret: str) -> SimpleNamespace:
        return SimpleNamespace(
            oidc_enabled=True, oidc_display_name="SSO", oidc_client_id_enc=encrypt_value("client"),
            oidc_client_secret_enc=encrypt_value(secret), oidc_discovery_url="", oidc_authorization_url="",
            oidc_token_url="", oidc_userinfo_url="", oidc_scopes="openid", saml_enabled=False, saml_display_name="",
            saml_idp_metadata_url="", saml_sp_entity_id="", saml_requested_authn_context=False,
            saml_groups_mapping_enabled=False, saml_groups_auto_create=False, oidc_auto_admin_new_users=False,
            saml_auto_admin_new_users=False, oidc_auto_create_users=True, saml_auto_create_users=True,
            local_auth_enabled=True, registration_enabled=False, allowed_domains="",
        )

    class _Db:
        def __init__(self, row):
            self.row = row

        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: self.row)

    referenced = await get_sso_config(_Db(_row("vault://corp/sso#secret")))
    assert (referenced.oidc_enabled, referenced.oidc_client_secret, referenced.local_auth_enabled) == (False, "", True)
    plain = await get_sso_config(_Db(_row("s3cret")))
    assert (plain.oidc_enabled, plain.oidc_client_secret) == (True, "s3cret")


async def test_a_vault_that_does_not_answer_holds_the_login_configuration_for_the_bound_only(monkeypatch, fake_redis):
    """Every login reads the SSO configuration; a hung vault turns OIDC off after the bound, and the read goes on for the next login."""
    from app.core import sso_helpers

    fake = FakeVault(secrets={"sso": {"secret": "from-vault"}})
    fake.gate = asyncio.Event()
    _use(fake)
    monkeypatch.setattr(sso_helpers, "OIDC_SECRET_READ_SECONDS", 0.05)
    row = SimpleNamespace(
        oidc_enabled=True, oidc_display_name="SSO", oidc_client_id_enc=encrypt_value("client"),
        oidc_client_secret_enc=encrypt_value("vault://corp/sso#secret"), oidc_discovery_url="", oidc_authorization_url="",
        oidc_token_url="", oidc_userinfo_url="", oidc_scopes="openid", saml_enabled=False, saml_display_name="",
        saml_idp_metadata_url="", saml_sp_entity_id="", saml_requested_authn_context=False,
        saml_groups_mapping_enabled=False, saml_groups_auto_create=False, oidc_auto_admin_new_users=False,
        saml_auto_admin_new_users=False, oidc_auto_create_users=True, saml_auto_create_users=True,
        local_auth_enabled=True, registration_enabled=False, allowed_domains="",
    )

    class _Db:
        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: row)

    started = asyncio.get_running_loop().time()
    waiting = await sso_helpers.get_sso_config(_Db())
    elapsed = asyncio.get_running_loop().time() - started
    assert (waiting.oidc_enabled, waiting.oidc_client_secret, waiting.local_auth_enabled) == (False, "", True)
    assert elapsed < 1.0

    # The read was not cancelled with the login's wait: once the vault answers, the next login has the secret.
    fake.gate.set()
    await asyncio.sleep(0)
    answered = await sso_helpers.get_sso_config(_Db())
    assert (answered.oidc_enabled, answered.oidc_client_secret) == (True, "from-vault")
    assert fake.reads == 1


async def test_a_token_endpoint_refusing_the_client_secret_drops_it_from_the_cache(fake_redis):
    from app.a2a.client import credentials

    fake = FakeVault(secrets={"secret/a2a": {"secret": "s1"}})
    _use(fake)
    integ = SimpleNamespace(id=uuid.uuid4(), client_secret_enc=encrypt_value("vault://corp/secret/a2a#secret"))
    assert await credentials._client_secret(integ) == "s1"
    assert await credentials._client_secret(integ) == "s1"
    assert fake.reads == 1

    fake.secrets["secret/a2a"] = {"secret": "s2"}
    await credentials._forget_refused_client_secret(integ, 401)

    assert await credentials._client_secret(integ) == "s2"
    assert fake.reads == 2


async def test_a_refresh_refused_for_a_rotated_client_secret_gets_one_more_try(fake_redis):
    """``invalid_client`` with the old value cached: read again, a changed secret is tried once before the verdict."""
    from app.services.token_refresh import rotated_client_secret

    fake = FakeVault(secrets={"secret/oauth": {"secret": "old"}})
    _use(fake)
    integ = SimpleNamespace(id=uuid.uuid4(), client_secret_enc=encrypt_value("vault://corp/secret/oauth#secret"))
    request = httpx.Request("POST", "https://login.example.com/token")
    refused = httpx.Response(401, json={"error": "invalid_client"}, request=request)
    assert await vault.reveal_field(integ, "client_secret_enc") == "old"

    # Not rotated: the same value, so the refusal is final.
    assert await rotated_client_secret(integ, "old", refused) is None
    fake.secrets["secret/oauth"] = {"secret": "new"}
    assert await rotated_client_secret(integ, "old", refused) == "new"
    # GitHub and Slack refuse the client inside a 200: the same retry.
    inside_a_success = httpx.Response(200, json={"error": "incorrect_client_credentials"}, request=request)
    assert await rotated_client_secret(integ, "old", inside_a_success) == "new"
    # A dead refresh grant stays terminal, on any status; a success or a stored value never retries.
    for dead in (
        httpx.Response(400, json={"error": "invalid_grant"}, request=request),
        httpx.Response(200, json={"error": "bad_refresh_token"}, request=request),
    ):
        assert await rotated_client_secret(integ, "old", dead) is None
    granted = httpx.Response(200, json={"access_token": "at", "expires_in": 60}, request=request)
    assert await rotated_client_secret(integ, "old", granted) is None
    stored = SimpleNamespace(id=uuid.uuid4(), client_secret_enc=encrypt_value("plain-secret"))
    assert await rotated_client_secret(stored, "plain-secret", refused) is None
    # A vault that cannot be read again is no verdict: the caller backs off.
    fake.secrets["secret/oauth"] = VaultReadError("unreachable", "ConnectTimeout")
    with pytest.raises(SecretUnavailableError):
        await rotated_client_secret(integ, "old", refused)


async def test_the_scheduled_refresh_retries_once_with_a_rotated_client_secret(fake_redis, monkeypatch):
    from app.services import token_refresh

    fake = FakeVault(secrets={"secret/oauth": {"secret": "new"}})
    _use(fake)
    integration = SimpleNamespace(
        id=uuid.uuid4(), name="Acme", provider="acme", status="connected", scopes=None, extra_config_enc=None,
        client_id_enc=encrypt_value("cid"), client_secret_enc=encrypt_value("vault://corp/secret/oauth#secret"),
        refresh_token_enc=encrypt_value("rt"), access_token_enc=None, token_expires_at=None,
        token_url="https://login.example.com/token",
    )
    sent: list[str] = []

    async def _post(url, timeout=None, **kwargs):
        secret = json.dumps(kwargs, default=str)
        sent.append(secret)
        request = httpx.Request("POST", url)
        if '"client_secret": "new"' in secret:
            return httpx.Response(200, json={"access_token": "at-2", "expires_in": 3600}, request=request)
        return httpx.Response(401, json={"error": "invalid_client"}, request=request)

    async def _allowed(*_args, **_kwargs):
        return True

    monkeypatch.setattr(token_refresh, "_token_endpoint_allowed", _allowed)
    # The cached value is the old one, rotated in the vault since.
    monkeypatch.setattr(vault, "reveal_field", _first_old_then(vault.reveal_field))

    assert await token_refresh._do_refresh_integration(integration, SimpleNamespace(post=_post)) is True
    assert len(sent) == 2 and integration.status == "connected"


def _referenced_oauth_integration() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(), name="Acme", provider="acme", status="connected", scopes=None, extra_config_enc=None,
        client_id_enc=encrypt_value("cid"), client_secret_enc=encrypt_value("vault://corp/secret/oauth#secret"),
        refresh_token_enc=encrypt_value("rt"), access_token_enc=None, token_expires_at=None,
        token_url="https://login.example.com/token",
    )


async def _refused_client(url, timeout=None, **_kwargs):
    return httpx.Response(401, json={"error": "invalid_client"}, request=httpx.Request("POST", url))


async def test_a_refused_client_whose_vault_is_down_keeps_the_scheduled_refresh_going(fake_redis, monkeypatch):
    """Not marked for re-authorization: the refusal may be the rotation the vault would show."""
    from app.services import token_refresh

    _use(FakeVault(secrets={"secret/oauth": VaultReadError("unreachable", "ConnectTimeout")}))
    integration = _referenced_oauth_integration()
    sent: list[str] = []

    async def _post(url, timeout=None, **kwargs):
        sent.append(url)
        return await _refused_client(url)

    async def _allowed(*_args, **_kwargs):
        return True

    monkeypatch.setattr(token_refresh, "_token_endpoint_allowed", _allowed)
    monkeypatch.setattr(vault, "reveal_field", _first_old_then(vault.reveal_field))

    assert await token_refresh._do_refresh_integration(integration, SimpleNamespace(post=_post)) is False
    assert len(sent) == 1 and integration.status == "connected"


async def test_a_refused_client_whose_vault_is_down_backs_the_inline_refresh_off(fake_redis, monkeypatch):
    from app.services import integration_tools

    _use(FakeVault(secrets={"secret/oauth": VaultReadError("unreachable", "ConnectTimeout")}))
    integration = _referenced_oauth_integration()

    async def _allowed(*_args, **_kwargs):
        return None

    verdict, back_off = AsyncMock(), AsyncMock()
    monkeypatch.setattr("app.integrations.token_endpoint.assert_token_endpoint_allowed", _allowed)
    monkeypatch.setattr(integration_tools, "_record_inline_refresh_failure", verdict)
    monkeypatch.setattr(integration_tools, "_start_inline_back_off", back_off)
    # The refresh's own read answers the stale cached value; the helper's read again finds the vault down.
    reveal = _first_old_then(vault.reveal_field)
    monkeypatch.setattr(integration_tools, "reveal_field", reveal)
    monkeypatch.setattr(vault, "reveal_field", reveal)
    posted: list[str] = []

    async def _post(url, timeout=None, **_kwargs):
        posted.append(url)
        return await _refused_client(url)

    class _Client:
        async def __aenter__(self):
            return SimpleNamespace(post=_post)

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(integration_tools, "egress_client", lambda *_a, **_k: _Client())

    db = SimpleNamespace(commit=AsyncMock())
    assert await integration_tools._do_inline_refresh(db, integration, AsyncMock(return_value=True)) is None
    assert posted == ["https://login.example.com/token"], "the refresh never reached the token endpoint"
    verdict.assert_not_awaited()
    back_off.assert_awaited_once()
    assert integration.status == "connected"


def _first_old_then(real):
    """reveal_field answering the stale cached value once, then the vault's."""
    calls = {"n": 0}

    async def _reveal(entity, column):
        calls["n"] += 1
        return "old" if calls["n"] == 1 else await real(entity, column)

    return _reveal


async def test_a_secret_is_read_once_per_cache_period():
    fake = FakeVault(secrets={"secret/github": {"token": "ghp_1"}})
    resolver = _use(fake)
    reference = parse_secret_reference("vault://corp/secret/github#token")
    assert await resolver.read(reference) == "ghp_1"
    assert await resolver.read(reference) == "ghp_1"
    assert fake.reads == 1


async def test_a_rotated_secret_is_picked_up_after_invalidation_without_restart():
    fake = FakeVault(secrets={"secret/github": {"token": "ghp_old"}})
    resolver = _use(fake)
    reference = parse_secret_reference("vault://corp/secret/github#token")
    assert await resolver.read(reference) == "ghp_old"
    fake.secrets["secret/github"] = {"token": "ghp_new"}
    assert await resolver.read(reference) == "ghp_old"  # within the cache period
    resolver.invalidate(reference)
    assert await resolver.read(reference) == "ghp_new"


async def test_a_rotated_secret_is_picked_up_once_the_cache_period_ends(monkeypatch):
    fake = FakeVault(secrets={"secret/github": {"token": "ghp_old"}})
    resolver = _use(fake, cache_seconds=60)
    reference = parse_secret_reference("vault://corp/secret/github#token")
    clock = [1000.0]
    monkeypatch.setattr("app.services.vault.resolver.time.monotonic", lambda: clock[0])
    assert await resolver.read(reference) == "ghp_old"
    fake.secrets["secret/github"] = {"token": "ghp_new"}
    clock[0] += 61
    assert await resolver.read(reference) == "ghp_new"


async def test_a_secret_read_once_leaves_memory_when_its_cache_time_ends():
    """Dropped at expiry even when nothing reads it again, failures included."""
    fake = FakeVault(secrets={"secret/github": {"token": "t"}})
    resolver = _use(fake, cache_seconds=0.05)
    assert await resolver.read(parse_secret_reference("vault://corp/secret/github#token")) == "t"
    assert ("corp", "secret/github") in resolver._cache

    await asyncio.sleep(0.2)

    assert resolver._cache == {}
    fake.secrets["secret/github"] = {"token": "rotated"}
    assert await resolver.read(parse_secret_reference("vault://corp/secret/github#token")) == "rotated"


async def test_concurrent_reads_of_one_secret_share_one_request():
    fake = FakeVault(secrets={"kv/db": {"password": "p"}})
    fake.gate = asyncio.Event()
    resolver = _use(fake)
    reference = parse_secret_reference("vault://corp/kv/db#password")
    readers = [asyncio.create_task(resolver.read(reference)) for _ in range(5)]
    await asyncio.sleep(0)
    fake.gate.set()
    assert await asyncio.gather(*readers) == ["p"] * 5
    assert fake.reads == 1


async def test_a_cancelled_reader_does_not_cancel_the_shared_read():
    fake = FakeVault(secrets={"kv/db": {"password": "p"}})
    fake.gate = asyncio.Event()
    resolver = _use(fake)
    reference = parse_secret_reference("vault://corp/kv/db#password")
    first = asyncio.create_task(resolver.read(reference))
    second = asyncio.create_task(resolver.read(reference))
    await asyncio.sleep(0)
    first.cancel()
    fake.gate.set()
    assert await second == "p"


async def test_a_failed_read_is_not_retried_at_once():
    fake = FakeVault(secrets={})
    resolver = _use(fake)
    reference = parse_secret_reference("vault://corp/missing#k")
    for _ in range(3):
        with pytest.raises(VaultReadError):
            await resolver.read(reference)
    assert fake.reads == 1


async def test_a_key_the_secret_does_not_hold_is_not_fetched_again_at_once(monkeypatch):
    fake = FakeVault(secrets={"kv/db": {"user": "u"}})
    resolver = _use(fake, cache_seconds=0)
    clock = [1000.0]
    monkeypatch.setattr("app.services.vault.resolver.time.monotonic", lambda: clock[0])
    reference = parse_secret_reference("vault://corp/kv/db#password")
    for _ in range(3):
        with pytest.raises(VaultReadError) as missing:
            await resolver.read(reference)
        assert missing.value.reason == "not_found"
    assert fake.reads == 1
    # Another key of the same secret is not held back by it.
    assert await resolver.read(parse_secret_reference("vault://corp/kv/db#user")) == "u"
    assert fake.reads == 2
    fake.secrets["kv/db"] = {"user": "u", "password": "p"}
    clock[0] += FAILURE_RETRY_SECONDS + 1
    assert await resolver.read(reference) == "p"
    assert fake.reads == 3


async def test_invalidating_a_secret_drops_the_keys_it_could_not_select():
    fake = FakeVault(secrets={"kv/db": {"user": "u"}})
    resolver = _use(fake, cache_seconds=0)
    reference = parse_secret_reference("vault://corp/kv/db#password")
    with pytest.raises(VaultReadError):
        await resolver.read(reference)
    fake.secrets["kv/db"] = {"password": "p"}
    resolver.invalidate(parse_secret_reference("vault://corp/kv/db"))
    assert await resolver.read(reference) == "p"


async def test_a_reference_to_an_unavailable_provider_fails_with_its_reason():
    resolver = _use(errors={"broken": "SECRET_VAULT_BROKEN_ADDRESS must be an http(s) URL"})
    with pytest.raises(VaultReadError) as refused:
        await resolver.read(parse_secret_reference("vault://broken/x"))
    assert refused.value.reason == "misconfigured"
    with pytest.raises(VaultReadError) as unknown:
        await resolver.read(parse_secret_reference("vault://nope/x"))
    assert unknown.value.reason == "provider_not_configured"


# ---------------------------------------------------------------------------
# Reading a secret where it is used
# ---------------------------------------------------------------------------


def test_decrypt_value_refuses_a_reference_and_the_stored_reader_keeps_it():
    stored = encrypt_value("vault://corp/secret/github#token")
    with pytest.raises(SecretReferenceError):
        decrypt_value(stored)
    with pytest.raises(SecretReferenceError):
        decrypt_value(encrypt_value("vault://corp/llm#k||https://gw"))
    assert decrypt_stored_value(stored) == "vault://corp/secret/github#token"
    assert decrypt_value(encrypt_value("plain")) == "plain"
    assert decrypt_json(encrypt_value(json.dumps({"a": "x||vault://corp/y"}))) == {"a": "x||vault://corp/y"}


async def test_reveal_field_resolves_a_reference_and_passes_a_value_through(fake_redis):
    _use(FakeVault(secrets={"secret/github": {"token": "ghp_resolved"}}))
    row = _integration(
        api_key_enc=encrypt_value("vault://corp/secret/github#token"),
        client_secret_enc=encrypt_value("plain-secret"),
    )
    assert await vault.reveal_field(row, "api_key_enc") == "ghp_resolved"
    assert await vault.reveal_field(row, "client_secret_enc") == "plain-secret"
    assert await vault.reveal_field(_integration(api_key_enc=None), "api_key_enc") is None


async def test_a_compound_key_is_resolved_segment_by_segment():
    _use(FakeVault(secrets={"llm": {"openai": "sk-1"}}))
    assert await vault.resolve_secret_text("vault://corp/llm#openai||https://gw/v1") == "sk-1||https://gw/v1"


async def test_an_unreadable_reference_fails_generically_and_is_recorded(fake_redis, caplog):
    _use(FakeVault(secrets={"secret/github": VaultReadError("access_denied", "HTTP 403")}))
    row = _integration(api_key_enc=encrypt_value("vault://corp/secret/github#token"))
    with caplog.at_level(logging.WARNING), pytest.raises(SecretUnavailableError) as exc:
        await vault.reveal_field(row, "api_key_enc")
    assert str(exc.value) == VAULT_UNAVAILABLE_DETAIL
    assert exc.value.reason == "access_denied"
    assert "secret/github" not in str(exc.value)
    assert "access_denied" in caplog.text
    recorded = await failures.entity_failures("integration", row.id)
    assert [(e["field"], e["provider"], e["reason"]) for e in recorded] == [("api_key", "corp", "access_denied")]


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("vault://nowhere/secret/github#token", "provider_not_configured"),
        ("vault://corp/other/github#token", "path_not_allowed"),
        ("vault://corp/xtm-one/github#nope", "not_found"),
        ("vault://corp/xtm-one/github", "key_required"),
    ],
)
async def test_a_refusal_the_vault_never_answered_is_logged_once(fake_redis, caplog, text, reason):
    _use(FakeVault(secrets={"xtm-one/github": {"token": "ghp_TOPSECRET", "user": "bot"}}, PATHS="xtm-one"))
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            with pytest.raises(SecretUnavailableError) as exc:
                await vault.resolve_secret_text(text)
            assert str(exc.value) == VAULT_UNAVAILABLE_DETAIL
    lines = [r.getMessage() for r in caplog.records if "could not be resolved" in r.getMessage()]
    assert len(lines) == 1
    assert f"reason={reason}" in lines[0]
    assert f"path={parse_secret_reference(text).path}" in lines[0]
    assert "ghp_TOPSECRET" not in caplog.text


async def test_a_success_clears_the_recorded_failure(fake_redis):
    fake = FakeVault(secrets={"secret/github": VaultReadError("unreachable", "ConnectError")})
    resolver = _use(fake, cache_seconds=0)
    row = _integration(api_key_enc=encrypt_value("vault://corp/secret/github#token"))
    with pytest.raises(SecretUnavailableError):
        await vault.reveal_field(row, "api_key_enc")
    assert await failures.entity_failures("integration", row.id)
    fake.secrets["secret/github"] = {"token": "ok"}
    resolver.invalidate(parse_secret_reference("vault://corp/secret/github"))
    assert await vault.reveal_field(row, "api_key_enc") == "ok"
    assert await failures.entity_failures("integration", row.id) == []


async def test_a_resolved_value_never_reaches_the_logs(fake_redis, caplog):
    _use(FakeVault(secrets={"secret/github": {"token": "ghp_TOPSECRET"}}))
    row = _integration(api_key_enc=encrypt_value("vault://corp/secret/github#token"))
    with caplog.at_level(logging.DEBUG):
        assert await vault.reveal_field(row, "api_key_enc") == "ghp_TOPSECRET"
    assert "ghp_TOPSECRET" not in caplog.text


async def test_mcp_headers_resolve_value_by_value():
    _use(FakeVault(secrets={"mcp/acme": {"auth": "Bearer abc"}}))
    server = SimpleNamespace(
        id="22222222-2222-2222-2222-222222222222",
        headers_enc=encrypt_value(json.dumps({"Authorization": "vault://corp/mcp/acme#auth", "X-Team": "red"})),
    )
    server.__tablename__ = "mcp_servers"
    assert await vault.reveal_json_field(server, "headers_enc") == {"Authorization": "Bearer abc", "X-Team": "red"}


async def test_a_channel_bot_extra_config_resolves_value_by_value():
    """A channel bot's extra config (a Slack signing secret, a WhatsApp app secret) may read from a vault (#6080)."""
    _use(FakeVault(secrets={"bots/slack": {"signing": "s3cr3t"}}))
    bot = SimpleNamespace(
        id="33333333-3333-3333-3333-333333333333",
        extra_config_enc=encrypt_value(json.dumps({"signing_secret": "vault://corp/bots/slack#signing", "team": "T1"})),
    )
    bot.__tablename__ = "channel_bots"
    assert await vault.reveal_json_field(bot, "extra_config_enc") == {"signing_secret": "s3cr3t", "team": "T1"}
    assert vault.stored_reference_providers(bot, ["extra_config_enc"]) == {"extra_config": ["corp"]}


async def test_the_integration_tools_read_their_extra_config_through_the_resolver():
    """The credential a tool sends from an integration's extra config resolves a vault reference (#6080, slice 2a)."""
    from app.services import integration_tools

    _use(FakeVault(secrets={"tools/aws": {"secret": "s3cr3t"}}))
    integ = SimpleNamespace(
        id="44444444-4444-4444-4444-444444444444",
        extra_config_enc=encrypt_value(
            json.dumps({"secret_access_key": "vault://corp/tools/aws#secret", "region": "eu-west-1"})
        ),
    )
    integ.__tablename__ = "integrations"
    assert await integration_tools._reveal_extra(integ) == {"secret_access_key": "s3cr3t", "region": "eu-west-1"}
    integ.extra_config_enc = encrypt_value(json.dumps(["not", "an", "object"]))
    assert await integration_tools._reveal_extra(integ) == {}
    integ.extra_config_enc = None
    assert await integration_tools._reveal_extra(integ) == {}


async def test_the_route_readers_of_an_integration_extra_config_resolve_a_reference():
    """The OpenRouter management key, the WhatsApp gateway pairing config and the GitHub webhook
    secret read a vault reference from an integration's extra config (#6080, slice 2b-3)."""
    from app.api.v1 import integration_github_webhooks, whatsapp_gateway_pairing
    from app.api.v1 import openrouter as console

    _use(FakeVault(secrets={"keys/openrouter": {"mgmt": "sk-or-v1-mgmt"}, "hooks/gh": {"secret": "wh-s3cr3t"}}))
    integ = SimpleNamespace(
        id="55555555-5555-5555-5555-555555555555",
        extra_config_enc=encrypt_value(
            json.dumps(
                {
                    "management_key": "vault://corp/keys/openrouter#mgmt",
                    "webhook_secret": "vault://corp/hooks/gh#secret",
                    "backend": "linked_device_gateway",
                }
            )
        ),
    )
    integ.__tablename__ = "integrations"
    assert await console._management_key(integ) == "sk-or-v1-mgmt"
    assert await integration_github_webhooks._webhook_secret(integ) == "wh-s3cr3t"
    extra = await whatsapp_gateway_pairing._extra(integ)
    assert extra["webhook_secret"] == "wh-s3cr3t"
    assert extra["backend"] == "linked_device_gateway"
    # The managed-hooks merge keeps the stored document as written: the reference is never replaced by its value.
    assert integration_github_webhooks._get_extra_config(integ)["webhook_secret"] == "vault://corp/hooks/gh#secret"
    integ.extra_config_enc = None
    assert await console._management_key(integ) is None
    assert await integration_github_webhooks._webhook_secret(integ) == ""
    assert await whatsapp_gateway_pairing._extra(integ) == {}


async def test_the_route_readers_of_an_integration_extra_config_read_a_plain_stored_secret():
    """A plain (non-vault) management key and webhook secret are read as stored, no vault read (#6080, slice 2b-3)."""
    from app.api.v1 import integration_github_webhooks
    from app.api.v1 import openrouter as console

    provider = FakeVault()
    _use(provider)
    integ = SimpleNamespace(
        id="66666666-6666-6666-6666-666666666666",
        extra_config_enc=encrypt_value(
            json.dumps({"management_key": " sk-or-v1-plain ", "webhook_secret": "wh-plain"})
        ),
    )
    integ.__tablename__ = "integrations"
    assert await console._management_key(integ) == "sk-or-v1-plain"
    assert await integration_github_webhooks._webhook_secret(integ) == "wh-plain"
    assert provider.reads == 0
    integ.extra_config_enc = encrypt_value(json.dumps({"management_key": "  ", "webhook_secret": None}))
    assert await console._management_key(integ) is None
    assert await integration_github_webhooks._webhook_secret(integ) == ""


async def test_the_oauth_url_readers_of_an_integration_extra_config_resolve_a_reference():
    """The Entra tenant id, the DocuSign environment and a custom service's base URL read a vault
    reference from an integration's extra config (#6080, slice 2b-4)."""
    from app.integrations import oauth, service_token_exchange

    _use(FakeVault(secrets={"oauth/ms": {"tenant": "TENANT-XYZ"}, "oauth/svc": {"url": "https://svc.example.com/"}}))
    entra = SimpleNamespace(
        id="66666666-6666-6666-6666-666666666666",
        provider="entra",
        extra_config_enc=encrypt_value(json.dumps({"tenant_id": "vault://corp/oauth/ms#tenant"})),
    )
    entra.__tablename__ = "integrations"
    common = "https://login.microsoftonline.com/common/oauth2/v2.0"
    authorize, token = await oauth._resolve_tenant_urls(entra, f"{common}/authorize", f"{common}/token")
    assert "/TENANT-XYZ/" in authorize and "/TENANT-XYZ/" in token

    service = SimpleNamespace(
        id="77777777-7777-7777-7777-777777777777",
        extra_config_enc=encrypt_value(json.dumps({"base_url": "vault://corp/oauth/svc#url"})),
    )
    service.__tablename__ = "integrations"
    assert await service_token_exchange.service_base_url(service) == "https://svc.example.com"

    docusign = SimpleNamespace(
        id="88888888-8888-8888-8888-888888888888",
        provider="docusign",
        extra_config_enc=encrypt_value(json.dumps({"environment": "demo"})),
    )
    docusign.__tablename__ = "integrations"
    assert await oauth.provider_token_url(docusign, "https://account.docusign.com/oauth/token") == (
        "https://account-d.docusign.com/oauth/token"
    )


def test_a_channel_bot_request_takes_a_reference_in_its_extra_config():
    from app.api.v1.schemas.channel_bot import CreateChannelBotRequest, UpdateChannelBotRequest

    _use(FakeVault(secrets={}))
    body = {"extra_config": {"signing_secret": "vault://corp/bots/slack#signing"}}
    assert UpdateChannelBotRequest(**body).extra_config == body["extra_config"]
    assert CreateChannelBotRequest(provider="slack_bot", name="b", **body).extra_config == body["extra_config"]
    with pytest.raises(ValueError, match="vault"):
        UpdateChannelBotRequest(client_id="vault://corp/bots/slack#id")


@pytest.mark.parametrize(
    ("transport", "sent_column", "kept_column"),
    [("streamable_http", "headers_enc", "env_enc"), ("sse", "headers_enc", "env_enc"), ("stdio", "env_enc", "headers_enc")],
)
async def test_an_mcp_server_resolves_only_the_credentials_its_transport_sends(
    fake_redis, transport, sent_column, kept_column
):
    """A transport switch keeps the other map stored: an unreadable reference there never takes the server down."""
    from app.api.v1.helpers import build_mcp_runtime

    _use(FakeVault(secrets={"mcp/acme": {"token": "t0ken"}}))
    server = _mcp_server(
        name="Acme",
        transport=transport,
        command="npx" if transport == "stdio" else None,
        is_company_managed=False,
        **{
            sent_column: encrypt_value(json.dumps({"TOKEN": "vault://corp/mcp/acme#token"})),
            kept_column: encrypt_value(json.dumps({"OLD": "vault://corp/mcp/gone#token"})),
        },
    )

    runtime = await build_mcp_runtime(server)

    sent, kept = (runtime.env, runtime.headers) if transport == "stdio" else (runtime.headers, runtime.env)
    assert sent == {"TOKEN": "t0ken"}
    assert kept is None
    # The other transport's reference is never read, so no failure is recorded for it.
    assert fake_redis.hashes == {}


async def test_an_oauth_mcp_server_never_reads_the_authorization_header_its_token_replaces(fake_redis):
    """Switched from header auth to OAuth, a server keeps its headers: the Authorization one is replaced, so never read."""
    from app.api.v1.helpers import build_mcp_runtime

    _use(FakeVault(secrets={"mcp/acme": {"team": "red"}}))
    server = _mcp_server(
        name="Acme",
        is_company_managed=False,
        auth_mode="oauth",
        access_token_enc=encrypt_value("oauth-token"),
        token_expires_at=None,
        headers_enc=encrypt_value(
            json.dumps({"authorization": "vault://corp/mcp/gone#token", "X-Team": "vault://corp/mcp/acme#team"})
        ),
    )

    runtime = await build_mcp_runtime(server)

    # The bearer token travels through the refreshing auth hook, not the static headers.
    assert runtime.headers == {"X-Team": "red"}
    assert runtime.auth is not None
    assert fake_redis.hashes == {}


async def test_an_oauth_mcp_server_without_a_token_still_reads_its_authorization_header(fake_redis):
    from app.api.v1.helpers import build_mcp_runtime

    _use(FakeVault(secrets={}))
    server = _mcp_server(
        name="Acme",
        is_company_managed=False,
        auth_mode="oauth",
        access_token_enc=None,
        headers_enc=encrypt_value(json.dumps({"Authorization": "vault://corp/mcp/gone#token"})),
    )

    with pytest.raises(SecretUnavailableError):
        await build_mcp_runtime(server)


async def test_an_upstream_refusal_invalidates_the_cached_value():
    fake = FakeVault(secrets={"secret/github": {"token": "old"}})
    _use(fake)
    row = _integration(api_key_enc=encrypt_value("vault://corp/secret/github#token"))
    assert await vault.reveal_field(row, "api_key_enc") == "old"
    fake.secrets["secret/github"] = {"token": "new"}
    await vault.invalidate_entity_secrets(row, ("api_key_enc", "client_secret_enc"))
    assert await vault.reveal_field(row, "api_key_enc") == "new"
    assert fake.reads == 2


def test_responses_name_the_provider_and_nothing_else():
    row = _integration(
        api_key_enc=encrypt_value("vault://corp/secret/github#token"),
        client_secret_enc=encrypt_value("plain"),
    )
    assert vault.stored_reference_providers(row, ("api_key_enc", "client_secret_enc")) == {"api_key": ["corp"]}


async def test_the_provider_list_names_providers_and_never_their_settings():
    from app.api.v1.settings import list_vault_providers

    resolver = SecretResolver(
        {"corp": FakeVault("corp")},
        {"broken": "SECRET_VAULT_BROKEN_ADDRESS is required"},
        120,
    )
    set_resolver(resolver)

    response = await list_vault_providers(user=SimpleNamespace(is_admin=True))

    assert response.providers == ["broken", "corp"]
    assert set(response.model_dump()) == {"providers", "cache_seconds"}


async def test_the_provider_list_offers_a_user_only_the_providers_they_may_write_to():
    from app.api.v1.settings import list_vault_providers

    _use(FakeVault("corp"), FakeVault("team", WRITERS="all"), errors={"broken": "bad"})

    assert (await list_vault_providers(user=SimpleNamespace(is_admin=False))).providers == ["team"]
    assert (await list_vault_providers(user=SimpleNamespace(is_admin=True))).providers == ["broken", "corp", "team"]


async def test_an_integration_response_and_export_never_hold_the_value(fake_redis):
    from datetime import UTC, datetime

    from app.api.v1.integrations import _build_bundle_integration_instance_payload, _to_response
    from app.api.v1.schemas.vault_references import attach_vault_failures
    from app.models.integration import Integration

    now = datetime.now(UTC)
    integ = Integration(
        id="44444444-4444-4444-4444-444444444444",
        provider="github",
        auth_type="api_key",
        status="connected",
        name="GitHub",
        api_key_enc=encrypt_value("vault://corp/secret/github#token"),
        is_company_managed=False,
        is_builtin=False,
        tags=[],
        oauth_share_acknowledged=False,
        created_at=now,
        updated_at=now,
    )
    await failures.record_failure(vault.owner_of(integ, "api_key_enc"), "corp", "access_denied", _GITHUB_REF)
    response = _to_response(integ, can_manage=True, can_authorize=True)
    await attach_vault_failures([response], "integrations")
    body = response.model_dump_json()
    assert response.has_api_key is True
    assert response.vault_secrets.references == {"api_key": ["corp"]}
    assert [failure.reason for failure in response.vault_secrets.failures] == ["access_denied"]
    assert "secret/github" not in body
    assert _build_bundle_integration_instance_payload(integ)["api_key"] == "vault://corp/secret/github#token"


async def test_the_failure_record_is_read_per_entity_in_one_round_trip(fake_redis):
    owner = vault.SecretOwner("integration", "abc", "api_key")
    await failures.record_failure(owner, "corp", "not_found", _GITHUB_REF)
    assert (await failures.failures_by_entity("integration", ["abc", "def"]))["abc"][0]["reason"] == "not_found"
    recent = await failures.recent_failures()
    assert recent is not None and recent.entries[0]["id"] == "abc"
    assert recent.truncated is False


async def test_the_audit_reads_the_whole_failure_record(fake_redis, monkeypatch):
    """Every entity the index retains is read (one pipelined round trip), and a full index says so."""
    monkeypatch.setattr(failures, "_INDEX_SIZE", 3)
    for index in range(3):
        await failures.record_failure(
            vault.SecretOwner("integration", f"id-{index}", "api_key"), "corp", "not_found", _GITHUB_REF
        )

    history = await failures.recent_failures()

    assert history is not None
    assert sorted(entry["id"] for entry in history.entries) == ["id-0", "id-1", "id-2"]
    assert history.truncated is True


@pytest.mark.parametrize("after_call", [1, 2, 3])
async def test_a_failure_recorded_while_another_field_is_cleared_stays_in_the_audit(fake_redis, monkeypatch, after_call):
    """Another worker's failure may land between any two Redis calls of a clear: its index entry survives."""
    cleared = vault.SecretOwner("integration", "abc", "api_key")
    meanwhile = vault.SecretOwner("integration", "abc", "client_secret")
    await failures.record_failure(cleared, "corp", "not_found", _GITHUB_REF)
    calls = 0
    landed = False

    class _Interleaving:
        """The fake Redis, letting the other worker's record land after the clear's *after_call*-th call."""

        def __getattr__(self, name):
            target = getattr(fake_redis, name)
            if name not in {"hdel", "exists", "zrem", "eval"}:
                return target

            async def _call(*args, **kwargs):
                nonlocal calls, landed
                result = await target(*args, **kwargs)
                calls += 1
                if calls == after_call and not landed:
                    landed = True
                    await failures.record_failure(meanwhile, "corp", "access_denied", _GITHUB_REF)
                return result

            return _call

    proxy = _Interleaving()

    async def _client():
        return proxy

    monkeypatch.setattr(failures, "_redis", _client)
    await failures.clear_failure(cleared)
    if not landed:
        await failures.record_failure(meanwhile, "corp", "access_denied", _GITHUB_REF)

    history = await failures.recent_failures()
    assert history is not None
    assert [(entry["id"], entry["field"]) for entry in history.entries] == [("abc", "client_secret")]


async def test_a_newer_failure_never_extends_an_older_field_record(fake_redis):
    """Each field's record ends with the retention, whatever the entity's other fields keep failing."""
    await failures.record_failure(vault.SecretOwner("integration", "abc", "api_key"), "corp", "not_found", _GITHUB_REF)
    key = failures._entity_key("integration", "abc")
    stale = json.loads(fake_redis.hashes[key]["api_key"])
    stale["at"] -= failures.FAILURE_RETENTION_SECONDS + 60
    fake_redis.hashes[key]["api_key"] = json.dumps(stale)
    await failures.record_failure(
        vault.SecretOwner("integration", "abc", "client_secret"), "corp", "access_denied", _GITHUB_REF
    )

    assert [entry["field"] for entry in await failures.entity_failures("integration", "abc")] == ["client_secret"]
    history = await failures.recent_failures()
    assert history is not None
    assert [entry["field"] for entry in history.entries] == ["client_secret"]


async def test_an_llm_provider_is_rebuilt_once_its_vault_key_rotates(fake_redis, monkeypatch):
    from app.llm import router

    fake = FakeVault(secrets={"llm/openai": {"key": "sk-old"}})
    _use(fake, cache_seconds=0)
    built: list[str | None] = []

    def _build(config, api_key=None):
        built.append(api_key)
        return SimpleNamespace(api_key=api_key)

    monkeypatch.setattr(router, "build_provider", _build)
    config = SimpleNamespace(
        id="55555555-5555-5555-5555-555555555555",
        provider="openai",
        api_key_enc=encrypt_value("vault://corp/llm/openai#key"),
    )
    config.__tablename__ = "llm_provider_configs"
    router.invalidate_provider_cache()
    try:
        first = await router._cached_provider(config)
        assert await router._cached_provider(config) is first
        fake.secrets["llm/openai"] = {"key": "sk-new"}
        rotated = await router._cached_provider(config)
        assert rotated is not first
        assert rotated.api_key == "sk-new"
        assert built == ["sk-old", "sk-new"]
    finally:
        router.invalidate_provider_cache()


@pytest.mark.parametrize(("cache_seconds", "served"), [(0, False), (300, True)])
async def test_the_failover_chain_serves_a_vault_key_no_older_than_the_resolver_would(
    fake_redis, monkeypatch, cache_seconds, served
):
    """The synchronous chain cannot read a vault: a cached instance is served only within the resolver's TTL."""
    from app.llm import router

    fake = FakeVault(secrets={"llm/openai": {"key": "sk-old"}})
    _use(fake, cache_seconds=cache_seconds)

    def _build(config, api_key=None):
        if api_key is None:
            raise SecretReferenceError("the stored value is a vault reference")
        return SimpleNamespace(api_key=api_key)

    monkeypatch.setattr(router, "build_provider", _build)
    config = SimpleNamespace(
        id="57575757-5757-5757-5757-575757575757",
        provider="openai",
        api_key_enc=encrypt_value("vault://corp/llm/openai#key"),
    )
    config.__tablename__ = "llm_provider_configs"
    router.invalidate_provider_cache()
    try:
        cached = await router._cached_provider(config)
        if served:
            assert router._get_cached_provider(config) is cached
        else:
            with pytest.raises(SecretReferenceError):
                router._get_cached_provider(config)
    finally:
        router.invalidate_provider_cache()


async def test_a_failover_chain_uses_the_keys_read_for_its_call(fake_redis, monkeypatch):
    from app.llm import router

    fake = FakeVault(secrets={"llm/openai": {"key": "sk-old"}})
    _use(fake, cache_seconds=0)

    def _build(config, api_key=None):
        if api_key is None:
            raise SecretReferenceError("the stored value is a vault reference")
        return SimpleNamespace(api_key=api_key, provider_name=config.provider)

    monkeypatch.setattr(router, "build_provider", _build)
    fallback = SimpleNamespace(
        id="58585858-5858-5858-5858-585858585858",
        provider="openai",
        api_key_enc=encrypt_value("vault://corp/llm/openai#key"),
    )
    fallback.__tablename__ = "llm_provider_configs"
    primary = SimpleNamespace(
        provider_name="anthropic",
        max_tools=128,
        context_window=200_000,
        context_windows={},
        max_output_tokens=8192,
        max_output_tokens_overrides={},
    )
    router.invalidate_provider_cache()
    try:
        await router._cached_provider(fallback)
        fake.secrets["llm/openai"] = {"key": "sk-new"}

        prepared = await router.warm_referenced_providers([fallback])
        chain = router.FailoverLLMProvider(primary=primary, fallback_configs=[fallback], prepared=prepared)

        assert [provider.api_key for provider in chain.fallbacks] == ["sk-new"]
        assert prepared[str(fallback.id)] is chain.fallbacks[0]
    finally:
        router.invalidate_provider_cache()


def _discord_bot(reference: str = "vault://corp/discord#token") -> SimpleNamespace:
    bot = SimpleNamespace(id="66666666-6666-6666-6666-666666666666", api_key_enc=encrypt_value(reference), extra_config_enc=None)
    bot.__tablename__ = "channel_bots"
    return bot


async def test_the_discord_gateway_reconnects_with_the_rotated_token(monkeypatch, fake_redis):
    """A gateway session keeps its token; when Discord ends it, the next one reads the vault again, not the cache."""
    from app.channels import discord as discord_channel

    fake = FakeVault(secrets={"discord": {"token": "old"}})
    _use(fake)
    bot = _discord_bot()

    async def _bot(_provider):
        return bot

    monkeypatch.setattr(discord_channel, "get_channel_bot", _bot)
    monkeypatch.setattr(discord_channel, "GATEWAY_RECONNECT_SECONDS", 0.0)
    stop = asyncio.Event()
    sessions: list[str] = []

    async def _session(token):
        sessions.append(token)
        if len(sessions) == 1:
            fake.secrets["discord"] = {"token": "new"}  # rotated; Discord ends the old token's session
        else:
            stop.set()
        return True

    monkeypatch.setattr(discord_channel, "_run_discord_gateway", _session)
    creds = await discord_channel._get_discord_creds()
    await asyncio.wait_for(discord_channel._supervise_discord_gateway(creds["token"], stop), timeout=5)

    assert sessions == ["old", "new"]


async def test_the_discord_gateway_retries_a_token_it_cannot_read_and_stops_when_told(monkeypatch):
    from app.channels import discord as discord_channel

    monkeypatch.setattr(discord_channel, "GATEWAY_RECONNECT_SECONDS", 0.0)
    stop = asyncio.Event()
    sessions: list[str] = []
    reads = iter([SecretUnavailableError("unreachable"), {"token": "t2"}])

    async def _creds(*, refresh=False):
        assert refresh
        answer = next(reads)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def _session(token):
        sessions.append(token)
        if token == "t2":
            stop.set()
        return True

    monkeypatch.setattr(discord_channel, "_get_discord_creds", _creds)
    monkeypatch.setattr(discord_channel, "_run_discord_gateway", _session)
    await asyncio.wait_for(discord_channel._supervise_discord_gateway("t1", stop), timeout=5)

    assert sessions == ["t1", "t2"]


async def test_a_discord_token_unreadable_at_startup_is_read_again_until_the_vault_answers(monkeypatch):
    """Through start_discord_gateway(): a vault down at boot does not leave the bot offline for good."""
    from app.channels import discord as discord_channel

    monkeypatch.setattr(discord_channel, "GATEWAY_RECONNECT_SECONDS", 0.0)
    reads = iter([SecretUnavailableError("unreachable"), SecretUnavailableError("unreachable"), {"token": "t1"}])
    sessions: list[str] = []

    async def _creds(*, refresh=False):
        answer = next(reads)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def _session(token):
        sessions.append(token)
        discord_channel._gateway_stop_event.set()
        return True

    monkeypatch.setattr(discord_channel, "_get_discord_creds", _creds)
    monkeypatch.setattr(discord_channel, "_run_discord_gateway", _session)
    try:
        await discord_channel.start_discord_gateway()
        await asyncio.wait_for(discord_channel._gateway_task, timeout=5)
    finally:
        discord_channel._gateway_task = None
        discord_channel._gateway_stop_event = None

    assert sessions == ["t1"]


async def test_no_discord_bot_starts_no_gateway(monkeypatch):
    from app.channels import discord as discord_channel

    async def _creds(*, refresh=False):
        return None

    monkeypatch.setattr(discord_channel, "_get_discord_creds", _creds)
    await discord_channel.start_discord_gateway()
    assert discord_channel._gateway_task is None


async def test_a_discord_gateway_that_cannot_run_is_not_retried(monkeypatch):
    from app.channels import discord as discord_channel

    calls: list[str] = []

    async def _session(token):
        calls.append(token)
        return False  # discord.py is not installed

    monkeypatch.setattr(discord_channel, "_run_discord_gateway", _session)
    await asyncio.wait_for(discord_channel._supervise_discord_gateway("t1", asyncio.Event()), timeout=5)

    assert calls == ["t1"]


# ---------------------------------------------------------------------------
# Security Audit
# ---------------------------------------------------------------------------


class _AuditDB:
    """Answers each per-table read of the audit with the rows given for that table."""

    def __init__(self, rows: dict[str, list[Any]] | None = None) -> None:
        self.rows = rows or {}

    async def execute(self, statement):
        table = statement.get_final_froms()[0].name
        return SimpleNamespace(all=lambda: self.rows.get(table, []))


async def test_the_audit_counts_a_failure_only_while_its_field_holds_a_reference(fake_redis):
    from app.services.vault.audit import gather_vault_facts

    _use(FakeVault())
    row = SimpleNamespace(
        id="66666666-6666-6666-6666-666666666666",
        name="GitHub",
        provider="github",
        api_key_enc=encrypt_value("vault://corp/secret/github#token"),
        client_secret_enc=encrypt_value("now-a-plain-value"),
    )
    await failures.record_failure(
        vault.SecretOwner("integration", row.id, "api_key"), "corp", "access_denied", _GITHUB_REF
    )
    # Recorded while the client secret was still a reference, before it was rewritten as a value.
    await failures.record_failure(
        vault.SecretOwner("integration", row.id, "client_secret"),
        "corp",
        "not_found",
        parse_secret_reference("vault://corp/secret/github#client_secret"),
    )

    facts = await gather_vault_facts(_AuditDB({"integrations": [row]}))

    label = f"integration 'GitHub' ({row.id})"
    assert facts.references == {f"{label}: api_key": ["corp"]}
    assert facts.failures == [f"{label}: api_key (corp: {vault.REASON_DESCRIPTIONS['access_denied']})"]


@pytest.mark.parametrize(
    ("endpoint", "reported"),
    [("http://secrets.internal", ["aws"]), ("http://localhost:4566", []), ("https://secrets.internal", [])],
)
async def test_an_aws_endpoint_over_plain_http_is_reported(fake_redis, endpoint, reported):
    from app.services.vault.audit import gather_vault_facts
    from app.services.vault.aws import AWSSecretsManagerProvider

    _use(AWSSecretsManagerProvider(VaultProviderConfig(name="aws", type="aws", settings={"ENDPOINT_URL": endpoint})))

    facts = await gather_vault_facts(_AuditDB())

    assert facts.providers["aws"]["address"] == endpoint
    assert facts.plain_http == reported


@pytest.mark.parametrize(
    ("recorded", "truncated", "finding_ids"),
    [
        (None, False, ["encryption.vault_failures_unknown"]),
        ([], False, ["encryption.vault_references_resolving"]),
        # A full record whose entries are all history: an older failure may have left it.
        ([], True, ["encryption.vault_failures_unknown"]),
    ],
)
async def test_the_audit_claims_no_pass_when_the_failures_could_not_be_read(
    monkeypatch, recorded, truncated, finding_ids
):
    from app.api.v1.security_audit import _check_secret_vaults
    from app.services.vault import audit

    async def _facts(_db):
        return audit.VaultAuditFacts(
            providers={"corp": {"type": "hashicorp", "address": "https://vault.acme.test"}},
            references={"integration 'GitHub' (1): api_key": ["corp"]},
            failures=recorded,
            failures_truncated=truncated,
        )

    monkeypatch.setattr(audit, "gather_vault_facts", _facts)
    findings: list[Any] = []

    await _check_secret_vaults(None, findings)

    assert [finding.id for finding in findings] == finding_ids


async def test_a_failure_of_a_replaced_reference_is_history(fake_redis):
    """A field rewritten from a failing reference to another one no longer shows the old failure."""
    from app.api.v1.schemas.vault_references import attach_vault_failures, vault_status
    from app.services.vault.audit import gather_vault_facts

    _use(FakeVault(), cache_seconds=0)
    row = _integration(name="GitHub", provider="github", api_key_enc=encrypt_value("vault://corp/secret/old#token"))
    with pytest.raises(SecretUnavailableError):
        await vault.reveal_field(row, "api_key_enc")

    async def _shown() -> list[str]:
        response = SimpleNamespace(id=row.id, vault_secrets=vault_status(row))
        await attach_vault_failures([response], "integrations")
        return [failure.reason for failure in response.vault_secrets.failures]

    assert await _shown() == ["not_found"]
    assert len((await gather_vault_facts(_AuditDB({"integrations": [row]}))).failures) == 1

    row.api_key_enc = encrypt_value("vault://corp/secret/new#token")

    assert await _shown() == []
    assert (await gather_vault_facts(_AuditDB({"integrations": [row]}))).failures == []
    assert "secret/old" not in json.dumps(fake_redis.hashes)


async def test_every_provider_an_mcp_document_names_is_reported(fake_redis):
    from app.api.v1.schemas.vault_references import vault_status
    from app.services.vault.audit import gather_vault_facts

    _use(FakeVault(), FakeVault("aws"))
    server = _mcp_server(
        name="Acme",
        headers_enc=encrypt_value(
            json.dumps({"A": "vault://corp/mcp#a", "B": "vault://aws/mcp#b", "C": {"nested": "vault://ghost/x"}})
        ),
    )

    assert vault_status(server).references == {"headers": ["aws", "corp", "ghost"]}
    facts = await gather_vault_facts(_AuditDB({"mcp_servers": [server]}))
    label = f"MCP server 'Acme' ({server.id})"
    assert facts.references == {f"{label}: headers": ["aws", "corp", "ghost"]}
    assert facts.unknown_provider == [f"{label}: headers -> ghost"]


async def test_a_malformed_stored_reference_is_reported_on_the_entity_and_in_the_audit(fake_redis):
    """A value stored before ``vault://`` was reserved that does not parse fails every use: diagnosed, never dropped."""
    from app.api.v1.schemas.vault_references import attach_vault_failures, vault_status
    from app.api.v1.security_audit import _check_secret_vaults
    from app.services.vault.audit import gather_vault_facts

    _use(FakeVault())
    row = _integration(
        name="GitHub",
        provider="github",
        api_key_enc=encrypt_value("vault://Legacy Token 42"),
        client_secret_enc=encrypt_value("vault://corp/secret/github#secret"),
    )
    with pytest.raises(SecretUnavailableError) as exc:
        await vault.reveal_field(row, "api_key_enc")
    assert exc.value.reason == "invalid_reference"

    # Diagnosed from the stored value, so the failed read records nothing.
    assert fake_redis.hashes == {}
    response = SimpleNamespace(id=row.id, vault_secrets=vault_status(row))
    await attach_vault_failures([response], "integrations")
    assert response.vault_secrets.invalid_references == ["api_key"]
    assert response.vault_secrets.references == {"client_secret": ["corp"]}
    assert response.vault_secrets.failures == []
    assert "Legacy Token" not in response.vault_secrets.model_dump_json()

    label = f"integration 'GitHub' ({row.id})"
    facts = await gather_vault_facts(_AuditDB({"integrations": [row]}))
    assert facts.invalid_references == [f"{label}: api_key"]
    assert facts.failures == []

    findings: list[Any] = []
    await _check_secret_vaults(_AuditDB({"integrations": [row]}), findings)
    flagged = [finding for finding in findings if finding.id == "encryption.vault_reference_invalid"]
    assert len(flagged) == 1
    assert f"{label}: api_key" in flagged[0].description
    assert "Legacy Token" not in f"{flagged[0].title} {flagged[0].description} {flagged[0].recommendation}"
    assert "encryption.vault_references_resolving" not in {finding.id for finding in findings}


async def test_a_malformed_value_is_reported_with_no_vault_provider_declared(fake_redis):
    """The pre-upgrade case: nothing is declared, yet a stored value starting with ``vault://`` fails every use."""
    from app.api.v1.schemas.vault_references import vault_status
    from app.api.v1.security_audit import _check_secret_vaults

    _use()
    row = _integration(name="Legacy", provider="github", api_key_enc=encrypt_value("vault:// not a reference"))
    findings: list[Any] = []

    await _check_secret_vaults(_AuditDB({"integrations": [row]}), findings)

    assert [finding.id for finding in findings] == ["encryption.vault_reference_invalid"]
    assert vault_status(row).model_dump() == {
        "references": {},
        "invalid_references": ["api_key"],
        "failures": [],
        "locations": {},
        "variables": {},
    }


def test_a_document_holding_a_malformed_value_keeps_its_valid_references():
    from app.api.v1.schemas.vault_references import vault_status

    _use(FakeVault())
    server = _mcp_server(
        headers_enc=encrypt_value(json.dumps({"A": "vault://corp/mcp#a", "B": {"nested": "vault://Not/Valid"}}))
    )

    status = vault_status(server)

    assert status.references == {"headers": ["corp"]}
    assert status.invalid_references == ["headers"]
    fingerprints = {"headers": frozenset({"abc"})}
    assert failures.failure_matches({"field": "headers", "ref": "abc"}, fingerprints) is True
    # A record without a fingerprint matches nothing, whatever the field holds now.
    assert failures.failure_matches({"field": "headers", "ref": ""}, fingerprints) is False


async def test_a_malformed_value_replaced_before_its_first_read_shows_nothing_stale(fake_redis):
    """Fixing a malformed value clears its diagnosis at once: no record of its failed reads lingers."""
    from app.api.v1.schemas.vault_references import attach_vault_failures, vault_status
    from app.services.vault.audit import gather_vault_facts

    _use(FakeVault(secrets={"secret/github": {"token": "ghp_x"}}))
    row = _integration(name="GitHub", provider="github", api_key_enc=encrypt_value("vault://Not Valid"))
    with pytest.raises(SecretUnavailableError):
        await vault.reveal_field(row, "api_key_enc")

    row.api_key_enc = encrypt_value("vault://corp/secret/github#token")

    response = SimpleNamespace(id=row.id, vault_secrets=vault_status(row))
    await attach_vault_failures([response], "integrations")
    assert response.vault_secrets.invalid_references == []
    assert response.vault_secrets.failures == []
    facts = await gather_vault_facts(_AuditDB({"integrations": [row]}))
    assert facts.invalid_references == []
    assert facts.failures == []


async def test_a_provider_open_to_every_writer_at_every_path_is_reported(fake_redis):
    from app.api.v1.security_audit import _check_secret_vaults

    _use(FakeVault("corp"), FakeVault("team", WRITERS="all"), FakeVault("scoped", WRITERS="all", PATHS="xtm"))
    findings: list[Any] = []

    await _check_secret_vaults(_AuditDB(), findings)

    flagged = [finding for finding in findings if finding.id == "encryption.vault_provider_open_writers"]
    assert len(flagged) == 1
    assert "team" in flagged[0].description and "scoped" not in flagged[0].description
    assert "encryption.vault_references_resolving" not in {finding.id for finding in findings}


async def test_an_openrouter_key_held_as_a_reference_is_read_where_used_and_copied_as_written(fake_redis):
    """The console resolves it; feeding the platform LLM provider copies the reference, never the value."""
    from app.api.v1 import openrouter as console
    from app.integrations.providers import openrouter as client
    from app.services import openrouter_llm_provider as llm_link

    _use(FakeVault(secrets={"llm/openrouter": {"key": "sk-or-vault"}}))
    integration = _integration(provider="openrouter", api_key_enc=encrypt_value("vault://corp/llm/openrouter#key"))

    assert await console._api_key(integration) == "sk-or-vault"
    assert llm_link._stored_key(integration) == "vault://corp/llm/openrouter#key"

    fed = SimpleNamespace(id=uuid.uuid4(), api_key_enc=encrypt_value("vault://corp/llm/openrouter#key"))
    fed.__tablename__ = "llm_provider_configs"
    broken = SimpleNamespace(id=uuid.uuid4(), api_key_enc=encrypt_value("vault://corp/llm/gone#key"))
    broken.__tablename__ = "llm_provider_configs"

    class _DB:
        def __init__(self, configs: list[Any]) -> None:
            self.configs = configs

        async def execute(self, _statement):
            return SimpleNamespace(scalars=lambda: iter(self.configs))

    uses = await console._keys_in_use(_DB([fed]), integration)
    assert uses == {client.key_hash("sk-or-vault"): {"integration", "llm_provider"}}
    # A key the vault does not hand over now may still be in use: the console never calls it unused.
    with pytest.raises(SecretUnavailableError):
        await console._keys_in_use(_DB([broken]), integration)

    _use()
    with pytest.raises(llm_link.LinkRefusedError) as refused:
        llm_link._stored_key(integration)
    assert refused.value.status == 422


async def test_secret_variables_are_read_a_few_at_a_time(monkeypatch):
    from app.models.variable import Variable
    from app.services import variable_resolver

    variables = [
        Variable(id=uuid.uuid4(), name=f"v{i}", is_secret=True, value_enc=encrypt_value(f"vault://corp/v/{i}#k"))
        for i in range(30)
    ]
    active = peak = 0

    async def _reveal(var, _column):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return f"value-{var.name}"

    class _DB:
        async def execute(self, _statement):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: variables))

    monkeypatch.setattr(variable_resolver, "reveal_field", _reveal)

    resolved = await variable_resolver.load_variables(_DB(), None)

    assert resolved == {str(var.id): f"value-{var.name}" for var in variables}
    assert 1 < peak <= variable_resolver._MAX_CONCURRENT_SECRET_READS


# ---------------------------------------------------------------------------
# Write validation
# ---------------------------------------------------------------------------


def test_an_integration_write_checks_its_references():
    from app.api.v1.schemas.integration import CreateIntegrationRequest

    _use(FakeVault())
    CreateIntegrationRequest(provider="github", name="gh", api_key="vault://corp/secret/github#token")
    with pytest.raises(ValidationError, match="No vault provider named 'nope'"):
        CreateIntegrationRequest(provider="github", name="gh", api_key="vault://nope/x")
    with pytest.raises(ValidationError, match="path"):
        CreateIntegrationRequest(provider="github", name="gh", client_secret="vault://corp/a b")
    with pytest.raises(ValidationError, match="not accepted"):
        CreateIntegrationRequest(provider="github", name="gh", extra_config={"webhook_secret": "vault://corp/x"})


def test_mcp_headers_and_the_oauth_client_secret_accept_references_the_client_id_does_not():
    from app.api.v1.schemas.mcp_server import MCPServerCreate

    _use(FakeVault())
    MCPServerCreate(name="m", headers={"Authorization": "vault://corp/mcp#auth"}, env={"TOKEN": "plain"})
    with pytest.raises(ValidationError, match="Authorization"):
        MCPServerCreate(name="m", headers={"Authorization": "vault://other/mcp"})
    # The OAuth client secret takes a reference; the client id is an identifier.
    MCPServerCreate(name="m", client_secret="vault://corp/x")
    with pytest.raises(ValidationError, match="not accepted"):
        MCPServerCreate(name="m", client_id="vault://corp/x")


def test_an_llm_key_and_a_channel_bot_token_accept_references():
    from app.api.v1.schemas.channel_bot import CreateChannelBotRequest
    from app.api.v1.schemas.settings import LLMProviderRequest

    _use(FakeVault())
    LLMProviderRequest(provider="openai", api_key="vault://corp/llm#openai")
    CreateChannelBotRequest(provider="slack_bot", name="s", api_key="vault://corp/slack#bot")
    with pytest.raises(ValidationError):
        LLMProviderRequest(provider="openai", api_key="vault://nope/x")


def test_the_builder_tools_apply_the_same_rule():
    _use(FakeVault())
    assert vault.secret_fields_problem({"api_key": "vault://corp/x"}, {"extra_config": {"a": "b"}}) is None
    assert "not accepted" in vault.secret_fields_problem({}, {"extra_config": {"a": "vault://corp/x"}})
    assert vault.secret_fields_problem({"api_key": "vault://zzz/x"}).startswith("api_key:")


async def test_a_route_refuses_a_reference_its_caller_may_not_write_with_a_403():
    from fastapi import HTTPException

    from app.api.v1.schemas.vault_references import require_vault_writer

    _use(FakeVault())
    await require_vault_writer(None, SimpleNamespace(is_admin=True), api_key="vault://corp/x#k")
    await require_vault_writer(None, SimpleNamespace(is_admin=False), api_key="sk-plain", client_secret=None)
    with pytest.raises(HTTPException) as exc:
        await require_vault_writer(None, SimpleNamespace(is_admin=False), headers={"Authorization": "vault://corp/x"})
    assert exc.value.status_code == 403
    assert exc.value.detail.startswith("headers: Authorization: only administrators")


async def test_a_secret_variable_takes_a_reference_its_caller_may_write():
    from fastapi import HTTPException

    from app.api.v1.variables import _check_value

    _use(FakeVault(), FakeVault("team", WRITERS="all", PATHS="vars"))
    await _check_value(None, "vault://corp/x#k", True, SimpleNamespace(is_admin=True))
    await _check_value(None, "vault://team/vars/a#k", True, SimpleNamespace(is_admin=False))
    with pytest.raises(HTTPException) as denied:
        await _check_value(None, "vault://corp/x#k", True, SimpleNamespace(is_admin=False))
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as outside:
        await _check_value(None, "vault://team/other#k", True, SimpleNamespace(is_admin=False))
    assert outside.value.status_code == 422
    with pytest.raises(HTTPException) as plain:
        await _check_value(None, "vault://corp/x#k", False, SimpleNamespace(is_admin=True))
    assert plain.value.status_code == 422


@pytest.mark.parametrize(("is_admin", "refused"), [(True, False), (False, True)])
async def test_the_builder_tools_refuse_a_reference_the_caller_may_not_write(monkeypatch, is_admin, refused):
    from app.services.builtin_tools import agent_builder

    async def _caller(_db, _uid):
        return SimpleNamespace(is_admin=is_admin)

    monkeypatch.setattr(agent_builder, "_load_caller_user_for_builder", _caller)
    _use(FakeVault())
    problem = await agent_builder._vault_writer_refusal(
        "uid", {"api_key": "vault://corp/x#k", "client_secret": None}, object()
    )
    assert (problem is not None) is refused
    if refused:
        assert problem == "api_key: only administrators may store a reference to vault provider 'corp'"
    assert await agent_builder._vault_writer_refusal("uid", {"api_key": "plain"}, object()) is None


def test_a_reference_nested_in_a_document_is_checked_like_any_other():
    _use(FakeVault())
    assert vault.reference_problem({"X-Auth": ["vault://nope/x"]}).startswith("X-Auth: No vault provider named 'nope'")
    assert vault.reference_problem({"X-Auth": [{"inner": "vault://corp/x#k"}]}) is None


# ---------------------------------------------------------------------------
# Retained references: an edit may not re-point what holds one it could not write
# ---------------------------------------------------------------------------

NON_ADMIN = SimpleNamespace(id="99999999-9999-9999-9999-999999999999", is_admin=False)


def _mcp_server(**columns: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "id": "77777777-7777-7777-7777-777777777777",
        "transport": "streamable_http",
        "command": None,
        "args": None,
        "url": "https://mcp.acme.test/mcp",
        "auth_mode": "headers",
        "authorize_url": None,
        "token_url": None,
        "registration_url": None,
        "relay_group_id": None,
        "headers_enc": None,
        "env_enc": None,
    }
    row = SimpleNamespace(**{**defaults, **columns})
    row.__tablename__ = "mcp_servers"
    return row


def _referencing_integration(reference: str = "vault://corp/secret/github#token") -> Any:
    from app.models.integration import Integration

    return Integration(
        id="88888888-8888-8888-8888-888888888888",
        provider="github",
        auth_type="api_key",
        name="GitHub",
        api_key_enc=encrypt_value(reference),
        extra_config_enc=encrypt_value(json.dumps({"base_url": "https://api.github.com", "token": "kept"})),
        token_url="https://github.com/login/oauth/access_token",
        is_company_managed=False,
        is_builtin=False,
    )


def test_a_retained_reference_holds_an_edit_to_its_provider_writers():
    _use(FakeVault(), FakeVault("team", WRITERS="all"))
    row = _integration(api_key_enc=encrypt_value("vault://corp/secret/github#token"), client_secret_enc=None)

    problem = vault.retained_reference_writer_problem(row, principal=USER_PRINCIPAL)

    assert problem is not None and problem.startswith("api_key holds a reference to vault provider 'corp'")
    assert "secret/github" not in problem
    assert vault.retained_reference_writer_problem(row, principal=ADMIN_PRINCIPAL) is None
    assert vault.retained_reference_writer_problem(row, replaced={"api_key_enc"}, principal=USER_PRINCIPAL) is None
    team = _integration(api_key_enc=encrypt_value("vault://team/github#token"), client_secret_enc=None)
    assert vault.retained_reference_writer_problem(team, principal=USER_PRINCIPAL) is None
    nested = _mcp_server(headers_enc=encrypt_value(json.dumps({"X-Auth": {"deep": ["vault://corp/mcp#auth"]}})))
    assert vault.retained_reference_writer_problem(nested, principal=USER_PRINCIPAL).startswith("headers holds")


async def test_a_non_admin_cannot_repoint_an_integration_keeping_an_admins_only_reference(monkeypatch):
    """Rule 19: as a non-admin editor (owner or config grantee), whom admins-only references do not admit."""
    from fastapi import HTTPException

    from app.api.v1 import integrations
    from app.api.v1.schemas.integration import UpdateIntegrationRequest

    _use(FakeVault())
    integ = _referencing_integration()

    async def _group_ids(_db, _uid):
        return []

    async def _load(*_args, **_kwargs):
        return integ

    monkeypatch.setattr(integrations, "get_user_group_ids", _group_ids)
    monkeypatch.setattr(integrations, "get_user_integration", _load)
    monkeypatch.setattr(integrations, "can_manage", lambda *_args, **_kwargs: True)
    before = (integ.api_key_enc, integ.extra_config_enc, integ.token_url)

    for body in (
        UpdateIntegrationRequest(extra_config={"base_url": "https://attacker.test", "token": "********"}),
        UpdateIntegrationRequest(token_url="https://attacker.test/token"),
    ):
        with pytest.raises(HTTPException) as exc:
            await integrations.update_integration(str(integ.id), body, request=None, user=NON_ADMIN, db=None)
        assert exc.value.status_code == 403
        assert "only an administrator may change where this sends it" in exc.value.detail
    assert (integ.api_key_enc, integ.extra_config_enc, integ.token_url) == before


async def test_re_sending_platform_managed_does_not_count_the_secrets_as_replaced(monkeypatch):
    """Only a switch to platform_managed clears the secrets: an edit of one keeps its reference, and is held to the rule."""
    from fastapi import HTTPException

    from app.api.v1 import integrations
    from app.api.v1.schemas.integration import UpdateIntegrationRequest

    _use(FakeVault())
    integ = _referencing_integration()
    integ.auth_type = "platform_managed"

    async def _group_ids(_db, _uid):
        return []

    async def _load(*_args, **_kwargs):
        return integ

    monkeypatch.setattr(integrations, "get_user_group_ids", _group_ids)
    monkeypatch.setattr(integrations, "get_user_integration", _load)
    monkeypatch.setattr(integrations, "can_manage", lambda *_args, **_kwargs: True)
    before = (integ.api_key_enc, integ.extra_config_enc)
    body = UpdateIntegrationRequest(
        auth_type="platform_managed", extra_config={"base_url": "https://attacker.test", "token": "********"}
    )

    with pytest.raises(HTTPException) as exc:
        await integrations.update_integration(str(integ.id), body, request=None, user=NON_ADMIN, db=None)

    assert exc.value.status_code == 403
    assert (integ.api_key_enc, integ.extra_config_enc) == before


async def test_an_integration_edit_that_keeps_the_destination_is_not_a_repoint():
    from app.api.v1.integrations import _update_repoints_integration
    from app.api.v1.schemas.integration import UpdateIntegrationRequest

    integ = _referencing_integration()
    unchanged = [
        UpdateIntegrationRequest(name="Renamed", tags=["a"], requires_approval=False),
        # The form sends the stored config back, its secrets as the mask.
        UpdateIntegrationRequest(extra_config={"base_url": "https://api.github.com", "token": "********"}),
        # The update merges key by key (#9898): a key left out keeps its stored value.
        UpdateIntegrationRequest(extra_config={"base_url": "https://api.github.com"}),
        UpdateIntegrationRequest(token_url="https://github.com/login/oauth/access_token", auth_type="api_key"),
        UpdateIntegrationRequest(api_key="ghp_literal"),
        UpdateIntegrationRequest(relay_group_id=None),
    ]
    for body in unchanged:
        assert await _update_repoints_integration(None, integ, body) is False
    for body in (
        # null removes the stored key: the config changes.
        UpdateIntegrationRequest(extra_config={"base_url": "https://api.github.com", "token": None}),
        UpdateIntegrationRequest(authorize_url="https://attacker.test/authorize"),
        UpdateIntegrationRequest(auth_type="oauth"),
        # Through a relay group, whose relays see the requests and the secrets they carry.
        UpdateIntegrationRequest(relay_group_id=str(uuid.uuid4())),
    ):
        assert await _update_repoints_integration(None, integ, body) is True


async def test_a_non_admin_cannot_repoint_an_mcp_server_keeping_an_admins_only_reference(monkeypatch):
    """Rule 19: as a non-admin editor; the edit form sends its unchanged fields back, which stays allowed."""
    from fastapi import HTTPException

    from app.api.v1 import mcp_servers
    from app.api.v1.schemas.mcp_server import MCPServerUpdate

    _use(FakeVault())
    srv = _mcp_server(headers_enc=encrypt_value(json.dumps({"Authorization": "vault://corp/mcp#auth"})))

    async def _group_ids(_db, _uid):
        return []

    async def _load(*_args, **_kwargs):
        return srv

    monkeypatch.setattr(mcp_servers, "get_user_group_ids", _group_ids)
    monkeypatch.setattr(mcp_servers, "get_manageable_entity", _load)

    for body in (
        MCPServerUpdate(url="https://attacker.test/mcp"),
        MCPServerUpdate(transport="stdio", command="curl", args=["https://attacker.test"]),
        MCPServerUpdate(env={"PLAIN": "x"}, url="https://attacker.test/mcp"),
    ):
        with pytest.raises(HTTPException) as exc:
            await mcp_servers.update_mcp_server(str(srv.id), body, request=None, user=NON_ADMIN, db=None)
        assert exc.value.status_code == 403
    assert srv.url == "https://mcp.acme.test/mcp"

    resend = MCPServerUpdate(
        name="Renamed", transport="streamable_http", url="https://mcp.acme.test/mcp", auth_mode="headers", token_url=" "
    )
    assert mcp_servers._update_repoints_mcp_server(srv, resend) is False
    replacing = MCPServerUpdate(url="https://elsewhere.test/mcp", headers={"Authorization": "Bearer literal"})
    assert mcp_servers._update_repoints_mcp_server(srv, replacing) is True
    assert vault.retained_reference_writer_problem(srv, replaced={"headers_enc"}, principal=USER_PRINCIPAL) is None

    group = uuid.uuid4()
    routed = _mcp_server(relay_group_id=group)
    assert mcp_servers._update_repoints_mcp_server(routed, MCPServerUpdate(relay_group_id=str(group))) is False
    assert mcp_servers._update_repoints_mcp_server(routed, MCPServerUpdate(relay_group_id=None)) is True
    assert mcp_servers._update_repoints_mcp_server(srv, MCPServerUpdate(relay_group_id=str(group))) is True


def test_the_mcp_form_sending_its_masked_arguments_back_is_not_a_repoint():
    """The response masks token-like arguments and the edit form sends them back: unchanged, kept as stored."""
    from app.api.v1 import mcp_servers
    from app.api.v1.schemas.mcp_server import MCPServerUpdate
    from app.core.utils import redact_secret_args, restore_masked_args

    stored = ["-y", "@acme/server", "ghp_StoredArgumentToken1234567890"]
    shown = redact_secret_args(stored)
    assert shown != stored
    srv = _mcp_server(
        transport="stdio", command="npx", args=stored, url=None,
        env_enc=encrypt_value(json.dumps({"TOKEN": "vault://corp/mcp#token"})),
    )

    assert restore_masked_args(shown, stored) == stored
    resend = MCPServerUpdate(name="Renamed", transport="stdio", command="npx", args=shown)
    resend.args = restore_masked_args(resend.args, srv.args)
    assert mcp_servers._update_repoints_mcp_server(srv, resend) is False
    # A mask moved elsewhere keeps its token, but the arguments changed: a re-point.
    moved = restore_masked_args([shown[2], "-y", "@acme/server"], stored)
    assert moved == [stored[2], "-y", "@acme/server"]
    assert mcp_servers._update_repoints_mcp_server(srv, MCPServerUpdate(args=moved)) is True
    changed = MCPServerUpdate(args=["-y", "@acme/server", "--url", "https://attacker.test"])
    assert mcp_servers._update_repoints_mcp_server(srv, changed) is True


@pytest.mark.parametrize(("is_admin", "refused"), [(True, False), (False, True)])
async def test_the_builder_tools_refuse_a_repoint_keeping_an_admins_only_reference(monkeypatch, is_admin, refused):
    from app.services.builtin_tools import agent_builder

    async def _caller(_db, _uid):
        return SimpleNamespace(is_admin=is_admin)

    monkeypatch.setattr(agent_builder, "_load_caller_user_for_builder", _caller)
    _use(FakeVault())
    srv = _mcp_server(env_enc=encrypt_value(json.dumps({"TOKEN": "vault://corp/mcp#token"})))

    problem = await agent_builder._retained_reference_refusal(srv, "uid", object(), repoints=True, replaced=set())

    assert (problem is not None) is refused
    assert await agent_builder._retained_reference_refusal(srv, "uid", object(), repoints=False, replaced=set()) is None
    assert (
        await agent_builder._retained_reference_refusal(srv, "uid", object(), repoints=True, replaced={"env_enc"})
        is None
    )
    assert agent_builder._mcp_args('["--url", 3]') == ["--url", "3"]
    assert agent_builder._mcp_args("a, b") == ["a", "b"]


# ---------------------------------------------------------------------------
# HashiCorp Vault
# ---------------------------------------------------------------------------


def _hashicorp(monkeypatch, handler, **settings: str):
    from app.services.vault.hashicorp import HashiCorpVaultProvider

    config = VaultProviderConfig(
        name="corp", type="hashicorp", settings={"ADDRESS": "https://vault.example.com", **settings}
    )
    provider = HashiCorpVaultProvider(config)
    provider._client = httpx.AsyncClient(base_url=provider.address, transport=httpx.MockTransport(handler))
    return provider


async def test_hashicorp_reads_kv_v2_with_a_token_and_a_namespace(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {"data": {"token": "ghp_x"}, "metadata": {"version": 3}}})

    provider = _hashicorp(monkeypatch, handler, TOKEN="s.root", NAMESPACE="team-a")
    assert await provider.read("secret/github") == {"token": "ghp_x"}
    assert seen[0].url.path == "/v1/secret/data/github"
    assert seen[0].headers["X-Vault-Token"] == "s.root"
    assert seen[0].headers["X-Vault-Namespace"] == "team-a"


async def test_hashicorp_reads_kv_v1_under_a_fixed_mount(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/kv/apps/github"
        return httpx.Response(200, json={"data": {"token": "v1"}})

    provider = _hashicorp(monkeypatch, handler, TOKEN="s.root", KV_VERSION="1", KV_MOUNT="kv")
    assert await provider.read("apps/github") == {"token": "v1"}


async def test_hashicorp_approle_logs_in_once_and_again_on_a_refusal(monkeypatch):
    logins: list[dict] = []
    tokens = iter(["s.first", "s.second"])
    refused_once = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/approle/login":
            logins.append(json.loads(request.content))
            return httpx.Response(200, json={"auth": {"client_token": next(tokens), "lease_duration": 3600}})
        if request.headers["X-Vault-Token"] == "s.first" and not refused_once:
            refused_once.append(True)
            return httpx.Response(403, json={"errors": ["permission denied"]})
        return httpx.Response(200, json={"data": {"data": {"k": request.headers["X-Vault-Token"]}}})

    provider = _hashicorp(monkeypatch, handler, AUTH_METHOD="approle", ROLE_ID="role-1", SECRET_ID="sid")
    assert await provider.read("secret/a") == {"k": "s.second"}
    assert await provider.read("secret/b") == {"k": "s.second"}
    assert logins == [{"role_id": "role-1", "secret_id": "sid"}] * 2


async def test_hashicorp_reads_refused_together_share_one_new_login(monkeypatch):
    """After a revocation, the reads refused at the same time log in again once, not once each."""
    logins: list[str] = []
    refused: list[str] = []
    all_refused = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/approle/login":
            logins.append(token := f"s.{len(logins) + 1}")
            return httpx.Response(200, json={"auth": {"client_token": token, "lease_duration": 3600}})
        if request.headers["X-Vault-Token"] == "s.1":
            refused.append(request.url.path)
            if len(refused) == 3:
                all_refused.set()
            await all_refused.wait()
            return httpx.Response(403, json={"errors": ["permission denied"]})
        return httpx.Response(200, json={"data": {"data": {"k": request.headers["X-Vault-Token"]}}})

    provider = _hashicorp(monkeypatch, handler, AUTH_METHOD="approle", ROLE_ID="role-1", SECRET_ID="sid")
    reads = asyncio.gather(*(provider.read(f"secret/{name}") for name in ("a", "b", "c")))

    assert await asyncio.wait_for(reads, timeout=10) == [{"k": "s.2"}] * 3
    assert logins == ["s.1", "s.2"]


async def test_hashicorp_a_failed_login_is_not_tried_again_by_every_waiting_read(monkeypatch):
    """A vault that does not answer: the reads queued on the login fail at once, not one timeout each."""
    logins: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/approle/login":
            logins.append(request.url.path)
            raise httpx.ConnectTimeout("timed out", request=request)
        return httpx.Response(200, json={"data": {"data": {"k": "v"}}})

    provider = _hashicorp(monkeypatch, handler, AUTH_METHOD="approle", ROLE_ID="role-1", SECRET_ID="sid")
    reads = await asyncio.gather(
        *(provider.read(f"secret/{name}") for name in ("a", "b", "c")), return_exceptions=True
    )

    assert [type(error) for error in reads] == [VaultReadError] * 3
    assert {error.reason for error in reads} == {"unreachable"}
    assert logins == ["/v1/auth/approle/login"]


async def test_hashicorp_a_path_the_policy_denies_does_not_log_in_on_every_read(monkeypatch):
    """Vault answers 403 for a denied path too: one new login per retry window, not one per read."""
    logins: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/approle/login":
            logins.append(token := f"s.{len(logins) + 1}")
            return httpx.Response(200, json={"auth": {"client_token": token, "lease_duration": 3600}})
        if request.url.path.startswith("/v1/secret/data/denied"):
            return httpx.Response(403, json={"errors": ["permission denied"]})
        return httpx.Response(200, json={"data": {"data": {"k": request.headers["X-Vault-Token"]}}})

    provider = _hashicorp(monkeypatch, handler, AUTH_METHOD="approle", ROLE_ID="role-1", SECRET_ID="sid")
    for name in ("one", "two", "three"):
        with pytest.raises(VaultReadError) as denied:
            await provider.read(f"secret/denied-{name}")
        assert denied.value.reason == "access_denied"

    assert logins == ["s.1", "s.2"]
    assert await provider.read("secret/allowed") == {"k": "s.2"}


async def test_hashicorp_kubernetes_auth_sends_the_service_account_token(monkeypatch, tmp_path):
    jwt_file = tmp_path / "token"
    jwt_file.write_text("eyJhbGciOi.k8s.jwt\n", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/k8s/login":
            assert json.loads(request.content) == {"role": "xtm-one", "jwt": "eyJhbGciOi.k8s.jwt"}
            return httpx.Response(200, json={"auth": {"client_token": "s.k8s", "lease_duration": 600}})
        return httpx.Response(200, json={"data": {"data": {"k": "v"}}})

    provider = _hashicorp(
        monkeypatch, handler, AUTH_METHOD="kubernetes", ROLE="xtm-one", AUTH_MOUNT="k8s", JWT_FILE=str(jwt_file)
    )
    assert await provider.read("secret/x") == {"k": "v"}


@pytest.mark.parametrize(("status", "reason"), [(404, "not_found"), (403, "access_denied"), (503, "unreachable")])
async def test_hashicorp_failures_carry_their_reason(monkeypatch, status, reason):
    provider = _hashicorp(monkeypatch, lambda request: httpx.Response(status), TOKEN="s.root")
    with pytest.raises(VaultReadError) as exc:
        await provider.read("secret/x")
    assert exc.value.reason == reason


def test_hashicorp_refuses_an_incomplete_declaration():
    from app.services.vault.config import VaultConfigError
    from app.services.vault.hashicorp import HashiCorpVaultProvider

    for settings in (
        {"ADDRESS": "vault:8200"},
        {"ADDRESS": "https://v", "AUTH_METHOD": "approle"},
        {"ADDRESS": "https://v", "KV_VERSION": "3"},
    ):
        with pytest.raises(VaultConfigError):
            HashiCorpVaultProvider(VaultProviderConfig(name="corp", type="hashicorp", settings=settings))


@pytest.mark.parametrize(
    "settings",
    [
        {"KV_VERSION": "1"},
        {"KV_VERSION": "1", "KV_MOUNT": "auth"},
        {"KV_MOUNT": "/sys/"},
        {"KV_MOUNT": "Identity/x"},
        {"KV_MOUNT": "kv/../auth"},
    ],
)
def test_hashicorp_refuses_a_kv_mount_that_is_not_one(settings):
    """KV v1 inserts nothing in the API path, so without a fixed mount a path could name any Vault endpoint."""
    from app.services.vault.config import VaultConfigError
    from app.services.vault.hashicorp import HashiCorpVaultProvider

    with pytest.raises(VaultConfigError, match="KV_MOUNT"):
        HashiCorpVaultProvider(
            VaultProviderConfig(
                name="corp", type="hashicorp", settings={"ADDRESS": "https://v", "TOKEN": "s.root", **settings}
            )
        )


@pytest.mark.parametrize(
    "path", ["auth/token/lookup-self", "sys/wrapping/lookup", "Identity/entity/name/x", "cubbyhole/x", "secret"]
)
async def test_hashicorp_never_reads_outside_a_kv_mount(monkeypatch, path):
    """``auth/token/lookup-self`` answers with the platform's own Vault token: refused on write and on read."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": {"id": "s.root"}})

    resolver = _use(_hashicorp(monkeypatch, handler, TOKEN="s.root"))
    reference = f"vault://corp/{path}#id"

    assert vault.reference_problem(reference)
    with pytest.raises(VaultReadError) as exc:
        await resolver.read(parse_secret_reference(reference))
    assert exc.value.reason == "invalid_reference"
    assert calls == []


async def test_hashicorp_reads_any_path_under_a_fixed_mount(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/kv/data/sys/github"
        return httpx.Response(200, json={"data": {"data": {"token": "fixed"}}})

    resolver = _use(_hashicorp(monkeypatch, handler, TOKEN="s.root", KV_MOUNT="kv"))
    assert vault.reference_problem("vault://corp/sys/github#token") is None
    assert await resolver.read(parse_secret_reference("vault://corp/sys/github#token")) == "fixed"


@pytest.mark.parametrize(
    ("kind", "setting", "base"),
    [
        ("hashicorp", "ADDRESS", {"TOKEN": "s.root"}),
        ("aws", "ENDPOINT_URL", {}),
        ("azure", "VAULT_URL", {}),
    ],
)
@pytest.mark.parametrize(
    ("address", "problem"),
    [
        ("https://user:hunter2@vault.example.com", "credentials"),
        ("https://vault.example.com/?token=hunter2", "query or a fragment"),
        ("https://vault.example.com/#hunter2", "query or a fragment"),
        ("https://vault.example.com:hunter2", "invalid port"),
        ("https://[hunter2", "must be"),
    ],
)
def test_a_vault_address_carries_no_credential(kind, setting, base, address, problem):
    """A provider's address is shown to admins and in the Security Audit, so it never holds a secret."""
    from app.services.vault.aws import AWSSecretsManagerProvider
    from app.services.vault.azure import AzureKeyVaultProvider
    from app.services.vault.config import VaultConfigError
    from app.services.vault.hashicorp import HashiCorpVaultProvider

    provider_class = {
        "hashicorp": HashiCorpVaultProvider,
        "aws": AWSSecretsManagerProvider,
        "azure": AzureKeyVaultProvider,
    }[kind]
    with pytest.raises(VaultConfigError, match=problem) as exc:
        provider_class(VaultProviderConfig(name="corp", type=kind, settings={**base, setting: address}))
    assert "hunter2" not in str(exc.value)


async def test_an_integration_keeps_working_after_a_rotation_in_hashicorp(monkeypatch, fake_redis):
    """Acceptance: a HashiCorp-referenced API key works, then a rotated one is used without restart."""
    from app.models.integration import Integration
    from app.services import integration_tools

    stored = {"token": "ghp_v1"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"data": dict(stored)}})

    provider = _hashicorp(monkeypatch, handler, TOKEN="s.root")
    resolver = _use(provider)
    integ = Integration(
        id="33333333-3333-3333-3333-333333333333",
        name="GitHub",
        provider="github",
        auth_type="api_key",
        api_key_enc=encrypt_value("vault://corp/secret/github#token"),
    )

    async def _token() -> str | None:
        return await integration_tools._get_token(None, str(integ.id), "github", integ=integ)

    assert await _token() == "ghp_v1"
    stored["token"] = "ghp_v2"
    await vault.invalidate_entity_secrets(integ, vault.REFERENCE_COLUMNS["integrations"])
    assert await _token() == "ghp_v2"
    assert resolver.providers["corp"] is provider
    assert decrypt_stored_value(integ.api_key_enc) == "vault://corp/secret/github#token"


# ---------------------------------------------------------------------------
# AWS Secrets Manager, Google Secret Manager, Azure Key Vault
# ---------------------------------------------------------------------------


async def test_aws_reads_a_secret_string_with_the_environment_role(monkeypatch):
    from botocore.exceptions import ClientError

    from app.services.vault.aws import AWSSecretsManagerProvider

    class Client:
        def get_secret_value(self, SecretId):  # noqa: N803 - the SDK's keyword
            if SecretId == "prod/github":
                return {"SecretString": json.dumps({"token": "ghp_aws"})}
            raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetSecretValue")

    provider = AWSSecretsManagerProvider(VaultProviderConfig(name="aws", type="aws", settings={"REGION": "eu-west-1"}))
    monkeypatch.setattr(provider, "_build_client", lambda: Client())
    resolver = _use(provider)
    assert await resolver.read(parse_secret_reference("vault://aws/prod/github#token")) == "ghp_aws"
    with pytest.raises(VaultReadError) as exc:
        await provider.read("prod/missing")
    assert exc.value.reason == "not_found"


async def test_aws_maps_an_access_refusal(monkeypatch):
    from botocore.exceptions import ClientError

    from app.services.vault.aws import AWSSecretsManagerProvider

    class Client:
        def get_secret_value(self, SecretId):  # noqa: N803
            raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetSecretValue")

    provider = AWSSecretsManagerProvider(VaultProviderConfig(name="aws", type="aws"))
    monkeypatch.setattr(provider, "_build_client", lambda: Client())
    with pytest.raises(VaultReadError) as exc:
        await provider.read("prod/github")
    assert exc.value.reason == "access_denied"


async def test_gcp_reads_the_latest_version_with_the_workload_identity(monkeypatch):
    from app.services.vault.gcp import GoogleSecretManagerProvider

    provider = GoogleSecretManagerProvider(VaultProviderConfig(name="gcp", type="gcp", settings={"PROJECT": "p1"}))
    monkeypatch.setattr(provider, "_refresh_credentials", lambda: "ya29.token")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/projects/p1/secrets/github/versions/latest:access"
        assert request.headers["Authorization"] == "Bearer ya29.token"
        return httpx.Response(200, json={"payload": {"data": base64.b64encode(b"ghp_gcp").decode()}})

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await provider.read("github") == "ghp_gcp"
    assert provider.resource_name("projects/p2/secrets/s/versions/4") == "projects/p2/secrets/s/versions/4"


def test_gcp_a_secret_named_projects_keeps_the_short_forms():
    from app.services.vault.gcp import GoogleSecretManagerProvider

    provider = GoogleSecretManagerProvider(VaultProviderConfig(name="gcp", type="gcp", settings={"PROJECT": "p1"}))
    assert provider.resource_name("projects") == "projects/p1/secrets/projects/versions/latest"
    assert provider.resource_name("projects/3") == "projects/p1/secrets/projects/versions/3"
    assert provider.resource_name("projects/p2/secrets/s") == "projects/p2/secrets/s/versions/latest"
    with pytest.raises(VaultReadError) as refused:
        provider.resource_name("projects/p2/secrets")
    assert refused.value.reason == "invalid_reference"


async def test_azure_reads_a_secret_with_the_managed_identity(monkeypatch):
    from app.services.vault.azure import AzureKeyVaultProvider

    for variable in ("AZURE_FEDERATED_TOKEN_FILE", "IDENTITY_ENDPOINT", "IDENTITY_HEADER"):
        monkeypatch.delenv(variable, raising=False)
    provider = AzureKeyVaultProvider(
        VaultProviderConfig(name="azure", type="azure", settings={"VAULT_URL": "https://kv1.vault.azure.net"})
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "169.254.169.254":
            assert request.headers["Metadata"] == "true"
            return httpx.Response(200, json={"access_token": "mi-token", "expires_in": "3600"})
        assert request.url.path == "/secrets/github"
        assert request.headers["Authorization"] == "Bearer mi-token"
        return httpx.Response(200, json={"value": "ghp_azure"})

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await provider.read("github") == "ghp_azure"


async def test_azure_reads_refused_together_share_one_new_token(monkeypatch):
    """Reads refused with the same expired token ask the identity for one new token, not one each."""
    from app.services.vault.azure import AzureKeyVaultProvider

    for variable in ("AZURE_FEDERATED_TOKEN_FILE", "IDENTITY_ENDPOINT", "IDENTITY_HEADER"):
        monkeypatch.delenv(variable, raising=False)
    provider = AzureKeyVaultProvider(
        VaultProviderConfig(name="azure", type="azure", settings={"VAULT_URL": "https://kv1.vault.azure.net"})
    )
    issued: list[str] = []
    refused: list[str] = []
    all_refused = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "169.254.169.254":
            issued.append(token := f"mi-{len(issued) + 1}")
            return httpx.Response(200, json={"access_token": token, "expires_in": "3600"})
        if request.headers["Authorization"] == "Bearer mi-1":
            refused.append(request.url.path)
            if len(refused) == 3:
                all_refused.set()
            await all_refused.wait()
            return httpx.Response(401)
        return httpx.Response(200, json={"value": request.headers["Authorization"]})

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reads = asyncio.gather(*(provider.read(name) for name in ("a", "b", "c")))

    assert await asyncio.wait_for(reads, timeout=10) == ["Bearer mi-2"] * 3
    assert issued == ["mi-1", "mi-2"]


async def test_gcp_refuses_a_payload_that_is_not_strict_base64(monkeypatch):
    from app.services.vault.gcp import GoogleSecretManagerProvider

    provider = GoogleSecretManagerProvider(VaultProviderConfig(name="gcp", type="gcp", settings={"PROJECT": "p1"}))
    monkeypatch.setattr(provider, "_refresh_credentials", lambda: "ya29.token")
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"payload": {"data": "!!!!"}}))
    )
    with pytest.raises(VaultReadError) as exc:
        await provider.read("github")
    assert exc.value.reason == "invalid_response"


async def test_listing_a_provider_models_answers_a_vault_failure_as_the_generic_502(fake_redis):
    """Not rewritten to a 400 by the route's own error handling: main.py's handler answers 502."""
    from app.api.v1.settings import list_provider_models
    from app.models.llm_config import LLMProviderConfig

    _use(FakeVault())
    config = LLMProviderConfig(
        id=uuid.uuid4(), provider="openai", api_key_enc=encrypt_value("vault://corp/llm/openai#key")
    )

    class _DB:
        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: config)

    with pytest.raises(SecretUnavailableError):
        await list_provider_models(str(config.id), user=SimpleNamespace(id=uuid.uuid4(), email="a@b.c"), db=_DB())


def test_gcp_credential_refresh_is_bounded_by_the_vault_request_timeout():
    from app.services.vault.base import REQUEST_TIMEOUT_SECONDS
    from app.services.vault.gcp import _BoundedRequest

    seen: list[Any] = []

    def _request(url, method="GET", body=None, headers=None, timeout=120, **kwargs):
        seen.append(timeout)
        return "ok"

    bounded = _BoundedRequest(_request)
    assert bounded("https://oauth2.googleapis.com/token", method="POST") == "ok"
    bounded("http://metadata.google.internal", timeout=3)
    bounded("http://metadata.google.internal", timeout=300)
    assert seen == [REQUEST_TIMEOUT_SECONDS, 3, REQUEST_TIMEOUT_SECONDS]


async def test_gcp_maps_a_transport_failure_of_the_credential_refresh(monkeypatch):
    import google.auth
    from google.auth.exceptions import TransportError

    from app.services.vault.gcp import GoogleSecretManagerProvider

    class Credentials:
        valid = False
        token = None

        def refresh(self, request):
            raise TransportError("metadata server unreachable")

    monkeypatch.setattr(google.auth, "default", lambda scopes: (Credentials(), "p1"))
    provider = GoogleSecretManagerProvider(VaultProviderConfig(name="gcp", type="gcp", settings={"PROJECT": "p1"}))
    with pytest.raises(VaultReadError) as exc:
        await provider.read("github")
    assert exc.value.reason == "unreachable"


async def test_hashicorp_reports_an_unusable_ca_bundle_as_misconfigured(tmp_path):
    from app.services.vault.hashicorp import HashiCorpVaultProvider

    not_a_bundle = tmp_path / "ca.pem"
    not_a_bundle.write_text("not a certificate", encoding="utf-8")
    for ca_file in (str(tmp_path / "missing.pem"), str(not_a_bundle)):
        provider = HashiCorpVaultProvider(
            VaultProviderConfig(
                name="corp",
                type="hashicorp",
                settings={"ADDRESS": "https://vault.example.com", "TOKEN": "s.root", "CA_CERT_FILE": ca_file},
            )
        )
        with pytest.raises(VaultReadError) as exc:
            await provider.read("secret/github")
        assert exc.value.reason == "misconfigured"
        assert "SECRET_VAULT_CORP_CA_CERT_FILE" in exc.value.detail


async def test_an_unexpected_provider_error_is_a_classified_failure(fake_redis, caplog):
    class Broken(FakeVault):
        async def read(self, path: str) -> Any:
            raise RuntimeError("detail that must stay out of every surface")

    _use(Broken())
    row = _integration(api_key_enc=encrypt_value("vault://corp/secret/github#token"))
    with caplog.at_level(logging.WARNING), pytest.raises(SecretUnavailableError) as exc:
        await vault.reveal_field(row, "api_key_enc")
    assert exc.value.reason == "unreachable"
    assert str(exc.value) == VAULT_UNAVAILABLE_DETAIL
    assert [entry["reason"] for entry in await failures.entity_failures("integration", row.id)] == ["unreachable"]
    assert "must stay out" not in caplog.text
