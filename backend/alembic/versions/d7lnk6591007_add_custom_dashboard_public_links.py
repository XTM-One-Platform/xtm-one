"""Add custom_dashboard_public_links: the published, login-free link of a dashboard.

One new table: one link per dashboard (``dashboard_id`` unique, ON DELETE
CASCADE), its token hashed for the lookup and encrypted for the managers, who
published it (``created_by_user_id`` ON DELETE SET NULL: a link whose
publisher is gone answers nothing), how often its snapshot is read again, and
the snapshot itself with when and for which revision it was read.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent - ``CREATE TABLE IF NOT EXISTS``, guarded foreign keys. Fresh
  installs run ``create_all`` + ``stamp head``.
* Lock-safe - created without inline keys; each key is added ``NOT VALID``
  on the empty table under ``set_lock_timeout`` (it locks the referenced
  table) and validated separately. The unique constraints come with the
  table, empty.

Revision ID: d7lnk6591007
Revises: d6pub6591006
Create Date: 2026-10-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "d7lnk6591007"
down_revision: str | None = "d6pub6591006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "custom_dashboard_public_links"

_CREATE = """
CREATE TABLE IF NOT EXISTS custom_dashboard_public_links (
    id UUID PRIMARY KEY,
    dashboard_id UUID NOT NULL UNIQUE,
    token_hash VARCHAR(64) NOT NULL UNIQUE,
    token_enc TEXT NOT NULL,
    created_by_user_id UUID,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    refresh_minutes INTEGER NOT NULL DEFAULT 60,
    snapshot JSON,
    snapshot_at TIMESTAMP WITH TIME ZONE,
    snapshot_revision INTEGER
)
"""

# (constraint name, literal DDL): spelled out, never assembled from variables (S608).
_FOREIGN_KEYS: tuple[tuple[str, str], ...] = (
    (
        "custom_dashboard_public_links_dashboard_id_fkey",
        "ALTER TABLE custom_dashboard_public_links ADD CONSTRAINT custom_dashboard_public_links_dashboard_id_fkey "
        "FOREIGN KEY (dashboard_id) REFERENCES custom_dashboards(id) ON DELETE CASCADE NOT VALID",
    ),
    (
        "custom_dashboard_public_links_created_by_user_id_fkey",
        "ALTER TABLE custom_dashboard_public_links ADD CONSTRAINT custom_dashboard_public_links_created_by_user_id_fkey "
        "FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL NOT VALID",
    ),
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


def _constraint_exists(conn, name: str) -> bool:
    return bool(conn.execute(sa.text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": name}).scalar())


def upgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn):
        set_lock_timeout(conn)
        conn.execute(sa.text(_CREATE))
    for name, ddl in _FOREIGN_KEYS:
        if not _constraint_exists(conn, name):
            set_lock_timeout(conn)
            conn.execute(sa.text(ddl))
        set_lock_timeout(conn)
        conn.execute(sa.text(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {name}"))


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    conn.execute(sa.text(f"DROP TABLE IF EXISTS {_TABLE}"))
