"""OpenAPI integrations that sign in: password grant, device code, implicit, OpenID Connect, mutual TLS."""

from __future__ import annotations

import base64
import io
import json
import socket
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import oauth_callbacks
from app.core.config import settings
from app.core.security import decrypt_json, decrypt_value, encrypt_value
from app.integrations import client_certificate, oauth, oauth_device
from app.integrations.client_certificate import client_ssl_context
from app.services.integration_tools import BindingBasis, ToolBindings, build_integration_tools
from app.tools.base import ToolCall
from tests import test_openapi_integration_api as _harness
from tests.client_certificates import make_client_certificate

# The OpenAPI integration API harness (in-memory database, fake DNS, mocked HTTP).
api = _harness.api
_mock_api = _harness._mock_api

FIXTURES = Path(__file__).parent / "openapi_fixtures"
AUTH_FLOWS = (FIXTURES / "auth_flows_v32.yaml").read_text()
TOKEN_URL = "https://login.acme.test/token"
DEVICE_URL = "https://login.acme.test/device"


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0

    async def getdel(self, key):
        return self.store.pop(key, None)

    async def ttl(self, key):
        return 600 if key in self.store else -2


@pytest.fixture
def redis(monkeypatch):
    fake = _FakeRedis()

    async def client():
        return fake

    monkeypatch.setattr(oauth, "redis_client", client)
    monkeypatch.setattr(oauth_device, "redis_client", client)
    return fake


@pytest.fixture(autouse=True)
def _fresh_certificates():
    client_certificate.clear_context_cache()
    yield
    client_certificate.clear_context_cache()


def _create(api, option: str, **overrides: Any):
    body: dict[str, Any] = {
        "provider": "openapi",
        "name": "Acme accounts",
        "openapi": {"content": AUTH_FLOWS, "filename": "accounts.yaml", "auth_option": option},
    }
    body.update(overrides)
    return api.client.post("/api/v1/integrations", json=body)


def _created(api, option: str, **overrides: Any) -> dict:
    resp = _create(api, option, **overrides)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


# ── Password grant ───────────────────────────────────────────────────────────


async def test_password_grant_integration(api, monkeypatch):
    created = _created(api, "oauth:password", api_key="alice:pa:ss", client_id="cli")
    integration = api.db.integrations[created["id"]]
    assert (created["auth_type"], created["status"]) == ("oauth_password", "connected")
    assert integration.token_url == TOKEN_URL
    assert integration.scopes == "accounts:read"
    assert decrypt_value(integration.api_key_enc) == "alice:pa:ss"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={"access_token": "alice-token", "expires_in": 600})
        return httpx.Response(200, json={"name": "Alice"})

    seen = _mock_api(monkeypatch, handler)
    _defs, executor = await build_integration_tools(api.db, ToolBindings({"openapi": created["id"]}, basis=BindingBasis.CALLER_CHECKED, reason="test"))
    result = await executor.execute_tool(ToolCall(id="1", name="acme_accounts_get_me", arguments={}))
    assert not result.is_error, result.content
    token_request, api_request = seen
    assert _form(token_request) == {
        "grant_type": "password",
        "username": "alice",
        "password": "pa:ss",
        "scope": "accounts:read",
        "client_id": "cli",
    }
    assert api_request.headers["Authorization"] == "Bearer alice-token"


