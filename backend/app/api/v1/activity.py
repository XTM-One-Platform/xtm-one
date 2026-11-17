# SPDX-License-Identifier: Apache-2.0

"""Activity feed endpoints – list + SSE stream."""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, resolve_session_jwt
from app.api.v1.helpers import (
    get_user_group_ids,
    resolve_owner_names,
    run_visible_filter,
    user_visible_filter,
)
from app.core.database import get_db, release_db_connection_before_loop
from app.core.redis import close_pubsub, open_pubsub, redis_pubsub_client
from app.core.sessions import (
    SESSION_EXPIRED_CODE,
    SESSION_RECHECK_SECONDS,
    SessionEndedError,
    WebSession,
    recheck_session,
)
from app.models.activity import Activity
from app.models.user import User

router = APIRouter()
logger = logging.getLogger(__name__)

# Max lifetime of a single /activity/stream SSE response (seconds). SSE
# clients (EventSource) reconnect automatically, so ending the response is
# invisible to the dashboard. Unbounded streams never complete, which held
# uvicorn's graceful drain open forever during MAX_REQUESTS worker recycling
# (probe-failure pod restarts under load) and let a stream
# outlive its JWT.
SSE_MAX_LIFETIME_SECONDS = 15 * 60


class ActivityResponse(BaseModel):
    id: str
    activity_type: str
    agent_id: str | None = None
    agent_name: str | None = None
    title: str
    summary: str | None = None
    metadata: dict | None = None
    created_at: str

    model_config = {"from_attributes": True}


@router.get("", response_model=list[ActivityResponse])
async def list_activity(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    activity_type: str | None = None,
):
    query = select(Activity).where(Activity.user_id == user.id)
    if activity_type:
        # Comma-separated list, so one request can carry the several types that
        # make up "things to show this user" — the notification bell needs the
        # operational alerts alongside plain notifications, and a
        # second round-trip per type would be paid on every SSE invalidation.
        # A single value keeps behaving exactly as before.
        # Deduplicated and capped: the value is caller-supplied, and while
        # ``in_()`` binds it safely, an unbounded list is still a query the
        # database has to plan.
        wanted = list(
            dict.fromkeys(t.strip() for t in activity_type.split(",") if t.strip())
        )[:20]
        if wanted:
            query = query.where(Activity.activity_type.in_(wanted))
    result = await db.execute(
        query.order_by(Activity.created_at.desc()).limit(limit).offset(offset)
    )
    return [
        ActivityResponse(
            id=str(a.id),
            activity_type=a.activity_type,
            agent_id=str(a.agent_id) if a.agent_id else None,
            agent_name=a.agent_name,
            title=a.title,
            summary=a.summary,
            metadata=a.extra_data,
            created_at=a.created_at.isoformat(),
        )
        for a in result.scalars().all()
    ]


