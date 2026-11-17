"""add provider-config reference columns for routing, fallback and the GA

A model id is not unique across provider configs, so resolving one to its owner
newest-config-first made credentials, billing and governance depend on config
creation order. Four surfaces already stored the owning config's id; these are
the three that did not:

* ``general_assistant_config.llm_provider_id`` — the GA's own chat model
* ``platform_settings.routing_provider_id`` — the routing evaluator's model
* ``platform_settings.fallback_provider_id`` — the explicit failover model

All nullable, no backfill: NULL means "no reference stored", which keeps the
historical model-owner lookup for choices saved before this migration.

Nullable ``ADD COLUMN`` with no default is metadata-only on PG 11+; the
canonical lock-safe recipe still applies because the ALTER takes an
ACCESS EXCLUSIVE lock: idempotency probe -> terminate_lock_holders ->
set_lock_timeout -> guarded DDL.

Revision ID: b3f7prov1d
Revises: k5p3358studio
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "b3f7prov1d"
down_revision: str | None = "k5p3358studio"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, column) pairs, grouped by table so each table is locked once.
_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("general_assistant_config", ("llm_provider_id",)),
    ("platform_settings", ("routing_provider_id", "fallback_provider_id")),
)


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
    for table, columns in _COLUMNS:
        missing = [c for c in columns if not _column_exists(conn, table, c)]
        # 1. Idempotency probe — steady-state re-runs take NO lock.
        if not missing:
            continue
        # 2. Pre-flight: kill same-role lock holders so the ALTER is instant.
        terminate_lock_holders(conn, table)
        # 3. Safety belt: fail fast (55P03) instead of wedging the lock queue.
        set_lock_timeout(conn)
        # 4. Guarded, nullable (metadata-only) DDL.
        for column in missing:
            op.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} UUID"
            )


def downgrade() -> None:
    conn = op.get_bind()
    for table, columns in _COLUMNS:
        present = [c for c in columns if _column_exists(conn, table, c)]
        if not present:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        for column in present:
            op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
