"""OAuth token requests only go where the strict token endpoint policy allows.

The strict policy is opt-in (OAUTH_TOKEN_URL_STRICT=true), and every test here
runs under it: private networks allowed (on-prem identity providers);
loopback, link-local, reserved and cloud-metadata addresses refused; origins
listed in OAUTH_TOKEN_DEV_TRUSTED_ORIGINS may use loopback. Applied before each
of the five token requests and when a token URL is saved. The default, which
refuses only metadata addresses and URLs no request can be sent to, is pinned
by ``test_token_endpoint_default_mode.py``.
"""

from __future__ import annotations

import ast
import inspect
import ipaddress
import pathlib
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app.a2a.client import destinations as a2a_destinations
from app.api.v1 import helpers as api_helpers
from app.api.v1 import integrations as integrations_api
from app.api.v1 import mcp_servers as mcp_servers_api
from app.core import config
from app.core.destination_policy import forbidden_address_reason
from app.core.security import encrypt_value
from app.integrations import oauth, token_endpoint
from app.integrations.token_endpoint import (
    TokenEndpointRefusedError,
    assert_token_endpoint_allowed,
)
from app.services import integration_tools, token_refresh

LOOPBACK_URL = "https://127.0.0.1/oauth/token"


def _resolves_to(*addresses: str):
    async def _fake(_host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses]

    return _fake


@pytest.fixture(autouse=True)
def strict(monkeypatch):
    monkeypatch.setattr(config.settings, "oauth_token_url_strict", True)


@pytest.fixture
def trusted(monkeypatch):
    def _set(*origins: str) -> None:
        # The guard reads the option through ``security_option``, which falls
        # back to ``settings`` when nothing is stored in Settings > Access and security.
        monkeypatch.setattr(config.settings, "oauth_token_dev_trusted_origins", list(origins))

    _set()
    return _set


# -- the address policy ---------------------------------------------------------


@pytest.mark.parametrize("address", ["10.0.0.5", "192.168.1.10", "172.16.4.4", "100.64.1.1", "8.8.8.8"])
def test_operator_network_policy_admits_private_and_public(address):
    ip = ipaddress.ip_address(address)
    assert forbidden_address_reason(ip, allow_private=True) is None


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "::ffff:127.0.0.1",
        "169.254.10.10",
        "169.254.169.254",
        "169.254.170.2",
        "fd00:ec2::254",
        "fd20:ce::254",
        "100.100.100.200",
        "0.0.0.0",  # noqa: S104 - the unspecified address, refused by the policy
        "224.0.0.1",
    ],
)
def test_operator_network_policy_refuses_loopback_link_local_metadata(address):
    ip = ipaddress.ip_address(address)
    assert forbidden_address_reason(ip, allow_private=True) is not None


def test_loopback_exemption_never_reaches_link_local_or_metadata():
    loopback = ipaddress.ip_address("127.0.0.1")
    assert forbidden_address_reason(loopback, allow_private=True, allow_loopback=True) is None
    for address in ("169.254.169.254", "169.254.10.10", "fd00:ec2::254", "fd20:ce::254"):
        ip = ipaddress.ip_address(address)
        assert forbidden_address_reason(ip, allow_private=True, allow_loopback=True)


@pytest.mark.parametrize(
    ("address", "reason"),
    [
        ("10.0.0.1", "private address"),
        ("127.0.0.1", "loopback address"),
        ("169.254.1.1", "link-local address"),
        ("169.254.169.254", "cloud metadata endpoint"),
        ("100.64.0.1", "non-globally-routable address"),
        ("8.8.8.8", None),
    ],
)
def test_the_a2a_guard_keeps_its_public_only_policy(address, reason):
    assert a2a_destinations._is_forbidden_address(ipaddress.ip_address(address)) == reason


# -- the URL guard --------------------------------------------------------------


@pytest.mark.asyncio
async def test_literal_loopback_token_url_is_refused(trusted):
    with pytest.raises(TokenEndpointRefusedError, match="loopback"):
        await assert_token_endpoint_allowed(LOOPBACK_URL)


@pytest.mark.asyncio
async def test_private_network_token_url_is_allowed(trusted):
    await assert_token_endpoint_allowed("http://10.1.2.3:8443/realms/x/token")


