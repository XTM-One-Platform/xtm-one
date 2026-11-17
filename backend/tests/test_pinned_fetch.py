"""A URL someone typed, fetched at the address the policy checked.

A built pack's free HTTP source names a host its author chose. What is pinned
here: the host is resolved once and every address must be public, the request
goes to that address with the name as `Host` and TLS server name (no second
resolution a rebinding answer could turn inward), every redirect is checked
the same way, no connection outlives its request (a redirect to another name
on the same address is a new connection, verified for that name), the
environment's proxies are ignored, the body is read raw and capped, only text,
JSON or XML is taken, and a refusal's precise reason stays in the exception
(for the logs), never in what a caller shows.
"""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest

from app.core import pinned_request
from app.core.pinned_request import send_pinned
from app.services.investigation_packs import _fetched_host
from app.services.pinned_fetch import MAX_REDIRECTS, PinnedFetchRefusedError, accepted_type, fetch_pinned


def _resolver(monkeypatch, table: dict[str, list[str]]) -> list[str]:
    """Serve `socket.getaddrinfo` from *table*; return the names asked for."""
    calls: list[str] = []

    def _getaddrinfo(host, port, *args, **kwargs):
        calls.append(host)
        if host not in table:
            raise socket.gaierror("unknown")
        return [
            (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
            for ip in table[host]
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
    return calls


def _answer(status: int, body: bytes = b"", headers: dict | None = None) -> tuple:
    """A response as a server streams it: unread, raw (a pre-read double
    would fail the raw, capped read the fetch does)."""
    return status, body, headers or {}


def _install_client(monkeypatch, handler) -> dict:
    """Serve every httpx.AsyncClient from *handler*; return the client's init
    kwargs. A mount, not `transport=`: httpx ignores proxy environment
    variables whenever a transport is given, which would hide a client that
    trusted them."""
    init_kwargs: dict = {}
    real = httpx.AsyncClient

    def factory(**kwargs):
        init_kwargs.update(kwargs)
        return real(mounts={"all://": httpx.MockTransport(handler)}, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return init_kwargs


def _serve(monkeypatch, routes: dict[str, tuple], seen: list[httpx.Request]) -> dict:
    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        key = f"{request.url.host}{request.url.raw_path.decode()}"
        status, body, headers = routes.get(key, _answer(404))
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))

    return _install_client(monkeypatch, _handler)


@pytest.mark.asyncio
async def test_the_request_goes_to_the_checked_address_with_the_name_as_host(monkeypatch):
    calls = _resolver(monkeypatch, {"rdap.example": ["93.184.216.34"]})
    seen: list[httpx.Request] = []
    routes = {"93.184.216.34/domain/x.com": _answer(200, b'{"ok":true}', {"content-type": "application/json"})}
    _serve(monkeypatch, routes, seen)
    result = await fetch_pinned("https://rdap.example/domain/x.com")
    (request,) = seen
    assert request.url.host == "93.184.216.34", "connected to the address that was checked"
    assert request.headers["host"] == "rdap.example"
    assert request.extensions["sni_hostname"] == "rdap.example", "the certificate is checked against the name"
    assert request.headers["accept-encoding"] == "identity"
    assert calls == ["rdap.example"], "resolved once"
    assert (result.status_code, result.text, result.url) == (200, '{"ok":true}', "https://rdap.example/domain/x.com")


@pytest.mark.parametrize(
    ("url", "table"),
    [
        ("http://127.0.0.1/", {}),
        ("http://[::1]/", {}),
        ("http://169.254.169.254/latest/meta-data/", {}),
        ("http://10.0.0.1/", {}),
        ("http://internal.example/", {"internal.example": ["10.1.2.3"]}),
        # One inward address among public ones is enough to refuse.
        ("http://mixed.example/", {"mixed.example": ["93.184.216.34", "192.168.1.10"]}),
        ("ftp://files.example/", {"files.example": ["93.184.216.34"]}),
        ("http://user:secret@rdap.example/", {"rdap.example": ["93.184.216.34"]}),
    ],
)
@pytest.mark.asyncio
async def test_an_inward_or_malformed_target_is_refused_before_any_request(monkeypatch, url, table):
    _resolver(monkeypatch, table)
    seen: list[httpx.Request] = []
    _serve(monkeypatch, {}, seen)
    with pytest.raises(PinnedFetchRefusedError) as refused:
        await fetch_pinned(url)
    assert seen == []
    assert refused.value.kind == "refused"


@pytest.mark.asyncio
async def test_every_redirect_is_checked_and_pinned_too(monkeypatch):
    calls = _resolver(monkeypatch, {"a.example": ["93.184.216.34"], "b.example": ["10.0.0.5"]})
    seen: list[httpx.Request] = []
    routes = {"93.184.216.34/": _answer(302, headers={"location": "http://b.example/metadata"})}
    _serve(monkeypatch, routes, seen)
    with pytest.raises(PinnedFetchRefusedError) as refused:
        await fetch_pinned("http://a.example/")
    assert refused.value.kind == "refused"
    assert calls == ["a.example", "b.example"]
    assert [r.url.host for r in seen] == ["93.184.216.34"], "the inward hop is never requested"


@pytest.mark.parametrize(
    ("location", "kinds"),
    [
        ("http://user:secret@b.example/", {"refused"}),
        ("ftp://b.example/", {"refused"}),
        # Which kind is httpx's choice, not ours: httpx 0.28.1 parses the
        # Location while sending (`next_request`, built even without
        # follow_redirects) and fails the response as a protocol error -
        # unreachable; our own join would refuse it. What is ours: the hop is
        # never requested.
        ("http://b.example:abc/", {"refused", "unreachable"}),
    ],
)
@pytest.mark.asyncio
async def test_a_redirect_to_credentials_or_a_malformed_url_is_refused(monkeypatch, location, kinds):
    calls = _resolver(monkeypatch, {"a.example": ["93.184.216.34"], "b.example": ["93.184.216.34"]})
    seen: list[httpx.Request] = []
    _serve(monkeypatch, {"93.184.216.34/": _answer(302, headers={"location": location})}, seen)
    with pytest.raises(PinnedFetchRefusedError) as refused:
        await fetch_pinned("http://a.example/")
    assert refused.value.kind in kinds
    assert calls == ["a.example"]
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_a_name_that_does_not_resolve_is_unreachable(monkeypatch):
    calls = _resolver(monkeypatch, {})
    seen: list[httpx.Request] = []
    _serve(monkeypatch, {}, seen)
    with pytest.raises(PinnedFetchRefusedError) as refused:
        await fetch_pinned("http://nowhere.example/")
    assert (refused.value.kind, calls, seen) == ("unreachable", ["nowhere.example"], [])


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (httpx.ConnectError("connection refused"), "unreachable"),
        (httpx.RemoteProtocolError("server hung up"), "unreachable"),
        (httpx.ConnectTimeout("connect timed out"), "timeout"),
        (httpx.ReadTimeout("read timed out"), "timeout"),
    ],
    ids=["connect-error", "protocol-error", "connect-timeout", "read-timeout"],
)
@pytest.mark.asyncio
async def test_a_transport_failure_keeps_its_kind(monkeypatch, error, kind):
    _resolver(monkeypatch, {"down.example": ["93.184.216.34"]})
    attempts: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise error

    _install_client(monkeypatch, _handler)
    with pytest.raises(PinnedFetchRefusedError) as refused:
        await fetch_pinned("http://down.example/")
    assert refused.value.kind == kind
    assert len(attempts) == 1, "one checked address: nothing else to try"


