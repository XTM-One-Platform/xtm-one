"""add video_render_ranges

The scene ranges of a render drawn on several workers: one row per
range, its frames, its status, who draws it and what it measured. A new
table with no foreign key (``video_renders`` is write-hot), so old replicas
during a rolling deploy ignore it and nothing is backfilled. The column set,
the unique constraint and the index mirror the ORM model (``VideoRenderRange``
in ``app/models/video_studio.py``), so a fresh install's ``create_all`` and
this body produce the same names.

Revision ID: v6163range01
Revises: v6155rate01
Create Date: 2026-10-20 21:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "v6163range01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "v6155rate01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "video_render_ranges"


def upgrade() -> None:
    conn = op.get_bind()
    if sa.inspect(conn).has_table(_TABLE):
        return
    set_lock_timeout(conn)
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id UUID PRIMARY KEY,
            render_id UUID NOT NULL,
            digest VARCHAR(64) NOT NULL,
            part INTEGER NOT NULL,
            first_frame INTEGER NOT NULL,
            end_frame INTEGER NOT NULL,
            scenes JSON,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            frames_done INTEGER NOT NULL DEFAULT 0,
            drawn_by VARCHAR(200),
            execution_id UUID,
            error TEXT,
            measured JSON,
            started_at TIMESTAMPTZ,
            progress_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT uq_video_render_ranges_part UNIQUE (render_id, digest, part)
        )
        """
    )
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_video_render_ranges_render ON {_TABLE} (render_id)")


def downgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE):
        return
    set_lock_timeout(conn)
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
