"""add platform_builds.builtin_tools: the built-in tools each build ships (#12192)

``platform_builds.builtin_tools`` holds ``{group id: [function, ...]}`` for the
build, written once by its first boot. The next build diffs against it to tell
the owners of agents whose ``builtin_allowed_tools`` scopes a group which new
functions the allowlist leaves out, once per release.

A nullable column with no default: metadata-only on PG 11+, no rewrite. It
follows the lock-safe recipe (idempotency probe, ``terminate_lock_holders``,
``set_lock_timeout``, guarded DDL) both ways.

Revision ID: b12192bltsnap1
Revises: w12168implws1
Create Date: 2026-11-16 05:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "b12192bltsnap1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "w12168implws1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_builds"
_COLUMN = "builtin_tools"


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
    if _column_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} JSON")


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
