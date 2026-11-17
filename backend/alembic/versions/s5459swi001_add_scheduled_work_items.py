"""scheduled work items: the columns the native dispatcher reads

A work item whose ``payload.action`` is an executable action is run by the
platform at its ``due_at``, with no assignment and no flow. Four nullable
columns carry what that needs - ``action_mode`` (the scheduler's only filter),
``run_id`` (the assignment-less run that executed it), ``conversation_id``
(where its outcome is posted) and ``outcome_code`` (why it is blocked:
``missed``, ``permission``, ...) - and one partial index covers the slice the
60 s tick reads. Every existing item stays NULL: nothing becomes executable.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent - every column is probed first and added with ``ADD COLUMN IF NOT
  EXISTS``, the index with ``create_index_in_place`` (a valid index takes no
  lock, an INVALID one is dropped and rebuilt).
* Lock-safe - nullable columns with no default are metadata-only; the canonical
  recipe runs before them (probe, ``terminate_lock_holders``,
  ``set_lock_timeout``). ``run_id`` and ``conversation_id`` carry NO foreign
  key on purpose: one would take a lock on ``assignment_runs`` /
  ``conversations``, two of the hottest tables, for a link the readers resolve
  by id anyway (the ``annotations.target_id`` precedent). The index is built in
  place in an autocommit block, never ``CONCURRENTLY``: a concurrent build
  waits for every older snapshot on the whole database, which a rolling deploy
  on a busy install never lets it outlast. A failed build does not stop
  startup: the index is declared on the model, so the boot-time model index
  reconciliation builds it after the upgrade.

Revision ID: s5459swi001
Revises: a5450uniq01
Create Date: 2026-10-07 01:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import (
    create_index_in_place,
    set_lock_timeout,
    terminate_lock_holders,
)

revision: str = "s5459swi001"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "a5450uniq01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "work_items"
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("action_mode", "VARCHAR(20)"),
    ("run_id", "UUID"),
    ("conversation_id", "UUID"),
    ("outcome_code", "VARCHAR(40)"),
)
_INDEX = "ix_work_items_scheduled_due"
_INDEX_WHERE = "action_mode IS NOT NULL AND status = 'open'"


def _missing_columns(conn) -> list[tuple[str, str]]:
    present = {
        row[0]
        for row in conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t"
            ),
            {"t": _TABLE},
        )
    }
    return [(name, ddl) for name, ddl in _COLUMNS if name not in present]


def upgrade() -> None:
    conn = op.get_bind()
    missing = _missing_columns(conn)
    if missing:
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        for name, ddl in missing:
            conn.execute(sa.text(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {name} {ddl}"))
    with op.get_context().autocommit_block():
        create_index_in_place(conn, _INDEX, _TABLE, f"(due_at) WHERE {_INDEX_WHERE}")


def downgrade() -> None:
    conn = op.get_bind()
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}"))
    present = [name for name, _ddl in _COLUMNS if (name, _ddl) not in _missing_columns(conn)]
    if not present:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name in present:
        conn.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {name}"))
