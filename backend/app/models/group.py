# SPDX-License-Identifier: Apache-2.0

"""Group and GroupMember models – user groups for entity sharing.

Groups allow admins to organize users and share entities (agents,
integrations, knowledge bases, MCP servers, custom tools, channel bots)
with specific groups instead of all users (company-managed).

A group is admin-managed:
  - Only admins can create, edit, or delete groups
  - Only admins can add/remove members and elect managers
  - Any user can see which groups they belong to
  - A group's managers read its members' AI usage (``services/group_managers.py``)
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import expression

from app.core.database import Base


class Group(Base):
    """A named group of users for entity sharing."""

    __tablename__ = "groups"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Admin who created this group
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Quota configuration (group-level overrides).
    # Same JSON structure as GeneralAssistantConfig.quota_config.
    # NULL = no quotas at group level (inherits from platform).
    quota_config: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    # AI switched off for the group's members: a member of such a group gets no
    # model, whatever other groups they are in (``services/group_ai_access.py``).
    # Nothing set = AI follows the platform.
    ai_disabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=expression.false()
    )

    # The CTEM right the group grants its members (``services/ctem_access.py``,
    # the one reader): ``view`` reads the organization's system signals and
    # cases, ``decide`` also acts on them and answers the case gates. NULL = no
    # CTEM right beyond what the member records and reads in the products.
    ctem_access: Mapped[str | None] = mapped_column(String(20), nullable=True, default=None)

    # With ``decide``: whether the members are notified of the case gates
    # (on by default); off, they decide from the screens with no bell.
    ctem_notify: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=expression.true()
    )

    # What the group IS for the analytics: ``org_unit``, ``access``,
    # ``distribution``, or ``auto`` (not classified: every group starts there,
    # a SAML-created one included) - ``GROUP_KINDS``.
    kind: Mapped[str] = mapped_column(
        String(20), nullable=False, default="auto", server_default="auto"
    )

    # The identity provider's id of the group when SCIM provisions it
    # (``externalId``, ``api/v1/scim.py``); NULL for a group SCIM never saw.
    scim_external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


#: ``Group.kind`` values, ``auto`` the unclassified default.
GROUP_KINDS: tuple[str, ...] = ("org_unit", "access", "distribution", "auto")


class GroupMember(Base):
    """Membership join table linking users to groups."""

    __tablename__ = "group_members"
    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="uq_group_members_group_user"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Admin who added this member (nullable — may be system-added)
    added_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # How this membership was created: "manual" (admin/API), "saml" (SAML sync)
    # or "scim" (SCIM provisioning). SAML sync and SCIM each manage only their
    # own memberships, preserving the others.
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="manual", default="manual"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class GroupManager(Base):
    """A user an admin elected manager of a group.

    Kept apart from ``GroupMember`` on purpose: a manager need not be a member
    (a director over a team they are not in), and the SAML group sync, which
    adds and removes ``saml`` memberships on every login, never touches it.
    """

    __tablename__ = "group_managers"
    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="uq_group_managers_group_user"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
