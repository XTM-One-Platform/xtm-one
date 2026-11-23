# SPDX-License-Identifier: Apache-2.0

"""The asset records of the organization context: the source of truth of the Inventory (#1543).

Until #1543 the assets and asset groups lived as two lists inside the profile
JSON (``assets_exposure`` capped at 5,000 rows, ``asset_groups``), and every
write copied the whole profile and rebuilt the canonical store. These tables
hold them row by row, so an organization can hold a million assets and a
write touches the rows it names:

- ``organization_asset_records``: one record from one source (a person, a CSV,
  an API, a sync: Intune, Defender, OpenAEV, OpenCTI...), unique per
  organization on ``(source, external_id)``. A hand row gets a generated
  ``external_id``. ``asset_id`` names the canonical asset
  (``organization_assets``) the identity engine put it in.
- ``organization_asset_identifiers``: the normalised identity keys of a record
  (serial, device id, MAC, FQDN, hostname, cloud resource id, IP), indexed, so
  the incremental matcher finds the records sharing a key without reading the
  others.
- ``organization_asset_groups`` and ``organization_asset_group_members``: the
  groups and which records join them.
- ``organization_crown_jewel_links``: a crown jewel (a profile row, a short
  list) linked to an asset record or a group.
- ``organization_asset_changes``: one row per write (create, update, delete,
  restore, import), who, when, from where, and the row before and after: the
  audit trail and what a restore replays.

Design: ``dev-docs/asset-model.md`` section 10.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _now() -> datetime:
    return datetime.now(UTC)


def _org_fk() -> Any:
    return ForeignKey("organizations.id", ondelete="CASCADE")


class OrganizationAssetRecord(Base):
    """One record of one asset from one source; its fields are the ``AssetEntry`` of the profile."""

    __tablename__ = "organization_asset_records"
    __table_args__ = (
        UniqueConstraint("organization_id", "source", "external_id", name="uq_organization_asset_records_ref"),
        Index("ix_organization_asset_records_org_name", "organization_id", "name_key", "id"),
        Index("ix_organization_asset_records_org_sync", "organization_id", "sync_id"),
        Index("ix_organization_asset_records_asset", "asset_id"),
        Index("ix_organization_asset_records_org_group", "organization_id", "group_key"),
        Index("ix_organization_asset_records_org_category", "organization_id", "category", "criticality"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _org_fk(), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False, default="manual")
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    #: The sync that last wrote the record (empty: typed, a one-off import or an agent).
    sync_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    #: The canonical asset the identity engine put the record in (``None`` until matched).
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization_assets.id", ondelete="SET NULL"), nullable=True
    )
    #: The columns the list, the facets and the filters read, copied from ``fields``.
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    name_key: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False, default="generic_asset")
    criticality: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    environment: Mapped[str] = mapped_column(String(20), nullable=False, default="", server_default="")
    internet_facing: Mapped[str] = mapped_column(String(10), nullable=False, default="unknown")
    owner: Mapped[str] = mapped_column(String(200), nullable=False, default="", server_default="")
    #: The group's name, lower-cased (empty: no group).
    group_key: Mapped[str] = mapped_column(String(200), nullable=False, default="", server_default="")
    #: The whole ``AssetEntry`` value but ``source``, ``external_id`` and ``sync_id`` (``software`` included).
    fields: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Raised by every write: ``If-Match`` on a row write answers 409 when it moved.
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="", server_default="")
    updated_by: Mapped[str] = mapped_column(String(120), nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()")
    )


class OrganizationAssetIdentifier(Base):
    """One normalised identity key of a record (``organization_asset_identity.normalize_identifier``)."""

    __tablename__ = "organization_asset_identifiers"
    __table_args__ = (
        Index("ix_organization_asset_identifiers_key", "organization_id", "kind", "value"),
        Index("ix_organization_asset_identifiers_record", "record_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _org_fk(), nullable=False)
    record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization_asset_records.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    value: Mapped[str] = mapped_column(String(300), nullable=False)


class OrganizationAssetGroup(Base):
    """A group of assets (OpenAEV's static asset group); the ``AssetGroupEntry`` of the profile."""

    __tablename__ = "organization_asset_groups"
    __table_args__ = (
        UniqueConstraint("organization_id", "source", "external_id", name="uq_organization_asset_groups_ref"),
        Index("ix_organization_asset_groups_org_name", "organization_id", "name_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _org_fk(), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False, default="manual")
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    sync_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    name_key: Mapped[str] = mapped_column(String(200), nullable=False)
    #: The whole ``AssetGroupEntry`` value but ``source``, ``external_id`` and ``sync_id``.
    fields: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="", server_default="")
    updated_by: Mapped[str] = mapped_column(String(120), nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()")
    )


class OrganizationAssetGroupMember(Base):
    """A record in a group, many to many, with who says so (a person, a group sync, the record's ``group``).

    Keyed on the source record, not the canonical asset: a record keeps its
    memberships when the identity engine moves it to another asset, and the
    group's assets are the distinct ``asset_id`` of its records. The primary
    key ``(group_id, record_id)`` serves the per-group member count and the
    per-group keyset member list; ``(record_id)`` the groups of one record.
    """

    __tablename__ = "organization_asset_group_members"
    __table_args__ = (
        Index("ix_organization_asset_group_members_record", "record_id"),
        Index("ix_organization_asset_group_members_sync", "group_id", "sync_id"),
    )

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization_asset_groups.id", ondelete="CASCADE"), primary_key=True
    )
    record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization_asset_records.id", ondelete="CASCADE"), primary_key=True
    )
    #: Who put the record in the group: ``record`` (its ``group`` field), ``manual``, or a group source.
    source: Mapped[str] = mapped_column(String(80), nullable=False, default="record", server_default="record")
    #: The sync that last wrote the membership (empty: not a sync).
    sync_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()")
    )


