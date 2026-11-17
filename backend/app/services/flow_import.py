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

"""Flow import — native XTM One JSON, LangFlow JSON and n8n JSON.

Three formats land on ``POST /api/v1/flows/import``:

- **native** — what ``GET /flows/{id}/export`` produced. Round-trips exactly.
- **LangFlow** — a flow exported from LangFlow (https://www.langflow.org).
- **n8n** — a workflow exported from n8n (https://n8n.io); converted by
  :mod:`app.services.flow_import_n8n`, which documents its own contract.

## Why the LangFlow mapping is deliberately shallow

LangFlow's node catalog is component-centric (``ChatInput``, ``Chroma``,
``ParserComponent``, …); XTM One's is agent-centric (agents, handovers,
assignments, integrations). Only a handful of components have an honest local
equivalent — see :data:`COMPONENT_MAP`. Everything else is imported as an
``unresolved`` node that **keeps its position, label and original definition**,
so the graph stays legible on the canvas and can be re-mapped by hand.

The alternative — guessing a mapping for every component — would produce flows
that look complete and behave wrongly. The import report therefore always states
how many nodes came in unresolved and how many edges could not be reconnected;
a partial import is never silent.

## Reading LangFlow edges

LangFlow serialises ``sourceHandle`` / ``targetHandle`` as JSON-ish strings that
escape quotes with the character ``œ`` — unparseable without undoing that hack.
It also keeps the *parsed* objects under ``edge.data``, so this module reads
``edge["data"]`` and ignores the encoded strings entirely.
"""

from __future__ import annotations

import re
from typing import Any

from app.services.flow_catalog import (
    EDGE_DATA,
    KIND_UNRESOLVED,
    allowed_edge_kinds,
    get_node_kind,
    get_port,
)
from app.services.flow_graph import MAX_EDGES, MAX_NODES, NODE_ID_RE, empty_graph

FORMAT_NATIVE = "native"
FORMAT_LANGFLOW = "langflow"
FORMAT_N8N = "n8n"

# LangFlow component type -> XTM One node kind.
#
# `note` is the only LangFlow node that maps to an annotation, and it is safe to
# do so because LangFlow notes are free-floating too — no edge is ever lost.
# Anything mapped to a *port-less* kind would silently break its connections,
# which is why `Prompt`, `TextInput` and friends go to `unresolved` (which HAS
# ports) rather than to `util.note`.
COMPONENT_MAP: dict[str, str] = {
    "ChatInput": "trigger.manual",
    "Agent": "agent",
    "LanguageModelComponent": "agent",
    "ChatOutput": "action.send_message",
    "note": "util.note",
}


class FlowImportError(ValueError):
    """The uploaded document is not a flow this platform can read."""


def sanitize_node_id(raw: Any, used: set[str], index: int) -> str:
    """Coerce a foreign node id into one our ``{{node.field}}`` syntax allows.

    Node ids appear inside ``{{node_id.output}}``, which splits on the first
    dot, so anything outside ``[A-Za-z0-9_-]`` is replaced and the result is
    truncated to the 64 characters ``NODE_ID_RE`` permits.
    """
    candidate = re.sub(r"[^A-Za-z0-9_-]", "_", str(raw or ""))[:64].strip("_")
    if not candidate or not NODE_ID_RE.match(candidate):
        candidate = f"node_{index}"
    base = candidate
    suffix = 2
    while candidate in used:
        # Keep room for the suffix so the result stays within 64 chars.
        candidate = f"{base[: 64 - len(str(suffix)) - 1]}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _template_value(template: Any, name: str) -> Any:
    """Read ``template[name].value`` from a LangFlow node template."""
    if not isinstance(template, dict):
        return None
    field = template.get(name)
    if isinstance(field, dict):
        return field.get("value")
    return None


