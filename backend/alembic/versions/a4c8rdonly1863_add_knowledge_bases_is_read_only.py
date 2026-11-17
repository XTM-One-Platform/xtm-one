"""add knowledge_bases.is_read_only

Adds ``knowledge_bases.is_read_only`` (default ``False``): the per-knowledge-base
content lock that marks a collection as ground truth. A read-only KB
stays fully searchable and readable; what is frozen is its *content* — agent
writes (``store_knowledge`` / ``update_knowledge_document``) and ad-hoc document
mutation (add / upload / crawl / update / delete) are refused. Configured import
sources and re-embedding are deliberately unaffected, so a KB whose authority is
an external system of record keeps refreshing.

The flag lives on the knowledge base rather than on the agent because per-agent
write grants (``agents.knowledge_base_permissions``) are granted from the agent
side — protection that can be granted away from the other end is no protection.

Constant-default ``ADD COLUMN`` (metadata-only on PG 11+), following the
canonical lock-safe recipe: idempotency probe -> terminate_lock_holders ->
set_lock_timeout -> guarded DDL.

Revision ID: a4c8rdonly1863
Revises: f7p1res2637
Create Date: 2026-07-30 23:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "a4c8rdonly1863"
down_revision: str | None = "f7p1res2637"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "knowledge_bases"
_COLUMN = "is_read_only"


def _column_exists(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": _TABLE, "column": _COLUMN},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # Fresh installs run create_all + stamp head; this body never executes.
    if not inspector.has_table(_TABLE):
        return
    if _column_exists(conn):
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(
        f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} "
        "BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return
    if not _column_exists(conn):
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
