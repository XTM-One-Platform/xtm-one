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

"""The structure each node of a flow declares for its outputs.

The builder's autocomplete and Input data tree offer the paths below an
output (`{{a1.fields.severity}}`). Which ones exist is the run's own rule, so
it is answered here, with the functions the run uses, never re-spelled in the
browser:

- an agent node answers in the format and schema of
  ``effective_output_contract`` (a wired Structured output replaces the
  agent's schema, ``wired_capabilities`` picks the first one), and publishes
  ``fields`` only for a JSON answer whose schema is an object;
- a manual trigger publishes its declared parameters under ``payload``
  (``declared_parameters``);
- an event trigger or a Wait for event node publishes its event type's
  fields under ``fields``.

Agents and event types are read with the caller's visibility: a node naming
one the caller cannot see declares nothing.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.helpers import get_user_group_ids, user_visible_filter
from app.models.agent import Agent
from app.models.flow_event import FlowEventType
from app.models.user import User
from app.services.flow_runner import declared_parameters, wired_capabilities
from app.services.output_format import effective_output_contract

EVENT_KINDS = frozenset({"trigger.event", "util.wait_event"})

# Declared parameter / event field type → JSON Schema type (`json` is any).
_FIELD_TYPES = {"string": "string", "number": "number", "boolean": "boolean"}


def _object_schema(fields: list[dict]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for field in fields:
        name = str(field.get("name") or "").strip()
        if not name:
            continue
        prop: dict[str, Any] = {}
        json_type = _FIELD_TYPES.get(str(field.get("type") or ""))
        if json_type:
            prop["type"] = json_type
        description = str(field.get("description") or "").strip()
        if description:
            prop["description"] = description
        properties[name] = prop
    return {"type": "object", "properties": properties}


def _is_object_schema(schema: Any) -> bool:
    return isinstance(schema, dict) and (
        schema.get("type") == "object" or isinstance(schema.get("properties"), dict)
    )


def _is_edge(edge: Any) -> bool:
    """An edge ``wired_capabilities`` can read.

    Its source, its target and its target handle (when it has one) are
    strings: they are looked up in a dict and a set, where a list or an
    object would raise ``unhashable type`` instead of being skipped.
    """
    return (
        isinstance(edge, dict)
        and isinstance(edge.get("source"), str)
        and isinstance(edge.get("target"), str)
        and (edge.get("target_handle") is None or isinstance(edge["target_handle"], str))
    )


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def declared_output_schemas(
    db: AsyncSession, user: User, graph: dict[str, Any]
) -> dict[str, dict[str, dict[str, Any]]]:
    """Node id → output name → the JSON Schema that output declares."""
    # A draft graph from the canvas: anything not shaped like a node or an
    # edge is skipped rather than failing the whole answer. A node's kind is
    # looked up in a set and a dict, so a node without a string kind is too.
    raw_nodes = graph.get("nodes")
    raw_edges = graph.get("edges")
    graph = {
        "nodes": [
            {**n, "config": n["config"] if isinstance(n.get("config"), dict) else {}}
            for n in (raw_nodes if isinstance(raw_nodes, list) else [])
            if isinstance(n, dict) and isinstance(n.get("kind"), str)
        ],
        "edges": [e for e in (raw_edges if isinstance(raw_edges, list) else []) if _is_edge(e)],
    }
    nodes = {str(n["id"]): n for n in graph["nodes"] if isinstance(n.get("id"), str) and n["id"]}

    def config_id(node: dict, key: str) -> uuid.UUID | None:
        return _uuid_or_none(node["config"].get(key))

    agent_ids = {
        aid for n in nodes.values() if n.get("kind") == "agent" and (aid := config_id(n, "agent_id"))
    }
    event_type_ids = {
        tid
        for n in nodes.values()
        if n.get("kind") in EVENT_KINDS and (tid := config_id(n, "event_type_id"))
    }
    group_ids = await get_user_group_ids(db, user.id) if agent_ids or event_type_ids else []
    agents: dict[uuid.UUID, Agent] = {}
    if agent_ids:
        rows = await db.execute(
            select(Agent).where(
                Agent.id.in_(agent_ids),
                user_visible_filter(Agent, user.id, group_ids, is_admin=user.is_admin),
            )
        )
        agents = {a.id: a for a in rows.scalars().all()}
    event_fields: dict[uuid.UUID, list] = {}
    if event_type_ids:
        rows = await db.execute(
            select(FlowEventType).where(
                FlowEventType.id.in_(event_type_ids),
                user_visible_filter(FlowEventType, user.id, group_ids, is_admin=user.is_admin),
            )
        )
        event_fields = {t.id: t.fields or [] for t in rows.scalars().all()}

    out: dict[str, dict[str, dict[str, Any]]] = {}
    for node_id, node in nodes.items():
        kind = node.get("kind")
        declared: dict[str, dict[str, Any]] = {}
        if kind == "agent":
            agent = agents.get(config_id(node, "agent_id"))
            wired = wired_capabilities(graph, node_id, nodes)["output_schema"]
            if agent is not None or wired is not None:
                answer_format, schema = effective_output_contract(
                    agent.output_format if agent else None,
                    agent.output_schema if agent else None,
                    wired,
                )
                if answer_format == "json" and _is_object_schema(schema):
                    declared["fields"] = schema
        elif kind == "trigger.manual":
            params = declared_parameters(node["config"])
            if params:
                declared["payload"] = _object_schema(params)
        elif kind in EVENT_KINDS:
            fields = event_fields.get(config_id(node, "event_type_id"))
            if fields is not None:
                declared["fields"] = _object_schema(fields)
        if declared:
            out[node_id] = declared
    return out
