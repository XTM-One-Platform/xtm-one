"""add agent tombstones and the deleted agent of a conversation

* ``agent_tombstones``: what a deleted agent leaves behind (its id, last name,
  icon, owner and successor), so its conversations and traces stay
  attributable once the agent row is gone. No foreign key, so the
  ``CREATE TABLE`` takes no lock on another table.
* ``conversations.deleted_agent_id``: the agent a conversation was held with
  when that agent was deleted. Nullable, no default, no foreign key and no
  index: metadata-only on the hottest table (PG 11+), behind the canonical
  recipe: idempotency probe -> terminate_lock_holders -> set_lock_timeout ->
  guarded DDL.

Revision ID: c6734cont0001
Revises: g7001gate01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c6734cont0001"
down_revision: str | None = "g7001gate01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": table},
        ).scalar()
    )


def _has_column(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :table "
                "AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, "agent_tombstones"):
        op.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_tombstones (
                agent_id UUID PRIMARY KEY,
                name VARCHAR(200) NOT NULL,
                slug VARCHAR(200),
                icon VARCHAR(50),
                user_id UUID,
                is_company_managed BOOLEAN NOT NULL DEFAULT false,
                successor_agent_id UUID,
                deleted_by UUID,
                deleted_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
            )
            """
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_agent_tombstones_user_id ON agent_tombstones (user_id)"
        )
    if not _has_column(conn, "conversations", "deleted_agent_id"):
        terminate_lock_holders(conn, "conversations")
        set_lock_timeout(conn)
        op.execute("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deleted_agent_id UUID")


def downgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn, "conversations", "deleted_agent_id"):
        terminate_lock_holders(conn, "conversations")
        set_lock_timeout(conn)
        op.execute("ALTER TABLE conversations DROP COLUMN IF EXISTS deleted_agent_id")
    if _has_table(conn, "agent_tombstones"):
        op.execute("DROP TABLE IF EXISTS agent_tombstones")
