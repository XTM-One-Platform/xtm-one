"""add video_renders.chapters

A whole render records its chapters: one per scene, where the scene
takes over and its own words, so a player can mark and name them. Nullable
with no default, so the ``ADD COLUMN`` is metadata-only and needs no
backfill: a render made before it simply has no chapters.

Revision ID: v6055chapt01
Revises: v6004scene01
Create Date: 2026-10-19 23:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v6055chapt01"
down_revision: str | None = "v6004scene01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(conn) -> bool | None:
    """Whether ``video_renders.chapters`` exists; ``None`` when the table does not."""
    inspector = sa.inspect(conn)
    if not inspector.has_table("video_renders"):
        return None
    return "chapters" in {c["name"] for c in inspector.get_columns("video_renders")}


def upgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn) is not False:
        return
    terminate_lock_holders(conn, "video_renders")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE video_renders ADD COLUMN IF NOT EXISTS chapters JSON")


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn):
        return
    terminate_lock_holders(conn, "video_renders")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE video_renders DROP COLUMN IF EXISTS chapters")
