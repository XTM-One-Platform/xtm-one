# SPDX-License-Identifier: Apache-2.0

"""Lightweight fixed-window rate limiting backed by Redis.

Used to throttle unauthenticated/sensitive endpoints (login, password reset,
invitation registration) per client IP (a native IPv6 client per address,
or per network under the ``rate_limit_ipv6_prefix`` security option, see
``client_bucket``), and
authenticated/targeted surfaces (chat message sends per user, webhook
receivers per assignment) via an explicit ``bucket`` key.

When Redis is unavailable the behaviour is governed by the
``rate_limit_fail_mode`` security option (Settings > Access and security, or
``RATE_LIMIT_FAIL_MODE``, which wins): ``"open"`` (default) skips the check
so a Redis hiccup never locks everyone out of authentication; ``"closed"``
raises 503 so limits cannot be bypassed by degrading the backing store.
"""

from __future__ import annotations

import ipaddress
import logging

from fastapi import HTTPException, Request, status

from app.core.redis import redis_client
from app.core.security_options import option_reference, security_option, where_to_set
from app.core.utils import canonical_ip, request_client_ip

logger = logging.getLogger(__name__)


def _client_ip(request: Request) -> str:
    """Resolve the per-client rate-limit bucket key.

    The address comes from ``app.core.utils.request_client_ip``, the one
    client-address rule the audit trail and the webhook diagnostics share.
    ``X-Forwarded-For`` is trusted ONLY when the
    ``trust_proxy_headers`` security option is explicitly on.  By default the backend is assumed
    to be addressable directly by clients (or behind a proxy that does
    not strip the header), so honoring the header unconditionally would
    let any caller spoof it to land in a fresh bucket on every request
    and trivially bypass per-IP throttling on ``/auth/login``,
    ``/auth/forgot-password``, etc.  The flag suits only a proxy that
    overwrites the header with the real client IP: behind one that
    appends to it (nginx's ``$proxy_add_x_forwarded_for``) the leftmost
    hop is still the caller's own, and ``FORWARDED_ALLOW_IPS``, which
    uvicorn applies from the end of the chain, is the setting to use.
    ``"unknown"`` when there is no address to report (no peer, or a peer
    that is not an IP address, as ``FORWARDED_ALLOW_IPS="*"`` can leave):
    one bucket those requests share, and no request with an address joins
    it. A forwarded hop that is not an address is keyed on the peer.
    """
    return client_bucket(request_client_ip(request))


def client_bucket(address: str | None) -> str:
    """The rate-limit bucket of a client address, as ``request_client_ip`` returns it.

    The one place a client address becomes a rate-limit key; every per-IP
    limiter goes through it (``_client_ip``). By default the key is the
    address exactly as ``request_client_ip`` returned it, the key earlier
    releases used. The ``rate_limit_ipv6_prefix`` security option
    (``RATE_LIMIT_IPV6_PREFIX``, or Settings > Access and security) keys a native IPv6
    address on its network instead - ``64`` gives ``2001:db8:1:2::/64`` -
    since a host or a site is handed a whole /64 and one client could
    otherwise draw a fresh bucket for every request from an address it owns.
    It is opt-in because every device of one /64 (a corporate IPv6 LAN) then
    shares one budget. Under a prefix, the address is read through
    ``canonical_ip`` first (port, brackets and zone index dropped: every
    link-local neighbour shares ``fe80::/64``), IPv4 - IPv4-mapped included -
    and loopback ``::1`` keep their own key, and the known limits of a network
    key apply: a site delegated a /48 or a /56 still holds many /64s, and
    every Teredo client of one Teredo server shares one.
    No address is the shared ``"unknown"`` bucket. Only the key changes: the
    audit trail, the access log and the webhook diagnostics record the
    address as ``request_client_ip`` returned it.
    """
    if not address:
        return "unknown"
    prefix = security_option("rate_limit_ipv6_prefix")
    if prefix == 128:
        return address
    canonical = canonical_ip(address)
    if canonical is None:
        # The ``unknown`` a proxy writes for a client it hides.
        return address
    ip = ipaddress.ip_address(canonical)
    if ip.version == 4 or ip.is_loopback:
        return address
    return str(ipaddress.IPv6Network((ip, prefix), strict=False))


