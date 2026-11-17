# SPDX-License-Identifier: Apache-2.0

"""One row per metric of an evaluation result (its scored metrics and its two durations), also keyed by run for server-side aggregates."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class EvaluationMetric(Base):
    """A single scalar metric logged for one result."""

    __tablename__ = "evaluation_metrics"
    __table_args__ = (
        UniqueConstraint("result_id", "key", name="uq_eval_metric_key"),
        Index("ix_eval_metrics_run_key", "run_id", "key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("evaluation_results.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Denormalized run link for fast run-level aggregates.
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("evaluation_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    # The entry's value of a scored metric. An unmeasured metric has no row, never a 0.
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    # A pooled metric's ratio terms: a run sums them instead of averaging
    # its entries' rates. Real, as a weighted share is fractional. NULL
    # for any other metric.
    numerator: Mapped[float | None] = mapped_column(Float, nullable=True)
    denominator: Mapped[float | None] = mapped_column(Float, nullable=True)
    # A macro metric's ``{class: {"tp", "fp", "fn"}}``: a run sums each
    # class over its entries before averaging their F1. NULL for any other metric.
    class_counts: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
