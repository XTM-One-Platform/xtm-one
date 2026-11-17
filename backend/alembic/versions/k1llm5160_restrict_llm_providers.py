"""restrict an LLM provider to groups and users

``llm_provider_configs.access_restricted`` (FALSE: usable by everyone, as
before), ``shared_group_ids`` and ``shared_user_ids`` (who may use a restricted
provider). Every existing provider stays unrestricted, so an upgrade changes
nothing until an admin restricts one.

A constant-default ``ADD COLUMN`` and nullable JSON columns are metadata-only
on PG 11+; the ALTER still takes an ACCESS EXCLUSIVE lock, hence the recipe:
idempotency probe -> terminate_lock_holders -> set_lock_timeout -> guarded DDL.

Revision ID: k1llm5160
Revises: s3lffanout01
Create Date: 2026-10-02 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "k1llm5160"
down_revision: str | None = "s3lffanout01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "llm_provider_configs"
_COLUMNS = {
    "access_restricted": "BOOLEAN NOT NULL DEFAULT FALSE",
    "shared_group_ids": "JSON",
    "shared_user_ids": "JSON",
}


def _missing(conn) -> list[str]:
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return []
    present = {c["name"] for c in inspector.get_columns(_TABLE)}
    return [name for name in _COLUMNS if name not in present]


def upgrade() -> None:
    conn = op.get_bind()
    missing = _missing(conn)
    if not missing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(
        f"ALTER TABLE {_TABLE} " + ", ".join(f"ADD COLUMN IF NOT EXISTS {name} {_COLUMNS[name]}" for name in missing)
    )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return
    present = {c["name"] for c in inspector.get_columns(_TABLE)}
    drop = [name for name in _COLUMNS if name in present]
    if not drop:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} " + ", ".join(f"DROP COLUMN IF EXISTS {name}" for name in drop))
