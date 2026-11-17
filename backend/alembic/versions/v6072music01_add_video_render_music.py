"""add video_renders.music

A render records the music it played: its source and, for an AI
track, the file it played, so a save can say whether the current render was
made with the music the video plays now (a refit keeps the track's id and the
video's revision). Nullable with no default, so the ``ADD COLUMN`` is
metadata-only and needs no backfill: a render made before it records none.

Revision ID: v6072music01
Revises: v6055chapt01
Create Date: 2026-10-20 02:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v6072music01"
down_revision: str | None = "v6055chapt01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(conn) -> bool | None:
    """Whether ``video_renders.music`` exists; ``None`` when the table does not."""
    inspector = sa.inspect(conn)
    if not inspector.has_table("video_renders"):
        return None
    return "music" in {c["name"] for c in inspector.get_columns("video_renders")}


def upgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn) is not False:
        return
    terminate_lock_holders(conn, "video_renders")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE video_renders ADD COLUMN IF NOT EXISTS music JSON")


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn):
        return
    terminate_lock_holders(conn, "video_renders")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE video_renders DROP COLUMN IF EXISTS music")
