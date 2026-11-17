"""add video_renders.failure

Why the platform, not the video, failed a render: its cause - the
headless Chromium, the lease, memory, a crash, a stall - and whether it ran
again once on its revision first. A render's answer says the failure was the
platform's from it, never "fix what its reason names". Nullable with no
default, so the ``ADD COLUMN`` is metadata-only and needs no backfill.

Revision ID: v6329fail01
Revises: v6295tlay01
Create Date: 2026-10-22 15:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v6329fail01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "v6295tlay01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "video_renders"
_COLUMN = "failure"


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
