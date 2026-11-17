"""exclude groups and users from an LLM provider

``llm_provider_configs.excluded_group_ids`` and ``excluded_user_ids``: who may
never run on a provider, whatever its restriction grants. Both NULL on every
existing row, so an upgrade changes nothing until an admin excludes someone.

Nullable JSON columns are metadata-only on PG 11+; the ALTER still takes an
ACCESS EXCLUSIVE lock, hence the recipe: idempotency probe ->
terminate_lock_holders -> set_lock_timeout -> guarded DDL.

Revision ID: x4684llmexcl
Revises: k1hub0001
Create Date: 2026-10-03 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "x4684llmexcl"
down_revision: str | None = "k1hub0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "llm_provider_configs"
_COLUMNS = ("excluded_group_ids", "excluded_user_ids")


def _present(conn) -> set[str] | None:
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return None
    return {c["name"] for c in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    conn = op.get_bind()
    present = _present(conn)
    if present is None:
        return
    missing = [name for name in _COLUMNS if name not in present]
    if not missing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(
        f"ALTER TABLE {_TABLE} " + ", ".join(f"ADD COLUMN IF NOT EXISTS {name} JSON" for name in missing)
    )


def downgrade() -> None:
    conn = op.get_bind()
    present = _present(conn)
    if present is None:
        return
    drop = [name for name in _COLUMNS if name in present]
    if not drop:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} " + ", ".join(f"DROP COLUMN IF EXISTS {name}" for name in drop))
