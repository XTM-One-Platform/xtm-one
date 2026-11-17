"""Add conversations.fleet_id: the fleet a "Modify with AI" conversation edits.

The fleet page docks a conversation next to the fleet, as the Presentation,
Video and Dashboard Studios do: ``conversations.fleet_id`` is the binding, the
twin of ``presentation_id``, ``video_project_id`` and ``custom_dashboard_id``.
No foreign key and no index, for the same reasons: ``conversations`` is the
hottest table and the code needs neither (the rail lists the caller's own
conversations, the ``user_id`` index narrows first; a deleted fleet leaves an
ordinary chat).

One nullable column (metadata-only), behind the canonical recipe: idempotency
probe -> terminate_lock_holders -> set_lock_timeout -> guarded DDL.

Revision ID: f10162fltconv1
Revises: q10168fltinst1
Create Date: 2026-11-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "f10162fltconv1"
down_revision: str | None = "q10168fltinst1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = 'conversations' AND column_name = 'fleet_id'"
            )
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _column_exists(conn):
        return
    terminate_lock_holders(conn, "conversations")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS fleet_id UUID")


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn):
        return
    terminate_lock_holders(conn, "conversations")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE conversations DROP COLUMN IF EXISTS fleet_id")
