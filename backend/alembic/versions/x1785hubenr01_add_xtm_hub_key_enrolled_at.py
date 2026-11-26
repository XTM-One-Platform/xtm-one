"""add xtm_hub_registrations.key_enrolled_at (#1785)

When the XTM Hub enrolled this instance's public key for the registration:
NULL until then. Once set, every call to the Hub is signed with the key
(``XTM-Hub-Platform-Assertion``) instead of carrying the platform token. A
nullable column without a default is metadata-only.

Revision ID: x1785hubenr01
Revises: x1784hubkey01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "x1785hubenr01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "x1784hubkey01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "xtm_hub_registrations"
_COLUMN = "key_enrolled_at"


def _column_exists(conn, table: str, column: str) -> bool:
    # Scoped to the active schema: information_schema.columns spans every schema.
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
    # 1. Idempotency probe — steady-state re-runs take NO lock.
    if _column_exists(conn, _TABLE, _COLUMN):
        return
    # 2. Pre-flight: kill same-role lock holders so the ALTER is instant.
    terminate_lock_holders(conn, _TABLE)
    # 3. Safety belt: fail fast (55P03) instead of wedging the lock queue.
    set_lock_timeout(conn)
    # 4. Guarded, nullable, no default: metadata-only.
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} TIMESTAMP WITH TIME ZONE")


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
