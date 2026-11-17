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

"""Where this platform will and will not send an outbound A2A request.

**Why this module exists at all.** No deployment egress policy constrains
backend-initiated ``httpx`` calls — the repository's only egress allow-list
(``opensandbox_network_allow``) builds network rules for OpenSandbox
containers — so the guard has to live here.

What the platform can honestly own is split in two, and this module is the
first half:

* **A scoped guard (FR-035)** — a card location is *operator-supplied* and
  fetched by the backend *with stored credentials*, which is a live
  request-forgery surface rather than a theoretical one. Loopback, private,
  link-local, cloud-metadata and any other non-globally-routable destination
  is refused outright — except for origins an operator has named one by one
  in ``a2a_client_dev_trusted_origins`` (the variable, or its Settings >
  Security field), which is empty by default and exists so the outbound
  half can be exercised against a second local deployment.
  Cloud metadata is exempt from that exemption.
* **Origin pinning (FR-034)** — every request must go to the origin the
  registration was verified against. A card that later declares an endpoint
  elsewhere, or a redirect off that origin, is refused.

Enforcing network policy stays where it belongs, in the deployment. When the
deployment refuses a destination, ``failures.py`` classifies it as durable and
it surfaces in the health verdict (FR-054) — this module does not pretend to
have prevented it.

Deliberately NOT an allow-list of permitted partners. That would make an
operator's sandbox rules decide which partners are reachable, which is a
different feature and a worse one.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from app.core.destination_policy import (
    METADATA_ADDRESSES,
    forbidden_address_reason,
    is_metadata_address,
)
from app.core.security_options import security_option
from app.models.a2a import (
    UNUSABLE_CARD_MALFORMED,
    UNUSABLE_CARD_UNFETCHABLE,
    UNUSABLE_DESTINATION_NOT_PERMITTED,
    UNUSABLE_ORIGIN_MISMATCH,
    UNUSABLE_TRANSPORT_UNVERIFIED,
)

# The addresses no allow-list entry can ever reach. Everything else the guard
# refuses is a judgement about reachability that an operator may legitimately
# overrule for a local test; these are where cloud credentials live, and an
# environment variable that could reach them would turn a development
# convenience into the exact request-forgery this module exists to stop.
# The one list lives in ``app.core.destination_policy``.
_METADATA_ADDRESSES = METADATA_ADDRESSES


class DestinationRefusedError(ValueError):
    """A destination this platform will not send to.

    Carries a machine-readable ``reason`` from the closed unusable-reason set,
    so the caller records a value rather than parsing a message. Three
    distinct causes reach here and they are NOT interchangeable: an
    unencrypted URL is a configuration mistake, a forbidden address is a
    request-forgery attempt or a misconfiguration, and a name that will not
    resolve is a partner that is simply not there.
    """

    def __init__(self, message: str, *, reason: str, host: str | None = None):
        self.host = host
        self.reason = reason
        super().__init__(message)


def origin_of(url: str) -> str:
    """Scheme, host and port — what a registration is pinned to.

    Port is included because ``https://partner.example`` and
    ``https://partner.example:8443`` are different services, and a partner
    that moves between them has moved.
    """
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        raise DestinationRefusedError(
            f"Not a usable URL: {url!r}", reason=UNUSABLE_CARD_MALFORMED
        )
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}"


def _is_forbidden_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Why this address is refused, or ``None`` if it is acceptable.

    Named classes first, ``is_global`` last. The named checks earn their place
    by producing a message that tells an operator which mistake they made, and
    the metadata address is checked first of all because it is the one that
    actually matters: it is globally routable in form, it is where cloud
    credentials live, and it is the single most valuable thing a
    request-forgery attempt can reach.

    ``is_global`` then backstops the list, because enumerating classes is a
    game you lose eventually. It already covers one range the named checks
    miss: RFC 6598 shared address space (``100.64.0.0/10``), for which
    ``is_private`` is False, so every check above passes it. That is the
    address space between a subscriber and their ISP, which means a deployment
    behind carrier-grade NAT could be steered at its provider's own
    infrastructure — the same class of target as the metadata endpoint, and
    reached the same way.

    The classification is shared (``app.core.destination_policy``); A2A applies
    its public-only policy.
    """
    return forbidden_address_reason(ip)


def _explicitly_trusted_origin(url: str) -> str | None:
    """The origin, when an operator has named this exact one as trusted.

    ``None`` means "decide normally" — an unlisted origin, an unparseable URL,
    or a metadata address, which no entry can exempt (see
    ``_METADATA_ADDRESSES``).

    Matching is on the ORIGIN, never the URL: the setting names a service, so
    listing ``http://localhost:8200`` reaches every path on that service and
    nothing on port 8201, on https, or on another host. And it is only ever a
    way past the transport and address checks — origin pinning still applies
    to a trusted partner exactly as to any other, so one that later moves is
    still refused.
    """
    try:
        origin = origin_of(url)
    except DestinationRefusedError:
        return None
    if origin not in security_option("a2a_client_dev_trusted_origins"):
        return None
    host = urlsplit(url).hostname or ""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        return origin
    return None if is_metadata_address(literal) else origin


