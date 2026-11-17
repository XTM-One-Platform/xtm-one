"""add knowledge_documents.added_by_user_id

The person a knowledge base document was last added or changed for. Re-embedding
existing documents (a reprocess, the platform-wide reset after an embedding model
change) binds no one, so the job had only the knowledge base's owner to check
against the embedding provider's exclusions - and a company-managed
knowledge base has none: a document refused for an excluded person was embedded
for no one, by the provider that excludes them. The job now checks the person
recorded here as well. Nothing is backfilled: the person of an existing document
is not known, and it is recorded the next time it is added, changed or refused.

Lock profile: the ``ADD COLUMN`` is nullable with no default (metadata-only) and
follows the canonical recipe (probe, ``terminate_lock_holders``,
``set_lock_timeout``). The foreign key is added ``NOT VALID`` - its SHARE ROW
EXCLUSIVE locks on both tables are held for the catalog change only, under the
lock timeout, and leave the reads of ``users`` through - then validated under
SHARE UPDATE EXCLUSIVE, which lets reads and writes through. No index: like
``group_members.added_by``, the column is only read on the document's own row.

The column mirrors ``KnowledgeDocument.added_by_user_id``, and the constraint
takes the name PostgreSQL gives it on a fresh install's ``create_all``.

Revision ID: k5361kbdoc01
Revises: h3637apprsrv01
Create Date: 2026-10-24 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "k5361kbdoc01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "h3637apprsrv01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "knowledge_documents"
_COLUMN = "added_by_user_id"
_FK_NAME = "knowledge_documents_added_by_user_id_fkey"


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


def _has_constraint(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE c.conname = :n AND t.relname = :t"
            ),
            {"n": _FK_NAME, "t": _TABLE},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} UUID")
    if not _has_constraint(conn):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(
            f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_FK_NAME} "
            f"FOREIGN KEY ({_COLUMN}) REFERENCES users (id) ON DELETE SET NULL NOT VALID"
        )
    # Every row is NULL: the scan finds nothing, under a lock that lets
    # reads and writes through.
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {_FK_NAME}")


def downgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_FK_NAME}")
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
