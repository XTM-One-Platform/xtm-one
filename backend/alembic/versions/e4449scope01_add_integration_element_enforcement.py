"""add integration_element_enforcement to agents and assignments

A Scope(s) selection (``integration_element_bindings``) became a hard boundary
on the GitHub, Slack, Microsoft Teams and HubSpot tools, for every
agent and assignment at once. Whether a selection is a boundary or prompt
guidance is now chosen per selection: ``integration_element_enforcement`` lists
the element types whose selection is enforced, next to the selection itself,
on both tables.

NULL (the default, and what every existing row reads as) means every selection
is guidance, as it was before. Nothing is backfilled: enforcing a
selection nobody chose to enforce would withdraw tools from, or refuse calls
of, agents that work today.

Lock safety: canonical recipe per table - idempotency probe + early return,
then ``terminate_lock_holders`` + ``set_lock_timeout``, then a metadata-only
``ADD COLUMN IF NOT EXISTS`` of a nullable JSON column with no default (no
table rewrite). No index: the column is only read with the whole row. Old
replicas during a rolling deploy never select a column they do not know about.

Revision ID: e4449scope01
Revises: sec4580opts01
Create Date: 2026-09-27 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "e4449scope01"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "sec4580opts01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("agents", "assignments")
_COLUMN = "integration_element_enforcement"


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
    for table in _TABLES:
        # Fresh install: create_all builds the full table (column included)
        # and stamps head - this body never needs to run.
        if not _table_exists(conn, table):
            continue
        # Steady-state re-run (a replica retried) takes no lock at all.
        if _column_exists(conn, table, _COLUMN):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {_COLUMN} JSON")


def downgrade() -> None:
    conn = op.get_bind()
    for table in _TABLES:
        if not _table_exists(conn, table) or not _column_exists(conn, table, _COLUMN):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {_COLUMN}")
