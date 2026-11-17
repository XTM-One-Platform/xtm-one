"""add organizations.daily_run_cap (#9157, slice M6)

A tracked organization's OpenCTI events start at most ``daily_run_cap`` runs
per day (UTC, default 5); the extra events are held and run the next day as
one digest (``dev-docs/ctem-fleets.md`` section 9.7). Every existing row gets
the default; nothing ran under a cap before, and no event of an organization
whose monitoring is off reaches a run, so an upgrade changes no behaviour.

Lock safety: canonical recipe - idempotency probe + early return, then
``terminate_lock_holders`` + ``set_lock_timeout``, then a metadata-only
``ADD COLUMN IF NOT EXISTS`` with a constant default (no table rewrite) and
its CHECK on a table of at most a few hundred rows.

Revision ID: m9157runcap01
Revises: s9174skillfork1
Create Date: 2026-11-06 20:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "m9157runcap01"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "s9174skillfork1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "organizations"
_COLUMN = "daily_run_cap"
_CHECK = "ck_organizations_daily_run_cap_valid"


def _table_exists(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :table"
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
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} INTEGER NOT NULL DEFAULT 5")
    op.execute(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CHECK} CHECK ({_COLUMN} >= 1 AND {_COLUMN} <= 100)")


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn) or not _column_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CHECK}")
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