def detect_format(document: Any) -> str:
    """Return ``native`` or ``langflow``, or raise :class:`FlowImportError`."""
    if not isinstance(document, dict):
        raise FlowImportError("The uploaded file is not a JSON object.")

    graph = document.get("graph") if isinstance(document.get("graph"), dict) else None
    if graph is not None and isinstance(graph.get("nodes"), list):
        return FORMAT_NATIVE

    data = document.get("data")
    if isinstance(data, dict) and isinstance(data.get("nodes"), list):
        nodes = data["nodes"]
        # A LangFlow node always wraps its definition in data.node; the
        # `noteNode` / `genericNode` react-flow type is the second tell.
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("type") in ("genericNode", "noteNode"):
                return FORMAT_LANGFLOW
            inner = node.get("data")
            if isinstance(inner, dict) and isinstance(inner.get("node"), dict):
                return FORMAT_LANGFLOW
        # An empty LangFlow flow still identifies itself by its envelope.
        if "is_component" in document or "last_tested_version" in document:
            return FORMAT_LANGFLOW

    # n8n keeps `connections` keyed by node name instead of an edge list —
    # checked before the bare native graph, which an n8n file never matches.
    from app.services.flow_import_n8n import unwrap_n8n_workflow

    if unwrap_n8n_workflow(document) is not None:
        return FORMAT_N8N

    # A bare native graph (no envelope) is accepted too.
    if isinstance(document.get("nodes"), list) and isinstance(
        document.get("edges"), list
    ):
        return FORMAT_NATIVE

    raise FlowImportError(
        "Unrecognised flow format — expected an XTM One, LangFlow or n8n "
        "flow export."
    )


# ─── LangFlow ────────────────────────────────────────────────────────────────


def _import_langflow_node(
    node: dict[str, Any], node_id: str
) -> tuple[dict[str, Any], bool]:
    """Convert one LangFlow node. Returns ``(node, was_unresolved)``."""
    inner = node.get("data") if isinstance(node.get("data"), dict) else {}
    definition = inner.get("node") if isinstance(inner.get("node"), dict) else {}
    template = definition.get("template", {})
    component_type = inner.get("type") or node.get("type") or "unknown"

    position = node.get("position") if isinstance(node.get("position"), dict) else {}
    x = position.get("x", 0)
    y = position.get("y", 0)

    label = (
        definition.get("display_name")
        or inner.get("display_name")
        or str(component_type)
    )

    kind = COMPONENT_MAP.get(str(component_type))
    imported: dict[str, Any] = {
        "id": node_id,
        "kind": kind or KIND_UNRESOLVED,
        "position": {
            "x": x if isinstance(x, (int, float)) else 0,
            "y": y if isinstance(y, (int, float)) else 0,
        },
        "label": label,
        "config": {},
    }

    if kind == "util.note":
        imported["config"] = {"text": definition.get("description") or ""}
        return imported, False

    if kind == "agent":
        prompt = _template_value(template, "system_prompt") or _template_value(
            template, "input_value"
        )
        config: dict[str, Any] = {}
        if isinstance(prompt, str) and prompt:
            config["prompt"] = prompt
        max_iterations = _template_value(template, "max_iterations")
        if isinstance(max_iterations, (int, str)) and str(max_iterations).isdigit():
            config["max_iterations"] = int(max_iterations)
        # `agent_id` is intentionally left empty: no LangFlow component names an
        # XTM One agent, and inventing a binding would be worse than the
        # "required field" warning the builder shows on the node.
        imported["config"] = config
        return imported, False

    if kind == "action.send_message":
        # No channel/recipient exists in the source document; the builder
        # surfaces both as required-field warnings.
        return imported, False

    if kind is not None:
        return imported, False

    imported["config"] = {"source_type": str(component_type)}
    # Keep the whole original definition so the node can be re-mapped by hand
    # (and so nothing from the source file is lost on a round-trip).
    imported["source_payload"] = {
        "format": FORMAT_LANGFLOW,
        "type": str(component_type),
        "node": definition,
    }
    return imported, True


def _import_langflow_edges(
    raw_edges: list[Any], id_map: dict[str, str], kinds: dict[str, str]
) -> tuple[list[dict[str, Any]], int]:
    """Reconnect LangFlow edges. Returns ``(edges, dropped_count)``.

    Every LangFlow connection is a data flow, so each becomes an ``out`` → ``in``
    edge here. A connection whose endpoint has no such port on this side — an
    edge into a note, say — is dropped and counted rather than emitted as an
    edge the validator would then reject.
    """
    edges: list[dict[str, Any]] = []
    dropped = 0

    for index, raw in enumerate(raw_edges):
        if not isinstance(raw, dict):
            dropped += 1
            continue
        source = id_map.get(str(raw.get("source")))
        target = id_map.get(str(raw.get("target")))
        if not source or not target or source == target:
            dropped += 1
            continue

        source_kind = get_node_kind(kinds[source])
        target_kind = get_node_kind(kinds[target])
        if source_kind is None or target_kind is None:
            dropped += 1
            continue

        source_port = get_port(source_kind, "out", output=True)
        target_port = get_port(target_kind, "in", output=False)
        if source_port is None or target_port is None:
            dropped += 1
            continue
        if EDGE_DATA not in allowed_edge_kinds(source_port, target_port):
            dropped += 1
            continue

        edges.append(
            {
                "id": f"e{index + 1}",
                "source": source,
                "source_handle": source_port.id,
                "target": target,
                "target_handle": target_port.id,
                "kind": EDGE_DATA,
            }
        )

    return edges, dropped


