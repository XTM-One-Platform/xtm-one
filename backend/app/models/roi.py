# SPDX-License-Identifier: Apache-2.0

"""ROI storage: the units each run produced and the organization's baseline overrides.

CTEM seeded fleets, slice S4 storage (issue #8990, ``dev-docs/ctem-fleets.md``
section 5.2). The figures themselves are computed by ``services/roi.py``.

* ``roi_run_records``: one row per assignment run whose agent declares an
  ``x-xtm-roi`` unit, written at the run's completion from its structured
  output (``services/roi_records.py``): units counted, review minutes when
  measured, LLM cost, duration, outcome and its review (#9310).
* ``roi_baseline_overrides``: the organization's own minutes for a catalog
  unit, set by an administrator; a unit without a row uses the default
  catalog (``services.roi.BASELINES``).
* ``roi_units``: the organization's custom units of work (issue #9252, design
  ``dev-docs/roi-outcomes.md`` section 3.1), next to the built-in catalog,
  archived and never deleted; ``roi_unit_versions`` keeps every version.
* ``roi_declarations``: the unit an agent, an assignment or a fleet declares
  and how it is counted (issue #9285, design section 3.2), set without editing
  an ``output_schema``.
* ``roi_rates``: the organization's hourly rate of a role in a currency
  (issue #9321, design section 3.4), so a custom unit's ``rate_role``
  ("legal") carries its own rate.
* ``roi_chat_records``: one row per finished deliverable an agent built in a
  conversation (issue #10544, design section 11), keyed on the deliverable's
  own id so every iteration updates the same row; counted on the low side by
  ``services/roi_chat.py``, a series of its own, never inside a fleet's
  automation count.
* ``roi_cost_facts``: the AI cost of one closed day, per person, attribution,
  agent, fleet and class (issue #12077, design section 12.3), built from
  ``llm_traces`` (or the ``llm_daily_stats`` rollup past the trace retention)
  by ``services/roi_ledger.py``; a closed day is never rewritten by a later
  fleet move.
"""

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _now() -> datetime:
    return datetime.now(UTC)


class RoiRunRecord(Base):
    __tablename__ = "roi_run_records"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('accepted', 'modified', 'rejected')",
            name="ck_roi_run_records_outcome_valid",
        ),
        CheckConstraint("units >= 0", name="ck_roi_run_records_units_positive"),
        Index("ix_roi_run_records_agent_created", "agent_id", "created_at"),
        Index("ix_roi_run_records_fleet_created", "fleet_id", "created_at"),
        Index("ix_roi_run_records_created_at", "created_at"),
        Index("uq_roi_run_records_work_item_id", "work_item_id", unique=True),
        Index("ix_roi_run_records_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: The run that produced the units; one record per run.
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assignment_runs.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    #: The agent's home fleet when the run ended (no FK: a deleted fleet keeps its history).
    fleet_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: The work item whose completion produced the units (``per_work_item``, #9285);
    #: one record per item. No FK: a deleted item keeps its history.
    work_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: The person the units count for (#12077, ``services/roi_ledger.py``); no FK: a
    #: deleted person keeps the organization's history. NULL with ``attribution`` set:
    #: nobody (``unknown``), counted in the organization only.
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: The rule that named the person (``assignment``, ``work_item``, ``agent_owner``)
    #: or ``unknown``; NULL until the ledger backfill stamps a row written before #12077.
    attribution: Mapped[str | None] = mapped_column(String(20), nullable=True)
    #: A catalog unit (``services.roi.BASELINES``) or a custom agent's own.
    unit: Mapped[str] = mapped_column(String(64), nullable=False)
    units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Measured review minutes for the whole run; NULL falls back to the baseline.
    review_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    llm_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default=text("0"))
    duration_s: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default=text("0"))
    #: ``accepted``, ``modified`` or ``rejected`` once a person reviewed the run;
    #: ``NULL`` while unreviewed (#9310). Rows written before reviews existed
    #: keep ``accepted``.
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: Set when the record was written by a retroactive pass after a declaration
    #: changed (#10006, ``services/roi_backfill.py``); ``NULL`` when written at completion.
    backfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()")
    )


