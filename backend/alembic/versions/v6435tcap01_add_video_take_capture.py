"""add how fast a recording captured its page to video_takes

``capture`` on ``video_takes``: what ``capture_rate.measure`` kept of the
take - its frames each second, each stretch it animated under 20 fps, the
stretches painted under its rate that a slowed scene blends, the GL backend,
the pixel ratio asked and recorded at, and the warm-up measured before step 1.

Nullable with no default: a take recorded before this one has none, so the
``ADD COLUMN`` is metadata-only and needs no backfill.

Revision ID: v6435tcap01
Revises: m6239manifest01
Create Date: 2026-10-24 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v6435tcap01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "m6239manifest01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "video_takes"
_COLUMN = "capture"


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
