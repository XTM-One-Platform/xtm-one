# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Trace Explorer API endpoints."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.api.v1.helpers import can_view_agent_chats, get_user_group_ids, parse_path_id, parse_uuid
from app.api.v1.schemas.traces import (
    LLMScore as LLMScoreSchema,
)
from app.api.v1.schemas.traces import (
    LLMSpan as LLMSpanSchema,
)
from app.api.v1.schemas.traces import (
    LLMTrace as LLMTraceSchema,
)
from app.core.database import async_session, get_db
from app.core.redis import redis_client
from app.core.utils import (
    build_content_disposition,
    coerce_tool_arguments_object,
    format_exception_message,
    safe_uuid,
)
from app.models.activity import Activity
from app.models.agent import Agent
from app.models.chat import Conversation, Message
from app.models.llm_trace import LLMScore as LLMScoreModel
from app.models.llm_trace import LLMSpan, LLMTrace
from app.models.quota import QuotaUsage
from app.models.user import User
from app.services.audit import log_audit
from app.services.failure_causes import (
    FAILURE_CAUSE_VALUES,
    UNRECORDED_FAILURE_CAUSE,
)
from app.services.group_managers import NO_MEMBER_ID, TeamScope, team_scope
from app.services.trace_storage import aggregate_daily_stats
from app.services.tracing.media_offload import (
    find_media_reference,
    resolve_media,
)
from app.services.tracing.payload_offload import (
    build_display_preview,
    delete_trace_objects,
    is_offload_envelope,
    payload_meta,
    payload_size_bytes,
    resolve_payload,
)
from app.services.tracing.read_store import (
    ANALYTICS_ERRORS_DEFAULT_LIMIT,
    ANALYTICS_ERRORS_MAX_LIMIT,
    ANALYTICS_PERIOD_PATTERN,
    DEFAULT_SORT_DIR,
    DEFAULT_TRACE_SESSION_SORT,
    DEFAULT_TRACE_SORT,
    TRACE_LIST_MAX_OFFSET,
    TRACE_SESSION_SORT_FIELDS,
    TRACE_SORT_FIELDS,
    TraceReadStore,
    agent_owners,
    agent_row_access,
    build_stats_filters,
    build_trace_filters,
    resolve_date_range,
    session_conversation_titles,
    session_user_emails,
    use_daily_stats,
)
from app.services.tracing.read_store import (
    trace_visible_query as store_trace_visible_query,
)
from app.services.tracing.store import (
    build_trace_read_store,
    ensure_trace_read_provider_supported,
)
from app.services.tracing.usage_breakdown import usage_by_group, usage_by_user


def require_trace_read_store():
    ensure_trace_read_provider_supported()
    return build_trace_read_store()


router = APIRouter(dependencies=[Depends(require_trace_read_store)])
logger = logging.getLogger(__name__)

_TRACE_BUNDLE_EXPORT_BATCH_SIZE = 200
_TRACE_BUNDLE_EXPORT_MAX_TRACES = 5000


class LLMScoreCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    data_type: str = Field(pattern="^(NUMERIC|BOOLEAN|CATEGORICAL|TEXT)$")
    value: float | None = None
    string_value: str | None = None
    comment: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AnalyticsFilters(BaseModel):
    period: str = Field(default="30d", pattern=ANALYTICS_PERIOD_PATTERN)
    date_from: datetime | None = None
    date_to: datetime | None = None
    agent_ids: str | None = None
    models: str | None = None
    sources: str | None = None
    # Narrow the read to the CURRENT members of these groups. A
    # non-admin names only groups they manage; a manager naming none reads
    # every group they manage, administrators left out of the team
    # (``services/group_managers.team_scope``).
    group_ids: str | None = None
    # Narrow the read to the traces these people started; within a team,
    # only its members count.
    user_ids: str | None = None


class TraceListItem(LLMTraceSchema):
    """Extends the base trace schema with joined fields for the Explorer API."""

    agent_name: str | None = None
    agent_icon: str | None = None
    user_email: str | None = None
    # Whether the trace has any score (drives the list "Score" column checkmark);
    # populated by read_store.list_traces so the UI need not fire a per-row fetch.
    has_scores: bool = False


class TraceListResponse(BaseModel):
    items: list[TraceListItem]
    total: int
    limit: int


class TraceSessionItem(BaseModel):
    conversation_id: str
    # The conversation's title: what a person reads to name the session (its id is no name).
    conversation_title: str | None = None
    # The person the session's conversation belongs to.
    user_email: str | None = None
    first_activity_at: str
    last_activity_at: str
    trace_count: int
    error_count: int
    agent_count: int
    model_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    total_cost: float


class TraceSessionListResponse(BaseModel):
    items: list[TraceSessionItem]
    total: int
    limit: int


class SessionExplorerResponse(BaseModel):
    session_id: str
    traces: list[TraceListItem]
    spans_by_trace_id: dict[str, list[LLMSpanSchema]]
    total_traces: int
    total_spans: int
    limit: int


class TraceResponse(TraceListItem):
    pass


class SpanListResponse(BaseModel):
    items: list[LLMSpanSchema]
    total: int


class ScoreListResponse(BaseModel):
    items: list[LLMScoreSchema]
    total: int


class AnalyticsSummaryMetrics(BaseModel):
    total_cost: float
    total_tokens: int
    trace_count: int
    avg_duration_ms: float
    error_rate: float
    avg_feedback_score: float | None = None
    quota_consumed: int = 0
    message_count: int = 0
    avg_quota_per_message: float | None = None


class AnalyticsSummaryResponse(AnalyticsSummaryMetrics):
    previous: AnalyticsSummaryMetrics
    last_aggregation_at: datetime | None = None
    aggregation_stale: bool
    unknown_cost_trace_count: int
    # Earliest day any figure can be reported for (oldest daily-stats rollup or
    # surviving trace). With ``window_starts_before_coverage`` it lets the UI say
    # "this window reaches further back than the data" instead of presenting the
    # missing days as zero activity.
    stats_coverage_start: date | None = None
    window_starts_before_coverage: bool = False


class CostByModelItem(BaseModel):
    model: str
    trace_count: int
    input_tokens: int
    output_tokens: int
    input_cost: float | None = None
    output_cost: float | None = None
    total_cost: float


class CostByModelResponse(BaseModel):
    items: list[CostByModelItem]


class CostOverTimePoint(BaseModel):
    date: str
    model: str
    cost: float


class CostOverTimeResponse(BaseModel):
    series: list[CostOverTimePoint]


class VolumeOverTimePoint(BaseModel):
    date: str
    source: str
    count: int


class VolumeOverTimeResponse(BaseModel):
    series: list[VolumeOverTimePoint]


class LatencyOverall(BaseModel):
    p50: float | None = None
    p75: float | None = None
    p90: float | None = None
    p95: float | None = None
    p99: float | None = None


class LatencyByModelItem(LatencyOverall):
    model: str
    trace_count: int


class LatencyDailyMedianPoint(BaseModel):
    date: str
    p50: float | None = None


class LatencyResponse(BaseModel):
    overall: LatencyOverall
    by_model: list[LatencyByModelItem]
    daily_median: list[LatencyDailyMedianPoint]
    # True when at least one figure had to be recombined from per-group stored
    # percentiles (rollup rows written before the mergeable latency histogram
    # existed) instead of merged exactly. Only reachable on windows longer than
    # 7 days, and it clears itself as those rows age out.
    approximate: bool = False


class CostByAgentItem(BaseModel):
    agent_id: str
    agent_name: str | None = None
    agent_icon: str | None = None
    owner_id: str | None = None
    owner_name: str | None = None
    owner_email: str | None = None
    is_company_managed: bool = False
    #: ``active``, ``disabled`` or ``deleted``: a deleted agent keeps its last known name.
    agent_status: Literal["active", "disabled", "deleted"] = "active"
    #: Whether the CALLER may open the agent's page, decided here, never by the client.
    can_open: bool = False
    #: The agent page to open, set only when ``can_open``.
    agent_url: str | None = None
    trace_count: int
    total_cost: float
    avg_cost_per_trace: float
    avg_duration_ms: float
    avg_feedback_score: float | None = None


class CostByAgentResponse(BaseModel):
    items: list[CostByAgentItem]


class UsageTotals(BaseModel):
    trace_count: int
    total_tokens: int
    total_cost: float
    avg_cost_per_trace: float


class CostByUserItem(UsageTotals):
    user_id: str
    user_email: str | None = None
    user_name: str | None = None
    # ``None`` once the account is deleted: its traces keep the id.
    is_active: bool | None = None
    group_names: list[str] = Field(default_factory=list)
    agent_count: int


class OtherUsersUsage(UsageTotals):
    user_count: int


class CostByUserResponse(BaseModel):
    items: list[CostByUserItem]
    total_users: int
    # The users past ``limit``, counted together so the response adds up to the total.
    other_users: OtherUsersUsage | None = None
    unattributed: UsageTotals | None = None


class CostByGroupItem(UsageTotals):
    group_id: str
    group_name: str
    member_count: int
    active_user_count: int
    avg_cost_per_active_user: float


