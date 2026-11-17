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

"""n8n workflow import.

``POST /api/v1/flows/import`` accepts a workflow exported from n8n
(https://n8n.io) — the JSON of *Download*, of a copy from the canvas, or of a
template from the n8n template library — and converts it into a native flow.

## The compatibility contract

n8n and XTM One both draw automations as node graphs, but they do not share a
node catalog, an expression language or a data model. Every n8n node therefore
lands in exactly one of three tiers, and the import report counts each:

- **mapped** — a node with an honest local equivalent (Webhook, Schedule, If,
  Switch, HTTP Request, Wait, Merge, AI Agent, ...). Its parameters and its
  ``{{ $json... }}`` references are translated.
- **mapped, to review** — the node type maps, but something the n8n node did
  could not be carried over faithfully: a JavaScript expression, credentials,
  an operator with no local equivalent. The node keeps its kind, its position
  and its branches, and ``source_payload.review`` lists what to check. The
  validator pins those notes on the canvas as warnings and the runner
  **refuses to execute the node** until someone clears them — an imported flow
  never runs a step that silently does something else than it did in n8n.
- **unresolved** — no local equivalent at all (an app XTM One has no
  integration for, a message-bus trigger, ...). The node keeps its position, label and original
  parameters, exactly like an unresolved LangFlow component.

A missing *binding* — the XTM One agent behind an AI Agent node, the flow a
sub-workflow call starts — is not a review note: the catalog already marks
those fields as required, so the builder flags them the same way it does for a
hand-drawn node.

## What never crosses over

- **Credentials.** An n8n export only names them, and XTM One keeps secrets in
  its own encrypted store. Literal secret-looking values typed into parameters
  (an ``Authorization`` header, an ``apiKey`` field) are removed as well.
- **Pinned test data** and the workflow's runtime settings.

## Expressions

n8n expressions are JavaScript. The importer translates the forms that are a
plain *reference* to upstream data — ``$json.a.b``, ``$('Node').item.json.a``,
``$node["Node"].json.a``, ``$input.first().json.a`` — into
``{{node_id.output.path}}``, mapping each n8n node's item shape onto the
outputs its XTM One counterpart publishes (a webhook's ``body`` is the
trigger's ``payload``, an HTTP Request's item is the response ``body``, ...).
Pass-through nodes (If, Switch, Wait, No-op) are looked through, as n8n does.
Anything else — arithmetic, method calls, ``$now`` — is kept verbatim between
``⟨n8n: ...⟩`` markers and flagged for review.

References are resolved in a final pass, once every node has its local kind:
while a node is converted its references are written as opaque tokens.
"""

from __future__ import annotations

import json
import math
import re
import shlex
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any
from urllib.parse import quote

from app.services import flow_import_n8n_apps as n8n_apps
from app.services import flow_import_n8n_scripts as n8n_scripts
from app.services.flow_catalog import (
    EDGE_KIND_BY_PORT_TYPE,
    KIND_UNRESOLVED,
    allowed_edge_kinds,
    get_node_kind,
    get_port,
)
from app.services.flow_graph import MAX_EDGES, MAX_NODES, ancestors
from app.services.flow_import import FORMAT_N8N, FlowImportError, sanitize_node_id

_BASE = "n8n-nodes-base."
_LANGCHAIN = "@n8n/n8n-nodes-langchain."

# How far a template envelope may nest the workflow (`{workflow: {workflow:
# {nodes, connections}}}` is what the template library API serves).
_MAX_ENVELOPE_DEPTH = 3

# Caps on what the report repeats back, so a pathological file cannot turn the
# response into a megabyte of notes.
_MAX_WARNINGS = 50
_MAX_NOTE_EXCERPT = 160

# An expression the importer could not translate stays readable — and inert:
# these markers are not `{{ }}`, so the renderer never resolves them.
_UNTRANSLATED = "⟨n8n: {}⟩"

# Longer expressions are never a plain reference; capping them also bounds
# the work the reference patterns may do on hostile input.
_MAX_EXPRESSION_LENGTH = 1000

# Reference tokens written while nodes are converted, resolved at the end:
# an index into the importer's token table between two private-use code
# points. The document is stripped of those code points first, so a token
# can only be one the importer wrote.
_REF_OPEN = "\ue000"
_REF_CLOSE = "\ue001"
_REF_RE = re.compile("\ue000(\\d+)\ue001")

# ─── Expression references ───────────────────────────────────────────────────

_JS_STRING = r"""'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|`(?:[^`\\$]|\\.)*`"""
_ITEM_SELECTOR = (
    r"(?:item|first\(\s*\)|last\(\s*\)|all\(\s*\)\[\s*0\s*\]"
    r"|itemMatching\(\s*0\s*\))"
)
# (pattern, group holding the node name or None for "the current input").
_REFERENCE_BASES: tuple[tuple[re.Pattern[str], int | None], ...] = (
    (re.compile(r"\$json\b"), None),
    (re.compile(r"\$input\." + _ITEM_SELECTOR + r"\.json\b"), None),
    (
        re.compile(r"\$\(\s*(" + _JS_STRING + r")\s*\)\." + _ITEM_SELECTOR + r"\.json\b"),
        1,
    ),
    (re.compile(r"\$node\[\s*(" + _JS_STRING + r")\s*\]\.json\b"), 1),
    (re.compile(r"\$items\(\s*(" + _JS_STRING + r")\s*\)\[\s*0\s*\]\.json\b"), 1),
    # The legacy `$item(0).$node["X"].json`: X's first item.
    (
        re.compile(r"\$item\(\s*(?:0|'0'|\"0\")\s*\)\.\$node\[\s*(" + _JS_STRING + r")\s*\]\.json\b"),
        1,
    ),
)
_ACCESSOR = re.compile(r"\s*(?:\??\.\s*([A-Za-z_$][\w$]*)|\??\.?\[\s*(" + _JS_STRING + r"|\d+)\s*\])")
# `x || ''` and `x ?? ''` are no-ops here: a missing value already renders "".
_EMPTY_FALLBACK = re.compile(r"\s*(?:\|\||\?\?)\s*(?:''|\"\"|``)\s*$")
# JavaScript properties that read like a path segment but are not data.
_JS_PROPERTIES = frozenset({"length"})
# Wrappers that change nothing once rendered: the renderer already writes an
# object as JSON and anything else as text.
_TRANSPARENT_WRAPPERS = (
    re.compile(r"^JSON\.stringify\(\s*(.+?)\s*(?:,\s*null\s*,\s*\d+\s*)?\)$", re.S),
    re.compile(r"^String\(\s*(.+?)\s*\)$", re.S),
    re.compile(r"^(.+?)\.toString\(\s*\)$", re.S),
)
_JS_LITERAL = re.compile(r"^(?:-?\d+(?:\.\d+)?|true|false|null|'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")$")

# Parameter keys whose literal value is a secret (`apiKey`, `password`, ...),
# and header names that carry one.
_SECRET_KEY_RE = re.compile(
    r"pass(word|phrase)?$|secret|token|api[_-]?key|authorization|cookie|"
    r"private[_-]?key|credential",
    re.IGNORECASE,
)

# n8n's canvas is denser than this one: its nodes sit about one XTM One node
# width apart, and its AI sub-nodes are small circles placed closer still.
# Positions are spread by this factor, then nodes of one row are pushed apart
# until no two overlap.
_LAYOUT_SCALE = 1.5
_ROW_TOLERANCE = 90  # positions this close vertically form one row
_MIN_SPACING = 280  # an XTM One node is about 240 wide

# What a parameter of an unexpected shape raises inside a converter (a number
# where n8n writes a list, a list where it writes an object): that node is
# then imported as unresolved instead of failing the whole import.
_SHAPE_ERRORS = (AttributeError, IndexError, KeyError, TypeError, ValueError)

# A `"key": "value"` pair inside JSON text (a JSON body or header block
# typed into a parameter). Linear: no nested quantifier.
_JSON_PAIR = re.compile(r'"((?:[^"\\]|\\.)*)"(\s*:\s*)"((?:[^"\\]|\\.)*)"')

# The value n8n's first If / Switch versions give a comparison value left at
# its default (an export omits it).
_V1_DEFAULT_VALUE: dict[str, Any] = {"boolean": False, "number": 0}

_SECONDS_PER_UNIT = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}

_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

# Kinds whose n8n counterpart hands its input items on unchanged: a `$json`
# read through them is a read of what they received.
_PASSTHROUGH_KINDS = frozenset(
    {
        "condition.if",
        "condition.switch",
        "util.reroute",
        "util.delay",
    }
)

# The n8n AI agent node types whose prompt reads the chat input by default.
_AGENT_TYPES = frozenset({_LANGCHAIN + "agent", _LANGCHAIN + "chainLlm"})


def _js_string(literal: str) -> str:
    """The value of a JavaScript string literal (single, double or backtick)."""
    body = literal[1:-1]
    if literal[0] == '"':
        try:
            return str(json.loads(literal))
        except json.JSONDecodeError:
            pass
    return re.sub(r"\\(.)", r"\1", body)


def parse_reference(expression: str) -> tuple[str | None, list[str]] | None:
    """Read a plain n8n data reference, or ``None`` for anything else.

    Returns ``(node_name, path)``; a ``None`` name means "the current node's
    input" (``$json``). The whole expression must be the reference — a method
    call, an operator or a second reference makes it untranslatable.
    """
    if len(expression) > _MAX_EXPRESSION_LENGTH:
        return None
    text = _EMPTY_FALLBACK.sub("", expression.strip())
    for wrapper in _TRANSPARENT_WRAPPERS:
        unwrapped = wrapper.match(text)
        if unwrapped:
            text = _EMPTY_FALLBACK.sub("", unwrapped.group(1).strip())
    base = next(
        ((match, group) for pattern, group in _REFERENCE_BASES if (match := pattern.match(text))),
        None,
    )
    if base is None:
        return None
    match, group = base
    name = _js_string(match.group(group)) if group else None
    path: list[str] = []
    pos = match.end()
    while pos < len(text):
        accessor = _ACCESSOR.match(text, pos)
        if accessor is None:
            return None
        if accessor.group(1) is not None:
            key = accessor.group(1)
            if key in _JS_PROPERTIES:
                return None
        else:
            raw = accessor.group(2)
            key = raw if raw.isdigit() else _js_string(raw)
        # The renderer splits a reference on dots, and braces would end it.
        if not key or any(ch in key for ch in ".{}"):
            return None
        path.append(key)
        pos = accessor.end()
    return name, path


def _inert(text: str) -> str:
    """*text* with every brace pair broken, so no `{{ }}` can form from it."""
    return text.replace("{{", "{ {").replace("}}", "} }")


def _untranslated(expression: str) -> str:
    return _UNTRANSLATED.format(_inert(expression.strip()))


def _inert_value(value: Any) -> Any:
    """:func:`_inert` over every string of a config value.

    Run on every converted config before references are resolved, it is the
    one place that guarantees the only ``{{...}}`` in an imported flow are
    the references the importer wrote: a hostile document cannot assemble
    ``{{var_id.SECRET}}`` from literals, string concatenation or nesting.
    """
    if isinstance(value, str):
        return _inert(value)
    if isinstance(value, list):
        return [_inert_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _inert_value(item) for key, item in value.items()}
    return value


def _strip_ref_marks(value: Any) -> Any:
    """The document with the private-use token delimiters removed."""
    if isinstance(value, str):
        return value.replace(_REF_OPEN, "").replace(_REF_CLOSE, "")
    if isinstance(value, list):
        return [_strip_ref_marks(item) for item in value]
    if isinstance(value, dict):
        return {_strip_ref_marks(key): _strip_ref_marks(item) for key, item in value.items()}
    return value


def _template_parts(body: str) -> list[tuple[bool, str]]:
    """Split an n8n expression body into ``(is_expression, text)`` parts.

    A linear scan, not a regex: an unclosed ``{{`` repeated thousands of
    times made the non-greedy pattern quadratic.
    """
    parts: list[tuple[bool, str]] = []
    pos = 0
    while True:
        start = body.find("{{", pos)
        end = body.find("}}", start + 2) if start >= 0 else -1
        if start < 0 or end < 0:
            break
        parts.append((False, body[pos:start]))
        parts.append((True, body[start + 2 : end]))
        pos = end + 2
    parts.append((False, body[pos:]))
    return parts


def _excerpt(text: str) -> str:
    text = " ".join(str(text).split())
    if len(text) > _MAX_NOTE_EXCERPT:
        return text[: _MAX_NOTE_EXCERPT - 1] + "…"
    return text


def _short_type(node_type: str) -> str:
    """``n8n-nodes-base.httpRequest`` → ``httpRequest``."""
    return node_type.rsplit(".", 1)[-1]


def _unwrap_locator(value: Any) -> Any:
    """n8n's resource locator ``{__rl: true, value, ...}`` → its value."""
    if isinstance(value, dict) and value.get("__rl"):
        return value.get("value")
    return value


def _get(params: Any, *keys: str, default: Any = None) -> Any:
    """``params[k1][k2]...`` without the KeyError/TypeError dance."""
    current = params
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def _number(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return default
        return parsed if math.isfinite(parsed) else default
    return default


def _scalar_text(value: Any) -> str:
    """A literal parameter value as the text the renderer would produce."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (int, float, str)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def unwrap_n8n_workflow(document: Any) -> dict[str, Any] | None:
    """The ``{nodes, connections}`` object inside *document*, if it is n8n's.

    Accepts a bare export and the template library's envelopes
    (``{workflow: {...}}``, ``{workflow: {workflow: {...}}}``).
    """
    current = document
    for _ in range(_MAX_ENVELOPE_DEPTH + 1):
        if not isinstance(current, dict):
            return None
        if isinstance(current.get("nodes"), list) and isinstance(current.get("connections"), dict):
            return current
        current = current.get("workflow")
    return None


def _envelope_name(document: Any) -> str | None:
    """The workflow name, wherever the envelope keeps it."""
    current = document
    for _ in range(_MAX_ENVELOPE_DEPTH + 1):
        if not isinstance(current, dict):
            return None
        name = current.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        current = current.get("workflow")
    return None


# ─── Structured-output schema ────────────────────────────────────────────────


def _schema_from_example(value: Any, depth: int = 0) -> dict[str, Any]:
    """A JSON schema describing *value* — n8n's "generate from example"."""
    if depth > 20:
        return {}
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {str(k): _schema_from_example(v, depth + 1) for k, v in value.items()},
            "required": [str(k) for k in value],
        }
    if isinstance(value, list):
        return {
            "type": "array",
            "items": _schema_from_example(value[0], depth + 1) if value else {},
        }
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, (int, float)):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    return {}


def _is_object_schema(schema: Any) -> bool:
    return isinstance(schema, dict) and (schema.get("type") == "object" or "properties" in schema)


# ─── Cron ────────────────────────────────────────────────────────────────────


def _python_regex(pattern: str, *, case_sensitive: bool) -> str | None:
    """n8n's regex (``/body/flags`` or a bare body) as a Python pattern.

    ``None`` when it does not compile here (JavaScript-only syntax) or uses a
    flag with no Python equivalent: the condition then goes to review instead
    of quietly never matching.
    """
    literal = re.fullmatch(r"/(.*)/([a-z]*)", pattern, re.S)
    body, flags = (literal.group(1), literal.group(2)) if literal else (pattern, "")
    if set(flags) - set("gimsu"):
        return None
    inline = "".join(sorted(set(flags) & set("ims")))
    if not case_sensitive and "i" not in inline:
        inline += "i"
    python = f"(?{inline}){body}" if inline else body
    try:
        re.compile(python)
    except re.error:
        return None
    return python


def _cron_is_valid(expression: str) -> bool:
    from croniter import croniter

    return bool(expression) and croniter.is_valid(expression)


def _five_field_cron(expression: str) -> str:
    """n8n accepts a leading seconds field; the scheduler reads five fields."""
    fields = expression.split()
    if len(fields) == 6:
        fields = fields[1:]
    return " ".join(fields)


def _int(value: Any, default: int) -> int:
    return int(_number(value, default))


# ─── The importer ────────────────────────────────────────────────────────────


@dataclass
class _Mapped:
    """Where one n8n node's connections land in the converted graph."""

    # The node receiving every main input, and the one references point at.
    entry: str
    # n8n main output index → the (node id, output handle) pairs it became.
    outputs: dict[int, list[tuple[str, str]]] = field(default_factory=dict)
    # Number of regular n8n outputs: the error output, when enabled, follows.
    regular_outputs: int = 1
    # The entry's port n8n's error output lands on.
    error_handle: str = "error"


