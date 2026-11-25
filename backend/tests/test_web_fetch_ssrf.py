"""SSRF regression tests for web_fetch and the browser tool URL policy.

Covers assessment findings SEC-2 (redirects not re-validated), SEC-3
(browser_navigate accepts any URL), SEC-9 (block list weaker than the
``is_global`` standard used by the LLM provider probe), and the DNS-rebinding
window: ``fetch_and_extract`` connects to the address it checked, with the
name as ``Host`` and as the TLS server name, never resolving the name again.
"""

import ipaddress
import socket
import ssl
import time
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.egress import EgressRoute, egress_route
from app.core.pinned_request import REFUSED_MESSAGE, send_pinned
from app.services import browser_tool, web_fetch
from app.services.web_fetch import (
    MAX_REDIRECTS,
    fetch_and_extract,
    validate_outbound_url,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _install_dns(monkeypatch, mapping: dict[str, list[str] | list[list[str]]]) -> list[str]:
    """Monkeypatch DNS resolution: hostname -> list of IPs.

    A value that is a list of lists answers each lookup of that name with the
    next list (the last one repeats): a short-TTL record that changes between
    two resolutions. Literal IP hosts resolve to themselves; unknown hostnames
    raise ``socket.gaierror`` like the real resolver. Returns the hosts looked
    up, in order.
    """
    lookups: list[str] = []
    answered: dict[str, int] = {}

    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        lookups.append(host)
        ips = mapping.get(host)
        if ips and isinstance(ips[0], list):
            turn = answered.get(host, 0)
            answered[host] = turn + 1
            ips = ips[min(turn, len(ips) - 1)]
        if ips is None:
            try:
                ipaddress.ip_address(host)
                ips = [host]
            except ValueError:
                raise socket.gaierror(f"unknown host: {host}") from None
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 0, 0, 0))
            if ":" in ip
            else (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))
            for ip in ips
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return lookups


def _response(
    status: int,
    *,
    location: str | None = None,
    text: str = "",
    content_type: str = "text/html",
    set_cookie: tuple[str, ...] = (),
) -> tuple[int, list[tuple[str, str]], bytes]:
    headers = [("content-type", content_type)]
    if location is not None:
        headers.append(("location", location))
    headers.extend(("set-cookie", cookie) for cookie in set_cookie)
    return status, headers, text.encode()


class _Web:
    """``httpx.MockTransport`` handler serving queued responses; keeps every request sent."""

    def __init__(
        self,
        responses: list[tuple[int, list[tuple[str, str]], bytes]],
        failures: dict[str, Callable[[httpx.Request], Exception]] | None = None,
    ):
        self._responses = responses
        self._failures = failures or {}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        failure = self._failures.get(request.url.host)
        if failure is not None:
            raise failure(request)
        status, headers, body = self._responses.pop(0)
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body), request=request)

    @property
    def calls(self) -> list[str]:
        """Each request's URL as named: scheme, the ``Host`` header, path."""
        return [
            f"{r.url.scheme}://{r.headers['host']}{r.url.raw_path.decode()}" for r in self.requests
        ]


def _refused(request: httpx.Request) -> Exception:
    return httpx.ConnectError("connection refused", request=request)


def _timed_out(request: httpx.Request) -> Exception:
    return httpx.ConnectTimeout("connect timed out", request=request)


def _wrong_certificate(request: httpx.Request) -> Exception:
    # What httpx raises when TLS refuses the server: a ConnectError caused by
    # the `ssl` error.
    error = httpx.ConnectError("certificate verify failed: Hostname mismatch", request=request)
    error.__cause__ = ssl.SSLCertVerificationError("Hostname mismatch")
    return error


