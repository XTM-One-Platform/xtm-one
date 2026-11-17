"""Tests for the /api/health/ready endpoint.

Specifically verifies that:
1. The S3/MinIO ``bucket_exists`` check runs on a worker thread (via the
   dedicated single-worker executor) so it never blocks the asyncio event
   loop.
2. A slow / unresponsive S3 backend causes the readiness endpoint to
   return 503 within the 5-second timeout instead of hanging indefinitely.
3. The overall check is bounded by a 5-second ``asyncio.wait_for`` so
   that a completely hung dependency cannot starve the liveness probe.
4. All three dependency checks run **concurrently** (not sequentially) so
   a slow DB connection-pool wait cannot consume the full timeout budget
   before Redis and S3 even start.
5. The S3 check uses a dedicated ``max_workers=1`` executor so a hung
   MinIO pins at most ONE thread process-wide: while that worker is busy,
   later probes queue behind it and their pending work items are
   cancelled by the per-check timeout before they ever run, so stuck
   threads never accumulate and the shared default executor (which also
   serves asyncpg DNS resolution) is never touched.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable
from concurrent.futures import ThreadPoolExecutor
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Minimal FastAPI app extracted from main.py for isolated testing
# ---------------------------------------------------------------------------

# Executors created (owned) by _make_app, shut down after each test by the
# autouse fixture below so non-daemon worker threads never leak across the
# pytest session.
_owned_executors: list[ThreadPoolExecutor] = []


@pytest.fixture(autouse=True)
def _cleanup_owned_executors():
    """Shut down every executor _make_app created during the test."""
    yield
    while _owned_executors:
        _owned_executors.pop().shutdown(wait=False, cancel_futures=True)


def _make_app(*, s3_executor: ThreadPoolExecutor | None = None):
    """Build a minimal copy of the health endpoints for testing.

    Parameters
    ----------
    s3_executor:
        Override the dedicated single-worker S3 executor.  Pass an executor
        whose only worker is already busy to test the "worker occupied ->
        queued check is cancelled before it runs" path.  The caller keeps
        ownership (and shutdown responsibility) of a passed-in executor.
        When ``None`` (the default) a fresh executor is created for isolation
        and shut down automatically at test teardown.
    """
    import json

    from fastapi import FastAPI
    from starlette.responses import Response as StarletteResponse

    app = FastAPI()

    # Mirror of the module-level executor in main.py (fresh instance per app
    # so tests are isolated from each other).
    if s3_executor is not None:
        executor = s3_executor
    else:
        executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="s3-health-check"
        )
        _owned_executors.append(executor)

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/health/ready")
    async def health_ready():
        async def _run_checks() -> tuple[dict, bool]:
            checks: dict = {}
            healthy = True

            from sqlalchemy import text

            from app.core.database import engine
            from app.core.redis import redis_client

            per_check_timeout = 4.0

            async def _check_database() -> str:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                return "ok"

            async def _check_redis() -> str:
                r = await redis_client()
                await cast(Awaitable[bool], r.ping())
                return "ok"

            async def _check_s3() -> str:
                from app.services.file_storage import file_storage

                def _bucket_exists() -> bool:
                    return file_storage.client.bucket_exists(file_storage._bucket)

                exists = await asyncio.get_running_loop().run_in_executor(
                    executor, _bucket_exists
                )
                if not exists:
                    raise RuntimeError("bucket not found")
                return "ok"

            results = await asyncio.gather(
                asyncio.wait_for(_check_database(), timeout=per_check_timeout),
                asyncio.wait_for(_check_redis(), timeout=per_check_timeout),
                asyncio.wait_for(_check_s3(), timeout=per_check_timeout),
                return_exceptions=True,
            )
            for name, result in zip(("database", "redis", "s3"), results, strict=True):
                if isinstance(result, TimeoutError):
                    checks[name] = f"error: timed out after {per_check_timeout}s"
                    healthy = False
                elif isinstance(result, Exception):
                    checks[name] = f"error: {result}"
                    healthy = False
                else:
                    checks[name] = result

            return checks, healthy

        try:
            checks, healthy = await asyncio.wait_for(_run_checks(), timeout=5.0)
        except TimeoutError:
            checks = {
                "status": "degraded",
                "error": "readiness check timed out",
            }
            return StarletteResponse(
                content=json.dumps(checks),
                status_code=503,
                media_type="application/json",
            )

        checks["status"] = "ok" if healthy else "degraded"
        if not healthy:
            return StarletteResponse(
                content=json.dumps(checks),
                status_code=503,
                media_type="application/json",
            )
        return checks

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_engine():
    """Return a mock async SQLAlchemy engine."""
    conn_ctx = MagicMock()
    conn_ctx.__aenter__ = AsyncMock(return_value=AsyncMock(execute=AsyncMock()))
    conn_ctx.__aexit__ = AsyncMock(return_value=False)
    engine = MagicMock()
    engine.connect = MagicMock(return_value=conn_ctx)
    return engine


def _make_mock_redis():
    """Return a mock async Redis client."""
    redis = AsyncMock()
    redis.ping = AsyncMock(return_value=True)
    return redis


def _make_mock_fs(bucket_exists):
    """Return a mock file_storage with the given bucket_exists mock."""
    mock_client = MagicMock()
    mock_client.bucket_exists = bucket_exists
    mock_fs = MagicMock()
    mock_fs.client = mock_client
    mock_fs._bucket = "test-bucket"
    return mock_fs


def _shortened_wait_for():
    """Return a wait_for wrapper that shrinks only the 4.0 s per-check timeout.

    The outer 5 s envelope stays intact so tests exercise the per-check
    timeout path, not the outer "all checks timed out" path.
    """
    _real_wait_for = asyncio.wait_for

    def wrapper(coro, timeout):
        short = 0.05 if timeout == 4.0 else timeout
        return _real_wait_for(coro, short)

    return wrapper


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s3_check_runs_off_event_loop():
    """bucket_exists must run on a worker thread, never on the event loop."""
    calls_from_thread: list[bool] = []

    def fake_bucket_exists(bucket: str) -> bool:
        # Worker threads have no running event loop, so get_running_loop()
        # raises RuntimeError there.  If it succeeds, we are (incorrectly)
        # still on the event loop thread.
        try:
            asyncio.get_running_loop()
            calls_from_thread.append(False)  # still in main thread
        except RuntimeError:
            calls_from_thread.append(True)  # correctly offloaded
        return True

    mock_fs = _make_mock_fs(fake_bucket_exists)
    engine = _make_mock_engine()
    redis = _make_mock_redis()

    with (
        patch("app.core.database.engine", engine),
        patch("app.core.redis.redis_client", AsyncMock(return_value=redis)),
        patch("app.services.file_storage.file_storage", mock_fs),
    ):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/health/ready")

    assert resp.status_code == 200
    assert calls_from_thread == [True], (
        "bucket_exists was NOT offloaded to a worker thread — "
        "it ran in the event loop thread instead"
    )


@pytest.mark.asyncio
async def test_slow_s3_returns_503_within_timeout():
    """A slow S3 response must time out the S3 check while DB and Redis succeed.

    With parallel check execution, a hung S3 call only fails the s3 check
    (via the per-check asyncio.wait_for); DB and Redis results are preserved.
    """
    release = threading.Event()

    def hung_bucket_exists(bucket: str) -> bool:
        # Simulate a very slow / hung MinIO.  Released at test teardown so
        # the worker thread does not outlive the test.
        release.wait(30)
        return True

    engine = _make_mock_engine()
    redis = _make_mock_redis()
    mock_fs = _make_mock_fs(MagicMock(side_effect=hung_bucket_exists))

    try:
        with (
            patch("app.core.database.engine", engine),
            patch("app.core.redis.redis_client", AsyncMock(return_value=redis)),
            patch("app.services.file_storage.file_storage", mock_fs),
            patch("asyncio.wait_for", new=_shortened_wait_for()),
        ):
            app = _make_app()
            import httpx

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as ac:
                resp = await ac.get("/api/health/ready")
    finally:
        release.set()

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    # S3 failed (per-check timeout); the parallel DB and Redis checks must
    # still have completed successfully.
    assert "timed out" in body.get("s3", ""), f"Expected S3 timeout, got: {body}"
    assert body.get("database") == "ok", f"DB check should succeed: {body}"
    assert body.get("redis") == "ok", f"Redis check should succeed: {body}"


@pytest.mark.asyncio
async def test_liveness_not_blocked_by_slow_s3():
    """The liveness endpoint must respond immediately regardless of S3 state.

    This is the core regression test: if bucket_exists blocked the event loop
    directly (as it did before the fix), the /api/health endpoint would also
    fail to respond during a MinIO timeout.  With the check offloaded to a
    worker thread the liveness check can be served concurrently.
    """
    engine = _make_mock_engine()
    redis = _make_mock_redis()

    s3_started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def hung_bucket_exists(bucket: str) -> bool:
        loop.call_soon_threadsafe(s3_started.set)
        release.wait(30)  # simulate hung MinIO; released at teardown
        return True

    mock_fs = _make_mock_fs(MagicMock(side_effect=hung_bucket_exists))

    try:
        with (
            patch("app.core.database.engine", engine),
            patch("app.core.redis.redis_client", AsyncMock(return_value=redis)),
            patch("app.services.file_storage.file_storage", mock_fs),
        ):
            app = _make_app()
            import httpx

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as ac:
                # Start the slow readiness check in background
                ready_task = asyncio.create_task(ac.get("/api/health/ready"))
                # Wait until S3 check has started (so the ready task is "stuck")
                await asyncio.wait_for(s3_started.wait(), timeout=2.0)
                # The liveness endpoint must respond immediately
                live_resp = await asyncio.wait_for(ac.get("/api/health"), timeout=1.0)
                assert live_resp.status_code == 200
                assert live_resp.json()["status"] == "ok"
                # Cancel the background task to avoid a lingering coroutine
                ready_task.cancel()
                with pytest.raises(asyncio.CancelledError) as exc_info:
                    _ = await ready_task
                assert exc_info.type is asyncio.CancelledError
    finally:
        release.set()


def test_health_ready_all_healthy():
    """Normal path: all checks pass → 200 with status ok."""
    mock_fs = _make_mock_fs(MagicMock(return_value=True))

    engine = _make_mock_engine()
    redis_inst = MagicMock()
    redis_inst.ping = AsyncMock(return_value=True)

    with (
        patch("app.core.database.engine", engine),
        patch("app.core.redis.redis_client", AsyncMock(return_value=redis_inst)),
        patch("app.services.file_storage.file_storage", mock_fs),
    ):
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/api/health/ready")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["redis"] == "ok"
    assert body["s3"] == "ok"


def test_health_ready_bucket_not_found():
    """When the bucket doesn't exist, readiness returns 503 degraded."""
    mock_fs = _make_mock_fs(MagicMock(return_value=False))
    mock_fs._bucket = "missing-bucket"

    engine = _make_mock_engine()
    redis_inst = MagicMock()
    redis_inst.ping = AsyncMock(return_value=True)

    with (
        patch("app.core.database.engine", engine),
        patch("app.core.redis.redis_client", AsyncMock(return_value=redis_inst)),
        patch("app.services.file_storage.file_storage", mock_fs),
    ):
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/api/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert "bucket not found" in body["s3"]


