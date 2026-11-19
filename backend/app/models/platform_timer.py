# SPDX-License-Identifier: Apache-2.0

"""A timer a run arms for a date: the platform raises ``timer.due`` at that time.

Written by ``services/platform_timers.py`` (the coordination tools
``arm_timer``, ``cancel_timer`` and ``list_timers``), fired by the scheduler
tick. A run that must act later on one object (a thread unanswered after three
business days, a calendar entry at T-60) arms one timer per object instead of
a cron that polls every object.

* One armed timer per (agent, ``correlation_key``), held by a partial unique
  index: arming the same key again replaces the earlier timer.
* ``unless_work_item_id`` / ``unless_shared_state_key``: the timer is skipped,
  never fired, when that work item is done or cancelled, or that shared-state
  entry is resolved, by its due time.
* The row lives in Postgres, so a rollout between arm and due loses nothing.
  The state is written ``fired`` before the event is raised: a timer fires at
  most once.
* ``fired_unheard``: fired while no ``platform_event`` assignment took
  ``timer.due`` for its key, so no run started: the follow-up it stood for is
  visible as lost instead of reading like any fired timer.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.utils import CORRELATION_KEY_MAX_LENGTH

TIMER_ARMED = "armed"
TIMER_FIRED = "fired"
TIMER_CANCELLED = "cancelled"
TIMER_REPLACED = "replaced"
TIMER_SKIPPED = "skipped"
#: Fired, but no ``platform_event`` assignment took its ``timer.due``: nothing ran.
TIMER_FIRED_UNHEARD = "fired_unheard"
TIMER_STATES: tuple[str, ...] = (
    TIMER_ARMED,
    TIMER_FIRED,
    TIMER_FIRED_UNHEARD,
    TIMER_CANCELLED,
    TIMER_REPLACED,
    TIMER_SKIPPED,
)


class PlatformTimer(Base):
    __tablename__ = "platform_timers"
    __table_args__ = (
        Index(
            "uq_platform_timers_armed_key",
            "agent_id",
            "correlation_key",
            unique=True,
            postgresql_where=text("state = 'armed'"),
        ),
        Index("ix_platform_timers_state_due", "state", "due_at"),
        Index("ix_platform_timers_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: The person the arming run acted for: the event is a personal object of theirs.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: The agent that armed it: the event's participant, so ``scope: own`` hears it.
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    #: The run that armed it, when one did (shown on the run page and in the payload).
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    correlation_key: Mapped[str] = mapped_column(String(CORRELATION_KEY_MAX_LENGTH), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    unless_work_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    unless_shared_state_key: Mapped[str | None] = mapped_column(String(CORRELATION_KEY_MAX_LENGTH), nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default=TIMER_ARMED)
    #: Why it ended without firing (skipped, cancelled, replaced), or what it raised.
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
