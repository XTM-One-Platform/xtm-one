"""mark the evaluations the Concierge built (#8257)

``evaluations.concierge_built``: the mark the evaluation engine gate reads to
run a Concierge evaluation whatever the mode (decision B9). The Concierge has
no user row, so its evaluations have no owner and carry this mark instead.

Additive, no backfill: ``NOT NULL DEFAULT false`` is metadata-only on
PostgreSQL 11+, but still takes a brief ACCESS EXCLUSIVE lock on a table the
run dispatcher reads, so it follows the lock-safe recipe.

Revision ID: c4cnc8254041
Revises: c3cnc8254032
Create Date: 2026-11-03 22:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c4cnc8254041"
down_revision: str | Sequence[str] | None = "c3cnc8254032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = 'evaluations' AND column_name = 'concierge_built'"
            )
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn):
        return
    terminate_lock_holders(conn, "evaluations")
    set_lock_timeout(conn)
    op.add_column(
        "evaluations",
        sa.Column("concierge_built", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn):
        return
    terminate_lock_holders(conn, "evaluations")
    set_lock_timeout(conn)
    op.drop_column("evaluations", "concierge_built")
