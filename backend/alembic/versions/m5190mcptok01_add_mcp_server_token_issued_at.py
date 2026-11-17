"""add mcp_servers.token_issued_at

The refresh sweep renewed an MCP server's OAuth token once it expired within 15
minutes. A 10-minute token is inside that window as soon as it is issued, so the
server was refreshed on every 60 s tick (and, with a rotating refresh token,
rotated on every tick). ``token_issued_at`` records when the current access
token was received; with ``token_expires_at`` it gives the token's lifetime, and
the sweep refreshes it once half of that lifetime has passed
(``token_refresh.mcp_refresh_due_at``).

Nothing is backfilled: a NULL ``token_issued_at`` is refreshed 15 minutes ahead
of its expiry, as before, and the next refresh or authorization stamps it.

Idempotent: guarded by a column-existence probe, so re-running converges.
Follows the canonical lock-safe recipe: probe -> terminate_lock_holders ->
set_lock_timeout -> guarded DDL. The column is nullable with no default, so the
``ALTER`` is a catalogue update and rewrites no rows.

Revision ID: m5190mcptok01
Revises: g5099galink01
Create Date: 2026-10-24 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "m5190mcptok01"
down_revision: str | Sequence[str] | None = "g5099galink01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "mcp_servers"
_COLUMN = "token_issued_at"


def _column_exists(conn) -> bool:
    return (
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": _TABLE, "column": _COLUMN},
        ).first()
        is not None
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE) or _column_exists(conn):
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} TIMESTAMP WITH TIME ZONE")


def downgrade() -> None:
    """Drop the column: every stored token is then refreshed 15 minutes ahead of its expiry again."""
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE) or not _column_exists(conn):
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