@pytest.mark.asyncio
async def test_every_resolved_address_must_pass_and_none_is_disclosed(monkeypatch, trusted):
    monkeypatch.setattr(token_endpoint, "_resolve", _resolves_to("10.0.0.7", "127.0.0.1"))
    with pytest.raises(TokenEndpointRefusedError) as refused:
        await assert_token_endpoint_allowed("https://idp.corp.example/token")
    assert "127.0.0.1" not in str(refused.value)

    monkeypatch.setattr(token_endpoint, "_resolve", _resolves_to("10.0.0.7"))
    await assert_token_endpoint_allowed("https://idp.corp.example/token")


@pytest.mark.asyncio
async def test_an_unresolvable_name_is_left_to_the_request(monkeypatch, trusted):
    async def _unresolvable(_host, _port):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(token_endpoint, "_resolve", _unresolvable)
    await assert_token_endpoint_allowed("https://idp.invalid/token")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url", ["ftp://idp.example/token", "https:///token", "not a url", "http://[::1"]
)
async def test_a_url_without_an_http_scheme_and_host_is_refused(url, trusted):
    with pytest.raises(TokenEndpointRefusedError):
        await assert_token_endpoint_allowed(url)


@pytest.mark.asyncio
async def test_a_trusted_origin_may_use_loopback_and_nothing_else(monkeypatch, trusted):
    trusted("http://localhost:8080")
    monkeypatch.setattr(token_endpoint, "_resolve", _resolves_to("127.0.0.1"))
    await assert_token_endpoint_allowed("http://localhost:8080/oauth/token")
    with pytest.raises(TokenEndpointRefusedError):
        await assert_token_endpoint_allowed("http://localhost:8081/oauth/token")

    monkeypatch.setattr(token_endpoint, "_resolve", _resolves_to("169.254.169.254"))
    with pytest.raises(TokenEndpointRefusedError, match="metadata"):
        await assert_token_endpoint_allowed("http://localhost:8080/oauth/token")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "origin"),
    [(LOOPBACK_URL, "https://127.0.0.1"), ("http://[::1]:8080/oauth/token", "http://[::1]:8080")],
)
async def test_a_loopback_refusal_names_the_origin_to_trust_and_the_advice_works(url, origin, trusted):
    with pytest.raises(TokenEndpointRefusedError) as refused:
        await assert_token_endpoint_allowed(url)
    assert f"add {origin} to OAUTH_TOKEN_DEV_TRUSTED_ORIGINS" in str(refused.value)

    trusted(*config.Settings(oauth_token_dev_trusted_origins=origin).oauth_token_dev_trusted_origins)
    await assert_token_endpoint_allowed(url)


@pytest.mark.asyncio
async def test_a_refusal_the_setting_cannot_lift_does_not_offer_it(trusted):
    with pytest.raises(TokenEndpointRefusedError) as refused:
        await assert_token_endpoint_allowed("http://169.254.169.254/latest/meta-data")
    assert "OAUTH_TOKEN_DEV_TRUSTED_ORIGINS" not in str(refused.value)
    assert "OAUTH_TOKEN_URL_STRICT" not in str(refused.value)


@pytest.mark.asyncio
async def test_a_loopback_refusal_names_both_settings_that_lift_it(trusted):
    with pytest.raises(TokenEndpointRefusedError) as refused:
        await assert_token_endpoint_allowed(LOOPBACK_URL)
    assert (
        "To accept it, set OAUTH_TOKEN_URL_STRICT=false (or turn off its field in Settings > Access and security)"
        in str(refused.value)
    )
    assert (
        "add https://127.0.0.1 to OAUTH_TOKEN_DEV_TRUSTED_ORIGINS (or to its field in Settings > Access and security)"
        in str(refused.value)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.10.10/token",
        "http://[fe80::1]/token",
        "http://0.0.0.0:8080/token",
        "http://240.0.0.1/token",
        "http://224.0.0.1/token",
    ],
)
async def test_every_other_strict_refusal_names_the_strict_setting(url, trusted):
    with pytest.raises(TokenEndpointRefusedError) as refused:
        await assert_token_endpoint_allowed(url)
    assert str(refused.value).endswith(
        " To accept it, set OAUTH_TOKEN_URL_STRICT=false (or turn off its field in Settings > Access and security)."
    )
    assert "OAUTH_TOKEN_DEV_TRUSTED_ORIGINS" not in str(refused.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["ws", "wss"])
