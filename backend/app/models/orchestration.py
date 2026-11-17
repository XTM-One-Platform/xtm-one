# SPDX-License-Identifier: Apache-2.0

"""Orchestration engine models — durable FSM + transactional outbox.

A single durable state machine backs every long-running multi-step platform
job (assignment dispatch, evaluation runs + episode callbacks, sub-agent
tasks). Each job instance is one ``OrchestrationExecution`` row; side effects
that must survive a crash (resume-from-callback, chaining) are recorded as
``OrchestrationOutboxEvent`` rows and driven by the reconciler.

The execution row is the single source of truth for:
  - scheduling          → ``run_at`` (earliest eligible execution time)
  - in-flight ownership  → ``lease_owner`` / ``lease_expires_at``
  - hard timeouts        → ``deadline_at``
  - retry bookkeeping    → ``attempt_count`` / ``max_attempts``

No Redis scheduling keys, no per-consumer recovery loops, no stale sweeps.
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
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Common-vocabulary lifecycle states. The transition table that governs which
# moves are legal lives in ``services.orchestration_engine._ALLOWED_TRANSITIONS``.
EXECUTION_STATES: tuple[str, ...] = (
    "pending",
    "running",
    "completed",
    "failed",
    "dead_letter",
    "cancelled",
)
TERMINAL_EXECUTION_STATES: frozenset[str] = frozenset(
    {"completed", "failed", "dead_letter", "cancelled"}
)
# The in-flight states as an ``IN`` list: the planner does not use a partial
# index on ``state IN (...)`` (``ix_orchestration_exec_rescore_run_ids``) for
# ``NOT IN (terminal)``, although the CHECK constraint makes them equal.
NON_TERMINAL_EXECUTION_STATES: tuple[str, ...] = tuple(
    s for s in EXECUTION_STATES if s not in TERMINAL_EXECUTION_STATES
)

OUTBOX_STATUSES: tuple[str, ...] = ("pending", "processed", "dead_letter")


class OrchestrationExecution(Base):
    """One durable, leased, retry-able instance of an orchestrated job."""

    __tablename__ = "orchestration_executions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Discriminator routing the row to the adapter that owns its behaviour
    # (e.g. "assignment", "evaluation", "sub_agent").
    adapter: Mapped[str] = mapped_column(String(50), nullable=False)

    # Common-vocabulary FSM state (CHECK-constrained to EXECUTION_STATES).
    state: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
    )

    # Optional adapter-specific sub-state — opaque to the engine, meaningful to
    # the adapter. Some adapters store a short label (e.g. "awaiting_human"),
    # others a small JSON blob (the autonomous adapter persists its cycle
    # counter, last mode, and no-progress streak here), so this is TEXT rather
    # than a bounded VARCHAR: a 50-char cap silently overflowed once the blob
    # grew (see the reconciler StringDataRightTruncationError).
    adapter_state: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Adapter-specific data (run_id, evaluation_id, trigger_reason, ...).
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Earliest time this execution is eligible to be claimed. Replaces the
    # legacy ``delayed_run:*`` Redis keys and SAQ scheduled set.
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    # Hard timeout: once past, only ``dead_letter`` / ``cancelled`` are legal.
    deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # In-flight ownership. A non-null, unexpired lease means a worker is
    # actively running this execution.
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Retry bookkeeping. ``attempt_count`` is incremented on each claim so a
    # repeatedly-crashing worker eventually dead-letters instead of spinning.
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Idempotent creation key (e.g. "assignment_run:<run_id>"). NULL rows are
    # not deduplicated (Postgres treats NULLs as distinct in unique indexes).
    dedup_key: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('pending','running','completed','failed',"
            "'dead_letter','cancelled')",
            name="ck_orchestration_executions_state",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_orchestration_executions_attempt_count",
        ),
        CheckConstraint(
            "max_attempts >= 1",
            name="ck_orchestration_executions_max_attempts",
        ),
        UniqueConstraint(
            "dedup_key",
            name="uq_orchestration_executions_dedup_key",
        ),
        # Hot claim query: pending rows for an adapter ordered by run_at.
        Index(
            "ix_orchestration_executions_due",
            "adapter",
            "run_at",
            postgresql_where=text("state = 'pending'"),
        ),
        Index("ix_orchestration_executions_state", "state"),
        # Lease-expiry sweep only ever scans running rows.
        Index(
            "ix_orchestration_executions_lease",
            "lease_expires_at",
            postgresql_where=text("state = 'running'"),
        ),
        Index("ix_orchestration_executions_deadline", "deadline_at"),
        # Sub-agent per-run / per-conversation concurrency cap
        # (``count_active_sub_agent_executions`` in ``services.sub_agent``):
        # a hot-path ``COUNT(*)`` over in-flight sub-agent rows scoped by a
        # ``payload`` key, also re-run under the advisory lock in
        # ``create_sub_agent_task``. Partial expression indexes (matching the
        # query's ``adapter='sub_agent' AND state IN ('pending','running')``
        # predicate and its ``payload->>'<scope>'`` accessor) keep that count an
        # index scan instead of a sequential scan as the table grows. One
        # index per scope key because the count filters on exactly one per call
        # (``parent_run_id`` takes priority, else ``conversation_id``, else
        # ``cap_scope`` - an evaluated turn's).
        Index(
            "ix_orchestration_exec_sub_agent_parent_run",
            text("(payload ->> 'parent_run_id')"),
            postgresql_where=text(
                "adapter = 'sub_agent' AND state IN ('pending', 'running')"
            ),
        ),
        Index(
            "ix_orchestration_exec_sub_agent_conversation",
            text("(payload ->> 'conversation_id')"),
            postgresql_where=text(
                "adapter = 'sub_agent' AND state IN ('pending', 'running')"
            ),
        ),
        # Only evaluated turns carry a cap_scope: the other rows stay out.
        Index(
            "ix_orchestration_exec_sub_agent_cap_scope",
            text("(payload ->> 'cap_scope')"),
            postgresql_where=text(
                "adapter = 'sub_agent' AND state IN ('pending', 'running') "
                "AND (payload ->> 'cap_scope') IS NOT NULL"
            ),
        ),
        # ``is_rescore_pending_for_run``: every completed run's detail
        # load asks whether an in-flight rescore names it, by JSONB membership on
        # ``payload -> 'run_ids'``.
        Index(
            "ix_orchestration_exec_rescore_run_ids",
            text("(payload -> 'run_ids')"),
            postgresql_using="gin",
            postgresql_where=text(
                "adapter = 'evaluation' AND state IN ('pending', 'running')"
            ),
        ),
    )


class OrchestrationOutboxEvent(Base):
    """A durable, idempotent, at-least-once event the reconciler must drive.

    Written in the SAME transaction as the state change that produced it
    (transactional outbox) so a crash between the state write and the side
    effect can never lose the event. ``(execution_id, event_type, dedup_key)``
    is unique, so re-emitting the same event is a no-op.
    """

    __tablename__ = "orchestration_outbox_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orchestration_executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
    )

    # Backoff gate: the event is invisible to ``claim_outbox_events`` until now.
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','processed','dead_letter')",
            name="ck_orchestration_outbox_events_status",
        ),
        CheckConstraint(
            "attempts >= 0",
            name="ck_orchestration_outbox_events_attempts",
        ),
        CheckConstraint(
            "max_attempts >= 1",
            name="ck_orchestration_outbox_events_max_attempts",
        ),
        UniqueConstraint(
            "execution_id",
            "event_type",
            "dedup_key",
            name="uq_orchestration_outbox_events_dedup",
        ),
        # Hot claim query: pending events that are due.
        Index(
            "ix_orchestration_outbox_events_available",
            "available_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )
