"""Platform Health Module Analyst: its own spend ceiling ledger

Slice S14b-1 of issue #101 (design ``dev-docs/platform-health.md`` section 12.5):
``health_module_analyst_spend``, one row per UTC day (``D:YYYY-MM-DD``) and month
(``M:YYYY-MM``), the same shape as ``health_analysis_spend``. Apart from it so the
analysis ceiling and the Module Analyst ceiling never share a count.

Nothing is backfilled. Lock profile: one empty table, no foreign key, no lock on an
existing table. Re-running the body after an interruption is safe (``IF NOT EXISTS``).
The column set mirrors ``app/models/health_analysis.py``, so a fresh install's
``create_all`` and this body agree.

Revision ID: h128healthmaspend01
Revises: c891ctemfleetcase1
Create Date: 2026-11-21 15:05:00.000000
"""

from collections.abc import Sequence

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "h128healthmaspend01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c891ctemfleetcase1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SPEND = "health_module_analyst_spend"


def upgrade() -> None:
    set_lock_timeout(op.get_bind())
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_SPEND} (
            period VARCHAR(12) PRIMARY KEY,
            reserved_tokens BIGINT NOT NULL DEFAULT 0,
            reserved_cost_usd NUMERIC(16, 6) NOT NULL DEFAULT 0,
            spent_tokens BIGINT NOT NULL DEFAULT 0,
            spent_cost_usd NUMERIC(16, 6) NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    set_lock_timeout(op.get_bind())
    op.execute(f"DROP TABLE IF EXISTS {_SPEND}")
