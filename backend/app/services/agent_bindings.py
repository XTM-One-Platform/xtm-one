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

"""What an agent may bind, and what its bindings become when its tier changes.

The rules every write surface of an agent applies - the agents REST API and
the agent-builder tool alike, so the two can never drift:

- handover targets: :func:`sanitize_handover_config` drops invalid ids and
  keeps the type / condition maps aligned, :func:`validate_handover_conditions`
  refuses ambiguous mandatory-transfer routing;
- bindings: :func:`validate_personal_agent_bindings` (a personal agent binds
  only what its owner can see) and :func:`validate_company_managed_bindings`
  (a company-managed agent binds only company-managed resources);
- channels: :func:`validate_channel_config` and
  :func:`derive_available_channel_types`;
- the company-managed toggle: :func:`strip_personal_bindings` when an agent
  becomes company-managed, :func:`revert_company_managed_to_private` when it
  goes back, and :func:`retain_agent_bindings`, which a duplicate also uses.

Refusals are ``HTTPException`` s; the agent-builder tool turns their ``detail``
into the message it returns to the model.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.binding_access import (
    WHITELIST_BYPASS_REFUSED_DETAIL,
    ids_matching,
    integration_binding_ids,
)
from app.api.v1.helpers import (
    bindable_knowledge_base_clause,
    parse_uuid_list,
    user_visible_filter,
)
from app.channels.base import CHANNEL_TO_BOT_PROVIDER
from app.core.utils import safe_uuid
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.assignment import Assignment
from app.models.channel_bot import ChannelBot
from app.models.custom_tool import CustomTool
from app.models.integration import Integration
from app.models.knowledge_base import KnowledgeBase
from app.models.mcp_server import MCPServer
from app.models.prompt import Prompt as PromptModel
from app.services.handover_types import handover_types_after_removal, is_legacy_handover_map


def validate_handover_conditions(
    handover_agent_ids: list[str] | None,
    handover_types: dict[str, str] | None,
    handover_conditions: dict[str, str] | None,
) -> None:
    """Validate handover condition rules.

    - When multiple mandatory_transfer targets exist, at most one may omit a condition.
    - No two mandatory_transfer targets may share the same condition text.
    """
    if not handover_agent_ids or not handover_types:
        return
    mandatory_ids = [
        aid for aid in handover_agent_ids
        if handover_types.get(aid) == "mandatory_transfer"
    ]
    if len(mandatory_ids) <= 1:
        return

    conditions = handover_conditions or {}
    # Check: at most one target without a condition (fallback)
    without_condition = [aid for aid in mandatory_ids if not conditions.get(aid, "").strip()]
    if len(without_condition) > 1:
        raise HTTPException(
            status_code=422,
            detail="When multiple mandatory transfers are configured, at most one target may omit a routing condition (fallback).",
        )

    # Check: no duplicate conditions
    seen: dict[str, str] = {}  # lowered text → agent id
    for aid in mandatory_ids:
        cond = conditions.get(aid, "").strip().lower()
        if not cond:
            continue
        if cond in seen:
            raise HTTPException(
                status_code=422,
                detail="Each mandatory transfer target must have a unique routing condition.",
            )
        seen[cond] = aid


def sanitize_handover_config(
    handover_agent_ids: list[str] | None,
    handover_types: dict[str, str] | None,
    handover_conditions: dict[str, str] | None,
    *,
    types_before: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """Drop invalid handover targets and keep maps aligned with remaining IDs.

    When aligning empties a map that holds a type while the agent already ran
    with an explicit map (*types_before*, the stored one), the targets left
    keep running as delegate instead of each getting every handover type
    (``handover_types_after_removal``). A new or legacy agent is aligned as it
    always was: a type written only for agents that are no target is dropped.
    """
    cleaned_ids: list[str] = []
    seen: set[str] = set()
    for raw_id in handover_agent_ids or []:
        if raw_id is None:
            continue
        parsed = safe_uuid(str(raw_id))
        if not parsed:
            continue
        aid = str(parsed)
        if aid in seen:
            continue
        seen.add(aid)
        cleaned_ids.append(aid)

    allowed = set(cleaned_ids)

    def _normalize_handover_map(
        handover_map: dict[str, str] | None,
    ) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for raw_id, value in (handover_map or {}).items():
            if raw_id is None:
                continue
            parsed = safe_uuid(str(raw_id))
            if not parsed:
                continue
            aid = str(parsed)
            if aid not in allowed:
                continue
            normalized[aid] = value
        return normalized

    cleaned_types = _normalize_handover_map(handover_types)
    if not is_legacy_handover_map(types_before):
        cleaned_types = handover_types_after_removal(cleaned_ids, handover_types, cleaned_types)
    cleaned_conditions = _normalize_handover_map(handover_conditions)
    return cleaned_ids, cleaned_types, cleaned_conditions


async def revert_company_managed_to_private(
    db: AsyncSession, agent_id: uuid.UUID, owner_id: uuid.UUID
) -> None:
    """Flip a former company-managed agent's assignments + runs back to private.

    Visibility (``is_company_managed``) is cleared on every assignment and run,
    and the run-as override is dropped (it is company-managed only). The owner
    is backfilled onto assignments and onto *ownerless* runs only — existing
    non-null run identities are preserved because ``AssignmentRun.user_id`` is
    the execution identity (triggered-by / run-as / owner), so rewriting it
    would corrupt past attribution for run-as and manual/follow-up runs.
    """
    from app.models.assignment import AssignmentRun

    await db.execute(
        Assignment.__table__.update()
        .where(Assignment.agent_id == agent_id)
        .values(is_company_managed=False, user_id=owner_id, run_as_user_id=None)
    )
    # Visibility flips for every run regardless of who executed it.
    await db.execute(
        AssignmentRun.__table__.update()
        .where(AssignmentRun.agent_id == agent_id)
        .values(is_company_managed=False)
    )
    # Owner is only backfilled onto legacy ownerless runs; run-as / manual /
    # follow-up execution identities are left intact.
    await db.execute(
        AssignmentRun.__table__.update()
        .where(AssignmentRun.agent_id == agent_id)
        .where(AssignmentRun.user_id.is_(None))
        .values(user_id=owner_id)
    )


def canonical_id(value: object) -> str | None:
    parsed = safe_uuid(str(value)) if value is not None else None
    return str(parsed) if parsed else None


#: Agent binding columns :func:`retain_agent_bindings` filters, with the model
#: that owns the ids.
BINDING_MODELS: dict[str, type] = {
    "mcp_server_ids": MCPServer,
    "knowledge_base_ids": KnowledgeBase,
    "integration_bindings": Integration,
    "handover_agent_ids": Agent,
    "custom_tool_ids": CustomTool,
    "skill_ids": AgentSkill,
    "prompt_ids": PromptModel,
    "work_item_owner_agent_ids": Agent,
}


def filter_integration_bindings(
    bindings: dict, allowed: set[str]
) -> tuple[dict[str, list], int]:
    """``bindings`` (provider -> ids) keeping only ``allowed`` ids, and how
    many ids were dropped. A provider left with no id is removed."""
    remaining: dict[str, list] = {}
    for provider, ids in bindings.items():
        kept_ids = [
            iid
            for iid in (ids if isinstance(ids, list) else [ids])
            if canonical_id(iid) in allowed
        ]
        if kept_ids:
            remaining[provider] = kept_ids
    return remaining, len(integration_binding_ids(bindings)) - len(
        integration_binding_ids(remaining)
    )


def retain_agent_bindings(agent: Agent, keep: dict[str, set[str]]) -> dict[str, int]:
    """Keep only the bound ids listed in ``keep`` (per column), in place.

    Columns absent from ``keep`` are left alone. The maps keyed by those ids
    (per-server tool scopes, skill load modes, handover types / conditions, KB
    write permissions, integration scopes) follow. Returns how many ids each
    filtered column lost.
    """
    dropped: dict[str, int] = {}

    def _filter(field: str) -> set[str]:
        ids = list(getattr(agent, field) or [])
        kept = [i for i in ids if canonical_id(i) in keep[field]]
        dropped[field] = len(ids) - len(kept)
        setattr(agent, field, kept or None)
        return {canonical_id(i) for i in kept}

    if "mcp_server_ids" in keep and agent.mcp_server_ids:
        kept_servers = _filter("mcp_server_ids")
        if agent.mcp_allowed_tools:
            agent.mcp_allowed_tools = {
                k: v for k, v in agent.mcp_allowed_tools.items()
                if canonical_id(k) in kept_servers
            } or None

    if "knowledge_base_ids" in keep and agent.knowledge_base_ids:
        _filter("knowledge_base_ids")
        agent.prune_orphaned_knowledge_base_permissions()

    if "integration_bindings" in keep and agent.integration_bindings:
        remaining, dropped["integration_bindings"] = filter_integration_bindings(
            agent.integration_bindings, keep["integration_bindings"]
        )
        agent.integration_bindings = remaining or None
        agent.prune_orphaned_integration_scopes()

    if "handover_agent_ids" in keep and agent.handover_agent_ids:
        kept_agents = _filter("handover_agent_ids")
        if agent.handover_types and agent.handover_agent_ids:
            agent.handover_types = handover_types_after_removal(
                agent.handover_agent_ids,
                agent.handover_types,
                {
                    k: v for k, v in agent.handover_types.items()
                    if canonical_id(k) in kept_agents
                },
            )
            agent.handover_conditions = {
                k: v for k, v in (agent.handover_conditions or {}).items()
                if canonical_id(k) in kept_agents
            } or None
        else:
            agent.handover_types = None
            agent.handover_conditions = None

    if "custom_tool_ids" in keep and agent.custom_tool_ids:
        _filter("custom_tool_ids")

    if "skill_ids" in keep and agent.skill_ids:
        kept_skills = _filter("skill_ids")
        if agent.skill_load_modes:
            agent.skill_load_modes = {
                k: v for k, v in agent.skill_load_modes.items()
                if canonical_id(k) in kept_skills
            } or None

    if "prompt_ids" in keep and agent.prompt_ids:
        _filter("prompt_ids")

    if "work_item_owner_agent_ids" in keep and agent.work_item_owner_agent_ids:
        _filter("work_item_owner_agent_ids")

    if "trace_supervision_target_agent_ids" in keep and agent.trace_supervision_target_agent_ids:
        _filter("trace_supervision_target_agent_ids")

    return dropped


#: How a dropped binding is named in a copy's warnings (singular form).
COPY_WARNING_LABELS: dict[str, str] = {
    "mcp_server_ids": "MCP server",
    "knowledge_base_ids": "knowledge base",
    "integration_bindings": "integration",
    "handover_agent_ids": "handover agent",
    "custom_tool_ids": "custom tool",
    "skill_ids": "skill",
    "prompt_ids": "prompt",
    "work_item_owner_agent_ids": "work-item owner agent",
    "trace_supervision_target_agent_ids": "trace supervision target agent",
    "channel_config": "channel setting",
    "assignment_tool_sources": "assignment MCP server",
    "assignment_integration_bindings": "assignment integration binding",
    "assignment_channel_config": "assignment channel setting",
    "assignment_trigger_integration": "assignment trigger integration",
}


def copy_warnings(dropped: dict[str, int]) -> list[str]:
    """One sentence per kind of binding a copy left out - counts only, never an
    id or a name, so nothing says whether a resource exists. Shared by the REST
    duplicate and the agent-builder ``clone_agent`` tool."""
    warnings: list[str] = []
    for field, label in COPY_WARNING_LABELS.items():
        count = dropped.get(field, 0)
        if count:
            warnings.append(
                f"{count} {label}{'' if count == 1 else 's'} of the original agent "
                f"{'was' if count == 1 else 'were'} not copied: you cannot use "
                f"{'it' if count == 1 else 'them'}."
            )
    return warnings


async def retain_copier_channels(
    db: AsyncSession,
    channel_config: dict | None,
    *,
    user,
    user_group_ids: list[str] | None,
) -> tuple[dict | None, int]:
    """The entries of ``channel_config`` a copier could set on a personal agent.

    Each entry naming a channel bot must name a bot the copier can see, and
    pass :func:`validate_channel_config`, the check agent create / update
    apply; an entry that fails either is dropped rather than failing the whole
    copy. An entry naming no bot binds nothing and is kept as is. Shared by the
    REST duplicate and the agent-builder ``clone_agent`` tool.
    """
    from app.api.v1 import binding_access

    if not channel_config or not isinstance(channel_config, dict):
        return channel_config, 0
    hidden_bots = await binding_access.refused_channel_bots(
        db, user=user, channel_config=channel_config, user_group_ids=user_group_ids
    )
    kept: dict = {}
    for ch_type, ch_cfg in channel_config.items():
        if not isinstance(ch_cfg, dict):
            continue
        if not ch_cfg.get("channel_bot_id"):
            kept[ch_type] = ch_cfg
            continue
        if binding_access.channel_bot_key(ch_cfg["channel_bot_id"]) in hidden_bots:
            continue
        try:
            await validate_channel_config(
                db,
                channel_config={ch_type: ch_cfg},
                is_company_managed=False,
                is_admin=user.is_admin,
            )
        except HTTPException:
            continue
        kept[ch_type] = ch_cfg
    return kept, len(channel_config) - len(kept)


async def strip_personal_bindings(db: AsyncSession, agent: Agent) -> None:
    """Auto-remove non-company-managed bindings from an agent being made company-managed.

    Strips personal MCP servers, KBs, integrations, and handover agents.
    Also cleans up related allowed-tools / element-bindings dicts for removed entries.
    Custom tools are exempt — personal tools can remain on any agent.
    """
    keep: dict[str, set[str]] = {}
    for field in ("mcp_server_ids", "knowledge_base_ids", "integration_bindings", "handover_agent_ids"):
        model = BINDING_MODELS[field]
        raw = getattr(agent, field)
        ids = integration_binding_ids(raw) if field == "integration_bindings" else raw
        # access_restricted: bypass - an admin classifying bindings by tier (company-managed or personal), not by who may use them.
        keep[field] = await ids_matching(db, model, ids, model.is_company_managed.is_(True))
    retain_agent_bindings(agent, keep)


async def refuse_unbindable_knowledge_bases(db: AsyncSession, uuids: list[uuid.UUID]) -> None:
    """Refuse binding a workspace's own or an internal knowledge base.

    Answered like an id the caller cannot see, admins included: such a
    knowledge base is not a collection anyone binds, so there is nothing to
    explain beyond "not available".
    """
    if await ids_matching(db, KnowledgeBase, uuids, not_(bindable_knowledge_base_clause())):
        raise HTTPException(
            status_code=400,
            detail="One or more knowledge bases were not found or are not accessible.",
        )


async def validate_personal_agent_bindings(
    db: AsyncSession,
    user_id: uuid.UUID,
    user_group_ids: list[str] | None,
    *,
    is_admin: bool = False,
    mcp_server_ids: list[str] | None,
    knowledge_base_ids: list[str] | None,
    integration_bindings: dict[str, list[str]] | None,
    handover_agent_ids: list[str] | None,
    custom_tool_ids: list[str] | None,
    skill_ids: list[str] | None,
    prompt_ids: list[str] | None,
) -> None:
    """Ensure all bindings on a personal agent are visible to the requesting user.

    Prevents an attacker from binding another user's private integration (and
    thus hijacking their OAuth token/API key), accessing their private KBs,
    MCP servers, custom tools, handover agents, skills, or prompts.

    Admins bypass all visibility checks (is_admin=True) to preserve existing
    behaviour where admins can configure any resource on their own agents.
    One bulk query per binding type — no N+1 round-trips.
    """

    async def _check(model, uuids: list[uuid.UUID], label: str) -> None:
        visible = await ids_matching(
            db,
            model,
            uuids,
            user_visible_filter(model, user_id, user_group_ids, is_admin=is_admin),
        )
        if visible != {str(u) for u in uuids}:
            raise HTTPException(
                status_code=400,
                detail=f"One or more {label} were not found or are not accessible.",
            )

    if knowledge_base_ids:
        kb_uuids = parse_uuid_list(knowledge_base_ids, "knowledge base")
        await _check(KnowledgeBase, kb_uuids, "knowledge bases")
        await refuse_unbindable_knowledge_bases(db, kb_uuids)

    if mcp_server_ids:
        await _check(MCPServer, parse_uuid_list(mcp_server_ids, "MCP server"), "MCP servers")

    if integration_bindings:
        integ_ids = [iid for ids in integration_bindings.values() for iid in (ids or [])]
        if integ_uuids := parse_uuid_list(integ_ids, "integration"):
            await _check(Integration, integ_uuids, "integrations")

    if handover_agent_ids:
        await _check(Agent, parse_uuid_list(handover_agent_ids, "handover agent"), "handover agents")

    if custom_tool_ids:
        await _check(CustomTool, parse_uuid_list(custom_tool_ids, "custom tool"), "custom tools")

    if skill_ids:
        await _check(AgentSkill, parse_uuid_list(skill_ids, "skill"), "skills")

    if prompt_ids:
        await _check(PromptModel, parse_uuid_list(prompt_ids, "prompt"), "prompts")


async def validate_company_managed_bindings(
    db: AsyncSession,
    *,
    mcp_server_ids: list[str] | None,
    knowledge_base_ids: list[str] | None,
    integration_bindings: dict[str, list[str]] | None,
    handover_agent_ids: list[str] | None,
) -> None:
    """Ensure all bound MCP servers, KBs, integrations, and handover agents are company-managed.

    Called when creating or updating a company-managed agent.
    Custom tools are exempt — personal tools can be added to any agent.
    Non-existent IDs are silently ignored here; they will fail at runtime.
    """

    async def _check(model, uuids: list[uuid.UUID], _resource_label: str, plural: str) -> None:
        result = await db.execute(
            select(model.id, model.name, model.is_company_managed).where(model.id.in_(uuids))
        )
        for row in result.all():
            if not row.is_company_managed:
                raise HTTPException(
                    status_code=400,
                    detail=f'Company-managed agents can only use company-managed {plural}. "{row.name}" is not company-managed.',
                )

    if mcp_server_ids:
        await _check(MCPServer, parse_uuid_list(mcp_server_ids, "MCP server"), "MCP server", "MCP servers")

    if knowledge_base_ids:
        kb_uuids = parse_uuid_list(knowledge_base_ids, "knowledge base")
        await _check(KnowledgeBase, kb_uuids, "knowledge base", "knowledge bases")
        await refuse_unbindable_knowledge_bases(db, kb_uuids)

    if integration_bindings:
        integ_ids = [iid for ids in integration_bindings.values() for iid in ids if iid]
        if integ_uuids := parse_uuid_list(integ_ids, "integration"):
            await _check(Integration, integ_uuids, "integration", "integrations")

    if handover_agent_ids:
        # Handover IDs may contain legacy/non-UUID sentinels (for example
        # "__general_assistant__"). Reuse the handover sanitizer so company-
        # managed validation only checks surviving UUID targets.
        cleaned_handover_ids, _, _ = sanitize_handover_config(
            handover_agent_ids,
            None,
            None,
        )
        if cleaned_handover_ids:
            await _check(
                Agent,
                parse_uuid_list(cleaned_handover_ids, "handover agent"),
                "agent",
                "agents",
            )


async def validate_channel_config(
    db: AsyncSession,
    *,
    channel_config: dict[str, dict] | None,
    is_company_managed: bool,
    is_admin: bool = False,
) -> None:
    """Validate channel_config entries.

    For each channel entry:
      - The channel_bot_id must point to a connected channel bot.
      - ``bypass_whitelist`` is admin-only — allows the agent to
        receive/publish on all channels regardless of bot whitelists.
      - Admins bypass all whitelist checks (can select any channel).
      - For non-admin personal agents: receive_channels must be a subset
        of the bot's allowed_receive_channels, and publish_channels must
        be a subset of allowed_publish_channels.  If no whitelist exists
        the bot cannot be used by personal agents.
    """
    if not channel_config:
        return

    valid_channels = set(CHANNEL_TO_BOT_PROVIDER.keys())
    for ch_type, ch_cfg in channel_config.items():
        if ch_type not in valid_channels:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid channel type '{ch_type}'. Valid types: {sorted(valid_channels)}",
            )

        channel_bot_id = ch_cfg.get("channel_bot_id")
        if not channel_bot_id:
            raise HTTPException(
                status_code=400,
                detail=f"Channel '{ch_type}' must have a channel_bot_id",
            )

        # Verify channel bot exists and is connected
        try:
            bid = uuid.UUID(channel_bot_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid channel_bot_id for channel '{ch_type}'",
            ) from exc

        result = await db.execute(
            select(
                ChannelBot.id,
                ChannelBot.name,
                ChannelBot.provider,
                ChannelBot.is_company_managed,
                ChannelBot.allowed_receive_channels,
                ChannelBot.allowed_publish_channels,
                ChannelBot.status,
            ).where(ChannelBot.id == bid)
        )
        bot = result.one_or_none()
        if not bot:
            raise HTTPException(
                status_code=404, detail=f"Channel bot not found for channel '{ch_type}'"
            )

        expected_provider = CHANNEL_TO_BOT_PROVIDER.get(ch_type)
        if bot.provider != expected_provider:
            raise HTTPException(
                status_code=400,
                detail=f"Channel bot for '{ch_type}' must be a {expected_provider} bot, got {bot.provider}",
            )

        # bypass_whitelist is admin-only — reject if set by non-admin.
        if ch_cfg.get("bypass_whitelist") and not is_admin:
            raise HTTPException(
                status_code=403,
                detail=WHITELIST_BYPASS_REFUSED_DETAIL,
            )

        # allow_private is admin-only — strip for non-admins so they
        # inherit the bot-level default.  Normal users should use
        # channel_agent_config in their profile to route DMs.
        if not is_admin and "allow_private" in ch_cfg:
            ch_cfg.pop("allow_private", None)

        # Admins bypass all whitelist restrictions — they can select any
        # channel and enable bypass_whitelist to receive/publish on all.
        if is_admin:
            continue

        # For non-admin personal agents, enforce per-direction whitelists.
        if not is_company_managed:
            has_any_whitelist = bool(bot.allowed_receive_channels) or bool(
                bot.allowed_publish_channels
            )
            if bot.is_company_managed and not has_any_whitelist:
                raise HTTPException(
                    status_code=400,
                    detail=f'Company-managed bot "{bot.name}" has no channel whitelist. '
                    f"Personal agents can only use bots with a configured whitelist.",
                )

            # Validate receive_channels against allowed_receive_channels
            if bot.allowed_receive_channels:
                receive_ids = {ch["id"] for ch in bot.allowed_receive_channels}
                for ch in ch_cfg.get("receive_channels", []):
                    if ch.get("id") not in receive_ids:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Channel '{ch.get('name', ch.get('id'))}' in receive_channels "
                            f"for '{ch_type}' is not in the bot's receive whitelist.",
                        )

            # Validate publish_channels against allowed_publish_channels
            if bot.allowed_publish_channels:
                publish_ids = {ch["id"] for ch in bot.allowed_publish_channels}
                for ch in ch_cfg.get("publish_channels", []):
                    if ch.get("id") not in publish_ids:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Channel '{ch.get('name', ch.get('id'))}' in publish_channels "
                            f"for '{ch_type}' is not in the bot's publish whitelist.",
                        )


def derive_available_channel_types(
    channel_config: dict | None, current: list | None
) -> list:
    """Derive available_channel_types from channel_config.

    Keeps "web" if present in the current list, and adds channel types
    from channel_config where receive_enabled is True.
    """
    types = []
    if current and "web" in current:
        types.append("web")
    elif current is None:
        # Legacy: NULL means all channels, preserve web
        types.append("web")

    if channel_config:
        for ch_type, ch_cfg in channel_config.items():
            if ch_cfg.get("receive_enabled"):
                types.append(ch_type)

    return types