_configuration_reported = False


def log_rate_limit_configuration() -> None:
    """Say once per process how the per-IP limits key a native IPv6 client.

    Called once the stored security options are read, so a prefix set in
    Settings > Access and security is the one reported (``main._do_all_startup_work``).
    """
    global _configuration_reported
    if _configuration_reported:
        return
    _configuration_reported = True
    prefix = security_option("rate_limit_ipv6_prefix")
    option = option_reference("rate_limit_ipv6_prefix")
    if prefix == 128:
        logger.info(
            "Per-IP rate limits key a native IPv6 client on its address (%s is 128): a client "
            "holding a /64 can draw a fresh bucket from each of its addresses. Where IPv6 clients "
            "do not share a /64 at scale, %s.",
            option,
            where_to_set("rate_limit_ipv6_prefix", "64"),
        )
    else:
        logger.info(
            "Per-IP rate limits key a native IPv6 client on its /%d network (%s is %d): every "
            "address of one /%d shares one budget. IPv4 clients are keyed per address.",
            prefix,
            option,
            prefix,
            prefix,
        )


# Lua script: atomically increment a windowed counter key and (re)apply the
# window TTL when one is missing.  Naively running ``INCR`` followed by an
# ``EXPIRE`` would leak a TTL-less key if the second round-trip failed
# (network blip, process crash, Redis OOM) — that bucket would then sit at
# its incremented value forever, permanently throttling the client and
# growing the Redis key-space unbounded.  The script collapses both ops into
# a single round-trip and also re-applies a TTL on the rare case where the
# key is found WITHOUT one (e.g. RDB restore that dropped TTLs, manual ops
# action) so the bucket can never end up un-expirable.
#
# Public: shared by every fixed-window counter in the codebase (rate-limit
# buckets here, direct-webhook miss counters in ``api/v1/webhooks.py``) so
# the atomicity guarantees stay in one place.
INCR_EXPIRE_LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
else
    local ttl = redis.call('TTL', KEYS[1])
    if ttl < 0 then
        redis.call('EXPIRE', KEYS[1], ARGV[1])
    end
end
return current
"""

#: ``INCR_EXPIRE_LUA`` charging ``ARGV[2]`` units at once: a budget of bytes rather than of requests.
INCRBY_EXPIRE_LUA = """
local current = redis.call('INCRBY', KEYS[1], ARGV[2])
if current == tonumber(ARGV[2]) then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
else
    local ttl = redis.call('TTL', KEYS[1])
    if ttl < 0 then
        redis.call('EXPIRE', KEYS[1], ARGV[1])
    end
