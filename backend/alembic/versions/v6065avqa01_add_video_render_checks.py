"""add video_renders.checks and gate_override, video_projects.scene_previews

A render's audio and picture checks: what they found, each finding
with its time (``video_renders.checks``), and a forced render's override of
the quality gate (``video_renders.gate_override``). The scenes a scene
preview or a whole render drew, each with the digest of what it was then
(``video_projects.scene_previews``), so ``render_video`` refuses a scene
changed since anyone looked at it - on the video, never on a preview row the
render retention deletes. Nullable with no default, so each ``ADD COLUMN``
is metadata-only and needs no backfill.

Revision ID: v6065avqa01
Revises: v6072music01
Create Date: 2026-10-20 01:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v6065avqa01"
down_revision: str | None = "v6072music01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: ``table -> (column, type)`` this revision adds.
_COLUMNS = {
    "video_renders": (("checks", "JSON"), ("gate_override", "JSON")),
    "video_projects": (("scene_previews", "JSON"),),
}


def _missing(conn, table: str, *, present: bool = False) -> list[str]:
    """The columns of *table* this revision adds that are absent (with *present*, those there); none without the table."""
    inspector = sa.inspect(conn)
    if not inspector.has_table(table):
        return []
    names = {c["name"] for c in inspector.get_columns(table)}
    return [name for name, _kind in _COLUMNS[table] if (name in names) == present]


def upgrade() -> None:
    conn = op.get_bind()
    for table, columns in _COLUMNS.items():
        if not _missing(conn, table):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        for name, kind in columns:
            op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {kind}")


def downgrade() -> None:
    conn = op.get_bind()
    for table, columns in _COLUMNS.items():
        if not _missing(conn, table, present=True):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        for name, _kind in columns:
            op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {name}")