class OrganizationCrownJewelLink(Base):
    """A crown jewel (by its name, lower-cased) running on an asset record or a group."""

    __tablename__ = "organization_crown_jewel_links"
    __table_args__ = (
        Index("ix_organization_crown_jewel_links_jewel", "organization_id", "crown_jewel_key"),
        Index("ix_organization_crown_jewel_links_record", "record_id"),
        Index("ix_organization_crown_jewel_links_group", "group_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _org_fk(), nullable=False)
    crown_jewel_key: Mapped[str] = mapped_column(String(200), nullable=False)
    record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization_asset_records.id", ondelete="CASCADE"), nullable=True
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization_asset_groups.id", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()")
    )


class OrganizationAssetImport(Base):
    """When an organization's profile rows (``assets_exposure``, ``asset_groups``) moved into the records.

    The migration creates the tables and moves nothing (no unbounded backfill):
    the first touch of an organization imports its rows under the organization's
    advisory lock and writes this row, so the import runs once.
    """

    __tablename__ = "organization_asset_imports"

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _org_fk(), primary_key=True)
    #: The profile version the rows were read from (0: no profile).
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()")
    )


class OrganizationAssetChange(Base):
    """One row write: who, when, from where, and the row before and after (the audit trail, what a restore replays)."""

    __tablename__ = "organization_asset_changes"
    __table_args__ = (
        Index("ix_organization_asset_changes_org_at", "organization_id", "at"),
        Index("ix_organization_asset_changes_entity_at", "entity_id", "at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _org_fk(), nullable=False)
    #: ``record``, ``group`` or ``link``.
    entity: Mapped[str] = mapped_column(String(10), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    #: ``create``, ``update``, ``delete``, ``restore`` or ``import``.
    op: Mapped[str] = mapped_column(String(10), nullable=False)
    #: ``form``, ``api``, ``agent``, ``import``, ``sync:<id>`` or ``profile``.
    origin: Mapped[str] = mapped_column(String(80), nullable=False, default="", server_default="")
    #: Who wrote it: a user id, ``agent:<id>`` or a sync name.
    actor: Mapped[str] = mapped_column(String(120), nullable=False, default="", server_default="")
    #: The row before (``None`` on a create) and after (``None`` on a delete); an update keeps the changed fields.
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()")
    )