async def test_strict_mode_takes_http_and_https_only(scheme, trusted):
    with pytest.raises(TokenEndpointRefusedError, match=f"To accept a {scheme} URL, set OAUTH_TOKEN_URL_STRICT=false"):
        await assert_token_endpoint_allowed(f"{scheme}://10.0.0.5/token")


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["ftp://idp.example/token", "not a url"])
async def test_another_scheme_is_told_the_schemes_strict_mode_accepts(url, trusted):
    # No setting accepts it, so none is offered.
    with pytest.raises(TokenEndpointRefusedError, match=r"^The token URL must be an http or https URL\.$"):
        await assert_token_endpoint_allowed(url)

@pytest.mark.asyncio
async def test_a_ws_url_is_refused_even_when_its_name_does_not_resolve(monkeypatch, trusted):
    async def _unresolvable(_host, _port):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(token_endpoint, "_resolve", _unresolvable)
    with pytest.raises(TokenEndpointRefusedError, match="must be an http or https URL"):
        await assert_token_endpoint_allowed("ws://idp.invalid/token")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "addresses"),
    [
        ("ws://169.254.169.254/latest/meta-data", ()),
        ("wss://idp.corp.example/token", ("169.254.169.254",)),
        ("https://idp.corp.example/token", ("127.0.0.1", "169.254.169.254")),
    ],
)
async def test_a_metadata_destination_is_never_told_a_setting_would_lift_it(monkeypatch, trusted, url, addresses):
    monkeypatch.setattr(token_endpoint, "_resolve", _resolves_to(*addresses))
    with pytest.raises(TokenEndpointRefusedError, match="cloud metadata endpoint") as refused:
        await assert_token_endpoint_allowed(url)
    assert "OAUTH_TOKEN_URL_STRICT" not in str(refused.value)
    assert "OAUTH_TOKEN_DEV_TRUSTED_ORIGINS" not in str(refused.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "addresses", "refusal"),
    [
        ("https://idp.corp.example/token", ("127.0.0.1", "169.254.10.10"), "link-local address"),
        ("ws://127.0.0.1/token", (), "must be an http or https URL"),
    ],
)
async def test_a_trusted_origin_is_offered_only_when_loopback_is_all_that_is_refused(
    monkeypatch, trusted, url, addresses, refusal
):
    monkeypatch.setattr(token_endpoint, "_resolve", _resolves_to(*addresses))
    with pytest.raises(TokenEndpointRefusedError, match=refusal) as refused:
        await assert_token_endpoint_allowed(url)
    assert "OAUTH_TOKEN_URL_STRICT=false" in str(refused.value)
    assert "OAUTH_TOKEN_DEV_TRUSTED_ORIGINS" not in str(refused.value)


@pytest.mark.asyncio
async def test_names_resolve_through_the_a2a_guard_resolver(monkeypatch, trusted):
    monkeypatch.setattr(a2a_destinations, "_resolve", _resolves_to("127.0.0.1"))
    with pytest.raises(TokenEndpointRefusedError, match="loopback"):
        await assert_token_endpoint_allowed("https://idp.corp.example/token")


@pytest.mark.asyncio
async def test_a_lookup_that_does_not_answer_in_time_is_left_to_the_request(monkeypatch, trusted):
    import asyncio

    async def _black_holed(_host, _port):
        await asyncio.Event().wait()

    monkeypatch.setattr(token_endpoint, "_resolve", _black_holed)
    monkeypatch.setattr(token_endpoint, "_DNS_TIMEOUT_SECONDS", 0.05)
    await asyncio.wait_for(assert_token_endpoint_allowed("https://idp.slow.example/token"), timeout=5)


# -- the five token requests ----------------------------------------------------


class _NoClient:
    """Any HTTP client construction is a test failure: the guard runs first."""

    def __init__(self, *_args, **_kwargs):
        raise AssertionError("a token request was sent to a refused destination")


class _Redis:
    def __init__(self):
        self.set = AsyncMock()
        self.get = AsyncMock(return_value=None)


@pytest.mark.asyncio
async def test_integration_code_exchange_is_refused_before_sending(monkeypatch, trusted):
    monkeypatch.setattr(httpx, "AsyncClient", _NoClient)
    integration = SimpleNamespace(
        provider="github",
        client_id_enc=encrypt_value("cid"),
        client_secret_enc=encrypt_value("secret"),
        token_url=LOOPBACK_URL,
        extra_config_enc=None,
    )
    with pytest.raises(TokenEndpointRefusedError):
        await oauth.exchange_code_for_tokens_with_integration(integration, "code")


@pytest.mark.asyncio
async def test_mcp_code_exchange_is_refused_before_sending(monkeypatch, trusted):
    monkeypatch.setattr(httpx, "AsyncClient", _NoClient)
    server = SimpleNamespace(
        client_id_enc=encrypt_value("cid"),
        client_secret_enc=None,
        token_url=LOOPBACK_URL,
        oauth_resource=None,
    )
    with pytest.raises(TokenEndpointRefusedError):
        await oauth.exchange_code_for_tokens_with_mcp_server(server, "code")


@pytest.mark.asyncio
async def test_scheduled_integration_refresh_is_refused_with_the_long_cooldown(monkeypatch, trusted):
    redis = _Redis()
    monkeypatch.setattr(token_refresh, "redis_client", AsyncMock(return_value=redis))
    http = SimpleNamespace(post=AsyncMock(side_effect=AssertionError("request sent")))
    integration = SimpleNamespace(
        id="i-1",
        name="IdP",
        provider="custom",
        client_id_enc=encrypt_value("cid"),
        client_secret_enc=None,
        refresh_token_enc=encrypt_value("rt"),
        scopes=None,
        token_url=LOOPBACK_URL,
        extra_config_enc=None,
        status="connected",
    )

    assert await token_refresh._do_refresh_integration(integration, http) is False
    assert integration.status == "error"
    http.post.assert_not_awaited()
    redis.set.assert_awaited_once_with(
        "token_refresh_cooldown:i-1", "1", ex=token_refresh._AUTH_FAIL_COOLDOWN
    )


@pytest.mark.asyncio
async def test_scheduled_mcp_refresh_is_refused_with_the_long_cooldown(monkeypatch, trusted):
    redis = _Redis()
    monkeypatch.setattr(token_refresh, "redis_client", AsyncMock(return_value=redis))
    http = SimpleNamespace(post=AsyncMock(side_effect=AssertionError("request sent")))
    server = SimpleNamespace(
        id="m-1",
        name="MCP",
        client_id_enc=encrypt_value("cid"),
        client_secret_enc=None,
        refresh_token_enc=encrypt_value("rt"),
        scopes=None,
        oauth_resource=None,
        token_url=LOOPBACK_URL,
        oauth_status="connected",
    )

    assert await token_refresh._do_refresh_mcp(server, http) is False
    assert server.oauth_status == "error"
    http.post.assert_not_awaited()
    redis.set.assert_awaited_once_with(
        "token_refresh_cooldown:mcp:m-1", "1", ex=token_refresh._AUTH_FAIL_COOLDOWN
    )


def _refused_row(path: str) -> SimpleNamespace:
    common = dict(
        name="IdP",
        client_id_enc=encrypt_value("cid"),
        client_secret_enc=None,
        refresh_token_enc=encrypt_value("rt"),
        scopes=None,
        token_url=LOOPBACK_URL,
    )
    if path == "integration":
        return SimpleNamespace(id="i-1", provider="custom", extra_config_enc=None, status="connected", **common)
    return SimpleNamespace(id="m-1", oauth_resource=None, oauth_status="connected", **common)


async def _refused_refresh(path: str, row, tokens_unchanged) -> bool:
    http = SimpleNamespace(post=AsyncMock(side_effect=AssertionError("request sent")))
    if path == "integration":
        return await token_refresh._do_refresh_integration(row, http, tokens_unchanged=tokens_unchanged)
    return await token_refresh._do_refresh_mcp(row, http, tokens_unchanged=tokens_unchanged)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["integration", "mcp"])
