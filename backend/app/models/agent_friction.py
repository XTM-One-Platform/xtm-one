# SPDX-License-Identifier: Apache-2.0

"""A friction an agent run met: what it struggled with, and what was done about it.

The harness improvement loop as a record instead of a habit. A friction is a
tool error, a retry, an approval pause, a sandbox or runner failure, a
code_interpreter write to a checked-out source file, a resumed turn, a trigger
filter edited by an agent, an event replay, or an observation the agent records
itself (``manual``). Each one is classified (harness, procedure, persona /
prompt / trigger, knowledge, or accepted platform behaviour) and disposed of:
the work item, shared-state entry, skill or prompt edit that carries its fix,
or ``accepted``.

Written by ``services/frictions.py`` (the coordination tools
``record_friction``, ``list_frictions``, ``dispose_friction``). Within one run
a friction is unique by (kind, tool, evidence hash): the same error met again
raises ``count`` instead of adding a row.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: What produced the friction. ``manual`` is the agent's own observation.
FRICTION_KINDS: tuple[str, ...] = (
    "tool_error",
    "retry",
    "approval_pause",
    "sandbox_failure",
    "runner_failure",
    "code_interpreter_source_write",
    "trigger_filter_hand_edit",
    "event_replayed",
    "turn_resumed",
    "manual",
    # A tool call declared ``expect_error`` (a probe) that errored as expected:
    # written already disposed (``accepted``), never counted as undisposed.
    "expected_error",
    # A tool call declared ``expect_error`` that succeeded: the probe was wrong.
    "probe_passed_unexpectedly",
)

#: Where its fix belongs. ``unclassified`` until someone disposes of it.
FRICTION_CLASSES: tuple[str, ...] = (
    "harness",
    "procedure",
    "persona_prompt_trigger",
    "knowledge",
    "platform_acceptable",
    "unclassified",
)

#: What carries the fix: a work item, a shared-state entry, a skill or prompt
#: edit, or nothing because the behaviour is accepted.
FRICTION_DISPOSITIONS: tuple[str, ...] = (
    "work_item",
    "shared_state",
    "skill_edit",
    "prompt_edit",
    "accepted",
)

#: Bound on the stored evidence (the error or the call).
FRICTION_EVIDENCE_MAX_CHARS = 2000


class AgentFriction(Base):
    """One friction of one run (or of one agent outside a run)."""

    __tablename__ = "agent_frictions"
    __table_args__ = (
        Index("ix_agent_frictions_run_dedupe", "run_id", "kind", "tool", "evidence_hash"),
        Index("ix_agent_frictions_agent_recorded", "agent_id", "recorded_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: The person the run acts for; their reads see the row.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    #: The agent that met it.
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    #: The run (assignment run or background task) it was met in, when there is one.
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    assignment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    work_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    #: The tool involved, empty for a friction no tool names.
    tool: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    #: What was observed, bounded to FRICTION_EVIDENCE_MAX_CHARS.
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: sha256 of the normalised evidence: the dedupe key within a run.
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: How many times the run met it.
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    friction_class: Mapped[str] = mapped_column(String(40), nullable=False, default="unclassified")
    disposition_kind: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: The id or key of what carries the fix (a work item id, a shared-state key, a skill section).
    disposition_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    disposed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
