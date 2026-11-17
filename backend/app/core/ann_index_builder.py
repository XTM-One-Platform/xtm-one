# SPDX-License-Identifier: Apache-2.0

"""Post-startup background builder for deferred (HNSW) ANN indexes.

Why this exists (liveness probe crash loop)
---------------------------------------------------------
An HNSW build on a populated ``embeddings`` table takes minutes.  When the
build ran synchronously inside the FastAPI lifespan (the original
``c9d3e7f1a5b2`` migration body and, for stamped-fresh installs, the boot
reconciler in ``app/core/migrations.py``), uvicorn had not yet started
accepting connections — so the Kubernetes liveness probe on ``/api/health``
got connection-refused for the whole build window, hit its failure threshold
(~4 probes x 3 min), and killed the container mid-build.  Every restart
re-entered the same multi-minute build, producing a permanent crash loop with
PostgreSQL connection drops (08006), repeated DDL waves, and worker statement
timeouts on ``embeddings`` writes.

Worse, the kill itself created a catalog race: a SIGKILLed pod's PostgreSQL
backend running ``CREATE INDEX`` does not notice the dead client until the
build completes, so it keeps building as an orphan and eventually COMMITS
(each index runs in its own autocommit block).  The migration advisory lock
was held on a DIFFERENT (idle) connection that died with the pod, so the
restarted pod immediately re-entered the migration and started a second build
of the same index — and whichever build committed second failed with
``23505 duplicate key value violates unique constraint
"pg_class_relname_nsp_index"`` even though the statement said
``IF NOT EXISTS`` (the existence check ran before the orphan committed).

The fix
-------
ANN (HNSW) index builds are **deferred out of the startup path** entirely:

* Fresh installs still get the indexes instantly via ``create_all`` (the
  table is empty, the build is O(1)).
* The boot reconciler (``index_maintenance.reconcile_model_indexes``) skips
  indexes whose access method is in ``DEFERRED_INDEX_ACCESS_METHODS`` so the
  blocking lifespan never waits on an HNSW build.
* The ``c9d3e7f1a5b2`` migration body no longer builds anything — it logs and
  returns immediately, so ``alembic upgrade head`` stays fast.
* This module builds whatever is still missing in an ``asyncio`` background
  task started from the lifespan AFTER migrations complete.  The server is
  already accepting connections (liveness green) while the builds run.

Safety properties
-----------------
* **Advisory lock on the SAME connection that runs the builds.**  Unlike the
  migration lock (held on a separate idle connection), this session-level
  lock lives on the backend executing ``CREATE INDEX``.  If the pod dies
  mid-build, the orphaned backend keeps holding the lock until it actually
  terminates — so a restarted pod (or a parallel uvicorn worker / second
  replica) blocks on ``pg_try_advisory_lock`` polling instead of starting a
  duplicate build.  This is what structurally eliminates the 23505 race.
* **Non-blocking lock acquisition with polling.**  ``pg_try_advisory_lock``
  + a sleep loop (instead of a blocking ``pg_advisory_lock``) so a losing
  worker does not pin a pooled connection for the whole build; the
  connection is returned to the pool between polls.
* **Duplicate-key tolerance.**  Even with the lock, a residual race against
  an orphaned pre-fix build is possible, so a 23505 / 42P07 from
  ``CREATE INDEX`` is treated as "another build won" and skipped.
* **Plain in-place builds, never CONCURRENTLY** — same rationale as
  ``index_maintenance``: a concurrent build hangs forever behind another
  live pod's idle-in-transaction pool sessions during a rolling deploy.
* **Invalid-leftover recovery.**  ``CREATE INDEX IF NOT EXISTS`` checks
  existence, not validity, so an INVALID leftover is dropped first.
* **Failure degrades, never crashes.**  If a build fails, semantic searches
  simply stay on the exact-scan path (the pre-index behaviour) until the
  next boot retries; the error is logged loudly but the app keeps serving.
* **Cancellation-safe cleanup.**  If the post-build ``RESET
  statement_timeout`` / ``pg_advisory_unlock`` cleanup cannot run (the task
  was cancelled during shutdown, or the connection broke mid-build), the
  connection is synchronously terminated instead of being returned to the
  pool — so a session with a lifted timeout or a held advisory lock can
  never be checked out by an unrelated request.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.schema import CreateIndex

from app.core.index_maintenance import (
    PG_DIALECT,
    deferred_model_indexes,
    existing_index_names,
    idempotent_if_not_exists,
    invalid_index_names,
    is_duplicate_index_error,
)

logger = logging.getLogger(__name__)

# Session-level advisory lock for the ANN builds.  MUST be distinct from the
# bootstrap/migration lock (``BOOTSTRAP_LOCK_ID = 294838`` in
# app/core/startup_locks.py): both use the single-key ``pg_advisory_lock(bigint)``
# space, so sharing the id let a multi-minute HNSW build (spawned from main.py
# before the bootstrap lock is taken) block readiness on every booting pod
# during a rolling deploy (the stall the deferral was meant to fix).
# Critically, it is taken on the SAME connection that executes the CREATE INDEX
# statements, so an orphaned build's backend keeps holding it until that backend
# terminates.
ANN_INDEX_BUILD_LOCK_ID = 294839

# How long a losing instance waits between lock polls while another instance
# (or an orphaned backend from a killed pod) is building.
LOCK_POLL_INTERVAL_SECONDS = 30.0

_HALFVEC_PROBE_SQL = "SELECT 1 FROM pg_type WHERE typname = 'halfvec' LIMIT 1"


def _build_ann_indexes_sync(conn: Connection) -> list[str]:
    """Create every missing deferred (HNSW) model index. Returns created names.

    Runs on the synchronous side of an AUTOCOMMIT async connection (via
    ``run_sync``) so each plain ``CREATE INDEX IF NOT EXISTS`` commits — and
    releases its SHARE lock on ``embeddings`` — as soon as that index is
    built.  The caller has already acquired the advisory lock on this same
    connection and lifted ``statement_timeout``.
    """
    halfvec_ok = conn.execute(text(_HALFVEC_PROBE_SQL)).first() is not None
    existing = existing_index_names(conn)
    invalid = invalid_index_names(conn)

    created: list[str] = []
    for index in deferred_model_indexes():
        if not index.name or index.table is None:
            continue
        ddl = idempotent_if_not_exists(
            str(CreateIndex(index).compile(dialect=PG_DIALECT)).strip()
        )
        if "halfvec" in ddl.lower() and not halfvec_ok:
            # Out-of-support pgvector (< 0.7): skip rather than fail — the
            # affected dimension stays on exact scan until the server is
            # upgraded; the next boot retries.
            logger.warning(
                "pgvector halfvec type not available (pgvector < 0.7); "
                "skipping %s — searches on its dimension remain on exact "
                "scan until pgvector is upgraded",
                index.name,
            )
            continue

        is_invalid = index.name in invalid
        if index.name in existing and not is_invalid:
            continue
        # Drop a leftover INVALID build first — CREATE ... IF NOT EXISTS
        # checks existence, not validity, and would skip it forever.  Plain
        # DROP (not CONCURRENTLY) for the rolling-deploy reason above.
        if is_invalid:
            conn.execute(text(f'DROP INDEX IF EXISTS "{index.name}"'))

        logger.info("Building ANN index %s in the background ...", index.name)
        try:
            conn.execute(text(ddl))
        except Exception as exc:
            if is_duplicate_index_error(exc):
                logger.info(
                    "ANN index %s was built concurrently by another "
                    "instance — skipping",
                    index.name,
                )
                continue
            raise
        created.append(index.name)
    return created


def _terminate_build_connection(conn) -> None:
    """Best-effort, fully synchronous termination of the build connection.

    Used only when the normal ``RESET statement_timeout`` +
    ``pg_advisory_unlock`` cleanup could not run — e.g. the build task was
    cancelled during shutdown and the cleanup ``await`` itself was
    interrupted by the pending ``CancelledError``, or the connection broke
    mid-build.  Returning such a connection to the pool would leak a session
    with ``statement_timeout = 0`` (session-level ``SET`` survives the
    pool's rollback-on-return) and a still-held advisory lock onto whatever
    request checks it out next.

    Closing the socket guarantees neither leaks: the timeout override dies
    with the session, and the advisory lock stays tied to the dying backend
    (released when PostgreSQL notices the disconnect, or when an in-flight
    orphaned ``CREATE INDEX`` finishes — exactly the orphan semantics this
    module is designed around).  asyncpg's ``terminate()`` just closes the
    socket synchronously, so this helper has no ``await`` and cannot itself
    be cancelled.  The pool discards the dead connection at check-in.
    """
    try:
        raw = conn.sync_connection.connection.dbapi_connection
        terminate = getattr(
            getattr(raw, "driver_connection", None), "terminate", None
        )
        if callable(terminate):
            terminate()
    except Exception:  # pragma: no cover - defensive
        logger.debug(
            "Could not terminate the ANN build connection", exc_info=True
        )


async def _try_build_once(engine) -> bool:
    """One lock-attempt + build pass. Returns ``True`` when done (built or
    nothing to do), ``False`` when another instance holds the build lock."""
    async with engine.connect() as conn:
        autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
        acquired = (
            await autocommit.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": ANN_INDEX_BUILD_LOCK_ID},
            )
        ).scalar()
        if not acquired:
            return False
        build_error: BaseException | None = None
        try:
            # An HNSW build on a populated table legitimately exceeds the
            # 30 s per-connection statement_timeout (app/core/database.py).
            await autocommit.execute(text("SET statement_timeout = 0"))
            created = await autocommit.run_sync(_build_ann_indexes_sync)
        except BaseException as exc:
            build_error = exc
            raise
        finally:
            try:
                # RESET restores the session default from the connection's
                # startup settings (30 s) before the pooled connection is
                # reused; then release the advisory lock.
                await autocommit.execute(text("RESET statement_timeout"))
                await autocommit.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": ANN_INDEX_BUILD_LOCK_ID},
                )
            except BaseException as cleanup_exc:
                # The cleanup itself failed — most likely a CancelledError
                # delivered at its first await during shutdown, or a
                # connection broken by the failed build.  Kill the
                # connection so the lifted timeout and the advisory lock
                # die with it instead of leaking into the pool.
                _terminate_build_connection(conn)
                if isinstance(build_error, asyncio.CancelledError):
                    # Preserve cancellation semantics: the caller must see
                    # the original CancelledError, not the cleanup failure
                    # (otherwise build_ann_indexes would swallow shutdown
                    # as a generic build failure).
                    raise build_error from cleanup_exc
                if isinstance(cleanup_exc, asyncio.CancelledError):
                    # Cancellation arrived DURING the cleanup (after a
                    # plain build failure): cancellation always wins so
                    # shutdown is never reported as a build failure; the
                    # build error stays attached as __context__.
                    raise
                if build_error is not None:
                    # Both the build and the cleanup failed with ordinary
                    # errors: the build failure is the actionable root
                    # cause, so re-raise IT (the cleanup failure stays
                    # chained as __cause__) instead of burying it under a
                    # secondary "connection is closed"-style error.
                    raise build_error from cleanup_exc
                raise
    if created:
        logger.info(
            "Background ANN index build complete: %s", ", ".join(created)
        )
    else:
        logger.info("ANN indexes already present — background build is a no-op")
    return True


async def build_ann_indexes() -> None:
    """Build any missing deferred (HNSW) model index in the background.

    Started as an ``asyncio`` task from the lifespan AFTER migrations, so the
    server is already accepting connections — the liveness probe stays green
    for the whole build window.  Polls the advisory lock so concurrent
    instances (other uvicorn workers, other replicas, or an orphaned backend
    from a killed pod) serialize instead of double-building.

    Never raises (except cancellation): a failed build leaves semantic
    search on the exact-scan path — the pre-index behaviour — and the next
    boot retries.
    """
    from app.core.database import engine

    if engine.dialect.name != "postgresql":
        return
    if not deferred_model_indexes():
        return

    try:
        while not await _try_build_once(engine):
            logger.info(
                "Another instance is building the ANN indexes — "
                "re-checking in %.0fs",
                LOCK_POLL_INTERVAL_SECONDS,
            )
            await asyncio.sleep(LOCK_POLL_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        # Shutdown while a build is in flight: asyncpg sends a server-side
        # cancel, the aborted plain CREATE INDEX rolls back cleanly, and the
        # next boot retries.
        raise
    except Exception:
        logger.exception(
            "Background ANN index build failed — semantic searches stay on "
            "the exact-scan path until the next boot retries"
        )
