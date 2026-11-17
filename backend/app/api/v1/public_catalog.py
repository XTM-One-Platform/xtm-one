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

"""Internal Catalog API – authenticated endpoints for browsing and installing
shared resources.

Provides the Filigran AI Catalog where published entities (``is_public_catalog=True``)
can be browsed by authenticated platform users.  Users can install catalog items
into their own account with a setup wizard that auto-binds company-managed
resources and lets them configure the rest.

All endpoints require JWT authentication.
"""

from __future__ import annotations

import uuid as _uuid_mod
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_ai_enabled
from app.api.v1.agent_import import _seed_initial_versions
from app.api.v1.helpers import (
    generate_unique_agent_slug,
    get_user_group_ids,
    lower_budget_to_ceiling,
    parse_path_id,
)
from app.api.v1.schemas.agent import VariableNeedingValueResponse
from app.core.database import get_db
from app.core.json_sql import when_json_parseable
from app.core.utils import (
    build_content_disposition,
    ensure_webhook_secret,
    is_valid_uuid,
    redact_secret_args,
    redact_secret_text,
)
from app.models.agent import Agent
from app.models.agent_copy import (
    REDACTED_MCP_SERVER_ID,
    build_agent_catalog_clone_kwargs,
    build_assignment_catalog_clone_kwargs,
    rekey_id_list,
    rekey_id_map,
    strip_trigger_config_internal_ids,
)
from app.models.agent_skill import AgentSkill
from app.models.agent_skill_resource import copy_skill_resources
from app.models.assignment import Assignment
from app.models.audit_log import AuditLog
from app.models.custom_tool import CustomTool
from app.models.general_assistant_config import clamp_max_iterations
from app.models.integration import Integration
from app.models.knowledge_base import KnowledgeBase
from app.models.mcp_server import MCPServer
from app.models.prompt import Prompt
from app.models.user import User
from app.services import agent_bundle_projection as projection
from app.services.agent_bundle_projection import safe_uuids as _safe_uuids
from app.services.assignment_pause import (
    actor_display_name,
    pause_assignment,
)
from app.services.audit import log_audit
from app.services.company_managed_writes import portable_coordination_access
from app.services.integration_read_only import carried_read_only
from app.services.skill_package_bindings import (
    reconcile_agent_skill_package_bindings,
)
from app.services.variable_resolver import (
    extract_variable_ids,
    load_owner_visible_variable_records,
    variable_has_value,
)

router = APIRouter()

CATALOG_PAGE_SIZE = 20


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _apply_catalog_filters(
    stmt: Any,
    model: Any,
    *,
    search: str | None,
    tags: str | None,
    search_fields: list[str] | None = None,
):
    """Apply search + tag filtering to a catalog query."""
    if search:
        q = f"%{search}%"
        fields = search_fields or ["name", "description"]
        conditions = [getattr(model, f).ilike(q) for f in fields if hasattr(model, f)]
        if conditions:
            stmt = stmt.where(or_(*conditions))
    if tags:
        for tag in tags.split(","):
            tag = tag.strip()
            if tag:
                # The tag is bound, never spliced into a JSON literal (a ``"``
                # or ``\\`` in it broke or bent the literal); one row's tags
                # holding a lone surrogate escape must not abort the filter
                # for everyone.
                stmt = stmt.where(
                    when_json_parseable(
                        model.tags,
                        cast(model.tags, JSONB).op("@>")(
                            cast(func.jsonb_build_array(tag), JSONB)
                        ),
                    )
                )
    return stmt


async def _resolve_shared_by(
    db: AsyncSession,
    entity_type: str,
    entity_ids: list,
) -> dict[str, str]:
    """Resolve the name of the user who last modified each entity."""
    if not entity_ids:
        return {}

    str_ids = [str(eid) for eid in entity_ids]

    latest_sub = (
        select(
            AuditLog.entity_id,
            func.max(AuditLog.created_at).label("latest"),
        )
        .where(
            AuditLog.entity_type == entity_type,
            AuditLog.entity_id.in_([_uuid_mod.UUID(s) for s in str_ids]),
        )
        .group_by(AuditLog.entity_id)
        .subquery()
    )

    result = await db.execute(
        select(AuditLog.entity_id, User.full_name)
        .join(
            latest_sub,
            (AuditLog.entity_id == latest_sub.c.entity_id)
            & (AuditLog.created_at == latest_sub.c.latest),
        )
        .join(User, AuditLog.user_id == User.id)
        .where(AuditLog.entity_type == entity_type)
    )
    return {str(eid): name for eid, name in result.all()}


async def _resolve_shared_by_single(
    db: AsyncSession,
    entity_type: str,
    entity_id: Any,
) -> str | None:
    mapping = await _resolve_shared_by(db, entity_type, [entity_id])
    return mapping.get(str(entity_id))


async def _get_all_tags(db: AsyncSession, model: Any) -> list[str]:
    result = await db.execute(
        select(model.tags).where(
            model.is_public_catalog.is_(True), model.tags.isnot(None)
        )
    )
    tag_set: set[str] = set()
    for (tags_list,) in result.all():
        if tags_list:
            tag_set.update(tags_list)
    return sorted(tag_set)


#: What the download and add routes answer, per catalog type, for an id that
#: names no published entity.
_DOWNLOAD_NOT_FOUND = dict.fromkeys(("agents", "prompts", "skills", "mcp-servers", "custom-tools"), "Not found")
_ADD_NOT_FOUND = {
    "agents": "Agent not found in catalog",
    "prompts": "Prompt not found in catalog",
    "skills": "Skill not found in catalog",
    "mcp-servers": "MCP server not found in catalog",
    "custom-tools": "Custom tool not found in catalog",
}


def _catalog_entity_uuid(entity_type: str, entity_id: str, not_found: dict[str, str]) -> _uuid_mod.UUID:
    """The catalog entity id, a malformed one answered like an unknown one of that type."""
    if entity_type not in not_found:
        raise HTTPException(status_code=400, detail=f"Unknown entity type: {entity_type}")
    return parse_path_id(entity_id, not_found_detail=not_found[entity_type])