class UngroupedUsage(UsageTotals):
    active_user_count: int
    avg_cost_per_active_user: float


class CostByGroupResponse(BaseModel):
    items: list[CostByGroupItem]
    ungrouped: UngroupedUsage | None = None
    unattributed: UsageTotals | None = None
    overlapping_membership: bool = False


class ErrorItem(BaseModel):
    error_message: str
    count: int
    last_seen: str | None = None
    agent_name: str | None = None
    model: str | None = None


class ErrorsResponse(BaseModel):
    items: list[ErrorItem]


class AnalyticsRefreshResponse(BaseModel):
    status: str
    message: str


# Sort params are validated against the read stores' allow-lists (a 422 on
# anything else) so the accepted columns and the ones the stores can actually
# order by stay one list.
_TRACE_SORT_PATTERN = f"^({'|'.join(TRACE_SORT_FIELDS)})$"
_TRACE_SESSION_SORT_PATTERN = f"^({'|'.join(TRACE_SESSION_SORT_FIELDS)})$"
_SORT_DIR_PATTERN = "^(asc|desc)$"

_LIST_OFFSET_DESCRIPTION = (
    f"Rows to skip, at most {TRACE_LIST_MAX_OFFSET}. A page deeper than that is "
    "reached from the other end of the list with `reverse`."
)
_LIST_REVERSE_DESCRIPTION = (
    "Read the list from its last row: the exact mirror of sort_by / sort_dir "
    "(NULL placement and tie-breaker included), so `offset` counts from the end "
    "and the rows come back in that mirrored order. Never paged by the keyset "
    "cursor."
)


def _validate_failure_causes(values: list[str] | None) -> list[str] | None:
    """Reject unknown ``failure_cause`` filter values with a 422.

    Validated against the enum rather than passed through, so a typo is a
    clear error instead of a silently empty result page.
    """
    if not values:
        return None
    allowed = set(FAILURE_CAUSE_VALUES) | {UNRECORDED_FAILURE_CAUSE}
    invalid = sorted({value for value in values if value not in allowed})
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown failure_cause value(s): {', '.join(invalid)}",
        )
    return values


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_agent_ids_csv(value: str | None, *, label: str) -> list[uuid.UUID]:
    return [parse_uuid(item, label=label) for item in _split_csv(value)]


async def _load_quota_message_metrics(
    db: AsyncSession,
    *,
    agent_ids: list[uuid.UUID],
) -> tuple[int, int, float | None]:
    """Quota + message KPIs for the agent-filtered observability view.

    The denominator counts **user** messages only: agentic quota is metered
    once per user interaction/turn, so counting assistant/system/tool rows
    would skew ``avg_quota_per_message`` downward (~2x). This mirrors the
    per-interaction semantics of the agents API message counting.

    Anonymous conversations (``Conversation.user_id IS NULL``) are excluded:
    anonymous users bypass agentic quota metering entirely (they never
    contribute to ``quota_consumed``), so their messages would artificially
    lower the average for agents receiving anonymous channel traffic.
    """
    quota_stmt = select(func.coalesce(func.sum(QuotaUsage.count), 0)).where(
        QuotaUsage.usage_type == "agentic",
        QuotaUsage.model.is_(None),
        QuotaUsage.agent_id.is_not(None),
    )
    message_stmt = (
        select(func.count(Message.id))
        .select_from(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.agent_id.is_not(None),
            Conversation.user_id.is_not(None),
            Message.role == "user",
        )
    )

    if agent_ids:
        quota_stmt = quota_stmt.where(QuotaUsage.agent_id.in_(agent_ids))
        message_stmt = message_stmt.where(Conversation.agent_id.in_(agent_ids))

    quota_consumed_result = await db.execute(quota_stmt)
    message_count_result = await db.execute(message_stmt)
    quota_consumed = int(quota_consumed_result.scalar_one_or_none() or 0)
    message_count = int(message_count_result.scalar_one_or_none() or 0)
    avg_quota_per_message = (
        (quota_consumed / message_count) if message_count > 0 else None
    )
    return quota_consumed, message_count, avg_quota_per_message


def _resolve_date_range(
    period: str,
    date_from: datetime | None,
    date_to: datetime | None,
) -> tuple[datetime, datetime, datetime, datetime]:
    return resolve_date_range(period, date_from, date_to)


def _use_daily_stats(start: datetime, end: datetime) -> bool:
    return use_daily_stats(start, end)


def _build_trace_filters(
    filters: AnalyticsFilters,
    *,
    user: User,
    user_group_ids: list[str],
    start: datetime,
    end: datetime,
):
    return build_trace_filters(
        filters,
        user=user,
        user_group_ids=user_group_ids,
        start=start,
        end=end,
    )


def _build_stats_filters(
    filters: AnalyticsFilters,
    *,
    user: User,
    user_group_ids: list[str],
    start: datetime,
    end: datetime,
):
    return build_stats_filters(
        filters,
        user=user,
        user_group_ids=user_group_ids,
        start=start,
        end=end,
    )


def _first_row(result) -> Any | None:
    rows = result.all()
    if not rows:
        return None
    return rows[0]


def _metric_float(value: Any) -> float:
    if value is None:
        return 0.0
    return float(value)


def _metric_int(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _normalize_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _as_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _trace_to_response(
    trace: LLMTrace,
    *,
    agent_name: str | None = None,
    agent_icon: str | None = None,
    user_email: str | None = None,
    has_scores: bool = False,
) -> TraceResponse:
    payloads, payload_meta_fields = _payload_fields(
        input=trace.input,
        output=trace.output,
        metadata=trace.metadata_json,
    )
    return TraceResponse(
        id=str(trace.id),
        agent_id=str(trace.agent_id) if trace.agent_id else None,
        user_id=str(trace.user_id) if trace.user_id else None,
        conversation_id=str(trace.conversation_id) if trace.conversation_id else None,
        assignment_run_id=str(trace.assignment_run_id) if trace.assignment_run_id else None,
        parent_trace_id=str(trace.parent_trace_id) if trace.parent_trace_id else None,
        source=trace.source,
        name=trace.name,
        model=trace.model or "",
        environment=trace.environment or "production",
        metadata=payloads["metadata"] or {},
        input=payloads["input"] or {},
        output=payloads["output"] or {},
        payload_meta=payload_meta_fields,
        tags=trace.tags or [],
        started_at=_as_iso(trace.started_at) or "",
        ended_at=_as_iso(trace.ended_at),
        duration_ms=trace.duration_ms,
        input_tokens=int(trace.input_tokens or 0),
        output_tokens=int(trace.output_tokens or 0),
        total_tokens=int(trace.total_tokens or 0),
        input_cost=float(trace.input_cost or 0),
        output_cost=float(trace.output_cost or 0),
        total_cost=float(trace.total_cost or 0),
        status=trace.status,
        error_message=trace.error_message,
        failure_cause=getattr(trace, "failure_cause", None),
        level=trace.level,
        created_at=_as_iso(trace.created_at) or "",
        updated_at=_as_iso(trace.updated_at) or "",
        agent_name=agent_name,
        agent_icon=agent_icon,
        user_email=user_email,
        has_scores=has_scores,
    )


def _payload_fields(
    **fields: Any,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]] | None]:
    """Bound payload fields for transport and describe what was held back.

    The durable store keeps trace payloads whole, so a single span can carry
    megabytes.  List and detail responses therefore carry a *preview* — bounded
    but structurally intact, so ``output.reasoning`` and ``input.messages``
    remain addressable — plus a ``payload_meta`` entry naming every field the
    caller must fetch from the payload endpoint to see in full.
    """
    previews: dict[str, Any] = {}
    meta: dict[str, dict[str, Any]] = {}
    for name, value in fields.items():
        preview, truncated = build_display_preview(value)
        previews[name] = preview
        field_meta = payload_meta(value, preview_truncated=truncated)
        if field_meta is not None:
            meta[name] = field_meta
    return previews, (meta or None)


def _span_to_response(span: LLMSpan) -> LLMSpanSchema:
    payloads, payload_meta_fields = _payload_fields(
        input=span.input,
        output=span.output,
        metadata=span.metadata_json,
    )
    return LLMSpanSchema(
        id=str(span.id),
        trace_id=str(span.trace_id),
        parent_span_id=str(span.parent_span_id) if span.parent_span_id else None,
        type=span.type,
        name=span.name,
        model=span.model,
        model_parameters=span.model_parameters,
        input=payloads["input"] or {},
        output=payloads["output"] or {},
        metadata=payloads["metadata"] or {},
        payload_meta=payload_meta_fields,
        level=span.level,
        status_message=span.status_message,
        started_at=_as_iso(span.started_at) or "",
        ended_at=_as_iso(span.ended_at),
        duration_ms=span.duration_ms,
        time_to_first_token=span.time_to_first_token,
        input_tokens=span.input_tokens,
        output_tokens=span.output_tokens,
        input_cost=float(span.input_cost) if span.input_cost is not None else None,
        output_cost=float(span.output_cost) if span.output_cost is not None else None,
        total_cost=float(span.total_cost) if span.total_cost is not None else None,
        usage_details=span.usage_details,
        tool_call_id=span.tool_call_id,
        tool_arguments=coerce_tool_arguments_object(span.tool_arguments),
        stop_reason=span.stop_reason,
        position=span.position,
        created_at=_as_iso(span.created_at) or "",
    )


