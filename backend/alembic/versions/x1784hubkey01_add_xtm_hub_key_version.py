"""add platform_settings.xtm_hub_key_version (#1784)

The version of the XTM Hub signing key (``app/core/xtm_hub_key.py``): starts
at 1, never below. platform_settings holds one row, so the CHECK is instant.

Revision ID: x1784hubkey01
Revises: s2443ctemgrp01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "x1784hubkey01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "s2443ctemgrp01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_settings"
_COLUMN = "xtm_hub_key_version"
_CHECK = "ck_platform_settings_xtm_hub_key_version_positive"


def _column_exists(conn, table: str, column: str) -> bool:
    # Scoped to the active schema: information_schema.columns spans every schema.
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


def _check_exists(conn, table: str, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT 1 FROM pg_constraint WHERE conname = :name AND conrelid = to_regclass(:table)"),
            {"name": name, "table": table},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    # 1. Idempotency probe — steady-state re-runs take NO lock. The column and
    #    the CHECK are probed apart: an earlier draft of this revision added the
    #    column alone.
    has_column = _column_exists(conn, _TABLE, _COLUMN)
    has_check = _check_exists(conn, _TABLE, _CHECK)
    if has_column and has_check:
        return
    # 2. Pre-flight: kill same-role lock holders so the ALTER is instant.
    terminate_lock_holders(conn, _TABLE)
    # 3. Safety belt: fail fast (55P03) instead of wedging the lock queue.
    set_lock_timeout(conn)
    # 4. Guarded, constant-default (metadata-only) DDL; the CHECK scans one row.
    clauses = []
    if not has_column:
        clauses.append(f"ADD COLUMN IF NOT EXISTS {_COLUMN} INTEGER NOT NULL DEFAULT 1")
    if not has_check:
        clauses.append(f"ADD CONSTRAINT {_CHECK} CHECK ({_COLUMN} >= 1)")
    op.execute(f"ALTER TABLE {_TABLE} " + ", ".join(clauses))


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    # Dropping the column drops its CHECK too.
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
