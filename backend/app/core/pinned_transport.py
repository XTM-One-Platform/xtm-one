# SPDX-License-Identifier: Apache-2.0

"""An httpx transport that connects only to addresses a policy admits.

A guard that checks what a name resolves to and then lets the client resolve
it again judges one answer and connects to another: a name that answers
differently the second time (DNS rebinding) passes. :class:`PinnedTransport`
closes that gap where it can be closed, at the TCP connect. httpcore hands the
host it is about to connect to its network backend; this one resolves it ONCE,
asks the policy about EVERY address it got (a name answering both a public and
an internal address would otherwise pass on the public one), and opens the
socket to a checked address itself. TLS still verifies the certificate against
the URL's host - httpcore passes the origin host as SNI - so pinning the address
changes nothing about who the server must prove it is.

Every request reaches :meth:`PinnedTransport.handle_async_request`, each hop
of a redirect a client follows included, and a new origin means a new
connection: a server answering with a redirect to an internal address is
checked again on that hop and refused there.

A client using it must be built with ``trust_env=False``: no proxy, whose
address would be the one connected to, and no ``.netrc`` credentials. The CA
bundle variables (``SSL_CERT_FILE`` / ``SSL_CERT_DIR``) are still honoured -
they decide whom the platform trusts, not where it connects.

A refusal raises ``httpx.ConnectError`` with the caller's generic message; why
(the host, the address, its class) goes to the log only, so the error an end
user sees is no oracle for how internal names resolve.
"""

from __future__ import annotations

import contextvars
import ipaddress
import logging
import socket
import ssl
from collections.abc import Awaitable, Callable, Iterable

import anyio
import httpcore
import httpx

from app.core.destination_policy import IPAddress

logger = logging.getLogger(__name__)

#: Why *address* may not be connected to for a request to *url*, or ``None``.
AddressCheck = Callable[[httpx.URL, IPAddress], str | None]
#: Every ``getaddrinfo`` entry for ``(host, port)``.
Resolver = Callable[[str, int], Awaitable[list]]

# The request being sent, read by the network backend when httpcore opens its
# connection - inline, in the task that sends the request.
_REQUEST_URL: contextvars.ContextVar[httpx.URL | None] = contextvars.ContextVar(
    "pinned_transport_request_url", default=None
)

#: How long an unanswered connection attempt waits before the next address is
#: tried in parallel (RFC 8305 recommends 250 ms, anyio's own default).
_HAPPY_EYEBALLS_DELAY = 0.25

_DEFAULT_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=5.0)


async def system_resolve(host: str, port: int) -> list:
    """The resolver a connection uses unless told otherwise: the system's, off the event loop."""
    return await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)


class PolicyRefusal(httpcore.ConnectError):
    """A connection the address check refused: no attempt was made, so a retry
    would only be refused again (``is_policy_refusal`` finds it under httpx's
    own ``ConnectError``)."""


def is_policy_refusal(exc: BaseException) -> bool:
    """Whether *exc*, or an exception it was raised from, is a ``PolicyRefusal``."""
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        if isinstance(current, PolicyRefusal):
            return True
        visited.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _parse_address(address: str) -> IPAddress | None:
    try:
        # A scoped IPv6 address (``fe80::1%eth0``) is judged without its zone.
        return ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return None


def _log_host(host: str) -> str:
    return repr(host[:200])


