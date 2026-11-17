"""Add dashboard_widget_packs: the library of ready-made dashboard widgets.

One new table (``models/dashboard_widget_pack.py``): a pack's name, its
widgets (unbound platform widgets: no integration), where it came from, and
the three-tier visibility column set. Built-in packs are written by the
startup seed, not here.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent - ``CREATE TABLE IF NOT EXISTS``, a guarded foreign key, indexes
  built only when missing. Fresh installs run ``create_all`` + ``stamp head``.
* Lock-safe - the table is created without its foreign key; the key to
  ``users`` is added ``NOT VALID`` on the empty table under
  ``set_lock_timeout`` (it takes a lock on ``users``) and validated separately.
  Indexes are plain builds (``create_index_in_place``) on an empty table.

Revision ID: d5pck6591005
Revises: d4std6591004
Create Date: 2026-10-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import create_index_in_place, set_lock_timeout, terminate_lock_holders

revision: str = "d5pck6591005"
down_revision: str | None = "d4std6591004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "dashboard_widget_packs"
_FK = "dashboard_widget_packs_user_id_fkey"

_CREATE = """
CREATE TABLE IF NOT EXISTS dashboard_widget_packs (
    id UUID PRIMARY KEY,
    user_id UUID,
    name VARCHAR(200) NOT NULL,
    description TEXT,
    tags JSON NOT NULL,
    origin VARCHAR(20) NOT NULL,
    sources JSON NOT NULL,
    widgets JSON NOT NULL,
    widget_count INTEGER NOT NULL DEFAULT 0,
    source_meta JSON,
    created_at TIMESTAMP WITH TIME ZONE,
    updated_at TIMESTAMP WITH TIME ZONE,
    is_company_managed BOOLEAN NOT NULL DEFAULT FALSE,
    shared_group_ids JSON,
    shared_group_permissions JSON,
    shared_user_ids JSON,
    shared_user_permissions JSON,
    access_restricted BOOLEAN NOT NULL DEFAULT FALSE
)
"""

_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_dashboard_widget_packs_user_id", "(user_id)"),
    ("ix_dashboard_widget_packs_is_company_managed", "(is_company_managed)"),
)


def _table_exists(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :t"
            ),
            {"t": _TABLE},
        ).scalar()
    )


def _constraint_exists(conn) -> bool:
    return bool(conn.execute(sa.text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": _FK}).scalar())


def upgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn):
        set_lock_timeout(conn)
        conn.execute(sa.text(_CREATE))
    if not _constraint_exists(conn):
        set_lock_timeout(conn)
        conn.execute(
            sa.text(
                "ALTER TABLE dashboard_widget_packs ADD CONSTRAINT dashboard_widget_packs_user_id_fkey "
                "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL NOT VALID"
            )
        )
    set_lock_timeout(conn)
    conn.execute(sa.text(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {_FK}"))
    with op.get_context().autocommit_block():
        for name, columns in _INDEXES:
            create_index_in_place(conn, name, _TABLE, columns)


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    conn.execute(sa.text(f"DROP TABLE IF EXISTS {_TABLE}"))
