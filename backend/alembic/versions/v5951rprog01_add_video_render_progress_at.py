"""add video_renders.progress_at

When a render last moved forward: its supervisor fails a render whose
progress has stood still past ``VIDEO_STUDIO_RENDER_STALL_SECONDS``, and the
render's status serves the time so a reader sees how long ago it moved.
Nullable with no default: a render before this one has none, so the
``ADD COLUMN`` is metadata-only and needs no backfill.

Revision ID: v5951rprog01
Revises: m5940heads01
Create Date: 2026-10-18 02:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v5951rprog01"
down_revision: str | None = "m5940heads01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("video_renders"):
        return
    columns = {c["name"] for c in inspector.get_columns("video_renders")}
    if "progress_at" in columns:
        return
    terminate_lock_holders(conn, "video_renders")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE video_renders ADD COLUMN IF NOT EXISTS progress_at TIMESTAMP WITH TIME ZONE")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("video_renders"):
        return
    columns = {c["name"] for c in inspector.get_columns("video_renders")}
    if "progress_at" not in columns:
        return
    terminate_lock_holders(conn, "video_renders")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE video_renders DROP COLUMN IF EXISTS progress_at")
