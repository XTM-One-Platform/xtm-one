"""add software as an entity of the canonical asset store (#1495)

Three new tables, ``organization_software`` (one per organization, vendor and
product, with its type, CPE 2.3, its resolution state and the administrator's
override), ``organization_software_versions`` (one per software and version,
``unknown`` when no source gives it) and ``organization_software_dependencies``
(a version that depends on, or is bundled in, another one). One new nullable
column on ``organization_asset_software``: ``software_version_id``, the version
an installation installs. Design: ``dev-docs/asset-model.md`` sections 6 and 9.

Additive, nothing backfilled: the next rebuild of each organization's store
(its next read or profile save) fills the software and links the
installations. Lock profile: the foreign keys of the new tables to
``organizations`` and to each other install triggers there, so their holders
are cleared and the lock timeout set before each guarded ``CREATE TABLE``; the
new column is nullable without a default (catalogue-only), added under the
lock timeout, with its foreign key.

Revision ID: s1495software01
Revises: g1467fleettoolgate1
Create Date: 2026-11-23 11:55:00.000000
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "s1495software01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "g1467fleettoolgate1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOFTWARE = "organization_software"
_VERSIONS = "organization_software_versions"
_EDGES = "organization_software_dependencies"
_INSTALLS = "organization_asset_software"
_COLUMN = "software_version_id"
_FK = "fk_organization_asset_software_version"


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


def _org_fk() -> sa.Column[uuid.UUID]:
    return sa.Column(
        "organization_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )


def _jsonb_list(name: str) -> sa.Column[list[str]]:
    return sa.Column(name, postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb"))


def upgrade() -> None:
    conn = op.get_bind()
    # Idempotency probes: fresh installs run create_all + stamp head, and a crashed replica may retry.
    if not _has_table(conn, _SOFTWARE):
        terminate_lock_holders(conn, "organizations")
        set_lock_timeout(conn)
        op.create_table(
            _SOFTWARE,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            _org_fk(),
            sa.Column("vendor", sa.String(200), nullable=False, server_default=""),
            sa.Column("product", sa.String(200), nullable=False),
            sa.Column("vendor_key", sa.String(200), nullable=False, server_default=""),
            sa.Column("product_key", sa.String(200), nullable=False),
            sa.Column("name", sa.String(300), nullable=False, server_default=""),
            sa.Column("name_key", sa.String(300), nullable=False, server_default=""),
            sa.Column("software_type", sa.String(30), nullable=False, server_default="application"),
            sa.Column("description", sa.String(2000), nullable=False, server_default=""),
            sa.Column("cpe", sa.String(300), nullable=False, server_default=""),
            sa.Column("cpe_state", sa.String(20), nullable=False, server_default="unresolved"),
            sa.Column("cpe_override", sa.String(300), nullable=False, server_default=""),
            sa.Column("purl", sa.String(500), nullable=False, server_default=""),
            sa.Column("origin", sa.String(10), nullable=False, server_default="synced"),
            _jsonb_list("sources"),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.UniqueConstraint("organization_id", "vendor_key", "product_key", name="uq_organization_software_key"),
            if_not_exists=True,
        )
        op.create_index(
            "ix_organization_software_org_name", _SOFTWARE, ["organization_id", "name_key"], if_not_exists=True
        )
    if not _has_table(conn, _VERSIONS):
        terminate_lock_holders(conn, "organizations")
        terminate_lock_holders(conn, _SOFTWARE)
        set_lock_timeout(conn)
        op.create_table(
            _VERSIONS,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            _org_fk(),
            sa.Column(
                "software_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey(f"{_SOFTWARE}.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("version", sa.String(100), nullable=False, server_default="unknown"),
            sa.Column("cpe", sa.String(300), nullable=False, server_default=""),
            sa.Column("purl", sa.String(500), nullable=False, server_default=""),
            sa.Column("eol", sa.String(40), nullable=False, server_default=""),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("software_id", "version", name="uq_organization_software_versions_key"),
            if_not_exists=True,
        )
    if not _has_table(conn, _EDGES):
        terminate_lock_holders(conn, "organizations")
        terminate_lock_holders(conn, _VERSIONS)
        set_lock_timeout(conn)
        op.create_table(
            _EDGES,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            _org_fk(),
            sa.Column(
                "version_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey(f"{_VERSIONS}.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "depends_on_version_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey(f"{_VERSIONS}.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("kind", sa.String(20), nullable=False, server_default="depends_on"),
            sa.Column("scope", sa.String(40), nullable=False, server_default=""),
            _jsonb_list("sources"),
            sa.UniqueConstraint(
                "version_id", "depends_on_version_id", "kind", name="uq_organization_software_dependencies_edge"
            ),
            if_not_exists=True,
        )
        op.create_index(
            "ix_organization_software_dependencies_target", _EDGES, ["depends_on_version_id"], if_not_exists=True
        )
    if _has_table(conn, _INSTALLS) and not _has_column(conn, _INSTALLS, _COLUMN):
        terminate_lock_holders(conn, _INSTALLS)
        terminate_lock_holders(conn, _VERSIONS)
        set_lock_timeout(conn)
        op.add_column(_INSTALLS, sa.Column(_COLUMN, postgresql.UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(_FK, _INSTALLS, _VERSIONS, [_COLUMN], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    if _has_table(conn, _INSTALLS) and _has_column(conn, _INSTALLS, _COLUMN):
        op.execute(sa.text(f"ALTER TABLE {_INSTALLS} DROP CONSTRAINT IF EXISTS {_FK}"))
        op.drop_column(_INSTALLS, _COLUMN)
    for table in (_EDGES, _VERSIONS, _SOFTWARE):
        if _has_table(conn, table):
            op.drop_table(table, if_exists=True)
