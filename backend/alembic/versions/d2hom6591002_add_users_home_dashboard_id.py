"""Add users.home_dashboard_id: a custom dashboard chosen as one's dashboard.

A person may show one of their own custom dashboards (built by their agents)
on ``/dashboard`` instead of the platform's dashboard. One nullable column,
its FK (``ON DELETE SET NULL``: deleting the dashboard brings back the
platform's) and its index; every existing user stays NULL.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent - ``ADD COLUMN IF NOT EXISTS``, guarded FK add, an index built
  only when missing (``create_index_in_place``). Fresh installs run ``create_all`` + ``stamp
  head``; existing installs run the body once, and a crashed replica may retry.
* Lock-safe - the ``ADD COLUMN`` is nullable with no default (metadata-only)
  and follows the canonical recipe: idempotency probe, ``terminate_lock_holders``
  pre-flight, ``set_lock_timeout`` belt. The FK is added ``NOT VALID`` and
  validated separately under ShareUpdateExclusive. The index is a plain build
  (``create_index_in_place``, never ``CONCURRENTLY``: it would wait on the old
  pods' transactions under the migration lock) of a column that is all NULL.

``custom_dashboards.user_id`` already points the other way, so the model marks
this side ``use_alter`` and this migration adds it after both tables exist.

Revision ID: d2hom6591002
Revises: d1cdb6591001
Create Date: 2026-10-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import create_index_in_place, set_lock_timeout, terminate_lock_holders

revision: str = "d2hom6591002"
down_revision: str | None = "d1cdb6591001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FK_NAME = "fk_users_home_dashboard_id_custom_dashboards"
_INDEX_NAME = "ix_users_home_dashboard_id"


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c"),
            {"t": table, "c": column},
        ).scalar()
    )


def _constraint_exists(conn, name: str) -> bool:
    return bool(conn.execute(sa.text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": name}).scalar())


def upgrade() -> None:
    conn = op.get_bind()

    if not _column_exists(conn, "users", "home_dashboard_id"):
        terminate_lock_holders(conn, "users")
        set_lock_timeout(conn)
        conn.execute(sa.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS home_dashboard_id UUID"))

    if not _constraint_exists(conn, _FK_NAME):
        terminate_lock_holders(conn, "users")
        terminate_lock_holders(conn, "custom_dashboards")
        set_lock_timeout(conn)
        conn.execute(
            sa.text(
                f"ALTER TABLE users ADD CONSTRAINT {_FK_NAME} "
                "FOREIGN KEY (home_dashboard_id) REFERENCES custom_dashboards (id) "
                "ON DELETE SET NULL NOT VALID"
            )
        )
    set_lock_timeout(conn)
    conn.execute(sa.text(f"ALTER TABLE users VALIDATE CONSTRAINT {_FK_NAME}"))

    with op.get_context().autocommit_block():
        create_index_in_place(conn, _INDEX_NAME, "users", "(home_dashboard_id)")


def downgrade() -> None:
    conn = op.get_bind()
    if _column_exists(conn, "users", "home_dashboard_id"):
        # Dropping the column drops its index and its FK with it.
        terminate_lock_holders(conn, "users")
        set_lock_timeout(conn)
        conn.execute(sa.text(f"ALTER TABLE users DROP CONSTRAINT IF EXISTS {_FK_NAME}"))
        conn.execute(sa.text("ALTER TABLE users DROP COLUMN IF EXISTS home_dashboard_id"))
