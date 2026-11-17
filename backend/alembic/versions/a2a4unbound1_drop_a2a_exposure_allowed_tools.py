"""drop a2a_exposures.allowed_tools — external work is bounded by identity

External work runs an agent's own configuration in full, as the exposure's
run-as user; nothing narrows it by tool, so the per-exposure allow-list this
column held has no reader. Any values on existing rows go with it.

``DROP COLUMN`` is metadata-only on PG but takes an ACCESS EXCLUSIVE lock, so
the canonical lock-safe recipe applies: idempotency probe ->
terminate_lock_holders -> set_lock_timeout -> guarded DDL. The downgrade
re-adds it nullable; the data is not recoverable, and nothing would read it if
it were.

Revision ID: a2a4unbound1
Revises: b3f7prov1d
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "a2a4unbound1"
down_revision: str | None = "b3f7prov1d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "a2a_exposures"
_COLUMN = "allowed_tools"


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
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")


def downgrade() -> None:
    conn = op.get_bind()
    if _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} JSON")
