"""record that a delegation ended because the partner went silent

Every other terminal outcome follows something that happened: the partner
failed, the credential was rejected, the attempt hit its ceiling and CANCELLED
the task at the partner. Abandonment cancels nothing — it is this side giving
up on silence — so the work may still be running, and a callback that finally
arrives is the answer the caller was promised rather than a contradiction.

``outcome`` cannot carry that distinction: abandonment and a cancelled ceiling
are both ``timeout``, and they mean opposite things about whether a late
partner may still be believed. This column is what lets the callback route tell
them apart, and it answers an operator's own question too — a row that ended
without anything being decided is one whose work may still be billing at the
partner.

A timestamp rather than a flag, like ``callback_seen_at`` beside it: "when did
we give up" is what gets asked when a partner turns out not to have been gone.

No backfill. NULL is "this row was not abandoned", which is true of every
existing row: the only writer of it is the floor, going forward.

``ADD COLUMN`` with no default is metadata-only; the canonical lock-safe recipe
still applies because the ALTER takes an ACCESS EXCLUSIVE lock: idempotency
probe -> terminate_lock_holders -> set_lock_timeout -> guarded DDL.

Revision ID: a2ae1aband01
Revises: a2ad1cbseen1
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "a2ae1aband01"
down_revision: str | None = "a2ad1cbseen1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "a2a_delegations"
_COLUMN = "abandoned_at"


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
