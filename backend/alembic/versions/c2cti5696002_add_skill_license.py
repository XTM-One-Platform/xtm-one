"""add agent_skills.license - the licence of a skill, outside its body

The Agent Skills spec's frontmatter ``license`` field: the licence name and
copyright of a skill, or a reference to a bundled licence file. Kept as
metadata, never in the body the agent reads, so a third-party skill's
attribution (the CTI pack's MIT notice) is shown in the skill's Catalog tab
and carried by every copy without reading to the model as an instruction.

Additive and lock-light: a nullable TEXT column with no default is
metadata-only - no table rewrite, and old replicas during a rolling deploy
never select a column they do not know about.

Revision ID: c2cti5696002
Revises: c1cti5696001
Create Date: 2026-10-12 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c2cti5696002"
down_revision: str | Sequence[str] | None = "c1cti5696001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agent_skills"
_COLUMN = "license"


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
        sa.text(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} TEXT")
    )


def downgrade() -> None:
    conn = op.get_bind()

    if not _has_column(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}"))
