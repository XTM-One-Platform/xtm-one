"""add linked phones, the VAPID key pair and server-side notification read state

* ``mobile_devices``: the phones a person linked (their encrypted Web Push
  subscription, opt-outs, quiet hours);
* ``web_push_keys``: the platform's VAPID key pair, one row, created on first use;
* ``notification_read_states``: what each person has read of their
  notifications, shared by the bell and the phone.

Lock safety, as every migration of this repository: the tables are created without inline
``REFERENCES``; each foreign key is added separately, ``NOT VALID``, under
``terminate_lock_holders`` + ``set_lock_timeout`` (a key takes a lock on
``users``). Indexes are plain builds on the new, empty tables.

Revision ID: c1phn6592001
Revises: f8528frict02
Create Date: 2026-10-20 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import create_index_in_place, set_lock_timeout, terminate_lock_holders

revision: str = "c1phn6592001"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "f8528frict02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES: tuple[tuple[str, str], ...] = (
    (
        "mobile_devices",
        """
        CREATE TABLE IF NOT EXISTS mobile_devices (
            id UUID NOT NULL,
            user_id UUID NOT NULL,
            name VARCHAR(120) NOT NULL,
            platform VARCHAR(20) NOT NULL DEFAULT 'other',
            push_subscription TEXT NOT NULL,
            endpoint_hash VARCHAR(64) NOT NULL,
            push_enabled BOOLEAN NOT NULL DEFAULT true,
            muted_kinds JSON NOT NULL DEFAULT '[]',
            quiet_hours JSON,
            last_seen_at TIMESTAMP WITH TIME ZONE,
            last_push_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT mobile_devices_pkey PRIMARY KEY (id),
            CONSTRAINT mobile_devices_endpoint_hash_key UNIQUE (endpoint_hash)
        )
        """,
    ),
    (
        "web_push_keys",
        """
        CREATE TABLE IF NOT EXISTS web_push_keys (
            id INTEGER NOT NULL,
            public_key VARCHAR(200) NOT NULL,
            private_key TEXT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT web_push_keys_pkey PRIMARY KEY (id)
        )
        """,
    ),
    (
        "notification_read_states",
        """
        CREATE TABLE IF NOT EXISTS notification_read_states (
            user_id UUID NOT NULL,
            read_at TIMESTAMP WITH TIME ZONE,
            dismissed_ids JSON NOT NULL DEFAULT '[]',
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT notification_read_states_pkey PRIMARY KEY (user_id)
        )
        """,
    ),
)

# (constraint, table, literal DDL): spelled out, never assembled (ruff S608).
_FOREIGN_KEYS: tuple[tuple[str, str, str], ...] = (
    (
        "mobile_devices_user_id_fkey",
        "mobile_devices",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mobile_devices_user_id_fkey') THEN
                ALTER TABLE mobile_devices
                    ADD CONSTRAINT mobile_devices_user_id_fkey
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                    NOT VALID;
            END IF;
        END $$;
        """,
    ),
    (
        "notification_read_states_user_id_fkey",
        "notification_read_states",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'notification_read_states_user_id_fkey') THEN
                ALTER TABLE notification_read_states
                    ADD CONSTRAINT notification_read_states_user_id_fkey
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                    NOT VALID;
            END IF;
        END $$;
        """,
    ),
)


def _validate_constraint(conn, table: str, constraint: str) -> None:
    """Validate a key added ``NOT VALID``, once: on a new table it scans nothing and holds only SHARE UPDATE EXCLUSIVE."""
    unvalidated = conn.execute(
        sa.text("SELECT 1 FROM pg_constraint WHERE conname = :name AND NOT convalidated"), {"name": constraint}
    ).scalar()
    if unvalidated:
        set_lock_timeout(conn)
        conn.execute(sa.text(f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}"))


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
        create_index_in_place(conn, "ix_mobile_devices_user_id", "mobile_devices", "(user_id)")


def downgrade() -> None:
    conn = op.get_bind()
    for table in ("notification_read_states", "web_push_keys", "mobile_devices"):
        if _table_exists(conn, table):
            terminate_lock_holders(conn, table)
            set_lock_timeout(conn)
            op.execute(f"DROP TABLE IF EXISTS {table}")