def import_langflow(document: dict[str, Any]) -> dict[str, Any]:
    """Convert a LangFlow export into a native flow payload + import report."""
    data = document.get("data") if isinstance(document.get("data"), dict) else {}
    raw_nodes = data.get("nodes") if isinstance(data.get("nodes"), list) else []
    raw_edges = data.get("edges") if isinstance(data.get("edges"), list) else []

    if len(raw_nodes) > MAX_NODES:
        raise FlowImportError(
            f"This flow holds {len(raw_nodes)} nodes; the limit is {MAX_NODES}."
        )
    if len(raw_edges) > MAX_EDGES:
        raise FlowImportError(
            f"This flow holds {len(raw_edges)} edges; the limit is {MAX_EDGES}."
        )

    used_ids: set[str] = set()
    id_map: dict[str, str] = {}
    kinds: dict[str, str] = {}
    nodes: list[dict[str, Any]] = []
    unresolved = 0

    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            continue
        node_id = sanitize_node_id(raw.get("id"), used_ids, index)
        id_map[str(raw.get("id"))] = node_id
        node, was_unresolved = _import_langflow_node(raw, node_id)
        kinds[node_id] = node["kind"]
        nodes.append(node)
        unresolved += int(was_unresolved)

    edges, dropped = _import_langflow_edges(raw_edges, id_map, kinds)

    viewport = data.get("viewport")
    if not isinstance(viewport, dict):
        viewport = {"x": 0, "y": 0, "zoom": 1}

    return {
        "name": str(document.get("name") or "Imported LangFlow flow")[:200],
        "description": str(document.get("description") or ""),
        "graph": {"nodes": nodes, "edges": edges, "viewport": viewport},
        "report": {
            "format": FORMAT_LANGFLOW,
            "nodes_imported": len(nodes),
            "nodes_unresolved": unresolved,
            "edges_imported": len(edges),
            "edges_dropped": dropped,
        },
    }


# ─── Native ──────────────────────────────────────────────────────────────────


def import_native(document: dict[str, Any]) -> dict[str, Any]:
    """Read a native XTM One flow export (with or without its envelope)."""
    graph = document.get("graph")
    if not isinstance(graph, dict):
        graph = {
            "nodes": document.get("nodes", []),
            "edges": document.get("edges", []),
            "viewport": document.get("viewport"),
        }

    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    if len(nodes) > MAX_NODES or len(edges) > MAX_EDGES:
        raise FlowImportError(
            f"A flow may hold at most {MAX_NODES} nodes and {MAX_EDGES} edges."
        )

    return {
        "name": str(document.get("name") or "Imported flow")[:200],
        "description": str(document.get("description") or ""),
        "graph": graph,
        "report": {
            "format": FORMAT_NATIVE,
            "nodes_imported": len(nodes),
            "nodes_unresolved": sum(
                1
                for n in nodes
                if isinstance(n, dict) and n.get("kind") == KIND_UNRESOLVED
            ),
            "edges_imported": len(edges),
            "edges_dropped": 0,
        },
    }


def import_flow_document(document: Any) -> dict[str, Any]:
    """Import any supported flow document.

    Returns ``{name, description, graph, report}``. The caller validates and
    normalises the graph before persisting it — this module only converts.
    """
    fmt = detect_format(document)
    if fmt == FORMAT_LANGFLOW:
        return import_langflow(document)
    if fmt == FORMAT_N8N:
        # Imported here: the n8n module builds on this one's helpers.
        from app.services.flow_import_n8n import import_n8n

        return import_n8n(document)
    return import_native(document)


def export_flow(flow: Any) -> dict[str, Any]:
    """The native export envelope for a flow row."""
    return {
        "format": FORMAT_NATIVE,
        "version": 1,
        "name": flow.name,
        "description": flow.description or "",
        "tags": flow.tags or [],
        "graph": flow.graph or empty_graph(),
    }
