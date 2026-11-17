"""Every token URL that worked keeps working; the strict checks are opt-in.

By default the token endpoint guard refuses only what no token exchange could
ever complete against: a cloud instance-metadata address and a URL no request
can be sent to. Every other token URL - loopback, link-local, unspecified,
reserved, private, public - is accepted when it is saved and before each
token request, for integrations and MCP servers alike, with no configuration
change. The opt-in strict mode (OAUTH_TOKEN_URL_STRICT) is pinned by
``test_token_endpoint_guard.py``.

No test here resolves a name or opens a connection: DNS answers are stubbed,
and every token request goes to a recording stand-in.
"""

from __future__ import annotations

import asyncio
import inspect
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app.api.v1 import helpers as api_helpers
from app.api.v1 import integrations as integrations_api
from app.api.v1 import security_audit
from app.core import config
from app.core.destination_policy import METADATA_ADDRESSES
from app.core.security import encrypt_value
from app.integrations import oauth, token_endpoint
from app.integrations.token_endpoint import (
    TokenEndpointRefusedError,
    assert_token_endpoint_allowed,
)
from app.services import integration_tools, token_refresh

# Each of these could receive a token request before the guard existed, and
# still can by default.
ACCEPTED_BY_DEFAULT = [
    "https://127.0.0.1/oauth/token",
    "http://127.0.0.1:8080/realms/x/protocol/openid-connect/token",
    "http://[::1]:8080/token",
    "http://169.254.10.10/token",
    "http://[fe80::1]/token",
    "http://0.0.0.0:8080/token",
    "http://[::]:8080/token",
    "http://240.0.0.1/token",
    "http://224.0.0.1/token",
    "https://10.1.2.3:8443/token",
    "https://100.64.1.1/token",
    "https://8.8.8.8/token",
    "ws://10.0.0.5/token",
    "wss://10.0.0.5/token",
]

LOOPBACK_URL = "http://127.0.0.1:8080/oauth/token"


def _resolves_to(*addresses: str):
    async def _fake(_host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses]

    return _fake


async def _no_dns(host, _port):
    raise AssertionError(f"a real DNS lookup was attempted for {host}")


@pytest.fixture(autouse=True)
def default_mode(monkeypatch):
    """The shipped defaults, whatever the environment running the tests sets.

    The guard reads both settings through ``security_option``, which falls
    back to ``settings`` when nothing is stored in Settings > Access and security.
    """
    monkeypatch.setattr(config.settings, "oauth_token_url_strict", False)
    monkeypatch.setattr(config.settings, "oauth_token_dev_trusted_origins", [])
    monkeypatch.setattr(token_endpoint, "_resolve", _no_dns)


# -- the setting ------------------------------------------------------------------


def test_the_strict_checks_are_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("OAUTH_TOKEN_URL_STRICT", raising=False)
    assert config.Settings().oauth_token_url_strict is False
    monkeypatch.setenv("OAUTH_TOKEN_URL_STRICT", "true")
    assert config.Settings().oauth_token_url_strict is True


# -- the URL guard ------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ACCEPTED_BY_DEFAULT)
async def test_a_token_url_that_worked_before_is_accepted(url):
    await assert_token_endpoint_allowed(url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "addresses",
    [("127.0.0.1",), ("::1",), ("169.254.10.10",), ("0.0.0.0",), ("10.0.0.7", "127.0.0.1")],  # noqa: S104
)
async def test_a_name_resolving_to_a_loopback_or_link_local_address_is_accepted(monkeypatch, addresses):
    monkeypatch.setattr(token_endpoint, "_resolve", _resolves_to(*addresses))
    await assert_token_endpoint_allowed("http://localhost:8080/oauth/token")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        *(f"http://[{a}]/token" if ":" in a else f"http://{a}/token" for a in sorted(METADATA_ADDRESSES)),
        "http://[::ffff:169.254.169.254]/latest/meta-data",
        "ws://169.254.169.254/latest/meta-data",
    ],
)
async def test_a_metadata_address_is_refused_and_no_setting_is_offered(url):
    with pytest.raises(TokenEndpointRefusedError, match="cloud metadata endpoint") as refused:
        await assert_token_endpoint_allowed(url)
    assert "OAUTH_TOKEN_URL_STRICT" not in str(refused.value)
    assert "OAUTH_TOKEN_DEV_TRUSTED_ORIGINS" not in str(refused.value)


