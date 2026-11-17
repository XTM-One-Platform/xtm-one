"""Outbound MCP OAuth without pre-provisioned credentials.

A remote MCP server must be connectable from its URL alone: the platform
discovers the authorization server (RFC 9728 -> RFC 8414), registers itself
dynamically (RFC 7591) to mint a ``client_id``, and runs Authorization Code +
PKCE in the browser.  These tests pin the parts of that chain that are easy to
break silently:

* the well-known probe order (three URL shapes are in real-world use);
* the refusal to carry a code or token over a discovered ``http://`` endpoint;
* operator-configured URLs winning over discovered ones;
* PKCE and the RFC 8707 ``resource`` parameter reaching the wire;
* ``client_secret`` being **absent**, not empty, for a public client — an empty
  one is a client-authentication attempt that an OAuth 2.1 server which issued
  no secret rejects with ``invalid_client``, which would break every
  dynamically registered connection at the token and refresh steps.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.services.mcp_oauth_discovery import (
    MCPOAuthDiscoveryError,
    OAuthEndpoints,
    _well_known_candidates,
    discover_oauth_endpoints,
    fallback_registration_url,
    generate_pkce_pair,
    is_safe_endpoint,
    register_dynamic_client,
)


def _async_runtime(build):
    """``build_mcp_runtime`` is a coroutine (it reads vault references)."""

    async def _build(server):
        return build(server)

    return _build


MCP_URL = "https://mcp.example.com/mcp"
AS_ORIGIN = "https://auth.example.com"


@pytest.fixture(autouse=True)
def _token_url_resolves_publicly(monkeypatch):
    """The token endpoint guard resolves the token URL's host; answer for DNS, never a real resolver."""
    import socket

    from app.integrations import token_endpoint

    async def _resolve(_host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("203.0.113.10", port))]

    monkeypatch.setattr(token_endpoint, "_resolve", _resolve)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _json_routes(routes: dict[str, dict]):
    """MockTransport handler serving ``routes`` and 404-ing everything else."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = routes.get(str(request.url))
        if body is None:
            return httpx.Response(404, json={"error": "not_found"})
        return httpx.Response(200, json=body)

    return handler


# ── URL shapes and safety ────────────────────────────────────────────────────


def test_well_known_candidates_cover_all_three_shapes():
    candidates = _well_known_candidates(MCP_URL, "oauth-protected-resource")
    assert candidates == [
        # RFC 9728 / RFC 8414: well-known segment inserted before the path.
        "https://mcp.example.com/.well-known/oauth-protected-resource/mcp",
        # OIDC Discovery 1.0: appended to the path.
        "https://mcp.example.com/mcp/.well-known/oauth-protected-resource",
        # Root document.
        "https://mcp.example.com/.well-known/oauth-protected-resource",
    ]


def test_well_known_candidates_deduplicate_for_a_pathless_url():
    assert _well_known_candidates(AS_ORIGIN, "oauth-authorization-server") == [
        "https://auth.example.com/.well-known/oauth-authorization-server"
    ]


@pytest.mark.parametrize(
    ("url", "safe"),
    [
        ("https://mcp.example.com/mcp", True),
        ("http://localhost:9000/mcp", True),
        ("http://127.0.0.1:9000/mcp", True),
        # A plain-HTTP endpoint on a routable host would carry the
        # authorization code and the access token in clear text.
        ("http://mcp.example.com/mcp", False),
        ("file:///etc/passwd", False),
        ("mcp.example.com", False),
        ("", False),
    ],
)
def test_is_safe_endpoint(url, safe):
    assert is_safe_endpoint(url) is safe


# ── Discovery ────────────────────────────────────────────────────────────────


async def test_discovery_follows_protected_resource_to_authorization_server():
    routes = {
        "https://mcp.example.com/.well-known/oauth-protected-resource/mcp": {
            "resource": MCP_URL,
            "authorization_servers": [AS_ORIGIN],
            "scopes_supported": ["mcp:read", "mcp:write"],
        },
        f"{AS_ORIGIN}/.well-known/oauth-authorization-server": {
            "issuer": AS_ORIGIN,
            "authorization_endpoint": f"{AS_ORIGIN}/authorize",
            "token_endpoint": f"{AS_ORIGIN}/token",
            "registration_endpoint": f"{AS_ORIGIN}/register",
        },
    }
    async with _client(_json_routes(routes)) as http:
        found = await discover_oauth_endpoints(server_url=MCP_URL, http=http)

    assert found.discovered is True
    assert found.is_complete is True
    assert found.authorize_url == f"{AS_ORIGIN}/authorize"
    assert found.token_url == f"{AS_ORIGIN}/token"
    assert found.registration_url == f"{AS_ORIGIN}/register"
    assert found.issuer == AS_ORIGIN
    # Audience binding for the token that will be issued (RFC 8707).
    assert found.resource == MCP_URL
    assert found.scopes == "mcp:read mcp:write"


async def test_discovery_falls_back_to_openid_configuration():
    routes = {
        f"{AS_ORIGIN}/.well-known/openid-configuration": {
            "issuer": AS_ORIGIN,
            "authorization_endpoint": f"{AS_ORIGIN}/authorize",
            "token_endpoint": f"{AS_ORIGIN}/token",
            "registration_endpoint": f"{AS_ORIGIN}/register",
        },
    }
    async with _client(_json_routes(routes)) as http:
        found = await discover_oauth_endpoints(server_url=AS_ORIGIN, http=http)

    assert found.registration_url == f"{AS_ORIGIN}/register"


async def test_discovery_uses_the_mcp_server_as_its_own_authorization_server():
    """A small MCP server that publishes AS metadata at its own origin."""
    routes = {
        "https://mcp.example.com/.well-known/oauth-authorization-server": {
            "authorization_endpoint": "https://mcp.example.com/oauth/authorize",
            "token_endpoint": "https://mcp.example.com/oauth/token",
            "registration_endpoint": "https://mcp.example.com/oauth/register",
        },
    }
    async with _client(_json_routes(routes)) as http:
        found = await discover_oauth_endpoints(server_url=MCP_URL, http=http)

    assert found.authorize_url == "https://mcp.example.com/oauth/authorize"
    assert found.registration_url == "https://mcp.example.com/oauth/register"


async def test_operator_configured_urls_win_over_discovered_ones():
    routes = {
        f"{AS_ORIGIN}/.well-known/oauth-authorization-server": {
            "authorization_endpoint": f"{AS_ORIGIN}/discovered-authorize",
            "token_endpoint": f"{AS_ORIGIN}/discovered-token",
            "registration_endpoint": f"{AS_ORIGIN}/register",
        },
    }
    async with _client(_json_routes(routes)) as http:
        found = await discover_oauth_endpoints(
            server_url=AS_ORIGIN,
            authorize_url=f"{AS_ORIGIN}/tenant/authorize",
            token_url=f"{AS_ORIGIN}/tenant/token",
            http=http,
        )

    assert found.authorize_url == f"{AS_ORIGIN}/tenant/authorize"
    assert found.token_url == f"{AS_ORIGIN}/tenant/token"
    # …while still picking up the piece the operator could not know.
    assert found.registration_url == f"{AS_ORIGIN}/register"


async def test_registration_endpoint_is_discovered_from_the_authorize_url_alone():
    """The "I only have Authorize + Token URLs" case.

    Nothing is known about the MCP server itself, so the authorize URL's origin
    is the only lead — and it is enough when the authorization server publishes
    metadata there.
    """
    routes = {
        f"{AS_ORIGIN}/.well-known/oauth-authorization-server": {
            "registration_endpoint": f"{AS_ORIGIN}/register",
        },
    }
    async with _client(_json_routes(routes)) as http:
        found = await discover_oauth_endpoints(
            authorize_url=f"{AS_ORIGIN}/authorize",
            token_url=f"{AS_ORIGIN}/token",
            http=http,
        )

    assert found.registration_url == f"{AS_ORIGIN}/register"


async def test_discovered_plain_http_endpoints_are_discarded():
    routes = {
        f"{AS_ORIGIN}/.well-known/oauth-authorization-server": {
            "authorization_endpoint": "http://auth.example.com/authorize",
            "token_endpoint": "http://auth.example.com/token",
            "registration_endpoint": "http://auth.example.com/register",
        },
    }
    async with _client(_json_routes(routes)) as http:
        found = await discover_oauth_endpoints(server_url=AS_ORIGIN, http=http)

    assert found.authorize_url is None
    assert found.token_url is None
    assert found.registration_url is None
    assert found.is_complete is False


async def test_discovery_of_a_server_publishing_nothing_is_not_an_error():
    async with _client(_json_routes({})) as http:
        found = await discover_oauth_endpoints(server_url=MCP_URL, http=http)

    assert found.discovered is False
    assert found.is_complete is False


async def test_each_well_known_url_is_probed_at_most_once():
    """Overlapping candidate bases must not multiply the outbound requests.

    An MCP server that is its own authorization server yields the same
    candidate URLs from several bases; probing each one repeatedly would
    multiply the latency of a dead host by the number of bases.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(404, json={"error": "not_found"})

    async with _client(handler) as http:
        await discover_oauth_endpoints(
            server_url=AS_ORIGIN,
            authorize_url=f"{AS_ORIGIN}/authorize",
            http=http,
        )

    assert len(seen) == len(set(seen)), f"duplicate probes: {seen}"


