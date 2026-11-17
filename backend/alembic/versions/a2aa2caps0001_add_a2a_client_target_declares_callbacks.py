"""add a2a_client_targets.declares_callbacks

Whether a partner's card says it will call back. Nothing can choose between
asking a partner repeatedly and being told without knowing which it offers, and
the card is the only thing that says.

NULL means "not verified since this column existed", which reads as not
declared — the safe interpretation, and the one that keeps a mechanism from
being attempted against a partner that never offered it. No backfill: the
verification sweep rewrites every target within its staleness threshold, so the
values arrive on their own and a backfill could only guess.

Deliberately NOT part of the card fingerprint. That fingerprint suspends a
target until an operator accepts the change, so including this would
re-fingerprint every existing registration and suspend them all on the next
sweep — for a change that alters nothing this platform sends.

Nullable ``ADD COLUMN`` with no default is metadata-only on PG 11+; the
canonical lock-safe recipe still applies because the ALTER takes an
ACCESS EXCLUSIVE lock: idempotency probe -> terminate_lock_holders ->
set_lock_timeout -> guarded DDL.

Revision ID: a2aa2caps0001
Revises: a2aa1wait0001
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "a2aa2caps0001"
down_revision: str | None = "a2aa1wait0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "a2a_client_targets"
_COLUMN = "declares_callbacks"


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
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} BOOLEAN")


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
