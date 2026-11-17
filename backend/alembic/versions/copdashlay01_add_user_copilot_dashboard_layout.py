"""add users.copilot_dashboard_layout

The Copilot dashboard each person built on the grid of widgets (the home of a
deployment that is not XTM One), a per-user preference written through
``PUT /settings/preferences``. Nullable JSON with no default: NULL is the
default layout, so existing rows need no backfill and the ``ADD COLUMN`` is
metadata-only.

Revision ID: copdashlay01
Revises: ctemdashlay01
Create Date: 2026-10-16 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "copdashlay01"
down_revision: str | None = "ctemdashlay01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("users"):
        return
    columns = {c["name"] for c in inspector.get_columns("users")}
    if "copilot_dashboard_layout" in columns:
        return
    terminate_lock_holders(conn, "users")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS copilot_dashboard_layout JSON")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("users"):
        return
    columns = {c["name"] for c in inspector.get_columns("users")}
    if "copilot_dashboard_layout" not in columns:
        return
    terminate_lock_holders(conn, "users")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS copilot_dashboard_layout")
