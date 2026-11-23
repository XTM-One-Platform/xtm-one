"""add the asset records of the organization context, the source of truth of the Inventory (#1543)

Seven new tables (``models/organization_asset_record.py``):
``organization_asset_records`` (one record from one source),
``organization_asset_identifiers`` (its normalised identity keys, indexed),
``organization_asset_groups`` and ``organization_asset_group_members``,
``organization_crown_jewel_links``, ``organization_asset_imports`` and
``organization_asset_changes`` (the row change log). Design:
``dev-docs/asset-model.md`` section 10.

Nothing is backfilled here (rule 4, no unbounded backfill): the first touch of
an organization imports its profile rows (``assets_exposure``,
``asset_groups``) into the records under its advisory lock
(``services/organization_asset_records.ensure_records``) and writes its
``organization_asset_imports`` row, so the import runs once. Saved profile
versions are history and are never rewritten; old replicas during a rolling
deploy ignore the tables.

Lock profile: the foreign keys to ``organizations`` and ``organization_assets``
install triggers there, so their holders are cleared and the lock timeout set
before the guarded ``CREATE TABLE``s; the other tables reference new ones only.

Revision ID: r1543assetrecords01
Revises: c1572ctemfixplat1
Create Date: 2026-11-23 14:40:00.000000
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "r1543assetrecords01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c1572ctemfixplat1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RECORDS = "organization_asset_records"
_IDENTIFIERS = "organization_asset_identifiers"
_GROUPS = "organization_asset_groups"
_MEMBERS = "organization_asset_group_members"
_LINKS = "organization_crown_jewel_links"
_IMPORTS = "organization_asset_imports"
_CHANGES = "organization_asset_changes"
_ALL = (_RECORDS, _IDENTIFIERS, _GROUPS, _MEMBERS, _LINKS, _IMPORTS, _CHANGES)


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


def _uuid_pk() -> sa.Column[Any]:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True)


def _text(name: str, size: int, default: str | None = "") -> sa.Column[Any]:
    if default is None:
        return sa.Column(name, sa.String(size), nullable=False)
    return sa.Column(name, sa.String(size), nullable=False, server_default=default)


def _at(name: str) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))


def _jsonb(name: str) -> sa.Column[Any]:
    return sa.Column(name, postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb"))


def _row_audit() -> list[sa.Column[Any]]:
    return [
        sa.Column("row_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        _text("created_by", 120),
        _text("updated_by", 120),
        _at("created_at"),
        _at("updated_at"),
    ]


def upgrade() -> None:
    conn = op.get_bind()
    # Idempotency probe: fresh installs run create_all + stamp head, and a crashed replica may retry.
    if all(_has_table(conn, name) for name in _ALL):
        return
    # The foreign keys to `organizations` and `organization_assets` install their triggers there.
    terminate_lock_holders(conn, "organizations")
    terminate_lock_holders(conn, "organization_assets")
    set_lock_timeout(conn)
    op.create_table(
        _RECORDS,
        _uuid_pk(),
        _org_fk(),
        _text("source", 80, "manual"),
        _text("external_id", 200, None),
        _text("sync_id", 64),
        sa.Column(
            "asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organization_assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _text("name", 200, None),
        _text("name_key", 200, None),
        _text("category", 40, "generic_asset"),
        _text("criticality", 20, "unknown"),
        _text("environment", 20),
        _text("internet_facing", 10, "unknown"),
        _text("owner", 200),
        _text("group_key", 200),
        _jsonb("fields"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        *_row_audit(),
        sa.UniqueConstraint("organization_id", "source", "external_id", name="uq_organization_asset_records_ref"),
        if_not_exists=True,
    )
    for name, columns in (
        ("ix_organization_asset_records_org_name", ["organization_id", "name_key", "id"]),
        ("ix_organization_asset_records_org_sync", ["organization_id", "sync_id"]),
        ("ix_organization_asset_records_asset", ["asset_id"]),
        ("ix_organization_asset_records_org_group", ["organization_id", "group_key"]),
        ("ix_organization_asset_records_org_category", ["organization_id", "category", "criticality"]),
    ):
        op.create_index(name, _RECORDS, columns, if_not_exists=True)
    op.create_table(
        _IDENTIFIERS,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        _org_fk(),
        sa.Column(
            "record_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{_RECORDS}.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _text("kind", 40, None),
        _text("value", 300, None),
        if_not_exists=True,
    )
    op.create_index(
        "ix_organization_asset_identifiers_key", _IDENTIFIERS, ["organization_id", "kind", "value"], if_not_exists=True
    )
    op.create_index("ix_organization_asset_identifiers_record", _IDENTIFIERS, ["record_id"], if_not_exists=True)
    op.create_table(
        _GROUPS,
        _uuid_pk(),
        _org_fk(),
        _text("source", 80, "manual"),
        _text("external_id", 200, None),
        _text("sync_id", 64),
        _text("name", 200, None),
        _text("name_key", 200, None),
        _jsonb("fields"),
        *_row_audit(),
        sa.UniqueConstraint("organization_id", "source", "external_id", name="uq_organization_asset_groups_ref"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_organization_asset_groups_org_name", _GROUPS, ["organization_id", "name_key"], if_not_exists=True
    )
    op.create_table(
        _MEMBERS,
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{_GROUPS}.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "record_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{_RECORDS}.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        _text("source", 80, "record"),
        _text("sync_id", 64),
        _at("created_at"),
        _at("updated_at"),
        if_not_exists=True,
    )
    op.create_index("ix_organization_asset_group_members_record", _MEMBERS, ["record_id"], if_not_exists=True)
    op.create_index("ix_organization_asset_group_members_sync", _MEMBERS, ["group_id", "sync_id"], if_not_exists=True)
    op.create_table(
        _LINKS,
        _uuid_pk(),
        _org_fk(),
        _text("crown_jewel_key", 200, None),
        sa.Column(
            "record_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{_RECORDS}.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{_GROUPS}.id", ondelete="CASCADE"),
            nullable=True,
        ),
        _at("created_at"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_organization_crown_jewel_links_jewel", _LINKS, ["organization_id", "crown_jewel_key"], if_not_exists=True
    )
    op.create_index("ix_organization_crown_jewel_links_record", _LINKS, ["record_id"], if_not_exists=True)
    op.create_index("ix_organization_crown_jewel_links_group", _LINKS, ["group_id"], if_not_exists=True)
    op.create_table(
        _IMPORTS,
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("profile_version", sa.Integer(), nullable=False, server_default=sa.text("0")),
        _at("imported_at"),
        if_not_exists=True,
    )
    op.create_table(
        _CHANGES,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        _org_fk(),
        _text("entity", 10, None),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        _text("op", 10, None),
        _text("origin", 80),
        _text("actor", 120),
        sa.Column("before", postgresql.JSONB(), nullable=True),
        sa.Column("after", postgresql.JSONB(), nullable=True),
        _at("at"),
        if_not_exists=True,
    )
    op.create_index("ix_organization_asset_changes_org_at", _CHANGES, ["organization_id", "at"], if_not_exists=True)
    op.create_index("ix_organization_asset_changes_entity_at", _CHANGES, ["entity_id", "at"], if_not_exists=True)


def downgrade() -> None:
    # The records came from the profile versions, which are never rewritten: dropping the tables
    # leaves every organization on its last saved profile, as before this revision.
    conn = op.get_bind()
    set_lock_timeout(conn)
    for table in (_CHANGES, _IMPORTS, _LINKS, _MEMBERS, _IDENTIFIERS, _GROUPS, _RECORDS):
        if _has_table(conn, table):
            op.drop_table(table, if_exists=True)
