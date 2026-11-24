"""add users.reply_suggestions_enabled

Each person may turn off the composer's reply suggestion
(``services/chat_reply_suggestions.py``), on by default:
``users.reply_suggestions_enabled`` (boolean, NOT NULL, default true). Off =
no suggestion is generated for that person, whatever the platform switch
(``platform_settings.reply_suggestions_enabled``, ``i125replysugps``) says.

Lock safety: canonical recipe - idempotency probe + early return, then
``terminate_lock_holders`` + ``set_lock_timeout``, then ``ADD COLUMN IF NOT
EXISTS`` with a constant server default (PostgreSQL 11+ stores it in the
catalog: metadata only, no table rewrite). No index: read with its user.
Reversible: ``downgrade`` drops the column.

Revision ID: i126replysugus
Revises: i125replysugps
Create Date: 2026-11-19 18:01:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "i126replysugus"
down_revision: str | None = "i125replysugps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "users"
_COLUMN = "reply_suggestions_enabled"


def _table_exists(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": _TABLE},
        ).scalar()
    )


def _column_exists(conn) -> bool:
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
    # Fresh install: create_all builds the full table (column included) and
    # stamps head - this body never needs to run.
    if not _table_exists(conn):
        return
    # Steady-state re-run (a replica retried) takes no lock at all.
    if _column_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} BOOLEAN NOT NULL DEFAULT true")


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn) or not _column_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
