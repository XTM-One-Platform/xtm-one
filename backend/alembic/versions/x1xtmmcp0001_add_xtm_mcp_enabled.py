"""add platform_settings.xtm_mcp_enabled (XTM One's own MCP server toggle)

Global admin switch for ``/mcp/xtm`` — the MCP server that exposes XTM One's
own management surface (agents, prompts, skills, knowledge bases, traces) to
external MCP clients.

Defaults to ``false``: unlike the platform MCP servers, this one hands an
external client the instance's configuration surface, so an upgrade must not
open it on a deployment that never asked for it.

Constant-default ``ADD COLUMN`` (metadata-only on PG 11+), following the
canonical lock-safe recipe: idempotency probe -> terminate_lock_holders
-> set_lock_timeout -> guarded DDL.

Revision ID: x1xtmmcp0001
Revises: wiz3158mbd01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "x1xtmmcp0001"
down_revision: str | None = "wiz3158mbd01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_settings"
_COLUMN = "xtm_mcp_enabled"


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
    # 4. Guarded, constant-default (metadata-only) DDL.
    op.execute(
        f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} "
        "BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
