"""add video_renders.captions_burned_in

Whether a render's captions are already in its frame: the player then
leaves the WebVTT track off instead of drawing the same words a second time
over them. Nullable boolean with no default: NULL (every render before this
one) reads as burned in, the default those renders had, so existing rows need
no backfill and the ``ADD COLUMN`` is metadata-only.

Revision ID: vcapburn5905
Revises: gaskill5901
Create Date: 2026-10-17 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "vcapburn5905"
down_revision: str | None = "gaskill5901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("video_renders"):
        return
    columns = {c["name"] for c in inspector.get_columns("video_renders")}
    if "captions_burned_in" in columns:
        return
    terminate_lock_holders(conn, "video_renders")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE video_renders ADD COLUMN IF NOT EXISTS captions_burned_in BOOLEAN")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("video_renders"):
        return
    columns = {c["name"] for c in inspector.get_columns("video_renders")}
    if "captions_burned_in" not in columns:
        return
    terminate_lock_holders(conn, "video_renders")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE video_renders DROP COLUMN IF EXISTS captions_burned_in")