@dataclass
class _Notes:
    review: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class _N8nImporter:
    def __init__(self, workflow: dict[str, Any]) -> None:
        self.workflow = workflow
        self.nodes: list[dict[str, Any]] = []
        self.node_by_id: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self.edge_keys: set[tuple[str, str, str, str]] = set()
        self.dropped = 0
        self.used_ids: set[str] = set()
        self.source: dict[str, dict[str, Any]] = {}  # n8n name → n8n node
        self.mapped: dict[str, _Mapped] = {}
        self.main_inputs: dict[str, list[str]] = {}  # n8n name → predecessors
        self.main_targets: dict[str, list[str]] = {}  # n8n name → successors
        # n8n name → input index → predecessors (Compare Datasets reads A and B).
        self.inputs_by_index: dict[str, dict[int, list[str]]] = {}
        self.notes: dict[str, _Notes] = {}  # xtm id → notes
        # n8n names of the agents whose answer is structured (an output
        # parser is wired), and the Break node that exposes its fields.
        self.structured: set[str] = set()
        self.fields_node: dict[str, str] = {}  # agent xtm id → Break xtm id
        self.ai_target: dict[str, str] = {}  # sub-node → node it serves
        self.sub_nodes: set[str] = set()
        self.warnings: list[str] = []
        self.folded = 0
        # Distinct literal secrets removed (one header may be both blanked in
        # the config and scrubbed from the kept parameters).
        self.scrubbed: set[str] = set()
        self.credential_types: set[str] = set()
        self.credentialed: set[str] = set()  # n8n names of nodes with credentials
        # The n8n node being converted: `$json` reads ITS input.
        self.current: str = ""
        # (sources, path, original expression) per reference token.
        self.tokens: list[tuple[list[str], list[str], str]] = []
        self.ancestry: dict[str, set[str]] = {}

    # ── bookkeeping ──────────────────────────────────────────────────────

    def _warn(self, message: str) -> None:
        if len(self.warnings) < _MAX_WARNINGS and message not in self.warnings:
            self.warnings.append(message)

    def _add_node(
        self,
        n8n: dict[str, Any],
        kind: str,
        config: dict[str, Any],
        *,
        id_hint: str | None = None,
        label: str | None = None,
        offset: tuple[float, float] = (0, 0),
    ) -> str:
        raw_id = re.sub(r"[^a-z0-9]+", "_", (id_hint or n8n["name"]).lower()).strip("_")
        # `{{trigger.*}}` and `{{var_id.*}}` belong to other renderers: a node
        # may never be named after them.
        if raw_id in ("trigger", "var_id"):
            raw_id = f"{raw_id}_node"
        node_id = sanitize_node_id(raw_id, self.used_ids, len(self.nodes))
        position = n8n.get("position")
        x, y = 0.0, 0.0
        if isinstance(position, list) and len(position) >= 2:
            x, y = _number(position[0], 0), _number(position[1], 0)
        node = {
            "id": node_id,
            "kind": kind,
            "position": {"x": x + offset[0], "y": y + offset[1]},
            "label": (label or n8n["name"])[:200],
            "config": config,
        }
        node["source_payload"] = {
            "format": FORMAT_N8N,
            "type": n8n["type"],
            "name": n8n["name"],
        }
        self.nodes.append(node)
        self.node_by_id[node_id] = node
        self.notes[node_id] = _Notes()
        if len(self.nodes) > MAX_NODES:
            raise FlowImportError(f"This workflow converts to more than {MAX_NODES} nodes, the limit of a flow.")
        return node_id

    def _count_credentials(self, raw: dict[str, Any]) -> None:
        credentials = raw.get("credentials")
        if isinstance(credentials, dict) and credentials:
            self.credentialed.add(raw["name"])
            self.credential_types.update(str(k) for k in credentials)

    def _mark(self) -> tuple[int, int, int]:
        return len(self.nodes), len(self.edges), self.dropped

    def _rollback(self, mark: tuple[int, int, int]) -> None:
        """Undo what a converter added before it failed."""
        nodes, edges, dropped = mark
        for node in self.nodes[nodes:]:
            node_id = node["id"]
            del self.node_by_id[node_id]
            self.notes.pop(node_id, None)
            self.used_ids.discard(node_id)
            self.fields_node.pop(node_id, None)
        del self.nodes[nodes:]
        for edge in self.edges[edges:]:
            self.edge_keys.discard((edge["source"], edge["source_handle"], edge["target"], edge["target_handle"]))
        del self.edges[edges:]
        self.dropped = dropped

    def _review(self, node_id: str, message: str) -> None:
        bucket = self.notes[node_id].review
        if message not in bucket:
            bucket.append(message)

    def _note(self, node_id: str, message: str) -> None:
        bucket = self.notes[node_id].notes
        if message not in bucket:
            bucket.append(message)

    def _edge(
        self,
        source: str,
        source_handle: str,
        target: str,
        target_handle: str = "in",
    ) -> bool:
        """Add an edge when the two ports accept it; count it dropped if not."""
        if source == target:
            self.dropped += 1
            return False
        key = (source, source_handle, target, target_handle)
        if key in self.edge_keys:
            return True
        source_kind = get_node_kind(self.node_by_id[source]["kind"])
        target_kind = get_node_kind(self.node_by_id[target]["kind"])
        source_port = get_port(source_kind, source_handle, output=True) if source_kind else None
        target_port = get_port(target_kind, target_handle, output=False) if target_kind else None
        legal = (
            allowed_edge_kinds(source_port, target_port)
            if source_port is not None and target_port is not None
            else set()
        )
        if not legal:
            self.dropped += 1
            return False
        # A port's first channel names the edge it emits (`branch` →
        # condition_branch, `error` → error, ...); fall back to any legal one.
        preferred = EDGE_KIND_BY_PORT_TYPE.get(source_port.types[0])
        edge_kind = preferred if preferred in legal else sorted(legal)[0]
        edge = {
            "id": f"e{len(self.edges) + 1}",
            "source": source,
            # The handle as given: a switch's `case:<name>` resolves to the
            # declared `case` port but must keep its name.
            "source_handle": source_handle,
            "target": target,
            "target_handle": target_handle,
            "kind": edge_kind,
        }
        self.edges.append(edge)
        self.edge_keys.add(key)
        return True

    # ── expressions ──────────────────────────────────────────────────────

    def _inputs_of(self, name: str, *, exclude: set[str] | None = None) -> list[str]:
        """The n8n nodes feeding *name*'s main input, in connection order."""
        return [
            predecessor
            for predecessor in self.main_inputs.get(name, [])
            if exclude is None or predecessor not in exclude
        ]

    def _token(self, sources: list[str], path: list[str], original: str) -> str:
        """A reference to *path* in the item of whichever of *sources* ran."""
        self.tokens.append((list(sources), list(path), original))
        return f"{_REF_OPEN}{len(self.tokens) - 1}{_REF_CLOSE}"

    def input_token(self, name: str, *, exclude: set[str] | None = None, path: list[str] | None = None) -> str:
        """A reference to the whole item *name* receives, or to *path* in it ("" without input)."""
        sources = self._inputs_of(name, exclude=exclude)
        return self._token(sources, path or [], "$json") if sources else ""

    def _segment(self, expression: str, node_id: str, *, current: str) -> str:
        """One ``{{ ... }}`` segment: a reference token, or an inert marker."""
        constant = expression.strip()
        if len(constant) <= _MAX_EXPRESSION_LENGTH and _JS_LITERAL.match(constant):
            if constant[0] in "'\"":
                return _js_string(constant)
            return "" if constant == "null" else constant
        parsed = parse_reference(expression)
        if parsed is not None:
            name, path = parsed
            sources = self._inputs_of(current) if name is None else [name]
            if sources and all(source in self.source for source in sources):
                return self._token(sources, path, expression)
        self._review(
            node_id,
            f"Expression not translated: `{_excerpt(expression)}` — rewrite it with {{{{node.output}}}} references.",
        )
        return _untranslated(expression)

    def text(self, value: Any, node_id: str) -> str:
        """A parameter for a field that accepts ``{{...}}`` references."""
        value = _unwrap_locator(value)
        if not isinstance(value, str):
            return _scalar_text(value)
        if not value.startswith("="):
            if "{{" in value and "}}" in value:
                # Literal `{{` would read as a reference here: it is broken
                # up (see `_inert_value`), which changes the text.
                self._review(
                    node_id,
                    f"Literal text `{_excerpt(value)}` contains `{{{{`, which XTM One reads as a reference.",
                )
            return value
        return "".join(
            self._segment(text, node_id, current=self.current) if is_expression else text
            for is_expression, text in _template_parts(value[1:])
        )

    def literal(self, value: Any, node_id: str, label: str) -> str:
        """A parameter for a field that cannot hold references."""
        value = _unwrap_locator(value)
        if not isinstance(value, str):
            return _scalar_text(value)
        if not value.startswith("="):
            return value
        parts = _template_parts(value[1:])
        if not any(is_expression for is_expression, _text in parts):
            return value[1:]
        self._review(
            node_id,
            f"{label} uses an n8n expression, which this field cannot hold: `{_excerpt(value[1:])}`.",
        )
        return "".join(_untranslated(text) if is_expression else text for is_expression, text in parts)

    def is_expression(self, value: Any) -> bool:
        value = _unwrap_locator(value)
        return isinstance(value, str) and value.startswith("=") and "{{" in value

    # ── reference resolution (final pass) ────────────────────────────────

    def _references(
        self, sources: list[str], path: list[str], seen: set[str]
    ) -> tuple[list[tuple[str, str]], list[str]]:
        """``([(target node, reference)], why_nots)`` for *path* in the item
        of *sources*.

        Several sources mean "whichever ran": n8n reads the item that arrived,
        and here a node that did not run renders empty — so the references,
        side by side, render the one that ran.
        """
        references: list[tuple[str, str]] = []
        problems: list[str] = []
        for source in sources:
            mapped = self.mapped.get(source)
            if mapped is None:
                problems.append(f"`{source}` has no counterpart in this flow")
                continue
            if self.node_by_id[mapped.entry]["kind"] in _PASSTHROUGH_KINDS:
                if source in seen:
                    continue
                seen.add(source)
                inputs = self._inputs_of(source)
                if not inputs:
                    problems.append(f"`{source}` has no input to read through")
                found, missing = self._references(inputs, path, seen)
            else:
                reference, why_not = self._output_path(source, path)
                found = [(self.mapped[source].entry, reference)] if reference else []
                missing = [why_not] if why_not else []
            references.extend(r for r in found if r not in references)
            problems.extend(p for p in missing if p not in problems)
        return references, problems

    def _output_path(self, name: str, path: list[str]) -> tuple[str | None, str | None]:
        """``(reference, why_not)`` for *path* in n8n node *name*'s own item."""
        mapped = self.mapped[name]
        kind = self.node_by_id[mapped.entry]["kind"]
        n8n_type = str(self.source[name].get("type") or "")

        entry = mapped.entry
        short = _short_type(n8n_type)
        root: list[str] | None
        if kind == "trigger.webhook" and short == "webhook":
            if not path:
                # n8n's item is `{body, headers, ...}`; both render as JSON
                # objects, so this template is that item.
                return '{"body": {{' + entry + '.payload}}, "headers": {{' + entry + ".headers}}}", None
            root = {"body": ["payload", *path[1:]], "headers": ["headers", *path[1:]]}.get(path[0])
        elif kind == "trigger.chat":
            if not path:
                return None, "the chat message cannot be read as a whole n8n item"
            root = {"chatInput": ["message", *path[1:]], "files": ["files", *path[1:]]}.get(path[0])
        elif kind == "trigger.schedule":
            # n8n stamps the time several ways; the scheduler publishes one.
            if not path:
                root = ["payload"]
            elif path == ["timestamp"]:
                root = ["payload", "fired_at"]
            else:
                return None, "the schedule only publishes its firing time (`fired_at`)"
        elif kind == "trigger.manual" and short == "chatTrigger":
            return None, f"`{name}` starts the flow manually here: there is no chat message"
        elif kind == "trigger.manual" and short == "executeWorkflowTrigger":
            if not path:
                root = ["payload", "input"]
            else:
                return None, (
                    "a flow called by another receives its input as one text, read with "
                    "{{" + entry + ".payload.input}}"
                )
        elif kind == "trigger.manual":
            root = ["payload", *path]
        elif kind == "util.merge":
            return None, (f"n8n merged the items of several branches in `{name}`; read the branch you need directly")
        elif kind == "agent":
            # The answer is text; n8n's `output` (or `text` for an LLM chain)
            # is the whole of it. A field inside a structured answer is read
            # through the Break node placed behind the agent.
            fields = self.fields_node.get(mapped.entry)
            if not path and fields is not None:
                # n8n's item is `{output: <the parsed answer>}`; the answer
                # renders as JSON, so this template is that same object.
                return '{"output": {{' + mapped.entry + ".output}}}", None
            if not path:
                return None, "the agent's text answer cannot be read as a whole n8n item"
            if len(path) == 1 and path[0] in ("output", "text"):
                root = ["output"]
            elif fields is not None and path[0] in ("output", "text"):
                return "{{" + ".".join([fields, "fields", *path[1:]]) + "}}", None
            else:
                return None, (
                    "the agent's answer is published as text — add a Break node to read a field of a structured answer"
                )
        elif kind == "action.http_request":
            root = ["body", *path]
        elif kind == "util.code":
            root = ["fields", *path] if path else ["output"]
        elif kind == "util.loop":
            root = ["item", *path]
        elif kind == "flow.call":
            root = ["output", *path]
        elif kind in ("trigger.webhook", "trigger.integration_event"):
            # An app's delivery: n8n's item is the event itself.
            root = ["payload", *path]
        elif kind == "trigger.event":
            root = ["fields", *path]
        elif kind == "integration":
            if path:
                return None, f"`{name}` answers with text here, not with n8n's fields"
            root = ["result"]
        elif kind == "action.runner_command" and len(path) == 1 and path[0] == _file_field(self.source[name]):
            # A file n8n read into a binary property: the runner's read_file
            # result holds its text in `content`.
            root = ["result", "content"]
        elif kind == "action.runner_command":
            # n8n's Execute Command item is `{exitCode, stdout, stderr}`; the
            # runner's run_shell result is `{exit_code, stdout, stderr}`.
            root = {
                (): ["result"],
                ("exitCode",): ["result", "exit_code"],
                ("stdout",): ["result", "stdout"],
                ("stderr",): ["result", "stderr"],
            }.get(tuple(path))
        elif kind == "action.ask_user":
            if path:
                return None, f"`{name}`'s answer arrives as one text (`response`)"
            root = ["response"]
        else:
            root = None
        if root is None:
            return None, (f"no equivalent for `{'.'.join(path) or '$json'}` of `{name}` ({short})")
        return "{{" + ".".join([entry, *root]) + "}}", None

    def _upstream_of(self, node_id: str) -> set[str]:
        """The validator's rule: a reference may only name an ancestor."""
        if node_id not in self.ancestry:
            self.ancestry[node_id] = ancestors(node_id, self.edges)
        return self.ancestry[node_id]

    def _resolve_value(self, value: Any, node_id: str) -> Any:
        if isinstance(value, str):
            if _REF_OPEN not in value:
                return value

            def replace(match: re.Match[str]) -> str:
                sources, path, original = self.tokens[int(match.group(1))]
                found, problems = self._references(sources, path, set())
                upstream = self._upstream_of(node_id)
                references = [text for target, text in found if target in upstream]
                if len(references) < len(found):
                    # n8n reads any node that ran earlier, a parallel branch
                    # included; here a reference must name an upstream node.
                    problems.append("it reads a node that does not run before this one here")
                if problems:
                    # A branch whose value cannot be read would render empty
                    # without a word: the node waits for a human.
                    self._review(
                        node_id,
                        f"Reference not translated: `{_excerpt(original)}` ({'; '.join(problems)}).",
                    )
                if not references:
                    return _untranslated(original)
                if len(references) > 1:
                    # Right when the branches exclude each other, wrong when
                    # two of them run: only a human knows which.
                    self._review(
                        node_id,
                        f"`{_excerpt(original)}` reads whichever of {len(references)} upstream branches "
                        "ran; check that they never run together.",
                    )
                return "".join(references)

            return _REF_RE.sub(replace, value)
        if isinstance(value, list):
            return [self._resolve_value(item, node_id) for item in value]
        if isinstance(value, dict):
            return {key: self._resolve_value(item, node_id) for key, item in value.items()}
        return value

    # ── secrets ──────────────────────────────────────────────────────────

    def _scrub(self, value: Any, key: str = "") -> Any:
        """Drop literal secret values from a parameter tree (expressions stay)."""
        if isinstance(value, dict):
            # `{name: "Authorization", value: "Bearer ..."}` pairs.
            pair_name = value.get("name")
            secret_pair = isinstance(pair_name, str) and _SECRET_KEY_RE.search(pair_name)
            out: dict[str, Any] = {}
            for k, v in value.items():
                if secret_pair and k == "value":
                    out[k] = self._blank(v)
                else:
                    out[k] = self._scrub(v, str(k))
            return out
        if isinstance(value, list):
            return [self._scrub(item, key) for item in value]
        if key and _SECRET_KEY_RE.search(key) and isinstance(value, str):
            return self._blank(value)
        if isinstance(value, str):
            # A JSON block typed as text (`jsonHeaders`, `jsonBody`, ...).
            return self.scrub_json_text(value)
        return value

    def scrub_json_text(self, text: str) -> str:
        """*text* with the literal value of every secret-named JSON pair blanked."""

        def blank(match: re.Match[str]) -> str:
            key, separator, value = match.group(1), match.group(2), match.group(3)
            if not _SECRET_KEY_RE.search(key) or not value or "{{" in value or _REF_OPEN in value:
                return match.group(0)
            self.scrubbed.add(value)
            return f'"{key}"{separator}""'

        return _JSON_PAIR.sub(blank, text) if '"' in text else text

    def secret_literal(self, name: Any, value: Any, node_id: str, where: str) -> bool:
        """Whether *value* is a literal secret (and record that it was dropped)."""
        if not isinstance(name, str) or not _SECRET_KEY_RE.search(name):
            return False
        value = _unwrap_locator(value)
        if value in (None, "") or self.is_expression(value):
            return False
        self.scrubbed.add(_scalar_text(value))
        self._review(
            node_id,
            f"The literal value of the `{name}` {where} was not imported: store it as a secret variable and reference it.",
        )
        return True

    def _blank(self, value: Any) -> Any:
        if isinstance(value, str) and value and not value.startswith("="):
            self.scrubbed.add(value)
            return ""
        return value

    # ── connections ──────────────────────────────────────────────────────

    def _index_connections(self) -> list[tuple[str, str, int, str, int]]:
        """Flatten n8n's connection map: (source, type, out, target, in)."""
        flat: list[tuple[str, str, int, str, int]] = []
        connections = self.workflow.get("connections") or {}
        for source, by_type in connections.items():
            if source not in self.source or not isinstance(by_type, dict):
                continue
            for conn_type, outputs in by_type.items():
                if not isinstance(outputs, list):
                    continue
                for out_index, targets in enumerate(outputs):
                    if not isinstance(targets, list):
                        continue
                    for target in targets:
                        if not isinstance(target, dict):
                            continue
                        target_name = target.get("node")
                        if not isinstance(target_name, str) or target_name not in self.source:
                            self.dropped += 1
                            continue
                        in_index = target.get("index")
                        flat.append(
                            (
                                str(source),
                                str(conn_type),
                                out_index,
                                str(target_name),
                                in_index if isinstance(in_index, int) else 0,
                            )
                        )
        return flat

    # ── entry point ──────────────────────────────────────────────────────

    def run(self) -> dict[str, Any]:
        raw_nodes = self.workflow.get("nodes") or []
        if len(raw_nodes) > MAX_NODES:
            raise FlowImportError(f"This workflow holds {len(raw_nodes)} nodes; the limit is {MAX_NODES}.")
        skipped = 0
        for raw in raw_nodes:
            if (
                not isinstance(raw, dict)
                or not isinstance(raw.get("name"), str)
                or not raw["name"]
                or not isinstance(raw.get("type"), str)
                or raw["name"] in self.source
            ):
                skipped += 1
                continue
            self.source[raw["name"]] = raw
        if skipped:
            self._warn(f"{skipped} entries of the file were not readable n8n nodes and were skipped.")

        connections = self._index_connections()
        if len(connections) > MAX_EDGES:
            raise FlowImportError(f"This workflow holds {len(connections)} connections; the limit is {MAX_EDGES}.")
        ai_links: list[tuple[str, str, str]] = []
        has_main_out: set[str] = set()
        for source, conn_type, _out, target, in_index in sorted(connections, key=lambda c: c[4]):
            if conn_type == "main":
                self.main_inputs.setdefault(target, [])
                if source not in self.main_inputs[target]:
                    self.main_inputs[target].append(source)
                self.main_targets.setdefault(source, []).append(target)
                by_index = self.inputs_by_index.setdefault(target, {}).setdefault(in_index, [])
                if source not in by_index:
                    by_index.append(source)
                has_main_out.add(source)
            elif conn_type.startswith("ai_"):
                ai_links.append((source, conn_type, target))
        sub_nodes = {
            source
            for source, _type, _target in ai_links
            if source not in has_main_out and source not in self.main_inputs
        }
        for source, _type, target in ai_links:
            self.ai_target.setdefault(source, target)
        self.sub_nodes = sub_nodes
        # A Break node only takes a JSON object: an answer shaped as a list
        # stays text.
        self.structured = {
            target
            for source, conn_type, _target in ai_links
            if conn_type == "ai_outputParser"
            and source in sub_nodes
            and (target := self._served_by(source)) is not None
            and _is_object_schema(self._parser_schema(self.source[source].get("parameters") or {}))
        }

        for name, raw in self.source.items():
            if name in sub_nodes:
                continue
            self.current = name
            self._convert(raw)

        for source, conn_type, out_index, target, _in in connections:
            if conn_type == "main":
                self._connect_main(source, out_index, target)
        # Wiring a Chat start point to an agent turns the agent's chat on in
        # the builder; an imported wiring does the same.
        for edge in self.edges:
            if (
                self.node_by_id[edge["source"]]["kind"] == "trigger.chat"
                and self.node_by_id[edge["target"]]["kind"] == "agent"
            ):
                self.node_by_id[edge["target"]]["config"]["chat_enabled"] = True
        self._attach_sub_nodes(ai_links, sub_nodes)
        self._finish_classifiers()

        for node in self.nodes:
            node["config"] = self._resolve_value(_inert_value(node["config"]), node["id"])

        if len(self.edges) > MAX_EDGES:
            raise FlowImportError(f"This workflow converts to more than {MAX_EDGES} connections.")

        review_count = 0
        unresolved = 0
        for node in self.nodes:
            notes = self.notes[node["id"]]
            payload = node["source_payload"]
            if notes.review:
                payload["review"] = notes.review
                review_count += 1
                # What to review is easier read next to the original: the
                # parameters, scrubbed of credentials and literal secrets.
                raw = self.source.get(payload["name"])
                if "parameters" not in payload and raw and isinstance(raw.get("parameters"), dict):
                    payload["parameters"] = self._scrub(raw["parameters"])
            if notes.notes:
                payload["notes"] = notes.notes
            if node["kind"] == KIND_UNRESOLVED:
                unresolved += 1

        self._spread_layout()
        self._global_warnings()
        name = _envelope_name(self.workflow) or "Imported n8n workflow"
        return {
            "name": name[:200],
            "description": "Imported from n8n.",
            "graph": {
                "nodes": self.nodes,
                "edges": self.edges,
                "viewport": {"x": 0, "y": 0, "zoom": 1},
            },
            "report": {
                "format": FORMAT_N8N,
                "nodes_imported": len(self.nodes),
                "nodes_unresolved": unresolved,
                "nodes_to_review": review_count,
                "edges_imported": len(self.edges),
                "edges_dropped": self.dropped,
                "warnings": self.warnings,
            },
        }

    def _spread_layout(self) -> None:
        """Keep n8n's layout, at a scale where the nodes do not overlap."""
        for node in self.nodes:
            position = node["position"]
            node["position"] = {"x": position["x"] * _LAYOUT_SCALE, "y": position["y"] * _LAYOUT_SCALE}
        # Notes float behind the graph in n8n too: they keep their place.
        placed = sorted(
            (n for n in self.nodes if n["kind"] != "util.note"),
            key=lambda n: (n["position"]["y"], n["position"]["x"]),
        )
        rows: list[list[dict[str, Any]]] = []
        for node in placed:
            if rows and node["position"]["y"] - rows[-1][0]["position"]["y"] <= _ROW_TOLERANCE:
                rows[-1].append(node)
            else:
                rows.append([node])
        for row in rows:
            row.sort(key=lambda n: n["position"]["x"])
            for left, right in pairwise(row):
                floor = left["position"]["x"] + _MIN_SPACING
                if right["position"]["x"] < floor:
                    right["position"]["x"] = floor

    def _global_warnings(self) -> None:
        if self.credentialed:
            types = ", ".join(sorted(self.credential_types)[:10])
            self._warn(
                f"{len(self.credentialed)} nodes used n8n credentials "
                f"({types}). Credentials are never imported: configure the "
                "matching XTM One integration, secret variable or channel."
            )
        if self.scrubbed:
            self._warn(
                f"{len(self.scrubbed)} literal secret values (API keys, passwords, "
                "authorization headers) were removed from the imported "
                "parameters. Store them as secret variables."
            )
        if self.folded:
            self._warn(
                f"{self.folded} AI sub-nodes (chat models, embeddings, document "
                "loaders, text splitters) were folded into the node they served."
            )
        if self.workflow.get("pinData"):
            self._warn("Pinned test data was not imported.")
        settings = self.workflow.get("settings")
        if isinstance(settings, dict) and settings.get("errorWorkflow"):
            self._warn(
                "The workflow's error workflow setting has no equivalent: wire the nodes' error outputs instead."
            )
        self._warn(
            "n8n runs each node once per item; XTM One passes one value from "
            "step to step. Where the workflow handled lists, iterate them with "
            "a Loop node."
        )

    # ── per-node conversion ──────────────────────────────────────────────

    def _convert(self, raw: dict[str, Any]) -> None:
        name = raw["name"]
        node_type = str(raw["type"])
        params = raw.get("parameters") if isinstance(raw.get("parameters"), dict) else {}
        self._count_credentials(raw)

        if raw.get("disabled") and node_type != _BASE + "stickyNote":
            node_id = self._add_node(raw, "util.reroute", {})
            self._note(
                node_id,
                "Disabled in n8n: imported as a pass-through, as n8n runs it.",
            )
            self._provenance(node_id, raw, keep_parameters=True)
            self.mapped[name] = _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})
            return

        converter = _CONVERTERS.get(node_type)
        mark = self._mark()
        try:
            mapped = converter(self, raw, params) if converter else None
            if mapped is None:
                # An operation its converter does not own, or an app node.
                mapped = _app_node(self, raw, params)
        except _SHAPE_ERRORS:
            self._rollback(mark)
            mapped = self._unresolved(raw, params)
            self._note(
                mapped.entry,
                "Its parameters did not have the shape this importer reads, so it was not converted.",
            )
        if mapped is None:
            mapped = self._unresolved(raw, params)
        self.mapped[name] = mapped
        entry = mapped.entry
        self._provenance(entry, raw, keep_parameters=self.node_by_id[entry]["kind"] == KIND_UNRESOLVED)
        self._node_settings(raw, mapped)

    def _provenance(self, node_id: str, raw: dict[str, Any], *, keep_parameters: bool) -> None:
        payload: dict[str, Any] = {
            "format": FORMAT_N8N,
            "type": raw["type"],
            "name": raw["name"],
        }
        if isinstance(raw.get("typeVersion"), (int, float)):
            payload["type_version"] = raw["typeVersion"]
        if keep_parameters and isinstance(raw.get("parameters"), dict):
            # Never the credentials block, and never a literal secret.
            payload["parameters"] = self._scrub(raw["parameters"])
        self.node_by_id[node_id]["source_payload"] = payload
        if isinstance(raw.get("notes"), str) and raw["notes"].strip():
            self._note(node_id, f"n8n note: {_excerpt(raw['notes'])}")

    def _node_settings(self, raw: dict[str, Any], mapped: _Mapped) -> None:
        """n8n's per-node settings: error handling, retries, run once."""
        entry = mapped.entry
        kind = get_node_kind(self.node_by_id[entry]["kind"])
        has_error_port = kind is not None and any(p.id == mapped.error_handle for p in kind.outputs)
        on_error = raw.get("onError")
        if raw.get("continueOnFail") and not on_error:
            on_error = "continueRegularOutput"
        if on_error == "continueErrorOutput":
            if has_error_port:
                mapped.outputs.setdefault(mapped.regular_outputs, []).append((entry, mapped.error_handle))
            elif self.node_by_id[entry]["kind"] != KIND_UNRESOLVED:
                self._note(
                    entry,
                    "n8n routed this node's errors to an error output; this node "
                    "type has none here, so those connections were dropped.",
                )
        elif on_error == "continueRegularOutput":
            if has_error_port:
                # n8n carries on down the regular output: the error path leads
                # to the same place (`_connect_main` reads -1 with output 0).
                mapped.outputs[-1] = [(entry, mapped.error_handle)]
            self._note(
                entry,
                "n8n continued on the regular output when this node failed; "
                "its error path now leads to the same nodes.",
            )
        if raw.get("retryOnFail"):
            self._note(entry, "n8n retried this node on failure; retries are not carried over.")
        if raw.get("executeOnce"):
            self._note(entry, "n8n ran this node once for all items.")
        if raw.get("alwaysOutputData"):
            self._note(entry, "n8n emitted an empty item when this node produced none.")

    def _connect_main(self, source: str, out_index: int, target: str) -> None:
        source_mapped = self.mapped.get(source)
        target_mapped = self.mapped.get(target)
        if source_mapped is None or target_mapped is None:
            self.dropped += 1
            return
        ends = list(source_mapped.outputs.get(out_index, []))
        if out_index == 0:
            # The "continue on the regular output" error path rides output 0.
            ends += source_mapped.outputs.get(-1, [])
        if not ends:
            self.dropped += 1
            return
        for node_id, handle in ends:
            self._edge(node_id, handle, target_mapped.entry)

    def _unresolved(self, raw: dict[str, Any], params: dict[str, Any]) -> _Mapped:
        node_type = str(raw["type"])
        node_id = self._add_node(raw, KIND_UNRESOLVED, {"source_type": node_type})
        self._note(node_id, _UNRESOLVED_HINTS.get(node_type, _default_hint(node_type)))
        # Every output keeps its connection, error output included: the graph
        # stays legible, and the node cannot run anyway until it is replaced.
        main = _get(self.workflow, "connections", raw["name"], "main", default=[])
        count = max(len(main) if isinstance(main, list) else 0, 1) + 1
        return _Mapped(
            entry=node_id,
            outputs={i: [(node_id, "out")] for i in range(count)},
            regular_outputs=count,
        )

    # ── AI sub-nodes ─────────────────────────────────────────────────────

    def _served_by(self, name: str) -> str | None:
        """The main node a sub-node ends up serving.

        A sub-node may serve another sub-node (a structured parser behind an
        auto-fixing parser): follow the chain to the first main node.
        """
        seen: set[str] = set()
        while name in self.sub_nodes and name not in seen:
            seen.add(name)
            name = self.ai_target.get(name, "")
        return name or None

    def structured_answer(self, raw: dict[str, Any], mapped: _Mapped) -> _Mapped:
        """Put a Break node behind an agent whose answer is a JSON object.

        n8n hands a parsed object on (``$json.output.field``); an agent
        publishes its answer as text. The Break node parses it once, every
        regular output of the agent leaves through it, and references to a
        field resolve to ``{{break.fields.field}}``.
        """
        agent_id = mapped.entry
        break_id = self._add_node(
            raw,
            "util.break",
            {"value": self._token([raw["name"]], ["output"], "$json.output")},
            id_hint=f"{raw['name']} fields",
            label=f"{raw['name']} — fields",
            offset=(0, -160),
        )
        self._edge(agent_id, "out", break_id)
        self.fields_node[agent_id] = break_id
        self._note(
            break_id,
            "n8n handed the agent's structured answer on as an object; this node exposes its fields.",
        )
        return _Mapped(entry=agent_id, outputs={0: [(break_id, "out")]})

    def _attach_sub_nodes(self, ai_links: list[tuple[str, str, str]], sub_nodes: set[str]) -> None:
        converted: dict[str, str] = {}
        for source, conn_type, target in ai_links:
            if source not in sub_nodes:
                continue
            raw = self.source[source]
            self.current = source
            parent = self._served_by(target)
            parent = parent if parent in self.mapped else None
            parent_id = self.mapped[parent].entry if parent else None
            parent_kind = self.node_by_id[parent_id]["kind"] if parent_id else None
            sub_type = str(raw.get("type"))
            self._count_credentials(raw)
            mark = self._mark()
            try:
                if self._attach_one(raw, sub_type, conn_type, parent_id, parent_kind, converted):
                    continue
            except _SHAPE_ERRORS:
                self._rollback(mark)
                for name, node_id in list(converted.items()):
                    if node_id not in self.node_by_id:
                        del converted[name]
            # Embeddings, loaders, splitters, retrievers — or a sub-node whose
            # parent has no socket for it: its role is the parent's.
            self.folded += 1
            if parent_id is not None and parent_kind != KIND_UNRESOLVED:
                self._note(
                    parent_id,
                    f"n8n attached `{source}` ({_short_type(sub_type)}) here; it "
                    "has no separate equivalent and was folded into this node.",
                )

    def _attach_one(
        self,
        raw: dict[str, Any],
        sub_type: str,
        conn_type: str,
        parent_id: str | None,
        parent_kind: str | None,
        converted: dict[str, str],
    ) -> bool:
        """Convert one sub-node link; False when it folds into its parent."""
        if parent_id is None:
            return False
        if conn_type == "ai_languageModel" and parent_kind == "agent":
            self._fold_model(raw, parent_id)
            return True
        if conn_type == "ai_languageModel" and parent_kind in ("condition.if", "condition.switch"):
            node_id = converted.get(raw["name"]) or self._model_node(raw)
            converted[raw["name"]] = node_id
            self._edge(node_id, "provides", parent_id, "llm")
            return True
        if parent_kind == "agent":
            return self._agent_capability(raw, sub_type, conn_type, parent_id, converted)
        return False

    def _fold_model(self, raw: dict[str, Any], agent_id: str) -> None:
        self.folded += 1
        params = raw.get("parameters") or {}
        config = self.node_by_id[agent_id]["config"]
        temperature = _get(params, "options", "temperature")
        if isinstance(temperature, (int, float)) and "temperature" not in config:
            config["temperature"] = max(0.0, min(2.0, float(temperature)))
        max_tokens = _get(params, "options", "maxTokens") or _get(params, "options", "maxTokensToSample")
        if isinstance(max_tokens, int) and max_tokens > 0:
            config.setdefault("max_tokens", max_tokens)
        model = _unwrap_locator(params.get("model") or params.get("modelName"))
        described = f"`{model}` " if isinstance(model, str) and model else ""
        self._note(
            agent_id,
            f"n8n ran this step on the chat model {described}"
            f"({_short_type(str(raw.get('type')))}). The agent's own model "
            "applies here; set Model to override it.",
        )

    def _model_node(self, raw: dict[str, Any]) -> str:
        params = raw.get("parameters") or {}
        config: dict[str, Any] = {}
        temperature = _get(params, "options", "temperature")
        if isinstance(temperature, (int, float)):
            config["temperature"] = max(0.0, min(2.0, float(temperature)))
        node_id = self._add_node(raw, "capability.llm", config)
        self._provenance(node_id, raw, keep_parameters=False)
        model = _unwrap_locator(params.get("model") or params.get("modelName"))
        if isinstance(model, str) and model:
            self._note(node_id, f"n8n used the model `{model}`: pick it under Model.")
        return node_id

    def _agent_capability(
        self,
        raw: dict[str, Any],
        sub_type: str,
        conn_type: str,
        agent_id: str,
        converted: dict[str, str],
    ) -> bool:
        name = raw["name"]
        short = _short_type(sub_type)
        params = raw.get("parameters") or {}
        if name in converted:
            node_id = converted[name]
        elif conn_type == "ai_memory":
            node_id = self._add_node(raw, "capability.memory", {"short_term": True})
            self._note(
                node_id,
                f"n8n's `{short}` kept the conversation; short-term memory does the same here.",
            )
        elif conn_type in ("ai_tool", "ai_retriever") and (
            short.startswith(("vectorStore", "retriever")) or short == "toolVectorStore"
        ):
            node_id = self._add_node(raw, "capability.knowledge_base", {})
            self._note(
                node_id,
                "n8n searched a vector store; pick the XTM One knowledge base holding the same documents.",
            )
        elif conn_type == "ai_tool" and (app := n8n_apps.app_for(short)) is not None:
            node_id = self._add_node(raw, "capability.integration", {})
            self._note(
                node_id,
                f"n8n gave the agent the {app.label} tool: pick the {app.label} integration "
                f"(`{app.provider}`); the agent chooses among its operations.",
            )
        elif conn_type == "ai_tool":
            node_id = self._add_node(raw, "capability.tool", {})
            hint = {
                "mcpClientTool": "register the MCP server in XTM One, then pick its tools",
                "toolWorkflow": "a sub-flow, a custom tool or an assignment can play this part",
                "toolCode": "a custom tool runs code in the platform sandbox",
                "toolHttpRequest": "an integration or a custom tool can make this call",
                "toolThink": "XTM One agents reason natively; this tool can usually be deleted",
                "toolCalculator": "an Expression node computes, or the agent's code interpreter",
                "toolWikipedia": "the agent's web search covers it",
                "toolSerpApi": "the agent's web search or the Google Search integration covers it",
                "httpRequestTool": "an integration or a custom tool can make this call",
                "executeCommandTool": "a custom tool, or a Runner command node in the flow, runs commands",
                "emailSendTool": "the agent sends email through its email channel",
            }.get(short) or (
                "pick the XTM One tool, MCP tool or integration that does the same"
                if str(raw.get("type")).startswith(_LANGCHAIN)
                else n8n_apps.outside_catalog_hint(short)
            )
            endpoint = params.get("sseEndpoint") or params.get("endpointUrl")
            if isinstance(endpoint, str) and endpoint and not endpoint.startswith("="):
                hint += f" (n8n used `{_excerpt(endpoint)}`)"
            self._note(node_id, f"n8n tool `{short}`: {hint.rstrip('.')}.")
        elif conn_type == "ai_outputParser" and short == "outputParserStructured":
            schema = self._parser_schema(params)
            node_id = self._add_node(raw, "capability.structured_output", {"schema": schema} if schema else {})
            if not schema:
                self._review(node_id, "The n8n output schema could not be read.")
        else:
            return False
        if name not in converted:
            self._provenance(node_id, raw, keep_parameters=bool(self.notes[node_id].review))
            converted[name] = node_id
        handle = {
            "ai_memory": "memory",
            "ai_outputParser": "output",
        }.get(conn_type)
        if handle is None:
            handle = {
                "capability.knowledge_base": "knowledge_bases",
                "capability.integration": "integrations",
            }.get(self.node_by_id[node_id]["kind"], "tools")
        self._edge(node_id, "provides", agent_id, handle)
        return True

    @staticmethod
    def _parser_schema(params: dict[str, Any]) -> dict[str, Any] | None:
        manual = params.get("inputSchema") or params.get("jsonSchema")
        example = params.get("jsonSchemaExample")
        if params.get("schemaType") == "manual" or (manual and not example):
            source, from_example = manual, False
        else:
            source, from_example = example, True
        if not isinstance(source, str):
            return None
        try:
            parsed = json.loads(source.removeprefix("="))
        except json.JSONDecodeError:
            return None
        if from_example:
            return _schema_from_example(parsed)
        return parsed if isinstance(parsed, dict) else None

    def _finish_classifiers(self) -> None:
        """An LLM switch with no model wired would match its cases literally."""
        for name, mapped in self.mapped.items():
            node_type = str(self.source[name].get("type"))
            if _short_type(node_type) not in ("textClassifier", "sentimentAnalysis"):
                continue
            entry = mapped.entry
            wired = any(e["target"] == entry and e["target_handle"] == "llm" for e in self.edges)
            if not wired:
                self._review(
                    entry,
                    "n8n classified with a chat model that did not come across: wire an LLM node into this switch.",
                )

    # ── conditions ───────────────────────────────────────────────────────

    def _conditions(self, block: Any) -> tuple[list[dict], str]:
        """Normalise an If/Filter/Switch condition block to (conditions, and|or)."""
        if not isinstance(block, dict):
            return [], "and"
        if isinstance(block.get("conditions"), list):  # v2+
            conditions = []
            case_sensitive = _get(block, "options", "caseSensitive", default=True)
            for item in block["conditions"]:
                if not isinstance(item, dict):
                    continue
                operator = item.get("operator") if isinstance(item.get("operator"), dict) else {}
                conditions.append(
                    {
                        "left": item.get("leftValue", ""),
                        "right": item.get("rightValue", ""),
                        "type": operator.get("type") or "string",
                        "operation": operator.get("operation") or "equals",
                        "case_sensitive": case_sensitive is not False,
                    }
                )
            return conditions, "or" if block.get("combinator") == "or" else "and"

        # v1: one list per data type, combined by `combineOperation`.
        v1_names = {
            "equal": "equals",
            "notEqual": "notEquals",
            "smaller": "lt",
            "smallerEqual": "lte",
            "larger": "gt",
            "largerEqual": "gte",
            "isEmpty": "empty",
            "isNotEmpty": "notEmpty",
            "after": "after",
            "before": "before",
        }
        defaults = {"number": "smaller", "string": "equal", "boolean": "equal"}
        conditions = []
        for data_type in ("boolean", "dateTime", "number", "string"):
            for item in block.get(data_type) or []:
                if not isinstance(item, dict):
                    continue
                operation = item.get("operation") or defaults.get(data_type, "equal")
                conditions.append(
                    {
                        "left": item.get("value1", ""),
                        # An export leaves out a value at its default.
                        "right": item.get("value2", _V1_DEFAULT_VALUE.get(data_type, "")),
                        "type": data_type,
                        "operation": v1_names.get(operation, operation),
                        "case_sensitive": True,
                    }
                )
        return conditions, "and"

    def _condition_config(self, condition: dict[str, Any], node_id: str) -> tuple[dict[str, Any], bool]:
        """A `condition.if` config for one condition, and whether to negate it."""
        left = self.text(condition["left"], node_id)
        right_raw = condition["right"]
        right = self.text(right_raw, node_id)
        operation = str(condition["operation"])
        data_type = str(condition["type"])
        right_is_literal = not self.is_expression(right_raw)

        def regex(pattern: str) -> dict[str, Any]:
            return {"left": left, "operator": "matches", "right": pattern}

        def literal_pattern(prefix: str, suffix: str) -> dict[str, Any] | None:
            if not right_is_literal:
                return None
            flags = "" if condition["case_sensitive"] else "(?i)"
            return regex(f"{flags}{prefix}{re.escape(right)}{suffix}")

        plain = {
            "equals": "equals",
            "notEquals": "not_equals",
            "contains": "contains",
            "notContains": "not_contains",
            "gt": "gt",
            "lt": "lt",
            "empty": "is_empty",
            "notEmpty": "is_not_empty",
            "exists": "is_not_empty",
            "notExists": "is_empty",
        }
        config: dict[str, Any] | None = None
        negate = False
        string_like = data_type in ("string", "any")
        if data_type == "boolean" and operation in ("true", "false"):
            config = {"left": left, "operator": "equals", "right": operation}
        elif data_type in ("array", "object", "dateTime") and operation not in (
            "contains",
            "notContains",
            "exists",
            "notExists",
        ):
            # Lengths, emptiness of a collection, date ordering: no operator
            # here reads them (an empty list renders "[]", not "").
            config = None
        elif operation in ("startsWith", "notStartsWith"):
            config = literal_pattern("^", "")
            negate = operation == "notStartsWith"
        elif operation in ("endsWith", "notEndsWith"):
            config = literal_pattern("", "$")
            negate = operation == "notEndsWith"
        elif operation in ("regex", "notRegex"):
            negate = operation == "notRegex"
            if right_is_literal:
                pattern = _python_regex(right, case_sensitive=condition["case_sensitive"])
                config = regex(pattern) if pattern is not None else None
            else:
                config = regex(right)
        elif operation in ("gte", "lte"):
            # a >= b  ⇔  not (a < b)
            config = {"left": left, "operator": "lt" if operation == "gte" else "gt", "right": right}
            negate = True
        elif operation in plain:
            if (
                string_like
                and not condition["case_sensitive"]
                and operation in ("equals", "notEquals", "contains", "notContains")
            ):
                anchors = ("^", "$") if operation in ("equals", "notEquals") else ("", "")
                config = literal_pattern(*anchors)
                negate = operation in ("notEquals", "notContains")
            else:
                config = {"left": left, "operator": plain[operation], "right": right}
        if operation in ("exists", "notExists") and config is not None:
            self._note(
                node_id,
                "n8n's exists / does not exist reads here as is not empty / is empty.",
            )
        if config is None:
            self._review(
                node_id,
                f"The n8n condition `{data_type} {operation}` has no equivalent operator here.",
            )
            config = {"left": left, "operator": "equals", "right": right}
        if config["operator"] in ("is_empty", "is_not_empty"):
            config.pop("right", None)
        return config, negate

    def _condition_chain(
        self,
        raw: dict[str, Any],
        conditions: list[dict[str, Any]],
        combinator: str,
        *,
        id_hint: str | None = None,
        label: str | None = None,
        y_offset: float = 0,
    ) -> tuple[str, list[tuple[str, str]], list[tuple[str, str]]]:
        """One `condition.if` per condition, chained by the combinator.

        Returns ``(entry, pass_exits, fail_exits)``. XTM One's If tests one
        condition; n8n's tests several, which is exactly a chain: under AND a
        node's True leads to the next test and any False is a fail, under OR
        the reverse.
        """
        meaningful = [
            c
            for c in conditions
            if not (c["operation"] == "equals" and _scalar_text(c["left"]) == "" and _scalar_text(c["right"]) == "")
        ]
        if not meaningful:
            node_id = self._add_node(
                raw,
                "util.reroute",
                {},
                id_hint=id_hint,
                label=label,
                offset=(0, y_offset),
            )
            self._note(node_id, "The n8n condition was empty, so it always held.")
            return node_id, [(node_id, "out")], []

        ids: list[str] = []
        exits: list[tuple[str, str, str]] = []  # (id, pass, fail)
        base_label = label or raw["name"]
        for index, condition in enumerate(meaningful):
            node_id = self._add_node(
                raw,
                "condition.if",
                {},
                id_hint=id_hint if index == 0 else f"{id_hint or raw['name']}_{index + 1}",
                label=(base_label if len(meaningful) == 1 else f"{base_label} ({index + 1}/{len(meaningful)})"),
                offset=(0, y_offset + 140 * index),
            )
            config, negate = self._condition_config(condition, node_id)
            self.node_by_id[node_id]["config"] = config
            ids.append(node_id)
            exits.append((node_id, "false" if negate else "true", "false" if not negate else "true"))
        if len(ids) > 1:
            self._note(
                ids[0],
                f"n8n tested {len(ids)} conditions combined with "
                f"{combinator.upper()}; each is one If node here, chained.",
            )
        if combinator == "or":
            for (node_id, _pass, fail), (next_id, _p, _f) in pairwise(exits):
                self._edge(node_id, fail, next_id)
            return ids[0], [(i, p) for i, p, _f in exits], [(exits[-1][0], exits[-1][2])]
        for (node_id, passed, _fail), (next_id, _p, _f) in pairwise(exits):
            self._edge(node_id, passed, next_id)
        return ids[0], [(exits[-1][0], exits[-1][1])], [(i, f) for i, _p, f in exits]


