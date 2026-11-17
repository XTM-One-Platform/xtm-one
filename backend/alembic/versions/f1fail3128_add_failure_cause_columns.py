"""persist why an agent run failed

``AssignmentRun.error`` is free text and ``llm_traces`` carries only
``status`` + ``error_message``: nothing classifiable, nothing aggregable. This
adds the structured cause the classifier in ``app/services/failure_causes.py``
produces, at both the run level (the record a future billing decision reads)
and the trace level (what the Trace Explorer filters on).

Three columns, all nullable, no backfill:

* ``assignment_runs.failure_cause`` — the enum value;
* ``assignment_runs.failure_determination`` — structural / heuristic /
  unattributed. Kept separate because it is NOT derivable from the cause: the
  same cause can be read off an SDK status code or recognised in a stringified
  message, and only the former is precise enough to bill on later;
* ``llm_traces.failure_cause`` — the same enum on the trace.

Historic rows read as NULL, which is honest: those failures were never
classified and inventing a cause for them would poison the very distribution
this feature exists to measure.

Additive and lock-light: ``ADD COLUMN`` with no default is metadata-only on
PG 11+ (no table rewrite), and the indexes are created ``CONCURRENTLY`` outside
the migration transaction so neither one blocks writes to the two busiest
tables in the system.

Both index builds go through ``create_index_concurrently``, which BOUNDS them.
Unbounded is not an option here: unlike every other ``CONCURRENTLY`` migration
in this tree, these indexes target pre-existing hot tables, a concurrent build
waits on every open snapshot over them, and this runs inside the blocking
startup path. Left unbounded it serves a blanket 503 for as long as it waits —
which is exactly what it did on 2026-08-28.

Revision ID: f1fail3128
Revises: p1fpool0001
Create Date: 2026-08-28 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import (
    create_index_concurrently,
    set_lock_timeout,
    terminate_lock_holders,
)

revision: str = "f1fail3128"
down_revision: str | Sequence[str] | None = "p1fpool0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUNS_TABLE = "assignment_runs"
_TRACES_TABLE = "llm_traces"

# (table, column, type) — every column this migration owns.
_COLUMNS: tuple[tuple[str, str, str], ...] = (
    (_RUNS_TABLE, "failure_cause", "VARCHAR(48)"),
    (_RUNS_TABLE, "failure_determination", "VARCHAR(16)"),
    (_TRACES_TABLE, "failure_cause", "VARCHAR(48)"),
)

# (index, table, definition body). Both are partial on ``failure_cause IS NOT
# NULL``: only failed rows carry one, and on ``assignment_runs`` (the largest
# table in the system) a full index would be almost entirely NULL entries.
_INDEXES: tuple[tuple[str, str, str, str], ...] = (
    (
        "ix_assignment_runs_failure_cause",
        _RUNS_TABLE,
        "failure_cause",
        "(failure_cause) WHERE failure_cause IS NOT NULL",
    ),
    (
        "ix_llm_traces_failure_cause_started_at",
        _TRACES_TABLE,
        "failure_cause",
        "(failure_cause, started_at) WHERE failure_cause IS NOT NULL",
    ),
)


def _has_column(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def _has_table(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :t"
            ),
            {"t": table},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()

    for table, column, coltype in _COLUMNS:
        # A ClickHouse-backed install still has both PostgreSQL tables (the
        # local sink is the default and the fallback), but guard anyway: a
        # missing table must skip, not abort the whole upgrade.
        if not _has_table(conn, table):
            continue
        # Idempotency probe first, so a steady-state re-run (fresh installs
        # stamp head; a retrying replica re-enters here) takes NO lock at all.
        if _has_column(conn, table, column):
            continue
        # An agentic loop holds a session open for minutes, and every
        # subsequent reader queues behind a blocked ALTER — clear same-role
        # lock holders so the brief ACCESS EXCLUSIVE is acquired instantly,
        # with a short lock_timeout so a racing reconnect fails fast (55P03)
        # instead of wedging the queue. The startup runner retries.
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(
            sa.text(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coltype}"
            )
        )

    # CONCURRENTLY must run outside a transaction and must NOT inherit the
    # lock_timeout belt above (the build waits on snapshots, not on a lock
    # queue, so lock_timeout would not bound it anyway).
    #
    # Unlike every other CONCURRENTLY migration in this tree, these two indexes
    # target PRE-EXISTING tables — and `assignment_runs` is the busiest one in
    # the system, routinely carrying sessions an agentic loop has held open for
    # minutes. A concurrent build waits for all of them, without limit. Startup
    # blocks here, so an unbounded wait means a blanket 503 for as long as it
    # lasts. `create_index_concurrently` bounds the build and, if it does not
    # finish in time, logs the SQL to run by hand and lets startup proceed:
    # these indexes only speed up failure-cause filtering, and no query plan is
    # worth keeping the platform dark.
    with op.get_context().autocommit_block():
        for index, table, column, definition in _INDEXES:
            if not _has_column(conn, table, column):
                continue
            create_index_concurrently(conn, index, table, definition)


def downgrade() -> None:
    conn = op.get_bind()

    with op.get_context().autocommit_block():
        for index, _table, _column, _definition in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index}")

    for table, column, _coltype in _COLUMNS:
        if not _has_table(conn, table) or not _has_column(conn, table, column):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}"))
