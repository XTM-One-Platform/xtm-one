# SPDX-License-Identifier: Apache-2.0

"""Send a request to the address the destination policy checked, not to a name.

Checking a URL by resolving its host and then letting the HTTP client resolve
the host AGAIN to connect leaves a DNS-rebinding window: a short-TTL answer can
pass the check with a public address and point the connection at the metadata
service or the private network. Here the host is resolved once, every address
it resolves to must pass ``destination_policy.forbidden_address_reason``
(public only, metadata endpoints refused whatever happens), and the request is
sent to a checked address with the original name as ``Host`` and as the TLS
server name (``sni_hostname``), so the certificate is still verified against
the name.

Because the client only ever sees an address, what it keys by host is put
back under the name here: no connection is kept for reuse (a TLS connection
verified for one name would otherwise carry the next name on the same address,
whose certificate nobody checked), and cookies are stored and sent by the name.

The client is this module's own (:func:`pinned_client`), and :func:`send_pinned`
refuses any other: HTTP/1.1 only and no keep-alive (an HTTP/2 connection is
multiplexed, and ``Connection: close`` means nothing to it), no environment
proxy (``trust_env=False``: a proxy would resolve the name itself), no
redirect followed by httpx. Redirects are the caller's: each hop is a new
:func:`send_pinned`, resolved and checked again. The CA bundle variables
(``SSL_CERT_FILE`` / ``SSL_CERT_DIR``) are still honoured - they decide whom
the platform trusts, not where it connects - through an SSL context built
here, since httpx reads them only for a client that trusts the environment.
A deployment that reaches the web only through a proxy is told once at
startup that these requests bypass it (:func:`log_environment_proxy_bypass`).

A refusal raises :class:`PinnedDestinationError`, a ``ValueError`` whose text is
the same whether the name does not resolve or resolves inward: which internal
names exist, and where they point, goes to the logs only, so a fetch is never
an oracle on the internal network.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import ssl

import httpx

from app.core.destination_policy import IPAddress, forbidden_address_reason

logger = logging.getLogger(__name__)

#: The one text a caller sees for a destination that was not reached by policy.
REFUSED_MESSAGE = "URL host is unresolvable or targets a private or non-routable address"
#: Checked addresses tried, in order, while the previous one refuses the connection.
MAX_ADDRESSES_TRIED = 3
#: Marks a client :func:`pinned_client` built.
_PINNED_CLIENT = "_xtm_pinned_client"
#: The proxy variables httpx reads from the environment, which a pinned client ignores.
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")


class PinnedDestinationError(ValueError):
    """The destination was refused (``kind="refused"``) or did not resolve
    (``kind="unreachable"``). ``reason`` is for the logs only."""

    def __init__(self, message: str, *, reason: str, kind: str = "refused") -> None:
        super().__init__(message)
        self.reason = reason
        self.kind = kind


def _literal(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _refusal(reason: str, *, kind: str = "refused") -> PinnedDestinationError:
    logger.info("Outbound request refused: %s", reason)
    return PinnedDestinationError(REFUSED_MESSAGE, reason=reason, kind=kind)


def resolve_pinned_addresses(host: str, port: int | None = None) -> list[str]:
    """The checked addresses a request to *host* may go to, in the order to try.

    Every address the name resolves to must pass the policy: a name with one
    inward address among public ones is refused, because which one a client
    would pick is not this code's to guess. IPv4 first when both families are
    offered, so a container without an IPv6 route still reaches a dual-stack
    host. Blocking (``getaddrinfo``): async code goes through
    :func:`send_pinned`, which resolves on a worker thread.
    """
    literal = _literal(host)
    if literal is not None:
        addresses = [literal]
    else:
        try:
            infos = socket.getaddrinfo(host, port, family=0, type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError) as exc:
            raise _refusal(f"cannot resolve {host!r}: {exc}", kind="unreachable") from exc
        addresses = []
        for info in infos:
            try:
                address = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if address not in addresses:
                addresses.append(address)
    if not addresses:
        raise _refusal(f"{host!r} resolved to no address", kind="unreachable")
    for address in addresses:
        reason = forbidden_address_reason(address)
        if reason is not None:
            raise _refusal(f"{host!r} resolves to {address} ({reason})")
    return [str(a) for a in sorted(addresses, key=lambda a: a.version)]


def resolve_pinned_address(host: str, port: int | None = None) -> str:
    """The first address of :func:`resolve_pinned_addresses`."""
    return resolve_pinned_addresses(host, port)[0]


async def _resolve(host: str, port: int | None, deadline: float | None) -> list[str]:
    """Resolution on a worker thread (a slow resolver must not stall the event
    loop), bounded like a connect: a name whose resolver never answers
    costs the fetch its connect timeout, not the resolver's own."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(resolve_pinned_addresses, host, port), deadline
        )
    except TimeoutError as exc:
        raise _refusal(f"no DNS answer for {host!r} in {deadline}s", kind="unreachable") from exc


def _refile_cookies(client: httpx.AsyncClient, response: httpx.Response, address: str) -> None:
    """Store the response's cookies under the name, never under the address.

    httpx filed them by the URL it sent, the address: kept there, a cookie of
    one site would ride along to any other name pinned to the same address,
    and a cookie scoped to the site's domain would be dropped as foreign.
    """
    jar = client.cookies.jar
    for cookie in list(jar):
        # An IPv6 host is filed as ``[addr].local`` (no dot in the name).
        if cookie.domain.lstrip(".").removesuffix(".local").strip("[]") == address:
            jar.clear(cookie.domain, cookie.path, cookie.name)
    client.cookies.extract_cookies(response)