def _install_client(
    monkeypatch,
    responses: list[tuple[int, list[tuple[str, str]], bytes]],
    *,
    failures: dict[str, Callable[[httpx.Request], Exception]] | None = None,
) -> tuple[_Web, dict]:
    """Serve httpx.AsyncClient from *responses*; return (the web, client init kwargs).

    A connection to an address in *failures* fails the way its factory says.
    """
    web = _Web(responses, failures)
    init_kwargs: dict = {}
    real = httpx.AsyncClient

    def factory(**kwargs):
        init_kwargs.update(kwargs)
        # A mount, not `transport=`: httpx ignores proxy environment variables
        # whenever a transport is given, and a client that trusted them would
        # then route around this fake (see the proxy test).
        return real(mounts={"all://": httpx.MockTransport(web.handler)}, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return web, init_kwargs


# ── validate_outbound_url (SEC-9: is_global semantics) ──────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5/",  # private
        "http://127.0.0.1:6379/",  # loopback
        "http://169.254.169.254/latest/meta-data/",  # link-local / metadata
        "http://100.64.0.1/",  # shared address space (missed by old check)
        "http://192.0.2.1/",  # reserved for documentation (non-global)
        "http://[::1]/",  # IPv6 loopback
        "http://[::ffff:10.0.0.1]/",  # IPv4-mapped IPv6 private
        "http://[64:ff9b::a9fe:a9fe]/",  # NAT64 of the metadata address (is_global on 3.13+)
        "http://[2002:a00:1::]/",  # 6to4 embedding 10.0.0.1
        "http://[fd00:ec2::254]/",  # AWS IMDS over IPv6
    ],
)
def test_validate_blocks_non_global_addresses(monkeypatch, url):
    _install_dns(monkeypatch, {})
    with pytest.raises(ValueError, match="private or non-routable"):
        validate_outbound_url(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/", "gopher://x/"])
def test_validate_blocks_non_http_schemes(url):
    with pytest.raises(ValueError, match="scheme must be http or https"):
        validate_outbound_url(url)


def test_validate_blocks_hostname_resolving_to_private(monkeypatch):
    _install_dns(monkeypatch, {"internal.example.com": ["10.1.2.3"]})
    with pytest.raises(ValueError, match="private or non-routable") as exc:
        validate_outbound_url("https://internal.example.com/secret")
    # The refusal never says where an internal name points.
    assert "10.1.2.3" not in str(exc.value)


def test_validate_blocks_when_any_resolved_ip_is_private(monkeypatch):
    # DNS rebinding style: one public IP and one private IP.
    _install_dns(monkeypatch, {"dual.example.com": ["93.184.216.34", "10.0.0.5"]})
    with pytest.raises(ValueError, match="private or non-routable"):
        validate_outbound_url("https://dual.example.com/")


def test_validate_allows_public_hostname(monkeypatch):
    _install_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    assert (
        validate_outbound_url("https://example.com/page?q=1")
        == "https://example.com/page?q=1"
    )


# ── fetch_and_extract redirect handling (SEC-2) ──────────────────────────────


async def test_fetch_initial_private_ip_blocked(monkeypatch):
    _install_dns(monkeypatch, {})
    web, _ = _install_client(monkeypatch, [])
    with pytest.raises(ValueError, match="private or non-routable"):
        await fetch_and_extract("http://10.0.0.5/")
    assert web.requests == []  # no HTTP request was issued


async def test_fetch_redirect_to_private_ip_blocked(monkeypatch):
    _install_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    responses = [
        _response(302, location="http://169.254.169.254/latest/meta-data/"),
    ]
    web, _ = _install_client(monkeypatch, responses)
    with pytest.raises(ValueError, match="private or non-routable"):
        await fetch_and_extract("https://example.com/")
    assert web.calls == ["https://example.com/"]  # the metadata IP was never requested


async def test_fetch_redirect_to_loopback_hostname_blocked(monkeypatch):
    _install_dns(
        monkeypatch,
        {"example.com": ["93.184.216.34"], "localhost": ["127.0.0.1"]},
    )
    responses = [_response(302, location="http://localhost:6379/")]
    web, _ = _install_client(monkeypatch, responses)
    with pytest.raises(ValueError, match="private or non-routable"):
        await fetch_and_extract("https://example.com/")
    assert web.calls == ["https://example.com/"]


async def test_fetch_redirect_chain_depth_capped(monkeypatch):
    _install_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    responses = [
        _response(302, location=f"https://example.com/hop{i + 1}")
        for i in range(MAX_REDIRECTS + 1)
    ]
    web, _ = _install_client(monkeypatch, responses)
    with pytest.raises(ValueError, match="Too many redirects"):
        await fetch_and_extract("https://example.com/hop0")
    assert len(web.requests) == MAX_REDIRECTS + 1


async def test_fetch_public_to_public_redirect_allowed(monkeypatch):
    _install_dns(
        monkeypatch,
        {"example.com": ["93.184.216.34"], "other.example.org": ["1.1.1.1"]},
    )
    html = "<html><head><title>Final Page</title></head><body>Hello world</body></html>"
    responses = [
        _response(302, location="https://other.example.org/page"),
        _response(200, text=html),
    ]
    web, init_kwargs = _install_client(monkeypatch, responses)

    result = await fetch_and_extract("https://example.com/")

    assert web.calls == ["https://example.com/", "https://other.example.org/page"]
    assert init_kwargs["follow_redirects"] is False  # auto-follow must stay off
    assert result.url == "https://other.example.org/page"
    assert result.status_code == 200
    assert result.title == "Final Page"
    # The hop followed is reported as the server answered it, unfollowed.
    assert [(h.status, h.url, h.location) for h in result.redirects] == [
        (302, "https://example.com/", "https://other.example.org/page")
    ]
    # The hop is pinned like the first request: its own checked address,
    # its own name as Host and TLS server name.
    hop = web.requests[1]
    assert str(hop.url) == "https://1.1.1.1/page"
    assert hop.headers["host"] == "other.example.org"
    assert hop.extensions["sni_hostname"] == "other.example.org"


async def test_fetch_relative_redirect_resolved_and_validated(monkeypatch):
    _install_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    responses = [
        _response(302, location="/next"),
        _response(200, text="plain body", content_type="text/plain"),
    ]
    web, _ = _install_client(monkeypatch, responses)

    result = await fetch_and_extract("https://example.com/start")

    # Relative to the NAME, never to the address the hop was pinned to.
    assert web.calls == ["https://example.com/start", "https://example.com/next"]
    assert result.text == "plain body"
    assert result.url == "https://example.com/next"
    assert [(h.status, h.location) for h in result.redirects] == [(302, "https://example.com/next")]


# ── fetch_and_extract connects to the checked address (DNS rebinding) ────────


async def test_fetch_sends_to_resolved_address_with_name_as_host_and_sni(monkeypatch):
    _install_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    web, init_kwargs = _install_client(
        monkeypatch, [_response(200, text="body", content_type="text/plain")]
    )

    result = await fetch_and_extract("https://example.com/page?q=1")

    assert len(web.requests) == 1
    request = web.requests[0]
    assert str(request.url) == "https://93.184.216.34/page?q=1"
    assert request.headers["host"] == "example.com"
    assert request.extensions["sni_hostname"] == "example.com"
    assert init_kwargs["trust_env"] is False
    assert init_kwargs["follow_redirects"] is False
    # HTTP/1.1 only, no keep-alive: an HTTP/2 connection is multiplexed and
    # `Connection: close` means nothing to it.
    assert init_kwargs["http2"] is False
    assert init_kwargs["limits"].max_keepalive_connections == 0
    # The pool keys connections by address: none is kept, so a connection
    # verified for this name never carries another name on the same address.
    assert request.headers["connection"] == "close"
    # The caller sees the name, never the address.
    assert result.url == "https://example.com/page?q=1"
    assert result.body == b"body"


async def test_fetch_keeps_a_non_default_port_in_the_host_header(monkeypatch):
    _install_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    web, _ = _install_client(monkeypatch, [_response(200, text="ok", content_type="text/plain")])

    await fetch_and_extract("https://example.com:8443/x")

    request = web.requests[0]
    assert str(request.url) == "https://93.184.216.34:8443/x"
    assert request.headers["host"] == "example.com:8443"
    assert request.extensions["sni_hostname"] == "example.com"


async def test_fetch_over_plain_http_sets_no_tls_server_name(monkeypatch):
    _install_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    web, _ = _install_client(monkeypatch, [_response(200, text="ok", content_type="text/plain")])

    await fetch_and_extract("http://example.com/")

    request = web.requests[0]
    assert str(request.url) == "http://93.184.216.34/"
    assert request.headers["host"] == "example.com"
    assert "sni_hostname" not in request.extensions


async def test_fetch_resolves_each_name_once_so_a_rebinding_answer_is_never_used(monkeypatch):
    # First answer public, every later one the metadata endpoint: a client
    # that resolved the name again to connect would reach 169.254.169.254.
    lookups = _install_dns(
        monkeypatch, {"rebind.example.com": [["93.184.216.34"], ["169.254.169.254"]]}
    )
    web, _ = _install_client(monkeypatch, [_response(200, text="ok", content_type="text/plain")])

    await fetch_and_extract("https://rebind.example.com/")

    assert lookups == ["rebind.example.com"]
    assert [str(r.url) for r in web.requests] == ["https://93.184.216.34/"]


async def test_fetch_redirect_to_a_name_resolving_privately_is_never_requested(monkeypatch):
    lookups = _install_dns(
        monkeypatch,
        {"example.com": ["93.184.216.34"], "internal.example.com": ["10.1.2.3"]},
    )
    web, _ = _install_client(
        monkeypatch, [_response(302, location="https://internal.example.com/admin")]
    )

    with pytest.raises(ValueError, match="private or non-routable") as exc:
        await fetch_and_extract("https://example.com/")

    assert web.calls == ["https://example.com/"]
    assert lookups == ["example.com", "internal.example.com"]
    assert "10.1.2.3" not in str(exc.value)


async def test_fetch_refuses_a_name_with_one_private_address_among_public_ones(monkeypatch):
    _install_dns(monkeypatch, {"dual.example.com": ["93.184.216.34", "10.0.0.5"]})
    web, _ = _install_client(monkeypatch, [])

    with pytest.raises(ValueError, match="private or non-routable"):
        await fetch_and_extract("https://dual.example.com/")

    assert web.requests == []


async def test_fetch_prefers_ipv4_on_a_dual_stack_name(monkeypatch):
    _install_dns(
        monkeypatch, {"example.com": ["2606:2800:220:1:248:1893:25c8:1946", "93.184.216.34"]}
    )
    web, _ = _install_client(monkeypatch, [_response(200, text="ok", content_type="text/plain")])

    await fetch_and_extract("https://example.com/")

    assert str(web.requests[0].url) == "https://93.184.216.34/"


async def test_fetch_sends_an_internationalised_name_in_its_ascii_form(monkeypatch):
    _install_dns(monkeypatch, {"xn--bcher-kva.example": ["93.184.216.34"]})
    web, _ = _install_client(monkeypatch, [_response(200, text="ok", content_type="text/plain")])

    await fetch_and_extract("https://bücher.example/")

    request = web.requests[0]
    assert str(request.url) == "https://93.184.216.34/"
    assert request.headers["host"] == "xn--bcher-kva.example"
    assert request.extensions["sni_hostname"] == "xn--bcher-kva.example"


async def test_fetch_of_a_public_ip_literal_is_sent_as_is(monkeypatch):
    lookups = _install_dns(monkeypatch, {})
    web, _ = _install_client(monkeypatch, [_response(200, text="ok", content_type="text/plain")])

    await fetch_and_extract("https://93.184.216.34/x")

    request = web.requests[0]
    assert str(request.url) == "https://93.184.216.34/x"
    assert request.headers["host"] == "93.184.216.34"
    assert "sni_hostname" not in request.extensions
    assert lookups == []


async def test_fetch_error_status_names_the_host_not_the_address(monkeypatch):
    _install_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    _install_client(monkeypatch, [_response(404, text="gone")])

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await fetch_and_extract("https://example.com/missing")

    assert str(exc.value.request.url) == "https://example.com/missing"
    # The body of an error answer is read, as before the fetch streamed.
    assert exc.value.response.text == "gone"
    assert "example.com" in str(exc.value)
    assert "93.184.216.34" not in str(exc.value)


async def test_fetch_unresolvable_name_is_refused_like_a_private_one(monkeypatch):
    # The same text either way: telling "no such name" from "an internal
    # name" would map the internal DNS for whoever can make the platform fetch.
    _install_dns(monkeypatch, {"internal.example.com": ["10.1.2.3"]})
    web, _ = _install_client(monkeypatch, [])

    with pytest.raises(ValueError) as unresolvable:
        await fetch_and_extract("https://nowhere.example/")
    with pytest.raises(ValueError) as private:
        await fetch_and_extract("https://internal.example.com/")

    assert str(unresolvable.value) == REFUSED_MESSAGE
    assert str(private.value) == REFUSED_MESSAGE
    assert web.requests == []


async def test_fetch_bounds_resolution_by_the_connect_timeout(monkeypatch):
    def slow_getaddrinfo(*_args, **_kwargs):
        time.sleep(2)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", slow_getaddrinfo)
    web, _ = _install_client(monkeypatch, [])

    started = time.monotonic()
    with pytest.raises(ValueError, match="private or non-routable"):
        await fetch_and_extract("https://slow.example/", timeout=0.05)

    # Well under the resolver's 2 s, with room for a loaded runner.
    assert time.monotonic() - started < 1
    assert web.requests == []


async def test_fetch_tries_the_next_checked_address_when_one_is_down(monkeypatch):
    _install_dns(monkeypatch, {"example.com": ["93.184.216.34", "93.184.216.35"]})
    web, _ = _install_client(
        monkeypatch,
        [_response(200, text="ok", content_type="text/plain")],
        failures={"93.184.216.34": _refused},
    )

    result = await fetch_and_extract("https://example.com/")

    assert [str(r.url) for r in web.requests] == ["https://93.184.216.34/", "https://93.184.216.35/"]
    assert [r.headers["host"] for r in web.requests] == ["example.com", "example.com"]
    assert result.text == "ok"


@pytest.mark.parametrize(
    ("failure", "raised"),
    [(_wrong_certificate, httpx.ConnectError), (_timed_out, httpx.ConnectTimeout)],
    ids=["tls-failure", "connect-timeout"],
)
async def test_fetch_does_not_try_another_address_after_a_tls_failure_or_a_timeout(
    monkeypatch, failure, raised
):
    # A certificate that does not name the host fails on every address, and
    # each black-holed address would cost another connect timeout.
    _install_dns(monkeypatch, {"example.com": ["93.184.216.34", "93.184.216.35"]})
    web, _ = _install_client(
        monkeypatch,
        [_response(200, text="ok", content_type="text/plain")],
        failures={"93.184.216.34": failure},
    )

    with pytest.raises(raised):
        await fetch_and_extract("https://example.com/")

    assert [str(r.url) for r in web.requests] == ["https://93.184.216.34/"]


async def test_fetch_keeps_cookies_by_name_not_by_the_shared_address(monkeypatch):
    # Both names sit on one address (a CDN). A redirect within example.com
    # carries its cookies, the domain-scoped one included; the hop to
    # other.example.org carries none of them.
    _install_dns(
        monkeypatch,
        {"example.com": ["93.184.216.34"], "other.example.org": ["93.184.216.34"]},
    )
    web, _ = _install_client(
        monkeypatch,
        [
            _response(
                302,
                location="/home",
                set_cookie=("sid=1; Path=/", "pref=2; Domain=example.com; Path=/"),
            ),
            _response(302, location="https://other.example.org/"),
            _response(200, text="ok", content_type="text/plain"),
        ],
    )

    await fetch_and_extract("https://example.com/login")

    assert web.calls == [
        "https://example.com/login",
        "https://example.com/home",
        "https://other.example.org/",
    ]
    assert "cookie" not in web.requests[0].headers
    assert sorted(web.requests[1].headers["cookie"].split("; ")) == ["pref=2", "sid=1"]
    assert "cookie" not in web.requests[2].headers


async def test_fetch_reaches_a_public_ipv4_site_through_nat64(monkeypatch):
    # An IPv6-only network with DNS64 answers an IPv4-only site this way.
    _install_dns(monkeypatch, {"v4only.example": ["64:ff9b::5db8:d822"]})
    web, _ = _install_client(monkeypatch, [_response(200, text="ok", content_type="text/plain")])

    await fetch_and_extract("https://v4only.example/")

    assert str(web.requests[0].url) == "https://[64:ff9b::5db8:d822]/"


async def test_fetch_ignores_proxy_environment_variables(monkeypatch):
    # With the environment trusted, httpx would mount a proxy transport for
    # these and hand the NAME to the proxy to resolve - the check pinned for
    # nothing. The request must reach the client's own transport, pinned.
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://proxy.invalid:3128")
    _install_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    web, _ = _install_client(monkeypatch, [_response(200, text="ok", content_type="text/plain")])

    await fetch_and_extract("https://example.com/")

    assert [str(r.url) for r in web.requests] == ["https://93.184.216.34/"]


@pytest.mark.parametrize(
    "client_kwargs",
    [{}, {"limits": httpx.Limits(max_keepalive_connections=0)}],
    ids=["default-client", "lookalike-client"],
)
async def test_send_pinned_refuses_a_client_it_did_not_build(monkeypatch, client_kwargs):
    # A client whose pool could keep a connection (keep-alive, HTTP/2) would
    # carry the next name on the same address without a certificate check, so
    # only `pinned_client()` is accepted - not one that merely looks like it.
    _install_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    web = _Web([])
    async with httpx.AsyncClient(transport=httpx.MockTransport(web.handler), **client_kwargs) as client:
        with pytest.raises(TypeError, match="pinned_client"):
            await send_pinned(client, "GET", "https://example.com/")
    assert web.requests == []


async def test_fetch_checks_the_address_even_under_a_relay_route(monkeypatch):
    # `validate_outbound_url` defers to a relay route; `fetch_and_extract`
    # never goes through a relay, so it must never skip the check.
    _install_dns(monkeypatch, {"internal.example.com": ["10.1.2.3"]})
    web, _ = _install_client(monkeypatch, [])

    with (
        egress_route(EgressRoute(group_id="relay-group")),
        pytest.raises(ValueError, match="private or non-routable"),
    ):
        await fetch_and_extract("https://internal.example.com/")

    assert web.requests == []


# ── browser_navigate URL policy (SEC-3) ──────────────────────────────────────


async def test_browser_navigate_rejects_file_scheme(monkeypatch):
    get_session = AsyncMock()
    monkeypatch.setattr(browser_tool, "_get_or_create_session", get_session)

    result = await browser_tool.browser_navigate("k", "file:///etc/passwd")

    assert "Navigation blocked by URL policy" in result
    get_session.assert_not_awaited()  # blocked before any session is created


async def test_browser_navigate_rejects_private_host(monkeypatch):
    _install_dns(monkeypatch, {})
    get_session = AsyncMock()
    monkeypatch.setattr(browser_tool, "_get_or_create_session", get_session)

    result = await browser_tool.browser_navigate("k", "http://127.0.0.1:6379/")

    assert "Navigation blocked by URL policy" in result
    get_session.assert_not_awaited()


async def test_browser_navigate_rejects_metadata_endpoint(monkeypatch):
    _install_dns(monkeypatch, {})
    get_session = AsyncMock()
    monkeypatch.setattr(browser_tool, "_get_or_create_session", get_session)

    result = await browser_tool.browser_navigate(
        "k", "http://169.254.169.254/latest/meta-data/"
    )

    assert "Navigation blocked by URL policy" in result
    get_session.assert_not_awaited()


async def test_browser_navigate_allows_public_url(monkeypatch):
    _install_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    page = SimpleNamespace(
        url="https://example.com/",
        goto=AsyncMock(),
        title=AsyncMock(return_value="Example"),
    )
    # A plain session: its cookies are kept after the navigation, in a store of this test's own.
    context = SimpleNamespace(storage_state=AsyncMock(return_value={"cookies": [], "origins": []}))
    session = browser_tool.BrowserSession(pw=None, browser=None, context=context, page=page, last_used=0.0)
    monkeypatch.setattr(browser_tool, "_kept_states", {})
    monkeypatch.setattr(browser_tool, "_known_stamps", {})
    monkeypatch.setattr(
        browser_tool, "_get_or_create_session", AsyncMock(return_value=session)
    )

    result = await browser_tool.browser_navigate("k", "https://example.com/")

    page.goto.assert_awaited_once_with(
        "https://example.com/", wait_until="domcontentloaded"
    )
    assert "Navigated to: https://example.com/" in result
    assert "Title: Example" in result


# ── Playwright route filter (defense in depth) ───────────────────────────────


@pytest.mark.parametrize(
    "url,blocked",
    [
        ("http://169.254.169.254/latest/meta-data/", True),
        ("http://127.0.0.1:8080/", True),
        ("http://10.0.0.5/", True),
        ("http://100.64.0.1/", True),
        ("file:///etc/passwd", True),
        ("https://example.com/asset.js", False),  # hostnames pass (no DNS here)
        ("https://1.1.1.1/img.png", False),  # literal global IP allowed
    ],
)
def test_route_filter_url_policy(url, blocked):
    assert browser_tool._is_blocked_request_url(url) is blocked


async def test_route_filter_aborts_blocked_request():
    route = SimpleNamespace(
        request=SimpleNamespace(url="http://169.254.169.254/"),
        abort=AsyncMock(),
        continue_=AsyncMock(),
    )
    await browser_tool._route_filter(route)
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()


async def test_route_filter_continues_allowed_request():
    route = SimpleNamespace(
        request=SimpleNamespace(url="https://example.com/style.css"),
        abort=AsyncMock(),
        continue_=AsyncMock(),
    )
    await browser_tool._route_filter(route)
    route.continue_.assert_awaited_once()
    route.abort.assert_not_awaited()


# ── allow_private policy (OpenAPI imports and calls) ─────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5/",
        "http://172.16.1.1/",
        "http://192.168.1.2/",
        "http://100.64.0.1/",
        "http://[fd12:3456::1]/",
        "https://example.com/",
    ],
)
def test_private_policy_admits_private_networks(monkeypatch, url):
    _install_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    assert validate_outbound_url(url, allow_private=True)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:6379/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[fd00:ec2::254]/",
        "http://100.100.100.200/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",
        "http://[::ffff:127.0.0.1]/",
    ],
)
def test_private_policy_still_refuses_loopback_and_metadata(monkeypatch, url):
    _install_dns(monkeypatch, {})
    with pytest.raises(ValueError, match="loopback, link-local, reserved or cloud-metadata"):
        validate_outbound_url(url, allow_private=True)


