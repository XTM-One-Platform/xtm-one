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

"""Which calls of a sandbox run write (#8255, PRD section 6.6).

One answer for an agent's tool call and for a flow's node, read from what the
platform already declares:

- a tool is read through the platform's one read/write classifier
  (``services/tool_classification.py``), on its base name when the composite
  registered it under a prefixed one; a name the classifier cannot read is a
  write, and so is any name its source does not vouch for
  (:func:`vouched_source`): a custom tool's, a personal MCP server's or an
  imported API spec's name is whatever its author typed;
- a flow node through the ``side_effect`` its catalog entry declares
  (``services/flow_catalog.py``); an undeclared node is a write, and a node
  that calls a tool by name is the tool's effect.

Nothing here spells a tool, a vendor or a node name: a new tool or a new node
kind is classified by the same rules, failing towards ``write``.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.tool_classification import infer_tool_type

CallEffect = Literal["read", "write"]
NodeEffect = Literal["none", "nested", "read", "write"]


def vouched_source(source: str | None, *, entity_id: Any = None, company_mcp_servers: Collection[str] = ()) -> bool:
    """Whether a tool of *source* may be classified by its name.

    The sources ``hitl_whitelist_derived`` derives reads from: the platform's
    own tools (built-ins, the integration provider catalog) and the servers an
    administrator vouched for (*company_mcp_servers*, by ``entity_id``).
    """
    category, _, rest = (source or "").partition(":")
    if category == "builtin":
        return True
    if category == "integration":
        # A provider the derivation reads nothing from is not vouched: its
        # tools are named by the integration's own content (an imported spec,
        # a renamed operation), not by the platform's catalog.
        from app.services.hitl_whitelist_derived import provider_read_tools

        return bool(provider_read_tools().get(rest))
    if category == "mcp":
        return entity_id is not None and str(entity_id) in company_mcp_servers
    return False


async def company_mcp_server_ids() -> frozenset[str]:
    """The enabled company-managed MCP servers, read in a session of their own."""
    from app.core.database import async_session
    from app.models.mcp_server import MCPServer

    async with async_session() as db:
        rows = await db.execute(
            select(MCPServer.id).where(MCPServer.is_company_managed.is_(True), MCPServer.enabled.is_(True))
        )
        return frozenset(str(server_id) for (server_id,) in rows.all())


def tool_call_effect(name: str, *, base_name: str | None = None, vouched: bool) -> CallEffect:
    """``read`` or ``write`` for a tool call, as the shared classifier reads it.

    *base_name* is the definition's ``base_name`` (the name the tool was built
    with, before a composite prefixed it); it decides when given, since a
    prefix is a label, not a verb. A tool whose source is not *vouched*
    (:func:`vouched_source`) is a write whatever its name.
    """
    if not vouched:
        return "write"
    return "read" if infer_tool_type(base_name or name) == "read" else "write"


def node_tool_name(kind: str, config: dict[str, Any]) -> str | None:
    """The tool a tool-calling node runs, when its config names one."""
    name = config.get("operation") if kind == "integration" else config.get("tool_name")
    return name.strip() if isinstance(name, str) and name.strip() else None


async def node_tool_vouched(db: AsyncSession | None, kind: str, config: dict[str, Any] | None) -> bool:
    """Whether the tool a tool-calling node names may be classified by its
    name: an operation of a catalog provider's integration, or a flow-callable
    built-in (it wins its name over a custom or an integration tool)."""
    from app.services.flow_catalog import NODE_KINDS_BY_ID, SIDE_EFFECT_TOOL

    spec = NODE_KINDS_BY_ID.get(kind)
    config = config or {}
    name = node_tool_name(kind, config)
    if spec is None or spec.side_effect != SIDE_EFFECT_TOOL or name is None:
        return False
    if kind != "integration":
        from app.services.flow_runner import flow_builtin_tools

        return any(definition.name == name for definition, _ in flow_builtin_tools())
    try:
        integration_id = uuid.UUID(str(config.get("integration_id")))
    except (TypeError, ValueError):
        return False
    if db is None:
        return False
    from app.models.integration import Integration

    provider = await db.scalar(select(Integration.provider).where(Integration.id == integration_id))
    return bool(provider) and vouched_source(f"integration:{provider}")


def node_effect(
    kind: str, config: dict[str, Any] | None, *, llm_wired: bool = False, vouched: bool = False
) -> NodeEffect:
    """What executing a node of *kind* does outside the run.

    ``none`` and ``nested`` nodes run (a nested agent or flow intercepts its
    own calls); ``read`` runs, or replays a recording; ``write`` is replayed or
    intercepted. *llm_wired*: a model wired into a Call-tool node picks its
    tool at run time, so the effect is unknown, hence a write. *vouched*: the
    node's tool may be classified by its name (:func:`node_tool_vouched`).
    """
    from app.services.flow_catalog import (
        NODE_KINDS_BY_ID,
        SIDE_EFFECT_NESTED,
        SIDE_EFFECT_NONE,
        SIDE_EFFECT_TOOL,
    )

    spec = NODE_KINDS_BY_ID.get(kind)
    declared = spec.side_effect if spec is not None else None
    if declared == SIDE_EFFECT_NONE:
        return "none"
    if declared == SIDE_EFFECT_NESTED:
        return "nested"
    if declared == SIDE_EFFECT_TOOL:
        name = None if llm_wired else node_tool_name(kind, config or {})
        return "read" if name is not None and tool_call_effect(name, vouched=vouched) == "read" else "write"
    return "write"