def _tls_failure(exc: BaseException) -> bool:
    """Whether a connect error is TLS refusing the server (httpx wraps the
    ``ssl`` error as a ``ConnectError``), rather than no connection at all."""
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        if isinstance(current, ssl.SSLError):
            return True
        visited.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def pinned_client(
    *, timeout: float | httpx.Timeout, headers: dict[str, str] | None = None
) -> httpx.AsyncClient:
    """The only client :func:`send_pinned` sends with.

    Its pool keys connections by address, so none may outlive its request:
    HTTP/1.1 only (``http2=False``) and no keep-alive. Platform-internal: it
    never goes through a relay route, so the destination policy is never
    skipped for it. Certificates are verified against ``SSL_CERT_FILE`` /
    ``SSL_CERT_DIR`` when set (certifi otherwise), read the way httpx reads
    them, while ``trust_env=False`` keeps the environment's proxies out.
    """
    client = httpx.AsyncClient(
        http1=True,
        http2=False,
        verify=httpx.create_ssl_context(trust_env=True),
        trust_env=False,
        follow_redirects=False,
        limits=httpx.Limits(max_keepalive_connections=0),
        timeout=timeout,
        headers=headers,
    )
    setattr(client, _PINNED_CLIENT, True)
    return client


def environment_proxy_variables() -> list[str]:
    """The proxy variables set in this process's environment, by name (either case)."""
    return [
        name
        for name in PROXY_VARIABLES
        if (os.environ.get(name) or os.environ.get(name.lower()) or "").strip()
    ]


def log_environment_proxy_bypass() -> None:
    """Warn once, at startup, that the pinned requests do not use the environment's proxy.

    A deployment that reaches the web only through ``HTTP(S)_PROXY`` would
    otherwise see these fetches time out with nothing saying why. Only the
    variables' names are logged: a proxy URL may carry credentials.
    """
    names = environment_proxy_variables()
    if not names:
        return
    logger.warning(
        "%s is set, but the web page fetches (the web_fetch tool, the knowledge base URL crawl, "
        "Deep Investigation web pages and HTTP sources) do not go through a proxy: they "
        "connect directly to the address the destination policy checked. Give the platform and the "
        "worker direct outbound access to ports 80 and 443 for them; see the deployment "
        "configuration documentation, section \"Outbound proxy and certificate authorities\".",
        " / ".join(names),
    )


async def send_pinned(
    client: httpx.AsyncClient,
    method: str,
    url: str | httpx.URL,
    *,
    headers: dict[str, str] | None = None,
    stream: bool = False,
) -> httpx.Response:
    """Send one request to a checked address of *url*'s host.

    Only the scheme and the address are judged here; anything else about the
    URL (credentials, path) is the caller's policy. *client* comes from
    :func:`pinned_client`; any other is refused. When an address refuses the
    connection, the next checked address is tried (at most
    :data:`MAX_ADDRESSES_TRIED`); a connect timeout or a TLS failure is raised
    at once, so a black-holed host costs one connect timeout, and a
    certificate that does not name the host is never tried elsewhere. The response's ``request`` is the request as
    the caller named it, so ``response.url``, ``raise_for_status()`` and a
    redirect's base URL carry the name, never the address it was pinned to.
    """
    if not getattr(client, _PINNED_CLIENT, False):
        raise TypeError("send_pinned sends with a client from pinned_client() only")
    try:
        named = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise PinnedDestinationError("Invalid URL", reason=f"invalid URL: {exc}") from exc
    if named.scheme not in ("http", "https"):
        raise PinnedDestinationError(
            f"URL scheme must be http or https, got: {named.scheme or 'empty'}",
            reason=f"scheme {named.scheme!r}",
        )
    # The ASCII (IDNA) form: what is resolved, sent as `Host` and as SNI.
    host = named.raw_host.decode("ascii")
    if not host:
        raise PinnedDestinationError("URL must have a hostname", reason="no host")
    pinned = _literal(host) is None
    addresses = await _resolve(host, named.port, client.timeout.connect)

    request_headers = dict(headers or {})
    # One connection per request: the pool keys connections by address.
    request_headers["Connection"] = "close"
    extensions: dict = {}
    if pinned:
        request_headers["Host"] = host if named.port is None else f"{host}:{named.port}"
        if named.scheme == "https":
            # The certificate is checked against the name, not the address.
            extensions["sni_hostname"] = host
        # Cookies go by the name: the jar would match them against the address.
        by_name = httpx.Request(method, named)
        client.cookies.set_cookie_header(by_name)
        if "cookie" in by_name.headers:
            request_headers["Cookie"] = by_name.headers["cookie"]

    tried = addresses[:MAX_ADDRESSES_TRIED] if pinned else [host]
    for index, address in enumerate(tried):
        target = named.copy_with(host=address) if pinned else named
        request = client.build_request(method, target, headers=request_headers, extensions=extensions)
        try:
            response = await client.send(request, stream=stream)
        except httpx.ConnectError as exc:
            if index + 1 == len(tried) or _tls_failure(exc):
                raise
            continue
        response.request = httpx.Request(method, named, headers=request.headers)
        if pinned:
            _refile_cookies(client, response, address)
        return response
    raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises
