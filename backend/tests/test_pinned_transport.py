"""``core/pinned_transport.py``: the address a request connects to is the one checked.

Driven against real sockets on loopback: a stand-in server counts the
connections it accepts, so "refused" means no connection was opened, not
merely that an error came back.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpcore
import httpx
import pytest

from app.core import pinned_transport
from app.core.pinned_transport import PinnedTransport

REFUSAL = "refused by the test policy"


@dataclass
class _Server:
    port: int
    accepted: list[str] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)


@asynccontextmanager
async def _server(*, status: int = 200, location: str | None = None, host: str = "127.0.0.1") -> AsyncIterator[_Server]:
    """An HTTP/1.1 server on *host* answering every request with *status*."""
    record = _Server(port=0)

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        record.accepted.append(writer.get_extra_info("peername")[0])
        head = await reader.readuntil(b"\r\n\r\n")
        record.requests.append(head.split(b"\r\n", 1)[0].decode())
        extra = f"Location: {location}\r\n" if location else ""
        writer.write(f"HTTP/1.1 {status} X\r\n{extra}Content-Length: 2\r\nConnection: close\r\n\r\nok".encode())
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(_handle, host, 0)
    record.port = server.sockets[0].getsockname()[1]
    try:
        yield record
    finally:
        server.close()
        await server.wait_closed()


def _answers(*addresses: str):
    """A resolver answering *addresses* for every name, counting its lookups."""
    calls: list[tuple[str, int]] = []

    async def _resolve(host: str, port: int) -> list:
        calls.append((host, port))
        family = lambda a: socket.AF_INET6 if ":" in a else socket.AF_INET  # noqa: E731
        return [(family(a), socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses]

    _resolve.calls = calls  # type: ignore[attr-defined]
    return _resolve


def _refuse(*refused: str):
    """A policy refusing exactly *refused*, recording every address it is asked about."""
    asked: list[str] = []

    def _check(url: httpx.URL, ip) -> str | None:
        asked.append(str(ip))
        return "refused class" if str(ip) in refused else None

    _check.asked = asked  # type: ignore[attr-defined]
    return _check


async def test_a_name_connects_to_the_address_it_was_checked_at_with_one_lookup():
    async with _server() as server:
        resolve = _answers("127.0.0.1")
        check = _refuse()
        async with httpx.AsyncClient(
            transport=PinnedTransport(check, refusal=REFUSAL, resolve=resolve), trust_env=False
        ) as client:
            response = await client.get(f"http://mcp.example.test:{server.port}/mcp")
    assert response.status_code == 200
    assert resolve.calls == [("mcp.example.test", server.port)]
    assert check.asked == ["127.0.0.1"]
    assert server.accepted == ["127.0.0.1"]
    # The Host header still names the URL's host: only the socket is pinned.
    assert server.requests == ["GET /mcp HTTP/1.1"]


async def test_a_name_resolving_to_one_refused_address_among_others_is_refused():
    # A rebinding answer: the first address passes, the second is internal. Any
    # refused address refuses, so the OS cannot pick the internal one.
    async with _server() as server:
        check = _refuse("10.0.0.7")
        transport = PinnedTransport(check, refusal=REFUSAL, resolve=_answers("127.0.0.1", "10.0.0.7"))
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            with pytest.raises(httpx.ConnectError, match=REFUSAL):
                await client.get(f"http://rebind.example.test:{server.port}/")
    assert check.asked == ["127.0.0.1", "10.0.0.7"]
    assert server.accepted == []


async def test_a_rebinding_answer_between_two_requests_is_judged_on_each_connection():
    # The first lookup answers an allowed address, the next an internal one:
    # the second request opens a new connection and is refused there.
    async with _server() as server:
        answers = iter([["127.0.0.1"], ["169.254.169.254"]])

        async def _resolve(host: str, port: int) -> list:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in next(answers)]

        transport = PinnedTransport(_refuse("169.254.169.254"), refusal=REFUSAL, resolve=_resolve)
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            first = await client.get(f"http://rebind.example.test:{server.port}/")
            with pytest.raises(httpx.ConnectError, match=REFUSAL):
                await client.get(f"http://rebind.example.test:{server.port}/")
    assert first.status_code == 200
    assert server.accepted == ["127.0.0.1"]


async def test_a_redirect_to_a_refused_address_is_checked_on_the_hop_and_refused():
    # Both servers listen on loopback; the policy refuses the hop's host, so
    # the only way the target is spared is the hop being judged before its
    # connection opens.
    asked: list[tuple[str, str]] = []

    def _check(url: httpx.URL, ip) -> str | None:
        asked.append((url.host, str(ip)))
        return "internal" if url.host == "internal.example.test" else None

    async with _server() as target:
        hop = f"http://internal.example.test:{target.port}/latest/meta-data/"
        async with _server(status=302, location=hop) as redirector:
            transport = PinnedTransport(_check, refusal=REFUSAL, resolve=_answers("127.0.0.1"))
            async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=True) as client:
                with pytest.raises(httpx.ConnectError, match=REFUSAL):
                    await client.get(f"http://public.example.test:{redirector.port}/mcp")
    assert redirector.accepted == ["127.0.0.1"]
    assert target.accepted == []
    assert asked == [("public.example.test", "127.0.0.1"), ("internal.example.test", "127.0.0.1")]


async def test_a_literal_address_is_judged_without_a_lookup():
    resolve = _answers("127.0.0.1")
    check = _refuse("::1")
    transport = PinnedTransport(check, refusal=REFUSAL, resolve=resolve)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        with pytest.raises(httpx.ConnectError, match=REFUSAL):
            await client.get("http://[::1]:9/")
    assert resolve.calls == []
    assert check.asked == ["::1"]


async def test_the_check_receives_the_request_url_of_each_hop():
    seen: list[str] = []

    def _check(url: httpx.URL, ip) -> str | None:
        seen.append(str(url))
        return None

    async with _server() as server, httpx.AsyncClient(
        transport=PinnedTransport(_check, refusal=REFUSAL, resolve=_answers("127.0.0.1")), trust_env=False
    ) as client:
        await client.get(f"http://a.example.test:{server.port}/x?q=1")
    assert seen == [f"http://a.example.test:{server.port}/x?q=1"]


async def test_a_name_that_does_not_resolve_is_a_connect_error():
    async def _fail(host: str, port: int) -> list:
        raise socket.gaierror(socket.EAI_NONAME, "unknown")

    transport = PinnedTransport(_refuse(), refusal=REFUSAL, resolve=_fail)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        with pytest.raises(httpx.ConnectError, match=r"Cannot resolve nowhere\.example\.test"):
            await client.get("http://nowhere.example.test/")


async def test_a_connection_outside_a_checked_request_is_refused():
    backend = pinned_transport._PinningBackend(_refuse(), REFUSAL, _answers("127.0.0.1"))
    with pytest.raises(httpcore.ConnectError, match=REFUSAL):
        await backend.connect_tcp("127.0.0.1", 9)


def test_the_pool_sends_through_the_pinning_backend():
    # httpx builds its own pool; the transport replaces the private ``_pool``
    # that ``AsyncHTTPTransport.handle_async_request`` sends through. The
    # tests above prove the requests reach the check; this one names the
    # cause when an httpx upgrade moves that attribute.
    transport = PinnedTransport(_refuse(), refusal=REFUSAL)
    pool = getattr(transport, "_pool", None)
    assert isinstance(pool, httpcore.AsyncConnectionPool), (
        f"httpx {httpx.__version__} no longer keeps its pool in AsyncHTTPTransport._pool: "
        "PinnedTransport must be adapted before the upgrade, or connections go unchecked."
    )
    assert isinstance(getattr(pool, "_network_backend", None), pinned_transport._PinningBackend), (
        f"httpcore {httpcore.__version__} no longer keeps its network backend in "
        "AsyncConnectionPool._network_backend: check PinnedTransport against it."
    )


async def test_a_black_holed_first_address_costs_the_stagger_not_the_timeout():
    # 192.0.2.1 (TEST-NET-1) never answers; the second, checked address does.
    # Tried one after the other, the first would hold the connect for the full
    # timeout; raced, the second wins within a fraction of it.
    async with _server() as server:
        check = _refuse()
        transport = PinnedTransport(check, refusal=REFUSAL, resolve=_answers("192.0.2.1", "127.0.0.1"))
        async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=10.0) as client:
            started = asyncio.get_running_loop().time()
            response = await client.get(f"http://dual.example.test:{server.port}/")
            elapsed = asyncio.get_running_loop().time() - started
    assert response.status_code == 200
    assert server.accepted == ["127.0.0.1"]
    assert check.asked == ["192.0.2.1", "127.0.0.1"]
    assert elapsed < 3.0, elapsed


class _FailingFirst:
    """An inner backend whose attempts to the given addresses raise *error* (not an httpcore error)."""

    def __init__(self, inner, failing: set[str], error: Exception) -> None:
        self._inner, self._failing, self._error = inner, failing, error
        self.dialled: list[str] = []

    async def connect_tcp(self, host, port, **kwargs):
        self.dialled.append(host)
        if host in self._failing:
            raise self._error
        return await self._inner.connect_tcp(host, port, **kwargs)

    async def sleep(self, seconds):
        await self._inner.sleep(seconds)


@pytest.mark.parametrize("answers", [("127.0.0.2", "127.0.0.1"), ("127.0.0.1",)], ids=["raced", "single"])
async def test_an_attempt_failing_outside_httpcore_is_a_connect_error(answers):
    # Whatever an attempt raises, a failed connect reaches httpx as a
    # ConnectError (never an ExceptionGroup), and a raced attempt that fails
    # lets the next checked address connect.
    async with _server() as server:
        transport = PinnedTransport(_refuse(), refusal=REFUSAL, resolve=_answers(*answers))
        backend = transport._pool._network_backend
        failing = _FailingFirst(backend._inner, {answers[0]}, RuntimeError("boom"))
        backend._inner = failing
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            if len(answers) == 2:
                response = await client.get(f"http://dual.example.test:{server.port}/")
                assert response.status_code == 200
                assert server.accepted == ["127.0.0.1"]
            else:
                with pytest.raises(httpx.ConnectError, match="Connection attempt failed: RuntimeError"):
                    await client.get(f"http://single.example.test:{server.port}/")
                assert server.accepted == []
    assert failing.dialled == list(answers)


async def test_every_raced_attempt_failing_outside_httpcore_is_a_connect_error(caplog):
    transport = PinnedTransport(_refuse(), refusal=REFUSAL, resolve=_answers("127.0.0.2", "127.0.0.3"))
    backend = transport._pool._network_backend
    backend._inner = _FailingFirst(backend._inner, {"127.0.0.2", "127.0.0.3"}, RuntimeError("boom"))
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        with caplog.at_level("DEBUG", logger="app.core.pinned_transport"), pytest.raises(
            httpx.ConnectError, match="Connection attempt failed: RuntimeError"
        ) as failed:
            await client.get("http://none.example.test:9/")
    # The caller's message names neither the address nor the original error;
    # the operator's DEBUG log names both, for each address tried.
    assert "127.0.0" not in str(failed.value) and "boom" not in str(failed.value)
    attempts = [r for r in caplog.records if r.getMessage().startswith("Connection attempt to ")]
    assert [r.getMessage() for r in attempts] == [
        "Connection attempt to 127.0.0.2:9 failed",
        "Connection attempt to 127.0.0.3:9 failed",
    ]
    assert [str(r.exc_info[1]) for r in attempts] == ["boom", "boom"]


async def test_every_checked_address_failing_is_a_connect_error():
    # Two closed ports on loopback: both attempts fail, the error is the last one.
    transport = PinnedTransport(_refuse(), refusal=REFUSAL, resolve=_answers("127.0.0.1", "::1"))
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get("http://closed.example.test:9/")


def test_scoped_ipv6_addresses_are_judged_without_their_zone():
    assert pinned_transport._parse_address("fe80::1%eth0") == ipaddress.ip_address("fe80::1")
    assert pinned_transport._parse_address("not-an-address") is None