@pytest.mark.asyncio
async def test_redirects_are_bounded(monkeypatch):
    _resolver(monkeypatch, {"loop.example": ["93.184.216.34"]})
    routes = {"93.184.216.34/": _answer(302, headers={"location": "/"})}
    seen: list[httpx.Request] = []
    _serve(monkeypatch, routes, seen)
    with pytest.raises(PinnedFetchRefusedError) as refused:
        await fetch_pinned("http://loop.example/")
    assert refused.value.kind == "too_many_redirects"
    assert len(seen) == MAX_REDIRECTS + 1


@pytest.mark.asyncio
async def test_the_body_is_read_raw_and_capped(monkeypatch):
    _resolver(monkeypatch, {"big.example": ["93.184.216.34"]})
    routes = {"93.184.216.34/": _answer(200, b"x" * 5000, {"content-type": "text/plain"})}
    _serve(monkeypatch, routes, [])
    result = await fetch_pinned("http://big.example/", max_bytes=1000)
    assert (len(result.text), result.capped) == (1000, True)


@pytest.mark.parametrize(
    ("headers", "kind"),
    [
        ({"content-type": "application/octet-stream"}, "type"),
        ({"content-type": "image/png"}, "type"),
        ({"content-type": "text/plain", "content-encoding": "gzip"}, "type"),
    ],
)
@pytest.mark.asyncio
async def test_only_text_json_or_xml_is_taken_uncompressed(monkeypatch, headers, kind):
    _resolver(monkeypatch, {"files.example": ["93.184.216.34"]})
    routes = {"93.184.216.34/": _answer(200, b"\x89PNG", headers)}
    _serve(monkeypatch, routes, [])
    with pytest.raises(PinnedFetchRefusedError) as refused:
        await fetch_pinned("http://files.example/")
    assert refused.value.kind == kind


