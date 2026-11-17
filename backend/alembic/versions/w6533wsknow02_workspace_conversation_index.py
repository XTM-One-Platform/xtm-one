"""workspace conversation index

* ``workspace_conversation_indexes`` - where each filed conversation stands in
  its workspace's conversation search: the workspace it was indexed into, its
  document, the last message sequence covered, a status.
* ``platform_settings.workspace_conversation_idle_minutes`` - how long a filed
  conversation stays idle before it is indexed.

Nothing is backfilled: the indexing tick picks idle conversations itself.

Lock profile: the new table carries foreign keys to ``conversations``,
``workspaces`` and ``knowledge_documents``, which take a SHARE ROW EXCLUSIVE
lock on each of them, so ``CREATE TABLE`` runs under ``set_lock_timeout``. The
column follows the canonical recipe (probe, ``terminate_lock_holders``,
``set_lock_timeout``). The downgrade drops both the same way.

The column set mirrors ``WorkspaceConversationIndex`` in
``app/models/workspace.py`` and the column ``PlatformSettings`` declares, so a
fresh install's ``create_all`` and this body agree.

Revision ID: w6533wsknow02
Revises: w6533wsknow01
Create Date: 2026-10-25 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "w6533wsknow02"
down_revision: str | Sequence[str] | None = "w6533wsknow01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "workspace_conversation_indexes"
_COLUMN = "workspace_conversation_idle_minutes"


def _has_table(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": _TABLE},
        ).scalar()
    )


def _has_column(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'platform_settings' "
                "AND column_name = :c"
            ),
            {"c": _COLUMN},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn):
        set_lock_timeout(conn)
        op.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_TABLE} (
                conversation_id UUID PRIMARY KEY
                    REFERENCES conversations (id) ON DELETE CASCADE,
                workspace_id UUID NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
                knowledge_document_id UUID
                    REFERENCES knowledge_documents (id) ON DELETE SET NULL,
                indexed_through_sequence INTEGER NOT NULL DEFAULT 0,
                status VARCHAR(20) NOT NULL,
                error TEXT,
                indexed_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        op.execute(
            f"CREATE INDEX IF NOT EXISTS ix_workspace_conversation_indexes_workspace "
            f"ON {_TABLE} (workspace_id)"
        )
    if not _has_column(conn):
        terminate_lock_holders(conn, "platform_settings")
        set_lock_timeout(conn)
        op.execute(
            f"ALTER TABLE platform_settings ADD COLUMN IF NOT EXISTS {_COLUMN} "
            "INTEGER NOT NULL DEFAULT 30"
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn):
        terminate_lock_holders(conn, "platform_settings")
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE platform_settings DROP COLUMN IF EXISTS {_COLUMN}")
    if _has_table(conn):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
