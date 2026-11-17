"""add a2a_tasks.terminal_announced_at — record that a partner was told

A task's terminal state is published to its stream and its callbacks by
whichever path ends it. Three can, and one of them ends the task without the
transition being visible at the moment it happens: a run that finishes while
work it delegated is still running leaves the task ``working``, and the
terminal transition arrives later, when the child finishes and nothing is
executing the task to notice.

The reaper sweep catches that one. It needs to know which tasks it has already
announced, or it would republish every minute — hence a stamp rather than a
derivation: "have we told them" is not a fact about the task's state.

Existing terminal tasks are backfilled so the first sweep after this revision
does not announce months of history to every registered callback. Their
run's ``completed_at`` is the honest timestamp where there is one.

Nullable ``ADD COLUMN`` with no default is metadata-only on PG 11+; the
canonical lock-safe recipe still applies because the ALTER takes an
ACCESS EXCLUSIVE lock: idempotency probe -> terminate_lock_holders ->
set_lock_timeout -> guarded DDL.

Revision ID: a2a9announce1
Revises: a2a8push0001
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "a2a9announce1"
down_revision: str | None = "a2a8push0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "a2a_tasks"
_COLUMN = "terminal_announced_at"
_TERMINAL_RUN_STATUSES = ("completed", "failed", "cancelled")


def _column_exists(conn, table: str, column: str) -> bool:
    # Scoped to the active schema: information_schema.columns spans all of
    # them, and a same-named table elsewhere must not decide this probe.
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    # NOT gated on the column being absent: ``create_all`` runs before this on
    # a fresh install, so the column can already exist with the backfill still
    # owed. The DDL is guarded on its own and the backfill only touches rows
    # the stamp is missing on, so both are safe to re-run.
    if not _column_exists(conn, _TABLE, _COLUMN):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(
            f"ALTER TABLE {_TABLE} "
            f"ADD COLUMN IF NOT EXISTS {_COLUMN} TIMESTAMPTZ"
        )

    # An already-finished task was either announced by the executor before this
    # revision or never will be; either way the sweep must not treat it as
    # owed. Rejected tasks never had a run and are terminal on arrival.
    conn.execute(
        sa.text(
            f"""
            UPDATE {_TABLE} t SET {_COLUMN} =
                   COALESCE(r.completed_at, t.updated_at, now())
              FROM assignment_runs r
             WHERE r.id = t.run_id
               AND t.{_COLUMN} IS NULL
               AND r.status = ANY(:statuses)
            """  # noqa: S608 - no input: every name is a module constant
        ),
        {"statuses": list(_TERMINAL_RUN_STATUSES)},
    )
    conn.execute(
        sa.text(
            f"""
            UPDATE {_TABLE} SET {_COLUMN} = COALESCE(updated_at, now())
             WHERE {_COLUMN} IS NULL AND rejected_reason IS NOT NULL
            """  # noqa: S608 - as above
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
