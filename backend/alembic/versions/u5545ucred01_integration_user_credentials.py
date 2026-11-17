"""each person's own account on a shared integration

Adds ``integrations.credential_mode`` (``shared`` | ``per_user``) and the
``integration_user_credentials`` table, one row per (integration, user): the
OAuth account a person authorized on a ``per_user`` integration - one shared
with ``Don't share my connected account``. Every integration starts
``shared``; ``u5545ucred02`` moves the ones already shared that way.

**Lock safety.** ``ADD COLUMN ... NOT NULL DEFAULT 'shared'`` is metadata-only
on PostgreSQL 11+, but it takes a brief ``ACCESS EXCLUSIVE`` lock on
``integrations``, a hot table: canonical recipe (idempotency probe,
``terminate_lock_holders``, ``set_lock_timeout``, guarded DDL). The new table
references ``integrations`` and ``users``, whose foreign keys lock the
referenced tables, so its creation runs under the same belt; its indexes are
plain builds on an empty table.

Revision ID: u5545ucred01
Revises: u5715sig01
Create Date: 2026-10-09 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "u5545ucred01"
down_revision: str | Sequence[str] | None = "u5715sig01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "integration_user_credentials"


def _has_column(conn, table: str, column: str) -> bool:
    return (
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).first()
        is not None
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
    needs_column = not _has_column(conn, "integrations", "credential_mode")
    needs_table = not _has_table(conn, _TABLE)
    if not needs_column and not needs_table:
        return

    if needs_column:
        terminate_lock_holders(conn, "integrations")
    set_lock_timeout(conn)

    if needs_column:
        op.execute(
            "ALTER TABLE integrations ADD COLUMN IF NOT EXISTS credential_mode "
            "VARCHAR(16) NOT NULL DEFAULT 'shared'"
        )

    if needs_table:
        op.create_table(
            _TABLE,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "integration_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("integrations.id", ondelete="CASCADE"),
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
            sa.Column("account_config_enc", sa.Text(), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="connected"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint(
                "integration_id",
                "user_id",
                name="uq_integration_user_credentials_integration_user",
            ),
        )
        op.create_index("ix_integration_user_credentials_user_id", _TABLE, ["user_id"])
        op.create_index(
            "ix_integration_user_credentials_status_token_expires",
            _TABLE,
            ["status", "token_expires_at"],
            postgresql_where=sa.text("refresh_token_enc IS NOT NULL"),
        )


def downgrade() -> None:
    conn = op.get_bind()
    has_table = _has_table(conn, _TABLE)
    has_column = _has_column(conn, "integrations", "credential_mode")
    if not has_table and not has_column:
        return
    terminate_lock_holders(conn, "integrations")
    set_lock_timeout(conn)
    if has_table:
        op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
    if has_column:
        op.execute("ALTER TABLE integrations DROP COLUMN IF EXISTS credential_mode")
