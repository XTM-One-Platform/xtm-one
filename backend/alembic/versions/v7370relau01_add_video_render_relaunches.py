"""add video_renders.relaunches

Each time another worker took a render over: the attempt, what it
resumed after, where it stood and the frame its drawing resumed from, said by
``get_video_jobs`` so a reader tells a relaunch from progress. Nullable JSON
with no default: a render before this one has none, so the ``ADD COLUMN`` is
metadata-only and needs no backfill.

Revision ID: v7370relau01
Revises: p7025taskwatch1
Create Date: 2026-11-02 18:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v7370relau01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "p7025taskwatch1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "video_renders"
_COLUMN = "relaunches"


def _columns(conn) -> set[str] | None:
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return None
    return {c["name"] for c in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    conn = op.get_bind()
    columns = _columns(conn)
    if columns is None or _COLUMN in columns:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} JSON")


def downgrade() -> None:
    conn = op.get_bind()
    columns = _columns(conn)
    if columns is None or _COLUMN not in columns:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
