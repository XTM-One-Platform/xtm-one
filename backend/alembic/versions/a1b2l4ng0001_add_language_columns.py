"""add platform_language and user language columns (i18n)

Adds:
  - ``platform_settings.platform_language`` (default "auto") — platform-wide
    UI language, admin-editable in Settings > Parameters.
  - ``users.language`` (default "auto") — per-user UI language override,
    editable in the Profile page.

Both are constant-default ``ADD COLUMN`` (metadata-only on PG 11+) and follow
the canonical lock-safe recipe: idempotency probe -> terminate_lock_holders
-> set_lock_timeout -> guarded DDL.

Revision ID: a1b2l4ng0001
Revises: d046bcdf345f
Create Date: 2026-07-17 10:05:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "a1b2l4ng0001"
down_revision: str | None = "d046bcdf345f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS: tuple[tuple[str, str], ...] = (
    ("platform_settings", "platform_language"),
    ("users", "language"),
)


def _column_exists(conn, table: str, column: str) -> bool:
    # Scope the probe to the active schema — information_schema.columns
    # spans ALL schemas, so a same-named table elsewhere must not make the
    # probe skip the ADD COLUMN in the schema being migrated.
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
    for table, column in _COLUMNS:
        # 1. Idempotency probe — steady-state re-runs take NO lock.
        if _column_exists(conn, table, column):
            continue
        # 2. Pre-flight: kill same-role lock holders so the ALTER is instant.
        terminate_lock_holders(conn, table)
        # 3. Safety belt: fail fast (55P03) instead of wedging the lock queue.
        set_lock_timeout(conn)
        # 4. Guarded, constant-default (metadata-only) DDL.
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} "
            "VARCHAR(10) NOT NULL DEFAULT 'auto'"
        )


def downgrade() -> None:
    conn = op.get_bind()
    for table, column in _COLUMNS:
        if not _column_exists(conn, table, column):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
