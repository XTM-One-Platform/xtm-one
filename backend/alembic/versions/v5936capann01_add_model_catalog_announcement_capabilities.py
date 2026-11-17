"""add model_catalog_announcements.capabilities and previous_capabilities

The image, video, speech, music and decision models the environment pins join
the catalog the model announcements compare: ``capabilities`` is the
set a recorded state holds, ``previous_capabilities`` the one before it. Both
nullable with no default, so the ``ADD COLUMN`` is metadata-only and every row
recorded before reads as "not tracked yet", which the boot compares against
the capabilities stored before its seed.

Canonical lock-safe recipe: idempotency probe -> terminate_lock_holders ->
set_lock_timeout -> guarded DDL.

Revision ID: v5936capann01
Revises: r5931gareport
Create Date: 2026-10-17 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v5936capann01"
down_revision: str | None = "r5931gareport"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "model_catalog_announcements"
_COLUMNS = ("capabilities", "previous_capabilities")


def _existing_columns(conn) -> set[str]:
    return set(
        conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :table "
                "AND column_name = ANY(:columns)"
            ),
            {"table": _TABLE, "columns": list(_COLUMNS)},
        ).scalars()
    )


def upgrade() -> None:
    conn = op.get_bind()
    missing = [column for column in _COLUMNS if column not in _existing_columns(conn)]
    if not missing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(
        f"ALTER TABLE {_TABLE} "
        + ", ".join(f"ADD COLUMN IF NOT EXISTS {column} JSON" for column in missing)
    )


def downgrade() -> None:
    conn = op.get_bind()
    present = [column for column in _COLUMNS if column in _existing_columns(conn)]
    if not present:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(
        f"ALTER TABLE {_TABLE} "
        + ", ".join(f"DROP COLUMN IF EXISTS {column}" for column in present)
    )
