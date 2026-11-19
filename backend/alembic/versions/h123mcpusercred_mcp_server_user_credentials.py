"""each person's own OAuth account on a shared MCP server

Adds ``mcp_servers.credential_mode`` (``shared`` | ``per_user``) and the
``mcp_server_user_credentials`` table, one row per (server, user): the OAuth
account a person authorized on a ``per_user`` MCP server (issue #443,
``dev-docs/mcp-server-user-credentials.md``). Every server starts ``shared``:
nothing changes on upgrade.

**Lock safety.** ``ADD COLUMN ... NOT NULL DEFAULT 'shared'`` is metadata-only
on PostgreSQL 11+, but it takes a brief ``ACCESS EXCLUSIVE`` lock on
``mcp_servers``: canonical recipe (idempotency probe, ``terminate_lock_holders``,
``set_lock_timeout``, guarded DDL). The new table references ``mcp_servers``
and ``users``, whose foreign keys lock the referenced tables, so its creation
runs under the same belt; its indexes are plain builds on an empty table.
Fresh installs run ``create_all`` + ``stamp head``: the body never executes there.

Revision ID: h123mcpusercred
Revises: h122healthengine
Create Date: 2026-11-19 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "h123mcpusercred"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "h122healthengine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "mcp_server_user_credentials"
_SERVERS = "mcp_servers"
_COLUMN = "credential_mode"


def _has_column(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :table AND column_name = :column"
            ),
            {"table": _SERVERS, "column": _COLUMN},
        ).first()
    )


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    needs_column = not _has_column(conn)
    needs_table = not _has_table(conn, _TABLE)
    if not needs_column and not needs_table:
        return

    terminate_lock_holders(conn, _SERVERS)
    set_lock_timeout(conn)

    if needs_column:
        op.execute(
            f"ALTER TABLE {_SERVERS} ADD COLUMN IF NOT EXISTS {_COLUMN} VARCHAR(16) NOT NULL DEFAULT 'shared'"
        )

    if needs_table:
        op.create_table(
            _TABLE,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "server_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("mcp_servers.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("access_token_enc", sa.Text(), nullable=True),
            sa.Column("refresh_token_enc", sa.Text(), nullable=True),
            sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("token_issued_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("granted_scopes", sa.Text(), nullable=True),
            sa.Column("oauth_status", sa.String(20), nullable=False, server_default="connected"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("server_id", "user_id", name="uq_mcp_server_user_credentials_server_user"),
        )
        op.create_index("ix_mcp_server_user_credentials_user_id", _TABLE, ["user_id"])
        op.create_index(
            "ix_mcp_server_user_credentials_status_token_expires",
            _TABLE,
            ["oauth_status", "token_expires_at"],
            postgresql_where=sa.text("refresh_token_enc IS NOT NULL"),
        )


def downgrade() -> None:
    conn = op.get_bind()
    has_table = _has_table(conn, _TABLE)
    has_column = _has_column(conn)
    if not has_table and not has_column:
        return
    terminate_lock_holders(conn, _SERVERS)
    set_lock_timeout(conn)
    if has_table:
        op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
    if has_column:
        op.execute(f"ALTER TABLE {_SERVERS} DROP COLUMN IF EXISTS {_COLUMN}")
