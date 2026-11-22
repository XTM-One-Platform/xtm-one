"""add the canonical asset store of the organization context (#1178, S2)

Three new tables: ``organization_assets`` (one row per real asset),
``organization_asset_sources`` (one row per source record) and
``organization_asset_software`` (one row per vendor, product and version on an
asset). Design: ``dev-docs/asset-model.md`` section 9.

Nothing is backfilled here (rule 4, no unbounded backfill): the store builds an
organization's canonical assets from its profile rows on its first read
(``services/organization_asset_store.ensure_built``), so existing profiles keep
working unchanged and old replicas during a rolling deploy ignore the tables.

Lock profile: the foreign keys to ``organizations`` install triggers there, so
its holders are cleared and the lock timeout set before each guarded
``CREATE TABLE``; the child tables reference the new parent only.

Revision ID: o1178assets01
Revises: h129healthcheckreports01
Create Date: 2026-11-22 13:10:00.000000
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "o1178assets01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "h129healthcheckreports01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ASSETS = "organization_assets"
_SOURCES = "organization_asset_sources"
_SOFTWARE = "organization_asset_software"


def _has_table(conn: sa.engine.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def _org_fk() -> sa.Column[Any]:
    return sa.Column(
        "organization_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )


def _asset_fk() -> sa.Column[Any]:
    return sa.Column(
        "asset_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(f"{_ASSETS}.id", ondelete="CASCADE"),
        nullable=False,
    )


def _jsonb(name: str, default: str) -> sa.Column[Any]:
    return sa.Column(name, postgresql.JSONB(), nullable=False, server_default=sa.text(f"'{default}'::jsonb"))


def upgrade() -> None:
    conn = op.get_bind()
    # Idempotency probe: fresh installs run create_all + stamp head, and a crashed replica may retry.
    if _has_table(conn, _ASSETS) and _has_table(conn, _SOURCES) and _has_table(conn, _SOFTWARE):
        return
    # The foreign keys to `organizations` install their triggers there: clear the holders first.
    terminate_lock_holders(conn, "organizations")
    set_lock_timeout(conn)
    op.create_table(
        _ASSETS,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _org_fk(),
        sa.Column("canonical_key", sa.String(64), nullable=False),
        sa.Column("name", sa.String(300), nullable=False, server_default=""),
        sa.Column("name_key", sa.String(300), nullable=False, server_default=""),
        _jsonb("fields", "{}"),
        _jsonb("merged_on", "[]"),
        _jsonb("possible_matches", "[]"),
        sa.Column("profile_version", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("organization_id", "canonical_key", name="uq_organization_assets_org_key"),
        if_not_exists=True,
    )
    op.create_index("ix_organization_assets_org_name", _ASSETS, ["organization_id", "name_key"], if_not_exists=True)
    op.create_table(
        _SOURCES,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _org_fk(),
        _asset_fk(),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        _jsonb("identifiers", "{}"),
        _jsonb("fields", "{}"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("organization_id", "source", "external_id", name="uq_organization_asset_sources_org_ref"),
        if_not_exists=True,
    )
    op.create_index("ix_organization_asset_sources_asset", _SOURCES, ["asset_id"], if_not_exists=True)
    op.create_table(
        _SOFTWARE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _org_fk(),
        _asset_fk(),
        sa.Column("vendor", sa.String(200), nullable=False, server_default=""),
        sa.Column("product", sa.String(200), nullable=False),
        sa.Column("vendor_key", sa.String(200), nullable=False, server_default=""),
        sa.Column("product_key", sa.String(200), nullable=False),
        sa.Column("version", sa.String(100), nullable=False, server_default="unknown"),
        sa.Column("cpe", sa.String(300), nullable=False, server_default=""),
        sa.Column("purl", sa.String(500), nullable=False, server_default=""),
        sa.Column("install_date", sa.String(40), nullable=False, server_default=""),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        _jsonb("sources", "[]"),
        sa.UniqueConstraint(
            "asset_id", "vendor_key", "product_key", "version", name="uq_organization_asset_software_entry"
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ix_organization_asset_software_org_product",
        _SOFTWARE,
        ["organization_id", "product_key", "version"],
        if_not_exists=True,
    )


def downgrade() -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    for table in (_SOFTWARE, _SOURCES, _ASSETS):
        if _has_table(conn, table):
            op.drop_table(table, if_exists=True)
