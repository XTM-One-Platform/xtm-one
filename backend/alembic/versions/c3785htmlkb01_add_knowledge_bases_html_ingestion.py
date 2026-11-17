"""add knowledge_bases.html_ingestion

Adds ``knowledge_bases.html_ingestion`` (``'source'`` | ``'rendered'``, default
``'source'``): the per-knowledge-base opt-in that embeds an HTML document as its
RENDERED text (the page text in reading order, its embedded data summarised
first) instead of its markup. The stored source is unchanged either way; only
what is chunked and embedded differs, and only for documents processed after
the switch. Every existing knowledge base reads ``'source'``, so nothing it
holds is re-embedded or embedded differently.

Constant-default ``ADD COLUMN`` (metadata-only on PG 11+), following the
canonical lock-safe recipe: idempotency probe -> terminate_lock_holders ->
set_lock_timeout -> guarded DDL.

Revision ID: c3785htmlkb01
Revises: f4566flwarn01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "c3785htmlkb01"
down_revision: str | None = "f4566flwarn01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "knowledge_bases"
_COLUMN = "html_ingestion"


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
    # Fresh installs run create_all + stamp head; this body never executes.
    if not sa.inspect(conn).has_table(_TABLE):
        return
    # 1. Idempotency probe - steady-state re-runs take NO lock.
    if _column_exists(conn):
        return
    # 2. Pre-flight: kill same-role lock holders on the table the DDL locks.
    terminate_lock_holders(conn, _TABLE)
    # 3. Safety belt: fail fast (55P03) instead of wedging the lock queue.
    set_lock_timeout(conn)
    # 4. Guarded DDL - constant default: metadata-only, no rewrite.
    op.execute(
        f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} "
        "VARCHAR(20) NOT NULL DEFAULT 'source'"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE):
        return
    if not _column_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
