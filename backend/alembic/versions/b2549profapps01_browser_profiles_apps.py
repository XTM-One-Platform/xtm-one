"""browser profiles declare the other applications their session signs in

Adds ``browser_profiles.apps`` (nullable JSON, no default: catalogue-only),
the ``[{name, url}]`` list of the applications a profile's saved session also
signs in (an application signing in through the profile's identity provider).
Nothing is backfilled: a profile without apps reads as before.

Canonical lock-safe recipe (rule 5): idempotency probe, then
``terminate_lock_holders`` and ``set_lock_timeout`` before the DDL. The
downgrade drops the column.

Revision ID: b2549profapps01
Revises: t2553keystat01
Create Date: 2026-11-27 04:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "b2549profapps01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "t2553keystat01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "browser_profiles"
_COLUMN = "apps"


def _column_exists(conn: sa.engine.Connection) -> bool:
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
    if not sa.inspect(conn).has_table(_TABLE) or _column_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} JSON")


def downgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE) or not _column_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
