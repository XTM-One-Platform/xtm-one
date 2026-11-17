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

"""Self-service standing approval preferences API.

Users view and revoke their own "Yes, always" approvals here. There is
deliberately no cross-user variant: an administrator who needs to inspect or
revoke someone else's preference impersonates that user, which routes through
this same endpoint and is recorded against both identities in the audit trail.

A preference belonging to another user is reported as 404 rather than 403, so
the API does not confirm the existence of another user's preferences.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.database import get_db
from app.models.tool_approval import ToolApprovalPreference
from app.models.user import User
from app.services.audit import log_audit
from app.services.hitl_whitelist import install_default_asks
from app.services.tool_approval_service import (
    grant_exception,
    list_preferences,
    list_recently_invalidated,
    revoke_exception,
)
from app.services.tool_approval_usage import approvals_in_use, load_approval_usage

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("")
async def list_my_tool_approvals(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The caller's own standing approvals, active and recently invalidated.

    Recently-invalidated ones are included deliberately. A tool that starts
    prompting again after the user approved it "always" is confusing, and this
    is the only place that explains why: the tool changed, so their consent no
    longer described it.
    """
    preferences = await list_preferences(db, user.id)
    invalidated = await list_recently_invalidated(db, user.id)

    # The platform kill switch rides along so the profile can explain why
    # nothing pauses even though the user's own mode is off. Read
    # defensively: a missing settings row means the default (approvals on).
    from app.models.platform_settings import PlatformSettings

    platform_enabled = await db.scalar(
        select(PlatformSettings.tool_approvals_enabled)
    )
    # What an agent with no setting of its own does, for the agent pages.
    install_default = await db.scalar(
        select(PlatformSettings.approve_tool_calls_default)
    )
    # Whether any tool call can pause on this platform: the screens show an
    # approval state only then, since a tool's place on the whitelist says
    # nothing while no agent asks.
    in_use = await approvals_in_use(db)

    return {
        "approve_all_tools": bool(user.approve_all_tools),
        "tool_approvals_enabled": platform_enabled is not False,
        "approve_tool_calls_default": install_default_asks(install_default),
        "approvals_in_use": in_use,
        "preferences": [
            {
                "id": str(p.id),
                "tool_key": p.tool_key,
                "tool_source": p.tool_source,
                "granted_at": p.granted_at.isoformat() if p.granted_at else None,
                "granted_by_admin_id": (
                    str(p.granted_by_admin_id) if p.granted_by_admin_id else None
                ),
            }
            for p in preferences
        ],
        "recently_invalidated": [
            {
                "id": str(p.id),
                "tool_key": p.tool_key,
                "tool_source": p.tool_source,
                "revoked_at": p.revoked_at.isoformat() if p.revoked_at else None,
                "reason": p.revoked_reason,
            }
            for p in invalidated
        ],
    }


