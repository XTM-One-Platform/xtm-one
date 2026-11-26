# SPDX-License-Identifier: Apache-2.0

"""Shared lock-safety helpers for Alembic migration bodies.

Why this module exists (production outage 2026-07-05)
------------------------------------------------------
``ALTER TABLE`` needs a brief ``ACCESS EXCLUSIVE`` lock even for
metadata-only changes (constant-default ``ADD COLUMN`` on PG 11+).
During a rolling deploy the previous-version pods keep serving traffic,
and long-lived open transactions (agentic loops hold DB sessions for
minutes) that have merely *read* the target table hold ``ACCESS SHARE``
locks that block the ALTER.  PostgreSQL queues fairly, so every
subsequent reader queues *behind* the blocked ALTER — the lock queue
never empties, the migration's ``lock_timeout`` fires (SQLSTATE 55P03,
"canceling statement due to lock timeout"), startup hard-fails, and the
platform is down.

There is NO cold table.  ``platform_settings`` — a singleton row nobody
"writes" to — took production down exactly this way because branding /
settings reads inside long transactions held ``ACCESS SHARE`` on it.

The canonical lock-safe DDL recipe (copy it for every migration)
-----------------------------------------------------------------
1. Idempotency probe first — early-return when there is nothing to do,
   so steady-state re-runs take NO lock at all.
2. ``terminate_lock_holders(conn, table)`` — pre-flight that kills
   same-role sessions holding any lock on the table so the ALTER
   acquires ``ACCESS EXCLUSIVE`` instantly.
3. ``set_lock_timeout(conn)`` — a short ``SET LOCAL lock_timeout``
   safety belt so a racing reconnect makes the DDL fail fast (55P03)
   instead of wedging the lock queue.  The startup runner
   (``app/core/migrations.py``) retries transient 55P03/57014 failures
   with backoff, so a fail-fast here self-heals on the next attempt.
4. The guarded DDL itself (``... IF NOT EXISTS`` / ``... IF EXISTS``).

These helpers are deliberately dependency-light (sqlalchemy + logging)
and stable: migration bodies import them at *runtime*, so behavioural
changes here apply to every not-yet-applied migration.  Only ever make
them safer.
"""

import logging

import sqlalchemy as sa

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

# Short safety-belt lock_timeout: the terminate pre-flight should make the
# DDL instant; if a reconnect races in and grabs a shared lock we fail fast
# within seconds (SQLSTATE 55P03) rather than wedging the lock queue for the
# whole startup window.  The startup runner retries these failures.
DEFAULT_LOCK_TIMEOUT = "5s"


def set_lock_timeout(conn, timeout: str = DEFAULT_LOCK_TIMEOUT) -> None:
    """Set a transaction-scoped ``lock_timeout`` safety belt.

    Uses ``set_config('lock_timeout', :timeout, is_local := true)`` — the
    parameterizable equivalent of ``SET LOCAL`` (``SET`` itself cannot take
    bound parameters) — so the timeout value is bound, never interpolated
    into the SQL string, and PostgreSQL validates the format.

    The setting auto-resets on commit/rollback.  NOTE: Alembic runs all
    pending migrations in ONE transaction by default, so the value set by
    one migration body persists into the next until commit — always call
    this explicitly rather than relying on a previous migration's value.
    """
    conn.execute(
        sa.text("SELECT set_config('lock_timeout', :timeout, true)"),
        {"timeout": timeout},
    )


def column_exists(conn, table: str, column: str) -> bool:
    """Whether *table* of the active schema has *column*: the idempotency probe of a column change."""
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).scalar()
    )