@pytest.mark.asyncio
async def test_an_error_status_is_a_refusal_with_its_status(monkeypatch):
    _resolver(monkeypatch, {"gone.example": ["93.184.216.34"]})
    _serve(monkeypatch, {}, [])
    with pytest.raises(PinnedFetchRefusedError) as refused:
        await fetch_pinned("http://gone.example/missing")
    assert (refused.value.kind, refused.value.status) == ("status", 404)


def test_the_media_types_taken():
    assert accepted_type("text/html; charset=utf-8") and accepted_type("application/rdap+json")
    assert accepted_type("application/json") and accepted_type("application/atom+xml")
    assert not accepted_type("application/pdf") and not accepted_type("")


@pytest.mark.asyncio
async def test_the_client_is_the_pinned_one_and_ignores_the_environment_s_proxies(monkeypatch):
    """`trust_env=False`: an HTTPS_PROXY in the environment is never used. And
    the client is `pinned_client`'s: HTTP/1.1 only, no connection kept."""
    _resolver(monkeypatch, {"rdap.example": ["93.184.216.34"]})
    routes = {"93.184.216.34/": _answer(200, b"ok", {"content-type": "text/plain"})}
    init_kwargs = _serve(monkeypatch, routes, [])
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    await fetch_pinned("http://rdap.example/")
    assert (init_kwargs["trust_env"], init_kwargs["follow_redirects"]) == (False, False)
    assert (init_kwargs["http2"], init_kwargs["limits"].max_keepalive_connections) == (False, 0)


@pytest.mark.asyncio
async def test_a_redirect_to_another_name_on_the_same_address_is_a_new_verified_connection(monkeypatch):
    """The pool keys connections by address, so a kept connection,
    verified for a.example, would carry b.example's request unchecked. Every
    request says `Connection: close` and names its own host to TLS."""
    calls = _resolver(monkeypatch, {"a.example": ["93.184.216.34"], "b.example": ["93.184.216.34"]})
    seen: list[httpx.Request] = []
    routes = {
        "93.184.216.34/": _answer(302, headers={"location": "https://b.example/data"}),
        "93.184.216.34/data": _answer(200, b"ok", {"content-type": "text/plain"}),
    }
    _serve(monkeypatch, routes, seen)
    result = await fetch_pinned("https://a.example/")
    assert calls == ["a.example", "b.example"]
    assert [r.url.host for r in seen] == ["93.184.216.34", "93.184.216.34"]
    assert [r.headers["connection"] for r in seen] == ["close", "close"]
    assert [r.headers["host"] for r in seen] == ["a.example", "b.example"]
    assert [r.extensions["sni_hostname"] for r in seen] == ["a.example", "b.example"]
    assert (result.url, result.text) == ("https://b.example/data", "ok")


@pytest.mark.asyncio
async def test_send_pinned_refuses_a_client_pinned_client_did_not_build():
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as foreign:
        with pytest.raises(TypeError, match="pinned_client"):
            await send_pinned(foreign, "GET", "https://a.example/")