# ─── Converters ──────────────────────────────────────────────────────────────


def _version(raw: dict[str, Any]) -> float:
    return _number(raw.get("typeVersion"), 1)


def _single(importer: _N8nImporter, raw: dict[str, Any], kind: str, config: dict[str, Any]) -> _Mapped:
    node_id = importer._add_node(raw, kind, config)
    return _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})


def _manual_trigger(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    return _single(imp, raw, "trigger.manual", {})


def _sub_workflow_trigger(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    mapped = _single(imp, raw, "trigger.manual", {})
    imp._note(
        mapped.entry,
        "Started by another n8n workflow: in XTM One a Flow call node starts "
        "this flow, and its input arrives as the payload.",
    )
    return mapped


def _webhook(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "trigger.webhook", {})
    path = imp.literal(params.get("path", ""), node_id, "The webhook path").strip("/")
    imp.node_by_id[node_id]["config"] = {"path": path} if path else {}
    method = str(params.get("httpMethod") or "GET").upper()
    if method != "POST":
        imp._note(node_id, f"n8n listened for {method}; XTM One webhooks receive POST requests.")
    if ":" in path:
        imp._note(node_id, "Path parameters (`:name`) are matched literally here.")
    if params.get("authentication") not in (None, "none"):
        imp._note(
            node_id,
            "n8n checked a credential on each call; XTM One signs the webhook "
            "with the secret minted when the flow is activated.",
        )
    if params.get("responseMode") in ("responseNode", "lastNode"):
        imp._note(
            node_id,
            "n8n answered the caller with the workflow's result; XTM One "
            "acknowledges the call as soon as it is received.",
        )
    return _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})


def _chat_trigger(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    targets = imp.main_targets.get(raw["name"], [])
    to_agents = bool(targets) and all(str(imp.source[t].get("type")) in _AGENT_TYPES for t in targets)
    greeting = params.get("initialMessages") or _get(params, "options", "initialMessages")
    if not to_agents:
        mapped = _single(imp, raw, "trigger.manual", {})
        imp._note(
            mapped.entry,
            "n8n's chat trigger fed a node other than an AI agent; a chat start "
            "point only connects to agents here, so this starts the flow manually.",
        )
        return mapped
    node_id = imp._add_node(raw, "trigger.chat", {})
    if greeting:
        imp.node_by_id[node_id]["config"]["greeting"] = imp.literal(greeting, node_id, "The greeting")
    return _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})


def _schedule(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "trigger.schedule", {})
    node_type = _short_type(str(raw["type"]))
    review: list[str] = []
    if node_type == "scheduleTrigger":
        intervals = _get(params, "rule", "interval", default=[{}]) or [{}]
        cron = _schedule_rule_cron(intervals[0] if isinstance(intervals[0], dict) else {}, review)
        if len(intervals) > 1:
            review.append(f"n8n fired on {len(intervals)} schedule rules; only the first is kept.")
    elif node_type == "cron":
        items = _get(params, "triggerTimes", "item", default=[]) or []
        if not items:
            cron = ""
            review.append("The n8n Cron node had no trigger time, so it never fired.")
        else:
            cron = _cron_item(items[0] if isinstance(items[0], dict) else {}, review)
            if len(items) > 1:
                review.append(f"n8n fired at {len(items)} times; only the first is kept.")
    else:  # interval
        amount = max(1, _int(params.get("interval"), 1))
        unit = params.get("unit") or "seconds"
        if unit == "hours":
            cron = f"0 */{amount} * * *"
        elif unit == "minutes":
            cron = f"*/{amount} * * * *"
        else:
            cron = "* * * * *"
            review.append(f"n8n fired every {amount} seconds; the scheduler's finest step is one minute.")
    seconds = cron.split()[0] if len(cron.split()) == 6 else "0"
    if seconds not in ("0", "00"):
        review.append(f"n8n fired at second `{seconds}`; the scheduler's finest step is one minute.")
    cron = _five_field_cron(cron)
    if cron and not _cron_is_valid(cron):
        review.append(f"The schedule `{cron}` is not a valid cron expression.")
        cron = ""
    config: dict[str, Any] = {"cron": cron} if cron else {}
    timezone = _get(imp.workflow, "settings", "timezone")
    if isinstance(timezone, str) and timezone and timezone != "DEFAULT":
        config["timezone"] = timezone
    imp.node_by_id[node_id]["config"] = config
    for message in review:
        imp._review(node_id, message)
    return _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})


def _schedule_rule_cron(rule: dict[str, Any], review: list[str]) -> str:
    unit = rule.get("field") or "days"
    minute = _int(rule.get("triggerAtMinute"), 0) % 60
    hour = _int(rule.get("triggerAtHour"), 0) % 24
    if unit == "cronExpression":
        expression = rule.get("expression")
        if not isinstance(expression, str) or expression.startswith("="):
            review.append("The n8n cron expression could not be read.")
            return ""
        return expression.strip()
    if unit == "seconds":
        review.append(
            f"n8n fired every {_int(rule.get('secondsInterval'), 30)} seconds; "
            "the scheduler's finest step is one minute."
        )
        return "* * * * *"
    if unit == "minutes":
        step = max(1, _int(rule.get("minutesInterval"), 5))
        return f"*/{step} * * * *" if step > 1 else "* * * * *"
    if unit == "hours":
        step = max(1, _int(rule.get("hoursInterval"), 1))
        return f"{minute} */{step} * * *" if step > 1 else f"{minute} * * * *"
    if unit == "days":
        step = max(1, _int(rule.get("daysInterval"), 1))
        return f"{minute} {hour} {'*/' + str(step) if step > 1 else '*'} * *"
    if unit == "weeks":
        step = max(1, _int(rule.get("weeksInterval"), 1))
        days = rule.get("triggerAtDay")
        if not isinstance(days, list) or not days:
            days = [0]
        weekdays = ",".join(str(_int(d, 0) % 7) for d in days)
        if step > 1:
            review.append(f"n8n fired every {step} weeks; this schedule fires weekly.")
        return f"{minute} {hour} * * {weekdays}"
    if unit == "months":
        step = max(1, _int(rule.get("monthsInterval"), 1))
        day = min(31, max(1, _int(rule.get("triggerAtDayOfMonth"), 1)))
        return f"{minute} {hour} {day} {'*/' + str(step) if step > 1 else '*'} *"
    review.append(f"The n8n schedule unit `{unit}` is not supported.")
    return ""


def _cron_item(item: dict[str, Any], review: list[str]) -> str:
    """One entry of the legacy Cron node's `triggerTimes`."""
    mode = item.get("mode") or "everyDay"
    if mode in ("everyDay", "everyWeek", "everyMonth") and "hour" not in item:
        review.append("The n8n Cron left its hour at the default; this assumes 14:00.")
    minute = _int(item.get("minute"), 0) % 60
    hour = _int(item.get("hour"), 14) % 24
    if mode == "everyMinute":
        return "* * * * *"
    if mode == "everyHour":
        return f"{minute} * * * *"
    if mode == "everyWeek":
        return f"{minute} {hour} * * {_int(item.get('weekday'), 1) % 7}"
    if mode == "everyMonth":
        return f"{minute} {hour} {min(31, max(1, _int(item.get('dayOfMonth'), 1)))} * *"
    if mode == "everyX":
        value = max(1, _int(item.get("value"), 2))
        unit = item.get("unit")
        if unit is None:
            unit = "hours"
            review.append("The n8n Cron left its unit at the default; this assumes hours.")
        return f"0 */{value} * * *" if unit == "hours" else f"*/{value} * * * *"
    if mode == "custom":
        expression = item.get("cronExpression")
        return expression.strip() if isinstance(expression, str) else ""
    return f"{minute} {hour} * * *"


