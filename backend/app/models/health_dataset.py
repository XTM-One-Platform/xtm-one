# SPDX-License-Identifier: Apache-2.0

"""Where a Platform Health-built dataset entry came from (#8257, PRD 6.7).

One row per entry Platform Health built from production (``services/health/datasets.py``):
the case kind, the source it was read from, and the tool calls that source
recorded, which a sandbox run replays (level 2). An opt-out, a revoked opt-in
or a narrowed scope finds the entries to remove through these columns.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

CASE_TO_FIX = "to_fix"
CASE_NON_REGRESSION = "non_regression"

SOURCE_MESSAGE = "message"
SOURCE_ASSIGNMENT_RUN = "assignment_run"
SOURCE_FLOW_RUN = "flow_run"


class HealthDatasetEntrySource(Base):
    __tablename__ = "health_dataset_entry_sources"
    __table_args__ = (
        CheckConstraint("case_kind IN ('to_fix', 'non_regression')", name="ck_health_entry_sources_case_kind"),
        CheckConstraint(
            "source_kind IN ('message', 'assignment_run', 'flow_run')", name="ck_health_entry_sources_source_kind"
        ),
        # One entry per source per dataset: a rebuild appends only what is new.
        UniqueConstraint("dataset_id", "source_kind", "source_id", name="uq_health_entry_sources_source"),
        Index("ix_health_entry_sources_user", "source_user_id"),
        Index("ix_health_entry_sources_rater", "source_rater_id"),
        Index("ix_health_entry_sources_agent", "source_agent_id"),
        Index("ix_health_entry_sources_flow", "source_flow_id"),
        Index("ix_health_entry_sources_node_agents", "source_node_agent_ids", postgresql_using="gin"),
        Index("ix_health_entry_sources_node_flows", "source_node_flow_ids", postgresql_using="gin"),
    )

    entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dataset_entries.id", ondelete="CASCADE"), primary_key=True
    )
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False
    )
    case_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    # The message, assignment run or flow run the entry was built from. No
    # foreign keys: the entry outlives the source's retention, and these ids are
    # what a revocation matches, never what a read follows.
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_trace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_rating_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Whose content it is: the conversation's or the run's user, and the rater.
    source_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_rater_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_flow_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # A flow run's agent nodes: their content is in the entry, so each agent's
    # consent was read and its revocation removes the entry.
    source_node_agent_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list, server_default="{}"
    )
    # The flows its ``flow.call`` nodes called, at any depth: the same rule.
    source_node_flow_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list, server_default="{}"
    )
    # ``dataclasses.asdict`` of level 2's ``RecordedCall``, in recorded order.
    recorded_calls: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), server_default=func.now()
    )