def test_health_ready_s3_error():
    """An S3 exception marks the check degraded but doesn't crash the endpoint."""
    mock_fs = _make_mock_fs(MagicMock(side_effect=OSError("connection refused")))

    engine = _make_mock_engine()
    redis_inst = MagicMock()
    redis_inst.ping = AsyncMock(return_value=True)

    with (
        patch("app.core.database.engine", engine),
        patch("app.core.redis.redis_client", AsyncMock(return_value=redis_inst)),
        patch("app.services.file_storage.file_storage", mock_fs),
    ):
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/api/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert "error" in body["s3"]


@pytest.mark.asyncio
async def test_stuck_s3_worker_does_not_spawn_more_threads():
    """When the dedicated S3 worker is stuck, later checks must not pile on.

    This is the thread-accumulation guard: a hung MinIO pins exactly ONE
    thread process-wide (the single worker of the dedicated executor).  A
    readiness check issued while that worker is busy queues behind it and is
    cancelled by the per-check timeout before it ever runs — so
    ``bucket_exists`` must NOT be called, no new thread is spawned, and the
    response honestly reports the S3 timeout while DB and Redis succeed.
    """
    blocker = threading.Event()
    blocker_started = threading.Event()

    def _occupy_worker() -> None:
        blocker_started.set()
        blocker.wait(30)

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="s3-health-test")
    executor.submit(_occupy_worker)  # occupy the single worker
    # Wait until the worker has actually dequeued the blocker before issuing
    # the readiness check.  (Strictly speaking the max_workers=1 FIFO queue
    # already guarantees bucket_exists can never run before the blocker, but
    # the explicit sync makes the precondition self-evident and independent
    # of executor scheduling internals.)
    assert blocker_started.wait(5), "blocker never started on the worker"

    engine = _make_mock_engine()
    redis = _make_mock_redis()
    bucket_exists = MagicMock(return_value=True)
    mock_fs = _make_mock_fs(bucket_exists)

    try:
        with (
            patch("app.core.database.engine", engine),
            patch("app.core.redis.redis_client", AsyncMock(return_value=redis)),
            patch("app.services.file_storage.file_storage", mock_fs),
            patch("asyncio.wait_for", new=_shortened_wait_for()),
        ):
            app = _make_app(s3_executor=executor)
            import httpx

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as ac:
                resp = await ac.get("/api/health/ready")
    finally:
        blocker.set()
        executor.shutdown(wait=True)

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert "timed out" in body["s3"], f"Expected S3 timeout, got: {body}"
    assert body["database"] == "ok", f"DB check should succeed: {body}"
    assert body["redis"] == "ok", f"Redis check should succeed: {body}"
    # The queued work item was cancelled before running — no thread pile-up.
    bucket_exists.assert_not_called()


