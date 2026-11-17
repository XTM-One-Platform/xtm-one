"""add platform timers

Adds ``platform_timers``: a timer a run arms for a date, fired by the
scheduler tick as the ``timer.due`` platform event
(``services/platform_timers.py``). The column set mirrors
``app/models/platform_timer.py``: a fresh install runs ``create_all`` +
``stamp head`` while an existing install runs this body, and the two must
converge.

**Lock safety.** ``CREATE TABLE`` with foreign keys takes a ``SHARE ROW
EXCLUSIVE`` lock on ``users`` and ``agents``: canonical recipe (idempotency
probe and early return, ``terminate_lock_holders`` on each referenced table,
``set_lock_timeout``, guarded DDL). The indexes are created on the new, empty
table in the same transaction. The downgrade drops the table with the same
recipe.

Revision ID: t8822timers01
Revises: n8899schres1
Create Date: 2026-11-05 12:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "t8822timers01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "n8899schres1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_timers"


def _table_exists(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": table},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _table_exists(conn, _TABLE):
        return
    terminate_lock_holders(conn, "users")
    terminate_lock_holders(conn, "agents")
    set_lock_timeout(conn)
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            run_id UUID,
            correlation_key VARCHAR(200) NOT NULL,
            due_at TIMESTAMPTZ NOT NULL,
            payload JSON NOT NULL,
            unless_work_item_id UUID,
            unless_shared_state_key VARCHAR(200),
            state VARCHAR(20) NOT NULL,
            outcome TEXT,
            event_id VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL,
            ended_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS uq_platform_timers_armed_key ON {_TABLE} "
        "(agent_id, correlation_key) WHERE state = 'armed'"
    )
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_platform_timers_state_due ON {_TABLE} (state, due_at)")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_platform_timers_user_id ON {_TABLE} (user_id)")


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, _TABLE):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
