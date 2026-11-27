# SPDX-License-Identifier: Apache-2.0

"""Getting through a short PostgreSQL outage without a flood of 500s.

A PostgreSQL restart, a failover, a connection-proxy restart or a Kubernetes
Service briefly left without a ready endpoint refuses NEW TCP connections for a
few seconds while the connections already open keep working. Every request that
needed a fresh pooled connection in that window failed with a 500 and a full
traceback (``ConnectionRefusedError`` raised from ``asyncpg.connect``), and the
platform then recovered on its own.

Two halves:

- :func:`retrying_connect` is the engine's ``async_creator_fn``: a connect that
  failed for a reason that ends on its own (refused, reset, unreachable, the
  server starting up or out of slots) is tried again with a capped, jittered
  back-off for up to ``DB_CONNECT_RETRY_SECONDS``, so a blip costs a request
  some latency instead of an error. A connect that TIMED OUT is not retried:
  ``DB_CONNECT_TIMEOUT`` already bounds it and retrying would multiply
  that wait.
- :func:`install_database_unavailable_handler` answers a request the database
  still could not serve past that with a 503 + ``Retry-After`` and one
  throttled WARNING line instead of a 500 and a traceback per request; the
  frontend already reads a 503 as "server unavailable" and retries.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import random
import socket
import time
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import asyncpg
import structlog
from sqlalchemy import exc as sa_exc
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, Response
from starlette.websockets import WebSocket

from app.core.config import settings
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: Seconds a client is told to wait before retrying a request the database could not serve.
DATABASE_UNAVAILABLE_RETRY_AFTER_SECONDS = 5
DATABASE_UNAVAILABLE_DETAIL = "The database is temporarily unavailable. Retry in a few seconds."
#: WebSocket close code 1013 "Try Again Later".
_WS_TRY_AGAIN_LATER = 1013

_FIRST_RETRY_DELAY_SECONDS = 0.25
_MAX_RETRY_DELAY_SECONDS = 2.0
_LOG_INTERVAL_SECONDS = 10.0

_TRANSIENT_ERRNOS = frozenset(
    code
    for name in (
        "ECONNREFUSED",
        "ECONNRESET",
        "ECONNABORTED",
        "EHOSTUNREACH",
        "ENETUNREACH",
        "ENETDOWN",
        "EHOSTDOWN",
    )
    if (code := getattr(errno, name, None)) is not None
)
_TRANSIENT_RESOLVER_ERRORS = frozenset(
    code for name in ("EAI_AGAIN",) if (code := getattr(socket, name, None)) is not None
)
_TRANSIENT_POSTGRES_CONNECT_ERRORS: tuple[type[BaseException], ...] = (
    # 57P03: the server is starting up, shutting down or in recovery.
    asyncpg.exceptions.CannotConnectNowError,
    # 53300: every connection slot is taken.
    asyncpg.exceptions.TooManyConnectionsError,
    # Class 08: the connection failed or was dropped while being established.
    asyncpg.exceptions.PostgresConnectionError,
)

#: Exception classes the HTTP handler is registered for. It answers 503 only
#: for the instances :func:`database_unavailable_reason` recognises and
#: re-raises every other one unchanged.
DATABASE_UNAVAILABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ConnectionRefusedError,
    ConnectionResetError,
    ConnectionAbortedError,
    socket.gaierror,
    *_TRANSIENT_POSTGRES_CONNECT_ERRORS,
    sa_exc.DBAPIError,
    sa_exc.TimeoutError,
)


class _LogThrottle:
    """At most one log line per ``interval`` seconds, counting the lines skipped."""

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._last = float("-inf")
        self._skipped = 0

    def take(self, now: float) -> int | None:
        """The number of lines skipped since the last one written, or ``None`` to skip this one."""
        if now - self._last < self._interval:
            self._skipped += 1
            return None
        skipped, self._skipped, self._last = self._skipped, 0, now
        return skipped


_retry_log = _LogThrottle(_LOG_INTERVAL_SECONDS)
_unavailable_log = _LogThrottle(_LOG_INTERVAL_SECONDS)


def _skipped_suffix(skipped: int) -> str:
    return f" ({skipped} more since the last line)" if skipped else ""


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {format_exception_message(exc)}"


def is_transient_connect_error(exc: BaseException) -> bool:
    """Whether a failed connect may succeed if simply tried again shortly."""
    if isinstance(exc, TimeoutError):
        return False
    if isinstance(exc, _TRANSIENT_POSTGRES_CONNECT_ERRORS):
        return True
    if isinstance(exc, socket.gaierror):
        return exc.errno in _TRANSIENT_RESOLVER_ERRORS
    if isinstance(exc, ConnectionError):
        return True
    return isinstance(exc, OSError) and exc.errno in _TRANSIENT_ERRNOS


#: Added to the error a connect through :func:`connect_with_retry` raises: a refused
#: or timed-out connect reaches the caller as a bare ``OSError`` / ``TimeoutError``.
DATABASE_CONNECT_NOTE = "Raised opening a PostgreSQL connection."


def _backoff(first: float, cap: float) -> Iterator[float]:
    """Delays doubling from ``first`` up to ``cap``, each jittered down by up to half."""
    delay = first
    while True:
        yield delay * random.uniform(0.5, 1.0)  # noqa: S311
        delay = min(delay * 2, cap)


async def connect_with_retry[T](
    connect: Callable[..., Awaitable[T]],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    budget_seconds: float,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> T:
    """``await connect(*args, **kwargs)``, retrying transient failures for up to ``budget_seconds``.

    The last failure is raised unchanged once the budget is spent or when it is
    not transient, so the caller sees exactly the error it saw before.
    """
    started = clock()
    deadline = started + max(0.0, budget_seconds)
    delays = _backoff(_FIRST_RETRY_DELAY_SECONDS, _MAX_RETRY_DELAY_SECONDS)
    attempt = 1
    while True:
        try:
            connection = await connect(*args, **kwargs)
        except Exception as exc:
            remaining = deadline - clock()
            if remaining <= 0 or not is_transient_connect_error(exc):
                exc.add_note(DATABASE_CONNECT_NOTE)
                raise
            skipped = _retry_log.take(clock())
            if skipped is not None:
                logger.warning(
                    "PostgreSQL connect failed (%s); retrying for up to %.0fs%s",
                    _describe(exc),
                    budget_seconds,
                    _skipped_suffix(skipped),
                )
            await sleep(min(remaining, next(delays)))
            attempt += 1
            continue
        if attempt > 1:
            logger.info(
                "PostgreSQL connection opened after %d attempts (%.1fs)",
                attempt,
                clock() - started,
            )
        return connection


async def retrying_connect(*args: Any, **kwargs: Any) -> Any:
    """The engine's ``async_creator_fn``: ``asyncpg.connect`` through :func:`connect_with_retry`."""
    return await connect_with_retry(
        asyncpg.connect,
        args,
        kwargs,
        budget_seconds=settings.db_connect_retry_seconds,
    )


