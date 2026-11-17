"""add native OS-control runners (runners, runner_commands, runner_enrollment_keys)

Creates the three tables backing XTM One Runners — native, pull-based
cross-platform OS-control daemons:

  - ``runners`` — one row per enrolled machine (first-class three-tier entity
    with a hashed ``run-`` credential, a per-runner permission model and the
    reported ``host_facts``).
  - ``runner_commands`` — the durable command queue + immutable audit trail
    behind the event-driven WebSocket fast-path.
  - ``runner_enrollment_keys`` — hashed ``renr-`` enrollment credentials
    (single-use or reusable fleet keys).

Lock safety: every ``CREATE TABLE`` is ``IF NOT EXISTS`` and takes no lock on
any existing table (no inline FK to a hot table — the FKs to ``users`` and
``file_attachments`` are added separately ``NOT VALID`` under the canonical
recipe: idempotency probe + early return, then ``terminate_lock_holders`` +
``set_lock_timeout``, then the guarded DDL). The FK from ``runner_commands`` to
the brand-new ``runners`` table is declared inline (safe: nobody else can hold
a lock on a table created in this same migration). Indexes are built with
``CREATE INDEX CONCURRENTLY`` outside the ``lock_timeout`` belt.

Object names match exactly what ``create_all`` produces on a fresh install
(where this body never runs), so both install paths converge on identically
named constraints and indexes.

Revision ID: a7f3run0001
Revises: f2adpst28350
Create Date: 2026-08-15 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "a7f3run0001"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "f2adpst28350"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUNNERS = "runners"
_COMMANDS = "runner_commands"
_ENROLL = "runner_enrollment_keys"

_RUNNERS_USER_FK = "runners_user_id_fkey"
_COMMANDS_USER_FK = "runner_commands_user_id_fkey"
_COMMANDS_FILE_FK = "runner_commands_result_file_id_fkey"
_ENROLL_USER_FK = "runner_enrollment_keys_user_id_fkey"

# Runner targeting columns added to existing tables. All nullable JSON
# (metadata-only ADD COLUMN — no table rewrite on PG 11+).
_EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("agents", "runner_ids", "JSON"),
    ("agents", "runner_labels", "JSON"),
    ("assignments", "runner_ids", "JSON"),
)

# (index name, table, column expression, guard column that must exist)
_INDEXES: tuple[tuple[str, str, str, str], ...] = (
    ("ix_runners_user_id", _RUNNERS, "user_id", "user_id"),
    ("ix_runners_is_company_managed", _RUNNERS, "is_company_managed", "is_company_managed"),
    ("ix_runners_key_prefix", _RUNNERS, "key_prefix", "key_prefix"),
    ("ix_runner_commands_runner_id", _COMMANDS, "runner_id", "runner_id"),
    ("ix_runner_commands_runner_status", _COMMANDS, "runner_id, status", "runner_id"),
    ("ix_runner_commands_created_at", _COMMANDS, "created_at", "created_at"),
    ("ix_runner_enrollment_keys_user_id", _ENROLL, "user_id", "user_id"),
    ("ix_runner_enrollment_keys_key_prefix", _ENROLL, "key_prefix", "key_prefix"),
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


# Literal DDL (no interpolation) per FK — mirrors the reference migration so
# the ``DO $$`` idempotent-constraint idiom stays static and never trips the
# SQL-injection linter. Each is ``NOT VALID`` so no validation scan runs.
_RUNNERS_USER_FK_DDL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'runners_user_id_fkey'
        ) THEN
            ALTER TABLE runners
                ADD CONSTRAINT runners_user_id_fkey
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                NOT VALID;
        END IF;
    END $$;
"""

_COMMANDS_USER_FK_DDL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'runner_commands_user_id_fkey'
        ) THEN
            ALTER TABLE runner_commands
                ADD CONSTRAINT runner_commands_user_id_fkey
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
                NOT VALID;
        END IF;
    END $$;
"""

_ENROLL_USER_FK_DDL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'runner_enrollment_keys_user_id_fkey'
        ) THEN
            ALTER TABLE runner_enrollment_keys
                ADD CONSTRAINT runner_enrollment_keys_user_id_fkey
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                NOT VALID;
        END IF;
    END $$;
"""


