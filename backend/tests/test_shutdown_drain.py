"""Graceful-shutdown drain handling.

The worker-recycle drain is bounded on purpose (--timeout-graceful-shutdown) and SSE streams are bounded at 15 minutes on purpose — so on
every MAX_REQUESTS recycle, streams younger than the drain window are still
open when the deadline fires and uvicorn cancels their ASGI tasks. That
routine cancellation must complete the response cleanly (one WARNING) instead
of surfacing as a dozen "Exception in ASGI application" ERROR tracebacks.
"""

import asyncio
import logging

import pytest

from app.core.logging import DemoteRoutineShutdownDrainLogs
from app.core.utils import is_graceful_shutdown_cancellation
from app.main import GracefulShutdownDrainMiddleware

UVICORN_DRAIN_CANCEL_MSG = "Task cancelled, timeout graceful shutdown exceeded"


# ---------------------------------------------------------------------------
# is_graceful_shutdown_cancellation
# ---------------------------------------------------------------------------


def test_detects_uvicorn_drain_cancel_message():
    exc = asyncio.CancelledError(UVICORN_DRAIN_CANCEL_MSG)
    assert is_graceful_shutdown_cancellation(exc)


def test_plain_cancellation_is_not_a_drain():
    assert not is_graceful_shutdown_cancellation(asyncio.CancelledError())


def test_application_cancel_message_is_not_a_drain():
    assert not is_graceful_shutdown_cancellation(
        asyncio.CancelledError("user cancelled the request")
    )


def test_generic_graceful_shutdown_mention_is_not_a_drain():
    # The hint is uvicorn's full distinctive tail ("timeout graceful shutdown
    # exceeded") — an application cancel message that merely mentions
    # "graceful shutdown" must NOT be classified as the bounded drain.
    assert not is_graceful_shutdown_cancellation(
        asyncio.CancelledError("app graceful shutdown requested")
    )


def test_drain_message_found_on_context_chain():
    # A CancelledError re-raised from a ``finally`` block chains the original
    # drain cancellation as __context__ — the walk must still find it.
    original = asyncio.CancelledError(UVICORN_DRAIN_CANCEL_MSG)
    wrapper = asyncio.CancelledError()
    wrapper.__context__ = original
    assert is_graceful_shutdown_cancellation(wrapper)


def test_context_cycle_is_safe():
    a = asyncio.CancelledError()
    b = asyncio.CancelledError()
    a.__context__ = b
    b.__context__ = a
    assert not is_graceful_shutdown_cancellation(a)


# ---------------------------------------------------------------------------
# GracefulShutdownDrainMiddleware
# ---------------------------------------------------------------------------


def _http_scope(path: str = "/api/v1/activity/stream") -> dict:
    return {"type": "http", "method": "GET", "path": path, "headers": []}


async def _never_receive() -> dict:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


async def _run_cancelled(middleware, scope, send, *, started: asyncio.Event, msg):
    """Run the middleware in a task and cancel it once the app is hanging."""
    task = asyncio.create_task(middleware(scope, _never_receive, send))
    await started.wait()
    await asyncio.sleep(0)  # let the app settle on its hang point
    if msg:
        task.cancel(msg=msg)
    else:
        task.cancel()
    return task


async def test_started_stream_is_completed_cleanly():
    started = asyncio.Event()
    sent: list[dict] = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send(
            {"type": "http.response.body", "body": b"data: x\n\n", "more_body": True}
        )
        started.set()
        await asyncio.Event().wait()  # hang like listen_for_disconnect

    async def send(message):
        sent.append(message)

    middleware = GracefulShutdownDrainMiddleware(app)
    task = await _run_cancelled(
        middleware, _http_scope(), send, started=started, msg=UVICORN_DRAIN_CANCEL_MSG
    )

    # The middleware must absorb the drain cancellation (no exception, the
    # task is NOT marked cancelled) and complete the response.
    assert await task is None
    assert not task.cancelled()
    assert sent[-1] == {"type": "http.response.body", "body": b"", "more_body": False}


async def test_unstarted_request_gets_completed_503():
    started = asyncio.Event()
    sent: list[dict] = []

    async def app(scope, receive, send):
        started.set()
        await asyncio.Event().wait()  # hang before any response frame

    async def send(message):
        sent.append(message)

    middleware = GracefulShutdownDrainMiddleware(app)
    task = await _run_cancelled(
        middleware, _http_scope("/api/v1/agents"), send, started=started,
        msg=UVICORN_DRAIN_CANCEL_MSG,
    )

    assert await task is None
    assert not task.cancelled()
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 503
    assert (b"retry-after", b"1") in sent[0]["headers"]
    assert sent[-1] == {
        "type": "http.response.body",
        "body": b'{"detail":"Server is restarting, please retry shortly."}',
        "more_body": False,
    }


