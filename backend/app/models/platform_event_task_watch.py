# SPDX-License-Identifier: Apache-2.0

"""A one-off platform event watch that wakes a background task.

The orchestrator's watch without an assignment: the first platform event that
matches starts one background task of ``agent_id`` with ``task`` as its brief,
then the watch ends. Written by ``services/platform_event_task_watches.py``
(the agent-builder tools ``watch_platform_event``,
``list_platform_event_watches``, ``cancel_platform_event_watch`` and the REST
routes under ``/api/v1/assignments/platform-event-watches``), read by the
platform event dispatcher.

A watch is armed while ``fired_at`` and ``cancelled_at`` are empty and
``expires_at`` is ahead: every watch has a deadline, so none is left armed
forever.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PlatformEventTaskWatch(Base):
    __tablename__ = "platform_event_task_watches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: Who set the watch: the task runs as them, and only their objects' events reach it.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The agent whose background task the event starts (and whose objects' events it receives).
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The agent that set the watch, when an agent did (the task's parent).
    created_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: The background task's brief.
    task: Mapped[str] = mapped_column(Text, nullable=False)
    #: The platform event names it waits for.
    events: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    #: The payload filter (``integration_event_filters`` semantics).
    filter: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fired_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The background task the event started.
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
