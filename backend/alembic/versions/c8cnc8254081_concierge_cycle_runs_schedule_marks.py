"""platform concierge: cycle runs and digests, schedule marks, one open cycle per target (#8261)

Level 8 of the Platform Concierge (#1783, ``dev-docs/platform-concierge.md``):

* ``concierge_cycle_runs``: the run a cycle started, who asked, its digest;
* ``concierge_schedule_marks``: when each fleet-wide schedule (and, from
  ``c8cnc8254082``, each signal pass) last fired;
* a partial unique index on level 1's ``concierge_cycles``: at most one open
  cycle per target (the acceptance rule "a signal opens at most one cycle per
  target at a time" is decided by the database, not by a read-then-write).

Safety contract (``dev-docs/reference/migrations-safety.md``):

* Idempotent - every table and index is guarded (``IF NOT EXISTS``).
* Lock-safe - the new tables are created empty and take no lock on an
  existing table but ``concierge_cycles`` (the foreign key and the indexes):
  a new, small table level 1 created, still under the canonical recipe
  (``terminate_lock_holders`` + ``set_lock_timeout``). The index is built
  with a plain ``CREATE INDEX``: the table is small and new.
* Reversible - the downgrade drops what was added; digests and schedule
  marks go with it.

Revision ID: c8cnc8254081
Revises: c7cnc8254071
Create Date: 2026-11-03 23:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c8cnc8254081"
# The last migration of the nearest lower Concierge level; re-pointed when a
# lower level that adds one is merged (stack protocol).
down_revision: str | Sequence[str] | None = "c7cnc8254071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CYCLES = "concierge_cycles"
_RUNS = "concierge_cycle_runs"
_MARKS = "concierge_schedule_marks"


def upgrade() -> None:
    conn = op.get_bind()
    terminate_lock_holders(conn, _CYCLES)
    set_lock_timeout(conn)
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS uq_concierge_cycles_open_target ON {_CYCLES} "
        "(target_type, target_id) WHERE status = 'open' AND target_id IS NOT NULL"
    )
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_RUNS} (
            cycle_id UUID PRIMARY KEY REFERENCES {_CYCLES} (id) ON DELETE CASCADE,
            agent_id UUID,
            assignment_id UUID,
            run_id UUID,
            target_type VARCHAR(16),
            target_id UUID,
            signal JSONB,
            requested_by_user_id UUID,
            digest JSONB,
            digest_built_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_concierge_cycle_runs_run_id ON {_RUNS} (run_id)")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_concierge_cycle_runs_target ON {_RUNS} (target_type, target_id)")
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_MARKS} (
            schedule VARCHAR(16) PRIMARY KEY,
            fired_at TIMESTAMPTZ NOT NULL,
            cycle_id UUID,
            refusal VARCHAR(32)
        )
        """
    )


def downgrade() -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    op.execute(f"DROP TABLE IF EXISTS {_MARKS}")
    op.execute(f"DROP TABLE IF EXISTS {_RUNS}")
    terminate_lock_holders(conn, _CYCLES)
    set_lock_timeout(conn)
    op.execute("DROP INDEX IF EXISTS uq_concierge_cycles_open_target")
