"""add fleets.is_builtin

Built-in fleets (#8988, CTEM seeded fleets S1): a fleet the platform seeds from
``BUILTIN_FLEETS`` carries ``fleets.is_builtin`` (``NOT NULL DEFAULT false``).
Every existing fleet is a user fleet, so an upgrade changes nothing until a
release ships a built-in fleet spec.

Lock safety: canonical recipe - idempotency probe + early return, then
``terminate_lock_holders`` + ``set_lock_timeout``, then a metadata-only
``ADD COLUMN IF NOT EXISTS`` (a constant default is no table rewrite on
PostgreSQL 11+). No index: the seed reads the few built-in rows by slug.

Revision ID: f1ctem8988s1
Revises: r4oirec8990
Create Date: 2026-11-05 17:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "f1ctem8988s1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "r4oirec8990"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "fleets"
_COLUMN = "is_builtin"


def _table_exists(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": _TABLE},
        ).scalar()
    )


def _column_exists(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": _TABLE, "column": _COLUMN},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    # Fresh install: create_all builds the full table (column included) and
    # stamps head - this body never needs to run.
    if not _table_exists(conn):
        return
    # Steady-state re-run (a replica retried) takes no lock at all.
    if _column_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} BOOLEAN NOT NULL DEFAULT false")


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn) or not _column_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
