"""Where a vault provider connected in the UI may be reached (``app/services/vault/destinations.py``).

Connections are driven against real sockets on loopback: a stand-in server
records the requests it accepts, so "refused" means no request reached it,
not merely that an error came back. DNS is a stand-in (``destinations._resolve``).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
import pytest

from app.core.config import Settings, settings
from app.services.vault import destinations
from app.services.vault.config import VaultConfigError, database_config, read_declarations
from app.services.vault.destinations import (
    ADDRESS_SETTINGS,
    REFUSED_DETAIL,
    VaultAddressRefusedError,
    address_refusal,
    assert_provider_addresses_allowed,
)
from app.services.vault.errors import VaultReadError


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """No proxy from the machine running the tests, no trusted origin."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings, "secret_vault_trusted_origins", [])


@pytest.fixture
def dns(monkeypatch):
    """Stand in for DNS: name -> addresses; lookups recorded under ``"calls"``."""
    table: dict[str, list[str]] = {}
    calls: list[tuple[str, int]] = []

    async def _resolve(host: str, port: int) -> list:
        calls.append((host, port))
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "unknown")
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in table[host]]

    monkeypatch.setattr(destinations, "_resolve", _resolve)
    table["calls"] = calls  # type: ignore[assignment]
    return table


def _trust(monkeypatch, *origins: str) -> None:
    monkeypatch.setattr(settings, "secret_vault_trusted_origins", list(origins))


# ---------------------------------------------------------------------------
# The policy, one address class per row
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "refused"),
    [
        ("169.254.169.254", "cloud metadata endpoint"),
        ("169.254.170.2", "cloud metadata endpoint"),
        ("fd00:ec2::254", "cloud metadata endpoint"),
        ("100.100.100.200", "cloud metadata endpoint"),
        ("::ffff:169.254.169.254", "cloud metadata endpoint"),
        ("64:ff9b::a9fe:a9fe", "cloud metadata endpoint"),
        ("169.254.10.20", "link-local address"),
        ("fe80::1", "link-local address"),
        ("127.0.0.1", "loopback address"),
        ("::1", "loopback address"),
        ("0.0.0.0", "reserved address"),  # noqa: S104 - an address judged, nothing bound
        ("224.0.0.1", "reserved address"),
        ("10.0.0.5", None),
        ("172.16.4.2", None),
        ("192.168.1.20", None),
        ("100.64.0.9", None),
        ("fd12:3456::1", None),
        ("93.184.216.34", None),
    ],
)
def test_a_vault_is_reached_on_a_private_or_public_address_never_an_internal_one(address, refused):
    url = httpx.URL("https://vault.example.com:8200")
    assert address_refusal(url, ipaddress.ip_address(address)) == refused


def test_a_trusted_origin_reaches_loopback_and_still_never_link_local_or_metadata(monkeypatch):
    _trust(monkeypatch, "http://127.0.0.1:8200", "http://169.254.169.254", "http://vault.local:8200")
    assert address_refusal(httpx.URL("http://127.0.0.1:8200/v1"), ipaddress.ip_address("127.0.0.1")) is None
    assert address_refusal(httpx.URL("http://127.0.0.1:8201/v1"), ipaddress.ip_address("127.0.0.1")) == "loopback address"
    assert (
        address_refusal(httpx.URL("http://169.254.169.254/"), ipaddress.ip_address("169.254.169.254"))
        == "cloud metadata endpoint"
    )
    assert address_refusal(httpx.URL("http://vault.local:8200"), ipaddress.ip_address("169.254.3.4")) == "link-local address"


def test_the_trusted_origins_are_an_environment_variable_parsed_like_every_allow_list():
    parsed = Settings(_env_file=None, secret_vault_trusted_origins="HTTP://127.0.0.1:8200, https://Localhost:8443")
    assert parsed.secret_vault_trusted_origins == ["http://127.0.0.1:8200", "https://localhost:8443"]
    with pytest.raises(ValueError, match="SECRET_VAULT_TRUSTED_ORIGINS"):
        Settings(_env_file=None, secret_vault_trusted_origins="127.0.0.1:8200")


def test_the_trusted_origins_variable_is_not_read_as_a_vault_provider():
    declared = read_declarations({"SECRET_VAULT_TRUSTED_ORIGINS": "http://127.0.0.1:8200"})
    assert dict(declared.providers) == {} and dict(declared.errors) == {}


