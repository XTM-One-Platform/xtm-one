"""add llm_provider_configs.env_models (env baseline snapshot)

``env_models`` stores the ``LLM_PROVIDER_{PROVIDER}_MODELS`` list applied at
the last boot for an env-seeded provider. With
``LLM_PROVIDER_MODELS_EDITABLE=true`` the seed diffs the current env value
against this snapshot and delta-merges the result into the admin-curated
``available_models`` (added env models appear, removed ones disappear, every
other admin edit is preserved). Nullable, no default: NULL means "no
baseline applied yet", which the merge treats as an empty previous list.

Nullable ``ADD COLUMN`` with no default is metadata-only on PG 11+; the
canonical lock-safe recipe still applies because the ALTER takes an
ACCESS EXCLUSIVE lock: idempotency probe -> terminate_lock_holders ->
set_lock_timeout -> guarded DDL.

Revision ID: e9m0dl3349
Revises: a2a3outbound1
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "e9m0dl3349"
down_revision: str | None = "a2a3outbound1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "llm_provider_configs"
_COLUMN = "env_models"


def _column_exists(conn, table: str, column: str) -> bool:
    # Scope the probe to the active schema — information_schema.columns spans
    # ALL schemas, so a same-named table elsewhere must not make the probe
    # skip the ADD COLUMN in the schema being migrated.
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
    # 4. Guarded, nullable (metadata-only) DDL.
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} JSON")


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
