# SPDX-License-Identifier: Apache-2.0

"""Shared SAQ queue helpers for web and worker processes."""

from __future__ import annotations

import logging
import socket
from contextlib import suppress
from typing import Any

from saq import Queue

from app.core.config import settings

logger = logging.getLogger(__name__)

_queue: Queue | None = None

# Lazily built teardown-safe RedisQueue subclass (see _teardown_safe_queue_cls).
_safe_queue_cls: type | None = None

# Error messages that identify the known executor/loop teardown race.
# redis-py wraps the executor's RuntimeError via ``raise
# ConnectionError(exc) from exc`` so ``str(exc)`` carries the inner message.
_TEARDOWN_RACE_SIGNATURES = (
    "cannot schedule new futures after shutdown",
    "Event loop is closed",
)


def _teardown_safe_queue_cls() -> type:
    """Return the teardown-safe ``RedisQueue`` subclass (built lazily).

    Why this exists (the fix that finally sticks)
    -----------------------------------------------------------
    SAQ's CLI runner (``saq.worker.start`` → ``worker_start``) wraps
    ``worker.start()`` in ``try/finally: await worker.queue.disconnect()``.
    That library-level ``finally`` runs AFTER our app shutdown hook — i.e.
    after ``close_queue()`` already disconnected the queue AND after
    ``shutdown_default_executor()`` killed the event loop's default
    ``ThreadPoolExecutor``.

    On that *second* ``disconnect()``, SAQ calls ``pubsub.punsubscribe()``
    and redis-py's ``PubSub.execute_command`` unconditionally RECONNECTS
    (``PubSub.connect`` → ``Connection.connect_check_health`` → ``_connect``
    → DNS via ``loop.run_in_executor(None, ...)``).  With the default
    executor already shut down, Python raises ``RuntimeError: cannot
    schedule new futures after shutdown``, which redis-py's blanket
    ``except Exception`` wraps as ``redis.exceptions.ConnectionError`` —
    the uncaught stderr traceback seen on every rolling restart.

    The hook orders its own teardown (queue close before
    executor shutdown) but cannot touch SAQ's own unconditional second
    disconnect, which is unreachable from app code.  The only app-level
    fix is to make ``disconnect()`` idempotent per connect cycle so the
    second call is a no-op, plus a tightly scoped belt so a benign
    teardown race can never surface as an unhandled traceback.
    """
    global _safe_queue_cls
    if _safe_queue_cls is None:
        from redis.exceptions import ConnectionError as RedisConnectionError
        from saq.queue.redis import RedisQueue

        class _TeardownSafeRedisQueue(RedisQueue):
            """RedisQueue with an idempotent, teardown-safe ``disconnect``."""

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self._app_disconnected = False

            async def connect(self) -> None:
                # A legitimate reconnect (e.g. SAQ's health-check path does
                # connect → info → disconnect) re-arms the disconnect.
                self._app_disconnected = False
                await super().connect()

            async def disconnect(self) -> None:
                if self._app_disconnected:
                    # Second disconnect in the same connect cycle — SAQ's CLI
                    # runner always calls queue.disconnect() in its finally
                    # block even though the app shutdown hook already closed
                    # the queue.  Doing nothing here is what prevents the
                    # pubsub reconnect-after-executor-shutdown.
                    return
                try:
                    await super().disconnect()
                except (RuntimeError, RedisConnectionError) as exc:
                    # Belt for the same teardown race: if the loop's default
                    # executor (or the loop itself) is already shutting down,
                    # redis-py's close path may still attempt a reconnect and
                    # fail.  The process is exiting and the connection is
                    # gone either way — never let this surface as an
                    # unhandled stderr traceback.  Known teardown-race
                    # signatures stay at debug; anything else is logged at
                    # warning so genuine connection problems remain visible
                    # in default log levels (still swallowed: raising during
                    # a disconnect would only recreate the unhandled
                    # traceback this class exists to prevent).
                    if any(sig in str(exc) for sig in _TEARDOWN_RACE_SIGNATURES):
                        logger.debug(
                            "Suppressed benign Redis teardown error during "
                            "SAQ queue disconnect",
                            exc_info=True,
                        )
                    else:
                        logger.warning(
                            "Suppressed unexpected error during SAQ queue "
                            "disconnect (connection is torn down regardless)",
                            exc_info=True,
                        )
                # Armed only once the disconnect attempt ran to completion —
                # either cleanly or via a suppressed teardown-race error (the
                # connection is gone either way).  An unexpected propagating
                # exception must NOT arm the guard: the underlying disconnect
                # never completed, so a later call (SAQ's CLI finally, a
                # caller retry) must be allowed to attempt the real cleanup
                # again — the suppression belt above still shields that retry
                # from the reconnect race.
                self._app_disconnected = True

        _safe_queue_cls = _TeardownSafeRedisQueue
    return _safe_queue_cls


