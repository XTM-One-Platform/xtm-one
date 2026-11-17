"""add the conversation a Video Studio job's outcome is owed to

A job a chat turn starts records the conversation that still has to be told
how it ended: ``owed_conversation_id`` on ``video_takes``,
``video_clips``, ``video_music`` and ``video_renders``. The worker resumes
that conversation when the job ends after the turn did, and a tool that told
the agent clears it.

Nullable with no default: a job before this one is owed to nobody, so each
``ADD COLUMN`` is metadata-only and needs no backfill.

Revision ID: v5977jwake01
Revises: v5965bprof01
Create Date: 2026-10-19 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v5977jwake01"
down_revision: str | None = "v5965bprof01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("video_takes", "video_clips", "video_music", "video_renders")
_COLUMN = "owed_conversation_id"


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    for table in _TABLES:
        if not inspector.has_table(table):
            continue
        if _COLUMN in {c["name"] for c in inspector.get_columns(table)}:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {_COLUMN} UUID")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    for table in _TABLES:
        if not inspector.has_table(table):
            continue
        if _COLUMN not in {c["name"] for c in inspector.get_columns(table)}:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {_COLUMN}")
