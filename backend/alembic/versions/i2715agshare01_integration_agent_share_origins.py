"""add integrations.agent_share_origins (#2715)

The grants an agent's sharing gave on the integrations bound to it, keyed by
principal (``user:<id>`` / ``group:<id>``) with the agent ids that gave them,
so an agent unshare removes only those. Nullable, no backfill: every grant
stored today was given by hand and keeps its no-origin meaning.

Revision ID: i2715agshare01
Revises: p2638ownerrd01
Create Date: 2026-11-27 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "i2715agshare01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "p2638ownerrd01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("integrations"):
        return
    if "agent_share_origins" in {c["name"] for c in inspector.get_columns("integrations")}:
        return
    terminate_lock_holders(conn, "integrations")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE integrations ADD COLUMN IF NOT EXISTS agent_share_origins JSON")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("integrations"):
        return
    if "agent_share_origins" not in {c["name"] for c in inspector.get_columns("integrations")}:
        return
    terminate_lock_holders(conn, "integrations")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE integrations DROP COLUMN IF EXISTS agent_share_origins")
