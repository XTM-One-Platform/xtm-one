"""add SCIM 2.0 provisioning (#122)

* ``scim_tokens``: the bearer tokens an identity provider provisions with,
  SHA-256 hashed, revocable.
* ``users.scim_external_id`` / ``groups.scim_external_id``: the identity
  provider's ``externalId``. Nullable, no default: metadata-only on PG 11+,
  no rewrite.

Lock-safe recipe both ways (idempotency probe, ``terminate_lock_holders``,
``set_lock_timeout``, guarded DDL): the new table's foreign key installs its
triggers on ``users`` under a SHARE ROW EXCLUSIVE lock.

Revision ID: c122scim00001
Revises: a105a2arot1
Create Date: 2026-11-18 12:20:00.000000
"""

import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c122scim00001"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "a105a2arot1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "scim_tokens"
_COLUMN = "scim_external_id"
_TABLES_WITH_COLUMN = ("users", "groups")


def _has_table(conn: Any, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def _has_column(conn: Any, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": _COLUMN},
        ).scalar()
    )


def _uuid() -> postgresql.UUID[uuid.UUID]:
    return postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    conn = op.get_bind()
    missing = [table for table in _TABLES_WITH_COLUMN if not _has_column(conn, table)]
    table_missing = not _has_table(conn, _TABLE)
    if not missing and not table_missing:
        return
    for table in missing:
        terminate_lock_holders(conn, table)
    if table_missing and "users" not in missing:
        terminate_lock_holders(conn, "users")
    set_lock_timeout(conn)
    for table in missing:
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {_COLUMN} VARCHAR(255)")
    if table_missing:
        op.create_table(
            _TABLE,
            sa.Column("id", _uuid(), primary_key=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("token_prefix", sa.String(20), nullable=False),
            sa.Column("created_by", _uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            if_not_exists=True,
        )


def downgrade() -> None:
    conn = op.get_bind()
    present = [table for table in _TABLES_WITH_COLUMN if _has_column(conn, table)]
    table_present = _has_table(conn, _TABLE)
    if not present and not table_present:
        return
    for table in present:
        terminate_lock_holders(conn, table)
    if table_present and "users" not in present:
        terminate_lock_holders(conn, "users")
    set_lock_timeout(conn)
    if table_present:
        op.drop_table(_TABLE, if_exists=True)
    for table in present:
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {_COLUMN}")
