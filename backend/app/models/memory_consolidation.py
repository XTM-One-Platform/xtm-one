# SPDX-License-Identifier: Apache-2.0

"""Memory Steward consolidations (Platform Health, #8258).

A consolidation pass works on ONE memory lane: an agent's own pool
(``user_id`` NULL) or one user's pool with that agent. Each change it applies
is one :class:`MemoryConsolidation` row, so the owner of the lane can see it
and undo it. Nothing is ever moved across lanes.

The changed entries stay in ``agent_memory_entries``, flagged superseded (the
flag the prompt block already leaves out), for ``RETENTION_DAYS``; the purge
then deletes them with the row. A promoted entry is a new active entry whose
undo deletes it.

:class:`MemoryLaneCheckpoint` remembers what a lane looked like at its last
pass, so a pass over an unchanged lane does nothing (no model call), and how
often each medium-term observation survived a re-consolidation (promotion).

:class:`MemoryConsolidationTombstone` keeps the fingerprint of a change its
owner undid, or of a promotion whose entry they deleted, after the row goes:
a pass never makes that change again.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: The operations a pass applies. ``flag`` changes nothing: a contradiction
#: the rule could not settle, listed for the lane's owner.
CONSOLIDATION_OPERATIONS = ("merge", "promote", "demote", "remove", "flag")


class MemoryConsolidation(Base):
    """One change a consolidation pass made to one memory lane."""

    __tablename__ = "memory_consolidations"
    __table_args__ = (
        Index("ix_memory_consolidations_lane", "agent_id", "user_id", "applied_at"),
        Index(
            "ix_memory_consolidations_user_applied",
            "user_id",
            "applied_at",
            postgresql_where=text("user_id IS NOT NULL"),
        ),
        # ``held_by_consolidation``: is this entry kept for an undo?
        Index("ix_memory_consolidations_before", "before_entry_ids", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    # NULL: the agent's own lane; else the user whose lane with the agent it is.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    operation: Mapped[str] = mapped_column(String(20), nullable=False)
    # Why: ``duplicate``, ``contradiction``, ``stale``, ``recurring``.
    reason: Mapped[str] = mapped_column(String(30), nullable=False)
    # The entries the operation flagged superseded (``flag``: the two it
    # could not settle). Ids only: the content stays in its own table, so a
    # deletion there leaves nothing behind here.
    before_entry_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    # The entry kept (merge, remove), created (promote), or NULL (demote).
    after_entry_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # The operation's identity in its lane: an undone operation is not
    # applied again by a later pass.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MemoryLaneCheckpoint(Base):
    """What one lane looked like at its last applied pass."""

    __tablename__ = "memory_lane_checkpoints"

    # ``<agent_id>:<user_id>`` or ``<agent_id>:agent``: one row per lane,
    # NULL user included.
    lane_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # Medium-term observation digest -> {"seen": n, "summary": digest of the
    # summary it was last seen in}. Digests only, never text.
    observations: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # A lane larger than one pass weighs is read in windows: where the next
    # pass starts among its older entries. 0: the lane was weighed whole.
    cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class MemoryConsolidationTombstone(Base):
    """A change a pass must never make again in its lane. A fingerprint only:
    no text, no entry id; it goes with the lane's purge."""

    __tablename__ = "memory_consolidation_tombstones"

    lane_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