async def test_non_drain_cancellation_propagates():
    started = asyncio.Event()

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        started.set()
        await asyncio.Event().wait()

    async def send(message):
        pass

    middleware = GracefulShutdownDrainMiddleware(app)
    task = await _run_cancelled(
        middleware, _http_scope(), send, started=started, msg=None
    )

    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


async def test_already_complete_response_sends_nothing_extra():
    started = asyncio.Event()
    sent: list[dict] = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})
        started.set()
        await asyncio.Event().wait()  # pathological: hang after completing

    async def send(message):
        sent.append(message)

    middleware = GracefulShutdownDrainMiddleware(app)
    task = await _run_cancelled(
        middleware, _http_scope(), send, started=started, msg=UVICORN_DRAIN_CANCEL_MSG
    )

    assert await task is None
    assert len(sent) == 2  # nothing appended after the completed response


async def test_non_http_scope_passes_through():
    called = []

    async def app(scope, receive, send):
        called.append(scope["type"])

    middleware = GracefulShutdownDrainMiddleware(app)
    await middleware({"type": "websocket"}, _never_receive, lambda m: None)
    assert called == ["websocket"]


async def test_first_drain_logs_warning_then_debug(caplog: pytest.LogCaptureFixture):
    started = asyncio.Event()

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        started.set()
        await asyncio.Event().wait()

    async def send(message):
        pass

    middleware = GracefulShutdownDrainMiddleware(app)
    with caplog.at_level(logging.DEBUG, logger="app.shutdown_drain"):
        for _ in range(3):
            started.clear()
            task = await _run_cancelled(
                middleware,
                _http_scope(),
                send,
                started=started,
                msg=UVICORN_DRAIN_CANCEL_MSG,
            )
            assert await task is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(warnings) == 1
    assert "routine worker-recycle hygiene" in warnings[0].getMessage()
    assert len(debugs) == 2


# ---------------------------------------------------------------------------
# DemoteRoutineShutdownDrainLogs (uvicorn.error filter)
# ---------------------------------------------------------------------------


def _record(msg: str, *args, exc: BaseException | None = None) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.error",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=(type(exc), exc, None) if exc is not None else None,
    )


def test_cancel_tasks_line_is_demoted_to_warning():
    record = _record("Cancel %s running task(s), timeout graceful shutdown exceeded", 16)
    assert DemoteRoutineShutdownDrainLogs().filter(record) is True
    assert record.levelno == logging.WARNING
    assert record.levelname == "WARNING"


def test_asgi_exception_with_drain_cancel_is_demoted():
    record = _record(
        "Exception in ASGI application\n",
        exc=asyncio.CancelledError(UVICORN_DRAIN_CANCEL_MSG),
    )
    assert DemoteRoutineShutdownDrainLogs().filter(record) is True
    assert record.levelno == logging.WARNING


def test_asgi_exception_with_real_error_stays_error():
    record = _record("Exception in ASGI application\n", exc=RuntimeError("boom"))
    assert DemoteRoutineShutdownDrainLogs().filter(record) is True
    assert record.levelno == logging.ERROR


def test_unrelated_error_record_stays_error():
    record = _record("Something actually broke: %s", "detail")
    assert DemoteRoutineShutdownDrainLogs().filter(record) is True
    assert record.levelno == logging.ERROR


def test_demoted_record_respects_error_level_floor():
    """At LOG_LEVEL=error the demoted (now WARNING) record must be dropped —
    demotion must not smuggle a WARNING past the configured verbosity."""
    uv_error = logging.getLogger("uvicorn.error")
    previous_level = uv_error.level
    uv_error.setLevel(logging.ERROR)
    try:
        record = _record(
            "Cancel %s running task(s), timeout graceful shutdown exceeded", 4
        )
        assert DemoteRoutineShutdownDrainLogs().filter(record) is False
        assert record.levelno == logging.WARNING
    finally:
        uv_error.setLevel(previous_level)


def test_filter_installed_on_uvicorn_error_logger_once():
    from app.core.logging import setup_logging

    setup_logging(log_level="warning", log_format="json")
    setup_logging(log_level="warning", log_format="json")
    uv_error = logging.getLogger("uvicorn.error")
    installed = [
        f for f in uv_error.filters if isinstance(f, DemoteRoutineShutdownDrainLogs)
    ]
    assert len(installed) == 1
