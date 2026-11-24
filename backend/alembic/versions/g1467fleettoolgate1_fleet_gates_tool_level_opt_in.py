"""add fleets.enforce_gates_at_tool_level, off for every fleet (#1467)

By design XTM One enables no programmatic tool-level gate by default: a
fleet's approval gates are instructions to its members (a decision item with
its ask card), and the platform pauses the matching tool calls only when an
administrator opts the fleet in. Boolean NOT NULL with a constant default
(catalogue-only since PostgreSQL 11): every existing fleet reads false, so the
upgrade turns tool-level enforcement off everywhere. The seed never writes it.

Revision ID: g1467fleettoolgate1
Revises: a1454authfail01
Create Date: 2026-11-23 10:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "g1467fleettoolgate1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "a1454authfail01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("fleets"):
        return
    columns = {c["name"] for c in inspector.get_columns("fleets")}
    if "enforce_gates_at_tool_level" in columns:
        return
    terminate_lock_holders(conn, "fleets")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE fleets ADD COLUMN IF NOT EXISTS enforce_gates_at_tool_level BOOLEAN NOT NULL DEFAULT false")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("fleets"):
        return
    columns = {c["name"] for c in inspector.get_columns("fleets")}
    if "enforce_gates_at_tool_level" not in columns:
        return
    terminate_lock_holders(conn, "fleets")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE fleets DROP COLUMN IF EXISTS enforce_gates_at_tool_level")
