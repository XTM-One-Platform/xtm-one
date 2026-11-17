"""add the ROI ledger attribution columns (who a value fact counts for)

Slice L1 of issue #12077 (design ``dev-docs/roi-outcomes.md`` sections 12.3
and 12.11):

* ``roi_run_records.user_id`` / ``attribution``: the person a run or work
  item record counts for and the rule that named them
  (``services/roi_ledger.py``), with an index on ``(user_id, created_at)``
  for a person's "Mine";
* ``roi_chat_records.fleet_id`` / ``fleet_backfilled``: the agent's home
  fleet when the deliverable was first written.

Nullable columns, no default, no backfill in the migration: metadata-only.
The rows written before are stamped in bounded batches by
``roi_ledger.run_ledger_backfill`` from the trace maintenance. Lock-safe per
the canonical recipe; the index is built in place in an autocommit block.
Fresh installs run ``create_all`` + ``stamp head``: the body never executes
there.

Revision ID: l12077ledger1
Revises: c12063cpfleet1
Create Date: 2026-11-15 15:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import create_index_in_place, set_lock_timeout, terminate_lock_holders

revision: str = "l12077ledger1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c12063cpfleet1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("roi_run_records", "user_id", "UUID"),
    ("roi_run_records", "attribution", "VARCHAR(20)"),
    ("roi_chat_records", "fleet_id", "UUID"),
    ("roi_chat_records", "fleet_backfilled", "BOOLEAN"),
)
_INDEX = "ix_roi_run_records_user_created"


def _has_column(conn: sa.engine.Connection, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).first()
    )


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    for table, column, coltype in _COLUMNS:
        if not inspector.has_table(table) or _has_column(conn, table, column):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coltype}")
    if inspector.has_table("roi_run_records"):
        with op.get_context().autocommit_block():
            create_index_in_place(conn, _INDEX, "roi_run_records", "(user_id, created_at)")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if inspector.has_table("roi_run_records"):
        terminate_lock_holders(conn, "roi_run_records")
        set_lock_timeout(conn)
        op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    for table, column, _coltype in reversed(_COLUMNS):
        if not inspector.has_table(table):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
