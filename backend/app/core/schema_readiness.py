# SPDX-License-Identifier: Apache-2.0

"""Hold the SAQ worker until the database schema has caught up with its code.

Only the platform migrates (``run_migrations`` in its startup). A deploy rolls
the platform and the worker images together, and the worker used to start
processing at once: for as long as the platform took to migrate, every job ran
the NEW models against the OLD schema. A column added to a table a job reads
(``assignments.integration_element_enforcement``) made every assignment
run fail with ``UndefinedColumnError`` until its attempts were exhausted.

The worker therefore waits, before it touches any table, until
``alembic_version`` is at the head of the migration scripts it ships with:

- at head: ready;
- ahead (a revision this code does not know): ready - an older worker during a
  rollout or after a rollback, and migrations are additive for older code;
- behind (a known revision that is not the head, or the last pre-squash head
  the platform re-stamps and upgrades): wait;
- no ``alembic_version`` yet (the platform has not bootstrapped the database):
  wait.

Nothing is lost while it waits: the work is durable in the orchestration
tables and runs once the schema is ready. The wait fails OPEN on anything
about this process's own code it cannot read, so the gate is never the reason
a worker stops working.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core._pre_squash_revisions import PRE_SQUASH_FINAL_HEAD
from app.core.config import settings
from app.core.startup_locks import MIGRATION_LOCK_ID
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

SCHEMA_WAIT_POLL_SECONDS = 5.0
SCHEMA_WAIT_LOG_EVERY_SECONDS = 60.0


@dataclass(frozen=True)
class CodeRevisions:
    heads: frozenset[str]
    known: frozenset[str]


@dataclass(frozen=True)
class SchemaVerdict:
    ready: bool
    reason: str


def load_code_revisions() -> CodeRevisions:
    """The heads and every revision of the migration scripts this process ships."""
    from alembic.script import ScriptDirectory

    from app.core.migrations import _build_alembic_config

    script = ScriptDirectory.from_config(_build_alembic_config())
    return CodeRevisions(
        heads=frozenset(script.get_heads()),
        known=frozenset(rev.revision for rev in script.walk_revisions()),
    )


def schema_verdict(current: frozenset[str] | None, code: CodeRevisions) -> SchemaVerdict:
    """Whether a database stamped at ``current`` can serve this code."""
    if not current:
        return SchemaVerdict(False, "the database has no Alembic revision yet")
    unknown = current - code.known
    if unknown and PRE_SQUASH_FINAL_HEAD not in unknown:
        return SchemaVerdict(True, f"the database is ahead of this code ({', '.join(sorted(unknown))})")
    if code.heads <= current:
        return SchemaVerdict(True, "the database is at head")
    return SchemaVerdict(
        False,
        f"the database is at {', '.join(sorted(current))}, this code expects {', '.join(sorted(code.heads))}",
    )


async def read_database_revisions() -> frozenset[str] | None:
    """The revisions stamped in ``alembic_version``, ``None`` when it does not exist."""
    from app.core.database import engine

    async with engine.connect() as conn:
        exists = await conn.execute(text("SELECT to_regclass('alembic_version')"))
        if exists.scalar() is None:
            return None
        rows = await conn.execute(text("SELECT version_num FROM alembic_version"))
        return frozenset(row[0] for row in rows)


async def read_migration_in_progress(engine: AsyncEngine | None = None) -> bool:
    """Whether a platform process holds the migration lock right now."""
    if engine is None:
        from app.core import database

        engine = database.engine
    async with engine.connect() as conn:
        held = await conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND classid = 0 "
                "AND objsubid = 1 AND objid = :lock_id AND granted "
                "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()))"
            ),
            {"lock_id": MIGRATION_LOCK_ID},
        )
        return bool(held.scalar())


async def _past_the_delay(migration_in_progress: Callable[[], Awaitable[bool]]) -> tuple[int, str]:
    """How a wait past the watchdog delay is logged, by what holds it.

    A platform still migrating reports at ERROR itself when it is stuck (the
    startup watchdog), so the worker only says so; with no platform process
    migrating, nothing will move until one does.
    """
    try:
        migrating: bool | None = await migration_in_progress()
    except Exception:
        migrating = None
    if migrating:
        return logging.WARNING, "; a platform process is migrating it, and logs at ERROR if its startup is stuck"
    if migrating is None:
        return logging.ERROR, "; no job runs until the platform has migrated it"
    return (
        logging.ERROR,
        "; no platform process is migrating it - its startup failed, was rolled back or has not reached the "
        "migrations - and no job runs until a platform of this release does",
    )


async def wait_for_schema_head(
    *,
    stop: asyncio.Event | None = None,
    read_revisions: Callable[[], Awaitable[frozenset[str] | None]] = read_database_revisions,
    poll_seconds: float = SCHEMA_WAIT_POLL_SECONDS,
    log_every_seconds: float = SCHEMA_WAIT_LOG_EVERY_SECONDS,
    escalate_after_seconds: float | None = None,
    migration_in_progress: Callable[[], Awaitable[bool]] = read_migration_in_progress,
) -> bool:
    """``True`` once the database schema can serve this process's models,
    ``False`` when ``stop`` was set first (the worker was asked to shut down).

    The wait is logged at WARNING; past ``escalate_after_seconds``
    (``startup_watchdog_seconds`` by default, 0 never) it says what holds it,
    at ERROR unless a platform process is migrating.
    """
    if escalate_after_seconds is None:
        escalate_after_seconds = float(settings.startup_watchdog_seconds)
    try:
        code = await asyncio.to_thread(load_code_revisions)
    except Exception as exc:
        logger.warning(
            "Could not read this worker's migration scripts (%s); starting without waiting for the database schema",
            format_exception_message(exc),
        )
        return True

    started = time.monotonic()
    last_logged: float | None = None
    while True:
        if stop is not None and stop.is_set():
            return False
        try:
            verdict = schema_verdict(await read_revisions(), code)
        except Exception as exc:
            verdict = SchemaVerdict(False, f"the database could not be read ({format_exception_message(exc)})")
        if verdict.ready:
            if last_logged is not None:
                logger.info(
                    "Database schema ready after %.0fs (%s); worker starting",
                    time.monotonic() - started,
                    verdict.reason,
                )
            return True
        now = time.monotonic()
        if last_logged is None or now - last_logged >= log_every_seconds:
            level, cause = logging.WARNING, ""
            if 0 < escalate_after_seconds <= now - started:
                level, cause = await _past_the_delay(migration_in_progress)
            logger.log(
                level,
                "Waiting for the platform to migrate the database before processing jobs: %s (waited %.0fs)%s",
                verdict.reason,
                now - started,
                cause,
            )
            last_logged = now
        if stop is None:
            await asyncio.sleep(poll_seconds)
            continue
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
