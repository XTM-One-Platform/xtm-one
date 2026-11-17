# SPDX-License-Identifier: Apache-2.0

"""Activity log model – tracks assignment runs, notifications, system events."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Activity(Base):
    """Stores activity feed items (assignment runs, notifications, system events)."""

    __tablename__ = "activities"
    # ``user_id`` and ``created_at`` are indexed inline. PostgreSQL does NOT
    # auto-index FKs, so ``agent_id`` (per-agent activity) and ``activity_type``
    # (feed filtering / activity stats) need explicit indexes.
    __table_args__ = (
        Index("ix_activities_agent_id", "agent_id"),
        Index("ix_activities_activity_type", "activity_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    activity_type: Mapped[str] = mapped_column(String(50), nullable=False)

    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    agent_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
