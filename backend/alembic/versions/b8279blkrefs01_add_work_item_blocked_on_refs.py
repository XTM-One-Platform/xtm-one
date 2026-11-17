"""add work_items.blocked_on_refs, blocked_reason, needs_human

A blocked work item always says why, and a pull request or issue releases it
when it merges or closes: ``blocked_on_refs`` (JSON list of code-host
references), ``blocked_reason`` (free text) and ``needs_human`` (boolean).
Every existing item stays NULL on the three: a legacy blocked item with no
reason is shown "No reason" until edited.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent (rule 2) - the columns are probed first and added with
  ``ADD COLUMN IF NOT EXISTS``.
* Lock-safe (rules 4 and 5) - nullable columns with no default are
  metadata-only; the canonical recipe runs before them (probe,
  ``terminate_lock_holders``, ``set_lock_timeout``). No index: the release
  reads blocked items only.
* Reversible (rule 6) - ``downgrade`` drops the columns.

Revision ID: b8279blkrefs01
Revises: m8156bigrp01
Create Date: 2026-11-03 23:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "b8279blkrefs01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "m8156bigrp01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "work_items"
_COLUMNS = (("blocked_on_refs", "JSON"), ("blocked_reason", "TEXT"), ("needs_human", "BOOLEAN"))


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
    missing = [(name, kind) for name, kind in _COLUMNS if name not in existing]
    if not missing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name, kind in missing:
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {name} {kind}")


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
    for name in present:
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {name}")
