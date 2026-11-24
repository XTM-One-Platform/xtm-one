# SPDX-License-Identifier: Apache-2.0

"""LLM observability trace models."""

import uuid
from datetime import UTC, date, datetime

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Every value ``llm_traces.source`` may hold - the CHECK constraint below is
# built from it. Also what lets a per-day existence probe ride the
# ``(source, started_at)`` index: there is no index on ``started_at`` alone.
LLM_TRACE_SOURCES: tuple[str, ...] = (
    "agent-chat",
    "general-chat",
    "assignment",
    "flow",
    "memory",
    "evaluation",
    "proxy",
    # Platform Health's analysis layer (#101 S9): its own source, so the spend
    # stays visible in observability and never counts as an agentic execution.
    "platform_health",
)


class LLMTrace(Base):
    __tablename__ = "llm_traces"
    __table_args__ = (
        Index("ix_llm_traces_agent_started_at", "agent_id", "started_at"),
        Index("ix_llm_traces_user_started_at", "user_id", "started_at"),
        Index("ix_llm_traces_source_started_at", "source", "started_at"),
        Index("ix_llm_traces_conversation_id", "conversation_id"),
        Index("ix_llm_traces_assignment_run_id", "assignment_run_id"),
        Index("ix_llm_traces_parent_trace_id", "parent_trace_id"),
        Index("ix_llm_traces_model_started_at", "model", "started_at"),
        Index("ix_llm_traces_tags_gin", "tags", postgresql_using="gin"),
        # Partial: only failed traces carry a cause, and the Trace Explorer's
        # failure-cause filter is always combined with a time window.
        Index(
            "ix_llm_traces_failure_cause_started_at",
            "failure_cause",
            "started_at",
            postgresql_where=sa.text("failure_cause IS NOT NULL"),
        ),
        sa.CheckConstraint(
            "source IN (" + ", ".join(f"'{s}'" for s in LLM_TRACE_SOURCES) + ")",
            name="ck_llm_traces_source_valid",
        ),
        sa.CheckConstraint("status IN ('ok', 'error')", name="ck_llm_traces_status_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    assignment_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    parent_trace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("llm_traces.id", ondelete="SET NULL"),
        nullable=True,
    )
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    environment: Mapped[str] = mapped_column(String(50), nullable=False, default="production", server_default="production")
    metadata_json: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    input: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(String()), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_cost: Mapped[float | None] = mapped_column(Numeric(16, 8), nullable=True)
    output_cost: Mapped[float | None] = mapped_column(Numeric(16, 8), nullable=True)
    total_cost: Mapped[float | None] = mapped_column(Numeric(16, 8), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ok", server_default="ok")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Structured failure attribution. ``status`` says a trace failed
    # and ``error_message`` says what the text was; neither is aggregable.
    # This is, and it is what the Trace Explorer filters on. Written from the
    # single classifier in ``services/failure_causes`` at trace-end, alongside
    # ``failure_determination`` / ``failure_detail`` on the trace metadata (the
    # metadata dict is still open at that point — unlike Langfuse root
    # metadata, which is immutable once the trace is created).
    failure_cause: Mapped[str | None] = mapped_column(String(48), nullable=True)
    level: Mapped[str] = mapped_column(String(20), nullable=False, default="DEFAULT", server_default="DEFAULT")
    # Who the trace's usage counts for and why, resolved when it is
    # written by ``services/tracing/attribution.py``; ``user_id`` stays what
    # the producer sent. NULL ``attribution`` = written before it existed and
    # not stamped yet (``attribution_backfill``).
    attributed_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    attribution: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # The attributed person's department when the trace was written
    # (``services/departments.py``): NULL with ``department_resolved_at`` set
    # is Unassigned, NULL without it is not resolved yet.
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    department_resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    department_backfilled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default="false"
    )
    # The agent's name when the trace was written: a deleted agent keeps a label.
    agent_name_snapshot: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Input tokens a prompt cache served (part of ``input_tokens``); NULL on older traces.
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=sa.func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=sa.func.now(),
    )


