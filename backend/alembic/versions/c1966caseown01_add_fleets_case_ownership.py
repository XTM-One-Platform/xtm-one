"""fleets.case_ownership: the CTEM cases a fleet runs (#1966)

One nullable JSON column: ``{"paths": null | [trigger path, ...], "key": str}``.
NULL = the fleet runs no case. The built-in CTEM fleets get theirs from the seed
at the next boot (``BUILTIN_FLEETS[...]["case_ownership"]``), so no backfill.
Nullable with no default: catalogue-only.

Revision ID: c1966caseown01
Revises: s1495swfield01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "c1966caseown01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "s1495swfield01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "fleets"
_COLUMN = "case_ownership"


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
    set_lock_timeout(conn)
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.JSON(none_as_null=True), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    set_lock_timeout(conn)
    op.drop_column(_TABLE, _COLUMN)