def _if(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    conditions, combinator = imp._conditions(params.get("conditions"))
    if params.get("combineOperation") == "any":
        combinator = "or"
    if _get(params, "options", "ignoreCase") or _get(params, "options", "caseSensitive") is False:
        for condition in conditions:
            condition["case_sensitive"] = False
    entry, passed, failed = imp._condition_chain(raw, conditions, combinator)
    return _Mapped(entry=entry, outputs={0: passed, 1: failed}, regular_outputs=2)


def _filter(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    conditions, combinator = imp._conditions(params.get("conditions"))
    if _get(params, "options", "ignoreCase") or _get(params, "options", "caseSensitive") is False:
        for condition in conditions:
            condition["case_sensitive"] = False
    entry, passed, _failed = imp._condition_chain(raw, conditions, combinator)
    imp._note(entry, "n8n dropped what did not pass the filter: the False branch leads nowhere.")
    return _Mapped(entry=entry, outputs={0: passed})


def _switch(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped | None:
    version = _version(raw)
    if params.get("mode") == "expression":
        count = max(1, _int(params.get("numberOutputs") or params.get("outputsAmount"), 4))
        node_id = imp._add_node(raw, "condition.switch", {})
        cases = {str(i): str(i) for i in range(count)}
        imp.node_by_id[node_id]["config"] = {
            "value": imp.text(params.get("output", ""), node_id),
            "cases": cases,
        }
        imp._note(node_id, "n8n picked the output by its index; case `N` is output N.")
        return _Mapped(
            entry=node_id,
            outputs={i: [(node_id, f"case:{i}")] for i in range(count)},
            regular_outputs=count,
        )

    # Normalise every rule to (conditions, combinator, output index, name).
    rules: list[tuple[list[dict], str, int, str | None]] = []
    fallback: int | None = None
    ignore_case = bool(_get(params, "options", "ignoreCase"))
    if version >= 3:
        values = _get(params, "rules", "values", default=[]) or []
        for index, rule in enumerate(values):
            if not isinstance(rule, dict):
                continue
            conditions, combinator = imp._conditions(rule.get("conditions"))
            name = rule.get("outputKey") if rule.get("renameOutput") else None
            rules.append((conditions, combinator, index, name if isinstance(name, str) else None))
        option = _get(params, "options", "fallbackOutput")
        if option == "extra":
            fallback = len(values)
        elif isinstance(option, int) and option >= 0:
            fallback = option
        regular = len(values) + (1 if option == "extra" else 0)
    else:
        data_type = params.get("dataType") or "number"
        v1_names = {
            "equal": "equals",
            "notEqual": "notEquals",
            "smaller": "lt",
            "smallerEqual": "lte",
            "larger": "gt",
            "largerEqual": "gte",
        }
        default_op = "equal"
        for rule in _get(params, "rules", "rules", default=[]) or []:
            if not isinstance(rule, dict):
                continue
            operation = rule.get("operation") or default_op
            condition = {
                "left": params.get("value1", ""),
                "right": rule.get("value2", _V1_DEFAULT_VALUE.get(data_type, "")),
                "type": data_type,
                "operation": v1_names.get(operation, operation),
                "case_sensitive": True,
            }
            rules.append(([condition], "and", _int(rule.get("output"), 0), None))
        option = params.get("fallbackOutput")
        fallback = option if isinstance(option, int) and option >= 0 else None
        regular = max([r[2] for r in rules] + [fallback if fallback is not None else -1]) + 1
    for conditions, *_rest in rules:
        if ignore_case:
            for condition in conditions:
                condition["case_sensitive"] = False

    if not rules:
        return None  # rules left at n8n's defaults are not in the export
    simple = all(
        len(conds) == 1
        and conds[0]["operation"] == "equals"
        and conds[0]["case_sensitive"]
        and json.dumps(conds[0]["left"]) == json.dumps(rules[0][0][0]["left"])
        for conds, *_rest in rules
    )
    outputs: dict[int, list[tuple[str, str]]] = {}
    if simple:
        node_id = imp._add_node(raw, "condition.switch", {})
        cases: dict[str, str] = {}
        for conds, _combinator, output, name in rules:
            case = name or f"output {output}"
            while case in cases:
                case += "'"
            cases[case] = imp.text(conds[0]["right"], node_id)
            outputs.setdefault(output, []).append((node_id, f"case:{case}"))
        imp.node_by_id[node_id]["config"] = {
            "value": imp.text(rules[0][0][0]["left"], node_id),
            "cases": cases,
        }
        entry = node_id
        if fallback is not None:
            outputs.setdefault(fallback, []).append((node_id, "default"))
    else:
        # Rules that are not all "value equals X": n8n's first-match switch
        # is a cascade of If tests, rule after rule.
        entry = ""
        pending_fail: list[tuple[str, str]] = []
        for index, (conds, combinator, output, _name) in enumerate(rules):
            rule_entry, passed, failed = imp._condition_chain(
                raw,
                conds,
                combinator,
                id_hint=raw["name"] if index == 0 else f"{raw['name']}_rule_{index + 1}",
                label=f"{raw['name']} — rule {index + 1}",
                y_offset=index * 160 * max(1, len(conds)),
            )
            entry = entry or rule_entry
            for node_id, handle in pending_fail:
                imp._edge(node_id, handle, rule_entry)
            pending_fail = failed
            outputs.setdefault(output, []).extend(passed)
        if fallback is not None:
            outputs.setdefault(fallback, []).extend(pending_fail)
        imp._note(
            entry,
            "n8n's rules compared more than one value or used other operators "
            "than equals: each rule is an If test here, tried in order.",
        )
    if _get(params, "options", "allMatchingOutputs"):
        imp._review(
            entry,
            "n8n sent items to every matching output; here only the first matching rule's branch runs.",
        )
    return _Mapped(entry=entry, outputs=outputs, regular_outputs=max(regular, 1))


def _merge(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    mode = params.get("mode") or "append"
    strategy = "concat" if mode == "append" else "wait_all"
    mapped = _single(imp, raw, "util.merge", {"strategy": strategy})
    if mode not in ("append", "wait", "passThrough"):
        detail = params.get("combineBy") or params.get("joinMode") or mode
        imp._note(
            mapped.entry,
            f"n8n combined its inputs (`{detail}`); here the node waits for "
            "every branch and publishes what each produced.",
        )
    return mapped


def _wait(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped | None:
    resume = params.get("resume") or "timeInterval"
    if resume == "form":
        return _ask_user(imp, raw, params)
    if resume == "webhook":
        mapped = _single(imp, raw, "util.wait_event", {})
        imp._review(
            mapped.entry,
            "n8n resumed when its resume URL was called: declare an event type, have the caller "
            "start a flow that emits it, and pick it here.",
        )
        return mapped
    if resume == "specificTime":
        mapped = _single(imp, raw, "util.delay", {})
        imp._review(
            mapped.entry,
            f"n8n waited until `{_excerpt(_scalar_text(params.get('dateTime')))}`: set the delay that gets there.",
        )
        return mapped
    if resume != "timeInterval":
        return None
    node_id = imp._add_node(raw, "util.delay", {})
    amount_raw = params.get("amount", 1)
    unit = params.get("unit")
    if unit is None:
        unit = "seconds" if _version(raw) >= 1.1 else "hours"
    if "amount" not in params or "unit" not in params:
        # An export leaves out a value at its default, and those defaults
        # changed between versions of the node.
        imp._review(
            node_id,
            f"The n8n wait left its duration at a default; this assumes {_scalar_text(amount_raw)} {unit}.",
        )
    if imp.is_expression(amount_raw):
        imp._review(node_id, "The n8n wait duration was an expression.")
        return _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})
    seconds = math.ceil(_number(amount_raw, 1) * _SECONDS_PER_UNIT.get(str(unit), 1))
    seconds = max(1, seconds)
    for suffix, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds % size == 0:
            duration = f"{seconds // size}{suffix}"
            break
    imp.node_by_id[node_id]["config"] = {"duration": duration}
    return _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})


def _loop(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    name = raw["name"]
    node_id = imp._add_node(raw, "util.loop", {})
    # What the loop iterates is what reached it from outside its own body.
    body: set[str] = set()
    frontier = list(imp.main_targets.get(name, []))
    while frontier:
        current = frontier.pop()
        if current in body or current == name:
            continue
        body.add(current)
        frontier.extend(imp.main_targets.get(current, []))
    # A list cannot be read from several branches at once: the first one.
    outside = imp._inputs_of(name, exclude=body)[:1]
    items = imp._token(outside, [], "$json") if outside else ""
    imp.node_by_id[node_id]["config"] = {"items": items} if items else {}
    imp._review(
        node_id,
        "n8n looped over the items it received; check that Items names the list to iterate.",
    )
    batch = _int(params.get("batchSize"), 1)
    if batch > 1:
        imp._note(node_id, f"n8n processed {batch} items per pass; this loop takes one at a time.")
    if _version(raw) >= 3:
        return _Mapped(
            entry=node_id,
            outputs={0: [(node_id, "done")], 1: [(node_id, "each")]},
            regular_outputs=2,
        )
    # Before Loop Over Items, output 0 carried each batch and a second output,
    # when there was one, the end.
    return _Mapped(
        entry=node_id,
        outputs={0: [(node_id, "each")], 1: [(node_id, "done")]},
        regular_outputs=2,
    )


def _no_op(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    return _single(imp, raw, "util.reroute", {})


def _sticky(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    content = params.get("content")
    node_id = imp._add_node(raw, "util.note", {"text": content if isinstance(content, str) else ""}, label="Note")
    return _Mapped(entry=node_id, outputs={})


_SET_CODE = """import json

# Generated from an n8n "Edit Fields (Set)" node: each entry of `inputs` is one
# field (a dotted name builds a nested object, as in n8n).
TYPES = {types}
INCLUDE_INPUT = {include}
DOT_NOTATION = {dots}


def cast(value, kind):
    if not isinstance(value, str):
        return value
    if kind == "number":
        try:
            number = float(value)
        except ValueError:
            return None
        return int(number) if number.is_integer() else number
    if kind == "boolean":
        return value.strip().lower() in ("true", "1", "yes")
    if kind in ("object", "array"):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


result = {{}}
base = inputs.pop("_input", None)
if INCLUDE_INPUT and isinstance(base, str):
    try:
        base = json.loads(base)
    except ValueError:
        base = None
if INCLUDE_INPUT and isinstance(base, dict):
    result.update(base)
for name, value in inputs.items():
    target = result
    *parents, leaf = name.split(".") if DOT_NOTATION else [name]
    for parent in parents:
        target = target.setdefault(parent, {{}})
    target[leaf] = cast(value, TYPES.get(name, "string"))
print(json.dumps(result))
"""


def _set(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    version = _version(raw)
    node_id = imp._add_node(raw, "util.code", {})
    fields: list[tuple[str, Any, str]] = []  # (name, value, type)
    if params.get("mode") == "raw":
        imp._review(
            node_id,
            "n8n built this item from a raw JSON template; rewrite it as fields or as Python here.",
        )
        fields.append(("json", params.get("jsonOutput", ""), "object"))
        include = False
    elif version >= 3.3:
        for item in _get(params, "assignments", "assignments", default=[]) or []:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                fields.append((item["name"], item.get("value"), str(item.get("type") or "string")))
        include = bool(params.get("includeOtherFields"))
        if include and params.get("include") in ("selected", "except"):
            imp._review(node_id, "n8n kept only some of the input fields; this keeps them all.")
    elif version >= 3:
        for item in _get(params, "fields", "values", default=[]) or []:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                value_key = str(item.get("type") or "stringValue")
                kind = value_key.removesuffix("Value")
                fields.append((item["name"], item.get(value_key), kind))
        include = (params.get("include") or "all") != "none"
        if params.get("include") in ("selected", "except"):
            imp._review(node_id, "n8n kept only some of the input fields; this keeps them all.")
    else:
        for kind in ("string", "number", "boolean"):
            for item in _get(params, "values", kind, default=[]) or []:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    fields.append((item["name"], item.get("value"), kind))
        include = not params.get("keepOnlySet")
    dots = _get(params, "options", "dotNotation", default=True) is not False
    inputs: dict[str, Any] = {}
    types: dict[str, str] = {}
    for name, value, kind in fields:
        inputs[name] = imp.text(value, node_id) if isinstance(value, str) else value
        types[name] = kind
    if include and (whole := imp.input_token(raw["name"])):
        inputs["_input"] = whole
    imp.node_by_id[node_id]["config"] = {
        "code": _SET_CODE.format(
            types=json.dumps(types, ensure_ascii=False),
            include="True" if include else "False",
            dots="True" if dots else "False",
        ),
        "inputs": inputs,
    }
    imp._note(node_id, "n8n's Edit Fields node runs as Python here; the fields are its inputs.")
    return _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})


def _code(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    if params.get("language") not in ("python", "pythonNative"):
        return _javascript(imp, raw, params)
    node_id = imp._add_node(raw, "util.code", {})
    code = params.get("pythonCode") if isinstance(params.get("pythonCode"), str) else ""
    whole = imp.input_token(raw["name"])
    imp.node_by_id[node_id]["config"] = {
        "code": code,
        "inputs": {"json": whole} if whole else {},
    }
    imp._review(
        node_id,
        "n8n's Python helpers (`_input`, `_json`, `items`) do not exist here: "
        "read the input from `inputs['json']` and print the result.",
    )
    return _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})


def _pairs(container: Any, key: str) -> list[tuple[Any, Any]]:
    items = _get(container, key, default=[]) or []
    return [(item.get("name"), item.get("value")) for item in items if isinstance(item, dict) and item.get("name")]


def _http(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    version = _version(raw)
    node_id = imp._add_node(raw, "action.http_request", {})
    headers: dict[str, str] = {}
    query: list[tuple[Any, Any]] = []
    body = ""
    if version < 3:
        method = str(params.get("requestMethod") or "GET").upper()
        if params.get("jsonParameters"):
            for label, key in (("headers", "headerParametersJson"), ("query", "queryParametersJson")):
                if params.get(key):
                    imp._review(node_id, f"n8n's JSON {label} were not carried over.")
            body = imp.text(params.get("bodyParametersJson", ""), node_id)
        else:
            headers_pairs = _pairs(params.get("headerParametersUi"), "parameter")
            query = _pairs(params.get("queryParametersUi"), "parameter")
            body_pairs = _pairs(params.get("bodyParametersUi"), "parameter")
            headers.update(_header_items(imp, headers_pairs, node_id))
            if body_pairs:
                body = _json_body(imp, body_pairs, node_id)
                headers.setdefault("Content-Type", "application/json")
        authentication = params.get("authentication") or "none"
    else:
        method = str(params.get("method") or "GET").upper()
        if params.get("sendQuery"):
            if params.get("specifyQuery") == "json":
                imp._review(node_id, "n8n's JSON query parameters were not carried over.")
            query = _pairs(params.get("queryParameters"), "parameters")
        if params.get("sendHeaders"):
            if params.get("specifyHeaders") == "json":
                imp._review(node_id, "n8n's JSON headers were not carried over.")
            headers.update(_header_items(imp, _pairs(params.get("headerParameters"), "parameters"), node_id))
        if params.get("sendBody"):
            content_type = params.get("contentType") or "json"
            specify = params.get("specifyBody") or "keypair"
            pairs = _pairs(params.get("bodyParameters"), "parameters")
            if content_type == "json":
                headers.setdefault("Content-Type", "application/json")
                body = (
                    imp.text(params.get("jsonBody", ""), node_id)
                    if specify == "json"
                    else _json_body(imp, pairs, node_id)
                )
            elif content_type == "form-urlencoded":
                headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
                body = (
                    imp.text(params.get("body", ""), node_id)
                    if specify == "string"
                    else "&".join(_query_item(imp, k, v, node_id) for k, v in pairs)
                )
            elif content_type == "raw":
                raw_type = params.get("rawContentType")
                if isinstance(raw_type, str) and raw_type:
                    headers.setdefault("Content-Type", raw_type)
                body = imp.text(params.get("body", ""), node_id)
            else:
                imp._review(
                    node_id,
                    f"n8n sent a `{content_type}` body, which this node cannot send.",
                )
        authentication = params.get("authentication") or "none"
        if _get(params, "options", "pagination"):
            imp._review(node_id, "n8n followed pagination; this node makes a single request.")

    if method not in _HTTP_METHODS:
        imp._review(node_id, f"n8n sent a {method} request; this node supports {', '.join(_HTTP_METHODS)}.")
        method = "GET"
    if authentication != "none":
        credential = params.get("nodeCredentialType") or params.get("genericAuthType") or authentication
        imp._review(
            node_id,
            f"n8n authenticated this request with `{credential}` credentials, "
            "which are never imported: add the header from a secret variable.",
        )
    url = imp.text(params.get("url", ""), node_id)
    if query:
        url += ("&" if "?" in url else "?") + "&".join(_query_item(imp, k, v, node_id) for k, v in query)
        if any(imp.is_expression(v) for _k, v in query):
            imp._note(node_id, "Query values taken from other nodes are inserted as they are, not URL-encoded.")
    scrubbed_before = len(imp.scrubbed)
    body = imp.scrub_json_text(body)
    if len(imp.scrubbed) > scrubbed_before:
        imp._review(
            node_id,
            "A literal secret in the JSON body was not imported: store it as a secret variable and reference it.",
        )
    _response_options(imp, params, node_id)
    config: dict[str, Any] = {"method": method, "url": url}
    if headers:
        config["headers"] = headers
    if body:
        config["body"] = body
    imp.node_by_id[node_id]["config"] = config
    return _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})


def _response_options(imp: _N8nImporter, params: dict, node_id: str) -> None:
    """n8n's response options change what the next nodes read."""
    options = params.get("options") if isinstance(params.get("options"), dict) else {}
    response = _get(options, "response", "response", default={})
    response = response if isinstance(response, dict) else {}
    settings = {**options, **response}
    if settings.get("fullResponse"):
        imp._review(
            node_id,
            "n8n returned status, headers and body as one item; here the status and the body are "
            "separate outputs (`status`, `body`).",
        )
    if settings.get("neverError"):
        imp._review(node_id, "n8n carried on after an HTTP error status; here a 4xx or 5xx takes the error path.")
    response_format = settings.get("responseFormat") or params.get("responseFormat")
    if response_format in ("text", "string", "file"):
        imp._review(
            node_id,
            f"n8n kept the response as {'a file' if response_format == 'file' else 'text'}; "
            "here a JSON response is parsed into `body`.",
        )


def _header_items(imp: _N8nImporter, pairs: list[tuple[Any, Any]], node_id: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in pairs:
        name = str(name)
        headers[name] = "" if imp.secret_literal(name, value, node_id, "header") else imp.text(value, node_id)
    return headers


def _json_body(imp: _N8nImporter, pairs: list[tuple[Any, Any]], node_id: str) -> str:
    parts = []
    for name, value in pairs:
        if imp.secret_literal(str(name), value, node_id, "body field"):
            value = ""
        if imp.is_expression(value):
            # The renderer inserts values as text: keep them inside quotes.
            parts.append(f'{json.dumps(str(name), ensure_ascii=False)}: "{imp.text(value, node_id)}"')
        else:
            parts.append(
                f"{json.dumps(str(name), ensure_ascii=False)}: {json.dumps(_unwrap_locator(value), ensure_ascii=False)}"
            )
    if any(imp.is_expression(v) for _n, v in pairs):
        imp._note(node_id, "Values from other nodes are inserted into the JSON body as text, without escaping.")
    return "{" + ", ".join(parts) + "}"


def _query_item(imp: _N8nImporter, name: Any, value: Any, node_id: str) -> str:
    key = quote(str(name), safe="")
    if imp.secret_literal(str(name), value, node_id, "query parameter"):
        return f"{key}="
    if imp.is_expression(value):
        return f"{key}={imp.text(value, node_id)}"
    return f"{key}={quote(_scalar_text(_unwrap_locator(value)), safe='')}"


def _message(
    imp: _N8nImporter,
    raw: dict,
    channel: str,
    target: Any,
    message: Any,
    *,
    review: str | None = None,
) -> _Mapped:
    node_id = imp._add_node(raw, "action.send_message", {})
    config: dict[str, Any] = {"channel": channel, "message": imp.text(message, node_id)}
    target_text = imp.text(target, node_id) if target not in (None, "") else ""
    if target_text:
        config["target"] = target_text
    imp.node_by_id[node_id]["config"] = config
    if review:
        imp._review(node_id, review)
    elif channel in ("slack", "teams"):
        imp._note(
            node_id,
            f"XTM One posts through its own {channel.title()} channel bot: check "
            "that Recipient names a conversation it can reach.",
        )
    return _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})


_NOT_WIRED = (
    "XTM One delivers flow messages by email, Slack or Teams: route this one "
    "through an agent with a {} channel, or change the channel."
)


def _slack(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped | None:
    if (params.get("resource") or "message") != "message":
        return None
    if (params.get("operation") or "post") != "post":
        return None
    target = params.get("channelId") if params.get("select", "channel") == "channel" else params.get("user")
    target = target if target is not None else params.get("channel")
    return _message(imp, raw, "slack", target, params.get("text", ""))


def _teams(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped | None:
    resource = params.get("resource")
    if (params.get("operation") or "create") != "create":
        return None
    if resource == "chatMessage":
        return _message(imp, raw, "teams", params.get("chatId"), params.get("message", ""))
    if resource == "channelMessage":
        return _message(imp, raw, "teams", params.get("channelId"), params.get("message", ""))
    return None


def _telegram(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped | None:
    if (params.get("resource") or "message") != "message":
        return None
    if (params.get("operation") or "sendMessage") != "sendMessage":
        return None
    return _message(
        imp,
        raw,
        "telegram",
        params.get("chatId"),
        params.get("text", ""),
        review=_NOT_WIRED.format("Telegram"),
    )


def _discord(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped | None:
    if params.get("operation") not in (None, "send", "sendLegacy"):
        return None
    return _message(
        imp,
        raw,
        "discord",
        params.get("channelId"),
        params.get("content") or params.get("text", ""),
        review=_NOT_WIRED.format("Discord"),
    )


def _whatsapp(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped | None:
    if (params.get("operation") or "send") != "send":
        return None
    return _message(
        imp,
        raw,
        "whatsapp",
        params.get("recipientPhoneNumber"),
        params.get("textBody", ""),
        review=_NOT_WIRED.format("WhatsApp"),
    )


def _google_chat(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped | None:
    if (params.get("resource") or "message") != "message" or params.get("operation") not in (None, "create"):
        return None
    return _message(
        imp,
        raw,
        "google_chat",
        params.get("spaceId"),
        _get(params, "messageUi", "text", default=""),
        review=_NOT_WIRED.format("Google Chat"),
    )


def _email(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped | None:
    node_type = _short_type(str(raw["type"]))
    if node_type == "gmail":
        if (params.get("resource") or "message") != "message" or (params.get("operation") or "send") != "send":
            return None
        target, subject, body = params.get("sendTo"), params.get("subject"), params.get("message", "")
    else:  # emailSend (SMTP)
        target, subject = params.get("toEmail"), params.get("subject")
        body = params.get("text") or params.get("html") or ""
    mapped = _message(imp, raw, "email", target, body)
    if subject:
        imp._note(
            mapped.entry,
            f"n8n's subject (`{_excerpt(str(subject))}`) is not carried over: "
            "flow emails go out under a fixed subject.",
        )
    return mapped


def _agent(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "agent", {})
    if params.get("promptType") == "define" or (
        "promptType" not in params and isinstance(params.get("text"), str) and params["text"]
    ):
        prompt = params.get("text", "")
    else:
        prompt = "={{ $json.chatInput }}"
    config: dict[str, Any] = {"prompt": imp.text(prompt, node_id)}
    system = _get(params, "options", "systemMessage")
    messages = _get(params, "messages", "messageValues", default=[]) or []
    if not system and messages:
        system = "\n\n".join(
            str(m.get("message")) for m in messages if isinstance(m, dict) and isinstance(m.get("message"), str)
        )
    if isinstance(system, str) and system.strip():
        config["persona_override"] = imp.literal(system, node_id, "The system message")
    iterations = _get(params, "options", "maxIterations")
    if isinstance(iterations, int) and iterations > 0:
        config["max_iterations"] = min(100, iterations)
    imp.node_by_id[node_id]["config"] = config
    imp._note(
        node_id,
        "n8n defined this agent inline: pick the XTM One agent that runs the "
        "step (its system message is kept as a persona override).",
    )
    mapped = _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})
    if raw["name"] in imp.structured:
        return imp.structured_answer(raw, mapped)
    return mapped


_EXTRACTION_PERSONA = (
    "You are an expert extraction algorithm. Only extract relevant "
    "information from the text. If you do not know the value of an attribute "
    "asked to extract, you may omit the attribute's value."
)


def _extractor(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    """n8n's Information Extractor: an agent answering in a fixed schema."""
    schema: dict[str, Any] | None
    if params.get("schemaType") in ("fromJson", "manual"):
        schema = imp._parser_schema(params)
    else:
        attributes = [
            a
            for a in _get(params, "attributes", "attributes", default=[]) or []
            if isinstance(a, dict) and isinstance(a.get("name"), str) and a["name"]
        ]
        schema = {
            "type": "object",
            "properties": {
                a["name"]: {
                    "type": {"number": "number", "boolean": "boolean"}.get(str(a.get("type")), "string"),
                    "description": str(a.get("description") or ""),
                }
                for a in attributes
            },
            "required": [a["name"] for a in attributes if a.get("required")],
        }
    persona = _get(params, "options", "systemPromptTemplate") or _EXTRACTION_PERSONA
    mapped = _agent(
        imp,
        raw,
        {
            "promptType": "define",
            "text": params.get("text", ""),
            "options": {"systemMessage": persona},
        },
    )
    agent_id = mapped.entry
    schema_id = imp._add_node(
        raw,
        "capability.structured_output",
        {"schema": schema} if schema else {},
        id_hint=f"{raw['name']} schema",
        label=f"{raw['name']} — schema",
        offset=(0, 200),
    )
    if not schema:
        imp._review(schema_id, "The n8n extraction schema could not be read.")
    imp._edge(schema_id, "provides", agent_id, "output")
    if raw["name"] in imp.structured or not _is_object_schema(schema):
        return mapped
    return imp.structured_answer(raw, mapped)


def _openai(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped | None:
    resource = params.get("resource") or "text"
    operation = params.get("operation") or "message"
    if resource == "assistant" and operation == "message":
        return _assistant(imp, raw, params)
    if resource == "image" and operation == "analyze":
        mapped = _agent(imp, raw, {"promptType": "define", "text": params.get("text") or "What is in this image?"})
        imp._note(
            mapped.entry,
            f"n8n sent the image `{_excerpt(_scalar_text(params.get('imageUrls')))}` along: attach it to "
            "the run, and pick an agent whose model reads images.",
        )
        return mapped
    if resource != "text" or operation != "message":
        return None
    messages = _get(params, "messages", "values", default=[]) or []
    system = "\n\n".join(
        str(m.get("content"))
        for m in messages
        if isinstance(m, dict) and m.get("role") == "system" and isinstance(m.get("content"), str)
    )
    user = [
        m.get("content")
        for m in messages
        if isinstance(m, dict) and m.get("role") in (None, "user") and isinstance(m.get("content"), str)
    ]
    params = {
        "promptType": "define",
        "text": user[-1] if user else "",
        "options": {"systemMessage": system} if system else {},
    }
    mapped = _agent(imp, raw, params)
    model = _unwrap_locator(raw.get("parameters", {}).get("modelId"))
    if isinstance(model, str) and model:
        imp._note(mapped.entry, f"n8n messaged `{model}`; the agent's own model applies here.")
    return mapped


def _classifier(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "condition.switch", {})
    if _short_type(str(raw["type"])) == "sentimentAnalysis":
        names = _get(params, "options", "categories", default="Positive, Neutral, Negative")
        categories = [
            (n.strip(), f"The text's sentiment is {n.strip().lower()}.") for n in str(names).split(",") if n.strip()
        ]
        fallback = False
    else:
        categories = [
            (str(c.get("category")), str(c.get("description") or c.get("category")))
            for c in _get(params, "categories", "categories", default=[]) or []
            if isinstance(c, dict) and c.get("category")
        ]
        fallback = _get(params, "options", "fallback") == "other"
    cases: dict[str, str] = {}
    outputs: dict[int, list[tuple[str, str]]] = {}
    for index, (name, description) in enumerate(categories):
        while name in cases:
            name += "'"
        cases[name] = imp.literal(description, node_id, "A category description")
        outputs[index] = [(node_id, f"case:{name}")]
    if fallback:
        outputs[len(categories)] = [(node_id, "default")]
    imp.node_by_id[node_id]["config"] = {
        "value": imp.text(params.get("inputText", ""), node_id),
        "cases": cases,
    }
    if _get(params, "options", "multiClass"):
        imp._review(node_id, "n8n could pick several categories; this switch picks one.")
    return _Mapped(entry=node_id, outputs=outputs, regular_outputs=len(categories) + int(fallback))


def _sub_workflow(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "flow.call", {})
    workflow = params.get("workflowId")
    described = _get(workflow, "cachedResultName") or _unwrap_locator(workflow)
    inputs = _get(params, "workflowInputs", "value")
    if isinstance(inputs, dict) and inputs:
        pairs = list(inputs.items())
        text = _json_body(imp, pairs, node_id)
    else:
        text = imp.input_token(raw["name"])
    if text:
        imp.node_by_id[node_id]["config"] = {"input": text}
    if isinstance(described, str) and "$workflow.id" in described:
        imp._note(node_id, "n8n called this same workflow: pick this flow.")
    elif described:
        imp._note(
            node_id,
            f"n8n called the workflow `{_excerpt(str(described))}`: import it too, then pick it here.",
        )
    if params.get("mode") == "each":
        imp._note(node_id, "n8n called the workflow once per item; this calls it once.")
    return _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})


def _respond(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "util.stop", {})
    respond_with = params.get("respondWith") or "firstIncomingItem"
    if respond_with in ("json", "text"):
        result = imp.text(params.get("responseBody", ""), node_id)
    elif respond_with in ("firstIncomingItem", "allIncomingItems"):
        result = imp.input_token(raw["name"])
    else:
        result = ""
    if result:
        imp.node_by_id[node_id]["config"] = {"result": result}
    imp._review(
        node_id,
        "XTM One acknowledges a webhook as soon as it arrives, so the caller "
        "never receives this response: it becomes the run's result instead.",
    )
    return _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})


def _stop_and_error(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "util.stop", {})
    message = params.get("errorMessage") or _get(params, "errorObject") or ""
    if message:
        imp.node_by_id[node_id]["config"] = {"result": imp.text(message, node_id)}
    imp._review(
        node_id,
        "n8n failed the execution here; a Stop node ends the run successfully.",
    )
    return _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})


# ── Data nodes: generated Python ─────────────────────────────────────────────


def _field_list(value: Any) -> list[str]:
    """n8n's field lists: ``"a, b"``, ``["a"]`` or ``{fields: [{fieldName}]}``."""
    if isinstance(value, dict):
        value = value.get("fields") or value.get("values") or []
    if isinstance(value, list):
        names = [
            _scalar_text(item.get("fieldName") or item.get("field") if isinstance(item, dict) else item)
            for item in value
        ]
    elif isinstance(value, str):
        names = value.split(",")
    else:
        names = []
    return [name.strip() for name in names if name and name.strip()]


def _computed_fields(imp: _N8nImporter, node_id: str, *values: Any) -> None:
    """Field names built by an expression cannot be read before the run."""
    for value in values:
        if imp.is_expression(value):
            imp._review(node_id, f"n8n computed a field name (`{_excerpt(str(value))}`); write the field here.")


def _item_path(imp: _N8nImporter, value: Any) -> str | None:
    """The path a ``$json.a.b`` expression reads in the item being processed."""
    value = _unwrap_locator(value)
    if not imp.is_expression(value):
        return None
    parts = _template_parts(value[1:])
    expressions = [text for is_expression, text in parts if is_expression]
    literal = "".join(text for is_expression, text in parts if not is_expression)
    if len(expressions) != 1 or literal.strip():
        return None
    parsed = parse_reference(expressions[0])
    if parsed is None or parsed[0] is not None or not parsed[1]:
        return None
    return ".".join(parsed[1])


def _item_arg(
    imp: _N8nImporter,
    node_id: str,
    value: Any,
    name: str,
    settings: dict[str, Any],
    inputs: dict[str, Any],
) -> None:
    """A parameter n8n evaluated for each item.

    ``$json.a.b`` reads the item being processed: it becomes a path the
    script reads in each item. Anything else is rendered once, as a flow
    reference.
    """
    path = _item_path(imp, value)
    if path is not None:
        settings.setdefault("paths", {})[name] = path
        return
    value = _unwrap_locator(value)
    if value is not None and value != "":
        inputs[name] = imp.text(value, node_id) if isinstance(value, str) else _scalar_text(value)


def _python(
    imp: _N8nImporter,
    raw: dict[str, Any],
    node_id: str,
    body: str,
    settings: dict[str, Any],
    inputs: dict[str, Any] | None = None,
    *,
    items: bool = True,
) -> _Mapped:
    """A Code node running one of the scripts reproducing n8n's data nodes."""
    config_inputs: dict[str, Any] = {"settings": json.dumps(settings, ensure_ascii=False)}
    if items and (whole := imp.input_token(raw["name"])):
        config_inputs["items"] = whole
    config_inputs.update(inputs or {})
    imp.node_by_id[node_id]["config"] = {"code": n8n_scripts.script(body), "inputs": config_inputs}
    imp._note(
        node_id,
        f"n8n's `{_short_type(str(raw['type']))}` node runs here as generated Python; "
        "its options are the `settings` input.",
    )
    return _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})


def _aggregate(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "util.code", {})
    if params.get("aggregate") == "aggregateAllItemData":
        include = {"specifiedFields": "specified", "allFieldsExcept": "except"}.get(str(params.get("include")), "all")
        listed = params.get("fieldsToInclude") if include == "specified" else params.get("fieldsToExclude")
        _computed_fields(imp, node_id, listed, params.get("destinationFieldName"))
        settings: dict[str, Any] = {
            "mode": "all",
            "include": include,
            "fields": _field_list(listed),
            "destination": _scalar_text(params.get("destinationFieldName")) or "data",
        }
    else:
        fields = []
        for spec in _get(params, "fieldsToAggregate", "fieldToAggregate", default=[]) or []:
            if not isinstance(spec, dict) or not spec.get("fieldToAggregate"):
                continue
            _computed_fields(imp, node_id, spec["fieldToAggregate"], spec.get("outputFieldName"))
            field: dict[str, str] = {"field": _scalar_text(spec["fieldToAggregate"])}
            if spec.get("renameField") and spec.get("outputFieldName"):
                field["rename"] = _scalar_text(spec["outputFieldName"])
            fields.append(field)
        settings = {
            "fields": fields,
            "keep_missing": bool(_get(params, "options", "keepMissing")),
            "merge_lists": bool(_get(params, "options", "mergeLists")),
        }
    return _python(imp, raw, node_id, n8n_scripts.AGGREGATE, settings)


def _limit(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "util.code", {})
    settings = {
        "max": _int(params.get("maxItems"), 1),
        "keep": "last" if params.get("keep") == "lastItems" else "first",
    }
    return _python(imp, raw, node_id, n8n_scripts.LIMIT, settings)


def _sort(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "util.code", {})
    kind = params.get("type") or "simple"
    fields = [
        {"field": _scalar_text(spec.get("fieldName")), "order": spec.get("order") or "ascending"}
        for spec in _get(params, "sortFieldsUi", "sortField", default=[]) or []
        if isinstance(spec, dict) and spec.get("fieldName")
    ]
    if kind == "code":
        imp._review(node_id, "n8n sorted with JavaScript; write the sort fields here.")
    return _python(imp, raw, node_id, n8n_scripts.SORT, {"fields": fields, "random": kind == "random"})


def _remove_duplicates(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "util.code", {})
    operation = params.get("operation") or "removeDuplicateInputItems"
    settings: dict[str, Any] = {
        "compare": params.get("compare") or "allFields",
        "fields": _field_list(params.get("fieldsToCompare") or params.get("fieldsToExclude")),
    }
    if operation != "removeDuplicateInputItems":
        imp._review(
            node_id,
            "n8n remembered the values it had seen in earlier executions; this removes the "
            "duplicates within one run: keep what was seen in a shared state to go further.",
        )
        path = _item_path(imp, params.get("dedupeValue"))
        if path is not None:
            settings = {"compare": "selectedFields", "fields": [path]}
    return _python(imp, raw, node_id, n8n_scripts.REMOVE_DUPLICATES, settings)


def _summarize(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "util.code", {})
    fields = []
    for spec in _get(params, "fieldsToSummarize", "values", default=[]) or []:
        if not isinstance(spec, dict) or not spec.get("field"):
            continue
        separator = spec.get("separateBy", ",")
        if separator == "other":
            separator = spec.get("customSeparator", "")
        elif separator == "\\n":
            separator = "\n"
        fields.append(
            {
                "field": _scalar_text(spec["field"]),
                "aggregation": spec.get("aggregation") or "count",
                "separator": separator,
            }
        )
    settings = {"split_by": _field_list(params.get("fieldsToSplitBy")), "fields": fields}
    return _python(imp, raw, node_id, n8n_scripts.SUMMARIZE, settings)


def _rename_keys(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "util.code", {})
    keys = []
    for pair in _get(params, "keys", "key", default=[]) or []:
        if isinstance(pair, dict):
            _computed_fields(imp, node_id, pair.get("currentKey"), pair.get("newKey"))
            keys.append({"current": _scalar_text(pair.get("currentKey")), "new": _scalar_text(pair.get("newKey"))})
    if _get(params, "additionalOptions", "regexReplacement"):
        imp._review(node_id, "n8n also renamed keys matching a regular expression; add those keys here.")
    return _python(imp, raw, node_id, n8n_scripts.RENAME_KEYS, {"keys": keys})


