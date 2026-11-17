"""add fleets.prerequisite_confirmations (a person's confirmation of a manual prerequisite)

A documented ``manual`` prerequisite of a fleet's About page (what no system
can read: a contract signed, a person briefed) stayed "To confirm" for ever:
nothing could move it (#10469, slice S2). A fleet manager now confirms it; the
confirmation is stored on the fleet, never in the AI documentation, so a
regenerate that keeps the line's label keeps it:
``{manual key: {by, name, at, label}}`` (``fleet_about.manual_key``).

Nullable JSON, no default, no backfill: metadata-only. Lock-safe per the
canonical recipe. Fresh installs run ``create_all`` + ``stamp head``: the body
never executes there.

Revision ID: t10469fltconf1
Revises: s9173orgsetup1
Create Date: 2026-11-09 19:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "t10469fltconf1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "s9173orgsetup1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "fleets"
_COLUMN = "prerequisite_confirmations"


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
