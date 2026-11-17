"""add users.chat_sidebar_expanded_workspaces

The workspace groups of the chat sidebar a person opened (#9179): the group
keys shown expanded, every other group collapsed. Kept in the browser's
localStorage, the choice would not follow the person to another device, so it
lives on the user, next to ``oauth_share_dismissed_integration_ids``. A pure UI
preference - no behaviour depends on it server-side.

Nullable JSON with no default, so this is a metadata-only ``ADD COLUMN`` (no
table rewrite), following the canonical lock-safe recipe: idempotency probe ->
terminate_lock_holders -> set_lock_timeout -> guarded DDL. NULL is read as
"never chosen" (every group collapsed), which is why no backfill is needed.

Revision ID: c9179sidebarws1
Revises: f9174fleetfork1
Create Date: 2026-11-06 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "c9179sidebarws1"
down_revision: str | None = "f9174fleetfork1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "users"
_COLUMN = "chat_sidebar_expanded_workspaces"
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
