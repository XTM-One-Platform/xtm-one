"""add custom tool repository sources and declared tool tests

Creates ``custom_tool_sources`` - a GitHub or GitLab repository synced into
custom tools (``services/custom_tool_sources.py``) - and adds to
``custom_tools`` the declared tests (``test_cases``, ``test_code``), the last
test run (``last_test_status``, ``last_test_run_at``, ``last_test_results``)
and the link to a source (``source_id``, ``source_path``,
``source_commit_sha``, ``source_content_hash``, ``source_removed_at``).

Every ``custom_tools`` column is nullable with no default: a metadata-only
``ADD COLUMN`` (dev-docs/reference/migrations-safety.md rule 4), behind the
canonical recipe. ``custom_tools.source_id`` carries no foreign key: one would
lock ``custom_tools`` for the build, and the code treats a missing source as
detached. The new table's only key (``user_id`` -> ``users``) is added
separately, ``NOT VALID`` then validated, as in ``d1cdb6591001``.

Revision ID: ctsrc0001
Revises: ctemscope11613
Create Date: 2026-11-13 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import create_index_in_place, set_lock_timeout, terminate_lock_holders

revision: str = "ctsrc0001"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "ctemscope11613"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "custom_tool_sources"

_FOREIGN_KEYS: tuple[tuple[str, str, str, str], ...] = (
    (
        "custom_tool_sources_user_id_fkey",
        "user_id",
        "users",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'custom_tool_sources_user_id_fkey'
            ) THEN
                ALTER TABLE custom_tool_sources
                    ADD CONSTRAINT custom_tool_sources_user_id_fkey
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                    NOT VALID;
            END IF;
        END $$;
        """,
    ),
)

#: (table, index name, column) - the same names as ``create_all``.
_INDEXES: tuple[tuple[str, str, str], ...] = (
    (_TABLE, "ix_custom_tool_sources_user_id", "user_id"),
    ("custom_tools", "ix_custom_tools_source_id", "source_id"),
)

#: (column, type) added to ``custom_tools``; all nullable, no default.
_TOOL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("test_cases", "JSON"),
    ("test_code", "TEXT"),
    ("last_test_status", "VARCHAR(20)"),
    ("last_test_run_at", "TIMESTAMP WITH TIME ZONE"),
    ("last_test_results", "JSON"),
    ("source_id", "UUID"),
    ("source_path", "VARCHAR(500)"),
    ("source_commit_sha", "VARCHAR(64)"),
    ("source_content_hash", "VARCHAR(64)"),
    ("source_removed_at", "TIMESTAMP WITH TIME ZONE"),
)


def _table_exists(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": table},
        ).scalar()
    )


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).scalar()
    )


def _constraint_exists(conn, name: str) -> bool:
    return bool(conn.execute(sa.text("SELECT 1 FROM pg_constraint WHERE conname = :name"), {"name": name}).scalar())


def _validate_constraint(conn, table: str, constraint: str) -> None:
    """Validate a key added ``NOT VALID``, once: on a new table it scans nothing."""
    unvalidated = conn.execute(
        sa.text("SELECT 1 FROM pg_constraint WHERE conname = :name AND NOT convalidated"), {"name": constraint}
    ).scalar()
    if unvalidated:
        set_lock_timeout(conn)
        conn.execute(sa.text(f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}"))


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. The table (no inline foreign key: no lock on another table) ───────
    if not _table_exists(conn, _TABLE):
        set_lock_timeout(conn)
        op.execute(
            """
            CREATE TABLE IF NOT EXISTS custom_tool_sources (
                id UUID NOT NULL,
                user_id UUID,
                name VARCHAR(200) NOT NULL,
                provider VARCHAR(20) NOT NULL,
                repository VARCHAR(300) NOT NULL,
                ref VARCHAR(255),
                path VARCHAR(300) NOT NULL DEFAULT '',
                integration_id UUID,
                sync_interval VARCHAR(20) NOT NULL DEFAULT 'manual',
                enabled BOOLEAN NOT NULL DEFAULT true,
                is_company_managed BOOLEAN NOT NULL DEFAULT false,
                detached_paths JSON,
                last_synced_at TIMESTAMP WITH TIME ZONE,
                last_commit_sha VARCHAR(64),
                last_sync_status VARCHAR(20),
                last_sync_error TEXT,
                last_sync_report JSON,
                sync_failure_count INTEGER NOT NULL DEFAULT 0,
                sync_next_retry_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                CONSTRAINT custom_tool_sources_pkey PRIMARY KEY (id)
            )
            """
        )

    # ── 2. Foreign keys (separate, NOT VALID, one pre-flight each) ───────────
    for constraint, column, referenced, ddl in _FOREIGN_KEYS:
        if not _column_exists(conn, _TABLE, column) or _constraint_exists(conn, constraint):
            continue
        if not _table_exists(conn, referenced):
            continue
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(ddl)
    for constraint, _column, _referenced, _ddl in _FOREIGN_KEYS:
        _validate_constraint(conn, _TABLE, constraint)

    # ── 3. The custom_tools columns ──────────────────────────────────────────
    if _table_exists(conn, "custom_tools"):
        for column, ddl in _TOOL_COLUMNS:
            # Steady-state re-runs take NO lock.
            if _column_exists(conn, "custom_tools", column):
                continue
            terminate_lock_holders(conn, "custom_tools")
            set_lock_timeout(conn)
            op.execute(f"ALTER TABLE custom_tools ADD COLUMN IF NOT EXISTS {column} {ddl}")

    # ── 4. Indexes: plain builds, each committed on its own ──────────────────
    with op.get_context().autocommit_block():
        for table, index_name, column in _INDEXES:
            if _column_exists(conn, table, column):
                create_index_in_place(conn, index_name, table, f"({column})")


def downgrade() -> None:
    conn = op.get_bind()
    if _table_exists(conn, "custom_tools") and any(
        _column_exists(conn, "custom_tools", column) for column, _ddl in _TOOL_COLUMNS
    ):
        terminate_lock_holders(conn, "custom_tools")
        set_lock_timeout(conn)
        # Dropping source_id drops its index with it.
        for column, _ddl in reversed(_TOOL_COLUMNS):
            op.execute(f"ALTER TABLE custom_tools DROP COLUMN IF EXISTS {column}")
    # Dropping the table drops its indexes and constraints with it. It takes an
    # ACCESS EXCLUSIVE lock on the table its key references: under the belt.
    if _table_exists(conn, _TABLE):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute("DROP TABLE IF EXISTS custom_tool_sources")
