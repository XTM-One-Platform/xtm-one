"""add fleets.approval_gates

The fleet's approval gate catalog as configuration (#10000): the gated actions
the autonomy level (#9769) acts on, stored on the fleet instead of being read
from its AI-written documentation, so a documentation regenerate can never
remove a gate the platform enforces. NULL on every existing fleet: the gates
of the documentation keep being read until the seed (built-in fleets) or a
person writes the catalog, so nothing changes at the migration.

A nullable ``ADD COLUMN`` with no default is metadata-only on PostgreSQL;
lock-safe per the canonical recipe: idempotency probe, terminate_lock_holders,
set_lock_timeout, then the guarded DDL. Fresh installs run ``create_all`` +
``stamp head``: the body never executes there.

Revision ID: k10000fltgate1
Revises: h9836roiorg1
Create Date: 2026-11-08 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "k10000fltgate1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "h9836roiorg1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "fleets"
_COLUMN = "approval_gates"


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
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} JSON")


def downgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE) or not _has_column(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
