# SPDX-License-Identifier: Apache-2.0

"""Platform Health engine: check findings and the daily score history (#101 S2).

``dev-docs/platform-health.md`` section 3. A check is a deterministic function
(``services/health/engine.py``) run by a health pass, never a model call and
never metered. What a failing check reports is a :class:`HealthCheckFinding`,
one row per check and subject: its ``id`` is stable (``sha256(check_id|subject)``),
so the same failure seen on every pass updates one row instead of adding one.
A finding auto-resolves once its check passed twice in a row; a dismissed one
stays silent until its dismissal expires.

:class:`HealthScoreDay` keeps one row per day and module (score and open
counts by severity), 90 days, for the 7, 30 and 90 day views.

Not the analysis layer's ``health_findings`` (``models/health_finding.py``):
those are hypotheses a model wrote about an agent; these are measured facts.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, Date, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: The modules a check reports on (section 3, "Finding").
MODULES = ("agents", "fleets", "xtm_one", "opencti", "openaev", "opencrq")
CATEGORIES = (
    "availability",
    "performance",
    "capacity",
    "configuration",
    "security",
    "licence",
    "coverage",
    "cost",
)
SEVERITIES = ("critical", "warning", "info")

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"
STATUS_DISMISSED = "dismissed"
#: A failure read fewer than ``engine.OPEN_AFTER_FAILS`` times in a row: not open, not scored,
#: not listed, never alerted (#958). A passing read deletes it.
STATUS_PENDING = "pending"
#: The statuses a reader may list (the API filter); ``pending`` is internal to the engine.
STATUSES = (STATUS_OPEN, STATUS_RESOLVED, STATUS_DISMISSED)
#: Every status a row may hold (the check constraint).
ROW_STATUSES = (*STATUSES, STATUS_PENDING)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def _now() -> datetime:
    return datetime.now(UTC)


class HealthCheckFinding(Base):
    __tablename__ = "health_check_findings"
    __table_args__ = (
        CheckConstraint(_in("module", MODULES), name="ck_health_check_findings_module_valid"),
        CheckConstraint(_in("category", CATEGORIES), name="ck_health_check_findings_category_valid"),
        CheckConstraint(_in("severity", SEVERITIES), name="ck_health_check_findings_severity_valid"),
        CheckConstraint(_in("status", ROW_STATUSES), name="ck_health_check_findings_status_valid"),
        Index("ix_health_check_findings_module_status", "module", "status"),
        Index("ix_health_check_findings_check", "check_id"),
    )

    #: ``sha256(check_id|subject)`` hex: the dedup key, stable across passes.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    check_id: Mapped[str] = mapped_column(String(96), nullable=False)
    module: Mapped[str] = mapped_column(String(16), nullable=False)
    #: What the check is about, as ids (``{"agent_id": ...}``); ``{}`` for the module itself.
    subject: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_OPEN)
    #: ``{"values": ..., "threshold": ..., "read_at": ..., "source": ...}``.
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )
    recommendation: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=sa.text("''"))
    doc_link: Mapped[str] = mapped_column(String(512), nullable=False, default="", server_default=sa.text("''"))
    #: Consecutive passing reads of an open finding; two resolve it.
    pass_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa.text("0"))
    #: Consecutive failing reads of a finding that is not open; ``OPEN_AFTER_FAILS`` open it (#958).
    fail_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa.text("0"))
    #: When the current open episode began; the alert window is measured from it (#958).
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: When the immediate alert of the current open episode went out; ``None`` until then (#958).
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()")
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: ``{"reason", "by", "by_name", "at", "until"}`` while dismissed, else ``None``.
    dismissal: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class HealthScoreDay(Base):
    __tablename__ = "health_score_days"
    __table_args__ = (CheckConstraint(_in("module", MODULES), name="ck_health_score_days_module_valid"),)

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    module: Mapped[str] = mapped_column(String(16), primary_key=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    critical: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa.text("0"))
    warning: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa.text("0"))
    info: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa.text("0"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=sa.text("now()")
    )
