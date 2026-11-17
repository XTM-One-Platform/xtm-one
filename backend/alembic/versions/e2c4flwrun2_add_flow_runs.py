"""add flow_runs + flow_run_steps for flow execution

Creates ``flow_runs`` — one row per execution of a flow's graph, carrying a
frozen copy of the graph, the walker's cursor (``state``) and the `flow.call`
lineage — and ``flow_run_steps``, the per-node journal keyed by a monotonic
``seq`` (``util.loop`` re-executes nodes, so ``node_id`` is not unique within
a run).  Also adds two execution columns to ``flows``: ``last_triggered_at``
(the cron sweep's cursor) and ``webhook_secret``.

Lock safety follows the Phase 1 migration verbatim: brand-new tables carry no
inline ``REFERENCES``; every FK is added separately ``NOT VALID`` under the
canonical recipe (idempotency probe + early return, then
``terminate_lock_holders`` + ``set_lock_timeout``, then guarded DDL); indexes
build ``CONCURRENTLY`` outside the lock_timeout belt.  The two ``ALTER TABLE
flows ADD COLUMN`` are metadata-only (nullable, no default rewrite) but still
run under the recipe because ``flows`` already exists and may be hot.

Object names match exactly what ``create_all`` emits on a fresh install.

Revision ID: e2c4flwrun2
Revises: d1f9flw1909
Create Date: 2026-08-11 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "e2c4flwrun2"
down_revision: str | None = "d1f9flw1909"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FLOWS = "flows"
_FLOW_RUNS = "flow_runs"
_FLOW_RUN_STEPS = "flow_run_steps"

# (constraint name, table, column, referenced table, literal DDL)
# Spelled out literally per constraint — see the Phase 1 migration for why
# (ruff S608: migration DDL must never be assembled from variables).
_FOREIGN_KEYS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "flow_runs_flow_id_fkey",
        _FLOW_RUNS,
        "flow_id",
        _FLOWS,
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'flow_runs_flow_id_fkey'
            ) THEN
                ALTER TABLE flow_runs
                    ADD CONSTRAINT flow_runs_flow_id_fkey
                    FOREIGN KEY (flow_id)
                    REFERENCES flows(id) ON DELETE CASCADE
                    NOT VALID;
            END IF;
        END $$;
        """,
    ),
    (
        "flow_runs_user_id_fkey",
        _FLOW_RUNS,
        "user_id",
        "users",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'flow_runs_user_id_fkey'
            ) THEN
                ALTER TABLE flow_runs
                    ADD CONSTRAINT flow_runs_user_id_fkey
                    FOREIGN KEY (user_id)
                    REFERENCES users(id) ON DELETE CASCADE
                    NOT VALID;
            END IF;
        END $$;
        """,
    ),
    (
        "flow_runs_parent_run_id_fkey",
        _FLOW_RUNS,
        "parent_run_id",
        _FLOW_RUNS,
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'flow_runs_parent_run_id_fkey'
            ) THEN
                ALTER TABLE flow_runs
                    ADD CONSTRAINT flow_runs_parent_run_id_fkey
                    FOREIGN KEY (parent_run_id)
                    REFERENCES flow_runs(id) ON DELETE SET NULL
                    NOT VALID;
            END IF;
        END $$;
        """,
    ),
    (
        "flow_run_steps_flow_run_id_fkey",
        _FLOW_RUN_STEPS,
        "flow_run_id",
        _FLOW_RUNS,
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'flow_run_steps_flow_run_id_fkey'
            ) THEN
                ALTER TABLE flow_run_steps
                    ADD CONSTRAINT flow_run_steps_flow_run_id_fkey
                    FOREIGN KEY (flow_run_id)
                    REFERENCES flow_runs(id) ON DELETE CASCADE
                    NOT VALID;
            END IF;
        END $$;
        """,
    ),
)

# (index name, table, column expression)
_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_flow_runs_flow_id", _FLOW_RUNS, "flow_id"),
    ("ix_flow_runs_user_id", _FLOW_RUNS, "user_id"),
    ("ix_flow_runs_status", _FLOW_RUNS, "status"),
    ("ix_flow_runs_flow_created", _FLOW_RUNS, "flow_id, created_at"),
    ("ix_flow_run_steps_flow_run_id", _FLOW_RUN_STEPS, "flow_run_id"),
)

# Columns added to the existing `flows` table.
_FLOWS_NEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("last_triggered_at", "TIMESTAMP WITH TIME ZONE"),
    ("webhook_secret", "VARCHAR(128)"),
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

    # ── 1. flow_runs ─────────────────────────────────────────────────────────
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_FLOW_RUNS} (
            id UUID NOT NULL,
            flow_id UUID NOT NULL,
            flow_version_number INTEGER NOT NULL DEFAULT 1,
            graph JSON NOT NULL DEFAULT '{{}}',
            user_id UUID,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            trigger_type VARCHAR(30) NOT NULL,
            trigger_payload JSON,
            trigger_context JSON,
            state JSON,
            depth INTEGER NOT NULL DEFAULT 0,
            parent_run_id UUID,
            parent_node_id VARCHAR(80),
            result_summary TEXT,
            error TEXT,
            is_company_managed BOOLEAN NOT NULL DEFAULT false,
            shared_group_ids JSON,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            started_at TIMESTAMP WITH TIME ZONE,
            completed_at TIMESTAMP WITH TIME ZONE,
            CONSTRAINT flow_runs_pkey PRIMARY KEY (id),
            CONSTRAINT ck_flow_runs_status CHECK (
                status IN ('pending','running','awaiting','completed','failed','cancelled')
            ),
            CONSTRAINT ck_flow_runs_depth CHECK (depth >= 0)
        )
        """
    )

    # ── 2. flow_run_steps ────────────────────────────────────────────────────
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_FLOW_RUN_STEPS} (
            id UUID NOT NULL,
            flow_run_id UUID NOT NULL,
            seq INTEGER NOT NULL,
            node_id VARCHAR(80) NOT NULL,
            node_kind VARCHAR(50) NOT NULL,
            iteration INTEGER NOT NULL DEFAULT 0,
            status VARCHAR(20) NOT NULL,
            input JSON,
            output JSON,
            untrusted BOOLEAN NOT NULL DEFAULT false,
            error TEXT,
            started_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            completed_at TIMESTAMP WITH TIME ZONE,
            CONSTRAINT flow_run_steps_pkey PRIMARY KEY (id),
            CONSTRAINT uq_flow_run_steps_run_seq UNIQUE (flow_run_id, seq),
            CONSTRAINT ck_flow_run_steps_status CHECK (
                status IN ('running','completed','failed','skipped')
            ),
            CONSTRAINT ck_flow_run_steps_seq CHECK (seq >= 0)
        )
        """
    )

    # ── 3. New columns on flows (existing, possibly hot table) ───────────────
    for column, ddl_type in _FLOWS_NEW_COLUMNS:
        if not _table_exists(conn, _FLOWS) or _column_exists(conn, _FLOWS, column):
            continue
        terminate_lock_holders(conn, _FLOWS)
        set_lock_timeout(conn)
        # Literal identifiers from the module-level tuple, never user input;
        # nullable + no default → metadata-only, no table rewrite.
        op.execute(
            f"ALTER TABLE {_FLOWS} ADD COLUMN IF NOT EXISTS {column} {ddl_type}"
        )

    # ── 4. Foreign keys (separate, NOT VALID, one pre-flight each) ───────────
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

    # ── 5. Indexes ───────────────────────────────────────────────────────────
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = 0")
        for index_name, table, columns in _INDEXES:
            first_column = columns.split(",")[0].strip()
            if not _column_exists(conn, table, first_column):
                continue
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name} "
                f"ON {table} ({columns})"
            )


def downgrade() -> None:
    conn = op.get_bind()

    with op.get_context().autocommit_block():
        for index_name, _table, _columns in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")

    # Children first so the steps -> runs FK never blocks the parent drop.
    for table in (_FLOW_RUN_STEPS, _FLOW_RUNS):
        if _table_exists(conn, table):
            terminate_lock_holders(conn, table)
            set_lock_timeout(conn)
            op.execute(f"DROP TABLE IF EXISTS {table}")

    for column, _ddl_type in _FLOWS_NEW_COLUMNS:
        if not _column_exists(conn, _FLOWS, column):
            continue
        terminate_lock_holders(conn, _FLOWS)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_FLOWS} DROP COLUMN IF EXISTS {column}")
