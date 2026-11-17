# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Where a remote MCP server may be reached: the MCP server URL policy.

A remote MCP server (``sse`` / ``streamable_http``) is a URL any user with the
agentic licence can save, and the platform then connects to it from its own
network - on a test, a tool call, OAuth discovery - and hands back what it
answered. Left unchecked that is a request-forgery primitive against internal
services and cloud metadata endpoints. The policy is one security option,
``mcp_server_url_policy`` (``MCP_SERVER_URL_POLICY``, or Settings > Access and security):

* ``permissive`` (default) - only cloud metadata addresses
  (``METADATA_ADDRESSES``) are refused, so every MCP server that worked before
  the policy existed keeps working, one on ``localhost`` included;
* ``private_networks`` - the operator-network policy of
  ``app.core.destination_policy``: loopback, link-local, unspecified, reserved
  and multicast addresses are refused too; an on-prem server on a private
  network keeps working;
* ``public_only`` - every address that is not globally routable is refused;
  an on-prem server is then reached through a relay group.

``mcp_server_trusted_origins`` (``MCP_SERVER_TRUSTED_ORIGINS``) names origins
reached at any address but a metadata one under the two strict modes. A cloud
metadata address is refused in every mode and no setting lifts it.

The policy runs twice:

* **on save** (:func:`url_problem` in the request models, a 422;
  :func:`assert_server_url_allowed` in the routes, a 422 too): the URL's
  syntax, a host that is or spells an address, and every address a name
  resolves to now. A name that does not resolve is accepted - the server may
  not be up yet, and connect time decides;
* **on every connection** (:func:`guarded_client`, the MCP SDK's client
  factory, the auth probe and OAuth discovery): through
  ``app.core.pinned_transport``, on the address actually connected to, with
  no second lookup, each redirect hop checked again (behind a proxy, see
  below).

Behind an outbound proxy (``HTTP_PROXY`` / ``HTTPS_PROXY``, ``NO_PROXY``
applied), the proxy resolves and connects, so no platform-side check can see
the address connected to. ``permissive`` keeps using such a proxy, as before
the policy existed, after refusing a URL that spells or resolves (from here)
to a metadata address; the two strict modes never use it and connect
directly, so that the connected address is the one judged.

A server routed through a relay group is reached from the customer's network:
the relay checks its own allow-list, so the platform judges only what the URL
itself spells (a metadata address is still refused) and resolves nothing.

Refusals answer one generic message (:data:`REFUSED_DETAIL`); the host, the
address and its class go to the log only, so a refusal is no oracle for how
internal names resolve.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import urllib.request
from typing import Any

import httpx

from app.core.destination_policy import (
    IPAddress,
    forbidden_address_reason,
    is_metadata_address,
    shorthand_ipv4_address,
)
from app.core.egress import egress_client
from app.core.pinned_transport import PinnedTransport
from app.core.security_options import security_option

logger = logging.getLogger(__name__)

PERMISSIVE = "permissive"
PRIVATE_NETWORKS = "private_networks"
PUBLIC_ONLY = "public_only"

#: The transports that reach a server over the network; ``stdio`` runs a process.
REMOTE_TRANSPORTS = frozenset({"sse", "streamable_http"})

#: What a refused URL or connection answers, whatever the reason.
REFUSED_DETAIL = (
    "This MCP server URL is not allowed: the platform does not connect to that address. "
    "An administrator decides which addresses MCP servers may use "
    "(Settings > Access and security > MCP server URL policy)."
)

_DEFAULT_PORTS = {"http": 80, "https": 443}

# A lookup on save is a courtesy (connect time decides), so it is bounded
# tightly: a slow resolver costs the save this long, at most.
_SAVE_DNS_TIMEOUT_SECONDS = 5.0


class MCPServerURLRefusedError(ValueError):
    """A URL the MCP server URL policy refuses; ``str()`` is safe to show."""


def origin_keys(url: httpx.URL) -> set[str]:
    """``url``'s origin as the trusted-origin list spells it, default port both ways.

    httpx drops a default port (``http://h:80`` is ``http://h``) and the list
    keeps what was typed, so both forms are looked up.
    """
    base = f"{url.scheme}://{url.host}"
    if url.port is not None:
        return {f"{base}:{url.port}"}
    default = _DEFAULT_PORTS.get(url.scheme)
    return {base, f"{base}:{default}"} if default else {base}