async def test_a_refused_token_url_backs_off_only_after_the_row_locked_check(monkeypatch, trusted, path):
    """A re-authorization clears the back-off after its commit, which waits on this row lock."""
    events: list[str] = []
    redis = _Redis()
    redis.set = AsyncMock(side_effect=lambda *_a, **_k: events.append("back-off"))
    monkeypatch.setattr(token_refresh, "redis_client", AsyncMock(return_value=redis))

    async def _tokens_unchanged() -> bool:
        events.append("row lock")
        return True

    row = _refused_row(path)
    assert await _refused_refresh(path, row, _tokens_unchanged) is False

    assert events == ["row lock", "back-off"]
    assert (row.status if path == "integration" else row.oauth_status) == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["integration", "mcp"])
async def test_a_refused_token_url_check_about_replaced_tokens_leaves_the_new_grant_alone(
    monkeypatch, trusted, path
):
    """A re-authorization committed while the old URL was being checked: no back-off, no error."""
    redis = _Redis()
    monkeypatch.setattr(token_refresh, "redis_client", AsyncMock(return_value=redis))

    row = _refused_row(path)
    assert await _refused_refresh(path, row, AsyncMock(return_value=False)) is False

    redis.set.assert_not_awaited()
    assert (row.status if path == "integration" else row.oauth_status) == "connected"


@pytest.mark.asyncio
async def test_inline_refresh_is_refused_before_sending(monkeypatch, trusted):
    monkeypatch.setattr(integration_tools.httpx, "AsyncClient", _NoClient)
    redis = _Redis()
    monkeypatch.setattr(token_refresh, "redis_client", AsyncMock(return_value=redis))
    integration = SimpleNamespace(
        id="i-1",
        name="IdP",
        provider="custom",
        client_id_enc=encrypt_value("cid"),
        client_secret_enc=None,
        refresh_token_enc=encrypt_value("rt"),
        scopes=None,
        token_url=LOOPBACK_URL,
        extra_config_enc=None,
    )
    tokens_unchanged = AsyncMock(return_value=True)

    assert await integration_tools._do_inline_refresh(AsyncMock(), integration, tokens_unchanged) is None
    tokens_unchanged.assert_not_awaited()
    redis.set.assert_awaited_once()


# -- tenant id, save-time check, setting ----------------------------------------


def test_the_tenant_id_stays_inside_one_path_segment():
    url = oauth.substitute_tenant(
        "https://login.microsoftonline.com/common/oauth2/v2.0/token", "a/b?c#d"
    )
    assert url == "https://login.microsoftonline.com/a%2Fb%3Fc%23d/oauth2/v2.0/token"


@pytest.mark.asyncio
async def test_saving_a_refused_token_url_answers_422(trusted):
    with pytest.raises(HTTPException) as refused:
        await api_helpers.require_allowed_token_url(LOOPBACK_URL)
    assert refused.value.status_code == 422
    await api_helpers.require_allowed_token_url("")
    await api_helpers.require_allowed_token_url(None)
    await api_helpers.require_allowed_token_url("https://10.0.0.9/token")


@pytest.mark.parametrize(
    ("route", "checked"),
    [
        (integrations_api.create_integration, "body.token_url"),
        (integrations_api.update_integration, "body.token_url"),
        (mcp_servers_api.create_mcp_server, "body.token_url"),
        # The URL that is saved: a masked one sent back stands for the stored
        # URL (test_mcp_server_masked_args_postgres.py spies the call).
        (mcp_servers_api.update_mcp_server, 'urls["token_url"]'),
    ],
)
def test_every_token_url_write_route_checks_it(route, checked):
    assert f"require_allowed_token_url({checked})" in inspect.getsource(route)


def test_the_mcp_import_route_checks_the_imported_token_url():
    source = inspect.getsource(mcp_servers_api.import_mcp_server)
    assert "require_allowed_token_url(token_url)" in source
    assert "token_url=token_url" in source


