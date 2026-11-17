# SPDX-License-Identifier: Apache-2.0

"""A reverse proxy whose client address the audit trail cannot read.

The audit trail records the connection's address unless the platform takes the
client address from the request: ``X-Forwarded-For`` from a peer
``FORWARDED_ALLOW_IPS`` names, which uvicorn copies into ``scope["client"]``
before the platform reads it, or from any peer under the ``trust_proxy_headers``
security option (``TRUST_PROXY_HEADERS``, or Settings > Access and security). No other
header is read. An audited request therefore records a proxy's address instead
of the user's when it carries ``X-Forwarded-For`` from a peer
``FORWARDED_ALLOW_IPS`` does not name - no proxy declared at all, or another
one - or names its client in ``X-Real-IP`` or ``Forwarded`` only: an undeclared
or misconfigured proxy, or a client that wrote the header itself.

That is reported, never acted on: one warning a day for the whole platform per
header and proxy configuration (:func:`report_undeclared_proxy`) and a
Redis marker naming the peer, which the security audit reads across replicas
(:func:`undeclared_proxy_seen`). Neither
changes what is recorded, and neither can fail or hold the request that
triggers it. :func:`forwarded_allow_ips` is the one read of
``FORWARDED_ALLOW_IPS`` and :func:`parse_forwarded_allow_ips` the one parsing
of it, uvicorn's own, shared with the startup report (``core/proxy_headers``)
and the security audit's proxy checks.
"""

from __future__ import annotations

import asyncio
import functools
import ipaddress
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Literal

from app.core.log_once import fingerprint, log_once_soon
from app.core.redis import redis_client
from app.core.security_options import option_reference, security_option
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

MARKER_KEY = "security_audit:undeclared_proxy_seen"
MARKER_TTL_SECONDS = 24 * 60 * 60
WARNING_INTERVAL_SECONDS = 24 * 60 * 60
_REPEAT_NOTE = "Said once a day for the whole platform; the security audit (Settings > Access and security) reports it too."

CLIENT_ADDRESSES_DOCS_URL = (
    "https://xtm-one-platform.github.io/xtm-one/latest/deployment/configuration/#client-addresses-in-the-audit-trail"
)

#: A process that keeps seeing the same sighting writes the marker again this
#: often, so a marker Redis lost (a restart, an eviction) is back within the
#: hour instead of the day its TTL allows.
_REWRITE_AFTER_SECONDS = 60 * 60

#: A sighting other than the one this process last wrote (another peer,
#: another header) is written no sooner than this after the previous write,
#: so the audit names the latest one without a write per request.
_CHANGE_AFTER_SECONDS = 60

#: A failed write is retried no sooner than this, so a Redis outage costs one
#: attempt per interval rather than one per audited request.
_RETRY_AFTER_FAILURE_SECONDS = 5 * 60

#: The client retries a timed-out command, so its own socket timeouts would let
#: a Redis that never answers hold the security audit far longer than this.
_REDIS_TIMEOUT_SECONDS = 2.0

#: What uvicorn trusts when ``FORWARDED_ALLOW_IPS`` is unset.
_UVICORN_DEFAULT_FORWARDED_ALLOW_IPS = "127.0.0.1"

FORWARDED_FOR = "x-forwarded-for"
REAL_IP = "x-real-ip"
FORWARDED = "forwarded"
#: The headers a sighting names, as an operator reads them.
HEADER_LABELS = {FORWARDED_FOR: "X-Forwarded-For", REAL_IP: "X-Real-IP", FORWARDED: "Forwarded"}
#: The same names as the ASGI scope spells them (lowercase bytes).
_RAW_HEADER_NAMES = frozenset(name.encode("latin-1") for name in HEADER_LABELS)

_lock = threading.Lock()
_next_write_at = 0.0
_next_change_at = 0.0
#: The sighting this process last wrote.
_written: Sighting | None = None
#: Kept referenced: the event loop holds only weak references to its tasks.
_write_tasks: set[asyncio.Task[None]] = set()


