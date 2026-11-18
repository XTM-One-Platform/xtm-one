# SPDX-License-Identifier: Apache-2.0

"""Platform Health experiments - one measured variant of a finding's target (#8259).

The Quality Analyst drafts a variant (a persona, a skill's content, an
assignment's prompt; level 7's Flow Lab a flow graph) for a finding; the
Experimenter measures it: a dataset of
the target's consented production cases, two sandbox copies (the current
configuration and the variant) evaluated on it with the same model, and the
comparison of the two runs. Nothing here writes the target: the only path to
production is the proposal the Experimenter files from a completed experiment
(``services/health/experiments.py``).

The base text is snapshotted with its version so an experiment whose target a
human edited meanwhile is marked ``stale`` and never filed over that edit.
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: Every proposal type an experiment measures: level 6's text changes, then
#: level 7's flow changes (each type's kind registers in ``experiments``).
CHANGE_TYPES = (
    "persona",
    "skill",
    "assignment_prompt",
    "flow_repair",
    "flow_optimization",
    "new_flow",
    "agent_to_flow",
)

#: Drafted by the Quality Analyst, not launched.
STATUS_DRAFT = "draft"
#: The evaluation runs are on their way.
STATUS_RUNNING = "running"
#: Both runs completed: ``result`` holds the comparison.
STATUS_COMPLETED = "completed"
#: A run stopped before the end (budget reached, opt-out mid-run): no verdict.
STATUS_INCOMPLETE = "incomplete"
#: Not launched (no budget, no dataset, evaluation unavailable): ``status_reason`` says why.
STATUS_SKIPPED = "skipped"
#: The target changed after the draft: never filed over the human edit.
STATUS_STALE = "stale"
STATUS_FAILED = "failed"

EXPERIMENT_STATUSES = (
    STATUS_DRAFT,
    STATUS_RUNNING,
    STATUS_COMPLETED,
    STATUS_INCOMPLETE,
    STATUS_SKIPPED,
    STATUS_STALE,
    STATUS_FAILED,
)
OPEN_STATUSES = (STATUS_DRAFT, STATUS_RUNNING)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def _now() -> datetime:
    return datetime.now(UTC)


class HealthExperiment(Base):
    __tablename__ = "health_experiments"
    __table_args__ = (
        CheckConstraint(_in("status", EXPERIMENT_STATUSES), name="ck_concierge_experiments_status_valid"),
        CheckConstraint(_in("change_type", CHANGE_TYPES), name="ck_concierge_experiments_change_type_valid"),
        Index("ix_concierge_experiments_finding", "finding_id", "created_at"),
        Index("ix_concierge_experiments_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    finding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("health_findings.id", ondelete="CASCADE"), nullable=False
    )
    change_type: Mapped[str] = mapped_column(String(32), nullable=False)
    #: What the variant changes: the agent (persona), the skill, the assignment.
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    #: The agent the evaluation runs (the target itself for a persona); ``None``
    #: for a kind whose subjects are flows.
    subject_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    base_text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=sa.text("''"))
    #: The target's version the base was read at (persona or skill version); ``None``
    #: where the target has no versions (an assignment prompt: compared by text).
    base_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    variant_text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=sa.text("''"))
    #: A structured variant, owned by its kind (a flow's ``{"sandbox_flow_id", "graph"}``).
    variant: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb"))
    variant_summary: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=sa.text("''"))

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_DRAFT)
    #: A short generic code (``budget``, ``no_dataset``, ...); the detail goes to the logs.
    status_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    dataset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    evaluation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    baseline_copy_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    variant_copy_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    baseline_run_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list, server_default=sa.text("'{}'")
    )
    variant_run_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list, server_default=sa.text("'{}'")
    )
    placebo_comparison_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    #: Cases, trials, the per-case token estimate and the budget read at launch.
    sizing: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb"))
    #: Quality metric, baseline, candidate, delta, verdict, cost and latency deltas,
    #: simulated writes (total and per case).
    result: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb"))

    proposal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    author_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        onupdate=_now,
        server_default=sa.text("now()"),
    )
    launched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
