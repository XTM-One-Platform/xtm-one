"""add groups.auto_join (#1901)

Whether people who sign in through single sign-on (OIDC or SAML) join the
group automatically at every sign-in. A boolean defaulting to false:
catalogue-only, no backfill.

Revision ID: a1901autojoin01
Revises: r1844noaisys01
Create Date: 2026-11-24 14:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "a1901autojoin01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "r1844noaisys01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("groups"):
        return
    if "auto_join" in {c["name"] for c in inspector.get_columns("groups")}:
        return
    terminate_lock_holders(conn, "groups")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE groups ADD COLUMN IF NOT EXISTS auto_join BOOLEAN NOT NULL DEFAULT false")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("groups"):
        return
    if "auto_join" not in {c["name"] for c in inspector.get_columns("groups")}:
        return
    terminate_lock_holders(conn, "groups")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE groups DROP COLUMN IF EXISTS auto_join")
