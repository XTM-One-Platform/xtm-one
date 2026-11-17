"""agent memory: subjects and supersession

* ``agent_memory_entries.subject_key``: the object an entry describes
  (``presentation:<id>``, ``doc:X``). A newer ``state`` entry on the same
  subject supersedes the older one.
* ``agent_memory_entries.superseded_at`` / ``superseded_by_id``: set once an
  entry no longer holds, by a newer entry or retired with no successor. The
  successor is referenced by id without a key.
* ``ix_agent_memory_entries_agent_subject``: partial index on
  ``(agent_id, subject_key)`` for the rows that name a subject.

Purely additive: every existing entry keeps NULL in the three columns, which is
what it was before - about no subject, never superseded. Nothing is backfilled.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent - the columns are probed first and added with ``ADD COLUMN IF NOT
  EXISTS``; the index is built with ``create_index_in_place`` (``IF NOT
  EXISTS``, an INVALID residue dropped first).
* Lock-safe - three nullable columns with no default are metadata-only, added
  in one ``ALTER TABLE`` after the canonical recipe (probe,
  ``terminate_lock_holders``, ``set_lock_timeout``). The index covers only rows
  naming a subject, none on an existing install, so its in-place build is
  instant; a failed build is left to the model index reconciliation.
* Reversible - the downgrade drops the index and the columns, under the belt.

Revision ID: m5688sup01
Revises: t5631act01
Create Date: 2026-10-11 21:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import (
    create_index_in_place,
    set_lock_timeout,
    terminate_lock_holders,
)

revision: str = "m5688sup01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "t5631act01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agent_memory_entries"
_INDEX = "ix_agent_memory_entries_agent_subject"
_COLUMNS = {
    "subject_key": "VARCHAR(255)",
    "superseded_at": "TIMESTAMP WITH TIME ZONE",
    "superseded_by_id": "UUID",
}


def _has_table(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :t"
            ),
            {"t": _TABLE},
        ).scalar()
    )


def _present_columns(conn) -> set[str]:
    rows = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t "
            "AND column_name = ANY(:c)"
        ),
        {"t": _TABLE, "c": list(_COLUMNS)},
    )
    return {row[0] for row in rows}


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn):
        # Fresh install: create_all builds the table with the columns and the
        # index declared on the model and stamps head.
        return
    missing = [name for name in _COLUMNS if name not in _present_columns(conn)]
    if missing:
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        additions = ", ".join(
            f"ADD COLUMN IF NOT EXISTS {name} {_COLUMNS[name]}" for name in missing
        )
        conn.execute(sa.text(f"ALTER TABLE {_TABLE} {additions}"))
    with op.get_context().autocommit_block():
        create_index_in_place(
            conn,
            _INDEX,
            _TABLE,
            "(agent_id, subject_key) WHERE subject_key IS NOT NULL",
        )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn):
        return
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}"))
    present = [name for name in _COLUMNS if name in _present_columns(conn)]
    if not present:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    drops = ", ".join(f"DROP COLUMN IF EXISTS {name}" for name in present)
    conn.execute(sa.text(f"ALTER TABLE {_TABLE} {drops}"))
