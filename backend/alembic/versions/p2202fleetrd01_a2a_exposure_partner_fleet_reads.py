"""add a2a_exposures.partner_fleet_reads (#2202)

The per-exposure opt-in that lends a partner's task the read-only fleet
surface: its exposed agent's home fleet, that fleet's health and work items,
and the running build. Off by default (Sam's decision on #2202), so every
existing exposure keeps the boundary it had: NOT NULL DEFAULT false, no
backfill.

Revision ID: p2202fleetrd01
Revises: c2070coordret01
Create Date: 2026-11-25 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "p2202fleetrd01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c2070coordret01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("a2a_exposures"):
        return
    if "partner_fleet_reads" in {c["name"] for c in inspector.get_columns("a2a_exposures")}:
        return
    terminate_lock_holders(conn, "a2a_exposures")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE a2a_exposures ADD COLUMN IF NOT EXISTS partner_fleet_reads BOOLEAN NOT NULL DEFAULT false")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("a2a_exposures"):
        return
    if "partner_fleet_reads" not in {c["name"] for c in inspector.get_columns("a2a_exposures")}:
        return
    terminate_lock_holders(conn, "a2a_exposures")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE a2a_exposures DROP COLUMN IF EXISTS partner_fleet_reads")
