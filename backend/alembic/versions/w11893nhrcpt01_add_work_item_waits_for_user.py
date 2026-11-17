"""add work_items.waits_for_user_id (the person a needs_human item waits for)

#11893: a needs_human item written by a company-managed agent in a run that
acts for nobody stored user_id NULL and reached no person's bell. The column
holds the resolved recipient when it is not user_id.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent (rule 2) - the column is probed first and added with
  ``ADD COLUMN IF NOT EXISTS``.
* Lock-safe (rules 4 and 5) - one nullable column with no default
  (metadata-only), after the canonical recipe (probe,
  ``terminate_lock_holders``, ``set_lock_timeout``). No index: the clause
  that reads it is always narrowed by status and the user index first.
* Reversible (rule 6) - ``downgrade`` drops the column.

Revision ID: w11893nhrcpt01
Revises: ictest01
Create Date: 2026-11-14 17:50:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "w11893nhrcpt01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "ictest01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "work_items"
_COLUMN = "waits_for_user_id"


def _existing(conn: sa.engine.Connection) -> set[str] | None:
    """The table's columns; ``None`` without the table."""
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return None
    return {c["name"] for c in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    conn = op.get_bind()
    existing = _existing(conn)
    if existing is None or _COLUMN in existing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} UUID REFERENCES users(id) ON DELETE SET NULL")


def downgrade() -> None:
    conn = op.get_bind()
    existing = _existing(conn)
    if not existing or _COLUMN not in existing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
