"""add integrations.uses_admin_oauth_credentials

Whether an integration's OAuth app credentials were copied from the admin's
OAuth defaults when it was created. The copy leaves nothing behind, so the
integration page read a PROVIDER-WIDE flag instead and told an integration
running on the user's own OAuth app that its webhooks are managed by the
administrator. Nullable, no default: every existing row reads as "not
recorded" and keeps the provider-wide behaviour it has today; new rows get a
value on insert (the ORM default) and when their client id is replaced.

Lock profile: a nullable ``ADD COLUMN`` with no default is metadata-only on
PG 11+, but the ALTER still takes ACCESS EXCLUSIVE on ``integrations`` - read
by every agent turn that loads its tools and by the token-refresh sweep, so
any open transaction that has merely read it queues the ALTER behind itself
in a rolling deploy. Canonical pre-flight: idempotency probe ->
terminate_lock_holders -> set_lock_timeout -> guarded DDL.

Revision ID: i4234credsrc1
Revises: m4630heads01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "i4234credsrc1"
down_revision: str | None = "m4630heads01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "integrations"
_COLUMN = "uses_admin_oauth_credentials"


def _column_exists(conn, table: str, column: str) -> bool:
    # Scoped to the active schema - information_schema.columns spans ALL
    # schemas, so a same-named table elsewhere must not make the probe skip
    # the ADD COLUMN in the schema being migrated.
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
    # 1. Idempotency probe - steady-state re-runs take NO lock.
    if _column_exists(conn, _TABLE, _COLUMN):
        return
    # 2. Pre-flight: kill same-role lock holders on the table the DDL locks.
    terminate_lock_holders(conn, _TABLE)
    # 3. Safety belt: fail fast (55P03) instead of wedging the lock queue.
    set_lock_timeout(conn)
    # 4. Guarded DDL - nullable, no default: metadata-only, no rewrite.
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} BOOLEAN")


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
