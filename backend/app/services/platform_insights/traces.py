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

"""``list_platform_traces`` / ``list_platform_sessions``: the Traces and Sessions pages, as rows.

Every row comes from the read path the pages use - the trace read store's
``list_traces`` and ``list_trace_sessions``, their filters and their order -
narrowed by the scope: the whole platform for an administrator's agent, the
caller's own traces otherwise. A group manager's team reaches their LLM
analytics, never these lists (``InsightScope.without_team``), as the
Traces and Sessions pages are an administrator's. A row says what a trace
IS - when, which agent and model, whose, its status, cost, tokens, duration,
error - never what it holds: a trace's content is the trace tools'
(``inspect_*_traces``, ``get_trace_payload``) and the trace page's, under
their own rules.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.utils import safe_uuid
from app.services.platform_insights.scope import (
    COST_MASKED_NOTE,
    CURRENT_MEMBERSHIP_NOTE,
    InsightRefusedError,
    InsightScope,
    as_list,
    group_member_ids,
    resolve_groups,
    resolve_period,
    resolve_users,
)

MAX_LIMIT = 100
DEFAULT_LIMIT = 25
#: An error message is a diagnosis, not a transcript: the start of it.
ERROR_EXCERPT_CHARS = 300

STATUSES = ("ok", "error")


async def _user_filter(db: AsyncSession, scope: InsightScope, args: dict[str, Any]) -> tuple[list[str] | None, dict[str, Any], list[str]]:
    """The ``user_ids`` the listing is narrowed to (``None`` = nobody narrowed), the filters echoed, the caveats."""
    named_users = await resolve_users(db, args.get("user_ids"), scope)
    groups = await resolve_groups(db, args.get("group_ids"), scope)
    caveats: list[str] = []
    user_filter: set[str] | None = {user.id for user in named_users} if named_users else None
    if groups:
        members = await group_member_ids(db, groups)
        user_filter = members if user_filter is None else user_filter & members
        caveats.append(CURRENT_MEMBERSHIP_NOTE)
    if not scope.platform:
        reach = scope.usage_reach()
        user_filter = set(reach) if user_filter is None else user_filter & reach
    echoed = {"users": [user.email for user in named_users], "groups": [group.name for group in groups]}
    return (None if user_filter is None else sorted(user_filter)), echoed, caveats


def _common(args: dict[str, Any]) -> dict[str, Any]:
    agent_ids = as_list(args.get("agent_ids"))
    bad = [value for value in agent_ids if safe_uuid(value) is None]
    if bad:
        raise InsightRefusedError(f"Error: agent_ids must be agent UUIDs, not {', '.join(map(repr, bad))}.")
    status = str(args.get("status") or "").strip().lower() or None
    if status is not None and status not in STATUSES:
        raise InsightRefusedError("Error: status must be ok or error, or omitted for both.")
    try:
        limit = max(1, min(int(args.get("limit") or DEFAULT_LIMIT), MAX_LIMIT))
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        raise InsightRefusedError("Error: limit and offset must be whole numbers.") from None
    return {
        "agent_id": [str(safe_uuid(value)) for value in agent_ids] or None,
        "model": as_list(args.get("models")) or None,
        "source": as_list(args.get("sources")) or None,
        "status_filter": status,
        "search": (str(args.get("search") or "").strip() or None),
        "limit": limit,
        "offset": offset,
    }


def _sort(args: dict[str, Any], allowed: tuple[str, ...], default: str) -> tuple[str, str]:
    sort_by = str(args.get("sort_by") or default).strip().lower()
    if sort_by not in allowed:
        raise InsightRefusedError(f"Error: sort_by must be one of {', '.join(allowed)}.")
    sort_dir = str(args.get("sort_dir") or "desc").strip().lower()
    if sort_dir not in ("asc", "desc"):
        raise InsightRefusedError("Error: sort_dir must be asc or desc.")
    return sort_by, sort_dir


def _viewer(scope: InsightScope) -> SimpleNamespace:
    # The store's per-user visibility decides whose CONVERSATIONS a person may
    # open. A listing of what traces ARE is the usage question: outside the
    # platform scope it is hard-filtered to the reach (``_user_filter``), so
    # the visibility is skipped, as the analytics read skips it.
    return SimpleNamespace(id=scope.user.id, is_admin=True)


def _cost(value: Any) -> float | None:
    if value is None or not settings.observability_cost_data_enabled:
        return None
    return float(value)


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _page(result: dict[str, Any], *, total: int, offset: int, shown: int, caveats: list[str]) -> dict[str, Any]:
    result["total"] = total
    result["next_offset"] = offset + shown if offset + shown < total else None
    result["cost_masked"] = not settings.observability_cost_data_enabled
    if result["cost_masked"]:
        caveats.append(COST_MASKED_NOTE)
    result["caveats"] = caveats
    return result


async def list_platform_traces(db: AsyncSession, scope: InsightScope, args: dict[str, Any]) -> dict[str, Any]:
    from app.services.tracing.read_store import DEFAULT_TRACE_SORT, TRACE_LIST_MAX_OFFSET, TRACE_SORT_FIELDS
    from app.services.tracing.store import build_trace_read_store, ensure_trace_read_provider_supported

    scope = scope.without_team()
    period, start, end = resolve_period(args.get("period"), args.get("date_from"), args.get("date_to"))
    common = _common(args)
    if common["offset"] > TRACE_LIST_MAX_OFFSET:
        raise InsightRefusedError(f"Error: offset is at most {TRACE_LIST_MAX_OFFSET}; narrow the filters instead.")
    sort_by, sort_dir = _sort(args, TRACE_SORT_FIELDS, DEFAULT_TRACE_SORT)
    user_ids, echoed, caveats = await _user_filter(db, scope, args)
    result: dict[str, Any] = {
        **scope.header(),
        "window": {"period": period, "start": start.isoformat(), "end": end.isoformat()},
        "filters": {**echoed, "agent_ids": common["agent_id"] or [], "models": common["model"] or [],
                    "sources": common["source"] or [], "status": common["status_filter"]},
        "sort": {"by": sort_by, "dir": sort_dir},
    }
    if user_ids is not None and not user_ids:
        result.update(rows=[], note="No user matches these filters today: nothing to list.")
        return _page(result, total=0, offset=0, shown=0, caveats=caveats)

    ensure_trace_read_provider_supported()
    store = build_trace_read_store()
    try:
        rows, total = await store.list_traces(
            db,
            user=_viewer(scope),
            user_group_ids=list(scope.group_ids),
            conversation_id=None,
            level=None,
            date_from=start,
            date_to=end,
            cursor=None,
            cursor_id=None,
            sort_by=sort_by,
            sort_dir=sort_dir,
            user_ids=[safe_uuid(value) for value in user_ids] if user_ids is not None else None,
            **common,
        )
    except HTTPException as exc:
        raise InsightRefusedError(f"Error: {exc.detail}") from None
    from app.services.tracing.run_rollup import PERCENTILE_FIGURES, rollup_percentiles, stored_rollup

    items = []
    figures: list[dict[str, Any]] = []
    for row in rows:
        trace = row[0]
        error = trace.error_message or None
        stored = stored_rollup(getattr(trace, "metadata_json", None))
        # The run's figures as numbers only: what the run cost in time,
        # calls and tokens, never what it read or wrote.
        rollup = (
            {name: stored[name] for name in (*PERCENTILE_FIGURES, "generations", "cache_write_tokens") if name in stored}
            if stored is not None
            else None
        )
        if rollup is not None:
            # The cost is the trace's own, priced by the sink after the close.
            figures.append({**rollup, "total_cost": _cost(trace.total_cost)})
        items.append(
            {
                "id": str(trace.id),
                "started_at": _iso(trace.started_at),
                "name": trace.name,
                "agent_id": str(trace.agent_id) if trace.agent_id else None,
                "agent_name": row.agent_name,
                "user_email": row.user_email,
                "source": trace.source,
                "model": trace.model or None,
                "status": trace.status,
                "failure_cause": getattr(trace, "failure_cause", None),
                "duration_ms": trace.duration_ms,
                "total_tokens": int(trace.total_tokens or 0),
                "total_cost": _cost(trace.total_cost),
                "error_excerpt": error[:ERROR_EXCERPT_CHARS] if error else None,
                "rollup": rollup,
            }
        )
    result["rows"] = items
    if figures:
        result["rollup_percentiles"] = rollup_percentiles(figures)
        caveats.append(
            "rollup_percentiles are p50 and p90 over the rows of this page that carry a rollup (an older trace "
            "carries none): widen limit or page with offset for more runs."
        )
    return _page(result, total=int(total), offset=common["offset"], shown=len(items), caveats=caveats)


async def list_platform_sessions(db: AsyncSession, scope: InsightScope, args: dict[str, Any]) -> dict[str, Any]:
    from app.services.tracing.read_store import (
        DEFAULT_TRACE_SESSION_SORT,
        TRACE_LIST_MAX_OFFSET,
        TRACE_SESSION_SORT_FIELDS,
        session_user_emails,
    )
    from app.services.tracing.store import build_trace_read_store, ensure_trace_read_provider_supported

    scope = scope.without_team()
    period, start, end = resolve_period(args.get("period"), args.get("date_from"), args.get("date_to"))
    common = _common(args)
    if common["offset"] > TRACE_LIST_MAX_OFFSET:
        raise InsightRefusedError(f"Error: offset is at most {TRACE_LIST_MAX_OFFSET}; narrow the filters instead.")
    sort_by, sort_dir = _sort(args, TRACE_SESSION_SORT_FIELDS, DEFAULT_TRACE_SESSION_SORT)
    user_ids, echoed, caveats = await _user_filter(db, scope, args)
    result: dict[str, Any] = {
        **scope.header(),
        "window": {"period": period, "start": start.isoformat(), "end": end.isoformat()},
        "filters": {**echoed, "agent_ids": common["agent_id"] or [], "models": common["model"] or [],
                    "sources": common["source"] or [], "status": common["status_filter"]},
        "sort": {"by": sort_by, "dir": sort_dir},
    }
    if user_ids is not None and not user_ids:
        result.update(rows=[], note="No user matches these filters today: nothing to list.")
        return _page(result, total=0, offset=0, shown=0, caveats=caveats)

    ensure_trace_read_provider_supported()
    store = build_trace_read_store()
    try:
        rows, total = await store.list_trace_sessions(
            db,
            user=_viewer(scope),
            user_group_ids=list(scope.group_ids),
            level=None,
            date_from=start,
            date_to=end,
            cursor=None,
            cursor_id=None,
            sort_by=sort_by,
            sort_dir=sort_dir,
            user_ids=[safe_uuid(value) for value in user_ids] if user_ids is not None else None,
            **common,
        )
    except HTTPException as exc:
        raise InsightRefusedError(f"Error: {exc.detail}") from None
    emails = await session_user_emails(db, [row.conversation_id for row in rows])
    result["rows"] = [
        {
            "conversation_id": str(row.conversation_id),
            "user_email": emails.get(str(row.conversation_id)),
            "first_activity_at": _iso(row.first_activity_at),
            "last_activity_at": _iso(row.last_activity_at),
            "trace_count": int(row.trace_count or 0),
            "error_count": int(row.error_count or 0),
            "agent_count": int(row.agent_count or 0),
            "model_count": int(row.model_count or 0),
            "total_tokens": int(row.total_tokens or 0),
            "total_cost": _cost(row.total_cost),
        }
        for row in rows
    ]
    return _page(result, total=int(total), offset=common["offset"], shown=len(rows), caveats=caveats)
