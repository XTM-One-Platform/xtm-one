"""add general_assistant_config.skill_ids

The skills the General Assistant carries, rendered as a catalog with
``load_skill`` like an agent's. Nullable JSON with no default: NULL is the
platform's choice (the built-in General Assistant skills), so existing rows
need no backfill and the ``ADD COLUMN`` is metadata-only.

Revision ID: gaskill5901
Revises: copdashlay01
Create Date: 2026-10-17 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "gaskill5901"
down_revision: str | None = "copdashlay01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("general_assistant_config"):
        return
    columns = {c["name"] for c in inspector.get_columns("general_assistant_config")}
    if "skill_ids" in columns:
        return
    terminate_lock_holders(conn, "general_assistant_config")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE general_assistant_config ADD COLUMN IF NOT EXISTS skill_ids JSON")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("general_assistant_config"):
        return
    columns = {c["name"] for c in inspector.get_columns("general_assistant_config")}
    if "skill_ids" not in columns:
        return
    terminate_lock_holders(conn, "general_assistant_config")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE general_assistant_config DROP COLUMN IF EXISTS skill_ids")
