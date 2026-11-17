# SPDX-License-Identifier: Apache-2.0

"""Database schema management: create tables + run Alembic migrations."""

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from alembic import command
from app.core._pre_squash_revisions import (
    BASELINE_REVISION,
    PRE_SQUASH_FINAL_HEAD,
    PRE_SQUASH_REVISIONS,
)
from app.core.config import settings
from app.core.database import Base, engine
from app.core.db_availability import sqlstates_in_chain
from app.core.migration_ddl import set_lock_timeout
from app.core.startup_locks import BOOTSTRAP_LOCK_ID, MIGRATION_APPLICATION_NAME, MIGRATION_LOCK_ID
from app.core.startup_watchdog import set_startup_step
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

# Root of the backend package (where alembic/ lives)
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI_PATH = _BACKEND_DIR / "alembic" / "alembic.ini"
_ALEMBIC_SCRIPT_DIR = _BACKEND_DIR / "alembic"


def alembic_server_settings() -> dict[str, str]:
    """Session defaults of the Alembic connection (``alembic/env.py``).

    Without them a migration statement inherits the server's unbounded
    defaults, and one waiting on a lock or a snapshot holds the rollout with
    nothing logged. Startup parameters rather than a ``SET``: a
    ``RESET`` in a migration body (``create_index_in_place``) comes back to
    these bounds, not to the server's. They also take precedence over a
    timeout set on the role or the database; a setting at 0 is left out, so
    that one applies.
    """
    bounds = {
        "lock_timeout": settings.migration_lock_timeout_seconds,
        "statement_timeout": settings.migration_statement_timeout_seconds,
    }
    return {
        "application_name": MIGRATION_APPLICATION_NAME,
        **{name: str(seconds * 1000) for name, seconds in bounds.items() if seconds},
    }


# Pause between two ``pg_try_advisory_lock`` attempts of a startup lock waiter.
_ADVISORY_LOCK_POLL_SECONDS = 0.25


async def wait_for_advisory_lock(
    conn, lock_id: int, name: str, *, timeout: float | None = None
) -> None:
    """Take the session-level advisory lock *lock_id* by polling for it.

    A session blocked in ``SELECT pg_advisory_lock(...)`` runs a statement for
    the whole wait, and a running statement holds a snapshot. Anything waiting
    for older snapshots, such as a ``CREATE INDEX CONCURRENTLY`` run by the
    lock holder, then waits for the waiter, which waits for the holder:
    PostgreSQL cannot see that deadlock, one side being on an advisory lock and
    the other on a snapshot. Each attempt here is an instant
    ``pg_try_advisory_lock`` and a miss ends its transaction before the sleep,
    so a waiter holds no snapshot and no open transaction between attempts.

    On success the lock is held inside the transaction of the last attempt,
    which the caller commits. ``timeout`` (seconds) bounds the wait with a
    :class:`TimeoutError` naming *name*; ``None`` waits as long as the holder
    needs.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        got = await conn.execute(
            text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": lock_id}
        )
        if got.scalar():
            return
        await conn.rollback()
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(
                f"timed out waiting for the {name} lock after {timeout}s"
            )
        await asyncio.sleep(_ADVISORY_LOCK_POLL_SECONDS)


async def _release_startup_lock(conn, lock_id: int, name: str, *, held: bool = True) -> None:
    """Unlock a startup advisory lock and restore the connection's ``statement_timeout``.

    The rollback clears an aborted transaction (session-level locks survive it).
    ``RESET`` restores the 30 s guard set at connect time (app/core/database.py)
    before the connection goes back to the pool.
    """
    try:
        await conn.rollback()
        if held:
            await conn.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": lock_id})
        await conn.execute(text("RESET statement_timeout"))
        await conn.commit()
    except Exception:
        # The connection is gone: PostgreSQL releases the lock when it ends
        # the session. Raising would hide the error that ended the locked block.
        if not conn.invalidated:
            raise
        logger.warning("The %s lock's connection was lost; PostgreSQL releases the lock with its session", name)


@asynccontextmanager
async def bootstrap_lock(wait_timeout: float | None = None):
    """Session-level Postgres advisory lock for the startup bootstrap writes.

    ``wait_timeout`` bounds the wait for request-path callers, raising
    :class:`TimeoutError`. Boot leaves it ``None`` and blocks as long as the
    winner needs; an admin ``PUT`` cannot, because a replica seeding through a
    rolling restart holds this lock past the ingress timeout.

    Migrations are serialized across uvicorn workers/replicas via
    ``MIGRATION_LOCK_ID``, but everything that runs AFTER ``run_migrations``
    (built-in seeding, env-provider provisioning, admin upsert, defaults
    sync) used to run concurrently in every worker.  All of those passes are
    non-atomic check-then-insert flows against unique-constrained tables
    (``intents.name``, ``agents.slug``, ``users.email``, ...), so two
    workers booting together could both pass the existence check and the
    loser crashed the whole background startup with a
    ``UniqueViolationError`` — leaving its pod alive but permanently
    unready (``ix_intents_name`` / ``global.assistant``).

    Same shape as the migration lock: a dedicated connection holds a
    session-level ``pg_advisory_lock`` for the duration of the block, with
    ``statement_timeout`` disabled so waiters can block as long as the
    winner needs.  The winner seeds; the losers then re-run the idempotent
    sync against the now-populated tables as a no-op.
    """
    async with engine.connect() as conn:
        await conn.execute(text("SET statement_timeout = 0"))
        await conn.commit()

        # The lock acquisition itself lives INSIDE the try block: if it
        # fails (cancelled wait, network error), the finally below still
        # RESETs the session-level ``statement_timeout = 0`` before the
        # pooled connection is returned — otherwise the disabled timeout
        # would leak to whatever request checks the connection out next.
        locked = False
        try:
            if wait_timeout is None:
                await conn.execute(
                    text("SELECT pg_advisory_lock(:lock_id)"),
                    {"lock_id": BOOTSTRAP_LOCK_ID},
                )
            else:
                # Poll rather than wrapping the blocking form in
                # ``asyncio.wait_for``: cancelling mid-wait would leave the
                # server-side lock wait in place on a connection that is
                # about to go back to the pool.
                await wait_for_advisory_lock(
                    conn, BOOTSTRAP_LOCK_ID, "bootstrap", timeout=wait_timeout
                )
            # The lock is held the moment the SELECT returns — track it
            # BEFORE the commit so a failed commit still unlocks in the
            # finally (a pooled connection silently holding the advisory
            # lock would block every future boot).
            locked = True
            # Commit to close the implicit transaction; session-level lock is kept.
            await conn.commit()
            yield
        finally:
            await _release_startup_lock(conn, BOOTSTRAP_LOCK_ID, "bootstrap", held=locked)


async def run_migrations() -> None:
    """Ensure database schema is up-to-date.

    1. The current revision is read from ``alembic_version`` FIRST, before any
       schema mutation.  On an **existing** DB a pre-flight
       ``_reconcile_pre_squash_revision`` step re-stamps a database parked at the known
       last pre-squash head to the baseline; ANY other unrecognised marker is
       fail-closed (raises) **before** ``create_all`` so a misplaced DB is left
       untouched for the operator to fix.
    2. ``Base.metadata.create_all`` then creates any missing tables (no-op when
       tables already exist thanks to ``checkfirst=True``), and ``alembic
       upgrade head`` applies pending migrations on an existing DB.
    3. On a **fresh** DB (no ``alembic_version``), we ``stamp head`` so
       Alembic knows the schema is already current.
    4. After Alembic, a safety-net pass adds any model-defined columns
       that are still missing from the DB (covers the edge case where
       ``stamp head`` was called on a DB whose tables pre-date a column
       addition).

    Uses a PostgreSQL advisory lock to prevent concurrent migration
    attempts when running with multiple uvicorn workers (``--workers N``).
    Only one worker runs Alembic at a time; the rest poll for the lock
    (``wait_for_advisory_lock``, holding no snapshot meanwhile) and then
    find nothing left to do (``upgrade head`` is a no-op).

    Raises ``RuntimeError`` if Alembic migration execution fails so the
    application refuses to start with a stale schema.
    """
    # Import models so SQLAlchemy metadata is populated (side-effect imports).
    # All model registrations are centralized in app/models/__init__.py.
    import app.models  # noqa: F401

    # Acquire a PostgreSQL session-level advisory lock to serialize ALL schema
    # operations (create_all + Alembic) across workers.  Without this,
    # concurrent create_all calls race on CREATE TABLE / CREATE TYPE and crash
    # with UniqueViolationError on pg_type_typname_nsp_index.
    async with engine.connect() as conn:
        # Disable statement_timeout for the migration connection so workers
        # can wait as long as needed for the advisory lock.  Production DBs
        # often set a statement_timeout that is shorter than the time needed
        # for the lock-holding worker to finish all migrations.
        await conn.execute(text("SET statement_timeout = 0"))
        await conn.commit()

        set_startup_step("waiting for the migration lock")
        # Polled, never the blocking ``pg_advisory_lock``: a session blocked
        # in that SELECT holds a snapshot for the whole wait, so a
        # grandfathered ``CREATE INDEX CONCURRENTLY`` run by the lock holder
        # would wait for this waiter, which waits for the holder.
        await wait_for_advisory_lock(conn, MIGRATION_LOCK_ID, "migration")
        # Commit to close the implicit transaction; session-level lock is kept.
        await conn.commit()

        try:
            # Ensure pgvector extension exists before any DDL that uses VECTOR columns.
            # CNPG postInitSQL should handle this, but may not run if the cluster
            # bootstrap was interrupted (e.g. by a blocking barman-cloud plugin).
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.commit()
            logger.info("pgvector extension ensured")

            # Determine whether this is an existing or fresh database FIRST,
            # before any schema mutation.  On an unrecognised (pre-squash)
            # revision ``_reconcile_pre_squash_revision`` fails closed (raises) and we
            # want that to happen with ZERO side effects — no ``create_all``,
            # no stamping — so a misplaced DB is left exactly as it was for the
            # operator to fix.
            current_rev: str | None = None
            try:
                row = await conn.execute(
                    text("SELECT version_num FROM alembic_version LIMIT 1")
                )
                current_rev = row.scalar_one_or_none()
                await conn.commit()
            except Exception as exc:
                # Only a missing table means a fresh database: a connection a
                # failover dropped here must not stamp head over an existing schema.
                if "42P01" not in sqlstates_in_chain(exc):
                    raise
                await conn.rollback()  # reset transaction state after error
                logger.debug("alembic_version table not found (fresh DB): %s", format_exception_message(exc))

            if current_rev is not None:
                # Auto-heal a DB stamped at the single last pre-squash head
                # (``PRE_SQUASH_FINAL_HEAD`` in ``_pre_squash_revisions.py``)
                # BEFORE attempting the upgrade, so fully-upgraded installs
                # cross the squash without a manual ``alembic stamp --purge``.
                # Any other unrecognised marker is fail-closed (raises here,
                # before ``create_all``) rather than silently stamped.
                await _reconcile_pre_squash_revision(current_rev)

            set_startup_step("creating the missing tables")
            await _create_missing_tables()

            set_startup_step("applying the database migrations")
            if current_rev is not None:
                await _apply_pending_migrations()
            else:
                await _stamp_head()

            # Safety net: ensure every model-defined column exists in the DB.
            # create_all only creates NEW tables (skips existing ones even if
            # new columns were added to the model), and stamp-head marks the
            # DB at the latest Alembic revision without running DDL.  Together
            # this can leave the DB stamped at head but missing columns that
            # were added after the table was first created.
            set_startup_step("reconciling the model columns, schema extras and indexes")
            await _ensure_model_columns()

            # Schema artifacts that ``Base.metadata.create_all`` does NOT
            # manage (Postgres triggers, functional indexes, etc.).  On a
            # FRESH database we ``create_all`` + ``stamp head`` and never
            # execute the migration body — so any DDL the migration emits
            # for these artifacts would be missing.  Running an idempotent
            # bootstrap pass here makes both paths converge: fresh installs
            # get the artifact created on first boot, existing installs hit
            # a CREATE-OR-REPLACE / IF-NOT-EXISTS no-op.  Runs INSIDE the
            # advisory lock so concurrent uvicorn workers serialize on it
            # (a parallel ``CREATE TRIGGER`` race would otherwise raise
            # "trigger already exists" on the loser).
            await _ensure_schema_extras()

            # Generic safety net: create ANY model-declared index that is
            # missing from the live DB.  ``create_all`` only builds indexes
            # on tables it CREATES (it skips existing tables entirely), and
            # the stamp-head path never runs migration bodies — so an index
            # added in a migration but missing from the model (or added to
            # the model after a table was first created) would otherwise be
            # absent forever on fresh / stamped-fresh installs.  This pass
            # reconciles the model's index set against the database on every
            # boot, creating only what's missing (a plain in-place ``CREATE
            # INDEX IF NOT EXISTS``, column-set guarded so it never duplicates a
            # legacy-named index that survived a table rename).  A plain build —
            # NOT ``CONCURRENTLY`` — because a concurrent build hangs forever
            # behind another live pod's idle-in-transaction sessions during a
            # rolling deploy, whereas a plain build takes a brief SHARE lock
            # (compatible with concurrent reads) and completes.  Runs inside the
            # advisory lock for the same worker-serialization reason as
            # ``_ensure_schema_extras``.
            await _ensure_model_indexes()
        finally:
            # Release advisory lock so the next worker can proceed.
            await _release_startup_lock(conn, MIGRATION_LOCK_ID, "migration")

    # Dispose the connection pool so subsequent queries use fresh connections.
    # Alembic runs through its own sync connection stack, so pooled asyncpg
    # connections may have stale prepared-statement caches that don't reflect
    # newly-added columns.
    await engine.dispose()


def _create_all_under_belt(sync_conn) -> None:
    set_lock_timeout(sync_conn)
    Base.metadata.create_all(sync_conn)


async def _create_missing_tables() -> None:
    """``Base.metadata.create_all`` under the migrations' own lock safety.

    On an existing install this runs BEFORE the pending migration bodies, so
    the table of a new model is created here and its migration's probe then
    finds it: the migration's ``set_lock_timeout`` belt never covers it. A
    table with a foreign key to an existing one takes a SHARE ROW EXCLUSIVE
    lock on the referenced table, which waits for any open transaction that
    wrote to it and makes every later writer of that table wait behind it
    (dev-docs/reference/migrations-safety.md, rule 5). Without the
    belt that wait lasted the connection's 30 s ``statement_timeout``, whose
    57014 (like a 55P03) reaches here as a plain ``DBAPIError``, not one of
    the benign errors below, and failed the startup for good.

    So the transaction runs under the same belt as a migration body, and a
    lock failure is retried like ``_apply_pending_migrations`` retries one:
    the transaction rolled back whole, so a retry starts from the same
    catalog. Every other outcome is handled as before.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, _MIGRATION_MAX_ATTEMPTS + 1):
        try:
            async with engine.begin() as ddl_conn:
                await ddl_conn.run_sync(_create_all_under_belt)
        except Exception as exc:
            if not _is_retryable_ddl_lock_error(exc):
                if not isinstance(exc, (IntegrityError, OperationalError)):
                    raise
                logger.warning(
                    "create_all raised %s (benign in multi-worker setup, "
                    "table likely created by another worker or migration): %s",
                    type(exc).__name__,
                    format_exception_message(exc),
                )
                return
            last_exc = exc
            if attempt >= _MIGRATION_MAX_ATTEMPTS:
                break
            delay = min(
                _MIGRATION_RETRY_BASE_DELAY * (2 ** (attempt - 1)),
                _MIGRATION_RETRY_MAX_DELAY,
            )
            logger.warning(
                "create_all hit transient lock contention (attempt %d/%d): "
                "%s — retrying in %.1fs",
                attempt,
                _MIGRATION_MAX_ATTEMPTS,
                format_exception_message(exc),
                delay,
            )
            await asyncio.sleep(delay)
        else:
            logger.info("Database tables ensured")
            return

    logger.error(
        "create_all still failing after %d attempts under sustained lock "
        "contention: %s",
        _MIGRATION_MAX_ATTEMPTS,
        last_exc,
        exc_info=last_exc,
    )
    raise RuntimeError(
        f"Creating the new tables failed after {_MIGRATION_MAX_ATTEMPTS} "
        "attempts due to sustained lock contention — refusing to start with "
        "a stale schema. Check for long-running transactions that wrote to "
        "the tables the new ones reference, and restart."
    ) from last_exc


