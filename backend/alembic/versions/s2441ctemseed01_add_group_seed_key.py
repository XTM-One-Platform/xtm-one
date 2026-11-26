"""add groups.seed_key (#2441)

The stable key of a group the platform seeds: the CTEM access groups
"CTEM Viewers" (``ctem_viewers``) and "CTEM Operators" (``ctem_operators``),
created at boot in XTM One mode by ``services/ctem_groups.py``, which also puts
every active account in "CTEM Viewers" the first time it creates it. A
nullable unique column: catalogue-only, no backfill here.

Revision ID: s2441ctemseed01
Revises: p2202fleetrd01
Create Date: 2026-11-26 01:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "s2441ctemseed01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "p2202fleetrd01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("groups"):
        return
    if "seed_key" in {c["name"] for c in inspector.get_columns("groups")}:
        return
    terminate_lock_holders(conn, "groups")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE groups ADD COLUMN IF NOT EXISTS seed_key VARCHAR(64)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_groups_seed_key ON groups (seed_key)")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("groups"):
        return
    if "seed_key" not in {c["name"] for c in inspector.get_columns("groups")}:
        return
    terminate_lock_holders(conn, "groups")
    set_lock_timeout(conn)
    op.execute("DROP INDEX IF EXISTS uq_groups_seed_key")
    op.execute("ALTER TABLE groups DROP COLUMN IF EXISTS seed_key")