def test_password_grant_needs_the_user_login(api):
    created = _created(api, "oauth:password", client_id="cli")
    assert created["status"] == "disconnected"
    resp = api.client.put(f"/api/v1/integrations/{created['id']}", json={"api_key": "bob:pw"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "connected"


# ── Device authorization ─────────────────────────────────────────────────────


async def test_device_code_sign_in(api, monkeypatch, redis):
    created = _created(api, "oauth:device", client_id="tv-app")
    integration = api.db.integrations[created["id"]]
    assert (created["auth_type"], created["status"]) == ("oauth", "configured")
    assert (integration.authorize_url, integration.token_url) == (DEVICE_URL, TOKEN_URL)

    answers = iter(
        [
            httpx.Response(400, json={"error": "authorization_pending"}),
            httpx.Response(400, json={"error": "slow_down"}),
            httpx.Response(200, json={"access_token": "tv-token", "refresh_token": "tv-refresh", "expires_in": 3600}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == DEVICE_URL:
            return httpx.Response(
                200,
                json={
                    "device_code": "dev-123",
                    "user_code": "WDJB-MJHT",
                    "verification_uri": "https://login.acme.test/activate",
                    "verification_uri_complete": "https://login.acme.test/activate?code=WDJB-MJHT",
                    "expires_in": 900,
                    "interval": 5,
                },
            )
        if str(request.url) == TOKEN_URL:
            return next(answers)
        return httpx.Response(200, json={"name": "TV"})

    seen = _mock_api(monkeypatch, handler)
    clock = [1_000.0]
    monkeypatch.setattr(oauth_device.time, "time", lambda: clock[0])

    resp = api.client.get(f"/api/v1/integrations/{created['id']}/authorize")
    assert resp.status_code == 200, resp.text
    device = resp.json()["device_authorization"]
    assert (device["user_code"], device["interval"], device["expires_in"]) == ("WDJB-MJHT", 5, 900)
    assert device["verification_uri_complete"].endswith("code=WDJB-MJHT")
    assert _form(seen[0]) == {"scope": "accounts:read", "client_id": "tv-app"}
    # The device code never leaves the server.
    assert "dev-123" not in resp.text

    def poll() -> dict:
        r = api.client.post(
            f"/api/v1/integrations/{created['id']}/openapi/device/poll", json={"flow_id": device["flow_id"]}
        )
        assert r.status_code == 200, r.text
        return r.json()

    assert poll()["status"] == "pending"  # too early: the API is not asked
    assert len(seen) == 1
    clock[0] += 5
    assert poll() == {"status": "pending", "interval": 5, "message": None}
    clock[0] += 5
    assert poll()["interval"] == 10  # slow_down
    clock[0] += 10
    assert poll()["status"] == "connected"
    token_request = seen[-1]
    assert _form(token_request) == {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": "dev-123",
        "client_id": "tv-app",
    }
    assert integration.status == "connected"
    assert decrypt_value(integration.access_token_enc) == "tv-token"
    assert decrypt_value(integration.refresh_token_enc) == "tv-refresh"
    assert redis.store == {}

    _defs, executor = await build_integration_tools(api.db, ToolBindings({"openapi": created["id"]}, basis=BindingBasis.CALLER_CHECKED, reason="test"))
    result = await executor.execute_tool(ToolCall(id="1", name="acme_accounts_get_me", arguments={}))
    assert not result.is_error, result.content
    assert seen[-1].headers["Authorization"] == "Bearer tv-token"


def test_switching_the_sign_in_option_moves_its_endpoints(api, monkeypatch, redis):
    """The device flow must start at the device endpoint, not the old authorize URL."""
    created = _created(api, "oauth:implicit", client_id="tv-app")
    integration = api.db.integrations[created["id"]]
    assert (integration.authorize_url, integration.token_url) == ("https://login.acme.test/authorize", None)

    def replace(option: str) -> None:
        resp = api.client.put(
            f"/api/v1/integrations/{created['id']}/openapi",
            json={"content": AUTH_FLOWS, "filename": "accounts.yaml", "auth_option": option},
        )
        assert resp.status_code == 200, resp.text

    replace("oauth:device")
    assert (integration.authorize_url, integration.token_url) == (DEVICE_URL, TOKEN_URL)
    seen = _mock_api(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            json={"device_code": "d", "user_code": "U", "verification_uri": "https://login.acme.test/activate"},
        ),
    )
    assert "device_authorization" in api.client.get(f"/api/v1/integrations/{created['id']}/authorize").json()
    assert str(seen[0].url) == DEVICE_URL

    # What the spec leaves as it was keeps its edit; what it changes follows it.
    resp = api.client.put(f"/api/v1/integrations/{created['id']}", json={"scopes": "accounts:read offline"})
    assert resp.status_code == 200, resp.text
    replace("oauth:device")
    assert integration.scopes == "accounts:read offline"
    replace("oauth:implicit")
    assert (integration.authorize_url, integration.token_url) == ("https://login.acme.test/authorize", None)
    assert integration.scopes == "accounts:read offline"


def test_a_device_flow_belongs_to_whoever_started_it(api, monkeypatch, redis):
    created = _created(api, "oauth:device", client_id="tv-app")
    _mock_api(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            json={"device_code": "d", "user_code": "U", "verification_uri": "https://login.acme.test/a"},
        ),
    )
    flow = api.client.get(f"/api/v1/integrations/{created['id']}/authorize").json()["device_authorization"]
    key = f"oauth_device:{flow['flow_id']}"
    state = json.loads(redis.store[key])
    redis.store[key] = json.dumps({**state, "user_id": "someone-else"})
    r = api.client.post(f"/api/v1/integrations/{created['id']}/openapi/device/poll", json={"flow_id": flow["flow_id"]})
    assert r.json()["status"] == "expired"


def test_device_sign_in_refusals_are_reported(api, monkeypatch, redis):
    created = _created(api, "oauth:device", client_id="tv-app")
    _mock_api(monkeypatch, lambda r: httpx.Response(400, json={"error": "invalid_client", "error_description": "unknown app"}))
    resp = api.client.get(f"/api/v1/integrations/{created['id']}/authorize")
    assert resp.status_code == 502
    assert "invalid_client: unknown app" in resp.json()["detail"]


# ── Implicit ─────────────────────────────────────────────────────────────────


def test_implicit_sign_in(api, redis):
    created = _created(api, "oauth:implicit", client_id="web-app")
    resp = api.client.get(f"/api/v1/integrations/{created['id']}/authorize")
    assert resp.status_code == 200, resp.text
    url = urlparse(resp.json()["authorize_url"])
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "login.acme.test"
    assert query["response_type"] == "token"
    assert query["redirect_uri"] == f"{settings.base_url}/api/oauth/callback"
    assert "code_challenge" not in query
    state = query["state"]
    assert json.loads(redis.store[f"oauth_state:{state}"])["flow"] == "implicit"

    done = api.client.post(
        "/api/v1/integrations/openapi/oauth/implicit",
        json={"state": state, "access_token": "implicit-token", "token_type": "bearer", "expires_in": "1800"},
    )
    assert done.status_code == 200, done.text
    assert done.json() == {"integration_id": created["id"], "provider": "openapi"}
    integration = api.db.integrations[created["id"]]
    assert integration.status == "connected"
    assert decrypt_value(integration.access_token_enc) == "implicit-token"
    assert integration.refresh_token_enc is None

    replay = api.client.post(
        "/api/v1/integrations/openapi/oauth/implicit", json={"state": state, "access_token": "other"}
    )
    assert replay.status_code == 400


async def test_a_state_is_consumed_once_under_concurrent_callbacks(monkeypatch):
    """Two requests carrying one state: the read and the delete are one step."""
    import asyncio

    class _SlowRedis(_FakeRedis):
        async def get(self, key):
            value = self.store.get(key)
            await asyncio.sleep(0.01)  # both readers get in before either deletes
            return value

        async def getdel(self, key):
            return self.store.pop(key, None)

    fake = _SlowRedis()
    fake.store["oauth_state:s1"] = json.dumps({"integration_id": "i", "flow": "implicit"})

    async def client():
        return fake

    monkeypatch.setattr(oauth, "redis_client", client)
    results = await asyncio.gather(oauth.get_state_data("s1"), oauth.get_state_data("s1"))
    assert sorted(r is not None for r in results) == [False, True]


@pytest.mark.parametrize(
    ("patch", "payload", "fragment"),
    [
        ({"user_id": "someone-else"}, {"access_token": "t"}, "not started from this account"),
        ({"flow": None}, {"access_token": "t"}, "not started from this account"),
        ({}, {"error": "access_denied", "error_description": "no"}, "refused (access_denied: no)"),
        ({}, {"access_token": "t", "token_type": "mac"}, "only Bearer"),
        ({}, {}, "no access token"),
    ],
)
def test_implicit_sign_in_refusals(api, redis, patch, payload, fragment):
    created = _created(api, "oauth:implicit", client_id="web-app")
    authorize_url = api.client.get(f"/api/v1/integrations/{created['id']}/authorize").json()["authorize_url"]
    state = parse_qs(urlparse(authorize_url).query)["state"][0]
    key = f"oauth_state:{state}"
    redis.store[key] = json.dumps({**json.loads(redis.store[key]), **patch})
    resp = api.client.post("/api/v1/integrations/openapi/oauth/implicit", json={"state": state, **payload})
    assert resp.status_code == 400
    assert fragment in resp.json()["detail"]
    assert api.db.integrations[created["id"]].access_token_enc is None


def test_the_callback_hands_an_implicit_redirect_to_the_frontend():
    app = FastAPI()
    app.include_router(oauth_callbacks.router, prefix="/api/oauth")
    with TestClient(app) as client:
        resp = client.get("/api/oauth/callback", follow_redirects=False)
        assert resp.status_code in (302, 307)
        assert resp.headers["location"] == f"{settings.frontend_url or settings.base_url}/oauth/implicit"
        # A query without a code is still refused, as before.
        assert client.get("/api/oauth/callback?error=access_denied", follow_redirects=False).status_code == 400


def test_the_code_flow_keeps_pkce(api, redis):
    created = _created(
        api,
        "oauth:authorization_code",
        client_id="cid",
        openapi={"content": (FIXTURES / "inventory_v31.yaml").read_text(), "auth_option": "oauth:authorization_code"},
    )
    query = parse_qs(urlparse(api.client.get(f"/api/v1/integrations/{created['id']}/authorize").json()["authorize_url"]).query)
    assert query["response_type"] == ["code"] and "code_challenge" in query


# ── OpenID Connect ───────────────────────────────────────────────────────────


async def test_behind_a_proxy_the_code_exchange_checks_the_token_url(monkeypatch):
    """Behind a proxy the proxy connects: the URL itself is checked before sending."""
    from app.services import web_fetch

    monkeypatch.setattr(web_fetch, "_environment_proxies", lambda: {"all://": "http://proxy.corp.test:3128"})
    monkeypatch.setattr(
        web_fetch.socket,
        "getaddrinfo",
        lambda host, *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))],
    )
    integration = SimpleNamespace(
        provider="openapi",
        client_id_enc=encrypt_value("client"),
        client_secret_enc=encrypt_value("secret"),
        token_url="https://login.acme.test/token",
        extra_config_enc=None,
    )
    from app.integrations.token_endpoint import TokenEndpointRefusedError

    # Refused by the token-endpoint check, or else by the client's own hook.
    with pytest.raises((TokenEndpointRefusedError, httpx.ConnectError), match=r"metadata|169\.254\.169\.254"):
        await oauth.exchange_code_for_tokens_with_integration(integration, "the-code")


