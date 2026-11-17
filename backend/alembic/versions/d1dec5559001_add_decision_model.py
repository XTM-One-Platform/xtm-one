"""add the decision model columns to platform_settings

The model ``act_on_page`` asks for each step on a host page, and the provider
config that owns it. Both nullable with no default, so each
``ADD COLUMN`` is metadata-only. Follows the canonical lock-safe recipe:
idempotency probe -> terminate_lock_holders -> set_lock_timeout -> guarded DDL.

Revision ID: d1dec5559001
Revises: v5826vstudio01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "d1dec5559001"
down_revision: str | None = "v5826vstudio01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_settings"
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("decision_model", "VARCHAR(100)"),
    ("decision_provider_id", "UUID"),
)


def _column_exists(conn, table: str, column: str) -> bool:
    # Scoped to the active schema: information_schema.columns spans ALL
    # schemas, and a same-named table elsewhere must not skip the DDL here.
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
    missing = [c for c, _ in _COLUMNS if not _column_exists(conn, _TABLE, c)]
    if not missing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for column, ddl_type in _COLUMNS:
        if column in missing:
            op.execute(
                f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {column} {ddl_type}"
            )


def downgrade() -> None:
    conn = op.get_bind()
    present = [c for c, _ in _COLUMNS if _column_exists(conn, _TABLE, c)]
    if not present:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for column in present:
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {column}")
