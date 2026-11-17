"""agents and fleets: hide_activity_from_non_admins (#11624)

An option, off by default on every agent and fleet, that hides the runs,
traces, work items and shared state of a company-managed agent or fleet from
non-administrators. ``ADD COLUMN ... NOT NULL DEFAULT false`` is metadata only
on PostgreSQL 11+: no table rewrite, every existing row reads false.

Revision ID: hideact11624
Revises: ctemmovers11547
Create Date: 2026-11-13 15:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "hideact11624"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "ctemmovers11547"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("agents", "fleets")
_COLUMN = "hide_activity_from_non_admins"


def _has_column(conn, table: str) -> bool:
    """Idempotency probe (lock-safe recipe step 1): no lock taken."""
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": _COLUMN},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    missing = [table for table in _TABLES if not _has_column(conn, table)]
    if not missing:
        return
    for table in missing:
        terminate_lock_holders(conn, table)
    set_lock_timeout(conn)
    for table in missing:
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {_COLUMN} BOOLEAN NOT NULL DEFAULT false")


def downgrade() -> None:
    conn = op.get_bind()
    present = [table for table in _TABLES if _has_column(conn, table)]
    if not present:
        return
    for table in present:
        terminate_lock_holders(conn, table)
    set_lock_timeout(conn)
    for table in present:
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {_COLUMN}")