class _PinningBackend(httpcore.AsyncNetworkBackend):
    """Resolves once, checks every address, connects to a checked one."""

    def __init__(self, check: AddressCheck, refusal: str, resolve: Resolver) -> None:
        self._check = check
        self._refusal = refusal
        self._resolve = resolve
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        url = _REQUEST_URL.get()
        if url is None:
            # A connection opened outside a request this transport sent has
            # no URL to judge: refused rather than waved through.
            logger.warning("Refused a connection to %s outside a checked request", _log_host(host))
            raise PolicyRefusal(self._refusal)
        literal = _parse_address(host)
        if literal is not None:
            addresses = [literal]
        else:
            try:
                with anyio.fail_after(timeout):
                    infos = await self._resolve(host, port)
            except TimeoutError as exc:
                raise httpcore.ConnectTimeout(f"Timed out resolving {host}") from exc
            except (OSError, UnicodeError) as exc:
                raise httpcore.ConnectError(f"Cannot resolve {host}") from exc
            addresses = list(dict.fromkeys(ip for info in infos if (ip := _parse_address(str(info[4][0])))))
            if not addresses:
                raise httpcore.ConnectError(f"Cannot resolve {host}")
        for address in addresses:
            reason = self._check(url, address)
            if reason:
                logger.warning(
                    "Refused to connect to %s: it %s %s, a %s",
                    _log_host(host),
                    "is" if literal is not None else "resolves to",
                    address,
                    reason,
                )
                raise PolicyRefusal(self._refusal)
        return await self._connect_first(addresses, port, timeout, local_address, socket_options)

    async def _connect_first(
        self,
        addresses: list[IPAddress],
        port: int,
        timeout: float | None,
        local_address: str | None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None,
    ) -> httpcore.AsyncNetworkStream:
        """The first of the checked *addresses* to connect, nothing else dialled.

        Happy Eyeballs (RFC 8305) over the list the resolver gave, in its
        order: the next attempt starts when the previous one fails, or after
        :data:`_HAPPY_EYEBALLS_DELAY` without an answer, and the first
        connection wins. An address that black-holes therefore costs the delay,
        not the whole connect timeout, as with httpcore's own backend.
        """
        if len(addresses) == 1:
            return await self._connect(addresses[0], port, timeout, local_address, socket_options)
        winner: list[httpcore.AsyncNetworkStream] = []
        errors: list[Exception] = []

        async def _attempt(address: IPAddress, failed: anyio.Event, scope: anyio.CancelScope) -> None:
            try:
                stream = await self._connect(address, port, timeout, local_address, socket_options)
            except Exception as exc:  # any failed attempt: recorded, the next one starts
                errors.append(exc)
                failed.set()
                return
            if winner:
                await stream.aclose()
                return
            winner.append(stream)
            scope.cancel()

        async with anyio.create_task_group() as attempts:
            for address in addresses:
                failed = anyio.Event()
                attempts.start_soon(_attempt, address, failed, attempts.cancel_scope)
                with anyio.move_on_after(_HAPPY_EYEBALLS_DELAY):
                    await failed.wait()
        if winner:
            return winner[0]
        raise errors[-1]

    async def _connect(
        self,
        address: IPAddress,
        port: int,
        timeout: float | None,
        local_address: str | None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None,
    ) -> httpcore.AsyncNetworkStream:
        """One attempt; a failed one is always ``ConnectError`` / ``ConnectTimeout``.

        httpcore's backend maps the socket errors itself today; anything else
        it may raise is mapped here, so that what httpx and the MCP client see
        of a failed connect never depends on httpcore internals. The address is
        left out of the message: it may be what an internal name resolves to.
        """
        try:
            return await self._inner.connect_tcp(
                str(address), port, timeout=timeout, local_address=local_address, socket_options=socket_options
            )
        except (httpcore.ConnectError, httpcore.ConnectTimeout):
            raise
        except Exception as exc:
            # The address and the original error go to the log, never to the caller.
            logger.debug("Connection attempt to %s:%s failed", address, port, exc_info=exc)
            raise httpcore.ConnectError(f"Connection attempt failed: {type(exc).__name__}") from exc

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise PolicyRefusal(self._refusal)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PinnedTransport(httpx.AsyncHTTPTransport):
    """``httpx.AsyncHTTPTransport`` whose every connection *check* admits.

    *refusal* is the message a refused connection raises. *resolve* stands in
    for DNS (a test seam); it is called once per connection opened.
    *ssl_context* verifies the server's certificate against a CA of the
    caller's own (a vault's pasted CA); by default, the system's trust store
    and ``SSL_CERT_FILE`` / ``SSL_CERT_DIR``.
    """

    def __init__(
        self,
        check: AddressCheck,
        *,
        refusal: str,
        resolve: Resolver | None = None,
        http2: bool = False,
        limits: httpx.Limits = _DEFAULT_LIMITS,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if ssl_context is None:
            ssl_context = httpx.create_ssl_context(trust_env=True)
        super().__init__(verify=ssl_context, trust_env=False, http2=http2, limits=limits)
        # The pool httpx built connects through httpcore's default backend;
        # this one is the same pool on the pinning backend. ``_pool`` is what
        # ``handle_async_request`` sends through (httpx pinned in
        # pyproject.toml; ``tests/test_pinned_transport.py`` fails if it moves).
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=True,
            http2=http2,
            network_backend=_PinningBackend(check, refusal, resolve or system_resolve),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        token = _REQUEST_URL.set(request.url)
        try:
            return await super().handle_async_request(request)
        finally:
            _REQUEST_URL.reset(token)
