"""add agent_templates.default_skills - the skills an agent starts with

An agent template can now name the built-in skills (by slug) that an agent
created from it is attached to: the CTI templates seeded in XTM One mode start
with their tradecraft and investigation skills instead of an empty Skills step.

Additive and lock-light: a nullable JSON column with no default is
metadata-only - no table rewrite, and old replicas during a rolling deploy
never select a column they do not know about.

Revision ID: c1cti5696001
Revises: c5687ckpt01
Create Date: 2026-10-11 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c1cti5696001"
down_revision: str | Sequence[str] | None = "c5687ckpt01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agent_templates"
_COLUMN = "default_skills"


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

    op.execute(
        sa.text(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} JSON")
    )


def downgrade() -> None:
    conn = op.get_bind()

    if not _has_column(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}"))
