"""add video_renders.stalls

The times a render stood still and was resumed in place, its finished frames
kept: where it stood, for how long and the frames kept, said by
``get_video_jobs``. Nullable with no default, so the ``ADD COLUMN`` is
metadata-only and needs no backfill.

Revision ID: v6101stall01
Revises: v5921arefs01
Create Date: 2026-10-20 12:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v6101stall01"
down_revision: str | None = "v5921arefs01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "video_renders"
_COLUMN = "stalls"


def _has_column(conn) -> bool | None:
    """Whether the column is there; ``None`` without the table."""
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return None
    return _COLUMN in {c["name"] for c in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn) is not False:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} JSON")


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
