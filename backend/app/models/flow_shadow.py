# SPDX-License-Identifier: Apache-2.0

"""FlowShadow + FlowShadowPair - a flow measured on an assignment's live triggers (#8260).

A **shadow** runs a flow beside an assignment: every live run of the
assignment gets a paired flow run on the same event, in level 2's intercepted
mode (no write node executes), and the pair's outputs are compared
(``services/health/flow_comparison``). It serves two purposes:

* ``measure`` - the Experimenter measures a sandbox flow before proposing it;
* ``switch_over`` - an approved ``agent_to_flow`` proposal (decision B12): the
  shadow runs for the configured days, then the flow takes the trigger over
  (flow active, assignment paused) and a live watch rolls it back if it
  regresses.

The shadow row carries that whole lifecycle, so the scheduler step
(``flow_shadow.flow_shadow_tick``) is resumable after any crash. The pair rows
reference runs by id without a key: ``assignment_runs`` is the largest table
of the platform and a key there would lock it at every migration touching it;
a pair whose run is gone reads as ``incomplete``.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

SHADOW_PURPOSE_MEASURE = "measure"
SHADOW_PURPOSE_SWITCH_OVER = "switch_over"
SHADOW_PURPOSES = (SHADOW_PURPOSE_MEASURE, SHADOW_PURPOSE_SWITCH_OVER)

# Lifecycle. ``running`` pairs live runs until ``ends_at``; the settlement
# gives ``held`` / ``not_held`` / ``incomplete`` (a measure shadow ends there).
# A held switch-over shadow goes ``watching`` once the flow took over, then
# ``kept`` / ``rolled_back`` / ``regression_kept``.
SHADOW_STATUS_RUNNING = "running"
SHADOW_STATUS_HELD = "held"
SHADOW_STATUS_NOT_HELD = "not_held"
SHADOW_STATUS_INCOMPLETE = "incomplete"
SHADOW_STATUS_WATCHING = "watching"
SHADOW_STATUS_KEPT = "kept"
SHADOW_STATUS_ROLLED_BACK = "rolled_back"
SHADOW_STATUS_REGRESSION_KEPT = "regression_kept"
SHADOW_STATUS_CANCELLED = "cancelled"
SHADOW_STATUSES = (
    SHADOW_STATUS_RUNNING,
    SHADOW_STATUS_HELD,
    SHADOW_STATUS_NOT_HELD,
    SHADOW_STATUS_INCOMPLETE,
    SHADOW_STATUS_WATCHING,
    SHADOW_STATUS_KEPT,
    SHADOW_STATUS_ROLLED_BACK,
    SHADOW_STATUS_REGRESSION_KEPT,
    SHADOW_STATUS_CANCELLED,
)
SHADOW_OPEN_STATUSES = (SHADOW_STATUS_RUNNING, SHADOW_STATUS_WATCHING)

PAIR_STATUS_PENDING = "pending"
PAIR_STATUS_COMPARED = "compared"
PAIR_STATUS_INCOMPLETE = "incomplete"
#: A run of the assignment no trigger started (a manual run, a re-run): seen,
#: never paired.
PAIR_STATUS_SKIPPED = "skipped"
PAIR_STATUSES = (PAIR_STATUS_PENDING, PAIR_STATUS_COMPARED, PAIR_STATUS_INCOMPLETE, PAIR_STATUS_SKIPPED)


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class FlowShadow(Base):
    """One flow shadowing one assignment."""

    __tablename__ = "flow_shadows"
    __table_args__ = (
        CheckConstraint(f"purpose IN ({_in(SHADOW_PURPOSES)})", name="ck_flow_shadows_purpose"),
        CheckConstraint(f"status IN ({_in(SHADOW_STATUSES)})", name="ck_flow_shadows_status"),
        # One open shadow per assignment: two would pair the same live runs
        # twice and two switch-overs would race for one trigger.
        Index(
            "uq_flow_shadows_open_assignment",
            "assignment_id",
            unique=True,
            postgresql_where=text(f"status IN ({_in(SHADOW_OPEN_STATUSES)})"),
        ),
        Index("ix_flow_shadows_status_ends", "status", "ends_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # SET NULL, not CASCADE: an assignment deleted mid-shadow or mid-watch
    # must still close the shadow and its proposal (``advance_shadow``).
    assignment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assignments.id", ondelete="SET NULL"), nullable=True
    )
    # The sandbox copy (level 2) whose runs shadow the assignment's. Kept
    # while the shadow and its watch last (``retain_sandbox_copy``); SET NULL
    # so a copy purged anyway never takes a watch, and its rollback, with it.
    flow_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("flows.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # The production flow a held switch-over created from the sandbox copy
    # (``flow_id``); NULL before the switch-over and for a measure shadow.
    production_flow_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("flows.id", ondelete="SET NULL"), nullable=True
    )
    # The ``agent_to_flow`` proposal a switch-over shadow serves (level 3's
    # ``improvement_proposals.id``); NULL for a measure shadow.
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False, default=SHADOW_PURPOSE_MEASURE)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=SHADOW_STATUS_RUNNING)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Live runs created after this instant are paired next; advanced past every
    # run the sweep paired, so a run is never paired twice.
    cursor_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    switched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    watch_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # The assignment's live metrics before the switch-over, what the watch
    # compares the flow to: {"runs", "failure_rate", "cost_per_run"}.
    baseline: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # The settlement's figures and, after a watch, its outcome.
    verdict: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # The Platform Health cycle its runs are reserved on and charged to: shadow
    # runs spend Platform Health's own budget line, never only the user's.
    cycle_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # The person who started it (a measure shadow from the platform MCP) or
    # approved the proposal; NULL for one Platform Health started.
    started_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class FlowShadowPair(Base):
    """One live assignment run and the flow run that shadowed it."""

    __tablename__ = "flow_shadow_pairs"
    __table_args__ = (
        UniqueConstraint("shadow_id", "assignment_run_id", name="uq_flow_shadow_pairs_run"),
        CheckConstraint(f"status IN ({_in(PAIR_STATUSES)})", name="ck_flow_shadow_pairs_status"),
        Index("ix_flow_shadow_pairs_shadow_status", "shadow_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shadow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("flow_shadows.id", ondelete="CASCADE"), nullable=False
    )
    assignment_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # NULL when no flow run could be created for the event (counted incomplete).
    flow_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=PAIR_STATUS_PENDING)
    # ``RunComparison.as_dict()`` once both runs ended.
    comparison: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    intercepted_writes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    simulated_writes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # What the flow run holds on the Platform Health budget until its spend is
    # charged: {"cycle_id", "tokens", "cost_usd"}; NULL once charged.
    reservation: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
