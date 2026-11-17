"""add users.ctem_dashboard_fleet_ids

The fleets the CTEM command center's fleets row shows for one person (#11464):
the built-in CTEM fleets by default, or the ones the person chose (built-in or
their own), in order. A pure UI preference: no behaviour depends on it
server-side beyond the visibility filter applied on write.

Nullable JSON with no default, so this is a metadata-only ``ADD COLUMN`` (no
table rewrite), following the canonical lock-safe recipe: idempotency probe ->
terminate_lock_holders -> set_lock_timeout -> guarded DDL. NULL is read as
"the built-in CTEM fleets", which is why no backfill is needed.

Revision ID: c11464dashflt1
Revises: fljsonfld0001
Create Date: 2026-11-13 00:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "c11464dashflt1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "fljsonfld0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "users"
_COLUMN = "ctem_dashboard_fleet_ids"
_TYPE = "JSON"


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
    if not inspector.has_table(_TABLE):
        return
    if _has_column(conn):
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} {_TYPE}")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not inspector.has_table(_TABLE):
        return
    if not _has_column(conn):
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