def test_openid_connect_through_its_discovery_document(api, monkeypatch):
    async def discovery(_document, _url_base):
        return {
            "https://login.acme.test/.well-known/openid-configuration": {
                "authorization_endpoint": "https://login.acme.test/oidc/authorize",
                "token_endpoint": "https://login.acme.test/oidc/token",
                "scopes_supported": ["openid", "email", "offline_access"],
            }
        }

    monkeypatch.setattr("app.services.openapi_import.service.fetch_discovery", discovery)
    preview = api.client.post("/api/v1/integrations/openapi/preview", json={"content": AUTH_FLOWS}).json()
    option = next(o for o in preview["auth_options"] if o["id"] == "oidc:authorization_code")
    assert option["default_scopes"] == ["openid", "offline_access"]
    created = _created(api, "oidc:authorization_code", client_id="cid", client_secret="sec")
    integration = api.db.integrations[created["id"]]
    assert integration.scopes == "openid offline_access"
    assert integration.authorize_url == "https://login.acme.test/oidc/authorize"


# ── Mutual TLS ───────────────────────────────────────────────────────────────


def test_the_mutual_tls_option_needs_a_certificate(api):
    resp = _create(api, "mtls")
    assert resp.status_code == 400
    assert "client certificate" in resp.json()["detail"]
    certificate, _key = make_client_certificate()
    resp = _create(api, "mtls", extra_config={"client_certificate": certificate})
    assert resp.status_code == 400
    assert "both the client certificate and its private key" in resp.json()["detail"]
    _other_certificate, other_key = make_client_certificate()
    resp = _create(api, "mtls", extra_config={"client_certificate": certificate, "client_private_key": other_key})
    assert resp.status_code == 400
    assert "does not belong to the certificate" in resp.json()["detail"]


