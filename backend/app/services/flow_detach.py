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

"""Detach a selection into a standalone flow.

Splits one canvas document in two: the selected nodes become a new flow, and
the parent keeps everything else. There is deliberately **no reference** left
behind — no sub-flow node, no parent→child link. A reference node would have to
mean something at execution time, and nothing executes yet; inventing that
contract now would be guessing.

Three things happen to edges, and each is reported rather than assumed:

* wholly inside the selection  → moved to the child
* wholly outside               → left in the parent
* crossing the boundary        → **dropped**, and counted

A crossing edge cannot survive: keeping it in the parent would point at a node
that is no longer there, and the validator refuses dangling endpoints — so the
honest options are "drop and say so" or "refuse the whole operation". Dropping
wins because the parent is versioned: the pre-detach graph is one restore away.

The same reasoning covers ``{{node_id.output}}`` references. A detached node may
read from a node that stayed behind; in the child that reference can never
resolve, and `ref.unknown_node` is a blocking error. Those references are
stripped from the child and listed individually in the report, so the user is
told exactly what was removed rather than discovering a silently altered prompt.
"""

from __future__ import annotations

import re
from typing import Any

from app.services.flow_catalog import variable_field_names
from app.services.flow_graph import PLACEHOLDER_RE, RESERVED_REF_PREFIXES


def _strip_reference(text: str, missing: set[str], removed: list[str]) -> str:
    """Remove `{{node.field}}` placeholders whose node is not in *keep*."""

    def replace(match: re.Match[str]) -> str:
        ref = match.group(1).strip()
        if ref.startswith(RESERVED_REF_PREFIXES):
            return match.group(0)
        target = ref.partition(".")[0]
        if target in missing:
            removed.append(ref)
            return ""
        return match.group(0)

    return PLACEHOLDER_RE.sub(replace, text)


def _strip_in_value(value: Any, missing: set[str], removed: list[str]) -> Any:
    """Apply the strip to every string reachable inside a config value."""
    if isinstance(value, str):
        return _strip_reference(value, missing, removed)
    if isinstance(value, list):
        return [_strip_in_value(v, missing, removed) for v in value]
    if isinstance(value, dict):
        return {k: _strip_in_value(v, missing, removed) for k, v in value.items()}
    return value


def _strip_node(
    node: dict[str, Any], missing: set[str], removed: list[str]
) -> dict[str, Any]:
    """Return *node* with references to *missing* nodes removed from its config.

    Only fields the catalog declares as variable-capable are touched — a
    literal ``{{`` in, say, an HTTP body template is not a flow reference.
    """
    config = node.get("config") or {}
    fields = variable_field_names(node.get("kind", ""))
    if not fields:
        return node
    stripped = {
        key: (_strip_in_value(value, missing, removed) if key in fields else value)
        for key, value in config.items()
    }
    return {**node, "config": stripped}