async def test_resource_is_only_claimed_from_protected_resource_metadata():
    """A plain OAuth provider must not be sent an RFC 8707 ``resource``.

    Only a server publishing protected-resource metadata is known to follow
    the MCP authorization spec; a generic provider may answer an unknown
    ``resource`` with ``invalid_target``.
    """
    routes = {
        "https://mcp.example.com/.well-known/oauth-authorization-server": {
            "authorization_endpoint": "https://mcp.example.com/authorize",
            "token_endpoint": "https://mcp.example.com/token",
        },
    }
    async with _client(_json_routes(routes)) as http:
        found = await discover_oauth_endpoints(server_url=MCP_URL, http=http)

    assert found.is_complete is True
    assert found.resource is None


async def test_oversized_metadata_is_ignored():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "authorization_endpoint": f"{AS_ORIGIN}/authorize",
                "padding": "x" * (300 * 1024),
            },
        )

    async with _client(handler) as http:
        found = await discover_oauth_endpoints(server_url=AS_ORIGIN, http=http)

    assert found.authorize_url is None


def test_fallback_registration_url_derives_from_the_authorize_origin():
    endpoints = OAuthEndpoints(
        authorize_url=f"{AS_ORIGIN}/oauth/authorize",
        token_url=f"{AS_ORIGIN}/oauth/token",
    )
    assert fallback_registration_url(endpoints) == f"{AS_ORIGIN}/register"
    assert fallback_registration_url(OAuthEndpoints()) is None


# ── Dynamic client registration ──────────────────────────────────────────────