async def test_mutual_tls_integration(api, monkeypatch):
    certificate, key = make_client_certificate(passphrase="open sesame")
    created = _created(
        api,
        "mtls",
        extra_config={
            "client_certificate": certificate,
            "client_private_key": key,
            "client_private_key_password": "open sesame",
        },
    )
    assert (created["auth_type"], created["status"]) == ("none", "connected")
    # Secrets never come back; the certificate is public.
    assert created["extra_config"]["client_private_key"] != key
    assert created["extra_config"]["client_private_key_password"] != "open sesame"

    spec = api.client.get(f"/api/v1/integrations/{created['id']}/openapi").json()
    assert spec["client_certificate"]["subject"] == "CN=agent.acme.test"
    assert spec["client_certificate"]["expired"] is False

    captured: dict[str, Any] = {}
    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        captured.update(kwargs)
        kwargs["transport"] = httpx.MockTransport(lambda r: httpx.Response(200, json={"name": "agent"}))
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr("app.services.web_fetch._environment_proxies", lambda: False)
    _defs, executor = await build_integration_tools(api.db, ToolBindings({"openapi": created["id"]}, basis=BindingBasis.CALLER_CHECKED, reason="test"))
    result = await executor.execute_tool(ToolCall(id="1", name="acme_accounts_get_me", arguments={}))
    assert not result.is_error, result.content
    extra = decrypt_json(api.db.integrations[created["id"]].extra_config_enc)
    assert captured["transport"]._pool._ssl_context is client_ssl_context(extra)

    resp = api.client.delete(f"/api/v1/integrations/{created['id']}/openapi/client-certificate")
    assert resp.status_code == 400
    assert "replace it instead" in resp.json()["detail"]


