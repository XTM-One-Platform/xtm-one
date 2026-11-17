"""add work_items.attachment_file_ids

The files a work item's creator attached to it: ``file_attachments``
ids, checked against the creating turn when the item is created, that the
owner's run reads whatever conversation it runs in. Every existing item stays
NULL: it has no attachment.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent - the column is probed first and added with ``ADD COLUMN IF NOT
  EXISTS``.
* Lock-safe - a nullable column with no default is metadata-only; the
  canonical recipe runs before it (probe, ``terminate_lock_holders``,
  ``set_lock_timeout``). A JSON list of ids carries no foreign key, so
  ``file_attachments`` is never locked.

Revision ID: d6758wiatt01
Revises: c6734cont0001
Create Date: 2026-10-30 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "d6758wiatt01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c6734cont0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "work_items"
_COLUMN = "attachment_file_ids"


def _has_column(conn) -> bool | None:
    """Whether the table has the column; ``None`` without the table."""
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return None
    return _COLUMN in {c["name"] for c in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn) is not False:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} JSON")


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