async def test_register_dynamic_client_registers_a_public_client():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={"client_id": "dyn-client-1"})

    async with _client(handler) as http:
        registered = await register_dynamic_client(
            f"{AS_ORIGIN}/register",
            client_name="XTM One",
            redirect_uri="https://copilot.example.com/api/oauth/callback",
            scopes="mcp:read",
            http=http,
        )

    assert registered.client_id == "dyn-client-1"
    # No secret issued -> none stored, so the token request omits the field.
    assert registered.client_secret is None
    assert seen[0]["token_endpoint_auth_method"] == "none"
    assert seen[0]["redirect_uris"] == [
        "https://copilot.example.com/api/oauth/callback"
    ]
    assert seen[0]["grant_types"] == ["authorization_code", "refresh_token"]
    assert seen[0]["scope"] == "mcp:read"


async def test_register_dynamic_client_retries_when_a_public_client_is_refused():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if body.get("token_endpoint_auth_method") == "none":
            return httpx.Response(400, json={"error": "invalid_client_metadata"})
        return httpx.Response(
            201, json={"client_id": "dyn-2", "client_secret": "s3cr3t"}
        )

    async with _client(handler) as http:
        registered = await register_dynamic_client(
            f"{AS_ORIGIN}/register",
            client_name="XTM One",
            redirect_uri="https://copilot.example.com/api/oauth/callback",
            http=http,
        )

    assert len(seen) == 2
    assert "token_endpoint_auth_method" not in seen[1]
    assert registered.client_id == "dyn-2"
    assert registered.client_secret == "s3cr3t"


async def test_register_dynamic_client_raises_when_refused_outright():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="registration is closed")

    async with _client(handler) as http:
        with pytest.raises(MCPOAuthDiscoveryError, match="refused"):
            await register_dynamic_client(
                f"{AS_ORIGIN}/register",
                client_name="XTM One",
                redirect_uri="https://copilot.example.com/api/oauth/callback",
                http=http,
            )


async def test_register_dynamic_client_rejects_a_plain_http_endpoint():
    with pytest.raises(MCPOAuthDiscoveryError, match="HTTPS"):
        await register_dynamic_client(
            "http://auth.example.com/register",
            client_name="XTM One",
            redirect_uri="https://copilot.example.com/api/oauth/callback",
        )


async def test_register_dynamic_client_rejects_a_response_without_a_client_id():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"client_name": "XTM One"})

    async with _client(handler) as http:
        with pytest.raises(MCPOAuthDiscoveryError, match="no client_id"):
            await register_dynamic_client(
                f"{AS_ORIGIN}/register",
                client_name="XTM One",
                redirect_uri="https://copilot.example.com/api/oauth/callback",
                http=http,
            )


# ── PKCE ─────────────────────────────────────────────────────────────────────


def test_generate_pkce_pair_matches_rfc7636():
    import base64
    import hashlib

    verifier, challenge = generate_pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert challenge == expected
    assert "=" not in challenge


# ── Authorize request shaping ────────────────────────────────────────────────


class _FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    async def get(self, key):
        return self.kv.get(key)

    async def delete(self, key):
        self.kv.pop(key, None)

    async def getdel(self, key):
        return self.kv.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch):
    import app.core.redis as redis_module
    import app.integrations.oauth as oauth_module

    fake = _FakeRedis()

    async def _client_factory():
        return fake

    # ``oauth`` imports the symbol directly; the discovery service resolves it
    # from the module at call time. Patch both so either path gets the fake.
    monkeypatch.setattr(oauth_module, "redis_client", _client_factory)
    monkeypatch.setattr(redis_module, "redis_client", _client_factory)
    return fake