def terminate_lock_holders(conn, table: str) -> None:
    """Terminate every same-role session holding any lock on *table*.

    The pre-flight from ``3815952fa962_add_users_password_changed_at``
    (and siblings ``s1d2m3o4d5e6`` / ``c7em01userv1`` / ``f0ctembrfg01``),
    centralized: every holder is killed so the upcoming ``ALTER TABLE``
    acquires ``ACCESS EXCLUSIVE`` instantly.  Same-role only
    (``activity.usename = current_user``) so it succeeds without
    superuser / ``pg_signal_backend`` membership.  Best-effort: on any
    failure we log and let the subsequent ``ALTER TABLE`` succeed
    naturally or fail with a clear error (which the startup runner
    retries when transient).

    *table* MUST be a SQL identifier — validated with
    ``str.isidentifier`` as defence-in-depth before being interpolated
    into the ``pg_class.relname`` predicate.
    """
    if not table.isidentifier():
        raise ValueError(
            f"terminate_lock_holders requires a SQL identifier, got {table!r}"
        )

    # Build the table predicate separately (validated identifier) and
    # concatenate with ``+`` so this stays lint-clean (S608).
    # ``lock_held.granted`` restricts the match to sessions actually HOLDING
    # a lock — ``pg_locks`` also lists non-granted (waiting) requests, and
    # killing those would terminate unrelated sessions merely queued behind
    # the same lock instead of the true blockers.
    relname_filter = f"  AND cls.relname = '{table}'"
    sql_text = (
        "SELECT DISTINCT activity.pid, activity.usename, activity.state, "
        "                activity.application_name "
        "FROM pg_stat_activity activity "
        "JOIN pg_locks lock_held ON lock_held.pid = activity.pid "
        "JOIN pg_class cls ON cls.oid = lock_held.relation "
        "WHERE activity.pid != pg_backend_pid() "
        "  AND lock_held.granted "
        "  AND activity.datname = current_database() "
        "  AND activity.usename = current_user "
        + relname_filter
    )
    try:
        sessions = conn.execute(sa.text(sql_text)).fetchall()
    except Exception as exc:
        logger.warning(
            "Pre-flight query for lock holders on %s failed (%s) - "
            "proceeding to ALTER TABLE without pre-flight cleanup",
            table,
            format_exception_message(exc),
        )
        return

    if not sessions:
        logger.info("Pre-flight: no sessions hold any lock on %s", table)
        return

    logger.warning(
        "Pre-flight: terminating %d session(s) holding locks on %s "
        "(typically previous-version pod connections from the rolling deploy)",
        len(sessions),
        table,
    )
    for pid, usename, state, app_name in sessions:
        logger.warning(
            "  -> pid=%s user=%s state=%r app=%s", pid, usename, state, app_name
        )
        try:
            conn.execute(
                sa.text("SELECT pg_terminate_backend(:pid)"), {"pid": pid}
            )
        except Exception as exc:
            logger.warning(
                "    pg_terminate_backend(%s) failed (%s) - continuing", pid, format_exception_message(exc)
            )


# ---------------------------------------------------------------------------
# Where the upgrade run started (expand / contract)
# ---------------------------------------------------------------------------
#
# A CONTRACT migration drops schema that an older release still reads, so it is
# only safe once no replica of that release can be serving.  During a rolling
# deploy the replicas being replaced run the release the database was at when
# the upgrade BEGAN - not the revision the run has reached by the time the
# contract body executes: an install that jumps several releases applies the
# expand migration and the contract migration in the same run, while pods of
# the release before the expand phase still serve traffic.  ``alembic/env.py``
# records the revisions the database was at before the first pending migration
# of THIS Alembic run ran, under ``STARTING_HEADS_ATTRIBUTE``.
#
# That record is where the upgrade began only if it was not interrupted.
# ``autocommit_block()`` commits the run so far ("the database transaction
# preceding the block is unconditionally committed"), stamped with the revision
# BEFORE the migration that opened the block.  An upgrade that fails or dies
# after such a commit - the startup runner retrying a lock timeout, a restarted
# pod - is finished by a NEW run, which records that committed revision while
# the replicas of the release the upgrade began on are still serving.  A
# contract migration must treat every revision an upgrade can resume from,
# between its expand revision and itself, as an unknown start
# (``c2797toudrop1._RESUME_POINTS``).

STARTING_HEADS_ATTRIBUTE = "starting_heads"


def run_starting_heads(migration_context) -> tuple[str, ...] | None:
    """The revisions the database was at when this Alembic run began.

    ``None`` when the run carries no record: a body executed outside
    ``env.py``, as a test driving ``MigrationContext`` directly does.  For an
    upgrade resumed after an interruption this is where the run RESUMED, not
    where the upgrade began (see the note above).
    """
    config = migration_context.config
    if config is None:
        return None
    heads = config.attributes.get(STARTING_HEADS_ATTRIBUTE)
    return None if heads is None else tuple(heads)


