"""A remote MCP server's short-lived OAuth token stays usable.

A customer's authorization server issued 10-minute access tokens and the MCP
connection stopped working after ten minutes. The scheduler's sweep refreshed
tokens ahead of their expiry, but nothing else did, and three cases left a
short-lived token dead. These tests pin the fix for each:

- the authorization request asks for ``offline_access`` when the authorization
  server advertises it (MCP 2026-07-28, "Refresh Tokens"), so Okta, Entra ID or
  Auth0 issue a refresh token at all;
- a token response without ``expires_in`` takes its lifetime from a JWT's
  ``exp`` instead of an assumed hour;
- a connection sends the freshest stored token on every request: refreshed at
  use time when it is about to expire, and once after a ``401``, under the
  sweep's lock and guards, with the refresh a ``401`` forces bounded, and run
  to completion even when the connection is cancelled meanwhile.

The SDK's own transports driven by the hook over a real socket are in
``test_mcp_oauth_refresh_e2e.py``.
"""

import asyncio
import base64
import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import anyio
import httpx
import pytest

from app.integrations.oauth import (
    MAX_TOKEN_LIFETIME_SECONDS,
    UNKNOWN_EXPIRY_TTL_SECONDS,
    apply_oauth_token_expiry,
    jwt_expiry_seconds,
)
from app.services import mcp_oauth_discovery, mcp_oauth_tokens, token_refresh
from app.services.mcp_oauth_discovery import (
    MCPOAuthClientSetup,
    OAuthEndpoints,
    discover_oauth_endpoints,
    ensure_mcp_oauth_client,
)
from app.services.mcp_oauth_tokens import StoredMCPOAuthToken
from app.services.token_refresh import MCPAccessToken, current_mcp_access_token
from tests.test_mcp_token_refresh_race import (
    SERVER_ID,
    _HttpClient,
    _RotatingTokenEndpoint,
)
from tests.test_token_refresh_rotation_race import _FakeRedis, _StoredRow, _TrackedSession

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


def _jwt(**claims) -> str:
    def _part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{_part({'alg': 'RS256', 'typ': 'JWT'})}.{_part(claims)}.c2lnbmF0dXJl"


def _in(seconds: int) -> int:
    return int(datetime.now(UTC).timestamp()) + seconds


# ── A token response without expires_in ──────────────────────────────────────


def _stamped(tokens: dict) -> float:
    # A refreshable connection: one nothing can renew keeps no expiry.
    target = SimpleNamespace(token_expires_at=None, refresh_token_enc="enc-rt")
    apply_oauth_token_expiry(target, tokens)
    return (target.token_expires_at - datetime.now(UTC)).total_seconds()


def test_expires_in_states_the_lifetime_when_present():
    assert _stamped({"access_token": _jwt(exp=_in(600)), "expires_in": 1800}) == pytest.approx(1800, abs=5)


def test_a_jwt_without_expires_in_lives_until_its_exp():
    """A 10-minute JWT was taken for an hour, and refreshed only after it."""
    assert _stamped({"access_token": _jwt(exp=_in(600))}) == pytest.approx(600, abs=5)


@pytest.mark.parametrize(
    "access_token",
    [
        "opaque-notion-token",
        _jwt(sub="no-exp"),
        _jwt(exp="soon"),
        _jwt(exp=float("inf")),
        "not.base64!.jwt",
        None,
    ],
    ids=["opaque", "no-exp", "bad-exp", "infinite-exp", "garbage", "missing"],
)
def test_without_a_readable_lifetime_the_hour_stays(access_token):
    assert jwt_expiry_seconds(access_token) is None
    assert _stamped({"access_token": access_token}) == pytest.approx(UNKNOWN_EXPIRY_TTL_SECONDS, abs=5)


def test_an_exp_already_past_is_stamped_as_expiring_now(caplog):
    """A skewed clock: taking the token for an hour would bring the bug back."""
    token = _jwt(exp=_in(-90))
    assert jwt_expiry_seconds(token) == pytest.approx(-90, abs=5)

    with caplog.at_level(logging.WARNING, logger="app.integrations.oauth"):
        assert _stamped({"access_token": token}) == pytest.approx(1, abs=2)

    assert "expired" in caplog.text and "clock" in caplog.text
    assert token not in caplog.text