def split_graph(
    graph: dict[str, Any], node_ids: list[str]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Split *graph* on *node_ids*.

    Returns ``(parent_graph, child_graph, report)``. Neither input nor output
    is validated here — the caller runs the normal validator on both halves,
    so a split that would produce an invalid graph is refused by the same rules
    as any other write.
    """
    selected = {n for n in node_ids}
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []

    present = {n.get("id") for n in nodes if isinstance(n, dict)}
    # A selection referring to nodes that are not in the graph is a client bug;
    # narrowing to what exists keeps the split well-defined either way.
    selected &= present

    child_nodes = [n for n in nodes if n.get("id") in selected]
    parent_nodes = [n for n in nodes if n.get("id") not in selected]

    child_edges: list[dict[str, Any]] = []
    parent_edges: list[dict[str, Any]] = []
    dropped_edges: list[dict[str, Any]] = []
    for edge in edges:
        source_in = edge.get("source") in selected
        target_in = edge.get("target") in selected
        if source_in and target_in:
            child_edges.append(edge)
        elif not source_in and not target_in:
            parent_edges.append(edge)
        else:
            dropped_edges.append(edge)

    # A split breaks references in BOTH directions, and both halves have to be
    # left valid: the child may read from a node that stayed behind, and — just
    # as often — a node that stayed behind reads from one that left. Missing the
    # second case makes the *parent* invalid and fails the whole operation.
    outsiders = present - selected

    removed_from_child: list[str] = []
    stripped_child_nodes = [
        _strip_node(node, outsiders, removed_from_child) for node in child_nodes
    ]
    removed_from_parent: list[str] = []
    stripped_parent_nodes = [
        _strip_node(node, selected, removed_from_parent) for node in parent_nodes
    ]

    viewport = {"x": 0, "y": 0, "zoom": 1}
    parent_graph = {
        "nodes": stripped_parent_nodes,
        "edges": parent_edges,
        "viewport": graph.get("viewport") or viewport,
    }
    child_graph = {
        "nodes": stripped_child_nodes,
        "edges": child_edges,
        # The child is framed on load rather than inheriting the parent's pan,
        # which would almost certainly put it off-screen.
        "viewport": viewport,
    }
    report = {
        "nodes_detached": len(stripped_child_nodes),
        "edges_detached": len(child_edges),
        "edges_dropped": len(dropped_edges),
        "references_removed": sorted(set(removed_from_child)),
        "parent_references_removed": sorted(set(removed_from_parent)),
    }
    return parent_graph, child_graph, report


# ─── Collapse: the UE "Collapse to Function" ─────────────────────────

# The endpoint replaces this with the freshly created child flow's id — the
# service cannot know it before the row is flushed.
CHILD_FLOW_SENTINEL = "__child_flow_id__"


def _sanitize_param_name(ref: str, taken: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_]+", "_", ref).strip("_") or "valeur"
    if base[0].isdigit():
        base = f"p_{base}"
    name = base[:60]
    n = 2
    while name in taken:
        name = f"{base[:56]}_{n}"
        n += 1
    taken.add(name)
    return name


def collapse_graph(
    graph: dict[str, Any], node_ids: list[str]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Collapse *node_ids* into a sub-flow, leaving a wired ``flow.call``.

    The execution contract detach could not offer now exists, so this is the
    honest upgrade: the child gets a Manual trigger whose PARAMETERS are
    minted from every outside reference the selection used, the parent keeps
    ONE ``flow.call`` node (arguments mapping each parameter back to its
    original reference), crossing exec edges are rewired through it, and
    parent references into the selection become ``{{<call>.output}}``.
    """
    selected = {n for n in node_ids}
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    present = {n.get("id") for n in nodes if isinstance(n, dict)}
    selected &= present

    child_nodes = [n for n in nodes if n.get("id") in selected]
    parent_nodes = [n for n in nodes if n.get("id") not in selected]
    if not child_nodes:
        return graph, {"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}}, {
            "nodes_detached": 0, "edges_detached": 0, "edges_dropped": 0,
            "references_removed": [], "parent_references_removed": [],
        }

    taken_ids = set(present)

    def _mint(base: str) -> str:
        candidate, n = base, 2
        while candidate in taken_ids:
            candidate = f"{base}_{n}"
            n += 1
        taken_ids.add(candidate)
        return candidate

    call_id = _mint("subflow")
    trigger_id = "t_in" if "t_in" not in {n.get("id") for n in child_nodes} else "t_entree"

    # ── Parameters: every outside reference the selection reads ────────────
    taken_params: set[str] = set()
    params_by_ref: dict[str, str] = {}

    def _param_for(ref: str) -> str:
        if ref not in params_by_ref:
            params_by_ref[ref] = _sanitize_param_name(ref, taken_params)
        return params_by_ref[ref]

    def _rewrite(text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            ref = match.group(1).strip()
            if ref.startswith(RESERVED_REF_PREFIXES):
                return match.group(0)
            if ref.partition(".")[0] in selected:
                return match.group(0)
            return f"{{{{{trigger_id}.payload.{_param_for(ref)}}}}}"
        return PLACEHOLDER_RE.sub(replace, text)

    def _rewrite_value(value: Any) -> Any:
        if isinstance(value, str):
            return _rewrite(value)
        if isinstance(value, list):
            return [_rewrite_value(v) for v in value]
        if isinstance(value, dict):
            return {k: _rewrite_value(v) for k, v in value.items()}
        return value

    rewritten_child = []
    for node in child_nodes:
        fields = variable_field_names(node.get("kind", ""))
        config = node.get("config") or {}
        rewritten_child.append(
            {
                **node,
                "config": {
                    key: (_rewrite_value(value) if key in fields else value)
                    for key, value in config.items()
                },
            }
        )

    # ── Edges ───────────────────────────────────────────────────────────────
    child_edges: list[dict[str, Any]] = []
    parent_edges: list[dict[str, Any]] = []
    inbound_targets: list[str] = []
    rewired = 0
    dropped: list[dict[str, Any]] = []
    seen_parent_edges: set[tuple] = set()

    def _add_parent_edge(edge: dict[str, Any]) -> None:
        key = (edge["source"], edge.get("source_handle"), edge["target"], edge.get("target_handle"))
        if key in seen_parent_edges:
            return
        seen_parent_edges.add(key)
        parent_edges.append(edge)

    edge_seq = 1

    def _edge_id() -> str:
        nonlocal edge_seq
        while True:
            candidate = f"ce{edge_seq}"
            edge_seq += 1
            if candidate not in {e.get("id") for e in parent_edges + child_edges}:
                return candidate

    for edge in edges:
        source_in = edge.get("source") in selected
        target_in = edge.get("target") in selected
        if source_in and target_in:
            child_edges.append(edge)
        elif not source_in and not target_in:
            _add_parent_edge(edge)
        elif target_in and edge.get("kind") != "value":
            # Inbound exec → the call node; the child enters via its trigger.
            _add_parent_edge({**edge, "id": _edge_id(), "target": call_id, "target_handle": "in"})
            if edge.get("target") not in inbound_targets:
                inbound_targets.append(edge.get("target"))
            rewired += 1
        elif source_in and edge.get("kind") != "value":
            # Outbound exec → continues after the call returns.
            _add_parent_edge({
                **edge, "id": _edge_id(), "source": call_id,
                "source_handle": "out", "kind": "data",
            })
            rewired += 1
        else:
            dropped.append(edge)

    # Entry wiring inside the child: the trigger feeds the nodes the exec
    # crossed into; with no crossing, every internal exec-root.
    child_ids = {n.get("id") for n in child_nodes}
    if not inbound_targets:
        has_inbound = {e.get("target") for e in child_edges if e.get("kind") != "value"}
        inbound_targets = [n for n in sorted(child_ids) if n not in has_inbound]
    for target in inbound_targets:
        child_edges.append({
            "id": _edge_id(), "source": trigger_id, "source_handle": "out",
            "target": target, "target_handle": "in", "kind": "data",
        })

    # ── Parent side: refs INTO the selection become the call's output ──────
    parent_rewritten: list[str] = []

    def _parent_rewrite(text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            ref = match.group(1).strip()
            if ref.startswith(RESERVED_REF_PREFIXES):
                return match.group(0)
            if ref.partition(".")[0] in selected:
                parent_rewritten.append(ref)
                return f"{{{{{call_id}.output}}}}"
            return match.group(0)
        return PLACEHOLDER_RE.sub(replace, text)

    def _parent_rewrite_value(value: Any) -> Any:
        if isinstance(value, str):
            return _parent_rewrite(value)
        if isinstance(value, list):
            return [_parent_rewrite_value(v) for v in value]
        if isinstance(value, dict):
            return {k: _parent_rewrite_value(v) for k, v in value.items()}
        return value

    rewritten_parent = []
    for node in parent_nodes:
        fields = variable_field_names(node.get("kind", ""))
        config = node.get("config") or {}
        rewritten_parent.append(
            {
                **node,
                "config": {
                    key: (_parent_rewrite_value(value) if key in fields else value)
                    for key, value in config.items()
                },
            }
        )

    # ── The two synthetic nodes ─────────────────────────────────────────────
    xs = [n.get("position", {}).get("x", 0) for n in child_nodes]
    ys = [n.get("position", {}).get("y", 0) for n in child_nodes]
    centroid = {
        "x": round(sum(xs) / len(xs)) if xs else 0,
        "y": round(sum(ys) / len(ys)) if ys else 0,
    }
    rewritten_parent.append({
        "id": call_id,
        "kind": "flow.call",
        "label": "Sous-flow",
        "position": centroid,
        "config": {
            "flow_id": CHILD_FLOW_SENTINEL,
            "input": "",
            "arguments": {
                name: f"{{{{{ref}}}}}" for ref, name in params_by_ref.items()
            },
        },
    })
    rewritten_child.insert(0, {
        "id": trigger_id,
        "kind": "trigger.manual",
        "position": {"x": (min(xs) if xs else 0) - 360, "y": ys[0] if ys else 0},
        "config": {
            "parameters": [
                {"name": name, "type": "string", "required": False}
                for name in params_by_ref.values()
            ],
        },
    })

    viewport = {"x": 0, "y": 0, "zoom": 1}
    parent_graph = {
        "nodes": rewritten_parent,
        "edges": parent_edges,
        "viewport": graph.get("viewport") or viewport,
    }
    child_graph = {"nodes": rewritten_child, "edges": child_edges, "viewport": viewport}
    report = {
        "nodes_detached": len(child_nodes),
        "edges_detached": len(child_edges),
        "edges_dropped": len(dropped),
        "references_removed": sorted(
            f"{ref} → {trigger_id}.payload.{name}" for ref, name in params_by_ref.items()
        ),
        "parent_references_removed": sorted(
            {f"{ref} → {call_id}.output" for ref in parent_rewritten}
        ),
    }
    return parent_graph, child_graph, report
