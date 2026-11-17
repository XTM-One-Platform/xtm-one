# SPDX-License-Identifier: Apache-2.0

"""Where a custom bundle integration's API may be: the base URL policy.

An integration of an imported ``filigran-integration-v1`` bundle (a ``custom:``
provider) reaches its API at a base URL read from its ``extra_config``:
``base_url``, or the key a tool's execution spec names in ``base_url_key``.
Anyone who can see the provider may create a personal integration of it, so
that value is user-supplied. Left unchecked, "Test connection" and the bundle's
tool calls would reach loopback services, link-local and cloud metadata
endpoints from the platform's own network, the integration's credential
attached.

The policy is the one OpenAPI integrations already apply to their
user-supplied base URL: the operator-network policy of
``app.core.destination_policy`` (``allow_private=True``). Private ranges and
the RFC 6598 shared address space stay reachable, because a bundle
legitimately targets an on-prem API. Loopback, link-local, multicast,
unspecified, reserved and cloud metadata addresses are refused.

It runs twice:

* **on save** (:func:`assert_base_url_allowed`, a 422 through
  ``helpers.require_allowed_base_urls``): the URL's syntax, the address the
  host is or spells, and every address a name resolves to now. A name that
  does not resolve, or not in time, is accepted: the API may not be up yet,
  and the connection decides;
* **on every connection**: the bundle's tool calls
  (``execute_declarative_tool`` with ``enforce_destination_guard``) and the
  "Test connection" probe (:func:`probe_transport`) connect through the pinned
  transport of ``app.services.web_fetch``: the address connected to is the
  one checked, with no second lookup, so a short-TTL DNS answer cannot turn
  the connection inward after the check.

An integration routed through a relay group is reached from the customer's
network, and the relay checks its own allow-list. On save, only a host that is
or spells a cloud metadata address is refused, and nothing is resolved; at
connection time the route wins over the transport.

A refusal answers one generic message (:data:`REFUSED_DETAIL`). The host, the
address and its class go to the log only, so a refusal is no oracle for how
internal names resolve.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Any

import httpx

from app.core.destination_policy import (
    IPAddress,
    forbidden_address_reason,
    is_metadata_address,
    shorthand_ipv4_address,
)

logger = logging.getLogger(__name__)

#: The operator-network policy: private ranges admitted (on-prem APIs).
ALLOW_PRIVATE = True

#: The ``extra_config`` key a spec without ``base_url_key`` reads.
DEFAULT_BASE_URL_KEY = "base_url"

#: What a refused base URL answers, whatever the reason.
REFUSED_DETAIL = (
    "This base URL is not allowed: the platform does not connect to loopback, "
    "link-local, reserved or cloud metadata addresses. Use the API's own address, "
    "or route the integration through a relay group."
)

_INVALID_DETAIL = "The base URL must be an http or https URL with a host."

_DEFAULT_PORTS = {"http": 80, "https": 443}

# A lookup on save only makes the mistake visible where it is made (the
# connection decides), so a slow resolver costs the save this long at most.
_SAVE_DNS_TIMEOUT_SECONDS = 5.0


class BaseURLRefusedError(ValueError):
    """A base URL the policy refuses; ``str()`` is safe to show."""


def base_url_keys(metadata_json: Any) -> list[str]:
    """The ``extra_config`` keys a bundle reads a base URL from.

    ``base_url`` (what the probe and a spec without ``base_url_key`` read) and
    every ``base_url_key`` the bundle's tools name.
    """
    keys = {DEFAULT_BASE_URL_KEY}
    tools: Any = None
    if isinstance(metadata_json, dict):
        tools = metadata_json.get("tools")
        if not isinstance(tools, list):
            tools = metadata_json.get("available_tools")
    for tool in tools if isinstance(tools, list) else []:
        execution = tool.get("execution") if isinstance(tool, dict) else None
        key = execution.get("base_url_key") if isinstance(execution, dict) else None
        if isinstance(key, str) and key:
            keys.add(key)
    return sorted(keys)


def _parse(url: str) -> httpx.URL:
    """*url* as httpx, which makes the requests, reads it; or a refusal."""
    try:
        parsed = httpx.URL(url.strip())
    except (httpx.InvalidURL, TypeError, ValueError) as exc:
        raise BaseURLRefusedError(_INVALID_DETAIL) from exc
    if parsed.scheme not in _DEFAULT_PORTS or not parsed.host:
        raise BaseURLRefusedError(_INVALID_DETAIL)
    if parsed.port is not None and not 0 < parsed.port <= 65535:
        raise BaseURLRefusedError(_INVALID_DETAIL)
    return parsed


def _spelled_address(url: httpx.URL) -> IPAddress | None:
    """The address the host is or spells (an IPv4 shorthand), with no lookup."""
    try:
        return ipaddress.ip_address(url.host)
    except ValueError:
        pass
    shorthand = shorthand_ipv4_address(url.raw_host.decode("ascii"))
    return ipaddress.ip_address(shorthand) if shorthand else None


def _parse_ip(address: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return None


async def _resolve(host: str, port: int) -> list:
    """The loop's resolver: one seam, so a test can stand in for DNS."""
    return await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)


def _refused(url: httpx.URL, verb: str, ip: IPAddress, why: str) -> BaseURLRefusedError:
    logger.info("Integration base URL refused: host %r %s %s, a %s", url.host[:200], verb, ip, why)
    return BaseURLRefusedError(REFUSED_DETAIL)


async def assert_base_url_allowed(url: object, *, routed: bool) -> None:
    """Raise :class:`BaseURLRefusedError` when *url* may not be saved as a base URL.

    *routed*: the integration is reached through a relay group, so only what
    the URL spells is judged. A blank or non-string value is "unset" (the
    executor reads it as missing) and left alone.
    """
    if not isinstance(url, str) or not url.strip():
        return
    parsed = _parse(url)
    spelled = _spelled_address(parsed)
    if routed:
        if spelled is not None and is_metadata_address(spelled):
            raise _refused(parsed, "is", spelled, "cloud metadata endpoint")
        return
    if spelled is not None:
        verb, addresses = "is", [spelled]
    else:
        port = parsed.port or _DEFAULT_PORTS[parsed.scheme]
        try:
            infos = await asyncio.wait_for(
                _resolve(parsed.raw_host.decode("ascii"), port), _SAVE_DNS_TIMEOUT_SECONDS
            )
        except (OSError, TimeoutError, UnicodeError):
            # Nothing to judge yet; the connection decides when it opens.
            return
        verb = "resolves to"
        addresses = [ip for info in infos if (ip := _parse_ip(str(info[4][0]))) is not None]
    for ip in addresses:
        why = forbidden_address_reason(ip, allow_private=ALLOW_PRIVATE)
        if why:
            raise _refused(parsed, verb, ip, why)


def probe_transport() -> httpx.AsyncBaseTransport:
    """The transport of a bundle's "Test connection" probe.

    Pinned to an address the policy admits, like the tool calls. The
    certificate authorities are the default bundle, never ``SSL_CERT_FILE`` /
    ``SSL_CERT_DIR``: the probe reads nothing from the environment.
    """
    from app.services.web_fetch import guarded_transport

    return guarded_transport(
        allow_private=ALLOW_PRIVATE, verify=httpx.create_ssl_context(trust_env=False)
    )
