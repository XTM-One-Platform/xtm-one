# SPDX-License-Identifier: Apache-2.0

"""QuotaUsage model – tracks per-user quota consumption.

Each row represents a usage counter for a specific user, quota type,
optional model, and time period.  Counters are primarily maintained in
Redis for performance and periodically flushed to this table for
durability and reporting.

``QuotaGlobalUsage`` is the platform-wide counterpart: the counters
the license ceilings and ``platform_limit`` are enforced against, flushed
from the same Redis buffer and attributed to no user.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class QuotaUsage(Base):
    __tablename__ = "quota_usage"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "usage_type",
            "model",
            "period",
            "agent_id",
            name="uq_quota_usage_user_type_model_period",
            # agent_id is nullable for legacy/non-attributed rows.
            # NULLS NOT DISTINCT ensures one logical row per key when agent_id is NULL.
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # "llm_proxy" or "agentic"
    usage_type: Mapped[str] = mapped_column(String(30), nullable=False)

    # For llm_proxy: the model name (e.g. "gpt-4o").
    # NULL means the aggregate counter across all models.
    # For agentic: always NULL.
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Period key: "2026-02" for monthly, "2026-02-18" for daily.
    period: Mapped[str] = mapped_column(String(10), nullable=False, index=True)

    # Optional attributed agent for agentic usage counters.
    # NULL keeps compatibility for legacy/global aggregate rows.
    # ON DELETE CASCADE (not SET NULL): the aggregate NULL-agent row for the
    # same (user, type, model, period) always coexists with attributed rows,
    # so SET NULL would violate the NULLS-NOT-DISTINCT unique constraint and
    # block agent deletion. The aggregate row keeps the totals; per-agent
    # attribution is meaningless once the agent is gone.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class QuotaGlobalUsage(Base):
    """Durable high-water mark of one platform-wide usage counter.

    One row per quota type (``agentic``, ``sandbox``, ``llm_proxy``) and period
    key (``2026``, ``2026-09``, ``2026-09-28``). No user column, on purpose: the
    platform-wide counters also count runs with no account behind them, which a
    ``quota_usage`` row would have to attribute to somebody. The flush only ever
    raises ``count``; the quota gate reads it back when Redis lost the counter.
    """

    __tablename__ = "quota_global_usage"

    usage_type: Mapped[str] = mapped_column(String(30), primary_key=True)

    period: Mapped[str] = mapped_column(String(10), primary_key=True)

    count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