def _split_out(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    """n8n's Split Out: every next node ran once per element — a Loop."""
    node_id = imp._add_node(raw, "util.loop", {})
    field = params.get("fieldToSplitOut") or ""
    fields = _field_list(field)
    sources = imp._inputs_of(raw["name"])[:1]
    if imp.is_expression(field) or not fields or fields[0].startswith("$binary"):
        imp._review(node_id, f"n8n split `{_excerpt(str(field))}`: name the list to iterate in Items.")
    elif sources:
        path = fields[0].split(".")
        imp.node_by_id[node_id]["config"] = {"items": imp._token(sources, path, f"$json.{fields[0]}")}
    if len(fields) > 1:
        imp._review(node_id, f"n8n split {len(fields)} lists side by side; this loop iterates the first.")
    if (params.get("include") or "noOtherFields") != "noOtherFields":
        imp._note(
            node_id,
            "n8n copied the item's other fields into each element; read them from the node before the loop.",
        )
    imp._note(node_id, "n8n ran each following node once per element: they hang from Each item.")
    return _Mapped(entry=node_id, outputs={0: [(node_id, "each")]})


def _item_lists(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    """The legacy Item Lists node: one of the list nodes, by operation."""
    operation = params.get("operation") or "splitOutItems"
    converter = {
        "aggregateItems": _aggregate,
        "concatenateItems": _aggregate,
        "removeDuplicates": _remove_duplicates,
        "sort": _sort,
        "limit": _limit,
        "summarize": _summarize,
    }.get(str(operation), _split_out)
    return converter(imp, raw, params)


_DATE_OUTPUTS = {
    "getCurrentDate": "currentDate",
    "addToDate": "newDate",
    "subtractFromDate": "newDate",
    "formatDate": "formattedDate",
    "roundDate": "roundedDate",
    "getTimeBetweenDates": "timeDifference",
    "extractDate": "datePart",
}


def _date_time(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "util.code", {})
    settings: dict[str, Any] = {}
    inputs: dict[str, Any] = {}

    def item(value: Any, name: str) -> None:
        _item_arg(imp, node_id, value, name, settings, inputs)

    if _version(raw) >= 2:
        operation = str(params.get("operation") or "getCurrentDate")
        settings["operation"] = operation
        settings["output"] = _scalar_text(params.get("outputFieldName")) or _DATE_OUTPUTS.get(operation, "data")
        if operation == "getCurrentDate":
            settings["include_time"] = params.get("includeTime", True) is not False
        elif operation in ("addToDate", "subtractFromDate"):
            item(params.get("magnitude"), "date")
            item(params.get("duration", 0), "duration")
            item(params.get("timeUnit") or "days", "unit")
        elif operation == "formatDate":
            item(params.get("date"), "date")
            pattern = params.get("format") or "MM/dd/yyyy"
            item(params.get("customFormat") if pattern == "custom" else pattern, "format")
        elif operation == "roundDate":
            item(params.get("date"), "date")
            settings["mode"] = params.get("mode") or "roundDown"
            settings["to_nearest"] = params.get("toNearest") or params.get("to") or "day"
        elif operation == "getTimeBetweenDates":
            item(params.get("startDate"), "start")
            item(params.get("endDate"), "end")
            units = params.get("units") or ["day"]
            settings["units"] = units if isinstance(units, list) else [units]
        elif operation == "extractDate":
            item(params.get("date"), "date")
            settings["part"] = params.get("part") or "month"
    else:
        settings["tokens"] = "moment"
        settings["output"] = _scalar_text(params.get("dataPropertyName")) or "data"
        item(params.get("value"), "date")
        if (params.get("action") or "format") == "calculate":
            settings["operation"] = "subtractFromDate" if params.get("operation") == "subtract" else "addToDate"
            item(params.get("duration", 0), "duration")
            item(params.get("timeUnit") or "days", "unit")
        else:
            settings["operation"] = "formatDate"
            item(params.get("toFormat") or "MM/DD/YYYY", "format")
            if _get(params, "options", "fromFormat"):
                imp._review(
                    node_id, "n8n read the date with a custom input format; ISO dates and timestamps are read here."
                )
    timezone = _get(params, "options", "timezone") or _get(imp.workflow, "settings", "timezone")
    if isinstance(timezone, str) and timezone not in ("", "DEFAULT", "UTC", "Etc/UTC"):
        imp._note(node_id, f"n8n computed dates in `{timezone}`; this node computes them in UTC.")
    return _python(imp, raw, node_id, n8n_scripts.DATE_TIME, settings, inputs)


def _crypto(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "util.code", {})
    action = str(params.get("action") or "hash")
    settings: dict[str, Any] = {
        "action": action,
        "type": params.get("type") or "MD5",
        "encoding": params.get("encoding") or "hex",
        "output": _scalar_text(params.get("dataPropertyName")) or "data",
        "generate": params.get("encodingType") or "uuid",
        "length": _int(params.get("stringLength"), 32),
    }
    inputs: dict[str, Any] = {}
    if action in ("hash", "hmac", "sign"):
        _item_arg(imp, node_id, params.get("value", ""), "value", settings, inputs)
    if action == "hmac":
        secret = params.get("secret")
        if imp.is_expression(secret):
            inputs["secret"] = imp.text(secret, node_id)
        elif not imp.secret_literal("secret", secret, node_id, "parameter"):
            imp._review(node_id, "n8n's HMAC secret did not come across: reference a secret variable in `secret`.")
    if action == "sign":
        imp._review(node_id, "n8n signed with a private key credential, which this node does not reproduce.")
    if params.get("binaryData"):
        imp._review(node_id, "n8n hashed a binary file; here the text that reaches the node is hashed.")
    return _python(imp, raw, node_id, n8n_scripts.CRYPTO, settings, inputs)


# What the generated extraction reads outside attribute values: no sibling
# combinators, no pseudo-class but :first-child, :last-child, :nth-child(n).
_SUPPORTED_SELECTOR = re.compile(r"^(?:[^:+~]|:(?:first-child|last-child|nth-child\(\s*\d+\s*\)))*$")
_SELECTOR_ATTRIBUTE = re.compile(r"\[[^\]]*\]")


def _html_extract(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "util.code", {})
    values = []
    for spec in _get(params, "extractionValues", "values", default=[]) or []:
        if not isinstance(spec, dict):
            continue
        selector = _scalar_text(spec.get("cssSelector"))
        if imp.is_expression(selector) or not _SUPPORTED_SELECTOR.match(_SELECTOR_ATTRIBUTE.sub("", selector)):
            imp._review(node_id, f"The CSS selector `{_excerpt(selector)}` uses a form this extraction does not read.")
        values.append(
            {
                "key": _scalar_text(spec.get("key")) or "data",
                "selector": selector,
                "return": spec.get("returnValue") or "text",
                "attribute": _scalar_text(spec.get("attribute")),
                "array": bool(spec.get("returnArray")),
            }
        )
    if params.get("sourceData") == "binary":
        imp._note(node_id, "n8n read the HTML from a binary file; here the text that reaches the node is read.")
    settings = {
        "property": _scalar_text(params.get("dataPropertyName")) or "data",
        "values": values,
        "trim": _get(params, "options", "trimValues") is not False,
        "clean_up": bool(_get(params, "options", "cleanUpText")),
    }
    return _python(imp, raw, node_id, n8n_scripts.HTML_DOM + n8n_scripts.HTML_EXTRACT, settings)


def _html(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    operation = params.get("operation") or "generateHtmlTemplate"
    if operation == "extractHtmlContent":
        return _html_extract(imp, raw, params)
    node_id = imp._add_node(raw, "util.code", {})
    if operation == "convertToHtmlTable":
        return _python(imp, raw, node_id, n8n_scripts.HTML_TABLE, {})
    # n8n evaluates the template's `{{ }}` whether or not it starts with `=`.
    template = _scalar_text(params.get("html"))
    html = imp.text(template if template.startswith("=") else "=" + template, node_id)
    return _python(imp, raw, node_id, n8n_scripts.HTML_TEMPLATE, {}, {"html": html}, items=False)


def _markdown(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "util.code", {})
    mode = params.get("mode") or "htmlToMarkdown"
    settings: dict[str, Any] = {"mode": mode, "output": _scalar_text(params.get("destinationKey")) or "data"}
    inputs: dict[str, Any] = {}
    source = params.get("markdown") if mode == "markdownToHtml" else params.get("html")
    _item_arg(imp, node_id, source, "source", settings, inputs)
    return _python(imp, raw, node_id, n8n_scripts.MARKDOWN, settings, inputs)


def _xml(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "util.code", {})
    options = params.get("options") if isinstance(params.get("options"), dict) else {}
    settings = {
        "mode": params.get("mode") or "xmlToJson",
        "property": _scalar_text(params.get("dataPropertyName")) or "data",
        "attrkey": options.get("attrkey") or "$",
        "charkey": options.get("charkey") or "_",
        "explicit_array": bool(options.get("explicitArray")),
        "explicit_root": options.get("explicitRoot") is not False,
        "headless": bool(options.get("headless")),
        "root": options.get("rootName") or "root",
        # n8n parses with `mergeAttrs` on unless the option turns it off.
        "merge_attrs": options.get("mergeAttrs") is not False,
        "ignore_attrs": bool(options.get("ignoreAttrs")),
        "normalize": bool(options.get("normalize")),
        "normalize_tags": bool(options.get("normalizeTags")),
        "trim": bool(options.get("trim")),
    }
    return _python(imp, raw, node_id, n8n_scripts.XML, settings)


_TEXT_FILE_FORMATS = frozenset({"csv", "json", "text", "xml", "html", "fromIcs", "binaryToPropery", "fromJson"})


_TEXT_FILE_WRITES = frozenset({"csv", "toJson", "json", "toText"})


def _file_field(raw: dict) -> str | None:
    """The field an imported step puts a file's text in (a file it wrote or read), n8n's binary property."""
    if not str(raw.get("type") or "").startswith(_BASE):
        return None
    short = _short_type(str(raw["type"]))
    params = raw.get("parameters") if isinstance(raw.get("parameters"), dict) else {}
    if short == "convertToFile" and str(params.get("operation") or "csv") in _TEXT_FILE_WRITES:
        return _scalar_text(params.get("binaryPropertyName")) or "data"
    if (
        short == "spreadsheetFile"
        and params.get("operation") == "toFile"
        and str(params.get("fileFormat") or "xls") in _TEXT_FILE_WRITES
    ):
        return _scalar_text(params.get("binaryPropertyName")) or "data"
    if short == "moveBinaryData" and params.get("mode") == "jsonToBinary":
        return _scalar_text(params.get("destinationKey")) or "data"
    if short in ("readBinaryFile", "readBinaryFiles") or (
        short == "readWriteFile" and params.get("operation") != "write"
    ):
        options = params.get("options") if isinstance(params.get("options"), dict) else {}
        return _scalar_text(params.get("dataPropertyName") or options.get("dataPropertyName")) or "data"
    return None


def _file_input(imp: _N8nImporter, name: str, field: str) -> str:
    """The file *name* reads in n8n's binary property *field*, as a reference.

    After imported steps that wrote a file there, it is that field; otherwise
    the whole input (a downloaded body, ...) is the file, read as written.
    """
    sources = imp._inputs_of(name)
    writes_file = bool(sources) and all(
        source in imp.source and _file_field(imp.source[source]) == field for source in sources
    )
    return imp.input_token(name, path=[field] if writes_file else None)


def _binary_file(imp: _N8nImporter, raw: dict, what: str) -> _Mapped:
    """A step on a binary file: flows pass text, so it is reviewed away."""
    node_id = imp._add_node(raw, "util.reroute", {})
    imp._review(
        node_id,
        f"n8n {what}. XTM One flows pass text, not files: agents read the files "
        "attached to a run and the files of their integrations directly, so "
        "this step usually goes; it passes its input through meanwhile.",
    )
    return _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})


def _extract_from_file(imp: _N8nImporter, raw: dict, params: dict, operation: str | None = None) -> _Mapped:
    operation = operation or str(params.get("operation") or "csv")
    if operation == "fromJson":
        operation = "json"
    if operation not in _TEXT_FILE_FORMATS:
        return _binary_file(imp, raw, f"extracted the content of a `{operation}` file")
    node_id = imp._add_node(raw, "util.code", {})
    options = params.get("options") if isinstance(params.get("options"), dict) else {}
    settings = {
        "operation": operation,
        "delimiter": options.get("delimiter") or ",",
        "header": options.get("headerRow") is not False,
        "output": _scalar_text(params.get("destinationKey")) or "data",
    }
    imp._note(node_id, "n8n read a binary file; here the text that reaches the node is read.")
    whole = _file_input(imp, raw["name"], _scalar_text(params.get("binaryPropertyName")) or "data")
    return _python(imp, raw, node_id, n8n_scripts.EXTRACT_TEXT, settings, {"data": whole} if whole else {}, items=False)


def _convert_to_file(imp: _N8nImporter, raw: dict, params: dict, operation: str | None = None) -> _Mapped:
    operation = operation or str(params.get("operation") or "csv")
    if operation not in ("csv", "toJson", "json", "toText", "html"):
        return _binary_file(imp, raw, f"wrote a `{operation}` file")
    node_id = imp._add_node(raw, "util.code", {})
    options = params.get("options") if isinstance(params.get("options"), dict) else {}
    if operation == "html":
        return _python(imp, raw, node_id, n8n_scripts.HTML_TABLE, {})
    settings = {
        "operation": operation,
        "delimiter": options.get("delimiter") or ",",
        "header": options.get("headerRow") is not False,
        "all_items": (params.get("mode") or "once") == "once",
        "pretty": bool(options.get("format")),
        "source": _scalar_text(params.get("sourceProperty")) or "data",
        "file_name": _scalar_text(options.get("fileName")) if not imp.is_expression(options.get("fileName")) else "",
        "output": _file_field(raw) or "data",
    }
    imp._note(node_id, f"n8n made a file of the items; here the file's text is the output (`{settings['output']}`).")
    return _python(imp, raw, node_id, n8n_scripts.CONVERT_TEXT, settings)


def _spreadsheet_file(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    """The legacy Spreadsheet File node: Extract from / Convert to File."""
    file_format = str(params.get("fileFormat") or "")
    if (params.get("operation") or "fromFile") == "fromFile":
        return _extract_from_file(imp, raw, params, file_format or "csv")
    return _convert_to_file(imp, raw, params, file_format or "xls")


def _move_binary_data(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "util.code", {})
    mode = params.get("mode") or "binaryToJson"
    options = params.get("options") if isinstance(params.get("options"), dict) else {}
    if mode == "jsonToBinary":
        settings = {
            "mode": mode,
            "source": "" if params.get("convertAllData", True) is not False else _scalar_text(params.get("sourceKey")),
            "raw": bool(options.get("useRawData")),
            "output": _scalar_text(params.get("destinationKey")) or "data",
        }
    else:
        settings = {
            "mode": mode,
            "source": _scalar_text(params.get("sourceKey")) or "data",
            "all": params.get("setAllData", True) is not False,
            "output": _scalar_text(params.get("destinationKey")) or "data",
        }
    imp._note(node_id, "n8n moved data between the item and a binary file; here both are text.")
    return _python(imp, raw, node_id, n8n_scripts.MOVE_DATA, settings)


def _compare_datasets(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "util.code", {})
    pairs = [
        [_scalar_text(pair.get("field1")), _scalar_text(pair.get("field2"))]
        for pair in _get(params, "mergeByFields", "values", default=[]) or []
        if isinstance(pair, dict)
    ]
    by_index = imp.inputs_by_index.get(raw["name"], {})
    inputs: dict[str, Any] = {}
    for index, name in ((0, "input_a"), (1, "input_b")):
        sources = by_index.get(index, [])[:1]
        if sources:
            inputs[name] = imp._token(sources, [], "$json")
    settings = {"fields": pairs, "skip": _field_list(_get(params, "options", "skipFields"))}
    imp._review(
        node_id,
        "n8n routed the comparison to four outputs (in A only, same, different, in B only); "
        "here one output carries them as `in_a_only`, `same`, `different` and `in_b_only`: "
        "branch on them with If nodes.",
    )
    mapped = _python(imp, raw, node_id, n8n_scripts.COMPARE_DATASETS, settings, inputs, items=False)
    mapped.outputs = {index: [(node_id, "out")] for index in range(4)}
    mapped.regular_outputs = 4
    return mapped


def _totp(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "util.code", {})
    options = params.get("options") if isinstance(params.get("options"), dict) else {}
    settings = {
        "algorithm": options.get("algorithm") or "SHA1",
        "digits": _int(options.get("digits"), 6),
        "period": _int(options.get("period"), 30),
    }
    imp._review(node_id, "n8n read the TOTP secret from a credential: reference a secret variable in `secret`.")
    return _python(imp, raw, node_id, n8n_scripts.TOTP, settings, {"secret": ""}, items=False)


def _javascript(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    """A JavaScript Code / Function node: a Code node holding it, to rewrite."""
    node_id = imp._add_node(raw, "util.code", {})
    source = params.get("jsCode") or params.get("functionCode") or params.get("code") or ""
    if isinstance(source, dict):  # the LangChain Code node nests it
        source = _get(source, "execute", "code", default="") or _get(source, "supplyData", "code", default="")
    whole = imp.input_token(raw["name"])
    imp.node_by_id[node_id]["config"] = {
        "code": n8n_scripts.javascript(source if isinstance(source, str) else ""),
        "inputs": {"json": whole} if whole else {},
    }
    imp._review(
        node_id,
        "n8n ran JavaScript here; this platform runs Python. The JavaScript is kept as "
        "comments: rewrite it, reading the input from `inputs['json']` and printing the result.",
    )
    if params.get("mode") == "runOnceForEachItem" or _short_type(str(raw["type"])) == "functionItem":
        imp._note(node_id, "n8n ran this code once per item.")
    return _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})


# ── Commands and files: an enrolled machine ──────────────────────────────────


def _shell_word(imp: _N8nImporter, value: Any, node_id: str) -> str:
    """A path or argument for a shell command: quoted when it is a literal."""
    text = imp.text(value, node_id)
    return text if imp.is_expression(_unwrap_locator(value)) else shlex.quote(text)


def _review_shell(imp: _N8nImporter, node_id: str, command: str) -> None:
    # An imported file decides what runs: a person reads it before a machine does.
    imp._review(node_id, "Read the imported command before a runner executes it on your machine.")
    if _REF_OPEN in command:  # a reference, written in the final pass
        imp._review(
            node_id,
            "Values from other nodes are inserted into the command as they are, unquoted: "
            "quote them, or check that no caller can put shell syntax in them.",
        )


def _runner(
    imp: _N8nImporter,
    raw: dict[str, Any],
    command_kind: str,
    payload: dict[str, Any],
    *,
    node_id: str | None = None,
) -> _Mapped:
    node_id = node_id or imp._add_node(raw, "action.runner_command", {})
    imp.node_by_id[node_id]["config"] = {"command_kind": command_kind, "payload": payload, "mode": "wait"}
    imp._note(
        node_id,
        "n8n ran this on its own host; here an enrolled XTM One Desktop runs it: pick the Runner.",
    )
    return _Mapped(entry=node_id, outputs={0: [(node_id, "ok")]}, error_handle="fail")


def _execute_command(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "action.runner_command", {})
    command = imp.text(params.get("command", ""), node_id)
    _review_shell(imp, node_id, command)
    return _runner(imp, raw, "run_shell", {"command": command}, node_id=node_id)


def _ssh(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "action.runner_command", {})
    if (params.get("resource") or "command") != "command":
        imp._review(node_id, "n8n moved a file over SSH; use a runner's Read file or Write file command.")
    command = imp.text(params.get("command", ""), node_id)
    cwd = params.get("cwd", "")
    if cwd and cwd != "/":
        command = f"cd {_shell_word(imp, cwd, node_id)} && {command}"
    _review_shell(imp, node_id, command)
    imp._review(
        node_id,
        "n8n ran this over SSH on the host of its credential: run it on a runner enrolled on that host.",
    )
    return _runner(imp, raw, "run_shell", {"command": command}, node_id=node_id)


def _read_write_file(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "action.runner_command", {})
    short = _short_type(str(raw["type"]))
    write = short == "writeBinaryFile" or (short == "readWriteFile" and params.get("operation") == "write")
    if write:
        path = imp.text(params.get("fileName", ""), node_id)
        content = _file_input(imp, raw["name"], _scalar_text(params.get("dataPropertyName")) or "data")
        imp._review(
            node_id,
            "n8n wrote a binary property of the item: check the path and that `content` is the text to write.",
        )
        return _runner(imp, raw, "write_file", {"path": path, "content": content}, node_id=node_id)
    path = imp.text(params.get("fileSelector") or params.get("filePath") or "", node_id)
    # As for a command: a person reads what an imported file makes a machine read.
    imp._review(node_id, "Read the imported path before a runner reads that file on your machine.")
    if any(ch in path for ch in "*?[") or short == "readBinaryFiles":
        imp._review(node_id, "n8n read every file matching a pattern; a runner reads one path at a time.")
    return _runner(imp, raw, "read_file", {"path": path}, node_id=node_id)


# ── Requests, feeds ──────────────────────────────────────────────────────────


def _json_string(imp: _N8nImporter, value: Any, node_id: str) -> str:
    """*value* as the inside of a JSON string, its references left in place."""
    value = _unwrap_locator(value)
    if not isinstance(value, str):
        return json.dumps(_scalar_text(value))[1:-1]
    if not value.startswith("="):
        return json.dumps(value, ensure_ascii=False)[1:-1]
    out = []
    for is_expression, text in _template_parts(value[1:]):
        if is_expression:
            out.append(imp._segment(text, node_id, current=imp.current))
        else:
            out.append(json.dumps(text, ensure_ascii=False)[1:-1])
    imp._note(node_id, "Values from other nodes are inserted into the JSON body as text, without escaping.")
    return "".join(out)


def _graphql(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "action.http_request", {})
    query = _json_string(imp, params.get("query", ""), node_id)
    variables = params.get("variables")
    variables_text = imp.text(variables, node_id).strip() if isinstance(variables, str) else ""
    if variables_text in ("", "="):
        variables_text = "{}"
        if isinstance(variables, dict):
            variables_text = json.dumps(_translated(imp, imp._scrub(variables), node_id), ensure_ascii=False)
    headers = {"Content-Type": "application/json"}
    headers.update(_header_items(imp, _pairs(params.get("headerParametersUi"), "parameter"), node_id))
    if (params.get("requestMethod") or "POST") != "POST":
        imp._review(node_id, "n8n sent the query as GET parameters; this node POSTs it.")
    if params.get("requestFormat") == "graphql":
        imp._note(node_id, "n8n sent a raw GraphQL body; this node sends the standard JSON envelope.")
    if (params.get("authentication") or "none") != "none":
        imp._review(
            node_id,
            "n8n authenticated this request with credentials, which are never imported: "
            "add the header from a secret variable.",
        )
    imp.node_by_id[node_id]["config"] = {
        "method": "POST",
        "url": imp.text(params.get("endpoint", ""), node_id),
        "headers": headers,
        "body": '{"query": "' + query + '", "variables": ' + variables_text + "}",
    }
    return _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})


def _rss_read(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "integration", {"operation": "fetch_rss_feeds"})
    url = _unwrap_locator(params.get("url"))
    if imp.is_expression(url):
        imp._review(node_id, f"n8n read the feed at a computed URL (`{_excerpt(str(url))}`).")
    else:
        imp._note(
            node_id,
            f"n8n read the feed `{_excerpt(_scalar_text(url))}`: add it to the feeds of the RSS "
            "integration picked here.",
        )
    imp._note(node_id, "The RSS integration answers with the entries as text, where n8n gave one item per entry.")
    return _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})


# ── Forms, error workflows, helpers ──────────────────────────────────────────


def _form_fields(params: dict) -> list[str]:
    return [
        _scalar_text(field.get("fieldLabel"))
        for field in _get(params, "formFields", "values", default=[]) or []
        if isinstance(field, dict) and field.get("fieldLabel")
    ]


def _start(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    return _single(imp, raw, "trigger.manual", {})


def _form_trigger(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    title = _scalar_text(params.get("formTitle"))
    description = _scalar_text(params.get("formDescription"))
    fields = _form_fields(params)
    prompt = "\n\n".join(part for part in (title, description) if part and not part.startswith("="))
    mapped = _single(imp, raw, "trigger.manual", {"prompt": prompt} if prompt else {})
    imp._review(
        mapped.entry,
        "n8n served a public form"
        + (f" (fields: {_excerpt(', '.join(fields))})" if fields else "")
        + "; XTM One has no public form: a person runs the flow by hand and the fields arrive "
        "in the run's payload, or a caller posts them to a Webhook trigger.",
    )
    declared = _get(params, "formFields", "values", default=[]) or []
    if any(isinstance(f, dict) and f.get("fieldType") == "file" for f in declared):
        imp._note(mapped.entry, "Files attached to a manual run reach every agent of the flow.")
    return mapped


def _form(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    """An n8n Form page: a question to a person; its ending: the run's result."""
    if params.get("operation") == "completion":
        node_id = imp._add_node(raw, "util.stop", {})
        message = params.get("completionMessage") or params.get("responseText") or ""
        if message:
            imp.node_by_id[node_id]["config"] = {"result": imp.text(message, node_id)}
        imp._note(node_id, "n8n showed this to the person filling the form; here it is the run's result.")
        return _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})
    return _ask_user(imp, raw, params)


def _ask_user(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "action.ask_user", {})
    options = params.get("options") if isinstance(params.get("options"), dict) else {}
    title = params.get("formTitle") or options.get("formTitle") or params.get("subject") or raw["name"]
    question = (
        params.get("message")
        or params.get("text")
        or params.get("formDescription")
        or options.get("formDescription")
        or ""
    )
    fields = _form_fields(params) or _form_fields(options)
    text = imp.text(question, node_id) if question else ""
    if fields:
        text = (text + "\n\n" if text else "") + "Answer: " + ", ".join(fields)
    imp.node_by_id[node_id]["config"] = {
        "title": imp.literal(title, node_id, "The title")[:200],
        "question": text or _scalar_text(title),
    }
    if params.get("defineForm") == "json":
        imp._review(node_id, "n8n built this form from JSON; write the question to ask here.")
    imp._note(
        node_id,
        "n8n waited for a person's answer; here the question lands in their notification bell "
        "and the answer comes out as text (`response`).",
    )
    return _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})


