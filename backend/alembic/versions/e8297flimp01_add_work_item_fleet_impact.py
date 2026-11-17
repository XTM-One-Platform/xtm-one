"""add work_items.fleet_impact

The agents a work item's completion concerns: a JSON object
``{"targets": [agent ids], "kind", "instructions"}``. Its
``work_item.completed`` event is routed to each target too. Every existing
item stays NULL: it impacts no one beyond its creator, owner and holder.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent (rule 2) - the column is probed first and added with
  ``ADD COLUMN IF NOT EXISTS``.
* Lock-safe (rules 4 and 5) - a nullable column with no default is
  metadata-only; the canonical recipe runs before it (probe,
  ``terminate_lock_holders``, ``set_lock_timeout``). No index: the field is
  read with the row.
* Reversible (rule 6) - ``downgrade`` drops the column.

Revision ID: e8297flimp01
Revises: b8279blkrefs01
Create Date: 2026-11-04 00:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "e8297flimp01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "b8279blkrefs01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "work_items"
_COLUMN = "fleet_impact"


def _existing(conn) -> set[str] | None:
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
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} JSON")


def downgrade() -> None:
    conn = op.get_bind()
    existing = _existing(conn)
    if not existing or _COLUMN not in existing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
