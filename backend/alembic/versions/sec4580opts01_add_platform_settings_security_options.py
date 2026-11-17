"""add platform_settings.security_options (Settings > Security)

The security options an administrator stores in Settings > Security, as one
JSON object keyed by option (``app/core/security_options.py``). NULL means
nothing is stored, so every option keeps its default or its environment
variable: no backfill, and an upgrade changes nothing.

One nullable column with no default is catalogue-only (no rewrite), and it
follows the canonical lock-safe recipe all the same, since ``platform_settings``
is read inside long-lived transactions: idempotency probe ->
terminate_lock_holders -> set_lock_timeout -> guarded DDL.

Revision ID: sec4580opts01
Revises: o3950tokown01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "sec4580opts01"
down_revision: str | None = "o3950tokown01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_settings"
_COLUMN = "security_options"


def _column_exists(conn, table: str, column: str) -> bool:
    # Scoped to the active schema: information_schema.columns spans every
    # schema, and a same-named table elsewhere must not skip the ADD COLUMN.
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    # 1. Idempotency probe - steady-state re-runs take NO lock.
    if _column_exists(conn, _TABLE, _COLUMN):
        return
    # 2. Pre-flight: kill same-role lock holders so the ALTER is instant.
    terminate_lock_holders(conn, _TABLE)
    # 3. Safety belt: fail fast (55P03) instead of wedging the lock queue.
    set_lock_timeout(conn)
    # 4. Guarded, nullable, no default: metadata-only.
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} JSON")


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
