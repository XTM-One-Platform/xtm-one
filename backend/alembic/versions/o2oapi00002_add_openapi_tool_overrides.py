"""add tool_overrides to integration_openapi_specs

Adds ``integration_openapi_specs.tool_overrides``: the tool names and
descriptions a user set on the operations of an OpenAPI integration
(``operation key → {"name", "description"}``). Nullable with no default, so
``ADD COLUMN`` only updates the catalog; old replicas ignore a column they do
not map, and nothing is backfilled.

Lock safety: ``ADD COLUMN`` takes ACCESS EXCLUSIVE on the table for that
catalog update, so it runs under the canonical recipe (idempotency probe +
early return, then ``terminate_lock_holders`` + ``set_lock_timeout``, then
guarded DDL).

The column name matches what ``create_all`` emits on a fresh install (where
this body never runs), so both install paths converge.

Revision ID: o2oapi00002
Revises: o2oapi00001
Create Date: 2026-09-23 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "o2oapi00002"
down_revision: str | Sequence[str] | None = "o2oapi00001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "integration_openapi_specs"
_COLUMN = "tool_overrides"


def _column_exists(conn, table: str, column: str) -> bool:
    # Scoped to the active schema — information_schema spans all of them.
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
    if not _table_exists(conn, _TABLE) or _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(
        "ALTER TABLE integration_openapi_specs ADD COLUMN IF NOT EXISTS tool_overrides JSON"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute("ALTER TABLE integration_openapi_specs DROP COLUMN IF EXISTS tool_overrides")
