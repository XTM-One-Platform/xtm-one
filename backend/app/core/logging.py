# SPDX-License-Identifier: Apache-2.0

"""Structured logging configuration.

Routes ALL logging (both structlog and stdlib) through structlog processors.
- Production (LOG_FORMAT=json): machine-parseable JSON, one object per line.
- Development  (LOG_FORMAT=console or DEBUG=true): colored human-readable output.

Usage
-----
Call ``setup_logging()`` once at application startup (main.py / worker.py).
Then use standard ``logging.getLogger(__name__)`` everywhere — the stdlib
handler is wired to emit through structlog processors automatically. Pass
structured fields via ``extra={...}`` (surfaced as top-level JSON keys by
the ``ExtraAdder()`` processor below) — do NOT call ``structlog.get_logger()``
directly; see the caution in ``setup_logging()`` for why that crashes.

Contextual fields (agent, assignment, run_id …) can be attached per-request
via ``structlog.contextvars``:

    structlog.contextvars.bind_contextvars(agent="Mail Desk", assignment="Draft replies")
    …
    structlog.contextvars.unbind_contextvars("agent", "assignment")

These fields are automatically merged into every log record emitted on
that asyncio task / thread.
"""

from __future__ import annotations

import logging
import sys

import structlog

# ``app.core.redaction`` and ``app.core.utils`` are stdlib-only (no app imports
# at import time), so this cannot cycle.
from app.core.redaction import redact_log_event
from app.core.utils import (
    GRACEFUL_SHUTDOWN_CANCEL_HINT,
    is_graceful_shutdown_cancellation,
)

_VALID_LOG_LEVELS = {"debug", "info", "warning", "error", "critical"}


