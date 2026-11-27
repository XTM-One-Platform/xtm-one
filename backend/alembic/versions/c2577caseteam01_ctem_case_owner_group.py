"""a platform group owns a CTEM case as a team

Adds ``ctem_cases.owner_group_id`` (nullable UUID, foreign key to
``groups.id`` with ``ON DELETE SET NULL``, #2577): the platform group an asset
group or primary asset owner text names, when the case was routed to it. The
case keeps its person owner (``owner_user_id``, the team's lead). Nothing is
backfilled: the case sweep's owner pass re-routes the open cases.

Canonical lock-safe recipe (rule 5): idempotency probe, then
``terminate_lock_holders`` and ``set_lock_timeout`` before the DDL. The
downgrade drops the column.

Revision ID: c2577caseteam01
Revises: b2549profapps01
Create Date: 2026-11-27 07:50:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c2577caseteam01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "b2549profapps01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "ctem_cases"
_COLUMN = "owner_group_id"


def _column_exists(conn: sa.engine.Connection) -> bool:
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
    if not sa.inspect(conn).has_table(_TABLE) or _column_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} UUID REFERENCES groups(id) ON DELETE SET NULL")


def downgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE) or not _column_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