def test_private_policy_refuses_a_name_resolving_to_loopback(monkeypatch):
    _install_dns(monkeypatch, {"internal.test": ["10.0.0.9", "127.0.0.1"]})
    with pytest.raises(ValueError):
        validate_outbound_url("https://internal.test/", allow_private=True)


# ── fetch_url_bytes ──────────────────────────────────────────────────────────


def _install_transport(monkeypatch, handler) -> list[str]:
    seen: list[str] = []
    real = httpx.AsyncClient

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return handler(request)

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(recording)
        return real(*args, **kwargs)

    monkeypatch.setattr(web_fetch.httpx, "AsyncClient", factory)
    return seen


async def test_fetch_url_bytes_returns_the_raw_body(monkeypatch):
    from app.services.web_fetch import fetch_url_bytes

    _install_dns(monkeypatch, {"specs.internal": ["10.2.3.4"]})
    seen = _install_transport(
        monkeypatch,
        lambda r: httpx.Response(200, content=b"openapi: 3.0.0\n", headers={"content-type": "application/yaml"}),
    )
    fetched = await fetch_url_bytes(
        "https://specs.internal/openapi.yaml", max_bytes=1024, allow_private=True, accept="application/yaml"
    )
    assert fetched.content == b"openapi: 3.0.0\n"
    assert fetched.content_type == "application/yaml"
    assert seen == ["https://specs.internal/openapi.yaml"]


async def test_fetch_url_bytes_enforces_the_ceiling_while_streaming(monkeypatch):
    from app.services.web_fetch import fetch_url_bytes

    _install_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    _install_transport(monkeypatch, lambda r: httpx.Response(200, content=b"x" * 2048))
    with pytest.raises(ValueError, match="maximum size"):
        await fetch_url_bytes("https://example.com/big.json", max_bytes=1024)


async def test_fetch_url_bytes_revalidates_redirects(monkeypatch):
    from app.services.web_fetch import fetch_url_bytes

    _install_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    seen = _install_transport(
        monkeypatch,
        lambda r: httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"}),
    )
    with pytest.raises(ValueError, match="cloud-metadata"):
        await fetch_url_bytes("https://example.com/spec.json", max_bytes=1024, allow_private=True)
    assert seen == ["https://example.com/spec.json"]


async def test_fetch_url_bytes_raises_on_http_errors(monkeypatch):
    from app.services.web_fetch import fetch_url_bytes

    _install_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    _install_transport(monkeypatch, lambda r: httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        await fetch_url_bytes("https://example.com/missing.json", max_bytes=1024)
