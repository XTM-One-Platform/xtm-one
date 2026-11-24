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
- ``organization_asset_software``: one installation, a vendor, product and
  version on an asset, with its sources and first and last seen; an unknown
  version is ``unknown``, never empty. ``(organization_id, product_key,
  version)`` answers "which assets run this software" without reading the
  assets; ``software_version_id`` names the software version it installs.

Software is an entity of its own (#1495), kept across the rebuilds of the store:

- ``organization_software``: one per organization, vendor and product, with its
  type, CPE 2.3 and its resolution state, PURL and sources.
- ``organization_software_versions``: one per software and version.
- ``organization_software_dependencies``: a software version that depends on,
  or is bundled in, another one (an SBOM's ``dependencies`` and nesting).

Design: ``dev-docs/asset-model.md`` sections 6 and 9.
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
    #: The component that bundles this one, ``product version`` (an SBOM's nesting; empty: none).
    bundled_in: Mapped[str] = mapped_column(String(300), nullable=False, default="", server_default="")
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The sources that report this entry on the asset.
    sources: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    #: The software version installed (#1495); set by the software sync of every rebuild.
    software_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization_software_versions.id", ondelete="SET NULL"), nullable=True
    )


class OrganizationSoftware(Base):
    """One software of an organization (#1495): a vendor and product, whatever its versions and assets.

    Convertible to the STIX 2.1 Software SCO (``name``, ``vendor``, ``cpe``,
    ``x_opencti_product``). ``origin`` ``synced``: a source reports it, it is
    read-only to people; ``manual``: an administrator typed it. Kept across the
    rebuilds of the store, so its id is stable.
    """

    __tablename__ = "organization_software"
    __table_args__ = (
        UniqueConstraint("organization_id", "vendor_key", "product_key", name="uq_organization_software_key"),
        Index("ix_organization_software_org_name", "organization_id", "name_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    vendor: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    product: Mapped[str] = mapped_column(String(200), nullable=False)
    vendor_key: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    product_key: Mapped[str] = mapped_column(String(200), nullable=False)
    #: The name shown (``vendor product`` unless set) and its lower-case form for the search.
    name: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    name_key: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    #: ``application``, ``operating_system``, ``library``, ``firmware``, ``container_image`` or ``saas``.
    software_type: Mapped[str] = mapped_column(String(30), nullable=False, default="application")
    description: Mapped[str] = mapped_column(String(2000), nullable=False, default="", server_default="")
    #: The product-level CPE 2.3 (version ``*``) and how it was found (``CPE_CONFIDENCES``).
    cpe: Mapped[str] = mapped_column(String(300), nullable=False, default="", server_default="")
    cpe_state: Mapped[str] = mapped_column(String(20), nullable=False, default="unresolved")
    #: An administrator's CPE, kept across syncs; it wins over every source.
    cpe_override: Mapped[str] = mapped_column(String(300), nullable=False, default="", server_default="")
    purl: Mapped[str] = mapped_column(String(500), nullable=False, default="", server_default="")
    #: ``synced`` (a source reports it) or ``manual`` (typed by an administrator).
    origin: Mapped[str] = mapped_column(String(10), nullable=False, default="synced")
    sources: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()")
    )


class OrganizationSoftwareVersion(Base):
    """One version of a software (#1495); ``unknown`` when no source knows it, never empty."""

    __tablename__ = "organization_software_versions"
    __table_args__ = (UniqueConstraint("software_id", "version", name="uq_organization_software_versions_key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    software_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization_software.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[str] = mapped_column(String(100), nullable=False, default="unknown")
    #: The CPE 2.3 of this version (reported, else the software's with the version set).
    cpe: Mapped[str] = mapped_column(String(300), nullable=False, default="", server_default="")
    purl: Mapped[str] = mapped_column(String(500), nullable=False, default="", server_default="")
    #: End of life (ISO date) when known.
    eol: Mapped[str] = mapped_column(String(40), nullable=False, default="", server_default="")
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OrganizationSoftwareDependency(Base):
    """A software version that needs another (#1495): ``depends_on`` or ``bundled_in``.

    Read from the SBOMs of the assets (CycloneDX ``dependencies`` and nested
    ``components``, SPDX ``DEPENDS_ON`` / ``CONTAINS``); rebuilt with the store.
    """

    __tablename__ = "organization_software_dependencies"
    __table_args__ = (
        UniqueConstraint(
            "version_id", "depends_on_version_id", "kind", name="uq_organization_software_dependencies_edge"
        ),
        Index("ix_organization_software_dependencies_target", "depends_on_version_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization_software_versions.id", ondelete="CASCADE"), nullable=False
    )
    depends_on_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization_software_versions.id", ondelete="CASCADE"), nullable=False
    )
    #: ``depends_on`` or ``bundled_in`` (the version is bundled in the other).
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="depends_on")
    #: The SBOM's scope when it gives one (``required``, ``optional``, ``dev``...).
    scope: Mapped[str] = mapped_column(String(40), nullable=False, default="", server_default="")
    sources: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))


class OrganizationAssetSbom(Base):
    """One SBOM imported on an asset (#1178): its components join the asset's software as source ``sbom``.

    Anchored on a source record of the asset (``anchor_source``, ``anchor_external_id``),
    not on the asset row, so every rebuild of the store (a newer profile, a merge)
    attaches it again to whichever asset holds that record. One row per document
    (``document_key``): importing the same document again replaces its
    components; another document on the same asset adds to them.
    """

    __tablename__ = "organization_asset_sboms"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "anchor_source",
            "anchor_external_id",
            "document_key",
            name="uq_organization_asset_sboms_doc",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    anchor_source: Mapped[str] = mapped_column(String(80), nullable=False)
    anchor_external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    #: What the document describes (its main component or name), else the file name.
    document_key: Mapped[str] = mapped_column(String(300), nullable=False)
    #: ``cyclonedx`` or ``spdx``.
    format: Mapped[str] = mapped_column(String(20), nullable=False)
    file_name: Mapped[str] = mapped_column(String(300), nullable=False, default="", server_default="")
    #: ``[{vendor, product, version, cpe, purl, bundled_in}]``, ``version`` ``unknown`` when the SBOM gives none.
    components: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()")
    )


class OrganizationAssetDecision(Base):
    """An administrator's merge or split of source records (#1178, S3).

    ``merge``: the records are one asset whatever their keys say. ``split``:
    the two records are never one asset. Kept apart from the asset rows, so a
    rebuild of the store (a newer profile) applies them again; removing the
    decision lets the keys decide again.
    """

    __tablename__ = "organization_asset_decisions"
    __table_args__ = (Index("ix_organization_asset_decisions_org", "organization_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    #: ``merge`` or ``split``.
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    #: ``[{source, external_id}]``: two or more for a merge, exactly two for a split.
    records: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    reason: Mapped[str] = mapped_column(String(500), nullable=False, default="", server_default="")
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()")
    )
