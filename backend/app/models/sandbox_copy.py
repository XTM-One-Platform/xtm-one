# SPDX-License-Identifier: Apache-2.0

"""Sandbox copies the Platform Health experiments on, and their runs (#8255, PRD 6.6).

A copy is a hidden clone of an agent or a flow (its ``is_sandbox`` column is
set) owned by Platform Health: ``user_id`` NULL, not company-managed, shared with
no one. Every run of it replays the calls recorded in its source and intercepts
its writes (``services/health/sandbox``). ``expires_at`` is when it may be
purged, once no open proposal references it.

``sandbox_runs`` keeps each run's report with its exact counts, so an
experiment states how many of its writes were simulated, per case.
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

SOURCE_AGENT = "agent"
SOURCE_FLOW = "flow"

PURPOSE_EXPERIMENT = "experiment"
#: A flow copy run on a production trigger's payload, beside the real run.
PURPOSE_SHADOW = "shadow"

RUN_RUNNING = "running"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"


def _now() -> datetime:
    return datetime.now(UTC)


class SandboxCopy(Base):
    __tablename__ = "sandbox_copies"
    __table_args__ = (
        CheckConstraint("source_type IN ('agent', 'flow')", name="ck_sandbox_copies_source_type"),
        CheckConstraint("num_nonnulls(copy_agent_id, copy_flow_id) = 1", name="ck_sandbox_copies_one_copy"),
        Index("ix_sandbox_copies_expires_at", "expires_at"),
        Index("ix_sandbox_copies_source", "source_type", "source_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The production agent or flow copied; NULL for a new flow prototype.
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    copy_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=True, unique=True
    )
    copy_flow_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("flows.id", ondelete="CASCADE"), nullable=True, unique=True
    )
    #: What the copy was made for: a finding or proposal reference.
    created_for: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    #: The Platform Health agent that asked for it, when one did (no key: a copy
    #: outlives a re-seeded agent until it is purged).
    created_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: The traces (agent) or flow runs (flow) whose recorded calls a run of the
    #: copy replays when it is given no recordings of its own.
    source_trace_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    source_run_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=sa.func.now()
    )

    @property
    def copy_id(self) -> uuid.UUID:
        """The copied agent's or flow's id."""
        return self.copy_agent_id or self.copy_flow_id  # type: ignore[return-value]


class SandboxRun(Base):
    __tablename__ = "sandbox_runs"
    __table_args__ = (
        CheckConstraint("purpose IN ('experiment', 'shadow')", name="ck_sandbox_runs_purpose"),
        CheckConstraint("status IN ('running', 'completed', 'failed')", name="ck_sandbox_runs_status"),
        Index("ix_sandbox_runs_copy_created", "copy_id", "created_at"),
        Index("ix_sandbox_runs_flow_run_id", "flow_run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    copy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sandbox_copies.id", ondelete="CASCADE"), nullable=False
    )
    #: The case this run measures (an evaluation entry, a replayed run).
    case_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False, default=PURPOSE_EXPERIMENT)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=RUN_RUNNING)
    #: The flow run of a flow copy (no key: the flow run table is write-hot).
    flow_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    trace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: ``SandboxReport.to_dict()``: counts and the bounded call log.
    report: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    simulated_writes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=sa.func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