def _score_to_response(score: LLMScoreModel) -> LLMScoreSchema:
    return LLMScoreSchema(
        id=str(score.id),
        trace_id=str(score.trace_id),
        span_id=str(score.span_id) if score.span_id else None,
        user_id=str(score.user_id) if score.user_id else None,
        name=score.name,
        source=score.source,
        data_type=score.data_type,
        value=score.value,
        string_value=score.string_value,
        comment=score.comment,
        metadata=score.metadata_json or {},
        created_at=_as_iso(score.created_at) or "",
    )


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        default=str,
    ).encode("utf-8")


def _trace_export_base_dir(trace_id: uuid.UUID, index: int) -> str:
    return f"traces/{index:04d}-{trace_id}"


def _write_trace_bundle_entry(
    zf: zipfile.ZipFile,
    *,
    trace: TraceResponse,
    spans: list[LLMSpanSchema],
    scores: list[LLMScoreSchema],
    index: int,
) -> None:
    trace_uuid = parse_uuid(trace.id, label="trace_id")
    base_dir = _trace_export_base_dir(trace_uuid, index)
    zf.writestr(f"{base_dir}/trace.json", _json_bytes(trace.model_dump(mode="json")))
    zf.writestr(
        f"{base_dir}/spans.json",
        _json_bytes([item.model_dump(mode="json") for item in spans]),
    )
    zf.writestr(
        f"{base_dir}/scores.json",
        _json_bytes([item.model_dump(mode="json") for item in scores]),
    )


def _trace_root_span_response(
    trace: LLMTrace,
    *,
    agent_name: str | None,
) -> LLMSpanSchema:
    root_id = f"t-{trace.id}"
    metadata: dict[str, Any] = {
        **(trace.metadata_json or {}),
        "synthetic_root": True,
        "observation_kind": "trace_root",
        "trace_source": trace.source,
    }
    if agent_name:
        metadata["agent_name"] = agent_name
    # The synthetic root mirrors the trace's own payloads, so it needs the same
    # transport bounding — and the same pointer to the payload endpoint, which
    # for this span resolves against the trace row rather than a span row.
    payloads, payload_meta_fields = _payload_fields(
        input=trace.input,
        output=trace.output,
    )
    return LLMSpanSchema(
        id=root_id,
        trace_id=str(trace.id),
        parent_span_id=None,
        type="SPAN",
        name=agent_name or "Agent",
        model=trace.model or None,
        model_parameters=None,
        input=payloads["input"] or {},
        output=payloads["output"] or {},
        metadata=metadata,
        payload_meta=payload_meta_fields,
        level=trace.level or "DEFAULT",
        status_message=trace.error_message,
        started_at=_as_iso(trace.started_at) or "",
        ended_at=_as_iso(trace.ended_at),
        duration_ms=trace.duration_ms,
        time_to_first_token=None,
        input_tokens=trace.input_tokens,
        output_tokens=trace.output_tokens,
        input_cost=float(trace.input_cost) if trace.input_cost is not None else None,
        output_cost=float(trace.output_cost) if trace.output_cost is not None else None,
        total_cost=float(trace.total_cost) if trace.total_cost is not None else None,
        usage_details=None,
        tool_call_id=None,
        tool_arguments=None,
        stop_reason=None,
        position=0,
        created_at=_as_iso(trace.created_at) or "",
    )


def _trace_loop_span_response(
    trace: LLMTrace,
    *,
    parent_span_id: str,
) -> LLMSpanSchema:
    return LLMSpanSchema(
        id=f"loop-{trace.id}",
        trace_id=str(trace.id),
        parent_span_id=parent_span_id,
        type="SPAN",
        name="Agent Loop",
        model=trace.model or None,
        model_parameters=None,
        input={},
        output={},
        metadata={
            "synthetic_loop": True,
            "observation_kind": "agent_loop",
            "trace_source": trace.source,
        },
        level="DEFAULT",
        status_message=None,
        started_at=_as_iso(trace.started_at) or "",
        ended_at=_as_iso(trace.ended_at),
        duration_ms=trace.duration_ms,
        time_to_first_token=None,
        input_tokens=None,
        output_tokens=None,
        input_cost=None,
        output_cost=None,
        total_cost=None,
        usage_details=None,
        tool_call_id=None,
        tool_arguments=None,
        stop_reason=None,
        position=1,
        created_at=_as_iso(trace.created_at) or "",
    )


def _build_trace_span_items(
    trace: LLMTrace,
    spans: list[LLMSpan],
    *,
    agent_name: str | None,
) -> list[LLMSpanSchema]:
    """Return trace spans with synthetic root/loop aligned to trace detail API.

    Session explorer must expose the exact same tree semantics as
    ``GET /traces/{trace_id}/spans`` for each trace: synthetic root SPAN,
    optional synthetic "Agent Loop" when iterations are otherwise orphaned,
    and parent remapping for top-level observations.
    """
    items = [_span_to_response(s) for s in spans]

    root = _trace_root_span_response(trace, agent_name=agent_name)
    item_ids = {item.id for item in items}
    has_loop_level = any(item.name.strip().lower().replace("_", " ") == "agent loop" for item in items)
    orphan_iteration_ids = {
        item.id
        for item in items
        if (item.parent_span_id is None or item.parent_span_id not in item_ids) and item.type == "ITERATION"
    }
    inject_loop = (not has_loop_level) and len(orphan_iteration_ids) > 0
    loop = _trace_loop_span_response(trace, parent_span_id=root.id) if inject_loop else None

    remapped_items: list[LLMSpanSchema] = []
    for item in items:
        parent_id = item.parent_span_id
        if parent_id is None or parent_id not in item_ids:
            parent_id = loop.id if loop is not None and item.id in orphan_iteration_ids else root.id
        remapped_items.append(item.model_copy(update={"parent_span_id": parent_id}))

    return [root, *([loop] if loop is not None else []), *remapped_items]


def trace_visible_query(user: User, user_group_ids: list[str]):
    return store_trace_visible_query(user, user_group_ids)


# Keep strong references to in-flight refresh tasks: asyncio holds only a weak
# reference to a bare create_task() result, so without this the GC can cancel the
# background refresh mid-run.
_analytics_refresh_tasks: set[asyncio.Task] = set()


def _parse_agent_ids_list(values: list[str] | None, *, label: str) -> list[uuid.UUID]:
    if not values:
        return []
    return [parse_uuid(value, label=label) for value in values]


async def _load_agents_by_ids(db: AsyncSession, agent_ids: list[uuid.UUID]) -> dict[uuid.UUID, Agent]:
    if not agent_ids:
        return {}
    rows = await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))
    return {agent.id: agent for agent in rows.scalars().all()}


#: Why a non-admin is refused an agent's traces.
TRACE_ACCESS_DETAIL = "Observability access requires agent owner, admin, or config_with_chats permission"