def setup_logging(*, debug: bool = False, log_level: str = "", log_format: str = "") -> None:
    """Configure structlog + stdlib logging for the application.

    Parameters
    ----------
    debug:
        When *True* and *log_format* is empty, defaults to colored console.
    log_level:
        Explicit root logger level (``"debug"``, ``"info"``, ``"warning"``,
        ``"error"``, ``"critical"``).  When empty, falls back to ``DEBUG``
        if *debug* is True, otherwise ``INFO``.
    log_format:
        ``"json"`` → JSON lines.  ``"console"`` → colored human output.
        When empty, inferred from *debug*.
    """
    fmt = log_format.lower().strip() if log_format else ("console" if debug else "json")
    use_json = fmt == "json"

    # Processors shared by both structlog-native loggers and stdlib loggers
    # routed through ProcessorFormatter.
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if use_json:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    # Configure structlog-native loggers.
    #
    # CAUTION: do NOT use ``structlog.get_logger()`` elsewhere in this
    # codebase and expect it to work — ``logger_factory=PrintLoggerFactory()``
    # below produces bare ``structlog.PrintLogger`` objects, which have no
    # ``.name`` attribute, and ``add_logger_name`` (shared with the stdlib
    # pipeline above) unconditionally reads ``logger.name`` for any logger
    # not routed through the stdlib bridge. Any ``structlog.get_logger()``
    # call raises ``AttributeError: 'PrintLogger' object has no attribute
    # 'name'`` on its first emission once this function has run — discovered
    # while implementing spec 003-sandbox-call-logging, whose original
    # design used a structlog-native logger before this was caught by the
    # full test suite. Use ``logging.getLogger(__name__)`` (the stdlib
    # bridge below) for every new logger; use ``extra={...}`` for
    # structured fields, surfaced via the ``ExtraAdder()`` processor below.
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            redact_log_event,
            renderer,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Route ALL stdlib logging through structlog processors so that every
    # ``logging.getLogger(__name__).info(…)`` call produces the same
    # structured output (JSON or console) as structlog-native loggers.
    formatter = structlog.stdlib.ProcessorFormatter(
        # These processors run on the stdlib LogRecord *before* it enters
        # the shared pipeline.
        foreign_pre_chain=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.UnicodeDecoder(),
            # Surfaces logging.getLogger(...).info(msg, extra={...}) kwargs as
            # top-level structured fields (spec 003-sandbox-call-logging) —
            # without this, `extra` is silently dropped by every processor
            # above, since none of them read LogRecord.__dict__.
            structlog.stdlib.ExtraAdder(),
        ],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            # The secret variable values of the run or turn logging:
            # after the traceback is rendered, so it is covered too.
            redact_log_event,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    # Resolve log level: explicit LOG_LEVEL > DEBUG flag > default (INFO)
    effective_level = log_level.lower().strip() if log_level else ""
    if effective_level in _VALID_LOG_LEVELS:
        root.setLevel(getattr(logging, effective_level.upper()))
    else:
        root.setLevel(logging.DEBUG if debug else logging.INFO)

    # Route Python warnings (e.g. DeprecationWarning, InsecureKeyLengthWarning)
    # through the logging system so they appear as structured JSON instead of
    # raw text on stderr.
    logging.captureWarnings(True)

    # Uvicorn sets up its own handlers *before* importing the app module, so
    # by the time setup_logging() runs those handlers already exist and bypass
    # our root handler.  Clear them, reset their levels to NOTSET (so they
    # inherit the root logger's level), and force propagation through the root
    # handler so uvicorn's startup / shutdown messages are structured too.
    for uv_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(uv_name)
        uv_logger.handlers.clear()
        uv_logger.setLevel(logging.NOTSET)
        uv_logger.propagate = True

    # Demote uvicorn's ROUTINE bounded-drain messages from ERROR to WARNING.
    # The worker-recycle drain is bounded on purpose
    # (--timeout-graceful-shutdown), so "Cancel N running
    # task(s), timeout graceful shutdown exceeded" is expected hygiene on
    # every recycle — ERROR level trips error-rate alerting for a non-fault.
    # Idempotent: the filter instance is only added once.
    uv_error_logger = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, DemoteRoutineShutdownDrainLogs) for f in uv_error_logger.filters):
        uv_error_logger.addFilter(DemoteRoutineShutdownDrainLogs())

    # Quiet noisy third-party loggers
    for noisy in (
        "httpx",
        "httpcore",
        "hpack",
        "urllib3",
        "asyncio",
        "aiosqlite",
        "openai",
        "anthropic",
        "google",
        "uvicorn.access",
        "mcp",
        # ``websockets`` emits keepalive/ping-timeout exceptions on the
        # default asyncio loop logger when a runner or relay WebSocket
        # misses a protocol pong (e.g. the agentic loop blocks the event
        # loop for >ws-ping-timeout while streaming an LLM response). The
        # connection is already handled gracefully by the FastAPI route
        # — these are not actionable errors, so demote the library logger
        # to WARNING and install an asyncio exception handler below that
        # specifically silences ``ConnectionClosedError`` + ``CancelledError``
        # exceptions raised inside ``shielded future`` close paths.
        "websockets",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    install_asyncio_exception_handler()


class DemoteRoutineShutdownDrainLogs(logging.Filter):
    """Demote uvicorn's routine bounded-drain logs from ERROR to WARNING.

    Covers two messages emitted by ``uvicorn.error`` during a MAX_REQUESTS
    worker-recycle drain that reaches its ``--timeout-graceful-shutdown``
    deadline (a fully intended, bounded behaviour):

    * ``"Cancel N running task(s), timeout graceful shutdown exceeded"`` —
      the single summary line before uvicorn cancels the leftover ASGI tasks.
    * ``"Exception in ASGI application"`` records whose exception is the
      drain ``CancelledError`` — belt-and-braces for any path the
      ``GracefulShutdownDrainMiddleware`` in ``app.main`` cannot absorb
      (e.g. protocols that bypass the HTTP middleware stack).

    Everything else passes through untouched. The record is demoted in
    place (``levelno``/``levelname``) so it still renders — at WARNING,
    matching its actual severity — instead of being dropped.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.ERROR:
            return True
        message = str(record.msg)
        demote = GRACEFUL_SHUTDOWN_CANCEL_HINT in message
        if not demote and message.startswith("Exception in ASGI application"):
            exc_info = record.exc_info
            exc = exc_info[1] if isinstance(exc_info, tuple) else None
            if isinstance(exc, BaseException):
                demote = is_graceful_shutdown_cancellation(exc)
        if demote:
            record.levelno = logging.WARNING
            record.levelname = "WARNING"
            # Honour the configured verbosity: the record already passed the
            # logger's ERROR gate before demotion, so at LOG_LEVEL=error the
            # now-WARNING record must be dropped like any other WARNING
            # instead of slipping through on its original level.
            effective = logging.getLogger(record.name).getEffectiveLevel()
            return record.levelno >= effective
        return True


def install_asyncio_exception_handler() -> None:
    """Install a custom asyncio exception handler that drops WS keepalive noise.

    Without this, every time a runner or relay WebSocket misses a protocol
    pong (routine while long LLM streams keep the event loop busy), the
    default asyncio handler logs::

        ConnectionClosedError exception in shielded future
        future: <Future finished exception=ConnectionClosedError(
            None, Close(code=<CloseCode.INTERNAL_ERROR: 1011>,
                        reason='keepalive ping timeout'), None
        )>

    at ERROR level with a full traceback — multiple times per minute on a
    busy pod, completely drowning out actionable errors. The connection is
    already handled by the FastAPI route's ``except WebSocketDisconnect``
    branch, so these traces are pure noise.

    Idempotent — safe to call from ``setup_logging`` *and* from the
    FastAPI lifespan startup hook. ``setup_logging`` runs before uvicorn's
    event loop exists, so it will silently no-op there; the lifespan call
    runs once the loop is running and is the one that actually attaches
    the handler.
    """
    import asyncio

    asyncio_logger = logging.getLogger("asyncio")

    # Try to import the specific exception class so we can target it
    # narrowly. If ``websockets`` isn't installed (unit-test edge case),
    # fall back to message-based matching.
    try:
        from websockets.exceptions import ConnectionClosed

        ws_connection_closed: type | None = ConnectionClosed
    except ImportError:  # pragma: no cover - defensive
        ws_connection_closed = None

    def _handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")
        message = context.get("message", "")

        if isinstance(exc, asyncio.CancelledError):
            # Routine task cancellation, not an error.
            return

        if ws_connection_closed is not None and isinstance(exc, ws_connection_closed):
            asyncio_logger.debug(
                "WebSocket closed: %s",
                exc,
                extra={"context": message},
            )
            return

        # Pattern match for the keepalive-ping-timeout future-close path
        # used by ``websockets``' legacy protocol — the exception is
        # raised inside the ``close_connection`` coroutine that we don't
        # await, so it surfaces here as an unhandled-task warning.
        if "shielded future" in message and "ConnectionClosedError" in message:
            asyncio_logger.debug("WebSocket closed in shielded future: %s", message)
            return

        # Anything else: defer to asyncio's default behaviour (ERROR + traceback).
        loop.default_exception_handler(context)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — likely called from ``setup_logging`` before
        # uvicorn started its loop. The lifespan call will install us once
        # the loop exists. Note: we can't pre-install via the loop *policy*
        # because uvicorn instantiates its loop directly without going
        # through the policy on Linux (uvloop) and on Windows (ProactorEventLoop).
        return
    loop.set_exception_handler(_handler)
