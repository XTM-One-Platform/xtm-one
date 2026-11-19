"""add the file retention columns: last use, tombstone and the stored policy

``file_attachments.last_accessed_at``: the last download, preview, read or
attach of a file; NULL reads as ``created_at`` (no backfill, no rewrite).
``file_attachments.deleted_at`` / ``deleted_reason``: the tombstone a retention
purge leaves, the bytes gone and the row kept so every surface draws a
"deleted" card. ``platform_settings.file_retention``: the days per file
category an administrator stored (``services/file_retention.py``).

Nullable columns without a default: metadata-only on PG 11+, no rewrite.
Lock-safe recipe both ways (idempotency probe, ``terminate_lock_holders``,
``set_lock_timeout``, guarded DDL).

Revision ID: c244fret00001
Revises: h121healthpet
Create Date: 2026-11-19 10:30:00.000000
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c244fret00001"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "h121healthpet"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("file_attachments", "last_accessed_at", "TIMESTAMP WITH TIME ZONE"),
    ("file_attachments", "deleted_at", "TIMESTAMP WITH TIME ZONE"),
    ("file_attachments", "deleted_reason", "VARCHAR(32)"),
    ("platform_settings", "file_retention", "JSON"),
)


def _has_column(conn: Any, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    for table, column, kind in _COLUMNS:
        if _has_column(conn, table, column):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {kind}")


def downgrade() -> None:
    conn = op.get_bind()
    for table, column, _kind in reversed(_COLUMNS):
        if not _has_column(conn, table, column):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