class LLMSpan(Base):
    __tablename__ = "llm_spans"
    __table_args__ = (
        Index("ix_llm_spans_trace_id_position", "trace_id", "position"),
        Index("ix_llm_spans_parent_span_id", "parent_span_id"),
        Index("ix_llm_spans_type_started_at", "type", sa.desc("started_at")),
        sa.CheckConstraint(
            "type IN ('GENERATION', 'TOOL', 'SPAN', 'ITERATION', 'ACTIVATION_GATE', 'MEMORY')",
            name="ck_llm_spans_type_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("llm_traces.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_span_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model_parameters: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    input: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    level: Mapped[str] = mapped_column(String(20), nullable=False, default="DEFAULT", server_default="DEFAULT")
    status_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    time_to_first_token: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_cost: Mapped[float | None] = mapped_column(Numeric(16, 8), nullable=True)
    output_cost: Mapped[float | None] = mapped_column(Numeric(16, 8), nullable=True)
    total_cost: Mapped[float | None] = mapped_column(Numeric(16, 8), nullable=True)
    usage_details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tool_arguments: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=sa.func.now(),
    )


class LLMScore(Base):
    __tablename__ = "llm_scores"
    __table_args__ = (
        Index("ix_llm_scores_trace_id", "trace_id"),
        Index("ix_llm_scores_span_id", "span_id"),
        Index("ix_llm_scores_name_created_at", "name", "created_at"),
        sa.CheckConstraint(
            "source IN ('USER', 'SYSTEM', 'EVAL')",
            name="ck_llm_scores_source_valid",
        ),
        sa.CheckConstraint(
            "data_type IN ('NUMERIC', 'BOOLEAN', 'CATEGORICAL', 'TEXT')",
            name="ck_llm_scores_data_type_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("llm_traces.id", ondelete="CASCADE"),
        nullable=False,
    )
    span_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("llm_spans.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    data_type: Mapped[str] = mapped_column(String(20), nullable=False)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    string_value: Mapped[str | None] = mapped_column(String(500), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=sa.func.now(),
    )


class LLMDailyStats(Base):
    __tablename__ = "llm_daily_stats"
    __table_args__ = (
        UniqueConstraint(
            "date",
            "agent_id",
            "model",
            "source",
            "user_id",
            "attributed_user_id",
            "attribution",
            "department_id",
            # The name predates the attribution columns: kept, so the
            # aggregator's ON CONFLICT of a replica still on the older build
            # names a constraint that exists during a rolling deploy.
            name="uq_llm_daily_stats_date_agent_model_source_user",
            # agent_id and user_id are both nullable (general-chat / proxy /
            # agentless assignments; system-initiated runs have no user).
            # NULLS NOT DISTINCT (PG15+) makes a NULL collapse to a single group
            # so the constraint actually prevents duplicate null-agent/null-user
            # rows and the aggregator's ON CONFLICT DO NOTHING can dedupe them.
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    # Carried so the rollup can answer the same question as the live path for a
    # non-admin viewer (their own general-chat / agentless volume) and so
    # per-user cost stays answerable past the trace retention window. The rollup
    # outlives the raw-trace purge, so this is per-user usage data kept beyond
    # the trace retention period - documented in docs/administration/ai-governance.md.
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    model: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    trace_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    total_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    total_output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    total_cost: Mapped[float | None] = mapped_column(Numeric(16, 8), nullable=True)
    avg_duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    p50_duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    p95_duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Log-spaced bucket counts for this group's durations. Percentiles stored
    # per group cannot be recombined into the percentile of a multi-day window
    # (a trace_count-weighted mean of p50s is not a p50); bucket counts add, so
    # the read path merges these instead. Shape and geometry:
    # services/tracing/latency_histogram.py. NULL on rows written before the
    # histogram existed - the read path falls back to the columns above and
    # flags the result as approximate.
    duration_histogram: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    avg_feedback_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The traces' attribution and department, part of the grain: the
    # rollup outlives the purge, so a department series does too. Rows built
    # before they existed are stamped by ``attribution_backfill``.
    attributed_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    attribution: Mapped[str | None] = mapped_column(String(20), nullable=True)
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    department_backfilled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default="false"
    )
    agent_name_snapshot: Mapped[str | None] = mapped_column(String(255), nullable=True)
    total_cached_input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=sa.func.now(),
    )


class LLMDailyStatsStaleDay(Base):
    """A day whose ``llm_daily_stats`` rows were built from traces that changed
    since - a repricing filled in their costs.

    The stale rows stay in place until ``trace_storage.rebuild_daily_stats``
    replaces them in one transaction: a rollup-served read sees the old rows or
    the new ones, never a day without its rollup. Recorded with the change
    itself, so a rebuild still owed survives a crash, a failed aggregation and
    the purge, which keeps a recorded day's traces until it is rebuilt.
    """

    __tablename__ = "llm_daily_stats_stale_days"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    marked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=sa.func.now(),
    )