def database_unavailable_reason(exc: BaseException) -> str | None:
    """Why ``exc`` means the database cannot serve requests right now, or ``None`` when it does not."""
    if isinstance(exc, sa_exc.TimeoutError):
        return "no database connection became free in time"
    if isinstance(exc, sa_exc.DBAPIError):
        return "database connection lost" if exc.connection_invalidated else None
    if is_transient_connect_error(exc):
        return "database unreachable"
    return None


#: What a tool answers when no pooled connection became free in time (#2561): busy, never a failure.
POOL_BUSY_ANSWER = "Busy: the platform is under load; nothing was read. Retry this call in a few seconds."
#: The same for a write that failed before its commit.
POOL_BUSY_WRITE_ANSWER = "Busy: the platform is under load; nothing was changed. Retry this call in a few seconds."


def is_pool_timeout(exc: BaseException) -> bool:
    """Whether *exc* is a pool checkout that timed out (``sqlalchemy.exc.TimeoutError``, #2561).

    Reads the error, the driver error a wrapper carries (``.orig``) and its causes,
    as ``ctem_cases_store.is_lock_wait`` does, so a pool timeout re-raised by a
    service is still found. A statement or lock timeout is not one: those are
    PostgreSQL errors, answered by their own paths.
    """
    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in seen and len(seen) < 5:
        seen.append(current)
        if isinstance(current, sa_exc.TimeoutError):
            return True
        current = getattr(current, "orig", None) or current.__cause__
    return False


async def database_unavailable_handler(conn: HTTPConnection, exc: Exception) -> Response | None:
    """503 + ``Retry-After`` for a request the database could not serve; anything else re-raised."""
    reason = database_unavailable_reason(exc)
    if reason is None:
        raise exc
    detail = f"{reason}: {_describe(exc)}"
    skipped = _unavailable_log.take(time.monotonic())
    if skipped is not None:
        logger.warning(
            "Database unavailable, answering 503 on %s (%s)%s",
            conn.url.path,
            detail,
            _skipped_suffix(skipped),
        )
    if isinstance(conn, WebSocket):
        # The socket may already be accepted, closed or gone.
        with contextlib.suppress(Exception):
            await conn.close(code=_WS_TRY_AGAIN_LATER)
        return None
    structlog.contextvars.bind_contextvars(error_detail=detail[:300])
    return JSONResponse(
        {"detail": DATABASE_UNAVAILABLE_DETAIL},
        status_code=503,
        headers={"Retry-After": str(DATABASE_UNAVAILABLE_RETRY_AFTER_SECONDS)},
    )


