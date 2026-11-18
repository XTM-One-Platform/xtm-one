"""add the user deactivation cascade record (#122)

* ``users.deactivated_at`` / ``users.deactivation_source`` / ``users.deprovisioned_at``:
  when and by what the account was deactivated, and whether the identity
  provider deleted it (SCIM DELETE, a soft state). Nullable, no default:
  metadata-only on PG 11+, no rewrite.
* ``user_deactivations``: one row per deactivation episode, the snapshot the
  reactivation restores from and the review list the administrators work.

Lock-safe recipe both ways (idempotency probe, ``terminate_lock_holders``,
``set_lock_timeout``, guarded DDL): the new table's foreign keys install their
triggers on ``users`` under a SHARE ROW EXCLUSIVE lock.

Revision ID: c122deact0001
Revises: a12346a2acred1
Create Date: 2026-11-18 09:40:00.000000
"""

import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c122deact0001"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "a12346a2acred1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_USERS = "users"
_TABLE = "user_deactivations"
_USER_COLUMNS = (
    ("deactivated_at", "TIMESTAMP WITH TIME ZONE"),
    ("deactivation_source", "VARCHAR(32)"),
    ("deprovisioned_at", "TIMESTAMP WITH TIME ZONE"),
)


def _has_table(conn: Any, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def _user_columns(conn: Any) -> set[str]:
    return set(
        conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t"
            ),
            {"t": _USERS},
        ).scalars()
    )


def _uuid() -> postgresql.UUID[uuid.UUID]:
    return postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    conn = op.get_bind()
    missing = [(name, kind) for name, kind in _USER_COLUMNS if name not in _user_columns(conn)]
    table_missing = not _has_table(conn, _TABLE)
    if not missing and not table_missing:
        return
    terminate_lock_holders(conn, _USERS)
    set_lock_timeout(conn)
    for name, kind in missing:
        op.execute(f"ALTER TABLE {_USERS} ADD COLUMN IF NOT EXISTS {name} {kind}")
    if table_missing:
        op.create_table(
            _TABLE,
            sa.Column("id", _uuid(), primary_key=True),
            sa.Column("user_id", _uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source", sa.String(32), nullable=False),
            sa.Column("actor_user_id", _uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("actor_label", sa.String(320), nullable=False),
            sa.Column("successor_user_id", _uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("snapshot", sa.JSON(), nullable=False),
            sa.Column("review_items", sa.JSON(), nullable=False),
            sa.Column("counts", sa.JSON(), nullable=False),
            sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("reactivated_at", sa.DateTime(timezone=True), nullable=True),
            if_not_exists=True,
        )
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_user_deactivations_user_id ON {_TABLE} (user_id)")
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS uq_user_deactivations_open_user ON {_TABLE} (user_id) "
        "WHERE reactivated_at IS NULL"
    )


def downgrade() -> None:
    conn = op.get_bind()
    present = {name for name, _ in _USER_COLUMNS} & _user_columns(conn)
    table_present = _has_table(conn, _TABLE)
    if not present and not table_present:
        return
    terminate_lock_holders(conn, _USERS)
    set_lock_timeout(conn)
    if table_present:
        op.drop_table(_TABLE, if_exists=True)
    for name, _ in _USER_COLUMNS:
        if name in present:
            op.execute(f"ALTER TABLE {_USERS} DROP COLUMN IF EXISTS {name}")