@dataclass(frozen=True)
class TrustedProxies:
    """``FORWARDED_ALLOW_IPS`` read as uvicorn reads it (``uvicorn.middleware.proxy_headers._TrustedHosts``).

    Only the whole value ``*`` trusts every peer; otherwise each comma-separated
    entry is an address, or a network when it contains ``/`` (host bits at
    zero), and anything else - a hostname (never resolved), a ``*`` inside a
    list, a CIDR with its host bits set - is a literal no TCP peer equals.
    """

    every_peer: bool
    addresses: frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    literals: tuple[str, ...]

    def trusts(self, host: str | None) -> bool:
        """Whether uvicorn takes the client address from ``X-Forwarded-For`` sent by *host*."""
        if self.every_peer:
            return True
        if not host:
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return host in self.literals
        return address in self.addresses or any(address in network for network in self.networks)

    @property
    def trusts_remote(self) -> bool:
        """Whether a peer beyond loopback is trusted: a proxy is declared."""
        return self.every_peer or any(not entry.is_loopback for entry in (*self.addresses, *self.networks))

    @property
    def trusts_every_peer(self) -> bool:
        """Whether forwarded headers are taken from any peer: ``*``, or a ``/0`` network."""
        return self.every_peer or any(network.prefixlen == 0 for network in self.networks)


@functools.lru_cache(maxsize=8)
def parse_forwarded_allow_ips(value: str) -> TrustedProxies:
    """Read a ``FORWARDED_ALLOW_IPS`` value (:class:`TrustedProxies`)."""
    if value == "*":
        return TrustedProxies(True, frozenset(), (), ())
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    literals: list[str] = []
    for entry in (item.strip() for item in value.split(",")):
        if not entry:
            continue
        try:
            if "/" in entry:
                networks.append(ipaddress.ip_network(entry))
            else:
                addresses.add(ipaddress.ip_address(entry))
        except ValueError:
            literals.append(entry)
    return TrustedProxies(False, frozenset(addresses), tuple(networks), tuple(literals))


def forwarded_allow_ips() -> str:
    """``FORWARDED_ALLOW_IPS`` as uvicorn reads it: the variable when set, even empty, else ``127.0.0.1``."""
    return os.environ.get("FORWARDED_ALLOW_IPS", _UVICORN_DEFAULT_FORWARDED_ALLOW_IPS)


def trusted_proxies() -> TrustedProxies:
    """What ``FORWARDED_ALLOW_IPS`` trusts now."""
    return parse_forwarded_allow_ips(forwarded_allow_ips())


def proxy_declared() -> bool:
    """Whether a proxy may speak for the client, so the audit trail sees through it.

    The ``trust_proxy_headers`` security option, or a ``FORWARDED_ALLOW_IPS``
    that trusts a peer beyond loopback. Loopback alone declares nothing: it is
    uvicorn's own default, which the Compose stack writes out.
    """
    return bool(security_option("trust_proxy_headers")) or trusted_proxies().trusts_remote


def believed_from_any_peer() -> bool:
    """Whether forwarded headers are believed from any peer that reaches the platform.

    The ``trust_proxy_headers`` security option, or a ``FORWARDED_ALLOW_IPS``
    that trusts every peer (``*``, a ``/0``). A client then picks the address
    recorded itself, which the security audit's any-peer warning says: the
    notice adds nothing.
    """
    return bool(security_option("trust_proxy_headers")) or trusted_proxies().trusts_every_peer


def _hop_address(hop: str) -> tuple[str, int]:
    """The client address uvicorn takes from one ``X-Forwarded-For`` hop (``_parse_host_port``)."""
    if hop.startswith("["):
        end = hop.find("]")
        if end == -1:
            return hop, 0
        host, remainder = hop[1:end], hop[end + 1 :]
        if not remainder:
            return host, 0
        if not remainder.startswith(":"):
            return hop, 0
        try:
            return host, int(remainder[1:])
        except ValueError:
            return host, 0
    if hop.count(":") == 1:
        host, port = hop.rsplit(":", 1)
        try:
            return host, int(port)
        except ValueError:
            return hop, 0
    return hop, 0


def _names_a_client(forwarded_for: str) -> bool:
    return any(hop.strip() for hop in forwarded_for.split(","))


def _uvicorn_read(host: object, port: object, forwarded_for: str) -> bool:
    """Whether uvicorn took ``request.client`` from *forwarded_for*, the header line it reads.

    uvicorn copies one of that line's hops, port included, into
    ``scope["client"]``; a connection it left alone keeps its own source port,
    which no proxy writes into a hop.
    """
    hops = (hop.strip() for hop in forwarded_for.split(","))
    return any(_hop_address(hop) == (host, port) for hop in hops if hop)


def _forwarded_names_client(value: str) -> bool:
    """Whether a ``Forwarded`` header (RFC 7239) carries a ``for`` parameter with a value."""
    for element in value.split(","):
        for pair in element.split(";"):
            key, separator, named = pair.partition("=")
            if separator and key.strip().lower() == "for" and named.strip().strip('"').strip():
                return True
    return False