def install_database_unavailable_handler(app: Any) -> None:
    """Register :func:`database_unavailable_handler` on ``app`` for every class it may answer."""
    for exc_class in DATABASE_UNAVAILABLE_EXCEPTIONS:
        app.add_exception_handler(exc_class, database_unavailable_handler)


# The server shut down or is starting up (57P01-57P03), the connection was lost
# (08000, 08003, 08006), or it still reaches a demoted, now read-only primary (25006).
_TRANSIENT_SQLSTATES = frozenset({"57P01", "57P02", "57P03", "08000", "08003", "08006", "25006"})

_STARTUP_FIRST_RETRY_SECONDS = 1.0
_STARTUP_MAX_RETRY_SECONDS = 30.0


def _cause_chain(exc: BaseException) -> Iterator[BaseException]:
    """``exc``, the errors it was raised from, and a ``DBAPIError``'s driver error.

    ``__context__`` is not followed: an error raised while handling a lost
    connection is not caused by it.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        if isinstance(current, sa_exc.DBAPIError) and isinstance(current.orig, BaseException):
            yield current.orig
        current = current.__cause__


def sqlstates_in_chain(exc: BaseException) -> set[str]:
    """The PostgreSQL SQLSTATEs of ``exc`` and its causes (asyncpg ``sqlstate``, psycopg ``pgcode``)."""
    codes = (getattr(link, "sqlstate", None) or getattr(link, "pgcode", None) for link in _cause_chain(exc))
    return {code for code in codes if isinstance(code, str)}


def _is_transient_database_failure(exc: BaseException) -> bool:
    if isinstance(exc, sa_exc.TimeoutError):  # no pooled connection became free
        return True
    if isinstance(exc, sa_exc.DBAPIError) and exc.connection_invalidated:
        return True
    if DATABASE_CONNECT_NOTE in getattr(exc, "__notes__", ()):
        return isinstance(exc, TimeoutError) or is_transient_connect_error(exc)
    return False


def outage_label(exc: BaseException) -> str:
    """Name a database outage without its message: the SQLSTATE, else the innermost cause's class.

    Safe for the unauthenticated readiness probe: neither carries a host or address.
    The outermost class (``DBAPIError``, ``RuntimeError``) would name only a wrapper.
    """
    codes = sqlstates_in_chain(exc)
    if codes:
        return min(codes & _TRANSIENT_SQLSTATES or codes)
    *_, innermost = _cause_chain(exc)
    return type(innermost).__name__


def is_transient_database_error(exc: BaseException) -> bool:
    """Whether ``exc`` comes from a database outage that ends on its own.

    Only errors from the database count: a lost or invalidated connection, a
    pool timeout, a failed connect of the engine, or one of the SQLSTATEs above.
    A timeout or a refused connect from anything else does not. Causes are
    followed because the migrations re-raise every failure as a ``RuntimeError``.
    """
    if sqlstates_in_chain(exc) & _TRANSIENT_SQLSTATES:
        return True
    return any(_is_transient_database_failure(link) for link in _cause_chain(exc))


async def retry_while_database_unavailable[T](
    step: str,
    run: Callable[[], Awaitable[T]],
    *,
    on_status: Callable[[BaseException | None], None] | None = None,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> T:
    """``await run()``, again after a back-off whenever it fails on a transient database error.

    No attempt limit: a failover or an outage ends on its own, and the startup
    watchdog reports a startup that never completes. Any other error is raised at once.
    ``on_status`` gets each transient error before the wait, then ``None`` once the
    retrying is over, whatever ended it.
    """
    from app.core.database import engine

    delays = _backoff(_STARTUP_FIRST_RETRY_SECONDS, _STARTUP_MAX_RETRY_SECONDS)
    attempt = 1
    retried = False
    try:
        while True:
            try:
                result = await run()
            except Exception as exc:
                if not is_transient_database_error(exc):
                    raise
                retried = True
                if on_status is not None:
                    on_status(exc)
                wait = next(delays)
                logger.warning(
                    "%s failed on a transient database error (%s); retrying in %.1fs (attempt %d)",
                    step,
                    _describe(exc),
                    wait,
                    attempt,
                )
                # The pre-ping passes on a demoted, read-only primary: the next
                # attempt starts on new connections.
                await engine.dispose()
                await sleep(wait)
                attempt += 1
                continue
            if attempt > 1:
                logger.info("%s succeeded after %d attempts", step, attempt)
            return result
    finally:
        if retried and on_status is not None:
            on_status(None)
