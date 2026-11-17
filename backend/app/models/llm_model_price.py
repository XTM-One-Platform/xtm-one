# SPDX-License-Identifier: Apache-2.0

"""Model pricing table for LLM observability cost calculation."""

import uuid
from datetime import UTC, date, datetime

import sqlalchemy as sa
from sqlalchemy import Date, DateTime, Index, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class LLMModelPrice(Base):
    __tablename__ = "llm_model_prices"
    __table_args__ = (
        Index("ix_llm_model_prices_provider_model_id", "provider", "model_id"),
        Index("ix_llm_model_prices_model_pattern", "model_pattern"),
        Index("ix_llm_model_prices_effective_from", "effective_from"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    model_pattern: Mapped[str | None] = mapped_column(String(255), nullable=True)
    input_price_per_1m: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False)
    output_price_per_1m: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False)
    cache_read_price_per_1m: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    cache_write_price_per_1m: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, default=date.today)
    is_custom: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=sa.func.now(),
    )
