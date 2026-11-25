"""add platform_settings.coordination_retention (#2070)

The administrator's retention windows of the coordination tables (closed work
items, resolved shared state, archived items, resolved worksets, abandoned open
items). A nullable JSON column: NULL means every window is the environment
default (the behaviour before this column), so no backfill.

Revision ID: c2070coordret01
Revises: c1951caseopen01
Create Date: 2026-11-25 09:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c2070coordret01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c1951caseopen01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("platform_settings"):
        return
    if "coordination_retention" in {c["name"] for c in inspector.get_columns("platform_settings")}:
        return
    terminate_lock_holders(conn, "platform_settings")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE platform_settings ADD COLUMN IF NOT EXISTS coordination_retention JSON")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("platform_settings"):
        return
    if "coordination_retention" not in {c["name"] for c in inspector.get_columns("platform_settings")}:
        return
    terminate_lock_holders(conn, "platform_settings")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE platform_settings DROP COLUMN IF EXISTS coordination_retention")
