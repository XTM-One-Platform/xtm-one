"""An unusable OIDC discovery document is a login-page error, not a 500.

The Discovery URL was the Keycloak realm root, which answers 200 with a
realm-info body that has no ``authorization_endpoint``.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx
import pytest

import app.core.sso_helpers as helpers
from app.api import auth_sso
from app.core.config import settings

FRONTEND = "https://app.example.com"
MISCONFIGURED = f"{FRONTEND}/login?sso_error=misconfigured"
FAILED = f"{FRONTEND}/login?sso_error=failed"
ISSUER = "https://idp.example/realms/cdc"
REALM_INFO = {
    "realm": "cdc",
    "public_key": "SECRET-LOOKING-KEY",
    "token-service": f"{ISSUER}/protocol/openid-connect",
}
DISCOVERY = {
    "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
    "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
}


def _cfg(url: str = ISSUER, **kw):
    return SimpleNamespace(
        oidc_enabled=True,
        oidc_discovery_url=url,
        oidc_authorization_url="",
        oidc_token_url="",
        oidc_userinfo_url="",
        **kw,
    )


def _serve(monkeypatch, handler) -> None:
    real = httpx.AsyncClient
    monkeypatch.setattr(
        auth_sso.httpx,
        "AsyncClient",
        lambda **kw: real(**{**kw, "transport": httpx.MockTransport(handler)}),
    )


@pytest.fixture(autouse=True)
def _frontend(monkeypatch):
    monkeypatch.setattr(settings, "frontend_url", FRONTEND)


def _stack(monkeypatch, cfg, handler):
    async def _get_cfg(db):
        return cfg

    monkeypatch.setattr(helpers, "get_sso_config", _get_cfg)
    _serve(monkeypatch, handler)


def _reply(status=200, **kw):
    return lambda request: httpx.Response(status, **kw)


def _raises(exc):
    def _handler(request):
        raise exc

    return _handler


# (handler, marker): what an unusable document or fetch is reported as.
UNUSABLE = [
    pytest.param(_reply(json=REALM_INFO), MISCONFIGURED, id="realm-info"),
    pytest.param(
        _reply(text="<html>blocked</html>", headers={"content-type": "text/html"}),
        MISCONFIGURED,
        id="html",
    ),
    pytest.param(_reply(404), MISCONFIGURED, id="not-found"),
    pytest.param(_reply(403, json={"error": "forbidden"}), MISCONFIGURED, id="forbidden"),
    pytest.param(_reply(json=["not", "an", "object"]), MISCONFIGURED, id="not-an-object"),
    pytest.param(
        _reply(json={**DISCOVERY, "authorization_endpoint": "", "token_endpoint": ""}),
        MISCONFIGURED,
        id="empty-endpoints",
    ),
    pytest.param(
        _raises(httpx.UnsupportedProtocol("no scheme")),
        MISCONFIGURED,
        id="no-scheme",
    ),
    pytest.param(
        _reply(content=b"{" + b" " * (256 * 1024 + 1) + b"}"),
        MISCONFIGURED,
        id="oversized",
    ),
    pytest.param(
        _reply(json={**DISCOVERY, "token_endpoint": None, "authorization_endpoint": 5}),
        MISCONFIGURED,
        id="non-string-endpoints",
    ),
    # Not an httpx.HTTPError, so it needs its own catch.
    pytest.param(_raises(httpx.InvalidURL("bad port")), MISCONFIGURED, id="invalid-url"),
    # The provider failing is not an administrator's mistake.
    pytest.param(_reply(503), FAILED, id="provider-5xx"),
    pytest.param(_reply(429), FAILED, id="rate-limited"),
    pytest.param(_reply(408), FAILED, id="request-timeout"),
    pytest.param(_raises(httpx.ConnectError("refused")), FAILED, id="unreachable"),
    pytest.param(
        _raises(httpx.RemoteProtocolError("reset")), FAILED, id="connection-reset"
    ),
    pytest.param(_raises(httpx.ReadTimeout("slow")), FAILED, id="timeout"),
]


@pytest.mark.parametrize(("handler", "location"), UNUSABLE)
async def test_login_redirects_when_the_document_is_unusable(
    monkeypatch, handler, location
):
    _stack(monkeypatch, _cfg(), handler)

    response = await auth_sso.oidc_login(db=None)

    assert response.headers["location"] == location


@pytest.mark.parametrize(("handler", "location"), UNUSABLE)
async def test_callback_redirects_when_the_document_is_unusable(
    monkeypatch, handler, location
):
    _stack(monkeypatch, _cfg(), handler)

    response = await auth_sso.oidc_callback(code="x", db=None)

    assert response.headers["location"] == location


async def test_login_redirects_when_manual_endpoints_are_empty(monkeypatch):
    _stack(monkeypatch, _cfg(url=""), _reply())

    response = await auth_sso.oidc_login(db=None)

    assert response.headers["location"] == MISCONFIGURED


async def test_login_sends_the_browser_to_the_authorization_endpoint(monkeypatch):
    _stack(monkeypatch, _cfg(), _reply(json=DISCOVERY))
    monkeypatch.setattr(
        auth_sso,
        "_oidc_client_from_cfg",
        lambda cfg: SimpleNamespace(
            create_authorization_url=lambda endpoint, **kw: (f"{endpoint}?x=1", "s")
        ),
    )

    response = await auth_sso.oidc_login(db=None)

    assert response.headers["location"] == f"{DISCOVERY['authorization_endpoint']}?x=1"


async def test_the_log_names_the_url_and_keys_but_no_values(monkeypatch, caplog):
    _stack(monkeypatch, _cfg(), _reply(json=REALM_INFO))

    with caplog.at_level(logging.WARNING, logger=auth_sso.logger.name):
        await auth_sso.oidc_login(db=None)

    text = caplog.text
    assert ISSUER in text
    assert "authorization_endpoint" in text
    assert "public_key" in text
    assert "SECRET-LOOKING-KEY" not in text
    assert "/.well-known/openid-configuration" in text


async def test_the_log_gives_the_http_status_for_a_refusal(monkeypatch, caplog):
    _stack(monkeypatch, _cfg(), _reply(403, json={"error": "forbidden"}))

    with caplog.at_level(logging.WARNING, logger=auth_sso.logger.name):
        await auth_sso.oidc_login(db=None)

    assert "HTTP 403" in caplog.text


async def test_callback_exchanges_the_code_at_the_token_endpoint(monkeypatch):
    _stack(monkeypatch, _cfg(), _reply(json=DISCOVERY))
    seen = {}

    class _Client:
        async def fetch_token(self, endpoint, **kw):
            seen["endpoint"] = endpoint
            raise RuntimeError("stop after the exchange")

    monkeypatch.setattr(auth_sso, "_oidc_client_from_cfg", lambda cfg: _Client())

    response = await auth_sso.oidc_callback(code="x", db=None)

    assert seen["endpoint"] == DISCOVERY["token_endpoint"]
    assert response.headers["location"] == FAILED


@pytest.mark.parametrize(
    "handler",
    [
        _reply(404),
        _reply(json=REALM_INFO),
        _reply(json={**DISCOVERY, "authorization_endpoint": ""}),
        _raises(httpx.UnsupportedProtocol("no scheme")),
    ],
    ids=["not-found", "realm-info", "empty-endpoint", "no-scheme"],
)
async def test_the_log_hints_at_the_issuer_root_mistake(monkeypatch, caplog, handler):
    _stack(monkeypatch, _cfg(), handler)

    with caplog.at_level(logging.WARNING, logger=auth_sso.logger.name):
        await auth_sso.oidc_login(db=None)

    assert "/.well-known/openid-configuration" in caplog.text


@pytest.mark.parametrize(
    "handler",
    [_reply(503), _reply(429), _raises(httpx.ConnectError("refused"))],
    ids=["5xx", "rate-limited", "unreachable"],
)
async def test_the_log_gives_no_hint_when_the_provider_is_down(
    monkeypatch, caplog, handler
):
    _stack(monkeypatch, _cfg(), handler)

    with caplog.at_level(logging.WARNING, logger=auth_sso.logger.name):
        await auth_sso.oidc_login(db=None)

    assert caplog.text
    assert "should end in" not in caplog.text


async def test_the_log_gives_no_hint_for_a_well_known_url(monkeypatch, caplog):
    url = f"{ISSUER}/.well-known/openid-configuration"
    _stack(monkeypatch, _cfg(url=url), _reply(404))

    with caplog.at_level(logging.WARNING, logger=auth_sso.logger.name):
        await auth_sso.oidc_login(db=None)

    assert "should end in" not in caplog.text


async def test_the_log_does_not_say_a_present_key_is_missing(monkeypatch, caplog):
    _stack(
        monkeypatch,
        _cfg(),
        _reply(json={**DISCOVERY, "authorization_endpoint": ""}),
    )

    with caplog.at_level(logging.WARNING, logger=auth_sso.logger.name):
        await auth_sso.oidc_login(db=None)

    assert "empty or non-string authorization_endpoint" in caplog.text
    assert "has no authorization_endpoint" not in caplog.text


async def test_the_log_names_a_blank_manual_endpoint(monkeypatch, caplog):
    _stack(monkeypatch, _cfg(url=""), _reply())

    with caplog.at_level(logging.WARNING, logger=auth_sso.logger.name):
        await auth_sso.oidc_login(db=None)

    assert "authorization_endpoint is blank in the SSO settings" in caplog.text
    assert "keys received" not in caplog.text


async def test_login_follows_a_redirect_to_the_document(monkeypatch):
    def _handler(request):
        if request.url.path.endswith("/moved"):
            return httpx.Response(301, headers={"location": f"{ISSUER}/doc"})
        return httpx.Response(200, json=DISCOVERY)

    _stack(monkeypatch, _cfg(url=f"{ISSUER}/moved"), _handler)
    monkeypatch.setattr(
        auth_sso,
        "_oidc_client_from_cfg",
        lambda cfg: SimpleNamespace(
            create_authorization_url=lambda endpoint, **kw: (endpoint, "s")
        ),
    )

    response = await auth_sso.oidc_login(db=None)

    assert response.headers["location"] == DISCOVERY["authorization_endpoint"]


async def test_callback_refuses_a_non_string_userinfo_endpoint(monkeypatch):
    _stack(monkeypatch, _cfg(), _reply(json={**DISCOVERY, "userinfo_endpoint": 5}))

    response = await auth_sso.oidc_callback(code="x", db=None)

    assert response.headers["location"] == MISCONFIGURED


async def test_callback_accepts_a_blank_userinfo_endpoint(monkeypatch):
    _stack(monkeypatch, _cfg(), _reply(json={**DISCOVERY, "userinfo_endpoint": ""}))
    seen = {}

    class _Client:
        async def fetch_token(self, endpoint, **kw):
            seen["endpoint"] = endpoint
            raise RuntimeError("stop after the exchange")

    monkeypatch.setattr(auth_sso, "_oidc_client_from_cfg", lambda cfg: _Client())

    await auth_sso.oidc_callback(code="x", db=None)

    assert seen["endpoint"] == DISCOVERY["token_endpoint"]


async def test_the_log_names_where_a_redirect_ended_without_its_query(
    monkeypatch, caplog
):
    def _handler(request):
        if request.url.path.endswith("/moved"):
            return httpx.Response(
                302, headers={"location": "https://sso.example/login?ticket=SECRET"}
            )
        return httpx.Response(
            200, text="<html>login</html>", headers={"content-type": "text/html"}
        )

    _stack(monkeypatch, _cfg(url=f"{ISSUER}/moved"), _handler)

    with caplog.at_level(logging.WARNING, logger=auth_sso.logger.name):
        await auth_sso.oidc_login(db=None)

    assert "ended at https://sso.example/login" in caplog.text
    assert "SECRET" not in caplog.text


async def test_the_log_does_not_call_an_error_status_body_unparsed(monkeypatch, caplog):
    _stack(monkeypatch, _cfg(), _reply(404))

    with caplog.at_level(logging.WARNING, logger=auth_sso.logger.name):
        await auth_sso.oidc_login(db=None)

    assert "HTTP 404" in caplog.text
    assert "not a JSON object" not in caplog.text
    assert "ended at" not in caplog.text
