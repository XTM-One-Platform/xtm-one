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

"""Trace read-store abstraction by provider."""
# ruff: noqa: S608

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from types import SimpleNamespace
from typing import Any, Literal, Protocol

from fastapi import HTTPException
from sqlalchemy import (
    Date,
    DateTime,
    Float,
    Integer,
    and_,
    cast,
    exists,
    func,
    literal,
    literal_column,
    not_,
    null,
    or_,
    select,
    text,
    true,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.helpers import parse_uuid, user_visible_filter
from app.core.utils import coerce_tool_arguments_object
from app.models.agent import Agent
from app.models.agent_tombstone import AgentTombstone
from app.models.assignment import AssignmentRun
from app.models.llm_trace import LLM_TRACE_SOURCES, LLMDailyStats, LLMSpan, LLMTrace
from app.models.llm_trace import LLMScore as LLMScoreModel
from app.models.platform_settings import PlatformSettings
from app.models.user import User
from app.services.failure_causes import UNRECORDED_FAILURE_CAUSE
from app.services.tracing.clickhouse_client import (
    HTTPClickHouseClient,
    has_clickhouse_runtime_config,
)
from app.services.tracing.feedback_scores import (
    FEEDBACK_PER_TRACE_SQL_CLICKHOUSE,
    FEEDBACK_SCORE_NAME,
    begin_score_action,
    end_score_action,
    feedback_per_trace_subquery,
    new_score_id,
)
from app.services.tracing.latency_histogram import (
    bucket_expression_sql,
    merge_histograms,
    normalize_histogram,
    percentile_from_histogram,
)

TraceStoreProvider = Literal["postgres", "clickhouse"]


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_agent_ids_csv(value: str | None, *, label: str) -> list[uuid.UUID]:
    return [parse_uuid(item, label=label) for item in split_csv(value)]


def analytics_user_ids(filters: Any) -> list[uuid.UUID]:
    """The ``user_ids`` an analytics read is narrowed to: the traces those people
    started. Absent (the REST dashboard's filters carry none) = no narrowing."""
    return parse_agent_ids_csv(getattr(filters, "user_ids", None), label="user_ids")


#: The analytics windows back from now, as the dashboard and the agent tools
#: offer them; ``custom`` takes both bounds instead.
ANALYTICS_PERIODS: dict[str, timedelta] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
    "180d": timedelta(days=180),
    "1y": timedelta(days=365),
}

#: ``AnalyticsFilters.period``: every window, and ``custom``.
ANALYTICS_PERIOD_PATTERN = f"^({'|'.join([*ANALYTICS_PERIODS, 'custom'])})$"

#: The error groups ``analytics_errors`` returns unless asked for more: the
#: agent Observability tab's top list. The dashboard pages through up to
#: ``ANALYTICS_ERRORS_MAX_LIMIT``.
ANALYTICS_ERRORS_DEFAULT_LIMIT = 20
ANALYTICS_ERRORS_MAX_LIMIT = 500


def resolve_date_range(
    period: str,
    date_from: datetime | None,
    date_to: datetime | None,
) -> tuple[datetime, datetime, datetime, datetime]:
    now = datetime.now(UTC)
    if period == "custom":
        if date_from is None or date_to is None:
            raise HTTPException(status_code=400, detail="date_from and date_to are required when period=custom")
        # Naive = UTC, as the rollup's day bounds and the ClickHouse store read
        # it: asyncpg would convert a naive bound through the process's local
        # zone, and one cannot even be compared with an aware bound.
        start, end = _normalize_utc(date_from), _normalize_utc(date_to)
    elif period in ANALYTICS_PERIODS:
        start, end = now - ANALYTICS_PERIODS[period], now
    else:
        raise HTTPException(status_code=400, detail=f"Invalid period: {period}")

    if end <= start:
        raise HTTPException(status_code=400, detail="date_to must be after date_from")

    span = end - start
    prev_start = start - span
    prev_end = start
    return start, end, prev_start, prev_end


async def session_user_emails(db: AsyncSession, conversation_ids: list[Any]) -> dict[str, str | None]:
    """``conversation id -> email`` of the person each session belongs to.

    Read from the conversation in PostgreSQL whichever trace store serves the
    sessions: a session row is an aggregate over traces and carries no user.
    """
    from app.models.chat import Conversation

    ids = [parsed for value in conversation_ids if (parsed := _as_uuid(value)) is not None]
    if not ids:
        return {}
    rows = await db.execute(
        select(Conversation.id, User.email)
        .outerjoin(User, User.id == Conversation.user_id)
        .where(Conversation.id.in_(ids))
    )
    return {str(conversation_id): email for conversation_id, email in rows.all()}


async def session_conversation_titles(db: AsyncSession, conversation_ids: list[Any]) -> dict[str, str]:
    """``conversation id -> title`` of each session's conversation.

    The session list names a session by its conversation's title, never its id;
    read from PostgreSQL whichever trace store serves the sessions. A
    conversation deleted since its traces is absent.
    """
    from app.models.chat import Conversation

    ids = [parsed for value in conversation_ids if (parsed := _as_uuid(value)) is not None]
    if not ids:
        return {}
    rows = await db.execute(select(Conversation.id, Conversation.title).where(Conversation.id.in_(ids)))
    return {str(conversation_id): title for conversation_id, title in rows.all()}


async def agent_owners(db: AsyncSession, agent_ids: list[Any]) -> dict[str, dict[str, Any]]:
    """``agent id -> owner`` of each agent of a cost breakdown.

    Read from the agent in PostgreSQL whichever trace store served the rows:
    ``owner_id`` / ``owner_name`` / ``owner_email`` name the person, and a
    company-managed agent, which has none (invariant 9), says so with
    ``is_company_managed``. An agent deleted since its traces is absent.
    """
    ids = [parsed for value in agent_ids if (parsed := _as_uuid(value)) is not None]
    if not ids:
        return {}
    rows = await db.execute(
        select(Agent.id, Agent.user_id, Agent.is_company_managed, User.full_name, User.email)
        .outerjoin(User, User.id == Agent.user_id)
        .where(Agent.id.in_(ids))
    )
    return {
        str(agent_id): {
            "owner_id": str(user_id) if user_id else None,
            "owner_name": full_name,
            "owner_email": email,
            "is_company_managed": bool(company_managed),
        }
        for agent_id, user_id, company_managed, full_name, email in rows.all()
    }


