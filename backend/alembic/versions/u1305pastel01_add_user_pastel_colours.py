"""add users.pastel_colours (#1305)

The "Pastel colours" profile preference: the design system renders standard
colours by default and its pastel fills only under a ``.pastel`` root class,
which the client sets when this is true. Boolean NOT NULL with a constant
default (catalogue-only since PostgreSQL 11): existing rows read false, no
backfill.

Revision ID: u1305pastel01
Revises: c1289ctemattackpath1
Create Date: 2026-11-22 22:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "u1305pastel01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c1289ctemattackpath1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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


def downgrade() -> None:
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
