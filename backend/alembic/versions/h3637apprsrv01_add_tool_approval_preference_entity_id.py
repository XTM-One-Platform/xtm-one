"""add tool_approval_preferences.tool_entity_id

A standing approval ("Yes, always") is keyed by ``(user_id, tool_key)``, and the
gate matched an MCP tool by name alone: an MCP server advertising the name and
schema of a tool the user approved ran on that approval. ``mcp:<server name>``
cannot tell them apart, because the server's owner chooses the name.

This column records ``ToolDefinition.entity_id`` of the approved tool — for an
MCP tool, its server's id — and the gate now covers an MCP server's tool only by
an approval granted on its own server.

Nullable and not backfilled: which server an existing approval was given on was
never recorded, and guessing it from the server name is the very match this
replaces. An MCP server's tool covered by an existing approval asks once more,
and the "Yes, always" given then records the server. Approvals of built-in,
integration and custom tools, and of the product's own ``read_mcp_resource``,
keep covering them.

Lock profile: a nullable column with no default is metadata-only, but
``ALTER TABLE`` still takes a brief ACCESS EXCLUSIVE lock on
``tool_approval_preferences``, so it follows the canonical recipe (probe,
``terminate_lock_holders``, ``set_lock_timeout``). The downgrade drops the
column the same way.

Revision ID: h3637apprsrv01
Revises: k5041wsdfcm01
Create Date: 2026-10-24 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "h3637apprsrv01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "k5041wsdfcm01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "tool_approval_preferences"
_COLUMN = "tool_entity_id"


def _has_table(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :t"
            ),
            {"t": _TABLE},
        ).scalar()
    )


def _has_column(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :t AND column_name = :c"
            ),
            {"t": _TABLE, "c": _COLUMN},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn) or _has_column(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(
        f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} VARCHAR(64)"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn) or not _has_column(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
