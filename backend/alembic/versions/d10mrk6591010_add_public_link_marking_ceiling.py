"""Add custom_dashboard_public_links.marking_ceiling: the highest markings a published link shows.

Like an OpenCTI public dashboard's maximum markings: the snapshot leaves out
OpenCTI's data marked above them. One JSON column with a constant default
(metadata-only), behind the canonical recipe: idempotency probe ->
terminate_lock_holders -> set_lock_timeout -> guarded DDL. Every existing link
keeps no ceiling.

Revision ID: d10mrk6591010
Revises: d9tag6591009
Create Date: 2026-10-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "d10mrk6591010"
down_revision: str | None = "d9tag6591009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = 'custom_dashboard_public_links' AND column_name = 'marking_ceiling'"
            )
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _column_exists(conn):
        return
    terminate_lock_holders(conn, "custom_dashboard_public_links")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE custom_dashboard_public_links ADD COLUMN IF NOT EXISTS marking_ceiling JSON NOT NULL DEFAULT '[]'")


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn):
        return
    terminate_lock_holders(conn, "custom_dashboard_public_links")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE custom_dashboard_public_links DROP COLUMN IF EXISTS marking_ceiling")