def is_trusted_origin(url: httpx.URL) -> bool:
    return not origin_keys(url).isdisjoint(security_option("mcp_server_trusted_origins"))


def address_refusal(url: httpx.URL, ip: IPAddress) -> str | None:
    """Why the policy refuses *ip* for a request to *url*, or ``None``."""
    if is_metadata_address(ip):
        return "cloud metadata endpoint"
    policy = security_option("mcp_server_url_policy")
    if policy == PERMISSIVE:
        return None
    if is_trusted_origin(url):
        return None
    # Anything but the two named modes is held to the strictest.
    return forbidden_address_reason(ip, allow_private=policy == PRIVATE_NETWORKS)


def parse_server_url(url: str) -> httpx.URL:
    """*url* as the MCP client will request it, or a refusal naming what is wrong.

    Parsed by httpx, which makes the request, so the guard and the request
    cannot disagree about the host. Nothing here needs the network, and each
    message is about the text the caller typed.
    """
    try:
        parsed = httpx.URL(url.strip())
    except (httpx.InvalidURL, TypeError, ValueError) as exc:
        raise MCPServerURLRefusedError("The MCP server URL is not a valid URL.") from exc
    if parsed.scheme not in _DEFAULT_PORTS:
        raise MCPServerURLRefusedError("The MCP server URL must be an http or https URL.")
    if not parsed.host:
        raise MCPServerURLRefusedError("The MCP server URL has no host.")
    if parsed.port is not None and not 0 < parsed.port <= 65535:
        # httpx parses any number; no connection can be opened to it.
        raise MCPServerURLRefusedError("The MCP server URL has an invalid port.")
    return parsed


def _spelled_address(url: httpx.URL) -> IPAddress | None:
    """The address the host is or spells (an IPv4 shorthand), with no lookup."""
    host = url.host
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    shorthand = shorthand_ipv4_address(url.raw_host.decode("ascii"))
    return ipaddress.ip_address(shorthand) if shorthand else None


def _refused(url: httpx.URL, verb: str, ip: IPAddress, why: str) -> MCPServerURLRefusedError:
    logger.info("MCP server URL refused: host %r %s %s, a %s", url.host[:200], verb, ip, why)
    return MCPServerURLRefusedError(REFUSED_DETAIL)


def url_problem(url: str | None) -> str | None:
    """What makes *url* unusable whatever the route, or ``None``; no network.

    For the request models: a malformed URL, another scheme, no host, or a
    host that is (or spells) a cloud metadata address. Blank is "unset".
    """
    if url is None or not url.strip():
        return None
    try:
        parsed = parse_server_url(url)
        ip = _spelled_address(parsed)
        if ip is not None and is_metadata_address(ip):
            raise _refused(parsed, "is", ip, "cloud metadata endpoint")
    except MCPServerURLRefusedError as exc:
        return str(exc)
    return None


async def _resolve(host: str, port: int) -> list:
    """The loop's resolver: one seam, so a test can stand in for DNS."""
    return await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)


async def assert_server_url_allowed(url: str | None, *, routed: bool) -> None:
    """Raise :class:`MCPServerURLRefusedError` when *url* may not be saved.

    *routed*: the server is reached through a relay group, so only what the
    URL spells is judged. Blank is "unset" and left to the caller.
    """
    if url is None or not url.strip():
        return
    problem = url_problem(url)
    if problem:
        raise MCPServerURLRefusedError(problem)
    if routed:
        return
    parsed = parse_server_url(url)
    spelled = _spelled_address(parsed)
    if spelled is not None:
        verb, addresses = "is", [spelled]
    else:
        port = parsed.port or _DEFAULT_PORTS[parsed.scheme]
        try:
            infos = await asyncio.wait_for(
                _resolve(parsed.raw_host.decode("ascii"), port), _SAVE_DNS_TIMEOUT_SECONDS
            )
        except (OSError, TimeoutError, UnicodeError):
            # No destination to judge; the connection decides when it opens.
            return
        verb = "resolves to"
        addresses = [ip for info in infos if (ip := _parse_ip(str(info[4][0]))) is not None]
    for ip in addresses:
        why = address_refusal(parsed, ip)
        if why:
            raise _refused(parsed, verb, ip, why)


