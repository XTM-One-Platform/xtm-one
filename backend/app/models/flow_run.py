# SPDX-License-Identifier: Apache-2.0

"""FlowRun + FlowRunStep – execution records for the flow builder.

A **FlowRun** is one execution of a Flow's graph, driven by the ``flow``
orchestration adapter.  The run — not the orchestration execution row — is the
domain record: the execution row only carries scheduling/lease/retry state
(and its ``adapter_state`` is a 50-char column that is wiped on retry), so the
walker's whole cursor lives here in ``state`` and the per-node journal lives
in **FlowRunStep** rows.  That split mirrors how ``AssignmentRun`` relates to
its execution row.

Two deliberate choices:

* ``graph`` is a **frozen copy** taken when the run is created.  A flow can be
  edited or restored to an older version while a run is parked on a delay;
  re-reading ``Flow.graph`` mid-walk would change the meaning of a run that
  already started.  The runs API reports which ``flow_version_number`` ran.
* Steps are keyed by a monotonic ``seq``, not by ``(node_id)`` — ``util.loop``
  legitimately executes the same node several times, so node ids are not
  unique within a run.  ``{{node.output}}`` resolves to the node's most recent
  completed step, which inside a loop body is naturally the current pass.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Run lifecycle.  ``awaiting`` covers every parked state (delay, sub-flow,
# assignment wait) — the precise reason lives in ``state["waiting"]``.
FLOW_RUN_STATUS_PENDING = "pending"
FLOW_RUN_STATUS_RUNNING = "running"
FLOW_RUN_STATUS_AWAITING = "awaiting"
FLOW_RUN_STATUS_COMPLETED = "completed"
FLOW_RUN_STATUS_FAILED = "failed"
FLOW_RUN_STATUS_CANCELLED = "cancelled"
# Held back before it started — today only "this flow needs AI and AI is off"
# (FR-016). Distinct from ``cancelled``, which reports someone's action,
# and from ``failed``, which reports a run that began.
FLOW_RUN_STATUS_SKIPPED = "skipped"
FLOW_RUN_STATUSES = (
    FLOW_RUN_STATUS_PENDING,
    FLOW_RUN_STATUS_RUNNING,
    FLOW_RUN_STATUS_AWAITING,
    FLOW_RUN_STATUS_COMPLETED,
    FLOW_RUN_STATUS_FAILED,
    FLOW_RUN_STATUS_CANCELLED,
    FLOW_RUN_STATUS_SKIPPED,
)
FLOW_RUN_ACTIVE_STATUSES = (
    FLOW_RUN_STATUS_PENDING,
    FLOW_RUN_STATUS_RUNNING,
    FLOW_RUN_STATUS_AWAITING,
)

FLOW_RUN_STEP_STATUSES = ("running", "completed", "failed", "skipped")

# How the run started.  ``flow_call`` marks child runs spawned by a parent's
# `flow.call` node; the other four match the trigger node kinds.
FLOW_RUN_TRIGGER_TYPES = (
    "manual",
    "schedule",
    "webhook",
    "integration_event",
    "flow_call",
)

# `flow.call` nesting cap.  Cycles between flows are allowed by the validator
# (they mirror how agents hand work to each other), so the runner bounds the
# depth instead — the same strategy the agent runtime uses for delegation.
FLOW_CALL_MAX_DEPTH = 5


class FlowRun(Base):
    """One execution of a flow's graph."""

    __tablename__ = "flow_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    flow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The version whose graph was frozen below — display/debug pointer only.
    flow_version_number: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    # Frozen at creation; the walker never re-reads Flow.graph.
    graph: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # Who the run acts as: the flow owner for automatic triggers, the caller
    # for manual runs.  Every node executor resolves entities through this
    # user's visibility, so a shared flow never widens what it can touch.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=FLOW_RUN_STATUS_PENDING, index=True
    )

    trigger_type: Mapped[str] = mapped_column(String(30), nullable=False)
    trigger_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    trigger_context: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # The walker's durable cursor: pending queue, loop states, merge arrivals,
    # park reason.  Written after every step so a crash or park resumes
    # exactly where the walk stopped.  Shape is owned by services/flow_runner.
    state: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # `flow.call` lineage.  ``depth`` is checked against FLOW_CALL_MAX_DEPTH
    # at spawn; the parent linkage lets the child's completion wake the
    # parent's orchestration execution.
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    parent_node_id: Mapped[str | None] = mapped_column(String(80), nullable=True)

    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Visibility snapshot of the flow at trigger time, so run listings follow
    # the same three-tier filter as the flow itself without a join.
    #
    # The snapshot must mirror EVERY tier the flow's own filter applies, not a
    # subset: ``get_visible_run`` re-implements ``user_visible_filter`` in
    # Python over these columns, and any tier missing here is a tier the run
    # journal fails to enforce. It carried only the group tier while flows
    # gained per-user grants and ``access_restricted``, which would
    # have handed every authenticated user the runs of a company flow narrowed
    # to a single group. Permission *tiers* are deliberately absent — the
    # snapshot answers "who may read this journal", never "what may they do",
    # which ``_get_controllable_run`` resolves against the live flow.
    is_company_managed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    shared_group_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # ``none_as_null=True`` on the newer column: a None must persist as SQL
    # NULL, not the JSON ``null`` scalar (see
    # ``tests/test_shared_group_json_null_normalization.py``).
    shared_user_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    access_restricted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','awaiting','completed','failed',"
            "'cancelled','skipped')",
            name="ck_flow_runs_status",
        ),
        CheckConstraint("depth >= 0", name="ck_flow_runs_depth"),
        # The runs panel reads "latest runs of this flow" constantly.
        Index("ix_flow_runs_flow_created", "flow_id", "created_at"),
    )


class FlowRunStep(Base):
    """One node execution inside a run — the journal the UI replays."""

    __tablename__ = "flow_run_steps"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    flow_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Monotonic execution order within the run — the journal's real key.
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    node_id: Mapped[str] = mapped_column(String(80), nullable=False)
    node_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    # Which loop pass produced this step (0 outside loops) — display only.
    iteration: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[str] = mapped_column(String(20), nullable=False)

    # The node's config after {{}} interpolation — what actually executed.
    input: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # The node's declared output variables; what {{node.x}} resolves against.
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Taint: True when any interpolated input came from an untrusted trigger
    # or from another untrusted step.  Untrusted values are quarantine-wrapped
    # when rendered into an agent prompt, exactly like {{trigger.*}} in
    # assignments — without this flag a hop through a node would launder them.
    untrusted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A COMPLETED step whose answer read as an error (an integration lookup
    # answering 404 on a node that did not opt in to failing on it): shown in
    # the journal, never a failure.
    warning: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("flow_run_id", "seq", name="uq_flow_run_steps_run_seq"),
        CheckConstraint(
            "status IN ('running','completed','failed','skipped')",
            name="ck_flow_run_steps_status",
        ),
        CheckConstraint("seq >= 0", name="ck_flow_run_steps_seq"),
    )
