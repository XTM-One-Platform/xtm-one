# SPDX-License-Identifier: Apache-2.0

"""ConversationChannelBinding - a messaging thread a conversation continues in.

A conversation keeps the channel it started on (``Conversation.channel`` /
``channel_thread_id``). This table is how the SAME conversation is also
reachable from another channel: a row maps one direct-message thread of a
messaging platform to the conversation its messages continue, so the channel
router finds the conversation from the thread and every turn - web or channel -
lands in one record (history, compaction state, files, approvals, agent and
session tool selection).

One thread continues at most one conversation (unique ``(channel,
channel_thread_id)``): binding a thread again re-points it. A Slack thread is
the one the bot opened in the user's direct messages; a Teams personal chat has
no threads, so binding it re-points the whole chat until ``/new`` or the next
binding. Only direct-message threads are ever bound - a group thread is read by
other people, and the conversation's web turns must never reach them.

``user_id`` is the owner who bound it. The router continues the conversation
only for a sender who resolves to that user AND still owns the conversation; a
binding whose conversation changed hands, or was archived, is ignored.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ConversationChannelBinding(Base):
    """One direct-message thread a conversation is continued in."""

    __tablename__ = "conversation_channel_bindings"
    __table_args__ = (
        UniqueConstraint(
            "channel",
            "channel_thread_id",
            name="uq_conversation_channel_bindings_thread",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # ``ChannelType`` value: slack | teams.
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    # The thread as the router reads it off an inbound message
    # (``InboundMessage.channel_thread_id``): ``<DM channel>:<root ts>`` on
    # Slack, the personal conversation id (``a:...``) on Teams.
    channel_thread_id: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
