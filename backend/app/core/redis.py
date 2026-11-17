# SPDX-License-Identifier: Apache-2.0

"""Redis connection layer with HA support.

Two modes are supported via ``settings.redis_mode``:

- ``standalone`` (default) — uses ``REDIS_URL``. A singleton ``Redis`` client
  backed by a shared ``ConnectionPool`` is created at import time.
- ``sentinel`` — discovers the current master through Redis Sentinel.  The
  singleton is a ``master_for(...)`` client that re-resolves the master on
  every command, transparently following failovers.

Pool is sized via environment variables (see ``core/config.py``).

A **singleton** client is created once at import time and reused by every
caller via ``redis_client()``.  This is safe because ``redis.asyncio`` borrows
a connection from the pool for each command and returns it immediately after
the response is read, so a single client handles unlimited concurrency without
holding connections open.

For **pub/sub** (which holds a dedicated connection for the lifetime of the
subscription), use ``redis_pubsub_client()`` — it returns a *new* lightweight
client that the caller **must** close with ``close_pubsub()`` when done.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import redis.asyncio as redis_asyncio
import redis.asyncio.sentinel as redis_sentinel

from app.core.cancellation import run_to_completion
from app.core.config import settings

logger = logging.getLogger(__name__)

# Backwards-compatible aliases used by tests and type annotations.
Redis = redis_asyncio.Redis
ConnectionPool = redis_asyncio.ConnectionPool
Sentinel = redis_sentinel.Sentinel


def _parse_hosts(raw: str, default_port: int) -> list[tuple[str, int]]:
    """Parse ``"h1:p1,h2:p2,h3"`` → list of (host, port) tuples."""
    out: list[tuple[str, int]] = []
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        if ":" in token:
            host, port = token.rsplit(":", 1)
            try:
                out.append((host, int(port)))
            except ValueError as exc:
                raise ValueError(f"Invalid port in Redis host {token!r}") from exc
        else:
            out.append((token, default_port))
    return out


def _common_kwargs() -> dict[str, Any]:
    """Connection kwargs shared by every mode."""
    return {
        "decode_responses": True,
        "max_connections": settings.redis_max_connections,
        "socket_connect_timeout": settings.redis_socket_connect_timeout,
        "socket_timeout": settings.redis_socket_timeout,
        "health_check_interval": settings.redis_health_check_interval,
        "retry_on_timeout": settings.redis_retry_on_timeout,
    }


def _ssl_kwargs() -> dict[str, Any]:
    """TLS kwargs applied to sentinel mode.

    Standalone mode uses the ``rediss://`` scheme in ``REDIS_URL`` instead.
    """
    if not settings.redis_ssl:
        return {}
    kwargs: dict[str, Any] = {
        "ssl": True,
        "ssl_cert_reqs": settings.redis_ssl_cert_reqs,
    }
    if settings.redis_ssl_ca_certs:
        kwargs["ssl_ca_certs"] = settings.redis_ssl_ca_certs
    return kwargs


def _data_plane_auth() -> dict[str, Any]:
    """Username/password/db for master & replica connections."""
    kwargs: dict[str, Any] = {}
    if settings.redis_username:
        kwargs["username"] = settings.redis_username
    if settings.redis_password:
        kwargs["password"] = settings.redis_password
    if settings.redis_db:
        kwargs["db"] = settings.redis_db
    return kwargs


# Module-level state populated by ``_build_client()``.
_pool: redis_asyncio.ConnectionPool | None = None
_sentinel: redis_sentinel.Sentinel | None = None
_shared_client: redis_asyncio.Redis


def _build_standalone() -> tuple[redis_asyncio.Redis, redis_asyncio.ConnectionPool]:
    pool = redis_asyncio.ConnectionPool.from_url(
        settings.redis_url,
        **_common_kwargs(),
    )
    return redis_asyncio.Redis(connection_pool=pool), pool


def _build_sentinel() -> tuple[redis_asyncio.Redis, redis_sentinel.Sentinel]:
    hosts = _parse_hosts(settings.redis_sentinel_hosts, default_port=26379)
    if not hosts:
        raise RuntimeError(
            "REDIS_MODE=sentinel but REDIS_SENTINEL_HOSTS is empty"
        )
    sentinel_kwargs: dict[str, Any] = {
        "socket_connect_timeout": settings.redis_socket_connect_timeout,
        "socket_timeout": settings.redis_socket_timeout,
        **_ssl_kwargs(),
    }
    if settings.redis_sentinel_password:
        sentinel_kwargs["password"] = settings.redis_sentinel_password
    # Data-plane connection options applied to master/replica clients.
    connection_kwargs: dict[str, Any] = {
        **_common_kwargs(),
        **_ssl_kwargs(),
        **_data_plane_auth(),
    }
    sentinel = redis_sentinel.Sentinel(
        hosts,
        sentinel_kwargs=sentinel_kwargs,
        **connection_kwargs,
    )
    client = sentinel.master_for(
        settings.redis_sentinel_service,
        redis_class=redis_asyncio.Redis,
    )
    logger.info(
        "Redis: sentinel mode (service=%s, hosts=%d)",
        settings.redis_sentinel_service,
        len(hosts),
    )
    return client, sentinel


def _build_client() -> None:
    """Initialize the module-level singleton client based on ``redis_mode``."""
    global _shared_client, _pool, _sentinel
    mode = settings.redis_mode
    if mode == "sentinel":
        _shared_client, _sentinel = _build_sentinel()
    else:
        _shared_client, _pool = _build_standalone()


_build_client()


async def redis_client() -> redis_asyncio.Redis:
    """Return the shared async Redis client.

    The returned client must **not** be closed by the caller — it is shared
    across the entire process.  In sentinel mode the underlying connection is
    re-resolved per command, so failovers are transparent.
    """
    return _shared_client


async def redis_pubsub_client() -> redis_asyncio.Redis:
    """Return a **new** Redis client intended for pub/sub subscriptions.

    Pub/sub holds a dedicated connection for the lifetime of the subscription,
    so each subscriber needs its own client.  The caller **must** close it with
    :func:`close_pubsub` when the subscription ends.
    """
    mode = settings.redis_mode
    if mode == "sentinel":
        if _sentinel is None:
            raise RuntimeError("Redis sentinel client is not initialized.")
        # ``master_for`` returns a fresh wrapper; pub/sub will pin to the
        # current master at subscribe time.
        return _sentinel.master_for(
            settings.redis_sentinel_service,
            redis_class=redis_asyncio.Redis,
        )
    if _pool is None:
        raise RuntimeError("Redis connection pool is not initialized.")
    return redis_asyncio.Redis(connection_pool=_pool)


#: How long closing a subscription may take in all: a Redis that stopped
#: answering must not hold a teardown that nothing can cancel. One deadline,
#: taken once, bounds every network step of the close.
PUBSUB_CLOSE_SECONDS = 2.0

#: The part of it the subscription's socket may take to close; what is left
#: is for closing the client. Handing the connection back to the pool is local
#: bookkeeping and is never cut: a skipped hand-back leaks a pool slot.
PUBSUB_SOCKET_CLOSE_SECONDS = 1.0


async def open_pubsub(
    client: redis_asyncio.Redis,
    *channels: str,
    patterns: tuple[str, ...] = (),
) -> redis_asyncio.client.PubSub:
    """A subscription of *client* to *channels* and *patterns*: THE way one starts.

    ``subscribe()`` checks a connection out of the pool before it can fail: one
    that fails or is cancelled closes the subscription and *client* through
    :func:`close_pubsub`, so the caller holds neither. Otherwise the caller
    closes both with :func:`close_pubsub` when the subscription ends.
    """
    pubsub = client.pubsub()
    try:
        if channels:
            await pubsub.subscribe(*channels)
        if patterns:
            await pubsub.psubscribe(*patterns)
    except BaseException:
        await close_pubsub(pubsub, client)
        raise
    return pubsub


async def close_pubsub(
    pubsub: redis_asyncio.client.PubSub | None,
    client: redis_asyncio.Redis | None = None,
) -> None:
    """Close *pubsub*, then its *client*: THE way a subscription ends.

    Holders call this, never ``pubsub.aclose()``: a cancelled holder's own close
    stops at its first await and keeps the pooled connection checked out. This
    one runs to completion, then lets the cancellation through
    (:func:`run_to_completion`), within :data:`PUBSUB_CLOSE_SECONDS`.
    """

    async def _close() -> None:
        # Both deadlines come from one reading of the clock and are applied one
        # after the other, never nested: two nested timers that expire in the
        # same loop iteration (a stalled worker) cancel the task twice, the
        # inner one no longer turns it into a TimeoutError, and the hand-back
        # to the pool is skipped.
        now = asyncio.get_running_loop().time()
        deadline = now + PUBSUB_CLOSE_SECONDS
        if pubsub is not None:
            await _end_subscription(pubsub, min(now + PUBSUB_SOCKET_CLOSE_SECONDS, deadline))
        if client is None:
            return
        try:
            async with asyncio.timeout_at(deadline):
                with contextlib.suppress(Exception):
                    await client.aclose()
        except TimeoutError:
            logger.warning("Closing a Redis pub/sub subscription took over %ss: left unfinished", PUBSUB_CLOSE_SECONDS)

    await run_to_completion(_close())


async def _end_subscription(pubsub: redis_asyncio.client.PubSub, socket_deadline: float) -> None:
    """What ``PubSub.aclose()`` does, with its socket close bounded by *socket_deadline*.

    The connection goes back to the pool once, after its socket is closed or
    dropped, and outside any deadline: a close cut short inside
    ``ConnectionPool.release()`` could hand back a connection already handed
    back, or lose its slot.
    """
    connection = getattr(pubsub, "connection", None)
    pubsub.connection = None
    pubsub.channels = {}
    pubsub.pending_unsubscribe_channels = set()
    pubsub.patterns = {}
    pubsub.pending_unsubscribe_patterns = set()
    if connection is None:
        return
    # The next borrower reconnects this connection: its callback would
    # otherwise subscribe this pubsub again on a connection it never gives back.
    with contextlib.suppress(Exception):
        connection.deregister_connect_callback(pubsub.on_connect)
    try:
        async with asyncio.timeout_at(socket_deadline):
            await connection.disconnect()
    except Exception as exc:
        logger.warning(
            "Closing a Redis pub/sub socket did not finish (%s): dropping it",
            type(exc).__name__,
        )
        with contextlib.suppress(Exception):
            await connection.disconnect(nowait=True)
    with contextlib.suppress(Exception):
        await pubsub.connection_pool.release(connection)


def sentinel_client() -> redis_sentinel.Sentinel:
    """Return the module-level Sentinel client (sentinel mode only).

    Public accessor for the ``Sentinel`` instance built by
    :func:`_build_sentinel` at import time. Use this from other
    modules instead of importing the underscored ``_sentinel`` global
    directly so the connection-layer's internal state stays
    encapsulated.

    Raises ``RuntimeError`` if Redis is not running in sentinel mode
    (the caller should branch on ``settings.redis_mode`` before
    calling this).
    """
    if _sentinel is None:
        raise RuntimeError(
            "Redis sentinel client is not initialized "
            "(REDIS_MODE is not 'sentinel')."
        )
    return _sentinel


# Read-then-decrement as ONE Lua script so a check-then-act race cannot drive
# a counter negative: concurrent decrements can never both observe a positive
# value and both ``DECR`` past zero. ``DECR`` leaves the key's TTL untouched, so
# the counter still expires on its own schedule.
DECR_IF_POSITIVE_LUA = (
    "local current = tonumber(redis.call('GET', KEYS[1]) or '0')\n"
    "if current > 0 then\n"
    "    return redis.call('DECR', KEYS[1])\n"
    "end\n"
    "return current"
)


async def decr_if_positive(client: redis_asyncio.Redis, key: str) -> None:
    """Atomically decrement *key*, never dropping below zero.

    The single source of truth for reversing a Redis-buffered counter (quota
    refunds, agentic-execution refunds): a refund must never underflow, which
    would grant free usage or a negative metric. See
    :data:`DECR_IF_POSITIVE_LUA` for the atomicity argument.
    """
    await client.eval(DECR_IF_POSITIVE_LUA, 1, key)


async def close_pool() -> None:
    """Gracefully release Redis resources.

    Call once during application shutdown.
    """
    try:
        await _shared_client.aclose()
    except Exception:
        logger.debug("Error closing shared Redis client", exc_info=True)
    if _pool is not None:
        try:
            await _pool.disconnect()
        except Exception:
            logger.debug("Error disconnecting Redis pool", exc_info=True)