@router.get("/usage")
async def tool_approval_usage(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Whether approvals are in use, and which agents ask.

    Administrators only: it names every enabled agent that asks, private ones
    included, for Settings > Access and security > Tool approvals.
    """
    usage = await load_approval_usage(db)
    return usage.to_payload()


class ApprovalModeRequest(BaseModel):
    approve_all_tools: bool


@router.get("/holders")
async def count_approval_holders(
    tool_key: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """How many people hold a standing approval for one tool.

    A count, never identities: what the caller is about to do is change a tool,
    and the question that governs it is "whose consent does this withdraw?" —
    not whose. Editing a tool's parameters changes its schema fingerprint, so
    every "always allow" for it stops describing it and the gate asks again.
    That was happening silently, and an unquantified warning is easy to skim
    past in a way that "3 people will be asked again" is not.

    Any authenticated caller, because whoever may edit a shared custom tool may
    not be an administrator — its owner qualifies. The aggregate is the whole
    disclosure, and it is one a person is only shown about a tool they were
    already looking at.

    Excludes the caller's own, which the profile and the tool's row already
    report, so the number answers "besides me" — the part that is somebody
    else's surprise rather than the caller's own.

    Counts only approvals that still describe the tool. Invalidation is lazy —
    the gate revokes a stale preference when it next sees one — so a row whose
    fingerprint stopped matching an earlier edit is still un-revoked, and
    counting it would promise to surprise people this change cannot reach:
    they are being re-asked already. When the shape cannot be resolved at all
    the count falls back to every live row, which over-reports rather than
    under-reports.
    """
    key = tool_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="tool_key cannot be empty")

    from app.tools.approval import fingerprint

    schema, _source, _entity_id = await _resolve_schema(
        db, key, user_id=user.id, is_admin=user.role == "admin"
    )

    conditions = [
        ToolApprovalPreference.tool_key == key,
        ToolApprovalPreference.revoked_at.is_(None),
        ToolApprovalPreference.user_id != user.id,
    ]
    if schema is not None:
        conditions.append(
            ToolApprovalPreference.schema_fingerprint == fingerprint(schema)
        )

    holders = await db.scalar(
        select(func.count()).select_from(ToolApprovalPreference).where(*conditions)
    )
    return {"tool_key": key, "others": int(holders or 0)}


@router.put("/mode")
async def set_my_approval_mode(
    body: ApprovalModeRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Turn the caller's allow-all mode on or off.

    The wholesale form of "Yes, always": with it on, none of this user's runs
    or chats pause for tool approval, and every call is recorded with an
    explicit ``auto_run_user_allows_all`` outcome. Individual standing
    approvals are deliberately kept — switching back off restores exactly the
    per-tool posture the user had before.
    """
    changed = bool(user.approve_all_tools) != body.approve_all_tools
    user.approve_all_tools = body.approve_all_tools
    if changed:
        await log_audit(
            db,
            user=user,
            action="configure",
            entity_type="tool_approval_preference",
            entity_name="Allow all tools",
            summary=(
                "Enabled allow-all mode: their tool calls run without asking"
                if body.approve_all_tools
                else "Disabled allow-all mode: tools ask for approval again"
            ),
            details={
                "approve_all_tools": body.approve_all_tools,
                "impersonated_by": getattr(user, "impersonated_by", None),
            },
            request=request,
        )
    await db.commit()
    return {"approve_all_tools": bool(user.approve_all_tools)}


class GrantRequest(BaseModel):
    tool_key: str


async def _resolve_schema(
    db: AsyncSession, tool_key: str, *, user_id, is_admin: bool
) -> tuple[dict | None, str, str | None]:
    """The tool's input schema, source and entity id, or ``(None, "", None)``.

    The entity id is the one the tool registers under at run time
    (``ToolDefinition.entity_id``) when the gate needs it to tell same-named
    tools apart: a custom tool's id, an MCP server's id. ``None`` for a
    product tool, which the gate matches by name.

    A standing approval is fingerprinted against the schema so that changing a
    tool's parameters invalidates the consent (FR-019a). Granting one without a
    schema would therefore promise "this will not ask again" and then ask again
    the first time the tool actually runs — the worst outcome for a control
    whose whole promise is that sentence. So only the two sources whose shape we
    can be fingerprinted can be granted from a list — which is every tool with a
    live definition: a builtin, one of this deployment's custom tools, one an
    integration provider or a channel bot contributes, or one an MCP server
    discovered. Only a name harvested from run history that nothing connected
    still claims has no schema to compare against, and that is refused with the
    reason.

    The provider and bot builders were long assumed to have no definition until
    connected. They do; it is the ``ProviderTool`` shape the catalogue maps them
    into that drops the schema. Introspection is by dummy credential, touching
    no connection, and building every provider is affordable here because a
    grant is a rare deliberate act — unlike the gate this feeds.
    """
    from app.services.builtin_tool_catalog import build_builtin_tool_catalog

    for defs in build_builtin_tool_catalog().values():
        for d in defs:
            if d.get("name") == tool_key:
                return d.get("input_schema"), "builtin", None

    from app.api.v1.helpers import get_user_group_ids, user_visible_query
    from app.models.custom_tool import CustomTool

    # Scoped to what this caller may see. Custom tool names are not unique
    # across owners, so an unscoped lookup resolved somebody else's private
    # tool: it fingerprinted the wrong same-named tool, and it let the holders
    # count report an aggregate about a tool the caller cannot reach.
    group_ids = await get_user_group_ids(db, user_id)
    row = (
        await db.execute(
            user_visible_query(CustomTool, user_id, group_ids, is_admin=is_admin)
            .where(CustomTool.name == tool_key)
            .with_only_columns(CustomTool.id, CustomTool.input_schema)
            .limit(1)
        )
    ).first()
    if row is not None and row.input_schema is not None:
        # ``custom:<id>``, the source the tool registers under: the gate
        # covers a custom tool only by an approval granted to that very tool.
        return row.input_schema, f"custom:{row.id}", str(row.id)

    from app.api.v1.integration_catalog import build_dummy_token
    from app.services.integration_tools import CHANNEL_BOT_TOOLS, PROVIDER_TOOLS
    from app.tools.base import CallableToolExecutor

    for registry, label in (
        (PROVIDER_TOOLS, "integration"),
        (CHANNEL_BOT_TOOLS, "channel_bot"),
    ):
        for provider, builder in registry.items():
            try:
                defs = builder(
                    build_dummy_token(provider), CallableToolExecutor(), refresh_fn=lambda: None
                )
            except Exception:  # pragma: no cover - one that cannot introspect
                logger.debug(
                    "Could not introspect %s while resolving %s",
                    provider,
                    tool_key,
                )
                continue
            for d in defs:
                if getattr(d, "name", None) == tool_key:
                    return (
                        getattr(d, "input_schema", None),
                        f"{label}:{provider}",
                        None,
                    )

    # Scoped to servers this caller can see: a name they cannot reach is not one
    # they can meaningfully approve.
    from app.api.v1.helpers import get_user_group_ids, user_visible_query
    from app.models.mcp_server import MCPServer

    group_ids = await get_user_group_ids(db, user_id)
    servers = (
        await db.execute(
            user_visible_query(MCPServer, user_id, group_ids, is_admin=is_admin)
        )
    ).scalars().all()
    for server in servers:
        for tool in server.discovered_tools or []:
            if isinstance(tool, dict) and tool.get("name") == tool_key:
                # The server's id, not only its name: the name is the
                # server owner's to choose, so the gate covers an MCP tool
                # only by an approval granted on its own server.
                return tool.get("input_schema"), f"mcp:{server.name}", str(server.id)

    return None, "", None


@router.post("", status_code=201)
async def grant_my_tool_approval(
    body: GrantRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Grant the caller's standing approval for one tool, outside a run.

    The same decision as answering "Yes, always" inside a pause, reached from
    the tools list instead (FR-045) — same scope, same fingerprint, same
    revocation path, and it lands in the same list on the profile. Nothing
    about the grant is new here except where the user was standing when they
    made it.
    """
    tool_key = body.tool_key.strip()
    if not tool_key:
        raise HTTPException(status_code=400, detail="Name a tool.")

    schema, source, entity_id = await _resolve_schema(
        db, tool_key, user_id=user.id, is_admin=user.role == "admin"
    )
    if not source:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{tool_key} has no definition to compare against. It has run "
                "here, but nothing connected still claims it — a server since "
                "removed, or a provider disconnected — and a standing approval "
                "has to be withdrawn when a tool's parameters change. Approve "
                "it next time it asks, or have an administrator whitelist it."
            ),
        )

    from app.tools.approval import fingerprint

    preference = await grant_exception(
        db,
        user_id=user.id,
        tool_key=tool_key,
        tool_source=source,
        tool_entity_id=entity_id,
        schema_fingerprint=fingerprint(schema),
    )
    await log_audit(
        db,
        user=user,
        action="configure",
        entity_type="tool_approval_preference",
        entity_name=tool_key,
        summary=f"Granted a standing approval for {tool_key} from the tools list",
        details={
            "tool_key": tool_key,
            "tool_source": source,
            "impersonated_by": getattr(user, "impersonated_by", None),
        },
        request=request,
    )
    await db.commit()
    return {"id": str(preference.id), "tool_key": tool_key, "tool_source": source}


@router.delete("/{preference_id}", status_code=204)
async def revoke_my_tool_approval(
    preference_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke one of the caller's own approvals; the tool prompts them again."""
    from app.api.v1.helpers import parse_path_id

    parsed_id = parse_path_id(preference_id, not_found_detail="Preference not found")

    preference = await revoke_exception(db, preference_id=parsed_id, user_id=user.id)
    if preference is None:
        # 404 whether it never existed or belongs to somebody else — a 403 would
        # confirm that another user has approved this tool.
        raise HTTPException(status_code=404, detail="Preference not found")

    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="tool_approval_preference",
        entity_id=preference.id,
        entity_name=preference.tool_key,
        summary=f"Revoked standing approval for '{preference.tool_key}'",
        details={
            "tool_key": preference.tool_key,
            # Present when an administrator did this while impersonating, so the
            # trail names who actually acted rather than only whose preference
            # it was.
            "impersonated_by": getattr(user, "impersonated_by", None),
        },
        request=request,
    )
    await db.commit()
