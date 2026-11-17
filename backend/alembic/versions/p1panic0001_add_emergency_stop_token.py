"""add the emergency stop API token columns to platform_settings

The emergency stop ("panic button") can be pressed without a session through
``POST /api/v1/panic/emergency`` with a platform-wide token an administrator
generates in Settings > Security. Stored like the user API keys: a bcrypt hash
and a display prefix, plus who minted it, when, and when it was last used.

All columns are nullable with no default, so each ``ADD COLUMN`` is
metadata-only. Follows the canonical lock-safe recipe: idempotency probe ->
terminate_lock_holders -> set_lock_timeout -> guarded DDL. The foreign key
locks ``users`` as well as ``platform_settings``, so the pre-flight clears the
lock holders on both before adding it.

Revision ID: p1panic0001
Revises: o1rtlink0001
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "p1panic0001"
down_revision: str | None = "o1rtlink0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_settings"
_FK = "fk_platform_settings_emergency_stop_token_created_by_id_users"
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("emergency_stop_token_hash", "VARCHAR(200)"),
    ("emergency_stop_token_prefix", "VARCHAR(20)"),
    ("emergency_stop_token_created_at", "TIMESTAMP WITH TIME ZONE"),
    ("emergency_stop_token_created_by", "VARCHAR(320)"),
    ("emergency_stop_token_created_by_id", "UUID"),
    ("emergency_stop_token_last_used_at", "TIMESTAMP WITH TIME ZONE"),
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


def _constraint_exists(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.table_constraints "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND constraint_name = :name"
            ),
            {"table": _TABLE, "name": name},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    missing = [c for c, _ in _COLUMNS if not _column_exists(conn, _TABLE, c)]
    need_fk = not _constraint_exists(conn, _FK)
    if not missing and not need_fk:
        return
    terminate_lock_holders(conn, _TABLE)
    if need_fk:
        terminate_lock_holders(conn, "users")
    set_lock_timeout(conn)
    for column, ddl_type in _COLUMNS:
        if column in missing:
            op.execute(
                f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {column} {ddl_type}"
            )
    if need_fk:
        op.execute(
            f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_FK} "
            "FOREIGN KEY (emergency_stop_token_created_by_id) "
            "REFERENCES users(id) ON DELETE SET NULL"
        )


def downgrade() -> None:
    conn = op.get_bind()
    present = [c for c, _ in _COLUMNS if _column_exists(conn, _TABLE, c)]
    if not present:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_FK}")
    for column in present:
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {column}")