def _send_and_wait(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    """`Send and wait for response`: a human approval, as an Ask user node."""
    mapped = _ask_user(imp, raw, params)
    imp._note(
        mapped.entry,
        f"n8n sent the request through {_short_type(str(raw['type']))} and waited for the reply.",
    )
    if (params.get("responseType") or "approval") == "approval":
        imp._note(mapped.entry, "n8n asked to approve or disapprove: branch on the answer with an If node.")
    return mapped


def _error_trigger(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    mapped = _single(imp, raw, "trigger.event", {})
    imp._review(
        mapped.entry,
        "n8n started this workflow when another one failed. Here a failing step takes its "
        "error path: declare an event type, emit it from the error paths of the flows to "
        "watch, and pick it here.",
    )
    return mapped


def _imap(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    """n8n's IMAP trigger: the new-email event of a connected mailbox."""
    node_id = imp._add_node(raw, "trigger.integration_event", {"event_type": "new_email"})
    imp._note(
        node_id,
        "n8n read an IMAP mailbox: connect that mailbox as a Microsoft or Google integration "
        "and pick it; its `new_email` event starts the flow.",
    )
    return _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})


def _git(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    node_id = imp._add_node(raw, "action.runner_command", {})
    operation = _scalar_text(params.get("operation")) or "log"
    if not re.fullmatch(r"[A-Za-z]{1,32}", operation):  # n8n's operations are words
        operation = "status"
    repository = params.get("repositoryPath", "")
    imp._review(
        node_id,
        f"n8n ran `git {operation}` on a repository of its host: write the git command the runner runs.",
    )
    command = f"git -C {_shell_word(imp, repository, node_id)} {operation}" if repository else f"git {operation}"
    _review_shell(imp, node_id, command)
    return _runner(imp, raw, "run_shell", {"command": command}, node_id=node_id)


def _ical(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    return _binary_file(imp, raw, "made an iCalendar file")


def _execution_data(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    mapped = _single(imp, raw, "util.reroute", {})
    imp._note(mapped.entry, "n8n saved searchable execution data; every step of a run is journaled here.")
    return mapped


def _debug_helper(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    mapped = _single(imp, raw, "util.reroute", {})
    imp._review(
        mapped.entry,
        f"n8n's Debug Helper ran `{_excerpt(_scalar_text(params.get('category')) or 'doNothing')}` "
        "for testing; it passes its input through here.",
    )
    return mapped


# ── App nodes: integrations ──────────────────────────────────────────────────

_META_PARAMETERS = frozenset({"resource", "operation", "authentication", "requestOptions"})


def _translated(imp: _N8nImporter, value: Any, node_id: str) -> Any:
    """An n8n parameter tree with its references translated."""
    value = _unwrap_locator(value)
    if isinstance(value, dict):
        return {key: _translated(imp, item, node_id) for key, item in value.items()}
    if isinstance(value, list):
        return [_translated(imp, item, node_id) for item in value]
    if isinstance(value, str):
        return imp.text(value, node_id)
    return value


def _app_node(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped | None:
    """An app node of an XTM One provider: an Integration node."""
    short = _short_type(str(raw["type"]))
    if params.get("operation") == "sendAndWait":
        return _send_and_wait(imp, raw, params)
    if str(raw["type"]).startswith(_LANGCHAIN) or short.endswith("Tool"):
        return _lone_sub_node(imp, raw, params)
    if short.endswith("Trigger"):
        return _app_trigger(imp, raw, params)
    app = n8n_apps.app_for(short)
    if app is None:
        return None
    resource = _scalar_text(params.get("resource")) or app.default_resource
    operation = _scalar_text(params.get("operation")) or app.default_operations.get(resource, "")
    tool = n8n_apps.tool_for(app, resource, operation)
    node_id = imp._add_node(raw, "integration", {"operation": tool} if tool else {})
    parameters = {
        key: value
        for key, value in imp._scrub(params).items()
        if key not in _META_PARAMETERS and value not in ("", {}, [])
    }
    if parameters:
        imp.node_by_id[node_id]["config"]["parameters"] = _translated(imp, parameters, node_id)
    action = f"`{resource}: {operation}`" if operation else f"`{resource}`"
    if tool:
        imp._review(
            node_id,
            f"n8n's {app.label} node did {action}, the `{tool}` operation here: its parameters keep "
            "their n8n names under Parameters; rename them to the operation's arguments.",
        )
    else:
        imp._review(
            node_id,
            f"n8n's {app.label} node did {action}: pick the {app.label} operation that does the "
            "same and set its parameters (n8n's are kept under Parameters).",
        )
    imp._note(node_id, f"Pick the {app.label} integration (`{app.provider}`) this step uses.")
    return _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})


def _app_label(short: str) -> str:
    app = n8n_apps.app_for(short)
    if app is not None:
        return app.label
    key = n8n_apps.app_key(short)
    candidate = n8n_apps.CTEM_CANDIDATES.get(key)
    # The type name comes from the document: bounded like any excerpt.
    return candidate[0] if candidate else _excerpt(key[:1].upper() + key[1:])


def _app_trigger(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped | None:
    """An app's trigger: its integration event, a schedule, or its webhook."""
    short = _short_type(str(raw["type"]))
    if short in n8n_apps.HOST_TRIGGERS:
        return None
    label = _app_label(short)
    app = n8n_apps.app_for(short)
    if app is not None and app.event:
        node_id = imp._add_node(raw, "trigger.integration_event", {"event_type": app.event})
        imp._note(
            node_id,
            f"n8n listened to {label}: the `{app.event}` event of the {label} integration "
            f"(`{app.provider}`) starts the flow; pick that integration.",
        )
        if params.get("filters") or params.get("simple") is False:
            imp._note(node_id, "n8n filtered what started the workflow: add the same condition as an Event filter.")
        return _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})
    if short in n8n_apps.POLLING_TRIGGERS:
        times = params.get("pollTimes") if isinstance(params.get("pollTimes"), dict) else {}
        if not _get(times, "item"):
            times = {"item": [{"mode": "everyMinute"}]}
        mapped = _schedule(imp, {**raw, "type": _BASE + "cron"}, {"triggerTimes": times})
        imp._review(
            mapped.entry,
            f"n8n polled {label} for new or changed records on this schedule: read them in "
            "the next step (an integration, an HTTP request) and keep those not seen yet.",
        )
        return mapped
    events = params.get("events") or params.get("event") or params.get("updates") or params.get("triggerOn")
    described = f" (events: {_excerpt(_scalar_text(events))})" if events else ""
    path = re.sub(r"[^a-z0-9-]+", "-", n8n_apps.app_key(short).lower()).strip("-") or "app"
    node_id = imp._add_node(raw, "trigger.webhook", {"path": path})
    imp._review(
        node_id,
        f"n8n registered a webhook with {label}{described}: once the flow is active, point "
        f"{label}'s webhook at this flow's URL.",
    )
    return _Mapped(entry=node_id, outputs={0: [(node_id, "out")]})


# ── LangChain ────────────────────────────────────────────────────────────────


def _lone_sub_node(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped | None:
    """An AI sub-node attached to no node: its capability node, unwired."""
    name = raw["name"]
    if name in imp.main_inputs or name in imp.main_targets:
        return None  # in the main flow (a vector store inserting documents)
    short = _short_type(str(raw["type"]))
    if short.startswith("lm"):
        node_id = imp._model_node(raw)
    elif short.startswith("memory") and short != "memoryManager":
        node_id = imp._add_node(raw, "capability.memory", {"short_term": True})
    elif short.startswith(("vectorStore", "retriever")) or short == "toolVectorStore":
        node_id = imp._add_node(raw, "capability.knowledge_base", {})
    elif short == "outputParserStructured":
        schema = imp._parser_schema(params)
        node_id = imp._add_node(raw, "capability.structured_output", {"schema": schema} if schema else {})
    elif short.endswith("Tool") and (app := n8n_apps.app_for(short)) is not None:
        node_id = imp._add_node(raw, "capability.integration", {})
        imp._note(node_id, f"n8n's {app.label} tool: pick the {app.label} integration (`{app.provider}`).")
    elif short.startswith("tool") or short.endswith("Tool"):
        node_id = imp._add_node(raw, "capability.tool", {})
    else:
        return None
    imp._note(node_id, "n8n attached this to no node: wire it into the agent it serves.")
    return _Mapped(entry=node_id, outputs={})


_SUMMARY_PERSONA = "Write a concise summary of the text you are given, keeping its key facts."


def _summarization(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    whole = "={{ $json }}"
    mapped = _agent(
        imp,
        raw,
        {"promptType": "define", "text": whole, "options": {"systemMessage": _SUMMARY_PERSONA}},
    )
    imp._note(mapped.entry, "n8n's Summarization Chain: the agent summarizes what reaches it.")
    return mapped


def _retrieval_qa(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    text = params.get("text") or params.get("query")
    mapped = _agent(imp, raw, {"promptType": "define", "text": text} if text else {})
    imp._note(
        mapped.entry,
        "n8n answered from a retriever: the knowledge base wired into this agent plays that part.",
    )
    return mapped


def _assistant(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped:
    text = params.get("text") or "={{ $json.chatInput }}"
    mapped = _agent(imp, raw, {"promptType": "define", "text": text})
    assistant = _unwrap_locator(params.get("assistantId"))
    if assistant:
        imp._note(mapped.entry, f"n8n ran the OpenAI assistant `{_excerpt(_scalar_text(assistant))}`.")
    return mapped


def _legacy_openai(imp: _N8nImporter, raw: dict, params: dict) -> _Mapped | None:
    """The pre-LangChain OpenAI node: chat or text completions."""
    resource = params.get("resource") or "text"
    operation = params.get("operation") or "complete"
    if resource == "chat":
        messages = _get(params, "prompt", "messages", default=[]) or []
        mapped_messages = [
            {"role": m.get("role") or "user", "content": m.get("content")} for m in messages if isinstance(m, dict)
        ]
        return _openai(imp, raw, {"messages": {"values": mapped_messages}})
    if resource == "text" and operation == "complete":
        return _agent(imp, raw, {"promptType": "define", "text": params.get("prompt", "")})
    if resource == "text" and operation == "edit":
        instruction = _scalar_text(params.get("instruction"))
        mapped = _agent(
            imp,
            raw,
            {"promptType": "define", "text": params.get("input", ""), "options": {"systemMessage": instruction}},
        )
        return mapped
    return None


_CONVERTERS: dict[str, Any] = {
    _BASE + "manualTrigger": _manual_trigger,
    _BASE + "executeWorkflowTrigger": _sub_workflow_trigger,
    _BASE + "webhook": _webhook,
    _BASE + "scheduleTrigger": _schedule,
    _BASE + "cron": _schedule,
    _BASE + "interval": _schedule,
    _LANGCHAIN + "chatTrigger": _chat_trigger,
    _LANGCHAIN + "manualChatTrigger": _chat_trigger,
    _BASE + "if": _if,
    _BASE + "filter": _filter,
    _BASE + "switch": _switch,
    _BASE + "merge": _merge,
    _BASE + "wait": _wait,
    _BASE + "splitInBatches": _loop,
    _BASE + "noOp": _no_op,
    _BASE + "stickyNote": _sticky,
    _BASE + "set": _set,
    _BASE + "code": _code,
    _BASE + "httpRequest": _http,
    _BASE + "slack": _slack,
    _BASE + "microsoftTeams": _teams,
    _BASE + "telegram": _telegram,
    _BASE + "discord": _discord,
    _BASE + "whatsApp": _whatsapp,
    _BASE + "googleChat": _google_chat,
    _BASE + "gmail": _email,
    _BASE + "emailSend": _email,
    _BASE + "executeWorkflow": _sub_workflow,
    _BASE + "respondToWebhook": _respond,
    _BASE + "stopAndError": _stop_and_error,
    _LANGCHAIN + "agent": _agent,
    _LANGCHAIN + "chainLlm": _agent,
    _LANGCHAIN + "openAi": _openai,
    _LANGCHAIN + "textClassifier": _classifier,
    _LANGCHAIN + "informationExtractor": _extractor,
    _LANGCHAIN + "sentimentAnalysis": _classifier,
    _LANGCHAIN + "chainSummarization": _summarization,
    _LANGCHAIN + "chainRetrievalQa": _retrieval_qa,
    _LANGCHAIN + "openAiAssistant": _assistant,
    _LANGCHAIN + "code": _javascript,
    _BASE + "openAi": _legacy_openai,
    _BASE + "start": _start,
    _BASE + "formTrigger": _form_trigger,
    _BASE + "form": _form,
    _BASE + "errorTrigger": _error_trigger,
    _BASE + "function": _javascript,
    _BASE + "functionItem": _javascript,
    _BASE + "aggregate": _aggregate,
    _BASE + "limit": _limit,
    _BASE + "sort": _sort,
    _BASE + "removeDuplicates": _remove_duplicates,
    _BASE + "summarize": _summarize,
    _BASE + "renameKeys": _rename_keys,
    _BASE + "splitOut": _split_out,
    _BASE + "itemLists": _item_lists,
    _BASE + "dateTime": _date_time,
    _BASE + "crypto": _crypto,
    _BASE + "html": _html,
    _BASE + "htmlExtract": _html_extract,
    _BASE + "markdown": _markdown,
    _BASE + "xml": _xml,
    _BASE + "extractFromFile": _extract_from_file,
    _BASE + "convertToFile": _convert_to_file,
    _BASE + "spreadsheetFile": _spreadsheet_file,
    _BASE + "moveBinaryData": _move_binary_data,
    _BASE + "compareDatasets": _compare_datasets,
    _BASE + "totp": _totp,
    _BASE + "executeCommand": _execute_command,
    _BASE + "ssh": _ssh,
    _BASE + "readWriteFile": _read_write_file,
    _BASE + "readBinaryFile": _read_write_file,
    _BASE + "readBinaryFiles": _read_write_file,
    _BASE + "writeBinaryFile": _read_write_file,
    _BASE + "graphql": _graphql,
    _BASE + "rssFeedRead": _rss_read,
    _BASE + "executionData": _execution_data,
    _BASE + "debugHelper": _debug_helper,
    _BASE + "emailReadImap": _imap,
    _BASE + "aiTransform": _javascript,
    _BASE + "git": _git,
    _BASE + "iCal": _ical,
}

_CHANNEL_ONLY = (
    "Only sending a message maps here: XTM One reaches {} through a channel bot "
    "bound to an agent, so route this step through that agent."
)

_UNRESOLVED_HINTS: dict[str, str] = {
    _BASE + "code": (
        "JavaScript has no runtime here: rewrite the logic as Python in a Code node, or as an Expression node."
    ),
    _BASE
    + "switch": "The switch's rules are not in the export (left at n8n's defaults): rebuild them with a Switch node.",
    _BASE + "wait": "n8n resumed on an external call or a date: use Wait for event, or an Ask user node.",
    _BASE + "telegram": _CHANNEL_ONLY.format("Telegram"),
    _BASE + "discord": _CHANNEL_ONLY.format("Discord"),
    _BASE + "whatsApp": _CHANNEL_ONLY.format("WhatsApp"),
    _BASE + "openAi": "Images and audio have no flow step here: an agent with the image tools makes them.",
    _LANGCHAIN + "openAi": (
        "Generating images or audio, transcribing and managing OpenAI files have no flow step "
        "here: an agent with the matching tools does it."
    ),
    _LANGCHAIN + "mcpTrigger": (
        "XTM One serves its agents and flows to MCP clients through its MCP endpoints: "
        "expose the flow there instead of an n8n MCP server trigger."
    ),
    _BASE + "editImage": "Image editing has no flow step here: an agent with the image tools edits images.",
    _BASE + "compression": "Archives are binary files, which flows do not pass: an agent's file tools handle them.",
    _BASE + "readPDF": "XTM One agents read the PDF files attached to a run directly: let the agent read it.",
    _BASE + "n8nTrainingCustomerDatastore": "n8n's tutorial node serves sample data: delete it.",
    _BASE + "n8nTrainingCustomerMessenger": "n8n's tutorial node sends nothing: delete it.",
    _LANGCHAIN + "outputParserAutofixing": (
        "XTM One checks an agent's structured answer itself: wire a Structured output node into the agent."
    ),
    _LANGCHAIN + "outputParserItemList": (
        "Ask for a list with a Structured output node (an array schema) wired into the agent."
    ),
    _LANGCHAIN + "memoryManager": (
        "XTM One keeps an agent's conversation itself: wire a Memory node into the agent instead."
    ),
    "n8n-nodes-mcp.mcpClient": (
        "Register the MCP server in XTM One, then call its tools with a Call tool node or give them to an agent."
    ),
}


def _default_hint(node_type: str) -> str:
    short = _short_type(node_type)
    if short in n8n_apps.HOST_TRIGGERS:
        return (
            f"n8n's `{short}` listened on its own host (a message bus, a database, the file "
            "system). XTM One starts flows from webhooks, schedules and integration events: "
            "have a consumer post each message to a Webhook trigger."
        )
    if short.lower().endswith("trigger"):
        return (
            f"No equivalent for the n8n `{short}`: start the flow with an "
            "Integration event trigger bound to the matching integration, or "
            "with a Webhook."
        )
    if short.startswith(("vectorStore", "embeddings", "document", "textSplitter")):
        return "Load these documents into an XTM One knowledge base instead."
    if node_type.startswith(_LANGCHAIN):
        return (
            f"No equivalent for the n8n `{short}` node: replace it with an "
            "agent with the matching tools."
        )
    return n8n_apps.outside_catalog_hint(short)


def import_n8n(document: dict[str, Any]) -> dict[str, Any]:
    """Convert an n8n workflow export into a native flow payload + report."""
    try:
        document = _strip_ref_marks(document)
    except RecursionError as exc:
        # Parameters nested past what any real workflow holds: refused as
        # unreadable rather than answered with a server error.
        raise FlowImportError("This workflow's parameters are nested too deeply to import.") from exc
    workflow = unwrap_n8n_workflow(document)
    if workflow is None:
        raise FlowImportError("This is not an n8n workflow export.")
    if _envelope_name(workflow) is None and _envelope_name(document):
        workflow = {**workflow, "name": _envelope_name(document)}
    try:
        return _N8nImporter(workflow).run()
    except RecursionError as exc:
        raise FlowImportError("This workflow's parameters are nested too deeply to import.") from exc
    except _SHAPE_ERRORS as exc:
        # A shape no converter owns (the connections, the envelope): a 400
        # naming the cause class only, never a 500.
        raise FlowImportError("This n8n workflow has a structure this importer cannot read.") from exc