def test_any_option_can_add_a_client_certificate(api):
    created = _created(
        api, "legacy", api_key="k", extra_config={"base_url": "https://api.acme.test/v1"}
    )
    url = f"/api/v1/integrations/{created['id']}/openapi/client-certificate"
    certificate, key = make_client_certificate("robot.acme.test")
    bad = api.client.put(url, json={"certificate": "not a cert", "private_key": key})
    assert bad.status_code == 400 and "PEM" in bad.json()["detail"]
    expired_certificate, expired_key = make_client_certificate(days=-3)
    bad = api.client.put(url, json={"certificate": expired_certificate, "private_key": expired_key})
    assert bad.status_code == 400 and "expired on" in bad.json()["detail"]
    ok = api.client.put(url, json={"certificate": certificate, "private_key": key})
    assert ok.status_code == 200, ok.text
    assert ok.json()["subject"] == "CN=robot.acme.test"
    assert ok.json()["expires_soon"] is True  # the helper's 30 days
    extra = decrypt_json(api.db.integrations[created["id"]].extra_config_enc)
    assert extra["client_private_key"] == key.strip()
    assert extra["base_url"] == "https://api.acme.test/v1"
    assert api.client.get(f"/api/v1/integrations/{created['id']}/openapi").json()["client_certificate"]["subject"] == "CN=robot.acme.test"

    assert api.client.delete(url).status_code == 204
    extra = decrypt_json(api.db.integrations[created["id"]].extra_config_enc)
    assert not any(k.startswith("client_") for k in extra)
    assert api.client.get(f"/api/v1/integrations/{created['id']}/openapi").json()["client_certificate"] is None


