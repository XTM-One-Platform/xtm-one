"""organization_context_syncs: exclusion list, upstream deletion on by default (#1134)

A sync now keeps an exclusion list (``excluded_external_ids``: the ids of the
source's rows an administrator deleted from the profile with "Exclude from
this sync"), and a deletion upstream deletes the row in XTM One: every
section sync (not the global ``all`` schedule) is switched to
``remove_gone`` true and new syncs default to it. A truncated or failed read
still removes nothing (``organization_context_import.plan_import``).

Lock safety: one ADD COLUMN with a constant default (catalogue-only since
PostgreSQL 11), one ALTER of a server default (catalogue-only) and an UPDATE
of a table that holds a handful of rows. Guarded: the migrations CI job runs
upgrade twice. Downgrade drops the column and restores the default false; the
``remove_gone`` values are left as they are (keeping them true deletes nothing
a person typed).

Revision ID: o1134prov01
Revises: s1087setupver1
Create Date: 2026-11-22 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "o1134prov01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "s1087setupver1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "organization_context_syncs"
_COLUMN = "excluded_external_ids"


def _column_exists(conn: sa.engine.Connection, table: str, column: str) -> bool:
    inspector = sa.inspect(conn)
    if not inspector.has_table(table):
        return False
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE):
        return
    set_lock_timeout(conn)
    if not _column_exists(conn, _TABLE, _COLUMN):
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        )
    op.alter_column(_TABLE, "remove_gone", server_default=sa.text("true"))
    op.execute(
        sa.text(
            "UPDATE organization_context_syncs SET remove_gone = true WHERE section <> 'all' AND remove_gone = false"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE):
        return
    set_lock_timeout(conn)
    op.alter_column(_TABLE, "remove_gone", server_default=sa.text("false"))
    if _column_exists(conn, _TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)
