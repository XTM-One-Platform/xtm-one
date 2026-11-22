"""add SBOM import to the canonical asset store (#1178)

One new table, ``organization_asset_sboms``: an SBOM (CycloneDX or SPDX JSON)
imported on an asset, anchored on one of its source records, applied by every
rebuild of the store as source ``sbom``. One new column on
``organization_asset_software``: ``bundled_in``, the component that bundles an
entry (an SBOM's nesting). Design: ``dev-docs/asset-model.md`` section 9.4.

Additive, nothing backfilled. Lock profile: the new table's foreign keys to
``organizations`` and ``users`` install triggers there, so their holders are
cleared and the lock timeout set before the guarded ``CREATE TABLE``; the new
column is nullable-free with a constant server default (catalogue-only on
PostgreSQL 11+), added under the lock timeout.

Revision ID: o1178assetsbom01
Revises: o1178assetdecisions01
Create Date: 2026-11-22 15:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "o1178assetsbom01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "o1178assetdecisions01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "organization_asset_sboms"
_SOFTWARE = "organization_asset_software"
_COLUMN = "bundled_in"


def _has_table(conn: sa.engine.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def _has_column(conn: sa.engine.Connection, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    # Idempotency probes: fresh installs run create_all + stamp head, and a crashed replica may retry.
    if _has_table(conn, _SOFTWARE) and not _has_column(conn, _SOFTWARE, _COLUMN):
        terminate_lock_holders(conn, _SOFTWARE)
        set_lock_timeout(conn)
        op.add_column(_SOFTWARE, sa.Column(_COLUMN, sa.String(300), nullable=False, server_default=""))
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
        sa.Column("anchor_source", sa.String(80), nullable=False),
        sa.Column("anchor_external_id", sa.String(200), nullable=False),
        sa.Column("document_key", sa.String(300), nullable=False),
        sa.Column("format", sa.String(20), nullable=False),
        sa.Column("file_name", sa.String(300), nullable=False, server_default=""),
        sa.Column("components", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column(
            "author_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "organization_id",
            "anchor_source",
            "anchor_external_id",
            "document_key",
            name="uq_organization_asset_sboms_doc",
        ),
        if_not_exists=True,
    )


def downgrade() -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    if _has_table(conn, _TABLE):
        op.drop_table(_TABLE, if_exists=True)
    if _has_table(conn, _SOFTWARE) and _has_column(conn, _SOFTWARE, _COLUMN):
        op.drop_column(_SOFTWARE, _COLUMN)
