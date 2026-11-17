"""add video_renders.preflight and video_renders.measured

What the pre-flight said before a render was queued, and what its drawing
measured - the pages and CPUs it drew on, the frames it drew in how
long: the next pre-flights estimate from the last ones measured, and
``get_video_jobs`` says both. Nullable with no default, so each ``ADD COLUMN``
is metadata-only and needs no backfill.

Revision ID: v6155rate01
Revises: v6101stall01
Create Date: 2026-10-20 20:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v6155rate01"
down_revision: str | None = "v6101stall01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "video_renders"
_COLUMNS = ("preflight", "measured")


def _missing(conn) -> list[str] | None:
    """The columns the table lacks; ``None`` without the table."""
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return None
    present = {c["name"] for c in inspector.get_columns(_TABLE)}
    return [column for column in _COLUMNS if column not in present]


def upgrade() -> None:
    conn = op.get_bind()
    missing = _missing(conn)
    if not missing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for column in missing:
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {column} JSON")


def downgrade() -> None:
    conn = op.get_bind()
    missing = _missing(conn)
    if missing is None or len(missing) == len(_COLUMNS):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for column in _COLUMNS:
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {column}")
