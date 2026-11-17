"""add work_items.origin_run_id and work_items.origin_flow_run_id

Where a work item was created: the assignment run and the flow run
its creating tool ran in, written by the platform at creation (the
conversation already has its column, ``conversation_id``). Every existing item
stays NULL: its origin was never recorded.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent - both columns are probed first and added with ``ADD COLUMN IF
  NOT EXISTS``.
* Lock-safe - nullable columns with no default are metadata-only; the canonical
  recipe runs before them (probe, ``terminate_lock_holders``,
  ``set_lock_timeout``). Neither column carries a foreign key: one would lock
  ``assignment_runs`` / ``flow_runs`` for a link the readers resolve by id, and
  a purged run reads as gone (the ``work_items.run_id`` precedent).

Revision ID: w6168wiorg01
Revises: v6163range01
Create Date: 2026-10-20 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "w6168wiorg01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "v6163range01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "work_items"
_COLUMNS = ("origin_run_id", "origin_flow_run_id")


def _missing(conn) -> list[str] | None:
    """The columns the table lacks; ``None`` without the table."""
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return None
    present = {c["name"] for c in inspector.get_columns(_TABLE)}
    return [column for column in _COLUMNS if column not in present]


def upgrade() -> None:
    conn = op.get_bind()
    missing = _missing(conn)
    if not missing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for column in missing:
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {column} UUID")


def downgrade() -> None:
    conn = op.get_bind()
    missing = _missing(conn)
    if missing is None or len(missing) == len(_COLUMNS):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for column in _COLUMNS:
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {column}")
