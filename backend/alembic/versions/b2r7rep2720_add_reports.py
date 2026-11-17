"""add reports table for agent-generated executive reports

Creates ``reports`` — one row per agent-generated report, holding the validated
``ReportSpec`` in a JSON column plus the denormalized ``title`` / ``theme`` /
``block_count`` used by list views, and a ``revision`` counter bumped on every
in-place edit.

Why a table rather than reusing ``file_attachments``: a report must keep a stable
id across conversation turns so the ``[[REPORT:<id>]]`` marker already sitting in
a conversation stays valid when the agent adds a scenario, re-scores the callout
or appends a source. Blob artifacts in S3 are immutable and addressed by object
key, so they cannot express that.

Lock safety: ``CREATE TABLE`` carries no inline ``REFERENCES`` — every FK is
added separately, ``NOT VALID`` (no validation scan), under the canonical recipe
(idempotency probe + early return, then ``terminate_lock_holders`` +
``set_lock_timeout``, then guarded DDL). An inline FK would take SHARE ROW
EXCLUSIVE on ``users`` / ``conversations`` inside ``CREATE TABLE``, where no
pre-flight can protect it. Indexes are built ``CONCURRENTLY`` outside the
``lock_timeout`` belt (a concurrent build legitimately waits on snapshots).

Object names match exactly what ``create_all`` emits on a fresh install (where
this body never runs), so both install paths converge on identical names.

Revision ID: b2r7rep2720
Revises: a7c3sk1ll2697
Create Date: 2026-08-01 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "b2r7rep2720"
# Chains behind the current single head. A report may link a
# ``template_container`` row and reference an ``assignment_run``, so both
# tables must exist first — they do, well upstream of this head. If this branch is
# rebased onto a main that added migrations, re-point this to the NEW single head
# rather than opening a second one (backend/AGENTS.md > "Database migrations
# (safety)", rule 1): a forked chain blocks every deployment.
down_revision: str | None = "a7c3sk1ll2697"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "reports"

# (constraint name, column, referenced table, literal DDL)
#
# The DDL is spelled out literally per constraint rather than interpolated in a
# loop: the ``DO $$`` block is the idempotent constraint idiom, and building it
# with an f-string would embed identifiers in a ``SELECT`` (flagged as a SQL
# injection vector by ruff's S608, correctly — migration DDL must never be
# assembled from variables). ``NOT VALID`` skips the validation scan so each
# statement stays metadata-only. Same arrangement as the presentations migration (``f7p1res2637``).
_FOREIGN_KEYS: tuple[tuple[str, str, str, str], ...] = (
    (
        "reports_user_id_fkey",
        "user_id",
        "users",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'reports_user_id_fkey'
            ) THEN
                ALTER TABLE reports
                    ADD CONSTRAINT reports_user_id_fkey
                    FOREIGN KEY (user_id)
                    REFERENCES users(id) ON DELETE SET NULL
                    NOT VALID;
            END IF;
        END $$;
        """,
    ),
    (
        "reports_conversation_id_fkey",
        "conversation_id",
        "conversations",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'reports_conversation_id_fkey'
            ) THEN
                ALTER TABLE reports
                    ADD CONSTRAINT reports_conversation_id_fkey
                    FOREIGN KEY (conversation_id)
                    REFERENCES conversations(id) ON DELETE SET NULL
                    NOT VALID;
            END IF;
        END $$;
        """,
    ),
    (
        "reports_assignment_run_id_fkey",
        "assignment_run_id",
        "assignment_runs",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'reports_assignment_run_id_fkey'
            ) THEN
                ALTER TABLE reports
                    ADD CONSTRAINT reports_assignment_run_id_fkey
                    FOREIGN KEY (assignment_run_id)
                    REFERENCES assignment_runs(id) ON DELETE SET NULL
                    NOT VALID;
            END IF;
        END $$;
        """,
    ),
    (
        "reports_template_container_id_fkey",
        "template_container_id",
        "template_containers",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'reports_template_container_id_fkey'
            ) THEN
                ALTER TABLE reports
                    ADD CONSTRAINT reports_template_container_id_fkey
                    FOREIGN KEY (template_container_id)
                    REFERENCES template_containers(id) ON DELETE SET NULL
                    NOT VALID;
            END IF;
        END $$;
        """,
    ),
)

# (index name, column)
_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_reports_user_id", "user_id"),
    ("ix_reports_conversation_id", "conversation_id"),
    ("ix_reports_assignment_run_id", "assignment_run_id"),
    ("ix_reports_template_container_id", "template_container_id"),
    ("ix_reports_created_at", "created_at"),
)


def _table_exists(conn, table: str) -> bool:
    # Scope the probe to the active schema — information_schema spans ALL
    # schemas, so a same-named table elsewhere must not make the probe skip the
    # DDL in the schema being migrated.
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

    # ── 1. reports ───────────────────────────────────────────────────────────
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id UUID NOT NULL,
            user_id UUID,
            conversation_id UUID,
            assignment_run_id UUID,
            template_container_id UUID,
            title VARCHAR(300) NOT NULL,
            theme VARCHAR(50) NOT NULL DEFAULT 'light',
            block_count INTEGER NOT NULL DEFAULT 0,
            revision INTEGER NOT NULL DEFAULT 1,
            spec JSON NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT reports_pkey PRIMARY KEY (id)
        )
        """
    )

    # ── 2. Foreign keys (separate, NOT VALID, one pre-flight each) ────────────
    for constraint, column, referenced, ddl in _FOREIGN_KEYS:
        # Skip when the referencing column is absent (never on a real install)
        # or the constraint already exists — steady-state re-runs take NO lock.
        if not _column_exists(conn, _TABLE, column):
            continue
        if _constraint_exists(conn, constraint):
            continue
        # A referenced table that does not exist yet would make the ALTER fail;
        # skipping keeps the migration re-runnable rather than wedging startup.
        if not _table_exists(conn, referenced):
            continue
        terminate_lock_holders(conn, _TABLE)
        terminate_lock_holders(conn, referenced)
        set_lock_timeout(conn)
        op.execute(ddl)

    # ── 3. Indexes ───────────────────────────────────────────────────────────
    # CONCURRENTLY must run outside a transaction and must NOT be wrapped in a
    # lock_timeout (the build legitimately waits on snapshots).
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = 0")
        for index_name, column in _INDEXES:
            if not _column_exists(conn, _TABLE, column):
                continue
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name} "
                f"ON {_TABLE} ({column})"
            )


def downgrade() -> None:
    conn = op.get_bind()

    with op.get_context().autocommit_block():
        for index_name, _column in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")

    # Dropping the table removes its constraints with it; the FKs are only
    # dropped separately when the table survives (it never does here).
    if _table_exists(conn, _TABLE):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
