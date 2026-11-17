# SPDX-License-Identifier: Apache-2.0

"""Platform Concierge cycles, signals and schedules (#8261).

Level 1's ``concierge_cycles`` is the budget unit; these tables hold what
level 8 adds around it: the run a cycle started and its digest
(:class:`ConciergeCycleRun`), the edge-triggered state of every signal
(:class:`ConciergeSignalState`) and when each fleet-wide schedule last fired
(:class:`ConciergeScheduleMark`). Read and written only by
``services/concierge/cycles.py``, ``signals.py`` and ``digest.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ConciergeCycleRun(Base):
    """The assignment run a cycle started, who asked for it, and its digest."""

    __tablename__ = "concierge_cycle_runs"
    __table_args__ = (Index("ix_concierge_cycle_runs_target", "target_type", "target_id"),)

    cycle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("concierge_cycles.id", ondelete="CASCADE"), primary_key=True
    )
    # No foreign keys below: a deleted agent, assignment or run leaves the
    # digest readable, and the cycle never locks the busy run tables.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    assignment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    # Copied from the cycle so the per-target reads need no join.
    target_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: The :class:`~app.services.concierge.signals.SignalHit` that started it, as JSON.
    signal: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    #: The person whose on-demand request started it. No foreign key: one would
    #: lock ``users`` for nothing the code needs; a deleted account reads as nobody.
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: :class:`~app.services.concierge.digest.CycleDigest`, built once the cycle closed.
    digest: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    digest_built_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), server_default=func.now()
    )


class ConciergeSignalState(Base):
    """One signal on one target: whether it is crossed, and what it last read.

    Signals are edge-triggered: a crossing (``active`` false -> true) starts a
    cycle, staying crossed starts nothing, clearing re-arms it.
    """

    __tablename__ = "concierge_signal_states"
    __table_args__ = (
        UniqueConstraint("target_type", "target_id", "kind", name="uq_concierge_signal_states_target_kind"),
        Index("ix_concierge_signal_states_active", "active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # ``agent`` | ``flow`` | ``assignment``.
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # ``signals.SignalKind``.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    #: What the last evaluation read (value, baseline, counts; the model id for ``model_change``).
    last_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The cycle the last crossing started, ``None`` when it was refused.
    cycle_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: Why the last crossing started no cycle (``cycles.CycleRefusal``).
    refusal: Mapped[str | None] = mapped_column(String(32), nullable=True)
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), server_default=func.now()
    )


class ConciergeScheduleMark(Base):
    """When a fleet-wide schedule last fired, and what came of it."""

    __tablename__ = "concierge_schedule_marks"

    # ``cycles.ScheduleKey``: ``memory`` | ``quality`` | ``flows`` | ``digest``.
    schedule: Mapped[str] = mapped_column(String(16), primary_key=True)
    fired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cycle_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: Why the last firing started no cycle, ``None`` when it started one.
    refusal: Mapped[str | None] = mapped_column(String(32), nullable=True)
