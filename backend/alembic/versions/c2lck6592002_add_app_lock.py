"""add the companion app lock: PINs and phone sessions

* ``app_lock_pins``: the PIN each person chose to unlock the mobile companion
  (bcrypt), and whether they turned the lock on;
* ``app_lock_sessions``: the web sessions the companion runs in, locked until
  the PIN is entered (``services/app_lock.py``).

Lock safety, as ``c1phn6592001``: tables without inline ``REFERENCES``, each key
added separately ``NOT VALID`` under ``terminate_lock_holders`` +
``set_lock_timeout``, indexes built in place on the new, empty tables.

Revision ID: c2lck6592002
Revises: c1phn6592001
Create Date: 2026-10-20 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import create_index_in_place, set_lock_timeout, terminate_lock_holders

revision: str = "c2lck6592002"
down_revision: str | None = "c1phn6592001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES: tuple[tuple[str, str], ...] = (
    (
        "app_lock_pins",
        """
        CREATE TABLE IF NOT EXISTS app_lock_pins (
            user_id UUID NOT NULL,
            pin_hash TEXT NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT app_lock_pins_pkey PRIMARY KEY (user_id)
        )
        """,
    ),
    (
        "app_lock_sessions",
        """
        CREATE TABLE IF NOT EXISTS app_lock_sessions (
            sid VARCHAR(64) NOT NULL,
            user_id UUID NOT NULL,
            unlocked_until TIMESTAMP WITH TIME ZONE,
            failed_attempts INTEGER NOT NULL DEFAULT 0,
            ended BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT app_lock_sessions_pkey PRIMARY KEY (sid)
        )
        """,
    ),
)

_FOREIGN_KEYS: tuple[tuple[str, str, str], ...] = (
    (
        "app_lock_pins_user_id_fkey",
        "app_lock_pins",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'app_lock_pins_user_id_fkey') THEN
                ALTER TABLE app_lock_pins
                    ADD CONSTRAINT app_lock_pins_user_id_fkey
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                    NOT VALID;
            END IF;
        END $$;
        """,
    ),
    (
        "app_lock_sessions_user_id_fkey",
        "app_lock_sessions",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'app_lock_sessions_user_id_fkey') THEN
                ALTER TABLE app_lock_sessions
                    ADD CONSTRAINT app_lock_sessions_user_id_fkey
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                    NOT VALID;
            END IF;
        END $$;
        """,
    ),
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
    for table, ddl in _TABLES:
        if not _table_exists(conn, table):
            set_lock_timeout(conn)
            op.execute(ddl)
    for constraint, table, ddl in _FOREIGN_KEYS:
        if _constraint_exists(conn, constraint) or not _table_exists(conn, "users"):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(ddl)
    # Validated at once: the tables are new and empty, and no key is left NOT VALID.
    for constraint, table, _ddl in _FOREIGN_KEYS:
        _validate_constraint(conn, table, constraint)
    with op.get_context().autocommit_block():
        create_index_in_place(conn, "ix_app_lock_sessions_user_id", "app_lock_sessions", "(user_id)")


def downgrade() -> None:
    conn = op.get_bind()
    for table in ("app_lock_sessions", "app_lock_pins"):
        if _table_exists(conn, table):
            terminate_lock_holders(conn, table)
            set_lock_timeout(conn)
            op.execute(f"DROP TABLE IF EXISTS {table}")
