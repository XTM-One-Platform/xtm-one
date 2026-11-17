"""add platform_settings.default_presentation_template_id

The template container ``generate_presentation`` styles a deck with when the
agent names none. Nullable, no default — NULL means "the built-in brand deck"
(resolved at run time by ``presentations.resolve_default_template_id``), so
the seed never has to write the row and an admin who clears the setting gets
the shipped brand back. ``ON DELETE SET NULL`` so deleting the chosen container
falls back to the built-in rather than leaving a dangling id.

Lock profile: the nullable ``ADD COLUMN`` is metadata-only on PG 11+, but the
ALTER takes ACCESS EXCLUSIVE on ``platform_settings`` — the very table that
took production down in the 2026-07-05 rolling-deploy lock queue — and the
foreign key additionally takes SHARE ROW EXCLUSIVE on ``template_containers``
(a small table, but every open transaction that has *read* it blocks the
constraint). Both tables therefore get the canonical pre-flight: idempotency
probe -> terminate_lock_holders (both) -> set_lock_timeout -> guarded DDL. The
FK is added ``NOT VALID`` then validated separately: validation only takes
SHARE UPDATE EXCLUSIVE and runs on a column that is NULL on every existing
row, so the exclusive lock is held for the metadata change alone.

Revision ID: f1p3358dflt
Revises: e9m0dl3349
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "f1p3358dflt"
down_revision: str | None = "e9m0dl3349"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_settings"
_COLUMN = "default_presentation_template_id"
_REF_TABLE = "template_containers"
_FK_NAME = "platform_settings_default_presentation_template_id_fkey"


def _column_exists(conn, table: str, column: str) -> bool:
    # Scoped to the active schema — information_schema.columns spans ALL
    # schemas, so a same-named table elsewhere must not make the probe skip
    # the ADD COLUMN in the schema being migrated.
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


def _constraint_exists(conn, table: str, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.table_constraints "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND constraint_name = :name"
            ),
            {"table": table, "name": name},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    column_present = _column_exists(conn, _TABLE, _COLUMN)
    constraint_present = _constraint_exists(conn, _TABLE, _FK_NAME)
    # 1. Idempotency probe — steady-state re-runs take NO lock.
    if column_present and constraint_present:
        return
    # 2. Pre-flight: kill same-role lock holders on BOTH tables the DDL locks.
    terminate_lock_holders(conn, _TABLE)
    terminate_lock_holders(conn, _REF_TABLE)
    # 3. Safety belt: fail fast (55P03) instead of wedging the lock queue.
    set_lock_timeout(conn)
    # 4. Guarded DDL.
    if not column_present:
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} UUID")
    if not constraint_present:
        op.execute(
            f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_FK_NAME} "
            f"FOREIGN KEY ({_COLUMN}) REFERENCES {_REF_TABLE} (id) "
            "ON DELETE SET NULL NOT VALID"
        )
        # Every existing row is NULL in the new column, so validation is a
        # no-op scan under a weaker lock than the ADD CONSTRAINT itself.
        op.execute(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {_FK_NAME}")


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    terminate_lock_holders(conn, _REF_TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_FK_NAME}")
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
