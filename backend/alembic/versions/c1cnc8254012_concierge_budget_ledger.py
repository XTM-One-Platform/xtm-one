"""platform concierge: the budget ledger (#8254)

Level 1 of the Platform Concierge (#1783, ``dev-docs/platform-concierge.md``
6.12): ``concierge_cycles``, ``concierge_budget_months`` and
``concierge_budget_work``, the Concierge's own line of the quota, read and
written only by ``services/concierge/budget.py``.

Safety contract (``dev-docs/reference/migrations-safety.md``):

* Idempotent - each table is probed first and created ``IF NOT EXISTS``.
* Lock-safe - the tables are new and empty, created with their indexes, with
  no foreign key: no lock on any existing table.
* Reversible - the downgrade drops the tables and the ledger with them.

Revision ID: c1cnc8254012
Revises: c1cnc8254011
Create Date: 2026-11-03 23:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "c1cnc8254012"
down_revision: str | Sequence[str] | None = "c1cnc8254011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CYCLES = "concierge_cycles"
_MONTHS = "concierge_budget_months"
_WORK = "concierge_budget_work"


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_CYCLES):
        set_lock_timeout(conn)
        op.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_CYCLES} (
                id UUID PRIMARY KEY,
                specialist VARCHAR(32) NOT NULL,
                trigger VARCHAR(16) NOT NULL,
                target_type VARCHAR(16),
                target_id UUID,
                month VARCHAR(7) NOT NULL,
                status VARCHAR(16) NOT NULL DEFAULT 'open',
                stop_reason TEXT,
                reserved_tokens BIGINT NOT NULL DEFAULT 0,
                reserved_cost_usd NUMERIC(16, 6) NOT NULL DEFAULT 0,
                spent_tokens BIGINT NOT NULL DEFAULT 0,
                spent_cost_usd NUMERIC(16, 6) NOT NULL DEFAULT 0,
                opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                closed_at TIMESTAMPTZ
            )
            """
        )
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_concierge_cycles_month ON {_CYCLES} (month)")
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_concierge_cycles_status_opened ON {_CYCLES} (status, opened_at)")
    if not inspector.has_table(_MONTHS):
        set_lock_timeout(conn)
        op.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_MONTHS} (
                month VARCHAR(7) PRIMARY KEY,
                reserved_tokens BIGINT NOT NULL DEFAULT 0,
                reserved_cost_usd NUMERIC(16, 6) NOT NULL DEFAULT 0,
                spent_tokens BIGINT NOT NULL DEFAULT 0,
                spent_cost_usd NUMERIC(16, 6) NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    if not inspector.has_table(_WORK):
        set_lock_timeout(conn)
        op.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_WORK} (
                kind VARCHAR(32) NOT NULL,
                ref_id UUID NOT NULL,
                cycle_id UUID NOT NULL,
                held_tokens BIGINT NOT NULL DEFAULT 0,
                held_cost_usd NUMERIC(16, 6) NOT NULL DEFAULT 0,
                charged_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (kind, ref_id)
            )
            """
        )
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_concierge_budget_work_cycle_id ON {_WORK} (cycle_id)")


def downgrade() -> None:
    set_lock_timeout(op.get_bind())
    op.execute(f"DROP TABLE IF EXISTS {_WORK}")
    op.execute(f"DROP TABLE IF EXISTS {_MONTHS}")
    op.execute(f"DROP TABLE IF EXISTS {_CYCLES}")
