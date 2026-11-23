"""add assignment_runs.integration_auth_failures (#1454)

The integrations whose calls failed authentication in a run (401 credential
rejected, 403 forbidden on some tools), so a run produced without its data is
recorded as degraded instead of a plain success. Nullable JSON, no default, no
backfill: catalogue-only.

Revision ID: a1454authfail01
Revises: i126replysugus
Create Date: 2026-11-23 10:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "a1454authfail01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "i126replysugus"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("assignment_runs"):
        return
    columns = {c["name"] for c in inspector.get_columns("assignment_runs")}
    if "integration_auth_failures" in columns:
        return
    terminate_lock_holders(conn, "assignment_runs")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE assignment_runs ADD COLUMN IF NOT EXISTS integration_auth_failures JSON")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("assignment_runs"):
        return
    columns = {c["name"] for c in inspector.get_columns("assignment_runs")}
    if "integration_auth_failures" not in columns:
        return
    terminate_lock_holders(conn, "assignment_runs")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE assignment_runs DROP COLUMN IF EXISTS integration_auth_failures")