@pytest.mark.parametrize(
    "tokens",
    [
        {"access_token": _jwt(exp=10**12)},
        {"access_token": "opaque", "expires_in": 10**15},
        {"access_token": "opaque", "expires_in": float("inf")},
    ],
    ids=["far-future-exp", "huge-expires-in", "infinite-expires-in"],
)
def test_an_out_of_range_lifetime_is_bounded_never_raised(tokens):
    """It is stamped after the refresh token rotated: an overflow would lose it."""
    stamped = _stamped(tokens)
    assert stamped <= MAX_TOKEN_LIFETIME_SECONDS
    assert stamped in (pytest.approx(MAX_TOKEN_LIFETIME_SECONDS, abs=5), pytest.approx(UNKNOWN_EXPIRY_TTL_SECONDS, abs=5))


# ── offline_access ───────────────────────────────────────────────────────────


def _metadata(
    *, as_scopes: list[str], token_endpoint: str = f"{AS_ORIGIN}/token", other_idp_scopes: list[str] | None = None
):
    routes = {
        "https://mcp.example.com/.well-known/oauth-protected-resource/mcp": {
            "resource": MCP_URL,
            "authorization_servers": [AS_ORIGIN],
            # An MCP server does not advertise offline_access itself.
            "scopes_supported": ["mcp:tools"],
        },
        f"{AS_ORIGIN}/.well-known/oauth-authorization-server": {
            "issuer": AS_ORIGIN,
            "authorization_endpoint": f"{AS_ORIGIN}/authorize",
            "token_endpoint": token_endpoint,
            "scopes_supported": as_scopes,
        },
    }
    if other_idp_scopes is not None:
        # The authorization server an operator typed by hand.
        routes["https://other-idp.example.com/.well-known/oauth-authorization-server"] = {
            "issuer": "https://other-idp.example.com",
            "authorization_endpoint": "https://other-idp.example.com/authorize",
            "token_endpoint": "https://other-idp.example.com/token",
            "registration_endpoint": "https://other-idp.example.com/register",
            "scopes_supported": other_idp_scopes,
        }

    def handler(request: httpx.Request) -> httpx.Response:
        body = routes.get(str(request.url))
        return httpx.Response(200, json=body) if body is not None else httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_discovery_says_when_the_authorization_server_issues_refresh_tokens_on_request():
    async with _metadata(as_scopes=["openid", "offline_access", "mcp:tools"]) as http:
        found = await discover_oauth_endpoints(server_url=MCP_URL, http=http)
    # The scopes still come from the MCP server's own metadata.
    assert found.scopes == "mcp:tools"
    assert found.offline_access_supported is True

    async with _metadata(as_scopes=["openid", "mcp:tools"]) as http:
        found = await discover_oauth_endpoints(server_url=MCP_URL, http=http)
    assert found.offline_access_supported is False


@pytest.mark.parametrize(
    ("other_idp_scopes", "expected"),
    [(None, False), (["openid"], False), (["openid", "offline_access"], True)],
    ids=["typed-server-unreadable", "typed-server-without-it", "typed-server-with-it"],
)
async def test_offline_access_is_said_of_the_authorization_server_the_flow_uses(other_idp_scopes, expected):
    """A token URL typed by hand may name another authorization server than the
    one the resource names: that one's metadata decides, and only that."""
    async with _metadata(as_scopes=["offline_access"], other_idp_scopes=other_idp_scopes) as http:
        found = await discover_oauth_endpoints(
            server_url=MCP_URL,
            authorize_url="https://other-idp.example.com/authorize",
            token_url="https://other-idp.example.com/token",
            http=http,
        )
    assert found.offline_access_supported is expected
    # The endpoints are still filled from the first document only, never the typed server's.
    assert found.registration_url is None
    assert found.issuer == AS_ORIGIN


class _Db:
    """``open``: a transaction holds a pool connection, as after a query."""

    def __init__(self):
        self.commits = 0
        self.open = False

    def in_transaction(self):
        return self.open

    async def commit(self):
        self.commits += 1
        self.open = False