def applied_before_this_run(migration_context, revision: str) -> bool | None:
    """Was *revision* already applied when this Alembic run began?

    Answered from ``run_starting_heads``.  ``None`` when the run carries no
    record or the recorded heads cannot be resolved: a caller must read
    ``None`` as "cannot prove it is safe".  ``True`` alone does not prove the
    upgrade began after *revision*: a resumed run records where it resumed.
    """
    heads = run_starting_heads(migration_context)
    if heads is None:
        return None
    if not heads:
        return False
    # Imported here so the lock helpers above stay importable without Alembic's
    # script machinery.
    from alembic.script import ScriptDirectory

    try:
        script = ScriptDirectory.from_config(migration_context.config)
        return any(
            ancestor.revision == revision
            for ancestor in script.iterate_revisions(tuple(heads), "base")
        )
    except Exception as exc:
        logger.warning(
            "Could not resolve the starting revisions %s (%s) - cannot tell "
            "whether %s was applied before this run",
            heads,
            format_exception_message(exc),
            revision,
        )
        return None


# ---------------------------------------------------------------------------
# Concurrent index builds
# ---------------------------------------------------------------------------
#
# Why this needs its own helper (production outage 2026-08-28)
# ------------------------------------------------------------
# ``CREATE INDEX CONCURRENTLY`` is the lock-friendly way to add an index to a
# table that is being written to — but "lock-friendly" is not "bounded".  By
# design the build waits for EVERY transaction that can still see the table to
# finish, twice.  On a hot table that wait has no ceiling: an agentic loop
# holds a DB session open for minutes, and a single ``idle in transaction``
# session pins the build indefinitely.
#
# ``lock_timeout`` does NOT cover it.  The build is not queued on a lock; it is
# waiting on snapshots, and only ``statement_timeout`` can bound that wait.
# The migrations that predate this helper all set ``statement_timeout = 0``
# around their builds — correct for them, because every one of those indexes
# targets a table the SAME migration had just created, where no other session
# can possibly hold a snapshot.  Applied to a PRE-EXISTING hot table, that same
# pattern is an unbounded wait inside the blocking startup path: migrations run
# before ``_startup_done`` is set, so the whole platform serves 503 for exactly
# as long as the build waits.  That is how ``assignment_runs`` took prod and
# staging down.
#
# Two rules follow, and this helper enforces both:
#
#   1. BOUND the build.  A performance index is not worth an outage; if it
#      cannot be built within the ceiling, log loudly and let startup proceed.
#   2. CLEAN UP after an interrupted build.  A failed/killed
#      ``CREATE INDEX CONCURRENTLY`` leaves an INVALID index behind — still
#      maintained on every write, never used by the planner, the worst of both
#      — and ``IF NOT EXISTS`` then matches that corpse forever, so no later
#      deploy ever rebuilds it.  Always drop the invalid leftover first.

# Ceiling for a single concurrent index build.  Startup blocks behind it, so
# this is a "how long may the platform stay dark for one index" budget, not a
# "how long could this index take on the biggest imaginable table" estimate.
DEFAULT_INDEX_BUILD_TIMEOUT = "5min"

# Ceiling for the cleanup drop.  ``DROP INDEX CONCURRENTLY`` waits on the very
# same snapshots the build does — verified against PostgreSQL: with a blocker
# present the drop is cancelled by ``statement_timeout`` exactly like the build
# — so cleanup MUST carry its own short bound.  Without one it inherits the
# build ceiling and a single index costs startup twice the budget.  Deliberately
# short: an INVALID index is metadata-only to drop, so any real wait here is a
# blocker we are not going to outlast during startup anyway.
INDEX_CLEANUP_TIMEOUT = "30s"


