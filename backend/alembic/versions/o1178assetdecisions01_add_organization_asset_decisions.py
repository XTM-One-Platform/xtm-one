"""add the administrator merge and split decisions of the asset store (#1178, S3)

One new table, ``organization_asset_decisions``: an administrator's merge
(the records are one asset) or split (the two records are never one asset),
applied by every rebuild of the canonical asset store. Design:
``dev-docs/asset-model.md`` section 9.2.

Additive, nothing backfilled. Lock profile: the foreign keys to
``organizations`` and ``users`` install triggers there, so their holders are
cleared and the lock timeout set before the guarded ``CREATE TABLE``.

Revision ID: o1178assetdecisions01
Revises: o1178assets01
Create Date: 2026-11-22 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "o1178assetdecisions01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "o1178assets01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "organization_asset_decisions"


def _has_table(conn: sa.engine.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    # Idempotency probe: fresh installs run create_all + stamp head, and a crashed replica may retry.
    if _has_table(conn, _TABLE):
        return
    terminate_lock_holders(conn, "organizations")
    terminate_lock_holders(conn, "users")
    set_lock_timeout(conn)
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("records", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("reason", sa.String(500), nullable=False, server_default=""),
        sa.Column(
            "author_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        if_not_exists=True,
    )
    op.create_index("ix_organization_asset_decisions_org", _TABLE, ["organization_id"], if_not_exists=True)


def downgrade() -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    if _has_table(conn, _TABLE):
        op.drop_table(_TABLE, if_exists=True)
