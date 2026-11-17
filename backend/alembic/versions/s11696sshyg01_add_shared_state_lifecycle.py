"""add shared_states lifecycle columns (lease, value age, linked item, expiry)

Shared state hygiene (#11696): an entry knows what ends it, so the platform
retires it instead of leaving it active forever.

* ``is_lease`` - the entry was created by a ``hold="take"`` to hold a lease;
  it is resolved when its hold is released (by its holder, or by the tick once
  the holder run ended). NULL reads as false.
* ``value_updated_at`` - when the VALUE last changed. A note bumps
  ``updated_at`` but not this, so staleness reads the value's age.
* ``linked_work_item_id`` - the work item the entry tracks: resolved once that
  item is done or cancelled.
* ``expires_at`` - resolved at that date.
* ``terminal_condition`` - free text naming what ends the entry, shown in reads.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent (rule 2) - the columns are probed first and added with
  ``ADD COLUMN IF NOT EXISTS``.
* Lock-safe (rules 4 and 5) - five nullable columns with no default, in ONE
  ``ALTER TABLE`` (metadata-only), after the canonical recipe (probe,
  ``terminate_lock_holders``, ``set_lock_timeout``). No foreign key and no
  index: the work item is read by id, so ``work_items`` is never locked.
* Reversible (rule 6) - ``downgrade`` drops the columns.

Revision ID: s11696sshyg01
Revises: v11458brand01
Create Date: 2026-11-13 23:50:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "s11696sshyg01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "v11458brand01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "shared_states"
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("is_lease", "BOOLEAN"),
    ("value_updated_at", "TIMESTAMP WITH TIME ZONE"),
    ("linked_work_item_id", "UUID"),
    ("expires_at", "TIMESTAMP WITH TIME ZONE"),
    ("terminal_condition", "TEXT"),
)


def _existing(conn: sa.engine.Connection) -> set[str] | None:
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
    missing = [(name, kind) for name, kind in _COLUMNS if name not in existing]
    if not missing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    adds = ", ".join(f"ADD COLUMN IF NOT EXISTS {name} {kind}" for name, kind in missing)
    op.execute(f"ALTER TABLE {_TABLE} {adds}")


def downgrade() -> None:
    conn = op.get_bind()
    existing = _existing(conn)
    if not existing:
        return
    present = [name for name, _kind in _COLUMNS if name in existing]
    if not present:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    drops = ", ".join(f"DROP COLUMN IF EXISTS {name}" for name in present)
    op.execute(f"ALTER TABLE {_TABLE} {drops}")