def _server(scopes: str | None, **overrides) -> SimpleNamespace:
    values = dict(
        id=SERVER_ID,
        url=MCP_URL,
        client_id_enc="operator-client",
        client_secret_enc=None,
        dcr_registered_at=None,
        authorize_url=f"{AS_ORIGIN}/authorize",
        token_url=f"{AS_ORIGIN}/token",
        registration_url=None,
        oauth_resource=MCP_URL,
        oauth_metadata_url=None,
        scopes=scopes,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def discovered(monkeypatch):
    calls: list[dict] = []
    answer = SimpleNamespace(endpoints=OAuthEndpoints(offline_access_supported=True), error=None)

    async def _discover(**kwargs):
        calls.append(kwargs)
        if answer.error:
            raise answer.error
        return answer.endpoints

    monkeypatch.setattr(mcp_oauth_discovery, "discover_oauth_endpoints", _discover)
    return SimpleNamespace(calls=calls, answer=answer)


async def test_the_authorization_asks_for_offline_access_when_it_is_advertised(discovered):
    srv, db = _server("mcp:tools"), _Db()
    assert await ensure_mcp_oauth_client(db, srv) == MCPOAuthClientSetup(offline_access_added=True)
    # Stored, so the authorize request AND the refresh request send it.
    assert srv.scopes == "mcp:tools offline_access"
    assert db.commits == 1
    assert len(discovered.calls) == 1


@pytest.mark.parametrize("scopes", ["mcp:tools offline_access", None, ""])
async def test_offline_access_is_not_added_twice_nor_alone(discovered, scopes):
    """With no scopes the request asks for the server's defaults, which a scope
    parameter holding offline_access alone could narrow."""
    srv, db = _server(scopes), _Db()
    assert await ensure_mcp_oauth_client(db, srv) == MCPOAuthClientSetup()
    assert srv.scopes == scopes
    assert discovered.calls == [] and db.commits == 0


async def test_offline_access_is_not_requested_where_it_is_not_advertised(discovered):
    discovered.answer.endpoints = OAuthEndpoints(offline_access_supported=False)
    srv, db = _server("mcp:tools"), _Db()
    assert await ensure_mcp_oauth_client(db, srv) == MCPOAuthClientSetup()
    assert srv.scopes == "mcp:tools" and db.commits == 0


async def test_a_failed_discovery_leaves_the_scopes_as_they_are(discovered):
    discovered.answer.error = httpx.ConnectError("unreachable")
    srv, db = _server("mcp:tools"), _Db()
    assert await ensure_mcp_oauth_client(db, srv) == MCPOAuthClientSetup()
    assert srv.scopes == "mcp:tools"


async def test_a_client_registered_without_offline_access_keeps_its_scopes(discovered):
    """An authorization server enforcing the registered scope would refuse the
    authorization of a client asking for one it was not registered with."""
    srv, db = _server("mcp:tools", client_id_enc="dyn-client", dcr_registered_at=_later(-3600)), _Db()
    assert await ensure_mcp_oauth_client(db, srv) == MCPOAuthClientSetup()
    assert srv.scopes == "mcp:tools"
    assert discovered.calls == [] and db.commits == 0


async def test_a_new_client_is_registered_with_offline_access(discovered, monkeypatch):
    """One discovery fills the endpoints in and settles the scopes, before the
    client is registered, so it is registered with the scopes it will ask for."""
    discovered.answer.endpoints = OAuthEndpoints(
        authorize_url=f"{AS_ORIGIN}/authorize",
        token_url=f"{AS_ORIGIN}/token",
        registration_url=f"{AS_ORIGIN}/register",
        resource=MCP_URL,
        scopes="mcp:tools",
        offline_access_supported=True,
        discovered=True,
    )
    registered_with: list[str | None] = []

    async def _register(registration_url, *, client_name, redirect_uri, scopes):
        registered_with.append(scopes)
        return mcp_oauth_discovery.DynamicClient(client_id="dyn-new", client_secret=None)

    async def _branding(_db):
        return SimpleNamespace(name="XTM One")

    redis = _FakeRedis()

    async def _redis():
        return redis

    monkeypatch.setattr(mcp_oauth_discovery, "register_dynamic_client", _register)
    monkeypatch.setattr("app.services.branding.get_branding", _branding)
    monkeypatch.setattr("app.core.redis.redis_client", _redis)
    srv = _server(None, client_id_enc=None, authorize_url=None, token_url=None)
    db = _Db()

    setup = await ensure_mcp_oauth_client(db, srv)

    assert setup == MCPOAuthClientSetup(registered=True, offline_access_added=True)
    assert registered_with == ["mcp:tools offline_access"]
    assert srv.scopes == "mcp:tools offline_access"
    assert len(discovered.calls) == 1
    assert db.commits == 1


async def test_no_transaction_is_held_across_discovery_and_registration(discovered, monkeypatch):
    """The client name is read first, then the connection is handed back: the
    discovery (up to 20 s of outbound requests) and the registration request
    run with no pool connection pinned and no server row locked."""
    discovered.answer.endpoints = OAuthEndpoints(
        authorize_url=f"{AS_ORIGIN}/authorize",
        token_url=f"{AS_ORIGIN}/token",
        registration_url=f"{AS_ORIGIN}/register",
        scopes="mcp:tools",
        offline_access_supported=True,
        discovered=True,
    )
    srv = _server(None, client_id_enc=None, authorize_url=None, token_url=None)
    db = _Db()
    seen: list[tuple[str, bool]] = []

    async def _branding(_db):
        db.open = True  # a query opens a transaction
        return SimpleNamespace(name="XTM One")

    async def _discover(**_kwargs):
        seen.append(("discovery", db.open))
        return discovered.answer.endpoints

    async def _register(registration_url, *, client_name, redirect_uri, scopes):
        seen.append((f"registration as {client_name}", db.open))
        return mcp_oauth_discovery.DynamicClient(client_id="dyn-new", client_secret=None)

    redis = _FakeRedis()

    async def _redis():
        return redis

    monkeypatch.setattr("app.services.branding.get_branding", _branding)
    monkeypatch.setattr(mcp_oauth_discovery, "discover_oauth_endpoints", _discover)
    monkeypatch.setattr(mcp_oauth_discovery, "register_dynamic_client", _register)
    monkeypatch.setattr("app.core.redis.redis_client", _redis)

    setup = await ensure_mcp_oauth_client(db, srv)

    assert setup == MCPOAuthClientSetup(registered=True, offline_access_added=True)
    assert seen == [("discovery", False), ("registration as XTM One", False)]
    # The release, then the one commit that stores the client.
    assert db.commits == 2


async def test_no_transaction_is_held_across_the_offline_access_discovery(discovered, monkeypatch):
    """A server with its client and endpoints configured still reads the
    authorization server metadata; the caller's lookup is committed first."""
    seen: list[bool] = []
    srv, db = _server("mcp:tools"), _Db()
    db.open = True  # the route looked the server up

    async def _discover(**_kwargs):
        seen.append(db.open)
        return discovered.answer.endpoints

    monkeypatch.setattr(mcp_oauth_discovery, "discover_oauth_endpoints", _discover)

    assert await ensure_mcp_oauth_client(db, srv) == MCPOAuthClientSetup(offline_access_added=True)
    assert seen == [False]
    assert db.commits == 2


# ── The connection's auth hook ───────────────────────────────────────────────


class _McpServer:
    """An MCP endpoint that accepts some bearer tokens and answers 401 to the rest."""

    def __init__(self, accepted: set[str]):
        self.accepted = accepted
        self.seen: list[tuple[str, bytes]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        self.seen.append((token, request.content))
        if token in self.accepted:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        return httpx.Response(401, headers={"www-authenticate": 'Bearer error="invalid_token"'})


@pytest.fixture
def stored(monkeypatch):
    """What ``current_mcp_access_token`` answers the hook, and what it was asked."""
    state = SimpleNamespace(asked=[], answer=None, error=None)

    async def _current(server_id, *, refused=None):
        state.asked.append(refused)
        if state.error:
            raise state.error
        return state.answer

    monkeypatch.setattr(token_refresh, "current_mcp_access_token", _current)
    return state


def _later(seconds: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


async def test_a_refused_token_is_replaced_and_the_request_replayed_once(stored):
    server = _McpServer({"at-2"})
    stored.answer = MCPAccessToken("at-2", _later(600))
    auth = StoredMCPOAuthToken(SERVER_ID, "at-1", _later(600))
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        response = await http.post(MCP_URL, json=body, auth=auth)

    assert response.status_code == 200
    assert [token for token, _ in server.seen] == ["at-1", "at-2"]
    # The replayed request carries the same body.
    assert server.seen[0][1] == server.seen[1][1] == json.dumps(body, separators=(",", ":")).encode()
    assert stored.asked == ["at-1"]
    assert auth.token == "at-2"


async def test_a_token_about_to_expire_is_replaced_before_it_is_sent(stored):
    server = _McpServer({"at-2"})
    stored.answer = MCPAccessToken("at-2", _later(600))
    auth = StoredMCPOAuthToken(SERVER_ID, "at-1", _later(10))

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        response = await http.get(MCP_URL, auth=auth)

    assert response.status_code == 200
    assert [token for token, _ in server.seen] == ["at-2"]
    assert stored.asked == [None]


async def test_a_server_that_refuses_every_token_gets_one_replacement(stored):
    server = _McpServer(set())
    stored.answer = MCPAccessToken("at-2", _later(600))
    auth = StoredMCPOAuthToken(SERVER_ID, "at-1", _later(600))

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        first = await http.get(MCP_URL, auth=auth)
        second = await http.get(MCP_URL, auth=auth)

    assert first.status_code == second.status_code == 401
    assert [token for token, _ in server.seen] == ["at-1", "at-2", "at-2"]
    assert stored.asked == ["at-1"]


@pytest.mark.parametrize("answer", ["nothing", "same", "error"])
async def test_without_a_replacement_the_servers_answer_stands(stored, answer):
    server = _McpServer(set())
    if answer == "same":
        stored.answer = MCPAccessToken("at-1", _later(600))
    elif answer == "error":
        stored.error = ConnectionError("redis is down")
    auth = StoredMCPOAuthToken(SERVER_ID, "at-1", _later(600))

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        response = await http.get(MCP_URL, auth=auth)

    assert response.status_code == 401
    assert [token for token, _ in server.seen] == ["at-1"]
    assert auth.token == "at-1"


async def test_the_runtime_of_an_oauth_server_carries_the_hook(monkeypatch):
    from app.api.v1 import helpers

    monkeypatch.setattr("app.core.security.decrypt_value", lambda value: value)
    monkeypatch.setattr("app.core.security.decrypt_json", lambda value: None)
    srv = SimpleNamespace(
        id=SERVER_ID,
        name="Remote MCP",
        transport="streamable_http",
        command=None,
        args=[],
        env_enc=None,
        headers_enc=None,
        url=MCP_URL,
        auth_mode="oauth",
        access_token_enc="at-1",
        token_expires_at=_later(600),
        is_company_managed=False,
    )
    runtime = await helpers.build_mcp_runtime(srv)
    assert isinstance(runtime.auth, StoredMCPOAuthToken)
    assert runtime.auth.token == "at-1"
    # The hook sets the header; a copy in the static headers would go stale.
    assert runtime.headers is None

    srv.auth_mode = "headers"
    assert (await helpers.build_mcp_runtime(srv)).auth is None


@pytest.mark.parametrize("transport", ["sse", "streamable_http"])
async def test_the_connection_hands_the_hook_to_the_transport(monkeypatch, transport):
    from contextlib import asynccontextmanager

    from app.tools import mcp_client

    handed: list = []

    @asynccontextmanager
    async def _transport(*, url, headers, auth=None, httpx_client_factory=None):
        # The platform's client factory rides along (the MCP server URL policy).
        handed.append(auth)
        raise ConnectionError("stop here")
        yield  # pragma: no cover

    monkeypatch.setattr(mcp_client, "sse_client", _transport)
    monkeypatch.setattr(mcp_client, "streamablehttp_client", _transport)
    auth = StoredMCPOAuthToken(SERVER_ID, "at-1", None)
    config = mcp_client.MCPServerRuntime(
        name=f"hook-handover-{transport}", transport=transport, url=MCP_URL, auth=auth
    )

    # Past the circuit breaker, whose failure counts other tests share.
    with pytest.raises(ConnectionError):
        await mcp_client.MCPConnection(config).connect(force=True)

    assert handed == [auth]


# ── The token a connection gets ──────────────────────────────────────────────


@pytest.fixture
def world(monkeypatch):
    row = _StoredRow(
        id=SERVER_ID,
        name="Remote MCP",
        auth_mode="oauth",
        oauth_status="connected",
        client_id_enc="dyn-client",
        client_secret_enc=None,
        access_token_enc="at-1",
        refresh_token_enc="rt-1",
        token_url=f"{AS_ORIGIN}/token",
        scopes="mcp:tools offline_access",
        oauth_resource=MCP_URL,
        token_expires_at=_later(600),
    )
    redis = _FakeRedis()
    endpoint = _RotatingTokenEndpoint()

    async def _redis():
        return redis

    monkeypatch.setattr(token_refresh, "redis_client", _redis)
    monkeypatch.setattr("app.core.database.async_session", lambda: _TrackedSession(row))
    monkeypatch.setattr(token_refresh, "decrypt_value", lambda value: value)
    monkeypatch.setattr(token_refresh, "encrypt_value", lambda value: value)
    monkeypatch.setattr(token_refresh.httpx, "AsyncClient", type("_Client", (_HttpClient,), {"endpoint": endpoint}))
    return SimpleNamespace(row=row, redis=redis, endpoint=endpoint)


async def test_a_fresh_stored_token_is_sent_without_a_refresh(world):
    token = await current_mcp_access_token(SERVER_ID)
    assert token.value == "at-1"
    assert world.endpoint.presented == []


async def test_a_token_the_sweep_refreshed_meanwhile_replaces_the_refused_one(world):
    world.row.values["access_token_enc"] = "at-sweep"
    token = await current_mcp_access_token(SERVER_ID, refused="at-1")
    assert token.value == "at-sweep"
    assert world.endpoint.presented == []


async def test_a_refused_token_is_refreshed_under_the_sweeps_guards(world):
    token = await current_mcp_access_token(SERVER_ID, refused="at-1")

    assert token.value == "at-2"
    assert world.endpoint.presented == ["rt-1"]
    values = world.row.values
    assert (values["access_token_enc"], values["refresh_token_enc"], values["oauth_status"]) == (
        "at-2",
        "rt-2",
        "connected",
    )
    # The refresh lock is released.
    assert not any(key.startswith("token_refresh:mcp:") for key in world.redis.values)


async def test_the_refresh_a_401_forces_happens_once_per_window(world):
    await current_mcp_access_token(SERVER_ID, refused="at-1")
    # The refreshed token is refused too: no second refresh in the window.
    assert await current_mcp_access_token(SERVER_ID, refused="at-2") is None
    assert world.endpoint.presented == ["rt-1"]


async def test_a_token_about_to_expire_is_refreshed_for_the_connection(world):
    world.row.values["token_expires_at"] = _later(20)
    token = await current_mcp_access_token(SERVER_ID)
    assert token.value == "at-2"
    assert world.endpoint.presented == ["rt-1"]


@pytest.mark.parametrize(
    "change",
    [
        {"oauth_status": "disconnected"},
        {"refresh_token_enc": None},
    ],
    ids=["disconnected", "no-refresh-token"],
)
async def test_no_refresh_where_the_sweep_would_not_refresh(world, change):
    world.row.values.update(change)
    assert await current_mcp_access_token(SERVER_ID, refused="at-1") is None
    assert world.endpoint.presented == []


async def test_a_refresh_backing_off_is_not_retried_by_a_connection(world):
    await token_refresh.start_refresh_cooldown(
        token_refresh.mcp_refresh_cooldown_key(SERVER_ID), "rt-1", 900
    )
    assert await current_mcp_access_token(SERVER_ID, refused="at-1") is None
    assert world.endpoint.presented == []


async def test_a_refresh_another_process_holds_is_waited_for(world, monkeypatch):
    monkeypatch.setattr(token_refresh, "_LOCK_POLL_SECONDS", 0.01)
    world.redis.values[f"token_refresh:{token_refresh.mcp_refresh_lock_id(SERVER_ID)}"] = "sweep"

    async def _sweep_stores_its_refresh():
        await asyncio.sleep(0.05)
        world.row.values.update(access_token_enc="at-sweep", token_expires_at=_later(600))

    stored_later = asyncio.create_task(_sweep_stores_its_refresh())
    token = await current_mcp_access_token(SERVER_ID, refused="at-1")
    await stored_later

    assert token.value == "at-sweep"
    assert world.endpoint.presented == []


async def test_a_refused_token_is_waited_for_without_spending_the_window(world, monkeypatch):
    """A refusal nothing could act on leaves the next one free to refresh."""
    monkeypatch.setattr(token_refresh, "_LOCK_POLL_SECONDS", 0.01)
    monkeypatch.setattr(token_refresh, "_LOCK_WAIT_SECONDS", 0.05)
    lock_key = f"token_refresh:{token_refresh.mcp_refresh_lock_id(SERVER_ID)}"
    world.redis.values[lock_key] = "sweep"

    assert await current_mcp_access_token(SERVER_ID, refused="at-1") is None
    assert token_refresh.mcp_forced_refresh_key(SERVER_ID) not in world.redis.values

    del world.redis.values[lock_key]
    token = await current_mcp_access_token(SERVER_ID, refused="at-1")
    assert token.value == "at-2"
    assert world.endpoint.presented == ["rt-1"]


async def test_a_refresh_backing_off_does_not_spend_the_window(world):
    cooldown_key = token_refresh.mcp_refresh_cooldown_key(SERVER_ID)
    await token_refresh.start_refresh_cooldown(cooldown_key, "rt-1", 900)
    assert await current_mcp_access_token(SERVER_ID, refused="at-1") is None
    assert token_refresh.mcp_forced_refresh_key(SERVER_ID) not in world.redis.values

    await token_refresh.clear_refresh_cooldown(cooldown_key)
    assert (await current_mcp_access_token(SERVER_ID, refused="at-1")).value == "at-2"


async def test_a_valid_token_is_sent_while_another_process_refreshes_it(world, monkeypatch):
    """Only a refused token waits for the lock holder; one still valid goes out."""

    async def _no_wait(*_args):
        raise AssertionError("a valid token waited for the lock")

    monkeypatch.setattr(token_refresh, "_await_refresh_in_flight", _no_wait)
    world.row.values["token_expires_at"] = _later(30)
    world.redis.values[f"token_refresh:{token_refresh.mcp_refresh_lock_id(SERVER_ID)}"] = "sweep"

    token = await current_mcp_access_token(SERVER_ID)

    assert token.value == "at-1"
    assert world.endpoint.presented == []


async def test_parallel_connections_refused_together_cause_one_refresh(world, monkeypatch):
    monkeypatch.setattr(token_refresh, "_LOCK_POLL_SECONDS", 0.01)

    async def _slow_answer():
        await asyncio.sleep(0.05)

    world.endpoint.while_in_flight = _slow_answer

    tokens = await asyncio.gather(*(current_mcp_access_token(SERVER_ID, refused="at-1") for _ in range(5)))

    assert [token.value for token in tokens] == ["at-2"] * 5
    assert world.endpoint.presented == ["rt-1"]


@pytest.mark.parametrize("cancel", ["task", "anyio-scope", "timeout"])
async def test_a_cancelled_connection_still_stores_the_rotated_tokens(world, cancel):
    """The authorization server rotated the refresh token: the new one exists
    nowhere else, and a cancel before the commit would force a re-authorization."""
    scope: dict = {}
    caller: asyncio.Task | None = None

    async def _cancelled_mid_answer():
        if cancel == "task":
            caller.cancel()
        elif cancel == "anyio-scope":
            scope["scope"].cancel()
        else:
            await asyncio.sleep(0.05)  # the connection's timeout expires meanwhile
        await asyncio.sleep(0)

    world.endpoint.while_in_flight = _cancelled_mid_answer

    async def _connection():
        if cancel == "task":
            await current_mcp_access_token(SERVER_ID, refused="at-1")
            return "not cancelled"
        if cancel == "timeout":
            try:
                async with asyncio.timeout(0.01):
                    await current_mcp_access_token(SERVER_ID, refused="at-1")
            except TimeoutError:
                return "timed out"
            return "not cancelled"
        with anyio.CancelScope() as cancel_scope:
            scope["scope"] = cancel_scope
            await current_mcp_access_token(SERVER_ID, refused="at-1")
            await anyio.sleep(0)
            return "not cancelled"
        return "cancelled"

    caller = asyncio.create_task(_connection())
    if cancel == "task":
        with pytest.raises(asyncio.CancelledError):
            await caller
    else:
        assert await caller == {"anyio-scope": "cancelled", "timeout": "timed out"}[cancel]

    values = world.row.values
    assert (values["access_token_enc"], values["refresh_token_enc"]) == ("at-2", "rt-2")
    assert world.endpoint.presented == ["rt-1"]
    assert not any(key.startswith("token_refresh:mcp:") for key in world.redis.values)


async def test_a_failed_refresh_logs_no_token(world, caplog):
    world.endpoint.answer = (400, {"error": "invalid_grant", "error_description": "expired"})

    with caplog.at_level(logging.DEBUG):
        assert await current_mcp_access_token(SERVER_ID, refused="at-1") is None

    assert "Failed to refresh token for MCP server" in caplog.text
    for secret in ("at-1", "rt-1", "dyn-client"):
        assert secret not in caplog.text


# ── The hook: scope refusals, retries, parallel requests ─────────────────────


async def test_a_refusal_for_a_missing_scope_is_not_a_stale_token(stored):
    """RFC 6750: refreshing would rotate the refresh token for nothing."""
    stored.answer = MCPAccessToken("at-2", _later(600))
    auth = StoredMCPOAuthToken(SERVER_ID, "at-1", _later(600))

    def _scope_refusal(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, headers={"www-authenticate": 'Bearer error="insufficient_scope", scope="mcp:admin"'}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(_scope_refusal)) as http:
        response = await http.get(MCP_URL, auth=auth)

    assert response.status_code == 401
    assert stored.asked == []
    assert auth.token == "at-1"


@pytest.mark.parametrize("answer", ["nothing", "expiring"])
async def test_a_token_about_to_expire_is_asked_for_again_only_after_a_pause(stored, monkeypatch, answer):
    """No fresh token came back: every request would otherwise read the database."""
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(mcp_oauth_tokens.time, "monotonic", lambda: clock.now)
    if answer == "expiring":
        stored.answer = MCPAccessToken("at-2", _later(5))
    server = _McpServer({"at-1", "at-2"})
    auth = StoredMCPOAuthToken(SERVER_ID, "at-1", _later(10))

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        for _ in range(3):
            await http.get(MCP_URL, auth=auth)
        assert stored.asked == [None]
        clock.now += mcp_oauth_tokens.RETRY_AFTER_SECONDS + 1
        await http.get(MCP_URL, auth=auth)

    assert stored.asked == [None, None]


async def test_parallel_requests_refused_together_share_one_replacement(stored, monkeypatch):
    server = _McpServer({"at-2"})
    stored.answer = MCPAccessToken("at-2", _later(600))
    auth = StoredMCPOAuthToken(SERVER_ID, "at-1", _later(600))

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        responses = await asyncio.gather(*(http.get(MCP_URL, auth=auth) for _ in range(5)))

    assert [response.status_code for response in responses] == [200] * 5
    assert stored.asked == ["at-1"]
    assert [token for token, _ in server.seen].count("at-2") == 5


# ── What a failed connection is diagnosed with ───────────────────────────────


async def test_the_failure_diagnosis_probes_with_the_token_the_hook_holds(monkeypatch):
    from app.tools import mcp_client

    probed: list[dict] = []

    async def _probe(url, headers=None):
        probed.append(headers)
        return None

    monkeypatch.setattr(mcp_client, "probe_auth_challenge", _probe)
    auth = StoredMCPOAuthToken(SERVER_ID, "at-1", None)
    auth._token = "at-2"  # replaced by the hook after the runtime was built
    config = mcp_client.MCPServerRuntime(
        name="remote", transport="streamable_http", url=MCP_URL, headers={"X-Tenant": "t1"}, auth=auth
    )

    assert await mcp_client.diagnose_auth_after_connect_failure(config) is None
    assert probed == [{"X-Tenant": "t1", "Authorization": "Bearer at-2"}]


async def test_a_connection_holding_a_token_through_the_hook_has_a_credential(monkeypatch):
    from app.tools import mcp_client

    async def _no_probe(_url):
        raise AssertionError("probed for metadata while a credential is held")

    monkeypatch.setattr(mcp_oauth_discovery, "find_protected_resource_metadata", _no_probe)
    config = mcp_client.MCPServerRuntime(
        name="remote", transport="streamable_http", url=MCP_URL, auth=StoredMCPOAuthToken(SERVER_ID, "at-1", None)
    )

    assert await mcp_client._pending_authorization_metadata(config) is None


async def test_the_refresh_a_connection_asks_for_goes_through_the_servers_relay_group(world, monkeypatch):
    """As the sweep's: a server routed through a relay group is never reached directly."""
    from app.core.egress import EgressRoute, current_route
    from app.services import relays

    route = EgressRoute(group_id="relay-group-1", source_type="mcp_server", source_id=SERVER_ID)
    world.row.values["relay_group_id"] = "relay-group-1"

    async def _route(_db, server):
        return route if getattr(server, "relay_group_id", None) == "relay-group-1" else None

    monkeypatch.setattr(relays, "mcp_server_egress_route", _route)
    routes_seen: list = []

    async def _record_route():
        routes_seen.append(current_route())

    world.endpoint.while_in_flight = _record_route

    token = await current_mcp_access_token(SERVER_ID, refused="at-1")

    assert token.value == "at-2"
    assert routes_seen == [route]
