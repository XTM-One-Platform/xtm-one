"""The MCP server URL policy (``app/tools/mcp_destinations.py``), one table per decision."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
import pytest

from app.core import security_options
from app.tools import mcp_destinations
from app.tools.mcp_destinations import (
    REFUSED_DETAIL,
    MCPServerURLRefusedError,
    address_refusal,
    assert_server_url_allowed,
    url_problem,
)


def _policy(policy: str, *origins: str) -> None:
    """Set both options as Settings > Access and security stores them (normalized by the registry)."""
    options = security_options.SECURITY_OPTIONS
    security_options.publish_stored(
        {
            "mcp_server_url_policy": options["mcp_server_url_policy"].normalize(policy),
            "mcp_server_trusted_origins": options["mcp_server_trusted_origins"].normalize(list(origins)),
        }
    )


@pytest.fixture
def dns(monkeypatch):
    """Stand in for DNS: name -> addresses; lookups recorded."""
    table: dict[str, list[str]] = {}
    calls: list[tuple[str, int]] = []

    async def _resolve(host: str, port: int) -> list:
        calls.append((host, port))
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "unknown")
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in table[host]]

    monkeypatch.setattr(mcp_destinations, "_resolve", _resolve)
    table["calls"] = calls  # type: ignore[assignment]
    return table


_ADDRESSES = {
    "public": "93.184.216.34",
    "private": "10.1.2.3",
    "shared": "100.64.0.9",
    "loopback": "127.0.0.1",
    "loopback6": "::1",
    "link_local": "169.254.10.10",
    "link_local6": "fe80::1",
    "unspecified": "0.0.0.0",  # noqa: S104 - an address judged, never bound
    "metadata": "169.254.169.254",
    "metadata_ecs": "169.254.170.2",
    "metadata6": "fd00:ec2::254",
    "metadata_mapped": "::ffff:169.254.169.254",
    "private_mapped": "::ffff:10.1.2.3",
    # NAT64 (RFC 6052): what a DNS64 resolver answers for an IPv4-only name.
    "nat64_public": "64:ff9b::5db8:d822",
    "nat64_metadata": "64:ff9b::a9fe:a9fe",
    "ula6": "fd12:3456::1",
}

# (class, permissive, private_networks, public_only): True = admitted.
_TABLE = [
    ("public", True, True, True),
    ("private", True, True, False),
    ("shared", True, True, False),
    ("ula6", True, True, False),
    ("private_mapped", True, True, False),
    ("loopback", True, False, False),
    ("loopback6", True, False, False),
    ("link_local", True, False, False),
    ("link_local6", True, False, False),
    ("unspecified", True, False, False),
    ("metadata", False, False, False),
    ("metadata_ecs", False, False, False),
    ("metadata6", False, False, False),
    ("metadata_mapped", False, False, False),
    ("nat64_public", True, True, True),
    ("nat64_metadata", False, False, False),
]


@pytest.mark.parametrize(("klass", "permissive", "private_networks", "public_only"), _TABLE, ids=[row[0] for row in _TABLE])
def test_every_address_class_under_every_policy(klass, permissive, private_networks, public_only):
    url = httpx.URL("https://mcp.example.com/mcp")
    ip = ipaddress.ip_address(_ADDRESSES[klass])
    for policy, admitted in (("permissive", permissive), ("private_networks", private_networks), ("public_only", public_only)):
        _policy(policy)
        assert (address_refusal(url, ip) is None) is admitted, (policy, klass)


def test_the_default_policy_is_permissive():
    assert security_options.security_option("mcp_server_url_policy") == "permissive"
    assert address_refusal(httpx.URL("http://localhost/"), ipaddress.ip_address("127.0.0.1")) is None


@pytest.mark.parametrize("policy", ["private_networks", "public_only"])
def test_a_trusted_origin_is_admitted_at_any_address_but_a_metadata_one(policy):
    _policy(policy, "http://mcp.internal:8080")
    trusted = httpx.URL("http://mcp.internal:8080/mcp")
    assert address_refusal(trusted, ipaddress.ip_address("127.0.0.1")) is None
    assert address_refusal(trusted, ipaddress.ip_address("10.0.0.5")) is None
    assert address_refusal(trusted, ipaddress.ip_address("169.254.169.254")) == "cloud metadata endpoint"
    # The origin, exactly: another port, scheme or host is not covered.
    for other in ("http://mcp.internal:8081/", "https://mcp.internal:8080/", "http://other.internal:8080/"):
        assert address_refusal(httpx.URL(other), ipaddress.ip_address("127.0.0.1")) is not None, other


def test_a_trusted_origin_matches_with_or_without_its_default_port():
    _policy("public_only", "http://mcp.internal:80", "https://[fd12::1]")
    ip = ipaddress.ip_address("10.0.0.5")
    assert address_refusal(httpx.URL("http://mcp.internal/"), ip) is None
    assert address_refusal(httpx.URL("http://mcp.internal:80/"), ip) is None
    assert address_refusal(httpx.URL("https://[fd12::1]:443/"), ip) is None


@pytest.mark.parametrize(
    ("url", "problem"),
    [
        ("ftp://mcp.example.com/", "The MCP server URL must be an http or https URL."),
        ("ws://mcp.example.com/", "The MCP server URL must be an http or https URL."),
        ("mcp.example.com/mcp", "The MCP server URL must be an http or https URL."),
        ("https:///mcp", "The MCP server URL has no host."),
        ("http://mcp.example.com:99999/", "The MCP server URL has an invalid port."),
        ("http://[::1/", "The MCP server URL is not a valid URL."),
        ("http://169.254.169.254/latest/meta-data/", REFUSED_DETAIL),
        ("http://0xa9fea9fe/", REFUSED_DETAIL),
        ("http://2852039166/", REFUSED_DETAIL),
        ("http://169.254.43518/", REFUSED_DETAIL),
        ("http://[fd00:ec2::254]/", REFUSED_DETAIL),
        ("http://[::ffff:169.254.169.254]/", REFUSED_DETAIL),
        ("  http://169.254.169.254/  ", REFUSED_DETAIL),
        ("http://localhost:8080/mcp", None),
        ("https://mcp.example.com/mcp", None),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_url_problem_needs_no_network_and_reads_the_url_as_httpx_does(url, problem, dns):
    _policy("public_only")
    assert url_problem(url) == problem
    assert dns["calls"] == []


async def test_a_name_resolving_to_a_refused_address_is_refused_on_save(dns):
    _policy("private_networks")
    dns["rebind.example.com"] = ["93.184.216.34", "127.0.0.1"]
    with pytest.raises(MCPServerURLRefusedError) as refused:
        await assert_server_url_allowed("https://rebind.example.com/mcp", routed=False)
    assert str(refused.value) == REFUSED_DETAIL
    assert dns["calls"] == [("rebind.example.com", 443)]


async def test_the_refusal_names_no_address_or_class(dns, caplog):
    _policy("public_only")
    dns["intranet.corp"] = ["10.9.8.7"]
    with caplog.at_level("INFO", logger="app.tools.mcp_destinations"), pytest.raises(MCPServerURLRefusedError) as refused:
        await assert_server_url_allowed("http://intranet.corp:9000/mcp", routed=False)
    assert "10.9.8.7" not in str(refused.value) and "private" not in str(refused.value)
    # The operator's log has the reason.
    assert "'intranet.corp' resolves to 10.9.8.7, a private address" in caplog.text


async def test_a_name_that_does_not_resolve_is_accepted_on_save(dns):
    _policy("public_only")
    await assert_server_url_allowed("https://not-yet-up.example.com/mcp", routed=False)
    assert dns["calls"] == [("not-yet-up.example.com", 443)]


async def test_a_routed_server_is_judged_on_what_its_url_spells_only(dns):
    _policy("public_only")
    dns["mcp.corp.local"] = ["10.0.0.1"]
    await assert_server_url_allowed("http://mcp.corp.local:8080/mcp", routed=True)
    await assert_server_url_allowed("http://10.0.0.1:8080/mcp", routed=True)
    assert dns["calls"] == []
    with pytest.raises(MCPServerURLRefusedError):
        await assert_server_url_allowed("http://169.254.169.254/", routed=True)


async def test_a_literal_address_is_judged_without_a_lookup(dns):
    _policy("private_networks")
    for url in ("http://127.0.0.1:8080/", "http://[::1]:8080/", "http://0x7f000001/", "http://[::ffff:127.0.0.1]/"):
        with pytest.raises(MCPServerURLRefusedError):
            await assert_server_url_allowed(url, routed=False)
    await assert_server_url_allowed("http://10.0.0.1:8080/", routed=False)
    assert dns["calls"] == []


async def test_the_name_looked_up_is_the_one_the_request_sends(dns):
    # httpx sends a non-ASCII host IDNA-encoded; that is the name to judge.
    _policy("public_only")
    dns["xn--strae-oqa.example"] = ["10.0.0.1"]
    with pytest.raises(MCPServerURLRefusedError):
        await assert_server_url_allowed("https://straße.example/mcp", routed=False)


async def test_a_guarded_client_refuses_on_the_connected_address(dns):
    # Save time saw a public address; the connection resolves again (its own
    # single lookup) and is refused on what it gets.
    _policy("private_networks")
    dns["mcp.example.com"] = ["127.0.0.1"]
    async with mcp_destinations.guarded_client(route=None) as client:
        assert client._trust_env is False
        with pytest.raises(httpx.ConnectError, match="This MCP server URL is not allowed"):
            await client.get("http://mcp.example.com:9/mcp")
    assert dns["calls"] == [("mcp.example.com", 9)]


# ---------------------------------------------------------------------------
# Connections: redirects, outbound proxies, lazy transports
# ---------------------------------------------------------------------------


@dataclass
class _Listener:
    port: int
    requests: list[str] = field(default_factory=list)


@asynccontextmanager
async def _listener(*, location: str | None = None) -> AsyncIterator[_Listener]:
    """A loopback HTTP server (or proxy): a 302 to *location*, else a 200, recording request lines."""
    record = _Listener(port=0)

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(asyncio.IncompleteReadError):
            head = await reader.readuntil(b"\r\n\r\n")
            record.requests.append(head.split(b"\r\n", 1)[0].decode())
        status = f"302 Found\r\nLocation: {location}" if location else "200 OK"
        writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok".encode())
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    record.port = server.sockets[0].getsockname()[1]
    try:
        yield record
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture(autouse=True)
def _no_proxy_variables(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)


async def test_a_redirect_to_a_metadata_address_is_refused_by_the_default_policy(dns):
    # The real policy (not a test check): permissive reaches loopback, and the
    # hop to the metadata address is refused before it connects.
    async with (
        _listener(location="http://169.254.169.254/latest/meta-data/") as redirector,
        mcp_destinations.guarded_client(route=None, follow_redirects=True) as client,
    ):
        with pytest.raises(httpx.ConnectError, match="This MCP server URL is not allowed"):
            await client.get(f"http://127.0.0.1:{redirector.port}/mcp")
    assert redirector.requests == ["GET /mcp HTTP/1.1"]


async def test_permissive_keeps_using_the_environment_proxy(dns, monkeypatch):
    async with _listener() as proxy:
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.port}")
        async with mcp_destinations.guarded_client(route=None) as client:
            response = await client.get("http://mcp.example.com/mcp")
    assert response.status_code == 200
    assert proxy.requests == ["GET http://mcp.example.com/mcp HTTP/1.1"]
    # The name is looked up once from here, for the metadata check only.
    assert dns["calls"] == [("mcp.example.com", 80)]


@pytest.mark.parametrize(
    ("url", "answers"),
    [
        ("http://169.254.169.254/latest/meta-data/", {}),
        ("http://0xa9fea9fe/", {}),
        ("http://metadata.example.com/", {"metadata.example.com": ["169.254.169.254"]}),
    ],
    ids=["literal", "shorthand", "name"],
)
async def test_permissive_refuses_a_metadata_address_before_the_proxy(url, answers, dns, monkeypatch, caplog):
    dns.update(answers)
    async with _listener() as proxy:
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.port}")
        async with mcp_destinations.guarded_client(route=None) as client:
            with caplog.at_level("WARNING", logger="app.tools.mcp_destinations"), pytest.raises(
                httpx.ConnectError, match="This MCP server URL is not allowed"
            ):
                await client.get(url)
    assert proxy.requests == []
    assert "through the outbound proxy" in caplog.text


async def test_a_strict_policy_never_uses_the_proxy(dns, monkeypatch, caplog):
    monkeypatch.setattr(mcp_destinations, "_proxy_ignored_warned", False)
    _policy("private_networks")
    dns["mcp.example.com"] = ["127.0.0.1"]
    async with _listener() as proxy:
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.port}")
        async with mcp_destinations.guarded_client(route=None) as client:
            with caplog.at_level("WARNING", logger="app.tools.mcp_destinations"):
                for _ in range(2):
                    with pytest.raises(httpx.ConnectError, match="This MCP server URL is not allowed"):
                        await client.get("http://mcp.example.com/mcp")
    assert proxy.requests == []
    assert caplog.text.count("the outbound proxy in HTTP_PROXY / HTTPS_PROXY is not used") == 1


async def test_no_proxy_sends_a_permissive_request_directly(dns, monkeypatch):
    async with _listener() as proxy, _listener() as target:
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.port}")
        monkeypatch.setenv("NO_PROXY", "mcp.internal")
        dns["mcp.internal"] = ["127.0.0.1"]
        async with mcp_destinations.guarded_client(route=None) as client:
            response = await client.get(f"http://mcp.internal:{target.port}/mcp")
    assert response.status_code == 200
    assert proxy.requests == []
    assert target.requests == ["GET /mcp HTTP/1.1"]


async def test_no_transport_is_built_until_a_request_needs_one(monkeypatch):
    built: list[str] = []
    real = mcp_destinations.guarded_transport
    monkeypatch.setattr(mcp_destinations, "guarded_transport", lambda: built.append("pinned") or real())
    async with mcp_destinations.guarded_client(route=None):
        pass
    assert built == []


def test_a_blank_policy_variable_is_unset_and_a_typo_stops_startup():
    from app.core.config import Settings

    blank = Settings(mcp_server_url_policy="  ", mcp_server_trusted_origins="")
    assert (blank.mcp_server_url_policy, blank.mcp_server_trusted_origins) == ("permissive", [])
    with pytest.raises(ValueError, match="MCP_SERVER_URL_POLICY must be one of"):
        Settings(mcp_server_url_policy="strict")
