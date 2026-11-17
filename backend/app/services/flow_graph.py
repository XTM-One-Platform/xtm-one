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

"""Flow graph schema + validation for the visual flow builder.

The canvas document is a plain, explicit JSON structure — deliberately *not*
React Flow's own node shape, which carries transient UI state (``selected``,
``dragging``, ``measured``) that has no business being persisted:

.. code-block:: json

    {
      "nodes": [
        {"id": "n1", "kind": "trigger.schedule", "label": "Every morning",
         "position": {"x": 0, "y": 0}, "config": {"cron": "0 9 * * 1-5"}}
      ],
      "edges": [
        {"id": "e1", "source": "n1", "source_handle": "out",
         "target": "n2", "target_handle": "in", "kind": "data", "label": ""}
      ],
      "viewport": {"x": 0, "y": 0, "zoom": 1}
    }

The builder maps this to and from React Flow's shape in a thin client adapter.

## Severity

Saving a *draft* must never be blocked by an unfinished flow, so validation
returns two classes of issue:

- **error** — the graph is structurally invalid (unknown node kind, dangling
  edge, incompatible ports, a ``{{ref}}`` that cannot resolve). The write is
  refused.
- **warning** — the graph is well-formed but incomplete (no trigger, a required
  field left empty, an unreachable node). The write proceeds and the builder
  pins the warning on the node.

Variable references are an **error**, not a warning: the issue requires
``{{node_id.output_field}}`` to be validated at save time rather than blowing up
mid-run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.core.lone_surrogates import find_lone_surrogate, lone_surrogate_error
from app.core.scopes import COORDINATION_SCOPE_WORKSPACE
from app.llm.decisions import MAX_CHOICES
from app.services.flow_catalog import (
    CATEGORY_CAPABILITIES,
    CATEGORY_TRIGGERS,
    FIELD_JSON,
    FIELD_SELECT,
    KIND_UNRESOLVED,
    MAX_WEBHOOK_CONCURRENT_RUNS,
    OUTPUT_TEXT,
    PER_CASE_SOCKET_KINDS,
    allowed_edge_kinds,
    concurrent_runs_setting,
    field_applies,
    get_node_kind,
    get_port,
    output_variable,
    output_variable_names,
    variable_field_names,
)

# The issue's stated performance target is 200 nodes; refusing beyond that is
# what keeps a pathological import from wedging the canvas.
MAX_NODES = 200
MAX_EDGES = 1000

# Node ids appear inside ``{{node_id.field}}``, which splits on the FIRST dot —
# so an id may never contain one.
NODE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# ``{{ ... }}`` placeholders. Non-greedy so two refs on one line stay separate.
PLACEHOLDER_RE = re.compile(r"\{\{\s*(.+?)\s*\}\}")

# Prefixes owned by the existing assignment template renderer
# (``app/services/assignment_helpers.py`` — ``{{var_id.UUID}}`` for platform
# variables, ``{{trigger.*}}`` for the trigger payload). They resolve at run
# time from a different source, so the flow validator must leave them alone
# rather than reporting them as unknown nodes.
RESERVED_REF_PREFIXES = ("var_id.", "trigger.")

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


@dataclass(frozen=True)
class ValidationIssue:
    """One problem found in a graph, addressed to a node when possible."""

    code: str
    message: str
    severity: str = SEVERITY_ERROR
    node_id: str | None = None
    edge_id: str | None = None
    field: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "node_id": self.node_id,
            "edge_id": self.edge_id,
            "field": self.field,
        }


@dataclass
class ValidationResult:
    issues: list[ValidationIssue]

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == SEVERITY_WARNING]

    @property
    def is_valid(self) -> bool:
        """True when nothing blocks the write (warnings are allowed through)."""
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.is_valid,
            "issues": [i.to_dict() for i in self.issues],
        }


def empty_graph() -> dict[str, Any]:
    """The canvas document a brand-new flow starts from."""
    return {"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}}


# ─── Structural helpers ──────────────────────────────────────────────────────


def _is_number(value: Any) -> bool:
    # bool is an int subclass — a position of `True` is a malformed graph, not 1.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


#: The first ``config`` key of a path into a node, when it is one a field can have.
_CONFIG_FIELD_RE = re.compile(r"^\.config\.([A-Za-z_][A-Za-z0-9_]*)")


def _lone_surrogate_issues(graph: dict[str, Any]) -> list[ValidationIssue]:
    """An error for each node or edge holding a lone UTF-16 surrogate, and one for the rest.

    The ``graph`` JSON column would store it as U+FFFD, not what was sent, and
    no response can encode it - so a node or edge is addressed by its id only
    when that id is clean, and nothing else is checked while one is left:
    another issue's message could echo it.
    """
    issues: list[ValidationIssue] = []
    for collection, id_key in (("nodes", "node_id"), ("edges", "edge_id")):
        items = graph.get(collection)
        for index, item in enumerate(items if isinstance(items, list) else []):
            found = find_lone_surrogate(item)
            if found is None:
                continue
            own_id = item.get("id") if isinstance(item, dict) else None
            addressable = isinstance(own_id, str) and find_lone_surrogate(own_id) is None
            name = "" if addressable else f"{collection}[{index}]"
            field = _CONFIG_FIELD_RE.match(found.path) if collection == "nodes" else None
            issues.append(
                ValidationIssue(
                    "graph.lone_surrogate",
                    lone_surrogate_error(name, item) or "",
                    field=field.group(1) if field else None,
                    **{id_key: own_id if addressable else None},
                )
            )
    rest = {key: value for key, value in graph.items() if key not in ("nodes", "edges")}
    message = lone_surrogate_error("", rest)
    if message is not None:
        issues.append(ValidationIssue("graph.lone_surrogate", message))
    return issues


def _validate_shape(graph: Any, issues: list[ValidationIssue]) -> bool:
    """Check the envelope. Returns False when nothing further can be checked."""
    if not isinstance(graph, dict):
        issues.append(
            ValidationIssue("graph.malformed", "The graph must be a JSON object.")
        )
        return False
    for key in ("nodes", "edges"):
        if not isinstance(graph.get(key, []), list):
            issues.append(
                ValidationIssue("graph.malformed", f"`{key}` must be a list.")
            )
            return False
    viewport = graph.get("viewport")
    if viewport is not None and not isinstance(viewport, dict):
        issues.append(
            ValidationIssue("graph.malformed", "`viewport` must be a JSON object.")
        )
        return False
    if len(graph.get("nodes", [])) > MAX_NODES:
        issues.append(
            ValidationIssue(
                "graph.too_large",
                f"A flow may hold at most {MAX_NODES} nodes.",
            )
        )
        return False
    if len(graph.get("edges", [])) > MAX_EDGES:
        issues.append(
            ValidationIssue(
                "graph.too_large",
                f"A flow may hold at most {MAX_EDGES} edges.",
            )
        )
        return False
    return True


def _validate_nodes(
    nodes: list[Any], issues: list[ValidationIssue]
) -> dict[str, dict[str, Any]]:
    """Validate every node and return the well-formed ones by id."""
    by_id: dict[str, dict[str, Any]] = {}
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            issues.append(
                ValidationIssue("node.malformed", f"Node #{index} is not an object.")
            )
            continue
        node_id = node.get("id")
        if not isinstance(node_id, str) or not NODE_ID_RE.match(node_id):
            issues.append(
                ValidationIssue(
                    "node.invalid_id",
                    f"Node #{index} has an invalid id — use letters, digits, "
                    "`-` and `_` only (no dots).",
                    node_id=node_id if isinstance(node_id, str) else None,
                )
            )
            continue
        if node_id in by_id:
            issues.append(
                ValidationIssue(
                    "node.duplicate_id",
                    f"Two nodes share the id `{node_id}`.",
                    node_id=node_id,
                )
            )
            continue

        kind = node.get("kind")
        node_kind = get_node_kind(kind) if isinstance(kind, str) else None
        if node_kind is None:
            issues.append(
                ValidationIssue(
                    "node.unknown_kind",
                    f"`{kind}` is not a known node type.",
                    node_id=node_id,
                )
            )
            continue

        position = node.get("position")
        if (
            not isinstance(position, dict)
            or not _is_number(position.get("x"))
            or not _is_number(position.get("y"))
        ):
            issues.append(
                ValidationIssue(
                    "node.invalid_position",
                    "A node needs a numeric `position.x` / `position.y`.",
                    node_id=node_id,
                )
            )
            continue

        if not isinstance(node.get("config", {}), dict):
            issues.append(
                ValidationIssue(
                    "node.invalid_config",
                    "`config` must be a JSON object.",
                    node_id=node_id,
                )
            )
            continue

        by_id[node_id] = node

    return by_id


def auto_layout(graph: dict) -> dict:
    """Assign positions to nodes that lack them — headless authoring.

    The canvas requires numeric ``position.x/y`` on every node (a missing
    position is a validation ERROR), but an agent building a flow from a
    prompt has no mouse. This lays out the EXEC graph as topological layers
    (longest path from the roots), one column per layer, nodes stacked
    within their column; capability attachments sit one column left of the
    node they feed and annotations stack above the first column. Nodes that
    already carry a position keep it — a partial layout only fills the
    holes. Cycles (loop back-edges) are cut by ignoring already-layered
    sources, so the walk terminates.
    """
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    if not isinstance(nodes, list):
        return graph

    def has_position(node: dict) -> bool:
        pos = node.get("position")
        return (
            isinstance(pos, dict)
            and isinstance(pos.get("x"), (int, float))
            and isinstance(pos.get("y"), (int, float))
        )

    todo = [n for n in nodes if isinstance(n, dict) and not has_position(n)]
    if not todo:
        return graph

    ids = {n.get("id") for n in nodes if isinstance(n, dict)}
    exec_out: dict[str, list[str]] = {}
    incoming: dict[str, int] = {}
    capability_targets: dict[str, str] = {}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        src, tgt = edge.get("source"), edge.get("target")
        if src not in ids or tgt not in ids:
            continue
        if edge.get("kind") == "capability":
            capability_targets[src] = tgt
            continue
        if edge.get("kind") == "value":
            continue
        exec_out.setdefault(src, []).append(tgt)
        incoming[tgt] = incoming.get(tgt, 0) + 1

    # Longest-path layering via Kahn's algorithm; leftovers (cycle members)
    # land one column after their deepest layered predecessor.
    layer: dict[str, int] = {}
    queue = [
        n["id"]
        for n in nodes
        if isinstance(n, dict)
        and n.get("id")
        and incoming.get(n["id"], 0) == 0
        and n.get("id") not in capability_targets
    ]
    for node_id in queue:
        layer[node_id] = 0
    remaining = dict(incoming)
    while queue:
        current = queue.pop(0)
        for nxt in exec_out.get(current, ()):
            candidate = layer[current] + 1
            if candidate > layer.get(nxt, -1):
                layer[nxt] = candidate
            remaining[nxt] = remaining.get(nxt, 1) - 1
            if remaining[nxt] == 0:
                queue.append(nxt)
    for node in nodes:
        node_id = node.get("id") if isinstance(node, dict) else None
        if not node_id or node_id in layer or node_id in capability_targets:
            continue
        preds = [
            layer[s_]
            for s_, targets in exec_out.items()
            if node_id in targets and s_ in layer
        ]
        layer[node_id] = (max(preds) + 1) if preds else 0
    for cap_id, target in capability_targets.items():
        layer[cap_id] = max(0, layer.get(target, 1) - 1)

    col_width, row_height = 340, 170
    rows_used: dict[int, int] = {}
    note_row = 0
    for node in nodes:
        if not isinstance(node, dict) or has_position(node):
            continue
        node_id = node.get("id")
        if str(node.get("kind") or "") == "util.note":
            node["position"] = {"x": 40, "y": -220 - note_row * 140}
            note_row += 1
            continue
        col = layer.get(node_id, 0)
        row = rows_used.get(col, 0)
        rows_used[col] = row + 1
        # Capability attachments hang BELOW their column's flow row.
        y_base = 380 if node_id in capability_targets else 60
        node["position"] = {"x": 60 + col * col_width, "y": y_base + row * row_height}
    return graph


def _config_strings(value: Any):
    """Yield every string buried in a config value (dicts/lists included)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _config_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _config_strings(v)