def _address(host: object) -> str | None:
    """*host* as an IP address without an IPv6 zone, or ``None``: never another text."""
    try:
        return str(ipaddress.ip_address(str(host).partition("%")[0]))
    except ValueError:
        return None


@dataclass(frozen=True)
class Sighting:
    """An audited request that left its proxy's address in the audit trail."""

    #: The peer it came from, or ``None`` when that is not an address (a
    #: marker an earlier release wrote names none either).
    peer: str | None
    #: The header naming the client: ``x-forwarded-for`` from a peer
    #: ``FORWARDED_ALLOW_IPS`` does not name, else ``x-real-ip`` or
    #: ``forwarded``, which the platform never reads.
    header: str


def unread_client_address(request) -> Sighting | None:
    """The sighting an audited request makes, ``None`` when the audit trail has its client's address or none is named.

    ``X-Forwarded-For`` is decided on the line uvicorn reads, the last one (it
    builds a dict of the headers), and on whether it copied one of that line's
    hops into ``request.client``, read as uvicorn left it, never through
    ``request_client_ip()``: only that value, compared with the hops as
    written, says so. A peer ``FORWARDED_ALLOW_IPS`` names is not reported
    either way (declaring it again would change nothing). ``X-Real-IP`` and
    ``Forwarded`` are never read, so any line of theirs naming a client counts,
    from any peer.

    A request carrying none of the three headers - nearly every one - costs one
    pass over its header names and nothing else: the configuration is read only
    once a sighting would be made.
    """
    client = request.client
    if client is None:
        return None
    headers = request.headers
    if not any(name in _RAW_HEADER_NAMES for name, _value in headers.raw):
        return None
    sighting = _sighting(client, headers)
    if sighting is None or believed_from_any_peer():
        return None
    return sighting


def _sighting(client, headers) -> Sighting | None:
    host = getattr(client, "host", None)
    forwarded_for = headers.getlist("x-forwarded-for")
    if forwarded_for and _names_a_client(forwarded_for[-1]):
        if _uvicorn_read(host, getattr(client, "port", None), forwarded_for[-1]):
            return None
        if trusted_proxies().trusts(host):
            return None
        return Sighting(_address(host), FORWARDED_FOR)
    if any(value.strip() for value in headers.getlist("x-real-ip")):
        return Sighting(_address(host), REAL_IP)
    if any(_forwarded_names_client(value) for value in headers.getlist("forwarded")):
        return Sighting(_address(host), FORWARDED)
    return None


def report_undeclared_proxy(request) -> None:
    """Report an audited request whose client address the audit trail cannot read (:func:`unread_client_address`).

    Logs one warning a day for the whole platform per header and proxy
    configuration and schedules the marker; never raises, never waits.
    """
    try:
        sighting = unread_client_address(request)
        if sighting is None:
            return
        log_once_soon(
            logger,
            logging.WARNING,
            _notice_key(sighting),
            "%s",
            _warning_text(sighting),
            interval=WARNING_INTERVAL_SECONDS,
        )
        _schedule_marker(sighting)
    except Exception as exc:
        logger.debug("Could not check a request for an undeclared proxy: %s", format_exception_message(exc))


def _notice_key(sighting: Sighting) -> str:
    """The warning's key: its header and what it says to do about the proxy configuration, never the peer.

    The peer is left out so the pods of one ingress make one notice; a restart
    on another ``FORWARDED_ALLOW_IPS`` says what that one leaves at once.
    """
    peer_trusted = sighting.header != FORWARDED_FOR and trusted_proxies().trusts(sighting.peer)
    return f"undeclared_proxy:{sighting.header}:{fingerprint(forwarded_allow_ips(), peer_trusted)}"


