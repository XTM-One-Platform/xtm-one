"""add groups.ctem_access and groups.ctem_notify (#1748)

The CTEM right a group grants its members: ``view`` reads the organization's
system signals and cases, ``decide`` also acts on them and answers the case
gates; ``ctem_notify`` says whether ``decide`` members are notified of the
gates. Nullable string with no default (no right) and a boolean defaulting to
true: catalogue-only, no backfill.

Revision ID: c1748ctemgrp01
Revises: x1735noailabel01
Create Date: 2026-11-24 07:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c1748ctemgrp01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "x1735noailabel01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("ctem_access", "VARCHAR(20)"),
    ("ctem_notify", "BOOLEAN NOT NULL DEFAULT true"),
)


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("groups"):
        return
    existing = {c["name"] for c in inspector.get_columns("groups")}
    missing = [(name, kind) for name, kind in _COLUMNS if name not in existing]
    if not missing:
        return
    terminate_lock_holders(conn, "groups")
    set_lock_timeout(conn)
    for name, kind in missing:
        op.execute(f"ALTER TABLE groups ADD COLUMN IF NOT EXISTS {name} {kind}")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("groups"):
        return
    existing = {c["name"] for c in inspector.get_columns("groups")}
    present = [name for name, _ in _COLUMNS if name in existing]
    if not present:
        return
    terminate_lock_holders(conn, "groups")
    set_lock_timeout(conn)
    for name in present:
        op.execute(f"ALTER TABLE groups DROP COLUMN IF EXISTS {name}")
