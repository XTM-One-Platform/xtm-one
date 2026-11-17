"""integrations.connection_test_status: the result of the last connectivity test

One nullable string so the integration page can say "Not tested" for a key
nobody tested and follow the result of the last Test connection (#11837).
NULL on every existing row, which the page reads as before: no default and
no backfill, so nothing is rewritten.

Revision ID: ictest01
Revises: s11839ssroll01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "ictest01"
down_revision: str | None = "s11839ssroll01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "integrations"
_COLUMN = "connection_test_status"


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
    # Nullable with no default is catalogue-only: no table rewrite.
    set_lock_timeout(conn)
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=20), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    set_lock_timeout(conn)
    op.drop_column(_TABLE, _COLUMN)
