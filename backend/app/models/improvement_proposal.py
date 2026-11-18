# SPDX-License-Identifier: Apache-2.0

"""Improvement proposals - a typed, evidenced change to a production target,
approved by the people who manage it, watched, and rolled back on regression
(#1783, PRD ``platform-concierge.md`` section 6.10).

The persona self-improvement loop (#1044) is the ``persona`` type: its rows
were migrated here from ``persona_improvements``. The other types are filed by
Platform Health's Experimenter (``propose_improvement``).

Lifecycle::

    pending_approval -> rejected
                     -> applying -> shadow (agent_to_flow only) -> watching
                                 -> watching -> kept | rolled_back | regression_kept | failed
                     -> failed (the target or the experiment is gone)

The target is polymorphic (``target_type`` + ``target_id``, no key). Only
``agent_id``, the agent the target belongs to when it has one, is a key
(``ON DELETE CASCADE``: an agent deleted mid-loop takes its proposals with it,
as in #1044); a proposal on a deleted skill, assignment or flow fails at its
next decision or settlement. The evaluation, the runs, the users and the author
agent are recorded by id without a key, which keeps the write-hot evaluation
tables out of the migration and keeps the record when one of them goes.

At most one open proposal changes a target at a time
(``uq_improvement_proposals_open_target``; ``new_flow`` changes no existing
target and is exempt), and one open proposal per dedup key
(``uq_improvement_proposals_open_dedup``).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# --- Types --------------------------------------------------------------------

TYPE_PERSONA = "persona"
TYPE_SKILL = "skill"
TYPE_ASSIGNMENT_PROMPT = "assignment_prompt"
TYPE_FLOW_REPAIR = "flow_repair"
TYPE_FLOW_OPTIMIZATION = "flow_optimization"
TYPE_NEW_FLOW = "new_flow"
TYPE_AGENT_TO_FLOW = "agent_to_flow"
PROPOSAL_TYPES = (
    TYPE_PERSONA,
    TYPE_SKILL,
    TYPE_ASSIGNMENT_PROMPT,
    TYPE_FLOW_REPAIR,
    TYPE_FLOW_OPTIMIZATION,
    TYPE_NEW_FLOW,
    TYPE_AGENT_TO_FLOW,
)

TARGET_AGENT = "agent"
TARGET_SKILL = "skill"
TARGET_ASSIGNMENT = "assignment"
TARGET_FLOW = "flow"
TARGET_TYPES = (TARGET_AGENT, TARGET_SKILL, TARGET_ASSIGNMENT, TARGET_FLOW)

#: The target type each proposal type changes.
TARGET_OF: dict[str, str] = {
    TYPE_PERSONA: TARGET_AGENT,
    TYPE_SKILL: TARGET_SKILL,
    TYPE_ASSIGNMENT_PROMPT: TARGET_ASSIGNMENT,
    TYPE_FLOW_REPAIR: TARGET_FLOW,
    TYPE_FLOW_OPTIMIZATION: TARGET_FLOW,
    # A new flow automates what an agent or an assignment was observed doing:
    # that agent's or assignment's managers approve it.
    TYPE_NEW_FLOW: TARGET_AGENT,
    TYPE_AGENT_TO_FLOW: TARGET_ASSIGNMENT,
}

# --- Statuses -----------------------------------------------------------------

#: Waiting for someone who may manage the target to approve or reject it.
STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_REJECTED = "rejected"
#: Approved; the change is being written (an applier that works in steps).
STATUS_APPLYING = "applying"
#: Approved ``agent_to_flow``: the flow runs beside the assignment, writes intercepted.
STATUS_SHADOW = "shadow"
#: Applied; the re-evaluation and the live metrics decide whether it stays.
STATUS_WATCHING = "watching"
#: The watch did not regress beyond the margin: the change stays.
STATUS_KEPT = "kept"
#: The watch regressed: the target went back to what it was, as a new version.
STATUS_ROLLED_BACK = "rolled_back"
#: It regressed, but a human edited the target meanwhile: that edit stays.
STATUS_REGRESSION_KEPT = "regression_kept"
#: No verdict could be reached (the run failed, the target is gone, applying failed).
STATUS_FAILED = "failed"

OPEN_STATUSES = (STATUS_PENDING_APPROVAL, STATUS_APPLYING, STATUS_SHADOW, STATUS_WATCHING)
PROPOSAL_STATUSES = (
    STATUS_PENDING_APPROVAL,
    STATUS_REJECTED,
    STATUS_APPLYING,
    STATUS_SHADOW,
    STATUS_WATCHING,
    STATUS_KEPT,
    STATUS_ROLLED_BACK,
    STATUS_REGRESSION_KEPT,
    STATUS_FAILED,
)

#: The verdict of the experiment a proposal was filed with: a comparison
#: verdict (``evaluation_comparison``), or ``unmeasured`` - never blank for a
#: proposal an agent filed, so a change nobody measured never reads as measured.
EXPERIMENT_UNMEASURED = "unmeasured"
EXPERIMENT_VERDICTS = ("progression", "neutral", "regression", EXPERIMENT_UNMEASURED)

#: How long an applied change is watched before it is kept (decision B18).
DEFAULT_WATCH_DAYS = 7


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


_OPEN_PREDICATE = _in("status", OPEN_STATUSES)


def _now() -> datetime:
    return datetime.now(UTC)



class ImprovementProposal(Base):
    __tablename__ = "improvement_proposals"
    __table_args__ = (
        CheckConstraint(_in("status", PROPOSAL_STATUSES), name="ck_improvement_proposals_status_valid"),
        CheckConstraint(_in("proposal_type", PROPOSAL_TYPES), name="ck_improvement_proposals_type_valid"),
        CheckConstraint(_in("target_type", TARGET_TYPES), name="ck_improvement_proposals_target_type_valid"),
        Index("ix_improvement_proposals_target_created", "target_type", "target_id", "created_at"),
        Index("ix_improvement_proposals_agent_created", "agent_id", "created_at"),
        Index("ix_improvement_proposals_status", "status"),
        Index(
            "uq_improvement_proposals_open_target",
            "target_type",
            "target_id",
            unique=True,
            postgresql_where=sa.text(f"{_OPEN_PREDICATE} AND proposal_type <> '{TYPE_NEW_FLOW}'"),
        ),
        Index(
            "uq_improvement_proposals_open_dedup",
            "dedup_key",
            unique=True,
            postgresql_where=sa.text(_OPEN_PREDICATE),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    proposal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    #: The agent the target belongs to (the persona's agent, the assignment's
    #: agent), when it has one.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=STATUS_PENDING_APPROVAL)
    #: sha256 of (type, target, evidence fingerprint): a re-run files no duplicate.
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False)

    # --- The change -----------------------------------------------------------
    #: The text the change starts from (persona, skill content, assignment
    #: prompt), kept verbatim: a rollback restores it even if retention has
    #: pruned its version row since. Empty for a structured change.
    base_content: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=sa.text("''"))
    proposed_content: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=sa.text("''"))
    #: A structured change (a flow graph, the flow an assignment hands over to),
    #: its shape owned by the type's handler.
    change: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )
    #: What the change does and why, in the author's words (platform language, B22).
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_version_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    applied_version_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rollback_version_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: What applying did, for the handler that undoes it (ids created, paused).
    apply_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )

    # --- Finding, evidence, experiment ---------------------------------------
    #: The pattern, the hypothesis, the finding's id and fingerprint (level 6).
    finding: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )
    #: ``[{"kind": "trace" | "rating" | "evaluation_run" | "flow_run" | ..., "id": "..."}]``
    evidence_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa.text("'[]'::jsonb")
    )
    #: ``[{"kind": "sandbox_copy" | "evaluation_run" | "shadow_run" | ..., "id": "..."}]``
    experiment_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa.text("'[]'::jsonb")
    )
    #: The measured result: quality, cost, latency, simulated writes (level 6).
    experiment: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )
    #: ``EXPERIMENT_VERDICTS``; ``NULL`` for a human request (#1044), measured by its watch only.
    experiment_verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # --- The watch's re-evaluation (#1044) -----------------------------------
    evaluation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    baseline_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    candidate_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: ``score`` (LLM judge) or ``f1_score``: the evaluation's quality metric.
    quality_metric: Mapped[str] = mapped_column(
        String(32), nullable=False, default="score", server_default=sa.text("'score'")
    )
    baseline_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    candidate_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: ``progression`` / ``neutral`` / ``regression`` (``evaluation_comparison``).
    verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: Why the proposal ended where it did, for the people reading its history.
    outcome_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Who and when ---------------------------------------------------------
    #: The agent that filed it (the Experimenter); ``NULL`` for a human request.
    author_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

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
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: When the shadow phase ends (``agent_to_flow``), then when the watch does.
    shadow_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    watch_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
