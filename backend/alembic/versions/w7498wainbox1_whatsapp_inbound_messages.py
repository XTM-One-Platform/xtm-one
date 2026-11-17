"""WhatsApp messages received for a whatsapp integration

Adds ``whatsapp_inbound_messages``: the messages a connected ``whatsapp``
integration's business number received, kept ``inbound_retention_days`` and
read by its ``list_whatsapp_conversations`` / ``read_whatsapp_messages``
tools. The column set mirrors ``app/models/whatsapp_inbound_message.py``: a
fresh install runs ``create_all`` + ``stamp head`` while an existing install
runs this body, and the two must converge.

**Lock safety.** ``CREATE TABLE`` with a foreign key takes a ``SHARE ROW
EXCLUSIVE`` lock on ``integrations``: canonical recipe (idempotency probe and
early return, ``terminate_lock_holders`` on the referenced table,
``set_lock_timeout``, guarded DDL). The indexes are created on the new, empty
table in the same transaction. The downgrade drops the table with the same
recipe.

Revision ID: w7498wainbox1
Revises: v7370relau01
Create Date: 2026-11-02 19:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "w7498wainbox1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "v7370relau01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "whatsapp_inbound_messages"


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
            wamid VARCHAR(255) NOT NULL,
            conversation_id VARCHAR(255) NOT NULL,
            chat_type VARCHAR(16) NOT NULL,
            sender VARCHAR(64) NOT NULL,
            sender_name VARCHAR(255),
            message_type VARCHAR(32) NOT NULL,
            text TEXT NOT NULL,
            media_id VARCHAR(64),
            received_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT uq_whatsapp_inbound_messages_wamid UNIQUE (integration_id, wamid)
        )
        """
    )
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_integration_id ON {_TABLE} (integration_id)")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_conversation_id ON {_TABLE} (conversation_id)")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_received_at ON {_TABLE} (received_at)")


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, _TABLE):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
