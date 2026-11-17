"""add activation_check and events_filtered_count to assignments

``activation_check`` holds an assignment's tool-capable gate: one read-only
tool call and a condition on its result, evaluated by the platform with no
model call before a run is charged or set up. ``events_filtered_count`` counts
the integration events the trigger's filter dropped before any run was created.

NULL (every existing row) means no tool check and no event filtered: nothing is
backfilled, so every assignment runs exactly as before.

Lock safety: canonical recipe - idempotency probe + early return, then
``terminate_lock_holders`` + ``set_lock_timeout``, then metadata-only
``ADD COLUMN IF NOT EXISTS`` of nullable columns with no default (no table
rewrite). No index: both columns are only read with the whole row. Old
replicas during a rolling deploy never select a column they do not know about.

Revision ID: g7001gate01
Revises: e6717sig0001
Create Date: 2026-10-29 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "g7001gate01"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "e6717sig0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "assignments"
_COLUMNS = (("activation_check", "JSON"), ("events_filtered_count", "INTEGER"))


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


def _missing_columns(conn) -> list[tuple[str, str]]:
    present = {
        row[0]
        for row in conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": _TABLE},
        ).all()
    }
    return [(name, sql_type) for name, sql_type in _COLUMNS if name not in present]


def upgrade() -> None:
    conn = op.get_bind()
    # Fresh install: create_all builds the full table (columns included) and
    # stamps head - this body never needs to run.
    if not _table_exists(conn):
        return
    missing = _missing_columns(conn)
    # Steady-state re-run (a replica retried) takes no lock at all.
    if not missing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    clauses = ", ".join(f"ADD COLUMN IF NOT EXISTS {name} {sql_type}" for name, sql_type in missing)
    op.execute(f"ALTER TABLE {_TABLE} {clauses}")


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn):
        return
    present = [name for name, _type in _COLUMNS if (name, _type) not in _missing_columns(conn)]
    if not present:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    clauses = ", ".join(f"DROP COLUMN IF EXISTS {name}" for name in present)
    op.execute(f"ALTER TABLE {_TABLE} {clauses}")