def _add_user_fk(conn, table: str, name: str, ddl: str) -> None:
    """Add a ``NOT VALID`` FK from ``table.user_id`` to ``users(id)``.

    ``ddl`` is a static literal (the caller passes one of the ``*_FK_DDL``
    constants), so nothing is interpolated into the statement.
    """
    if not _table_exists(conn, table) or _constraint_exists(conn, name):
        return
    terminate_lock_holders(conn, table)
    terminate_lock_holders(conn, "users")
    set_lock_timeout(conn)
    op.execute(ddl)


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. runners ────────────────────────────────────────────────────────────
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_RUNNERS} (
            id UUID NOT NULL,
            user_id UUID,
            name VARCHAR(200) NOT NULL,
            platform VARCHAR(20) NOT NULL,
            hostname VARCHAR(255),
            key_prefix VARCHAR(20) NOT NULL,
            key_hash TEXT NOT NULL,
            permission_mode VARCHAR(20) NOT NULL DEFAULT 'restricted',
            capabilities JSON,
            allowed_shell_commands JSON,
            allowed_paths JSON,
            require_approval BOOLEAN NOT NULL DEFAULT FALSE,
            labels JSON,
            tags JSON,
            host_facts JSON,
            agent_version VARCHAR(40),
            status VARCHAR(20) NOT NULL DEFAULT 'offline',
            last_seen_at TIMESTAMP WITH TIME ZONE,
            transport VARCHAR(20),
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            is_company_managed BOOLEAN DEFAULT FALSE,
            shared_group_ids JSON,
            shared_group_permissions JSON,
            command_count INTEGER NOT NULL DEFAULT 0,
            enrolled_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT runners_pkey PRIMARY KEY (id)
        )
        """
    )

    # ── 2. runner_commands ────────────────────────────────────────────────────
    # Inline FK to the brand-new ``runners`` table is safe (no external lock
    # holder possible). FKs to ``users`` / ``file_attachments`` are added below.
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_COMMANDS} (
            id UUID NOT NULL,
            runner_id UUID NOT NULL,
            user_id UUID,
            agent_id UUID,
            conversation_id UUID,
            assignment_run_id UUID,
            kind VARCHAR(40) NOT NULL,
            payload JSON,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            result JSON,
            result_file_id UUID,
            error TEXT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            dispatched_at TIMESTAMP WITH TIME ZONE,
            completed_at TIMESTAMP WITH TIME ZONE,
            CONSTRAINT runner_commands_pkey PRIMARY KEY (id),
            CONSTRAINT runner_commands_runner_id_fkey
                FOREIGN KEY (runner_id) REFERENCES {_RUNNERS}(id) ON DELETE CASCADE
        )
        """
    )

    # ── 3. runner_enrollment_keys ─────────────────────────────────────────────
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_ENROLL} (
            id UUID NOT NULL,
            user_id UUID NOT NULL,
            name VARCHAR(255) NOT NULL,
            key_prefix VARCHAR(20) NOT NULL,
            key_hash TEXT NOT NULL,
            max_uses INTEGER,
            use_count INTEGER NOT NULL DEFAULT 0,
            default_labels JSON,
            default_capabilities JSON,
            default_permission_mode VARCHAR(20) NOT NULL DEFAULT 'restricted',
            enroll_as_company_managed BOOLEAN NOT NULL DEFAULT FALSE,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            expires_at TIMESTAMP WITH TIME ZONE,
            last_used_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT runner_enrollment_keys_pkey PRIMARY KEY (id)
        )
        """
    )

    # ── 4. FKs to hot tables (NOT VALID, canonical recipe) ────────────────────
    _add_user_fk(conn, _RUNNERS, _RUNNERS_USER_FK, _RUNNERS_USER_FK_DDL)
    _add_user_fk(conn, _COMMANDS, _COMMANDS_USER_FK, _COMMANDS_USER_FK_DDL)
    _add_user_fk(conn, _ENROLL, _ENROLL_USER_FK, _ENROLL_USER_FK_DDL)

    if (
        _column_exists(conn, _COMMANDS, "result_file_id")
        and _table_exists(conn, "file_attachments")
        and not _constraint_exists(conn, _COMMANDS_FILE_FK)
    ):
        terminate_lock_holders(conn, _COMMANDS)
        terminate_lock_holders(conn, "file_attachments")
        set_lock_timeout(conn)
        op.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'runner_commands_result_file_id_fkey'
                ) THEN
                    ALTER TABLE runner_commands
                        ADD CONSTRAINT runner_commands_result_file_id_fkey
                        FOREIGN KEY (result_file_id)
                        REFERENCES file_attachments(id) ON DELETE SET NULL
                        NOT VALID;
                END IF;
            END $$;
            """
        )

    # ── 5. Runner targeting columns on agents / assignments ──────────────────
    for table, column, ddl_type in _EXTRA_COLUMNS:
        if not _table_exists(conn, table) or _column_exists(conn, table, column):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl_type}"
        )

    # ── 6. Indexes ────────────────────────────────────────────────────────────
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = 0")
        for index_name, table, column_expr, guard in _INDEXES:
            if not _column_exists(conn, table, guard):
                continue
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name} "
                f"ON {table} ({column_expr})"
            )


def downgrade() -> None:
    conn = op.get_bind()

    with op.get_context().autocommit_block():
        for index_name, _table, _col, _guard in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")

    for table, column, _ddl_type in _EXTRA_COLUMNS:
        if not _column_exists(conn, table, column):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")

    # Drop child tables first (FK dependency order).
    for table in (_COMMANDS, _ENROLL, _RUNNERS):
        if _table_exists(conn, table):
            terminate_lock_holders(conn, table)
            set_lock_timeout(conn)
            op.execute(f"DROP TABLE IF EXISTS {table}")
