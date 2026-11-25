"""add platform_builds.released_at: the release event waits for the rollout to complete (#2214)

``platform_builds.released_at`` is when ``platform.release.deployed`` was raised
for the build. The event is raised once the build's rollout is complete (it is
still the newest build and no other build was seen within the drain grace), not
at its first boot, which a newer build may supersede within minutes. Every row
already recorded is stamped with its first boot, so no past build raises it again.

A nullable column with no default: metadata-only on PG 11+, no rewrite. It
follows the lock-safe recipe (idempotency probe, ``terminate_lock_holders``,
``set_lock_timeout``, guarded DDL) both ways.

Revision ID: r2214relsettle1
Revises: c2070coordret01
Create Date: 2026-11-25 14:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "r2214relsettle1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c2070coordret01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_builds"
_COLUMN = "released_at"


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
    if not _column_exists(conn):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} TIMESTAMP WITH TIME ZONE")
    # The builds recorded before this column raised their event at first boot.
    op.execute("UPDATE platform_builds SET released_at = first_seen_at WHERE released_at IS NULL")


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
