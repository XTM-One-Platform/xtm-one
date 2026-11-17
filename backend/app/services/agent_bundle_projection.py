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

"""An agent's export bundle, and what it carries for a reader who cannot manage it.

:func:`build_agent_export_bundle` assembles the native bundle a manager's
export gets; everything below is the projection of it other readers get.

Two surfaces hand the bundle to someone other than the agent's managers: the
Filigran AI Catalog download (any user of the instance) and
``GET /agents/{id}/export`` called by a user who can see the agent but not
manage it - a company-managed agent is visible to every user, a personal one
may be shared ``read_only``. Both get this ONE projection:

- the per-section allow-list of ``strip_internal_ids_for_public_download``: the
  owner's ids dropped or redacted, a credential passed to an MCP server as an
  argument or in its URL masked;
- nothing of a company-managed custom tool, MCP server, skill or prompt narrowed
  to an audience (``access_restricted``) the reader is not in, nor of a variable
  only such a resource names;
- no code of the custom tools that are not company-managed, unless the
  publisher's catalog switch includes it.

An export by a non-manager also leaves every variable's value out: disclosing
the owner's values is a publishing choice, which the catalog download reflects
and a shared agent never made.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.helpers import user_visible_filter
from app.core.utils import strip_webhook_secret
from app.models.agent import Agent
from app.models.agent_copy import (
    AGENT_BUNDLE_EXPORT_ONLY_FIELDS,
    AGENT_BUNDLE_PORTABLE_FIELDS,
    ASSIGNMENT_INTEGRATION_OVERRIDE_KEY,
    export_integration_override,
    strip_internal_ids_for_public_download,
)
from app.models.agent_skill import AgentSkill
from app.models.assignment import Assignment
from app.models.custom_tool import CustomTool
from app.models.integration import Integration
from app.models.knowledge_base import KnowledgeBase
from app.models.mcp_server import MCPServer
from app.models.prompt import Prompt
from app.models.user import User
from app.services.agent_bindings import canonical_id


def safe_uuids(ids: list | None) -> list[uuid.UUID]:
    """Convert a list of string ids to UUIDs, skipping invalid ones."""
    if not ids:
        return []
    result = []
    for sid in ids:
        try:
            result.append(uuid.UUID(sid) if isinstance(sid, str) else sid)
        except (ValueError, AttributeError):
            continue
    return result


async def bindable_ids(
    db: AsyncSession,
    model: Any,
    raw_ids: Any,
    user: User,
    user_group_ids: list[str],
) -> set[str]:
    """Ids among ``raw_ids`` of company-managed ``model`` rows ``user`` may use.

    What a catalog install binds BY REFERENCE, and what the catalog page shows
    as bound automatically. Company-managed alone is not enough: a row narrowed
    to an audience (``access_restricted``) the installer is not in is not
    theirs to use, and a run uses a bound integration, server or tool without
    re-checking who owns the agent. The installer's three-tier visibility
    decides, through the shared predicate (an admin sees everything, as
    everywhere else); the caller copies, recreates or drops the rest.
    """
    uuids = safe_uuids([str(i) for i in (raw_ids or []) if i])
    if not uuids:
        return set()
    rows = await db.execute(
        select(model).where(
            model.id.in_(uuids),
            model.is_company_managed.is_(True),
            user_visible_filter(
                model,
                user.id,
                user_group_ids,
                is_admin=bool(getattr(user, "is_admin", False)),
            ),
        )
    )
    return {str(row.id) for row in rows.scalars().all() if row.is_company_managed}


async def narrowed_ids(
    db: AsyncSession,
    model: Any,
    raw_ids: Any,
    user: User,
    user_group_ids: list[str],
) -> frozenset[str]:
    """Ids among ``raw_ids`` of company-managed ``model`` rows narrowed to an
    audience ``user`` is not in: the company-managed ones :func:`bindable_ids`
    leaves out. The content of a narrowed tool, server, skill or prompt is not
    theirs to read."""
    uuids = safe_uuids([str(i) for i in (raw_ids or []) if i])
    if not uuids:
        return frozenset()
    rows = await db.execute(select(model).where(model.id.in_(uuids)))
    managed = {str(row.id) for row in rows.scalars().all() if row.is_company_managed}
    if not managed:
        return frozenset()
    return frozenset(
        managed - await bindable_ids(db, model, sorted(managed), user, user_group_ids)
    )


async def omitted_personal_custom_tools(db: AsyncSession, agent: Agent) -> list[CustomTool]:
    """The custom tools the projection leaves out: the ones that are not
    company-managed, unless the publisher chose to include them.
    Company-managed tools are always carried (to a reader in their audience)."""
    if agent.catalog_download_includes_personal_tools:
        return []
    uuids = safe_uuids(agent.custom_tool_ids)
    if not uuids:
        return []
    rows = await db.execute(select(CustomTool).where(CustomTool.id.in_(uuids)))
    return [tool for tool in rows.scalars().all() if not tool.is_company_managed]


async def build_reader_agent_bundle(
    db: AsyncSession,
    agent: Agent,
    user: User,
    user_group_ids: list[str],
    *,
    variable_values: bool,
) -> dict[str, Any]:
    """``agent``'s export bundle as a reader who cannot manage it gets it.

    ``variable_values`` keeps the value of each non-secret variable the owner
    can see (the catalog download, a publishing act); without it every
    variable travels by name only and the importer fills it in.
    """

    async def _narrowed(model: Any, raw_ids: Any) -> frozenset[str]:
        return await narrowed_ids(db, model, raw_ids, user, user_group_ids)

    omitted = await omitted_personal_custom_tools(db, agent)
    bundle = strip_internal_ids_for_public_download(
        await build_agent_export_bundle(
            db,
            agent,
            omit_custom_tool_ids=frozenset(str(t.id) for t in omitted)
            | await _narrowed(CustomTool, agent.custom_tool_ids),
            omit_mcp_server_ids=await _narrowed(MCPServer, agent.mcp_server_ids),
            omit_skill_ids=await _narrowed(AgentSkill, agent.skill_ids),
            omit_prompt_ids=await _narrowed(Prompt, agent.prompt_ids),
        )
    )
    if omitted:
        names = sorted(t.name for t in omitted)
        bundle["omitted_custom_tools"] = [{"name": name} for name in names]
        bundle["download_notes"] = [
            "The code of personal custom tools is not included in this file: "
            f"{', '.join(names)}. The agent imports without them."
        ]
    if not variable_values:
        for variable in bundle.get("variables") or []:
            variable.pop("value", None)
    return bundle


def _export_tool_sources(
    value: object, ref_by_id: dict[str, str]
) -> list[str] | None:
    """An assignment's MCP scope as the bundle carries it.

    A server of the bundle is named by its ``ref``, which the import maps onto
    the copy it recreates, whatever spelling of its id the row holds (the API
    stores the one it was sent); any other entry is written as is and checked
    against the importer's visibility there. ``None`` stays ``None``. The API
    only writes a list or ``None``: a legacy value of another shape is written
    the way the engine reads it (empty = every server, anything else = a scope
    that names no server), so the file still imports.
    """
    if isinstance(value, list):
        return [
            ref_by_id.get(canonical_id(entry) or str(entry), str(entry))
            for entry in value
        ]
    return [str(value)] if value else None


async def build_agent_export_bundle(
    db: AsyncSession,
    agent: Agent,
    *,
    omit_custom_tool_ids: frozenset[str] = frozenset(),
    omit_mcp_server_ids: frozenset[str] = frozenset(),
    omit_skill_ids: frozenset[str] = frozenset(),
    omit_prompt_ids: frozenset[str] = frozenset(),
) -> dict:
    """Assemble the native ``version: 3`` export bundle for ``agent``.

    The ``omit_*`` ids leave those resources out entirely - their content and
    the variables only they reference. The catalog download passes the
    personal custom tools of a publisher who keeps them to themselves
    and the company-managed resources narrowed to an audience its reader is
    not in.

    The single place that decides what leaves the platform. Both export formats
    are rendered from this dict, so a field that is secret-stripped or omitted
    here cannot reappear through the AgentSchema format.

    Includes the agent config, its assignments, bound custom tools, and bound
    MCP servers.  Secrets (env vars, headers) and platform-specific bindings
    (integrations, knowledge bases) are intentionally omitted.
    """
    # --- Assignments ---
    wf_result = await db.execute(
        select(Assignment)
        .where(Assignment.agent_id == agent.id)
        .order_by(Assignment.created_at)
    )
    assignments = wf_result.scalars().all()

    # --- Custom Tools ---
    custom_tools_data: list[dict] = []
    if agent.custom_tool_ids:
        ct_uuids = []
        for ct_id in agent.custom_tool_ids:
            try:
                ct_uuids.append(uuid.UUID(ct_id))
            except ValueError:
                continue
        if ct_uuids:
            ct_result = await db.execute(
                select(CustomTool).where(CustomTool.id.in_(ct_uuids))
            )
            for ct in ct_result.scalars().all():
                if str(ct.id) in omit_custom_tool_ids:
                    continue
                custom_tools_data.append(
                    {
                        "name": ct.name,
                        "description": ct.description or "",
                        "icon_url": ct.icon_url,
                        "input_schema": ct.input_schema,
                        "js_function": ct.js_function,
                        # Without it a Python tool re-imports as JavaScript;
                        # the custom-tool export already carries both.
                        "language": ct.language or "javascript",
                        "timeout": ct.timeout or 300,
                        "tags": ct.tags or [],
                    }
                )

    # --- MCP Servers ---
    mcp_servers_data: list[dict] = []
    mcp_ref_by_id: dict[str, str] = {}
    if agent.mcp_server_ids:
        mcp_uuids = []
        for mcp_id in agent.mcp_server_ids:
            try:
                mcp_uuids.append(uuid.UUID(mcp_id))
            except ValueError:
                continue
        if mcp_uuids:
            mcp_result = await db.execute(
                select(MCPServer).where(MCPServer.id.in_(mcp_uuids))
            )
            # Per-server tool scoping travels WITH the server entry rather than
            # as the id-keyed ``mcp_allowed_tools`` map, for the same reason as
            # the skills' ``load_mode`` below: the bundle carries no server ids
            # (they don't resolve in the target tenant), so an id-keyed map
            # would be unmappable on import and the scope would silently widen
            # to every tool the server offers.
            _allowed = agent.mcp_allowed_tools or {}
            for srv in mcp_result.scalars().all():
                if str(srv.id) in omit_mcp_server_ids:
                    continue
                # An opaque, bundle-local reference: an assignment's
                # ``tool_sources`` names its servers by it, and the import
                # maps it onto the server it recreates.
                ref = f"mcp-{len(mcp_servers_data)}"
                mcp_ref_by_id[str(srv.id)] = ref
                mcp_servers_data.append(
                    {
                        "ref": ref,
                        "name": srv.name,
                        "transport": srv.transport,
                        "command": srv.command,
                        "args": srv.args or [],
                        "url": srv.url,
                        "tags": srv.tags or [],
                        "allowed_tools": _allowed.get(str(srv.id)),
                    }
                )

    # --- Integration providers (type info only, no secrets) ---
    from app.models.agent_skill import AgentSkill
    from app.models.prompt import Prompt
    from app.services.variable_resolver import (
        extract_variable_ids,
        load_owner_visible_variable_records,
    )

    integration_providers: list[dict] = []
    if agent.integration_bindings:
        for provider_key, int_ids in agent.integration_bindings.items():
            ids_list = int_ids if isinstance(int_ids, list) else [int_ids]
            for int_id in ids_list:
                try:
                    int_uuid = uuid.UUID(int_id) if isinstance(int_id, str) else int_id
                    int_result = await db.execute(
                        select(Integration).where(Integration.id == int_uuid)
                    )
                    integration = int_result.scalar_one_or_none()
                    if integration:
                        integration_providers.append(
                            {
                                "provider": provider_key,
                                "name": integration.name,
                                "type": integration.provider,
                            }
                        )
                except (ValueError, AttributeError):
                    integration_providers.append({"provider": provider_key})

    # --- Skills (name + URL, no secrets) ---
    skills_data: list[dict] = []
    if agent.skill_ids:
        skill_uuids = []
        for sid in agent.skill_ids:
            try:
                skill_uuids.append(uuid.UUID(sid))
            except ValueError:
                continue
        if skill_uuids:
            sk_result = await db.execute(
                select(AgentSkill).where(AgentSkill.id.in_(skill_uuids))
            )
            # Per-skill load mode travels WITH the skill entry rather than as
            # a separate id-keyed map: the bundle carries no skill ids (they
            # don't resolve in the target tenant), so an id-keyed map would be
            # unmappable on import and a pinned skill would silently revert to
            # progressive loading.
            _load_modes = agent.skill_load_modes or {}
            for sk in sk_result.scalars().all():
                if str(sk.id) in omit_skill_ids:
                    continue
                skills_data.append(
                    {
                        "name": sk.name,
                        "description": sk.description,
                        "url": sk.url,
                        "content": sk.content,
                        "compatibility": sk.compatibility,
                        "license": sk.license,
                        "tags": sk.tags or [],
                        "load_mode": _load_modes.get(str(sk.id), "progressive"),
                    }
                )

    # --- Prompts (name + content, no secrets) ---
    prompts_data: list[dict] = []
    if agent.prompt_ids:
        prompt_uuids = []
        for pid in agent.prompt_ids:
            try:
                prompt_uuids.append(uuid.UUID(pid))
            except ValueError:
                continue
        if prompt_uuids:
            pr_result = await db.execute(
                select(Prompt).where(Prompt.id.in_(prompt_uuids))
            )
            for pr in pr_result.scalars().all():
                if str(pr.id) in omit_prompt_ids:
                    continue
                prompts_data.append(
                    {
                        "name": pr.name,
                        "description": pr.description,
                        "content": pr.content,
                        "tags": pr.tags or [],
                    }
                )

    # --- Knowledge bases (names only, informational) ---
    kb_notes: list[dict] = []
    if agent.knowledge_base_ids:
        kb_uuids = []
        for kid in agent.knowledge_base_ids:
            try:
                kb_uuids.append(uuid.UUID(kid))
            except ValueError:
                continue
        if kb_uuids:
            kb_result = await db.execute(
                select(KnowledgeBase).where(KnowledgeBase.id.in_(kb_uuids))
            )
            for kb in kb_result.scalars().all():
                kb_notes.append(
                    {
                        "name": kb.name,
                        "description": kb.description,
                        "note": "Knowledge bases are not included in exports. "
                        "Create and configure them separately.",
                    }
                )

    # --- Referenced variables (full records) ---
    # Every text the import rewrites onto the new variable ids: a variable only
    # one of them names would otherwise stay behind and never resolve.
    all_text_parts = [
        agent.persona or "",
        agent.guardrails or "",
    ]
    for wf in assignments:
        all_text_parts.append(wf.prompt or "")
        all_text_parts.append(wf.activation_prompt or "")
    for ct in custom_tools_data:
        all_text_parts.append(ct.get("description") or "")
        all_text_parts.append(ct.get("js_function") or "")
    for sk in skills_data:
        all_text_parts.append(sk.get("content") or "")
    for pr in prompts_data:
        all_text_parts.append(pr.get("content") or "")

    referenced_ids: list[str] = []
    for part in all_text_parts:
        referenced_ids.extend(extract_variable_ids(part))
    referenced_ids = sorted(set(referenced_ids))

    variables_data: list[dict] = []
    if referenced_ids:
        # Only what the agent's owner can resolve: an id typed into the text
        # that names someone else's variable is not the export's to hand out
        # (this bundle is also the public catalog download).
        var_records = await load_owner_visible_variable_records(
            db, referenced_ids, agent.user_id
        )
        for v in var_records:
            entry: dict = {
                "id": str(v.id),
                # ``Variable`` exposes ``name`` (there is no ``key`` attribute).
                # ``key`` is kept in the bundle only for backward compatibility
                # with any exports already in the wild.
                "name": v.name,
                "key": v.name,
                "description": v.description,
                "tags": v.tags or [],
                "is_secret": v.is_secret,
            }
            if not v.is_secret:
                entry["value"] = v.value
            variables_data.append(entry)

    # --- Build export bundle ---
    bundle = {
        "version": 3,
        "export_type": "agent",
        "exported_at": datetime.now(UTC).isoformat(),
        # The ``agent`` section is driven by the shared bundle manifest, not by
        # a field list maintained here: a hand-written list silently drops each
        # newly added column, which is how a re-imported OpenCTI agent lost
        # ``integration_template_bindings`` — and every per-user OpenCTI tool
        # with it. ``AGENT_BUNDLE_SKIP_FIELDS`` records, per column, why
        # anything absent from the bundle is absent.
        "agent": {
            **{
                name: getattr(agent, name)
                for name in (
                    *AGENT_BUNDLE_PORTABLE_FIELDS,
                    *AGENT_BUNDLE_EXPORT_ONLY_FIELDS,
                )
            },
            # Id-shaped lists are stringified: the column holds JSON that may
            # carry UUID objects, which are not JSON-serialisable.
            "runner_ids": [str(i) for i in (agent.runner_ids or [])],
            "work_item_owner_agent_ids": [
                str(i) for i in (agent.work_item_owner_agent_ids or [])
            ],
            "trace_supervision_target_agent_ids": [
                str(i) for i in (agent.trace_supervision_target_agent_ids or [])
            ],
        },
        "assignments": [
            {
                "name": wf.name,
                "description": wf.description,
                "trigger_type": wf.trigger_type,
                # The webhook signing secret is a live credential — exported
                # bundles must never carry it (the endpoint contract says
                # secrets are omitted, and bundles travel between tenants).
                # A fresh secret is generated when the bundle is imported.
                "trigger_config": strip_webhook_secret(wf.trigger_config or {}),
                "activation_prompt": wf.activation_prompt,
                "activation_check": wf.activation_check,
                "prompt": wf.prompt,
                "delay": wf.delay,
                "tool_sources": _export_tool_sources(wf.tool_sources, mcp_ref_by_id),
                ASSIGNMENT_INTEGRATION_OVERRIDE_KEY: export_integration_override(
                    wf.integration_bindings
                ),
                "use_knowledge_bases": wf.use_knowledge_bases,
                "ai_label": wf.ai_label,
                "ai_label_prompt": wf.ai_label_prompt,
                "notify_on_complete": wf.notify_on_complete,
                "show_in_digest": wf.show_in_digest,
                "human_in_the_loop": wf.human_in_the_loop,
                "human_in_the_loop_prompt": wf.human_in_the_loop_prompt,
                "max_iterations": wf.max_iterations,
                "timeout_seconds": wf.timeout_seconds,
                "retry_on_failure": wf.retry_on_failure,
                "max_retries": wf.max_retries,
                "dry_run": wf.dry_run,
                # The self-trigger loop guard and the assignment's tool scopes
                # (keyed by provider name, portable) narrow what it may do: a
                # bundle without them re-imports with the loop guard off and the
                # scopes widened.
                "ignore_self_triggered_events": wf.ignore_self_triggered_events,
                "integration_allowed_tools": wf.integration_allowed_tools,
                # Built-in function scoping: group ids, portable.
                "builtin_allowed_tools": getattr(wf, "builtin_allowed_tools", None),
                "integration_element_bindings": wf.integration_element_bindings,
                "integration_element_enforcement": wf.integration_element_enforcement,
                "hide_runs": wf.hide_runs,
                "manual_trigger_parameters": wf.manual_trigger_parameters,
                "enabled": wf.enabled,
                "read_knowledge_base_ids": wf.read_knowledge_base_ids,
                "write_knowledge_base_ids": wf.write_knowledge_base_ids,
                "write_knowledge_base_enabled": wf.write_knowledge_base_enabled,
                # Tri-state runner refinement (null = inherit, [] = none, subset
                # = refine). Preserved so an assignment's machine scope survives
                # a round-trip instead of collapsing to inherit-all on import.
                "runner_ids": wf.runner_ids,
            }
            for wf in assignments
        ],
        "custom_tools": custom_tools_data,
        "mcp_servers": mcp_servers_data,
        "integration_providers": integration_providers,
        "skills": skills_data,
        "prompts": prompts_data,
        "knowledge_base_notes": kb_notes,
        "variables": variables_data,
    }
    return bundle
