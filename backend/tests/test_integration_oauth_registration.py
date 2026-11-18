"""OAuth clients an integration registers for itself (#160, Wordsmith slice 2).

A catalog entry with ``oauth_registration`` (Wordsmith) gets its OAuth
client by RFC 7591 dynamic client registration, and its authorize request
and code exchange carry PKCE S256 and the RFC 8707 ``resource``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG, supported_auth_types
from app.core.security import decrypt_value, encrypt_value
from app.integrations import oauth as oauth_module
from app.services import integration_oauth_registration as registration_module
from app.services.mcp_oauth_discovery import DynamicClient, MCPOAuthDiscoveryError


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)


class _FakeDb:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


def _integration(**overrides):
    fields = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        provider="wordsmith",
        auth_type="oauth",
        client_id_enc=None,
        client_secret_enc=None,
        scopes=PROVIDER_CATALOG["wordsmith"]["scopes"],
        authorize_url=PROVIDER_CATALOG["wordsmith"]["authorize_url"],
        token_url=PROVIDER_CATALOG["wordsmith"]["token_url"],
        extra_config_enc=None,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.fixture
def redis(monkeypatch):
    fake = _FakeRedis()

    async def _redis():
        return fake

    monkeypatch.setattr(registration_module, "redis_client", _redis)
    monkeypatch.setattr(oauth_module, "redis_client", _redis)

    async def _branding(_db):
        return SimpleNamespace(name="XTM One")

    import app.services.branding as branding

    monkeypatch.setattr(branding, "get_branding", _branding)
    return fake


def test_wordsmith_offers_api_key_and_oauth_with_self_registration():
    entry = PROVIDER_CATALOG["wordsmith"]
    assert supported_auth_types(entry) == ["api_key", "oauth"]
    assert entry["auth_type"] == "api_key"
    reg = registration_module.oauth_registration("wordsmith")
    assert reg == {
        "registration_url": "https://api.wordsmith.ai/oauth/register",
        "resource": "https://api.wordsmith.ai/api/v1",
    }
    scopes = set(entry["scopes"].split())
    # The trigger needs webhooks:write; the admin scopes are never asked.
    assert "webhooks:write" in scopes and "sessions:write" in scopes and "tasks:write" in scopes
    assert not scopes & {"directory:admin", "audit:read", "webhooks:admin", "mcp:access"}


def test_a_provider_without_oauth_registration_has_none():
    assert registration_module.oauth_registration("github") is None
    assert registration_module.oauth_registration(None) is None
    assert registration_module.oauth_resource("github") == ""


@pytest.mark.asyncio
async def test_register_stores_a_public_client(monkeypatch, redis):
    seen = {}

    async def _register(url, *, client_name, redirect_uri, scopes=None, http=None):
        seen.update(url=url, client_name=client_name, redirect_uri=redirect_uri, scopes=scopes)
        return DynamicClient(client_id="ws-client-1")

    monkeypatch.setattr(registration_module, "register_dynamic_client", _register)
    db = _FakeDb()
    integration = _integration()

    assert await registration_module.register_integration_client(db, integration) is True

    assert decrypt_value(integration.client_id_enc) == "ws-client-1"
    assert integration.client_secret_enc is None
    assert seen["url"] == "https://api.wordsmith.ai/oauth/register"
    assert seen["client_name"] == "XTM One"
    assert seen["redirect_uri"].endswith("/api/oauth/callback")
    assert "webhooks:write" in seen["scopes"]
    assert db.commits == 1
    assert redis.store == {}  # the lock is released


@pytest.mark.asyncio
async def test_register_keeps_a_secret_the_server_insists_on(monkeypatch, redis):
    async def _register(*_a, **_k):
        return DynamicClient(client_id="ws-client-2", client_secret="ws-secret")

    monkeypatch.setattr(registration_module, "register_dynamic_client", _register)
    integration = _integration()
    await registration_module.register_integration_client(_FakeDb(), integration, commit=False)
    assert decrypt_value(integration.client_secret_enc) == "ws-secret"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"auth_type": "api_key"},
        {"client_id_enc": encrypt_value("already-there")},
        {"provider": "github"},
    ],
)
async def test_nothing_to_register(monkeypatch, redis, overrides):
    async def _register(*_a, **_k):  # pragma: no cover - must not be called
        raise AssertionError("registered")

    monkeypatch.setattr(registration_module, "register_dynamic_client", _register)
    assert await registration_module.register_integration_client(_FakeDb(), _integration(**overrides)) is False


@pytest.mark.asyncio
async def test_a_refused_registration_names_the_allowlist(monkeypatch, redis):
    async def _register(*_a, **_k):
        raise MCPOAuthDiscoveryError(
            "The OAuth registration endpoint refused the client (400): redirect_uri not allowed."
        )

    monkeypatch.setattr(registration_module, "register_dynamic_client", _register)
    integration = _integration()
    with pytest.raises(registration_module.IntegrationOAuthRegistrationError) as err:
        await registration_module.register_integration_client(_FakeDb(), integration)
    message = str(err.value)
    assert "allowlist" in message and "/api/oauth/callback" in message and "API key" in message
    assert integration.client_id_enc is None
    assert redis.store == {}


@pytest.mark.asyncio
async def test_a_registration_in_flight_is_refused(monkeypatch, redis):
    integration = _integration()
    redis.store[f"integration_dcr_register:{integration.id}"] = "1"
    with pytest.raises(registration_module.IntegrationOAuthRegistrationError):
        await registration_module.register_integration_client(_FakeDb(), integration)


@pytest.mark.asyncio
async def test_authorize_url_carries_pkce_s256_and_the_resource(monkeypatch, redis):
    integration = _integration(client_id_enc=encrypt_value("ws-client-1"))
    url = await oauth_module.build_authorize_url_for_integration(integration, str(uuid.uuid4()))

    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://api.wordsmith.ai/oauth/authorize"
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert query["response_type"] == "code"
    assert query["client_id"] == "ws-client-1"
    assert query["code_challenge_method"] == "S256"
    assert query["code_challenge"]
    assert query["resource"] == "https://api.wordsmith.ai/api/v1"
    assert "webhooks:write" in query["scope"]
    # The verifier waits in the state for the callback's code exchange.
    import json

    state = json.loads(redis.store[f"oauth_state:{query['state']}"])
    assert state["code_verifier"]


@pytest.mark.asyncio
async def test_authorize_url_of_another_provider_has_no_resource(monkeypatch, redis):
    integration = _integration(
        provider="github",
        client_id_enc=encrypt_value("gh"),
        authorize_url="https://github.com/login/oauth/authorize",
        scopes="repo",
    )
    url = await oauth_module.build_authorize_url_for_integration(integration, str(uuid.uuid4()))
    query = parse_qs(urlparse(url).query)
    assert "resource" not in query and "code_challenge" not in query


@pytest.mark.asyncio
async def test_code_exchange_sends_verifier_and_resource_as_a_public_client(monkeypatch):
    sent = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

        async def post(self, url, **kwargs):
            sent.update(url=url, **kwargs)
            return _Response()

    async def _allowed(_url):
        return None

    async def _no_secret(*_a, **_k):
        return None

    import app.services.vault as vault

    monkeypatch.setattr(oauth_module, "egress_client", lambda **_k: _Client())
    monkeypatch.setattr(oauth_module, "assert_token_endpoint_allowed", _allowed)
    monkeypatch.setattr(vault, "reveal_field", _no_secret)

    integration = _integration(client_id_enc=encrypt_value("ws-client-1"))
    tokens = await oauth_module.exchange_code_for_tokens_with_integration(
        integration, "the-code", code_verifier="the-verifier"
    )

    assert tokens["access_token"] == "at"
    assert sent["url"] == "https://api.wordsmith.ai/oauth/token"
    body = sent["data"]
    assert body["grant_type"] == "authorization_code"
    assert body["code_verifier"] == "the-verifier"
    assert body["resource"] == "https://api.wordsmith.ai/api/v1"
    assert body["client_id"] == "ws-client-1"
    assert "client_secret" not in body
