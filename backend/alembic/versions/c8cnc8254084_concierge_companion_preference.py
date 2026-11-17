"""platform concierge: each user's companion (#8261)

Level 8 of the Platform Concierge (#1783): ``users.concierge_companion``,
the user's opt-in and pet (``services/concierge/companion.py``), off when
unset.

Safety contract (``dev-docs/reference/migrations-safety.md``):

* Idempotent - ``ADD COLUMN IF NOT EXISTS``.
* Lock-safe - a nullable column without a default is metadata-only, behind
  ``terminate_lock_holders`` + ``set_lock_timeout`` on ``users``.
* Reversible - the downgrade drops the column; the preferences go with it.

Revision ID: c8cnc8254084
Revises: c8cnc8254083
Create Date: 2026-11-05 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c8cnc8254084"
down_revision: str | Sequence[str] | None = "c8cnc8254083"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    terminate_lock_holders(conn, "users")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS concierge_companion JSON")


def downgrade() -> None:
    conn = op.get_bind()
    terminate_lock_holders(conn, "users")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS concierge_companion")
