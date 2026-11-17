# SPDX-License-Identifier: Apache-2.0

"""Which network addresses a backend-initiated request may reach.

One classification, two policies:

* **public only** (``allow_private=False``) - loopback, private, link-local,
  reserved and every other non-globally-routable address is refused. The A2A
  destination guard (``app.a2a.client.destinations``) uses it.
* **operator network** (``allow_private=True``) - private ranges (and the
  RFC 6598 shared address space) are admitted, because an operator-configured
  endpoint such as an on-prem identity provider legitimately lives there;
  loopback, link-local, multicast, unspecified and reserved addresses stay
  refused. The OAuth token endpoint guard (``app.integrations.token_endpoint``)
  uses it under ``OAUTH_TOKEN_URL_STRICT``; by default that guard refuses only
  the metadata endpoints below.

Cloud metadata endpoints are refused by both policies and cannot be exempted:
they are where cloud credentials live, and two of them sit inside ranges the
operator-network policy otherwise admits.

Stdlib only, so the config layer and any guard can import it.
"""

from __future__ import annotations

import ipaddress
import socket

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

#: Cloud instance-metadata endpoints (string form of the parsed address).
METADATA_ADDRESSES: frozenset[str] = frozenset(
    {
        "169.254.169.254",  # AWS / GCP / Azure / OCI / OpenStack
        "169.254.170.2",  # AWS ECS task metadata
        "fd00:ec2::254",  # AWS IMDS over IPv6
        "fd20:ce::254",  # Google Cloud metadata server over IPv6
        "100.100.100.200",  # Alibaba Cloud
    }
)

#: RFC 6598 shared address space: not ``is_private`` in :mod:`ipaddress`, yet
#: used for internal overlays, so the operator-network policy admits it.
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")

#: RFC 6052 NAT64 well-known prefix: the last 32 bits are the IPv4 address.
_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")


def is_metadata_address(ip: IPAddress) -> bool:
    return any(str(a) in METADATA_ADDRESSES for a in (_unmapped(ip), *_tunnelled_ipv4(ip)))


def shorthand_ipv4_address(name: str) -> str | None:
    """The IPv4 address *name* spells without any lookup, or ``None`` for a name.

    The C resolver - and so any request - reads an IPv4 shorthand
    ``inet_aton`` accepts as the address it denotes (``0xa9fea9fe``,
    ``2852039166`` and ``169.254.43518`` are all 169.254.169.254). A guard
    judges such a host as that address directly, so it cannot slip through on
    a lookup that times out or is refused.
    """
    try:
        return str(ipaddress.IPv4Address(socket.inet_aton(name)))
    except (OSError, ValueError):
        return None


def _unmapped(ip: IPAddress) -> IPAddress:
    """An IPv6 address that carries an IPv4 one judged as that IPv4 address.

    IPv4-mapped (``::ffff:10.0.0.1``) and the NAT64 well-known prefix
    (``64:ff9b::/96``, RFC 6052): a DNS64 resolver answers every IPv4-only
    site that way on an IPv6-only network, so the public ones must pass and
    ``64:ff9b::a9fe:a9fe`` must be the metadata endpoint it reaches, not a
    "global" address (``is_global`` says so since Python 3.13).
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip in _NAT64_WELL_KNOWN:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def _tunnelled_ipv4(ip: IPAddress) -> tuple[ipaddress.IPv4Address, ...]:
    """The IPv4 addresses a 6to4 (``2002::/16``) or Teredo (``2001::/32``)
    address delivers to: the 6to4 relay or the Teredo server and client.

    Both prefixes are already refused by the public-only policy
    (``is_private``), but the operator-network policy admits private ranges,
    so ``2002:a9fe:a9fe::`` would pass it to the metadata endpoint through a
    6to4 interface. Every carried address must pass the same policy as well.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.sixtofour is not None:
            return (ip.sixtofour,)
        if ip.teredo is not None:
            return ip.teredo
    return ()


def forbidden_address_reason(
    ip: IPAddress, *, allow_private: bool = False, allow_loopback: bool = False
) -> str | None:
    """Why *ip* is refused, or ``None`` if the policy admits it.

    Named classes first, so the message tells an operator which mistake they
    made; ``is_global`` last as the backstop, because enumerating classes is a
    game you lose eventually. ``allow_loopback`` exists only for an origin an
    operator named for local development; it never admits link-local or a
    metadata address. A 6to4 or Teredo address is refused for any IPv4
    address it carries that the same policy refuses.
    """
    for carried in _tunnelled_ipv4(ip):
        reason = forbidden_address_reason(carried, allow_private=allow_private, allow_loopback=allow_loopback)
        if reason is not None:
            return reason
    ip = _unmapped(ip)
    if str(ip) in METADATA_ADDRESSES:
        return "cloud metadata endpoint"
    if ip.is_loopback:
        return None if allow_loopback else "loopback address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_private and not allow_private:
        return "private address"
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return "reserved address"
    if ip.is_global:
        return None
    if allow_private and (ip.is_private or ip in _SHARED_ADDRESS_SPACE):
        return None
    return "non-globally-routable address"