# ---------------------------------------------------------------------------
# On save and on a test: what the address spells and resolves to now
# ---------------------------------------------------------------------------


def _config(kind: str, address: str):
    settings_by_type = {
        "hashicorp": {"address": address},
        "onepassword": {"address": address},
        "aws": {"endpoint_url": address},
        "azure": {"vault_url": address},
    }
    return database_config(name="corp", type=kind, settings=settings_by_type[kind], credentials={})


@pytest.mark.parametrize("kind", ["hashicorp", "onepassword", "aws"])
@pytest.mark.parametrize(
    ("address", "answers"),
    [
        ("https://169.254.169.254", {}),
        ("https://[::ffff:169.254.169.254]:8200", {}),
        ("https://0xa9fea9fe:8200", {}),
        ("https://2852039166", {}),
        ("https://127.0.0.1:8200", {}),
        ("https://localhost:8200", {"localhost": ["127.0.0.1"]}),
        ("https://vault.example.com", {"vault.example.com": ["169.254.169.254"]}),
        ("https://vault.example.com", {"vault.example.com": ["10.0.0.5", "169.254.169.254"]}),
        ("https://vault.example.com", {"vault.example.com": ["169.254.0.7"]}),
    ],
    ids=["metadata", "mapped", "hex-shorthand", "int-shorthand", "loopback", "localhost", "name", "one-of-two", "link-local"],
)
async def test_every_address_setting_of_a_ui_provider_is_refused_with_one_generic_message(kind, address, answers, dns, caplog):
    dns.update(answers)
    with caplog.at_level("INFO", logger="app.services.vault.destinations"), pytest.raises(VaultAddressRefusedError) as exc:
        await assert_provider_addresses_allowed(_config(kind, address))
    assert str(exc.value) == REFUSED_DETAIL
    # Why goes to the log only: the message is the same whatever the class or the resolution.
    assert "169.254" not in str(exc.value) and "127.0.0.1" not in str(exc.value)
    assert "Vault address refused" in caplog.text


@pytest.mark.parametrize(
    ("address", "answers"),
    [
        ("https://10.0.0.5:8200", {}),
        ("https://192.168.1.20", {}),
        ("https://vault.corp.internal:8200", {"vault.corp.internal": ["172.16.4.2"]}),
        ("https://vault.example.com", {"vault.example.com": ["93.184.216.34"]}),
        ("https://vault.not-up-yet.internal", {}),
    ],
    ids=["private", "private-2", "private-name", "public-name", "unresolved"],
)
async def test_a_private_or_public_vault_is_accepted_and_a_name_that_does_not_resolve_is_left_to_the_connection(
    address, answers, dns
):
    dns.update(answers)
    await assert_provider_addresses_allowed(_config("hashicorp", address))


async def test_a_resolved_name_is_judged_on_the_port_the_request_uses(dns):
    dns["vault.corp.internal"] = ["10.0.0.5"]
    await assert_provider_addresses_allowed(_config("hashicorp", "https://vault.corp.internal:8200"))
    await assert_provider_addresses_allowed(_config("hashicorp", "http://vault.corp.internal"))
    assert dns["calls"] == [("vault.corp.internal", 8200), ("vault.corp.internal", 80)]


async def test_a_provider_declared_in_the_environment_is_the_operators_and_is_not_judged(dns):
    declared = read_declarations(
        {"SECRET_VAULT_LOCAL_TYPE": "hashicorp", "SECRET_VAULT_LOCAL_ADDRESS": "http://127.0.0.1:8200"}
    )
    await assert_provider_addresses_allowed(declared.providers["local"])
    assert dns["calls"] == []


async def test_a_trusted_origin_saves_an_on_host_vault(monkeypatch, dns):
    _trust(monkeypatch, "http://127.0.0.1:8200")
    await assert_provider_addresses_allowed(_config("hashicorp", "http://127.0.0.1:8200"))
    with pytest.raises(VaultAddressRefusedError):
        await assert_provider_addresses_allowed(_config("hashicorp", "http://127.0.0.1:9200"))


