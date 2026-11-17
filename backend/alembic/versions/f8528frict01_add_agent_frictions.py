"""add agent_frictions: what a run struggled with, and what was done about it

Adds ``agent_frictions`` (``app/models/agent_friction.py``): one row per
friction of a run (a tool error, a retry, an approval pause, a sandbox or
runner failure, a manual observation...), deduplicated within the run by
(kind, tool, evidence hash) with a count, then classified and disposed of.
The column set mirrors the model: a fresh install runs ``create_all`` +
``stamp head`` while an existing install runs this body, and the two must
converge.

**Lock safety.** ``CREATE TABLE`` with foreign keys takes a ``SHARE ROW
EXCLUSIVE`` lock on ``users`` and ``agents``: canonical recipe (idempotency
probe and early return, ``terminate_lock_holders`` on each referenced table,
``set_lock_timeout``, guarded DDL). The indexes are created on the new, empty
table in the same transaction. The downgrade drops the table with the same
recipe.

Revision ID: f8528frict01
Revises: v8486tsub01
Create Date: 2026-11-04 22:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "f8528frict01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "v8486tsub01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agent_frictions"


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
            user_id UUID REFERENCES users(id) ON DELETE CASCADE,
            agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            run_id UUID,
            assignment_id UUID,
            work_item_id UUID,
            kind VARCHAR(40) NOT NULL,
            tool VARCHAR(200) NOT NULL,
            evidence TEXT NOT NULL,
            evidence_hash VARCHAR(64) NOT NULL,
            count INTEGER NOT NULL,
            friction_class VARCHAR(40) NOT NULL,
            disposition_kind VARCHAR(40),
            disposition_ref VARCHAR(500),
            recorded_at TIMESTAMPTZ NOT NULL,
            last_seen_at TIMESTAMPTZ NOT NULL,
            disposed_at TIMESTAMPTZ
        )
        """
    )
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_user_id ON {_TABLE} (user_id)")
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_run_dedupe ON {_TABLE} (run_id, kind, tool, evidence_hash)"
    )
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_agent_recorded ON {_TABLE} (agent_id, recorded_at)")


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, _TABLE):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