async def test_an_unusable_stored_certificate_refuses_every_call(api, monkeypatch):
    created = _created(api, "legacy", api_key="k")
    integration = api.db.integrations[created["id"]]
    extra = decrypt_json(integration.extra_config_enc)
    certificate, key = make_client_certificate(passphrase="right")
    integration.extra_config_enc = encrypt_value(
        json.dumps({**extra, "client_certificate": certificate, "client_private_key": key, "client_private_key_password": "wrong"})
    )
    seen = _mock_api(monkeypatch, lambda r: httpx.Response(200, json={}))
    _defs, executor = await build_integration_tools(api.db, ToolBindings({"openapi": created["id"]}, basis=BindingBasis.CALLER_CHECKED, reason="test"))
    result = await executor.execute_tool(ToolCall(id="1", name="acme_accounts_get_me", arguments={}))
    assert result.is_error
    assert "client certificate cannot be used" in result.content
    assert seen == []


def _store_certificate(api, integration_id: str, certificate: str, key: str) -> None:
    integration = api.db.integrations[integration_id]
    extra = decrypt_json(integration.extra_config_enc)
    integration.extra_config_enc = encrypt_value(
        json.dumps({**extra, "client_certificate": certificate, "client_private_key": key})
    )


async def test_an_expired_certificate_says_so_instead_of_calling(api, monkeypatch):
    created = _created(api, "legacy", api_key="k")
    _store_certificate(api, created["id"], *make_client_certificate(days=-3))
    seen = _mock_api(monkeypatch, lambda r: httpx.Response(200, json={}))
    _defs, executor = await build_integration_tools(api.db, ToolBindings({"openapi": created["id"]}, basis=BindingBasis.CALLER_CHECKED, reason="test"))
    result = await executor.execute_tool(ToolCall(id="1", name="acme_accounts_get_me", arguments={}))
    assert result.is_error
    assert "The certificate expired on" in result.content and "Replace the certificate" in result.content
    assert seen == []


async def test_a_certificate_near_its_end_is_logged_once_a_day(api, monkeypatch, caplog):
    from app.integrations.providers import openapi as provider

    monkeypatch.setattr(provider, "_expiry_logged", {})
    created = _created(api, "legacy", api_key="k")
    _store_certificate(api, created["id"], *make_client_certificate(days=10))
    _mock_api(monkeypatch, lambda r: httpx.Response(200, json={}))
    with caplog.at_level("WARNING", logger=provider.__name__):
        for _turn in range(3):
            await build_integration_tools(api.db, ToolBindings({"openapi": created["id"]}, basis=BindingBasis.CALLER_CHECKED, reason="test"))
    warnings = [r for r in caplog.records if "expires on" in r.getMessage()]
    assert len(warnings) == 1
    assert created["id"] in warnings[0].getMessage()


# ── Archives ─────────────────────────────────────────────────────────────────


