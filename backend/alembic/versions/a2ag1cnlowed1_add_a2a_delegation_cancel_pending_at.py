"""record a partner cancel this side owes until the partner has been told

Cancelling a run tree ends the local half in the caller's transaction and
tells the partners afterwards, in a detached task: reaching N partners is N
network calls, and the caller is often a partner waiting to hear its own
cancel took. The obligation lived only in memory between the two, so a hard
kill (OOM, node eviction) after the commit lost it, and the partner kept
working - and billing - until its own lifetime bound, while the floor sweep
could only ever abandon the row, which cancels nothing.

``cancel_pending_at`` is written before any partner is called - for a run
tree in the decision's own transaction - and kept once the row concludes: a
row still outstanding with it set is a cancel still owed, which the client
sweep re-sends after a grace period.

No backfill. NULL is "no cancel owed", which is true of every existing row.

``ADD COLUMN`` with no default is metadata-only; the canonical lock-safe recipe
still applies because the ALTER takes an ACCESS EXCLUSIVE lock: idempotency
probe -> terminate_lock_holders -> set_lock_timeout -> guarded DDL.

Revision ID: a2ag1cnlowed1
Revises: s2977stale01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "a2ag1cnlowed1"
down_revision: str | None = "s2977stale01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "a2a_delegations"
_COLUMN = "cancel_pending_at"


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
