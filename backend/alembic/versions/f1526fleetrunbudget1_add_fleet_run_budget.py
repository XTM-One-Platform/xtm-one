"""add fleets.run_budget, the optional per-fleet run budget (#1526)

``{runs_per_hour, tokens_per_hour}``, each optional, over the home members'
runs of the last rolling hour (``services/fleet_run_budget``). Nullable JSON
without a default (catalogue-only): every existing fleet reads NULL, no
budget, so the upgrade changes nothing. The seed never writes it.

Revision ID: f1526fleetrunbudget1
Revises: c1498surfacepass1
Create Date: 2026-11-23 13:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "f1526fleetrunbudget1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c1498surfacepass1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("fleets"):
        return
    columns = {c["name"] for c in inspector.get_columns("fleets")}
    if "run_budget" in columns:
        return
    terminate_lock_holders(conn, "fleets")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE fleets ADD COLUMN IF NOT EXISTS run_budget JSON")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("fleets"):
        return
    columns = {c["name"] for c in inspector.get_columns("fleets")}
    if "run_budget" not in columns:
        return
    terminate_lock_holders(conn, "fleets")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE fleets DROP COLUMN IF EXISTS run_budget")