def _delegate_branch_reads_the_answer(
    agent_id: str,
    branch_start: str,
    nodes_by_id: dict[str, dict[str, Any]],
    edges: list[Any],
) -> bool:
    """True when a node reachable from the delegate branch references the
    agent's handover ANSWER — the one template guaranteed to render empty
    for a delegation. Walks execution edges only: `value` edges
    point INTO their consumer and never carry the flow forward."""
    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        src, tgt = edge.get("source"), edge.get("target")
        if src and tgt and edge.get("kind") != "value":
            adjacency.setdefault(src, []).append(tgt)
    direct = f"{agent_id}.handover.answer"
    listed = f"{agent_id}.handovers"
    seen: set[str] = set()
    queue = [branch_start]
    while queue:
        node_id = queue.pop(0)
        if node_id in seen:
            continue
        seen.add(node_id)
        node = nodes_by_id.get(node_id)
        if node:
            for text in _config_strings(node.get("config")):
                if direct in text or (listed in text and ".answer" in text):
                    return True
        queue.extend(adjacency.get(node_id, ()))
    return False


def _validate_edges(
    edges: list[Any],
    nodes_by_id: dict[str, dict[str, Any]],
    issues: list[ValidationIssue],
) -> list[dict[str, Any]]:
    """Validate every edge and return the well-formed ones."""
    valid: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    # (target node, target port) -> count, for the single-connection rule.
    incoming: dict[tuple[str, str], int] = {}

    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            issues.append(
                ValidationIssue("edge.malformed", f"Edge #{index} is not an object.")
            )
            continue
        edge_id = edge.get("id")
        if not isinstance(edge_id, str) or not edge_id:
            issues.append(
                ValidationIssue("edge.invalid_id", f"Edge #{index} has no id.")
            )
            continue
        if edge_id in seen_ids:
            issues.append(
                ValidationIssue(
                    "edge.duplicate_id",
                    f"Two edges share the id `{edge_id}`.",
                    edge_id=edge_id,
                )
            )
            continue
        seen_ids.add(edge_id)

        source_id = edge.get("source")
        target_id = edge.get("target")
        if source_id not in nodes_by_id or target_id not in nodes_by_id:
            issues.append(
                ValidationIssue(
                    "edge.dangling",
                    "An edge points at a node that is not in the flow.",
                    edge_id=edge_id,
                )
            )
            continue
        if source_id == target_id:
            issues.append(
                ValidationIssue(
                    "edge.self_loop",
                    "A node cannot connect to itself.",
                    edge_id=edge_id,
                    node_id=source_id,
                )
            )
            continue

        # Both are known kinds — _validate_nodes dropped anything else — so the
        # guard is only here to keep the type checker (and a future refactor)
        # honest.
        source_kind = get_node_kind(nodes_by_id[source_id]["kind"])
        target_kind = get_node_kind(nodes_by_id[target_id]["kind"])
        if source_kind is None or target_kind is None:
            continue

        source_port = get_port(source_kind, edge.get("source_handle"), output=True)
        target_port = get_port(target_kind, edge.get("target_handle"), output=False)
        if source_port is None:
            issues.append(
                ValidationIssue(
                    "edge.unknown_source_port",
                    f"`{source_kind.label}` has no output `{edge.get('source_handle')}`.",
                    edge_id=edge_id,
                    node_id=source_id,
                )
            )
            continue
        if target_port is None:
            issues.append(
                ValidationIssue(
                    "edge.unknown_target_port",
                    f"`{target_kind.label}` has no input `{edge.get('target_handle')}`.",
                    edge_id=edge_id,
                    node_id=target_id,
                )
            )
            continue

        legal = allowed_edge_kinds(source_port, target_port)
        if not legal:
            issues.append(
                ValidationIssue(
                    "edge.incompatible",
                    f"`{source_kind.label}` cannot connect to `{target_kind.label}` "
                    "— these ports carry different kinds of data.",
                    edge_id=edge_id,
                )
            )
            continue

        kind = edge.get("kind")
        if kind not in legal:
            issues.append(
                ValidationIssue(
                    "edge.invalid_kind",
                    f"`{kind}` is not a valid connection between these ports "
                    f"(expected one of {', '.join(sorted(legal))}).",
                    edge_id=edge_id,
                )
            )
            continue

        # Handover semantics (unified): the branch fires for EVERY
        # handover occurrence, routed by edge label. The delegate note is
        # CONTEXTUAL: a delegation's RESULT is asynchronous — the occurrence
        # carries the task, never the answer — so the warning only fires
        # when a node downstream actually reads the answer, the one
        # reference guaranteed to render empty. Trace-only branches and
        # event-join patterns (On delegate + Wait for event, which read the
        # result from the EVENT payload) stay silent.
        if source_port.id == "handover":
            label = (edge.get("label") or "").strip().lower()
            if label == "delegate" and _delegate_branch_reads_the_answer(
                source_id, target_id, nodes_by_id, edges
            ):
                issues.append(
                    ValidationIssue(
                        "handover.delegate_not_awaited",
                        "This branch references the delegation's answer "
                        "({{node.handover.answer}}), which is always empty "
                        "for a delegate — the result arrives later, in its "
                        "own run. Attach On delegate to the agent and join "
                        "with Wait for event: the result travels in the "
                        "event payload.",
                        severity="warning",
                        edge_id=edge_id,
                        node_id=source_id,
                    )
                )

        if not target_port.multiple:
            key = (target_id, target_port.id)
            incoming[key] = incoming.get(key, 0) + 1
            if incoming[key] > 1:
                issues.append(
                    ValidationIssue(
                        "edge.port_occupied",
                        f"`{target_kind.label}`'s `{target_port.label}` input "
                        "accepts a single connection.",
                        edge_id=edge_id,
                        node_id=target_id,
                    )
                )
                continue

        valid.append(edge)

    return valid