@pytest.mark.asyncio
async def test_every_resolved_address_is_still_checked_for_metadata(monkeypatch):
    monkeypatch.setattr(token_endpoint, "_resolve", _resolves_to("10.0.0.7", "169.254.169.254"))
    with pytest.raises(TokenEndpointRefusedError, match="cloud metadata endpoint") as refused:
        await assert_token_endpoint_allowed("https://idp.corp.example/token")
    assert "169.254.169.254" not in str(refused.value)


@pytest.mark.asyncio
async def test_a_trusted_origin_changes_nothing_by_default(monkeypatch):
    monkeypatch.setattr(config.settings, "oauth_token_dev_trusted_origins", ["http://localhost:8080"])
    await assert_token_endpoint_allowed("http://127.0.0.1:9000/token")
    with pytest.raises(TokenEndpointRefusedError, match="cloud metadata endpoint"):
        await assert_token_endpoint_allowed("http://169.254.169.254/token")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        "ftp://idp.example/token",
        "https:///token",
        "http://[::1",
        "http://idp.example:abc/token",
        "http://idp.example:99999/token",
    ],
)
async def test_a_url_no_request_can_be_sent_to_is_refused(url):
    with pytest.raises(TokenEndpointRefusedError) as refused:
        await assert_token_endpoint_allowed(url)
    assert "OAUTH_TOKEN_URL_STRICT" not in str(refused.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["ftp://idp.example/token", "not a url"])
async def test_another_scheme_is_told_the_schemes_the_default_accepts(url):
    with pytest.raises(TokenEndpointRefusedError, match=r"^The token URL must be an http, https, ws or wss URL\.$"):
        await assert_token_endpoint_allowed(url)


@pytest.mark.asyncio
async def test_saving_a_url_with_another_scheme_answers_422_naming_those_schemes():
    with pytest.raises(HTTPException) as refused:
        await api_helpers.require_allowed_token_url("ftp://idp.example/token")
    assert refused.value.status_code == 422
    assert refused.value.detail == "The token URL must be an http, https, ws or wss URL."


@pytest.mark.asyncio
async def test_a_name_is_still_resolved_and_the_lookup_still_bounded(monkeypatch):
    looked_up: list[tuple[str, int]] = []

    async def _recording(host, port):
        looked_up.append((host, port))
        return await _resolves_to("10.0.0.7")(host, port)

    monkeypatch.setattr(token_endpoint, "_resolve", _recording)
    await assert_token_endpoint_allowed("https://idp.corp.example/token")
    assert looked_up == [("idp.corp.example", 443)]

    async def _black_holed(_host, _port):
        await asyncio.Event().wait()

    monkeypatch.setattr(token_endpoint, "_resolve", _black_holed)
    monkeypatch.setattr(token_endpoint, "_DNS_TIMEOUT_SECONDS", 0.05)
    await asyncio.wait_for(assert_token_endpoint_allowed("https://idp.slow.example/token"), timeout=5)


# -- when a token URL is saved ----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ACCEPTED_BY_DEFAULT)
async def test_saving_a_token_url_that_worked_before_is_accepted(url):
    await api_helpers.require_allowed_token_url(url)


@pytest.mark.asyncio
async def test_saving_a_metadata_token_url_answers_422():
    with pytest.raises(HTTPException) as refused:
        await api_helpers.require_allowed_token_url("http://169.254.169.254/latest/meta-data")
    assert refused.value.status_code == 422


class _PastTheGuardError(Exception):
    pass


@pytest.mark.asyncio
async def test_the_bundle_import_takes_a_loopback_instance_token_url(monkeypatch):
    parsed = SimpleNamespace(
        metadata=SimpleNamespace(provider="acme-idp", version="1.0.0", icon=None),
        icon_bytes=None,
        metadata_raw={},
        integration_instance=SimpleNamespace(token_url=LOOPBACK_URL, extra_config=None),
    )

    async def _read(_bundle):
        return b"bundle"

    def _next_step(*_args):
        raise _PastTheGuardError

    monkeypatch.setattr(integrations_api, "_read_bundle_upload", _read)
    monkeypatch.setattr(integrations_api, "parse_integration_bundle_zip", lambda _raw: parsed)
    monkeypatch.setattr(integrations_api, "_encode_logo_data_url", _next_step)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)))

    with pytest.raises(_PastTheGuardError):
        await integrations_api.import_integration_bundle(
            request=SimpleNamespace(), bundle=None, user=SimpleNamespace(role="admin", id="u-1"), db=db
        )