@router.get("/stats")
async def dashboard_stats(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    activity_range: str = "all",
    scope: str = "user",
):
    """Get dashboard statistics.

    **scope=user** (default): user-scoped — shows only entities visible to the
    current user (own + group-shared + company-managed).  Used by the home dashboard.

    **scope=platform**: platform-wide — shows all entities without visibility
    filtering.  Used by the admin settings dashboard.

    Performance: uses CASE-WHEN aggregations to consolidate ~20 individual
    COUNT queries into ~7 queries (one per table), critical for 200+ users.
    """
    # scope=platform requires admin privileges
    if scope == "platform" and not user.is_admin:
        raise HTTPException(
            status_code=403, detail="Platform scope requires admin privileges"
        )

    from app.models.agent import Agent
    from app.models.agent_skill import AgentSkill
    from app.models.assignment import Assignment, AssignmentRun
    from app.models.chat import Conversation, Message
    from app.models.custom_tool import CustomTool
    from app.models.integration import Integration
    from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
    from app.models.mcp_server import MCPServer
    from app.models.prompt import Prompt
    from app.models.user import User as UserModel

    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    month_ago = now - timedelta(days=30)
    two_months_ago = now - timedelta(days=60)
    week_ago = now - timedelta(days=7)

    # Resolve activity range cutoff for top-active charts
    _range_map = {
        "1w": timedelta(days=7),
        "1m": timedelta(days=30),
        "3m": timedelta(days=90),
        "6m": timedelta(days=180),
        "1y": timedelta(days=365),
    }
    activity_cutoff: datetime | None = None  # None = all time
    if activity_range in _range_map:
        activity_cutoff = now - _range_map[activity_range]

    # --- Visibility setup ---
    # scope=user  → three-tier visibility (own + group-shared + company-managed)
    # scope=platform → no filtering (all entities, for admin settings dashboard)
    _platform = scope == "platform"
    if not _platform:
        user_group_ids = await get_user_group_ids(db, user.id)
        _agent_vis = user_visible_filter(Agent, user.id, user_group_ids)
        _visible_agent_ids = select(Agent.id).where(_agent_vis)
        # Run-derived dashboard data (run counts, notification count, recent
        # digests, failed runs, per-agent last run / weekly runs, top agents /
        # assignments by runs) must follow RUN visibility, NOT agent visibility:
        # a group-shared agent is read-only-visible to every group member, but
        # its runs stay private to the run owner. Only own runs + company-managed
        # runs (read-only) are surfaced — mirroring ``_filtered_runs_query`` in
        # ``assignments.py`` so the dashboard never shows runs (or run content
        # like digest summaries) the user cannot open. Agent / assignment /
        # conversation counts keep using agent visibility.
        _run_vis = run_visible_filter(user.id)

    # --- Consolidated agent counts ---
    _agent_q = select(
        func.count().label("total"),
        func.count().filter(Agent.enabled.is_(True)).label("active"),
        func.count()
        .filter(Agent.enabled.is_(True), Agent.created_at < month_ago)
        .label("active_prev_month"),
    ).select_from(Agent)
    if not _platform:
        _agent_q = _agent_q.where(_agent_vis)
    agent_stats = (await db.execute(_agent_q)).one()
    agents_total = agent_stats.total or 0
    agents_active = agent_stats.active or 0
    agents_prev_month = agent_stats.active_prev_month or 0

    # --- Consolidated assignment counts ---
    _wf_q = select(
        func.count().label("total"),
        func.count().filter(Assignment.enabled.is_(True)).label("active"),
        func.count()
        .filter(Assignment.enabled.is_(True), Assignment.created_at < month_ago)
        .label("active_prev_month"),
    ).select_from(Assignment)
    if not _platform:
        _wf_q = _wf_q.where(Assignment.agent_id.in_(_visible_agent_ids))
    wf_stats = (await db.execute(_wf_q)).one()
    assignments_total = wf_stats.total or 0
    assignments_active = wf_stats.active or 0
    assignments_prev_month = wf_stats.active_prev_month or 0

    # --- Consolidated run counts ---
    _run_q = select(
        func.count().label("total"),
        func.count().filter(AssignmentRun.status == "completed").label("completed"),
        func.count().filter(AssignmentRun.status == "failed").label("failed"),
        func.count().filter(AssignmentRun.created_at >= today_start).label("today"),
        func.count()
        .filter(AssignmentRun.status.in_(["pending", "running", "delayed"]))
        .label("running"),
        func.count()
        .filter(
            AssignmentRun.created_at >= yesterday_start,
            AssignmentRun.created_at < today_start,
        )
        .label("yesterday"),
        func.count()
        .filter(
            AssignmentRun.status == "completed",
            AssignmentRun.created_at >= yesterday_start,
            AssignmentRun.created_at < today_start,
        )
        .label("completed_yesterday"),
        func.count()
        .filter(
            AssignmentRun.status == "failed",
            AssignmentRun.created_at >= yesterday_start,
            AssignmentRun.created_at < today_start,
        )
        .label("failed_yesterday"),
        # this month (last 30 days) and previous month (30-60 days ago)
        func.count().filter(AssignmentRun.created_at >= month_ago).label("this_month"),
        func.count()
        .filter(
            AssignmentRun.created_at >= two_months_ago,
            AssignmentRun.created_at < month_ago,
        )
        .label("prev_month_count"),
        # prev-month snapshots for total / completed / failed
        func.count()
        .filter(AssignmentRun.created_at < month_ago)
        .label("total_prev_month"),
        func.count()
        .filter(
            AssignmentRun.status == "completed", AssignmentRun.created_at < month_ago
        )
        .label("completed_prev_month"),
        func.count()
        .filter(AssignmentRun.status == "failed", AssignmentRun.created_at < month_ago)
        .label("failed_prev_month"),
    ).select_from(AssignmentRun)
    if not _platform:
        _run_q = _run_q.where(_run_vis)
    run_stats = (await db.execute(_run_q)).one()
    runs_total = run_stats.total or 0
    runs_completed = run_stats.completed or 0
    runs_failed = run_stats.failed or 0
    runs_today = run_stats.today or 0
    runs_this_month = run_stats.this_month or 0
    runs_prev_month_count = run_stats.prev_month_count or 0
    runs_running = run_stats.running or 0
    runs_yesterday = run_stats.yesterday or 0
    runs_completed_yesterday = run_stats.completed_yesterday or 0
    runs_failed_yesterday = run_stats.failed_yesterday or 0
    runs_total_prev_month = run_stats.total_prev_month or 0
    runs_completed_prev_month = run_stats.completed_prev_month or 0
    runs_failed_prev_month = run_stats.failed_prev_month or 0
    success_rate_yesterday = round(
        runs_completed_yesterday / max(runs_yesterday, 1) * 100, 1
    )

    # --- Consolidated integration count ---
    _integ_q = select(
        func.count()
        .filter(
            Integration.status == "connected",
        )
        .label("connected"),
        func.count()
        .filter(
            Integration.status == "connected",
            Integration.created_at < month_ago,
        )
        .label("connected_prev_month"),
    ).select_from(Integration)
    if not _platform:
        _integ_q = _integ_q.where(
            user_visible_filter(Integration, user.id, user_group_ids)
        )
    integ_stats = (await db.execute(_integ_q)).one()
    integrations_connected = integ_stats.connected or 0
    integrations_prev_month = integ_stats.connected_prev_month or 0

    # --- KB + document counts ---
    _kb_q = select(func.count()).select_from(KnowledgeBase)
    if not _platform:
        _kb_vis = user_visible_filter(KnowledgeBase, user.id, user_group_ids)
        _kb_q = _kb_q.where(_kb_vis)
        _visible_kb_ids = select(KnowledgeBase.id).where(_kb_vis)
    kbs_total = await db.scalar(_kb_q) or 0
    _doc_q = select(
        func.count().label("total"),
        func.count()
        .filter(KnowledgeDocument.created_at < month_ago)
        .label("prev_month"),
    ).select_from(KnowledgeDocument)
    if not _platform:
        _doc_q = _doc_q.where(KnowledgeDocument.knowledge_base_id.in_(_visible_kb_ids))
    doc_stats = (await db.execute(_doc_q)).one()
    document_count = doc_stats.total or 0
    documents_prev_month = doc_stats.prev_month or 0

    # --- Custom tool / MCP server / Skill / Prompt counts ---
    _tool_q = select(
        func.count().label("total"),
        func.count().filter(CustomTool.created_at < month_ago).label("prev_month"),
    ).select_from(CustomTool)
    if not _platform:
        _tool_q = _tool_q.where(
            user_visible_filter(CustomTool, user.id, user_group_ids)
        )
    tool_stats = (await db.execute(_tool_q)).one()
    custom_tool_count = tool_stats.total or 0
    custom_tools_prev_month = tool_stats.prev_month or 0

    _mcp_q = select(
        func.count().label("total"),
        func.count().filter(MCPServer.created_at < month_ago).label("prev_month"),
    ).select_from(MCPServer)
    if not _platform:
        _mcp_q = _mcp_q.where(user_visible_filter(MCPServer, user.id, user_group_ids))
    mcp_stats = (await db.execute(_mcp_q)).one()
    mcp_server_count = mcp_stats.total or 0
    mcp_servers_prev_month = mcp_stats.prev_month or 0

    _skill_q = select(
        func.count().label("total"),
        func.count().filter(AgentSkill.created_at < month_ago).label("prev_month"),
    ).select_from(AgentSkill)
    if not _platform:
        _skill_q = _skill_q.where(
            user_visible_filter(AgentSkill, user.id, user_group_ids)
        )
    skill_stats = (await db.execute(_skill_q)).one()
    skill_count = skill_stats.total or 0
    skills_prev_month = skill_stats.prev_month or 0

    _prompt_q = select(
        func.count().label("total"),
        func.count().filter(Prompt.created_at < month_ago).label("prev_month"),
    ).select_from(Prompt)
    if not _platform:
        _prompt_q = _prompt_q.where(
            user_visible_filter(Prompt, user.id, user_group_ids)
        )
    prompt_stats = (await db.execute(_prompt_q)).one()
    prompt_count = prompt_stats.total or 0
    prompts_prev_month = prompt_stats.prev_month or 0

    # --- User count (platform-wide — always global) ---
    user_stats = (
        await db.execute(
            select(
                func.count().label("total"),
                func.count()
                .filter(UserModel.created_at < month_ago)
                .label("prev_month"),
            )
        )
    ).one()
    user_count = user_stats.total or 0
    users_prev_month = user_stats.prev_month or 0

    # --- Group count (user groups — always global) ---
    from app.models.group import Group as GroupModel

    group_count_result = (
        await db.execute(select(func.count()).select_from(GroupModel))
    ).scalar() or 0

    # --- Conversation counts ---
    _conv_where = [Conversation.is_archived.is_(False)]
    if not _platform:
        _conv_where.append(Conversation.user_id == user.id)
    conv_stats = (
        await db.execute(
            select(
                func.count().label("total"),
                func.count()
                .filter(Conversation.created_at < month_ago)
                .label("prev_month"),
            ).where(*_conv_where)
        )
    ).one()
    conversation_count = conv_stats.total or 0
    conversations_prev_month = conv_stats.prev_month or 0

    # --- Message counts ---
    _msg_conv_ids = select(Conversation.id).where(*_conv_where)
    message_count = (
        await db.scalar(
            select(func.count()).where(Message.conversation_id.in_(_msg_conv_ids))
        )
        or 0
    )

    # --- Notification count — runs today that sent a notification ---
    _notif_where = [
        AssignmentRun.notification_sent.is_(True),
        AssignmentRun.created_at >= today_start,
    ]
    if not _platform:
        _notif_where.append(_run_vis)
    notification_count = await db.scalar(select(func.count()).where(*_notif_where)) or 0

    # --- Per-agent summary (batch queries to avoid N+1) ---
    _agent_sum_q = (
        select(Agent)
        .where(Agent.enabled.is_(True), Agent.is_sandbox.is_(False))
        .order_by(Agent.updated_at.desc())
    )
    if not _platform:
        _agent_sum_q = _agent_sum_q.where(_agent_vis)
    agents_result = await db.execute(_agent_sum_q)
    agents_list = agents_result.scalars().all()
    agent_ids_list = [a.id for a in agents_list]

    # Batch: assignment counts per agent
    wf_counts_map: dict = {}
    if agent_ids_list:
        wf_counts_result = await db.execute(
            select(Assignment.agent_id, func.count())
            .where(Assignment.agent_id.in_(agent_ids_list))
            .group_by(Assignment.agent_id)
        )
        wf_counts_map = dict(wf_counts_result.all())

    # Batch: runs this week per agent
    runs_week_map: dict = {}
    if agent_ids_list:
        _runs_week_where = [
            AssignmentRun.agent_id.in_(agent_ids_list),
            AssignmentRun.created_at >= week_ago,
        ]
        if not _platform:
            _runs_week_where.append(_run_vis)
        runs_week_result = await db.execute(
            select(AssignmentRun.agent_id, func.count())
            .where(*_runs_week_where)
            .group_by(AssignmentRun.agent_id)
        )
        runs_week_map = dict(runs_week_result.all())

    # Batch: last run per agent (using a lateral subquery pattern)
    from sqlalchemy import desc

    last_runs_map: dict = {}
    if agent_ids_list:
        # Use a window function to get the last run per agent efficiently
        _ranked_where = [AssignmentRun.agent_id.in_(agent_ids_list)]
        if not _platform:
            _ranked_where.append(_run_vis)
        ranked_runs = (
            select(
                AssignmentRun,
                func.row_number()
                .over(
                    partition_by=AssignmentRun.agent_id,
                    order_by=desc(AssignmentRun.created_at),
                )
                .label("rn"),
            )
            .where(*_ranked_where)
            .subquery()
        )
        last_runs_result = await db.execute(
            select(ranked_runs).where(ranked_runs.c.rn == 1)
        )
        for row in last_runs_result.all():
            last_runs_map[row.agent_id] = row

    # Batch: chat query counts per agent (total + this week)
    chat_total_map: dict = {}
    chat_week_map: dict = {}
    if agent_ids_list:
        chat_total_result = await db.execute(
            select(Conversation.agent_id, func.count(Message.id))
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(Conversation.agent_id.in_(agent_ids_list), Message.role == "user")
            .group_by(Conversation.agent_id)
        )
        chat_total_map = dict(chat_total_result.all())

        chat_week_result = await db.execute(
            select(Conversation.agent_id, func.count(Message.id))
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(
                Conversation.agent_id.in_(agent_ids_list),
                Message.role == "user",
                Message.created_at >= week_ago,
            )
            .group_by(Conversation.agent_id)
        )
        chat_week_map = dict(chat_week_result.all())

    owner_names = await resolve_owner_names(db, agents_list)

    agent_summaries = []
    for agent in agents_list:
        last_run_row = last_runs_map.get(agent.id)
        agent_summaries.append(
            {
                "id": str(agent.id),
                "name": agent.name,
                "icon": agent.icon,
                "enabled": agent.enabled,
                "is_company_managed": agent.is_company_managed,
                "user_id": str(agent.user_id) if agent.user_id else None,
                "owner_name": owner_names.get(str(agent.user_id)),
                "shared_group_ids": agent.shared_group_ids,
                "assignment_count": wf_counts_map.get(agent.id, 0),
                "runs_this_week": runs_week_map.get(agent.id, 0),
                "chat_query_count": chat_total_map.get(agent.id, 0),
                "chat_queries_this_week": chat_week_map.get(agent.id, 0),
                "last_run": (
                    {
                        "status": last_run_row.status,
                        "summary": last_run_row.result_summary,
                        "created_at": last_run_row.created_at.isoformat(),
                        "error": last_run_row.error,
                    }
                    if last_run_row
                    else None
                ),
            }
        )

    # --- Recent completed runs that are marked as digests ---
    _digest_where = [
        AssignmentRun.status == "completed",
        AssignmentRun.result_summary.isnot(None),
        AssignmentRun.result_summary != "",
        Assignment.show_in_digest.is_(True),
    ]
    if not _platform:
        _digest_where.append(_run_vis)
    recent_runs_result = await db.execute(
        select(AssignmentRun)
        .join(Assignment, AssignmentRun.assignment_id == Assignment.id)
        .where(*_digest_where)
        .order_by(AssignmentRun.created_at.desc())
        .limit(9)
    )
    recent_runs_list = recent_runs_result.scalars().all()

    # Batch-fetch agents and assignments for digest + failed runs (avoid N+1)
    digest_agent_ids = {r.agent_id for r in recent_runs_list if r.agent_id}
    digest_wf_ids = {r.assignment_id for r in recent_runs_list if r.assignment_id}

    # --- Failed runs (need attention) ---
    _failed_where = [AssignmentRun.status == "failed"]
    if not _platform:
        _failed_where.append(_run_vis)
    failed_result = await db.execute(
        select(AssignmentRun)
        .where(*_failed_where)
        .order_by(AssignmentRun.created_at.desc())
        .limit(5)
    )
    failed_runs_list = failed_result.scalars().all()

    # Extend batch sets with failed run IDs
    digest_agent_ids |= {r.agent_id for r in failed_runs_list if r.agent_id}
    digest_wf_ids |= {r.assignment_id for r in failed_runs_list if r.assignment_id}

    # Single batch load for all agents/assignments needed
    digest_agents_map: dict = {}
    if digest_agent_ids:
        result = await db.execute(select(Agent).where(Agent.id.in_(digest_agent_ids)))
        digest_agents_map = {a.id: a for a in result.scalars().all()}

    digest_wf_map: dict = {}
    if digest_wf_ids:
        result = await db.execute(
            select(Assignment).where(Assignment.id.in_(digest_wf_ids))
        )
        digest_wf_map = {w.id: w for w in result.scalars().all()}

    recent_digests = []
    for run in recent_runs_list:
        ag = digest_agents_map.get(run.agent_id)
        wf = digest_wf_map.get(run.assignment_id)
        recent_digests.append(
            {
                "id": str(run.id),
                "agent_name": ag.name if ag else "Unknown",
                "agent_icon": ag.icon if ag else "Bot",
                "assignment_name": wf.name if wf else "Unknown",
                "summary": run.result_summary,
                "actions_count": len(run.actions_taken) if run.actions_taken else 0,
                "created_at": run.created_at.isoformat(),
            }
        )

    failed_runs = []
    for run in failed_runs_list:
        ag = digest_agents_map.get(run.agent_id)
        wf = digest_wf_map.get(run.assignment_id)
        failed_runs.append(
            {
                "id": str(run.id),
                "agent_name": ag.name if ag else "Unknown",
                "assignment_name": wf.name if wf else "Unknown",
                "error": run.error,
                "created_at": run.created_at.isoformat(),
            }
        )

    success_rate = round(runs_completed / max(runs_total, 1) * 100, 1)

    # --- Top active users (by conversations + assignment runs) ---
    _conv_user_q = select(Conversation.user_id, func.count().label("cnt")).where(
        Conversation.user_id.isnot(None)
    )
    if activity_cutoff is not None:
        _conv_user_q = _conv_user_q.where(Conversation.created_at >= activity_cutoff)
    top_users_conv = (
        await db.execute(_conv_user_q.group_by(Conversation.user_id))
    ).all()
    _run_user_q = select(AssignmentRun.user_id, func.count().label("cnt")).where(
        AssignmentRun.user_id.isnot(None),
        # Exclude company-managed automation so "top active users" reflects
        # genuine personal activity (these runs are now owned by the admin /
        # run-as user instead of being NULL).
        AssignmentRun.is_company_managed.is_(False),
    )
    if activity_cutoff is not None:
        _run_user_q = _run_user_q.where(AssignmentRun.created_at >= activity_cutoff)
    top_users_runs = (
        await db.execute(_run_user_q.group_by(AssignmentRun.user_id))
    ).all()
    user_activity: dict[uuid.UUID, int] = {}
    for uid, cnt in top_users_conv:
        user_activity[uid] = user_activity.get(uid, 0) + cnt
    for uid, cnt in top_users_runs:
        user_activity[uid] = user_activity.get(uid, 0) + cnt
    sorted_users = sorted(user_activity.items(), key=lambda x: x[1], reverse=True)[:10]
    top_user_ids = [uid for uid, _ in sorted_users]
    user_names_map: dict[uuid.UUID, str] = {}
    if top_user_ids:
        user_rows = await db.execute(
            select(UserModel.id, UserModel.full_name, UserModel.email).where(
                UserModel.id.in_(top_user_ids)
            )
        )
        user_names_map = {r.id: r.full_name or r.email for r in user_rows.all()}
    top_users = [
        {"name": user_names_map.get(uid, "Unknown"), "value": cnt}
        for uid, cnt in sorted_users
    ]

    # --- Top active agents (by conversations + assignment runs) ---
    _conv_agent_where = [Conversation.agent_id.isnot(None)]
    if not _platform:
        _conv_agent_where.append(Conversation.agent_id.in_(_visible_agent_ids))
    _conv_agent_q = select(Conversation.agent_id, func.count().label("cnt")).where(
        *_conv_agent_where
    )
    if activity_cutoff is not None:
        _conv_agent_q = _conv_agent_q.where(Conversation.created_at >= activity_cutoff)
    top_agents_conv = (
        await db.execute(_conv_agent_q.group_by(Conversation.agent_id))
    ).all()
    _run_agent_where = [AssignmentRun.agent_id.isnot(None)]
    if not _platform:
        _run_agent_where.append(_run_vis)
    _run_agent_q = select(AssignmentRun.agent_id, func.count().label("cnt")).where(
        *_run_agent_where
    )
    if activity_cutoff is not None:
        _run_agent_q = _run_agent_q.where(AssignmentRun.created_at >= activity_cutoff)
    top_agents_runs = (
        await db.execute(_run_agent_q.group_by(AssignmentRun.agent_id))
    ).all()
    agent_activity: dict[uuid.UUID, int] = {}
    for aid, cnt in top_agents_conv:
        agent_activity[aid] = agent_activity.get(aid, 0) + cnt
    for aid, cnt in top_agents_runs:
        agent_activity[aid] = agent_activity.get(aid, 0) + cnt
    sorted_agents = sorted(agent_activity.items(), key=lambda x: x[1], reverse=True)[
        :10
    ]
    top_agent_ids = [aid for aid, _ in sorted_agents]
    agent_names_map: dict[uuid.UUID, str] = {}
    if top_agent_ids:
        agent_rows = await db.execute(
            select(Agent.id, Agent.name).where(Agent.id.in_(top_agent_ids))
        )
        agent_names_map = {r.id: r.name for r in agent_rows.all()}
    top_agents = [
        {"name": agent_names_map.get(aid, "Unknown"), "value": cnt}
        for aid, cnt in sorted_agents
    ]

    # --- Top active assignments (by runs) ---
    _run_wf_where = [AssignmentRun.assignment_id.isnot(None)]
    if not _platform:
        _run_wf_where.append(_run_vis)
    _run_wf_q = select(AssignmentRun.assignment_id, func.count().label("cnt")).where(
        *_run_wf_where
    )
    if activity_cutoff is not None:
        _run_wf_q = _run_wf_q.where(AssignmentRun.created_at >= activity_cutoff)
    top_wf_result = (
        await db.execute(
            _run_wf_q.group_by(AssignmentRun.assignment_id)
            .order_by(func.count().desc())
            .limit(10)
        )
    ).all()
    top_wf_ids = [wid for wid, _ in top_wf_result]
    wf_names_map: dict[uuid.UUID, str] = {}
    if top_wf_ids:
        wf_rows = await db.execute(
            select(Assignment.id, Assignment.name).where(Assignment.id.in_(top_wf_ids))
        )
        wf_names_map = {r.id: r.name for r in wf_rows.all()}
    top_assignments = [
        {"name": wf_names_map.get(wid, "Unknown"), "value": cnt}
        for wid, cnt in top_wf_result
    ]

    # --- Top API key users (by total request count) ---
    from app.models.api_key import UserApiKey

    _apikey_q = select(
        UserApiKey.user_id,
        func.sum(UserApiKey.request_count).label("total_requests"),
    ).where(
        UserApiKey.is_active.is_(True),
        UserApiKey.request_count > 0,
    )
    if activity_cutoff is not None:
        _apikey_q = _apikey_q.where(UserApiKey.last_used_at >= activity_cutoff)
    _apikey_result = (
        await db.execute(
            _apikey_q.group_by(UserApiKey.user_id)
            .order_by(func.sum(UserApiKey.request_count).desc())
            .limit(10)
        )
    ).all()
    _apikey_user_ids = [row.user_id for row in _apikey_result]
    _apikey_user_names: dict[uuid.UUID, str] = {}
    if _apikey_user_ids:
        _apikey_users = await db.execute(
            select(UserModel.id, UserModel.full_name, UserModel.email).where(
                UserModel.id.in_(_apikey_user_ids)
            )
        )
        _apikey_user_names = {r.id: r.full_name or r.email for r in _apikey_users.all()}
    top_api_key_users = [
        {
            "name": _apikey_user_names.get(row.user_id, "Unknown"),
            "value": row.total_requests,
        }
        for row in _apikey_result
    ]

    # --- Top active integrations (by tool call count) ---
    from app.models.integration import Integration as _IntModel

    _int_q = select(
        _IntModel.provider.label("provider"),
        func.sum(_IntModel.call_count).label("cnt"),
    ).where(_IntModel.call_count > 0)
    if not _platform:
        _int_q = _int_q.where(user_visible_filter(_IntModel, user.id, user_group_ids))
    if activity_cutoff is not None:
        _int_q = _int_q.where(_IntModel.last_called_at >= activity_cutoff)
    _int_result = (
        await db.execute(
            _int_q.group_by(_IntModel.provider)
            .order_by(func.sum(_IntModel.call_count).desc())
            .limit(10)
        )
    ).all()
    top_integrations = [
        {"name": row.provider or "unknown", "value": row.cnt or 0}
        for row in _int_result
    ]

    # --- Top active channels (by message count) ---
    _ch_q = select(
        Message.channel.label("channel"),
        func.count().label("cnt"),
    ).where(Message.role.in_(["user", "assistant"]))
    if not _platform:
        _ch_conv_ids = select(Conversation.id).where(Conversation.user_id == user.id)
        _ch_q = _ch_q.where(Message.conversation_id.in_(_ch_conv_ids))
    if activity_cutoff is not None:
        _ch_q = _ch_q.where(Message.created_at >= activity_cutoff)
    _ch_result = (
        await db.execute(
            _ch_q.group_by(Message.channel).order_by(func.count().desc()).limit(10)
        )
    ).all()
    top_channels = [{"name": row.channel, "value": row.cnt} for row in _ch_result]

    return {
        "agents": {"total": agents_total, "active": agents_active},
        "assignments": {"total": assignments_total, "active": assignments_active},
        "runs": {
            "total": runs_total,
            "completed": runs_completed,
            "failed": runs_failed,
            "today": runs_today,
            "this_month": runs_this_month,
            "running": runs_running,
        },
        "integrations_connected": integrations_connected,
        "knowledge_bases": kbs_total,
        "document_count": document_count,
        "custom_tool_count": custom_tool_count,
        "mcp_server_count": mcp_server_count,
        "skill_count": skill_count,
        "prompt_count": prompt_count,
        "user_count": user_count,
        "group_count": group_count_result,
        "conversation_count": conversation_count,
        "message_count": message_count,
        "notification_count": notification_count,
        "agent_summaries": agent_summaries,
        "recent_digests": recent_digests,
        "failed_runs": failed_runs,
        "success_rate": success_rate,
        "top_users": top_users,
        "top_agents": top_agents,
        "top_assignments": top_assignments,
        "top_api_key_users": top_api_key_users,
        "top_integrations": top_integrations,
        "top_channels": top_channels,
        # Comparison data for KPI diffs
        "diffs": {
            "runs_yesterday": runs_yesterday,
            "runs_prev_month_count": runs_prev_month_count,
            "success_rate_yesterday": success_rate_yesterday,
            "agents_prev_month": agents_prev_month,
            "assignments_prev_month": assignments_prev_month,
            "integrations_prev_month": integrations_prev_month,
            "documents_prev_month": documents_prev_month,
            "conversations_prev_month": conversations_prev_month,
            "custom_tools_prev_month": custom_tools_prev_month,
            "mcp_servers_prev_month": mcp_servers_prev_month,
            "skills_prev_month": skills_prev_month,
            "prompts_prev_month": prompts_prev_month,
            "users_prev_month": users_prev_month,
            "runs_total_prev_month": runs_total_prev_month,
            "runs_completed_prev_month": runs_completed_prev_month,
            "runs_failed_prev_month": runs_failed_prev_month,
            "runs_failed_yesterday": runs_failed_yesterday,
        },
    }


