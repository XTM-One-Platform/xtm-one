"""restart an abandoned delegation's clock when the partner finally speaks

A callback that arrives after this side gave up re-opens the row so the
collect that follows ends the row it finds rather than writing a second one.
But the floor measures SILENCE, and it measured it from ``submitted_at`` —
which is older than the abandonment cutoff by definition for any row that
reached this state. So a re-opened row was re-abandoned on the very next
cycle, resuming the caller with "The partner never reported a terminal state"
for a partner that had just called; and each retry of the partner's delivery
re-opened it again, so a partner doing exactly what the protocol asks drove
the loop.

This is where the restart is kept. ``submitted_at`` stays immutable — it is
what the history renders as the submission time, and mutating it would make
the row lie about when the work was sent — so the floor reads
``coalesce(reopened_at, submitted_at)`` instead: the last time this side heard
anything at all.

No backfill. NULL is "never re-opened", which is true of every existing row:
re-opening did not exist before the revision this one follows.

``ADD COLUMN`` with no default is metadata-only; the canonical lock-safe recipe
still applies because the ALTER takes an ACCESS EXCLUSIVE lock: idempotency
probe -> terminate_lock_holders -> set_lock_timeout -> guarded DDL.

Revision ID: a2af1reopen1
Revises: a2ae1aband01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "a2af1reopen1"
down_revision: str | None = "a2ae1aband01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "a2a_delegations"
_COLUMN = "reopened_at"


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
    if _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(
        f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} "
        "TIMESTAMP WITH TIME ZONE"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
