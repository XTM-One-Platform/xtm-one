"""platform concierge: signal states (#8261)

Level 8 of the Platform Concierge (#1783, ``dev-docs/platform-concierge.md``):
``concierge_signal_states``, the edge-triggered state of each signal
(``services/concierge/signals.py``). A crossing starts one investigation;
staying crossed starts nothing; clearing re-arms it.

Safety contract (``dev-docs/reference/migrations-safety.md``):

* Idempotent - the table and its index are guarded (``IF NOT EXISTS``).
* Lock-safe - one new, empty table, no foreign key: no lock on an existing
  table (``set_lock_timeout`` still bounds the statement).
* Reversible - the downgrade drops the table; the signal states go with it.

Revision ID: c8cnc8254082
Revises: c8cnc8254081
Create Date: 2026-11-04 21:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "c8cnc8254082"
down_revision: str | Sequence[str] | None = "c8cnc8254081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIGNALS = "concierge_signal_states"


def upgrade() -> None:
    set_lock_timeout(op.get_bind())
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_SIGNALS} (
            id UUID PRIMARY KEY,
            target_type VARCHAR(16) NOT NULL,
            target_id UUID NOT NULL,
            kind VARCHAR(32) NOT NULL,
            active BOOLEAN NOT NULL DEFAULT false,
            last_value JSONB,
            fired_at TIMESTAMPTZ,
            cycle_id UUID,
            refusal VARCHAR(32),
            evaluated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_concierge_signal_states_target_kind UNIQUE (target_type, target_id, kind)
        )
        """
    )
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_concierge_signal_states_active ON {_SIGNALS} (active)")


def downgrade() -> None:
    set_lock_timeout(op.get_bind())
    op.execute(f"DROP TABLE IF EXISTS {_SIGNALS}")