@router.get("/adoption")
async def adoption_timeseries(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    interval: str = "1w",
):
    """Time series data for platform adoption metrics.

    Returns bucketed counts for chat messages, agentic executions, and
    cumulative agent count.  **Admin only.**

    Query params:
      interval – bucket size: ``1d`` (daily, 30 days), ``1w`` (weekly,
                 12 weeks, default), ``1m`` (monthly, 12 months).
    """
    if not user.is_admin:
        raise HTTPException(
            status_code=403, detail="Platform adoption data requires admin privileges"
        )

    from app.models.agent import Agent
    from app.models.assignment import AssignmentRun
    from app.models.chat import Message

    now = datetime.now(UTC)

    if interval not in ("1d", "1w", "1m"):
        interval = "1w"

    _trunc_map = {"1d": "day", "1w": "week", "1m": "month"}
    _range_map = {
        "1d": timedelta(days=30),
        "1w": timedelta(days=84),
        "1m": timedelta(days=365),
    }
    trunc = _trunc_map[interval]
    lookback = _range_map[interval]
    cutoff = now - lookback

    # ── Messages over time ────────────────────────────────────────────
    _msg_bucket = func.date_trunc(trunc, Message.created_at)
    msg_result = (
        await db.execute(
            select(_msg_bucket.label("bucket"), func.count().label("cnt"))
            .where(
                Message.role.in_(["user", "assistant"]),
                Message.created_at >= cutoff,
            )
            .group_by(_msg_bucket)
            .order_by(_msg_bucket)
        )
    ).all()
    messages_over_time = [
        {"date": row.bucket.strftime("%Y-%m-%d"), "value": row.cnt}
        for row in msg_result
    ]

    # ── Agentic executions over time ──────────────────────────────────
    _exec_bucket = func.date_trunc(trunc, AssignmentRun.created_at)
    exec_result = (
        await db.execute(
            select(_exec_bucket.label("bucket"), func.count().label("cnt"))
            .where(AssignmentRun.created_at >= cutoff)
            .group_by(_exec_bucket)
            .order_by(_exec_bucket)
        )
    ).all()
    executions_over_time = [
        {"date": row.bucket.strftime("%Y-%m-%d"), "value": row.cnt}
        for row in exec_result
    ]

    # ── Agents cumulative over time ───────────────────────────────────
    base_count = (
        await db.scalar(
            select(func.count())
            .select_from(Agent)
            .where(Agent.created_at < cutoff, Agent.is_sandbox.is_(False))
        )
        or 0
    )
    _ag_bucket = func.date_trunc(trunc, Agent.created_at)
    agent_result = (
        await db.execute(
            select(_ag_bucket.label("bucket"), func.count().label("cnt"))
            .where(Agent.created_at >= cutoff, Agent.is_sandbox.is_(False))
            .group_by(_ag_bucket)
            .order_by(_ag_bucket)
        )
    ).all()

    running = base_count
    agents_over_time: list[dict] = [
        {"date": cutoff.strftime("%Y-%m-%d"), "value": running}
    ]
    for row in agent_result:
        running += row.cnt
        agents_over_time.append(
            {"date": row.bucket.strftime("%Y-%m-%d"), "value": running}
        )
    if len(agents_over_time) == 1:
        agents_over_time.append({"date": now.strftime("%Y-%m-%d"), "value": running})

    # ── API Key calls over time (sum api_key_requests from platform metrics) ──
    from app.models.ai_adoption_metric import AiAdoptionMetric

    _api_bucket = func.date_trunc(trunc, AiAdoptionMetric.date)
    api_raw = (
        await db.execute(
            select(
                _api_bucket.label("bucket"),
                AiAdoptionMetric.data,
            ).where(
                AiAdoptionMetric.provider == "copilot_platform",
                AiAdoptionMetric.date >= cutoff.date(),
                AiAdoptionMetric.user_id.isnot(None),
            )
        )
    ).all()

    api_by_bucket: dict[str, int] = {}
    for bucket_dt, row_data in api_raw:
        bucket_str = bucket_dt.strftime("%Y-%m-%d")
        val = 0
        if row_data and isinstance(row_data, dict):
            val = int(row_data.get("api_key_requests") or 0) + int(
                row_data.get("messages") or 0
            )
        api_by_bucket[bucket_str] = api_by_bucket.get(bucket_str, 0) + val

    api_key_calls_over_time = [
        {"date": d, "value": v} for d, v in sorted(api_by_bucket.items())
    ]

    return {
        "messages_over_time": messages_over_time,
        "executions_over_time": executions_over_time,
        "agents_over_time": agents_over_time,
        "api_key_calls_over_time": api_key_calls_over_time,
    }


