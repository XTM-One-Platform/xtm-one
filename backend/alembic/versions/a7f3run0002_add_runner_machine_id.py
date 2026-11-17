"""add runners.machine_id for re-enrollment convergence

A runner re-enrolling (reinstall, credential loss, install-script re-run on a
machine that already has the app) used to create a brand-new ``runners`` row
every time, duplicating the machine in the fleet list. The runner now reports
a stable, salted-hash machine identifier at enrollment; ``POST /enroll``
converges on the existing active row for the same machine (same identity
scope + platform) instead of inserting a duplicate.

``machine_id`` is intentionally the ONLY dedup key — hostname is not a machine
identity (cloned VM fleets share one), and a false merge would invalidate a
different machine's credential, which is far worse than a cosmetic duplicate.

Lock safety: canonical recipe — idempotency probe + early return, then
``terminate_lock_holders`` + ``set_lock_timeout``, then a metadata-only
``ADD COLUMN IF NOT EXISTS`` (nullable, no default → no rewrite). The index is
built ``CONCURRENTLY`` outside the lock_timeout belt.

Revision ID: a7f3run0002
Revises: a7f3run0001
Create Date: 2026-08-16 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "a7f3run0002"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "a7f3run0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "runners"
_COLUMN = "machine_id"
_INDEX = "ix_runners_machine_id"


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
            f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} VARCHAR(128)"
        )

    # Index guarded independently so a crashed retry between the column and
    # the index still converges.
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = 0")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} ON {_TABLE} ({_COLUMN})"
        )


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, _TABLE):
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
    if _column_exists(conn, _TABLE, _COLUMN):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