def _decide_without_network(url: str) -> tuple[str | None, str, int]:
    """Every check that needs no network. Returns ``(origin, host, port)``.

    ``origin`` is set when these checks settle it (a trusted origin, a literal
    address that passed) and ``None`` when only resolution can. Shared by both
    entry points so they cannot disagree about what is refused.
    """
    if security_option("a2a_client_dev_trusted_origins"):
        trusted = _explicitly_trusted_origin(url)
        if trusted is not None:
            return trusted, "", 0

    parts = urlsplit(url)
    if parts.scheme != "https":
        # FR-012: refused rather than downgraded to. An operator who typed
        # http:// gets told, instead of silently sending a partner credential
        # over a connection nobody can vouch for.
        raise DestinationRefusedError(
            f"A2A partner URLs must use https, got {parts.scheme or 'no scheme'!r}.",
            reason=UNUSABLE_TRANSPORT_UNVERIFIED,
        )
    host = parts.hostname
    if not host:
        raise DestinationRefusedError(
            f"No host in URL: {url!r}", reason=UNUSABLE_CARD_MALFORMED
        )

    # A literal address needs no DNS and must be checked whatever ``resolve``
    # says — it is the case an attacker controls most directly.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        why = _is_forbidden_address(literal)
        if why:
            raise DestinationRefusedError(
                f"Refusing to reach {host} — {why}.",
                reason=UNUSABLE_DESTINATION_NOT_PERMITTED,
                host=host,
            )
        return origin_of(url), "", 0
    return None, host, parts.port or 443


def _unresolvable(host: str) -> DestinationRefusedError:
    # NOT ``destination_not_permitted``: a name that will not resolve is a
    # partner that is not there, which an operator fixes by correcting the
    # URL — not a destination policy refused.
    return DestinationRefusedError(
        f"Cannot resolve {host}.", reason=UNUSABLE_CARD_UNFETCHABLE, host=host
    )


def _refuse_if_resolves_inward(host: str, infos) -> None:
    for info in infos:
        addr = info[4][0]
        try:
            resolved = ipaddress.ip_address(addr)
        except ValueError:
            continue
        why = _is_forbidden_address(resolved)
        if why:
            # EVERY resolved address must be acceptable, not merely one: a
            # name resolving to both a public and a private address would
            # otherwise pass on the public one and connect to whichever the
            # OS picked.
            raise DestinationRefusedError(
                f"Refusing to reach {host} — it resolves to {addr}, a {why}.",
                reason=UNUSABLE_DESTINATION_NOT_PERMITTED,
                host=host,
            )


def assert_allowed(url: str, *, resolve: bool = True) -> str:
    """Refuse a destination this platform must not reach; return its origin.

    ``resolve=False`` skips DNS, for the callers that already resolved or that
    must not make a network call. It is NOT a way to skip the check: a literal
    address in the URL is still rejected, so the only thing it gives up is
    catching a hostname that resolves inward.

    The DNS-then-connect gap is real and not closed here: a name can resolve
    to a public address for this check and a private one for the request that
    follows. Closing it means pinning the resolved address into the connection
    itself, which is a transport-layer change out of proportion to a feature
    whose destinations are operator-supplied and few. Recorded rather than
    left for someone to discover.

    ``a2a_client_dev_trusted_origins`` exempts origins an operator has named
    one by one, and nothing else. It is empty by default, so the check below
    is skipped entirely on every deployment that has not opted in.

    Synchronous, for callers that are not on the event loop; a coroutine uses
    :func:`assert_allowed_async`, the same check.
    """
    origin, host, port = _decide_without_network(url)
    if origin is not None:
        return origin
    if resolve:
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise _unresolvable(host) from exc
        _refuse_if_resolves_inward(host, infos)
    return origin_of(url)


async def _resolve(host: str, port: int) -> list:
    """The loop's own resolver — one seam, so a test can stand in for DNS."""
    return await asyncio.get_running_loop().getaddrinfo(
        host, port, proto=socket.IPPROTO_TCP
    )


async def assert_allowed_async(url: str, *, resolve: bool = True) -> str:
    """:func:`assert_allowed`, for a caller on the event loop.

    Same checks, same reasons, same return; the lookup goes through the loop's
    resolver instead of blocking the thread, so a partner with slow DNS costs
    this call rather than every request the worker is serving.
    """
    origin, host, port = _decide_without_network(url)
    if origin is not None:
        return origin
    if resolve:
        try:
            infos = await _resolve(host, port)
        except socket.gaierror as exc:
            raise _unresolvable(host) from exc
        _refuse_if_resolves_inward(host, infos)
    return origin_of(url)


def assert_same_origin(url: str, pinned_origin: str) -> None:
    """Refuse a URL that leaves the origin the registration was verified on.

    Applied to the invocation endpoint a card declares AND to every redirect,
    because both are ways for a partner to move us somewhere we never verified
    (FR-034).
    """
    actual = origin_of(url)
    if actual != pinned_origin:
        raise DestinationRefusedError(
            f"Refusing to reach {actual}: this registration is pinned to "
            f"{pinned_origin}.",
            reason=UNUSABLE_ORIGIN_MISMATCH,
        )
