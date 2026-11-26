"""add evaluation_episodes.superseded_idempotency_keys

The callback keys an episode accepted before its latest one, so a replay of an
earlier callback is refused. No backfill: the latest key stays in
``idempotency_key``, and the next accepted callback moves it here.

Revision ID: e684cbkeys01
Revises: x1784hubkey01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "e684cbkeys01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "x1784hubkey01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "evaluation_episodes"
_COLUMN = "superseded_idempotency_keys"


def _column_exists(conn) -> bool:
    # Scoped to the active schema: information_schema.columns spans every schema.
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": _TABLE, "column": _COLUMN},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _column_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    # Constant default: metadata-only, no table rewrite.
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} JSONB NOT NULL DEFAULT '[]'::jsonb")


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
