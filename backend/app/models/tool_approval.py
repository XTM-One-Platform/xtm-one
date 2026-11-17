# SPDX-License-Identifier: Apache-2.0

"""Models backing per-tool human-in-the-loop approval.

Two tables live here:

``hitl_whitelist_overrides``
    The administrator overlay on top of the shipped default whitelist. One row
    per tool, recording whether an administrator explicitly exempted it or
    explicitly re-gated it.

``tool_approval_preferences``
    Per-user standing approvals, created when a reviewer answers "Yes, always"
    to an approval prompt.

Neither table foreign-keys ``tool_key`` to anything: tools come from four
different sources (built-in constants, ``custom_tools`` rows, integration
provider modules, MCP servers) with no single table to reference. Integrity is
maintained by the fingerprint and by resolving against the live tool catalogue,
not by the database.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class HitlWhitelistOverride(Base):
    """An administrator's explicit decision about one tool's approval gating.

    This is an *overlay*, not a materialised list. Effective membership is
    computed as ``(shipped_default - removals) | additions`` at read time, which
    is what allows a deliberate removal to survive a later release that puts the
    tool back into the shipped default. Storing the resolved list instead would
    force a merge on every upgrade, and any merge rule either discards operator
    intent or silently re-exempts a tool somebody chose to gate.
    """

    __tablename__ = "hitl_whitelist_overrides"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Canonical tool name as resolved by ``CompositeToolExecutor`` — aliases and
    # reordered variants have already collapsed to this by the time the gate
    # runs, so overrides key on the same string the gate will look up.
    #
    # UNIQUE: one decision per tool. Re-adding a tool that is currently
    # overridden to "remove" flips this row rather than inserting a second one,
    # so two administrators editing concurrently converge instead of leaving an
    # ambiguous pair of rows.
    tool_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    # "add" = exempt this tool from approval; "remove" = require approval even
    # though the shipped default exempts it.
    action: Mapped[str] = mapped_column(String(10), nullable=False)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    # Optional operator note, surfaced in the admin list so a later reader can
    # see why a tool was exempted without digging through the audit log.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ToolApprovalPreference(Base):
    """One user's standing "Yes, always" approval for one tool.

    Scoped to ``(user_id, tool_key)`` deliberately — not to an agent or
    assignment. The preference follows the user across every run carrying their
    identity, scheduled ones included, but never applies to anyone else.

    Revocation is a soft delete so the audit trail survives it, and so the
    reason a tool started prompting again ("you revoked it" vs "the tool
    changed") remains answerable.
    """

    __tablename__ = "tool_approval_preferences"
    __table_args__ = (
        # One ACTIVE preference per user per tool. Partial on ``revoked_at`` so
        # revoked rows accumulate as history without blocking a fresh grant.
        # This is also what lets two concurrent "Yes, always" clicks converge
        # via upsert instead of racing to insert duplicates.
        Index(
            "uq_tool_approval_preferences_active",
            "user_id",
            "tool_key",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("ix_tool_approval_preferences_user_active", "user_id", "revoked_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # CASCADE: a deleted user's preferences go with them, which is also how the
    # spec's "user is deleted" case resolves — the tool simply gates again for
    # whoever reviews next.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    tool_key: Mapped[str] = mapped_column(String(255), nullable=False)

    # ``ToolDefinition.source`` captured at grant time, for display and audit.
    tool_source: Mapped[str] = mapped_column(String(100), nullable=False)

    # ``ToolDefinition.entity_id`` captured at grant time: the connection the
    # approved tool came from. An MCP tool's ``tool_source`` is
    # ``mcp:<server name>``, a name the server's owner chooses, so the gate
    # covers an MCP server's tool only by an approval whose id is its server's.
    # NULL for a tool that belongs to no connection, and for grants
    # made before it was recorded: an MCP server's tool then asks once more.
    tool_entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Digest of the tool's input schema when consent was given. A preference is
    # consent to a tool's *shape*, not to its name: if the schema changes
    # materially the consent no longer describes what would run, so the tool
    # gates again rather than inheriting stale approval.
    schema_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    # Set when the grant happened while an administrator was impersonating this
    # user, so the audit trail names both identities rather than attributing an
    # admin's action solely to the impersonated account.
    granted_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # NULL = active.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # "user_revoked" | "tool_changed" — distinguishes a deliberate revocation
    # from automatic invalidation by a schema change.
    revoked_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
