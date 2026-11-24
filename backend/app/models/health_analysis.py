# SPDX-License-Identifier: Apache-2.0

"""Platform Health analysis layer (#101 S9, ``dev-docs/platform-health.md`` section 6).

Two tables, read and written only by ``services/health/analysis.py``:

* ``health_module_analyses``: the latest analysis of each module, one row per
  module, replaced by the next one. It keeps the hash of the open finding set it
  read, so a module whose findings did not change is not analysed again.
* ``health_analysis_spend``: the ceiling ledger, one row per period (``D:YYYY-MM-DD``
  and ``M:YYYY-MM``, UTC). An analysis reserves its worst case on both rows
  before the model call, under a row lock, and books its real cost after.

Neither table is the agentic quota: nothing here is read by ``services/quota.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: Dollars to the micro-dollar, like ``llm_traces.total_cost``.
_COST = Numeric(16, 6)

#: What an analysis row says happened.
ANALYSIS_STATUSES = ("completed", "refused", "failed")


def _now() -> datetime:
    return datetime.now(UTC)


class HealthModuleAnalysis(Base):
    __tablename__ = "health_module_analyses"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in ANALYSIS_STATUSES) + ")",
            name="ck_health_module_analyses_status_valid",
        ),
    )

    module: Mapped[str] = mapped_column(String(16), primary_key=True)
    #: sha256 of the open finding ids and severities the analysis read.
    finding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Why it was refused or failed (``ceiling_not_set``, ``ceiling_reached``, ``no_model``...).
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(_COST, nullable=False, default=0, server_default="0")
    #: ``schedule`` or ``on_demand``.
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, default="schedule", server_default="schedule")
    analysed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=func.now()
    )


class HealthAnalysisSpend(Base):
    __tablename__ = "health_analysis_spend"

    #: ``D:YYYY-MM-DD`` or ``M:YYYY-MM`` (UTC).
    period: Mapped[str] = mapped_column(String(12), primary_key=True)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    reserved_cost_usd: Mapped[Decimal] = mapped_column(_COST, nullable=False, default=0, server_default="0")
    spent_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    spent_cost_usd: Mapped[Decimal] = mapped_column(_COST, nullable=False, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=func.now(), onupdate=_now
    )


class HealthModuleAnalystSpend(Base):
    """The Module Analyst's own ceiling ledger (#101 S14b, dev-docs section 12.5).

    Same shape as ``health_analysis_spend``, one row per UTC day and month; apart so the
    two ceilings never share a count. Not the agentic quota, not the fleet budget.
    """

    __tablename__ = "health_module_analyst_spend"

    #: ``D:YYYY-MM-DD`` or ``M:YYYY-MM`` (UTC).
    period: Mapped[str] = mapped_column(String(12), primary_key=True)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    reserved_cost_usd: Mapped[Decimal] = mapped_column(_COST, nullable=False, default=0, server_default="0")
    spent_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    spent_cost_usd: Mapped[Decimal] = mapped_column(_COST, nullable=False, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=func.now(), onupdate=_now
    )