# ---------------------------------------------------------------------------
# The hosts another rule holds: each one is judged once
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "vault_url",
    ["https://169.254.169.254", "https://[::ffff:169.254.169.254]", "https://127.0.0.1:8443", "https://localhost", "https://vault.example.com"],
)
async def test_an_azure_vault_url_is_held_to_an_azure_key_vault_host_not_to_the_address_policy(vault_url, dns):
    """A UI provider's VAULT_URL is a key vault of an Azure cloud (azure.KEY_VAULT_DOMAINS): no internal address passes it."""
    from app.services.vault.resolver import build_provider

    dns["vault.example.com"] = ["169.254.169.254"]
    config = _config("azure", vault_url)
    with pytest.raises(VaultConfigError, match="URL of an Azure key vault"):
        build_provider(config)
    await assert_provider_addresses_allowed(config)
    assert "azure" not in ADDRESS_SETTINGS and dns["calls"] == []


async def test_a_google_service_account_key_is_exchanged_at_googles_token_endpoint_whatever_it_names(monkeypatch):
    """google-auth posts the key's signed assertion to its token_uri: a key pasted in the UI never chooses that address."""
    import google.auth.transport.requests as google_requests
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from app.services.vault.gcp import TOKEN_URI, GoogleSecretManagerProvider

    pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        .decode()
    )
    key = {
        "type": "service_account",
        "private_key": pem,
        "client_email": "reader@acme-prod.iam.gserviceaccount.com",
        "project_id": "acme-prod",
        "token_uri": "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token",
    }
    posted: list[str] = []

    class _Response:
        status = 200
        headers: dict[str, str] = {}
        data = json.dumps({"access_token": "ya29.token", "expires_in": 3600}).encode()

    class _Request:
        def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
            posted.append(url)
            return _Response()

    monkeypatch.setattr(google_requests, "Request", _Request)
    provider = GoogleSecretManagerProvider(
        database_config(
            name="gcp", type="gcp", settings={"auth_method": "service_account_key"}, credentials={"service_account_key": json.dumps(key)}
        )
    )
    await provider.authenticate()
    assert posted == [TOKEN_URI]


# ---------------------------------------------------------------------------
# On every connection: the address connected to, no redirect, the proxy
# ---------------------------------------------------------------------------


@dataclass
class _Listener:
    port: int
    requests: list[str] = field(default_factory=list)


