"""add the columns of the Video Studio's one media reference index

A purge read only the media a video's spec names in its scenes: the
files it names, what the AI shots it plays are made from and what its ready
renders drew were never in ``video_projects.asset_refs``. Three columns let the
index hold them:

* ``video_clips.source_refs`` - the media a shot is made from (its start
  frame and what it was cut from, its reference images, its ingredients),
  written on every flush of the shot; NULL for a shot written before, read from
  its params meanwhile;
* ``video_renders.asset_refs`` - the media the spec a render drew named,
  written when it ends ready;
* ``video_projects.asset_refs_version`` - the rule the video's index was
  written by; NULL (every video before this) is indexed again by the media
  retention's sweep, the index it had kept meanwhile.

Nullable with no default, so each ``ADD COLUMN`` is metadata-only and needs no
backfill.

Revision ID: v6164mref01
Revises: v6163range01
Create Date: 2026-10-20 21:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v6164mref01"
down_revision: str | None = "v6163range01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("video_clips", "source_refs", "JSON"),
    ("video_renders", "asset_refs", "JSON"),
    ("video_projects", "asset_refs_version", "INTEGER"),
)


def _present(conn, table: str) -> set[str] | None:
    """The columns of *table*; ``None`` without the table."""
    inspector = sa.inspect(conn)
    if not inspector.has_table(table):
        return None
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    conn = op.get_bind()
    for table, column, kind in _COLUMNS:
        present = _present(conn, table)
        if present is None or column in present:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {kind}")


def downgrade() -> None:
    conn = op.get_bind()
    for table, column, _kind in _COLUMNS:
        present = _present(conn, table)
        if present is None or column not in present:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