def _drop_invalid_index(conn, index: str) -> bool:
    """Drop *index* if it exists but is INVALID.  Returns True if dropped.

    An ``INVALID`` index is the residue of a ``CREATE INDEX CONCURRENTLY``
    that failed or was killed (a redeploy replacing the pod mid-build is the
    common case).  It must be dropped before any retry, because
    ``CREATE INDEX CONCURRENTLY IF NOT EXISTS`` considers the name taken and
    skips the rebuild silently.
    """
    try:
        is_invalid = bool(
            conn.execute(
                sa.text(
                    "SELECT 1 FROM pg_class idx "
                    "JOIN pg_index i ON i.indexrelid = idx.oid "
                    "JOIN pg_namespace n ON n.oid = idx.relnamespace "
                    "WHERE idx.relname = :index "
                    "  AND n.nspname = current_schema() "
                    "  AND NOT i.indisvalid"
                ),
                {"index": index},
            ).scalar()
        )
    except Exception as exc:
        logger.warning(
            "Could not probe %s for validity (%s) - continuing",
            index,
            format_exception_message(exc),
        )
        return False

    if not is_invalid:
        return False

    logger.warning(
        "Dropping INVALID index %s left behind by an interrupted concurrent "
        "build (it is maintained on writes but never used, and would make "
        "IF NOT EXISTS skip the rebuild forever)",
        index,
    )
    drop_sql = f"DROP INDEX CONCURRENTLY IF EXISTS {index}"
    try:
        # Own, short bound: the drop waits on the same snapshots as the build,
        # so without this it would inherit the build ceiling (or run unbounded
        # when called before one is set).
        conn.execute(
            sa.text("SELECT set_config('statement_timeout', :timeout, false)"),
            {"timeout": INDEX_CLEANUP_TIMEOUT},
        )
        conn.execute(sa.text(drop_sql))
        return True
    except Exception as exc:
        logger.warning(
            "Could not drop INVALID index %s (%s). It stays behind: it is "
            "maintained on every write, never used by the planner, and makes "
            "IF NOT EXISTS skip any rebuild. Drop it by hand once the table "
            "is quiet:\n    %s;",
            index,
            format_exception_message(exc),
            drop_sql,
        )
        return False


def create_index_concurrently(
    conn,
    index: str,
    table: str,
    definition: str,
    timeout: str = DEFAULT_INDEX_BUILD_TIMEOUT,
) -> bool:
    """Build *index* concurrently, bounded, best-effort.  True if it exists after.

    MUST be called inside ``op.get_context().autocommit_block()`` —
    ``CONCURRENTLY`` cannot run inside a transaction.

    *definition* is the part after the table name, e.g.
    ``"(failure_cause) WHERE failure_cause IS NOT NULL"``.

    Best-effort by design: a build that exceeds *timeout* is logged at ERROR
    with the exact SQL to run by hand, the invalid residue is dropped, and the
    caller continues.  A missing performance index degrades some queries; a
    wedged migration takes the whole platform down (see the module note above).

    *index* and *table* MUST be SQL identifiers — validated as defence in depth
    before interpolation.
    """
    for name in (index, table):
        if not name.isidentifier():
            raise ValueError(
                f"create_index_concurrently requires SQL identifiers, got {name!r}"
            )

    # Clear the residue of any earlier interrupted build first, so this call
    # actually rebuilds instead of matching a corpse via IF NOT EXISTS.
    _drop_invalid_index(conn, index)

    sql = f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index} ON {table} {definition}"
    try:
        # Session-scoped (is_local=False): we are outside a transaction here,
        # so a LOCAL setting would not survive to the next statement.
        conn.execute(
            sa.text("SELECT set_config('statement_timeout', :timeout, false)"),
            {"timeout": timeout},
        )
        conn.execute(sa.text(sql))
        logger.info("Index %s on %s is ready", index, table)
        return True
    except Exception as exc:
        logger.error(
            "Concurrent build of %s on %s did not complete within %s (%s). "
            "Startup CONTINUES without it - the platform must not stay dark "
            "for an index. Run this by hand once the table is quiet:\n    %s;",
            index,
            table,
            timeout,
            format_exception_message(exc),
            sql,
        )
        # The failed build leaves an INVALID index; drop it so a future
        # deploy (or the manual command above) starts from a clean slate.
        _drop_invalid_index(conn, index)
        return False
    finally:
        try:
            conn.execute(sa.text("RESET statement_timeout"))
        except Exception:
            # A dead connection is already being reported by the caller.
            logger.debug("RESET statement_timeout failed", exc_info=True)


