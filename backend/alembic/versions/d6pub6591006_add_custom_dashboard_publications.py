"""Add custom_dashboard_publications: where a dashboard was published.

One new table: a dashboard sent to OpenCTI or OpenAEV, the revision sent, the
integration it went through, what the platform answered and who sent it.
``dashboard_id`` ON DELETE CASCADE, ``published_by`` ON DELETE SET NULL, no key
to the integration (the record outlives it).

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent - ``CREATE TABLE IF NOT EXISTS``, guarded foreign keys, an index
  built only when missing. Fresh installs run ``create_all`` + ``stamp head``.
* Lock-safe - created without inline keys; each key is added ``NOT VALID``
  on the empty table under ``set_lock_timeout`` (it locks the referenced
  table) and validated separately. The index is a plain build on an empty table.

Revision ID: d6pub6591006
Revises: d5pck6591005
Create Date: 2026-10-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import create_index_in_place, set_lock_timeout, terminate_lock_holders

revision: str = "d6pub6591006"
down_revision: str | None = "d5pck6591005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "custom_dashboard_publications"

_CREATE = """
CREATE TABLE IF NOT EXISTS custom_dashboard_publications (
    id UUID PRIMARY KEY,
    dashboard_id UUID NOT NULL,
    revision INTEGER NOT NULL,
    integration_id UUID NOT NULL,
    target VARCHAR(20) NOT NULL,
    remote_id VARCHAR(200) NOT NULL,
    remote_url VARCHAR(1000),
    published_by UUID,
    published_at TIMESTAMP WITH TIME ZONE NOT NULL
)
"""

# (constraint name, literal DDL): spelled out, never assembled from variables (S608).
_FOREIGN_KEYS: tuple[tuple[str, str], ...] = (
    (
        "custom_dashboard_publications_dashboard_id_fkey",
        "ALTER TABLE custom_dashboard_publications ADD CONSTRAINT custom_dashboard_publications_dashboard_id_fkey "
        "FOREIGN KEY (dashboard_id) REFERENCES custom_dashboards(id) ON DELETE CASCADE NOT VALID",
    ),
    (
        "custom_dashboard_publications_published_by_fkey",
        "ALTER TABLE custom_dashboard_publications ADD CONSTRAINT custom_dashboard_publications_published_by_fkey "
        "FOREIGN KEY (published_by) REFERENCES users(id) ON DELETE SET NULL NOT VALID",
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
    with op.get_context().autocommit_block():
        create_index_in_place(conn, "ix_custom_dashboard_publications_dashboard_id", _TABLE, "(dashboard_id)")


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    conn.execute(sa.text(f"DROP TABLE IF EXISTS {_TABLE}"))