class RoiBaselineOverride(Base):
    __tablename__ = "roi_baseline_overrides"
    __table_args__ = (
        CheckConstraint(
            "manual_min_low <= manual_min_likely AND manual_min_likely <= manual_min_high",
            name="ck_roi_baseline_overrides_ordered",
        ),
        CheckConstraint("review_min >= 0", name="ck_roi_baseline_overrides_review_positive"),
    )

    #: The catalog unit it overrides.
    unit: Mapped[str] = mapped_column(String(64), primary_key=True)
    manual_min_low: Mapped[float] = mapped_column(Float, nullable=False)
    manual_min_likely: Mapped[float] = mapped_column(Float, nullable=False)
    manual_min_high: Mapped[float] = mapped_column(Float, nullable=False)
    review_min: Mapped[float] = mapped_column(Float, nullable=False)
    #: Where the organization's figure comes from (a time-and-motion study, telemetry).
    source: Mapped[str] = mapped_column(String(300), nullable=False, default="", server_default=text("''"))
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()")
    )


class RoiUnit(Base):
    """A custom unit of work an administrator added to the catalog (#9252)."""

    __tablename__ = "roi_units"
    __table_args__ = (
        CheckConstraint(
            "manual_min_low <= manual_min_likely AND manual_min_likely <= manual_min_high",
            name="ck_roi_units_ordered",
        ),
        CheckConstraint("manual_min_low >= 0 AND review_min >= 0", name="ck_roi_units_positive"),
    )

    #: Slug ``[a-z0-9_]``, unique across the built-in and the custom units.
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: The human role the unit replaces ("Legal counsel").
    role: Mapped[str] = mapped_column(String(100), nullable=False, default="", server_default=text("''"))
    manual_min_low: Mapped[float] = mapped_column(Float, nullable=False)
    manual_min_likely: Mapped[float] = mapped_column(Float, nullable=False)
    manual_min_high: Mapped[float] = mapped_column(Float, nullable=False)
    review_min: Mapped[float] = mapped_column(Float, nullable=False)
    #: A key of the rate table (``services.roi.HOURLY_RATES``).
    rate_role: Mapped[str] = mapped_column(
        String(64), nullable=False, default="analyst", server_default=text("'analyst'")
    )
    #: Where the minutes come from ("internal time study, Q3").
    source: Mapped[str] = mapped_column(String(300), nullable=False, default="", server_default=text("''"))
    #: Archived units stay readable in history and can no longer be declared.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()")
    )


class RoiDeclaration(Base):
    """The unit a subject declares and how its units are counted (#9285).

    One row per subject (an agent, an assignment or a fleet). Resolution at
    the end of a run: the assignment's, then the agent's, then the agent's
    legacy ``x-xtm-roi``, then the home fleet's (``roi_records.resolve_declaration``).
    """

    __tablename__ = "roi_declarations"
    __table_args__ = (
        UniqueConstraint("subject_type", "subject_id", name="uq_roi_declarations_subject"),
        CheckConstraint(
            "subject_type IN ('agent', 'assignment', 'fleet')",
            name="ck_roi_declarations_subject_type",
        ),
        CheckConstraint(
            "count_mode IN ('per_run', 'per_output_item', 'per_work_item')",
            name="ck_roi_declarations_count_mode",
        ),
        CheckConstraint("units_per_count >= 1", name="ck_roi_declarations_units_per_count"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The agent, assignment or fleet id (no FK: one table for three subjects).
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    #: A key of the effective catalog (built-in or custom unit).
    unit: Mapped[str] = mapped_column(String(64), nullable=False)
    count_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Dot path in the structured output, only for ``per_output_item``.
    count_path: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Units a counted run or work item produces (a run that always reviews three contracts).
    units_per_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()")
    )


class RoiRate(Base):
    """The organization's fully loaded hourly rate of one role in one currency (#9321)."""

    __tablename__ = "roi_rates"
    __table_args__ = (CheckConstraint("amount > 0", name="ck_roi_rates_amount_positive"),)

    #: A built-in role (``services.roi.HOURLY_RATES``) or a custom one ("legal").
    role: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: ISO 4217 code, upper case.
    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    #: Where the figure comes from ("payroll 2026, loaded").
    source: Mapped[str] = mapped_column(String(300), nullable=False, default="", server_default=text("''"))
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()")
    )