@router.get("/traces")
async def platform_traces(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    minutes: int = 60,
):
    """Return recent platform flow traces for the dashboard flow visualization.

    Extracts structured trace events from recent assignment runs (actions_taken)
    and activity entries, providing a real-time view of data flowing between
    agents, integrations, and knowledge bases.
    """
    from app.models.agent import Agent
    from app.models.assignment import Assignment, AssignmentRun

    since = datetime.now(UTC) - timedelta(minutes=max(min(minutes, 1440), 1))

    # Fetch recent runs (include company-managed runs visible to all users).
    # Uses the shared ``run_visible_filter`` so this can't drift from the
    # canonical "which runs can this user see" contract.
    runs_result = await db.execute(
        select(AssignmentRun)
        .where(
            run_visible_filter(user.id),
            AssignmentRun.created_at >= since,
        )
        .order_by(AssignmentRun.created_at.desc())
        .limit(30)
    )
    runs = runs_result.scalars().all()

    # Batch-fetch agents and assignments for efficiency
    agent_ids = {r.agent_id for r in runs if r.agent_id}
    assignment_ids = {r.assignment_id for r in runs if r.assignment_id}

    agents_map: dict = {}
    if agent_ids:
        result = await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))
        agents_map = {a.id: a for a in result.scalars().all()}

    assignments_map: dict = {}
    if assignment_ids:
        result = await db.execute(
            select(Assignment).where(Assignment.id.in_(assignment_ids))
        )
        assignments_map = {w.id: w for w in result.scalars().all()}

    traces: list[dict] = []
    for run in runs:
        agent = agents_map.get(run.agent_id)
        assignment = assignments_map.get(run.assignment_id)
        if not agent or not assignment:
            continue

        ag_name = agent.name
        ag_id = str(agent.id)

        # Trace: assignment triggered
        traces.append(
            {
                "id": f"{run.id}-trigger",
                "event_type": "assignment_trigger",
                "source_type": "trigger",
                "source_name": assignment.trigger_type.replace("_", " ").title(),
                "target_type": "agent",
                "target_name": ag_name,
                "target_id": ag_id,
                "label": assignment.name,
                "status": run.status,
                "created_at": run.created_at.isoformat(),
            }
        )

        # Traces from actions_taken
        if run.actions_taken:
            for i, action in enumerate(run.actions_taken):
                if not isinstance(action, dict):
                    continue
                tool = str(action.get("tool", action.get("name", "")))
                if not tool:
                    continue

                ev_type, tgt_type, tgt_name = "tool_call", "tool", tool
                source = str(action.get("source", ""))

                # Classify using the structured source field (available in
                # newer runs).  Falls back to name-based heuristics for
                # older runs that were recorded before the source field
                # was added.
                if source.startswith("custom:"):
                    ev_type = "custom_tool_call"
                    tgt_type = "custom_tool"
                    tgt_name = tool
                elif source.startswith("mcp:"):
                    ev_type = "mcp_tool_call"
                    tgt_type = "mcp_server"
                    tgt_name = source.removeprefix("mcp:")
                elif source.startswith("channel_bot:"):
                    # ``channel_bot:<provider>:<id>``. Named by its
                    # provider, like every other row here — the id is in
                    # ``entity_id`` and means nothing to a reader.
                    ev_type = "integration_call"
                    tgt_type = "channel_bot"
                    tgt_name = source.split(":")[1] if ":" in source else source
                elif source.startswith("integration:"):
                    ev_type = "integration_call"
                    tgt_type = "integration"
                    tgt_name = source.removeprefix("integration:")
                elif source == "builtin:knowledge":
                    ev_type = "rag_query"
                    tgt_type = "knowledge_base"
                    tgt_name = str(action.get("kb_name", "Knowledge Base"))
                elif source.startswith("builtin:"):
                    ev_type = "builtin_tool_call"
                    tgt_type = "builtin"
                    tgt_name = tool
                else:
                    # Fallback: name-based heuristic for older runs
                    low = tool.lower()
                    if any(
                        k in low
                        for k in (
                            "search_knowledge",
                            "vector_search",
                            "rag",
                            "semantic_search",
                            "query_doc",
                        )
                    ):
                        ev_type = "rag_query"
                        tgt_type = "knowledge_base"
                        tgt_name = str(action.get("kb_name", "Knowledge Base"))
                    else:
                        for prov in (
                            "microsoft",
                            "google",
                            "github",
                            "slack",
                            "notion",
                            "opencti",
                            "whatsapp",
                            "teams",
                        ):
                            if prov in low:
                                ev_type = "integration_call"
                                tgt_type = "integration"
                                tgt_name = prov
                                break

                traces.append(
                    {
                        "id": f"{run.id}-a{i}",
                        "event_type": ev_type,
                        "source_type": "agent",
                        "source_name": ag_name,
                        "source_id": ag_id,
                        "target_type": tgt_type,
                        "target_name": tgt_name,
                        "label": tool.replace("_", " ").title(),
                        "status": "completed",
                        "created_at": run.created_at.isoformat(),
                    }
                )

        # Trace: assignment completed/failed
        if run.status in ("completed", "failed"):
            traces.append(
                {
                    "id": f"{run.id}-done",
                    "event_type": "assignment_complete",
                    "source_type": "agent",
                    "source_name": ag_name,
                    "source_id": ag_id,
                    "target_type": "result",
                    "target_name": run.status,
                    "label": (run.result_summary or run.status)[:120],
                    "status": run.status,
                    "created_at": (run.completed_at or run.created_at).isoformat(),
                }
            )

    # Also include recent activity entries
    act_result = await db.execute(
        select(Activity)
        .where(Activity.user_id == user.id, Activity.created_at >= since)
        .order_by(Activity.created_at.desc())
        .limit(15)
    )
    for a in act_result.scalars().all():
        traces.append(
            {
                "id": str(a.id),
                "event_type": a.activity_type,
                "source_type": "agent" if a.agent_id else "system",
                "source_name": a.agent_name or "System",
                "source_id": str(a.agent_id) if a.agent_id else None,
                "target_type": "platform",
                "target_name": (a.title or "")[:60],
                "label": (a.summary or a.title or "")[:120],
                "status": "info",
                "created_at": a.created_at.isoformat(),
            }
        )

    traces.sort(key=lambda t: t["created_at"], reverse=True)
    return traces[:50]


