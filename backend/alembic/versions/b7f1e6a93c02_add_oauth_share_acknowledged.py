"""add integrations.oauth_share_acknowledged

Adds ``integrations.oauth_share_acknowledged`` (default ``False``): whether
the owner of a group-shared/company-managed OAuth integration explicitly
chose to keep their personal access/refresh token in place instead of having
it stripped (see ``keep_oauth_credentials_on_share`` in
``app.api.v1.schemas.integration`` and the guard in
``app.api.v1.integrations._apply_oauth_share_guard``). It is the read-side
check ``shadow_invisible_bindings`` (``app.services.integration_tools``) uses
to decide whether a non-owner may use such an integration's binding.

Constant-default ``ADD COLUMN`` (metadata-only on PG 11+), following the
canonical lock-safe recipe: idempotency probe -> terminate_lock_holders ->
set_lock_timeout -> guarded DDL.

Revision ID: b7f1e6a93c02
Revises: c2f7sk1ll2628
Create Date: 2026-07-28 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "b7f1e6a93c02"
down_revision: str | None = "c2f7sk1ll2628"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "integrations"
_COLUMN = "oauth_share_acknowledged"


def _column_exists(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": _TABLE, "column": _COLUMN},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # Fresh installs run create_all + stamp head; this body never executes.
    if not inspector.has_table(_TABLE):
        return
    if _column_exists(conn):
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(
        f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} "
        "BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return
    if not _column_exists(conn):
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