@pytest.mark.asyncio
async def test_checks_run_concurrently():
    """DB, Redis, and S3 checks start at the same time (parallel gather).

    We verify this by making DB artificially wait until it receives a signal
    that the S3 check has already started.  If the checks were sequential, the
    S3 check could only start AFTER DB finished, and the test would deadlock.
    With parallel execution, both start immediately and the signal arrives
    before DB's wait times out.
    """
    s3_has_started = asyncio.Event()
    loop = asyncio.get_running_loop()

    # --- DB side: wait for S3 to start before finishing ---

    class SlowConnCtx:
        async def __aenter__(self):
            # Signal that DB has entered its check; wait for S3 to start.
            await asyncio.wait_for(s3_has_started.wait(), timeout=2.0)
            return AsyncMock(execute=AsyncMock())

        async def __aexit__(self, *_):
            return False

    slow_engine = MagicMock()
    slow_engine.connect = MagicMock(return_value=SlowConnCtx())

    # --- S3 side: signal from the worker thread, then complete ---
    def fast_bucket_exists(bucket: str) -> bool:
        loop.call_soon_threadsafe(s3_has_started.set)
        return True  # bucket exists

    redis = _make_mock_redis()
    mock_fs = _make_mock_fs(MagicMock(side_effect=fast_bucket_exists))

    with (
        patch("app.core.database.engine", slow_engine),
        patch("app.core.redis.redis_client", AsyncMock(return_value=redis)),
        patch("app.services.file_storage.file_storage", mock_fs),
    ):
        app = _make_app()
        import httpx

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await asyncio.wait_for(ac.get("/api/health/ready"), timeout=5.0)

    # If we reached here, the test did NOT deadlock → checks ran in parallel.
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
