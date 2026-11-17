"""add api_url to platform_registrations

Adds one column to ``platform_registrations``:

  - ``api_url`` (TEXT, nullable) - the URL XTM One reaches a registered
    platform on when it is not the platform's identity (``platform_url``): the
    ``{PROVIDER}_API_URL`` override of XTM One's own environment, captured at
    registration. The platform's signing keys are fetched there, and the
    tokens sent there are still addressed to ``platform_url``. ``NULL`` until
    the platform's next registration, which is also its heartbeat.

The column is a nullable, no-default ``ADD COLUMN`` (metadata-only on PG 11+)
and follows the canonical lock-safe recipe: idempotency probe ->
terminate_lock_holders -> set_lock_timeout -> guarded ``ADD COLUMN IF NOT
EXISTS`` DDL.

Revision ID: p4880apiurl01
Revises: m4728heads01
Create Date: 2026-09-29 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "p4880apiurl01"
down_revision: str | None = "m4728heads01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_registrations"
_COLUMN = "api_url"


def _column_exists(conn, table: str, column: str) -> bool:
    # Scoped to the active schema: information_schema.columns spans every
    # schema, and a same-named table elsewhere must not skip the ADD COLUMN.
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

    if _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} TEXT")


def downgrade() -> None:
    conn = op.get_bind()

    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
