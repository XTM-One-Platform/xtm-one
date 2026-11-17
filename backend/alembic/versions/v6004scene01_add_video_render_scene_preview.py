"""add video_renders.scene_id and frame_file_ids

A render may be a draft preview of one scene: ``scene_id`` names the
scene (NULL for a whole render, every render before this one), and
``frame_file_ids`` lists the preview's first, middle and last frames. Both
nullable with no default, so each ``ADD COLUMN`` is metadata-only and needs
no backfill.

Revision ID: v6004scene01
Revises: x5762ocqtool01
Create Date: 2026-10-19 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v6004scene01"
down_revision: str | None = "x5762ocqtool01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (("scene_id", "VARCHAR(64)"), ("frame_file_ids", "JSON"))


def _missing(conn, *, present: bool) -> list[str]:
    inspector = sa.inspect(conn)
    if not inspector.has_table("video_renders"):
        return []
    columns = {c["name"] for c in inspector.get_columns("video_renders")}
    return [name for name, _type in _COLUMNS if (name in columns) == present]


def upgrade() -> None:
    conn = op.get_bind()
    missing = _missing(conn, present=False)
    if not missing:
        return
    terminate_lock_holders(conn, "video_renders")
    set_lock_timeout(conn)
    for name, kind in _COLUMNS:
        if name in missing:
            op.execute(f"ALTER TABLE video_renders ADD COLUMN IF NOT EXISTS {name} {kind}")


def downgrade() -> None:
    conn = op.get_bind()
    present = _missing(conn, present=True)
    if not present:
        return
    terminate_lock_holders(conn, "video_renders")
    set_lock_timeout(conn)
    for name in present:
        op.execute(f"ALTER TABLE video_renders DROP COLUMN IF EXISTS {name}")