def test_preview_and_create_from_a_zip_archive(api):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "api/openapi.yaml",
            AUTH_FLOWS.replace(
                "      summary: The signed-in account\n",
                "      summary: The signed-in account\n      parameters:\n        - $ref: 'common.yaml#/Verbose'\n",
            ),
        )
        archive.writestr("api/common.yaml", "Verbose: {name: verbose, in: query, schema: {type: boolean}}\n")
    encoded = base64.b64encode(buffer.getvalue()).decode()
    preview = api.client.post("/api/v1/integrations/openapi/preview", json={"archive": encoded, "filename": "api.zip"})
    assert preview.status_code == 200, preview.text
    assert preview.json()["warnings"] == []
    created = _created(
        api, "legacy", api_key="k", openapi={"archive": encoded, "filename": "api.zip", "auth_option": "legacy"}
    )
    spec = api.db.specs[created["id"]]
    assert (spec.source_type, spec.source_filename) == ("file", "api.zip")
    me = next(t for t in spec.active_tools if t["name"] == "acme_accounts_get_me")
    assert me["input_schema"]["properties"]["verbose"] == {"type": "boolean"}
    bad = api.client.post("/api/v1/integrations/openapi/preview", json={"archive": "%%%"})
    assert bad.status_code == 400 and "base64" in bad.json()["detail"]


# -- Review fixes: endpoints stay where the user configured them --


def test_a_replaced_spec_never_moves_the_token_url_to_another_server(api, monkeypatch):
    """A re-published spec naming another token host must not receive the stored secret."""
    created = _created(api, "oauth:password", api_key="alice:secret", client_id="cli")
    integration = api.db.integrations[created["id"]]
    assert integration.token_url == TOKEN_URL
    moved = AUTH_FLOWS.replace("https://login.acme.test/token", "https://collector.attacker.test/token")
    resp = api.client.put(
        f"/api/v1/integrations/{created['id']}/openapi",
        json={"content": moved, "filename": "accounts.yaml", "auth_option": "oauth:password"},
    )
    assert resp.status_code == 200, resp.text
    assert integration.token_url == TOKEN_URL
    assert any("another server" in w and TOKEN_URL in w for w in resp.json()["warnings"])


def test_a_same_server_token_url_change_follows_the_spec(api):
    created = _created(api, "oauth:password", api_key="alice:secret", client_id="cli")
    integration = api.db.integrations[created["id"]]
    moved = AUTH_FLOWS.replace("https://login.acme.test/token", "https://login.acme.test/v2/token")
    resp = api.client.put(
        f"/api/v1/integrations/{created['id']}/openapi",
        json={"content": moved, "filename": "accounts.yaml", "auth_option": "oauth:password"},
    )
    assert resp.status_code == 200, resp.text
    assert integration.token_url == "https://login.acme.test/v2/token"


def test_a_device_code_is_only_redeemed_where_its_flow_started(api, monkeypatch, redis):
    created = _created(api, "oauth:device", client_id="tv-app")
    _mock_api(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            json={"device_code": "d", "user_code": "U", "verification_uri": "https://login.acme.test/a"},
        ),
    )
    flow = api.client.get(f"/api/v1/integrations/{created['id']}/authorize").json()["device_authorization"]
    state = json.loads(redis.store[f"oauth_device:{flow['flow_id']}"])
    assert (state["token_url"], state["client_id"]) == (TOKEN_URL, "tv-app")
    # The token URL moved while the user was typing the code.
    api.db.integrations[created["id"]].token_url = "https://login.acme.test/other-token"
    seen = _mock_api(monkeypatch, lambda r: httpx.Response(200, json={"access_token": "t"}))
    r = api.client.post(f"/api/v1/integrations/{created['id']}/openapi/device/poll", json={"flow_id": flow["flow_id"]})
    assert r.json()["status"] == "expired"
    assert seen == []
    assert f"oauth_device:{flow['flow_id']}" not in redis.store


def test_an_implicit_token_with_an_absurd_lifetime_is_stored(api, redis):
    created = _created(api, "oauth:implicit", client_id="spa")
    authorize = api.client.get(f"/api/v1/integrations/{created['id']}/authorize").json()["authorize_url"]
    state = parse_qs(urlparse(authorize).query)["state"][0]
    r = api.client.post(
        "/api/v1/integrations/openapi/oauth/implicit",
        json={"state": state, "access_token": "tok", "token_type": "Bearer", "expires_in": "9" * 40},
    )
    assert r.status_code == 200, r.text
    integration = api.db.integrations[created["id"]]
    assert integration.status == "connected" and integration.token_expires_at is not None