async def _ensure_model_columns() -> None:
    """Add any model-defined columns that are missing from the database.

    Uses ``ALTER TABLE ADD COLUMN IF NOT EXISTS`` (PostgreSQL 9.6+).
    Columns are added as nullable without defaults or constraints — the
    proper migration handles those once Alembic state is corrected.
    """

    def _sync_check_and_add(sync_conn):
        inspector = sa.inspect(sync_conn)
        tables_in_db = set(inspector.get_table_names())
        added = []
        for table in Base.metadata.sorted_tables:
            if table.name not in tables_in_db:
                continue
            existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing_cols:
                    continue
                try:
                    col_type = col.type.compile(dialect=sync_conn.dialect)
                except Exception:
                    logger.warning(
                        "Skipping safety-net migration for column %s.%s because its type could not be compiled",
                        table.name,
                        col.name,
                        exc_info=True,
                    )
                    continue
                sync_conn.execute(
                    sa.text(
                        f'ALTER TABLE "{table.name}" '
                        f'ADD COLUMN IF NOT EXISTS "{col.name}" {col_type}'
                    )
                )
                added.append(f"{table.name}.{col.name}")
        return added

    async with engine.begin() as fix_conn:
        added = await fix_conn.run_sync(_sync_check_and_add)
    if added:
        logger.warning(
            "Added %d missing column(s) as safety net: %s",
            len(added),
            ", ".join(added),
        )


def _build_alembic_config() -> Config:
    """Build an Alembic config with absolute paths for in-process execution."""
    config = Config(str(_ALEMBIC_INI_PATH))
    config.set_main_option("script_location", str(_ALEMBIC_SCRIPT_DIR))
    config.attributes["configure_logger"] = False
    return config


