"""OAuth2 token grants (client credentials, password): fetching, caching, client authentication."""

from __future__ import annotations

import asyncio
import base64
import socket
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from app.integrations import oauth_token_grants as cc
from app.integrations.oauth_token_grants import (
    TokenGrantConfig,
    TokenGrantError,
    clear_token_cache,
    get_grant_token,
)
from app.services import web_fetch

_REAL_ASYNC_CLIENT = httpx.AsyncClient

CONFIG = TokenGrantConfig(
    token_url="https://auth.example.test/oauth/token",
    client_id="client-1",
    client_secret="s3cr3t",
    scopes=("items:read",),
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    clear_token_cache()

    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        ip = {"auth.example.test": "93.184.216.34", "localhost": "127.0.0.1"}.get(host, host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", fake_getaddrinfo)
    yield
    clear_token_cache()


def _install(monkeypatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(recording)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


def _form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


async def test_token_is_fetched_with_basic_client_auth_and_cached(monkeypatch):
    seen = _install(
        monkeypatch,
        lambda r: httpx.Response(200, json={"access_token": "at-1", "token_type": "Bearer", "expires_in": 3600}),
    )
    assert await get_grant_token(CONFIG) == "at-1"
    assert await get_grant_token(CONFIG) == "at-1"
    assert len(seen) == 1
    request = seen[0]
    expected = "Basic " + base64.b64encode(b"client-1:s3cr3t").decode()
    assert request.headers["Authorization"] == expected
    assert _form(request) == {"grant_type": "client_credentials", "scope": "items:read"}


async def test_falls_back_to_credentials_in_the_body(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if "Authorization" in request.headers:
            return httpx.Response(401, json={"error": "invalid_client"})
        return httpx.Response(200, json={"access_token": "at-post", "expires_in": 60})

    seen = _install(monkeypatch, handler)
    assert await get_grant_token(CONFIG) == "at-post"
    assert _form(seen[1])["client_secret"] == "s3cr3t"
    # The method that worked is remembered: a forced refresh sends body credentials only.
    await get_grant_token(CONFIG, force_refresh=True)
    assert "Authorization" not in seen[2].headers
    assert len(seen) == 3


async def test_expired_tokens_are_renewed(monkeypatch):
    tokens = iter(["short", "next"])
    seen = _install(
        monkeypatch,
        lambda r: httpx.Response(200, json={"access_token": next(tokens), "expires_in": 1}),
    )
    assert await get_grant_token(CONFIG) == "short"
    # expires_in=1 → skew = 0.5 s; pretend a second has passed.
    real_monotonic = cc.time.monotonic
    monkeypatch.setattr(cc.time, "monotonic", lambda: real_monotonic() + 5)
    assert await get_grant_token(CONFIG) == "next"
    assert len(seen) == 2


async def test_concurrent_callers_share_one_request(monkeypatch):
    seen: list[httpx.Request] = []
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        await gate.wait()
        return httpx.Response(200, json={"access_token": "shared", "expires_in": 3600})

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    tasks = [asyncio.create_task(get_grant_token(CONFIG)) for _ in range(5)]
    await asyncio.sleep(0.05)
    gate.set()
    assert await asyncio.gather(*tasks) == ["shared"] * 5
    assert len(seen) == 1


@pytest.mark.parametrize(
    ("response", "match"),
    [
        (httpx.Response(200, json={"token_type": "bearer"}), "no access_token"),
        (httpx.Response(200, json={"access_token": "x", "token_type": "mac"}), "only Bearer"),
        (httpx.Response(400, json={"error": "invalid_scope", "error_description": "bad scope"}), r"HTTP 400 \(invalid_scope: bad scope\)"),
    ],
)
async def test_unusable_answers(monkeypatch, response, match):
    _install(monkeypatch, lambda r: response)
    with pytest.raises(TokenGrantError, match=match):
        await get_grant_token(CONFIG)


async def test_form_encoded_token_response(monkeypatch):
    _install(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            content=b"access_token=legacy&expires_in=100",
            headers={"content-type": "application/x-www-form-urlencoded"},
        ),
    )
    assert await get_grant_token(CONFIG) == "legacy"


async def test_token_url_behind_the_destination_guard(monkeypatch):
    seen = _install(monkeypatch, lambda r: httpx.Response(200, json={"access_token": "x"}))
    local = TokenGrantConfig(token_url="http://localhost:9000/token", client_id="a", client_secret="b")
    with pytest.raises(TokenGrantError, match="not allowed"):
        await get_grant_token(local)
    assert seen == []


def test_error_messages_never_carry_the_secret():
    with pytest.raises(TokenGrantError) as excinfo:
        TokenGrantConfig.client_credentials("https://auth.test/token", "id", "", None)
    assert "client secret" in str(excinfo.value)
    config = TokenGrantConfig.client_credentials(" https://auth.test/token ", " id ", " sec ", "a b  c")
    assert config.scopes == ("a", "b", "c")
    assert "sec" not in config.cache_key


PASSWORD = TokenGrantConfig.password_grant(
    "https://auth.example.test/oauth/token",
    "alice:pa:ss word",
    client_id="cli",
    scopes="read write",
)


async def test_password_grant_sends_the_user_credentials(monkeypatch):
    seen = _install(monkeypatch, lambda r: httpx.Response(200, json={"access_token": "user-token", "expires_in": 600}))
    assert await get_grant_token(PASSWORD) == "user-token"
    assert await get_grant_token(PASSWORD) == "user-token"
    assert len(seen) == 1
    # A public client (no secret): its id goes in the body, no Basic header.
    assert "Authorization" not in seen[0].headers
    assert _form(seen[0]) == {
        "grant_type": "password",
        "username": "alice",
        "password": "pa:ss word",
        "scope": "read write",
        "client_id": "cli",
    }


async def test_password_grant_with_a_confidential_client(monkeypatch):
    config = TokenGrantConfig.password_grant(
        "https://auth.example.test/oauth/token", "bob:pw", client_id="cli", client_secret="sec"
    )
    seen = _install(monkeypatch, lambda r: httpx.Response(200, json={"access_token": "t"}))
    await get_grant_token(config)
    assert seen[0].headers["Authorization"] == "Basic " + base64.b64encode(b"cli:sec").decode()
    assert _form(seen[0]) == {"grant_type": "password", "username": "bob", "password": "pw"}


def test_password_grant_needs_a_username_and_a_client_id_for_a_secret():
    with pytest.raises(TokenGrantError, match="username"):
        TokenGrantConfig.password_grant("https://auth.test/token", ":pw")
    with pytest.raises(TokenGrantError, match="no client ID"):
        TokenGrantConfig.password_grant("https://auth.test/token", "u:p", client_secret="s")
    # Another user or password is another token.
    other = TokenGrantConfig.password_grant("https://auth.example.test/oauth/token", "alice:other", client_id="cli")
    assert other.cache_key != PASSWORD.cache_key
    assert "pa:ss" not in repr(PASSWORD)


async def test_the_client_certificate_reaches_the_transport(monkeypatch):
    import ssl

    captured: dict[str, Any] = {}

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        captured.update(kwargs)
        kwargs["transport"] = httpx.MockTransport(lambda r: httpx.Response(200, json={"access_token": "mtls"}))
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr(web_fetch, "_environment_proxies", lambda: False)
    context = ssl.create_default_context()
    config = TokenGrantConfig.client_credentials(
        "https://auth.example.test/oauth/token", "a", "b", ssl_context=context, certificate_id="cert-1"
    )
    assert await get_grant_token(config) == "mtls"
    transport = captured["transport"]
    assert isinstance(transport, web_fetch.GuardedTransport)
    assert transport._pool._ssl_context is context
    # A certificate change is a different client: its token is not reused.
    assert config.cache_key != TokenGrantConfig.client_credentials(
        "https://auth.example.test/oauth/token", "a", "b"
    ).cache_key


async def test_a_tls_failure_says_so(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[SSL: TLSV13_ALERT_CERTIFICATE_REQUIRED] tlsv13 alert certificate required")

    _install(monkeypatch, handler)
    with pytest.raises(TokenGrantError, match="TLS handshake with the token endpoint failed"):
        await get_grant_token(CONFIG)
