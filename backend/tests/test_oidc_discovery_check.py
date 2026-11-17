"""Test connection for the OIDC Discovery URL, and the discovery cache."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import httpx
import pytest

from app.api import auth_sso
from app.api.deps import require_admin
from app.api.v1 import settings as settings_api
from app.api.v1.schemas.settings import SSOOIDCTestRequest

ISSUER = "https://idp.example/realms/cdc"
URL = f"{ISSUER}/.well-known/openid-configuration"
DISCOVERY = {
    "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
    "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
}
REALM_INFO = {"realm": "cdc", "public_key": "SECRET-LOOKING-KEY"}


def _serve(monkeypatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def _record(request):
        seen.append(request)
        return handler(request)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        auth_sso.httpx,
        "AsyncClient",
        lambda **kw: real(**{**kw, "transport": httpx.MockTransport(_record)}),
    )
    return seen


def _cfg(url: str = URL):
    return SimpleNamespace(
        oidc_discovery_url=url,
        oidc_authorization_url="",
        oidc_token_url="",
        oidc_userinfo_url="",
    )


def _test(url: str):
    return settings_api.test_sso_oidc(SSOOIDCTestRequest(oidc_discovery_url=url), user=None)


def test_the_test_route_requires_an_administrator():
    params = inspect.signature(settings_api.test_sso_oidc).parameters
    assert params["user"].default.dependency is require_admin


async def test_a_usable_document_is_ok(monkeypatch):
    _serve(monkeypatch, lambda request: httpx.Response(200, json=DISCOVERY))

    result = await _test(URL)

    assert result.ok is True
    assert result.status == "ok"


async def test_an_issuer_root_names_the_missing_key_and_the_fix(monkeypatch):
    _serve(monkeypatch, lambda request: httpx.Response(200, json=REALM_INFO))

    result = await _test(ISSUER)

    assert (result.ok, result.status) == (False, "misconfigured")
    assert "authorization_endpoint" in result.detail
    assert "realm" in result.detail
    assert "/.well-known/openid-configuration" in result.detail
    # Keys only: a realm-info body carries a public key.
    assert "SECRET-LOOKING-KEY" not in result.detail


async def test_a_wrong_status_is_reported_with_the_status(monkeypatch):
    _serve(monkeypatch, lambda request: httpx.Response(404))

    result = await _test(URL)

    assert (result.ok, result.status) == (False, "misconfigured")
    assert "HTTP 404" in result.detail


async def test_a_provider_outage_reads_as_failed_not_misconfigured(monkeypatch):
    _serve(monkeypatch, lambda request: httpx.Response(503))

    result = await _test(URL)

    assert (result.ok, result.status) == (False, "failed")
    assert "HTTP 503" in result.detail


async def test_a_blank_url_is_refused():
    with pytest.raises(Exception) as exc:
        await _test("   ")

    assert getattr(exc.value, "status_code", None) == 422


async def test_the_test_ignores_a_cached_document(monkeypatch):
    seen = _serve(monkeypatch, lambda request: httpx.Response(200, json=DISCOVERY))
    await auth_sso._oidc_discovery_from_cfg(_cfg())

    await _test(URL)

    assert len(seen) == 2


async def test_the_test_leaves_the_cache_empty(monkeypatch):
    _serve(monkeypatch, lambda request: httpx.Response(200, json=DISCOVERY))

    await _test(URL)

    assert auth_sso._discovery_cache == {}


async def test_the_fetch_is_held_to_the_operator_network_policy(monkeypatch):
    # An intranet IdP is admitted; loopback, link-local and metadata addresses
    # are refused by the guarded transport (tested with ``web_fetch``).
    _serve(monkeypatch, lambda request: httpx.Response(200, json=DISCOVERY))
    policies: list[bool] = []
    real = auth_sso.guarded_client_options

    def _spy(*, allow_private):
        policies.append(allow_private)
        return real(allow_private=allow_private)

    monkeypatch.setattr(auth_sso, "guarded_client_options", _spy)

    await _test(URL)

    assert policies == [True]


async def test_sign_in_fetches_the_document_once_for_login_and_callback(monkeypatch):
    seen = _serve(monkeypatch, lambda request: httpx.Response(200, json=DISCOVERY))

    first = await auth_sso._oidc_discovery_from_cfg(_cfg())
    second = await auth_sso._oidc_discovery_from_cfg(_cfg())

    assert first == second == DISCOVERY
    assert len(seen) == 1


async def test_the_cache_is_keyed_on_the_url(monkeypatch):
    seen = _serve(monkeypatch, lambda request: httpx.Response(200, json=DISCOVERY))

    await auth_sso._oidc_discovery_from_cfg(_cfg())
    await auth_sso._oidc_discovery_from_cfg(_cfg(f"{URL}?other"))

    assert len(seen) == 2


async def test_the_cache_expires(monkeypatch):
    seen = _serve(monkeypatch, lambda request: httpx.Response(200, json=DISCOVERY))
    now = [1000.0]
    monkeypatch.setattr(auth_sso.time, "monotonic", lambda: now[0])

    await auth_sso._oidc_discovery_from_cfg(_cfg())
    now[0] += auth_sso._DISCOVERY_TTL_SECONDS + 1
    await auth_sso._oidc_discovery_from_cfg(_cfg())

    assert len(seen) == 2


async def test_saving_the_settings_clears_the_cache(monkeypatch):
    seen = _serve(monkeypatch, lambda request: httpx.Response(200, json=DISCOVERY))
    await auth_sso._oidc_discovery_from_cfg(_cfg())

    auth_sso.clear_oidc_discovery_cache()
    await auth_sso._oidc_discovery_from_cfg(_cfg())

    assert len(seen) == 2


async def test_a_failed_fetch_is_not_cached(monkeypatch):
    replies = iter([httpx.Response(503), httpx.Response(200, json=DISCOVERY)])
    seen = _serve(monkeypatch, lambda request: next(replies))

    with pytest.raises(auth_sso._OidcDiscoveryError):
        await auth_sso._oidc_discovery_from_cfg(_cfg())
    recovered = await auth_sso._oidc_discovery_from_cfg(_cfg())

    assert recovered == DISCOVERY
    assert len(seen) == 2
