"""durable inbox of verified CTEM signal-source deliveries

Adds ``ctem_signal_inbox``: the signal-source webhook writes a verified delivery
here and answers at once, and the autonomous tick records it off the request path
(#2570). The column set mirrors ``app/models/ctem_signal_inbox.py``: a fresh install
runs ``create_all`` + ``stamp head`` while an existing install runs this body, and
the two must converge.

**Lock safety.** A new table with no foreign key: idempotency probe and early
return, ``set_lock_timeout``, guarded DDL; the index is created on the new, empty
table in the same transaction. The downgrade drops the table with the same recipe.

Revision ID: c2570siginbox1
Revises: c2577caseteam01
Create Date: 2026-11-27 07:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c2570siginbox1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c2577caseteam01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "ctem_signal_inbox"


def _table_exists(conn: sa.engine.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": table},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _table_exists(conn, _TABLE):
        return
    set_lock_timeout(conn)
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id UUID PRIMARY KEY,
            provider VARCHAR(50) NOT NULL,
            integration_id VARCHAR(64) NOT NULL,
            event_key VARCHAR(200) NOT NULL,
            payload JSON NOT NULL,
            verified BOOLEAN NOT NULL,
            attempts INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_created_at ON {_TABLE} (created_at)")


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, _TABLE):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
