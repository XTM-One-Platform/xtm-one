"""add agent_templates.must_be_defined — the wizard's mandatory setup steps

An agent template can now declare what the user still has to configure once an
agent is created from it (bind an integration of a given type, pick a
knowledge base, fill a persona placeholder). Each entry drives one mandatory
step of the guided creation wizard; ``required_integration_types``
stays as the informational hint the advanced form reads today.

Additive and lock-light: a nullable JSON column with no default is
metadata-only — no table rewrite, and old replicas during a rolling deploy
never select a column they do not know about.

Revision ID: wiz3158mbd01
Revises: b1bud3170001
Create Date: 2026-08-29 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "wiz3158mbd01"
down_revision: str | Sequence[str] | None = "b1bud3170001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agent_templates"
_COLUMN = "must_be_defined"


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
