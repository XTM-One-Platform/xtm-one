"""add coordination_access to agents

An agent may narrow which agents read and write the company-managed work items
it creates and the company-managed shared state it owns
(``services/company_managed_writes.py``). The policy lives on the governing
agent and is read live on every access, so no row of ``work_items`` or
``shared_states`` changes.

NULL (the default, and what every existing agent reads as) is the always-on
base rule: everyone reads those rows, every company-managed agent writes them.
Nothing is backfilled: narrowing rows nobody chose to narrow would take a
fleet's coordination away from agents that work today.

Lock safety: canonical recipe - idempotency probe + early return, then
``terminate_lock_holders`` + ``set_lock_timeout``, then a metadata-only
``ADD COLUMN IF NOT EXISTS`` of a nullable JSON column with no default (no
table rewrite). No index: the column is read with the governing agent's row,
or scanned by the listings' ``IS NOT NULL`` subquery over the agents table.
Old replicas during a rolling deploy never select a column they do not know
about.

Revision ID: c3870coord01
Revises: e4449scope01
Create Date: 2026-09-28 19:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "c3870coord01"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "e4449scope01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agents"
_COLUMN = "coordination_access"


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
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} JSON")


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn) or not _column_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
