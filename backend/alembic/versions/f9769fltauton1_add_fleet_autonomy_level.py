"""add fleets.autonomy_level

Fleet autonomy level (#9769, slice S1): one stored level per fleet that says
how many of its gated actions wait for a person. ``full_gates`` (every gated
action waits) is today's behaviour, so every existing fleet keeps it.

A NOT NULL ``ADD COLUMN`` with a constant default is metadata-only on
PostgreSQL 11+ (no table rewrite), following the lock-safe recipe: idempotency
probe, ``terminate_lock_holders``, ``set_lock_timeout``, then the guarded DDL.
Fresh installs run ``create_all`` + ``stamp head``: the body never executes there.

Revision ID: f9769fltauton1
Revises: c9566fleetinv1
Create Date: 2026-11-07 16:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "f9769fltauton1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c9566fleetinv1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "fleets"
_COLUMN = "autonomy_level"


def _has_column(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": _TABLE, "column": _COLUMN},
        ).first()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE) or _has_column(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(
        f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} VARCHAR(20) NOT NULL DEFAULT 'full_gates'"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE) or not _has_column(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
