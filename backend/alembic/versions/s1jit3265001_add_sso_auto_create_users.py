"""add sso_config on-the-fly user creation toggles (OIDC + SAML)

``oidc_auto_create_users`` / ``saml_auto_create_users`` — when disabled, an
SSO login only succeeds for an account that already exists (matched by SSO
subject or email); nothing is created on the fly. Both default to ``true``
so existing deployments keep the historical auto-provisioning behavior.

Constant-default ``ADD COLUMN`` (metadata-only on PG 11+), following the
canonical lock-safe recipe: idempotency probe -> terminate_lock_holders
-> set_lock_timeout -> guarded DDL.

Revision ID: s1jit3265001
Revises: a1paus3198
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "s1jit3265001"
down_revision: str | None = "a1paus3198"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "sso_config"
_COLUMNS = ("oidc_auto_create_users", "saml_auto_create_users")


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
    missing = [c for c in _COLUMNS if not _column_exists(conn, _TABLE, c)]
    if not missing:
        return
    # 2. Pre-flight: kill same-role lock holders so the ALTER is instant.
    terminate_lock_holders(conn, _TABLE)
    # 3. Safety belt: fail fast (55P03) instead of wedging the lock queue.
    set_lock_timeout(conn)
    # 4. Guarded, constant-default (metadata-only) DDL.
    for column in missing:
        op.execute(
            f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {column} "
            "BOOLEAN NOT NULL DEFAULT true"
        )


def downgrade() -> None:
    conn = op.get_bind()
    present = [c for c in _COLUMNS if _column_exists(conn, _TABLE, c)]
    if not present:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for column in present:
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {column}")
