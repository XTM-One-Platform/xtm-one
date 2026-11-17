"""add sandbox_workspaces and sandbox_workspace_files

The code interpreter's durable workspace: one row per conversation (or
assignment run) and one row per path it holds, the bytes in the object store.
Keyed on the conversation / run with no foreign key to them.

Lock profile: two new tables; the only foreign key goes from the second to the
first, created in the same body, so no existing table is locked. The belt is
set anyway, as for every index build. Dropping them (downgrade) takes ACCESS
EXCLUSIVE on the tables themselves and follows the canonical recipe.

The column sets, constraint and index names mirror ``SandboxWorkspace`` /
``SandboxWorkspaceFile`` in ``app/models/sandbox_workspace.py``, so a fresh
install's ``create_all`` and this body agree.

Revision ID: s6260wksp01
Revises: g6258gainsights
Create Date: 2026-10-21 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "s6260wksp01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "g6258gainsights"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WORKSPACES = "sandbox_workspaces"
_FILES = "sandbox_workspace_files"


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, _WORKSPACES) and _has_table(conn, _FILES):
        return
    set_lock_timeout(conn)
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_WORKSPACES} (
            id UUID PRIMARY KEY,
            user_id UUID,
            conversation_id UUID,
            assignment_run_id UUID,
            size_bytes BIGINT NOT NULL DEFAULT 0,
            file_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL,
            last_used_at TIMESTAMPTZ NOT NULL,
            last_gc_at TIMESTAMPTZ,
            kernel_restarts INTEGER NOT NULL DEFAULT 0,
            bytes_read BIGINT NOT NULL DEFAULT 0,
            bytes_written BIGINT NOT NULL DEFAULT 0,
            kernel_state JSONB,
            CONSTRAINT ck_sandbox_workspaces_one_scope
                CHECK ((conversation_id IS NULL) <> (assignment_run_id IS NULL))
        )
        """
    )
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS uq_sandbox_workspaces_conversation ON {_WORKSPACES} (conversation_id)"
    )
    op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_sandbox_workspaces_run ON {_WORKSPACES} (assignment_run_id)")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_sandbox_workspaces_last_used ON {_WORKSPACES} (last_used_at)")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_sandbox_workspaces_user ON {_WORKSPACES} (user_id)")
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_FILES} (
            workspace_id UUID NOT NULL REFERENCES {_WORKSPACES} (id) ON DELETE CASCADE,
            path TEXT NOT NULL,
            size BIGINT NOT NULL,
            sha256 VARCHAR(64) NOT NULL,
            mtime DOUBLE PRECISION NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (workspace_id, path)
        )
        """
    )
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_sandbox_workspace_files_sha ON {_FILES} (workspace_id, sha256)")


def downgrade() -> None:
    conn = op.get_bind()
    for table in (_FILES, _WORKSPACES):
        if not _has_table(conn, table):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"DROP TABLE IF EXISTS {table}")
