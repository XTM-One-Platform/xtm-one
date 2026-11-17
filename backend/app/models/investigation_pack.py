# SPDX-License-Identifier: Apache-2.0

"""Deep Investigation packs built by users (the pack builder).

A built pack is a method: ordered steps, each served by sources, turned into
a `SourcePack` for each caller by `investigation_pack_builder` (the sources
are resolved against what the caller may use, never the author's). It follows
the standard three-tier visibility model, and every save of its definition is
an immutable `InvestigationPackVersion`.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class InvestigationPack(Base):
    """A Deep Investigation pack a user built."""

    __tablename__ = "investigation_packs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Subject types the pack answers (a subset of `SUBJECT_TYPES`).
    subject_types: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Pre-ticked in the picker for its subject types, for everyone who sees it.
    recommended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    max_iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=10, server_default="10")
    # The current definition (`investigation_pack_builder.PackDefinition`)
    # and its version number; every version is also an immutable row below.
    definition: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    # Company-managed packs are shared with all users; only admins can edit.
    # When True, user_id is NULL.
    is_company_managed: Mapped[bool] = mapped_column(Boolean, default=False)
    shared_group_ids: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    shared_group_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    shared_user_ids: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    shared_user_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    access_restricted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class InvestigationPackVersion(Base):
    """One saved definition of a built pack, immutable."""

    __tablename__ = "investigation_pack_versions"
    __table_args__ = (
        UniqueConstraint("pack_id", "version_number", name="uq_investigation_pack_versions_pack_ver"),
        CheckConstraint("version_number > 0", name="ck_investigation_pack_versions_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pack_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigation_packs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict] = mapped_column(JSON, nullable=False)
    subject_types: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    commit_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