# Retry policy for transient lock contention during ``alembic upgrade head``.
#
# Migration bodies set a short ``SET LOCAL lock_timeout`` safety belt before
# lock-taking DDL (see ``app/core/migration_ddl.py``), so a DDL statement that
# queues behind another session's lock fails fast with SQLSTATE 55P03
# ("canceling statement due to lock timeout") instead of wedging the lock
# queue.  Before this retry existed, that fail-fast was TERMINAL: one fired
# belt during a rolling deploy (previous-version pods hold long-lived
# transactions with ``ACCESS SHARE`` locks on the target table) hard-failed
# startup and took production down — the 2026-07-05 ``platform_settings``
# outage.
#
# Retrying is safe because every migration is required to be idempotent
# (fresh installs never run the body; crashed replicas may re-run it; CI's
# "Idempotent re-run" job enforces it): a failed ``upgrade head`` rolls back
# and the retry re-runs the pending bodies from the same ``alembic_version``.
# Exponential backoff (2 → 4 → 8 → 16 → 30 s) rides out the transient lock
# window; a NON-lock error (genuine schema bug) still hard-fails on the
# first attempt.  All of this runs while holding the pg_advisory_lock, so
# sibling replicas simply keep waiting.
_MIGRATION_MAX_ATTEMPTS = 6
# A statement timeout (57014) is retried once, not until the attempts run out:
# a wait on a snapshot or a lock may clear between two attempts, but a
# statement too long for its bound is cancelled again each time, after the
# whole bound.
_MIGRATION_STATEMENT_TIMEOUT_RETRIES = 1
_MIGRATION_RETRY_BASE_DELAY = 2.0
_MIGRATION_RETRY_MAX_DELAY = 30.0


def _known_revisions() -> set[str]:
    """Return the set of revision IDs present in the Alembic script directory."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_build_alembic_config())
    return {rev.revision for rev in script.walk_revisions()}


def _head_revisions() -> list[str]:
    """Return the head revision IDs of the Alembic script directory this image ships."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_build_alembic_config())
    return sorted(script.get_heads())


# The shape of an Alembic revision id this project writes (12 hex characters by
# default, hand-written ids of 8 to 16 lower-case characters with an occasional
# underscore).  Anything else in ``alembic_version`` is a foreign marker.
_REVISION_ID_SHAPE = re.compile(r"[0-9a-z_]{6,32}")


def _unknown_revision_error(current_rev: str, image_heads: list[str]) -> RuntimeError:
    """Explain why an ``alembic_version`` value this image does not know refuses the boot.

    Three diagnoses, each fail-closed and none of them stamps anything:

    - a revision of the frozen pre-squash history (``PRE_SQUASH_REVISIONS``): an
      under-upgraded pre-squash database; upgrade through the last pre-squash
      release, the manual re-stamp being right only for a verified fully upgraded one;
    - a revision-shaped id outside it: the database is AHEAD of this image (a
      rollback, or an older release deployed after a newer one migrated); deploy
      an image whose history contains it, and never re-stamp, which would hide
      the newer schema and replay every post-squash migration on the next upgrade;
    - anything else: a foreign or corrupted marker; check the database pointed at.
    """
    if current_rev in PRE_SQUASH_REVISIONS:
        logger.error(
            "Database is stamped at the pre-squash revision '%s', older than the "
            "last pre-squash head '%s'. Refusing to auto-heal an under-upgraded "
            "database.",
            current_rev,
            PRE_SQUASH_FINAL_HEAD,
        )
        return RuntimeError(
            f"alembic_version points at the pre-squash revision '{current_rev}' "
            f"- expected the last pre-squash head '{PRE_SQUASH_FINAL_HEAD}'. "
            "Refusing to start. A deployment on an older pre-squash revision "
            "must first fully upgrade to the last pre-squash release on the old "
            "code before installing the squashed version. If the schema is "
            "verified fully upgraded, re-stamp manually per UPGRADING.md "
            f"('alembic stamp --purge {BASELINE_REVISION}')."
        )
    heads = ", ".join(image_heads) or "none"
    if _REVISION_ID_SHAPE.fullmatch(current_rev):
        logger.error(
            "Database is stamped at revision '%s', which this image does not know "
            "(its head: %s): the database is ahead of this image.",
            current_rev,
            heads,
        )
        return RuntimeError(
            f"The database is ahead of this image: alembic_version points at "
            f"revision '{current_rev}', which this image's migration history "
            f"(head {heads}) does not contain. Refusing to start. Deploy an image "
            f"whose migration history contains '{current_rev}' (a build at or "
            "after the commit that added it); do NOT re-stamp the database. "
            "See UPGRADING.md, 'Rolled back to an older image'."
        )
    logger.error(
        "Database is stamped at '%s', which is not an Alembic revision id this image can recognise (its head: %s).",
        current_rev,
        heads,
    )
    return RuntimeError(
        f"alembic_version points at unrecognised value '{current_rev}', which "
        f"is not a revision of this image (head {heads}). Refusing to start. "
        "Verify you are pointing at the correct XTM One database."
    )


async def _reconcile_pre_squash_revision(current_rev: str) -> None:
    """Re-stamp a DB pointing at the pre-squash final head to the baseline.

    The Alembic history was squashed into a single baseline
    (``d046bcdf345f``), so an existing database still stamped at the last
    pre-squash revision (``PRE_SQUASH_FINAL_HEAD``) points at a revision that no
    longer exists in the script directory.  A plain ``alembic upgrade head``
    would then fail hard with "Can't locate revision", and the app would refuse
    to boot — forcing every existing operator (including **air-gapped on-prem
    installs**) to run a manual ``alembic stamp --purge`` before the new version
    could start.

    We self-heal that case automatically, but **fail-closed**: we only re-stamp
    when the current revision is *exactly* the single head that existed
    immediately before the squash.  Because the squash assumes the
    **fully-upgraded contract** — every deployment is on the last pre-squash
    release before installing this version — that one revision is the only one
    guaranteed to be schema-equivalent to the baseline.  Three outcomes:

    - Revision **known to the current history** (a genuine pending migration):
      left untouched; the normal ``upgrade head`` path handles it.
    - Revision **equal to ``PRE_SQUASH_FINAL_HEAD``**: re-stamped to
      ``BASELINE_REVISION`` with ``purge=True`` — the revision the DB is
      schema-equivalent to.  A pure version-pointer move: ZERO DDL, zero data
      risk.  Deliberately NOT ``head``: once post-squash migrations exist,
      ``head`` is baseline+N and stamping it would silently mark those N
      migrations as applied without ever running their bodies.  Stamping the
      baseline leaves them pending, so the ``upgrade head`` that runs right
      after this reconcile step applies them normally — a deployment can jump
      from the pre-squash head straight to squash+N in one boot.
    - **Any other unknown revision** (an older/intermediate pre-squash
      revision, a post-squash revision newer than this image, a corrupted
      ``alembic_version`` value, a database belonging to a different product):
      raise ``RuntimeError`` and refuse to boot (fail-closed) so a human can
      investigate instead of the app silently stamping a schema as "up to date";
      ``_unknown_revision_error`` names which case it is and its fix.
    """
    known = await asyncio.to_thread(_known_revisions)
    if current_rev in known:
        return  # normal pending-migration path — nothing to heal

    if current_rev != PRE_SQUASH_FINAL_HEAD:
        # Not the one revision we know is schema-equivalent to the baseline. Do
        # NOT stamp blindly: that could hide an incomplete schema, a schema
        # newer than this image, a corrupted marker or a foreign database.
        image_heads = await asyncio.to_thread(_head_revisions)
        raise _unknown_revision_error(current_rev, image_heads)

    logger.warning(
        "Database is stamped at the last pre-squash head '%s', which is no "
        "longer in the migration history (expected on the first boot after the "
        "Alembic history squash). Auto-healing: re-stamping to the baseline "
        "'%s' with purge (metadata-only, no DDL, no data change); any "
        "post-squash migrations remain pending and are applied by the normal "
        "upgrade that follows. See UPGRADING.md.",
        current_rev,
        BASELINE_REVISION,
    )
    await asyncio.to_thread(
        command.stamp, _build_alembic_config(), BASELINE_REVISION, purge=True
    )
    logger.info(
        "Auto-heal complete — alembic_version re-stamped to the baseline "
        "'%s'; pending migrations (if any) run next; continuing startup "
        "normally (no manual intervention required).",
        BASELINE_REVISION,
    )


async def _apply_pending_migrations() -> None:
    """Run ``alembic upgrade head``; retry transient lock contention.

    Raises ``RuntimeError`` on a non-retryable error (immediately) or when
    lock contention persists after ``_MIGRATION_MAX_ATTEMPTS`` attempts, so
    the application refuses to start with a stale schema.
    """
    last_exc: BaseException | None = None
    statement_timeouts = 0
    for attempt in range(1, _MIGRATION_MAX_ATTEMPTS + 1):
        try:
            await asyncio.to_thread(
                command.upgrade, _build_alembic_config(), "head"
            )
        except Exception as exc:
            if not _is_retryable_ddl_lock_error(exc):
                logger.error("Database migration FAILED: %s", format_exception_message(exc), exc_info=True)
                raise RuntimeError(
                    "Alembic migration failed — refusing to start with a stale schema. Fix the migration chain and restart."
                ) from exc
            last_exc = exc
            if _is_statement_timeout(exc):
                statement_timeouts += 1
            if (
                attempt >= _MIGRATION_MAX_ATTEMPTS
                or statement_timeouts > _MIGRATION_STATEMENT_TIMEOUT_RETRIES
            ):
                break
            delay = min(
                _MIGRATION_RETRY_BASE_DELAY * (2 ** (attempt - 1)),
                _MIGRATION_RETRY_MAX_DELAY,
            )
            logger.warning(
                "Database migration hit transient lock contention "
                "(attempt %d/%d): %s — retrying in %.1fs (idempotent "
                "bodies make the re-run safe)",
                attempt,
                _MIGRATION_MAX_ATTEMPTS,
                format_exception_message(exc),
                delay,
            )
            await asyncio.sleep(delay)
        else:
            logger.info("Database migrations applied")
            return

    if statement_timeouts > _MIGRATION_STATEMENT_TIMEOUT_RETRIES:
        logger.error(
            "Database migration cancelled by a statement timeout on %d "
            "attempts: %s",
            statement_timeouts,
            last_exc,
            exc_info=last_exc,
        )
        raise RuntimeError(
            "Alembic migration failed: a statement ran past its "
            f"statement_timeout on {statement_timeouts} attempts "
            "(MIGRATION_STATEMENT_TIMEOUT_SECONDS="
            f"{settings.migration_statement_timeout_seconds}, unless the "
            "migration sets its own) — refusing to start with a stale "
            "schema. If the migration legitimately needs longer, raise "
            "MIGRATION_STATEMENT_TIMEOUT_SECONDS and restart; otherwise the "
            "startup watchdog's report names what it waits on."
        ) from last_exc
    logger.error(
        "Database migration still failing after %d attempts under "
        "sustained lock contention: %s",
        _MIGRATION_MAX_ATTEMPTS,
        last_exc,
        exc_info=last_exc,
    )
    raise RuntimeError(
        "Alembic migration failed after "
        f"{_MIGRATION_MAX_ATTEMPTS} attempts due to sustained lock "
        "contention — refusing to start with a stale schema. Check for "
        "long-running transactions holding locks on the altered tables "
        "and restart."
    ) from last_exc


