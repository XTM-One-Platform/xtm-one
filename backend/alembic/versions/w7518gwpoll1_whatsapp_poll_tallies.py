"""WhatsApp poll tallies of the linked-device gateway backend

Adds ``whatsapp_poll_tallies``: one row per poll a ``whatsapp`` integration on
the linked-device gateway backend saw through its inbound webhook, with the
voters of each option. The column set mirrors
``app/models/whatsapp_poll_tally.py``: a fresh install runs ``create_all`` +
``stamp head`` while an existing install runs this body, and the two must
converge.

**Lock safety.** ``CREATE TABLE`` with a foreign key takes a ``SHARE ROW
EXCLUSIVE`` lock on ``integrations``: canonical recipe (idempotency probe and
early return, ``terminate_lock_holders`` on the referenced table,
``set_lock_timeout``, guarded DDL). The indexes are created on the new, empty
table in the same transaction. The downgrade drops the table with the same
recipe.

Revision ID: w7518gwpoll1
Revises: w7498wainbox1
Create Date: 2026-11-02 21:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "w7518gwpoll1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "w7498wainbox1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "whatsapp_poll_tallies"


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
    terminate_lock_holders(conn, "integrations")
    set_lock_timeout(conn)
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id UUID PRIMARY KEY,
            integration_id UUID NOT NULL REFERENCES integrations(id) ON DELETE CASCADE,
            poll_id VARCHAR(255) NOT NULL,
            conversation_id VARCHAR(255) NOT NULL,
            title TEXT NOT NULL,
            options TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT uq_whatsapp_poll_tallies_poll UNIQUE (integration_id, poll_id)
        )
        """
    )
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_integration_id ON {_TABLE} (integration_id)")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_updated_at ON {_TABLE} (updated_at)")


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, _TABLE):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
