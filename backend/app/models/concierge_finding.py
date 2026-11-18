# SPDX-License-Identifier: Apache-2.0

"""Concierge findings - what the Quality Analyst diagnosed, kept across cycles (#8259).

A finding names one pattern on one target (an agent, an assignment, a flow):
its evidence (trace, rating and run ids, counts), the hypothesis that
explains it and the experiment it calls for. Its ``fingerprint`` is the
pattern's identity - target, kind, key - and never its evidence, which changes
every cycle: recording the same pattern again updates the row instead of
reporting it twice (``services/concierge/findings.py``).

Not a shareable entity: it belongs to the Concierge, and who may read it
follows its target (``services/concierge/access.py``). The target is recorded
by id without a key, as it can be of several types; a finding whose target is
gone is no longer listed.
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

TARGET_TYPES = ("agent", "assignment", "flow")

#: The closed set of pattern kinds; the key names the instance inside the kind.
PATTERN_KINDS = (
    "failure_cause",  # key: the failure cause code
    "tool_errors",  # key: the tool name
    "negative_feedback",  # key: "" (all ratings) or a feedback criterion id
    "low_criterion",  # key: the feedback criterion id
    "cost_drift",  # key: ""
    "latency",  # key: ""
    "evaluation_weakness",  # key: the evaluation id
    "behaviour",  # key: a slug the analyst chose, reused across cycles
)

#: Diagnosed, no experiment yet.
STATUS_OPEN = "open"
#: A variant of it is drafted or under measurement.
STATUS_EXPERIMENTING = "experimenting"
#: A proposal was filed for it.
STATUS_PROPOSED = "proposed"
#: Its proposal was applied and kept; a recurrence reopens it.
STATUS_RESOLVED = "resolved"
#: Judged not worth acting on; a recurrence stays silent.
STATUS_DISMISSED = "dismissed"

FINDING_STATUSES = (STATUS_OPEN, STATUS_EXPERIMENTING, STATUS_PROPOSED, STATUS_RESOLVED, STATUS_DISMISSED)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def _now() -> datetime:
    return datetime.now(UTC)


class ConciergeFinding(Base):
    __tablename__ = "health_findings"
    __table_args__ = (
        CheckConstraint(_in("status", FINDING_STATUSES), name="ck_concierge_findings_status_valid"),
        CheckConstraint(_in("target_type", TARGET_TYPES), name="ck_concierge_findings_target_type_valid"),
        CheckConstraint(_in("pattern_kind", PATTERN_KINDS), name="ck_concierge_findings_pattern_kind_valid"),
        Index("uq_concierge_findings_fingerprint", "fingerprint", unique=True),
        Index("ix_concierge_findings_target", "target_type", "target_id", "status"),
        Index("ix_concierge_findings_last_seen", "last_seen_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    pattern_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    pattern_key: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default=sa.text("''"))
    #: ``sha256(target_type|target_id|pattern_kind|pattern_key)``: the dedup key.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_OPEN)

    #: Written by the analyst in the platform language; derived from hostile data.
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=sa.text("''"))
    suggested_experiment: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=sa.text("''")
    )
    #: ``{"trace_ids": [...], "rating_ids": [...], "run_ids": [...], "counts": {...}}``,
    #: every id checked against its row when recorded.
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb"))
    #: The evidence reaches conversation content (trace ids, rating comments): it is
    #: redacted once the target's improvement opt-in is revoked.
    content_evidence: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa.text("false")
    )
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=sa.text("1"))

    author_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()")
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()")
    )
    #: When the finding was last reported as new (first record, or a reopening).
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        onupdate=_now,
        server_default=sa.text("now()"),
    )