@asynccontextmanager
async def _listener(*, location: str | None = None, body: bytes = b"{}") -> AsyncIterator[_Listener]:
    """A loopback HTTP server (or proxy): a 307 to *location*, else a 200, recording request lines."""
    record = _Listener(port=0)

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(asyncio.IncompleteReadError):
            head = await reader.readuntil(b"\r\n\r\n")
            record.requests.append(head.split(b"\r\n", 1)[0].decode())
        status = f"307 Temporary Redirect\r\nLocation: {location}" if location else "200 OK"
        writer.write(
            f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    record.port = server.sockets[0].getsockname()[1]
    try:
        yield record
    finally:
        server.close()
        await server.wait_closed()


def _hashicorp(address: str, *, source: str = "database"):
    from app.services.vault.hashicorp import HashiCorpVaultProvider

    if source == "environment":
        declared = read_declarations(
            {"SECRET_VAULT_CORP_TYPE": "hashicorp", "SECRET_VAULT_CORP_ADDRESS": address, "SECRET_VAULT_CORP_TOKEN": "hvs.t"}
        )
        return HashiCorpVaultProvider(declared.providers["corp"])
    return HashiCorpVaultProvider(
        database_config(name="corp", type="hashicorp", settings={"address": address}, credentials={"token": "hvs.t"})
    )


def _ui_provider(kind: str, address: str):
    from app.services.vault.resolver import build_provider

    token = {"hashicorp": "hvs.t", "onepassword": "ops.t"}[kind]
    return build_provider(database_config(name="corp", type=kind, settings={"address": address}, credentials={"token": token}))


@pytest.mark.parametrize("kind", ["hashicorp", "onepassword"])
async def test_a_ui_provider_never_connects_to_a_loopback_vault_it_was_not_trusted_with(kind, dns):
    async with _listener() as vault:
        provider = _ui_provider(kind, f"http://127.0.0.1:{vault.port}")
        try:
            with pytest.raises(httpx.ConnectError, match="This vault address is not allowed"):
                await provider._http().get(f"http://127.0.0.1:{vault.port}/v1/sys/health")
        finally:
            await provider.aclose()
    assert vault.requests == []


async def test_the_hashicorp_client_connects_to_the_address_it_checked_never_one_the_name_resolves_to_later(
    monkeypatch, dns
):
    async with _listener(body=b'{"data": {}}') as vault:
        _trust(monkeypatch, f"http://vault.corp.internal:{vault.port}")
        # The save-time check saw a private address; by the connection the name answers a metadata one.
        dns["vault.corp.internal"] = ["10.0.0.5"]
        provider = _hashicorp(f"http://vault.corp.internal:{vault.port}")
        await assert_provider_addresses_allowed(provider.config)
        dns["vault.corp.internal"] = ["169.254.169.254"]
        try:
            with pytest.raises(VaultReadError) as exc:
                await provider.authenticate()
            assert exc.value.reason == "unreachable"
            # The trusted origin's own loopback answer is connected to: the transport's lookup, not the system's.
            dns["vault.corp.internal"] = ["127.0.0.1"]
            await provider.authenticate()
        finally:
            await provider.aclose()
    assert vault.requests == ["GET /v1/auth/token/lookup-self HTTP/1.1"]
    assert dns["calls"][-2:] == [("vault.corp.internal", vault.port), ("vault.corp.internal", vault.port)]


async def test_the_hashicorp_client_does_not_follow_a_redirect(monkeypatch, dns):
    async with _listener() as elsewhere, _listener(location=f"http://127.0.0.1:{elsewhere.port}/latest/meta-data/") as vault:
        # Both trusted: what stops the second hop is that no redirect is followed, not the policy.
        _trust(monkeypatch, f"http://127.0.0.1:{vault.port}", f"http://127.0.0.1:{elsewhere.port}")
        provider = _hashicorp(f"http://127.0.0.1:{vault.port}")
        try:
            with pytest.raises(VaultReadError) as exc:
                await provider.authenticate()
        finally:
            await provider.aclose()
    assert (exc.value.reason, exc.value.detail) == ("unreachable", "HTTP 307")
    assert vault.requests == ["GET /v1/auth/token/lookup-self HTTP/1.1"]
    assert elsewhere.requests == []


async def test_behind_a_proxy_the_address_is_judged_from_here_before_the_request_leaves(monkeypatch, dns, caplog):
    dns["vault.corp.internal"] = ["10.0.0.5"]
    dns["metadata.example.com"] = ["169.254.169.254"]
    async with _listener(body=b'{"data": {}}') as proxy:
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.port}")
        reached = _hashicorp("http://vault.corp.internal:8200")
        refused = _hashicorp("http://metadata.example.com")
        try:
            await reached.authenticate()
            with caplog.at_level("WARNING", logger="app.services.vault.destinations"), pytest.raises(VaultReadError) as exc:
                await refused.authenticate()
        finally:
            await reached.aclose()
            await refused.aclose()
    assert exc.value.reason == "unreachable"
    assert proxy.requests == ["GET http://vault.corp.internal:8200/v1/auth/token/lookup-self HTTP/1.1"]
    assert "through the outbound proxy" in caplog.text


async def test_a_provider_declared_in_the_environment_keeps_its_client_as_it_was(dns):
    async with _listener(body=b'{"data": {}}') as vault:
        provider = _hashicorp(f"http://127.0.0.1:{vault.port}", source="environment")
        try:
            await provider.authenticate()
        finally:
            await provider.aclose()
    assert vault.requests == ["GET /v1/auth/token/lookup-self HTTP/1.1"]
    assert dns["calls"] == []


async def test_aws_judges_its_endpoint_before_building_a_client_since_the_sdk_resolves_itself(monkeypatch, dns):
    from app.services.vault.aws import AWSSecretsManagerProvider

    dns["vpce.example.com"] = ["169.254.169.254"]
    provider = AWSSecretsManagerProvider(
        database_config(
            name="corp",
            type="aws",
            settings={"region": "eu-west-1", "endpoint_url": "https://vpce.example.com", "auth_method": "access_key", "access_key_id": "AKIA"},
            credentials={"secret_access_key": "s"},
        )
    )
    built: list[bool] = []
    monkeypatch.setattr(provider, "_build_client", lambda: built.append(True))
    with pytest.raises(VaultReadError) as exc:
        await provider.read("prod/github")
    assert exc.value.reason == "misconfigured"
    assert built == []
    dns["vpce.example.com"] = ["10.0.0.5"]
    monkeypatch.setattr(provider, "_build_client", lambda: built.append(True) or _StubSecretsManager())
    assert await provider.read("prod/github") == "value"
    assert built == [True]


class _StubSecretsManager:
    def get_secret_value(self, SecretId: str) -> dict[str, str]:  # noqa: N803 - the SDK's spelling
        return {"SecretString": "value"}
