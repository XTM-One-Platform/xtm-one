"""add platform_settings.xtm_hub_key_version (XTM Hub signing key version)

The version of the Ed25519 key XTM One derives from SECRET_KEY to sign its
XTM Hub calls (``app/core/xtm_hub_key.py``). Every instance starts at
1; a new version gives a new key, which the Hub learns only through a new
registration.

Constant-default ``ADD COLUMN`` (metadata-only on PG 11+), following the
canonical lock-safe recipe: idempotency probe -> terminate_lock_holders
-> set_lock_timeout -> guarded DDL.

Revision ID: x1784hubkey01
Revises: c1951caseopen01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "x1784hubkey01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c1951caseopen01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_settings"
_COLUMN = "xtm_hub_key_version"


def _column_exists(conn, table: str, column: str) -> bool:
    # Scoped to the active schema: information_schema.columns spans every schema.
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
    # 1. Idempotency probe — steady-state re-runs take NO lock.
    if _column_exists(conn, _TABLE, _COLUMN):
        return
    # 2. Pre-flight: kill same-role lock holders so the ALTER is instant.
    terminate_lock_holders(conn, _TABLE)
    # 3. Safety belt: fail fast (55P03) instead of wedging the lock queue.
    set_lock_timeout(conn)
    # 4. Guarded, constant-default (metadata-only) DDL.
    op.execute(
        f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} INTEGER NOT NULL DEFAULT 1"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
