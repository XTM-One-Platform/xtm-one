# SPDX-License-Identifier: Apache-2.0

"""AiAdoptionMetric model: the platform's daily agentic AI adoption metrics.

Stores the platform's own daily usage in a flexible JSON data column. Each row
represents one day of metrics for a provider (``copilot_platform``), user and
metric type. The development providers were removed in #11380.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Date, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AiAdoptionMetric(Base):
    __tablename__ = "ai_adoption_metrics"
    __table_args__ = (
        UniqueConstraint(
            "date",
            "provider",
            "user_id",
            "metric_type",
            name="uq_ai_adoption_metric_date_provider_user_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # The day this metric covers
    date: Mapped[datetime] = mapped_column(Date, nullable=False, index=True)

    # Provider: "copilot_platform" (the platform's own agentic usage; the
    # development providers were removed in #11380)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # User-level metrics (nullable for aggregate/org-level metrics)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # External email for unmatched users (when user_id is NULL)
    user_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Metric type: "usage", "cost", "code_tracking"
    metric_type: Mapped[str] = mapped_column(String(50), nullable=False)

    # Flexible metric payload (schema varies by provider + metric_type)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)

    # Optional sync error for this specific metric entry
    sync_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
