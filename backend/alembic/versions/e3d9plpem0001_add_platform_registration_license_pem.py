"""add enterprise_license_pem_enc to platform_registrations

Adds a single Fernet-encrypted column to ``platform_registrations``:

  - ``enterprise_license_pem_enc`` (TEXT, nullable) — the connected platform's
    (OpenCTI / OpenAEV / OpenCRQ) enterprise license PEM, encrypted at rest.
    Persisted only when the PEM validated at registration; used by the Filigran
    AI extraction path to entitle "import doc AI" on the XTM One Starter Pack
    tier (a connected EE platform, no XTM One EE license of its own).

The column is a nullable, no-default ``ADD COLUMN`` (metadata-only on PG 11+)
and follows the canonical lock-safe recipe: idempotency probe ->
terminate_lock_holders -> set_lock_timeout -> guarded ``ADD COLUMN IF NOT
EXISTS`` DDL.

Revision ID: e3d9plpem0001
Revises: b7f1e6a93c02
Create Date: 2026-07-29 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "e3d9plpem0001"
down_revision: str | None = "b7f1e6a93c02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_registrations"
_COLUMN = "enterprise_license_pem_enc"


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
    # 4. Guarded, nullable no-default (metadata-only) DDL.
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} TEXT")


def downgrade() -> None:
    conn = op.get_bind()

    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
