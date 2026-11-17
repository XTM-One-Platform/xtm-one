"""add placebo comparisons to evaluations

The placebo-controlled method (``dev-docs/agent-placebo-evaluation.md``), on
the evaluation vertical v1:

* ``evaluations.placebo_config`` - the method's configuration, NULL when the
  evaluation runs without placebo arms;
* ``evaluation_runs.arm`` (``real`` / ``placebo`` / ``no_target``) and
  ``trial_index`` - each trial of an arm is one run -, the target a control run
  replaces or removes, the placebo it ran with, and the comparison its runs
  belong to;
* ``evaluation_placebo_comparisons``, one row per placebo launch, with the
  configuration it ran with.

The per-trial measures need no column: they are the vertical's
``evaluation_metrics`` rows.

Every added column is nullable or carries a constant default, so each
``ADD COLUMN`` is metadata-only: no rewrite, no backfill. Every existing run is
``arm = 'real'``, ``trial_index = 0``, which is what it was. Each ALTER follows
the canonical recipe (idempotency probe, ``terminate_lock_holders``,
``set_lock_timeout``, guarded DDL). The new table's foreign key locks
``evaluations``, so its ``CREATE TABLE`` runs under ``set_lock_timeout``. A
run's ``placebo_comparison_id`` carries no foreign key, as
``owner_execution_id``: both rows go with their evaluation.

Revision ID: r1plc6535001
Revises: e1780evp001
Create Date: 2026-10-25 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "r1plc6535001"
# NOT a constant: must always be the base branch's CURRENT head, re-pointed
# whenever it gains a migration (backend/AGENTS.md > "Database migrations
# (safety)", rule 1).
down_revision: str | Sequence[str] | None = "e1780evp001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COMPARISONS = "evaluation_placebo_comparisons"
_COMPARISONS_INDEX = "ix_evaluation_placebo_comparisons_evaluation_id"

#: Columns added to existing tables, with their DDL type.
_COLUMNS: dict[str, dict[str, str]] = {
    "evaluations": {"placebo_config": "JSON"},
    "evaluation_runs": {
        "arm": "VARCHAR(20) NOT NULL DEFAULT 'real'",
        "trial_index": "INTEGER NOT NULL DEFAULT 0",
        "placebo_target": "JSON",
        "placebo_snapshot": "JSON",
        "placebo_comparison_id": "UUID",
    },
}


def _columns(conn, table: str) -> set[str]:
    inspector = sa.inspect(conn)
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    conn = op.get_bind()

    if not sa.inspect(conn).has_table(_COMPARISONS):
        # Its foreign key takes a SHARE ROW EXCLUSIVE lock on `evaluations`.
        set_lock_timeout(conn)
        op.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_COMPARISONS} (
                id UUID PRIMARY KEY,
                evaluation_id UUID NOT NULL REFERENCES evaluations(id) ON DELETE CASCADE,
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                seed VARCHAR(64) NOT NULL,
                corpus_version VARCHAR(64),
                config JSON,
                created_by UUID,
                results JSON,
                warnings JSON,
                created_at TIMESTAMPTZ NOT NULL,
                completed_at TIMESTAMPTZ
            )
            """
        )
        op.execute(f"CREATE INDEX IF NOT EXISTS {_COMPARISONS_INDEX} ON {_COMPARISONS} (evaluation_id)")

    for table, columns in _COLUMNS.items():
        existing = _columns(conn, table)
        missing = [name for name in columns if name not in existing]
        if not missing:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        additions = ", ".join(f"ADD COLUMN IF NOT EXISTS {name} {columns[name]}" for name in missing)
        op.execute(f"ALTER TABLE {table} {additions}")


def downgrade() -> None:
    conn = op.get_bind()

    for table, columns in reversed(_COLUMNS.items()):
        existing = _columns(conn, table)
        present = [name for name in columns if name in existing]
        if not present:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        removals = ", ".join(f"DROP COLUMN IF EXISTS {name}" for name in present)
        op.execute(f"ALTER TABLE {table} {removals}")

    if sa.inspect(conn).has_table(_COMPARISONS):
        # ACCESS EXCLUSIVE on `evaluations` to drop the FK trigger.
        set_lock_timeout(conn)
        op.execute(f"DROP TABLE IF EXISTS {_COMPARISONS}")