@pytest.mark.asyncio
async def test_two_names_on_one_address_never_share_a_connection(monkeypatch):
    """The same, on a real socket: a server that would keep every connection
    open is asked to close each one, and sees the redirect's second name
    arrive on a connection of its own. (The old client sent `keep-alive` and
    left reuse to whether httpx happened to read the redirect's body.)"""
    connections: list[list[tuple[str, str, str]]] = []

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        requests: list[tuple[str, str, str]] = []
        connections.append(requests)
        try:
            while head := await reader.readuntil(b"\r\n\r\n"):
                lines = head.decode().split("\r\n")
                path = lines[0].split(" ")[1]
                fields = {
                    k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:] if line)
                }
                requests.append((fields["host"], path, fields.get("connection", "")))
                if path == "/":
                    location = f"http://b.example:{port}/data"
                    writer.write(f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n\r\n".encode())
                else:
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    _resolver(monkeypatch, {"a.example": ["127.0.0.1"], "b.example": ["127.0.0.1"]})
    # Loopback stands in for the shared public address the policy would pass.
    monkeypatch.setattr(pinned_request, "forbidden_address_reason", lambda _address: None)
    async with server:
        result = await fetch_pinned(f"http://a.example:{port}/", timeout=5)
    assert result.text == "ok"
    assert connections == [[(f"a.example:{port}", "/", "close")], [(f"b.example:{port}", "/data", "close")]]


@pytest.mark.asyncio
async def test_ipv4_is_preferred_when_both_are_public(monkeypatch):
    _resolver(monkeypatch, {"dual.example": ["2606:2800:220:1:248:1893:25c8:1946", "93.184.216.34"]})
    seen: list[httpx.Request] = []
    routes = {"93.184.216.34/": _answer(200, b"ok", {"content-type": "text/plain"})}
    _serve(monkeypatch, routes, seen)
    await fetch_pinned("http://dual.example/")
    assert [r.url.host for r in seen] == ["93.184.216.34"]


@pytest.mark.asyncio
async def test_an_ipv6_only_host_is_reached_at_its_bracketed_address(monkeypatch):
    _resolver(monkeypatch, {"v6.example": ["2606:2800:220:1:248:1893:25c8:1946"]})
    seen: list[httpx.Request] = []
    routes = {"2606:2800:220:1:248:1893:25c8:1946/": _answer(200, b"ok", {"content-type": "text/plain"})}
    _serve(monkeypatch, routes, seen)
    await fetch_pinned("http://v6.example/")
    assert seen[0].url.host == "2606:2800:220:1:248:1893:25c8:1946" and seen[0].headers["host"] == "v6.example"


class _Drip(httpx.AsyncByteStream):
    """A server sending a byte at a time, each well inside a per-read timeout."""

    async def __aiter__(self):
        while True:
            await asyncio.sleep(0.05)
            yield b"x"


@pytest.mark.asyncio
async def test_the_timeout_bounds_the_whole_fetch_not_each_read(monkeypatch):
    _resolver(monkeypatch, {"slow.example": ["93.184.216.34"]})
    _install_client(
        monkeypatch, lambda _request: httpx.Response(200, headers={"content-type": "text/plain"}, stream=_Drip())
    )
    with pytest.raises(PinnedFetchRefusedError) as refused:
        await fetch_pinned("http://slow.example/", timeout=0.3)
    assert refused.value.kind == "timeout"


@pytest.mark.asyncio
async def test_an_ipv6_literal_with_a_port_keeps_its_brackets_in_the_host_header(monkeypatch):
    seen: list[httpx.Request] = []
    routes = {"2606:4700::1111/x": _answer(200, b"ok", {"content-type": "text/plain"})}
    _serve(monkeypatch, routes, seen)
    await fetch_pinned("http://[2606:4700::1111]:8443/x")
    assert seen[0].headers["host"] == "[2606:4700::1111]:8443"


@pytest.mark.parametrize(
    ("url", "resolved"),
    [
        ("https://Collector.Example/x", ["collector.example"]),
        ("https://Bücher.example/x", ["xn--bcher-kva.example"]),
        ("http://collector.example:8443/q", ["collector.example"]),
        ("https://collector.example:443/", ["collector.example"]),
        ("http://[2606:4700::1111]:8443/x", []),
        ("https://collector.example#@trusted.example/v", ["collector.example"]),
        ("https://collector.example/?next=https://trusted.example/", ["collector.example"]),
    ],
)
@pytest.mark.asyncio
async def test_the_pack_picker_names_the_host_the_fetch_sends(monkeypatch, url, resolved):
    """`investigation_packs._fetched_host` reads the fetch's own parse: the
    host a pack's runner is shown is the `Host` the fetch sends."""
    calls = _resolver(monkeypatch, {"collector.example": ["93.184.216.34"], "xn--bcher-kva.example": ["93.184.216.34"]})
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/plain"}, stream=httpx.ByteStream(b"ok"))

    _install_client(monkeypatch, _handler)
    await fetch_pinned(url)
    assert calls == resolved
    assert [r.headers["host"] for r in seen] == [_fetched_host(url)]
