"""add work_items.claimed_by_run_id and work_items.claim_notes

The run that holds a work item's claim and the platform's dated notes about
the claim: when the holding run ends, its claims are released with a
note, and another claimant may take an item over from a run that ended. Every
existing item stays NULL: its claim records no run, so it is neither released
nor taken over automatically (``Put back in the queue`` still frees it).

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent (rule 2) - each column is probed first and added with
  ``ADD COLUMN IF NOT EXISTS``.
* Lock-safe (rules 4 and 5) - nullable columns with no default are
  metadata-only; the canonical recipe runs before them (probe,
  ``terminate_lock_holders``, ``set_lock_timeout``). No foreign key and no
  index: the run is read by id, so ``assignment_runs`` and
  ``sub_agent_tasks`` are never locked.
* Reversible (rule 6) - ``downgrade`` drops both columns.

Revision ID: e6879wirun01
Revises: d6758wiatt01
Create Date: 2026-10-31 07:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "e6879wirun01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "d6758wiatt01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "work_items"
_COLUMNS = {"claimed_by_run_id": "UUID", "claim_notes": "JSON"}


def _existing(conn) -> set[str] | None:
    """The table's columns; ``None`` without the table."""
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return None
    return {c["name"] for c in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    conn = op.get_bind()
    existing = _existing(conn)
    if existing is None:
        return
    missing = [name for name in _COLUMNS if name not in existing]
    if not missing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name in missing:
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {name} {_COLUMNS[name]}")


def downgrade() -> None:
    conn = op.get_bind()
    existing = _existing(conn)
    if not existing or not (existing & set(_COLUMNS)):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name in _COLUMNS:
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {name}")
