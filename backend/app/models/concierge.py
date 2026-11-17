# SPDX-License-Identifier: Apache-2.0

"""Platform Concierge budget ledger (#8254).

The Concierge's own line of the quota (PRD ``dev-docs/platform-concierge.md``
6.12): what each cycle and each month reserved and spent, in tokens and cost.
Read and written only by ``services/concierge/budget.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Index, Numeric, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: Cost columns: dollars to the micro-dollar, like ``llm_traces.total_cost``'s scale.
_COST = Numeric(16, 6)


class ConciergeCycle(Base):
    """One unit of Concierge work and the budget it holds.

    A cycle is opened before a specialist starts work (a schedule, a signal or
    an on-demand request) and closed when it ends. ``reserved_*`` is what work
    in flight set aside, ``spent_*`` what it really cost.
    """

    __tablename__ = "concierge_cycles"
    __table_args__ = (
        Index("ix_concierge_cycles_status_opened", "status", "opened_at"),
        # One open cycle per target (#8261, migration c8cnc8254081): the
        # database decides, not a read-then-write.
        Index(
            "uq_concierge_cycles_open_target",
            "target_type",
            "target_id",
            unique=True,
            postgresql_where=text("status = 'open' AND target_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # ``identity.ConciergeRole`` of the specialist the cycle is for.
    specialist: Mapped[str] = mapped_column(String(32), nullable=False)
    # ``schedule`` | ``signal`` | ``on_demand``.
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # ``YYYY-MM`` (UTC) the cycle's spend is booked on.
    month: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    # ``open`` | ``completed`` | ``budget_reached`` | ``failed`` | ``cancelled``.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", server_default="open")
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    reserved_cost_usd: Mapped[Decimal] = mapped_column(_COST, nullable=False, default=0, server_default="0")
    spent_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    spent_cost_usd: Mapped[Decimal] = mapped_column(_COST, nullable=False, default=0, server_default="0")
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ConciergeBudgetMonth(Base):
    """The Concierge's spend of one month, the row every reservation locks first."""

    __tablename__ = "concierge_budget_months"

    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    reserved_cost_usd: Mapped[Decimal] = mapped_column(_COST, nullable=False, default=0, server_default="0")
    spent_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    spent_cost_usd: Mapped[Decimal] = mapped_column(_COST, nullable=False, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=lambda: datetime.now(UTC),
    )


class ConciergeBudgetWork(Base):
    """One piece of Concierge work the ledger knows by its id: what it still
    holds in reservation past its cycle's close, or that its spend is booked.

    An evaluation outlives the cycle that launched it: its reservation is held
    here until its spend is charged (``held_*``). A run's spend is booked once
    (``charged_at``), whichever tick books it.
    """

    __tablename__ = "concierge_budget_work"

    # What the work is (``evaluation``, ``run``) and its id.
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    ref_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    cycle_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    held_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    held_cost_usd: Mapped[Decimal] = mapped_column(_COST, nullable=False, default=0, server_default="0")
    charged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), server_default=func.now()
    )
