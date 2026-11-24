"""add platform_settings.ctem_case_opening (#1951)

The administrator switch that pauses and resumes CTEM case opening, per
vertical: ``{vertical: false}`` pauses it. A nullable JSON column, NULL means
case opening is on everywhere (the behaviour before this column): no backfill.

Revision ID: c1951caseopen01
Revises: h1943healthgrp01
Create Date: 2026-11-24 18:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c1951caseopen01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "h1943healthgrp01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("platform_settings"):
        return
    if "ctem_case_opening" in {c["name"] for c in inspector.get_columns("platform_settings")}:
        return
    terminate_lock_holders(conn, "platform_settings")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE platform_settings ADD COLUMN IF NOT EXISTS ctem_case_opening JSON")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("platform_settings"):
        return
    if "ctem_case_opening" not in {c["name"] for c in inspector.get_columns("platform_settings")}:
        return
    terminate_lock_holders(conn, "platform_settings")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE platform_settings DROP COLUMN IF EXISTS ctem_case_opening")
