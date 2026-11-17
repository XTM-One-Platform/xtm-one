"""Add dashboard_widget_packs.inputs: the entity inputs a pack's widgets read.

An OpenCTI custom view is a dashboard shown on an entity's page; imported, its
widgets read the ``$entity$`` token and the pack carries the entity input that
sets it, bound to an integration when the pack is inserted into a dashboard.

One JSON column with a constant default (metadata-only), behind the canonical
recipe: idempotency probe -> terminate_lock_holders -> set_lock_timeout ->
guarded DDL.

Revision ID: d8pin6591008
Revises: d7lnk6591007
Create Date: 2026-10-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "d8pin6591008"
down_revision: str | None = "d7lnk6591007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = 'dashboard_widget_packs' AND column_name = 'inputs'"
            )
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _column_exists(conn):
        return
    terminate_lock_holders(conn, "dashboard_widget_packs")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE dashboard_widget_packs ADD COLUMN IF NOT EXISTS inputs JSON NOT NULL DEFAULT '[]'")


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn):
        return
    terminate_lock_holders(conn, "dashboard_widget_packs")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE dashboard_widget_packs DROP COLUMN IF EXISTS inputs")