def _assert_can_read_agent_traces(
    *,
    user: User,
    user_group_ids: list[str],
    agents_by_id: dict[uuid.UUID, Agent],
    agent_ids: list[uuid.UUID],
) -> None:
    """A trace holds the conversation it records, so an agent's traces follow
    the rule for its chats (``helpers.can_view_agent_chats``)."""
    if user.is_admin:
        return
    for agent_id in set(agent_ids):
        agent = agents_by_id.get(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        if not can_view_agent_chats(user, agent, user_group_ids):
            raise HTTPException(status_code=403, detail=TRACE_ACCESS_DETAIL)


async def _require_readable_observability_scope(
    *,
    db: AsyncSession,
    user: User,
    user_group_ids: list[str],
    agent_ids: list[uuid.UUID],
) -> None:
    if user.is_admin:
        return
    if not agent_ids:
        raise HTTPException(
            status_code=403,
            detail="Agent-scoped observability is required for non-admin users",
        )
    agents_by_id = await _load_agents_by_ids(db, agent_ids)
    _assert_can_read_agent_traces(
        user=user,
        user_group_ids=user_group_ids,
        agents_by_id=agents_by_id,
        agent_ids=agent_ids,
    )


async def _require_readable_trace_agent(
    *,
    db: AsyncSession,
    user: User,
    user_group_ids: list[str],
    trace: LLMTrace,
) -> None:
    if user.is_admin:
        return
    if trace.agent_id is None:
        raise HTTPException(
            status_code=403,
            detail="Observability access requires an agent-scoped trace",
        )
    agents_by_id = await _load_agents_by_ids(db, [trace.agent_id])
    _assert_can_read_agent_traces(
        user=user,
        user_group_ids=user_group_ids,
        agents_by_id=agents_by_id,
        agent_ids=[trace.agent_id],
    )


async def _require_readable_trace_rows(
    *,
    db: AsyncSession,
    user: User,
    user_group_ids: list[str],
    rows: list[Any],
) -> None:
    if user.is_admin:
        return
    trace_agent_ids = [row[0].agent_id for row in rows if row[0].agent_id is not None]
    if len(trace_agent_ids) != len(rows):
        raise HTTPException(
            status_code=403,
            detail="Observability access requires agent-scoped traces",
        )
    agents_by_id = await _load_agents_by_ids(db, trace_agent_ids)
    _assert_can_read_agent_traces(
        user=user,
        user_group_ids=user_group_ids,
        agents_by_id=agents_by_id,
        agent_ids=trace_agent_ids,
    )


def _usage_reader(user: User) -> SimpleNamespace:
    """*user* reading a team's usage: the store's per-user visibility is skipped.

    That visibility decides whose CONVERSATIONS a person may open; a group
    manager's read is another question, answered by hard-filtering the
    read to the team's members (``TeamScope``) - the same split as the
    platform insights tools' own-usage reader.
    """
    return SimpleNamespace(id=user.id, is_admin=True)


def _team_user_ids(team: TeamScope, requested: str | None) -> str:
    """The team's ``user_ids`` filter, narrowed to the people *requested* names among its members."""
    if not requested:
        return team.user_ids_csv()
    named = [parse_uuid(value, label="user_ids") for value in _split_csv(requested)]
    kept = sorted(str(user_id) for user_id in named if user_id in team.member_ids)
    return ",".join(kept) if kept else str(NO_MEMBER_ID)


@dataclass(frozen=True)
class AnalyticsRead:
    """How one analytics request reads the trace store: as whom, with which filters."""

    filters: AnalyticsFilters
    viewer: Any
    user_group_ids: list[str]
    agent_ids: list[uuid.UUID]
    #: The groups the read is narrowed to (an admin's group filter, a manager's team).
    team: TeamScope | None = None

    def store_kwargs(self) -> dict[str, Any]:
        return {"filters": self.filters, "user": self.viewer, "user_group_ids": self.user_group_ids}


async def _authorize_analytics_scope(
    db: AsyncSession,
    user: User,
    filters: AnalyticsFilters,
) -> AnalyticsRead:
    """Resolve how the caller reads analytics, or refuse them.

    An admin reads the platform, narrowed to the members of ``group_ids`` when
    named. A non-admin naming groups - or naming no agent while managing a
    group - reads the members of the groups they manage. Anyone else
    reads the agents they may open the chats of (``agent_ids``, required).
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    parsed_agent_ids = _parse_agent_ids_csv(filters.agent_ids, label="agent_ids")
    requested_groups = _split_csv(filters.group_ids)
    team: TeamScope | None = None
    if requested_groups or (not user.is_admin and not parsed_agent_ids):
        team = await team_scope(db, user_id=user.id, platform=user.is_admin, requested=requested_groups)
    if team is not None:
        return AnalyticsRead(
            filters=filters.model_copy(update={"user_ids": _team_user_ids(team, filters.user_ids)}),
            viewer=user if user.is_admin else _usage_reader(user),
            user_group_ids=user_group_ids,
            agent_ids=parsed_agent_ids,
            team=team,
        )
    await _require_readable_observability_scope(
        db=db,
        user=user,
        user_group_ids=user_group_ids,
        agent_ids=parsed_agent_ids,
    )
    return AnalyticsRead(filters=filters, viewer=user, user_group_ids=user_group_ids, agent_ids=parsed_agent_ids)


_LIST_GROUP_DESCRIPTION = (
    "Only the traces of the CURRENT members of this group. Repeatable. Administrators only: "
    "a group's managers read its LLM Analytics, never its traces or sessions."
)

#: Why a non-admin is refused a group filter on the trace and session lists.
TRACE_GROUP_FILTER_DETAIL = "Filtering traces and sessions by group is reserved to administrators."


@dataclass(frozen=True)
class _TraceListRead:
    """How a trace or session listing reads the store."""

    user_group_ids: list[str]
    #: The members of the groups an admin named, or ``None`` for an unnarrowed read.
    user_ids: list[uuid.UUID] | None


async def _trace_list_read(
    db: AsyncSession,
    user: User,
    *,
    group_id: list[str] | None,
    agent_id: list[str] | None,
) -> _TraceListRead:
    """An admin's listing, narrowed to the members of the groups named; anyone else's, the agents they may read."""
    user_group_ids = await get_user_group_ids(db, user.id)
    agent_ids = _parse_agent_ids_list(agent_id, label="agent_id")
    requested = [value for value in (group_id or []) if value and value.strip()]
    if not user.is_admin:
        if requested:
            raise HTTPException(status_code=403, detail=TRACE_GROUP_FILTER_DETAIL)
        await _require_readable_observability_scope(
            db=db,
            user=user,
            user_group_ids=user_group_ids,
            agent_ids=agent_ids,
        )
        return _TraceListRead(user_group_ids=user_group_ids, user_ids=None)
    team = await team_scope(db, user_id=user.id, platform=True, requested=requested)
    return _TraceListRead(
        user_group_ids=user_group_ids,
        user_ids=None if team is None else (sorted(team.member_ids) or [NO_MEMBER_ID]),
    )


async def _readable_trace(
    db: AsyncSession,
    *,
    user: User,
    user_group_ids: list[str],
    trace_store: TraceReadStore,
    trace_id: uuid.UUID,
) -> tuple[LLMTrace, str | None, str | None, str | None]:
    """The trace, or the caller's refusal under the rule for the agent's chats.

    404 for a trace the caller cannot see, 403 for a visible agent whose chats
    they may not open. Managing a group opens none of its members' traces.
    """
    detail = await trace_store.get_trace_detail(db, user=user, user_group_ids=user_group_ids, trace_id=trace_id)
    await _require_readable_trace_agent(db=db, user=user, user_group_ids=user_group_ids, trace=detail[0])
    return detail


@router.post(
    "/analytics/refresh",
    response_model=AnalyticsRefreshResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def refresh_analytics(
    admin: User = Depends(require_admin),
):
    task = asyncio.create_task(_run_analytics_refresh_background(str(admin.id)))
    _analytics_refresh_tasks.add(task)
    task.add_done_callback(_analytics_refresh_tasks.discard)
    return AnalyticsRefreshResponse(
        status="started",
        message="Analytics refresh started. You will receive a notification when it finishes.",
    )


async def _run_analytics_refresh_background(user_id: str) -> None:
    """Backfill missing daily analytics aggregates and notify the requester."""
    requester_id = uuid.UUID(user_id)

    try:
        inserted_rows = 0
        today = datetime.now(UTC).date()

        async with async_session() as db:
            # Fill gaps for the last 30 complete days.
            for days_ago in range(30, 0, -1):
                target_date = today - timedelta(days=days_ago)
                inserted_rows += await aggregate_daily_stats(db, target_date=target_date)

            title = "LLM analytics refresh completed"
            summary = (
                f"Recalculation finished. {inserted_rows} aggregated row"
                f"{'s' if inserted_rows != 1 else ''} inserted for missing days."
            )
            await _store_and_publish_refresh_notification(
                db=db,
                user_id=requester_id,
                title=title,
                summary=summary,
                status_value="completed",
            )
    except Exception as exc:
        logger.error("LLM analytics refresh failed: %s", format_exception_message(exc), exc_info=True)
        try:
            async with async_session() as db:
                await _store_and_publish_refresh_notification(
                    db=db,
                    user_id=requester_id,
                    title="LLM analytics refresh failed",
                    summary=(
                        "Recalculation failed. Please retry and check backend logs if the issue persists."
                    ),
                    status_value="failed",
                )
        except Exception:
            logger.error(
                "Failed to persist analytics refresh failure notification",
                exc_info=True,
            )


async def _store_and_publish_refresh_notification(
    *,
    db: AsyncSession,
    user_id: uuid.UUID,
    title: str,
    summary: str,
    status_value: str,
) -> None:
    activity = Activity(
        user_id=user_id,
        activity_type="notification",
        title=title,
        summary=summary,
        extra_data={
            "notification_source": "llm_analytics_refresh",
            "dashboard_path": "/observability",
            "status": status_value,
        },
    )
    db.add(activity)
    await db.commit()

    payload = json.dumps(
        {
            "type": "notification",
            "activity_type": "notification",
            "title": title,
            "summary": summary,
            "notification_source": "llm_analytics_refresh",
            "dashboard_path": "/observability",
            "status": status_value,
        }
    )
    try:
        redis = await redis_client()
        await redis.publish(f"activity:{user_id}", payload)
    except Exception:
        logger.warning(
            "Failed to publish analytics refresh notification to SSE channel",
            exc_info=True,
        )


@router.get("/analytics/summary", response_model=AnalyticsSummaryResponse)
async def analytics_summary(
    filters: AnalyticsFilters = Depends(),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    trace_store: TraceReadStore = Depends(require_trace_read_store),
):
    read = await _authorize_analytics_scope(db, user, filters)
    start, end, prev_start, prev_end = _resolve_date_range(filters.period, filters.date_from, filters.date_to)
    use_daily = _use_daily_stats(start, end)
    payload = await trace_store.analytics_summary(
        db,
        **read.store_kwargs(),
        start=start,
        end=end,
        prev_start=prev_start,
        prev_end=prev_end,
        use_daily=use_daily,
    )
    if read.agent_ids and read.team is None:
        # Quota/message KPIs are only meaningful for an agent-filtered view
        # (the agent Observability tab). Skipping them on the unfiltered
        # global Analytics page avoids two unbounded aggregations over
        # quota_usage and messages on every summary poll; the response model
        # defaults (0 / 0 / None) apply instead. A team read skips them too:
        # they count every user of the agent, not the team's members.
        (
            quota_consumed,
            message_count,
            avg_quota_per_message,
        ) = await _load_quota_message_metrics(db, agent_ids=read.agent_ids)
        payload["quota_consumed"] = quota_consumed
        payload["message_count"] = message_count
        payload["avg_quota_per_message"] = avg_quota_per_message
    return AnalyticsSummaryResponse(**payload)


@router.get("/analytics/cost-by-model", response_model=CostByModelResponse)
async def analytics_cost_by_model(
    filters: AnalyticsFilters = Depends(),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    trace_store: TraceReadStore = Depends(require_trace_read_store),
):
    read = await _authorize_analytics_scope(db, user, filters)
    start, end, _, _ = _resolve_date_range(filters.period, filters.date_from, filters.date_to)
    use_daily = _use_daily_stats(start, end)
    items = await trace_store.analytics_cost_by_model(
        db,
        **read.store_kwargs(),
        start=start,
        end=end,
        use_daily=use_daily,
    )
    return CostByModelResponse(items=[CostByModelItem(**item) for item in items])


@router.get("/analytics/cost-over-time", response_model=CostOverTimeResponse)
async def analytics_cost_over_time(
    filters: AnalyticsFilters = Depends(),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    trace_store: TraceReadStore = Depends(require_trace_read_store),
):
    read = await _authorize_analytics_scope(db, user, filters)
    start, end, _, _ = _resolve_date_range(filters.period, filters.date_from, filters.date_to)
    use_daily = _use_daily_stats(start, end)
    series = await trace_store.analytics_cost_over_time(
        db,
        **read.store_kwargs(),
        start=start,
        end=end,
        use_daily=use_daily,
    )
    return CostOverTimeResponse(series=[CostOverTimePoint(**point) for point in series])


@router.get("/analytics/volume-over-time", response_model=VolumeOverTimeResponse)
async def analytics_volume_over_time(
    filters: AnalyticsFilters = Depends(),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    trace_store: TraceReadStore = Depends(require_trace_read_store),
):
    read = await _authorize_analytics_scope(db, user, filters)
    start, end, _, _ = _resolve_date_range(filters.period, filters.date_from, filters.date_to)
    use_daily = _use_daily_stats(start, end)
    series = await trace_store.analytics_volume_over_time(
        db,
        **read.store_kwargs(),
        start=start,
        end=end,
        use_daily=use_daily,
    )
    return VolumeOverTimeResponse(series=[VolumeOverTimePoint(**point) for point in series])


@router.get("/analytics/latency", response_model=LatencyResponse)
async def analytics_latency(
    filters: AnalyticsFilters = Depends(),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    trace_store: TraceReadStore = Depends(require_trace_read_store),
):
    read = await _authorize_analytics_scope(db, user, filters)
    start, end, _, _ = _resolve_date_range(filters.period, filters.date_from, filters.date_to)
    use_daily = _use_daily_stats(start, end)
    payload = await trace_store.analytics_latency(
        db,
        **read.store_kwargs(),
        start=start,
        end=end,
        use_daily=use_daily,
    )
    return LatencyResponse(**payload)


@router.get("/analytics/cost-by-agent", response_model=CostByAgentResponse)
async def analytics_cost_by_agent(
    filters: AnalyticsFilters = Depends(),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    trace_store: TraceReadStore = Depends(require_trace_read_store),
):
    read = await _authorize_analytics_scope(db, user, filters)
    start, end, _, _ = _resolve_date_range(filters.period, filters.date_from, filters.date_to)
    use_daily = _use_daily_stats(start, end)
    items = await trace_store.analytics_cost_by_agent(
        db,
        **read.store_kwargs(),
        start=start,
        end=end,
        use_daily=use_daily,
    )
    agent_ids = [item["agent_id"] for item in items]
    owners = await agent_owners(db, agent_ids)
    # The caller's own rights, not the usage reader a manager's read takes.
    access = await agent_row_access(db, agent_ids, user=user, user_group_ids=read.user_group_ids)
    return CostByAgentResponse(
        items=[_cost_by_agent_item(item, owners.get(item["agent_id"], {}), access.get(item["agent_id"])) for item in items]
    )


def _cost_by_agent_item(item: dict[str, Any], owner: dict[str, Any], access: dict[str, Any] | None) -> CostByAgentItem:
    """One row of the per-agent breakdown, with what the caller may do with it.

    An agent with no row left and no tombstone is reported deleted and inert.
    """
    access = access or {"agent_status": "deleted", "can_open": False, "last_known_name": None}
    can_open = bool(access["can_open"])
    return CostByAgentItem(
        **{**item, "agent_name": item.get("agent_name") or access.get("last_known_name")},
        **owner,
        agent_status=access["agent_status"],
        can_open=can_open,
        agent_url=f"/agents/{item['agent_id']}" if can_open else None,
    )


#: Why a per-user or per-group read is refused: it is a governance view.
PEOPLE_BREAKDOWN_DETAIL = (
    "Per-user and per-group usage is reserved to administrators and to the managers of a group."
)


async def _usage_rows_by_user(
    db: AsyncSession,
    user: User,
    filters: AnalyticsFilters,
    trace_store: TraceReadStore,
) -> tuple[list[dict[str, Any]], AnalyticsRead]:
    """Per-user usage rows: the platform for an admin, a team for its manager, nobody else."""
    read = await _authorize_analytics_scope(db, user, filters)
    if not user.is_admin and read.team is None:
        raise HTTPException(status_code=403, detail=PEOPLE_BREAKDOWN_DETAIL)
    start, end, _, _ = _resolve_date_range(filters.period, filters.date_from, filters.date_to)
    rows = await trace_store.analytics_usage_by_user(
        db,
        **read.store_kwargs(),
        start=start,
        end=end,
        use_daily=_use_daily_stats(start, end),
    )
    return rows, read


@router.get("/analytics/cost-by-user", response_model=CostByUserResponse)
async def analytics_cost_by_user(
    filters: AnalyticsFilters = Depends(),
    limit: int = Query(100, ge=1, le=1000),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    trace_store: TraceReadStore = Depends(require_trace_read_store),
):
    """Per-user spend: a governance view, for admins and a group's managers (their team).

    A manager's rows name only the groups they manage, never the rest of a
    member's memberships.
    """
    rows, read = await _usage_rows_by_user(db, user, filters, trace_store)
    named_groups = None if user.is_admin or read.team is None else read.team.group_ids
    return CostByUserResponse(**await usage_by_user(db, rows, limit=limit, group_ids=named_groups))


@router.get("/analytics/cost-by-group", response_model=CostByGroupResponse)
async def analytics_cost_by_group(
    filters: AnalyticsFilters = Depends(),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    trace_store: TraceReadStore = Depends(require_trace_read_store),
):
    """Per-group spend, like the per-user view; a group-narrowed read lists those groups alone.

    A manager's groups count their team's members, administrators left out.
    """
    rows, read = await _usage_rows_by_user(db, user, filters, trace_store)
    named_groups = read.team.group_ids if read.team is not None else None
    return CostByGroupResponse(**await usage_by_group(db, rows, group_ids=named_groups, team=not user.is_admin))


@router.get("/analytics/errors", response_model=ErrorsResponse)
async def analytics_errors(
    filters: AnalyticsFilters = Depends(),
    limit: int = Query(ANALYTICS_ERRORS_DEFAULT_LIMIT, ge=1, le=ANALYTICS_ERRORS_MAX_LIMIT),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    trace_store: TraceReadStore = Depends(require_trace_read_store),
):
    read = await _authorize_analytics_scope(db, user, filters)
    start, end, _, _ = _resolve_date_range(filters.period, filters.date_from, filters.date_to)
    items = await trace_store.analytics_errors(
        db,
        **read.store_kwargs(),
        start=start,
        end=end,
        limit=limit,
    )
    return ErrorsResponse(items=[ErrorItem(**item) for item in items])


@router.get("", response_model=TraceListResponse)
async def list_traces(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    trace_store: TraceReadStore = Depends(require_trace_read_store),
    search: str | None = Query(None, description="Search trace name/model/agent name"),
    conversation_id: str | None = Query(None, description="Filter by conversation/session ID"),
    assignment_id: uuid.UUID | None = Query(None, description="Filter by the assignment whose runs produced the trace"),
    assignment_run_id: uuid.UUID | None = Query(None, description="Filter by assignment run"),
    source: list[str] | None = Query(None),
    agent_id: list[str] | None = Query(None),
    model: list[str] | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    failure_cause: list[str] | None = Query(
        None,
        description=(
            "Filter failed traces by structured failure cause. "
            "Repeatable. Accepts any FailureCause value plus the sentinel "
            "'unrecorded', which selects traces carrying no classification "
            "at all — distinct from 'unknown', which means classified and "
            "not attributable."
        ),
    ),
    level: str | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    cursor: datetime | None = Query(None),
    cursor_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    sort_by: str = Query(DEFAULT_TRACE_SORT, pattern=_TRACE_SORT_PATTERN),
    sort_dir: str = Query(DEFAULT_SORT_DIR, pattern=_SORT_DIR_PATTERN),
    offset: int = Query(0, ge=0, le=TRACE_LIST_MAX_OFFSET, description=_LIST_OFFSET_DESCRIPTION),
    reverse: bool = Query(False, description=_LIST_REVERSE_DESCRIPTION),
    group_id: list[str] | None = Query(None, description=_LIST_GROUP_DESCRIPTION),
):
    read = await _trace_list_read(db, user, group_id=group_id, agent_id=agent_id)
    rows, total = await trace_store.list_traces(
        db,
        user=user,
        user_group_ids=read.user_group_ids,
        search=search,
        conversation_id=conversation_id,
        assignment_id=assignment_id,
        assignment_run_id=assignment_run_id,
        source=source,
        agent_id=agent_id,
        model=model,
        status_filter=status_filter,
        failure_cause=_validate_failure_causes(failure_cause),
        level=level,
        date_from=date_from,
        date_to=date_to,
        cursor=cursor,
        cursor_id=cursor_id,
        limit=limit,
        sort_by=sort_by,
        sort_dir=sort_dir,
        offset=offset,
        reverse=reverse,
        user_ids=read.user_ids,
    )

    items = [
        _trace_to_response(
            row[0],
            agent_name=row.agent_name,
            agent_icon=row.agent_icon,
            user_email=row.user_email,
            has_scores=getattr(row, "has_scores", False),
        )
        for row in rows
    ]
    return TraceListResponse(items=items, total=int(total), limit=limit)


@router.get("/sessions", response_model=TraceSessionListResponse)
async def list_trace_sessions(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    trace_store: TraceReadStore = Depends(require_trace_read_store),
    search: str | None = Query(None, description="Search conversation/model/agent name"),
    source: list[str] | None = Query(None),
    agent_id: list[str] | None = Query(None),
    model: list[str] | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    level: str | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    cursor: datetime | None = Query(None),
    cursor_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    sort_by: str = Query(DEFAULT_TRACE_SESSION_SORT, pattern=_TRACE_SESSION_SORT_PATTERN),
    sort_dir: str = Query(DEFAULT_SORT_DIR, pattern=_SORT_DIR_PATTERN),
    offset: int = Query(0, ge=0, le=TRACE_LIST_MAX_OFFSET, description=_LIST_OFFSET_DESCRIPTION),
    reverse: bool = Query(False, description=_LIST_REVERSE_DESCRIPTION),
    group_id: list[str] | None = Query(None, description=_LIST_GROUP_DESCRIPTION),
):
    read = await _trace_list_read(db, user, group_id=group_id, agent_id=agent_id)
    rows, total = await trace_store.list_trace_sessions(
        db,
        user=user,
        user_group_ids=read.user_group_ids,
        user_ids=read.user_ids,
        search=search,
        source=source,
        agent_id=agent_id,
        model=model,
        status_filter=status_filter,
        level=level,
        date_from=date_from,
        date_to=date_to,
        cursor=cursor,
        cursor_id=cursor_id,
        limit=limit,
        sort_by=sort_by,
        sort_dir=sort_dir,
        offset=offset,
        reverse=reverse,
    )

    emails = await session_user_emails(db, [row.conversation_id for row in rows])
    titles = await session_conversation_titles(db, [row.conversation_id for row in rows])
    items = [
        TraceSessionItem(
            conversation_id=str(row.conversation_id),
            conversation_title=titles.get(str(row.conversation_id)),
            user_email=emails.get(str(row.conversation_id)),
            first_activity_at=_as_iso(row.first_activity_at) or "",
            last_activity_at=_as_iso(row.last_activity_at) or "",
            trace_count=_metric_int(row.trace_count),
            error_count=_metric_int(row.error_count),
            agent_count=_metric_int(row.agent_count),
            model_count=_metric_int(row.model_count),
            input_tokens=_metric_int(row.input_tokens),
            output_tokens=_metric_int(row.output_tokens),
            total_tokens=_metric_int(row.total_tokens),
            total_cost=_metric_float(row.total_cost),
        )
        for row in rows
    ]
    return TraceSessionListResponse(items=items, total=int(total), limit=limit)


@router.get("/sessions/{conversation_id}/explorer", response_model=SessionExplorerResponse)
async def get_session_explorer(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    trace_store: TraceReadStore = Depends(require_trace_read_store),
    limit: int = Query(200, ge=1, le=500),
):
    parsed_conversation_id = safe_uuid(conversation_id)
    if parsed_conversation_id is None:
        # Names no session, like an unknown id: the same empty explorer.
        return SessionExplorerResponse(
            session_id=conversation_id,
            traces=[],
            spans_by_trace_id={},
            total_traces=0,
            total_spans=0,
            limit=limit,
        )
    user_group_ids = await get_user_group_ids(db, user.id)
    rows, total = await trace_store.list_traces(
        db,
        user=user,
        user_group_ids=user_group_ids,
        search=None,
        conversation_id=str(parsed_conversation_id),
        source=None,
        agent_id=None,
        model=None,
        status_filter=None,
        level=None,
        date_from=None,
        date_to=None,
        cursor=None,
        cursor_id=None,
        limit=limit,
    )
    await _require_readable_trace_rows(
        db=db,
        user=user,
        user_group_ids=user_group_ids,
        rows=rows,
    )

    traces = [
        _trace_to_response(row[0], agent_name=row.agent_name, agent_icon=row.agent_icon, user_email=row.user_email)
        for row in rows
    ]
    trace_ids = [row[0].id for row in rows]

    grouped_spans = await trace_store.get_spans_for_trace_ids(db, trace_ids=trace_ids)
    spans_by_trace_id: dict[str, list[LLMSpanSchema]] = {}
    for row in rows:
        trace = row[0]
        trace_id = str(trace.id)
        spans_by_trace_id[trace_id] = _build_trace_span_items(
            trace,
            grouped_spans.get(trace_id, []),
            agent_name=row.agent_name,
        )
    total_spans = sum(len(items) for items in spans_by_trace_id.values())

    return SessionExplorerResponse(
        session_id=str(parsed_conversation_id),
        traces=traces,
        spans_by_trace_id=spans_by_trace_id,
        total_traces=int(total),
        total_spans=total_spans,
        limit=limit,
    )


@router.get("/sessions/{conversation_id}/export-bundle")
async def export_session_trace_bundle(
    conversation_id: str,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    trace_store: TraceReadStore = Depends(require_trace_read_store),
):
    parsed_conversation_id = parse_path_id(conversation_id, not_found_detail="No traces found for this session")
    user_group_ids = await get_user_group_ids(db, user.id)

    archive = io.BytesIO()
    exported_trace_ids: list[str] = []
    total = 0
    cursor: datetime | None = None
    cursor_id: str | None = None

    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        # Page through the session batch by batch and serialize each batch into
        # the archive immediately: a session can reach
        # _TRACE_BUNDLE_EXPORT_MAX_TRACES traces carrying full LLM payloads, so
        # traces, spans, and scores must never all be resident in memory at
        # once. Only the (compressed) archive accumulates. The export cap is
        # the loop condition itself, so no iteration can start with a zero
        # remaining budget (no empty-slice batches or no-op bulk fetches).
        while len(exported_trace_ids) < _TRACE_BUNDLE_EXPORT_MAX_TRACES:
            batch_rows, batch_total = await trace_store.list_traces(
                db,
                user=user,
                user_group_ids=user_group_ids,
                search=None,
                conversation_id=str(parsed_conversation_id),
                source=None,
                agent_id=None,
                model=None,
                status_filter=None,
                level=None,
                date_from=None,
                date_to=None,
                cursor=cursor,
                cursor_id=cursor_id,
                limit=_TRACE_BUNDLE_EXPORT_BATCH_SIZE,
            )
            if not exported_trace_ids:
                total = int(batch_total)
            if not batch_rows:
                break

            remaining = _TRACE_BUNDLE_EXPORT_MAX_TRACES - len(exported_trace_ids)
            batch_rows = batch_rows[:remaining]
            batch_trace_ids = [row[0].id for row in batch_rows]
            grouped_spans = await trace_store.get_spans_for_trace_ids(db, trace_ids=batch_trace_ids)
            grouped_scores = await trace_store.get_scores_for_trace_ids(db, trace_ids=batch_trace_ids)

            for offset, row in enumerate(batch_rows):
                trace = row[0]
                spans = _build_trace_span_items(
                    trace,
                    grouped_spans.get(str(trace.id), []),
                    agent_name=row.agent_name,
                )
                scores = [_score_to_response(item) for item in grouped_scores.get(str(trace.id), [])]
                trace_resp = _trace_to_response(
                    trace,
                    agent_name=row.agent_name,
                    agent_icon=row.agent_icon,
                    user_email=row.user_email,
                    # trace.json must agree with scores.json in the same bundle
                    # entry: derive has_scores from the materialized scores, not
                    # the list-query probe (which can be stale or race with
                    # score creation/deletion).
                    has_scores=bool(scores),
                )
                _write_trace_bundle_entry(
                    zf,
                    trace=trace_resp,
                    spans=spans,
                    scores=scores,
                    index=len(exported_trace_ids) + offset + 1,
                )

            exported_trace_ids.extend(str(trace_id) for trace_id in batch_trace_ids)

            last_trace = batch_rows[-1][0]
            cursor = _normalize_utc(last_trace.started_at)
            cursor_id = str(last_trace.id)
            if len(batch_rows) < _TRACE_BUNDLE_EXPORT_BATCH_SIZE:
                break

        if not exported_trace_ids:
            raise HTTPException(status_code=404, detail="No traces found for this session")

        manifest = {
            "bundle_type": "trace_bundle",
            "export_mode": "session",
            "provider": trace_store.provider,
            "session_id": str(parsed_conversation_id),
            "trace_count": len(exported_trace_ids),
            "total_available_traces": total,
            "truncated": total > len(exported_trace_ids),
            "generated_at": datetime.now(UTC).isoformat(),
            "layout": {
                "session": "session.json",
                "per_trace": "traces/<order>-<trace_id>/{trace.json,spans.json,scores.json}",
            },
        }
        zf.writestr("manifest.json", _json_bytes(manifest))
        zf.writestr(
            "session.json",
            _json_bytes(
                {
                    "session_id": str(parsed_conversation_id),
                    "trace_ids": exported_trace_ids,
                    "total_traces_in_bundle": len(exported_trace_ids),
                }
            ),
        )

    filename = f"trace_session_bundle_{parsed_conversation_id}.zip"
    await log_audit(
        db,
        user=user,
        action="export_bundle",
        entity_type="trace_bundle",
        entity_id=parsed_conversation_id,
        entity_name=f"Session {parsed_conversation_id}",
        summary=f"Exported session trace bundle ({len(exported_trace_ids)} traces)",
        request=request,
        details={
            "export_mode": "session",
            "provider": trace_store.provider,
            "session_id": str(parsed_conversation_id),
            "trace_count": len(exported_trace_ids),
            "total_available_traces": total,
            "truncated": total > len(exported_trace_ids),
        },
    )
    logger.info(
        "session trace bundle exported: session_id=%s user_id=%s traces=%s",
        parsed_conversation_id,
        user.id,
        len(exported_trace_ids),
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    # The archive is already fully materialized in memory, so return a plain
    # Response: a StreamingResponse would keep the get_db() session (and its
    # pooled connection) open until the client finishes downloading the body.
    return Response(
        content=archive.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": build_content_disposition(filename)},
    )


@router.get("/{trace_id}/export-bundle")
async def export_trace_bundle(
    trace_id: str,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    trace_store: TraceReadStore = Depends(require_trace_read_store),
):
    parsed_trace_id = parse_path_id(trace_id, not_found_detail="Trace not found")
    user_group_ids = await get_user_group_ids(db, user.id)
    trace, agent_name, agent_icon, user_email = await trace_store.get_trace_detail(
        db,
        user=user,
        user_group_ids=user_group_ids,
        trace_id=parsed_trace_id,
    )
    spans = await trace_store.get_trace_spans(
        db,
        user=user,
        user_group_ids=user_group_ids,
        trace_id=parsed_trace_id,
    )
    span_items = _build_trace_span_items(trace, spans, agent_name=agent_name)
    raw_scores = await trace_store.get_trace_scores(
        db,
        user=user,
        user_group_ids=user_group_ids,
        trace_id=parsed_trace_id,
    )
    score_items = [_score_to_response(item) for item in raw_scores]
    trace_resp = _trace_to_response(
        trace,
        agent_name=agent_name,
        agent_icon=agent_icon,
        user_email=user_email,
        # trace.json must agree with scores.json in the same bundle entry.
        has_scores=bool(score_items),
    )

    manifest = {
        "bundle_type": "trace_bundle",
        "export_mode": "agent_span",
        "provider": trace_store.provider,
        "trace_id": str(parsed_trace_id),
        # span_items[0] is the synthetic trace-root span prepended by
        # _build_trace_span_items (id "t-<trace_id>"), not a stored agent span.
        "root_span_id": span_items[0].id if span_items else None,
        "generated_at": datetime.now(UTC).isoformat(),
        "layout": {
            "manifest": "manifest.json",
            "trace": "traces/0001-<trace_id>/{trace.json,spans.json,scores.json}",
        },
    }

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", _json_bytes(manifest))
        _write_trace_bundle_entry(
            zf,
            trace=trace_resp,
            spans=span_items,
            scores=score_items,
            index=1,
        )

    filename = f"trace_bundle_{parsed_trace_id}.zip"
    await log_audit(
        db,
        user=user,
        action="export_bundle",
        entity_type="trace_bundle",
        entity_id=parsed_trace_id,
        entity_name=trace.name or f"Trace {parsed_trace_id}",
        summary=f"Exported trace bundle ({len(span_items)} spans, {len(score_items)} scores)",
        request=request,
        details={
            "export_mode": "agent_span",
            "provider": trace_store.provider,
            "trace_id": str(parsed_trace_id),
            "span_count": len(span_items),
            "score_count": len(score_items),
        },
    )
    logger.info(
        "trace bundle exported: trace_id=%s user_id=%s spans=%s scores=%s",
        parsed_trace_id,
        user.id,
        len(span_items),
        len(score_items),
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    # Fully in-memory archive: a plain Response rather than a StreamingResponse,
    # so the download does not pin the session for as long as the body streams.
    return Response(
        content=archive.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": build_content_disposition(filename)},
    )


@router.get("/{trace_id}", response_model=TraceResponse)
async def get_trace(
    trace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    trace_store: TraceReadStore = Depends(require_trace_read_store),
):
    parsed_trace_id = parse_path_id(trace_id, not_found_detail="Trace not found")
    user_group_ids = await get_user_group_ids(db, user.id)
    trace, agent_name, agent_icon, user_email = await _readable_trace(
        db, user=user, user_group_ids=user_group_ids, trace_store=trace_store, trace_id=parsed_trace_id
    )
    return _trace_to_response(trace, agent_name=agent_name, agent_icon=agent_icon, user_email=user_email)


@router.get("/{trace_id}/spans", response_model=SpanListResponse)
async def get_trace_spans(
    trace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    trace_store: TraceReadStore = Depends(require_trace_read_store),
):
    parsed_trace_id = parse_path_id(trace_id, not_found_detail="Trace not found")
    user_group_ids = await get_user_group_ids(db, user.id)
    trace, agent_name, _, _ = await _readable_trace(
        db, user=user, user_group_ids=user_group_ids, trace_store=trace_store, trace_id=parsed_trace_id
    )
    spans = await trace_store.get_trace_spans(
        db,
        user=user,
        user_group_ids=user_group_ids,
        trace_id=parsed_trace_id,
    )
    items = _build_trace_span_items(trace, spans, agent_name=agent_name)
    return SpanListResponse(items=items, total=len(items))


_PAYLOAD_FIELDS = ("input", "output", "metadata")

# Media is addressed by digest; validated before use so a malformed value fails
# as a bad request instead of reaching the payload walk.
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class PayloadResponse(BaseModel):
    """One trace/span payload field, complete."""

    field: str
    # ``True`` when the value was read back from object storage rather than
    # from the row itself — surfaced so the UI can say where it came from.
    offloaded: bool = False
    size_bytes: int | None = None
    sha256: str | None = None
    # Set when the payload was never stored: object storage was unreachable at
    # write time and the value exceeded the inline fallback ceiling.  ``value``
    # then holds the preview that was captured instead of the whole payload.
    unavailable: bool = False
    error: str | None = None
    value: Any = None


async def _payload_response(raw: Any, field: str) -> PayloadResponse:
    """Resolve one stored payload field into its complete value."""
    if not is_offload_envelope(raw):
        return PayloadResponse(
            field=field,
            offloaded=False,
            size_bytes=payload_size_bytes(raw),
            value=raw,
        )

    if raw.get("offload_failed") or not raw.get("object_name"):
        # Nothing to fetch — report the gap explicitly rather than handing back
        # a preview that looks like the whole payload.
        return PayloadResponse(
            field=field,
            offloaded=True,
            size_bytes=raw.get("size_bytes"),
            sha256=raw.get("sha256"),
            unavailable=True,
            error=str(raw.get("error") or "payload was not stored"),
            value=raw.get("preview"),
        )

    try:
        value = await resolve_payload(raw)
    except Exception as exc:
        logger.warning(
            "Failed to load offloaded trace payload %s: %s",
            raw.get("object_name"),
            format_exception_message(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The stored payload could not be read from object storage.",
        ) from exc

    return PayloadResponse(
        field=field,
        offloaded=True,
        size_bytes=raw.get("size_bytes"),
        sha256=raw.get("sha256"),
        value=value,
    )


async def _media_response(raw: Any, field: str, sha256: str) -> Response:
    """Return the bytes of one media blob referenced from a payload field.

    The blob is addressed by its sha256 *within the payload the caller already
    has access to*, never by object name.  That keeps the endpoint from being a
    read primitive for arbitrary objects in the bucket: a digest that is not
    referenced by this payload simply is not found, whatever it names.

    The payload is resolved first, so a media reference still works when the
    payload it lives in was itself offloaded.
    """
    if not sha256 or not _SHA256_RE.fullmatch(sha256):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="sha256 must be a 64-character hex digest",
        )

    try:
        payload = await resolve_payload(raw)
    except Exception as exc:
        logger.warning(
            "Failed to load offloaded trace payload while resolving media: %s",
            format_exception_message(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The stored payload could not be read from object storage.",
        ) from exc

    reference = find_media_reference(payload, sha256)
    if reference is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No media with that digest in the {field} payload.",
        )
    if reference.get("offload_failed") or not reference.get("object_name"):
        # The blob was never stored: object storage was unreachable at write
        # time.  Report the gap rather than a generic 404, which would read as
        # "wrong digest" and send the caller looking in the wrong place.
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=str(reference.get("error") or "media was not stored"),
        )

    try:
        resolved = await resolve_media(reference)
    except Exception as exc:
        logger.warning(
            "Failed to load trace media %s: %s",
            reference.get("object_name"),
            format_exception_message(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The stored media could not be read from object storage.",
        ) from exc

    if resolved is None:  # pragma: no cover - guarded by the checks above
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Media reference could not be resolved.",
        )
    data, content_type = resolved
    return Response(
        content=data,
        media_type=content_type,
        headers={
            # Immutable: the URL is content-addressed by digest, so the bytes
            # behind it can never change. Private and keyed on the credential
            # that was authorised for them.
            "Cache-Control": "private, max-age=86400, immutable",
            "Vary": "Authorization",
            "Content-Disposition": f'inline; filename="{sha256[:16]}"',
        },
    )


def _validated_payload_field(field: str) -> str:
    if field not in _PAYLOAD_FIELDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"field must be one of {', '.join(_PAYLOAD_FIELDS)}",
        )
    return field


@router.get("/{trace_id}/payload", response_model=PayloadResponse)
async def get_trace_payload(
    trace_id: str,
    field: str = Query("output", description="input | output | metadata"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    trace_store: TraceReadStore = Depends(require_trace_read_store),
):
    """Return one of a trace's payload fields in full.

    List and detail responses carry bounded previews so the Explorer stays
    responsive; this is how a caller gets the complete audit payload, whether it
    was stored inline or offloaded to object storage.
    """
    checked_field = _validated_payload_field(field)
    parsed_trace_id = parse_path_id(trace_id, not_found_detail="Trace not found")
    user_group_ids = await get_user_group_ids(db, user.id)
    trace, _, _, _ = await _readable_trace(
        db, user=user, user_group_ids=user_group_ids, trace_store=trace_store, trace_id=parsed_trace_id
    )
    raw = trace.metadata_json if checked_field == "metadata" else getattr(trace, checked_field)
    return await _payload_response(raw, checked_field)


@router.get("/{trace_id}/payload/media")
async def get_trace_payload_media(
    trace_id: str,
    field: str = Query("input", description="input | output | metadata"),
    sha256: str = Query(..., description="Digest of the media blob to fetch"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    trace_store: TraceReadStore = Depends(require_trace_read_store),
):
    """Return the raw bytes of one media blob referenced by a trace payload."""
    checked_field = _validated_payload_field(field)
    parsed_trace_id = parse_path_id(trace_id, not_found_detail="Trace not found")
    user_group_ids = await get_user_group_ids(db, user.id)
    trace, _, _, _ = await _readable_trace(
        db, user=user, user_group_ids=user_group_ids, trace_store=trace_store, trace_id=parsed_trace_id
    )
    raw = trace.metadata_json if checked_field == "metadata" else getattr(trace, checked_field)
    return await _media_response(raw, checked_field, sha256)


@router.get("/{trace_id}/spans/{span_id}/payload/media")
async def get_span_payload_media(
    trace_id: str,
    span_id: str,
    field: str = Query("input", description="input | output | metadata"),
    sha256: str = Query(..., description="Digest of the media blob to fetch"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    trace_store: TraceReadStore = Depends(require_trace_read_store),
):
    """Return the raw bytes of one media blob referenced by a span payload.

    Scoped to the span that carries the reference, exactly like the payload
    endpoint it accompanies — a digest is only fetchable through the payload it
    belongs to.
    """
    checked_field = _validated_payload_field(field)
    parsed_trace_id = parse_path_id(trace_id, not_found_detail="Trace not found")
    user_group_ids = await get_user_group_ids(db, user.id)
    trace, _, _, _ = await _readable_trace(
        db, user=user, user_group_ids=user_group_ids, trace_store=trace_store, trace_id=parsed_trace_id
    )

    if span_id == f"t-{parsed_trace_id}":
        raw = trace.metadata_json if checked_field == "metadata" else getattr(trace, checked_field)
        return await _media_response(raw, checked_field, sha256)

    span = await trace_store.get_trace_span(
        db,
        user=user,
        user_group_ids=user_group_ids,
        trace_id=parsed_trace_id,
        span_id=parse_path_id(span_id, not_found_detail="Span not found for this trace"),
    )
    raw = span.metadata_json if checked_field == "metadata" else getattr(span, checked_field)
    return await _media_response(raw, checked_field, sha256)


@router.get("/{trace_id}/spans/{span_id}/payload", response_model=PayloadResponse)
async def get_span_payload(
    trace_id: str,
    span_id: str,
    field: str = Query("output", description="input | output | metadata"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    trace_store: TraceReadStore = Depends(require_trace_read_store),
):
    """Return one of a span's payload fields in full.

    The synthetic trace-root span (``t-<trace_id>``) carries the trace's own
    payloads, so it resolves against the trace row.
    """
    checked_field = _validated_payload_field(field)
    parsed_trace_id = parse_path_id(trace_id, not_found_detail="Trace not found")
    user_group_ids = await get_user_group_ids(db, user.id)
    trace, _, _, _ = await _readable_trace(
        db, user=user, user_group_ids=user_group_ids, trace_store=trace_store, trace_id=parsed_trace_id
    )

    if span_id == f"t-{parsed_trace_id}":
        raw = trace.metadata_json if checked_field == "metadata" else getattr(trace, checked_field)
        return await _payload_response(raw, checked_field)

    # One row, not the whole trace: span payloads are stored whole, so loading
    # every span here would pull megabytes to serve a single field.
    span = await trace_store.get_trace_span(
        db,
        user=user,
        user_group_ids=user_group_ids,
        trace_id=parsed_trace_id,
        span_id=parse_path_id(span_id, not_found_detail="Span not found for this trace"),
    )
    raw = span.metadata_json if checked_field == "metadata" else getattr(span, checked_field)
    return await _payload_response(raw, checked_field)


@router.get("/{trace_id}/scores", response_model=ScoreListResponse)
async def get_trace_scores(
    trace_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    trace_store: TraceReadStore = Depends(require_trace_read_store),
):
    parsed_trace_id = parse_path_id(trace_id, not_found_detail="Trace not found")
    user_group_ids = await get_user_group_ids(db, user.id)
    scores = await trace_store.get_trace_scores(
        db,
        user=user,
        user_group_ids=user_group_ids,
        trace_id=parsed_trace_id,
    )
    items = [_score_to_response(s) for s in scores]
    return ScoreListResponse(items=items, total=len(items))


@router.post("/{trace_id}/scores", response_model=LLMScoreSchema, status_code=status.HTTP_201_CREATED)
async def create_trace_score(
    trace_id: str,
    body: LLMScoreCreate,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    trace_store: TraceReadStore = Depends(require_trace_read_store),
):
    parsed_trace_id = parse_path_id(trace_id, not_found_detail="Trace not found")
    user_group_ids = await get_user_group_ids(db, user.id)
    score = await trace_store.create_trace_score(
        db,
        user=user,
        user_group_ids=user_group_ids,
        trace_id=parsed_trace_id,
        name=body.name,
        data_type=body.data_type,
        value=body.value,
        string_value=body.string_value,
        comment=body.comment,
        metadata=body.metadata,
    )
    return _score_to_response(score)


@router.delete("/{trace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_trace(
    trace_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    trace_store: TraceReadStore = Depends(require_trace_read_store),
):
    parsed_trace_id = parse_path_id(trace_id, not_found_detail="Trace not found")
    admin_group_ids = await get_user_group_ids(db, admin.id)

    await trace_store.delete_trace(
        db,
        admin=admin,
        admin_group_ids=admin_group_ids,
        trace_id=parsed_trace_id,
    )
    # Sweep the trace's whole object-storage prefix once the rows are gone.  Both
    # offloaded payloads and media blobs live under ``traces/<trace_id>/``, and a
    # sweep needs no pointer to find them — which matters because media
    # referenced from *inside* an offloaded payload is not visible in the rows at
    # all.  Runs after the delete: ``delete_trace`` owns the access check, so
    # reaching this line means the trace existed and was ours to remove.
    await delete_trace_objects([parsed_trace_id])
