# SPDX-License-Identifier: Apache-2.0

"""Agent delegation model – unified audit trail for all inter-agent interactions.

Tracks every delegation between agents: consultations (synchronous ask),
transfers (permanent handoff), and background tasks (async fire-and-forget).
Replaces the narrower ``sub_agent_tasks`` model with a single source of truth.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AgentDelegation(Base):
    """A delegation from one agent to another (consult, transfer, or background task)."""

    __tablename__ = "agent_delegations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # The agent that initiated the delegation
    parent_agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The agent that was delegated to
    target_agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Delegation type: consult | transfer | background_task
    type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        index=True,
    )

    # Optional conversation context
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Optional message that triggered this delegation
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Delegation details
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        index=True,
    )  # pending | running | completed | failed | incomplete

    # Result from the target agent
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Duration in milliseconds (computed on completion)
    duration_ms: Mapped[int | None] = mapped_column(nullable=True)

    # Metadata (e.g. handover reason, source context)
    metadata_json: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=dict
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
