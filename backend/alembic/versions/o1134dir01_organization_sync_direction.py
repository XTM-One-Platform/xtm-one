"""organization_context_syncs: direction and the outbound rows a sync created (#1134 S3)

A sync now has a direction: ``inbound`` (the platform is the master, the
default and every existing sync) or ``outbound`` (the XTM One list is the
master and the platform rows this sync created follow it). ``outbound_ids``
keeps those rows, keyed on the XTM One row, so a run updates and deletes only
what it created.

Lock safety: two ADD COLUMN with a constant default (catalogue-only since
PostgreSQL 11) and one CHECK constraint added NOT VALID then validated on a
table that holds a handful of rows. Guarded: the migrations CI job runs
upgrade twice. Downgrade drops the constraint and both columns (an outbound
sync then reads as an inbound one; nothing on the platforms is touched).

Revision ID: o1134dir01
Revises: o1178assetsbom01
Create Date: 2026-11-22 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "o1134dir01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "o1178assetsbom01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "organization_context_syncs"
_CHECK = "ck_organization_context_syncs_direction_valid"


def _columns(conn: sa.engine.Connection) -> set[str]:
    return {col["name"] for col in sa.inspect(conn).get_columns(_TABLE)}


def _has_check(conn: sa.engine.Connection) -> bool:
    return any(c.get("name") == _CHECK for c in sa.inspect(conn).get_check_constraints(_TABLE))


def upgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE):
        return
    set_lock_timeout(conn)
    columns = _columns(conn)
    if "direction" not in columns:
        op.add_column(
            _TABLE, sa.Column("direction", sa.String(16), nullable=False, server_default=sa.text("'inbound'"))
        )
    if "outbound_ids" not in columns:
        op.add_column(
            _TABLE,
            sa.Column("outbound_ids", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        )
    if not _has_check(conn):
        op.execute(
            sa.text(
                f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CHECK} CHECK (direction IN ('inbound', 'outbound')) NOT VALID"
            )
        )
        op.execute(sa.text(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {_CHECK}"))


def downgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE):
        return
    set_lock_timeout(conn)
    if _has_check(conn):
        op.drop_constraint(_CHECK, _TABLE, type_="check")
    columns = _columns(conn)
    for column in ("outbound_ids", "direction"):
        if column in columns:
            op.drop_column(_TABLE, column)
