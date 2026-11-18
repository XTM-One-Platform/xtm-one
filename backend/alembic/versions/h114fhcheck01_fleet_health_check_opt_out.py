"""add fleets.health_check (a fleet's opt-out of the seeded weekly health check)

The platform gives every fleet orchestrator a "Weekly fleet health check" and
the boot backfill re-created it whenever it was missing, so a fleet that runs
its own health sweep could not delete it. The fleet now records its choice:
NULL = the seeded check (default), ``{mode: "off", by, at}`` = opted out,
``{mode: "own", assignment_id, by, at}`` = one of its own assignments is its
health check (``app.services.fleets.fleet_health_check_mode``).

Nullable JSON, no default, no backfill: metadata-only. Lock-safe per the
canonical recipe. Fresh installs run ``create_all`` + ``stamp head``: the body
never executes there.

Revision ID: h114fhcheck01
Revises: a12346a2acred1
Create Date: 2026-11-18 08:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "h114fhcheck01"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "a12346a2acred1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "fleets"
_COLUMN = "health_check"


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
