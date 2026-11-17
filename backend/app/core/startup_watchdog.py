# SPDX-License-Identifier: Apache-2.0

"""Turn a startup that never completes into an ERROR naming what holds it.

A startup that waits forever fails nothing: the pod stays unready, the previous
release keeps serving, and at the default WARNING log level not one line says
why. ``s5459swi001`` held copilot-staging that way for 25 hours: a
migration building an index ``CONCURRENTLY`` waited on transactions that could
not end, with no timeout and nothing logged.

``watch_startup`` runs beside the background startup. Each time
``startup_watchdog_seconds`` pass without the startup settling, it logs at ERROR
the step in progress (``set_startup_step``) and the database sessions of the
startup locks: the Alembic connection, the holders and waiters of the migration
and bootstrap advisory locks, what each one waits on, and the sessions blocking
them (``pg_blocking_pids``), then where the startup task awaits
(``asyncio.format_call_graph``) for a step that is not a database wait. A
replica waiting for the migration lock polls for it, so it shows as
its step, "waiting for the migration lock", rather than as a lock waiter.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import settings
from app.core.startup_locks import BOOTSTRAP_LOCK_ID, MIGRATION_APPLICATION_NAME, MIGRATION_LOCK_ID
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

_DESCRIBE_TIMEOUT_SECONDS = 20.0
_QUERY_CHARS = 200
_MAX_BLOCKERS = 10
_CALL_GRAPH_DEPTH = 12

_step = "starting"


def set_startup_step(step: str) -> None:
    """Record the startup step in progress, named in the watchdog's report."""
    global _step
    _step = step


def current_startup_step() -> str:
    return _step


# The startup's own sessions, their SQL included: the Alembic connection and
# the holders and waiters of the startup locks (a single bigint advisory key
# sits in ``objid``, with ``classid`` 0 and ``objsubid`` 1).
_STARTUP_SESSIONS_SQL = """
WITH startup AS (
    SELECT pid FROM pg_stat_activity
    WHERE application_name = :application_name AND datname = current_database()
    UNION
    SELECT pid FROM pg_locks
    WHERE locktype = 'advisory' AND classid = 0 AND objsubid = 1
      AND objid = ANY(CAST(:lock_ids AS oid[]))
      AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
)
SELECT a.pid, a.application_name, a.state, a.wait_event_type, a.wait_event,
       CAST(EXTRACT(EPOCH FROM now() - a.query_start) AS integer) AS running_s,
       left(a.query, :query_chars) AS query,
       pg_blocking_pids(a.pid) AS blocked_by
FROM pg_stat_activity a JOIN startup USING (pid)
WHERE a.pid != pg_backend_pid()
ORDER BY a.pid
"""

# The sessions blocking them belong to anyone: who they are and for how long,
# never their SQL, whose literals the log aggregator has no business holding.
_BLOCKERS_SQL = """
SELECT pid, usename, application_name, state,
       CAST(EXTRACT(EPOCH FROM now() - xact_start) AS integer) AS xact_s
FROM pg_stat_activity
WHERE pid = ANY(:pids) AND datname = current_database()
ORDER BY xact_start NULLS LAST
LIMIT :limit
"""


async def describe_startup_sessions(engine: AsyncEngine | None = None) -> str:
    """The database sessions of the startup locks and their blockers, as text."""
    if engine is None:
        from app.core import database

        engine = database.engine
    async with engine.connect() as conn:
        sessions = (
            await conn.execute(
                text(_STARTUP_SESSIONS_SQL),
                {
                    "application_name": MIGRATION_APPLICATION_NAME,
                    "lock_ids": [MIGRATION_LOCK_ID, BOOTSTRAP_LOCK_ID],
                    "query_chars": _QUERY_CHARS,
                },
            )
        ).all()
        listed = {row.pid for row in sessions}
        blocker_pids = sorted({pid for row in sessions for pid in row.blocked_by or ()} - listed)
        blockers = []
        if blocker_pids:
            blockers = (
                await conn.execute(
                    text(_BLOCKERS_SQL),
                    {"pids": blocker_pids, "limit": _MAX_BLOCKERS},
                )
            ).all()

    if not sessions:
        return "No database session holds or waits for a startup lock."
    lines = ["Database sessions of the startup locks:"]
    for row in sessions:
        waiting = f", waiting on {row.wait_event_type}:{row.wait_event}" if row.wait_event_type else ""
        blocked = f", blocked by {list(row.blocked_by)}" if row.blocked_by else ""
        lines.append(
            f"  pid {row.pid} ({row.application_name or '-'}) {row.state}{waiting} "
            f"for {row.running_s}s{blocked}: {row.query}"
        )
    if blockers:
        lines.append("Blocking sessions:")
        lines.extend(
            f"  pid {row.pid} (role {row.usename}, {row.application_name or '-'}) {row.state}, "
            f"transaction open for {row.xact_s}s"
            for row in blockers
        )
    return "\n".join(lines)


async def watch_startup(
    settled: asyncio.Event,
    *,
    interval: float | None = None,
    describe: Callable[[], Awaitable[str]] = describe_startup_sessions,
    task: asyncio.Task | None = None,
) -> None:
    """Log at ERROR every ``interval`` seconds until ``settled`` is set.

    *task* is the startup task, whose await chain the report ends with.
    """
    if interval is None:
        interval = float(settings.startup_watchdog_seconds)
    if interval <= 0:
        return
    started = time.monotonic()
    while True:
        try:
            await asyncio.wait_for(settled.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        try:
            report = await asyncio.wait_for(describe(), timeout=_DESCRIBE_TIMEOUT_SECONDS)
        except Exception as exc:
            report = f"Could not read the database sessions: {format_exception_message(exc)}"
        if task is not None and not task.done():
            report += "\nThe startup awaits:\n" + asyncio.format_call_graph(task, limit=_CALL_GRAPH_DEPTH)
        logger.error(
            "Startup has not completed after %.0fs; step in progress: %s. The pod stays unready until it does.\n%s",
            time.monotonic() - started,
            _step,
            report,
        )
