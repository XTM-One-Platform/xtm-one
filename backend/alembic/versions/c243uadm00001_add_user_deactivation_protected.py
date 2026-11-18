"""add users.deactivation_protected, the break-glass flag of the user-admin tools

``users.deactivation_protected``: an administrator flags a break-glass or
service account so the agent user-admin tools (``user_admin`` built-in group)
refuse to deactivate it. ``BOOLEAN NOT NULL DEFAULT false``: a constant default
is metadata-only on PG 11+, no rewrite.

Lock-safe recipe both ways (idempotency probe, ``terminate_lock_holders``,
``set_lock_timeout``, guarded DDL).

Revision ID: c243uadm00001
Revises: c122scim00001
Create Date: 2026-11-18 22:30:00.000000
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c243uadm00001"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c122scim00001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_USERS = "users"
_COLUMN = "deactivation_protected"


def _has_column(conn: Any) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"
            ),
            {"t": _USERS, "c": _COLUMN},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn):
        return
    terminate_lock_holders(conn, _USERS)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_USERS} ADD COLUMN IF NOT EXISTS {_COLUMN} BOOLEAN NOT NULL DEFAULT false")


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn):
        return
    terminate_lock_holders(conn, _USERS)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_USERS} DROP COLUMN IF EXISTS {_COLUMN}")