async def agent_row_access(
    db: AsyncSession,
    agent_ids: list[Any],
    *,
    user: Any,
    user_group_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """``agent id -> what the caller may do with it`` for a per-agent analytics row.

    ``can_open`` is :func:`user_visible_filter` for the CALLER (an
    administrator, the owner, a user or group share, a company-managed agent),
    the predicate ``GET /agents/{id}`` applies, so a row links exactly where the
    agent page opens. ``agent_status`` is ``active``, ``disabled`` or
    ``deleted``; a deleted agent carries its last known name from its tombstone
    (``last_known_name``) and is never opened. Pass the real caller, never the
    usage reader a manager's analytics read takes.
    """
    ids = [parsed for value in agent_ids if (parsed := _as_uuid(value)) is not None]
    if not ids:
        return {}
    enabled = dict((await db.execute(select(Agent.id, Agent.enabled).where(Agent.id.in_(ids)))).all())
    visible: set[uuid.UUID] = set()
    if enabled:
        visible = set(
            (
                await db.execute(
                    select(Agent.id).where(
                        Agent.id.in_(list(enabled)),
                        user_visible_filter(Agent, user.id, user_group_ids, is_admin=bool(user.is_admin)),
                    )
                )
            )
            .scalars()
            .all()
        )
    gone = [agent_id for agent_id in ids if agent_id not in enabled]
    tombstones: dict[uuid.UUID, str] = {}
    if gone:
        tombstones = dict(
            (await db.execute(select(AgentTombstone.agent_id, AgentTombstone.name).where(AgentTombstone.agent_id.in_(gone)))).all()
        )
    access: dict[str, dict[str, Any]] = {}
    for agent_id in ids:
        if agent_id in enabled:
            access[str(agent_id)] = {
                "agent_status": "active" if enabled[agent_id] is not False else "disabled",
                "can_open": agent_id in visible,
                "last_known_name": None,
            }
        else:
            access[str(agent_id)] = {
                "agent_status": "deleted",
                "can_open": False,
                "last_known_name": tombstones.get(agent_id),
            }
    return access


def _as_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def use_daily_stats(start: datetime, end: datetime) -> bool:
    normalized_start = start.replace(microsecond=0)
    normalized_end = end.replace(microsecond=0)
    return (normalized_end - normalized_start) > timedelta(days=7)


#: The longest trace retention Settings > Usage and quotas > Observability accepts
#: (``trace_retention_days``, at most 365): an agent deleted before it has no
#: trace left anywhere.
_LONGEST_TRACE_RETENTION_DAYS = 365


def _deleted_agent_trace_filter(agent_id_column: Any, user_id_column: Any, user: User):
    """A row of an agent that no longer exists, readable by its own user
    and by the agent's owner when it was deleted (``agent_tombstones``), until
    the retention purges it. Needs the ``agents`` outer join of the query."""
    from app.services.conversation_continuity import deleted_agent_owner_clause

    return and_(
        agent_id_column.is_not(None),
        Agent.id.is_(None),
        or_(user_id_column == user.id, deleted_agent_owner_clause(agent_id_column, user.id)),
    )


def trace_visible_query(user: User, user_group_ids: list[str]):
    """Single source of truth for trace visibility.

    ``agent_tombstones`` is joined too: a deleted agent's traces keep its last
    name for the listing and the search.
    """
    base = (
        select(LLMTrace)
        .outerjoin(Agent, Agent.id == LLMTrace.agent_id)
        .outerjoin(AgentTombstone, AgentTombstone.agent_id == LLMTrace.agent_id)
    )
    if user.is_admin:
        # Every trace but a sandbox copy's (#8255): an experiment is not usage.
        return base.where(or_(Agent.id.is_(None), Agent.is_sandbox.is_(False)))

    from app.services.activity_visibility import agent_activity_shown

    visible_agent_filter = and_(
        LLMTrace.agent_id.is_not(None),
        user_visible_filter(Agent, user.id, user_group_ids),
        # A trace of an agent whose activity is hidden from non-admins
        # (#11624) stays visible to the person it ran for only.
        or_(LLMTrace.user_id == user.id, agent_activity_shown(LLMTrace.agent_id, user.id)),
    )
    ga_filter = and_(
        LLMTrace.source == "general-chat",
        LLMTrace.user_id == user.id,
    )
    own_trace_filter = and_(
        LLMTrace.user_id == user.id,
        LLMTrace.agent_id.is_(None),
    )
    deleted_agent_filter = _deleted_agent_trace_filter(LLMTrace.agent_id, LLMTrace.user_id, user)
    return base.where(or_(visible_agent_filter, ga_filter, own_trace_filter, deleted_agent_filter))


def build_trace_filters(
    filters: Any,
    *,
    user: User,
    user_group_ids: list[str],
    start: datetime,
    end: datetime,
):
    query = trace_visible_query(user, user_group_ids).where(
        LLMTrace.started_at >= start,
        LLMTrace.started_at < end,
    )

    agent_ids = parse_agent_ids_csv(filters.agent_ids, label="agent_ids")
    if agent_ids:
        query = query.where(LLMTrace.agent_id.in_(agent_ids))

    user_ids = analytics_user_ids(filters)
    if user_ids:
        query = query.where(LLMTrace.user_id.in_(user_ids))

    models = split_csv(filters.models)
    if models:
        query = query.where(LLMTrace.model.in_(models))

    sources = split_csv(filters.sources)
    if sources:
        query = query.where(LLMTrace.source.in_(sources))

    return query


def sandbox_rollup_excluded():
    """The rollup rows of every agent but a sandbox copy (#8255), the admin
    twin of :func:`trace_visible_query`'s exclusion: the rollup has no
    ``agents`` join, so it reads the copies' ids."""
    return or_(
        LLMDailyStats.agent_id.is_(None),
        LLMDailyStats.agent_id.not_in(select(Agent.id).where(Agent.is_sandbox.is_(True))),
    )


def daily_stats_visible_filter(user: User, user_group_ids: list[str]):
    """Rollup twin of :func:`trace_visible_query`'s visibility predicate.

    The two MUST agree, otherwise the same viewer sees different figures either
    side of the 7-day threshold that switches the read path from raw traces to
    the rollup. That is exactly why ``llm_daily_stats`` carries ``user_id``: the
    rollup used to drop every ``agent_id IS NULL`` row for non-admins, so their
    general-chat / proxy / agentless volume vanished from long-window views while
    the live path counted it.

    For an admin, every row but a sandbox copy's (#8255). Rollup rows written before
    ``user_id`` existed carry NULL and therefore match neither user-scoped arm -
    the same rows a non-admin could see before, no more and no less.
    """
    if user.is_admin:
        return sandbox_rollup_excluded()
    from app.services.activity_visibility import agent_activity_shown

    visible_agent_filter = and_(
        LLMDailyStats.agent_id.is_not(None),
        user_visible_filter(Agent, user.id, user_group_ids),
        # Same narrowing as `trace_visible_query` (#11624).
        or_(LLMDailyStats.user_id == user.id, agent_activity_shown(LLMDailyStats.agent_id, user.id)),
    )
    ga_filter = and_(
        LLMDailyStats.source == "general-chat",
        LLMDailyStats.user_id == user.id,
    )
    own_filter = and_(
        LLMDailyStats.user_id == user.id,
        LLMDailyStats.agent_id.is_(None),
    )
    deleted_agent_filter = _deleted_agent_trace_filter(LLMDailyStats.agent_id, LLMDailyStats.user_id, user)
    return or_(visible_agent_filter, ga_filter, own_filter, deleted_agent_filter)


def _utc_day(value: datetime) -> date_type:
    """The UTC calendar day of *value* (naive = UTC): a rollup day is a UTC day."""
    return _normalize_utc(value).astimezone(UTC).date()


def _utc_midnight(day: date_type) -> datetime:
    """00:00 UTC of *day*, where its rollup row starts."""
    return datetime.combine(day, datetime.min.time(), tzinfo=UTC)


def utc_date(column: Any):
    """SQL: the UTC calendar day of a ``timestamptz`` column.

    ``(column AT TIME ZONE 'UTC')::date``, the key ``trace_storage`` gives a
    rollup day. ``date(column)`` is computed in the session TimeZone, which
    the engine does not pin, so a live day could land on its neighbour. The
    zone is a literal, not a bind: a SELECT and its GROUP BY must render the
    same expression.
    """
    return cast(func.timezone(literal_column("'UTC'"), column), Date)


def build_stats_filters(
    filters: Any,
    *,
    user: User,
    user_group_ids: list[str],
    start: datetime,
    end: datetime,
):
    query = select(LLMDailyStats).outerjoin(Agent, Agent.id == LLMDailyStats.agent_id).where(
        LLMDailyStats.date >= _utc_day(start),
        LLMDailyStats.date <= _utc_day(end),
    )

    visible = daily_stats_visible_filter(user, user_group_ids)
    if visible is not None:
        query = query.where(visible)

    agent_ids = parse_agent_ids_csv(filters.agent_ids, label="agent_ids")
    if agent_ids:
        query = query.where(LLMDailyStats.agent_id.in_(agent_ids))

    user_ids = analytics_user_ids(filters)
    if user_ids:
        query = query.where(LLMDailyStats.user_id.in_(user_ids))

    models = split_csv(filters.models)
    if models:
        query = query.where(LLMDailyStats.model.in_(models))

    sources = split_csv(filters.sources)
    if sources:
        query = query.where(LLMDailyStats.source.in_(sources))

    return query


def _feedback_avg_subquery():
    """One row per trace: its feedback value, one rating per rater.

    Joining ``llm_scores`` directly multiplies a trace's row by its number of
    feedback scores, which over-counts SUM(cost)/SUM(tokens)/error counts and
    weights AVG(duration) by feedback volume. Pre-aggregating feedback to a
    single row per trace keeps those aggregates over distinct traces while still
    yielding a correct AVG feedback. The rule lives in ``feedback_scores``.
    """
    return feedback_per_trace_subquery()


def _escape_like(term: str) -> str:
    r"""Escape LIKE wildcards so a user search term matches literally.

    Without this a ``%`` or ``_`` typed into the search box acts as a SQL LIKE
    wildcard (e.g. "50%" would match everything). Backslash is the escape char,
    so escape it first, then the two wildcard metacharacters. Callers must pass
    ``escape="\\"`` to ``ilike`` so Postgres honours the escaping.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --- Sorting -------------------------------------------------------------------
# The observability tables (Traces, Trace Sessions) sort server-side, so the
# column a user clicked arrives from a query string and lands in an ORDER BY.
# Hence an explicit allow-list, shared by both read stores: it keeps the value
# out of SQL unless it names a real column, and it guarantees a sorted view
# behaves the same whichever trace store the platform runs on.
#
# The trace list deliberately omits Agent and Score: both live outside the trace
# row (``agents``, ``llm_scores``), and the ClickHouse path cannot order by them
# without a cross-store join. A header that sorts on one provider and silently
# does nothing on another is worse than a plain header.
DEFAULT_TRACE_SORT = "started_at"
DEFAULT_TRACE_SESSION_SORT = "last_activity_at"
DEFAULT_SORT_DIR = "desc"

TRACE_SORT_FIELDS = (
    DEFAULT_TRACE_SORT,
    "name",
    "model",
    "duration_ms",
    "total_tokens",
    "total_cost",
    "status",
)

TRACE_SESSION_SORT_FIELDS = (
    DEFAULT_TRACE_SESSION_SORT,
    "conversation_id",
    "trace_count",
    "error_count",
    "agent_count",
    "model_count",
    "total_tokens",
    "total_cost",
)

# Only these sort columns can hold NULL. It matters because ``NULLS LAST`` is
# not free: on PostgreSQL it no longer matches the default ordering of a btree
# index, so pinning it onto the NOT NULL default column would cost the trace
# list the index-ordered scan it has always had. Emitting it only where a NULL
# is actually possible keeps the default order's SQL byte-identical.
_NULLABLE_TRACE_SORT_FIELDS = frozenset({"model", "duration_ms", "total_tokens", "total_cost"})

_TRACE_SORT_COLUMNS = {
    "started_at": LLMTrace.started_at,
    "name": LLMTrace.name,
    "model": LLMTrace.model,
    "duration_ms": LLMTrace.duration_ms,
    "total_tokens": LLMTrace.total_tokens,
    "total_cost": LLMTrace.total_cost,
    "status": LLMTrace.status,
}


def _normalize_sort_dir(sort_dir: str | None) -> str:
    return "asc" if sort_dir == "asc" else "desc"


def normalize_trace_sort(sort_by: str | None) -> str:
    """Resolve a trace-list sort column, falling back to the default order.

    The API rejects unknown columns outright, but the stores are also called
    from non-API entry points (builtin tools, exports). Normalizing once here —
    rather than per decision — keeps the column, the keyset eligibility and the
    NULLS LAST choice from ever describing different orders.
    """
    return sort_by if sort_by in TRACE_SORT_FIELDS else DEFAULT_TRACE_SORT


def normalize_trace_session_sort(sort_by: str | None) -> str:
    """Sessions twin of :func:`normalize_trace_sort`."""
    return sort_by if sort_by in TRACE_SESSION_SORT_FIELDS else DEFAULT_TRACE_SESSION_SORT


def _read_dir(sort_dir: str, *, reverse: bool) -> str:
    """Direction a column is read in: as requested, or flipped for a far-end read."""
    if not reverse:
        return sort_dir
    return "asc" if sort_dir == "desc" else "desc"


def _order_term(column: Any, sort_dir: str, *, nulls_last: bool, reverse: bool = False):
    """ORDER BY term for a sortable column.

    PostgreSQL sorts NULLs last ascending but first descending; for a nullable
    column, pinning them to the bottom in both directions matches what
    ``EntityTable`` already does client-side. A descending sort on Cost must open
    on the most expensive traces, not on a page of traces that never recorded a
    cost at all.

    ``reverse`` reads the same order from its last row: the direction flips and
    the NULLs pinned to the bottom of the order come first, so every row sits
    exactly where it would counting from the other end.
    """
    ordered = column.desc() if _read_dir(sort_dir, reverse=reverse) == "desc" else column.asc()
    if not nulls_last:
        return ordered
    return ordered.nulls_first() if reverse else ordered.nulls_last()


def trace_sort_is_keyset(sort_by: str, sort_dir: str, offset: int, *, reverse: bool = False) -> bool:
    """Whether a trace-list request can still be paged by keyset cursor.

    The cursor predicate is a plain ``<`` on ``started_at``, so it only holds for
    the default newest-first order read from its first row. Any other order, and
    the far-end read of that one, pages by OFFSET instead, and a cursor left over
    in the URL is ignored rather than silently truncating the page.
    """
    return sort_by == DEFAULT_TRACE_SORT and sort_dir == "desc" and not offset and not reverse


def trace_session_sort_is_keyset(sort_by: str, sort_dir: str, offset: int, *, reverse: bool = False) -> bool:
    """Keyset twin of :func:`trace_sort_is_keyset` for the sessions list."""
    return sort_by == DEFAULT_TRACE_SESSION_SORT and sort_dir == "desc" and not offset and not reverse


# Deepest OFFSET the trace and session lists accept. The database still reads
# and discards every skipped row, so an unbounded offset lets one request force
# a scan of the whole table. The bound holds for a far-end read too: a page near
# the end of a long list is reached with ``reverse`` and a small offset, and a
# page deeper than this from both ends is not served at all.
TRACE_LIST_MAX_OFFSET = 100_000


def page_is_past_end(offset: int, total: int) -> bool:
    """Whether an OFFSET page starts beyond the last matching row.

    Such a page is empty by definition, so the page query is not run. A zero
    offset always runs it: that is the first page, and the keyset path.
    """
    return offset > 0 and offset >= total


def _today_live_stats_query(
    filters: Any,
    *,
    user: User,
    user_group_ids: list[str],
    window_start: datetime,
    window_end: datetime,
    day_ranges: Any | None = None,
):
    """Not-yet-rolled-up raw traces aggregated into the ``llm_daily_stats`` shape.

    Built straight from ``build_trace_filters`` - the same visibility and the same
    filter parsing the live (<=7d) path uses - so the live figures line up both
    with the rollups they are unioned with (which now apply the mirrored
    ``daily_stats_visible_filter``) and with what the same viewer sees on a short
    window.

    Rows are keyed by each trace's own day - not a constant "today" - because
    the live slice can span several days, and ``day_ranges`` (the
    :func:`unrolled_day_ranges` of the window) restricts the scan to exactly the
    days no rollup covers, each read whole like a rollup row.
    """
    trace_base = _live_traces(
        filters,
        user=user,
        user_group_ids=user_group_ids,
        start=window_start,
        end=window_end,
        day_ranges=day_ranges,
    ).subquery()
    feedback_sub = _feedback_avg_subquery()
    day = utc_date(trace_base.c.started_at)
    query = (
        select(
            day.label("date"),
            trace_base.c.agent_id.label("agent_id"),
            trace_base.c.user_id.label("user_id"),
            func.coalesce(trace_base.c.model, "").label("model"),
            trace_base.c.source.label("source"),
            func.count(trace_base.c.id).label("trace_count"),
            func.count(trace_base.c.id).filter(trace_base.c.status == "error").label("error_count"),
            func.coalesce(func.sum(trace_base.c.input_tokens), 0).label("total_input_tokens"),
            func.coalesce(func.sum(trace_base.c.output_tokens), 0).label("total_output_tokens"),
            func.sum(trace_base.c.total_cost).filter(trace_base.c.total_cost.is_not(None)).label("total_cost"),
            func.avg(trace_base.c.duration_ms).label("avg_duration_ms"),
            func.percentile_cont(0.5).within_group(trace_base.c.duration_ms).label("p50_duration_ms"),
            func.percentile_cont(0.95).within_group(trace_base.c.duration_ms).label("p95_duration_ms"),
            func.avg(feedback_sub.c.feedback_value).label("avg_feedback_score"),
            trace_base.c.attributed_user_id.label("attributed_user_id"),
            trace_base.c.attribution.label("attribution"),
            trace_base.c.department_id.label("department_id"),
            func.coalesce(func.sum(trace_base.c.cached_input_tokens), 0).label("total_cached_input_tokens"),
        )
        .select_from(trace_base.outerjoin(feedback_sub, feedback_sub.c.trace_id == trace_base.c.id))
        # Group by the BARE model column, not func.coalesce(model, ""). A second
        # coalesce here emits a DISTINCT bind parameter for the "" default, so
        # Postgres cannot match it to the SELECT's coalesce and rejects the query
        # ("column ... model must appear in the GROUP BY"). The SELECT coalesce is
        # still valid because it is a deterministic function of the grouped
        # ``model`` column; NULL is its own group and renders as "" (consistent
        # with the stored llm_daily_stats.model), and the outer analytics queries
        # re-aggregate by model so the result is identical.
        .group_by(
            day,
            trace_base.c.agent_id,
            trace_base.c.user_id,
            trace_base.c.model,
            trace_base.c.source,
            trace_base.c.attributed_user_id,
            trace_base.c.attribution,
            trace_base.c.department_id,
        )
    )
    return query


def unrolled_day_ranges(start: datetime, end: datetime, today: date_type):
    """The UTC days of the window no rollup covers, as ``[day_start, day_end)``.

    A day is covered once ``aggregate_daily_stats`` wrote its rows: it writes
    every group of a day in one transaction and ``rebuild_daily_stats``
    replaces them in one, so "has a row" is a per-day, viewer-independent fact.
    Today never is (the rollup arm stops at yesterday). Every other day is
    served live: the trailing ones the aggregator has not reached yet, and a
    hole in the middle of the window - the backfill after downtime
    builds the newest days first, and a day whose aggregation failed stays
    behind the later ones that succeeded.

    Evaluated inside the statement that reads the rollup, so both arms see one
    snapshot and a day is counted exactly once whatever commits meanwhile.

    Only the days from the oldest trace to the newest are walked: no other day
    has anything to serve, and a custom window can span a century.
    """
    first_day = cast(literal(_utc_day(start), Date), DateTime())
    last_day = cast(literal(_utc_day(end), Date), DateTime())
    one_day = literal_column("interval '1 day'")
    window_days = select(
        func.generate_series(
            # No trace at all: an empty series.
            func.greatest(first_day, func.coalesce(_traced_day(newest=False), last_day + one_day)),
            func.least(last_day, _traced_day(newest=True)),
            one_day,
        ).label("day")
    ).subquery("window_days")
    rolled_up = exists().where(
        LLMDailyStats.date == cast(window_days.c.day, Date),
        LLMDailyStats.date < today,
    )
    return (
        select(
            func.timezone("UTC", window_days.c.day).label("day_start"),
            func.timezone("UTC", window_days.c.day + one_day).label("day_end"),
        )
        .where(~rolled_up)
        .subquery("unrolled_days")
    )


def _traced_day(*, newest: bool):
    """SQL: the UTC midnight of the oldest (or newest) day that has a trace,
    NULL when none has.

    One ``(source, started_at)`` index probe per source: no index leads with
    ``started_at``, so a plain MIN / MAX would read every trace.
    """
    order = LLMTrace.started_at.desc() if newest else LLMTrace.started_at.asc()
    edges = [
        select(LLMTrace.started_at).where(LLMTrace.source == source).order_by(order).limit(1).scalar_subquery()
        for source in LLM_TRACE_SOURCES
    ]
    edge = func.greatest(*edges) if newest else func.least(*edges)
    return cast(utc_date(edge), DateTime())


def _live_traces(
    filters: Any,
    *,
    user: User,
    user_group_ids: list[str],
    start: datetime,
    end: datetime,
    day_ranges: Any | None,
):
    """The window's traces, restricted to *day_ranges* when given.

    With *day_ranges* the window is read in whole UTC days, its first and last
    included: a rollup row cannot be split, so a day the rollup arm would read
    whole is read whole live too - cut at the window's instants, it would read
    differently the moment it is rolled up.

    Each unrolled day is a ``(source, started_at)`` range on
    ``ix_llm_traces_source_started_at`` (every source is allowed, so the filter
    changes nothing but the plan): there is no index on ``started_at`` alone.
    The ranges drive a LATERAL scan, fenced with ``OFFSET 0`` so it is not
    pulled up into a plain join: then the planner, which cannot estimate the
    day set, reads every trace of the window and filters them against it -
    a 90-day window read the whole table.
    """
    if day_ranges is not None:
        start = _utc_midnight(_utc_day(start))
        end = _utc_midnight(_utc_day(end) + timedelta(days=1))
    query = build_trace_filters(
        filters,
        user=user,
        user_group_ids=user_group_ids,
        start=start,
        end=end,
    ).where(LLMTrace.source.in_(LLM_TRACE_SOURCES))
    if day_ranges is None:
        return query
    day_traces = (
        query.where(
            LLMTrace.started_at >= day_ranges.c.day_start,
            LLMTrace.started_at < day_ranges.c.day_end,
        )
        .offset(0)
        .lateral("day_traces")
    )
    return select(day_traces).select_from(day_ranges).join(day_traces, true())


async def _skip_jit(db: AsyncSession) -> None:
    """Turn JIT off for the rest of the transaction, before a rollup-served read.

    The live arm's day set and its per-day ranges only exist at run time, so
    the planner costs them blind - a thousand series rows, each half a percent
    of the traces - and the estimate crosses ``jit_above_cost`` by orders of
    magnitude: every such read compiled for ~250 ms to run for a few. JIT
    changes no result, and ``SET LOCAL`` ends with the transaction.
    """
    # Spelled at the call: the routes' commit analysis reads a SET as a setting, not a write.
    await db.execute(text("SET LOCAL jit = off"))


def combined_daily_stats_subquery(
    filters: Any,
    *,
    user: User,
    user_group_ids: list[str],
    start: datetime,
    end: datetime,
):
    """Rollup rows UNION ALL a live aggregation of the days no rollup covers.

    ``llm_daily_stats`` is only ever populated up to *yesterday*, yesterday
    itself only by the ~01:00 UTC aggregator run, and a day the backfill has not
    reached (downtime, a failed aggregation) can sit behind later rolled-up
    ones. A range served purely from daily stats silently drops all of those
    days, so we union the rollups with a live aggregation, shaped like
    daily-stats rows, of every day of the window without one
    (:func:`unrolled_day_ranges`).

    Both arms read ``llm_daily_stats`` inside one statement - hence one
    snapshot: however an aggregation or a rebuild commit interleaves with
    this read, a day is served by exactly one arm, never zero nor two. And
    both read whole UTC days, from the one the window opens on through the one
    it closes on - a rollup row cannot be split - so a day reads the same
    before and after it is rolled up.
    """
    now = datetime.now(UTC)
    today = now.date()

    daily_base = (
        build_stats_filters(filters, user=user, user_group_ids=user_group_ids, start=start, end=end)
        .where(LLMDailyStats.date < today)
        .subquery()
    )
    daily_select = select(
        daily_base.c.date.label("date"),
        daily_base.c.agent_id.label("agent_id"),
        # Column order must match _today_live_stats_query exactly - UNION ALL
        # pairs columns positionally, not by label.
        daily_base.c.user_id.label("user_id"),
        daily_base.c.model.label("model"),
        daily_base.c.source.label("source"),
        daily_base.c.trace_count.label("trace_count"),
        daily_base.c.error_count.label("error_count"),
        daily_base.c.total_input_tokens.label("total_input_tokens"),
        daily_base.c.total_output_tokens.label("total_output_tokens"),
        daily_base.c.total_cost.label("total_cost"),
        daily_base.c.avg_duration_ms.label("avg_duration_ms"),
        daily_base.c.p50_duration_ms.label("p50_duration_ms"),
        daily_base.c.p95_duration_ms.label("p95_duration_ms"),
        daily_base.c.avg_feedback_score.label("avg_feedback_score"),
        daily_base.c.attributed_user_id.label("attributed_user_id"),
        daily_base.c.attribution.label("attribution"),
        daily_base.c.department_id.label("department_id"),
        daily_base.c.total_cached_input_tokens.label("total_cached_input_tokens"),
    )

    # The ``date < today`` guard is the rollup arm's, so a stray same-day rollup
    # row is neither served nor allowed to hide today's live traces.
    live_select = _today_live_stats_query(
        filters,
        user=user,
        user_group_ids=user_group_ids,
        window_start=start,
        window_end=end,
        day_ranges=unrolled_day_ranges(start, end, today),
    )
    return daily_select.union_all(live_select).subquery()


async def analytics_stats_subquery(
    db: AsyncSession,
    filters: Any,
    *,
    user: User,
    user_group_ids: list[str],
    start: datetime,
    end: datetime,
    use_daily: bool,
):
    """The window's usage as ``llm_daily_stats``-shaped rows, the way every analytics read serves it.

    Past 7 days, the rollup UNION ALL the days it lacks
    (:func:`combined_daily_stats_subquery`); within 7 days, the window's
    traces themselves, cut at its instants. ``breakdown.py`` groups it by any
    dimension, so its figures are the dashboard's on the same window.
    """
    if use_daily:
        await _skip_jit(db)
        return combined_daily_stats_subquery(filters, user=user, user_group_ids=user_group_ids, start=start, end=end)
    return _today_live_stats_query(
        filters, user=user, user_group_ids=user_group_ids, window_start=start, window_end=end
    ).subquery()


def first_row(result) -> Any | None:
    rows = result.all()
    if not rows:
        return None
    return rows[0]


def metric_float(value: Any) -> float:
    if value is None:
        return 0.0
    return float(value)


def metric_int(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _analytics_summary_from_row(row: Any) -> dict[str, Any]:
    trace_count = metric_int(getattr(row, "trace_count", 0))
    error_count = metric_int(getattr(row, "error_count", 0))
    error_rate = (error_count / trace_count) if trace_count else 0.0
    avg_feedback_score = row.avg_feedback_score if hasattr(row, "avg_feedback_score") else None
    return {
        "total_cost": metric_float(getattr(row, "total_cost", 0)),
        "total_tokens": metric_int(getattr(row, "total_tokens", 0)),
        "trace_count": trace_count,
        "avg_duration_ms": metric_float(getattr(row, "avg_duration_ms", 0)),
        "error_rate": error_rate,
        "avg_feedback_score": (None if avg_feedback_score is None else float(avg_feedback_score)),
    }


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


async def oldest_trace_started_at(db: AsyncSession) -> datetime | None:
    """When the oldest trace the platform still holds started: one ``(source, started_at)`` index probe per source."""
    edges = [
        select(LLMTrace.started_at).where(LLMTrace.source == source).order_by(LLMTrace.started_at.asc()).limit(1).scalar_subquery()
        for source in LLM_TRACE_SOURCES
    ]
    return _normalize_utc((await db.execute(select(func.least(*edges)))).scalar())


async def stats_coverage_start(db: AsyncSession) -> date_type | None:
    """Earliest day this platform can report any observability figure for.

    ``min(llm_daily_stats.date)`` and ``min(llm_traces.started_at)``, whichever is
    older: the rollup outlives the raw-trace purge, so together they bound what is
    answerable. Callers compare it with the requested window start so a window
    reaching further back than any surviving data is reported as such instead of
    silently returning zeroes for those days - the difference between "no activity"
    and "no data" is otherwise invisible.

    Deliberately platform-wide, not viewer-scoped: the question is what data
    exists, not what a given user may see. ``None`` when the platform has neither
    traces nor rollups yet (a brand-new install has nothing to warn about).
    """
    earliest_rollup = (await db.execute(select(func.min(LLMDailyStats.date)))).scalar()
    earliest_trace = (await db.execute(select(func.min(LLMTrace.started_at)))).scalar()
    candidates = [value for value in (earliest_rollup, _normalize_utc(earliest_trace)) if value is not None]
    if not candidates:
        return None
    return min(value.date() if isinstance(value, datetime) else value for value in candidates)


_LATENCY_QUANTILES: tuple[tuple[str, float], ...] = (
    ("p50", 0.5),
    ("p75", 0.75),
    ("p90", 0.9),
    ("p95", 0.95),
    ("p99", 0.99),
)


@dataclass
class _LatencyCell:
    """Accumulator for one output cell (the whole window, one model, one day)."""

    histogram: dict[int, int] = field(default_factory=dict)
    # Today's buckets kept separately: they have no stored p50/p95 column, so the
    # legacy fallback below has to derive their contribution from these.
    live_histogram: dict[int, int] = field(default_factory=dict)
    trace_count: int = 0
    weighted_p50: float = 0.0
    weighted_p95: float = 0.0
    p50_weight: int = 0
    p95_weight: int = 0
    # Set by a row that HAS latency data but no histogram (written before the
    # histogram column existed). Such a cell cannot be merged exactly.
    missing_histogram: bool = False

    def percentiles(self) -> tuple[dict[str, float | None], bool]:
        """``({quantile: ms}, approximate)`` for this cell."""
        if self.histogram and not self.missing_histogram:
            return (
                {name: percentile_from_histogram(self.histogram, q) for name, q in _LATENCY_QUANTILES},
                False,
            )

        weighted_p50, p50_weight = self.weighted_p50, self.p50_weight
        weighted_p95, p95_weight = self.weighted_p95, self.p95_weight
        if self.live_histogram:
            live_count = sum(self.live_histogram.values())
            live_p50 = percentile_from_histogram(self.live_histogram, 0.5)
            live_p95 = percentile_from_histogram(self.live_histogram, 0.95)
            if live_p50 is not None:
                weighted_p50 += live_p50 * live_count
                p50_weight += live_count
            if live_p95 is not None:
                weighted_p95 += live_p95 * live_count
                p95_weight += live_count

        if not p50_weight and not p95_weight:
            # No latency data at all in this cell - not an approximation, just empty.
            return ({name: None for name, _ in _LATENCY_QUANTILES}, False)

        return (
            {
                "p50": (weighted_p50 / p50_weight) if p50_weight else None,
                # A weighted mean of stored percentiles can only reproduce the two
                # percentiles that were stored; the rest stay unknown rather than
                # invented.
                "p75": None,
                "p90": None,
                "p95": (weighted_p95 / p95_weight) if p95_weight else None,
                "p99": None,
            },
            True,
        )


class _LatencyCells:
    """Fans every contributing row into the overall / per-model / per-day cells."""

    def __init__(self) -> None:
        self._overall = _LatencyCell()
        self._by_model: dict[str, _LatencyCell] = {}
        self._by_date: dict[Any, _LatencyCell] = {}

    def _cells_for(self, date_key: Any, model: str | None) -> list[_LatencyCell]:
        cells = [self._overall, self._by_date.setdefault(date_key, _LatencyCell())]
        if model is not None:
            cells.append(self._by_model.setdefault(model, _LatencyCell()))
        return cells

    def add_rollup_row(
        self,
        *,
        date: Any,
        model: str | None,
        trace_count: int,
        p50: float | None,
        p95: float | None,
        histogram: dict[int, int] | None,
    ) -> None:
        for cell in self._cells_for(date, model):
            cell.trace_count += trace_count
            if histogram:
                cell.histogram = merge_histograms((cell.histogram, histogram))
            elif p50 is not None or p95 is not None:
                cell.missing_histogram = True
            if p50 is not None:
                cell.weighted_p50 += float(p50) * trace_count
                cell.p50_weight += trace_count
            if p95 is not None:
                cell.weighted_p95 += float(p95) * trace_count
                cell.p95_weight += trace_count

    def add_live_bucket(self, *, date: Any, model: str | None, bucket: int, count: int) -> None:
        for cell in self._cells_for(date, model):
            cell.trace_count += count
            cell.histogram[bucket] = cell.histogram.get(bucket, 0) + count
            cell.live_histogram[bucket] = cell.live_histogram.get(bucket, 0) + count

    def to_payload(self) -> dict[str, Any]:
        overall, overall_approximate = self._overall.percentiles()
        approximate = overall_approximate

        by_model: list[dict[str, Any]] = []
        for model, cell in self._by_model.items():
            values, cell_approximate = cell.percentiles()
            approximate = approximate or cell_approximate
            by_model.append({"model": model, **values, "trace_count": cell.trace_count})
        by_model.sort(key=lambda item: item["trace_count"], reverse=True)

        daily_median: list[dict[str, Any]] = []
        for date_key in sorted(self._by_date):
            values, cell_approximate = self._by_date[date_key].percentiles()
            approximate = approximate or cell_approximate
            daily_median.append({"date": str(date_key), "p50": values["p50"]})

        return {
            "overall": overall,
            "by_model": by_model,
            "daily_median": daily_median,
            "approximate": approximate,
        }


class TraceReadStore(Protocol):
    provider: TraceStoreProvider

    async def list_traces(self, db: AsyncSession, **kwargs) -> tuple[list[Any], int]:
        pass

    async def list_trace_sessions(self, db: AsyncSession, **kwargs) -> tuple[list[Any], int]:
        pass

    async def get_trace_detail(self, db: AsyncSession, **kwargs) -> tuple[LLMTrace, str | None, str | None, str | None]:
        pass

    async def get_trace_spans(self, db: AsyncSession, **kwargs) -> list[LLMSpan]:
        pass

    async def get_trace_span(self, db: AsyncSession, **kwargs) -> LLMSpan:
        pass

    async def get_spans_for_trace_ids(self, db: AsyncSession, **kwargs) -> dict[str, list[LLMSpan]]:
        pass

    async def get_trace_scores(self, db: AsyncSession, **kwargs) -> list[LLMScoreModel]:
        pass

    async def get_scores_for_trace_ids(self, db: AsyncSession, **kwargs) -> dict[str, list[LLMScoreModel]]:
        pass

    async def create_trace_score(self, db: AsyncSession, **kwargs) -> LLMScoreModel:
        pass

    async def delete_trace(self, db: AsyncSession, **kwargs) -> None:
        pass

    async def analytics_summary(self, db: AsyncSession, **kwargs) -> dict[str, Any]:
        pass

    async def analytics_cost_by_model(self, db: AsyncSession, **kwargs) -> list[dict[str, Any]]:
        pass

    async def analytics_cost_over_time(self, db: AsyncSession, **kwargs) -> list[dict[str, Any]]:
        pass

    async def analytics_volume_over_time(self, db: AsyncSession, **kwargs) -> list[dict[str, Any]]:
        pass

    async def analytics_latency(self, db: AsyncSession, **kwargs) -> dict[str, Any]:
        pass

    async def analytics_cost_by_agent(self, db: AsyncSession, **kwargs) -> list[dict[str, Any]]:
        pass

    async def analytics_usage_by_user(self, db: AsyncSession, **kwargs) -> list[dict[str, Any]]:
        pass

    async def analytics_errors(self, db: AsyncSession, **kwargs) -> list[dict[str, Any]]:
        pass


class _NoopClickHouseReadClient:
    """Fallback client used when no ClickHouse connector is configured."""

    async def query_json_rows(self, query: str) -> list[dict[str, Any]]:
        _ = query
        raise RuntimeError(
            "ClickHouse read client is not configured. "
            "Provide clickhouse_read_client_factory when tracing_store_provider=clickhouse."
        )

    async def insert_json_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        _ = table
        _ = rows
        raise RuntimeError(
            "ClickHouse read client is not configured. "
            "Provide clickhouse_read_client_factory when tracing_store_provider=clickhouse."
        )

    async def execute(self, query: str) -> None:
        _ = query
        raise RuntimeError(
            "ClickHouse read client is not configured. "
            "Provide clickhouse_read_client_factory when tracing_store_provider=clickhouse."
        )

    async def close(self) -> None:
        return None


class _ClickHouseTraceListRow(SimpleNamespace):
    """Compat row object: row[0] is trace, attrs hold joined metadata."""

    def __getitem__(self, idx: int):
        if idx == 0:
            return self.trace
        raise IndexError(idx)


@dataclass(slots=True, frozen=True)
class PostgresTraceReadStore:
    provider: TraceStoreProvider = "postgres"

    async def _get_visible_trace(
        self,
        db: AsyncSession,
        *,
        user: User,
        user_group_ids: list[str],
        trace_id: uuid.UUID,
    ) -> LLMTrace:
        statement = trace_visible_query(user, user_group_ids).where(LLMTrace.id == trace_id)
        result = await db.execute(statement)
        trace = result.scalar_one_or_none()
        if trace is None:
            raise HTTPException(status_code=404, detail="Trace not found")
        return trace

    async def list_traces(
        self,
        db: AsyncSession,
        *,
        user: User,
        user_group_ids: list[str],
        search: str | None,
        conversation_id: str | None,
        source: list[str] | None,
        agent_id: list[str] | None,
        model: list[str] | None,
        status_filter: str | None,
        level: str | None,
        date_from: datetime | None,
        date_to: datetime | None,
        cursor: datetime | None,
        cursor_id: str | None,
        limit: int,
        sort_by: str = DEFAULT_TRACE_SORT,
        sort_dir: str = DEFAULT_SORT_DIR,
        offset: int = 0,
        failure_cause: list[str] | None = None,
        reverse: bool = False,
        assignment_id: uuid.UUID | None = None,
        assignment_run_id: uuid.UUID | None = None,
        user_ids: list[uuid.UUID] | None = None,
    ) -> tuple[list[Any], int]:
        query = trace_visible_query(user, user_group_ids)
        filters = []
        if user_ids:
            filters.append(LLMTrace.user_id.in_(user_ids))
        if source:
            filters.append(LLMTrace.source.in_(source))
        if agent_id:
            parsed_agent_ids = [parse_uuid(item, label="agent_id") for item in agent_id]
            filters.append(LLMTrace.agent_id.in_(parsed_agent_ids))
        if model:
            filters.append(LLMTrace.model.in_(model))
        if status_filter:
            filters.append(LLMTrace.status == status_filter)
        if failure_cause:
            # ``unrecorded`` is not a cause — it selects FAILED traces that
            # carry no classification at all (written before the classifier
            # existed, or by a site not yet wired to it). Kept distinct from
            # ``unknown``, which means "classified, and not attributable":
            # collapsing the two would hide the difference between a gap in
            # instrumentation and a gap in the taxonomy.
            #
            # The ``status`` guard is load-bearing, not defensive: a
            # successful trace also has ``failure_cause IS NULL``, and
            # successes vastly outnumber failures on any real install — so
            # without it this filter returns a page of successes and the
            # instrumentation-gap metric becomes unreadable, which is the one
            # thing keeping ``unrecorded`` separate was for.
            named = [
                value
                for value in failure_cause
                if value != UNRECORDED_FAILURE_CAUSE
            ]
            arms = []
            if named:
                arms.append(LLMTrace.failure_cause.in_(named))
            if UNRECORDED_FAILURE_CAUSE in failure_cause:
                arms.append(
                    and_(
                        LLMTrace.failure_cause.is_(None),
                        LLMTrace.status == "error",
                    )
                )
            filters.append(or_(*arms) if len(arms) > 1 else arms[0])
        if level:
            filters.append(LLMTrace.level == level)
        if date_from:
            filters.append(LLMTrace.started_at >= date_from)
        if date_to:
            filters.append(LLMTrace.started_at <= date_to)
        if search:
            pattern = f"%{_escape_like(search)}%"
            filters.append(
                or_(
                    LLMTrace.name.ilike(pattern, escape="\\"),
                    LLMTrace.model.ilike(pattern, escape="\\"),
                    Agent.name.ilike(pattern, escape="\\"),
                    # A deleted agent by its last name.
                    AgentTombstone.name.ilike(pattern, escape="\\"),
                    LLMTrace.agent_name_snapshot.ilike(pattern, escape="\\"),
                )
            )
        if conversation_id:
            filters.append(LLMTrace.conversation_id == parse_uuid(conversation_id, label="conversation_id"))
        if assignment_run_id:
            filters.append(LLMTrace.assignment_run_id == assignment_run_id)
        if assignment_id:
            # A trace carries its run, not its assignment: the run row is the
            # source of truth, and ``ix_llm_traces_assignment_run_id`` serves
            # the lookup. The ClickHouse store, which cannot reach the run
            # table, matches the ``assignment_id`` the engine stamps on the
            # trace metadata instead - so the two stores differ for a trace
            # carrying only one of the run id and the stamp (a writer passing
            # one to ``trace_llm_call`` without the other) and for a run whose
            # history was deleted (``entities.md``, LLM Trace).
            filters.append(
                LLMTrace.assignment_run_id.in_(
                    select(AssignmentRun.id).where(AssignmentRun.assignment_id == assignment_id)
                )
            )

        for trace_filter in filters:
            query = query.where(trace_filter)

        count_query = select(func.count()).select_from(query.subquery())

        sort_by = normalize_trace_sort(sort_by)
        sort_dir = _normalize_sort_dir(sort_dir)
        sort_column = _TRACE_SORT_COLUMNS[sort_by]
        use_keyset = trace_sort_is_keyset(sort_by, sort_dir, offset, reverse=reverse)

        if use_keyset and cursor_id:
            parsed_cursor_id = parse_uuid(cursor_id, label="cursor_id")
            if cursor:
                query = query.where(
                    or_(
                        LLMTrace.started_at < cursor,
                        and_(LLMTrace.started_at == cursor, LLMTrace.id < parsed_cursor_id),
                    )
                )
            else:
                query = query.where(LLMTrace.id < parsed_cursor_id)
        elif use_keyset and cursor:
            query = query.where(LLMTrace.started_at < cursor)

        total = await db.scalar(count_query) or 0
        if page_is_past_end(offset, int(total)):
            return [], int(total)
        # has_scores: correlated EXISTS so the trace list can render the Score column
        # without a per-row N+1 fetch of llm_scores.
        has_scores_col = (
            select(LLMScoreModel.id)
            .where(LLMScoreModel.trace_id == LLMTrace.id)
            .exists()
            .label("has_scores")
        )
        enriched_query = (
            query.add_columns(
                func.coalesce(Agent.name, AgentTombstone.name, LLMTrace.agent_name_snapshot).label("agent_name"),
                func.coalesce(Agent.icon, AgentTombstone.icon).label("agent_icon"),
                User.email.label("user_email"),
                has_scores_col,
            )
            .outerjoin(User, User.id == LLMTrace.user_id)
            # ``id`` breaks ties so the order is total: without it OFFSET paging
            # could show the same row twice, or skip one, across two pages. A
            # far-end read flips it with the column, or the page counted from the
            # end would not be the one counted from the start.
            .order_by(
                _order_term(
                    sort_column,
                    sort_dir,
                    nulls_last=sort_by in _NULLABLE_TRACE_SORT_FIELDS,
                    reverse=reverse,
                ),
                _order_term(LLMTrace.id, "desc", nulls_last=False, reverse=reverse),
            )
            .limit(limit)
            .offset(offset or None)
        )
        result = await db.execute(enriched_query)
        return result.all(), int(total)

    async def list_trace_sessions(
        self,
        db: AsyncSession,
        *,
        user: User,
        user_group_ids: list[str],
        search: str | None,
        source: list[str] | None,
        agent_id: list[str] | None,
        model: list[str] | None,
        status_filter: str | None,
        level: str | None,
        date_from: datetime | None,
        date_to: datetime | None,
        cursor: datetime | None,
        cursor_id: str | None,
        limit: int,
        sort_by: str = DEFAULT_TRACE_SESSION_SORT,
        sort_dir: str = DEFAULT_SORT_DIR,
        offset: int = 0,
        reverse: bool = False,
        user_ids: list[uuid.UUID] | None = None,
    ) -> tuple[list[Any], int]:
        query = trace_visible_query(user, user_group_ids).where(LLMTrace.conversation_id.is_not(None))
        filters = []
        if user_ids:
            filters.append(LLMTrace.user_id.in_(user_ids))
        if source:
            filters.append(LLMTrace.source.in_(source))
        if agent_id:
            parsed_agent_ids = [parse_uuid(item, label="agent_id") for item in agent_id]
            filters.append(LLMTrace.agent_id.in_(parsed_agent_ids))
        if model:
            filters.append(LLMTrace.model.in_(model))
        if status_filter:
            filters.append(LLMTrace.status == status_filter)
        if level:
            filters.append(LLMTrace.level == level)
        if date_from:
            filters.append(LLMTrace.started_at >= date_from)
        if date_to:
            filters.append(LLMTrace.started_at <= date_to)
        if search:
            pattern = f"%{_escape_like(search)}%"
            filters.append(
                or_(
                    LLMTrace.conversation_id.cast(postgresql.TEXT).ilike(pattern, escape="\\"),
                    LLMTrace.model.ilike(pattern, escape="\\"),
                    Agent.name.ilike(pattern, escape="\\"),
                    AgentTombstone.name.ilike(pattern, escape="\\"),
                    LLMTrace.agent_name_snapshot.ilike(pattern, escape="\\"),
                )
            )

        for trace_filter in filters:
            query = query.where(trace_filter)

        base = query.subquery()
        sessions_query = (
            select(
                base.c.conversation_id.label("conversation_id"),
                func.min(base.c.started_at).label("first_activity_at"),
                func.max(base.c.started_at).label("last_activity_at"),
                func.count(base.c.id).label("trace_count"),
                func.count(base.c.id).filter(base.c.status == "error").label("error_count"),
                func.count(func.distinct(base.c.agent_id)).label("agent_count"),
                func.count(func.distinct(base.c.model)).label("model_count"),
                func.coalesce(func.sum(base.c.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(base.c.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(base.c.total_tokens), 0).label("total_tokens"),
                func.coalesce(func.sum(base.c.total_cost), 0).label("total_cost"),
            )
            .group_by(base.c.conversation_id)
        )
        total = await db.scalar(select(func.count()).select_from(sessions_query.subquery())) or 0
        if page_is_past_end(offset, int(total)):
            return [], int(total)

        sessions_page = sessions_query.subquery()
        paged_query = select(sessions_page)
        sort_by = normalize_trace_session_sort(sort_by)
        sort_dir = _normalize_sort_dir(sort_dir)
        sort_column = sessions_page.c[sort_by]
        use_keyset = trace_session_sort_is_keyset(sort_by, sort_dir, offset, reverse=reverse)
        if use_keyset and cursor_id:
            parsed_cursor_id = parse_uuid(cursor_id, label="cursor_id")
            if cursor:
                paged_query = paged_query.where(
                    or_(
                        sessions_page.c.last_activity_at < cursor,
                        and_(
                            sessions_page.c.last_activity_at == cursor,
                            sessions_page.c.conversation_id > parsed_cursor_id,
                        ),
                    )
                )
            else:
                paged_query = paged_query.where(sessions_page.c.conversation_id > parsed_cursor_id)
        elif use_keyset and cursor:
            paged_query = paged_query.where(sessions_page.c.last_activity_at < cursor)

        result = await db.execute(
            paged_query
            # Every session column is an aggregate over a grouped, non-empty
            # set (or the grouping key itself), so none of them can be NULL.
            .order_by(
                _order_term(sort_column, sort_dir, nulls_last=False, reverse=reverse),
                _order_term(sessions_page.c.conversation_id, "asc", nulls_last=False, reverse=reverse),
            )
            .limit(limit)
            .offset(offset or None)
        )
        return result.all(), int(total)

    async def get_trace_detail(
        self,
        db: AsyncSession,
        *,
        user: User,
        user_group_ids: list[str],
        trace_id: uuid.UUID,
    ) -> tuple[LLMTrace, str | None, str | None, str | None]:
        trace = await self._get_visible_trace(db, user=user, user_group_ids=user_group_ids, trace_id=trace_id)

        agent_name = None
        agent_icon = None
        user_email = None
        if trace.agent_id:
            agent_row = await db.execute(select(Agent.name, Agent.icon).where(Agent.id == trace.agent_id))
            agent_data = agent_row.one_or_none()
            if agent_data is None:
                # A deleted agent keeps its last name.
                agent_row = await db.execute(
                    select(AgentTombstone.name, AgentTombstone.icon).where(AgentTombstone.agent_id == trace.agent_id)
                )
                agent_data = agent_row.one_or_none()
            if agent_data:
                agent_name = agent_data.name
                agent_icon = agent_data.icon
            else:
                agent_name = getattr(trace, "agent_name_snapshot", None)
        if trace.user_id:
            user_row = await db.execute(select(User.email).where(User.id == trace.user_id))
            user_email = user_row.scalar_one_or_none()

        return trace, agent_name, agent_icon, user_email

    async def get_trace_spans(
        self,
        db: AsyncSession,
        *,
        user: User,
        user_group_ids: list[str],
        trace_id: uuid.UUID,
    ) -> list[LLMSpan]:
        trace = await self._get_visible_trace(db, user=user, user_group_ids=user_group_ids, trace_id=trace_id)
        result = await db.execute(
            select(LLMSpan)
            .where(LLMSpan.trace_id == trace.id)
            .order_by(LLMSpan.position.asc(), LLMSpan.started_at.asc())
        )
        return result.scalars().all()

    async def get_trace_span(
        self,
        db: AsyncSession,
        *,
        user: User,
        user_group_ids: list[str],
        trace_id: uuid.UUID,
        span_id: uuid.UUID,
    ) -> LLMSpan:
        """Fetch one span of a visible trace.

        Kept separate from :meth:`get_trace_spans` for the payload endpoint:
        span payloads are stored whole, so loading every span of a trace to
        serve a single field would pull megabytes per request.
        """
        trace = await self._get_visible_trace(db, user=user, user_group_ids=user_group_ids, trace_id=trace_id)
        result = await db.execute(
            select(LLMSpan).where(LLMSpan.trace_id == trace.id, LLMSpan.id == span_id)
        )
        span = result.scalar_one_or_none()
        if span is None:
            raise HTTPException(status_code=404, detail="Span not found for this trace")
        return span

    async def get_spans_for_trace_ids(
        self,
        db: AsyncSession,
        *,
        trace_ids: list[uuid.UUID],
    ) -> dict[str, list[LLMSpan]]:
        if not trace_ids:
            return {}
        result = await db.execute(
            select(LLMSpan)
            .where(LLMSpan.trace_id.in_(trace_ids))
            .order_by(LLMSpan.trace_id.asc(), LLMSpan.position.asc(), LLMSpan.started_at.asc())
        )
        grouped: dict[str, list[LLMSpan]] = {str(trace_id): [] for trace_id in trace_ids}
        for span in result.scalars().all():
            grouped.setdefault(str(span.trace_id), []).append(span)
        return grouped

    async def get_trace_scores(
        self,
        db: AsyncSession,
        *,
        user: User,
        user_group_ids: list[str],
        trace_id: uuid.UUID,
    ) -> list[LLMScoreModel]:
        trace = await self._get_visible_trace(db, user=user, user_group_ids=user_group_ids, trace_id=trace_id)
        result = await db.execute(
            select(LLMScoreModel)
            .where(LLMScoreModel.trace_id == trace.id)
            .order_by(LLMScoreModel.created_at.desc())
        )
        return result.scalars().all()

    async def get_scores_for_trace_ids(
        self,
        db: AsyncSession,
        *,
        trace_ids: list[uuid.UUID],
    ) -> dict[str, list[LLMScoreModel]]:
        if not trace_ids:
            return {}
        result = await db.execute(
            select(LLMScoreModel)
            .where(LLMScoreModel.trace_id.in_(trace_ids))
            .order_by(LLMScoreModel.trace_id.asc(), LLMScoreModel.created_at.desc())
        )
        grouped: dict[str, list[LLMScoreModel]] = {str(trace_id): [] for trace_id in trace_ids}
        for score in result.scalars().all():
            grouped.setdefault(str(score.trace_id), []).append(score)
        return grouped

    async def create_trace_score(
        self,
        db: AsyncSession,
        *,
        user: User,
        user_group_ids: list[str],
        trace_id: uuid.UUID,
        name: str,
        data_type: str,
        value: float | None,
        string_value: str | None,
        comment: str | None,
        metadata: dict[str, Any],
    ) -> LLMScoreModel:
        trace = await self._get_visible_trace(db, user=user, user_group_ids=user_group_ids, trace_id=trace_id)
        # Serialize concurrent submissions for the same (trace, user, name) before
        # the read-then-write below. Without this, two near-simultaneous requests
        # (a double-click or a retried flush) can both observe "no existing row"
        # and both INSERT, recreating the duplicate this guard exists to prevent
        # and skewing avg_feedback_score. A transaction-scoped advisory lock closes
        # that TOCTOU window without a schema migration and is released
        # automatically on commit/rollback; hashtextextended is a stable
        # cross-process hash so every replica serializes on the same key. The
        # chat endpoints and the tracing sink take the same lock, and the row
        # holds the time of the latest action on it: the sink refuses a
        # delayed chat rating older than this one.
        action_at = await begin_score_action(db, trace.id, user.id, name)
        # Idempotent per (trace, user, name): a user re-submitting feedback (a
        # double-click, a corrected thumbs-up -> thumbs-down, or a retried flush)
        # must UPDATE the existing score, not insert a second row - duplicate
        # user_feedback rows would skew avg_feedback_score (the analytics average
        # over name='user_feedback'). `.first()` (not scalar_one_or_none) tolerates
        # any pre-existing duplicates created before this guard.
        existing = (
            await db.execute(
                select(LLMScoreModel)
                .where(
                    LLMScoreModel.trace_id == trace.id,
                    LLMScoreModel.user_id == user.id,
                    LLMScoreModel.name == name,
                )
                .order_by(LLMScoreModel.created_at.desc(), LLMScoreModel.id.desc())
                .limit(1)
            )
        ).scalars().first()
        if existing is not None and existing.created_at > action_at:
            # A later action already holds this rater's score.
            await db.commit()
            await db.refresh(existing)
            return existing
        if existing is not None:
            existing.source = "USER"
            existing.data_type = data_type
            existing.value = value
            existing.string_value = string_value
            existing.comment = comment
            existing.metadata_json = metadata
            existing.created_at = action_at
            score = existing
        else:
            score = LLMScoreModel(
                # Version 7, so a copy into ClickHouse never reads as a legacy
                # retraction there (``feedback_scores.new_score_id``).
                id=new_score_id(),
                trace_id=trace.id,
                user_id=user.id,
                name=name,
                source="USER",
                data_type=data_type,
                value=value,
                string_value=string_value,
                comment=comment,
                metadata_json=metadata,
                created_at=action_at,
            )
            db.add(score)
        if name == FEEDBACK_SCORE_NAME:
            from app.services.trace_storage import record_feedback_day_stale

            await record_feedback_day_stale(db, trace.id)
        await end_score_action(db, action_at)
        await db.commit()
        await db.refresh(score)
        return score

    async def delete_trace(
        self,
        db: AsyncSession,
        *,
        admin: User,
        admin_group_ids: list[str],
        trace_id: uuid.UUID,
    ) -> None:
        statement = trace_visible_query(admin, admin_group_ids).where(LLMTrace.id == trace_id)
        result = await db.execute(statement)
        trace = result.scalar_one_or_none()
        if trace is None:
            raise HTTPException(status_code=404, detail="Trace not found")
        await db.delete(trace)
        await db.commit()

    async def analytics_summary(
        self,
        db: AsyncSession,
        *,
        filters: Any,
        user: User,
        user_group_ids: list[str],
        start: datetime,
        end: datetime,
        prev_start: datetime,
        prev_end: datetime,
        use_daily: bool,
    ) -> dict[str, Any]:
        if use_daily:
            # C2: the current window is served from rollups UNION a live aggregation
            # of every day without a rollup (today, yesterday until the ~01:00 UTC
            # aggregation, a day the backfill has not reached) so 30d/90d summaries
            # are not missing any of them.
            await _skip_jit(db)
            current_sub = combined_daily_stats_subquery(
                filters, user=user, user_group_ids=user_group_ids, start=start, end=end
            )
            # The previous window is in the past, but a day of it the backfill
            # has not reached yet has no rollup and is served live too. It closes
            # on the day the current window opens on, which - read in whole days -
            # counts in both, as its rollup always did.
            previous_sub = combined_daily_stats_subquery(
                filters, user=user, user_group_ids=user_group_ids, start=prev_start, end=prev_end
            )

            current_query = select(
                func.coalesce(func.sum(current_sub.c.trace_count), 0).label("trace_count"),
                func.sum(current_sub.c.total_cost).filter(current_sub.c.total_cost.is_not(None)).label("total_cost"),
                func.coalesce(func.sum(current_sub.c.total_input_tokens + current_sub.c.total_output_tokens), 0).label("total_tokens"),
                # C3: weight by trace_count only over rows that actually have a value,
                # otherwise NULL-duration/NULL-feedback days dilute the average.
                (
                    func.sum(current_sub.c.avg_duration_ms * current_sub.c.trace_count)
                    / func.nullif(
                        func.sum(current_sub.c.trace_count).filter(current_sub.c.avg_duration_ms.is_not(None)),
                        0,
                    )
                ).label("avg_duration_ms"),
                func.coalesce(func.sum(current_sub.c.error_count), 0).label("error_count"),
                (
                    func.sum(current_sub.c.avg_feedback_score * current_sub.c.trace_count)
                    / func.nullif(
                        func.sum(current_sub.c.trace_count).filter(current_sub.c.avg_feedback_score.is_not(None)),
                        0,
                    )
                ).label("avg_feedback_score"),
            )
            previous_query = select(
                func.coalesce(func.sum(previous_sub.c.trace_count), 0).label("trace_count"),
                func.sum(previous_sub.c.total_cost).filter(previous_sub.c.total_cost.is_not(None)).label("total_cost"),
                func.coalesce(func.sum(previous_sub.c.total_input_tokens + previous_sub.c.total_output_tokens), 0).label("total_tokens"),
                (
                    func.sum(previous_sub.c.avg_duration_ms * previous_sub.c.trace_count)
                    / func.nullif(
                        func.sum(previous_sub.c.trace_count).filter(previous_sub.c.avg_duration_ms.is_not(None)),
                        0,
                    )
                ).label("avg_duration_ms"),
                func.coalesce(func.sum(previous_sub.c.error_count), 0).label("error_count"),
                (
                    func.sum(previous_sub.c.avg_feedback_score * previous_sub.c.trace_count)
                    / func.nullif(
                        func.sum(previous_sub.c.trace_count).filter(previous_sub.c.avg_feedback_score.is_not(None)),
                        0,
                    )
                ).label("avg_feedback_score"),
            )
        else:
            current_sub = build_trace_filters(filters, user=user, user_group_ids=user_group_ids, start=start, end=end).subquery()
            previous_sub = build_trace_filters(
                filters,
                user=user,
                user_group_ids=user_group_ids,
                start=prev_start,
                end=prev_end,
            ).subquery()

            # E: pre-aggregate feedback to one row per trace before joining, so a
            # trace with N feedback scores does not multiply SUM(cost)/SUM(tokens)
            # or the error count, and AVG(duration) is not weighted by feedback volume.
            current_feedback = _feedback_avg_subquery()
            previous_feedback = _feedback_avg_subquery()

            current_query = select(
                func.count(func.distinct(current_sub.c.id)).label("trace_count"),
                func.sum(current_sub.c.total_cost).filter(current_sub.c.total_cost.is_not(None)).label("total_cost"),
                func.coalesce(func.sum(current_sub.c.total_tokens), 0).label("total_tokens"),
                func.avg(current_sub.c.duration_ms).label("avg_duration_ms"),
                func.count(func.distinct(current_sub.c.id)).filter(current_sub.c.status == "error").label("error_count"),
                func.avg(current_feedback.c.feedback_value).label("avg_feedback_score"),
            ).select_from(
                current_sub.outerjoin(
                    current_feedback,
                    current_feedback.c.trace_id == current_sub.c.id,
                )
            )
            previous_query = select(
                func.count(func.distinct(previous_sub.c.id)).label("trace_count"),
                func.sum(previous_sub.c.total_cost).filter(previous_sub.c.total_cost.is_not(None)).label("total_cost"),
                func.coalesce(func.sum(previous_sub.c.total_tokens), 0).label("total_tokens"),
                func.avg(previous_sub.c.duration_ms).label("avg_duration_ms"),
                func.count(func.distinct(previous_sub.c.id)).filter(previous_sub.c.status == "error").label("error_count"),
                func.avg(previous_feedback.c.feedback_value).label("avg_feedback_score"),
            ).select_from(
                previous_sub.outerjoin(
                    previous_feedback,
                    previous_feedback.c.trace_id == previous_sub.c.id,
                )
            )

        current_row = first_row(await db.execute(current_query))
        previous_row = first_row(await db.execute(previous_query))
        current = _analytics_summary_from_row(current_row or SimpleNamespace())
        previous = _analytics_summary_from_row(previous_row or SimpleNamespace())
        platform_settings_row = first_row(
            await db.execute(
                select(PlatformSettings.last_aggregation_at.label("last_aggregation_at"))
                .order_by(PlatformSettings.created_at.desc())
                .limit(1)
            )
        )
        last_aggregation_at = _normalize_utc(
            getattr(platform_settings_row, "last_aggregation_at", None)
            if platform_settings_row
            else None
        )
        aggregation_stale = last_aggregation_at is None or (
            datetime.now(UTC) - last_aggregation_at
        ).total_seconds() > 48 * 3600

        unknown_cost_sub = build_trace_filters(
            filters,
            user=user,
            user_group_ids=user_group_ids,
            start=start,
            end=end,
        ).subquery()
        unknown_cost_row = first_row(
            await db.execute(
                select(
                    func.count(unknown_cost_sub.c.id).label("unknown_cost_trace_count")
                ).where(unknown_cost_sub.c.total_cost.is_(None))
            )
        )
        unknown_cost_trace_count = metric_int(
            getattr(unknown_cost_row, "unknown_cost_trace_count", 0)
            if unknown_cost_row
            else 0
        )

        coverage_start = await stats_coverage_start(db)

        return {
            **current,
            "previous": previous,
            "last_aggregation_at": last_aggregation_at,
            "aggregation_stale": aggregation_stale,
            "unknown_cost_trace_count": unknown_cost_trace_count,
            "stats_coverage_start": coverage_start,
            "window_starts_before_coverage": (
                coverage_start is not None and _utc_day(start) < coverage_start
            ),
        }

    async def analytics_cost_by_model(
        self,
        db: AsyncSession,
        *,
        filters: Any,
        user: User,
        user_group_ids: list[str],
        start: datetime,
        end: datetime,
        use_daily: bool,
    ) -> list[dict[str, Any]]:
        if use_daily:
            # C2: rollups UNION a live aggregation of every day without a rollup
            # (today, yesterday until the ~01:00 UTC aggregation, a day the backfill
            # has not reached) so none of them are dropped.
            await _skip_jit(db)
            base = combined_daily_stats_subquery(filters, user=user, user_group_ids=user_group_ids, start=start, end=end)
            query = (
                select(
                    base.c.model.label("model"),
                    func.coalesce(func.sum(base.c.trace_count), 0).label("trace_count"),
                    func.coalesce(func.sum(base.c.total_input_tokens), 0).label("input_tokens"),
                    func.coalesce(func.sum(base.c.total_output_tokens), 0).label("output_tokens"),
                    func.cast(None, postgresql.NUMERIC()).label("input_cost"),
                    func.cast(None, postgresql.NUMERIC()).label("output_cost"),
                    func.sum(base.c.total_cost).filter(base.c.total_cost.is_not(None)).label("total_cost"),
                )
                .where(base.c.model.is_not(None))
                .group_by(base.c.model)
                .order_by(func.coalesce(func.sum(base.c.total_cost), 0).desc())
            )
        else:
            base = build_trace_filters(filters, user=user, user_group_ids=user_group_ids, start=start, end=end).subquery()
            query = (
                select(
                    base.c.model.label("model"),
                    func.count(base.c.id).label("trace_count"),
                    func.coalesce(func.sum(base.c.input_tokens), 0).label("input_tokens"),
                    func.coalesce(func.sum(base.c.output_tokens), 0).label("output_tokens"),
                    func.sum(base.c.input_cost).filter(base.c.input_cost.is_not(None)).label("input_cost"),
                    func.sum(base.c.output_cost).filter(base.c.output_cost.is_not(None)).label("output_cost"),
                    func.sum(base.c.total_cost).filter(base.c.total_cost.is_not(None)).label("total_cost"),
                )
                .where(base.c.model.is_not(None))
                .group_by(base.c.model)
                .order_by(func.coalesce(func.sum(base.c.total_cost), 0).desc())
            )
        rows = (await db.execute(query)).all()
        return [
            {
                "model": row.model,
                "trace_count": metric_int(row.trace_count),
                "input_tokens": metric_int(row.input_tokens),
                "output_tokens": metric_int(row.output_tokens),
                "input_cost": None if row.input_cost is None else float(row.input_cost),
                "output_cost": None if row.output_cost is None else float(row.output_cost),
                "total_cost": metric_float(row.total_cost),
            }
            for row in rows
        ]

    async def analytics_cost_over_time(
        self,
        db: AsyncSession,
        *,
        filters: Any,
        user: User,
        user_group_ids: list[str],
        start: datetime,
        end: datetime,
        use_daily: bool,
    ) -> list[dict[str, Any]]:
        if use_daily:
            # C2: rollups UNION a live aggregation of every day without a rollup
            # (today, yesterday until the ~01:00 UTC aggregation, a day the backfill
            # has not reached) so none of them are dropped.
            await _skip_jit(db)
            base = combined_daily_stats_subquery(filters, user=user, user_group_ids=user_group_ids, start=start, end=end)
            query = (
                select(
                    base.c.date.label("date"),
                    base.c.model.label("model"),
                    func.sum(base.c.total_cost).filter(base.c.total_cost.is_not(None)).label("cost"),
                )
                .where(base.c.model.is_not(None))
                .group_by(base.c.date, base.c.model)
                .order_by(base.c.date.asc(), base.c.model.asc())
            )
        else:
            base = build_trace_filters(filters, user=user, user_group_ids=user_group_ids, start=start, end=end).subquery()
            query = (
                select(
                    utc_date(base.c.started_at).label("date"),
                    base.c.model.label("model"),
                    func.sum(base.c.total_cost).filter(base.c.total_cost.is_not(None)).label("cost"),
                )
                .where(base.c.model.is_not(None))
                .group_by(utc_date(base.c.started_at), base.c.model)
                .order_by(utc_date(base.c.started_at).asc(), base.c.model.asc())
            )
        rows = (await db.execute(query)).all()
        return [
            {
                "date": str(row.date),
                "model": row.model,
                "cost": metric_float(row.cost),
            }
            for row in rows
        ]

    async def analytics_volume_over_time(
        self,
        db: AsyncSession,
        *,
        filters: Any,
        user: User,
        user_group_ids: list[str],
        start: datetime,
        end: datetime,
        use_daily: bool,
    ) -> list[dict[str, Any]]:
        if use_daily:
            # C2: rollups UNION a live aggregation of every day without a rollup
            # (today, yesterday until the ~01:00 UTC aggregation, a day the backfill
            # has not reached) so none of them are dropped.
            await _skip_jit(db)
            base = combined_daily_stats_subquery(filters, user=user, user_group_ids=user_group_ids, start=start, end=end)
            query = (
                select(
                    base.c.date.label("date"),
                    base.c.source.label("source"),
                    func.coalesce(func.sum(base.c.trace_count), 0).label("count"),
                )
                .group_by(base.c.date, base.c.source)
                .order_by(base.c.date.asc(), base.c.source.asc())
            )
        else:
            base = build_trace_filters(filters, user=user, user_group_ids=user_group_ids, start=start, end=end).subquery()
            query = (
                select(
                    utc_date(base.c.started_at).label("date"),
                    base.c.source.label("source"),
                    func.count(base.c.id).label("count"),
                )
                .group_by(utc_date(base.c.started_at), base.c.source)
                .order_by(utc_date(base.c.started_at).asc(), base.c.source.asc())
            )
        rows = (await db.execute(query)).all()
        return [
            {
                "date": str(row.date),
                "source": row.source,
                "count": metric_int(row.count),
            }
            for row in rows
        ]

    async def analytics_latency(
        self,
        db: AsyncSession,
        *,
        filters: Any,
        user: User,
        user_group_ids: list[str],
        start: datetime,
        end: datetime,
        use_daily: bool,
    ) -> dict[str, Any]:
        if use_daily:
            return await self._daily_latency(
                db,
                filters=filters,
                user=user,
                user_group_ids=user_group_ids,
                start=start,
                end=end,
            )

        base = build_trace_filters(filters, user=user, user_group_ids=user_group_ids, start=start, end=end).subquery()
        overall_query = select(
            func.percentile_cont(0.5).within_group(base.c.duration_ms).label("p50"),
            func.percentile_cont(0.75).within_group(base.c.duration_ms).label("p75"),
            func.percentile_cont(0.9).within_group(base.c.duration_ms).label("p90"),
            func.percentile_cont(0.95).within_group(base.c.duration_ms).label("p95"),
            func.percentile_cont(0.99).within_group(base.c.duration_ms).label("p99"),
        ).where(base.c.duration_ms.is_not(None))
        by_model_query = (
            select(
                base.c.model.label("model"),
                func.percentile_cont(0.5).within_group(base.c.duration_ms).label("p50"),
                func.percentile_cont(0.75).within_group(base.c.duration_ms).label("p75"),
                func.percentile_cont(0.9).within_group(base.c.duration_ms).label("p90"),
                func.percentile_cont(0.95).within_group(base.c.duration_ms).label("p95"),
                func.percentile_cont(0.99).within_group(base.c.duration_ms).label("p99"),
                func.count(base.c.id).label("trace_count"),
            )
            .where(base.c.duration_ms.is_not(None), base.c.model.is_not(None))
            .group_by(base.c.model)
            .order_by(func.count(base.c.id).desc())
        )
        daily_query = (
            select(
                utc_date(base.c.started_at).label("date"),
                func.percentile_cont(0.5).within_group(base.c.duration_ms).label("p50"),
            )
            .where(base.c.duration_ms.is_not(None))
            .group_by(utc_date(base.c.started_at))
            .order_by(utc_date(base.c.started_at).asc())
        )
        overall_row = first_row(await db.execute(overall_query)) or SimpleNamespace()
        by_model_rows = (await db.execute(by_model_query)).all()
        daily_rows = (await db.execute(daily_query)).all()

        return {
            "overall": {
                "p50": (None if getattr(overall_row, "p50", None) is None else float(overall_row.p50)),
                "p75": (None if getattr(overall_row, "p75", None) is None else float(overall_row.p75)),
                "p90": (None if getattr(overall_row, "p90", None) is None else float(overall_row.p90)),
                "p95": (None if getattr(overall_row, "p95", None) is None else float(overall_row.p95)),
                "p99": (None if getattr(overall_row, "p99", None) is None else float(overall_row.p99)),
            },
            "by_model": [
                {
                    "model": row.model,
                    "p50": (None if row.p50 is None else float(row.p50)),
                    "p75": (None if row.p75 is None else float(row.p75)),
                    "p90": (None if row.p90 is None else float(row.p90)),
                    "p95": (None if row.p95 is None else float(row.p95)),
                    "p99": (None if row.p99 is None else float(row.p99)),
                    "trace_count": metric_int(row.trace_count),
                }
                for row in by_model_rows
            ],
            "daily_median": [
                {
                    "date": str(row.date),
                    "p50": (None if row.p50 is None else float(row.p50)),
                }
                for row in daily_rows
            ],
            # Percentiles straight off the raw durations - exact, nothing merged.
            "approximate": False,
        }

    async def _daily_latency(
        self,
        db: AsyncSession,
        *,
        filters: Any,
        user: User,
        user_group_ids: list[str],
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        """Percentiles for a >7d window, merged from the rollup's histograms.

        Percentiles are NOT averageable: the previous implementation recombined
        the per-group ``p50``/``p95`` with a ``trace_count``-weighted mean, so
        latency KPIs stepped - sometimes materially - the moment a window crossed
        onto the rollup. Bucket counts, however, add: summing every contributing
        row's ``duration_histogram`` and reading the quantile off the merged
        counts is the percentile of the union, to within a bucket width.

        The merge runs in Python because each output cell (overall, per model, per
        day) needs the *summed* histogram before a quantile can be read off it,
        and the rows are already grouped down to one per
        ``(day, agent, model, source, user)`` - a 90-day window is a few thousand
        small JSON objects, not a scan of raw traces.

        Rows written before ``duration_histogram`` existed carry NULL. A cell with
        any such row falls back to the old weighted mean for that cell only and
        the response is flagged ``approximate`` - so a mixed window is honest
        rather than quietly wrong, and self-heals as the legacy rows age out.
        """
        now = datetime.now(UTC)
        today = now.date()

        # The rollup rows and the live buckets of the days no rollup covers
        # (trailing un-aggregated days and holes in the middle) in ONE
        # statement, so both read one snapshot: an aggregation or a rebuild
        # committing meanwhile can neither drop a day from percentiles presented
        # as exact nor count it twice. Live days are bucketed with the very
        # expression the aggregator uses, so they merge like any other day.
        rollup = (
            build_stats_filters(filters, user=user, user_group_ids=user_group_ids, start=start, end=end)
            .where(LLMDailyStats.date < today)
            .subquery()
        )
        rollup_rows = select(
            rollup.c.date.label("date"),
            rollup.c.model.label("model"),
            rollup.c.trace_count.label("trace_count"),
            rollup.c.p50_duration_ms.label("p50_duration_ms"),
            rollup.c.p95_duration_ms.label("p95_duration_ms"),
            rollup.c.duration_histogram.label("duration_histogram"),
            cast(null(), Integer).label("bucket"),
            cast(null(), Integer).label("bucket_count"),
        )
        live = _live_traces(
            filters,
            user=user,
            user_group_ids=user_group_ids,
            start=start,
            end=end,
            day_ranges=unrolled_day_ranges(start, end, today),
        ).subquery("live_traces")
        bucket = literal_column(bucket_expression_sql("live_traces.duration_ms"))
        live_rows = (
            select(
                utc_date(live.c.started_at).label("date"),
                func.coalesce(live.c.model, "").label("model"),
                cast(null(), Integer).label("trace_count"),
                cast(null(), Float).label("p50_duration_ms"),
                cast(null(), Float).label("p95_duration_ms"),
                cast(null(), postgresql.JSONB).label("duration_histogram"),
                bucket.label("bucket"),
                func.count(live.c.id).label("bucket_count"),
            )
            .where(live.c.duration_ms.is_not(None))
            .group_by(utc_date(live.c.started_at), live.c.model, bucket)
        )

        cells = _LatencyCells()
        await _skip_jit(db)
        for row in (await db.execute(rollup_rows.union_all(live_rows))).all():
            if row.bucket is None:
                cells.add_rollup_row(
                    date=row.date,
                    model=row.model,
                    trace_count=metric_int(row.trace_count),
                    p50=row.p50_duration_ms,
                    p95=row.p95_duration_ms,
                    histogram=normalize_histogram(row.duration_histogram),
                )
            else:
                cells.add_live_bucket(
                    date=row.date,
                    model=row.model,
                    bucket=int(row.bucket),
                    count=metric_int(row.bucket_count),
                )

        return cells.to_payload()

    async def analytics_cost_by_agent(
        self,
        db: AsyncSession,
        *,
        filters: Any,
        user: User,
        user_group_ids: list[str],
        start: datetime,
        end: datetime,
        use_daily: bool,
    ) -> list[dict[str, Any]]:
        if use_daily:
            # C2: rollups UNION a live aggregation of every day without a rollup
            # (today, yesterday until the ~01:00 UTC aggregation, a day the backfill
            # has not reached) so none of them are dropped.
            await _skip_jit(db)
            base = combined_daily_stats_subquery(filters, user=user, user_group_ids=user_group_ids, start=start, end=end)
            query = (
                select(
                    base.c.agent_id.label("agent_id"),
                    Agent.name.label("agent_name"),
                    Agent.icon.label("agent_icon"),
                    func.coalesce(func.sum(base.c.trace_count), 0).label("trace_count"),
                    func.sum(base.c.total_cost).filter(base.c.total_cost.is_not(None)).label("total_cost"),
                    (
                        func.sum(base.c.total_cost).filter(base.c.total_cost.is_not(None))
                        / func.nullif(func.sum(base.c.trace_count), 0)
                    ).label("avg_cost_per_trace"),
                    # C3: weight by trace_count only over rows that have a value.
                    (
                        func.sum(base.c.avg_duration_ms * base.c.trace_count)
                        / func.nullif(func.sum(base.c.trace_count).filter(base.c.avg_duration_ms.is_not(None)), 0)
                    ).label("avg_duration_ms"),
                    (
                        func.sum(base.c.avg_feedback_score * base.c.trace_count)
                        / func.nullif(func.sum(base.c.trace_count).filter(base.c.avg_feedback_score.is_not(None)), 0)
                    ).label("avg_feedback_score"),
                )
                .outerjoin(Agent, Agent.id == base.c.agent_id)
                .where(base.c.agent_id.is_not(None))
                .group_by(base.c.agent_id, Agent.name, Agent.icon)
                .order_by(func.coalesce(func.sum(base.c.total_cost), 0).desc())
            )
        else:
            base = build_trace_filters(filters, user=user, user_group_ids=user_group_ids, start=start, end=end).subquery()
            # E: pre-aggregate feedback to one row per trace before joining, otherwise a
            # trace with N feedback scores multiplies SUM(total_cost) and weights AVG(duration).
            feedback_sub = _feedback_avg_subquery()
            query = (
                select(
                    base.c.agent_id.label("agent_id"),
                    Agent.name.label("agent_name"),
                    Agent.icon.label("agent_icon"),
                    func.count(func.distinct(base.c.id)).label("trace_count"),
                    func.sum(base.c.total_cost).filter(base.c.total_cost.is_not(None)).label("total_cost"),
                    (
                        func.sum(base.c.total_cost).filter(base.c.total_cost.is_not(None))
                        / func.nullif(func.count(func.distinct(base.c.id)), 0)
                    ).label("avg_cost_per_trace"),
                    func.avg(base.c.duration_ms).label("avg_duration_ms"),
                    func.avg(feedback_sub.c.feedback_value).label("avg_feedback_score"),
                )
                .outerjoin(Agent, Agent.id == base.c.agent_id)
                .outerjoin(
                    feedback_sub,
                    feedback_sub.c.trace_id == base.c.id,
                )
                .where(base.c.agent_id.is_not(None))
                .group_by(base.c.agent_id, Agent.name, Agent.icon)
                .order_by(func.coalesce(func.sum(base.c.total_cost), 0).desc())
            )
        rows = (await db.execute(query)).all()
        return [
            {
                "agent_id": str(row.agent_id),
                "agent_name": row.agent_name,
                "agent_icon": row.agent_icon,
                "trace_count": metric_int(row.trace_count),
                "total_cost": metric_float(row.total_cost),
                "avg_cost_per_trace": metric_float(row.avg_cost_per_trace),
                "avg_duration_ms": metric_float(row.avg_duration_ms),
                "avg_feedback_score": (None if row.avg_feedback_score is None else float(row.avg_feedback_score)),
            }
            for row in rows
        ]

    async def analytics_usage_by_user(
        self,
        db: AsyncSession,
        *,
        filters: Any,
        user: User,
        user_group_ids: list[str],
        start: datetime,
        end: datetime,
        use_daily: bool,
    ) -> list[dict[str, Any]]:
        """Usage totals per user over the window, one row per ``user_id`` (``None`` included).

        The per-user and per-group cost breakdowns both fold these rows
        (``usage_breakdown``), so the two can never disagree with each other or
        with the summary: every trace of the window lands in exactly one row.
        """
        if use_daily:
            await _skip_jit(db)
            base = combined_daily_stats_subquery(filters, user=user, user_group_ids=user_group_ids, start=start, end=end)
            query = select(
                base.c.user_id.label("user_id"),
                func.coalesce(func.sum(base.c.trace_count), 0).label("trace_count"),
                func.coalesce(func.sum(base.c.total_input_tokens + base.c.total_output_tokens), 0).label("total_tokens"),
                func.sum(base.c.total_cost).filter(base.c.total_cost.is_not(None)).label("total_cost"),
                func.count(func.distinct(base.c.agent_id)).label("agent_count"),
            ).group_by(base.c.user_id)
        else:
            base = build_trace_filters(filters, user=user, user_group_ids=user_group_ids, start=start, end=end).subquery()
            query = select(
                base.c.user_id.label("user_id"),
                func.count(base.c.id).label("trace_count"),
                func.coalesce(func.sum(base.c.total_tokens), 0).label("total_tokens"),
                func.sum(base.c.total_cost).filter(base.c.total_cost.is_not(None)).label("total_cost"),
                func.count(func.distinct(base.c.agent_id)).label("agent_count"),
            ).group_by(base.c.user_id)
        rows = (await db.execute(query)).all()
        return [
            {
                "user_id": None if row.user_id is None else str(row.user_id),
                "trace_count": metric_int(row.trace_count),
                "total_tokens": metric_int(row.total_tokens),
                "total_cost": metric_float(row.total_cost),
                "agent_count": metric_int(row.agent_count),
            }
            for row in rows
        ]

    async def analytics_errors(
        self,
        db: AsyncSession,
        *,
        filters: Any,
        user: User,
        user_group_ids: list[str],
        start: datetime,
        end: datetime,
        limit: int = ANALYTICS_ERRORS_DEFAULT_LIMIT,
    ) -> list[dict[str, Any]]:
        base = build_trace_filters(filters, user=user, user_group_ids=user_group_ids, start=start, end=end).subquery()
        truncated_error = func.substring(base.c.error_message, 1, 200)

        query = (
            select(
                truncated_error.label("error_message"),
                func.count(base.c.id).label("count"),
                func.max(base.c.started_at).label("last_seen"),
                Agent.name.label("agent_name"),
                base.c.model.label("model"),
            )
            .outerjoin(Agent, Agent.id == base.c.agent_id)
            .where(base.c.status == "error", base.c.error_message.is_not(None))
            .group_by(truncated_error, Agent.name, base.c.model)
            .order_by(func.count(base.c.id).desc())
            .limit(max(1, int(limit)))
        )
        rows = (await db.execute(query)).all()
        return [
            {
                "error_message": row.error_message,
                "count": metric_int(row.count),
                "last_seen": (
                    _as_iso(row.last_seen)
                    if isinstance(row.last_seen, datetime)
                    else (str(row.last_seen) if row.last_seen else None)
                ),
                "agent_name": row.agent_name,
                "model": row.model,
            }
            for row in rows
        ]


# Process-shared ClickHouse read client. The read store is a frozen dataclass
# constructed per request, so without sharing every analytics query (~8-10 per
# dashboard load) built a fresh HTTPClickHouseClient -> a new httpx.AsyncClient +
# connection pool that was NEVER closed, leaking sockets/FDs over time. Cache one
# client for the process and close it at shutdown via close_clickhouse_read_client.
_shared_ch_read_client: HTTPClickHouseClient | None = None
_shared_ch_read_lock = asyncio.Lock()


async def _get_shared_clickhouse_read_client() -> HTTPClickHouseClient:
    global _shared_ch_read_client
    if _shared_ch_read_client is None:
        async with _shared_ch_read_lock:
            if _shared_ch_read_client is None:
                _shared_ch_read_client = HTTPClickHouseClient.from_settings()
    return _shared_ch_read_client


async def close_clickhouse_read_client() -> None:
    """Close the process-shared ClickHouse read client (call at app shutdown)."""
    global _shared_ch_read_client
    client = _shared_ch_read_client
    _shared_ch_read_client = None
    if client is not None:
        await client.close()


@dataclass(slots=True, frozen=True)
class ClickHouseTraceReadStore:
    provider: TraceStoreProvider = "clickhouse"
    _postgres: PostgresTraceReadStore = field(default_factory=PostgresTraceReadStore)
    _client_factory: Any | None = None

    async def _build_default_client(self):
        if not has_clickhouse_runtime_config():
            return _NoopClickHouseReadClient()
        return await _get_shared_clickhouse_read_client()

    @staticmethod
    def _serve_from_rollup(kwargs: dict[str, Any]) -> bool:
        """Whether this analytics call must be answered from ``llm_daily_stats``.

        ClickHouse mode used to ignore ``use_daily`` entirely and query raw
        ``llm_traces`` in ClickHouse for every window, so 30d/90d dashboards went
        blank as soon as those traces aged out - even though the PostgreSQL rollup
        that is supposed to outlive them held the answer. Writes are mirrored to
        PostgreSQL (``store.build_trace_write_sink``), which is what makes the
        rollup - and today's not-yet-aggregated raw traces the rollup path unions
        in - available on this provider too.
        """
        return bool(kwargs.get("use_daily"))

    async def _query_clickhouse(self, sql: str) -> list[dict[str, Any]]:
        factory = self._client_factory or self._build_default_client
        try:
            maybe_client = factory()
            client = await maybe_client if hasattr(maybe_client, "__await__") else maybe_client
            if hasattr(client, "query_json_rows"):
                rows = await client.query_json_rows(sql)
            else:
                rows = await client.query_rows(sql)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return [self._row_mapping(row) for row in rows]

    async def _execute_clickhouse(self, sql: str) -> None:
        factory = self._client_factory or self._build_default_client
        try:
            maybe_client = factory()
            client = await maybe_client if hasattr(maybe_client, "__await__") else maybe_client
            await client.execute(sql)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    async def _insert_clickhouse(self, table: str, rows: list[dict[str, Any]]) -> None:
        factory = self._client_factory or self._build_default_client
        try:
            maybe_client = factory()
            client = await maybe_client if hasattr(maybe_client, "__await__") else maybe_client
            await client.insert_json_rows(table, rows)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @staticmethod
    def _row_mapping(row: Any) -> dict[str, Any]:
        if row is None:
            return {}
        if isinstance(row, dict):
            return row
        mapping = getattr(row, "_mapping", None)
        if mapping is not None:
            return dict(mapping)
        row_dict = getattr(row, "__dict__", None)
        if isinstance(row_dict, dict):
            return row_dict
        return {}

    @staticmethod
    def _sql_quote(value: Any) -> str:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            # Keep millisecond precision: the columns are DateTime64(3) and inserts
            # preserve ms, so truncating to whole seconds would skip/duplicate rows
            # at window and cursor boundaries.
            value = value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        # ClickHouse string literals honour BACKSLASH escapes as well as quotes, so
        # escape backslash FIRST then the single quote. Doubling only the quote
        # would let a trailing backslash escape the closing quote and break out of
        # the literal (malformed query / injection on user-controlled filters).
        escaped = str(value).replace("\\", "\\\\").replace("'", "''")
        return f"'{escaped}'"

    @classmethod
    def _sql_list(cls, values: list[str]) -> str:
        return ", ".join(cls._sql_quote(value) for value in values)

    @staticmethod
    def _order_by_sql(
        sort_by: str,
        sort_dir: str,
        allowed: tuple[str, ...],
        default: str,
        nullable: frozenset[str] = frozenset(),
        *,
        reverse: bool = False,
    ) -> str:
        """ORDER BY expression for a sortable column.

        Callers normalize ``sort_by`` before deciding anything else; re-checking
        it against ``allowed`` here keeps the guarantee where the SQL string is
        actually built, so no future caller can interpolate a raw request value.
        ``NULLS LAST`` is added for the columns that can hold NULL, mirroring the
        PostgreSQL path so a descending sort opens on real values rather than on
        empty cells. ``reverse`` reads that order from its last row, as
        :func:`_order_term` does: direction flipped, NULLs first.
        """
        column = sort_by if sort_by in allowed else default
        direction = ClickHouseTraceReadStore._direction_sql(sort_dir, reverse=reverse)
        nulls = (" NULLS FIRST" if reverse else " NULLS LAST") if column in nullable else ""
        return f"{column} {direction}{nulls}"

    @staticmethod
    def _direction_sql(sort_dir: str, *, reverse: bool) -> str:
        return "DESC" if _read_dir(sort_dir, reverse=reverse) == "desc" else "ASC"

    @staticmethod
    def _offset_sql(offset: int) -> str:
        return f" OFFSET {int(offset)}" if offset else ""

    async def _visible_agent_ids(
        self,
        db: AsyncSession,
        *,
        user: User,
        user_group_ids: list[str],
    ) -> list[str]:
        result = await db.execute(
            select(Agent.id).where(user_visible_filter(Agent, user.id, user_group_ids))
        )
        visible: list[str] = []
        for row in result.all():
            mapping = self._row_mapping(row)
            value = mapping.get("id")
            if value is None and isinstance(row, tuple) and row:
                value = row[0]
            if value is not None:
                visible.append(str(value))
        return visible

    async def _hidden_agent_ids(self, db: AsyncSession, *, user: User, among: list[str]) -> set[str]:
        """The agents of *among* whose activity is hidden from *user* (#11624)."""
        if not among:
            return set()
        from app.services.activity_visibility import agent_activity_shown

        ids = [uuid.UUID(agent_id) for agent_id in among]
        result = await db.execute(
            select(Agent.id).where(Agent.id.in_(ids), not_(agent_activity_shown(Agent.id, user.id)))
        )
        return {str(agent_id) for agent_id in result.scalars().all()}

    async def _deleted_agent_ids(self, db: AsyncSession, *, user: User) -> tuple[list[str], list[str]]:
        """``(owned, all)``: the agents deleted within the longest trace retention,
        those *user* owned, and every one. ClickHouse cannot join ``agents``, so a
        deleted agent is known by its tombstone; an agent deleted before
        tombstones were written is not, there."""
        cutoff = datetime.now(UTC) - timedelta(days=_LONGEST_TRACE_RETENTION_DAYS)
        result = await db.execute(
            select(AgentTombstone.agent_id, AgentTombstone.user_id).where(AgentTombstone.deleted_at >= cutoff)
        )
        owned: list[str] = []
        every: list[str] = []
        for row in result.all():
            mapping = self._row_mapping(row)
            agent_id = mapping.get("agent_id")
            if agent_id is None:
                continue
            every.append(str(agent_id))
            if str(mapping.get("user_id")) == str(user.id):
                owned.append(str(agent_id))
        return owned, every

    async def _visibility_sql(self, db: AsyncSession, *, user: User, user_group_ids: list[str]) -> str:
        """The non-admin visibility of a row: ``trace_visible_query``'s arms, in ClickHouse SQL."""
        visible_agent_ids = await self._visible_agent_ids(db, user=user, user_group_ids=user_group_ids)
        owned_deleted, deleted = await self._deleted_agent_ids(db, user=user)
        user_id_sql = self._sql_quote(str(user.id))
        visibility_clauses = [
            f"(source = 'general-chat' AND toString(user_id) = {user_id_sql})",
            f"(toString(user_id) = {user_id_sql} AND agent_id IS NULL)",
        ]
        # Activity hidden from non-admins (#11624): a hidden agent's traces
        # stay readable by the person they ran for only.
        hidden = await self._hidden_agent_ids(db, user=user, among=visible_agent_ids)
        if hidden:
            visible_agent_ids = [agent_id for agent_id in visible_agent_ids if agent_id not in hidden]
            visibility_clauses.append(
                f"(toString(user_id) = {user_id_sql} AND toString(agent_id) IN ({self._sql_list(sorted(hidden))}))"
            )
        readable = list(dict.fromkeys([*visible_agent_ids, *owned_deleted]))
        if readable:
            visibility_clauses.insert(0, f"toString(agent_id) IN ({self._sql_list(readable)})")
        if deleted:
            visibility_clauses.append(
                f"(toString(user_id) = {user_id_sql} AND toString(agent_id) IN ({self._sql_list(deleted)}))"
            )
        return f"({' OR '.join(visibility_clauses)})"

    async def _build_trace_where(
        self,
        db: AsyncSession,
        *,
        filters: Any,
        user: User,
        user_group_ids: list[str],
        start: datetime,
        end: datetime,
    ) -> str:
        clauses = [
            f"started_at >= {self._sql_quote(start)}",
            f"started_at < {self._sql_quote(end)}",
        ]

        agent_ids = [str(agent_id) for agent_id in parse_agent_ids_csv(filters.agent_ids, label="agent_ids")]
        if agent_ids:
            clauses.append(f"toString(agent_id) IN ({self._sql_list(agent_ids)})")

        user_ids = [str(user_id) for user_id in analytics_user_ids(filters)]
        if user_ids:
            clauses.append(f"toString(user_id) IN ({self._sql_list(user_ids)})")

        models = split_csv(filters.models)
        if models:
            clauses.append(f"model IN ({self._sql_list(models)})")

        sources = split_csv(filters.sources)
        if sources:
            clauses.append(f"source IN ({self._sql_list(sources)})")

        if not user.is_admin:
            clauses.append(await self._visibility_sql(db, user=user, user_group_ids=user_group_ids))

        return " AND ".join(clauses)

    async def _resolve_agent_metadata(
        self,
        db: AsyncSession,
        agent_ids: list[str],
    ) -> dict[str, tuple[str | None, str | None]]:
        parsed_ids: list[uuid.UUID] = []
        for agent_id in agent_ids:
            try:
                parsed_ids.append(uuid.UUID(agent_id))
            except (TypeError, ValueError):
                continue
        if not parsed_ids:
            return {}
        result = await db.execute(select(Agent.id, Agent.name, Agent.icon).where(Agent.id.in_(parsed_ids)))
        metadata: dict[str, tuple[str | None, str | None]] = {}
        for row in result.all():
            mapping = self._row_mapping(row)
            agent_id = mapping.get("id")
            if agent_id is None:
                continue
            metadata[str(agent_id)] = (mapping.get("name"), mapping.get("icon"))
        gone = [agent_id for agent_id in parsed_ids if str(agent_id) not in metadata]
        if gone:
            # A deleted agent keeps its last name.
            result = await db.execute(
                select(AgentTombstone.agent_id.label("id"), AgentTombstone.name, AgentTombstone.icon).where(
                    AgentTombstone.agent_id.in_(gone)
                )
            )
            for row in result.all():
                mapping = self._row_mapping(row)
                agent_id = mapping.get("id")
                if agent_id is not None:
                    metadata[str(agent_id)] = (mapping.get("name"), mapping.get("icon"))
        return metadata

    @staticmethod
    def _parse_uuid(value: Any) -> uuid.UUID | None:
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        try:
            return uuid.UUID(str(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if value is None or isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None

    @staticmethod
    def _maybe_parse_json(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return value

    @classmethod
    def _json_object_or_none(cls, value: Any) -> dict[str, Any] | None:
        parsed = cls._maybe_parse_json(value)
        if isinstance(parsed, dict):
            return parsed
        return None

    @classmethod
    def _json_object(cls, value: Any) -> dict[str, Any]:
        parsed = cls._maybe_parse_json(value)
        if isinstance(parsed, dict):
            return parsed
        return {}

    @classmethod
    def _json_array(cls, value: Any) -> list[Any]:
        parsed = cls._maybe_parse_json(value)
        if isinstance(parsed, list):
            return parsed
        return []

    def _trace_from_row(self, row: dict[str, Any]) -> LLMTrace:
        return LLMTrace(
            id=self._parse_uuid(row.get("id")),
            agent_id=self._parse_uuid(row.get("agent_id")),
            user_id=self._parse_uuid(row.get("user_id")),
            conversation_id=self._parse_uuid(row.get("conversation_id")),
            assignment_run_id=self._parse_uuid(row.get("assignment_run_id")),
            parent_trace_id=self._parse_uuid(row.get("parent_trace_id")),
            source=str(row.get("source") or ""),
            name=str(row.get("name") or ""),
            model=row.get("model"),
            environment=str(row.get("environment") or "production"),
            metadata_json=self._json_object(row.get("metadata")),
            input=(
                self._maybe_parse_json(row.get("input"))
                if row.get("input") is not None
                else {}
            ),
            output=(
                self._maybe_parse_json(row.get("output"))
                if row.get("output") is not None
                else {}
            ),
            tags=self._json_array(row.get("tags")),
            started_at=self._parse_datetime(row.get("started_at")),
            ended_at=self._parse_datetime(row.get("ended_at")),
            duration_ms=row.get("duration_ms"),
            input_tokens=row.get("input_tokens"),
            output_tokens=row.get("output_tokens"),
            total_tokens=row.get("total_tokens"),
            input_cost=row.get("input_cost"),
            output_cost=row.get("output_cost"),
            total_cost=row.get("total_cost"),
            status=str(row.get("status") or "ok"),
            error_message=row.get("error_message"),
            failure_cause=row.get("failure_cause"),
            level=str(row.get("level") or "DEFAULT"),
            created_at=self._parse_datetime(row.get("created_at")),
            updated_at=self._parse_datetime(row.get("updated_at")),
        )

    def _span_from_row(self, row: dict[str, Any]) -> LLMSpan:
        return LLMSpan(
            id=self._parse_uuid(row.get("id")),
            trace_id=self._parse_uuid(row.get("trace_id")),
            parent_span_id=self._parse_uuid(row.get("parent_span_id")),
            type=str(row.get("type") or "SPAN"),
            name=str(row.get("name") or ""),
            model=row.get("model"),
            model_parameters=self._json_object_or_none(row.get("model_parameters")),
            input=self._maybe_parse_json(row.get("input")),
            output=self._maybe_parse_json(row.get("output")),
            metadata_json=self._json_object(row.get("metadata")),
            level=str(row.get("level") or "DEFAULT"),
            status_message=row.get("status_message"),
            started_at=self._parse_datetime(row.get("started_at")),
            ended_at=self._parse_datetime(row.get("ended_at")),
            duration_ms=row.get("duration_ms"),
            time_to_first_token=row.get("time_to_first_token"),
            input_tokens=row.get("input_tokens"),
            output_tokens=row.get("output_tokens"),
            input_cost=row.get("input_cost"),
            output_cost=row.get("output_cost"),
            total_cost=row.get("total_cost"),
            usage_details=self._json_object_or_none(row.get("usage_details")),
            tool_call_id=row.get("tool_call_id"),
            # Same normalization contract as the Postgres API serializer:
            # legacy opaque strings survive as {"raw": ...} instead of being
            # silently dropped to None like _json_object_or_none would do.
            tool_arguments=coerce_tool_arguments_object(row.get("tool_arguments")),
            stop_reason=row.get("stop_reason"),
            position=int(row.get("position") or 0),
            created_at=self._parse_datetime(row.get("created_at")),
        )

    def _score_from_row(self, row: dict[str, Any]) -> LLMScoreModel:
        return LLMScoreModel(
            id=self._parse_uuid(row.get("id")),
            trace_id=self._parse_uuid(row.get("trace_id")),
            span_id=self._parse_uuid(row.get("span_id")),
            user_id=self._parse_uuid(row.get("user_id")),
            name=str(row.get("name") or ""),
            source=str(row.get("source") or "USER"),
            data_type=str(row.get("data_type") or "BOOLEAN"),
            value=row.get("value"),
            string_value=row.get("string_value"),
            comment=row.get("comment"),
            metadata_json=self._json_object(row.get("metadata")),
            created_at=self._parse_datetime(row.get("created_at")),
        )

    async def _get_visible_trace(
        self,
        db: AsyncSession,
        *,
        user: User,
        user_group_ids: list[str],
        trace_id: uuid.UUID,
    ) -> LLMTrace:
        clauses = [f"toString(id) = {self._sql_quote(str(trace_id))}"]
        if not user.is_admin:
            clauses.append(await self._visibility_sql(db, user=user, user_group_ids=user_group_ids))
        rows = await self._query_clickhouse(
            "SELECT * FROM llm_traces "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY started_at DESC "
            "LIMIT 1"
        )
        if not rows:
            raise HTTPException(status_code=404, detail="Trace not found")
        return self._trace_from_row(rows[0])

    def _list_filters_sql(
        self,
        *,
        search: str | None,
        conversation_id: str | None,
        source: list[str] | None,
        agent_id: list[str] | None,
        model: list[str] | None,
        status_filter: str | None,
        level: str | None,
        date_from: datetime | None,
        date_to: datetime | None,
        include_conversation_search: bool,
        failure_cause: list[str] | None = None,
        assignment_id: uuid.UUID | None = None,
        assignment_run_id: uuid.UUID | None = None,
        user_ids: list[uuid.UUID] | None = None,
    ) -> list[str]:
        clauses: list[str] = []
        if user_ids:
            clauses.append(f"toString(user_id) IN ({self._sql_list([str(user_id) for user_id in user_ids])})")
        if source:
            clauses.append(f"source IN ({self._sql_list(source)})")
        if agent_id:
            parsed = [str(parse_uuid(item, label='agent_id')) for item in agent_id]
            clauses.append(f"toString(agent_id) IN ({self._sql_list(parsed)})")
        if model:
            clauses.append(f"model IN ({self._sql_list(model)})")
        if status_filter:
            clauses.append(f"status = {self._sql_quote(status_filter)}")
        if failure_cause:
            # Mirrors the Postgres arm, ``status`` guard included — see the
            # note there for why the sentinel cannot be a bare NULL check.
            named = [
                value
                for value in failure_cause
                if value != UNRECORDED_FAILURE_CAUSE
            ]
            arms = []
            if named:
                arms.append(f"failure_cause IN ({self._sql_list(named)})")
            if UNRECORDED_FAILURE_CAUSE in failure_cause:
                arms.append("(failure_cause IS NULL AND status = 'error')")
            clauses.append(f"({' OR '.join(arms)})")
        if level:
            clauses.append(f"level = {self._sql_quote(level)}")
        if date_from:
            clauses.append(f"started_at >= {self._sql_quote(date_from)}")
        if date_to:
            clauses.append(f"started_at <= {self._sql_quote(date_to)}")
        if search:
            # Reuse _sql_quote (backslash + quote escaping, returns a quoted
            # literal) instead of hand-rolling quote-only escaping.
            needle = self._sql_quote(search)
            terms = [
                f"positionCaseInsensitiveUTF8(model, {needle}) > 0",
                f"positionCaseInsensitiveUTF8(name, {needle}) > 0",
            ]
            if include_conversation_search:
                terms.append(f"positionCaseInsensitiveUTF8(toString(conversation_id), {needle}) > 0")
            clauses.append(f"({' OR '.join(terms)})")
        if conversation_id:
            parsed_conversation_id = str(parse_uuid(conversation_id, label="conversation_id"))
            clauses.append(f"toString(conversation_id) = {self._sql_quote(parsed_conversation_id)}")
        # Both compare the bare column, so its bloom filter can skip granules
        # (a ``toString()`` around it would read every one).
        if assignment_run_id:
            clauses.append(f"assignment_run_id = toUUID({self._sql_quote(str(assignment_run_id))})")
        if assignment_id:
            # The run table lives in PostgreSQL only: ``assignment_id`` is the
            # column materialized from the id the engine stamps on every run's
            # trace metadata (``trace_llm_call``, ``clickhouse_client``). Not
            # the PostgreSQL question exactly - see the note there.
            clauses.append(f"assignment_id = {self._sql_quote(str(assignment_id))}")
        return clauses

    async def list_traces(
        self,
        db: AsyncSession,
        *,
        user: User,
        user_group_ids: list[str],
        search: str | None,
        conversation_id: str | None,
        source: list[str] | None,
        agent_id: list[str] | None,
        model: list[str] | None,
        status_filter: str | None,
        level: str | None,
        date_from: datetime | None,
        date_to: datetime | None,
        cursor: datetime | None,
        cursor_id: str | None,
        limit: int,
        sort_by: str = DEFAULT_TRACE_SORT,
        sort_dir: str = DEFAULT_SORT_DIR,
        offset: int = 0,
        failure_cause: list[str] | None = None,
        reverse: bool = False,
        assignment_id: uuid.UUID | None = None,
        assignment_run_id: uuid.UUID | None = None,
        user_ids: list[uuid.UUID] | None = None,
    ) -> tuple[list[Any], int]:
        clauses = self._list_filters_sql(
            search=search,
            conversation_id=conversation_id,
            source=source,
            agent_id=agent_id,
            model=model,
            status_filter=status_filter,
            level=level,
            date_from=date_from,
            date_to=date_to,
            include_conversation_search=False,
            failure_cause=failure_cause,
            assignment_id=assignment_id,
            assignment_run_id=assignment_run_id,
            user_ids=user_ids,
        )
        if not user.is_admin:
            clauses.append(await self._visibility_sql(db, user=user, user_group_ids=user_group_ids))
        # F: total must reflect the full filtered set, not the post-cursor remainder
        # (otherwise it shrinks as you page). Mirror the Postgres path: count from the
        # base filters only and apply the keyset cursor to the row fetch alone.
        count_where_sql = " AND ".join(clauses) if clauses else "1 = 1"
        count_rows = await self._query_clickhouse(f"SELECT count() AS total FROM llm_traces WHERE {count_where_sql}")
        total = metric_int((count_rows[0] if count_rows else {}).get("total"))
        if page_is_past_end(offset, total):
            return [], total
        row_clauses = list(clauses)
        sort_by = normalize_trace_sort(sort_by)
        sort_dir = _normalize_sort_dir(sort_dir)
        use_keyset = trace_sort_is_keyset(sort_by, sort_dir, offset, reverse=reverse)
        if use_keyset and cursor_id:
            parsed_cursor_id = parse_uuid(cursor_id, label="cursor_id")
            if cursor:
                row_clauses.append(
                    "("
                    f"started_at < {self._sql_quote(cursor)} "
                    f"OR (started_at = {self._sql_quote(cursor)} AND toString(id) < {self._sql_quote(str(parsed_cursor_id))})"
                    ")"
                )
            else:
                row_clauses.append(f"toString(id) < {self._sql_quote(str(parsed_cursor_id))}")
        elif use_keyset and cursor:
            row_clauses.append(f"started_at < {self._sql_quote(cursor)}")
        where_sql = " AND ".join(row_clauses) if row_clauses else "1 = 1"
        rows = await self._query_clickhouse(
            "SELECT * "
            "FROM llm_traces "
            f"WHERE {where_sql} "
            f"ORDER BY {self._order_by_sql(sort_by, sort_dir, TRACE_SORT_FIELDS, DEFAULT_TRACE_SORT, _NULLABLE_TRACE_SORT_FIELDS, reverse=reverse)}, "
            f"id {self._direction_sql('desc', reverse=reverse)} "
            f"LIMIT {int(limit)}"
            f"{self._offset_sql(offset)}"
        )
        traces = [self._trace_from_row(row) for row in rows]
        # has_scores: drive the trace-list Score column without a per-row N+1
        # fetch of llm_scores. Scope the membership lookup to THIS page's trace
        # ids (a second, bounded query) rather than an unfiltered
        # ``id IN (SELECT DISTINCT trace_id FROM llm_scores)``, which scanned +
        # DISTINCT-ed the entire scores table on every page load and would grow
        # into the dominant cost of the admin trace list as llm_scores grows.
        scored_ids: set[str] = set()
        page_trace_ids = [str(t.id) for t in traces if t.id is not None]
        if page_trace_ids:
            score_rows = await self._query_clickhouse(
                "SELECT DISTINCT toString(trace_id) AS trace_id FROM llm_scores "
                f"WHERE toString(trace_id) IN ({self._sql_list(page_trace_ids)})"
            )
            scored_ids = {
                str(mapping["trace_id"])
                for mapping in (self._row_mapping(r) for r in score_rows)
                if mapping.get("trace_id") is not None
            }
        metadata = await self._resolve_agent_metadata(db, [str(t.agent_id) for t in traces if t.agent_id is not None])
        user_map: dict[str, str | None] = {}
        user_ids = [trace.user_id for trace in traces if trace.user_id is not None]
        if user_ids:
            user_rows = await db.execute(select(User.id, User.email).where(User.id.in_(user_ids)))
            user_map = {}
            for row in user_rows.all():
                mapping = self._row_mapping(row)
                if mapping.get("id") is not None:
                    user_map[str(mapping["id"])] = mapping.get("email")
        return (
            [
                _ClickHouseTraceListRow(
                    trace=trace,
                    agent_name=metadata.get(str(trace.agent_id), (None, None))[0],
                    agent_icon=metadata.get(str(trace.agent_id), (None, None))[1],
                    user_email=user_map.get(str(trace.user_id)) if trace.user_id else None,
                    has_scores=str(trace.id) in scored_ids,
                )
                for trace in traces
            ],
            total,
        )

    async def list_trace_sessions(
        self,
        db: AsyncSession,
        *,
        user: User,
        user_group_ids: list[str],
        search: str | None,
        source: list[str] | None,
        agent_id: list[str] | None,
        model: list[str] | None,
        status_filter: str | None,
        level: str | None,
        date_from: datetime | None,
        date_to: datetime | None,
        cursor: datetime | None,
        cursor_id: str | None,
        limit: int,
        sort_by: str = DEFAULT_TRACE_SESSION_SORT,
        sort_dir: str = DEFAULT_SORT_DIR,
        offset: int = 0,
        reverse: bool = False,
        user_ids: list[uuid.UUID] | None = None,
    ) -> tuple[list[Any], int]:
        clauses = self._list_filters_sql(
            search=search,
            conversation_id=None,
            source=source,
            agent_id=agent_id,
            model=model,
            status_filter=status_filter,
            level=level,
            date_from=date_from,
            date_to=date_to,
            include_conversation_search=True,
            user_ids=user_ids,
        )
        clauses.append("conversation_id IS NOT NULL")
        if not user.is_admin:
            clauses.append(await self._visibility_sql(db, user=user, user_group_ids=user_group_ids))
        where_sql = " AND ".join(clauses)
        # F: count distinct conversations from the base filters (no keyset cursor) so
        # total stays stable across pages, and push the keyset predicate (HAVING on the
        # aggregated last_activity_at) + LIMIT into SQL instead of materialising every
        # session into memory and slicing in Python.
        count_rows = await self._query_clickhouse(
            f"SELECT uniqExact(conversation_id) AS total FROM llm_traces WHERE {where_sql}"
        )
        total = metric_int((count_rows[0] if count_rows else {}).get("total"))
        if page_is_past_end(offset, total):
            return [], total
        having_clauses: list[str] = []
        sort_by = normalize_trace_session_sort(sort_by)
        sort_dir = _normalize_sort_dir(sort_dir)
        use_keyset = trace_session_sort_is_keyset(sort_by, sort_dir, offset, reverse=reverse)
        if use_keyset and cursor_id:
            parsed_cursor_id = parse_uuid(cursor_id, label="cursor_id")
            if cursor:
                having_clauses.append(
                    "("
                    f"max(started_at) < {self._sql_quote(cursor)} "
                    f"OR (max(started_at) = {self._sql_quote(cursor)} "
                    f"AND toString(conversation_id) > {self._sql_quote(str(parsed_cursor_id))})"
                    ")"
                )
            else:
                having_clauses.append(f"toString(conversation_id) > {self._sql_quote(str(parsed_cursor_id))}")
        elif use_keyset and cursor:
            having_clauses.append(f"max(started_at) < {self._sql_quote(cursor)}")
        having_sql = f"HAVING {' AND '.join(having_clauses)} " if having_clauses else ""
        rows = await self._query_clickhouse(
            "SELECT "
            "conversation_id, "
            "min(started_at) AS first_activity_at, "
            "max(started_at) AS last_activity_at, "
            "count() AS trace_count, "
            "countIf(status = 'error') AS error_count, "
            "uniqExact(agent_id) AS agent_count, "
            "uniqExact(model) AS model_count, "
            "sum(ifNull(input_tokens, 0)) AS input_tokens, "
            "sum(ifNull(output_tokens, 0)) AS output_tokens, "
            "sum(ifNull(total_tokens, 0)) AS total_tokens, "
            "sum(ifNull(total_cost, 0)) AS total_cost "
            "FROM llm_traces "
            f"WHERE {where_sql} "
            "GROUP BY conversation_id "
            f"{having_sql}"
            f"ORDER BY {self._order_by_sql(sort_by, sort_dir, TRACE_SESSION_SORT_FIELDS, DEFAULT_TRACE_SESSION_SORT, reverse=reverse)}, "
            f"conversation_id {self._direction_sql('asc', reverse=reverse)} "
            f"LIMIT {int(limit)}"
            f"{self._offset_sql(offset)}"
        )
        sessions = [
            SimpleNamespace(
                conversation_id=self._parse_uuid(row.get("conversation_id")),
                first_activity_at=self._parse_datetime(row.get("first_activity_at")),
                last_activity_at=self._parse_datetime(row.get("last_activity_at")),
                trace_count=metric_int(row.get("trace_count")),
                error_count=metric_int(row.get("error_count")),
                agent_count=metric_int(row.get("agent_count")),
                model_count=metric_int(row.get("model_count")),
                input_tokens=metric_int(row.get("input_tokens")),
                output_tokens=metric_int(row.get("output_tokens")),
                total_tokens=metric_int(row.get("total_tokens")),
                total_cost=metric_float(row.get("total_cost")),
            )
            for row in rows
        ]
        return sessions, total

    async def get_trace_detail(
        self,
        db: AsyncSession,
        *,
        user: User,
        user_group_ids: list[str],
        trace_id: uuid.UUID,
    ) -> tuple[LLMTrace, str | None, str | None, str | None]:
        trace = await self._get_visible_trace(db, user=user, user_group_ids=user_group_ids, trace_id=trace_id)
        agent_name = None
        agent_icon = None
        user_email = None
        if trace.agent_id:
            agent_row = await db.execute(select(Agent.name, Agent.icon).where(Agent.id == trace.agent_id))
            agent_data = agent_row.one_or_none()
            if agent_data is None:
                # A deleted agent keeps its last name.
                agent_row = await db.execute(
                    select(AgentTombstone.name, AgentTombstone.icon).where(AgentTombstone.agent_id == trace.agent_id)
                )
                agent_data = agent_row.one_or_none()
            if agent_data:
                agent_name = agent_data.name
                agent_icon = agent_data.icon
            else:
                agent_name = getattr(trace, "agent_name_snapshot", None)
        if trace.user_id:
            user_row = await db.execute(select(User.email).where(User.id == trace.user_id))
            user_email = user_row.scalar_one_or_none()
        return trace, agent_name, agent_icon, user_email

    async def get_trace_spans(
        self,
        db: AsyncSession,
        *,
        user: User,
        user_group_ids: list[str],
        trace_id: uuid.UUID,
    ) -> list[LLMSpan]:
        trace = await self._get_visible_trace(db, user=user, user_group_ids=user_group_ids, trace_id=trace_id)
        rows = await self._query_clickhouse(
            "SELECT * FROM llm_spans "
            f"WHERE toString(trace_id) = {self._sql_quote(str(trace.id))} "
            "ORDER BY position ASC, started_at ASC"
        )
        return [self._span_from_row(row) for row in rows]

    async def get_trace_span(
        self,
        db: AsyncSession,
        *,
        user: User,
        user_group_ids: list[str],
        trace_id: uuid.UUID,
        span_id: uuid.UUID,
    ) -> LLMSpan:
        """Fetch one span of a visible trace (see the Postgres counterpart)."""
        trace = await self._get_visible_trace(db, user=user, user_group_ids=user_group_ids, trace_id=trace_id)
        rows = await self._query_clickhouse(
            "SELECT * FROM llm_spans "
            f"WHERE toString(trace_id) = {self._sql_quote(str(trace.id))} "
            f"AND toString(id) = {self._sql_quote(str(span_id))} "
            "LIMIT 1"
        )
        if not rows:
            raise HTTPException(status_code=404, detail="Span not found for this trace")
        return self._span_from_row(rows[0])

    async def get_spans_for_trace_ids(
        self,
        db: AsyncSession,
        *,
        trace_ids: list[uuid.UUID],
    ) -> dict[str, list[LLMSpan]]:
        _ = db
        if not trace_ids:
            return {}

        trace_id_strs = [str(trace_id) for trace_id in trace_ids]
        rows = await self._query_clickhouse(
            "SELECT * FROM llm_spans "
            f"WHERE toString(trace_id) IN ({self._sql_list(trace_id_strs)}) "
            "ORDER BY trace_id ASC, position ASC, started_at ASC"
        )
        grouped: dict[str, list[LLMSpan]] = {trace_id: [] for trace_id in trace_id_strs}
        for row in rows:
            span = self._span_from_row(row)
            grouped.setdefault(str(span.trace_id), []).append(span)
        return grouped

    async def get_trace_scores(
        self,
        db: AsyncSession,
        *,
        user: User,
        user_group_ids: list[str],
        trace_id: uuid.UUID,
    ) -> list[LLMScoreModel]:
        trace = await self._get_visible_trace(db, user=user, user_group_ids=user_group_ids, trace_id=trace_id)
        rows = await self._query_clickhouse(
            "SELECT * FROM llm_scores "
            f"WHERE toString(trace_id) = {self._sql_quote(str(trace.id))} "
            "ORDER BY created_at DESC"
        )
        return [self._score_from_row(row) for row in rows]

    async def get_scores_for_trace_ids(
        self,
        db: AsyncSession,
        *,
        trace_ids: list[uuid.UUID],
    ) -> dict[str, list[LLMScoreModel]]:
        _ = db
        if not trace_ids:
            return {}

        trace_id_strs = [str(trace_id) for trace_id in trace_ids]
        rows = await self._query_clickhouse(
            "SELECT * FROM llm_scores "
            f"WHERE toString(trace_id) IN ({self._sql_list(trace_id_strs)}) "
            "ORDER BY trace_id ASC, created_at DESC"
        )
        grouped: dict[str, list[LLMScoreModel]] = {trace_id: [] for trace_id in trace_id_strs}
        for row in rows:
            score = self._score_from_row(row)
            grouped.setdefault(str(score.trace_id), []).append(score)
        return grouped

    async def create_trace_score(
        self,
        db: AsyncSession,
        *,
        user: User,
        user_group_ids: list[str],
        trace_id: uuid.UUID,
        name: str,
        data_type: str,
        value: float | None,
        string_value: str | None,
        comment: str | None,
        metadata: dict[str, Any],
    ) -> LLMScoreModel:
        trace = await self._get_visible_trace(db, user=user, user_group_ids=user_group_ids, trace_id=trace_id)
        # The latest row of a rater is their score: time this one like
        # every other writer of it, on the database clock under the rater's lock.
        action_at = await begin_score_action(db, trace.id, user.id, name)
        await end_score_action(db, action_at)
        await db.commit()
        payload = {
            # Version 7: ClickHouse stores empty metadata like none, and the id
            # version is what tells this row from a legacy retraction there.
            "id": str(new_score_id()),
            "trace_id": str(trace.id),
            "span_id": None,
            "user_id": str(user.id),
            "name": name,
            "source": "USER",
            "data_type": data_type,
            "value": value,
            "string_value": string_value,
            "comment": comment,
            "metadata": metadata,
            "created_at": action_at,
        }
        await self._insert_clickhouse("llm_scores", [payload])
        return self._score_from_row(payload)

    async def delete_trace(
        self,
        db: AsyncSession,
        *,
        admin: User,
        admin_group_ids: list[str],
        trace_id: uuid.UUID,
    ) -> None:
        trace = await self._get_visible_trace(
            db,
            user=admin,
            user_group_ids=admin_group_ids,
            trace_id=trace_id,
        )
        trace_id_sql = self._sql_quote(str(trace.id))
        await self._execute_clickhouse(f"ALTER TABLE llm_scores DELETE WHERE toString(trace_id) = {trace_id_sql}")
        await self._execute_clickhouse(f"ALTER TABLE llm_spans DELETE WHERE toString(trace_id) = {trace_id_sql}")
        await self._execute_clickhouse(f"ALTER TABLE llm_traces DELETE WHERE toString(id) = {trace_id_sql}")

    async def analytics_summary(self, db: AsyncSession, **kwargs) -> dict[str, Any]:
        if self._serve_from_rollup(kwargs):
            return await self._postgres.analytics_summary(db, **kwargs)

        filters = kwargs["filters"]
        user = kwargs["user"]
        user_group_ids = kwargs["user_group_ids"]
        start = kwargs["start"]
        end = kwargs["end"]
        prev_start = kwargs["prev_start"]
        prev_end = kwargs["prev_end"]

        where_current = await self._build_trace_where(
            db,
            filters=filters,
            user=user,
            user_group_ids=user_group_ids,
            start=start,
            end=end,
        )
        where_previous = await self._build_trace_where(
            db,
            filters=filters,
            user=user,
            user_group_ids=user_group_ids,
            start=prev_start,
            end=prev_end,
        )

        # E: pre-aggregate feedback to one row per trace BEFORE joining. Joining
        # llm_scores directly multiplies a trace's row by its feedback count, which
        # inflates sum(total_cost)/sum(total_tokens) and lets countIf(error) exceed
        # the distinct trace_count (so error_rate could exceed 1.0).
        current_rows = await self._query_clickhouse(
            f"""
            SELECT
                countDistinct(t.id) AS trace_count,
                sumIf(t.total_cost, t.total_cost IS NOT NULL) AS total_cost,
                sum(ifNull(t.total_tokens, 0)) AS total_tokens,
                avg(t.duration_ms) AS avg_duration_ms,
                countIf(t.status = 'error') AS error_count,
                avg(fb.feedback_value) AS avg_feedback_score
            FROM llm_traces t
            LEFT JOIN ({FEEDBACK_PER_TRACE_SQL_CLICKHOUSE}) fb ON fb.trace_id = t.id
            WHERE {where_current}
            """
        )
        previous_rows = await self._query_clickhouse(
            f"""
            SELECT
                countDistinct(t.id) AS trace_count,
                sumIf(t.total_cost, t.total_cost IS NOT NULL) AS total_cost,
                sum(ifNull(t.total_tokens, 0)) AS total_tokens,
                avg(t.duration_ms) AS avg_duration_ms,
                countIf(t.status = 'error') AS error_count,
                avg(fb.feedback_value) AS avg_feedback_score
            FROM llm_traces t
            LEFT JOIN ({FEEDBACK_PER_TRACE_SQL_CLICKHOUSE}) fb ON fb.trace_id = t.id
            WHERE {where_previous}
            """
        )
        unknown_rows = await self._query_clickhouse(
            f"""
            SELECT count() AS unknown_cost_trace_count
            FROM llm_traces
            WHERE {where_current} AND total_cost IS NULL
            """
        )

        current = _analytics_summary_from_row(SimpleNamespace(**(current_rows[0] if current_rows else {})))
        previous = _analytics_summary_from_row(SimpleNamespace(**(previous_rows[0] if previous_rows else {})))
        platform_settings_row = first_row(
            await db.execute(
                select(PlatformSettings.last_aggregation_at.label("last_aggregation_at"))
                .order_by(PlatformSettings.created_at.desc())
                .limit(1)
            )
        )
        last_aggregation_at = _normalize_utc(
            getattr(platform_settings_row, "last_aggregation_at", None)
            if platform_settings_row
            else None
        )
        aggregation_stale = last_aggregation_at is None or (
            datetime.now(UTC) - last_aggregation_at
        ).total_seconds() > 48 * 3600
        unknown_cost_trace_count = metric_int(
            (unknown_rows[0] if unknown_rows else {}).get("unknown_cost_trace_count", 0)
        )
        coverage_start = await self._coverage_start(db)

        return {
            **current,
            "previous": previous,
            "last_aggregation_at": last_aggregation_at,
            "aggregation_stale": aggregation_stale,
            "unknown_cost_trace_count": unknown_cost_trace_count,
            "stats_coverage_start": coverage_start,
            "window_starts_before_coverage": (
                coverage_start is not None and _utc_day(start) < coverage_start
            ),
        }

    async def _coverage_start(self, db: AsyncSession) -> date_type | None:
        """Earliest reportable day, taking ClickHouse's own trace history into account.

        The two providers expire on the same boundary but not by the same
        mechanism: ``purge_expired_traces`` deletes from PostgreSQL on a schedule,
        while ClickHouse drops rows through the TTL that
        ``sync_clickhouse_trace_retention`` declares - and a TTL only takes effect
        when the affected parts are merged, so ClickHouse routinely still holds
        traces PostgreSQL has already deleted (and holds everything, if it was
        populated by a backfill before the TTL was ever declared). Take the older
        of the two rather than under-reporting coverage.
        """
        postgres_start = await stats_coverage_start(db)
        rows = await self._query_clickhouse(
            "SELECT min(started_at) AS earliest FROM llm_traces"
        )
        earliest = (rows[0] if rows else {}).get("earliest")
        clickhouse_start = None
        if isinstance(earliest, datetime):
            normalized = _normalize_utc(earliest)
            # ClickHouse returns the DateTime zero value (1970-01-01) for an empty
            # table rather than NULL - that is "no data", not 1970.
            if normalized is not None and normalized.year > 1970:
                clickhouse_start = normalized.date()

        candidates = [value for value in (postgres_start, clickhouse_start) if value is not None]
        return min(candidates) if candidates else None

    async def analytics_cost_by_model(self, db: AsyncSession, **kwargs) -> list[dict[str, Any]]:
        if self._serve_from_rollup(kwargs):
            return await self._postgres.analytics_cost_by_model(db, **kwargs)

        filters = kwargs["filters"]
        user = kwargs["user"]
        user_group_ids = kwargs["user_group_ids"]
        start = kwargs["start"]
        end = kwargs["end"]
        where_clause = await self._build_trace_where(
            db,
            filters=filters,
            user=user,
            user_group_ids=user_group_ids,
            start=start,
            end=end,
        )
        rows = await self._query_clickhouse(
            f"""
            SELECT
                model,
                count() AS trace_count,
                sum(ifNull(input_tokens, 0)) AS input_tokens,
                sum(ifNull(output_tokens, 0)) AS output_tokens,
                sumIf(input_cost, input_cost IS NOT NULL) AS input_cost,
                sumIf(output_cost, output_cost IS NOT NULL) AS output_cost,
                sumIf(total_cost, total_cost IS NOT NULL) AS total_cost
            FROM llm_traces
            WHERE {where_clause} AND model IS NOT NULL
            GROUP BY model
            ORDER BY ifNull(total_cost, 0) DESC
            """
        )
        return [
            {
                "model": row.get("model"),
                "trace_count": metric_int(row.get("trace_count")),
                "input_tokens": metric_int(row.get("input_tokens")),
                "output_tokens": metric_int(row.get("output_tokens")),
                "input_cost": (None if row.get("input_cost") is None else float(row["input_cost"])),
                "output_cost": (None if row.get("output_cost") is None else float(row["output_cost"])),
                "total_cost": metric_float(row.get("total_cost")),
            }
            for row in rows
        ]

    async def analytics_cost_over_time(self, db: AsyncSession, **kwargs) -> list[dict[str, Any]]:
        if self._serve_from_rollup(kwargs):
            return await self._postgres.analytics_cost_over_time(db, **kwargs)

        filters = kwargs["filters"]
        user = kwargs["user"]
        user_group_ids = kwargs["user_group_ids"]
        start = kwargs["start"]
        end = kwargs["end"]
        where_clause = await self._build_trace_where(
            db,
            filters=filters,
            user=user,
            user_group_ids=user_group_ids,
            start=start,
            end=end,
        )
        rows = await self._query_clickhouse(
            f"""
            SELECT
                toDate(started_at, 'UTC') AS date,
                model,
                sumIf(total_cost, total_cost IS NOT NULL) AS cost
            FROM llm_traces
            WHERE {where_clause} AND model IS NOT NULL
            GROUP BY date, model
            ORDER BY date ASC, model ASC
            """
        )
        return [
            {
                "date": str(row.get("date")),
                "model": row.get("model"),
                "cost": metric_float(row.get("cost")),
            }
            for row in rows
        ]

    async def analytics_volume_over_time(self, db: AsyncSession, **kwargs) -> list[dict[str, Any]]:
        if self._serve_from_rollup(kwargs):
            return await self._postgres.analytics_volume_over_time(db, **kwargs)

        filters = kwargs["filters"]
        user = kwargs["user"]
        user_group_ids = kwargs["user_group_ids"]
        start = kwargs["start"]
        end = kwargs["end"]
        where_clause = await self._build_trace_where(
            db,
            filters=filters,
            user=user,
            user_group_ids=user_group_ids,
            start=start,
            end=end,
        )
        rows = await self._query_clickhouse(
            f"""
            SELECT
                toDate(started_at, 'UTC') AS date,
                source,
                count() AS count
            FROM llm_traces
            WHERE {where_clause}
            GROUP BY date, source
            ORDER BY date ASC, source ASC
            """
        )
        return [
            {
                "date": str(row.get("date")),
                "source": row.get("source"),
                "count": metric_int(row.get("count")),
            }
            for row in rows
        ]

    async def analytics_latency(self, db: AsyncSession, **kwargs) -> dict[str, Any]:
        if self._serve_from_rollup(kwargs):
            return await self._postgres.analytics_latency(db, **kwargs)

        filters = kwargs["filters"]
        user = kwargs["user"]
        user_group_ids = kwargs["user_group_ids"]
        start = kwargs["start"]
        end = kwargs["end"]
        where_clause = await self._build_trace_where(
            db,
            filters=filters,
            user=user,
            user_group_ids=user_group_ids,
            start=start,
            end=end,
        )
        overall_rows = await self._query_clickhouse(
            f"""
            SELECT
                quantile(0.5)(duration_ms) AS p50,
                quantile(0.75)(duration_ms) AS p75,
                quantile(0.9)(duration_ms) AS p90,
                quantile(0.95)(duration_ms) AS p95,
                quantile(0.99)(duration_ms) AS p99
            FROM llm_traces
            WHERE {where_clause} AND duration_ms IS NOT NULL
            """
        )
        by_model_rows = await self._query_clickhouse(
            f"""
            SELECT
                model,
                quantile(0.5)(duration_ms) AS p50,
                quantile(0.75)(duration_ms) AS p75,
                quantile(0.9)(duration_ms) AS p90,
                quantile(0.95)(duration_ms) AS p95,
                quantile(0.99)(duration_ms) AS p99,
                count() AS trace_count
            FROM llm_traces
            WHERE {where_clause} AND duration_ms IS NOT NULL AND model IS NOT NULL
            GROUP BY model
            ORDER BY trace_count DESC
            """
        )
        daily_rows = await self._query_clickhouse(
            f"""
            SELECT
                toDate(started_at, 'UTC') AS date,
                quantile(0.5)(duration_ms) AS p50
            FROM llm_traces
            WHERE {where_clause} AND duration_ms IS NOT NULL
            GROUP BY date
            ORDER BY date ASC
            """
        )
        overall = overall_rows[0] if overall_rows else {}
        return {
            "overall": {
                "p50": (None if overall.get("p50") is None else float(overall["p50"])),
                "p75": (None if overall.get("p75") is None else float(overall["p75"])),
                "p90": (None if overall.get("p90") is None else float(overall["p90"])),
                "p95": (None if overall.get("p95") is None else float(overall["p95"])),
                "p99": (None if overall.get("p99") is None else float(overall["p99"])),
            },
            "by_model": [
                {
                    "model": row.get("model"),
                    "p50": (None if row.get("p50") is None else float(row["p50"])),
                    "p75": (None if row.get("p75") is None else float(row["p75"])),
                    "p90": (None if row.get("p90") is None else float(row["p90"])),
                    "p95": (None if row.get("p95") is None else float(row["p95"])),
                    "p99": (None if row.get("p99") is None else float(row["p99"])),
                    "trace_count": metric_int(row.get("trace_count")),
                }
                for row in by_model_rows
            ],
            "daily_median": [
                {
                    "date": str(row.get("date")),
                    "p50": (None if row.get("p50") is None else float(row["p50"])),
                }
                for row in daily_rows
            ],
            # Computed over the raw durations, not recombined from per-group
            # summaries (ClickHouse's own quantile() sampling aside).
            "approximate": False,
        }

    async def analytics_cost_by_agent(self, db: AsyncSession, **kwargs) -> list[dict[str, Any]]:
        if self._serve_from_rollup(kwargs):
            return await self._postgres.analytics_cost_by_agent(db, **kwargs)

        filters = kwargs["filters"]
        user = kwargs["user"]
        user_group_ids = kwargs["user_group_ids"]
        start = kwargs["start"]
        end = kwargs["end"]
        where_clause = await self._build_trace_where(
            db,
            filters=filters,
            user=user,
            user_group_ids=user_group_ids,
            start=start,
            end=end,
        )
        # E: pre-aggregate feedback to one row per trace BEFORE joining, otherwise a
        # trace with N feedback scores multiplies sum(total_cost) (and inflates
        # avg_cost_per_trace, since the divisor stays countDistinct) and weights
        # avg(duration_ms) by feedback volume.
        rows = await self._query_clickhouse(
            f"""
            SELECT
                toString(t.agent_id) AS agent_id,
                countDistinct(t.id) AS trace_count,
                sumIf(t.total_cost, t.total_cost IS NOT NULL) AS total_cost,
                sumIf(t.total_cost, t.total_cost IS NOT NULL) / nullIf(countDistinct(t.id), 0) AS avg_cost_per_trace,
                avg(t.duration_ms) AS avg_duration_ms,
                avg(fb.feedback_value) AS avg_feedback_score
            FROM llm_traces t
            LEFT JOIN ({FEEDBACK_PER_TRACE_SQL_CLICKHOUSE}) fb ON fb.trace_id = t.id
            WHERE {where_clause} AND t.agent_id IS NOT NULL
            GROUP BY agent_id
            ORDER BY ifNull(total_cost, 0) DESC
            """
        )
        metadata = await self._resolve_agent_metadata(db, [str(row.get("agent_id")) for row in rows if row.get("agent_id")])
        return [
            {
                "agent_id": str(row.get("agent_id")),
                "agent_name": metadata.get(str(row.get("agent_id")), (None, None))[0],
                "agent_icon": metadata.get(str(row.get("agent_id")), (None, None))[1],
                "trace_count": metric_int(row.get("trace_count")),
                "total_cost": metric_float(row.get("total_cost")),
                "avg_cost_per_trace": metric_float(row.get("avg_cost_per_trace")),
                "avg_duration_ms": metric_float(row.get("avg_duration_ms")),
                "avg_feedback_score": (
                    None if row.get("avg_feedback_score") is None else float(row["avg_feedback_score"])
                ),
            }
            for row in rows
        ]

    async def analytics_usage_by_user(self, db: AsyncSession, **kwargs) -> list[dict[str, Any]]:
        if self._serve_from_rollup(kwargs):
            return await self._postgres.analytics_usage_by_user(db, **kwargs)

        where_clause = await self._build_trace_where(
            db,
            filters=kwargs["filters"],
            user=kwargs["user"],
            user_group_ids=kwargs["user_group_ids"],
            start=kwargs["start"],
            end=kwargs["end"],
        )
        # ``user_key``, not ``user_id``: ClickHouse substitutes a SELECT alias into
        # WHERE, and the visibility clause reads the ``user_id`` column.
        rows = await self._query_clickhouse(
            f"""
            SELECT
                toString(user_id) AS user_key,
                count() AS trace_count,
                sum(ifNull(total_tokens, 0)) AS total_tokens,
                sumIf(total_cost, total_cost IS NOT NULL) AS total_cost,
                uniqExact(agent_id) AS agent_count
            FROM llm_traces
            WHERE {where_clause}
            GROUP BY user_key
            """
        )
        return [
            {
                "user_id": str(row["user_key"]) if row.get("user_key") else None,
                "trace_count": metric_int(row.get("trace_count")),
                "total_tokens": metric_int(row.get("total_tokens")),
                "total_cost": metric_float(row.get("total_cost")),
                "agent_count": metric_int(row.get("agent_count")),
            }
            for row in rows
        ]

    async def analytics_errors(self, db: AsyncSession, **kwargs) -> list[dict[str, Any]]:
        filters = kwargs["filters"]
        user = kwargs["user"]
        user_group_ids = kwargs["user_group_ids"]
        start = kwargs["start"]
        end = kwargs["end"]
        where_clause = await self._build_trace_where(
            db,
            filters=filters,
            user=user,
            user_group_ids=user_group_ids,
            start=start,
            end=end,
        )
        rows = await self._query_clickhouse(
            f"""
            SELECT
                substring(error_message, 1, 200) AS error_message,
                count() AS count,
                max(started_at) AS last_seen,
                toString(agent_id) AS agent_id,
                model
            FROM llm_traces
            WHERE {where_clause}
              AND status = 'error'
              AND error_message IS NOT NULL
            GROUP BY error_message, agent_id, model
            ORDER BY count DESC
            LIMIT {max(1, int(kwargs.get("limit", ANALYTICS_ERRORS_DEFAULT_LIMIT)))}
            """
        )
        metadata = await self._resolve_agent_metadata(db, [str(row.get("agent_id")) for row in rows if row.get("agent_id")])
        return [
            {
                "error_message": row.get("error_message"),
                "count": metric_int(row.get("count")),
                "last_seen": (
                    _as_iso(row.get("last_seen"))
                    if isinstance(row.get("last_seen"), datetime)
                    else (str(row.get("last_seen")) if row.get("last_seen") else None)
                ),
                "agent_name": metadata.get(str(row.get("agent_id")), (None, None))[0],
                "model": row.get("model"),
            }
            for row in rows
        ]
