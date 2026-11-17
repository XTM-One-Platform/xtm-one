"""Client ID Metadata Documents and loopback redirects at the MCP OAuth server.

Pins ``app/services/mcp_oauth_clients.py`` and its wiring in
``app/api/v1/mcp_oauth.py``:

- which ``client_id`` names a document, and what a document must say (a
  ``client_name`` without control or formatting characters included);
- the fetch: one resolution, every address public, the connection pinned to
  the validated address (DNS rebinding), no proxy, no redirect, raw bytes
  bounded, JSON only; one constant answer whatever the reason;
- the cache and the budgets: refusals cached, the per-IP, per-host and
  platform budgets spent on cache misses only and refused while Redis cannot
  count them, a known client served from its stale entry instead of a 429;
- the authorize endpoints resolve a document client, show its host as the
  headline and where the code goes, check the request before any fetch,
  honour the ``mcp_cimd_enabled`` switch, and match an ``http`` loopback
  redirect registered without a port whatever its port;
- the token endpoint treats a document client as public, stops refreshing it
  once the switch is off or its document was refused, and ends its refresh
  family after 90 days; a registered client still proves its secret;
- the authorization server metadata advertises the switch;
- ``MCP_DCR_RATE_LIMIT`` sizes the registration budget (see
  ``test_mcp_dcr_protection.py``).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.v1 import mcp_oauth
from app.core.config import settings
from app.services import mcp_oauth_clients
from app.services.mcp_oauth_clients import (
    CLIENT_METADATA_REFUSED,
    MAX_DOCUMENT_BYTES,
    STALE_GRACE_SECONDS,
    ClientMetadataError,
    is_metadata_document_url,
    redirect_uri_matches,
    resolve_client_metadata,
    validate_metadata_document,
)

CLIENT_ID = "https://claude.ai/oauth/claude-code-client-metadata"


def _document(**overrides) -> dict:
    document = {
        "client_id": CLIENT_ID,
        "client_name": "Claude Code",
        "client_uri": "https://claude.ai",
        "redirect_uris": ["http://localhost/callback", "http://127.0.0.1/callback"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    document.update(overrides)
    return {k: v for k, v in document.items() if v is not None}


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/mcp/oauth/authorize",
            "headers": [],
            "query_string": b"",
            "client": ("203.0.113.5", 40000),
        }
    )


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.ttls[key] = ex


@pytest.fixture
def redis(monkeypatch):
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr(mcp_oauth_clients, "redis_client", _client)
    return fake


class _Budgets(list):
    """The budgets spent, by name; ``exhausted`` makes the next spend a 429."""

    exhausted = False
    calls: list[dict]


@pytest.fixture
def budget(monkeypatch):
    spent = _Budgets()
    spent.calls = []

    async def _rate_limit(_request, name, *, limit, window_seconds, bucket=None, fail_closed=None):
        spent.calls.append({"name": name, "bucket": bucket, "fail_closed": fail_closed})
        if spent.exhausted:
            raise HTTPException(status_code=429, detail="Too many requests")
        spent.append(name)

    monkeypatch.setattr(mcp_oauth_clients, "enforce_rate_limit", _rate_limit)
    return spent


PUBLIC_ADDRESS = "93.184.215.14"  # public: a TEST-NET address is not global


@pytest.fixture
def served(monkeypatch):
    """Serve documents from a mock transport; the host resolves to a public address."""
    responses: dict[str, httpx.Response] = {}
    requested: list[str] = []
    connections: list[dict] = []
    clients: list[dict] = []
    resolution = SimpleNamespace(addresses=[PUBLIC_ADDRESS], error=None)

    def handler(request: httpx.Request) -> httpx.Response:
        # The URL the document was asked at: the connection goes to an address.
        url = f"https://{request.headers['host']}{request.url.path}"
        requested.append(url)
        connections.append({"host": request.url.host, "sni": request.extensions.get("sni_hostname")})
        response = responses.get(url, httpx.Response(404))
        if not hasattr(response, "_content"):
            return response  # already a stream
        body = response.content

        async def _stream():
            yield body

        # Served as a stream, as the network serves it: an httpx.Response
        # built from bytes counts as read, and the fetch reads raw bytes.
        return httpx.Response(response.status_code, headers=response.headers, content=_stream())

    real_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        clients.append(dict(kwargs))
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    async def _getaddrinfo(_host, port):
        if resolution.error is not None:
            raise resolution.error
        family = {4: socket.AF_INET, 6: socket.AF_INET6}
        return [
            (family[ipaddress.ip_address(a).version], socket.SOCK_STREAM, 6, "", (a, port))
            for a in resolution.addresses
        ]

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    monkeypatch.setattr(mcp_oauth_clients, "_getaddrinfo", _getaddrinfo)
    return SimpleNamespace(
        responses=responses,
        requested=requested,
        connections=connections,
        clients=clients,
        resolution=resolution,
    )


# ── Which client_id is a document ────────────────────────────────────────────


@pytest.mark.parametrize(
    "client_id",
    [
        CLIENT_ID,
        "https://vscode.dev/oauth/client-metadata.json",
        "https://example.com:8443/clients/mcp",
    ],
)
def test_an_https_url_with_a_path_is_a_document(client_id):
    assert is_metadata_document_url(client_id)


@pytest.mark.parametrize(
    "client_id",
    [
        "mcp-0123456789abcdef01234567",
        "http://claude.ai/oauth/client",
        "https://claude.ai",
        "https://claude.ai/",
        "https://user:pass@claude.ai/client",
        "https://claude.ai/client?x=1",
        "https://claude.ai/client#frag",
        "https://claude.ai/a/../client",
        "https://claude.ai/" + "a" * 300,
        "https://claude.ai/with space",
        # A control character would reach the logs of an unauthenticated request.
        "https://claude.ai/client\x1b[31m",
        "https://claude.ai/client\x00",
        # A Unicode host can spell a lookalike of another client's host.
        "https://cl\u0430ude.ai/oauth/client",
    ],
)
def test_anything_else_is_not(client_id):
    assert not is_metadata_document_url(client_id)


# ── Redirect URIs ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "requested",
    [
        "http://localhost:3118/callback",
        "http://localhost/callback",
        "http://127.0.0.1:50211/callback",
        "http://[::1]:9000/callback",
    ],
)
def test_an_http_loopback_redirect_registered_without_a_port_matches_any_port(requested):
    registered = ["http://localhost/callback", "http://127.0.0.1/callback", "http://[::1]/callback"]
    assert redirect_uri_matches(requested, registered)


@pytest.mark.parametrize(
    "requested",
    [
        "http://localhost:3118/other",
        "https://localhost:3118/callback",
        "http://127.0.0.1:3118/callback",
        "http://evil.example:3118/callback",
        "http://localhost:3118/callback?x=1",
        "http://localhost:notaport/callback",
    ],
)
def test_anything_but_the_port_must_match(requested):
    assert not redirect_uri_matches(requested, ["http://localhost/callback"])


@pytest.mark.parametrize(
    ("requested", "registered"),
    [
        # A registration that pinned a port keeps it.
        ("http://localhost:4000/callback", "http://localhost:3000/callback"),
        ("http://127.0.0.1:4000/callback", "http://127.0.0.1:3000/callback"),
        # RFC 8252 §7.3 is about http loopback redirects only.
        ("https://localhost:4000/callback", "https://localhost/callback"),
    ],
    ids=["pinned-localhost", "pinned-ipv4", "https"],
)
def test_a_pinned_or_https_loopback_redirect_matches_exactly(requested, registered):
    assert not redirect_uri_matches(requested, [registered])
    assert redirect_uri_matches(registered, [registered])


def test_a_non_loopback_redirect_matches_exactly():
    assert redirect_uri_matches("https://app.example.com/cb", ["https://app.example.com/cb"])
    assert not redirect_uri_matches("https://app.example.com:8443/cb", ["https://app.example.com/cb"])
    assert not redirect_uri_matches("https://app.example.com/cb", [])
    assert not redirect_uri_matches("https://app.example.com/cb", None)


# ── What a document must say ─────────────────────────────────────────────────


def test_a_valid_document_keeps_its_name_and_redirects():
    fields = validate_metadata_document(CLIENT_ID, _document())
    assert fields == {
        "client_name": "Claude Code",
        "client_uri": "https://claude.ai",
        "redirect_uris": ["http://localhost/callback", "http://127.0.0.1/callback"],
    }


@pytest.mark.parametrize(
    "document",
    [
        "not an object",
        _document(client_id="https://evil.example/client"),
        _document(redirect_uris=[]),
        _document(redirect_uris="http://localhost/callback"),
        _document(redirect_uris=["http://app.example.com/cb"]),
        _document(redirect_uris=["https://app.example.com/cb#x"]),
        _document(redirect_uris=["ftp://localhost/cb"]),
        _document(token_endpoint_auth_method="client_secret_basic"),
        _document(token_endpoint_auth_method="private_key_jwt"),
        _document(grant_types=["client_credentials"]),
        _document(response_types=["token"]),
        _document(client_name=7),
        # Required (MCP 2025-11-25, Client ID Metadata Documents).
        {k: v for k, v in _document().items() if k != "client_name"},
        _document(client_name="   "),
        # What makes a name read as another one.
        _document(client_name="Claude Code\u202e"),
        _document(client_name="Claude\u200bCode"),
        _document(client_name="Claude\x07Code"),
    ],
)
def test_a_document_this_server_does_not_accept_is_refused(document):
    with pytest.raises(ClientMetadataError) as exc_info:
        validate_metadata_document(CLIENT_ID, document)
    assert exc_info.value.status_code == 400
    # Why is logged, never answered.
    assert exc_info.value.detail == CLIENT_METADATA_REFUSED


def test_a_client_name_is_normalised():
    fields = validate_metadata_document(CLIENT_ID, _document(client_name="  \uff23laude \n Code  "))
    assert fields["client_name"] == "Claude Code"


# ── Fetch and cache ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_document_is_fetched_once_and_cached_for_its_max_age(redis, budget, served):
    served.responses[CLIENT_ID] = httpx.Response(
        200, json=_document(), headers={"Cache-Control": "public, max-age=1800"}
    )
    client = await resolve_client_metadata(CLIENT_ID, request=_request())
    again = await resolve_client_metadata(CLIENT_ID, request=_request())

    assert served.requested == [CLIENT_ID]
    # Each budget once: the second read is cached.
    assert budget == ["mcp-oauth-cimd-fetch", "mcp-oauth-cimd-fetch-host", "mcp-oauth-cimd-fetch-platform"]
    assert client.client_id == again.client_id == CLIENT_ID
    # What the client says it is; the consent page shows it after its host.
    assert client.client_name == "Claude Code"
    assert client.client_secret_hash is None
    assert client.token_endpoint_auth_method == "none"
    # Fresh for its max-age, then kept to serve stale.
    assert list(redis.ttls.values()) == [1800 + STALE_GRACE_SECONDS]


@pytest.mark.asyncio
async def test_the_budgets_refuse_while_redis_cannot_count_them_and_one_host_has_its_own(
    redis, budget, served
):
    served.responses[CLIENT_ID] = httpx.Response(200, json=_document())
    await resolve_client_metadata(CLIENT_ID, request=_request())
    assert budget.calls == [
        {"name": "mcp-oauth-cimd-fetch", "bucket": None, "fail_closed": True},
        {"name": "mcp-oauth-cimd-fetch-host", "bucket": "claude.ai", "fail_closed": True},
        {"name": "mcp-oauth-cimd-fetch-platform", "bucket": "platform", "fail_closed": True},
    ]


@pytest.mark.asyncio
async def test_the_fetch_connects_to_the_address_it_validated(redis, budget, served):
    """Resolved once: a host cannot answer a public address to the check and a
    private one to the connection (DNS rebinding). No proxy from the
    environment, TLS still verified against the host name."""
    served.responses[CLIENT_ID] = httpx.Response(200, json=_document())
    await resolve_client_metadata(CLIENT_ID, request=_request())
    assert served.connections == [{"host": PUBLIC_ADDRESS, "sni": "claude.ai"}]
    assert [client["trust_env"] for client in served.clients] == [False]
    assert [client["follow_redirects"] for client in served.clients] == [False]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        [PUBLIC_ADDRESS, "127.0.0.1"],
        ["169.254.169.254"],
        ["10.0.0.7"],
        ["::1"],
        # NAT64 (RFC 6052): delivered to 127.0.0.1, although ``is_global``.
        ["64:ff9b::7f00:1"],
        [],
    ],
    ids=["loopback", "public-then-loopback", "metadata", "private", "ipv6-loopback", "nat64-loopback", "none"],
)
async def test_a_host_resolving_to_any_private_address_is_never_fetched(redis, budget, served, addresses):
    served.resolution.addresses = addresses
    with pytest.raises(ClientMetadataError) as exc_info:
        await resolve_client_metadata(CLIENT_ID, request=_request())
    assert served.requested == []
    assert exc_info.value.detail == CLIENT_METADATA_REFUSED
    for address in addresses:
        assert address not in exc_info.value.detail


def test_a_nat64_address_is_judged_by_its_ipv4():
    from app.services.web_fetch import _is_safe_address

    assert not _is_safe_address(ipaddress.ip_address("64:ff9b::a9fe:a9fe"))
    assert _is_safe_address(ipaddress.ip_address("64:ff9b::808:808"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cache_control, seconds",
    [(None, 3600), ("max-age=10", 300), ("max-age=999999", 3600), ("no-store", 300)],
)
async def test_the_cache_lifetime_is_bounded(redis, budget, served, cache_control, seconds):
    headers = {"Cache-Control": cache_control} if cache_control else {}
    served.responses[CLIENT_ID] = httpx.Response(200, json=_document(), headers=headers)
    await resolve_client_metadata(CLIENT_ID, request=_request())
    assert list(redis.ttls.values()) == [seconds + STALE_GRACE_SECONDS]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "https://evil.example/doc"}),
        httpx.Response(404),
        httpx.Response(200, content=b"not json", headers={"Content-Type": "application/json"}),
        httpx.Response(200, content=b"{}", headers={"Content-Type": "text/html"}),
        httpx.Response(200, content=b" " * (MAX_DOCUMENT_BYTES + 1), headers={"Content-Type": "application/json"}),
        httpx.Response(
            200,
            content=b"{}",
            headers={"Content-Type": "application/json", "Content-Length": str(MAX_DOCUMENT_BYTES + 1)},
        ),
        httpx.Response(200, content=b"\x1f\x8b", headers={"Content-Type": "application/json", "Content-Encoding": "gzip"}),
        httpx.Response(200, json=_document(client_id="https://evil.example/client")),
    ],
    ids=["redirect", "404", "not-json", "not-json-media-type", "too-large", "declared-too-large", "encoded", "wrong-client-id"],
)
async def test_a_refused_document_is_refused_and_remembered(redis, budget, served, response):
    served.responses[CLIENT_ID] = response
    with pytest.raises(ClientMetadataError):
        await resolve_client_metadata(CLIENT_ID, request=_request())
    with pytest.raises(ClientMetadataError):
        await resolve_client_metadata(CLIENT_ID, request=_request())

    # Fetched once, the redirect never followed; the refusal cached briefly.
    assert served.requested == [CLIENT_ID]
    assert list(redis.ttls.values()) == [mcp_oauth_clients.REFUSAL_CACHE_SECONDS]


@pytest.mark.asyncio
async def test_a_known_client_is_served_stale_rather_than_refused(redis, budget, served, monkeypatch):
    """Anyone can spend the platform budget with new URLs: a client already
    known never depends on it, nor on its document answering right now."""
    served.responses[CLIENT_ID] = httpx.Response(200, json=_document())
    await resolve_client_metadata(CLIENT_ID, request=_request())
    later = time.time() + 7200  # past its max-age
    monkeypatch.setattr(mcp_oauth_clients.time, "time", lambda: later)

    budget.exhausted = True
    assert (await resolve_client_metadata(CLIENT_ID, request=_request())).client_name == "Claude Code"

    budget.exhausted = False
    served.responses[CLIENT_ID] = httpx.Response(503)
    assert (await resolve_client_metadata(CLIENT_ID, request=_request())).client_name == "Claude Code"
    served.resolution.error = OSError("resolver down")
    assert (await resolve_client_metadata(CLIENT_ID, request=_request())).client_name == "Claude Code"


@pytest.mark.asyncio
async def test_a_known_client_whose_document_is_now_refused_is_refused(redis, budget, served, monkeypatch):
    served.responses[CLIENT_ID] = httpx.Response(200, json=_document())
    await resolve_client_metadata(CLIENT_ID, request=_request())
    later = time.time() + 7200
    monkeypatch.setattr(mcp_oauth_clients.time, "time", lambda: later)

    # The document now describes another client: refused, not served stale.
    served.responses[CLIENT_ID] = httpx.Response(200, json=_document(client_id="https://evil.example/client"))
    with pytest.raises(ClientMetadataError):
        await resolve_client_metadata(CLIENT_ID, request=_request())
    assert await mcp_oauth_clients.cached_refusal(CLIENT_ID) is True


@pytest.mark.asyncio
async def test_an_unknown_client_gets_the_429_when_no_fetch_may_be_spent(redis, budget, served):
    budget.exhausted = True
    with pytest.raises(HTTPException) as exc_info:
        await resolve_client_metadata(CLIENT_ID, request=_request())
    assert exc_info.value.status_code == 429
    assert served.requested == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(418),
        httpx.Response(200, content=b"[]", headers={"Content-Type": "application/json"}),
        httpx.Response(200, json=_document(redirect_uris=[])),
    ],
    ids=["status", "not-an-object", "invalid-document"],
)
async def test_one_answer_whatever_the_reason(redis, budget, served, response):
    """No oracle: the answer says neither whether the URL answered, nor what."""
    served.responses[CLIENT_ID] = response
    with pytest.raises(ClientMetadataError) as exc_info:
        await resolve_client_metadata(CLIENT_ID, request=_request())
    assert exc_info.value.detail == CLIENT_METADATA_REFUSED
    with pytest.raises(ClientMetadataError) as cached:
        await resolve_client_metadata(CLIENT_ID, request=_request())
    assert cached.value.detail == CLIENT_METADATA_REFUSED


@pytest.mark.asyncio
async def test_a_server_that_trickles_bytes_is_cut_off(redis, budget, served, monkeypatch):
    """The timeout bounds the whole fetch, not each read."""
    monkeypatch.setattr(mcp_oauth_clients, "FETCH_TIMEOUT_SECONDS", 0.2)

    async def _trickle():
        for _ in range(100):
            await asyncio.sleep(0.02)
            yield b" "

    served.responses[CLIENT_ID] = httpx.Response(
        200, content=_trickle(), headers={"Content-Type": "application/json"}
    )
    with pytest.raises(ClientMetadataError):
        await asyncio.wait_for(resolve_client_metadata(CLIENT_ID, request=_request()), timeout=2)


# ── The authorize endpoints ──────────────────────────────────────────────────


class _NoRowsDB:
    async def execute(self, _statement):
        return SimpleNamespace(scalar_one_or_none=lambda: None)


@pytest.fixture
def document_client(monkeypatch):
    async def _resolve(client_id, *, request):
        return mcp_oauth_clients._transient_client(client_id, validate_metadata_document(client_id, _document()))

    async def _branding(_db):
        return SimpleNamespace(name="XTM One")

    monkeypatch.setattr(mcp_oauth, "resolve_client_metadata", _resolve)
    monkeypatch.setattr(mcp_oauth, "get_branding", _branding)
    monkeypatch.setattr(settings, "mcp_cimd_enabled", True)


async def _authorize(redirect_uri: str, client_id: str = CLIENT_ID):
    return await mcp_oauth.authorize_get(
        _request(),
        response_type="code",
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge="x" * 43,
        code_challenge_method="S256",
        state="s",
        resource="",
        db=_NoRowsDB(),
    )


@pytest.mark.asyncio
async def test_a_document_client_reaches_the_consent_page_with_its_host(document_client):
    response = await _authorize("http://localhost:3118/callback")
    page = response.body.decode()
    # The host the document came from is the headline; the name is a claim.
    assert "<strong>claude.ai</strong> wants to access" in page
    assert "It calls itself &ldquo;Claude Code&rdquo;." in page
    # Where the code goes, flagged as this computer.
    assert "sent to <strong>localhost</strong> &mdash; an application on this computer" in page
    assert "http://localhost:3118/callback" in page


@pytest.mark.asyncio
async def test_a_client_name_cannot_spell_another_host(monkeypatch):
    async def _resolve(client_id, *, request):
        fields = validate_metadata_document(client_id, _document(client_name="Claude Code (claude.ai)"))
        return mcp_oauth_clients._transient_client(client_id, fields)

    async def _branding(_db):
        return SimpleNamespace(name="XTM One")

    evil = "https://evil.example/claude-code-client-metadata"

    def _evil_document():
        return _document(client_id=evil, client_name="Claude Code (claude.ai)")

    async def _resolve_evil(client_id, *, request):
        return mcp_oauth_clients._transient_client(client_id, validate_metadata_document(client_id, _evil_document()))

    monkeypatch.setattr(mcp_oauth, "resolve_client_metadata", _resolve_evil)
    monkeypatch.setattr(mcp_oauth, "get_branding", _branding)
    monkeypatch.setattr(settings, "mcp_cimd_enabled", True)
    page = (await _authorize("http://localhost:3118/callback", client_id=evil)).body.decode()
    assert "<strong>evil.example</strong> wants to access" in page
    assert "<strong>claude.ai</strong>" not in page
    assert "It calls itself &ldquo;Claude Code (claude.ai)&rdquo;." in page


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("redirect_uri", "code_challenge_method"),
    [("http://evil.example/callback", "S256"), ("http://localhost:3118/callback", "plain")],
    ids=["redirect-that-may-not-receive-a-code", "unsupported-pkce"],
)
async def test_a_request_that_cannot_succeed_fetches_no_document(monkeypatch, redirect_uri, code_challenge_method):
    fetched: list[str] = []

    async def _resolve(client_id, *, request):
        fetched.append(client_id)

    monkeypatch.setattr(mcp_oauth, "resolve_client_metadata", _resolve)
    monkeypatch.setattr(settings, "mcp_cimd_enabled", True)
    with pytest.raises(HTTPException) as exc_info:
        await mcp_oauth.authorize_get(
            _request(),
            response_type="code",
            client_id=CLIENT_ID,
            redirect_uri=redirect_uri,
            code_challenge="x" * 43,
            code_challenge_method=code_challenge_method,
            state="s",
            resource="",
            db=_NoRowsDB(),
        )
    assert exc_info.value.status_code == 400
    assert fetched == []


@pytest.mark.asyncio
async def test_a_document_client_redirect_it_does_not_list_is_refused(document_client):
    with pytest.raises(HTTPException) as exc_info:
        await _authorize("https://evil.example/callback")
    assert exc_info.value.detail == "redirect_uri not registered"


@pytest.mark.asyncio
async def test_documents_switched_off_refuse_a_document_client(document_client, monkeypatch):
    monkeypatch.setattr(settings, "mcp_cimd_enabled", False)
    with pytest.raises(HTTPException) as exc_info:
        await _authorize("http://localhost:3118/callback")
    assert exc_info.value.status_code == 400
    assert "disabled" in exc_info.value.detail


@pytest.mark.asyncio
async def test_a_registered_client_is_still_looked_up(document_client):
    with pytest.raises(HTTPException) as exc_info:
        await _authorize("http://localhost:3118/callback", client_id="mcp-0123456789abcdef01234567")
    assert exc_info.value.detail == "Unknown client_id"


@pytest.mark.asyncio
async def test_a_registered_loopback_redirect_matches_whatever_its_port(monkeypatch):
    registered = SimpleNamespace(
        client_id="mcp-abc", client_name="Cursor", redirect_uris=["http://127.0.0.1/callback"]
    )

    async def _load(_db, _client_id, _request, **_checks):
        return registered

    async def _branding(_db):
        return SimpleNamespace(name="XTM One")

    monkeypatch.setattr(mcp_oauth, "_load_client", _load)
    monkeypatch.setattr(mcp_oauth, "get_branding", _branding)
    response = await _authorize("http://127.0.0.1:61000/callback", client_id="mcp-abc")
    assert response.status_code == 200


# ── The token endpoint ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_document_client_is_a_public_client_at_the_token_endpoint(monkeypatch):
    monkeypatch.setattr(settings, "mcp_cimd_enabled", True)
    assert await mcp_oauth._authenticate_token_client(_NoRowsDB(), CLIENT_ID, {}) is None

    monkeypatch.setattr(settings, "mcp_cimd_enabled", False)
    refused = await mcp_oauth._authenticate_token_client(_NoRowsDB(), CLIENT_ID, {})
    assert refused.status_code == 401
    assert json.loads(refused.body) == {"error": "invalid_client"}


@pytest.mark.asyncio
async def test_a_document_client_whose_document_was_refused_gets_no_token(redis, monkeypatch):
    """No row to delete: a refused document is how such a client is revoked."""
    monkeypatch.setattr(settings, "mcp_cimd_enabled", True)
    await mcp_oauth_clients._remember(CLIENT_ID, {"refused": "its client_id is not the URL"}, 60)
    refused = await mcp_oauth._authenticate_token_client(_NoRowsDB(), CLIENT_ID, {})
    assert refused.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(("age_days", "granted"), [(1, True), (89, True), (91, False)])
async def test_a_document_clients_refresh_family_ends_after_90_days(monkeypatch, age_days, granted):
    monkeypatch.setattr(settings, "mcp_cimd_enabled", True)
    started = time.time() - age_days * 86400
    stored: list[dict] = []

    async def _consume(_token):
        return {"user_id": "u", "client_id": CLIENT_ID, "resource": None, "family_started_at": started}

    async def _store(token_hash, **kwargs):
        stored.append(kwargs)

    monkeypatch.setattr(mcp_oauth, "_consume_refresh_token", _consume)
    monkeypatch.setattr(mcp_oauth, "_store_refresh_token", _store)
    response = await mcp_oauth._handle_refresh_token({"refresh_token": "r", "client_id": CLIENT_ID}, _NoRowsDB())
    if granted:
        assert response.status_code == 200
        # The family keeps its start across the rotation.
        assert [entry["family_started_at"] for entry in stored] == [started]
    else:
        assert response.status_code == 400
        assert json.loads(response.body)["error"] == "invalid_grant"
        assert stored == []


def test_a_document_clients_last_refresh_token_lives_to_the_family_end_only():
    started = time.time() - 80 * 86400
    deadline = mcp_oauth._refresh_family_deadline(CLIENT_ID, started)
    assert deadline == pytest.approx(started + 90 * 86400)
    assert mcp_oauth._refresh_family_deadline("mcp-registered", started) is None


@pytest.mark.asyncio
async def test_an_unknown_registered_client_is_refused_at_the_token_endpoint():
    refused = await mcp_oauth._authenticate_token_client(_NoRowsDB(), "mcp-unknown", {})
    assert refused.status_code == 401


@pytest.mark.asyncio
async def test_a_document_client_exchanges_its_code(monkeypatch):
    monkeypatch.setattr(settings, "mcp_cimd_enabled", True)
    verifier = "v" * 64
    stored: list[dict] = []

    async def _consume(_code):
        return {
            "client_id": CLIENT_ID,
            "redirect_uri": "http://localhost:3118/callback",
            "user_id": "8f0e0e9c-63d5-4a61-9a5e-0b7f1c2d3e4f",
            "code_challenge": mcp_oauth._hash_code_verifier(verifier),
            "code_challenge_method": "S256",
            "resource": "https://xtm.example.com/mcp/xtm",
        }

    async def _store(token_hash, **kwargs):
        stored.append(kwargs)

    monkeypatch.setattr(mcp_oauth, "_consume_auth_code", _consume)
    monkeypatch.setattr(mcp_oauth, "_store_refresh_token", _store)
    response = await mcp_oauth._handle_authorization_code(
        {
            "code": "c",
            "code_verifier": verifier,
            "client_id": CLIENT_ID,
            "redirect_uri": "http://localhost:3118/callback",
        },
        _NoRowsDB(),
    )
    assert response.status_code == 200
    assert json.loads(response.body)["access_token"]
    (entry,) = stored
    assert entry.pop("family_started_at") == pytest.approx(time.time(), abs=5)
    assert entry == {
        "user_id": "8f0e0e9c-63d5-4a61-9a5e-0b7f1c2d3e4f",
        "client_id": CLIENT_ID,
        "resource": "https://xtm.example.com/mcp/xtm",
    }


# ── Metadata ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_the_authorization_server_advertises_the_switch(monkeypatch, enabled):
    monkeypatch.setattr(settings, "mcp_cimd_enabled", enabled)
    response = await mcp_oauth.authorization_server_metadata("")
    metadata = json.loads(response.body)
    assert metadata["client_id_metadata_document_supported"] is enabled
    # Claude's CIMD client authenticates as a public client.
    assert "none" in metadata["token_endpoint_auth_methods_supported"]
