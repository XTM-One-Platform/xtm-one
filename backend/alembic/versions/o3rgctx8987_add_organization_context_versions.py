"""add organization_context_versions (#8987)

The organization profile every agent reads (``get_organization_context``):
one row per saved version, the highest ``version`` is the current profile
(``models/organization_context.py``, contract in ``dev-docs/ctem-fleets.md``
section 6).

A new table, created only when missing (idempotent); its indexes are built
in place on the new, empty table, so no CONCURRENTLY is needed. The
downgrade drops it under the lock-safe recipe (``terminate_lock_holders`` +
``set_lock_timeout``).

Revision ID: o3rgctx8987
Revises: l8824lend01
Create Date: 2026-11-05 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "o3rgctx8987"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "l8824lend01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "organization_context_versions"


def _has_table(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": _TABLE},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("profile", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("source", sa.String(16), nullable=False, server_default=sa.text("'form'")),
        sa.Column(
            "author_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version", name="organization_context_versions_version_key"),
        sa.CheckConstraint(
            "source IN ('form', 'draft', 'api')", name="ck_organization_context_versions_source_valid"
        ),
        if_not_exists=True,
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.drop_table(_TABLE, if_exists=True)