def _warning_text(sighting: Sighting) -> str:
    relayed_by = f"the peer that relayed it, {sighting.peer}," if sighting.peer else "the peer that relayed it"
    declare = (
        "set FORWARDED_ALLOW_IPS to its address or CIDR (behind a Kubernetes ingress, the cluster's pod network) "
        "and restart"
    )
    if sighting.header == FORWARDED_FOR:
        if not proxy_declared():
            return (
                "A request carried X-Forwarded-For while no reverse proxy is declared - "
                f"{option_reference('trust_proxy_headers')} is off and FORWARDED_ALLOW_IPS names none beyond this "
                f"host - so the audit trail recorded the address of {relayed_by} instead of the user's. If that peer "
                f"is your reverse proxy or ingress, {declare}. {_REPEAT_NOTE}"
            )
        return (
            f"A request carried X-Forwarded-For from a peer FORWARDED_ALLOW_IPS ({forwarded_allow_ips()}) does not "
            f"name, so the audit trail recorded the address of {relayed_by} instead of the user's. If that peer is a "
            "reverse proxy or ingress of yours, add its address or CIDR to FORWARDED_ALLOW_IPS (behind a Kubernetes "
            f"ingress, the cluster's pod network) and restart. {_REPEAT_NOTE}"
        )
    text = (
        f"A request named its client in {HEADER_LABELS[sighting.header]} but not in X-Forwarded-For, the only "
        f"header the platform reads the client address from, so the audit trail recorded the address of "
        f"{relayed_by} instead of the user's. If that peer is your reverse proxy or ingress, make it send "
        "X-Forwarded-For (nginx: proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;)"
    )
    if not trusted_proxies().trusts(sighting.peer):
        text += f" and {declare}"
    return f"{text}. {_REPEAT_NOTE}"


def _marker_value(sighting: Sighting) -> str:
    return json.dumps({"header": sighting.header, "peer": sighting.peer}, separators=(",", ":"), sort_keys=True)


def _write_due(sighting: Sighting, now: float) -> bool:
    return now >= _next_write_at or (sighting != _written and now >= _next_change_at)


def _schedule_marker(sighting: Sighting) -> None:
    global _next_write_at, _next_change_at
    if _write_tasks or not _write_due(sighting, time.monotonic()):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    with _lock:
        now = time.monotonic()
        if _write_tasks or not _write_due(sighting, now):
            return
        _next_write_at = _next_change_at = now + _RETRY_AFTER_FAILURE_SECONDS
        task = loop.create_task(_write_marker(sighting))
        _write_tasks.add(task)
    task.add_done_callback(_write_tasks.discard)


async def _set_marker(value: str) -> None:
    redis = await redis_client()
    await redis.set(MARKER_KEY, value, ex=MARKER_TTL_SECONDS)


async def _write_marker(sighting: Sighting) -> None:
    global _next_write_at, _next_change_at, _written
    try:
        await asyncio.wait_for(_set_marker(_marker_value(sighting)), _REDIS_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.debug("Could not record the undeclared proxy marker: %s", format_exception_message(exc))
        return
    now = time.monotonic()
    _written = sighting
    _next_write_at = now + _REWRITE_AFTER_SECONDS
    _next_change_at = now + _CHANGE_AFTER_SECONDS


def _parse_marker(raw: object) -> Sighting:
    """What a marker says: its peer and header, or an ``X-Forwarded-For`` sighting of a peer it does not name.

    ``1``, the value a release that named no peer wrote, reads that way, and
    so does any value that does not parse: anyone who can write to Redis can
    write this key, so nothing of it reaches a finding but a validated address
    and a known header.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        record = json.loads(raw) if isinstance(raw, str) else None
    except (ValueError, RecursionError):
        record = None
    if not isinstance(record, dict) or record.get("header") not in HEADER_LABELS:
        return Sighting(None, FORWARDED_FOR)
    peer = record.get("peer")
    return Sighting(_address(peer) if isinstance(peer, str) and "%" not in peer else None, record["header"])


async def _read_marker() -> object:
    redis = await redis_client()
    return await redis.get(MARKER_KEY)


async def undeclared_proxy_seen() -> Sighting | Literal[False] | None:
    """The sighting any replica recorded in the last day: ``False`` when none, ``None`` when Redis cannot say."""
    try:
        raw = await asyncio.wait_for(_read_marker(), _REDIS_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.debug("Could not read the undeclared proxy marker: %s", format_exception_message(exc))
        return None
    if raw is None:
        return False
    return _parse_marker(raw)


def sighting_stands(sighting: Sighting) -> bool:
    """Whether the configuration still leaves the sighting's proxy address in the audit trail.

    An ``X-Forwarded-For`` sighting ends once ``FORWARDED_ALLOW_IPS`` names
    its peer - for a marker naming none, written by a release that reported
    only while no proxy was declared, once a proxy is. A header the platform
    never reads stands until the marker expires.
    """
    if sighting.header != FORWARDED_FOR:
        return True
    if sighting.peer is None:
        return not proxy_declared()
    return not trusted_proxies().trusts(sighting.peer)
