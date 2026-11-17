"""add fleets.health_dismissals (the health findings a fleet manager dismissed)

A fleet health finding (``get_fleet_health``, #11700) the fleet's owner has
knowingly accepted was raised again on every check, so the fleet never read
healthy (#12014). A fleet manager now dismisses it with a reason; the record
is stored on the fleet: ``[{id, kind, scope, summary, reason, by, by_name, at,
until}]`` (``app.services.fleet_health_dismissals``).

Nullable JSON, no default, no backfill: metadata-only. Lock-safe per the
canonical recipe. Fresh installs run ``create_all`` + ``stamp head``: the body
never executes there.

Revision ID: h12014fltdism1
Revises: l11958live01
Create Date: 2026-11-15 10:50:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "h12014fltdism1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "l11958live01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "fleets"
_COLUMN = "health_dismissals"


def _has_column(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :table AND column_name = :column"
            ),
            {"table": _TABLE, "column": _COLUMN},
        ).first()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE) or _has_column(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} JSON")


def downgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
