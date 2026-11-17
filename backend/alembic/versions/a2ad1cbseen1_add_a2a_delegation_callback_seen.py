"""record that a partner's callback arrived and lost the claim

The attempt that published a delegation row holds the callback claim while it
polls, so a partner finishing inside the grace period cannot have its answer
delivered twice — once inline and once as a wake turn. The cost is a window one
poll wide at the end of every grace period: a callback landing there loses the
claim, and the partner has its 200 and will not call again.

This is where that fact is kept. The attempt releases the claim when it stops,
and collects there if a callback was seen — instead of leaving an answer that
is already waiting to the floor, which would not look at the row for another
cycle.

A timestamp rather than a flag: "when" is what an operator asks when a
delegation resolved later than the partner said it had.

No backfill. NULL is "no callback was refused", which is true of every existing
row.

``ADD COLUMN`` with no default is metadata-only; the canonical lock-safe recipe
still applies because the ALTER takes an ACCESS EXCLUSIVE lock: idempotency
probe -> terminate_lock_holders -> set_lock_timeout -> guarded DDL.

Revision ID: a2ad1cbseen1
Revises: a2ac1pver001
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "a2ad1cbseen1"
down_revision: str | None = "a2ac1pver001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "a2a_delegations"
_COLUMN = "callback_seen_at"


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
