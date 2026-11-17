"""add runner_commands.flow_run_id for the flow `Runner command` node

A flow's `Runner command` node dispatches through the same transport as agent
tools; this column records WHICH flow run asked (audit parity with
``assignment_run_id``) and is the join key its wait-mode park re-checks.

Plain UUID, no FK — flow runs are prunable history and the immutable command
audit trail must survive their deletion, exactly like the other context ids
on this table.

Lock safety: canonical recipe — idempotency probe + early return, then
``terminate_lock_holders`` + ``set_lock_timeout``, then a metadata-only
``ADD COLUMN IF NOT EXISTS`` (nullable, no default → no rewrite). No index:
the only per-row lookup is by primary key at resume time.

Revision ID: f7h9runflw1
Revises: e6g8flwtrc1
Create Date: 2026-08-16 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "f7h9runflw1"
down_revision: str | None = "e6g8flwtrc1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "runner_commands"
_COLUMN = "flow_run_id"


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
    if not _table_exists(conn, _TABLE):
        # Fresh install: create_all builds the full table (column included)
        # and stamps head — this body never needs to run.
        return

    if not _column_exists(conn, _TABLE, _COLUMN):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(
            f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} UUID"
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _table_exists(conn, _TABLE) and _column_exists(conn, _TABLE, _COLUMN):
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN {_COLUMN}")
