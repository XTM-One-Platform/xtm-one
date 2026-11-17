"""record the sync source that imported a knowledge-base document

``knowledge_documents.sync_source_id`` names the ``kb_sync_sources`` row whose
sync imported the document, so a sync's full-refresh sweep and a force re-sync
delete only that source's documents: two Notion sources in one knowledge base
no longer replace each other's pages on every run.

Nothing is backfilled: existing documents keep a NULL owner, which the sync
reads as "unowned" (``kb_sync.owned_sync_documents``), and each source claims
the documents it sees on its next run.

No foreign key and no index, on purpose: the key would lock
``kb_sync_sources`` and validate every row of ``knowledge_documents``, and the
code needs neither (a removed source's id is read as unowned; the sweep
already filters on the indexed ``knowledge_base_id``).

Lock profile: adding a nullable column without a default is metadata-only but
takes a brief ACCESS EXCLUSIVE lock on ``knowledge_documents``, so it follows
the canonical recipe (probe, ``terminate_lock_holders``, ``set_lock_timeout``);
the downgrade drops it the same way.

Revision ID: k5351kbdocsrc01
Revises: p5251stop01
Create Date: 2026-10-24 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "k5351kbdocsrc01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "p5251stop01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "knowledge_documents"
_COLUMN = "sync_source_id"


def _has_column(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"
            ),
            {"t": _TABLE, "c": _COLUMN},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} UUID")


def downgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
