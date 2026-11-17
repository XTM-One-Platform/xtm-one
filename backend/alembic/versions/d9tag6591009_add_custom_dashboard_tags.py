"""Add custom_dashboards.tags: the words a person files a dashboard under.

The dashboards list sorts and filters by them, as OpenCTI's and OpenAEV's do.
One JSON column with a constant default (metadata-only), behind the canonical
recipe: idempotency probe -> terminate_lock_holders -> set_lock_timeout ->
guarded DDL. Every existing dashboard has no tag.

Revision ID: d9tag6591009
Revises: d8pin6591008
Create Date: 2026-10-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "d9tag6591009"
down_revision: str | None = "d8pin6591008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = 'custom_dashboards' AND column_name = 'tags'"
            )
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _column_exists(conn):
        return
    terminate_lock_holders(conn, "custom_dashboards")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE custom_dashboards ADD COLUMN IF NOT EXISTS tags JSON NOT NULL DEFAULT '[]'")


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn):
        return
    terminate_lock_holders(conn, "custom_dashboards")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE custom_dashboards DROP COLUMN IF EXISTS tags")
