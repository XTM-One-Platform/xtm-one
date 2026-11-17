"""add fleet_members.role_label

A home member's role in its fleet ("Triage", "Release manager"), shown beside
its name on the fleet page and set from the members panel. A label only: it
never changes the agent, its prompt or its handovers.

A constant-default ``ADD COLUMN`` (metadata-only on PostgreSQL 11+), following
the lock-safe recipe: idempotency probe, ``terminate_lock_holders``,
``set_lock_timeout``, then the guarded DDL. Fresh installs run ``create_all``
+ ``stamp head``: the body never executes there.

Revision ID: f7065rolelbl
Revises: a7104sent01
Create Date: 2026-11-01 13:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "f7065rolelbl"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "a7104sent01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "fleet_members"
_COLUMN = "role_label"


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
    if _column_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} VARCHAR(100) NOT NULL DEFAULT ''")


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
