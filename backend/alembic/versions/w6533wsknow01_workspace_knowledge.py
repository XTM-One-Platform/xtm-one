"""workspace knowledge: instructions, owned knowledge bases, linked skills

* ``workspaces.instructions`` - text every agent turn in a filed conversation receives.
* ``knowledge_bases.owner_workspace_id`` / ``workspace_role`` - the Sources and
  Conversations knowledge bases a workspace owns, one of each
  (``uq_knowledge_bases_workspace_role``), the two columns set together
  (``ck_knowledge_bases_workspace_role``). The FK cascades: every child of a
  knowledge base cascades too, so a deleted workspace leaves nothing behind.
* ``workspace_skills`` - library skills linked to a workspace.
* ``platform_settings.workspace_sources_context_percent`` - the share of the
  context window sources may fill before they are searched instead.

Nothing is backfilled: every new column is NULL or has a default.

Lock profile: each ALTER needs a brief ACCESS EXCLUSIVE lock and follows the
canonical recipe (probe, ``terminate_lock_holders``, ``set_lock_timeout``). The
new FK on ``knowledge_bases`` validates instantly (every value is NULL) and
takes SHARE ROW EXCLUSIVE on ``workspaces``. The partial unique index is built
with ``create_index_in_place`` (``knowledge_bases`` holds one row per
collection, not per document, so the build is short).
The downgrade drops everything the same way.

Column, index and FK names mirror ``app/models/knowledge_base.py``,
``app/models/workspace.py`` and ``app/models/platform_settings.py``, so a fresh
install's ``create_all`` and this body agree.

Revision ID: w6533wsknow01
Revises: v6435tcap01
Create Date: 2026-10-24 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import (
    create_index_in_place,
    set_lock_timeout,
    terminate_lock_holders,
)

revision: str = "w6533wsknow01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "v6435tcap01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": table},
        ).scalar()
    )


def _has_column(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def _has_constraint(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": name}
        ).scalar()
    )


def _has_index(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND indexname = :n"
            ),
            {"n": name},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()

    if not _has_column(conn, "workspaces", "instructions"):
        terminate_lock_holders(conn, "workspaces")
        set_lock_timeout(conn)
        op.execute(
            "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS instructions TEXT NOT NULL DEFAULT ''"
        )

    if not _has_column(conn, "knowledge_bases", "owner_workspace_id"):
        terminate_lock_holders(conn, "knowledge_bases")
        set_lock_timeout(conn)
        op.execute(
            "ALTER TABLE knowledge_bases ADD COLUMN IF NOT EXISTS owner_workspace_id UUID "
            "REFERENCES workspaces (id) ON DELETE CASCADE"
        )
    if not _has_column(conn, "knowledge_bases", "workspace_role"):
        terminate_lock_holders(conn, "knowledge_bases")
        set_lock_timeout(conn)
        op.execute(
            "ALTER TABLE knowledge_bases ADD COLUMN IF NOT EXISTS workspace_role VARCHAR(20)"
        )
    if not _has_constraint(conn, "ck_knowledge_bases_workspace_role"):
        terminate_lock_holders(conn, "knowledge_bases")
        set_lock_timeout(conn)
        op.execute(
            "ALTER TABLE knowledge_bases ADD CONSTRAINT ck_knowledge_bases_workspace_role "
            "CHECK ((owner_workspace_id IS NULL) = (workspace_role IS NULL))"
        )

    if not _has_table(conn, "workspace_skills"):
        set_lock_timeout(conn)
        op.execute(
            """
            CREATE TABLE IF NOT EXISTS workspace_skills (
                id UUID PRIMARY KEY,
                workspace_id UUID NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
                skill_id UUID NOT NULL REFERENCES agent_skills (id) ON DELETE CASCADE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_skills_pair "
            "ON workspace_skills (workspace_id, skill_id)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_workspace_skills_skill ON workspace_skills (skill_id)"
        )

    if not _has_column(conn, "platform_settings", "workspace_sources_context_percent"):
        terminate_lock_holders(conn, "platform_settings")
        set_lock_timeout(conn)
        op.execute(
            "ALTER TABLE platform_settings ADD COLUMN IF NOT EXISTS "
            "workspace_sources_context_percent INTEGER NOT NULL DEFAULT 15"
        )

    # Last: the build commits on its own (autocommit block), releasing its
    # SHARE lock at once instead of holding it to the end of the upgrade.
    if not _has_index(conn, "uq_knowledge_bases_workspace_role"):
        with op.get_context().autocommit_block():
            create_index_in_place(
                conn,
                "uq_knowledge_bases_workspace_role",
                "knowledge_bases",
                "(owner_workspace_id, workspace_role) WHERE owner_workspace_id IS NOT NULL",
                unique=True,
                required=True,
            )


def downgrade() -> None:
    conn = op.get_bind()

    if _has_column(conn, "platform_settings", "workspace_sources_context_percent"):
        terminate_lock_holders(conn, "platform_settings")
        set_lock_timeout(conn)
        op.execute(
            "ALTER TABLE platform_settings DROP COLUMN IF EXISTS workspace_sources_context_percent"
        )
    if _has_table(conn, "workspace_skills"):
        terminate_lock_holders(conn, "workspace_skills")
        set_lock_timeout(conn)
        op.execute("DROP TABLE IF EXISTS workspace_skills")
    if _has_column(conn, "knowledge_bases", "owner_workspace_id"):
        # The workspace-owned rows exist only through this revision: dropping the
        # column would leave them as ordinary, ownerless-looking collections.
        terminate_lock_holders(conn, "knowledge_bases")
        set_lock_timeout(conn)
        op.execute("DELETE FROM knowledge_bases WHERE owner_workspace_id IS NOT NULL")
        op.execute("DROP INDEX IF EXISTS uq_knowledge_bases_workspace_role")
        op.execute(
            "ALTER TABLE knowledge_bases DROP CONSTRAINT IF EXISTS ck_knowledge_bases_workspace_role"
        )
        op.execute("ALTER TABLE knowledge_bases DROP COLUMN IF EXISTS workspace_role")
        op.execute("ALTER TABLE knowledge_bases DROP COLUMN IF EXISTS owner_workspace_id")
    if _has_column(conn, "workspaces", "instructions"):
        terminate_lock_holders(conn, "workspaces")
        set_lock_timeout(conn)
        op.execute("ALTER TABLE workspaces DROP COLUMN IF EXISTS instructions")