def _mcp_server(**overrides):
    from app.core.security import encrypt_value

    defaults = dict(
        id="11111111-1111-1111-1111-111111111111",
        name="Remote MCP",
        url=MCP_URL,
        client_id_enc=encrypt_value("dyn-client-1"),
        client_secret_enc=None,
        authorize_url=f"{AS_ORIGIN}/authorize",
        token_url=f"{AS_ORIGIN}/token",
        registration_url=f"{AS_ORIGIN}/register",
        oauth_resource=MCP_URL,
        # Set only once a 401 challenge has named it (RFC 9728 §5.1).
        oauth_metadata_url=None,
        scopes="mcp:read",
        dcr_registered_at=None,
        oauth_status="disconnected",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


async def test_authorize_url_carries_pkce_and_resource_for_a_public_client(
    fake_redis,
):
    from app.integrations.oauth import build_authorize_url_for_mcp_server

    srv = _mcp_server()
    url = await build_authorize_url_for_mcp_server(srv, "user-1")

    params = parse_qs(urlparse(url).query)
    assert params["client_id"] == ["dyn-client-1"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["resource"] == [MCP_URL]
    challenge = params["code_challenge"][0]

    # The verifier is stashed with the state so the callback can complete the
    # exchange — without it a PKCE-enforcing server rejects the code.
    state = params["state"][0]
    stored = json.loads(fake_redis.kv[f"oauth_state:{state}"])
    assert stored["entity_type"] == "mcp_server"
    verifier = stored["code_verifier"]

    import base64
    import hashlib

    assert challenge == (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )


async def test_authorize_url_leaves_a_confidential_client_unchanged(fake_redis):
    """A hand-configured client with a secret keeps its pre-existing request."""
    from app.core.security import encrypt_value
    from app.integrations.oauth import build_authorize_url_for_mcp_server

    srv = _mcp_server(
        client_secret_enc=encrypt_value("shhh"), oauth_resource=None
    )
    url = await build_authorize_url_for_mcp_server(srv, "user-1")

    params = parse_qs(urlparse(url).query)
    assert "code_challenge" not in params
    assert "resource" not in params

    state = params["state"][0]
    assert "code_verifier" not in json.loads(fake_redis.kv[f"oauth_state:{state}"])


# ── Onboarding orchestration ─────────────────────────────────────────────────


class _FakeDB:
    def __init__(self) -> None:
        self.commits = 0

    def in_transaction(self) -> bool:
        return False

    async def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def stub_branding(monkeypatch):
    import app.services.branding as branding_module

    async def _get_branding(_db):
        return SimpleNamespace(name="XTM One")

    monkeypatch.setattr(branding_module, "get_branding", _get_branding)


async def test_ensure_client_discovers_and_registers_from_the_url_alone(
    fake_redis, stub_branding, monkeypatch
):
    """The headline case: a server configured with nothing but its URL."""
    import app.services.mcp_oauth_discovery as discovery_module
    from app.core.security import decrypt_value

    routes = {
        "https://mcp.example.com/.well-known/oauth-protected-resource/mcp": {
            "resource": MCP_URL,
            "authorization_servers": [AS_ORIGIN],
        },
        f"{AS_ORIGIN}/.well-known/oauth-authorization-server": {
            "authorization_endpoint": f"{AS_ORIGIN}/authorize",
            "token_endpoint": f"{AS_ORIGIN}/token",
            "registration_endpoint": f"{AS_ORIGIN}/register",
            "scopes_supported": ["mcp:read"],
        },
    }
    json_handler = _json_routes(routes)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and str(request.url) == f"{AS_ORIGIN}/register":
            return httpx.Response(201, json={"client_id": "dyn-from-url"})
        return json_handler(request)

    class _Client(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(discovery_module.httpx, "AsyncClient", _Client)

    srv = _mcp_server(
        client_id_enc=None,
        authorize_url=None,
        token_url=None,
        registration_url=None,
        oauth_resource=None,
        scopes=None,
    )
    db = _FakeDB()

    setup = await discovery_module.ensure_mcp_oauth_client(db, srv)

    assert setup == discovery_module.MCPOAuthClientSetup(registered=True)
    assert decrypt_value(srv.client_id_enc) == "dyn-from-url"
    assert srv.client_secret_enc is None
    assert srv.authorize_url == f"{AS_ORIGIN}/authorize"
    assert srv.token_url == f"{AS_ORIGIN}/token"
    assert srv.registration_url == f"{AS_ORIGIN}/register"
    assert srv.oauth_resource == MCP_URL
    assert srv.scopes == "mcp:read"
    assert srv.dcr_registered_at is not None
    assert db.commits == 1


async def test_ensure_client_is_a_no_op_for_a_fully_configured_server(fake_redis, monkeypatch):
    import app.services.mcp_oauth_discovery as discovery_module

    # Read only to learn whether its authorization server issues refresh tokens
    # on request; this one does not.
    _no_discovery_network(monkeypatch, discovery_module)
    srv = _mcp_server()
    db = _FakeDB()

    assert await discovery_module.ensure_mcp_oauth_client(db, srv) == discovery_module.MCPOAuthClientSetup()
    assert srv.scopes == "mcp:read"
    assert db.commits == 0


async def test_ensure_client_reports_a_server_with_no_discoverable_endpoints(
    fake_redis, stub_branding, monkeypatch
):
    import app.services.mcp_oauth_discovery as discovery_module

    class _Client(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_json_routes({}))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(discovery_module.httpx, "AsyncClient", _Client)

    srv = _mcp_server(client_id_enc=None, authorize_url=None, token_url=None)
    with pytest.raises(MCPOAuthDiscoveryError, match="Authorize URL"):
        await discovery_module.ensure_mcp_oauth_client(_FakeDB(), srv)


def _no_discovery_network(monkeypatch, discovery_module) -> None:
    """Discovery still runs for a server with no client id: answer it locally (every probe 404s)."""

    class _Client(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_json_routes({}))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(discovery_module.httpx, "AsyncClient", _Client)


async def test_ensure_client_honours_the_operator_kill_switch(fake_redis, monkeypatch):
    import app.services.mcp_oauth_discovery as discovery_module
    from app.core.config import settings

    monkeypatch.setattr(settings, "mcp_outbound_dcr_enabled", False)
    _no_discovery_network(monkeypatch, discovery_module)

    srv = _mcp_server(client_id_enc=None)
    with pytest.raises(MCPOAuthDiscoveryError, match="disabled"):
        await discovery_module.ensure_mcp_oauth_client(_FakeDB(), srv)


async def test_concurrent_registration_is_serialized(
    fake_redis, stub_branding, monkeypatch
):
    import app.services.mcp_oauth_discovery as discovery_module

    _no_discovery_network(monkeypatch, discovery_module)
    # Hold the per-server lock as if another request were mid-registration.
    await fake_redis.set("mcp_dcr_register:11111111-1111-1111-1111-111111111111", "1")

    srv = _mcp_server(client_id_enc=None)
    with pytest.raises(MCPOAuthDiscoveryError, match="already in progress"):
        await discovery_module.ensure_mcp_oauth_client(_FakeDB(), srv)


async def test_forget_dynamic_client_only_discards_a_registered_one():
    from app.services.mcp_oauth_discovery import forget_dynamic_client

    manual = _mcp_server(oauth_status="connected")
    assert await forget_dynamic_client(_FakeDB(), manual) is False
    # Credentials only the operator can restore are never thrown away.
    assert manual.client_id_enc is not None

    from datetime import UTC, datetime

    dynamic = _mcp_server(
        dcr_registered_at=datetime.now(UTC), oauth_status="connected"
    )
    assert await forget_dynamic_client(_FakeDB(), dynamic) is True
    assert dynamic.client_id_enc is None
    assert dynamic.dcr_registered_at is None
    assert dynamic.oauth_status == "disconnected"


# ── Token exchange / refresh ─────────────────────────────────────────────────


class _CapturingClient:
    """``httpx.AsyncClient`` stand-in that records the single POST it receives."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        type(self).captured = {"url": url, **kwargs}
        return httpx.Response(
            200,
            json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
            request=httpx.Request("POST", url),
        )


async def test_token_exchange_omits_client_secret_for_a_public_client(monkeypatch):
    import app.integrations.oauth as oauth_module

    monkeypatch.setattr(httpx, "AsyncClient", _CapturingClient)

    tokens = await oauth_module.exchange_code_for_tokens_with_mcp_server(
        _mcp_server(), "the-code", code_verifier="the-verifier"
    )

    assert tokens["access_token"] == "at"
    body = _CapturingClient.captured["data"]
    assert "client_secret" not in body
    assert body["code_verifier"] == "the-verifier"
    assert body["resource"] == MCP_URL
    assert body["client_id"] == "dyn-client-1"


async def test_token_exchange_still_sends_a_configured_client_secret(monkeypatch):
    import app.integrations.oauth as oauth_module
    from app.core.security import encrypt_value

    monkeypatch.setattr(httpx, "AsyncClient", _CapturingClient)

    await oauth_module.exchange_code_for_tokens_with_mcp_server(
        _mcp_server(client_secret_enc=encrypt_value("shhh")), "the-code"
    )

    assert _CapturingClient.captured["data"]["client_secret"] == "shhh"


async def test_refresh_omits_client_secret_for_a_public_client(monkeypatch):
    import app.services.token_refresh as refresh_module
    from app.core.security import encrypt_value

    fake = _FakeRedis()

    async def _client_factory():
        return fake

    monkeypatch.setattr(refresh_module, "redis_client", _client_factory)

    captured: dict = {}

    class _Http:
        async def post(self, url, **kwargs):
            captured.update({"url": url, **kwargs})
            return httpx.Response(
                200,
                json={"access_token": "new-at", "expires_in": 3600},
                request=httpx.Request("POST", url),
            )

    srv = _mcp_server(refresh_token_enc=encrypt_value("rt"), oauth_status="connected")
    ok = await refresh_module._do_refresh_mcp(srv, _Http())

    assert ok is True
    assert "client_secret" not in captured["data"]
    assert captured["data"]["resource"] == MCP_URL


# ── 401 challenge handling (RFC 9728 §5.1) ───────────────────────────────────


def _taskgroup_wrapped_401(www_authenticate: str | None = None):
    """A 401 shaped the way the MCP SDK actually delivers one.

    The SDK runs its transport in an anyio task group, so the httpx error is
    buried in a BaseExceptionGroup behind a CancelledError — which is why the
    original report saw "unhandled errors in a TaskGroup" instead of "401".
    """
    headers = {"www-authenticate": www_authenticate} if www_authenticate else {}
    response = httpx.Response(
        401,
        headers=headers,
        request=httpx.Request("POST", MCP_URL),
    )
    http_error = httpx.HTTPStatusError(
        "Client error '401 Unauthorized'", request=response.request, response=response
    )
    return BaseExceptionGroup("unhandled errors in a TaskGroup", [http_error])


def test_auth_challenge_is_found_inside_a_task_group():
    from app.tools.mcp_client import _extract_auth_challenge

    exc = _taskgroup_wrapped_401(
        'Bearer resource_metadata="https://auth.example.com/.well-known/'
        'oauth-protected-resource/tenant-7", error="invalid_token"'
    )
    found = _extract_auth_challenge(exc)

    assert found is not None, (
        "a 401 buried in a TaskGroup must still be recognised — otherwise it "
        "surfaces as an opaque transport failure"
    )
    is_401, metadata_url = found
    assert is_401 is True
    assert metadata_url == (
        "https://auth.example.com/.well-known/oauth-protected-resource/tenant-7"
    )


def test_auth_challenge_survives_a_cancellation_wrapper():
    from app.tools.mcp_client import _extract_auth_challenge

    group = _taskgroup_wrapped_401('Bearer resource_metadata="https://a.example/prm"')
    cancelled = asyncio.CancelledError("Cancelled via cancel scope")
    cancelled.__context__ = group

    found = _extract_auth_challenge(cancelled)
    assert found is not None
    assert found[1] == "https://a.example/prm"


def test_auth_challenge_without_resource_metadata_is_still_a_401():
    from app.tools.mcp_client import _extract_auth_challenge

    found = _extract_auth_challenge(_taskgroup_wrapped_401("Bearer"))
    assert found == (True, None)


def test_non_401_errors_are_not_treated_as_auth_challenges():
    from app.tools.mcp_client import _extract_auth_challenge

    response = httpx.Response(503, request=httpx.Request("POST", MCP_URL))
    exc = httpx.HTTPStatusError("boom", request=response.request, response=response)
    assert _extract_auth_challenge(BaseExceptionGroup("tg", [exc])) is None
    assert _extract_auth_challenge(ConnectionError("refused")) is None


def test_challenge_parsing_stops_at_the_closing_quote():
    """A greedy pattern would swallow the following challenge parameter."""
    from app.tools.mcp_client import _extract_auth_challenge

    found = _extract_auth_challenge(
        _taskgroup_wrapped_401('Bearer resource_metadata="https://a/prm", error="x"')
    )
    assert found[1] == "https://a/prm"


async def test_challenge_url_outranks_the_well_known_guess():
    """RFC 9728 lets the metadata live where no probe would look."""
    off_path = "https://auth.example.com/tenant-7/prm.json"
    routes = {
        # The guessed locations answer with the WRONG authorization server…
        "https://mcp.example.com/.well-known/oauth-protected-resource/mcp": {
            "resource": MCP_URL,
            "authorization_servers": ["https://wrong.example.com"],
        },
        # …while the challenge points at the right document.
        off_path: {
            "resource": MCP_URL,
            "authorization_servers": [AS_ORIGIN],
        },
        f"{AS_ORIGIN}/.well-known/oauth-authorization-server": {
            "authorization_endpoint": f"{AS_ORIGIN}/authorize",
            "token_endpoint": f"{AS_ORIGIN}/token",
            "registration_endpoint": f"{AS_ORIGIN}/register",
        },
    }
    async with _client(_json_routes(routes)) as http:
        found = await discover_oauth_endpoints(
            server_url=MCP_URL, metadata_url=off_path, http=http
        )

    assert found.authorize_url == f"{AS_ORIGIN}/authorize"
    assert found.registration_url == f"{AS_ORIGIN}/register"


async def test_discovery_falls_back_when_the_challenge_url_is_dead():
    routes = {
        "https://mcp.example.com/.well-known/oauth-protected-resource/mcp": {
            "resource": MCP_URL,
            "authorization_servers": [AS_ORIGIN],
        },
        f"{AS_ORIGIN}/.well-known/oauth-authorization-server": {
            "authorization_endpoint": f"{AS_ORIGIN}/authorize",
            "token_endpoint": f"{AS_ORIGIN}/token",
        },
    }
    async with _client(_json_routes(routes)) as http:
        found = await discover_oauth_endpoints(
            server_url=MCP_URL,
            metadata_url="https://auth.example.com/gone.json",
            http=http,
        )

    assert found.is_complete is True


# ── "Connected, but authorization required" ──────────────────────────────────


def _runtime(**overrides):
    from app.tools.mcp_client import MCPServerRuntime

    defaults = dict(
        name="Remote MCP",
        transport="streamable_http",
        url=MCP_URL,
        headers=None,
    )
    defaults.update(overrides)
    return MCPServerRuntime(**defaults)


async def test_pending_authorization_detected_when_no_credential_is_held(monkeypatch):
    """The reported bug: tools list fine, every call then 401s."""
    import app.services.mcp_oauth_discovery as discovery_module
    from app.tools.mcp_client import _pending_authorization_metadata

    async def _found(url, **_kw):
        return "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"

    monkeypatch.setattr(
        discovery_module, "find_protected_resource_metadata", _found
    )

    assert await _pending_authorization_metadata(_runtime()) is not None


async def test_no_pending_authorization_once_a_token_is_being_sent(monkeypatch):
    import app.services.mcp_oauth_discovery as discovery_module
    from app.tools.mcp_client import _pending_authorization_metadata

    async def _boom(url, **_kw):
        raise AssertionError("must not probe when a credential is already sent")

    monkeypatch.setattr(discovery_module, "find_protected_resource_metadata", _boom)

    runtime = _runtime(headers={"Authorization": "Bearer at"})
    assert await _pending_authorization_metadata(runtime) is None


async def test_no_pending_authorization_for_an_unprotected_server(monkeypatch):
    import app.services.mcp_oauth_discovery as discovery_module
    from app.tools.mcp_client import _pending_authorization_metadata

    async def _none(url, **_kw):
        return None

    monkeypatch.setattr(discovery_module, "find_protected_resource_metadata", _none)

    assert await _pending_authorization_metadata(_runtime()) is None


async def test_stdio_servers_are_never_probed(monkeypatch):
    import app.services.mcp_oauth_discovery as discovery_module
    from app.tools.mcp_client import _pending_authorization_metadata

    async def _boom(url, **_kw):
        raise AssertionError("stdio has no HTTP endpoint to probe")

    monkeypatch.setattr(discovery_module, "find_protected_resource_metadata", _boom)

    runtime = _runtime(transport="stdio", url=None)
    assert await _pending_authorization_metadata(runtime) is None


# ── Recovering the 401 the MCP SDK hides ─────────────────────────────────────
#
# The SDK runs its transport in an anyio task group that consumes the HTTP
# error and cancels the pending request, so `initialize()` raises a bare
# CancelledError with no status code anywhere in the chain — which is why an
# unauthorized server used to surface as an opaque transport failure. The
# recovery is a direct probe, and it has to survive the delivered cancellation
# without leaving one pending for the caller's next await.


class _ProbeClient:
    """httpx.AsyncClient stand-in returning a canned response."""

    response: httpx.Response | None = None
    raises: Exception | None = None
    calls: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    # The probe sends through the SDK's ``request_within_origin``.
    max_redirects = 20

    def build_request(self, method, url, **kwargs):
        return httpx.Request(method, url)

    async def send(self, request, **kwargs):
        type(self).calls.append(str(request.url))
        if type(self).raises is not None:
            raise type(self).raises
        return type(self).response


def _install_probe(monkeypatch, *, status=401, challenge=None, raises=None):
    import httpx as httpx_module

    _ProbeClient.calls = []
    _ProbeClient.raises = raises
    headers = {"WWW-Authenticate": challenge} if challenge else {}
    _ProbeClient.response = httpx.Response(
        status, headers=headers, request=httpx.Request("POST", MCP_URL)
    )
    monkeypatch.setattr(httpx_module, "AsyncClient", _ProbeClient)


async def test_probe_recovers_the_challenge_from_a_live_401(monkeypatch):
    from app.tools.mcp_client import probe_auth_challenge

    _install_probe(
        monkeypatch, challenge='Bearer resource_metadata="https://a.example/prm"'
    )
    assert await probe_auth_challenge(MCP_URL) == (True, "https://a.example/prm")


async def test_probe_returns_nothing_for_a_healthy_endpoint(monkeypatch):
    from app.tools.mcp_client import probe_auth_challenge

    _install_probe(monkeypatch, status=200)
    assert await probe_auth_challenge(MCP_URL) is None


async def test_probe_swallows_transport_errors(monkeypatch):
    from app.tools.mcp_client import probe_auth_challenge

    _install_probe(monkeypatch, raises=httpx.ConnectError("refused"))
    assert await probe_auth_challenge(MCP_URL) is None


async def test_diagnosis_turns_a_hidden_401_into_auth_required(monkeypatch):
    from app.tools.mcp_client import diagnose_auth_after_connect_failure

    _install_probe(
        monkeypatch, challenge='Bearer resource_metadata="https://a.example/prm"'
    )
    result = await diagnose_auth_after_connect_failure(_runtime())

    assert result is not None
    assert result["status"] == "auth_required"
    assert result["resource_metadata"] == "https://a.example/prm"


async def test_diagnosis_clears_the_spurious_cancellation(monkeypatch):
    """The caller's next await — a DB write — must not be cancelled.

    anyio delivers its transport-scope cancellation to *this* task. Once the
    401 is confirmed the cancellation is known to be spurious, so leaving it
    pending would kill the endpoint's own commit (it did, before this was
    handled explicitly).
    """
    from app.tools.mcp_client import diagnose_auth_after_connect_failure

    _install_probe(
        monkeypatch, challenge='Bearer resource_metadata="https://a.example/prm"'
    )

    task = asyncio.current_task()
    task.cancel()  # simulate anyio's delivered cancellation
    assert task.cancelling() > 0

    result = await diagnose_auth_after_connect_failure(_runtime())

    assert result is not None and result["status"] == "auth_required"
    assert task.cancelling() == 0, (
        "a confirmed 401 must clear the spurious cancellation, otherwise the "
        "caller's next await dies"
    )
    # Proves the task is genuinely usable again.
    await asyncio.sleep(0)


async def test_diagnosis_restores_the_cancellation_when_it_finds_no_401(
    monkeypatch,
):
    """A real cancellation must still end the task."""
    from app.tools.mcp_client import diagnose_auth_after_connect_failure

    _install_probe(monkeypatch, status=503)

    task = asyncio.current_task()
    task.cancel()

    assert await diagnose_auth_after_connect_failure(_runtime()) is None
    assert task.cancelling() > 0
    task.uncancel()  # tidy up so the test itself can finish


async def test_diagnosis_ignores_stdio_servers(monkeypatch):
    from app.tools.mcp_client import diagnose_auth_after_connect_failure

    _install_probe(monkeypatch, challenge='Bearer resource_metadata="https://x/prm"')
    runtime = _runtime(transport="stdio", url=None)

    assert await diagnose_auth_after_connect_failure(runtime) is None
    assert _ProbeClient.calls == []


# ── Discovery response consistency ───────────────────────────────────────────


def _discovery_payload(endpoints, *, dcr_enabled=True):
    """Reproduce what ``POST /mcp-servers/oauth/discover`` returns.

    Mirrors the endpoint's own resolution so the invariant below is pinned
    without standing up FastAPI, a DB session and a rate limiter.
    """
    from app.api.v1.schemas.mcp_server import MCPOAuthDiscoveryResponse

    registration_url = endpoints.registration_url or fallback_registration_url(
        endpoints
    )
    return MCPOAuthDiscoveryResponse(
        authorize_url=endpoints.authorize_url,
        token_url=endpoints.token_url,
        registration_url=registration_url,
        registration_url_advertised=endpoints.registration_url is not None,
        resource=endpoints.resource,
        scopes=endpoints.scopes,
        issuer=endpoints.issuer,
        discovered=endpoints.discovered,
        supports_dynamic_registration=bool(registration_url and dcr_enabled),
    )


def test_discovery_never_claims_registration_without_naming_the_endpoint():
    """``supports_dynamic_registration`` and ``registration_url`` must agree.

    Reporting "registration is possible" while returning no URL left the form
    with nothing to pre-fill in precisely the case the operator most needs to
    see: a server that advertises no registration endpoint, where the
    conventional one is the only lead.
    """
    advertised = OAuthEndpoints(
        authorize_url=f"{AS_ORIGIN}/authorize",
        token_url=f"{AS_ORIGIN}/token",
        registration_url=f"{AS_ORIGIN}/register",
    )
    conventional = OAuthEndpoints(
        authorize_url=f"{AS_ORIGIN}/authorize", token_url=f"{AS_ORIGIN}/token"
    )
    nothing = OAuthEndpoints()

    for endpoints in (advertised, conventional, nothing):
        payload = _discovery_payload(endpoints)
        assert payload.supports_dynamic_registration == bool(
            payload.registration_url
        ), f"inconsistent payload for {endpoints}"


def test_discovery_flags_a_conventional_registration_url_as_unadvertised():
    """A guessed endpoint must not be presented as one the server published."""
    conventional = _discovery_payload(
        OAuthEndpoints(
            authorize_url=f"{AS_ORIGIN}/authorize", token_url=f"{AS_ORIGIN}/token"
        )
    )
    assert conventional.registration_url == f"{AS_ORIGIN}/register"
    assert conventional.registration_url_advertised is False
    assert conventional.supports_dynamic_registration is True

    advertised = _discovery_payload(
        OAuthEndpoints(
            authorize_url=f"{AS_ORIGIN}/authorize",
            token_url=f"{AS_ORIGIN}/token",
            registration_url=f"{AS_ORIGIN}/oauth/register",
        )
    )
    assert advertised.registration_url == f"{AS_ORIGIN}/oauth/register"
    assert advertised.registration_url_advertised is True


def test_discovery_reports_no_registration_when_the_kill_switch_is_off():
    payload = _discovery_payload(
        OAuthEndpoints(
            authorize_url=f"{AS_ORIGIN}/authorize",
            token_url=f"{AS_ORIGIN}/token",
            registration_url=f"{AS_ORIGIN}/register",
        ),
        dcr_enabled=False,
    )
    assert payload.supports_dynamic_registration is False


# ── Tool discovery after authorization ───────────────────────────────────────


class _CallbackDB:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


@pytest.fixture
def stub_post_auth_discovery(monkeypatch):
    """Neutralise the DB-connection release; return the MCPManager stub hook."""
    import app.core.database as database_module

    async def _release(_db):
        return None

    monkeypatch.setattr(
        database_module, "release_db_connection_before_loop", _release
    )


async def test_tools_are_discovered_right_after_authorization(
    monkeypatch, stub_post_auth_discovery
):
    """Otherwise the flow ends on a connected server showing no tools at all."""
    import app.api.oauth_callbacks as callbacks
    import app.tools.mcp_client as mcp_client_module

    tools = [{"name": "search", "description": "d", "input_schema": {}}]

    class _Manager:
        async def test_connection(self, runtime):
            return {"status": "ok", "tools": tools}

    monkeypatch.setattr(mcp_client_module, "MCPManager", _Manager)
    monkeypatch.setattr(
        "app.api.v1.helpers.build_mcp_runtime",
        _async_runtime(lambda srv: SimpleNamespace()),
    )

    srv = _mcp_server(transport="streamable_http", discovered_tools=None,
                      tools_discovered_at=None)
    db = _CallbackDB()

    await callbacks._discover_tools_after_authorization(db, srv)

    assert srv.discovered_tools == tools
    assert srv.tools_discovered_at is not None
    assert db.commits == 1


async def test_post_auth_discovery_replaces_a_pre_authorization_catalog(
    monkeypatch, stub_post_auth_discovery
):
    """A catalog listed before authorization may be reduced or stubbed."""
    import app.api.oauth_callbacks as callbacks
    import app.tools.mcp_client as mcp_client_module

    full = [{"name": "a"}, {"name": "b"}, {"name": "c"}]

    class _Manager:
        async def test_connection(self, runtime):
            return {"status": "ok", "tools": full}

    monkeypatch.setattr(mcp_client_module, "MCPManager", _Manager)
    monkeypatch.setattr(
        "app.api.v1.helpers.build_mcp_runtime",
        _async_runtime(lambda srv: SimpleNamespace()),
    )

    srv = _mcp_server(
        transport="streamable_http",
        discovered_tools=[{"name": "a"}],  # what the server showed unauthenticated
        tools_discovered_at=None,
    )
    await callbacks._discover_tools_after_authorization(_CallbackDB(), srv)

    assert srv.discovered_tools == full


async def test_a_failed_discovery_never_breaks_the_callback(
    monkeypatch, stub_post_auth_discovery
):
    """The authorization is the irreplaceable part; the catalog is one click."""
    import app.api.oauth_callbacks as callbacks
    import app.tools.mcp_client as mcp_client_module

    class _Manager:
        async def test_connection(self, runtime):
            raise RuntimeError("server exploded")

    monkeypatch.setattr(mcp_client_module, "MCPManager", _Manager)
    monkeypatch.setattr(
        "app.api.v1.helpers.build_mcp_runtime",
        _async_runtime(lambda srv: SimpleNamespace()),
    )

    srv = _mcp_server(transport="streamable_http", discovered_tools=None,
                      tools_discovered_at=None)
    db = _CallbackDB()

    await callbacks._discover_tools_after_authorization(db, srv)  # must not raise

    assert srv.discovered_tools is None
    assert db.commits == 0


async def test_post_auth_discovery_is_bounded(monkeypatch, stub_post_auth_discovery):
    """A sluggish server must cost a moment, not the browser's whole redirect."""
    import app.api.oauth_callbacks as callbacks
    import app.tools.mcp_client as mcp_client_module

    class _Manager:
        async def test_connection(self, runtime):
            await asyncio.sleep(30)
            return {"status": "ok", "tools": [{"name": "late"}]}

    monkeypatch.setattr(mcp_client_module, "MCPManager", _Manager)
    monkeypatch.setattr(
        "app.api.v1.helpers.build_mcp_runtime",
        _async_runtime(lambda srv: SimpleNamespace()),
    )
    monkeypatch.setattr(callbacks, "_POST_AUTH_DISCOVERY_TIMEOUT_SECS", 0.05)

    srv = _mcp_server(transport="streamable_http", discovered_tools=None,
                      tools_discovered_at=None)
    await callbacks._discover_tools_after_authorization(_CallbackDB(), srv)

    assert srv.discovered_tools is None


async def test_stdio_servers_skip_post_auth_discovery(monkeypatch):
    import app.api.oauth_callbacks as callbacks
    import app.tools.mcp_client as mcp_client_module

    class _Manager:
        async def test_connection(self, runtime):
            raise AssertionError("stdio has no OAuth connection to refresh")

    monkeypatch.setattr(mcp_client_module, "MCPManager", _Manager)
    srv = _mcp_server(transport="stdio", url=None, discovered_tools=None)
    await callbacks._discover_tools_after_authorization(_CallbackDB(), srv)