async def _stamp_head() -> None:
    """Stamp Alembic head on a fresh database."""
    try:
        await asyncio.to_thread(command.stamp, _build_alembic_config(), "head")
    except Exception as exc:
        logger.error("Alembic stamp failed: %s", format_exception_message(exc), exc_info=True)
        raise RuntimeError(
            "Alembic stamp failed — refusing to start without a consistent schema state."
        ) from exc
    logger.info("Fresh database — stamped Alembic migration head")


# ---------------------------------------------------------------------------
# Schema extras — DDL that ``Base.metadata.create_all`` does NOT manage.
#
# Triggers, functional indexes and partial indexes live here.  Each
# artifact's bootstrap SQL must be idempotent (``CREATE OR REPLACE
# FUNCTION``, ``DROP TRIGGER IF EXISTS`` + ``CREATE TRIGGER``, a plain
# ``CREATE INDEX IF NOT EXISTS``, never ``CONCURRENTLY``) so a
# fresh-install ``create_all + stamp head`` path and an existing-install
# ``alembic upgrade`` path BOTH converge on the same artifact set after a
# boot — without this, fresh installs would be missing whatever Alembic
# migrations skipped.
#
# Add new artifacts here by appending another ``_ensure_*`` helper to
# ``_ensure_schema_extras`` below.  Keep the SQL idempotent and runnable
# from any boot order (Alembic-first OR create_all-first).
# ---------------------------------------------------------------------------


# Bootstrap SQL for the ``knowledge_documents.updated_at`` column
# constraints (``DEFAULT clock_timestamp()`` + backfill of NULL rows +
# ``NOT NULL``).  Mirrors the migration in
# ``v2w3x4y5z6a7_add_updated_at_to_knowledge_documents.py``; kept here
# because ``_ensure_model_columns`` (the generic safety net that adds a
# missing model column on stamp-head installs) only emits a bare
# ``ALTER TABLE … ADD COLUMN IF NOT EXISTS … DateTime`` — no DEFAULT,
# no NOT NULL, no backfill — by design (the comment in
# ``_ensure_model_columns`` says "proper migration handles those once
# Alembic state is corrected").  But on a stamped-fresh install the
# migration body NEVER runs, so the bare column comes through with
# NULL values for every existing row and the sweeper's
# ``updated_at < cutoff`` predicate silently never matches.  This
# bootstrap pass fills that gap.
#
# Order is deliberate:
#
#   1. ``SET DEFAULT clock_timestamp()`` first so any concurrent INSERT
#      that races against our backfill gets the default (no NULL).
#   2. ``UPDATE … COALESCE(created_at, clock_timestamp()) WHERE
#      updated_at IS NULL`` to backfill existing NULL rows.  ``COALESCE``
#      reuses ``created_at`` when available so the sweeper's age cutoff
#      reflects the document's actual age, not the migration time.
#      ``clock_timestamp()`` (NOT ``now()``) for the fallback so a slow
#      bootstrap inside a long transaction doesn't tag every backfilled
#      row with the transaction's *start* time — same reasoning as the
#      trigger and the migration's own backfill.
#   3. ``SET NOT NULL`` last so the constraint validation reads the
#      table state AFTER the backfill (within the same transaction)
#      and the future-default is already in place to absorb any
#      racing INSERT that lands without the column.
#
# Each statement is idempotent on its own — repeated boots on an
# install where the migration already ran become metadata-only
# no-ops at every step.
_KB_DOCUMENT_SET_DEFAULT_UPDATED_AT_SQL = (
    "ALTER TABLE knowledge_documents "
    "ALTER COLUMN updated_at SET DEFAULT clock_timestamp()"
)

# Idempotency probe for the ``SET DEFAULT`` above — the single most
# important guard against the SQLSTATE 57014 crash-loop.
# ``ALTER TABLE … SET DEFAULT`` takes an ``ACCESS EXCLUSIVE`` lock; when
# it ran unconditionally on every boot, any lock holder at deploy time
# (a long-running query, an idle-in-transaction connection) queued the
# DDL at the head of the lock queue — blocking every following query —
# until the inherited 30 s ``statement_timeout`` cancelled it, hard-
# failing startup and triggering a 10–25 min restart loop.  Reading the
# stored default expression from ``pg_attrdef`` lets us skip the DDL
# entirely once it has been applied: the steady-state boot takes NO lock
# on ``knowledge_documents``.  ``pg_get_expr`` renders the stored node
# tree back to text (e.g. ``clock_timestamp()``); the catalog read takes
# only an ``AccessShareLock`` on system catalogs, never on the table.
_KB_DOCUMENT_UPDATED_AT_DEFAULT_CHECK_SQL = (
    "SELECT pg_get_expr(d.adbin, d.adrelid) "
    "FROM pg_attrdef d "
    "JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum "
    "JOIN pg_class c ON c.oid = d.adrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relname = 'knowledge_documents' "
    "AND a.attname = 'updated_at' "
    "AND a.attnum > 0 "
    "AND NOT a.attisdropped "
    "AND n.nspname = ANY(current_schemas(false))"
)

_KB_DOCUMENT_BACKFILL_UPDATED_AT_SQL = (
    "UPDATE knowledge_documents "
    "SET updated_at = COALESCE(created_at, clock_timestamp()) "
    "WHERE updated_at IS NULL"
)

# Live-safe NOT NULL sequence (same constraint name as the matching
# Alembic migration so a partial migration that left the CHECK around
# won't conflict on the next bootstrap run).  See the long-form
# rationale in the migration ``v2w3x4y5z6a7_add_updated_at_to_knowledge_
# documents.py`` — a bare ``ALTER COLUMN … SET NOT NULL`` takes an
# ``ACCESS EXCLUSIVE`` lock for the full duration of a synchronous
# full-table scan, blocking sync upserts and embedding status flips on
# populated production tables.  The standard PostgreSQL live-safe
# pattern is:
#
#   1. ``ADD CONSTRAINT … CHECK (col IS NOT NULL) NOT VALID`` —
#      metadata-only, brief ``ACCESS EXCLUSIVE``, no scan.
#   2. ``VALIDATE CONSTRAINT`` — scans the table under
#      ``SHARE UPDATE EXCLUSIVE`` (allows concurrent INSERT/UPDATE/
#      SELECT).  Must run OUTSIDE any enclosing transaction.
#   3. ``SET NOT NULL`` — PostgreSQL ≥ 12 recognises the validated
#      CHECK and skips the full-table scan; this then completes as a
#      fast metadata-only ``ACCESS EXCLUSIVE`` (milliseconds).
#   4. ``DROP CONSTRAINT`` — the column's NOT NULL attribute now
#      supersedes the CHECK.
_KB_DOCUMENT_UPDATED_AT_CHECK_CONSTRAINT_NAME = (
    "ck_knowledge_documents_updated_at_not_null"
)

_KB_DOCUMENT_ADD_CHECK_NOT_VALID_UPDATED_AT_SQL = (
    "ALTER TABLE knowledge_documents "
    f"ADD CONSTRAINT {_KB_DOCUMENT_UPDATED_AT_CHECK_CONSTRAINT_NAME} "
    "CHECK (updated_at IS NOT NULL) NOT VALID"
)

_KB_DOCUMENT_VALIDATE_CHECK_UPDATED_AT_SQL = (
    "ALTER TABLE knowledge_documents "
    f"VALIDATE CONSTRAINT {_KB_DOCUMENT_UPDATED_AT_CHECK_CONSTRAINT_NAME}"
)

_KB_DOCUMENT_SET_NOT_NULL_UPDATED_AT_SQL = (
    "ALTER TABLE knowledge_documents "
    "ALTER COLUMN updated_at SET NOT NULL"
)

_KB_DOCUMENT_DROP_CHECK_UPDATED_AT_SQL = (
    "ALTER TABLE knowledge_documents "
    f"DROP CONSTRAINT IF EXISTS {_KB_DOCUMENT_UPDATED_AT_CHECK_CONSTRAINT_NAME}"
)

