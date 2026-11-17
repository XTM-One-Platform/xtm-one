"""add knowledge_bases.describe_figures and knowledge_documents.figure_reading

Adds the per-knowledge-base opt-in that describes the figures of an uploaded
PDF for its index (``knowledge_bases.describe_figures``, ``false`` by default),
and the per-document record of that figure pass
(``knowledge_documents.figure_reading``, nullable JSON). Every existing
knowledge base reads ``false`` and every existing document ``NULL``, so nothing
already stored is embedded differently.

Both are metadata-only ``ADD COLUMN``s on PG 11+ (a constant default, a
nullable column without default), each following the canonical lock-safe
recipe: idempotency probe -> terminate_lock_holders -> set_lock_timeout ->
guarded DDL.

Revision ID: c8488kbfigr01
Revises: f8528frict02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "c8488kbfigr01"
down_revision: str | None = "f8528frict02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (table, column, column definition)
_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("knowledge_bases", "describe_figures", "BOOLEAN NOT NULL DEFAULT false"),
    ("knowledge_documents", "figure_reading", "JSON"),
)


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    for table, column, definition in _COLUMNS:
        # Fresh installs run create_all + stamp head; this body never executes.
        if not inspector.has_table(table):
            continue
        # 1. Idempotency probe - steady-state re-runs take NO lock.
        if _column_exists(conn, table, column):
            continue
        # 2. Pre-flight: kill same-role lock holders on the table the DDL locks.
        terminate_lock_holders(conn, table)
        # 3. Safety belt: fail fast (55P03) instead of wedging the lock queue.
        set_lock_timeout(conn)
        # 4. Guarded DDL - metadata-only, no rewrite.
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    for table, column, _definition in reversed(_COLUMNS):
        if not inspector.has_table(table):
            continue
        if not _column_exists(conn, table, column):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
