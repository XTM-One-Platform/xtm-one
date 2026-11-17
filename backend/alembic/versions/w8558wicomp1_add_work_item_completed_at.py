"""add completed_at to work_items

``completed_at`` on ``work_items``: when the item reached ``done`` or
``cancelled`` (``models/work_item.py``, kept in step with ``status`` by a mapper
listener), so ``list_work_items`` filters and sorts by completion time.

Nullable with no default: an item that finished before this migration has
none and simply never matches a completion filter, so the ``ADD COLUMN`` is
metadata-only and needs no backfill (rule 4, no unbounded backfill).

Revision ID: w8558wicomp1
Revises: f8528frict01
Create Date: 2026-11-04 23:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "w8558wicomp1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "f8528frict01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "work_items"
_COLUMN = "completed_at"


def _has_column(conn) -> bool | None:
    """Whether the column is there; ``None`` when the table is not."""
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return None
    return _COLUMN in {column["name"] for column in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn) is not False:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} TIMESTAMP WITH TIME ZONE")


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