def _tcp_keepalive_options() -> dict[int, int]:
    """Platform-dependent TCP keepalive options for the SAQ Redis client.

    The SAQ worker parks its dequeue loop on long-blocking ``BLMOVE``
    reads.  redis-py never applies ``socket_timeout`` semantics we could
    safely use there (a socket timeout would also fire on healthy idle
    blocking reads and on the pub/sub listener), so dead-peer detection
    must happen at the TCP layer: without keepalive, a connection that
    dies *while the worker is blocked reading* (network blip, NAT/LB
    idle reset, Docker restart, host sleep) never raises — the worker
    waits forever with zero errors while documents pile up in
    ``pending`` (issue: full embedding stall after >24h uptime).

    Values: start probing after 60s idle, probe every 10s, give up
    after 3 failed probes — a dead peer is detected in ~90s.  Constants
    are resolved via ``getattr`` because availability varies by
    platform (``TCP_KEEPIDLE`` on Linux/Windows, ``TCP_KEEPALIVE`` on
    macOS).
    """
    options: dict[int, int] = {}
    for name, value in (
        ("TCP_KEEPIDLE", 60),
        ("TCP_KEEPALIVE", 60),  # macOS spelling of KEEPIDLE
        ("TCP_KEEPINTVL", 10),
        ("TCP_KEEPCNT", 3),
    ):
        const = getattr(socket, name, None)
        if const is not None and const not in options:
            options[const] = value
    return options


def _saq_connection_kwargs() -> dict[str, Any]:
    """Connection kwargs for the SAQ Redis client.

    Deliberately different from ``app.core.redis._common_kwargs()``:

    * **No** ``socket_timeout`` — SAQ relies on long-blocking reads
      (``BLMOVE`` for dequeue, pub/sub for job updates).  A socket
      timeout would fire on every healthy blocking read and spam
      reconnect/error noise.  Dead peers are detected via TCP
      keepalive instead (see :func:`_tcp_keepalive_options`).
    * ``decode_responses`` is FORCED to ``False`` — SAQ expects
      ``bytes`` payloads end to end (its ``iter_jobs`` calls
      ``job_id.decode("utf-8")`` on SCAN results, so a string-mode
      client breaks job iteration/cancellation with ``AttributeError:
      'str' object has no attribute 'decode'``).
      Explicit rather than implicit because redis-py's ``from_url``
      also honours a ``?decode_responses=True`` URL query param;
      explicit kwargs take precedence over URL options, so forcing it
      here makes the bytes contract deployment-proof in both
      standalone and sentinel modes.
    * ``health_check_interval`` so pooled command connections that sat
      idle get a PING before reuse instead of failing mid-command.
    """
    return {
        "decode_responses": False,
        "socket_connect_timeout": settings.redis_socket_connect_timeout,
        "socket_keepalive": True,
        "socket_keepalive_options": _tcp_keepalive_options(),
        "health_check_interval": settings.redis_health_check_interval,
        "retry_on_timeout": settings.redis_retry_on_timeout,
        "max_connections": settings.redis_max_connections,
    }


def get_queue() -> Queue:
    """Return the process-local SAQ queue singleton.

    Thread safety
    -------------
    The module-level ``_queue`` global is **not** guarded by a lock — this
    helper is intended to be called from the asyncio event loop, which is
    single-threaded by design.  Calling it concurrently from multiple
    OS threads (e.g. from a ``ThreadPoolExecutor`` worker) could race on
    the lazy initialisation and produce two ``Queue`` instances.  If a
    future caller needs threaded access, wrap construction in a
    ``threading.Lock`` or instantiate the queue eagerly at module
    import.  All current callers (``main.startup``, ``assignment_engine``,
    ``autonomous``) run on the FastAPI / SAQ event loop, so the
    single-threaded assumption holds.
    """
    global _queue
    if _queue is None:
        import redis.asyncio as redis_asyncio

        # Teardown-safe subclass: idempotent disconnect so SAQ's own
        # queue.disconnect() in its CLI finally block cannot trigger a
        # pubsub reconnect after the default executor is shut down.
        queue_cls = _teardown_safe_queue_cls()

        if settings.redis_mode == "sentinel":
            # SAQ's ``Queue.from_url`` does not understand Sentinel
            # discovery, so we build a redis-py ``master_for`` proxy
            # (which transparently re-resolves the current master on
            # every command, following Sentinel failovers) and hand
            # that pre-built client to SAQ via ``RedisQueue``. The
            # Sentinel singleton is owned by ``app.core.redis``; we
            # go through the public ``sentinel_client()`` accessor
            # so the queue layer doesn't depend on the connection
            # layer's private module state.
            #
            # The per-call kwargs OVERRIDE the Sentinel singleton's
            # data-plane connection kwargs (which are tuned for the
            # app client: ``decode_responses=True`` + a short
            # ``socket_timeout``).  Both are wrong for SAQ — string
            # decoding breaks SAQ's bytes contract and the socket
            # timeout fires on every healthy blocking ``BLMOVE`` —
            # so they are explicitly reset here.
            from app.core.redis import sentinel_client

            client = sentinel_client().master_for(
                settings.redis_sentinel_service,
                redis_class=redis_asyncio.Redis,
                socket_timeout=None,
                **_saq_connection_kwargs(),
            )
            _queue = queue_cls(client)
        elif settings.redis_mode == "standalone":
            # ``Queue.from_url`` forwards kwargs to ``RedisQueue``, not
            # to the redis client — build the client explicitly so the
            # keepalive/health-check hardening actually applies.
            client = redis_asyncio.from_url(
                settings.redis_url,
                **_saq_connection_kwargs(),
            )
            _queue = queue_cls(client)
        else:
            raise ValueError(f"Unsupported REDIS_MODE for SAQ queue: {settings.redis_mode}")
    return _queue


async def close_queue() -> None:
    """Disconnect and clear the process-local SAQ queue if it was created.

    Safe to call multiple times — subsequent calls are no-ops.  Matches
    the asyncio-only thread-safety contract of :func:`get_queue`.
    """
    global _queue
    if _queue is None:
        return
    with suppress(Exception):
        await _queue.disconnect()
    _queue = None
