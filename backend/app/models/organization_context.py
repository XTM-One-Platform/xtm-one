# SPDX-License-Identifier: Apache-2.0

"""OrganizationContextVersion model - the organization profile every agent reads.

One row per saved version of an organization's profile (sector,
regions, crown jewels, software, AI systems, PIRs, risk appetite, ROI
rate, ticketing target: the contract of ``dev-docs/ctem-fleets.md`` section 6,
validated by ``services/organization_context.py``). A save never edits a row:
it inserts the next ``version`` of that organization (``organization_id``,
``models/organization.py``; numbers are per organization since #9157), so
the current profile is its highest version and every earlier one stays
readable. ``source`` says who wrote it:
the admin form, a drafting agent, the API, or the accepted proposals of a
draft with AI (``ai_draft``, #9484).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: Who wrote a version.
ORGANIZATION_CONTEXT_SOURCES: tuple[str, ...] = ("form", "draft", "api", "ai_draft")


class OrganizationContextVersion(Base):
    __tablename__ = "organization_context_versions"
    __table_args__ = (
        CheckConstraint(
            "source IN ('form', 'draft', 'api', 'ai_draft')",
            name="ck_organization_context_versions_source_valid",
        ),
        Index(
            "uq_organization_context_versions_org_version",
            "organization_id",
            "version",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: The organization this profile describes (the primary or a tracked one).
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE", name="fk_organization_context_versions_organization_id"),
        nullable=False,
    )
    #: 1, 2, 3... per organization: the highest is its current profile.
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The validated profile (``services.organization_context.OrganizationProfile``).
    profile: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="form", server_default=text("'form'"))
    #: The accepted proposals' provenance of an ``ai_draft`` save: field -> ``{source, basis}`` (#9484).
    sources: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), server_default=text("now()")
    )


#: What a pending proposal asks: field changes of a profile, or a new tracked organization.
ORGANIZATION_PROPOSAL_KINDS: tuple[str, ...] = ("fields", "new_tracked")
#: Where a proposal stands; only an administrator's decision moves it out of ``pending``.
ORGANIZATION_PROPOSAL_STATUSES: tuple[str, ...] = ("pending", "accepted", "rejected")


class OrganizationContextProposal(Base):
    """A change proposed from chat, waiting for an administrator's review (#9484, S-a2).

    ``propose_organization_context_change`` stores it; it never writes the
    profile. Accepting it in Settings > Organization is the profile ``PUT``
    with source ``ai_draft`` (or the creation of the tracked organization,
    monitoring ``off``), then the decision recorded here.
    """

    __tablename__ = "organization_context_proposals"
    __table_args__ = (
        CheckConstraint("kind IN ('fields', 'new_tracked')", name="ck_organization_context_proposals_kind_valid"),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected')", name="ck_organization_context_proposals_status_valid"
        ),
        Index("ix_organization_context_proposals_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: The organization whose profile changes; ``None`` for a new tracked organization.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE", name="fk_organization_context_proposals_organization_id"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="fields")
    #: ``fields``: ``[{field, value, basis}]``; ``new_tracked``: ``[{name, relationship, criticality, ...}]``.
    proposals: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    #: The profile version the proposal was made on (re-diffed when it is older than the current one).
    based_on_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default=text("'pending'"))
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: The chat conversation it came from (a link on the page; no foreign key: a deleted chat keeps the proposal).
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), server_default=text("now()")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


#: How often a sync runs.
ORGANIZATION_SYNC_FREQUENCIES: tuple[str, ...] = ("hourly", "daily", "weekly")


class OrganizationContextSync(Base):
    """A saved import of one profile section from one platform, run on a schedule (#11913).

    The import of ``services/organization_context_import.py`` with its source,
    filters, batch type and removal policy kept, so the scheduler tick runs it
    again. Section ``all`` is the global schedule: it runs every other enabled
    sync in order. A run reads with the token of ``owner_user_id`` (the
    administrator who saved it); a run that cannot read for that reason sets
    ``paused_reason`` and the sync stops until it is saved again.
    """

    __tablename__ = "organization_context_syncs"
    __table_args__ = (
        CheckConstraint(
            "frequency IN ('hourly', 'daily', 'weekly')", name="ck_organization_context_syncs_frequency_valid"
        ),
        CheckConstraint("direction IN ('inbound', 'outbound')", name="ck_organization_context_syncs_direction_valid"),
        Index("ix_organization_context_syncs_due", "enabled", "next_run_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: A section of the import (assets, asset_groups, ai_systems, software, crown_jewels, pirs) or ``all``.
    section: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The platform it reads (empty for ``all``).
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="", server_default=text("''"))
    filters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    batch_type: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default=text("''"))
    #: Remove the rows this source wrote before that it no longer returns: a deletion upstream deletes the
    #: row here (default since #1134; a truncated or failed read removes nothing).
    remove_gone: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    #: The source ids an administrator excluded from this sync (#1134): never imported, removed if present.
    excluded_external_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    #: ``inbound`` (the platform is the master, its rows are read-only here) or ``outbound`` (the XTM One
    #: list is the master: the rows this sync created on the platform follow it, #1134 S3).
    direction: Mapped[str] = mapped_column(
        String(16), nullable=False, default="inbound", server_default=text("'inbound'")
    )
    #: Outbound: the platform rows this sync created, keyed on the XTM One row (its name, case-folded).
    #: Only these are updated and deleted; the platform's other rows are never touched.
    outbound_ids: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    frequency: Mapped[str] = mapped_column(String(16), nullable=False, default="daily")
    #: Hour and minute of the run in ``timezone`` (the hour is ignored hourly).
    hour: Mapped[int] = mapped_column(Integer, nullable=False, default=6, server_default=text("6"))
    minute: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    #: 0 Monday ... 6 Sunday, weekly only.
    weekday: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC", server_default=text("'UTC'"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    #: Why the sync stopped by itself (a missing or revoked token); empty while it runs.
    paused_reason: Mapped[str] = mapped_column(String(500), nullable=False, default="", server_default=text("''"))
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The last run: ``{added, updated, removed, version, error}``.
    last_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), server_default=text("now()")
    )
