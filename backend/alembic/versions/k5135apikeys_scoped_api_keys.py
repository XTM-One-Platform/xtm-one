"""scoped API keys: surfaces, read-only, agents, expiry, per-key limits

Adds the scope, expiry and limit columns of a personal API key. Every column is
nullable or carries a constant default, so a key that exists at upgrade reads
exactly as it did: every surface, read-write, every agent, no expiry, no limit
of its own. Nothing is backfilled.

Lock profile: nullable columns and a ``NOT NULL`` column with a CONSTANT default
are metadata-only on PG 11+ (no table rewrite), but the ALTER still takes ACCESS
EXCLUSIVE on ``user_api_keys``, which every API key request reads, so an open
transaction that merely read it would queue the ALTER behind itself in a rolling
deploy. One ALTER for every column (one lock), behind the canonical pre-flight:
idempotency probe -> terminate_lock_holders -> set_lock_timeout -> guarded DDL.

Revision ID: k5135apikeys
Revises: o1ptin0173
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "k5135apikeys"
down_revision: str | None = "o1ptin0173"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "user_api_keys"

#: Column -> its DDL type. Constant defaults only: no rewrite, no backfill.
_COLUMNS: dict[str, str] = {
    "surfaces": "JSON",
    "read_only": "BOOLEAN NOT NULL DEFAULT false",
    "agent_ids": "JSON",
    "expires_at": "TIMESTAMP WITH TIME ZONE",
    "rate_limit_per_minute": "INTEGER",
    "quota_limit": "INTEGER",
    "quota_period": "VARCHAR(16)",
    "expiry_notified_days": "INTEGER",
    "revoked_at": "TIMESTAMP WITH TIME ZONE",
}


def _existing_columns(conn) -> set[str]:
    # Scoped to the active schema: information_schema.columns spans every
    # schema, and a same-named table elsewhere must not skip the ADD COLUMN.
    rows = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :table"
        ),
        {"table": _TABLE},
    )
    return {row[0] for row in rows}


def upgrade() -> None:
    conn = op.get_bind()
    missing = [name for name in _COLUMNS if name not in _existing_columns(conn)]
    if not missing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    clauses = ", ".join(f"ADD COLUMN IF NOT EXISTS {name} {_COLUMNS[name]}" for name in missing)
    op.execute(f"ALTER TABLE {_TABLE} {clauses}")


def downgrade() -> None:
    conn = op.get_bind()
    present = [name for name in _COLUMNS if name in _existing_columns(conn)]
    if not present:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    clauses = ", ".join(f"DROP COLUMN IF EXISTS {name}" for name in present)
    op.execute(f"ALTER TABLE {_TABLE} {clauses}")
