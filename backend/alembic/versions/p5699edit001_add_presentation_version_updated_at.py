"""add presentation_versions.updated_at - the edit session of an autosaving editor

The slide editor saves on its own, every few seconds while someone
edits. Recorded one revision per save, the history would keep its 30 newest
entries for a few minutes of typing. A run of manual saves by one person is
therefore ONE entry, an edit session, moved forward to each new revision.
``updated_at`` is the latest save such an entry holds; NULL marks an entry
that is not an edit session (an agent edit, a restore, a template change,
every entry written before this column), which no later save continues.

Additive and lock-light: a nullable TIMESTAMPTZ column with no default is
metadata-only - no table rewrite, and old replicas during a rolling deploy
never select a column they do not know about (they keep writing NULL, which
reads as "not a session").

Revision ID: p5699edit001
Revises: c2cti5696002
Create Date: 2026-10-12 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "p5699edit001"
down_revision: str | Sequence[str] | None = "c2cti5696002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "presentation_versions"
_COLUMN = "updated_at"


def _has_column(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()

    # Idempotency probe + early return, so a steady-state re-run takes no
    # lock at all.
    if _has_column(conn, _TABLE, _COLUMN):
        return

    # Pre-flight: kill same-role lock holders so the ALTER acquires its brief
    # ACCESS EXCLUSIVE instantly, then fail fast rather than wedge the queue.
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)

    op.execute(sa.text(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} TIMESTAMP WITH TIME ZONE"))


def downgrade() -> None:
    conn = op.get_bind()

    if not _has_column(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}"))
