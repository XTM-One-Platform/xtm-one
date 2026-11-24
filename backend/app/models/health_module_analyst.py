# SPDX-License-Identifier: Apache-2.0

"""Platform Health Module Analyst: recommendations on findings (#101 S14a).

``dev-docs/platform-health.md`` section 12.4. One :class:`HealthFindingRecommendation`
row per finding and cycle: what the Module Analyst recommends about a finding of a
registered module, the evidence it read and how sure it is. The finding's own
deterministic ``recommendation`` is never rewritten; this is shown next to it.

No foreign key: a finding resolved and purged, or a cycle pruned, leaves the
recommendation readable as history, and a write never locks ``health_check_findings``,
which every health pass updates.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, DateTime, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: The verdicts of one check of a module checker's playbook (section 13.6).
CHECK_VERDICTS = ("pass", "warn", "fail", "not_run")
#: How sure the analyst is of a recommendation.
RECOMMENDATION_CONFIDENCES = ("high", "medium", "low")
#: The longest recommendation text kept (section 12.4).
RECOMMENDATION_MAX_CHARS = 1500


def _now() -> datetime:
    return datetime.now(UTC)


class HealthFindingRecommendation(Base):
    __tablename__ = "health_finding_recommendations"
    __table_args__ = (
        UniqueConstraint("finding_id", "cycle_id", name="uq_health_finding_recommendations_finding_cycle"),
        Index("ix_health_finding_recommendations_finding_created", "finding_id", "created_at"),
        Index("ix_health_finding_recommendations_registration", "registration_id"),
        CheckConstraint(
            "confidence IN (" + ", ".join(f"'{c}'" for c in RECOMMENDATION_CONFIDENCES) + ")",
            name="ck_health_finding_recommendations_confidence_valid",
        ),
        CheckConstraint(
            f"char_length(text) <= {RECOMMENDATION_MAX_CHARS}",
            name="ck_health_finding_recommendations_text_length",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: ``health_check_findings.id`` (sha256 of check and subject).
    finding_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The registered platform the cycle investigated (``platform_registrations.id``).
    registration_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    #: The Module Analyst cycle that wrote it (``health_cycles.id``).
    cycle_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    #: ``opencti`` | ``openaev`` | ``opencrq``, copied from the finding.
    module: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    #: What it read: ``[{"tool", "object_ids", "values", "read_at"}]``.
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa.text("'[]'::jsonb")
    )
    confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=func.now()
    )


class HealthModuleCheckReport(Base):
    """One module checker round on one registration: every check with its verdict (#101 S15c, section 13.6).

    One row per cycle; a second ``record_module_check_report`` in the cycle replaces it.
    No foreign key, as :class:`HealthFindingRecommendation`.
    """

    __tablename__ = "health_module_check_reports"
    __table_args__ = (
        UniqueConstraint("cycle_id", name="uq_health_module_check_reports_cycle"),
        Index("ix_health_module_check_reports_registration_finished", "registration_id", "finished_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: The registered platform the round checked (``platform_registrations.id``).
    registration_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    #: The checker cycle that wrote it (``health_cycles.id``).
    cycle_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    #: ``opencti`` | ``openaev`` | ``opencrq``.
    module: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=func.now()
    )
    passed: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default=sa.text("0"))
    warned: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default=sa.text("0"))
    failed: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default=sa.text("0"))
    not_run: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default=sa.text("0"))
    #: ``[{"id", "verdict", "evidence", "recommendation", "reason", "finding_id"}]``.
    checks: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa.text("'[]'::jsonb")
    )