# Idempotency guard: skip the entire NOT NULL flow when the column is
# already NOT NULL.  This is the common case on every boot after the
# first — ``create_all`` on fresh installs builds the column NOT NULL
# straight from the model definition, and existing installs run the
# migration body once.  Only the rare path where the column was added
# as a bare nullable column (e.g. ``_ensure_model_columns`` ran before
# this helper on a populated install) needs the live-safe sequence.
_KB_DOCUMENT_UPDATED_AT_NOT_NULL_CHECK_SQL = (
    "SELECT a.attnotnull "
    "FROM pg_attribute a "
    "JOIN pg_class c ON c.oid = a.attrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relname = 'knowledge_documents' "
    "AND a.attname = 'updated_at' "
    "AND a.attnum > 0 "
    "AND NOT a.attisdropped "
    "AND n.nspname = ANY(current_schemas(false))"
)

# Idempotency guard for VALIDATE: if a prior bootstrap added the CHECK
# but crashed before VALIDATE or before SET NOT NULL, the constraint
# row in ``pg_constraint`` already exists.  We detect it and either
# skip the ADD (re-run path) or skip the entire sequence if the column
# became NOT NULL on the side.  Querying ``convalidated`` lets us tell
# whether the CHECK still needs validating.
_KB_DOCUMENT_UPDATED_AT_CHECK_LOOKUP_SQL = (
    "SELECT convalidated FROM pg_constraint "
    "WHERE conname = :constraint_name"
)


# Bootstrap SQL for the ``knowledge_documents.updated_at`` trigger that
# refreshes the timestamp on every UPDATE, regardless of whether the ORM
# remembered to set it.  Mirrors the migration in
# ``v2w3x4y5z6a7_add_updated_at_to_knowledge_documents.py``; kept here so
# fresh installs (which skip the migration body) still get the trigger.
_KB_DOCUMENT_TRIGGER_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION knowledge_documents_set_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at = clock_timestamp();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_KB_DOCUMENT_TRIGGER_DROP_SQL = (
    "DROP TRIGGER IF EXISTS trg_knowledge_documents_set_updated_at "
    "ON knowledge_documents"
)

_KB_DOCUMENT_TRIGGER_CREATE_SQL = """
CREATE TRIGGER trg_knowledge_documents_set_updated_at
BEFORE UPDATE ON knowledge_documents
FOR EACH ROW
EXECUTE FUNCTION knowledge_documents_set_updated_at();
"""

# Idempotency probes for the trigger recreation.  Just like ``SET
# DEFAULT``, the trigger DDL takes table-level locks that conflict with
# concurrent writes and, under contention, would queue and trip
# ``statement_timeout`` exactly like the SQLSTATE 57014 crash-loop:
#
#   * ``DROP TRIGGER``   → ``ACCESS EXCLUSIVE``
#   * ``CREATE TRIGGER`` → ``SHARE ROW EXCLUSIVE``
#
# Skipping the recreation when the trigger is already present AND its
# backing function body is current means the steady-state boot takes NO
# lock on ``knowledge_documents``.  Both probes are pure catalog reads.
#
# ``tgisinternal`` filters out the internal constraint triggers Postgres
# creates for FKs/unique constraints so we only match our own trigger.
_KB_DOCUMENT_TRIGGER_PRESENT_CHECK_SQL = (
    "SELECT EXISTS ("
    "SELECT 1 FROM pg_trigger t "
    "JOIN pg_class c ON c.oid = t.tgrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relname = 'knowledge_documents' "
    "AND t.tgname = 'trg_knowledge_documents_set_updated_at' "
    "AND NOT t.tgisinternal "
    "AND n.nspname = ANY(current_schemas(false))"
    ")"
)

# Read the backing function's source so a body change (e.g. a future
# swap from ``clock_timestamp()`` to something else) forces a refresh
# via ``CREATE OR REPLACE FUNCTION`` + trigger rebind rather than being
# silently skipped by the presence check alone.
#
# The probe joins **through the trigger** (``pg_trigger.tgfoid`` →
# ``pg_proc.oid``) rather than matching ``pg_proc.proname`` by name +
# search_path.  Matching by name alone is ambiguous: a same-named
# function in another schema on the search_path (or an accidental
# overload) makes ``scalar_one_or_none()`` raise ``MultipleResultsFound``,
# which :func:`_is_updated_at_trigger_current` swallows and treats as
# "not current" — forcing the DROP/CREATE TRIGGER DDL on every boot and
# reintroducing the very lock-taking behaviour this fix removes.
# Reading the body of the function the trigger is *actually
# bound to* is unambiguous: there is exactly one trigger of this name on
# ``knowledge_documents`` and ``tgfoid`` points at exactly one function.
_KB_DOCUMENT_TRIGGER_FUNCTION_BODY_CHECK_SQL = (
    "SELECT p.prosrc "
    "FROM pg_trigger t "
    "JOIN pg_class c ON c.oid = t.tgrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "JOIN pg_proc p ON p.oid = t.tgfoid "
    "WHERE c.relname = 'knowledge_documents' "
    "AND t.tgname = 'trg_knowledge_documents_set_updated_at' "
    "AND NOT t.tgisinternal "
    "AND n.nspname = ANY(current_schemas(false))"
)

_KB_DOCUMENT_STATUS_UPDATED_AT_INDEX_NAME = (
    "ix_knowledge_documents_status_updated_at"
)

# Plain in-place build, deliberately NOT ``CREATE INDEX CONCURRENTLY``.
# This bootstrap runs while holding the pg_advisory_lock that
# serializes migrations.  ``CONCURRENTLY`` DDL waits for every open
# transaction holding an older snapshot: the sibling uvicorn workers/pods
# poll the lock rather than wait inside ``SELECT pg_advisory_lock(...)``,
# but during a rolling deploy the previous release's sessions never release
# theirs.  The boot hangs (liveness probe SIGTERM →
# INVALID index → CrashLoopBackOff).
# A plain build takes a brief SHARE lock and completes — same rationale
# as ``_ensure_model_indexes``.
_KB_DOCUMENT_STATUS_UPDATED_AT_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS "
    f"{_KB_DOCUMENT_STATUS_UPDATED_AT_INDEX_NAME} "
    "ON knowledge_documents (status, updated_at)"
)

# ``CREATE INDEX CONCURRENTLY`` can legitimately take longer than the
# 30 s ``statement_timeout`` set on every application connection in
# ``app/core/database.py`` when the target table is already populated.
# Disable the timeout for the autocommit connection that runs this
# statement only — session-level, so other connections in the pool
# keep their 30 s guard.
_DISABLE_STATEMENT_TIMEOUT_SQL = "SET statement_timeout = 0"

# ── Phase A crash-loop hardening ──────────────────────
#
# A short ``lock_timeout`` for the Phase A DDL transaction so a blocked
# ``ALTER``/``DROP``/``CREATE`` gives up its slot at the head of the
# lock queue within a few seconds (raising SQLSTATE 55P03,
# ``lock_not_available``) instead of stalling every following query for
# the full 30 s ``statement_timeout`` and then hard-failing startup.
# ``SET LOCAL`` scopes it to the transaction so it auto-resets on
# commit/rollback and never leaks to other pooled users of the
# connection.
_PHASE_A_LOCK_TIMEOUT_MS = 3000
_PHASE_A_SET_LOCK_TIMEOUT_SQL = (
    f"SET LOCAL lock_timeout = '{_PHASE_A_LOCK_TIMEOUT_MS}ms'"
)

# ``SET LOCAL statement_timeout = 0`` for the Phase A transaction: lock
# contention is now bounded by ``lock_timeout`` (above), so we can lift
# the statement timeout to let a *genuine* backfill on a populated
# stamped-fresh table complete instead of cancelling honest work.  This
# is the inverse of the old behaviour where ``statement_timeout`` was
# the (wrong) contention guard — it cancelled the metadata-only DDL and
# crashed the deploy.
_PHASE_A_DISABLE_STATEMENT_TIMEOUT_SQL = "SET LOCAL statement_timeout = 0"

# Retry policy for the genuine first-deploy path where Phase A DDL must
# actually run and hits transient lock contention.  Exponential
# backoff: 1 → 2 → 4 → 8 → 16 s (capped), 6 attempts total.  Combined
# with the idempotency skip this means: steady-state boots do nothing;
# first-deploy boots ride out a transient lock window instead of
# crash-looping; a genuinely stuck DDL still hard-fails loudly after
# the attempts are exhausted (preserving the "refuse to start with a
# misbehaving sweeper" invariant).
_PHASE_A_MAX_ATTEMPTS = 6
_PHASE_A_RETRY_BASE_DELAY = 1.0
_PHASE_A_RETRY_MAX_DELAY = 16.0

# SQLSTATEs that indicate transient lock/timeout contention on the Phase
# A DDL and are therefore safe to retry:
#   * ``55P03`` lock_not_available — our ``lock_timeout`` fired.
#   * ``57014`` query_canceled     — ``statement_timeout`` fired (e.g.
#     a stray pooled connection that didn't pick up the ``SET LOCAL``).
#   * ``40P01`` deadlock_detected  — PostgreSQL aborted this side of a
#     deadlock with a session of the previous release; every migration is
#     idempotent, so the next attempt resumes where this one stopped.
_RETRYABLE_DDL_SQLSTATES = frozenset({"55P03", "57014", "40P01"})