end
return current
"""


def _rate_limit_fail_closed() -> bool:
    """True when the rate-limit fail mode is ``closed`` (reject on Redis failure)."""
    return security_option("rate_limit_fail_mode") == "closed"


async def enforce_rate_limit(
    request: Request | None,
    name: str,
    *,
    limit: int,
    window_seconds: int,
    bucket: str | None = None,
    fail_closed: bool | None = None,
    quiet: bool = False,
    cost: int = 1,
) -> None:
    """Allow up to ``limit`` requests per ``window_seconds`` per bucket.

    *cost* charges a request more than one unit: ``limit`` is then a budget
    of those units - the bytes a public video route answers - and the
    request that would cross it is refused, never served in part.

    The bucket defaults to the client IP resolved from ``request``; pass an
    explicit ``bucket`` (e.g. user ID, assignment ID) for authenticated or
    targeted surfaces where the IP is not the right throttling key —
    ``request`` may then be ``None`` (the web chat's turn runner has no Request).

    Raises HTTP 429 when the limit is exceeded.  On a Redis failure the
    behaviour follows the ``rate_limit_fail_mode`` security option: ``"open"``
    skips the check, ``"closed"`` raises HTTP 503. Pass ``fail_closed=True`` to
    demand the closed behaviour regardless of that setting. ``quiet`` logs
    that failure at debug level: for a route browsers call on their own, where
    a line with a traceback per request would flood the log during an outage
    the route reports once by other means.

    The 429 carries a ``Retry-After`` header holding the seconds left in the
    current fixed window (read from the bucket key's TTL, falling back to
    ``window_seconds`` when Redis cannot answer).  Machine senders — webhook
    producers in particular — back off on ``Retry-After`` and otherwise
    hot-retry into the same closed window; a bare 429 turns an alert storm
    into a retry storm.
    """
    if bucket is None:
        if request is None:
            raise ValueError("enforce_rate_limit requires a request or a bucket")
        bucket = _client_ip(request)
    key = f"rl:{name}:{bucket}"
    try:
        r = await redis_client()
        # ``eval`` returns int via redis-py async; the Lua script guarantees
        # the increment-and-expire pair is atomic on the server side, so the
        # caller cannot observe a TTL-less bucket even if Redis returns an
        # error to the *next* request right after this one.
        if cost == 1:
            count = int(await r.eval(INCR_EXPIRE_LUA, 1, key, window_seconds))
        else:
            count = int(await r.eval(INCRBY_EXPIRE_LUA, 1, key, window_seconds, max(0, int(cost))))
    except Exception:
        # ``fail_closed=True`` overrides the platform default for surfaces
        # where the limiter is the only control on load, so a Redis outage
        # must refuse rather than admit.
        if _rate_limit_fail_closed() if fail_closed is None else fail_closed:
            (logger.debug if quiet else logger.error)(
                "Rate-limit check failed for %s; failing closed "
                "(RATE_LIMIT_FAIL_MODE=closed)",
                name,
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Rate limiting is temporarily unavailable and the platform "
                    "is configured to fail closed. Please try again shortly."
                ),
            ) from None
        (logger.debug if quiet else logger.warning)(
            "Rate-limit check failed for %s; failing open", name, exc_info=True
        )
        return
    if count > limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please slow down and try again shortly.",
            headers={
                "Retry-After": str(await _window_retry_after(r, key, window_seconds))
            },
        )


async def refund_rate_limit(
    request: Request | None,
    name: str,
    *,
    bucket: str | None = None,
) -> None:
    """Return one previously counted request to its fixed-window bucket.

    For surfaces where one limiter must end up charging only the requests
    that fail a LATER check — the concrete case being a pre-authentication
    limiter that has to run before the credential is looked at, but whose
    budget must not be consumed by requests that then authenticate. The
    enforce/refund pair keeps the check cheap and early while making the
    charge land where the docstring of the enforcing site says it does.

    Best-effort by design: a refund lost to a Redis blip merely under-counts
    the bucket's remaining budget for the rest of one window, so any failure
    is swallowed. The decrement only touches an existing positive counter —
    a plain ``DECR`` on a missing key would mint a TTL-less negative bucket.
    """
    if bucket is None:
        if request is None:
            raise ValueError("refund_rate_limit requires a request or a bucket")
        bucket = _client_ip(request)
    key = f"rl:{name}:{bucket}"
    try:
        r = await redis_client()
        await r.eval(DECR_IF_POSITIVE_LUA, 1, key)
    except Exception:
        logger.debug("Rate-limit refund failed for %s", name, exc_info=True)


# Lua: decrement only an existing, positive counter. Guards both failure
# modes of a naive DECR — creating a missing key at -1 with no TTL, and
# driving an existing bucket below zero when refunds race the window reset.
DECR_IF_POSITIVE_LUA = """
local current = redis.call('GET', KEYS[1])
if current and tonumber(current) > 0 then
    return redis.call('DECR', KEYS[1])
end
return 0
"""


async def _window_retry_after(r, key: str, window_seconds: int) -> int:
    """Seconds until the fixed window behind ``key`` resets.

    Best-effort and only reached on the throttled path (one extra round-trip
    per rejected request, never on the allowed path).  Takes the client the
    caller already checked out rather than acquiring another one.  Any Redis
    problem — or a key that lost its TTL between the ``INCR`` and this read —
    degrades to the full window, which is a safe over-estimate: a sender
    waiting slightly too long is strictly better than one retrying into a
    window that is still shut.
    """
    try:
        ttl = int(await r.ttl(key))
    except Exception:
        logger.debug("Retry-After TTL lookup failed for %s", key, exc_info=True)
        return max(1, window_seconds)
    if ttl <= 0:
        return max(1, window_seconds)
    return min(ttl, max(1, window_seconds))
