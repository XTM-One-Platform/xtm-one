"""add the columns an outstanding delegation needs

A delegation is recorded while it is outstanding, not only once it concludes,
because three things need the row while the partner task is still running: a
collect, for its ownership check; the cancel cascade, to know what to cancel;
and a partner's callback, to find the run to resume.

``mechanism`` records how the delegation waited — asked, or called back — so an
operator can tell why one waited the way it did. ``callback_secret``
holds the per-delegation secret a partner presents back, encrypted at rest like
every other partner credential here. ``conversation_ref`` is what a CHAT
delegation is cancelled by: ``run_ref`` is NULL on that path, and a chat turn
ending must not cancel — handing back is what ends it — so the conversation is
the only boundary an outstanding task has.

No backfill. Every existing row is concluded, and none of them had a mechanism
to record.

Nullable ``ADD COLUMN`` with no default is metadata-only on PG 11+; the
canonical lock-safe recipe still applies because the ALTER takes an
ACCESS EXCLUSIVE lock: idempotency probe -> terminate_lock_holders ->
set_lock_timeout -> guarded DDL.

Revision ID: a2aa1wait0001
Revises: a2a9announce1
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "a2aa1wait0001"
down_revision: str | None = "a2a9announce1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "a2a_delegations"
_COLUMNS = {
    "mechanism": "VARCHAR(16)",
    "callback_secret": "TEXT",
    "conversation_ref": "UUID",
    "callback_claimed_at": "TIMESTAMPTZ",
}
_FK = "fk_a2a_delegations_conversation_ref"
_IX = "ix_a2a_delegations_conversation_ref"


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
    missing = [c for c in _COLUMNS if not _column_exists(conn, _TABLE, c)]
    if not missing:
        return
    # One lock belt for both columns rather than one each: the table is taken
    # ACCESS EXCLUSIVE either way, and twice is twice the chance of waiting.
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for column in missing:
        op.execute(
            f"ALTER TABLE {_TABLE} "
            f"ADD COLUMN IF NOT EXISTS {column} {_COLUMNS[column]}"
        )
    if "conversation_ref" in missing:
        # Outside the belt above: both take SHARE UPDATE EXCLUSIVE, which
        # blocks neither reads nor writes. NOT VALID first so the ALTER does
        # not scan the table under a stronger lock; every existing row has the
        # column NULL, so the validation that follows finds nothing to reject.
        op.execute(
            f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_FK} "
            "FOREIGN KEY (conversation_ref) REFERENCES conversations(id) "
            "ON DELETE CASCADE NOT VALID"
        )
        op.execute(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {_FK}")
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {_IX} ON {_TABLE} (conversation_ref)"
        )


def downgrade() -> None:
    conn = op.get_bind()
    present = [c for c in _COLUMNS if _column_exists(conn, _TABLE, c)]
    if not present:
        return
    op.execute(f"DROP INDEX IF EXISTS {_IX}")
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for column in present:
        # Dropping the column drops its constraint with it.
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {column}")