@pytest.mark.asyncio
async def test_discovery_keeps_a_discovered_loopback_token_url(monkeypatch):
    from app.services import mcp_oauth_discovery as discovery

    async def _discovered(**_kwargs):
        return discovery.OAuthEndpoints(
            authorize_url="http://127.0.0.1:9000/authorize",
            token_url="http://127.0.0.1:9000/token",
            registration_url=None,
            discovered=True,
        )

    monkeypatch.setattr(discovery, "discover_oauth_endpoints", _discovered)
    db = SimpleNamespace(commit=AsyncMock())
    server = SimpleNamespace(
        id="m-1",
        url="http://127.0.0.1:9000/mcp",
        client_id_enc=encrypt_value("cid"),
        client_secret_enc=None,
        authorize_url=None,
        token_url=None,
        registration_url=None,
        oauth_resource=None,
        scopes=None,
        oauth_metadata_url=None,
    )
    assert await discovery.ensure_mcp_oauth_client(db, server) == discovery.MCPOAuthClientSetup()
    assert server.token_url == "http://127.0.0.1:9000/token"
    db.commit.assert_awaited_once()


# -- before each token request ----------------------------------------------------


class _TokenResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"access_token": "new-access", "expires_in": 3600}


@pytest.fixture
def token_requests(monkeypatch):
    """Where the token requests went; httpx.AsyncClient answers without a network."""
    sent: list[str] = []

    class _Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, url, **_kwargs):
            sent.append(url)
            return _TokenResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    redis = AsyncMock()
    redis.get.return_value = None
    monkeypatch.setattr(token_refresh, "redis_client", AsyncMock(return_value=redis))
    return sent


def _integration(token_url: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="i-1",
        name="IdP",
        provider="custom",
        client_id_enc=encrypt_value("cid"),
        client_secret_enc=encrypt_value("secret"),
        refresh_token_enc=encrypt_value("rt"),
        access_token_enc=None,
        scopes=None,
        token_url=token_url,
        extra_config_enc=None,
        status="connected",
        token_expires_at=None,
    )


def _mcp_server(token_url: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="m-1",
        name="MCP",
        client_id_enc=encrypt_value("cid"),
        client_secret_enc=None,
        refresh_token_enc=encrypt_value("rt"),
        access_token_enc=None,
        scopes=None,
        oauth_resource=None,
        token_url=token_url,
        oauth_status="connected",
        token_expires_at=None,
    )


@pytest.mark.asyncio
async def test_the_integration_code_exchange_reaches_a_loopback_token_url(token_requests):
    tokens = await oauth.exchange_code_for_tokens_with_integration(_integration(LOOPBACK_URL), "code")
    assert tokens["access_token"] == "new-access"
    assert token_requests == [LOOPBACK_URL]


@pytest.mark.asyncio
async def test_the_mcp_code_exchange_reaches_a_loopback_token_url(token_requests):
    tokens = await oauth.exchange_code_for_tokens_with_mcp_server(_mcp_server(LOOPBACK_URL), "code")
    assert tokens["access_token"] == "new-access"
    assert token_requests == [LOOPBACK_URL]


@pytest.mark.asyncio
async def test_the_scheduled_integration_refresh_reaches_a_loopback_token_url(token_requests):
    http = SimpleNamespace(post=AsyncMock(return_value=_TokenResponse()))
    integration = _integration(LOOPBACK_URL)
    assert await token_refresh._do_refresh_integration(integration, http) is True
    assert http.post.await_args.args[0] == LOOPBACK_URL
    assert integration.status == "connected"


@pytest.mark.asyncio
async def test_the_scheduled_mcp_refresh_reaches_a_loopback_token_url(token_requests):
    http = SimpleNamespace(post=AsyncMock(return_value=_TokenResponse()))
    server = _mcp_server(LOOPBACK_URL)
    assert await token_refresh._do_refresh_mcp(server, http) is True
    assert http.post.await_args.args[0] == LOOPBACK_URL
    assert server.oauth_status == "connected"


@pytest.mark.asyncio
async def test_the_inline_refresh_reaches_a_loopback_token_url(token_requests):
    db = SimpleNamespace(commit=AsyncMock())
    tokens_unchanged = AsyncMock(return_value=True)
    assert await integration_tools._do_inline_refresh(db, _integration(LOOPBACK_URL), tokens_unchanged) == "new-access"
    assert token_requests == [LOOPBACK_URL]


# -- what the default still refuses, before each token request ---------------------

METADATA_URL = "http://169.254.169.254/latest/meta-data"


@pytest.mark.asyncio
async def test_the_integration_code_exchange_refuses_a_metadata_token_url(token_requests):
    with pytest.raises(TokenEndpointRefusedError, match="cloud metadata endpoint"):
        await oauth.exchange_code_for_tokens_with_integration(_integration(METADATA_URL), "code")
    assert token_requests == []