@pytest.mark.asyncio
async def test_the_bundle_import_refuses_an_instance_token_url_before_writing_anything(monkeypatch, trusted):
    parsed = SimpleNamespace(
        metadata=SimpleNamespace(provider="acme-idp", version="1.0.0"),
        integration_instance=SimpleNamespace(token_url=LOOPBACK_URL),
    )

    async def _read(_bundle):
        return b"bundle"

    monkeypatch.setattr(integrations_api, "_read_bundle_upload", _read)
    monkeypatch.setattr(integrations_api, "parse_integration_bundle_zip", lambda _raw: parsed)

    class _Db:
        def __init__(self):
            self.added = []

        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

        def add(self, row):
            self.added.append(row)

    db = _Db()
    with pytest.raises(HTTPException) as refused:
        await integrations_api.import_integration_bundle(
            request=SimpleNamespace(), bundle=None, user=SimpleNamespace(role="admin", id="u-1"), db=db
        )
    assert refused.value.status_code == 422
    assert "loopback" in refused.value.detail
    assert db.added == []


@pytest.mark.asyncio
async def test_discovery_refuses_a_token_url_before_registering_a_client(monkeypatch, trusted):
    from app.services import mcp_oauth_discovery as discovery

    async def _discovered(**_kwargs):
        return discovery.OAuthEndpoints(
            authorize_url="https://auth.example.com/authorize",
            token_url="http://127.0.0.1:9000/token",
            registration_url="https://auth.example.com/register",
            discovered=True,
        )

    async def _no_registration(*_args, **_kwargs):
        raise AssertionError("a client was registered for a refused token URL")

    async def _branding(_db):
        return SimpleNamespace(name="XTM One")

    monkeypatch.setattr(discovery, "discover_oauth_endpoints", _discovered)
    monkeypatch.setattr(discovery, "register_dynamic_client", _no_registration)
    monkeypatch.setattr("app.services.branding.get_branding", _branding)
    db = SimpleNamespace(commit=AsyncMock(), in_transaction=lambda: False)
    server = SimpleNamespace(
        id="m-1",
        url="https://mcp.example.com/mcp",
        client_id_enc=None,
        client_secret_enc=None,
        authorize_url=None,
        token_url=None,
        registration_url=None,
        oauth_resource=None,
        scopes=None,
        oauth_metadata_url=None,
    )
    with pytest.raises(discovery.MCPOAuthDiscoveryError, match="loopback"):
        await discovery.ensure_mcp_oauth_client(db, server)
    db.commit.assert_not_awaited()


def test_the_trusted_origins_setting_is_normalised_and_refuses_malformed_entries():
    settings = config.Settings(oauth_token_dev_trusted_origins="HTTP://LocalHost:8080/x, http://localhost:8080")
    assert settings.oauth_token_dev_trusted_origins == ["http://localhost:8080"]
    assert config.Settings().oauth_token_dev_trusted_origins == []
    with pytest.raises(ValueError, match="OAUTH_TOKEN_DEV_TRUSTED_ORIGINS"):
        config.Settings(oauth_token_dev_trusted_origins="localhost:8080")


def test_a_configured_trusted_origin_says_so_at_boot():
    main = pathlib.Path(inspect.getfile(config)).parents[1] / "main.py"
    tree = ast.parse(main.read_text(encoding="utf-8"))
    guarded = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and "oauth_token_dev_trusted_origins" in ast.unparse(node.test)
    ]
    assert guarded
    by_mode = [
        node
        for node in ast.walk(guarded[0])
        if isinstance(node, ast.If) and "oauth_token_url_strict" in ast.unparse(node.test)
    ]
    assert by_mode
    # Logged before the Settings > Access and security snapshot is read: only the variable
    # turning the strict checks off proves the list changes nothing; otherwise
    # they may be on, from the variable or from Settings > Access and security.
    test = ast.unparse(by_mode[0].test)
    assert "is_environment_locked('oauth_token_url_strict')" in test
    assert "not settings.oauth_token_url_strict" in test
    no_effect_branch, relaxed_branch = ast.unparse(by_mode[0].body), ast.unparse(by_mode[0].orelse)
    assert "logger.info" in no_effect_branch
    assert "join(settings.oauth_token_dev_trusted_origins)" in no_effect_branch
    assert "logger.warning" in relaxed_branch
    assert "join(settings.oauth_token_dev_trusted_origins)" in relaxed_branch
