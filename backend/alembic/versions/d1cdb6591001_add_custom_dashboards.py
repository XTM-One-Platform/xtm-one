"""add custom_dashboards for agent-built dashboards

Creates ``custom_dashboards`` - one row per dashboard an agent built for a
person, holding the validated ``DashboardSpec`` in a JSON column plus the
denormalized ``title`` / ``widget_count`` of list views and the ``revision``
bumped on every write.

Lock safety, as ``b2r7rep2720`` (reports): ``CREATE TABLE`` carries no inline
``REFERENCES``. Every foreign key is added separately, ``NOT VALID`` (no
validation scan), under the canonical recipe (idempotency probe, then
``terminate_lock_holders`` + ``set_lock_timeout``, then guarded DDL). The
indexes are plain builds on the new, empty table (``create_index_in_place``
inside an autocommit block); ``CONCURRENTLY`` is banned in a migration body.

Object names match what ``create_all`` emits on a fresh install (where this
body never runs), so both install paths converge on identical names.

Revision ID: d1cdb6591001
Revises: n8753notbef01
Create Date: 2026-10-20 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import create_index_in_place, set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "d1cdb6591001"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "n8753notbef01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "custom_dashboards"

# (constraint name, column, referenced table, literal DDL). Spelled out per
# constraint rather than interpolated: migration DDL is never assembled from
# variables (ruff S608).
_FOREIGN_KEYS: tuple[tuple[str, str, str, str], ...] = (
    (
        "custom_dashboards_user_id_fkey",
        "user_id",
        "users",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'custom_dashboards_user_id_fkey'
            ) THEN
                ALTER TABLE custom_dashboards
                    ADD CONSTRAINT custom_dashboards_user_id_fkey
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
                    NOT VALID;
            END IF;
        END $$;
        """,
    ),
    (
        "custom_dashboards_conversation_id_fkey",
        "conversation_id",
        "conversations",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'custom_dashboards_conversation_id_fkey'
            ) THEN
                ALTER TABLE custom_dashboards
                    ADD CONSTRAINT custom_dashboards_conversation_id_fkey
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE SET NULL
                    NOT VALID;
            END IF;
        END $$;
        """,
    ),
    (
        "custom_dashboards_assignment_run_id_fkey",
        "assignment_run_id",
        "assignment_runs",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'custom_dashboards_assignment_run_id_fkey'
            ) THEN
                ALTER TABLE custom_dashboards
                    ADD CONSTRAINT custom_dashboards_assignment_run_id_fkey
                    FOREIGN KEY (assignment_run_id) REFERENCES assignment_runs(id) ON DELETE SET NULL
                    NOT VALID;
            END IF;
        END $$;
        """,
    ),
)

# (index name, column)
_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_custom_dashboards_user_id", "user_id"),
    ("ix_custom_dashboards_conversation_id", "conversation_id"),
    ("ix_custom_dashboards_assignment_run_id", "assignment_run_id"),
    ("ix_custom_dashboards_created_at", "created_at"),
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
    """Validate a key added ``NOT VALID``, once: on a new table it scans nothing and holds only SHARE UPDATE EXCLUSIVE."""
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
            CREATE TABLE IF NOT EXISTS custom_dashboards (
                id UUID NOT NULL,
                user_id UUID,
                conversation_id UUID,
                assignment_run_id UUID,
                title VARCHAR(200) NOT NULL,
                widget_count INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 1,
                spec JSON NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                CONSTRAINT custom_dashboards_pkey PRIMARY KEY (id)
            )
            """
        )

    # ── 2. Foreign keys (separate, NOT VALID, one pre-flight each) ───────────
    for constraint, column, referenced, ddl in _FOREIGN_KEYS:
        # Steady-state re-runs take NO lock.
        if not _column_exists(conn, _TABLE, column) or _constraint_exists(conn, constraint):
            continue
        if not _table_exists(conn, referenced):
            continue
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(ddl)
    # Validated at once: the table is new and empty, and no key is left NOT VALID.
    for constraint, _column, _referenced, _ddl in _FOREIGN_KEYS:
        _validate_constraint(conn, _TABLE, constraint)

    # ── 3. Indexes: plain builds, each committed on its own ──────────────────
    with op.get_context().autocommit_block():
        for index_name, column in _INDEXES:
            create_index_in_place(conn, index_name, _TABLE, f"({column})")


def downgrade() -> None:
    conn = op.get_bind()
    # Dropping the table drops its indexes and constraints with it. It takes an
    # ACCESS EXCLUSIVE lock on the tables its keys reference: under the belt.
    if _table_exists(conn, _TABLE):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute("DROP TABLE IF EXISTS custom_dashboards")
