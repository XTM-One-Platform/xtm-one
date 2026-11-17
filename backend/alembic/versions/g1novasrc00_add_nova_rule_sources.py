"""add nova_rule_sources — scheduled GitHub sync of .nov rules

Creates ``nova_rule_sources`` — one row per repo synced into company-managed
NovaRules on a schedule (mirrors ``KBSyncSource``). Brand-new table: the
integration FK is added separately, ``NOT VALID``, and the index builds
``CONCURRENTLY``, per the canonical recipe. Object names match ``create_all``.

Revision ID: g1novasrc00
Revises: f3nova00rule
Create Date: 2026-08-12 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "g1novasrc00"
down_revision: str | None = "f3nova00rule"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "nova_rule_sources"

_FOREIGN_KEYS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "nova_rule_sources_integration_id_fkey",
        _TABLE,
        "integration_id",
        "integrations",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'nova_rule_sources_integration_id_fkey'
            ) THEN
                ALTER TABLE nova_rule_sources
                    ADD CONSTRAINT nova_rule_sources_integration_id_fkey
                    FOREIGN KEY (integration_id)
                    REFERENCES integrations(id) ON DELETE CASCADE
                    NOT VALID;
            END IF;
        END $$;
        """,
    ),
)

_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_nova_rule_sources_integration_id", _TABLE, "integration_id"),
)


def _table_exists(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": table},
        ).scalar()
    )


def _column_exists(conn, table: str, column: str) -> bool:
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


def _constraint_exists(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT 1 FROM pg_constraint WHERE conname = :name"),
            {"name": name},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id UUID NOT NULL,
            integration_id UUID,
            repo VARCHAR(200) NOT NULL,
            branch VARCHAR(100) NOT NULL DEFAULT 'main',
            path VARCHAR(300) NOT NULL DEFAULT '',
            sync_interval VARCHAR(20) NOT NULL DEFAULT 'daily',
            enabled BOOLEAN NOT NULL DEFAULT true,
            last_synced_at TIMESTAMP WITH TIME ZONE,
            last_sync_status VARCHAR(20),
            last_sync_error TEXT,
            last_sync_rules INTEGER NOT NULL DEFAULT 0,
            sync_failure_count INTEGER NOT NULL DEFAULT 0,
            sync_next_retry_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT nova_rule_sources_pkey PRIMARY KEY (id)
        )
        """
    )

    for constraint, table, column, referenced, ddl in _FOREIGN_KEYS:
        if not _column_exists(conn, table, column):
            continue
        if _constraint_exists(conn, constraint):
            continue
        if not _table_exists(conn, referenced):
            continue
        terminate_lock_holders(conn, table)
        terminate_lock_holders(conn, referenced)
        set_lock_timeout(conn)
        op.execute(ddl)

    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = 0")
        for index_name, table, column in _INDEXES:
            if not _column_exists(conn, table, column):
                continue
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name} "
                f"ON {table} ({column})"
            )


def downgrade() -> None:
    conn = op.get_bind()

    with op.get_context().autocommit_block():
        for index_name, _table, _column in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")

    if _table_exists(conn, _TABLE):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
