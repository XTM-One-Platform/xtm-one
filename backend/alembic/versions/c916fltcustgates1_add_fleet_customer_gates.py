"""add fleets.customer_gates

The gates a person added to a built-in fleet (#916, CTEM-34): the customer
layer of its approval gate catalog. The seed writes ``approval_gates`` on every
boot and never writes this column, so an added gate survives every upgrade.
NULL on every existing fleet: nothing changes at the migration.

A nullable ``ADD COLUMN`` with no default is metadata-only on PostgreSQL;
lock-safe per the canonical recipe: idempotency probe, terminate_lock_holders,
set_lock_timeout, then the guarded DDL. Fresh installs run ``create_all`` +
``stamp head``: the body never executes there.

Revision ID: c916fltcustgates1
Revises: c936ctemexploit1
Create Date: 2026-11-21 20:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c916fltcustgates1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c936ctemexploit1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "fleets"
_COLUMN = "customer_gates"


def _has_column(conn: sa.engine.Connection) -> bool:
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
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} JSON")


def downgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE) or not _has_column(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
