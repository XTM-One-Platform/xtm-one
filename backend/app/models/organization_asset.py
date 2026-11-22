# SPDX-License-Identifier: Apache-2.0

"""The canonical asset store of the organization context (#1178, S2).

The identity engine (``services/organization_asset_identity.py``, S1) turns
what several sources say about one machine into one canonical thing. These
three tables keep its answer, so an organization can hold far more assets than
the profile JSON (capped at 5,000 rows) and an asset can carry hundreds of
software entries:

- ``organization_assets``: one row per real asset, keyed per organization on
  the engine's ``canonical_key`` (a hash of its first record, stable whatever
  the order of the records), with the fields as the precedence shows them and
  every source's value (``fields``), why its records were merged and the weak
  matches left apart.
- ``organization_asset_sources``: one row per source record (source and its id
  there), unique per organization, so one source record belongs to one asset.
- ``organization_asset_software``: one row per vendor, product and version on
  an asset, with its sources and first and last seen; an unknown version is
  ``unknown``, never empty. ``(organization_id, product_key, version)`` answers
  "which assets run this software" without reading the assets.

Design: ``dev-docs/asset-model.md`` section 9.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _now() -> datetime:
    return datetime.now(UTC)


class OrganizationAsset(Base):
    __tablename__ = "organization_assets"
    __table_args__ = (
        UniqueConstraint("organization_id", "canonical_key", name="uq_organization_assets_org_key"),
        Index("ix_organization_assets_org_name", "organization_id", "name_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    #: ``organization_asset_identity.canonical_id`` of its records.
    canonical_key: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The name shown (the precedence's pick) and its lower-case form for the search.
    name: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    name_key: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    #: field -> ``{value, source, external_id, candidates}`` (``FieldValue.as_dict``).
    fields: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: ``[{record, kind, value}]``: how each record after the first joined.
    merged_on: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    #: ``[{kind, value, with}]``: weak-key matches with other assets, never merged.
    possible_matches: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    #: The profile version the row was built from (``OrganizationContextVersion.version``; 0: none).
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()")
    )


class OrganizationAssetSource(Base):
    __tablename__ = "organization_asset_sources"
    __table_args__ = (
        UniqueConstraint("organization_id", "source", "external_id", name="uq_organization_asset_sources_org_ref"),
        Index("ix_organization_asset_sources_asset", "asset_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization_assets.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    #: kind -> raw values, as the source sent them.
    identifiers: Mapped[dict[str, list[str]]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: The attributes as this source sends them.
    fields: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OrganizationAssetSoftware(Base):
    __tablename__ = "organization_asset_software"
    __table_args__ = (
        UniqueConstraint(
            "asset_id", "vendor_key", "product_key", "version", name="uq_organization_asset_software_entry"
        ),
        Index("ix_organization_asset_software_org_product", "organization_id", "product_key", "version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization_assets.id", ondelete="CASCADE"), nullable=False
    )
    vendor: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    product: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Case-folded vendor and product: the keys a lookup and the unique constraint read.
    vendor_key: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    product_key: Mapped[str] = mapped_column(String(200), nullable=False)
    #: The version, ``unknown`` when no source knows it (never empty).
    version: Mapped[str] = mapped_column(String(100), nullable=False, default="unknown")
    cpe: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    purl: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    install_date: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The sources that report this entry on the asset.
    sources: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
