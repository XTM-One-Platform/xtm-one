"""add work_items.not_before

``not_before`` is the earliest time an inbox may pick a work item up;
``due_at`` stays the deadline and no longer defers the pickup (#8753). A
nullable column with no default: ``ADD COLUMN`` is metadata-only, existing
rows keep their ``due_at`` and read NULL (pickup now), except the open owned
items a future ``due_at`` deferred until now: the backfill copies that
``due_at`` into ``not_before`` so they keep their deferral across the deploy.

Revision ID: n8753notbef01
Revises: t6593accreq01
Create Date: 2026-11-05 10:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "n8753notbef01"
# NOT a constant: must always be the base branch's CURRENT head, re-pointed
# whenever it gains a migration (backend/AGENTS.md > "Database migrations
# (safety)", rule 1).
down_revision: str | Sequence[str] | None = "t6593accreq01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "work_items"
_COLUMN = "not_before"


def _column_exists(conn) -> bool:
    # Scoped to the active schema: information_schema spans every schema.
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
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} TIMESTAMP WITH TIME ZONE")
    # Items an inbox skipped because their due_at was ahead keep that pickup
    # time: before this column, a future due_at was the deferral.
    op.execute(
        "UPDATE work_items SET not_before = due_at "
        "WHERE status = 'open' AND owner_agent_id IS NOT NULL AND due_at > now()"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