class RoiChatRecord(Base):
    """One deliverable an agent built in a conversation, counted automatically on the low side (#10544).

    Written by ``services/roi_chat_records.py``: created at the deliverable's first save, its size and
    ``last_edit_at`` refreshed on every iteration, a use signal appended when it is exported, shared,
    given a public link, presented, sent or attached, ``deleted_at`` stamped when it is deleted. Whether
    it counts and how much is computed on read (``roi_chat.is_counted`` / ``roi_chat.estimate``), so a
    rule change never rewrites rows. ``adjusted_min`` is a person's correction from the ROI page or an
    agent tool; NULL keeps the estimate.
    """

    __tablename__ = "roi_chat_records"
    __table_args__ = (
        UniqueConstraint("record_key", name="uq_roi_chat_records_record_key"),
        CheckConstraint(
            "kind IN ('presentation', 'report', 'video', 'file')",
            name="ck_roi_chat_records_kind_valid",
        ),
        CheckConstraint("size >= 0", name="ck_roi_chat_records_size_positive"),
        CheckConstraint("adjusted_min IS NULL OR adjusted_min >= 0", name="ck_roi_chat_records_adjusted_positive"),
        Index("ix_roi_chat_records_user_created", "user_id", "created_at"),
        Index("ix_roi_chat_records_agent_created", "agent_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: ``roi_chat.record_key``: ``<kind>:<deliverable id>``, one row per deliverable.
    record_key: Mapped[str] = mapped_column(String(120), nullable=False)
    #: One of ``roi_chat.KINDS``.
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The deliverable's own id (no FK: a deleted deliverable that counted keeps its record).
    deliverable_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The person the work is attributed to.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: The agent of the conversation (no FK: a deleted agent keeps its history).
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: The agent's home fleet when the row was first written (#12077; no FK: a deleted
    #: fleet keeps its history).
    fleet_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: ``False`` when stamped at the first write, ``True`` when stamped by the ledger
    #: backfill from the agent's home fleet at that time; NULL until stamped.
    fleet_backfilled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: The conversation the deliverable was built in (no FK, same reason).
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="", server_default=text("''"))
    #: Slides, pages or minutes of video (``roi_chat.SIZE_LABEL``), as of the last edit.
    size: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default=text("0"))
    #: The use signals seen, each once (``roi_chat.USE_SIGNALS``).
    signals: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    first_signal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_edit_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: A person's correction of the counted minutes; NULL keeps the estimate.
    adjusted_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    adjusted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    adjusted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Set when the row was written by the one-off pass over past conversations (slice S3).
    backfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()")
    )


class RoiUnitVersion(Base):
    """One version of a custom unit: the whole row as written, by whom, when."""

    __tablename__ = "roi_unit_versions"
    __table_args__ = (UniqueConstraint("unit_key", "version", name="uq_roi_unit_versions_unit_version"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: No FK: a unit is never deleted, and the history outlives any cleanup.
    unit_key: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()")
    )


#: The class of a cost fact (design section 12.3): what the usage was spent on.
COST_CLASSES: tuple[str, ...] = ("automation", "chat", "platform")


class RoiCostFact(Base):
    """The AI cost of one closed day for one person, attribution, agent, fleet and class (#12077, L2).

    Written by ``roi_ledger.close_cost_days``, a day whole in one transaction under the closing
    lock, so a day holds one row per grain: from ``llm_traces`` (``built_from`` ``traces``) or, for
    a day past the trace retention, from the ``llm_daily_stats`` rollup (``rollup``). A day is
    closed once, so a later fleet move never rewrites it. The days not closed yet are read live
    with the same query (``roi_ledger.cost_facts``). ``cost`` is the cost as priced, never masked
    here: masking is the reader's (design 12.6).
    """

    __tablename__ = "roi_cost_facts"
    __table_args__ = (
        CheckConstraint("cost_class IN ('automation', 'chat', 'platform')", name="ck_roi_cost_facts_class_valid"),
        CheckConstraint("built_from IN ('traces', 'rollup')", name="ck_roi_cost_facts_built_from_valid"),
        Index("ix_roi_cost_facts_user_day", "user_id", "day"),
        Index("ix_roi_cost_facts_agent_day", "agent_id", "day"),
        Index("ix_roi_cost_facts_fleet_day", "fleet_id", "day"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    day: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    #: The person the usage counts for (``llm_traces.attributed_user_id``; no FK: history outlives a user).
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: The rule that named the person (``tracing/attribution.py``); NULL on a rollup day never stamped.
    attribution: Mapped[str | None] = mapped_column(String(20), nullable=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: The fleet of the run's ROI record, else the agent's home fleet when the day was closed.
    fleet_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: One of :data:`COST_CLASSES`.
    cost_class: Mapped[str] = mapped_column(String(16), nullable=False)
    cost: Mapped[float] = mapped_column(Numeric(16, 8), nullable=False, default=0, server_default=text("0"))
    trace_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    #: ``traces`` or ``rollup``: what the day was closed from.
    built_from: Mapped[str] = mapped_column(String(8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()")
    )
