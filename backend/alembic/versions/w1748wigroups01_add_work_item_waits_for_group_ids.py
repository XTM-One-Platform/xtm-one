"""add work_items.waits_for_group_ids (#1748)

A ``needs_human`` work item can wait for the members of one or more groups
instead of one person: the ids are stored here, membership is read at answer
time (a member added later answers, a member removed cannot) and the first
answer closes the decision for all. Nullable JSON, no backfill.

Revision ID: w1748wigroups01
Revises: n1883nodefsync01
Create Date: 2026-11-24 12:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "w1748wigroups01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "n1883nodefsync01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMN = "waits_for_group_ids"


def _present(conn: sa.engine.Connection) -> bool | None:
    inspector = sa.inspect(conn)
    if not inspector.has_table("work_items"):
        return None
    return _COLUMN in {c["name"] for c in inspector.get_columns("work_items")}


def upgrade() -> None:
    conn = op.get_bind()
    if _present(conn) is not False:
        return
    terminate_lock_holders(conn, "work_items")
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE work_items ADD COLUMN IF NOT EXISTS {_COLUMN} JSON")


def downgrade() -> None:
    conn = op.get_bind()
    if not _present(conn):
        return
    terminate_lock_holders(conn, "work_items")
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE work_items DROP COLUMN IF EXISTS {_COLUMN}")