@router.get("/stream")
async def activity_stream(
    request: Request,
    token: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """SSE stream for real-time activity updates.

    EventSource can't set Authorization headers, so this endpoint
    accepts the JWT as a ?token= query parameter. It reads a session token
    only (never an ``fcp-`` key in a URL), under the REST session rules:
    confined tokens, deactivated accounts and tokens issued before the last
    password change are refused exactly as ``deps.get_current_user`` refuses
    them, and so is a session past its absolute lifetime or idle too long -
    checked again while the stream is open, which then ends with a
    ``session_expired`` event.
    """
    # Try query param token first (EventSource), fall back to header
    jwt_token = token
    if not jwt_token:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            jwt_token = auth[7:]

    if not jwt_token:
        raise HTTPException(status_code=401, detail="Missing token")

    user = await resolve_session_jwt(jwt_token, db)
    web_session: WebSession = user.web_session
    # A companion session locked behind its PIN gets no events either: refused
    # here, and the stream ends once it locks while open (checked with the
    # session below).
    from types import SimpleNamespace

    from app.core.database import async_session
    from app.services.app_lock import enforce as enforce_app_lock

    await enforce_app_lock(db, user)
    user_id = user.id

    async def _locked() -> bool:
        """Whether this companion session is now locked behind its PIN, or ended by it.

        A fresh DB session: the request's own connection is released before
        the loop; one that cannot be read ends the stream too. The stream then just ends; the app's next request reads the
        423 (lock screen) or the 401 (signed out), which an EventSource cannot.
        """
        try:
            async with async_session() as lock_db:
                await enforce_app_lock(lock_db, SimpleNamespace(id=user_id, web_session=web_session))
        except HTTPException:
            return True
        except Exception:
            # The lock cannot be read: the stream ends rather than relaying
            # on (fail closed); the reconnect is checked again in full.
            logger.warning("Activity stream: the app lock could not be read, the stream ends", exc_info=True)
            return True
        return False

    # Return the pooled DB connection NOW, before the long-lived SSE stream.
    # ``event_generator`` below does all its work over a dedicated Redis pub/sub
    # connection and never touches ``db`` again, but FastAPI only runs the
    # ``get_db`` teardown (commit + close) when the stream ends — i.e. when the
    # browser tab closes. Without this release, every open dashboard tab pins
    # one main-pool connection (idle-in-transaction) for its entire lifetime,
    # exhausting the pool ("Pool near exhaustion: 25/25"). Committing here hands
    # the connection back immediately; the harmless ``get_db`` teardown still
    # runs at stream end.
    await release_db_connection_before_loop(db)

    async def event_generator() -> AsyncIterator[str]:
        nonlocal web_session
        # Use a dedicated Redis client for pub/sub — pub/sub holds a
        # persistent connection for the lifetime of the subscription,
        # so we must NOT use the shared singleton (which other callers
        # rely on).  The dedicated client is closed in the finally block.
        redis = await redis_pubsub_client()
        # Subscribe to personal channel AND the company broadcast channel
        # so users receive real-time updates from company-managed agent runs.
        personal_channel = f"activity:{user.id}"
        company_channel = "activity:company"
        pubsub = await open_pubsub(redis, personal_channel, company_channel)
        started_at = asyncio.get_running_loop().time()
        session_checked_at = started_at
        try:
            # Heartbeat counter: send a heartbeat every ~30 s to keep
            # the connection alive through load balancers and proxies.
            # We poll Redis every 1 s but only emit a heartbeat comment
            # every 30 iterations with no real message.
            idle_ticks = 0
            while True:
                if await request.is_disconnected():
                    break
                # Bounded stream lifetime: end the response cleanly after
                # SSE_MAX_LIFETIME_SECONDS and let EventSource reconnect
                # (the frontend hook reconnects automatically). An unbounded
                # SSE response never completes, which (a) wedged uvicorn's
                # graceful drain during MAX_REQUESTS worker recycling — the
                # worker stopped accepting connections but waited on open
                # dashboard streams forever, eventually failing the pod's
                # liveness probes — and (b) let a stream authenticated once
                # at connect time outlive its JWT indefinitely. Reconnecting
                # re-validates the token and lands on a live worker.
                if (
                    asyncio.get_running_loop().time() - started_at
                    >= SSE_MAX_LIFETIME_SECONDS
                ):
                    break
                # The session's absolute lifetime and idle timeout:
                # the stream ends with an event the web app signs out on,
                # since an EventSource reads no status of a refused reconnect.
                if asyncio.get_running_loop().time() - session_checked_at >= SESSION_RECHECK_SECONDS:
                    session_checked_at = asyncio.get_running_loop().time()
                    try:
                        web_session = await recheck_session(web_session)
                    except SessionEndedError as exc:
                        yield f"event: {SESSION_EXPIRED_CODE}\ndata: {json.dumps({'reason': exc.reason})}\n\n"
                        break
                    if await _locked():
                        break
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message and message["type"] == "message":
                    yield f"data: {message['data']}\n\n"
                    idle_ticks = 0
                else:
                    idle_ticks += 1
                    # Send heartbeat every ~30 s (30 * 1 s poll).
                    # This keeps the TCP connection alive through reverse
                    # proxies (nginx default timeout = 60 s).
                    if idle_ticks >= 30:
                        yield ": heartbeat\n\n"
                        idle_ticks = 0
                    else:
                        await asyncio.sleep(1)
        finally:
            # The browser leaving cancels this generator, and every await here.
            await close_pubsub(pubsub, redis)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            # Tell proxies not to buffer SSE events
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
        },
    )
