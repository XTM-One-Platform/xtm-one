"""add roi_run_records.backfilled_at

When a record was written by a retroactive pass after a declaration changed
(#10006, slice S1, ``services/roi_backfill.py``); ``NULL`` for a record
written at the run's or the work item's completion.

Nullable ``ADD COLUMN`` with no default, metadata-only. Idempotent via a
column-existence probe and lock-safe per the canonical recipe: idempotency
probe -> terminate_lock_holders -> set_lock_timeout -> guarded DDL.

Revision ID: r10006roibf1
Revises: k10000fltgate1
Create Date: 2026-11-08 10:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "r10006roibf1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "k10000fltgate1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "roi_run_records"
_COLUMN = "backfilled_at"
_DEFINITION = "TIMESTAMP WITH TIME ZONE"


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
    inspector = sa.inspect(conn)
    # Fresh installs run create_all + stamp head; this body never executes.
    if not inspector.has_table(_TABLE) or _has_column(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} {_DEFINITION}")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE) or not _has_column(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
