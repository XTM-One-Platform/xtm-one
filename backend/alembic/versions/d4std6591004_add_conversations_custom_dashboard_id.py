"""Add conversations.custom_dashboard_id: the dashboard a studio conversation edits.

The Dashboard Studio docks a conversation next to a dashboard, as the
Presentation and Video Studios do: ``conversations.custom_dashboard_id`` is
the binding, the twin of ``presentation_id`` and ``video_project_id``. No
foreign key and no index, for the same reasons: ``conversations`` is the
hottest table and the code needs neither (the studio lists the caller's own
conversations, the ``user_id`` index narrows first; a deleted dashboard leaves
an ordinary chat).

One nullable column (metadata-only), behind the canonical recipe: idempotency
probe -> terminate_lock_holders -> set_lock_timeout -> guarded DDL.

Revision ID: d4std6591004
Revises: d3shr6591003
Create Date: 2026-10-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "d4std6591004"
down_revision: str | None = "d3shr6591003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = 'conversations' AND column_name = 'custom_dashboard_id'"
            )
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _column_exists(conn):
        return
    terminate_lock_holders(conn, "conversations")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS custom_dashboard_id UUID")


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn):
        return
    terminate_lock_holders(conn, "conversations")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE conversations DROP COLUMN IF EXISTS custom_dashboard_id")
