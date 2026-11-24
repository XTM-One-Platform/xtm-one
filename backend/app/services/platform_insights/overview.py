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

"""``get_platform_overview``: what this platform holds today, in one call.

The counts per object type are the Admin Objects union's (``objects_union``
over every arm this deployment offers), so they agree with
``query_platform_objects``; the breakdowns beside them (agents by state,
assignments by trigger, integrations by provider, knowledge-base chunks,
machines online, the files' storage footprint, the sandbox workspaces' footprint
and what their clean-up freed, the sandbox pods per image key) and the platform facts (the
deployed build, the feature flags on, trace retention, the freshness of the
daily aggregation) are what the settings pages show. Outside the administrator
scope every count is of the caller's own objects, and the people and groups of
the platform are left out. The instance identity (Instance ID, display name, URL)
is answered to every caller: GET /platform/config already serves it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.file_attachment import FileAttachment
from app.models.group import Group
from app.models.integration import Integration
from app.models.knowledge_base import KnowledgeBase
from app.models.mcp_server import MCPServer
from app.models.runner import Runner
from app.models.user import User
from app.services.platform_builds import (
    ROLLOUT_DRAINING,
    ROLLOUT_IN_PROGRESS,
    ROLLOUT_ROLLED_BACK,
    is_build,
    release_windows,
    rollout,
    running_build,
)
from app.services.platform_insights.scope import InsightScope

#: Past this, the daily aggregation reads as stale (the dashboard's threshold).
AGGREGATION_STALE_HOURS = 48


async def _grouped(db: AsyncSession, column, *clauses) -> dict[str, int]:
    rows = await db.execute(select(column, func.count()).where(*clauses).group_by(column))
    return {str(key): int(count) for key, count in rows.all()}


#: Owners listed in ``sandbox_workspaces.top_owners`` (administrator scope).
SANDBOX_TOP_OWNERS = 10


async def sandbox_workspaces_overview(db: AsyncSession, own) -> dict[str, Any]:
    """The sandbox workspaces' footprint: count, bytes, by holder, top owners, what the clean-up freed.

    Their objects live in the object store under ``sandbox-workspaces/<id>/``,
    outside ``files.storage_bytes``. *own* narrows every figure to one
    person's workspaces; the top owners and the clean-up totals are an
    administrator view (``own`` is ``None``).
    """
    from app.models.assignment import AssignmentRun
    from app.models.sandbox_workspace import SandboxWorkspace as W
    from app.services.assignment_engine import TERMINAL_RUN_STATUSES

    mine = () if own is None else (W.user_id == own,)
    holder = case(
        (W.conversation_id.is_not(None), "conversations"),
        (AssignmentRun.id.is_(None), "deleted_runs"),
        (AssignmentRun.status.in_(TERMINAL_RUN_STATUSES), "ended_runs"),
        else_="live_runs",
    )
    rows = (
        await db.execute(
            select(
                holder,
                func.count(W.id),
                func.coalesce(func.sum(W.size_bytes), 0),
                func.coalesce(func.sum(W.file_count), 0),
            )
            .select_from(W)
            .outerjoin(AssignmentRun, AssignmentRun.id == W.assignment_run_id)
            .where(*mine)
            .group_by(holder)
        )
    ).all()
    by_holder = {str(name): {"count": int(count), "bytes": int(size), "files": int(files)} for name, count, size, files in rows}
    result: dict[str, Any] = {
        "count": sum(v["count"] for v in by_holder.values()),
        "bytes": sum(v["bytes"] for v in by_holder.values()),
        "files": sum(v["files"] for v in by_holder.values()),
        "by_holder": by_holder,
    }
    if own is None:
        top = (
            await db.execute(
                select(User.email, func.count(W.id), func.coalesce(func.sum(W.size_bytes), 0))
                .select_from(W)
                .outerjoin(User, User.id == W.user_id)
                .group_by(User.email)
                .order_by(func.coalesce(func.sum(W.size_bytes), 0).desc())
                .limit(SANDBOX_TOP_OWNERS)
            )
        ).all()
        result["top_owners"] = [
            {"owner_email": email, "count": int(count), "bytes": int(size)} for email, count, size in top
        ]
        from app.services.sandbox_workspace.gc import freed_totals

        result["freed"] = await freed_totals()
    return result


async def get_platform_overview(db: AsyncSession, scope: InsightScope, args: dict[str, Any]) -> dict[str, Any]:
    from app.api.v1.admin_objects import active_entity_arms, objects_union
    from app.api.v1.platform_settings_helpers import get_platform_settings
    from app.core.feature_flags import resolve_enabled_feature_flags
    from app.core.module_versions import registered_modules
    from app.services.branding import get_cached_branding

    now = datetime.now(UTC)
    own = None if scope.platform else scope.user.id

    def owned(model):
        return () if own is None else (model.user_id == own,)

    sub = objects_union(list(active_entity_arms().values()), now=now)
    by_type = select(sub.c.object_type, func.count()).group_by(sub.c.object_type)
    if own is not None:
        by_type = by_type.where(sub.c.user_id == str(own))
    objects = {str(kind): int(count) for kind, count in (await db.execute(by_type)).all()}

    agent_state = case(
        (Agent.enabled.is_(False), "disabled"),
        (Agent.disable_chat.is_(True), "enabled, chat off"),
        else_="enabled",
    )
    assignment_state = case((Assignment.enabled.is_(True), "enabled"), else_="paused")
    kb = (
        await db.execute(
            select(
                func.count(KnowledgeBase.id),
                func.coalesce(func.sum(KnowledgeBase.document_count), 0),
                func.coalesce(func.sum(KnowledgeBase.chunk_count), 0),
            ).where(*owned(KnowledgeBase))
        )
    ).one()
    files = (
        await db.execute(
            select(func.count(FileAttachment.id), func.coalesce(func.sum(FileAttachment.size), 0)).where(
                *owned(FileAttachment)
            )
        )
    ).one()
    # Count and bytes per file source: a storage spike is attributable in one call.
    by_source = await db.execute(
        select(FileAttachment.source, func.count(FileAttachment.id), func.coalesce(func.sum(FileAttachment.size), 0))
        .where(*owned(FileAttachment))
        .group_by(FileAttachment.source)
    )
    files_by_source = {
        str(source or "unknown"): {"count": int(count), "storage_bytes": int(size)}
        for source, count, size in by_source.all()
    }
    platform_settings = await get_platform_settings(db)
    last_aggregation = platform_settings.last_aggregation_at
    if last_aggregation is not None and last_aggregation.tzinfo is None:
        last_aggregation = last_aggregation.replace(tzinfo=UTC)
    version, git_sha = running_build()
    windows = await release_windows(db)
    deployment = rollout(windows, (version, git_sha), now=now)

    result: dict[str, Any] = {
        **scope.header(),
        "as_of": now.isoformat(),
        # Who this platform is: the Instance ID of Settings > Parameters (PLATFORM_ID when forced),
        # the resolved display name and the public URL, so an agent can name its own instance.
        # The id and the name are already public on GET /platform/config.
        "instance": {
            "instance_id": str(platform_settings.id),
            "platform_name": get_cached_branding().name,
            "url": settings.frontend_url,
        },
        "objects_by_type": objects,
        "agents_by_state": await _grouped(db, agent_state, Agent.is_sandbox.is_(False), *owned(Agent)),
        "assignments_by_trigger_type": await _grouped(db, Assignment.trigger_type, *owned(Assignment)),
        "assignments_by_state": await _grouped(db, assignment_state, *owned(Assignment)),
        "integrations_by_provider": await _grouped(db, Integration.provider, *owned(Integration)),
        # The version each connected module registered, the string the CTEM Command Center
        # chips show: the source of a build baseline, never a module's own live read (#888).
        # Every caller: GET /platform/registrations already serves it.
        "registered_modules": await registered_modules(db),
        "knowledge_bases": {"count": int(kb[0]), "documents": int(kb[1]), "chunks": int(kb[2])},
        "mcp_servers": await _grouped(
            db, case((MCPServer.enabled.is_(True), "enabled"), else_="disabled"), *owned(MCPServer)
        ),
        "files": {"count": int(files[0]), "storage_bytes": int(files[1])},
        "files_by_source": files_by_source,
        "sandbox_workspaces": await sandbox_workspaces_overview(db, own),
        "deployed_build": {
            "version": version,
            "git_sha": git_sha or None,
            "first_seen_at": next(
                (w.first_seen_at.isoformat() for w in reversed(windows) if is_build(w, version, git_sha)), None
            ),
            "rollout": deployment.payload(),
        },
        "feature_flags_on": resolve_enabled_feature_flags(),
        "trace_retention_days": platform_settings.trace_retention_days,
        "aggregation": {
            "last_aggregation_at": last_aggregation.isoformat() if last_aggregation else None,
            "stale": last_aggregation is None
            or (now - last_aggregation).total_seconds() > AGGREGATION_STALE_HOURS * 3600,
        },
    }
    if settings.runners_enabled:
        from app.services.runners import runner_status_expression

        result["machines_by_status"] = await _grouped(db, runner_status_expression(now), *owned(Runner))
    if scope.platform:
        from app.services._sandbox_selector import sandbox_pool_state

        # The OpenSandbox pods per image key (ready, creating, pulling the image): a cold or
        # exhausted Pool shows here before a create answers HTTP 504. ``None``: not read.
        result["sandbox_pods_by_image_key"] = await sandbox_pool_state()
        users = (
            await db.execute(select(func.count(User.id), func.count(User.id).filter(User.is_active.is_(True))))
        ).one()
        result["users"] = {"total": int(users[0]), "active": int(users[1])}
        result["groups"] = int(await db.scalar(select(func.count(Group.id))) or 0)
    else:
        result["groups"] = len(scope.group_ids)
        result["note"] = "Counts are of your own objects. People and groups of the platform are an administrator view."
    caveats: list[str] = []
    if result["aggregation"]["stale"]:
        caveats.append(
            "The daily analytics aggregation has not run for more than 48 hours: say the analytics figures "
            "for older days may still move."
        )
    if deployment.state in (ROLLOUT_IN_PROGRESS, ROLLOUT_DRAINING, ROLLOUT_ROLLED_BACK):
        caveats.append(deployment.words())
    if caveats:
        result["caveats"] = caveats
    return result
