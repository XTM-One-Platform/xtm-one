"""record when the user last stopped a conversation's work

A partner's answer can be recorded just before the user presses Stop and be
delivered after it: the delegation row is already concluded, so the owed-cancel
stamp of a2ag1cnlowed1 has nothing to mark, and the detached partner wake - or
the follow-up a stopped wake turn starts from its stranded folds - posts the
answer into a conversation the user stopped. ``stopped_at`` is written by the
Stop's cancel cascade and read by the wake before it delivers.

No backfill. NULL is "never stopped", which is true of every existing row.

``conversations`` is one of the hottest tables on the platform. ``ADD COLUMN``
with no default is metadata-only, and the canonical lock-safe recipe applies
because the ALTER takes an ACCESS EXCLUSIVE lock: idempotency probe ->
terminate_lock_holders -> set_lock_timeout -> guarded DDL.

Revision ID: a2ah1stopped1
Revises: a2ag1cnlowed1
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "a2ah1stopped1"
down_revision: str | None = "a2ag1cnlowed1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "conversations"
_COLUMN = "stopped_at"


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