def _rekey_assignment_integration_bindings(
    value: dict | None,
    final_bindings: dict[str, list[str]],
    chosen_bindings: dict[str, list[str]],
) -> dict | None:
    """Re-key an assignment's tri-state ``integration_bindings`` onto the clone.

    ``None`` inherits every binding of the agent and stays ``None``; ``{}``
    means "no integrations" and stays ``{}``. A subset keeps, per provider, the
    ids the clone still binds (company-managed ones). A provider the source
    reached only through an integration the clone does not carry is mapped
    onto the installer's own choice for that provider in the setup body - never
    onto an integration the clone merely binds for it, which the installer did
    not pick for this assignment - and a provider the source selected nothing
    for stays unselected. Any other provider is dropped, and a subset nothing
    survives from becomes ``{}``, never ``None``: dropping the override would
    widen the assignment to every integration of the cloned agent.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        return {}
    out: dict = {}
    for provider, ids in value.items():
        available = final_bindings.get(provider) or []
        wanted = [
            canonical
            for raw in (ids if isinstance(ids, list) else [ids])
            if (canonical := _canonical_uuid(raw))
        ]
        kept = [i for i in wanted if i in available]
        if not kept and wanted and provider in chosen_bindings:
            kept = list(available)
        if kept:
            out[provider] = kept if isinstance(ids, list) else kept[0]
    return out


def _rekey_tool_sources(value: list | None, mcp_id_map: dict[str, str]) -> list | None:
    """Re-key an assignment's ``tool_sources`` onto the clone's MCP servers.

    Entries are server names or ids: an id the clone re-bound is mapped and a
    name passes through. An id it did not re-bind (a publisher's server outside
    the agent's set) is never left pointing at a server the installer does not
    own - and never simply dropped either: at run time an empty list means
    "every server of the agent", so a scope naming only that server would
    widen to all of them. It becomes ``REDACTED_MCP_SERVER_ID``, which names no
    server, exactly as in a public download.
    """
    if value is None:
        return None
    out: list = []
    for entry in value:
        key = str(entry)
        if key in mcp_id_map:
            out.append(mcp_id_map[key])
        elif not is_valid_uuid(key):
            out.append(entry)
        elif REDACTED_MCP_SERVER_ID not in out:
            out.append(REDACTED_MCP_SERVER_ID)
    return out


def _unmapped_tool_sources(value: list | None, mcp_id_map: dict[str, str]) -> int:
    """How many servers :func:`_rekey_tool_sources` does not carry: the ids it
    redacts, the placeholder itself aside (it names no server)."""
    return len(
        {
            str(entry)
            for entry in value or []
            if str(entry) not in mcp_id_map
            and is_valid_uuid(str(entry))
            and str(entry) != REDACTED_MCP_SERVER_ID
        }
    )


def _dropped_assignment_integrations(
    value: dict | None, rekeyed: dict | None, chosen_bindings: dict[str, list[str]]
) -> int:
    """How many integrations of an assignment override
    :func:`_rekey_assignment_integration_bindings` does not carry. A provider
    the installer chose their own integration for is replaced, not dropped."""
    if not isinstance(value, dict):
        return 0
    carried = rekeyed if isinstance(rekeyed, dict) else {}
    dropped = 0
    for provider, ids in value.items():
        if provider in chosen_bindings:
            continue
        kept = carried.get(provider)
        kept_ids = set(kept if isinstance(kept, list) else [kept] if kept else [])
        dropped += len(
            [
                canonical
                for raw in (ids if isinstance(ids, list) else [ids])
                if (canonical := _canonical_uuid(raw)) and canonical not in kept_ids
            ]
        )
    return dropped


def _canonical_uuid(value: Any) -> str | None:
    try:
        return str(_uuid_mod.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


def _setup_integration_bindings(value: dict | None) -> dict[str, list[str]]:
    """The setup body's per-provider choice as ``{provider: [ids]}``.

    The install wizard sends a list per provider; a bare id is accepted too.
    """
    out: dict[str, list[str]] = {}
    for provider, ids in (value or {}).items():
        wanted = [i for i in (ids if isinstance(ids, list) else [ids]) if i]
        if wanted:
            out[provider] = list(dict.fromkeys(wanted))
    return out


def _clone_trigger_config(
    trigger_type: str, value: Any, final_bindings: dict[str, list[str]]
) -> dict:
    """The trigger configuration a catalog clone's assignment starts with.

    Never the publisher's webhook signing secret (a fresh one is generated,
    SEC-5) nor the keys that name the publisher's rows: absent, the trigger
    polls the clone's own binding for its provider and a work-item inbox
    drains the clone's own queue. A polled integration the clone binds itself
    - a company-managed one the installer may use - is kept.
    """
    source = value if isinstance(value, dict) else {}
    config = strip_trigger_config_internal_ids(source)
    polled = _canonical_uuid(source.get("integration_id"))
    bound = {
        canonical
        for ids in final_bindings.values()
        for raw in ids
        if (canonical := _canonical_uuid(raw))
    }
    if polled and polled in bound:
        config["integration_id"] = polled
    return ensure_webhook_secret(trigger_type, config)


# The ``_resolve_*`` helpers describe a catalog agent's resources to ONE
# reader. ``is_company_managed`` there answers the question the install wizard
# asks - "will this be bound automatically?" - so it is true only for the ids
# in ``bindable`` (see ``_bindable_ids``). A company-managed row narrowed to an
# audience the reader is not in is not the reader's: an integration or a
# knowledge base is presented like a private one (the install asks for the
# reader's own integration and recreates the knowledge base empty), while an
# MCP server, custom tool, skill or prompt - which the install and the
# download leave out for that reader - is not listed at all.


def _listed_for_reader(row: Any, bindable: set[str]) -> bool:
    """Whether a catalog page lists ``row`` to the reader ``bindable`` was
    computed for: anything but a company-managed row narrowed away from them."""
    return str(row.id) in bindable or not row.is_company_managed


async def _resolve_integrations(
    db: AsyncSession, bindings: dict | None, bindable: set[str]
) -> list[dict]:
    """Resolve integration bindings, marking the ones bound automatically.

    An integration id is only handed to a reader who could bind it.
    """
    if not bindings:
        return []
    result: list[dict] = []
    for provider_key, int_ids in bindings.items():
        if not isinstance(int_ids, list):
            int_ids = [int_ids] if int_ids else []
        for int_id in int_ids:
            try:
                int_uuid = _uuid_mod.UUID(int_id) if isinstance(int_id, str) else int_id
                ir = await db.execute(
                    select(Integration).where(Integration.id == int_uuid)
                )
                integ = ir.scalar_one_or_none()
                if integ:
                    entry = {
                        "provider": provider_key,
                        "name": integ.name,
                        "is_company_managed": str(integ.id) in bindable,
                    }
                    if str(integ.id) in bindable:
                        entry["integration_id"] = str(integ.id)
                    result.append(entry)
                else:
                    result.append({"provider": provider_key})
            except (ValueError, AttributeError):
                result.append({"provider": provider_key})
    return result


async def _resolve_knowledge_bases(
    db: AsyncSession, kb_ids: list | None, bindable: set[str]
) -> list[dict]:
    """Resolve KBs, marking the ones bound automatically."""
    uuids = _safe_uuids(kb_ids)
    if not uuids:
        return []
    kr = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id.in_(uuids)))
    return [
        {
            "id": str(kb.id),
            "name": kb.name,
            "description": kb.description,
            "is_company_managed": str(kb.id) in bindable,
        }
        for kb in kr.scalars().all()
    ]


async def _resolve_mcp_servers(
    db: AsyncSession, server_ids: list | None, bindable: set[str]
) -> list[dict]:
    """Resolve MCP servers, marking the ones bound automatically."""
    uuids = _safe_uuids(server_ids)
    if not uuids:
        return []
    mr = await db.execute(select(MCPServer).where(MCPServer.id.in_(uuids)))
    return [
        {
            "id": str(srv.id),
            "name": srv.name,
            "is_company_managed": str(srv.id) in bindable,
        }
        for srv in mr.scalars().all()
        if _listed_for_reader(srv, bindable)
    ]


async def _resolve_custom_tools(
    db: AsyncSession, tool_ids: list | None, bindable: set[str]
) -> list[dict]:
    """Resolve custom tools, marking the ones bound automatically."""
    uuids = _safe_uuids(tool_ids)
    if not uuids:
        return []
    tr = await db.execute(select(CustomTool).where(CustomTool.id.in_(uuids)))
    return [
        {
            "id": str(t.id),
            "name": t.name,
            "is_company_managed": str(t.id) in bindable,
        }
        for t in tr.scalars().all()
        if _listed_for_reader(t, bindable)
    ]


async def _resolve_skills(
    db: AsyncSession, skill_ids: list | None, bindable: set[str]
) -> list[dict]:
    """Resolve skills, marking the ones bound automatically."""
    uuids = _safe_uuids(skill_ids)
    if not uuids:
        return []
    sr = await db.execute(select(AgentSkill).where(AgentSkill.id.in_(uuids)))
    return [
        {
            "id": str(sk.id),
            "name": sk.name,
            "description": sk.description,
            "is_company_managed": str(sk.id) in bindable,
        }
        for sk in sr.scalars().all()
        if _listed_for_reader(sk, bindable)
    ]


async def _resolve_prompts(
    db: AsyncSession, prompt_ids: list | None, bindable: set[str]
) -> list[dict]:
    """Resolve prompts, marking the ones bound automatically."""
    uuids = _safe_uuids(prompt_ids)
    if not uuids:
        return []
    pr = await db.execute(select(Prompt).where(Prompt.id.in_(uuids)))
    return [
        {
            "id": str(p.id),
            "name": p.name,
            "description": p.description,
            "is_company_managed": str(p.id) in bindable,
        }
        for p in pr.scalars().all()
        if _listed_for_reader(p, bindable)
    ]


def _integration_ids(bindings: dict | None) -> list[str]:
    return [
        str(raw)
        for ids in (bindings or {}).values()
        for raw in (ids if isinstance(ids, list) else [ids])
        if raw
    ]


# ---------------------------------------------------------------------------
# Catalog landing – counts + featured items
# ---------------------------------------------------------------------------


@router.get("/catalog")
async def catalog_summary(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return counts per type + a few featured items for the landing page."""
    counts = {}
    for model, key in [
        (Agent, "agents"),
        (Prompt, "prompts"),
        (AgentSkill, "skills"),
        (MCPServer, "mcp_servers"),
        (CustomTool, "custom_tools"),
    ]:
        result = await db.execute(
            select(func.count(model.id)).where(model.is_public_catalog.is_(True))
        )
        counts[key] = result.scalar() or 0

    return JSONResponse(content={"counts": counts, "total": sum(counts.values())})