# Detect a leftover ``INVALID`` index from a previous failed bootstrap
# (e.g. timeout, deadlock, deploy crash mid-build). PostgreSQL marks
# ``pg_index.indisvalid = false`` on the row but the catalog entry
# still exists, so ``CREATE INDEX IF NOT EXISTS`` would
# silently skip on the next boot and the sweeper would seq-scan
# forever with no surfaced error.  We must explicitly drop the invalid
# row before re-creating.
_KB_DOCUMENT_STATUS_UPDATED_AT_INDEX_INVALID_CHECK_SQL = (
    "SELECT 1 FROM pg_index i "
    "JOIN pg_class c ON c.oid = i.indexrelid "
    "WHERE c.relname = :index_name AND NOT i.indisvalid"
)

# Plain ``DROP INDEX`` — NOT ``CONCURRENTLY`` — for the same
# advisory-lock deadlock reason as the create path above.
# Dropping an INVALID index is catalog-only; the AccessExclusiveLock
# is held for milliseconds.
_KB_DOCUMENT_STATUS_UPDATED_AT_INDEX_DROP_SQL = (
    "DROP INDEX IF EXISTS "
    f"{_KB_DOCUMENT_STATUS_UPDATED_AT_INDEX_NAME}"
)

# Composite ``(knowledge_base_id, status)`` index for the per-KB document
# status-breakdown count used by ``list_knowledge_bases``.
# Without it the ``SELECT count(*) FILTER (WHERE status = …) … WHERE
# knowledge_base_id = $1`` runs a heap scan per knowledge base; under
# concurrent load that storms ``knowledge_documents`` and the 30 s
# ``statement_timeout`` cancels the query (SQLSTATE 57014), turning
# ``GET /api/v1/knowledge_bases`` into a 500 for every caller. Built with
# the same in-place build + invalid-leftover recovery machinery as the
# ``(status, updated_at)`` sweeper index above.
_KB_DOCUMENT_KB_STATUS_INDEX_NAME = "ix_knowledge_documents_kb_status"

# Plain DDL (not CONCURRENTLY) — see the deadlock note on
# ``_KB_DOCUMENT_STATUS_UPDATED_AT_INDEX_SQL`` above.
_KB_DOCUMENT_KB_STATUS_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS "
    f"{_KB_DOCUMENT_KB_STATUS_INDEX_NAME} "
    "ON knowledge_documents (knowledge_base_id, status)"
)

_KB_DOCUMENT_KB_STATUS_INDEX_DROP_SQL = (
    "DROP INDEX IF EXISTS "
    f"{_KB_DOCUMENT_KB_STATUS_INDEX_NAME}"
)

# Every bootstrap-managed concurrent index on ``knowledge_documents``,
# created/repaired in one AUTOCOMMIT pass: ``(name, create_sql, drop_sql)``.
# The shared invalid-leftover check SQL is parameterised by ``:index_name``.
_KB_DOCUMENT_CONCURRENT_INDEXES: tuple[tuple[str, str, str], ...] = (
    (
        _KB_DOCUMENT_STATUS_UPDATED_AT_INDEX_NAME,
        _KB_DOCUMENT_STATUS_UPDATED_AT_INDEX_SQL,
        _KB_DOCUMENT_STATUS_UPDATED_AT_INDEX_DROP_SQL,
    ),
    (
        _KB_DOCUMENT_KB_STATUS_INDEX_NAME,
        _KB_DOCUMENT_KB_STATUS_INDEX_SQL,
        _KB_DOCUMENT_KB_STATUS_INDEX_DROP_SQL,
    ),
)


async def _is_updated_at_already_not_null() -> bool:
    """Return ``True`` when ``knowledge_documents.updated_at`` is NOT NULL.

    Used by :func:`_ensure_schema_extras` to short-circuit the live-safe
    NOT NULL sequence on the common case where the column is already
    correctly typed — fresh installs after ``create_all`` and existing
    installs after the Alembic migration both land here.  Only the
    narrow path where ``_ensure_model_columns`` added the column as a
    bare nullable ``DateTime`` on a populated table needs the live-safe
    sequence to actually run.

    Returns ``False`` when the column is nullable OR when the lookup
    itself can't tell (column not yet present, etc.) — the caller then
    falls through to the safe-but-slightly-more-work path, which is
    itself idempotent for either case.
    """
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(_KB_DOCUMENT_UPDATED_AT_NOT_NULL_CHECK_SQL)
            )
            row = result.scalar_one_or_none()
            return bool(row)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "Could not determine knowledge_documents.updated_at NOT NULL "
            "state (%s); falling through to live-safe sequence",
            format_exception_message(exc),
        )
        return False


async def _get_updated_at_check_state(conn) -> bool | None:
    """Return the ``convalidated`` flag for the transient NOT NULL CHECK.

    The bootstrap uses a transient ``CHECK (updated_at IS NOT NULL)``
    constraint as part of the live-safe ``SET NOT NULL`` sequence.  A
    prior bootstrap can crash between ``ADD CONSTRAINT NOT VALID`` and
    ``VALIDATE CONSTRAINT``, or between ``VALIDATE`` and ``SET NOT
    NULL`` + ``DROP CONSTRAINT``, leaving the catalog in one of three
    states:

    * ``None`` — no CHECK row in ``pg_constraint``.  Must ``ADD`` it.
    * ``False`` — ``convalidated = false`` (added but not validated).
      Skip ``ADD``, run ``VALIDATE``.
    * ``True`` — already validated.  Skip both ``ADD`` and ``VALIDATE``;
      go straight to ``SET NOT NULL``.

    The ``conn`` parameter accepts either an ``AsyncConnection`` from
    ``engine.connect()`` (transactional) or one returned by
    ``execution_options(isolation_level="AUTOCOMMIT")`` — both expose
    the same ``execute`` interface.
    """
    result = await conn.execute(
        text(_KB_DOCUMENT_UPDATED_AT_CHECK_LOOKUP_SQL),
        {
            "constraint_name": (
                _KB_DOCUMENT_UPDATED_AT_CHECK_CONSTRAINT_NAME
            ),
        },
    )
    return result.scalar_one_or_none()


async def _is_updated_at_default_set() -> bool:
    """Return ``True`` when ``knowledge_documents.updated_at`` already
    defaults to ``clock_timestamp()``.

    The idempotency guard that eliminates the SQLSTATE 57014 crash-loop:
    after the first successful deploy the ``ALTER TABLE …
    SET DEFAULT`` — which takes an ``ACCESS EXCLUSIVE`` lock — is skipped
    entirely, so steady-state boots take no lock on the table.

    Returns ``False`` on any uncertainty (no default, column missing,
    probe error) so the caller falls through to the idempotent DDL,
    which is itself safe to re-run.
    """
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(_KB_DOCUMENT_UPDATED_AT_DEFAULT_CHECK_SQL)
            )
            expr = result.scalar_one_or_none()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "Could not determine knowledge_documents.updated_at default "
            "expression (%s); will attempt SET DEFAULT",
            format_exception_message(exc),
        )
        return False
    return isinstance(expr, str) and "clock_timestamp()" in expr.lower()


async def _is_updated_at_trigger_current() -> bool:
    """Return ``True`` when the ``BEFORE UPDATE`` trigger exists AND its
    backing function body uses ``clock_timestamp()``.

    Lets :func:`_ensure_schema_extras` skip the ``DROP TRIGGER`` (ACCESS
    EXCLUSIVE) + ``CREATE TRIGGER`` (SHARE ROW EXCLUSIVE) recreation on
    every boot after the first — the second half of the lock-taking DDL
    behind the crash-loop.  Both probes are pure catalog
    reads (``AccessShareLock`` on system catalogs only).

    Returns ``False`` on any uncertainty so the caller (re)creates the
    trigger via the idempotent ``DROP IF EXISTS`` + ``CREATE`` path.
    """
    try:
        async with engine.connect() as conn:
            present = await conn.execute(
                text(_KB_DOCUMENT_TRIGGER_PRESENT_CHECK_SQL)
            )
            if not bool(present.scalar_one_or_none()):
                return False
            body = await conn.execute(
                text(_KB_DOCUMENT_TRIGGER_FUNCTION_BODY_CHECK_SQL)
            )
            src = body.scalar_one_or_none()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "Could not determine knowledge_documents updated_at trigger "
            "state (%s); will (re)create the trigger",
            format_exception_message(exc),
        )
        return False
    return isinstance(src, str) and "clock_timestamp()" in src.lower()


def _is_statement_timeout(exc: BaseException) -> bool:
    """Was *exc* raised by a ``statement_timeout`` (SQLSTATE 57014)?"""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if any(getattr(cur, attr, None) == "57014" for attr in ("sqlstate", "pgcode")):
            return True
        cur = cur.__cause__ or cur.__context__
    blob = str(exc).lower()
    return "57014" in blob or "canceling statement due to statement timeout" in blob


