"""Platform Health module checkers: the check report store

Slice S15c of issue #101 (design ``dev-docs/platform-health.md`` section 13.6):
``health_module_check_reports``, one row per module checker cycle with the verdict of
every check of its playbook and the counts.

Nothing is backfilled. Lock profile: one new empty table and its index, no foreign
key, no change to an existing table, so no lock on a live table. Re-running the body
after an interruption is safe (``IF NOT EXISTS``). The column set mirrors
``app/models/health_module_analyst.py``, so a fresh install's ``create_all`` and this
body agree.

Revision ID: h129healthcheckreports01
Revises: o1134prov01
Create Date: 2026-11-22 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "h129healthcheckreports01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "o1134prov01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "health_module_check_reports"


def upgrade() -> None:
    set_lock_timeout(op.get_bind())
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id UUID PRIMARY KEY,
            registration_id UUID NOT NULL,
            cycle_id UUID NOT NULL,
            module VARCHAR(16) NOT NULL,
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            passed INTEGER NOT NULL DEFAULT 0,
            warned INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            not_run INTEGER NOT NULL DEFAULT 0,
            checks JSONB NOT NULL DEFAULT '[]'::jsonb,
            CONSTRAINT uq_health_module_check_reports_cycle UNIQUE (cycle_id)
        )
        """
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_health_module_check_reports_registration_finished "
        f"ON {_TABLE} (registration_id, finished_at)"
    )


def downgrade() -> None:
    set_lock_timeout(op.get_bind())
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
