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

"""The tools an administrator marked "always ask" that an evaluated agent carries.

An evaluation whose tool approval setting is off runs every tool call without
approval, those of the tools an administrator re-gated included. Its
configuration names them, per agent, so the author chooses knowingly.

An agent keeps no list of its tools: a turn builds them, connecting MCP servers
and loading credentials. This module reads the names from the same columns the
turn reads, without running one, and matches them by exact name against the
marks (``resolve_vetoed``), which is how the gate compares a call
(``tools/approval.py``: ``tool_key in vetoed``). So the warning is advisory, and
says so. It cannot see:

- an MCP server never discovered, or a tool added since its last discovery;
- tools a turn adds at run time (studio, desktop, deck tools, session or
  flow-wired tools);
- an agent bound to several integrations of one provider, whose tools are
  renamed ``name__slug`` at run time: the warning names the base tool, which
  errs on the side of warning.

It can also name a tool a turn would not offer: an agent with handover peers is
read as carrying every handover tool, whatever its handover types.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Tools a turn adds from an agent's columns rather than from a suite id.
_KNOWLEDGE_TOOLS = ("search_knowledge", "list_knowledge_documents", "get_knowledge_document")
_KNOWLEDGE_WRITE_TOOLS = ("store_knowledge", "update_knowledge_document")
_MEMORY_TOOLS = ("store_memory", "search_memory")
_HANDOVER_TOOLS = (
    "consult_agent",
    "transfer_to_agent",
    "spawn_background_task",
    "check_task_status",
    "get_task_result",
    "get_full_task_result",
    "list_background_tasks",
    "cancel_background_task",
    "collect_task_results",
    "clear_collected",
)
_SKILL_TOOLS = ("load_skill", "read_skill_resource")


def _allowed(names: Iterable[str], allow_list: object) -> list[str]:
    """``names`` narrowed by an allow list. No list (``None``, or a value that
    is not a list) allows all: the warning errs on the side of warning."""
    if not isinstance(allow_list, list):
        return list(names)
    allowed = {str(n) for n in allow_list}
    return [n for n in names if n in allowed]


def _bound_providers(agent: Any) -> list[str]:
    bindings = getattr(agent, "integration_bindings", None) or {}
    providers = [str(p) for p in bindings] if isinstance(bindings, Mapping) else []
    providers += [str(p) for p in (getattr(agent, "integration_template_bindings", None) or [])]
    return sorted(set(providers))


def agent_tool_names(
    agent: Any,
    *,
    custom_tool_names: Iterable[str],
    mcp_servers: Iterable[Any],
    provider_tool_names: Mapping[str, Iterable[str]],
) -> set[str]:
    """The tool names *agent* carries, from its columns and the rows they name."""
    from app.tools.builtin_suites import builtin_suite_tools

    names: set[str] = set()
    for suite in getattr(agent, "builtin_tool_ids", None) or []:
        names.update(builtin_suite_tools(str(suite)))
    names.update(str(n) for n in custom_tool_names)

    mcp_allowed = getattr(agent, "mcp_allowed_tools", None) or {}
    for server in mcp_servers:
        discovered = [
            str(t["name"])
            for t in (getattr(server, "discovered_tools", None) or [])
            if isinstance(t, Mapping) and t.get("name")
        ]
        names.update(_allowed(discovered, mcp_allowed.get(str(server.id)) if isinstance(mcp_allowed, Mapping) else None))

    integration_allowed = getattr(agent, "integration_allowed_tools", None) or {}
    for provider, provider_names in provider_tool_names.items():
        allow_list = integration_allowed.get(provider) if isinstance(integration_allowed, Mapping) else None
        names.update(_allowed(provider_names, allow_list))

    if getattr(agent, "knowledge_base_ids", None):
        names.update(_KNOWLEDGE_TOOLS)
        permissions = getattr(agent, "knowledge_base_permissions", None) or {}
        if isinstance(permissions, Mapping) and "write" in permissions.values():
            names.update(_KNOWLEDGE_WRITE_TOOLS)
    if getattr(agent, "memory_persistent_enabled", False):
        names.update(_MEMORY_TOOLS)
    if getattr(agent, "handover_agent_ids", None):
        names.update(_HANDOVER_TOOLS)
    if getattr(agent, "skill_ids", None):
        names.update(_SKILL_TOOLS)

    return names - {str(n) for n in (getattr(agent, "tool_deny_list", None) or [])}


@functools.cache
def _one_provider_tool_names(provider: str) -> tuple[str, ...]:
    """A provider's tool names, built with a placeholder token, as the catalog does.

    Cached: the build only depends on the provider. Empty for an unknown
    provider, or one whose builder needs more than a token (the warning is
    advisory).
    """
    from app.api.v1.integration_catalog import build_dummy_token
    from app.services.integration_tools import PROVIDER_TOOLS
    from app.tools.base import CallableToolExecutor

    builder = PROVIDER_TOOLS.get(provider)
    if builder is None:
        return ()
    try:
        defs = builder(build_dummy_token(provider), CallableToolExecutor(), refresh_fn=lambda: None)
    except Exception:
        logger.debug("Could not list %s's tools for the always-ask warning", provider, exc_info=True)
        return ()
    return tuple(d.name for d in defs)


def _provider_tool_names(providers: Iterable[str]) -> dict[str, list[str]]:
    """Each provider's tool names, the providers with none left out."""
    result: dict[str, list[str]] = {}
    for provider in providers:
        names = _one_provider_tool_names(provider)
        if names:
            result[provider] = list(names)
    return result


def _uuids(values: Iterable[object]) -> list[uuid.UUID]:
    ids = []
    for value in values:
        try:
            ids.append(uuid.UUID(str(value)))
        except ValueError:
            continue
    return ids


async def load_agent_tool_names(db: AsyncSession, agent: Any) -> set[str]:
    """:func:`agent_tool_names`, with the custom tools and MCP servers loaded.

    The first build of the built-in suite index and of a provider's tools
    imports and runs their builders (seconds in a cold process), so both run
    off the event loop.
    """
    from app.models.custom_tool import CustomTool
    from app.models.mcp_server import MCPServer
    from app.tools.builtin_suites import ensure_suite_index_loaded

    await ensure_suite_index_loaded()

    custom_ids = _uuids(getattr(agent, "custom_tool_ids", None) or [])
    mcp_ids = _uuids(getattr(agent, "mcp_server_ids", None) or [])
    custom_names = (
        list((await db.execute(select(CustomTool.name).where(CustomTool.id.in_(custom_ids)))).scalars())
        if custom_ids
        else []
    )
    servers = (
        list((await db.execute(select(MCPServer).where(MCPServer.id.in_(mcp_ids)))).scalars())
        if mcp_ids
        else []
    )
    return agent_tool_names(
        agent,
        custom_tool_names=custom_names,
        mcp_servers=servers,
        provider_tool_names=await asyncio.to_thread(_provider_tool_names, _bound_providers(agent)),
    )


async def always_ask_tools(db: AsyncSession, agents: Iterable[Any]) -> list[dict]:
    """For each agent carrying a tool marked "always ask", the agent and those tools, sorted.

    Raises ``WhitelistUnavailableError`` when the marks cannot be read: not
    knowing must not read as "nothing marked".
    """
    from app.services.hitl_whitelist import resolve_vetoed

    marked = await resolve_vetoed(db)
    if not marked:
        return []
    report = []
    for agent in agents:
        tools = sorted((await load_agent_tool_names(db, agent)) & marked)
        if tools:
            report.append({"agent_id": str(agent.id), "agent_name": agent.name, "tools": tools})
    return report
