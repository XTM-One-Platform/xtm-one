"""add model_catalog_announcements.source

Who asked for an announcement: ``env`` (the env seed changed the catalog; every
existing row is one) or ``admin`` (a default model or a model
list an admin changed in Settings). The env watch compares only with
``env`` rows, so an admin announcement never makes a boot announce a change.

Lock profile: a ``NOT NULL`` column with a CONSTANT default is metadata-only on
PG 11+, but the ALTER still takes ACCESS EXCLUSIVE on the table, which the
scheduler reads every tick. Canonical pre-flight: idempotency probe ->
terminate_lock_holders -> set_lock_timeout -> guarded DDL.

Revision ID: a4996mcsrc01
Revises: i7con1939005
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "a4996mcsrc01"
down_revision: str | None = "i7con1939005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "model_catalog_announcements"
_COLUMN = "source"


def _column_exists(conn, table: str, column: str) -> bool:
    # Scoped to the active schema: information_schema.columns spans every
    # schema, and a same-named table elsewhere must not skip the ADD COLUMN.
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
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} VARCHAR(16) NOT NULL DEFAULT 'env'")


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
