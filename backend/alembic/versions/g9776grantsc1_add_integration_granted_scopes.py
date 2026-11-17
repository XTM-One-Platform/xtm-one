"""add granted_scopes to integrations and integration_user_credentials

The OAuth scopes a stored token was granted, from the token endpoint's
``scope`` field (#9776): a person can decline a scope at consent, and nothing
recorded it, so a reconnect that did not obtain a new scope looked like one
that did.

Nullable TEXT with no default on both tables: metadata-only ``ADD COLUMN``
(no table rewrite), canonical lock-safe recipe (idempotency probe ->
terminate_lock_holders -> set_lock_timeout -> guarded DDL). NULL reads as
"the grant is unknown", so no backfill is needed.

Revision ID: g9776grantsc1
Revises: f9769fltauton1
Create Date: 2026-11-07 16:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "g9776grantsc1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "f9769fltauton1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("integrations", "integration_user_credentials")
_COLUMN = "granted_scopes"
_TYPE = "TEXT"


def _has_column(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": _COLUMN},
        ).first()
    )


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    for table in _TABLES:
        # Fresh installs run create_all + stamp head; this body never executes.
        if not inspector.has_table(table) or _has_column(conn, table):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {_COLUMN} {_TYPE}")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    for table in _TABLES:
        if not inspector.has_table(table) or not _has_column(conn, table):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {_COLUMN}")
