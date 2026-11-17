"""add what a whole render draws to video_renders

``drawing`` on ``video_renders``: the revision a render's attempt read and the
digest of what it draws of each scene then (``render_checks.SceneDigests.drawn``);
a save whose revision changes nothing drawn carries the render in progress to
it (``projects.carry_renders``) instead of having it cancelled and drawn again,
and records here the revision's spec the render mixes its sound from.

Nullable with no default: a render made before this one has none, so the
``ADD COLUMN`` is metadata-only and needs no backfill.

Revision ID: v6339draw01
Revises: v6329fail01
Create Date: 2026-10-22 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v6339draw01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "v6329fail01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "video_renders"
_COLUMN = "drawing"


def _has_column(conn) -> bool | None:
    """Whether the column is there; ``None`` when the table is not."""
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return None
    return _COLUMN in {column["name"] for column in inspector.get_columns(_TABLE)}


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
