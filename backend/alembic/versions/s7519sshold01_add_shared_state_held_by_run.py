"""add shared_states.held_by_run_id

The run holding a shared-state entry as a lease: stamped by
``set_shared_state(hold="take")``, refused to another run while it lives,
taken over or released once it ended. Every existing entry stays NULL: it
records no holder, so nothing is released or taken over automatically.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent (rule 2) - the column is probed first and added with
  ``ADD COLUMN IF NOT EXISTS``.
* Lock-safe (rules 4 and 5) - a nullable column with no default is
  metadata-only; the canonical recipe runs before it (probe,
  ``terminate_lock_holders``, ``set_lock_timeout``). No foreign key and no
  index: the run is read by id, so ``assignment_runs`` and
  ``sub_agent_tasks`` are never locked.
* Reversible (rule 6) - ``downgrade`` drops the column.

Revision ID: s7519sshold01
Revises: w7498wainbox1
Create Date: 2026-11-02 21:15:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "s7519sshold01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "w7498wainbox1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "shared_states"
_COLUMN = "held_by_run_id"


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
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} UUID")


def downgrade() -> None:
    conn = op.get_bind()
    existing = _existing(conn)
    if not existing or _COLUMN not in existing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