def _flow_call_is_detached(
    node_id: str,
    nodes_by_id: dict[str, Any],
    edges: list[dict[str, Any]],
) -> bool:
    """Mirror of the walker's `_flow_call_mode` — see flow_runner."""
    for edge in edges:
        if edge.get("target") != node_id:
            continue
        if (edge.get("source_handle") or "") != "handover":
            continue
        source = nodes_by_id.get(edge.get("source") or "")
        if not isinstance(source, dict):
            continue
        mode = ((source.get("config") or {}).get("handover_mode") or "detached")
        return str(mode).strip() != "wait"
    return False


# ─── Reachability ────────────────────────────────────────────────────────────


def ancestors(
    node_id: str, edges: list[dict[str, Any]]
) -> set[str]:
    """Every node that can reach *node_id*, following edges backwards.

    Cycle-safe (``util.loop`` makes cycles legitimate) — the visited set stops
    the walk rather than an acyclicity assumption.
    """
    incoming: dict[str, list[str]] = {}
    for edge in edges:
        incoming.setdefault(edge["target"], []).append(edge["source"])

    seen: set[str] = set()
    stack = list(incoming.get(node_id, []))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(incoming.get(current, []))
    return seen


def _descendants_of_triggers(
    nodes_by_id: dict[str, dict[str, Any]], edges: list[dict[str, Any]]
) -> set[str]:
    """Every node reachable from any trigger node."""
    outgoing: dict[str, list[str]] = {}
    for edge in edges:
        # `value` edges are pure data wiring — the runner never walks them,
        # so they must not make a node look executable.
        if edge.get("kind") == "value":
            continue
        outgoing.setdefault(edge["source"], []).append(edge["target"])

    seen: set[str] = set()
    stack = [
        node_id
        for node_id, node in nodes_by_id.items()
        if (kind := get_node_kind(node["kind"])) is not None
        and kind.category == CATEGORY_TRIGGERS
    ]
    seen.update(stack)
    while stack:
        current = stack.pop()
        for nxt in outgoing.get(current, []):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