# ---------------------------------------------------------------------------
# Tags (for filter pills)
# ---------------------------------------------------------------------------


@router.get("/catalog/tags")
async def catalog_tags(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result: dict[str, list[str]] = {}
    for model, key in [
        (Agent, "agents"),
        (Prompt, "prompts"),
        (AgentSkill, "skills"),
        (MCPServer, "mcp_servers"),
        (CustomTool, "custom_tools"),
    ]:
        result[key] = await _get_all_tags(db, model)
    return JSONResponse(content=result)


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


@router.get("/catalog/agents")
async def list_public_agents(
    search: str | None = Query(None),
    tags: str | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(CATALOG_PAGE_SIZE, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    base = select(Agent).where(Agent.is_public_catalog.is_(True))
    base = _apply_catalog_filters(base, Agent, search=search, tags=tags)

    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar() or 0

    result = await db.execute(base.order_by(Agent.name).offset(offset).limit(limit))
    agents = result.scalars().all()

    agent_ids = [a.id for a in agents]
    shared_by_map = await _resolve_shared_by(db, "agent", agent_ids)

    # Batch the per-agent assignment counts into one grouped query instead of
    # one count query per agent (every authenticated user can list the
    # catalog).
    wf_counts: dict = {}
    if agent_ids:
        counts_result = await db.execute(
            select(Assignment.agent_id, func.count(Assignment.id))
            .where(Assignment.agent_id.in_(agent_ids))
            .group_by(Assignment.agent_id)
        )
        wf_counts = {row[0]: row[1] for row in counts_result.all()}

    items = []
    for a in agents:
        wf_count = wf_counts.get(a.id, 0)
        items.append(
            {
                "id": str(a.id),
                "name": a.name,
                "description": a.description,
                "icon": a.icon,
                "tags": a.tags or [],
                "output_format": a.output_format,
                "output_schema": a.output_schema,
                "assignment_count": wf_count,
                "shared_by": shared_by_map.get(str(a.id)),
                "created_at": a.created_at.isoformat(),
            }
        )

    return JSONResponse(content={"items": items, "total": total})


@router.get("/catalog/agents/{agent_id}")
async def get_public_agent(
    agent_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a single catalog agent with enriched binding info."""
    uid = parse_path_id(agent_id, not_found_detail="Agent not found in catalog")
    result = await db.execute(
        select(Agent).where(Agent.id == uid, Agent.is_public_catalog.is_(True))
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found in catalog")

    wf_result = await db.execute(
        select(Assignment).where(Assignment.agent_id == agent.id)
    )
    assignments = wf_result.scalars().all()
    shared_by = await _resolve_shared_by_single(db, "agent", agent.id)

    user_group_ids = await get_user_group_ids(db, user.id)

    async def _bindable(model: Any, raw_ids: Any) -> set[str]:
        return await projection.bindable_ids(db, model, raw_ids, user, user_group_ids)

    integration_providers = await _resolve_integrations(
        db,
        agent.integration_bindings,
        await _bindable(Integration, _integration_ids(agent.integration_bindings)),
    )
    knowledge_bases = await _resolve_knowledge_bases(
        db, agent.knowledge_base_ids, await _bindable(KnowledgeBase, agent.knowledge_base_ids)
    )
    mcp_servers = await _resolve_mcp_servers(
        db, agent.mcp_server_ids, await _bindable(MCPServer, agent.mcp_server_ids)
    )
    custom_tools = await _resolve_custom_tools(
        db, agent.custom_tool_ids, await _bindable(CustomTool, agent.custom_tool_ids)
    )
    skills = await _resolve_skills(
        db, agent.skill_ids, await _bindable(AgentSkill, agent.skill_ids)
    )
    prompts = await _resolve_prompts(
        db, agent.prompt_ids, await _bindable(Prompt, agent.prompt_ids)
    )

    all_text = [agent.persona or "", agent.guardrails or ""]
    for w in assignments:
        all_text.extend([w.prompt or "", w.activation_prompt or ""])
    ref_ids = sorted(set(v for t in all_text for v in extract_variable_ids(t)))

    variables_data: list[dict] = []
    if ref_ids:
        var_records = await load_owner_visible_variable_records(
            db, ref_ids, agent.user_id
        )
        for v in var_records:
            entry: dict = {
                "id": str(v.id),
                # ``Variable`` exposes ``name``; ``key`` retained for legacy
                # bundle compatibility only.
                "name": v.name,
                "key": v.name,
                "description": v.description,
                "tags": v.tags or [],
                "is_secret": v.is_secret,
            }
            if not v.is_secret:
                entry["value"] = v.value
            variables_data.append(entry)

    return JSONResponse(
        content={
            "id": str(agent.id),
            "name": agent.name,
            "description": agent.description,
            "icon": agent.icon,
            "persona": agent.persona,
            "output_format": agent.output_format,
            "output_schema": agent.output_schema,
            "tags": agent.tags or [],
            "disable_chat": agent.disable_chat,
            "shared_by": shared_by,
            "created_at": agent.created_at.isoformat(),
            "updated_at": agent.updated_at.isoformat(),
            "assignments": [
                {
                    "name": w.name,
                    "description": w.description,
                    "trigger_type": w.trigger_type,
                    "prompt": w.prompt,
                }
                for w in assignments
            ],
            "integration_providers": integration_providers,
            "knowledge_bases": knowledge_bases,
            "mcp_servers": mcp_servers,
            "custom_tools": custom_tools,
            "skills": skills,
            "prompts": prompts,
            "variables": variables_data,
            "builtin_tool_ids": agent.builtin_tool_ids or [],
            "download_omitted_custom_tools": sorted(
                t.name for t in await projection.omitted_personal_custom_tools(db, agent)
            ),
        }
    )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


@router.get("/catalog/prompts")
async def list_public_prompts(
    search: str | None = Query(None),
    tags: str | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(CATALOG_PAGE_SIZE, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    base = select(Prompt).where(Prompt.is_public_catalog.is_(True))
    base = _apply_catalog_filters(base, Prompt, search=search, tags=tags)

    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar() or 0

    result = await db.execute(base.order_by(Prompt.name).offset(offset).limit(limit))
    prompts = result.scalars().all()

    shared_by_map = await _resolve_shared_by(db, "prompt", [p.id for p in prompts])

    return JSONResponse(
        content={
            "items": [
                {
                    "id": str(p.id),
                    "name": p.name,
                    "description": p.description,
                    "tags": p.tags or [],
                    "shared_by": shared_by_map.get(str(p.id)),
                    "created_at": p.created_at.isoformat(),
                }
                for p in prompts
            ],
            "total": total,
        }
    )


@router.get("/catalog/prompts/{prompt_id}")
async def get_public_prompt(
    prompt_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    uid = parse_path_id(prompt_id, not_found_detail="Prompt not found in catalog")
    result = await db.execute(
        select(Prompt).where(Prompt.id == uid, Prompt.is_public_catalog.is_(True))
    )
    prompt = result.scalar_one_or_none()
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found in catalog")

    shared_by = await _resolve_shared_by_single(db, "prompt", prompt.id)

    return JSONResponse(
        content={
            "id": str(prompt.id),
            "name": prompt.name,
            "description": prompt.description,
            "content": prompt.content,
            "tags": prompt.tags or [],
            "shared_by": shared_by,
            "created_at": prompt.created_at.isoformat(),
            "updated_at": prompt.updated_at.isoformat(),
        }
    )


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


@router.get("/catalog/skills")
async def list_public_skills(
    search: str | None = Query(None),
    tags: str | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(CATALOG_PAGE_SIZE, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    base = select(AgentSkill).where(AgentSkill.is_public_catalog.is_(True))
    base = _apply_catalog_filters(base, AgentSkill, search=search, tags=tags)

    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar() or 0

    result = await db.execute(
        base.order_by(AgentSkill.name).offset(offset).limit(limit)
    )
    skills = result.scalars().all()

    shared_by_map = await _resolve_shared_by(db, "agent_skill", [s.id for s in skills])

    return JSONResponse(
        content={
            "items": [
                {
                    "id": str(s.id),
                    "name": s.name,
                    "description": s.description,
                    "url": s.url,
                    "compatibility": s.compatibility,
                    "license": s.license,
                    "tags": s.tags or [],
                    "shared_by": shared_by_map.get(str(s.id)),
                    "created_at": s.created_at.isoformat(),
                }
                for s in skills
            ],
            "total": total,
        }
    )


@router.get("/catalog/skills/{skill_id}")
async def get_public_skill(
    skill_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    uid = parse_path_id(skill_id, not_found_detail="Skill not found in catalog")
    result = await db.execute(
        select(AgentSkill).where(
            AgentSkill.id == uid, AgentSkill.is_public_catalog.is_(True)
        )
    )
    skill = result.scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found in catalog")

    shared_by = await _resolve_shared_by_single(db, "agent_skill", skill.id)

    return JSONResponse(
        content={
            "id": str(skill.id),
            "name": skill.name,
            "description": skill.description,
            "url": skill.url,
            "compatibility": skill.compatibility,
            "license": skill.license,
            "content": skill.content,
            "tags": skill.tags or [],
            "shared_by": shared_by,
            "created_at": skill.created_at.isoformat(),
            "updated_at": skill.updated_at.isoformat(),
        }
    )


# ---------------------------------------------------------------------------
# MCP Servers
# ---------------------------------------------------------------------------


@router.get("/catalog/mcp-servers")
async def list_public_mcp_servers(
    search: str | None = Query(None),
    tags: str | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(CATALOG_PAGE_SIZE, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    base = select(MCPServer).where(MCPServer.is_public_catalog.is_(True))
    base = _apply_catalog_filters(base, MCPServer, search=search, tags=tags)

    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar() or 0

    result = await db.execute(base.order_by(MCPServer.name).offset(offset).limit(limit))
    servers = result.scalars().all()

    shared_by_map = await _resolve_shared_by(db, "mcp_server", [s.id for s in servers])

    return JSONResponse(
        content={
            "items": [
                {
                    "id": str(s.id),
                    "name": s.name,
                    "transport": s.transport,
                    "url": redact_secret_text(s.url),
                    "tags": s.tags or [],
                    "tool_count": (
                        len(s.discovered_tools) if s.discovered_tools else 0
                    ),
                    "shared_by": shared_by_map.get(str(s.id)),
                    "created_at": s.created_at.isoformat(),
                }
                for s in servers
            ],
            "total": total,
        }
    )


@router.get("/catalog/mcp-servers/{server_id}")
async def get_public_mcp_server(
    server_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    uid = parse_path_id(server_id, not_found_detail="MCP server not found in catalog")
    result = await db.execute(
        select(MCPServer).where(
            MCPServer.id == uid, MCPServer.is_public_catalog.is_(True)
        )
    )
    srv = result.scalar_one_or_none()
    if not srv:
        raise HTTPException(status_code=404, detail="MCP server not found in catalog")

    tools = []
    if srv.discovered_tools:
        tools = [
            {"name": t["name"], "description": t.get("description", "")}
            for t in srv.discovered_tools
        ]

    shared_by = await _resolve_shared_by_single(db, "mcp_server", srv.id)

    return JSONResponse(
        content={
            "id": str(srv.id),
            "name": srv.name,
            "description": getattr(srv, "description", None),
            "transport": srv.transport,
            "command": srv.command,
            "args": redact_secret_args(srv.args),
            "url": redact_secret_text(srv.url),
            "tags": srv.tags or [],
            "tools": tools,
            "tool_count": len(tools),
            "shared_by": shared_by,
            "created_at": srv.created_at.isoformat(),
            "updated_at": srv.updated_at.isoformat(),
        }
    )


# ---------------------------------------------------------------------------
# Custom Tools
# ---------------------------------------------------------------------------


@router.get("/catalog/custom-tools")
async def list_public_custom_tools(
    search: str | None = Query(None),
    tags: str | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(CATALOG_PAGE_SIZE, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    base = select(CustomTool).where(CustomTool.is_public_catalog.is_(True))
    base = _apply_catalog_filters(base, CustomTool, search=search, tags=tags)

    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar() or 0

    result = await db.execute(
        base.order_by(CustomTool.name).offset(offset).limit(limit)
    )
    tools = result.scalars().all()

    shared_by_map = await _resolve_shared_by(db, "custom_tool", [t.id for t in tools])

    return JSONResponse(
        content={
            "items": [
                {
                    "id": str(t.id),
                    "name": t.name,
                    "description": t.description,
                    "icon_url": t.icon_url,
                    "tags": t.tags or [],
                    "shared_by": shared_by_map.get(str(t.id)),
                    "created_at": t.created_at.isoformat(),
                }
                for t in tools
            ],
            "total": total,
        }
    )


@router.get("/catalog/custom-tools/{tool_id}")
async def get_public_custom_tool(
    tool_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    uid = parse_path_id(tool_id, not_found_detail="Custom tool not found in catalog")
    result = await db.execute(
        select(CustomTool).where(
            CustomTool.id == uid, CustomTool.is_public_catalog.is_(True)
        )
    )
    tool = result.scalar_one_or_none()
    if not tool:
        raise HTTPException(status_code=404, detail="Custom tool not found in catalog")

    shared_by = await _resolve_shared_by_single(db, "custom_tool", tool.id)

    return JSONResponse(
        content={
            "id": str(tool.id),
            "name": tool.name,
            "description": tool.description,
            "icon_url": tool.icon_url,
            "input_schema": tool.input_schema,
            "js_function": tool.js_function,
            "language": tool.language or "javascript",
            "timeout": tool.timeout or 300,
            "tags": tool.tags or [],
            "shared_by": shared_by,
            "created_at": tool.created_at.isoformat(),
            "updated_at": tool.updated_at.isoformat(),
        }
    )


# ---------------------------------------------------------------------------
# Unified download (JSON export bundle)
# ---------------------------------------------------------------------------


@router.get("/catalog/{entity_type}/{entity_id}/download")
async def download_catalog_item(
    entity_type: str,
    entity_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Download a catalog item as a JSON export bundle."""
    uid = _catalog_entity_uuid(entity_type, entity_id, _DOWNLOAD_NOT_FOUND)
    now = datetime.now(UTC).isoformat()

    if entity_type == "agents":
        result = await db.execute(
            select(Agent).where(Agent.id == uid, Agent.is_public_catalog.is_(True))
        )
        agent = result.scalar_one_or_none()
        if not agent:
            raise HTTPException(status_code=404, detail="Not found")

        # The catalog download IS an export bundle: built by the one builder
        # ``GET /agents/{id}/export`` uses, so what travels is decided in ONE
        # place (the agent section by the bundle manifest) instead of a
        # hand-written copy that drifted - it had lost the activation gate,
        # the tool deny list, the tool scopes and the approval flags.
        # Projected for a reader, like an export by a non-manager.
        bundle = await projection.build_reader_agent_bundle(
            db,
            agent,
            user,
            await get_user_group_ids(db, user.id),
            variable_values=True,
        )
        safe_name = agent.name.lower().replace(" ", "-")
        return JSONResponse(
            content=bundle,
            headers={
                "Content-Disposition": build_content_disposition(
                    f"agent-{safe_name}.json"
                )
            },
        )

    elif entity_type == "prompts":
        result = await db.execute(
            select(Prompt).where(Prompt.id == uid, Prompt.is_public_catalog.is_(True))
        )
        prompt = result.scalar_one_or_none()
        if not prompt:
            raise HTTPException(status_code=404, detail="Not found")

        bundle = {
            "version": 1,
            "export_type": "prompt",
            "exported_at": now,
            "prompt": {
                "name": prompt.name,
                "description": prompt.description,
                "content": prompt.content,
                "tags": prompt.tags or [],
            },
        }
        safe_name = prompt.name.lower().replace(" ", "-")
        return JSONResponse(
            content=bundle,
            headers={
                "Content-Disposition": build_content_disposition(
                    f"prompt-{safe_name}.json"
                )
            },
        )

    elif entity_type == "skills":
        result = await db.execute(
            select(AgentSkill).where(
                AgentSkill.id == uid, AgentSkill.is_public_catalog.is_(True)
            )
        )
        skill = result.scalar_one_or_none()
        if not skill:
            raise HTTPException(status_code=404, detail="Not found")

        bundle = {
            "version": 1,
            "export_type": "skill",
            "exported_at": now,
            "skill": {
                "url": skill.url,
                "name": skill.name,
                "description": skill.description,
                "compatibility": skill.compatibility,
                "license": skill.license,
                "content": skill.content,
                "tags": skill.tags or [],
            },
        }
        safe_name = skill.name.lower().replace(" ", "-")
        return JSONResponse(
            content=bundle,
            headers={
                "Content-Disposition": build_content_disposition(
                    f"skill-{safe_name}.json"
                )
            },
        )

    elif entity_type == "mcp-servers":
        result = await db.execute(
            select(MCPServer).where(
                MCPServer.id == uid, MCPServer.is_public_catalog.is_(True)
            )
        )
        srv = result.scalar_one_or_none()
        if not srv:
            raise HTTPException(status_code=404, detail="Not found")

        bundle = {
            "version": 1,
            "export_type": "mcp_server",
            "exported_at": now,
            "mcp_server": {
                "name": srv.name,
                "transport": srv.transport,
                "command": srv.command,
                "args": redact_secret_args(srv.args),
                "url": redact_secret_text(srv.url),
                "tags": srv.tags or [],
            },
        }
        safe_name = srv.name.lower().replace(" ", "-")
        return JSONResponse(
            content=bundle,
            headers={
                "Content-Disposition": build_content_disposition(
                    f"mcp-server-{safe_name}.json"
                )
            },
        )

    elif entity_type == "custom-tools":
        result = await db.execute(
            select(CustomTool).where(
                CustomTool.id == uid, CustomTool.is_public_catalog.is_(True)
            )
        )
        tool = result.scalar_one_or_none()
        if not tool:
            raise HTTPException(status_code=404, detail="Not found")

        bundle = {
            "version": 1,
            "export_type": "custom_tool",
            "exported_at": now,
            "custom_tool": {
                "name": tool.name,
                "description": tool.description,
                "icon_url": tool.icon_url,
                "input_schema": tool.input_schema,
                "js_function": tool.js_function,
                # Same shape as the owner's own export: without them a Python
                # tool re-imports as JavaScript with the default timeout.
                "language": tool.language or "javascript",
                "timeout": tool.timeout or 300,
                "tags": tool.tags or [],
            },
        }
        safe_name = tool.name.lower().replace(" ", "-")
        return JSONResponse(
            content=bundle,
            headers={
                "Content-Disposition": build_content_disposition(
                    f"custom-tool-{safe_name}.json"
                )
            },
        )

    else:
        raise HTTPException(
            status_code=400, detail=f"Unknown entity type: {entity_type}"
        )


# ---------------------------------------------------------------------------
# Add to account (authenticated, with setup configuration)
# ---------------------------------------------------------------------------


class AgentSetupConfig(BaseModel):
    """Configuration provided during agent catalog installation."""

    name: str | None = None
    #: Per provider, the installer's own integration to bind. The install
    #: wizard sends a list; a bare id is accepted too.
    integration_bindings: dict[str, str | list[str]] | None = None


class AddToAccountResponse(BaseModel):
    id: str
    name: str
    entity_type: str
    #: The variables an agent install created without a value: a
    #: secret is never copied, so the installer must fill it in.
    variables_needing_value: list[VariableNeedingValueResponse] = []
    #: What an agent install did not copy, or copied only in part, and why -
    #: one sentence per kind, counts only, never a name.
    warnings: list[str] = []


def _plural(count: int, singular: str) -> str:
    return f"{count} {singular}{'' if count == 1 else 's'}"


#: How an agent install says what it left out: per kind, the singular label
#: and the rest of the sentence (singular form, plural form).
_INSTALL_LEFT_OUT: dict[str, tuple[str, str, str]] = {
    "integration_ids": (
        "integration",
        "was not connected: it is not shared with you. Connect your own in the copy.",
        "were not connected: they are not shared with you. Connect your own in the copy.",
    ),
    "mcp_server_ids": (
        "MCP server",
        "was not copied: it is not shared with you.",
        "were not copied: they are not shared with you.",
    ),
    "mcp_servers_without_credentials": (
        "MCP server",
        "was copied as your own without any credential: add them in the copy if it needs them.",
        "were copied as your own without any credential: add them in the copies that need them.",
    ),
    "knowledge_bases_recreated": (
        "knowledge base",
        "was copied as your own, empty: add its documents.",
        "were copied as your own, empty: add their documents.",
    ),
    "custom_tool_ids": (
        "custom tool",
        "was not copied: it is not shared with you.",
        "were not copied: they are not shared with you.",
    ),
    "skill_ids": (
        "skill",
        "was not copied: it is not shared with you.",
        "were not copied: they are not shared with you.",
    ),
    "prompt_ids": (
        "prompt",
        "was not copied: it is not shared with you.",
        "were not copied: they are not shared with you.",
    ),
    "handover_agent_ids": (
        "handover agent",
        "was not copied: a catalog copy never carries handovers.",
        "were not copied: a catalog copy never carries handovers.",
    ),
    "channel_config": (
        "channel setting",
        "was not copied: a catalog copy never carries channel bots.",
        "were not copied: a catalog copy never carries channel bots.",
    ),
    "assignment_tool_sources": (
        "assignment MCP server",
        "was not copied: it is not one of the agent's servers, so the assignment runs without it.",
        "were not copied: they are not among the agent's servers, so the assignments run without them.",
    ),
    "assignment_integration_bindings": (
        "assignment integration binding",
        "was not copied: it is not shared with you.",
        "were not copied: they are not shared with you.",
    ),
}


def _install_warnings(left_out: dict[str, int]) -> list[str]:
    """One sentence per kind of binding an agent install left out or copied
    only in part - counts, never an id or a name."""
    return [
        f"{_plural(count, label)} of the agent {singular if count == 1 else plural}"
        for kind, (label, singular, plural) in _INSTALL_LEFT_OUT.items()
        if (count := left_out.get(kind, 0))
    ]


@router.post(
    "/catalog/{entity_type}/{entity_id}/add", response_model=AddToAccountResponse
)
async def add_catalog_item_to_account(
    entity_type: str,
    entity_id: str,
    body: AgentSetupConfig | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Clone a catalog item into the current user's account.

    For agents, accepts an optional setup body with name and integration
    bindings; an integration named there must be one the installer may use
    (400 otherwise). "Bindable" below means company-managed AND visible to the
    installer (``_bindable_ids``); "narrowed" means company-managed but not
    visible to them (``access_restricted``). Bindable custom tools are
    automatically bound; the others are skipped. Non-bindable KBs are
    recreated as empty shells (documents must be added manually). MCP
    servers, skills and prompts: bindable ones are bound directly, private
    ones are cloned as the installer's copies (an MCP server without its
    secrets, a credential in its arguments or URL masked), narrowed ones are
    left out - their content is not the installer's to read.

    Agents and their assignments are copied through the catalog manifests in
    ``app/models/agent_copy.py``: every narrowing and safety field
    travels, references are re-keyed onto what the installer ends up with, and
    the publisher's channel bots, sharing grants, run-as identity, handover
    targets, trigger rows and public web credentials never do.
    """
    from app.core.utils import fit_copy_name, text_column_error

    uid = _catalog_entity_uuid(entity_type, entity_id, _ADD_NOT_FOUND)
    config = body or AgentSetupConfig()
    # The installer's own name for the copy is kept as typed, so one its column
    # cannot hold is refused rather than shortened.
    copy_model = {
        "agents": Agent,
        "prompts": Prompt,
        "skills": AgentSkill,
        "mcp-servers": MCPServer,
        "custom-tools": CustomTool,
    }[entity_type]
    if config.name and (name_problem := text_column_error(copy_model.__table__.c.name, config.name)):
        raise HTTPException(status_code=400, detail=name_problem)

    if entity_type == "agents":
        from app.models.variable import Variable as VariableModel
        from app.services.agent_bindings import validate_personal_agent_bindings
        from app.services.llm_provider_deletion import (
            lock_provider_configs,
            provider_reference_problem,
        )
        from app.services.variable_resolver import rewrite_variable_ids

        # The copy carries the source's model choice, ``llm_provider_id``
        # included: read it under the provider config lock, like a duplicate.
        await lock_provider_configs(db, shared=True)
        result = await db.execute(
            select(Agent).where(Agent.id == uid, Agent.is_public_catalog.is_(True))
        )
        source = result.scalar_one_or_none()
        if not source:
            raise HTTPException(status_code=404, detail="Agent not found in catalog")
        # A pin to a config that no longer exists, or that the installer may
        # not use, is not copied: the copy keeps the model, which the
        # router resolves by its owner as it does for the source.
        copied_provider_id = source.llm_provider_id
        if copied_provider_id is not None and await provider_reference_problem(
            db,
            copied_provider_id,
            stored=None,
            label="the copied model",
            user_id=user.id,
            scoped=not user.is_admin,
        ):
            copied_provider_id = None

        user_group_ids = await get_user_group_ids(db, user.id)

        # The installer's own integration choice, refused unless the installer
        # may use it - the gate agent create / update apply - before anything
        # is written: a crafted body naming another user's integration would
        # otherwise bind their credentials to the clone, and assignment runs
        # use a fixed binding without re-checking who owns it.
        chosen_bindings = _setup_integration_bindings(config.integration_bindings)
        if chosen_bindings:
            await validate_personal_agent_bindings(
                db,
                user.id,
                user_group_ids,
                is_admin=bool(getattr(user, "is_admin", False)),
                integration_bindings=chosen_bindings,
                mcp_server_ids=None,
                knowledge_base_ids=None,
                handover_agent_ids=None,
                custom_tool_ids=None,
                skill_ids=None,
                prompt_ids=None,
            )

        async def _bindable(model: Any, raw_ids: Any) -> set[str]:
            return await projection.bindable_ids(db, model, raw_ids, user, user_group_ids)

        # What the install leaves out or copies only in part, per kind: said in
        # the response (``_install_warnings``), never a silent drop.
        left_out: dict[str, int] = {}

        def _count(kind: str, n: int = 1) -> None:
            if n:
                left_out[kind] = left_out.get(kind, 0) + n

        # The prompts and skills the clone copies - the private ones; bindable
        # ones are bound as is and narrowed ones left out: their content is
        # the installer's own from here on, so it is rewritten onto the cloned
        # variables like the agent's text.
        bindable_skills = await _bindable(AgentSkill, source.skill_ids)
        bindable_prompts = await _bindable(Prompt, source.prompt_ids)
        copied_texts: list[str] = []
        for model, raw_ids, bindable in (
            (AgentSkill, source.skill_ids, bindable_skills),
            (Prompt, source.prompt_ids, bindable_prompts),
        ):
            if uuids := _safe_uuids(raw_ids):
                rows = await db.execute(select(model).where(model.id.in_(uuids)))
                copied_texts.extend(
                    row.content or ""
                    for row in rows.scalars().all()
                    if str(row.id) not in bindable and not row.is_company_managed
                )

        # Clone referenced variables - only those the agent's owner can
        # resolve: a persona may name any id, and the catalog is not a way to
        # copy someone else's variable.
        src_texts = [source.persona or "", source.guardrails or "", *copied_texts]
        wf_result_0 = await db.execute(
            select(Assignment).where(Assignment.agent_id == source.id)
        )
        src_assignments = list(wf_result_0.scalars().all())
        for w in src_assignments:
            src_texts.extend([w.prompt or "", w.activation_prompt or ""])

        ref_ids = sorted(set(v for t in src_texts for v in extract_variable_ids(t)))
        _var_id_map: dict[str, str] = {}
        needing_value: list[VariableModel] = []
        if ref_ids:
            src_vars = await load_owner_visible_variable_records(
                db, ref_ids, source.user_id
            )
            for sv in src_vars:
                nv = VariableModel(
                    user_id=user.id,
                    name=sv.name,
                    value=sv.value if not sv.is_secret else None,
                    is_secret=sv.is_secret,
                    description=sv.description,
                    tags=sv.tags or [],
                    is_company_managed=False,
                )
                db.add(nv)
                await db.flush()
                _var_id_map[str(sv.id).lower()] = str(nv.id)
                if not variable_has_value(nv):
                    needing_value.append(nv)

        def _rw(text: str | None) -> str | None:
            if not text or not _var_id_map:
                return text
            return rewrite_variable_ids(text, _var_id_map)

        # Bind the source's integrations the installer may use (company-managed
        # and visible to them), then the installer's own validated choice wins.
        final_bindings: dict[str, list[str]] = {}
        if source.integration_bindings:
            bindable_integrations = await _bindable(
                Integration, _integration_ids(source.integration_bindings)
            )
            for prov_key, int_ids in source.integration_bindings.items():
                raw_ids = int_ids if isinstance(int_ids, list) else [int_ids]
                kept = [
                    canonical
                    for raw_id in raw_ids
                    if (canonical := _canonical_uuid(raw_id)) in bindable_integrations
                ]
                if kept:
                    final_bindings[prov_key] = kept
                # The installer's own choice for the provider replaces it.
                if prov_key not in chosen_bindings:
                    _count("integration_ids", len([r for r in raw_ids if r]) - len(kept))

        for prov_key, int_ids in chosen_bindings.items():
            final_bindings[prov_key] = [str(_uuid_mod.UUID(i)) for i in int_ids]

        # Recreate KBs (bindable ones are bound; others are cloned as empty shells).
        # ``kb_id_map`` (source id -> clone's id) re-keys everything that names a
        # KB by id: the agent's write grants and the assignments' KB refinements.
        final_kb_ids: list[str] = []
        kb_id_map: dict[str, str] = {}
        recreated_kb_ids: set[str] = set()
        if source.knowledge_base_ids:
            bindable_kbs = await _bindable(KnowledgeBase, source.knowledge_base_ids)
            for kid in source.knowledge_base_ids:
                uuids = _safe_uuids([kid])
                if uuids:
                    kr = await db.execute(
                        select(KnowledgeBase).where(KnowledgeBase.id == uuids[0])
                    )
                    kb = kr.scalar_one_or_none()
                    if kb:
                        if str(kb.id) in bindable_kbs:
                            new_kb_id = str(kb.id)
                        else:
                            clone_kb = KnowledgeBase(
                                user_id=user.id,
                                name=kb.name,
                                description=kb.description,
                                tags=kb.tags or [],
                                is_company_managed=False,
                            )
                            db.add(clone_kb)
                            await db.flush()
                            new_kb_id = str(clone_kb.id)
                            recreated_kb_ids.add(new_kb_id)
                        final_kb_ids.append(new_kb_id)
                        kb_id_map[str(kid)] = new_kb_id
                        kb_id_map[str(kb.id)] = new_kb_id
        _count("knowledge_bases_recreated", len(recreated_kb_ids))

        # MCP servers: bindable ones are bound, a private one is cloned as the
        # installer's copy without secrets, a narrowed one is left out.
        final_mcp_ids: list[str] = []
        mcp_id_map: dict[str, str] = {}
        if source.mcp_server_ids:
            bindable_servers = await _bindable(MCPServer, source.mcp_server_ids)
            for mid in source.mcp_server_ids:
                uuids = _safe_uuids([mid])
                if uuids:
                    mr = await db.execute(
                        select(MCPServer).where(MCPServer.id == uuids[0])
                    )
                    srv = mr.scalar_one_or_none()
                    if srv:
                        if str(srv.id) in bindable_servers:
                            final_mcp_ids.append(str(srv.id))
                            mcp_id_map[str(mid)] = str(srv.id)
                            mcp_id_map[str(srv.id)] = str(srv.id)
                        elif not srv.is_company_managed:
                            clone_srv = MCPServer(
                                user_id=user.id,
                                name=srv.name,
                                transport=srv.transport,
                                command=srv.command,
                                args=redact_secret_args(srv.args),
                                url=redact_secret_text(srv.url),
                                tags=srv.tags or [],
                                is_company_managed=False,
                                enabled=True,
                            )
                            db.add(clone_srv)
                            await db.flush()
                            final_mcp_ids.append(str(clone_srv.id))
                            mcp_id_map[str(mid)] = str(clone_srv.id)
                            mcp_id_map[str(srv.id)] = str(clone_srv.id)
                            _count("mcp_servers_without_credentials")
                        else:
                            _count("mcp_server_ids")

        # Auto-bind the bindable custom tools; any other is skipped
        final_tool_ids: list[str] = []
        if source.custom_tool_ids:
            bindable_tools = await _bindable(CustomTool, source.custom_tool_ids)
            for tid in source.custom_tool_ids:
                uuids = _safe_uuids([tid])
                if uuids:
                    tr = await db.execute(
                        select(CustomTool).where(CustomTool.id == uuids[0])
                    )
                    ct = tr.scalar_one_or_none()
                    if ct and str(ct.id) in bindable_tools:
                        final_tool_ids.append(str(ct.id))
                    elif ct:
                        _count("custom_tool_ids")

        # Skills: bindable ones are bound, a private one is cloned as the
        # installer's copy, a narrowed one is left out.
        final_skill_ids: list[str] = []
        skill_id_map: dict[str, str] = {}
        if source.skill_ids:
            for sid in source.skill_ids:
                uuids = _safe_uuids([sid])
                if uuids:
                    sr = await db.execute(
                        select(AgentSkill).where(AgentSkill.id == uuids[0])
                    )
                    sk = sr.scalar_one_or_none()
                    if sk:
                        if str(sk.id) in bindable_skills:
                            final_skill_ids.append(str(sk.id))
                            skill_id_map[str(sid)] = str(sk.id)
                            skill_id_map[str(sk.id)] = str(sk.id)
                        elif not sk.is_company_managed:
                            clone_sk = AgentSkill(
                                user_id=user.id,
                                url=sk.url,
                                name=sk.name,
                                description=sk.description,
                                compatibility=sk.compatibility,
                                license=sk.license,
                                content=_rw(sk.content),
                                fetched_at=sk.fetched_at,
                                tags=sk.tags or [],
                                is_company_managed=False,
                            )
                            db.add(clone_sk)
                            await db.flush()
                            await copy_skill_resources(db, sk.id, clone_sk.id)
                            final_skill_ids.append(str(clone_sk.id))
                            skill_id_map[str(sid)] = str(clone_sk.id)
                            skill_id_map[str(sk.id)] = str(clone_sk.id)
                        else:
                            _count("skill_ids")

        # Prompts: bindable ones are bound, a private one is cloned as the
        # installer's copy, a narrowed one is left out.
        final_prompt_ids: list[str] = []
        if source.prompt_ids:
            for pid in source.prompt_ids:
                uuids = _safe_uuids([pid])
                if uuids:
                    pr = await db.execute(select(Prompt).where(Prompt.id == uuids[0]))
                    p = pr.scalar_one_or_none()
                    if p:
                        if str(p.id) in bindable_prompts:
                            final_prompt_ids.append(str(p.id))
                        elif not p.is_company_managed:
                            clone_p = Prompt(
                                user_id=user.id,
                                name=p.name,
                                description=p.description,
                                content=_rw(p.content),
                                tags=p.tags or [],
                                is_company_managed=False,
                            )
                            db.add(clone_p)
                            await db.flush()
                            final_prompt_ids.append(str(clone_p.id))
                        else:
                            _count("prompt_ids")

        agent_name = config.name or fit_copy_name(source.name, Agent.__table__.c.name, suffix=" (copy)")

        # Never carried by a catalog copy (``AGENT_CATALOG_CLONE_SKIP_FIELDS``).
        _count("handover_agent_ids", len([aid for aid in source.handover_agent_ids or [] if aid]))
        _count(
            "channel_config",
            len(
                [
                    cfg
                    for cfg in (source.channel_config or {}).values()
                    if isinstance(cfg, dict) and cfg.get("channel_bot_id")
                ]
            ),
        )

        # Write grants travel only onto the KBs recreated for the installer: a
        # write grant onto a bound company-managed KB is not inherited (see
        # ``AGENT_CATALOG_CLONE_SKIP_FIELDS``).
        kb_permissions = {
            new_id: level
            for old_id, level in (source.knowledge_base_permissions or {}).items()
            if (new_id := kb_id_map.get(str(old_id))) in recreated_kb_ids
        }

        # Manifest-driven: every column is either carried verbatim
        # (``AGENT_CATALOG_CLONE_FIELDS`` - every narrowing and safety field
        # included) or skipped with a stated reason; the overrides below are
        # the identity and the re-keyed references.
        clone = Agent(
            **build_agent_catalog_clone_kwargs(
                source,
                user_id=user.id,
                name=agent_name,
                slug=await generate_unique_agent_slug(db, agent_name),
                llm_provider_id=copied_provider_id,
                persona=_rw(source.persona),
                guardrails=_rw(source.guardrails),
                max_iterations=(
                    clamp_max_iterations(source.max_iterations)
                    if source.max_iterations is not None
                    else None
                ),
                # The publisher typed this budget, not the installer: carried
                # at the installer's ceiling, like a duplicate or an import.
                budget_config=lower_budget_to_ceiling(source.budget_config, user=user),
                available_channel_types=["web"],
                is_company_managed=False,
                integration_bindings=final_bindings or None,
                integration_read_only=carried_read_only(
                    source.integration_read_only,
                    final_bindings,
                    replaced_providers=set(chosen_bindings),
                ),
                knowledge_base_ids=final_kb_ids or None,
                knowledge_base_permissions=kb_permissions or None,
                mcp_server_ids=final_mcp_ids or None,
                mcp_allowed_tools=rekey_id_map(source.mcp_allowed_tools, mcp_id_map),
                custom_tool_ids=final_tool_ids or None,
                skill_ids=final_skill_ids or None,
                skill_load_modes=rekey_id_map(source.skill_load_modes, skill_id_map),
                prompt_ids=final_prompt_ids or None,
                # The scopes, never the publisher's agents they list.
                coordination_access=portable_coordination_access(
                    source.coordination_access
                ),
            )
        )
        db.add(clone)
        await db.flush()

        # Company-managed skills are bound by reference (not cloned), so the ones
        # that ship packaged resources must hand their Custom Tools / KBs to the
        # clone too — otherwise the installed agent has instructions pointing at
        # references it cannot search.
        await reconcile_agent_skill_package_bindings(db, clone)
        await _seed_initial_versions(db, agent=clone, author_id=user.id)

        cloned_assignments: dict[_uuid_mod.UUID, tuple[Assignment, Assignment]] = {}
        for wf in src_assignments:
            target_kb = (
                kb_id_map.get(str(wf.target_knowledge_base_id))
                if wf.target_knowledge_base_id
                else None
            )
            clone_tool_sources = _rekey_tool_sources(wf.tool_sources, mcp_id_map)
            clone_integration_bindings = _rekey_assignment_integration_bindings(
                wf.integration_bindings, final_bindings, chosen_bindings
            )
            _count(
                "assignment_tool_sources",
                _unmapped_tool_sources(wf.tool_sources, mcp_id_map),
            )
            _count(
                "assignment_integration_bindings",
                _dropped_assignment_integrations(
                    wf.integration_bindings, clone_integration_bindings, chosen_bindings
                ),
            )
            clone_wf = Assignment(
                **build_assignment_catalog_clone_kwargs(
                    wf,
                    user_id=user.id,
                    agent_id=clone.id,
                    # Never the publisher's live webhook signing secret (SEC-5)
                    # nor an id of their rows: the polled integration and the
                    # drained work-item queue follow the clone.
                    trigger_config=_clone_trigger_config(
                        wf.trigger_type, wf.trigger_config, final_bindings
                    ),
                    prompt=_rw(wf.prompt),
                    activation_prompt=_rw(wf.activation_prompt),
                    max_iterations=(
                        clamp_max_iterations(wf.max_iterations)
                        if wf.max_iterations is not None
                        else None
                    ),
                    tool_sources=clone_tool_sources,
                    integration_bindings=clone_integration_bindings,
                    # Read has no "none" state: an empty selection is NULL.
                    read_knowledge_base_ids=(
                        rekey_id_list(wf.read_knowledge_base_ids, kb_id_map) or None
                    ),
                    # Write is tri-state (NULL inherit / [] none / subset):
                    # a subset that maps to nothing stays "none" (fail-closed).
                    write_knowledge_base_ids=rekey_id_list(
                        wf.write_knowledge_base_ids, kb_id_map
                    ),
                    target_knowledge_base_id=(
                        _uuid_mod.UUID(target_kb) if target_kb else None
                    ),
                    enabled=False,
                )
            )
            # Explain the "Paused" badge on a catalog install.
            pause_assignment(
                clone_wf,
                reason="created_disabled",
                details=(
                    f"Created switched off: installed from the catalog agent "
                    f"'{source.name}'. Review the configuration, then enable it."
                ),
                actor_user_id=user.id,
                actor_label=actor_display_name(user),
            )
            db.add(clone_wf)
            cloned_assignments[wf.id] = (wf, clone_wf)

        # Re-point chains at the cloned counterparts; a target outside the
        # cloned set is dropped rather than wiring the clone into the
        # publisher's pipeline.
        if any(src.next_assignment_id for src, _ in cloned_assignments.values()):
            await db.flush()
            for src_wf, new_wf in cloned_assignments.values():
                chained = cloned_assignments.get(src_wf.next_assignment_id)
                new_wf.next_assignment_id = chained[1].id if chained else None

        await log_audit(
            db,
            user=user,
            action="import",
            entity_type="agent",
            entity_id=clone.id,
            entity_name=clone.name,
            summary=f"Installed agent '{source.name}' from catalog",
            details={"bindings_not_copied": left_out} if left_out else None,
        )
        await db.commit()
        return AddToAccountResponse(
            id=str(clone.id),
            name=clone.name,
            entity_type="agents",
            variables_needing_value=[
                VariableNeedingValueResponse(id=str(v.id), name=v.name, is_secret=v.is_secret)
                for v in sorted(needing_value, key=lambda v: v.name)
            ],
            warnings=_install_warnings(left_out),
        )

    elif entity_type == "prompts":
        result = await db.execute(
            select(Prompt).where(Prompt.id == uid, Prompt.is_public_catalog.is_(True))
        )
        source = result.scalar_one_or_none()
        if not source:
            raise HTTPException(status_code=404, detail="Prompt not found in catalog")

        clone = Prompt(
            user_id=user.id,
            name=config.name or fit_copy_name(source.name, Prompt.__table__.c.name, suffix=" (copy)"),
            description=source.description,
            content=source.content,
            tags=source.tags or [],
            is_company_managed=False,
        )
        db.add(clone)
        await db.flush()
        await log_audit(
            db,
            user=user,
            action="import",
            entity_type="prompt",
            entity_id=clone.id,
            entity_name=clone.name,
            summary=f"Installed prompt '{source.name}' from catalog",
        )
        await db.commit()
        return AddToAccountResponse(
            id=str(clone.id), name=clone.name, entity_type="prompts"
        )

    elif entity_type == "skills":
        result = await db.execute(
            select(AgentSkill).where(
                AgentSkill.id == uid, AgentSkill.is_public_catalog.is_(True)
            )
        )
        source = result.scalar_one_or_none()
        if not source:
            raise HTTPException(status_code=404, detail="Skill not found in catalog")

        clone = AgentSkill(
            user_id=user.id,
            url=source.url,
            name=config.name or fit_copy_name(source.name, AgentSkill.__table__.c.name, suffix=" (copy)"),
            description=source.description,
            compatibility=source.compatibility,
            license=source.license,
            content=source.content,
            fetched_at=source.fetched_at,
            tags=source.tags or [],
            is_company_managed=False,
        )
        db.add(clone)
        await db.flush()
        await copy_skill_resources(db, source.id, clone.id)
        await log_audit(
            db,
            user=user,
            action="import",
            entity_type="agent_skill",
            entity_id=clone.id,
            entity_name=clone.name,
            summary=f"Installed skill '{source.name}' from catalog",
        )
        await db.commit()
        return AddToAccountResponse(
            id=str(clone.id), name=clone.name, entity_type="skills"
        )

    elif entity_type == "mcp-servers":
        result = await db.execute(
            select(MCPServer).where(
                MCPServer.id == uid, MCPServer.is_public_catalog.is_(True)
            )
        )
        source = result.scalar_one_or_none()
        if not source:
            raise HTTPException(
                status_code=404, detail="MCP server not found in catalog"
            )

        clone = MCPServer(
            user_id=user.id,
            name=config.name or fit_copy_name(source.name, MCPServer.__table__.c.name, suffix=" (copy)"),
            transport=source.transport,
            command=source.command,
            # Never the publisher's credential: the installer's copy is
            # theirs to read back (its export is raw).
            args=redact_secret_args(source.args),
            url=redact_secret_text(source.url),
            tags=source.tags or [],
            is_company_managed=False,
        )
        db.add(clone)
        await db.flush()
        await log_audit(
            db,
            user=user,
            action="import",
            entity_type="mcp_server",
            entity_id=clone.id,
            entity_name=clone.name,
            summary=f"Installed MCP server '{source.name}' from catalog",
        )
        await db.commit()
        return AddToAccountResponse(
            id=str(clone.id), name=clone.name, entity_type="mcp-servers"
        )

    elif entity_type == "custom-tools":
        result = await db.execute(
            select(CustomTool).where(
                CustomTool.id == uid, CustomTool.is_public_catalog.is_(True)
            )
        )
        source = result.scalar_one_or_none()
        if not source:
            raise HTTPException(
                status_code=404, detail="Custom tool not found in catalog"
            )

        clone = CustomTool(
            user_id=user.id,
            name=config.name or fit_copy_name(source.name, CustomTool.__table__.c.name, suffix=" (copy)"),
            description=source.description,
            icon_url=source.icon_url,
            input_schema=source.input_schema,
            js_function=source.js_function,
            # Without it a Python tool would install as JavaScript.
            language=source.language or "javascript",
            tags=source.tags or [],
            timeout=source.timeout or 300,
            is_company_managed=False,
        )
        db.add(clone)
        await db.flush()
        await log_audit(
            db,
            user=user,
            action="import",
            entity_type="custom_tool",
            entity_id=clone.id,
            entity_name=clone.name,
            summary=f"Installed custom tool '{source.name}' from catalog",
        )
        await db.commit()
        return AddToAccountResponse(
            id=str(clone.id), name=clone.name, entity_type="custom-tools"
        )

    else:
        raise HTTPException(
            status_code=400, detail=f"Unknown entity type: {entity_type}"
        )
