"""drop users.pastel_colours (#1595)

The design system draws one chip style for every product since 1.13.0: the
standard / pastel split is gone and the ``.pastel`` root class is a no-op, so
the "Pastel colours" profile preference added by u1305pastel01 has nothing
left to switch. The column is dropped; downgrade puts it back with its
constant default (every row reads false, the former default).

Revision ID: u1595nopastel01
Revises: r1543assetrecords01
Create Date: 2026-11-23 20:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "u1595nopastel01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "r1543assetrecords01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("users"):
        return
    columns = {c["name"] for c in inspector.get_columns("users")}
    if "pastel_colours" not in columns:
        return
    terminate_lock_holders(conn, "users")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS pastel_colours")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("users"):
        return
    columns = {c["name"] for c in inspector.get_columns("users")}
    if "pastel_colours" in columns:
        return
    terminate_lock_holders(conn, "users")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS pastel_colours BOOLEAN NOT NULL DEFAULT false")
