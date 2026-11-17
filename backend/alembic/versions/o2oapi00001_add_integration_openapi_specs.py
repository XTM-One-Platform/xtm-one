"""add integration_openapi_specs for OpenAPI / Swagger integrations

Creates ``integration_openapi_specs`` — one row per ``provider == "openapi"``
integration holding what was converted from its imported Swagger 2.0 /
OpenAPI 3.x document: the operation catalogue, every supported operation's
tool definition, the selected operations and their tools, the chosen
authentication option. Purely additive: old replicas ignore a table they do
not know, and nothing is backfilled (no existing integration has a spec).

Lock safety: ``CREATE TABLE`` carries no inline ``REFERENCES`` — the foreign
key to ``integrations`` is added separately, ``NOT VALID`` (the table is new,
there is nothing to validate), under the canonical recipe (idempotency probe
+ early return, then ``terminate_lock_holders`` on the new table +
``set_lock_timeout``, then guarded DDL). The key takes SHARE ROW EXCLUSIVE on
the hot ``integrations`` table: that lock does not conflict with readers, so
the pre-flight is not run there (it would kill every session reading an
integration, backend/AGENTS.md) and the ``lock_timeout`` belt bounds the wait
behind a writer. The unique index is built ``CONCURRENTLY`` outside the
``lock_timeout`` belt.

Object names match what ``create_all`` emits on a fresh install (where this
body never runs), so both install paths converge.

Revision ID: o2oapi00001
Revises: r5ro5315001
Create Date: 2026-09-23 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "o2oapi00001"
# Chain behind the CURRENT single head; after a rebase onto a main that added
# migrations, re-point this (backend/AGENTS.md > "Database migrations", rule 1).
down_revision: str | Sequence[str] | None = "r5ro5315001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "integration_openapi_specs"
_FK_NAME = "integration_openapi_specs_integration_id_fkey"
_INDEX_NAME = "ix_integration_openapi_specs_integration_id"

# Spelled out literally rather than interpolated: the DO $$ block is the
# idempotent constraint idiom, and migration DDL must never be assembled from
# variables (ruff S608).
_ADD_FK_DDL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'integration_openapi_specs_integration_id_fkey'
    ) THEN
        ALTER TABLE integration_openapi_specs
            ADD CONSTRAINT integration_openapi_specs_integration_id_fkey
            FOREIGN KEY (integration_id)
            REFERENCES integrations(id) ON DELETE CASCADE
            NOT VALID;
    END IF;
END $$;
"""


def _table_exists(conn, table: str) -> bool:
    # Scoped to the active schema — information_schema spans all of them.
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": table},
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

    # ── 1. The table (no inline FK) ──────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS integration_openapi_specs (
            id UUID NOT NULL,
            integration_id UUID NOT NULL,
            source_type VARCHAR(10) NOT NULL,
            source_url VARCHAR(2000),
            source_filename VARCHAR(255),
            document_format VARCHAR(10) NOT NULL,
            spec_version VARCHAR(32) NOT NULL,
            document_sha256 VARCHAR(64) NOT NULL,
            api_title VARCHAR(500) NOT NULL DEFAULT '',
            api_version VARCHAR(100) NOT NULL DEFAULT '',
            api_description TEXT,
            default_server_url VARCHAR(2000),
            tool_prefix VARCHAR(32) NOT NULL DEFAULT '',
            auth_option JSON,
            auth_options JSON,
            operations JSON NOT NULL,
            tool_definitions JSON NOT NULL,
            selected_operation_keys JSON NOT NULL,
            active_tools JSON NOT NULL,
            warnings JSON,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            refreshed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT integration_openapi_specs_pkey PRIMARY KEY (id)
        )
        """
    )

    # ── 2. Foreign key (separate, NOT VALID, with the pre-flight) ────────────
    # Steady-state re-runs find the constraint and take NO lock.
    if not _constraint_exists(conn, _FK_NAME) and _table_exists(conn, "integrations"):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(_ADD_FK_DDL)

    # ── 3. Unique index ──────────────────────────────────────────────────────
    # CONCURRENTLY must run outside a transaction and must NOT be wrapped in a
    # lock_timeout (the build legitimately waits on snapshots).
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = 0")
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_integration_openapi_specs_integration_id "
            "ON integration_openapi_specs (integration_id)"
        )


def downgrade() -> None:
    conn = op.get_bind()

    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")

    # Dropping the table removes its constraint with it.
    if _table_exists(conn, _TABLE):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
