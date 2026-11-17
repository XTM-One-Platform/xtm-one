# SPDX-License-Identifier: Apache-2.0

"""CtemMetric model — daily CTEM metrics collected from registered platforms."""

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CtemMetric(Base):
    __tablename__ = "ctem_metrics"
    __table_args__ = (
        UniqueConstraint(
            "date",
            "provider",
            "platform_registration_id",
            "metric_type",
            name="uq_ctem_metric_day_provider_reg_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    provider: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    platform_registration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("platform_registrations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    metric_type: Mapped[str] = mapped_column(String(50), nullable=False)

    data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