# ─── Variable references ─────────────────────────────────────────────────────


def pending_import_review(node: Any) -> list[str]:
    """What an importer asked a human to check on *node* before it runs.

    An importer that converts a node it could not carry over faithfully (an
    expression it could not translate, credentials it never imports) lists
    why under ``source_payload.review``. The validator pins the notes on the
    canvas as a warning — the draft still saves — and the walker refuses to
    execute the node until someone clears them, so an imported step never
    runs doing something else than it did where it came from.
    """
    payload = node.get("source_payload") if isinstance(node, dict) else None
    review = payload.get("review") if isinstance(payload, dict) else None
    if not isinstance(review, list):
        return []
    return [str(note) for note in review if str(note).strip()]


def _iter_strings(value: Any) -> list[str]:
    """Every string reachable inside a config value (lists/dicts included)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for item in value for s in _iter_strings(item)]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _iter_strings(item)]
    return []


def extract_references(text: str) -> list[str]:
    """The ``{{...}}`` placeholder bodies in *text*, reserved ones excluded."""
    return [
        ref
        for ref in PLACEHOLDER_RE.findall(text)
        if not ref.startswith(RESERVED_REF_PREFIXES)
    ]


def _validate_references(
    nodes_by_id: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    issues: list[ValidationIssue],
) -> None:
    ancestor_cache: dict[str, set[str]] = {}

    for node_id, node in nodes_by_id.items():
        config = node.get("config", {})
        # Only the fields declared as variable-capable are scanned. A literal
        # `{{` inside, say, an HTTP body template is not a flow reference.
        for field_name in variable_field_names(node["kind"]):
            for text in _iter_strings(config.get(field_name)):
                for ref in extract_references(text):
                    target, _, output = ref.partition(".")
                    if not output:
                        issues.append(
                            ValidationIssue(
                                "ref.malformed",
                                f"`{{{{{ref}}}}}` must name a node and one of its "
                                "outputs, e.g. {{step_1.output}}.",
                                node_id=node_id,
                                field=field_name,
                            )
                        )
                        continue
                    if target not in nodes_by_id:
                        issues.append(
                            ValidationIssue(
                                "ref.unknown_node",
                                f"`{{{{{ref}}}}}` refers to `{target}`, which is "
                                "not a node in this flow.",
                                node_id=node_id,
                                field=field_name,
                            )
                        )
                        continue
                    # Keyed by the *referencing* node — the question is always
                    # "does `target` run before `node_id`?".
                    if node_id not in ancestor_cache:
                        ancestor_cache[node_id] = ancestors(node_id, edges)
                    # A constant never "runs": it is pre-evaluated, so any
                    # node may reference it regardless of graph order.
                    if nodes_by_id[target].get("kind") == "util.primitive":
                        continue
                    if target not in ancestor_cache[node_id]:
                        issues.append(
                            ValidationIssue(
                                "ref.not_upstream",
                                f"`{{{{{ref}}}}}` refers to `{target}`, which does "
                                "not run before this node.",
                                node_id=node_id,
                                field=field_name,
                            )
                        )
                        continue
                    # A DETACHED flow call never publishes `output` — the
                    # relay's result lives in ITS run, not this one. Yellow:
                    # the reference will render empty at runtime. Detached is
                    # decided at the handover SOURCE: a call fed by an agent's
                    # handover edge follows that agent's handover_mode
                    # (default detached); a normal call always waits.
                    if (
                        nodes_by_id[target].get("kind") == "flow.call"
                        and _flow_call_is_detached(target, nodes_by_id, edges)
                        and output.split(".", 1)[0] == "output"
                    ):
                        issues.append(
                            ValidationIssue(
                                "ref.detached_output",
                                f"`{{{{{ref}}}}}`: this Flow call is DETACHED — "
                                "it publishes no `output` (the relay runs in "
                                "parallel). Join with Wait for event, or "
                                "switch the call back to Wait mode.",
                                severity="warning",
                                node_id=node_id,
                                field=field_name,
                            )
                        )
                        continue
                    available = output_variable_names(nodes_by_id[target]["kind"])
                    # Only the FIRST segment is the declared output; anything
                    # after it is a path into that value. A webhook publishes
                    # `payload` as a dict, and `{{t1.payload.issue.id}}` is
                    # exactly how it is meant to be read — refusing sub-paths
                    # would make publishing a dict pointless.
                    output_head = output.split(".", 1)[0]
                    if output_head not in available:
                        expected = ", ".join(sorted(available)) or "no outputs"
                        issues.append(
                            ValidationIssue(
                                "ref.unknown_output",
                                f"`{target}` does not publish `{output_head}` "
                                f"(it publishes {expected}).",
                                node_id=node_id,
                                field=field_name,
                            )
                        )
                        continue
                    # A path into a text output resolves to nothing at run
                    # time: `{{agent.output.severity}}` renders empty even
                    # when the agent answered `{"severity": ...}`.
                    _, _, sub_path = output.partition(".")
                    declared = output_variable(nodes_by_id[target]["kind"], output_head)
                    if sub_path and declared is not None and declared.type == OUTPUT_TEXT:
                        hint = (
                            f" Read a JSON answer with `{{{{{target}.fields.{sub_path}}}}}`."
                            if "fields" in available
                            else ""
                        )
                        issues.append(
                            ValidationIssue(
                                "ref.text_path",
                                f"`{{{{{ref}}}}}` reads into `{output_head}`, which "
                                f"is text: it always renders empty.{hint}",
                                severity=SEVERITY_WARNING,
                                node_id=node_id,
                                field=field_name,
                            )
                        )


# ─── Completeness warnings ───────────────────────────────────────────────────


def _collect_warnings(
    nodes_by_id: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    issues: list[ValidationIssue],
) -> None:
    if not nodes_by_id:
        return

    trigger_ids = [
        node_id
        for node_id, node in nodes_by_id.items()
        if (kind := get_node_kind(node["kind"])) is not None
        and kind.category == CATEGORY_TRIGGERS
    ]
    if not trigger_ids:
        issues.append(
            ValidationIssue(
                "flow.no_trigger",
                "This flow has no trigger, so nothing will ever start it.",
                severity=SEVERITY_WARNING,
            )
        )

    reachable = _descendants_of_triggers(nodes_by_id, edges)
    for node_id, node in nodes_by_id.items():
        node_kind = get_node_kind(node["kind"])
        if node_kind is None:  # unreachable: _validate_nodes dropped these
            continue
        if node_kind.is_annotation:
            continue

        if node_kind.category == CATEGORY_CAPABILITIES:
            # Capabilities sit OUTSIDE control flow by design — no trigger
            # ever leads to them, so "unreachable" would be constant noise.
            # The real mistake worth flagging is a capability wired to nothing.
            if not any(edge.get("source") == node_id for edge in edges):
                issues.append(
                    ValidationIssue(
                        "capability.unwired",
                        "This capability is not connected to any agent, so it "
                        "grants nothing.",
                        severity=SEVERITY_WARNING,
                        node_id=node_id,
                    )
                )
        elif node_kind.kind == "util.primitive":
            # Constants sit OUTSIDE control flow — never "unreachable". The
            # mistake worth flagging is a value node with nothing to give.
            config = node.get("config") or {}
            # Object constants carry their content in object_fields — the
            # scalar emptiness check does not apply to them.
            if (config.get("value_type") or "text") != "object" and (
                not str(config.get("value") or "").strip()
                and not str(config.get("variable_id") or "").strip()
            ):
                issues.append(
                    ValidationIssue(
                        "primitive.empty",
                        "This value is empty — set a literal or pick a "
                        "platform variable.",
                        severity=SEVERITY_WARNING,
                        node_id=node_id,
                        field="value",
                    )
                )
        elif node_kind.kind == "trigger.manual":
            # Typed input parameters (the UE function signature): names must
            # be identifier-shaped ({{t.payload.<name>}} path segments) and
            # unique, or references to them become ambiguous.
            params = node.get("config", {}).get("parameters")
            if isinstance(params, list):
                seen_names: set[str] = set()
                for item in params:
                    name = (
                        str(item.get("name") or "").strip()
                        if isinstance(item, dict)
                        else ""
                    )
                    if not name:
                        continue
                    if name in seen_names or not re.fullmatch(
                        r"[A-Za-z_][A-Za-z0-9_]*", name
                    ):
                        issues.append(
                            ValidationIssue(
                                "trigger.invalid_parameters",
                                f"Parameter `{name}` is duplicated or not a "
                                "valid identifier (letters, digits, `_`).",
                                severity=SEVERITY_WARNING,
                                node_id=node_id,
                                field="parameters",
                            )
                        )
                    seen_names.add(name)
        elif node_kind.kind in PER_CASE_SOCKET_KINDS:
            # Per-case sockets are minted from the node's configured cases (a
            # Switch's cases, a Classify node's labels); an edge can outlive
            # the case it was drawn for (renamed / deleted), after which
            # nothing ever routes down it.
            cases = node.get("config", {}).get("cases")
            case_names = set(cases.keys()) if isinstance(cases, dict) else set()
            for edge in edges:
                handle = edge.get("source_handle") or ""
                if edge.get("source") != node_id or not handle.startswith("case:"):
                    continue
                name = handle.removeprefix("case:")
                if name not in case_names:
                    issues.append(
                        ValidationIssue(
                            "switch.stale_case",
                            f"The case `{name}` no longer exists on this "
                            "node — its branch will never run.",
                            severity=SEVERITY_WARNING,
                            node_id=node_id,
                            edge_id=edge.get("id"),
                        )
                    )
            if (
                node_kind.kind == "condition.classify"
                # The runtime drops an empty label; count what it sends.
                and len([n for n in case_names if str(n)]) > MAX_CHOICES
            ):
                issues.append(
                    ValidationIssue(
                        "classify.too_many_labels",
                        f"A decision model takes at most {MAX_CHOICES} labels.",
                        severity=SEVERITY_WARNING,
                        node_id=node_id,
                        field="cases",
                    )
                )
            if trigger_ids and node_id not in reachable:
                issues.append(
                    ValidationIssue(
                        "node.unreachable",
                        "No trigger leads to this node.",
                        severity=SEVERITY_WARNING,
                        node_id=node_id,
                    )
                )
        elif node_kind.kind == "agent":
            # A Chat start point makes its agent the conversational endpoint —
            # wiring one auto-enables the node's chat toggle, but the author
            # can still untick it by hand, and that contradiction deserves a
            # flag rather than a silent non-conversational chat entry.
            behind_chat_trigger = any(
                e.get("target") == node_id
                and (nodes_by_id.get(e.get("source") or "") or {}).get("kind")
                == "trigger.chat"
                for e in edges
            )
            if behind_chat_trigger and not (node.get("config") or {}).get(
                "chat_enabled"
            ):
                issues.append(
                    ValidationIssue(
                        "agent.chat_disabled_behind_chat_trigger",
                        "A Chat start point leads here but this agent node's "
                        "'Enable chat' toggle is off.",
                        severity=SEVERITY_WARNING,
                        node_id=node_id,
                        field="chat_enabled",
                    )
                )
            # Singular attachment sockets take ONE wired node; a second is
            # silently dropped (first wins), so flag it.
            for handle, code, what in (
                ("output", "agent.multiple_output_schemas", "structured-output schema"),
                ("memory", "agent.multiple_memory", "memory node"),
                ("guardrails", "agent.multiple_guardrails", "guardrails node"),
                ("shared_state", "agent.multiple_shared_state", "shared-state node"),
                # `_wired_workset` documents "first wired wins" but nothing
                # said so to the builder — a real gap this list already had,
                # and it is how two nodes naming DIFFERENT workspaces would
                # otherwise pick one silently.
                ("workset", "agent.multiple_worksets", "workset node"),
                ("budget", "agent.multiple_budgets", "budget node"),
            ):
                wired = [
                    e
                    for e in edges
                    if e.get("target") == node_id
                    and (e.get("target_handle") or "") == handle
                ]
                if len(wired) > 1:
                    issues.append(
                        ValidationIssue(
                            code,
                            f"This agent has more than one {what} wired; "
                            "only the first applies.",
                            severity=SEVERITY_WARNING,
                            node_id=node_id,
                        )
                    )
            # Work-items nodes naming DIFFERENT workspaces. Deliberately NOT
            # folded into the list above, because for this socket "only the
            # first applies" is half wrong: the PEERS from every wired node
            # accumulate — that is how a roster is built — while the workspace
            # is first-wins. A second node is therefore partly honoured and
            # partly ignored, which is worth saying precisely.
            wi_workspaces = {
                str(
                    (
                        (nodes_by_id.get(e.get("source") or "") or {}).get("config")
                        or {}
                    ).get("workspace_id")
                    or ""
                ).strip()
                for e in edges
                if e.get("target") == node_id
                and (e.get("target_handle") or "") == "work_items"
            } - {""}
            if len(wi_workspaces) > 1:
                issues.append(
                    ValidationIssue(
                        "agent.conflicting_work_item_workspaces",
                        "This agent has work-items nodes naming different "
                        "workspaces; their peers all apply but only the first "
                        "workspace does.",
                        severity=SEVERITY_WARNING,
                        node_id=node_id,
                    )
                )
            if trigger_ids and node_id not in reachable:
                issues.append(
                    ValidationIssue(
                        "node.unreachable",
                        "No trigger leads to this node.",
                        severity=SEVERITY_WARNING,
                        node_id=node_id,
                    )
                )
        elif trigger_ids and node_id not in reachable:
            issues.append(
                ValidationIssue(
                    "node.unreachable",
                    "No trigger leads to this node.",
                    severity=SEVERITY_WARNING,
                    node_id=node_id,
                )
            )

        pending = pending_import_review(node)
        if pending:
            more = f" (+{len(pending) - 1} more)" if len(pending) > 1 else ""
            issues.append(
                ValidationIssue(
                    "node.import_review",
                    f"Imported node to review before it can run: {pending[0]}{more}",
                    severity=SEVERITY_WARNING,
                    node_id=node_id,
                )
            )

        if node_kind.kind == KIND_UNRESOLVED:
            issues.append(
                ValidationIssue(
                    "node.unresolved",
                    "This node was imported without a local equivalent — pick a "
                    "node type for it before activating the flow.",
                    severity=SEVERITY_WARNING,
                    node_id=node_id,
                )
            )
            # Its config is the foreign payload; required-field checks below
            # would be noise.
            continue

        config = node.get("config", {})
        # A node with an LLM wired into it runs in LLM mode, and only that
        # mode's fields are required — asking for a string operator on a
        # question the model answers (or the reverse) would be noise.
        llm_edges = [
            e
            for e in edges
            if e.get("target") == node_id
            and (e.get("target_handle") or "") == "llm"
        ]
        llm_wired = bool(llm_edges)
        # First wired wins at run time (the socket is `multiple` only so ONE
        # LLM node can drive several nodes), so a second is silently ignored.
        # A `workspace`-scoped coordination node with no workspace named will
        # fail CLOSED at run time — deliberately, since the only fallback is
        # the shared global namespace. Catching it at save time turns a failed
        # run into a field to fill in.
        if (
            node_kind.kind
            in ("capability.shared_state", "capability.workset")
            and str(config.get("scope") or "") == COORDINATION_SCOPE_WORKSPACE
            and not str(config.get("workspace_id") or "").strip()
        ):
            issues.append(
                ValidationIssue(
                    "node.workspace_scope_unset",
                    "Scope is a workspace but none is picked — this node will "
                    "fail rather than fall back to Global.",
                    severity=SEVERITY_WARNING,
                    node_id=node_id,
                    field="workspace_id",
                )
            )
        if len(llm_edges) > 1:
            issues.append(
                ValidationIssue(
                    "node.multiple_llms",
                    "This node has more than one LLM wired; only the first "
                    "applies.",
                    severity=SEVERITY_WARNING,
                    node_id=node_id,
                )
            )
        # Get variable's name is only meaningful when reading a FLOW
        # variable — a catalog-level `required` would false-flag collection
        # mode, so the requirement is contextual.
        if (
            node_kind.kind == "util.get_variable"
            and str(config.get("source") or "flow") != "collection"
            and not str(config.get("name") or "").strip()
        ):
            issues.append(
                ValidationIssue(
                    "node.missing_field",
                    "`Variable` is required.",
                    severity=SEVERITY_WARNING,
                    node_id=node_id,
                    field="name",
                )
            )
        # A Webhook trigger's concurrency outside its range is clamped at run
        # time (an unreadable one means 1): say so rather than let the author
        # believe the flow takes more deliveries at once than it will.
        if node_kind.kind == "trigger.webhook" and config.get(
            "max_concurrent_runs"
        ) not in (None, ""):
            limit = concurrent_runs_setting(config.get("max_concurrent_runs"))
            if limit is None or not 1 <= limit <= MAX_WEBHOOK_CONCURRENT_RUNS:
                issues.append(
                    ValidationIssue(
                        "webhook.concurrent_runs_out_of_range",
                        "`Concurrent runs` takes a whole number from 1 to "
                        f"{MAX_WEBHOOK_CONCURRENT_RUNS}; outside it the trigger "
                        f"uses 1 or {MAX_WEBHOOK_CONCURRENT_RUNS}.",
                        severity=SEVERITY_WARNING,
                        node_id=node_id,
                        field="max_concurrent_runs",
                    )
                )
        # An object constant without its declared type renders `{}` at run
        # time — say so while authoring (yellow, not blocking).
        if (
            node_kind.kind == "util.primitive"
            and (config.get("value_type") or "text") == "object"
            and not str(config.get("object_type_id") or "").strip()
        ):
            issues.append(
                ValidationIssue(
                    "primitive.object_type_missing",
                    "Pick the declared type this object conforms to — "
                    "without it the value is empty at run time.",
                    severity=SEVERITY_WARNING,
                    node_id=node_id,
                    field="object_type_id",
                )
            )
        for cfg in node_kind.config_fields:
            if not field_applies(cfg, llm_wired):
                continue
            value = config.get(cfg.name)
            if cfg.required and (
                value is None or value == "" or value == [] or value == {}
            ):
                issues.append(
                    ValidationIssue(
                        "node.missing_field",
                        f"`{cfg.label}` is required.",
                        severity=SEVERITY_WARNING,
                        node_id=node_id,
                        field=cfg.name,
                    )
                )
                continue
            # JSON text that does not parse stays text, and every reader of a
            # JSON field takes an object or a list: the step would run as if
            # the field were empty. Say so while authoring.
            if (
                cfg.type == FIELD_JSON
                and isinstance(value, str)
                and value.strip()
                and _parsed_json_text(value) is _NOT_JSON
            ):
                issues.append(
                    ValidationIssue(
                        "node.invalid_json",
                        f"`{cfg.label}` is not valid JSON, so this step runs "
                        "as if it were empty. Write a reference inside a "
                        'string: "{{node.output}}".',
                        severity=SEVERITY_WARNING,
                        node_id=node_id,
                        field=cfg.name,
                    )
                )
                continue
            # A STATIC select is an enum: the builder can only pick from the
            # declared options, so an out-of-range value (hand-written JSON,
            # API callers) is malformed — and the runtime treats unknown
            # enum members as silent no-ops (an unknown If operator
            # evaluates false, an unknown scope falls back...), the worst
            # kind of failure. Dynamic selects (source=...) are validated by
            # their own resolvers; templated values keep working.
            if (
                cfg.type == FIELD_SELECT
                and cfg.options
                and not cfg.source
                and isinstance(value, str)
                and value
                and "{{" not in value
                and value not in {o.get("value") for o in cfg.options}
            ):
                allowed = ", ".join(
                    str(o.get("value")) for o in cfg.options
                )
                issues.append(
                    ValidationIssue(
                        "node.invalid_option",
                        f"`{value}` is not a valid choice for "
                        f"`{cfg.label}` — expected one of: {allowed}.",
                        node_id=node_id,
                        field=cfg.name,
                    )
                )


# ─── Entry point ─────────────────────────────────────────────────────────────


def validate_graph(graph: Any) -> ValidationResult:
    """Validate a canvas document.

    Errors block the write; warnings are surfaced on the canvas and let a draft
    save through. See the module docstring for the split.
    """
    issues: list[ValidationIssue] = []

    if not _validate_shape(graph, issues):
        return ValidationResult(issues)
    # What is stored: a transient key the write drops may hold anything.
    surrogate_issues = _lone_surrogate_issues(normalize_graph(graph))
    if surrogate_issues:
        return ValidationResult(surrogate_issues)

    nodes_by_id = _validate_nodes(graph.get("nodes", []), issues)
    edges = _validate_edges(graph.get("edges", []), nodes_by_id, issues)
    _validate_references(nodes_by_id, edges, issues)
    _collect_warnings(nodes_by_id, edges, issues)

    return ValidationResult(issues)


_NOT_JSON = object()


def _parsed_json_text(text: str) -> Any:
    """The object, list or scalar *text* holds as JSON, or ``_NOT_JSON``.

    A JSON string is ``_NOT_JSON`` too: it would only trade text for text.
    """
    try:
        value = json.loads(text)
    except ValueError:
        return _NOT_JSON
    return _NOT_JSON if isinstance(value, str) else value


def parse_json_fields(kind: Any, config: Any) -> Any:
    """*config* with the text of its JSON fields replaced by the value it holds.

    The builder edits a JSON field as text, and every reader of one (the
    Structured output schema, a trigger filter, a payload, arguments) takes
    the object or the list: kept as text, the field is ignored at run time.
    Text that is not JSON (a draft, a bare ``{{ref}}``) is kept, and the
    validator flags it. Idempotent; the write path (``normalize_graph``) and
    the migration of stored flows apply it.
    """
    node_kind = get_node_kind(kind) if isinstance(kind, str) else None
    if node_kind is None or not isinstance(config, dict):
        return config
    parsed = dict(config)
    for cfg in node_kind.config_fields:
        if cfg.type != FIELD_JSON:
            continue
        value = parsed.get(cfg.name)
        if not isinstance(value, str) or not value.strip():
            continue
        result = _parsed_json_text(value)
        if result is not _NOT_JSON:
            parsed[cfg.name] = result
    return parsed


def normalize_graph(graph: Any) -> dict[str, Any]:
    """Strip a canvas document down to the fields we persist.

    Keeps the stored JSON stable across builder versions: transient React Flow
    state a client might send (``selected``, ``dragging``, ``measured``) never
    reaches the database, so two identical graphs serialise identically and an
    export round-trips byte-for-byte. A JSON field's text is stored as the
    value it holds (``parse_json_fields``).
    """
    if not isinstance(graph, dict):
        return empty_graph()

    nodes = []
    for node in graph.get("nodes", []):
        if not isinstance(node, dict):
            continue
        position = node.get("position") or {}
        kept: dict[str, Any] = {
            "id": node.get("id"),
            "kind": node.get("kind"),
            "position": {
                "x": position.get("x", 0) if _is_number(position.get("x")) else 0,
                "y": position.get("y", 0) if _is_number(position.get("y")) else 0,
            },
            "config": parse_json_fields(node.get("kind"), node.get("config") or {}),
        }
        if isinstance(node.get("label"), str):
            kept["label"] = node["label"]
        if isinstance(node.get("parent_id"), str):
            kept["parent_id"] = node["parent_id"]
        # The import fallback carries the foreign definition it came from, so a
        # LangFlow node can be re-mapped by hand later.
        if isinstance(node.get("source_payload"), dict):
            kept["source_payload"] = node["source_payload"]
        nodes.append(kept)

    # A parent must be an existing group node — anything else dangles and is
    # dropped, so React Flow never receives an orphan parentId.
    group_ids = {
        n.get("id") for n in nodes if n.get("kind") == "util.group"
    }
    for node in nodes:
        if "parent_id" in node and node["parent_id"] not in group_ids:
            del node["parent_id"]

    edges = []
    for edge in graph.get("edges", []):
        if not isinstance(edge, dict):
            continue
        kept_edge: dict[str, Any] = {
            "id": edge.get("id"),
            "source": edge.get("source"),
            "source_handle": edge.get("source_handle"),
            "target": edge.get("target"),
            "target_handle": edge.get("target_handle"),
            "kind": edge.get("kind"),
        }
        if isinstance(edge.get("label"), str) and edge["label"]:
            kept_edge["label"] = edge["label"]
        # Presentation: how the edge is drawn. Absent = bezier (the default).
        if edge.get("path_style") in ("straight", "step", "smoothstep"):
            kept_edge["path_style"] = edge["path_style"]
        edges.append(kept_edge)

    viewport = graph.get("viewport")
    if not isinstance(viewport, dict):
        viewport = {"x": 0, "y": 0, "zoom": 1}
    else:
        viewport = {
            "x": viewport.get("x", 0) if _is_number(viewport.get("x")) else 0,
            "y": viewport.get("y", 0) if _is_number(viewport.get("y")) else 0,
            "zoom": viewport.get("zoom", 1) if _is_number(viewport.get("zoom")) else 1,
        }

    return {"nodes": nodes, "edges": edges, "viewport": viewport}
