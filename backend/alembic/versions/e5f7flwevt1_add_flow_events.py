"""add flow_event_types + flow_events for typed builder events

Creates ``flow_event_types`` — user-declared, typed event signatures (the UE
Event Dispatcher pattern: emit and listen nodes bind to the same declaration)
— and ``flow_events``, one row per emission, which both audits the fire and
drives dispatch (listener runs start at emit time; parked ``util.wait_event``
nodes poll this table).

Lock safety follows the flow_runs migration verbatim: brand-new tables carry
no inline ``REFERENCES``; every FK is added separately ``NOT VALID`` under
the canonical recipe (idempotency probe + early return, then
``terminate_lock_holders`` + ``set_lock_timeout``, then guarded DDL); indexes
build ``CONCURRENTLY`` outside the lock_timeout belt.

Object names match exactly what ``create_all`` emits on a fresh install.

Revision ID: e5f7flwevt1
Revises: e2c4flwrun2
Create Date: 2026-08-13 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "e5f7flwevt1"
down_revision: str | None = "e2c4flwrun2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVENT_TYPES = "flow_event_types"
_EVENTS = "flow_events"

# (constraint name, table, column, referenced table, literal DDL)
_FOREIGN_KEYS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "flow_event_types_user_id_fkey",
        _EVENT_TYPES,
        "user_id",
        "users",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'flow_event_types_user_id_fkey'
            ) THEN
                ALTER TABLE flow_event_types
                    ADD CONSTRAINT flow_event_types_user_id_fkey
                    FOREIGN KEY (user_id)
                    REFERENCES users(id) ON DELETE CASCADE
                    NOT VALID;
            END IF;
        END $$;
        """,
    ),
    (
        "flow_events_event_type_id_fkey",
        _EVENTS,
        "event_type_id",
        _EVENT_TYPES,
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'flow_events_event_type_id_fkey'
            ) THEN
                ALTER TABLE flow_events
                    ADD CONSTRAINT flow_events_event_type_id_fkey
                    FOREIGN KEY (event_type_id)
                    REFERENCES flow_event_types(id) ON DELETE CASCADE
                    NOT VALID;
            END IF;
        END $$;
        """,
    ),
    (
        "flow_events_user_id_fkey",
        _EVENTS,
        "user_id",
        "users",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'flow_events_user_id_fkey'
            ) THEN
                ALTER TABLE flow_events
                    ADD CONSTRAINT flow_events_user_id_fkey
                    FOREIGN KEY (user_id)
                    REFERENCES users(id) ON DELETE CASCADE
                    NOT VALID;
            END IF;
        END $$;
        """,
    ),
)

# (index name, table, column expression)
_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_flow_event_types_user_id", _EVENT_TYPES, "user_id"),
    ("ix_flow_events_event_type_id", _EVENTS, "event_type_id"),
    ("ix_flow_events_user_id", _EVENTS, "user_id"),
    ("ix_flow_events_flow_id", _EVENTS, "flow_id"),
    ("ix_flow_events_run_id", _EVENTS, "run_id"),
    ("ix_flow_events_created_at", _EVENTS, "created_at"),
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

    # ── 1. flow_event_types ──────────────────────────────────────────────────
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_EVENT_TYPES} (
            id UUID NOT NULL,
            user_id UUID,
            name VARCHAR(120) NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            fields JSON NOT NULL DEFAULT '[]',
            is_company_managed BOOLEAN NOT NULL DEFAULT false,
            shared_group_ids JSON,
            shared_group_permissions JSON,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT flow_event_types_pkey PRIMARY KEY (id)
        )
        """
    )

    # ── 2. flow_events ───────────────────────────────────────────────────────
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_EVENTS} (
            id UUID NOT NULL,
            event_type_id UUID NOT NULL,
            user_id UUID,
            flow_id UUID,
            run_id UUID,
            scope VARCHAR(10) NOT NULL DEFAULT 'flow',
            payload JSON NOT NULL DEFAULT '{{}}',
            chain_depth INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT flow_events_pkey PRIMARY KEY (id),
            CONSTRAINT ck_flow_events_chain_depth CHECK (chain_depth >= 0),
            CONSTRAINT ck_flow_events_scope CHECK (
                scope IN ('run','flow','global')
            )
        )
        """
    )

    # ── 3. Foreign keys (separate, NOT VALID, one pre-flight each) ───────────
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

    # ── 4. Indexes ───────────────────────────────────────────────────────────
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

    # Children first so the events -> types FK never blocks the parent drop.
    for table in (_EVENTS, _EVENT_TYPES):
        if _table_exists(conn, table):
            terminate_lock_holders(conn, table)
            set_lock_timeout(conn)
            op.execute(f"DROP TABLE IF EXISTS {table}")