@pytest.mark.asyncio
async def test_the_mcp_code_exchange_refuses_a_metadata_token_url(token_requests):
    with pytest.raises(TokenEndpointRefusedError, match="cloud metadata endpoint"):
        await oauth.exchange_code_for_tokens_with_mcp_server(_mcp_server(METADATA_URL), "code")
    assert token_requests == []


@pytest.mark.asyncio
async def test_the_scheduled_integration_refresh_refuses_a_metadata_token_url(token_requests):
    http = SimpleNamespace(post=AsyncMock(side_effect=AssertionError("request sent")))
    integration = _integration(METADATA_URL)
    assert await token_refresh._do_refresh_integration(integration, http) is False
    http.post.assert_not_awaited()
    assert integration.status == "error"
    token_refresh.redis_client.return_value.set.assert_awaited_once_with(
        "token_refresh_cooldown:i-1", "1", ex=token_refresh._AUTH_FAIL_COOLDOWN
    )


@pytest.mark.asyncio
async def test_the_scheduled_mcp_refresh_refuses_a_metadata_token_url(token_requests):
    http = SimpleNamespace(post=AsyncMock(side_effect=AssertionError("request sent")))
    server = _mcp_server(METADATA_URL)
    assert await token_refresh._do_refresh_mcp(server, http) is False
    http.post.assert_not_awaited()
    assert server.oauth_status == "error"


@pytest.mark.asyncio
async def test_the_inline_refresh_refuses_a_metadata_token_url(token_requests):
    db = SimpleNamespace(commit=AsyncMock())
    tokens_unchanged = AsyncMock(return_value=True)
    assert await integration_tools._do_inline_refresh(db, _integration(METADATA_URL), tokens_unchanged) is None
    assert token_requests == []
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_discovery_refuses_a_discovered_metadata_token_url(monkeypatch):
    from app.services import mcp_oauth_discovery as discovery

    async def _discovered(**_kwargs):
        return discovery.OAuthEndpoints(
            authorize_url="https://auth.example.com/authorize",
            token_url=METADATA_URL,
            registration_url=None,
            discovered=True,
        )

    monkeypatch.setattr(discovery, "discover_oauth_endpoints", _discovered)
    db = SimpleNamespace(commit=AsyncMock())
    server = SimpleNamespace(
        id="m-1",
        url="https://mcp.example.com/mcp",
        client_id_enc=encrypt_value("cid"),
        client_secret_enc=None,
        authorize_url=None,
        token_url=None,
        registration_url=None,
        oauth_resource=None,
        scopes=None,
        oauth_metadata_url=None,
    )
    with pytest.raises(discovery.MCPOAuthDiscoveryError, match="cloud metadata endpoint"):
        await discovery.ensure_mcp_oauth_client(db, server)
    db.commit.assert_not_awaited()


# -- the security audit -----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strict", "finding_id", "severity"),
    [
        (False, "integrations.oauth_token_url_strict_off", "info"),
        (True, "integrations.oauth_token_url_strict_on", "pass"),
    ],
)
async def test_the_audit_mentions_strict_mode_without_alarm(monkeypatch, strict, finding_id, severity):
    # No stored token URL relies on the default: nothing in use to warn about.
    monkeypatch.setattr(config.settings, "oauth_token_url_strict", strict)

    class _NoTokenUrls:
        async def execute(self, *_args, **_kwargs):
            return SimpleNamespace(all=lambda: [])

    db = _NoTokenUrls()
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_oauth_token_url_policy(db, findings)
    assert [(f.id, f.category, f.severity, f.control) for f in findings] == [
        (finding_id, "integrations", severity, "integrations.oauth_token_url_strict")
    ]
    if not strict:
        recommendation = findings[0].recommendation or ""
        assert 'Turn on "Strict OAuth token URL checks" in Settings > Access and security' in recommendation
        assert "OAUTH_TOKEN_URL_STRICT=true" in recommendation
        # The field that keeps a development machine working, as the card names it.
        assert '"Loopback token URL origins (development)" (OAUTH_TOKEN_DEV_TRUSTED_ORIGINS)' in recommendation

    async def _no_finding(*_args, **_kwargs) -> None:
        return None

    def _no_finding_sync(*_args, **_kwargs) -> None:
        return None

    for name in dir(security_audit):
        if name.startswith("_check_") and name != "_check_oauth_token_url_policy":
            check = getattr(security_audit, name)
            monkeypatch.setattr(
                security_audit, name, _no_finding if inspect.iscoroutinefunction(check) else _no_finding_sync
            )
    report = await security_audit.run_security_audit(admin=object(), db=db)  # type: ignore[arg-type]
    assert report.overall_score == "healthy"