def _is_retryable_ddl_lock_error(exc: BaseException) -> bool:
    """Return ``True`` when *exc* is transient lock/timeout contention on
    the Phase A DDL and is therefore worth retrying.

    Walks the exception chain looking for a PostgreSQL SQLSTATE (asyncpg
    exposes it as ``sqlstate``/``pgcode`` on the driver exception, which
    SQLAlchemy wraps) and matches ``55P03`` (``lock_timeout`` fired) or
    ``57014`` (``statement_timeout`` fired).  Falls back to a string
    probe so detection stays robust across driver/wrapper differences.
    A non-lock error (a genuine schema bug) returns ``False`` and the
    caller hard-fails immediately rather than retrying a hopeless DDL.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        for attr in ("sqlstate", "pgcode"):
            code = getattr(cur, attr, None)
            if isinstance(code, str) and code in _RETRYABLE_DDL_SQLSTATES:
                return True
        cur = cur.__cause__ or cur.__context__
    blob = str(exc).lower()
    return (
        "lock_not_available" in blob
        or "55p03" in blob
        or "57014" in blob
        or "40p01" in blob
        or "canceling statement due to statement timeout" in blob
        or "canceling statement due to lock timeout" in blob
        or "deadlock detected" in blob
    )


async def _run_phase_a_ddl(
    *,
    set_default_needed: bool,
    backfill_needed: bool,
    trigger_setup_needed: bool,
    not_null_setup_needed: bool,
) -> None:
    """Run Phase A of the schema-extras bootstrap with crash-loop hardening.

    Phase A ensures the ``knowledge_documents.updated_at`` column
    ``DEFAULT``, backfills NULL rows, (re)creates the ``BEFORE UPDATE``
    trigger, and — on the nullable-column path — adds the transient NOT
    NULL ``CHECK`` that Phase B later validates.

    Crash-loop hardening
    ----------------------------------
    Every statement here that touches ``knowledge_documents`` takes a
    table-level lock that conflicts with concurrent writes:

      * ``ALTER … SET DEFAULT`` and ``DROP TRIGGER`` → ACCESS EXCLUSIVE
      * ``CREATE TRIGGER``                          → SHARE ROW EXCLUSIVE

    Historically these ran unconditionally on *every* boot, so any lock
    holder at deploy time queued the DDL at the head of the lock queue —
    blocking every following query — until the inherited 30 s
    ``statement_timeout`` cancelled it (SQLSTATE 57014).  The bootstrap
    then hard-failed and the container crash-looped for 10–25 minutes.

    Two layers remove the failure mode:

    1. **Idempotency skip.**  The caller probes the catalog and passes
       in exactly which operations are still needed.  When nothing is
       needed (the universal case after the first successful deploy) this
       function returns immediately — Phase A takes **no** lock on
       ``knowledge_documents`` and the crash-loop is structurally
       impossible.
    2. **Fail-fast + retry.**  For the genuine first-deploy path the
       transaction sets a short ``lock_timeout`` so a blocked statement
       gives up its lock-queue slot in seconds (55P03) instead of
       stalling traffic for 30 s, lifts ``statement_timeout`` so an
       honest backfill on a populated table can complete, and retries
       with exponential backoff.  A non-lock error fails immediately; a
       sustained lock conflict hard-fails only after the attempts are
       exhausted (preserving the loud-crash invariant for a genuinely
       broken schema).
    """
    if not (
        set_default_needed
        or backfill_needed
        or trigger_setup_needed
        or not_null_setup_needed
    ):
        logger.info(
            "knowledge_documents updated_at DEFAULT + trigger already "
            "current (and column already NOT NULL) — Phase A is a no-op "
            "this boot; no lock taken on knowledge_documents"
        )
        return

    last_exc: BaseException | None = None
    for attempt in range(1, _PHASE_A_MAX_ATTEMPTS + 1):
        try:
            async with engine.begin() as ddl_conn:
                # Bound lock waits so a blocked DDL fails fast (55P03)
                # instead of hogging the lock queue for 30 s; lift the
                # statement timeout so a genuine backfill can finish.
                await ddl_conn.execute(text(_PHASE_A_SET_LOCK_TIMEOUT_SQL))
                await ddl_conn.execute(
                    text(_PHASE_A_DISABLE_STATEMENT_TIMEOUT_SQL)
                )

                if set_default_needed:
                    await ddl_conn.execute(
                        text(_KB_DOCUMENT_SET_DEFAULT_UPDATED_AT_SQL)
                    )
                if backfill_needed:
                    await ddl_conn.execute(
                        text(_KB_DOCUMENT_BACKFILL_UPDATED_AT_SQL)
                    )
                if trigger_setup_needed:
                    await ddl_conn.execute(
                        text(_KB_DOCUMENT_TRIGGER_FUNCTION_SQL)
                    )
                    await ddl_conn.execute(text(_KB_DOCUMENT_TRIGGER_DROP_SQL))
                    await ddl_conn.execute(
                        text(_KB_DOCUMENT_TRIGGER_CREATE_SQL)
                    )
                if not_null_setup_needed:
                    # Live-safe NOT NULL prep — only add the CHECK if it
                    # isn't already present (a prior bootstrap may have
                    # added it and crashed before VALIDATE / SET NOT
                    # NULL).  Phase B validates + sets NOT NULL outside
                    # the transaction.
                    check_state = await _get_updated_at_check_state(ddl_conn)
                    if check_state is None:
                        await ddl_conn.execute(
                            text(
                                _KB_DOCUMENT_ADD_CHECK_NOT_VALID_UPDATED_AT_SQL
                            )
                        )
            return
        except Exception as exc:
            last_exc = exc
            if not _is_retryable_ddl_lock_error(exc):
                logger.error(
                    "Failed to ensure knowledge_documents trigger / column "
                    "constraints: %s",
                    format_exception_message(exc),
                    exc_info=True,
                )
                raise RuntimeError(
                    "Schema-extras bootstrap failed — stuck-document "
                    "sweeper would misbehave without the updated_at "
                    "backfill + NOT NULL + BEFORE UPDATE trigger; refusing "
                    "to start."
                ) from exc
            if attempt >= _PHASE_A_MAX_ATTEMPTS:
                break
            delay = min(
                _PHASE_A_RETRY_BASE_DELAY * (2 ** (attempt - 1)),
                _PHASE_A_RETRY_MAX_DELAY,
            )
            logger.warning(
                "knowledge_documents Phase A DDL hit transient lock/timeout "
                "contention (attempt %d/%d): %s — retrying in %.1fs",
                attempt,
                _PHASE_A_MAX_ATTEMPTS,
                format_exception_message(exc),
                delay,
            )
            await asyncio.sleep(delay)

    # ``exc_info`` takes the saved exception INSTANCE: this log runs outside
    # the ``except`` block, so ``exc_info=True`` would find no active
    # exception and print "NoneType: None" instead of the real traceback.
    logger.error(
        "knowledge_documents Phase A DDL still failing after %d attempts "
        "under sustained lock contention: %s",
        _PHASE_A_MAX_ATTEMPTS,
        last_exc,
        exc_info=last_exc,
    )
    raise RuntimeError(
        "Schema-extras bootstrap failed — could not apply the "
        "knowledge_documents updated_at DEFAULT + BEFORE UPDATE trigger + "
        f"NOT NULL setup after {_PHASE_A_MAX_ATTEMPTS} attempts due to "
        "sustained lock contention; refusing to start so the stuck-document "
        "sweeper does not silently misbehave."
    ) from last_exc


async def _ensure_schema_extras() -> None:
    """Apply non-``create_all`` schema artifacts (triggers, concurrent indexes).

    On a fresh database the migration body that creates the
    ``knowledge_documents`` ``updated_at`` trigger and the
    ``(status, updated_at)`` index is never executed — fresh installs go
    through ``Base.metadata.create_all`` + ``alembic stamp head`` instead
    of ``alembic upgrade head``.  Without a bootstrap pass the stuck-
    document sweeper would silently regress on a fresh install: the
    trigger wouldn't fire for raw-SQL or ORM-bypass updates and the
    sweeper's ``WHERE status IN (...) AND updated_at < cutoff`` would
    seq-scan instead of using the index.

    Idempotent across reboots and across both install paths:

    * ``CREATE OR REPLACE FUNCTION`` always succeeds whether the function
      already exists or not.
    * ``DROP TRIGGER IF EXISTS`` + ``CREATE TRIGGER`` handles the case
      where the function definition just got refreshed; running this on
      an existing install drops the prior trigger and re-creates it
      bound to the (functionally identical) replaced function.
    * ``CREATE INDEX IF NOT EXISTS`` is a fast no-op when the index
      already exists.
    * The live-safe NOT NULL sequence (CHECK NOT VALID → VALIDATE →
      SET NOT NULL → DROP CHECK) is short-circuited up front when the
      column is already NOT NULL (the common case after every boot
      following the first).  When it does run, ``VALIDATE CONSTRAINT``
      uses a ``SHARE UPDATE EXCLUSIVE`` lock that allows concurrent
      reads and writes — production sync upserts and embedding status
      flips continue uninterrupted across the scan window.

    Crash-loop hardening
    ----------------------------------
    The Phase A column/trigger DDL takes table-level locks on
    ``knowledge_documents`` (``ACCESS EXCLUSIVE`` for ``SET DEFAULT`` /
    ``DROP TRIGGER``, ``SHARE ROW EXCLUSIVE`` for ``CREATE TRIGGER``).
    Running it unconditionally on every boot meant any lock holder at
    deploy time queued the DDL until ``statement_timeout`` cancelled it
    (SQLSTATE 57014) and startup crash-looped.  The fix probes the
    catalog up front and only runs the operations that are genuinely
    missing — so steady-state boots take **no** lock on the table — then
    delegates the genuine first-deploy path to :func:`_run_phase_a_ddl`,
    which fails fast on lock contention (``lock_timeout``) and retries
    with exponential backoff.

    Failures are logged and re-raised so a bootstrap that can't ensure
    these artifacts halts startup — the sweeper would silently
    misbehave otherwise, which is worse than a loud crash.
    """
    # ── Column constraints + trigger + function ──────────────────────
    #
    # Column constraints come BEFORE the trigger because:
    #
    #   * ``SET DEFAULT`` + ``UPDATE … WHERE updated_at IS NULL`` +
    #     the live-safe NOT NULL sequence mirror the migration body —
    #     on a stamped-fresh install where ``_ensure_model_columns``
    #     added the column as a bare nullable ``DateTime``, the rows
    #     still have ``updated_at = NULL`` and the sweeper's predicate
    #     (``WHERE status IN (…) AND updated_at < cutoff``) silently
    #     never matches.
    #   * The trigger sets ``updated_at = clock_timestamp()`` on
    #     subsequent UPDATEs, but does nothing for rows that haven't
    #     been touched since the column was added — which is exactly
    #     the install path we're patching.  Running the backfill
    #     first means the trigger then takes over for fresh writes.
    #
    # Each operation is gated on a catalog probe so that on every boot
    # after the first NOTHING here runs (DEFAULT already set, trigger
    # already current, column already NOT NULL) — Phase A then takes no
    # lock on ``knowledge_documents`` and the SQLSTATE 57014 crash-loop
    # is structurally impossible.  The probes are pure catalog reads.
    column_already_not_null = await _is_updated_at_already_not_null()
    default_already_set = await _is_updated_at_default_set()
    trigger_already_current = await _is_updated_at_trigger_current()

    # Phase A: idempotent transactional setup — DEFAULT + backfill +
    # trigger + (when needed) add CHECK NOT VALID — run only for the
    # operations still missing, with lock_timeout + retry/backoff so the
    # genuine first-deploy path can't crash-loop on lock contention.
    await _run_phase_a_ddl(
        set_default_needed=not default_already_set,
        backfill_needed=not column_already_not_null,
        trigger_setup_needed=not trigger_already_current,
        not_null_setup_needed=not column_already_not_null,
    )

    if not column_already_not_null:
        # Phase B: ``VALIDATE CONSTRAINT`` outside any transaction so
        # the SHARE UPDATE EXCLUSIVE lock acquired during the scan
        # allows concurrent INSERT/UPDATE/SELECT against the table.
        # Then ``SET NOT NULL`` in a fresh transactional block — fast
        # because PG ≥ 12 uses the now-validated CHECK to skip the
        # full-table scan — and DROP the now-redundant CHECK.
        try:
            async with engine.connect() as validate_conn:
                autocommit_validate = await validate_conn.execution_options(
                    isolation_level="AUTOCOMMIT"
                )
                # Statement timeout reset for the same reason as the
                # concurrent index build below — the VALIDATE scan
                # legitimately exceeds the 30 s default on populated
                # tables.
                await autocommit_validate.execute(
                    text(_DISABLE_STATEMENT_TIMEOUT_SQL)
                )
                # Re-probe inside the new connection: if the CHECK
                # already exists AND is validated we can skip straight
                # to SET NOT NULL.  This covers the partial-failure
                # recovery path (bootstrap crashed mid-validation on a
                # prior boot).
                check_state = await _get_updated_at_check_state(
                    autocommit_validate
                )
                if check_state is False:
                    await autocommit_validate.execute(
                        text(_KB_DOCUMENT_VALIDATE_CHECK_UPDATED_AT_SQL)
                    )

            async with engine.begin() as finalize_conn:
                await finalize_conn.execute(
                    text(_KB_DOCUMENT_SET_NOT_NULL_UPDATED_AT_SQL)
                )
                await finalize_conn.execute(
                    text(_KB_DOCUMENT_DROP_CHECK_UPDATED_AT_SQL)
                )
        except Exception as exc:
            logger.error(
                "Failed to apply live-safe NOT NULL on knowledge_documents.updated_at: %s",
                format_exception_message(exc),
                exc_info=True,
            )
            raise RuntimeError(
                "Schema-extras bootstrap failed — could not finalize the "
                "live-safe NOT NULL sequence on updated_at; refusing to start."
            ) from exc

    # ── Bootstrap indexes: plain DDL on an AUTOCOMMIT connection ──
    # Plain ``CREATE INDEX IF NOT EXISTS`` / ``DROP INDEX IF EXISTS``,
    # NOT ``CONCURRENTLY`` — see the deadlock note on the SQL constants:
    # concurrent DDL waits for every older snapshot and hangs the
    # boot.  AUTOCOMMIT so
    # each statement commits (and releases its lock) immediately.
    #
    # Two production-safety hardenings on top of the basic AUTOCOMMIT
    # +``IF NOT EXISTS`` shape:
    #
    # 1. ``SET statement_timeout = 0`` before the build.  Every
    #    connection from this engine inherits the 30 s timeout set in
    #    ``app/core/database.py`` via ``connect_args``; on populated
    #    production tables an index build can take minutes, so without
    #    this reset the build would time out and fail the boot.
    #    Session-level so other pooled connections keep their 30 s
    #    guard.
    # 2. Pre-check for and drop any leftover ``INVALID`` index from a
    #    prior failed bootstrap (older deploys killed mid-build).
    #    ``IF NOT EXISTS`` only checks existence — it does not check
    #    ``indisvalid`` — so without the explicit drop, an invalid
    #    index would block bootstrap forever and the sweeper would
    #    silently seq-scan.
    try:
        async with engine.connect() as idx_conn:
            autocommit_conn = await idx_conn.execution_options(
                isolation_level="AUTOCOMMIT"
            )
            await autocommit_conn.execute(text(_DISABLE_STATEMENT_TIMEOUT_SQL))

            # One AUTOCOMMIT pass creates/repairs every bootstrap-managed
            # concurrent index on the table — the ``(status, updated_at)``
            # sweeper index and the ``(knowledge_base_id, status)`` count
            # index — so a single SET statement_timeout=0 covers both
            # and we don't open a second connection (one AUTOCOMMIT flip).
            for index_name, create_sql, drop_sql in _KB_DOCUMENT_CONCURRENT_INDEXES:
                invalid_row = await autocommit_conn.execute(
                    text(_KB_DOCUMENT_STATUS_UPDATED_AT_INDEX_INVALID_CHECK_SQL),
                    {"index_name": index_name},
                )
                if invalid_row.scalar() is not None:
                    logger.warning(
                        "Detected invalid %s from prior failed bootstrap — "
                        "dropping before re-creating so "
                        "``CREATE INDEX IF NOT EXISTS`` does not silently skip.",
                        index_name,
                    )
                    await autocommit_conn.execute(text(drop_sql))

                await autocommit_conn.execute(text(create_sql))
    except Exception as exc:
        logger.error(
            "Failed to ensure knowledge_documents concurrent indexes "
            "(status/updated_at, knowledge_base_id/status): %s",
            format_exception_message(exc),
            exc_info=True,
        )
        raise RuntimeError(
            "Schema-extras bootstrap failed — the stuck-document sweeper would "
            "seq-scan without the (status, updated_at) index and "
            "list_knowledge_bases would time out without the "
            "(knowledge_base_id, status) index; refusing to start."
        ) from exc

    logger.info(
        "Schema extras ensured: knowledge_documents trigger + "
        "(status, updated_at) + (knowledge_base_id, status) indexes"
    )


async def _ensure_model_indexes() -> None:
    """Create any model-declared index missing from the live database.

    The single generic guarantee that ``Base.metadata`` and the database
    converge on index set, regardless of how the DB was bootstrapped:

    * **Fresh install** — ``create_all`` already built every model index, so
      this is a pure no-op (a few catalog ``SELECT``s).
    * **Stamped-fresh install** (bootstrapped via ``create_all`` + ``stamp
      head`` before the index existed on the model) — the migration body that
      would have created the index never ran; this pass creates it.
    * **Normal upgrade** — most indexes already exist; only genuinely-missing
      ones are created.  The column-set guard prevents creating a duplicate of
      a legacy-named index that survived a table rename.

    Uses a dedicated ``AUTOCOMMIT`` connection so each plain ``CREATE INDEX
    IF NOT EXISTS`` commits — and releases its ``SHARE`` lock — immediately
    rather than holding every lock until the end of one big transaction, with
    ``statement_timeout = 0`` (an in-place build on a populated table, notably
    the GIN full-text indexes, can legitimately exceed the 30 s per-connection
    default set in ``app/core/database.py``).  The reconciler deliberately uses
    a plain in-place build, NOT ``CREATE INDEX CONCURRENTLY`` — a concurrent
    build hangs indefinitely behind another live pod's idle-in-transaction pool
    sessions during a rolling deploy.  Failures are logged and re-raised so a
    boot that cannot guarantee the index set halts loudly rather than silently
    serving seq-scans under load.
    """
    from app.core.index_maintenance import reconcile_model_indexes

    try:
        async with engine.connect() as conn:
            autocommit_conn = await conn.execution_options(
                isolation_level="AUTOCOMMIT"
            )
            await autocommit_conn.execute(text(_DISABLE_STATEMENT_TIMEOUT_SQL))
            created = await autocommit_conn.run_sync(
                lambda sync_conn: reconcile_model_indexes(sync_conn)
            )
    except Exception as exc:
        logger.error("Model index reconciliation failed: %s", format_exception_message(exc), exc_info=True)
        raise RuntimeError(
            "Index reconciliation failed — refusing to start with a schema "
            "that is missing model-declared indexes (queries would silently "
            "seq-scan under load). Fix the failing index and restart."
        ) from exc

    if created:
        logger.warning(
            "Reconciled %d missing index(es) on boot: %s",
            len(created),
            ", ".join(created),
        )
    else:
        logger.info("Model indexes ensured: all present, nothing to reconcile")
