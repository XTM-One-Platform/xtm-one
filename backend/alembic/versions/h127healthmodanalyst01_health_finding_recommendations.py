"""Platform Health Module Analyst: recommendations on findings

Slice S14a of issue #101 (design ``dev-docs/platform-health.md`` section 12.4):
``health_finding_recommendations``, one row per finding and Module Analyst cycle.

Nothing is backfilled. Lock profile: one new empty table and its indexes, no foreign
key, no change to an existing table, so no lock on a live table. Re-running the body
after an interruption is safe (``IF NOT EXISTS``). The column set mirrors
``app/models/health_module_analyst.py``, so a fresh install's ``create_all`` and this
body agree.

Revision ID: h127healthmodanalyst01
Revises: c826ctemsignals1
Create Date: 2026-11-21 08:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "h127healthmodanalyst01"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c826ctemsignals1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "health_finding_recommendations"


def upgrade() -> None:
    set_lock_timeout(op.get_bind())
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id UUID PRIMARY KEY,
            finding_id VARCHAR(64) NOT NULL,
            registration_id UUID NOT NULL,
            cycle_id UUID NOT NULL,
            module VARCHAR(16) NOT NULL,
            text TEXT NOT NULL,
            evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
            confidence VARCHAR(8) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_health_finding_recommendations_finding_cycle UNIQUE (finding_id, cycle_id),
            CONSTRAINT ck_health_finding_recommendations_confidence_valid
                CHECK (confidence IN ('high', 'medium', 'low')),
            CONSTRAINT ck_health_finding_recommendations_text_length CHECK (char_length(text) <= 1500)
        )
        """
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_health_finding_recommendations_finding_created "
        f"ON {_TABLE} (finding_id, created_at)"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_health_finding_recommendations_registration ON {_TABLE} (registration_id)"
    )


def downgrade() -> None:
    set_lock_timeout(op.get_bind())
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
