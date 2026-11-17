"""add focus mode policies to users and groups

An administrator can decide a person's focus mode, or a group's:
``focus_mode_policy`` on ``users`` (NULL = the groups decide, ``choice``,
``focus_default``, ``focus_only``) and on ``groups`` (NULL, ``focus_default``,
``focus_only``). ``users.standalone_mode_chosen`` records that the person
switched focus mode themselves, so a ``focus_default`` policy keeps their
choice once they made one.

Nothing is backfilled: NULL policies decide nothing, and a person who never
switched focus mode has not chosen.

Lock safety: canonical recipe per column - idempotency probe + early return,
then ``terminate_lock_holders`` + ``set_lock_timeout``, then a metadata-only
``ADD COLUMN IF NOT EXISTS`` (a nullable column with no default, or a
constant default: no table rewrite on PostgreSQL 11+). No index: the columns
are only read with their row. Old replicas during a rolling deploy never
select a column they do not know about.

Revision ID: w5foc3314005
Revises: i9pkbuild001
Create Date: 2026-09-30 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "w5foc3314005"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "i9pkbuild001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("users", "focus_mode_policy", "VARCHAR(20)"),
    ("users", "standalone_mode_chosen", "BOOLEAN NOT NULL DEFAULT false"),
    ("groups", "focus_mode_policy", "VARCHAR(20)"),
)


def _table_exists(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": table},
        ).scalar()
    )


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    for table, column, ddl in _COLUMNS:
        # Fresh install: create_all builds the full table (column included)
        # and stamps head - this body never needs to run.
        if not _table_exists(conn, table):
            continue
        # Steady-state re-run (a replica retried) takes no lock at all.
        if _column_exists(conn, table, column):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}")


def downgrade() -> None:
    conn = op.get_bind()
    for table, column, _ddl in _COLUMNS:
        if not _table_exists(conn, table) or not _column_exists(conn, table, column):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
