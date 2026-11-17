"""add ctem_posture_snapshots.objects

The command center briefing's risk movers (#11547, dev-docs/ctem-stage-viz.md
row B3) need a per-object value on each daily posture snapshot; the snapshot
stored stage metrics only. ``objects`` keeps a bounded
``{"<kind>:<id>": {"label", "value"}}`` map per (user, vertical, day), written
by the live posture build from that user's own reads.

Nullable JSON with no default, so this is a metadata-only ``ADD COLUMN`` (no
table rewrite), following the canonical lock-safe recipe: idempotency probe ->
terminate_lock_holders -> set_lock_timeout -> guarded DDL. NULL is read as "no
object recorded that day", which is why no backfill is needed: the movers
answer "insufficient history" until two days hold objects.

Revision ID: ctemmovers11547
Revises: ctsrc0001
Create Date: 2026-11-13 15:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "ctemmovers11547"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "ctsrc0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "ctem_posture_snapshots"
_COLUMN = "objects"
_TYPE = "JSON"


def _has_column(conn: sa.engine.Connection) -> bool:
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
