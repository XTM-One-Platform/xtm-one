# SPDX-License-Identifier: Apache-2.0

"""Pre-flight dependency checks with retry.

Called during application startup to wait for PostgreSQL, Redis, and object storage
before proceeding with migrations and seeding.  Without this, the app
crashes immediately with a ConnectionRefusedError when infrastructure
pods are still starting (common in Kubernetes).
"""

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

MAX_RETRIES = 30
RETRY_DELAY = 2  # seconds
# One object-storage attempt: a role credential lookup plus the bucket check.
OBJECT_STORAGE_TIMEOUT = 10.0  # seconds
# The one in-flight object-storage probe: a timed-out attempt leaves it running
# and the next attempt awaits it again, so threads do not pile up.
_storage_probe: asyncio.Future | None = None


async def _check_postgres() -> None:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


async def _check_redis() -> None:
    """Ping Redis through the configured topology (standalone/sentinel).

    Using the shared factory ensures the readiness probe validates the same
    connection path the rest of the app will use (including Sentinel master
    discovery in HA setups).

    A hard 5-second timeout is enforced regardless of ``redis_socket_timeout``
    so the readiness endpoint always fails fast (important for Kubernetes).
    """
    from app.core.redis import redis_client

    client = await redis_client()
    await asyncio.wait_for(client.ping(), timeout=5.0)


async def _check_object_storage() -> None:
    """Reach the object store through the S3 API, as the app itself does.

    Any S3 answer, an error response included, means the store is up: only a
    transport failure or a timeout retries.
    """
    from minio.error import S3Error

    from app.services.file_storage import file_storage

    global _storage_probe
    if _storage_probe is None:
        _storage_probe = asyncio.ensure_future(asyncio.to_thread(lambda: file_storage.client))
        # Retrieved even when the gate gives up before the probe finishes.
        _storage_probe.add_done_callback(lambda probe: probe.cancelled() or probe.exception())
    probe = _storage_probe
    # ``wait``, not ``wait_for`` + ``shield``: a timeout leaves the probe untouched
    # for the next attempt, and nothing logs its late failure twice.
    if not (await asyncio.wait({probe}, timeout=OBJECT_STORAGE_TIMEOUT))[0]:
        raise TimeoutError(f"no answer within {OBJECT_STORAGE_TIMEOUT:g}s")
    _storage_probe = None
    try:
        probe.result()
    except S3Error as exc:
        logger.warning(
            "Object storage answered but refused the bucket check (%s): "
            "file operations fail until the credentials, region or bucket are fixed",
            exc.code,
        )


async def _wait_for(name: str, check_fn) -> None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await check_fn()
            logger.info("%s is ready", name)
            return
        except Exception as exc:
            logger.warning(
                "%s not ready (attempt %d/%d): %s",
                name,
                attempt,
                MAX_RETRIES,
                format_exception_message(exc),
            )
            if attempt == MAX_RETRIES:
                raise RuntimeError(
                    f"{name} not reachable after {MAX_RETRIES} attempts — "
                    f"refusing to start. Last error: {exc}"
                ) from exc
            await asyncio.sleep(RETRY_DELAY)


async def _validate_pool_config() -> None:
    """Warn if the SQLAlchemy pool configuration risks exhausting PostgreSQL slots.

    Queries ``max_connections`` from the server and compares it against
    ``(pool_size + max_overflow) * web_workers``.  Logs a warning when the
    theoretical maximum exceeds 80 % of available slots, and an error when
    it exceeds 100 %.
    """
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            row = await conn.execute(text("SHOW max_connections"))
            pg_max = int(row.scalar())
    except Exception as exc:
        logger.warning("Could not read max_connections from PostgreSQL: %s", format_exception_message(exc))
        return
    finally:
        await engine.dispose()

    reserved = 3  # superuser_reserved_connections default
    available = pg_max - reserved

    per_process = settings.db_pool_size + settings.db_max_overflow
    workers = max(settings.web_workers, 1)
    # Each SAQ worker also gets its own pool — assume at least 1.
    estimated_total = per_process * (workers + 1)

    logger.info(
        "Connection pool: %d pool + %d overflow per process, "
        "%d web workers → %d estimated max connections "
        "(PostgreSQL max_connections=%d, usable=%d)",
        settings.db_pool_size,
        settings.db_max_overflow,
        workers,
        estimated_total,
        pg_max,
        available,
    )

    if estimated_total > available:
        logger.error(
            "POOL OVERCOMMIT: estimated max connections (%d) exceeds "
            "PostgreSQL usable slots (%d).  This WILL cause "
            "'TooManyConnectionsError' under load.  "
            "Reduce DB_POOL_SIZE/DB_MAX_OVERFLOW, reduce WEB_WORKERS, "
            "or increase PostgreSQL max_connections.  "
            "Formula: (DB_POOL_SIZE + DB_MAX_OVERFLOW) "
            "× (WEB_WORKERS + saq_workers) < max_connections - %d",
            estimated_total,
            available,
            reserved,
        )
    elif estimated_total > int(available * 0.8):
        logger.warning(
            "Connection pool uses >80%% of PostgreSQL slots "
            "(%d / %d).  Consider lowering DB_POOL_SIZE or "
            "increasing max_connections for headroom.",
            estimated_total,
            available,
        )


async def wait_for_dependencies() -> None:
    """Block until PostgreSQL, Redis, and object storage are reachable."""
    logger.info("Checking infrastructure dependencies...")
    await _wait_for("PostgreSQL", _check_postgres)
    await _wait_for("Redis", _check_redis)
    await _wait_for("Object storage", _check_object_storage)
    logger.info("All dependencies ready")

    await _validate_pool_config()
