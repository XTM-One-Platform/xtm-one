"""add video_renders.superseded_by_id

A new render of a video cancels the renders of that video still queued or
running; each records the render that replaced it, so the render
history and the agent tools can say why it stopped. Nullable with no
default: a render before this one was replaced by nothing, so the
``ADD COLUMN`` is metadata-only and needs no backfill.

Revision ID: v5991rsupr01
Revises: v5977jwake01
Create Date: 2026-10-19 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v5991rsupr01"
down_revision: str | None = "v5977jwake01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("video_renders"):
        return
    columns = {c["name"] for c in inspector.get_columns("video_renders")}
    if "superseded_by_id" in columns:
        return
    terminate_lock_holders(conn, "video_renders")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE video_renders ADD COLUMN IF NOT EXISTS superseded_by_id UUID")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("video_renders"):
        return
    columns = {c["name"] for c in inspector.get_columns("video_renders")}
    if "superseded_by_id" not in columns:
        return
    terminate_lock_holders(conn, "video_renders")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE video_renders DROP COLUMN IF EXISTS superseded_by_id")