# ---------------------------------------------------------------------------
# In-place index builds
# ---------------------------------------------------------------------------
#
# ``CREATE INDEX CONCURRENTLY`` waits for every transaction whose snapshot
# predates the build, WHATEVER table that transaction reads - not only the
# sessions touching the indexed table.  During a rolling deploy the previous
# release keeps such transactions open for minutes (agentic loops, pooled
# ``idle in transaction`` sessions), so on a busy install the build rides its
# ceiling, the pod is restarted before the migration completes, and the next
# pod starts the same wait again: the upgrade never lands (workspaces).
# Replicas queued on the migration advisory lock can also deadlock it (40P01).
#
# A plain ``CREATE INDEX`` waits on none of that.  It takes a ``SHARE`` lock on
# its own table - reads continue, writes queue - for as long as the build runs,
# then (in autocommit) releases it.  After ``terminate_lock_holders`` the lock
# is granted at once, and building a btree on a column is seconds even on a
# large table.  It is the choice ``index_maintenance`` already made for the
# same reason.  A failed plain build rolls back its catalog entry, so it leaves
# no INVALID residue; one left by an earlier CONCURRENTLY attempt is dropped
# first, or ``IF NOT EXISTS`` would match it forever.


def _index_state(conn, index: str) -> bool | None:
    """``True`` valid, ``False`` INVALID, ``None`` absent or unreadable."""
    try:
        valid = conn.execute(
            sa.text(
                "SELECT i.indisvalid FROM pg_class idx "
                "JOIN pg_index i ON i.indexrelid = idx.oid "
                "JOIN pg_namespace n ON n.oid = idx.relnamespace "
                "WHERE idx.relname = :index AND n.nspname = current_schema()"
            ),
            {"index": index},
        ).scalar()
    except Exception as exc:
        logger.warning(
            "Could not probe %s for validity (%s) - continuing",
            index,
            format_exception_message(exc),
        )
        return None
    return None if valid is None else bool(valid)


def create_index_in_place(
    conn,
    index: str,
    table: str,
    definition: str,
    *,
    unique: bool = False,
    required: bool = False,
    timeout: str = DEFAULT_INDEX_BUILD_TIMEOUT,
) -> bool:
    """Build *index* with a plain ``CREATE INDEX``.  True if it exists after.

    MUST be called inside ``op.get_context().autocommit_block()``, so the
    ``SHARE`` lock is released the moment the build commits instead of being
    held until the end of the whole upgrade.

    *definition* is the part after the table name, e.g. ``"(workspace_id)"``.

    ``required=False`` (a performance index): a failure is logged and startup
    continues; ``_ensure_model_indexes`` builds a model-declared index right
    after the upgrade, without a lock timeout.  ``required=True`` (an index a
    later step of the migration depends on, a unique index replacing a
    constraint): the failure is raised - a lock or statement timeout is retried
    by the startup runner.

    *index* and *table* MUST be SQL identifiers - validated as defence in depth
    before interpolation.
    """
    for name in (index, table):
        if not name.isidentifier():
            raise ValueError(
                f"create_index_in_place requires SQL identifiers, got {name!r}"
            )

    state = _index_state(conn, index)
    if state is True:
        return True

    kind = "UNIQUE INDEX" if unique else "INDEX"
    sql = f"CREATE {kind} IF NOT EXISTS {index} ON {table} {definition}"
    terminate_lock_holders(conn, table)
    try:
        # Session-scoped (is_local=False): outside a transaction a LOCAL
        # setting would not survive to the next statement.
        conn.execute(
            sa.text("SELECT set_config('lock_timeout', :timeout, false)"),
            {"timeout": DEFAULT_LOCK_TIMEOUT},
        )
        conn.execute(
            sa.text("SELECT set_config('statement_timeout', :timeout, false)"),
            {"timeout": timeout},
        )
        if state is False:
            logger.warning(
                "Dropping INVALID index %s left behind by an interrupted "
                "concurrent build before rebuilding it in place",
                index,
            )
            conn.execute(sa.text(f"DROP INDEX IF EXISTS {index}"))
        conn.execute(sa.text(sql))
        logger.info("Index %s on %s is ready", index, table)
        return True
    except Exception as exc:
        if required:
            raise
        logger.error(
            "In-place build of %s on %s failed (%s). Startup CONTINUES: the "
            "model index reconciliation builds it after the upgrade. By hand:"
            "\n    %s;",
            index,
            table,
            format_exception_message(exc),
            sql,
        )
        return False
    finally:
        for setting in ("lock_timeout", "statement_timeout"):
            try:
                conn.execute(sa.text(f"RESET {setting}"))
            except Exception:
                logger.debug("RESET %s failed", setting, exc_info=True)
