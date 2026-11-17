"""AI-disabled mode: runtime override on the settings singleton

One nullable boolean so an administrator can turn AI off or on without a
restart. NULL means "follow PLATFORM_MODE" — no default and no
backfill, or existing installs lose that state and PLATFORM_MODE stops taking
effect.

Revision ID: ai1disabled01
Revises: f1fail3128
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "ai1disabled01"
down_revision: str | None = "f1fail3128"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_settings"
_COLUMN = "ai_disabled"


def _column_exists(conn, table: str, column: str) -> bool:
    inspector = sa.inspect(conn)
    if not inspector.has_table(table):
        return False
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    conn = op.get_bind()
    # Guarded: the migrations CI job runs upgrade twice.
    if _column_exists(conn, _TABLE, _COLUMN):
        return
    # Nullable with no default is catalogue-only, so this should never wait —
    # the belt is for a concurrent lock holder, not for the rewrite there isn't.
    set_lock_timeout(conn)
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Boolean(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    set_lock_timeout(conn)
    op.drop_column(_TABLE, _COLUMN)
