"""add agents.web_uploads_enabled

Whether a visitor of the agent's public chat widget may attach a file to a
message. Off for every widget, existing and new: the constant default leaves
every row as it was, and only the widget's owner turns it on.

Lock profile: a ``NOT NULL`` column with a CONSTANT default is metadata-only on
PG 11+, but the ALTER still takes ACCESS EXCLUSIVE on ``agents``, a hot table
(every chat turn and run reads it), so an open transaction that merely read it
queues the ALTER behind itself in a rolling deploy. Canonical pre-flight:
idempotency probe -> terminate_lock_holders -> set_lock_timeout -> guarded DDL.

Revision ID: w4646upload01
Revises: p4880apiurl01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "w4646upload01"
down_revision: str | None = "p4880apiurl01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agents"
_COLUMN = "web_uploads_enabled"


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
    op.execute(
        f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} "
        "BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
