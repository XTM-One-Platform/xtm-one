"""add shared_states.retention (the rolling policy of an entry's lists)

Shared state retention (#11839): an entry may carry a retention policy
(``keep_last``, ``max_chars``, ``archive_key``, ``lists``) applied on every
write, so an append on a list at its size cap rolls the oldest items into the
archive key instead of being refused.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent (rule 2) - the column is probed first and added with
  ``ADD COLUMN IF NOT EXISTS``.
* Lock-safe (rules 4 and 5) - one nullable column with no default
  (metadata-only), after the canonical recipe (probe,
  ``terminate_lock_holders``, ``set_lock_timeout``). No index.
* Reversible (rule 6) - ``downgrade`` drops the column.

Revision ID: s11839ssroll01
Revises: acof01callerfmt
Create Date: 2026-11-14 16:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "s11839ssroll01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "acof01callerfmt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "shared_states"
_COLUMN = "retention"


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
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} JSON")


def downgrade() -> None:
    conn = op.get_bind()
    existing = _existing(conn)
    if not existing or _COLUMN not in existing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