def _parse_ip(address: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return None


def guarded_transport() -> PinnedTransport:
    """A transport whose every connection the policy admits, on the address connected to."""
    return PinnedTransport(address_refusal, refusal=REFUSED_DETAIL, resolve=lambda host, port: _resolve(host, port))


def environment_proxy(url: httpx.URL) -> str | None:
    """The proxy ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY`` name for *url*, ``NO_PROXY`` applied."""
    proxies = urllib.request.getproxies_environment()
    if not proxies or urllib.request.proxy_bypass_environment(url.host, proxies):
        return None
    return proxies.get(url.scheme) or proxies.get("all")


async def _refuse_metadata_behind_proxy(request: httpx.Request) -> None:
    """What ``permissive`` refuses, judged before a request leaves through a proxy.

    The proxy resolves and connects, so the platform judges what the URL
    spells and what the name resolves to from here. A name that does not
    resolve here is left to the proxy: proxy-only networks often cannot.
    """
    url = request.url
    spelled = _spelled_address(url)
    if spelled is not None:
        verb, addresses = "is", [spelled]
    else:
        try:
            infos = await asyncio.wait_for(
                _resolve(url.raw_host.decode("ascii"), url.port or _DEFAULT_PORTS.get(url.scheme, 443)),
                _SAVE_DNS_TIMEOUT_SECONDS,
            )
        except (OSError, TimeoutError, UnicodeError):
            return
        verb = "resolves to"
        addresses = [ip for info in infos if (ip := _parse_ip(str(info[4][0]))) is not None]
    for ip in addresses:
        if why := address_refusal(url, ip):
            logger.warning("Refused to send %r through the outbound proxy: it %s %s, a %s", url.host[:200], verb, ip, why)
            raise httpx.ConnectError(REFUSED_DETAIL, request=request)


_proxy_ignored_warned = False


def _warn_proxy_ignored(policy: str) -> None:
    """Said once per process: a strict policy connects directly, whatever the proxy variables say."""
    global _proxy_ignored_warned
    if _proxy_ignored_warned:
        return
    _proxy_ignored_warned = True
    logger.warning(
        "MCP_SERVER_URL_POLICY=%s: remote MCP servers are connected to directly and the outbound "
        "proxy in HTTP_PROXY / HTTPS_PROXY is not used, because the address connected to is what "
        "the policy judges. Route a server the platform cannot reach directly through a relay group.",
        policy,
    )


class _PolicyTransport(httpx.AsyncBaseTransport):
    """Chooses, per request, how a direct MCP request leaves the platform.

    Under ``permissive`` (which refuses only cloud metadata), a URL the
    environment sends through a proxy still goes through it, as before the
    policy existed, after :func:`_refuse_metadata_behind_proxy`. Every other
    request takes :func:`guarded_transport`, the address it connects to
    judged. Each transport is built on its first use, so a client whose
    requests a relay group carries builds none.
    """

    def __init__(self) -> None:
        self._pinned: PinnedTransport | None = None
        self._proxied: dict[str, httpx.AsyncHTTPTransport] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        proxy = environment_proxy(request.url)
        if proxy is not None:
            policy = security_option("mcp_server_url_policy")
            if policy == PERMISSIVE:
                await _refuse_metadata_behind_proxy(request)
                if proxy not in self._proxied:
                    self._proxied[proxy] = httpx.AsyncHTTPTransport(
                        proxy=proxy, verify=httpx.create_ssl_context(trust_env=True), trust_env=False
                    )
                return await self._proxied[proxy].handle_async_request(request)
            _warn_proxy_ignored(policy)
        if self._pinned is None:
            self._pinned = guarded_transport()
        return await self._pinned.handle_async_request(request)

    async def aclose(self) -> None:
        for transport in (self._pinned, *self._proxied.values()):
            if transport is not None:
                await transport.aclose()


def guarded_client(**kwargs: Any) -> httpx.AsyncClient:
    """``egress_client(**kwargs)`` whose direct requests are held to the policy.

    A request a relay group carries goes to the relay instead (the route wins
    over the transport, every redirect hop included); every other one goes
    through :class:`_PolicyTransport`. The client reads nothing from the
    environment itself (``trust_env=False``): the transport alone decides
    whether a proxy is used.
    """
    return egress_client(transport=_PolicyTransport(), trust_env=False, **kwargs)
