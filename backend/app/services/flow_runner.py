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

"""Flow execution — the graph walker and per-node executors.

Executes a :class:`~app.models.flow_run.FlowRun`'s frozen graph node by node,
sequentially and deterministically.  The walk is a **burst**: it runs until the
graph completes, a node fails with no error route, or a node needs to wait
(delay, sub-flow, assignment) — at which point the whole cursor is persisted in
``run.state`` and the ``flow`` orchestration adapter parks the execution.  A
resume picks the cursor back up exactly where the burst stopped.

Design decisions, and why:

* **Sequential, not parallel.**  Branches that fan out are executed in FIFO
  order, one node at a time.  Two runs of the same graph therefore execute the
  same node order — which is what makes the step journal readable and the
  merge semantics decidable.  Parallelism inside one run is a later phase.
* **The declared graph is the plan.**  Unlike the agent-handover runtime,
  where ``handover_agent_ids`` is a *permission set* and the LLM decides the
  path, an edge here is always honoured.  The two systems stay side by side:
  an agent node may still delegate to its declared peers *inside* the node
  (``allow_handover`` per node), but which node runs next is decided by edges,
  never by the model.
* **Conditions are deterministic unless you say otherwise.**  ``condition.if``
  / ``condition.switch`` evaluate the catalog's nine operators over
  interpolated strings.  Wiring a ``capability.llm`` node into the node's
  ``llm`` socket opts that ONE branch into a judgement call instead
  (`flow_condition_llm.py`) — visible on the canvas as an edge, never implicit.
  The model only picks among branches the graph already declares, and a failed
  call takes the false/default branch rather than killing the run, so the
  walker stays predictable either way.
* **Loops advance on queue drain.**  ``util.loop`` enqueues its ``each``
  branch; when the queue empties and the loop has items left, the next pass
  starts, and ``done`` fires after the last pass.  ``util.while`` rides the
  same machinery.  An explicit back-edge to an unfinished loop is tolerated
  (it is how the canvas draws the intent) but is not what advances the loop
  — that keeps nested shapes from double-stepping.  Loops nest: the drain
  advances the innermost unfinished loop (the one armed last) first, and a
  finished loop reached again starts afresh only when a loop it runs inside
  has moved to another pass since it was armed — so the inner body runs on
  every outer pass, while an edge back into a finished top-level loop (a
  merge-joined back-edge, a wire drawn from ``done``) stays ignored.
* **Taint travels.**  ``{{trigger.*}}`` from a webhook or integration event is
  untrusted (same rule as assignments); any step whose inputs interpolated an
  untrusted value becomes untrusted itself, and untrusted values are
  quarantine-wrapped when they land in an agent prompt.  Without propagation,
  one hop through a passthrough node would launder attacker input.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.egress import egress_client, egress_route
from app.core.redaction import restore_known_secrets
from app.core.scopes import (
    COORDINATION_SCOPE_DEFAULT,
    COORDINATION_SCOPE_FLOW,
    COORDINATION_SCOPE_GLOBAL,
    COORDINATION_SCOPE_RUN,
    COORDINATION_SCOPE_WORKSPACE,
    COORDINATION_SCOPES,
)
from app.core.utils import format_exception_message, parse_delay, safe_uuid
from app.models.flow import Flow
from app.models.flow_run import (
    FLOW_CALL_MAX_DEPTH,
    FLOW_RUN_ACTIVE_STATUSES,
    FLOW_RUN_STATUS_AWAITING,
    FLOW_RUN_STATUS_CANCELLED,
    FLOW_RUN_STATUS_COMPLETED,
    FLOW_RUN_STATUS_FAILED,
    FLOW_RUN_STATUS_PENDING,
    FLOW_RUN_STATUS_RUNNING,
    FLOW_RUN_STATUS_SKIPPED,
    FlowRun,
    FlowRunStep,
)
from app.models.user import User
from app.services.budget_policy import (
    merge_overrides as merge_budget_overrides,
)
from app.services.budget_policy import (
    parse_overrides as parse_budget_overrides,
)
from app.services.flow_catalog import get_node_kind
from app.services.flow_graph import PLACEHOLDER_RE, pending_import_review
from app.services.flow_run_stream import publish_flow_run_update
from app.services.workspaces import (
    WorkspaceScopeError,
    resolve_workspace_scope,
    workspace_namespace,
)

logger = logging.getLogger(__name__)

# Hard cap on journal length per run: catches accidental infinite shapes that
# the per-loop max_iterations cannot see (e.g. two loops feeding each other).
MAX_STEPS_PER_RUN = 1000

# How often a paused (breakpoint) run re-checks for a debugger release.
BREAKPOINT_POLL_SECONDS = 2
# A human answer arrives via the respond endpoint (which wakes the engine);
# the poll is only the safety net.
HUMAN_POLL_SECONDS = 5

# How often a parked run re-checks a triggered assignment.
ASSIGNMENT_POLL_SECONDS = 15
# Safety recheck while waiting on a sub-flow (its completion event is the real
# wake signal; this is the belt-and-braces sweep).
FLOW_CALL_RECHECK_SECONDS = 600

# Trigger types whose payload is attacker-influenceable — mirrors the
# assignment renderer's untrusted set.
# A platform event's payload carries titles and notes other people and agents
# wrote: read like a webhook's.
UNTRUSTED_TRIGGER_TYPES = frozenset({"webhook", "integration_event", "platform_event"})

_ANNOTATION_KINDS = frozenset({"util.note", "util.group"})
_TRIGGER_PREFIX = "trigger."



def _coerce_uuid(value) -> uuid.UUID | None:
    """State JSON stores ids as strings; typed UUID columns want UUIDs."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _parse_iso_datetime(value: Any) -> datetime | None:
    """A rendered date field as a datetime, or None when unusable.

    Mapping the wrong upstream field is the likely mistake here, so an
    unparseable value becomes "undated" rather than today: a wrong date silently
    places a document in the wrong reporting window, while a missing one is
    visible in every listing.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _as_int(value: Any) -> int | None:
    """A numeric config value as int, or None for empty/blank/invalid."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


# ─── Burst outcome (what the adapter consumes) ───────────────────────────────


@dataclass(frozen=True)
class BurstOutcome:
    """How a burst ended.  ``park`` carries when to self-wake, if ever."""

    kind: str  # "completed" | "failed" | "cancelled" | "park" | "skipped"
    error: str | None = None
    wake_at: datetime | None = None


# ─── Deterministic condition evaluation ──────────────────────────────────────


def evaluate_operator(left: str, operator: str, right: str) -> bool:
    """The catalog's nine `condition.if` operators, over two rendered strings.

    Numeric comparison is attempted first for ``gt``/``lt`` so `"9" < "10"`
    behaves the way every user expects; it falls back to lexicographic when
    either side is not a number.
    """
    if operator == "equals":
        return left == right
    if operator == "not_equals":
        return left != right
    if operator == "contains":
        return right in left
    if operator == "not_contains":
        return right not in left
    if operator == "matches":
        try:
            return re.search(right, left) is not None
        except re.error:
            return False
    if operator in ("gt", "lt"):
        try:
            l_num, r_num = float(left), float(right)
            return l_num > r_num if operator == "gt" else l_num < r_num
        except ValueError:
            return left > right if operator == "gt" else left < right
    if operator == "is_empty":
        return not left.strip()
    if operator == "is_not_empty":
        return bool(left.strip())
    return False


def matches_event_filter(payload: Any, predicate: Any) -> bool:
    """The trigger JSON filter: subset equality, with list = any-of.

    Every key in *predicate* must exist in *payload* with an equal value;
    nested dicts recurse.  An empty/None predicate matches everything.

    A **list** value means "any of these" — ``{"status": ["planned",
    "blocked"]}`` matches either.  Without it, subscribing to a set of values
    (the memory-event status tokens, a set of webhook actions) would
    need one trigger node per value, and the alternative reading — exact
    equality against a list-valued payload field — is not something a flow
    author can express against these payloads anyway.  A list on BOTH sides
    still compares element-wise, so a genuinely list-valued field can be
    matched by nesting it: ``{"labels": [["a", "b"]]}``.
    """
    if predicate in (None, {}, ""):
        return True
    if not isinstance(predicate, dict) or not isinstance(payload, dict):
        return payload == predicate
    for key, expected in predicate.items():
        if key not in payload:
            return False
        actual = payload[key]
        if isinstance(expected, dict):
            if not matches_event_filter(actual, expected):
                return False
        elif isinstance(expected, list):
            # Scalars compare directly rather than recursing: the empty-ish
            # predicate short-circuit at the top means a bare ``None`` option
            # would otherwise match every value.
            if not any(
                matches_event_filter(actual, option)
                if isinstance(option, dict)
                else actual == option
                for option in expected
            ):
                return False
        elif actual != expected:
            return False
    return True


# ─── Interpolation with taint tracking ───────────────────────────────────────


@dataclass
class _RenderResult:
    text: str
    tainted: bool = False


def _lookup_path(source: Any, path: list[str]) -> Any:
    """Walk a ``{{ref.path}}`` into dicts (by key) and lists (by index).

    A key on a list of objects lists that key of each object.

    Deliberately NOT ``getattr``: attribute access on arbitrary values let a
    template render bound methods (``{{x.title}}`` on a string) and reach
    things like ``__class__`` — a template resolves DATA, never Python
    attributes.
    """
    current = source
    for part in path:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, (list, tuple)) and part.lstrip("-").isdigit():
            if not -len(current) <= int(part) < len(current):
                return None
            current = current[int(part)]
        elif isinstance(current, (list, tuple)):
            # A key on a list of objects lists that key: `{{n.items.name}}`
            # is every item's name.
            objects = [item for item in current if isinstance(item, dict)]
            if not objects:
                return None
            current = [item[part] for item in objects if item.get(part) is not None]
        else:
            return None
        if current is None:
            return None
    return current


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _json_object(text: str) -> dict | None:
    """*text* parsed when it is a JSON object, else None.

    The ``fields`` output of the nodes that print or answer (Script, Run code,
    agent): ``{{node.fields.KEY}}`` reads a key, any other text has no fields.
    """
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def flow_integration_context(user_id: Any) -> dict[str, Any]:
    """The context a flow step builds its integration tools with.

    The person the run acts as: an integration whose people each connect their
    own account acts with theirs. Autonomous: a step asks nobody during the
    run, so a write that waits for a person (OpenCTI's knowledge draft) is
    provisioned the way an assignment's is, never answered with a question.
    """
    return {"source": "flow", "credential_user_id": str(user_id), "autonomous": True}


def operation_data(result: Any) -> dict[str, Any]:
    """What an Integration node's operation answered as data.

    ``fields`` for an object (``{{node.fields.KEY}}``, the contract of the
    Script and Run code nodes), ``items`` for a list (a Loop iterates it,
    ``{{node.items.name}}`` lists one key). The data is the operation's
    structured value, or its text when that text is JSON: one reading with an
    assignment's gate check (``structured_result``).
    """
    from app.services.assignment_activation_check import structured_result

    value, found = structured_result(result)
    if found and isinstance(value, dict):
        return {"fields": value}
    if found and isinstance(value, list):
        return {"items": value}
    return {}


class _Renderer:
    """Resolves ``{{node.output}}`` / ``{{trigger.*}}`` / ``{{var_id.*}}``.

    Owns the taint verdict: rendering marks the result tainted when any
    substituted value came from an untrusted trigger or an untrusted step.
    ``wrap`` quarantines those values inline (agent prompts only — wrapping a
    URL or a work-item title would corrupt it; the taint still propagates so
    a later agent node quarantines it there).
    """

    def __init__(
        self,
        *,
        node_outputs: dict[str, dict],
        untrusted_nodes: set[str],
        trigger_payload: dict,
        trigger_untrusted: bool,
        variables: dict[str, str],
        trigger_source: str,
    ) -> None:
        self._outputs = node_outputs
        self._untrusted = untrusted_nodes
        self._trigger = trigger_payload
        self._trigger_untrusted = trigger_untrusted
        self._variables = variables
        self._trigger_source = trigger_source

    def render(self, template: str, *, wrap: bool = False) -> _RenderResult:
        from app.services.content_safety import wrap_untrusted
        from app.services.variable_resolver import substitute_variables

        tainted = False

        def replace(match: re.Match[str]) -> str:
            nonlocal tainted
            ref = match.group(1).strip()
            head, _, rest = ref.partition(".")
            path = rest.split(".") if rest else []
            if ref.startswith("var_id."):
                # Platform variables resolve below via substitute_variables.
                return match.group(0)
            if head == "trigger":
                value = _to_text(_lookup_path(self._trigger, path))
                if self._trigger_untrusted and value:
                    tainted = True
                    if wrap:
                        return wrap_untrusted(value, self._trigger_source)
                return value
            step_output = self._outputs.get(head)
            if step_output is None:
                return ""
            # A step of this run stored a secret variable value as its marker:
            # restored before an object is serialized, so JSON escapes the value too.
            value = _to_text(restore_known_secrets(_lookup_path(step_output, path)))
            if head in self._untrusted and value:
                tainted = True
                if wrap:
                    return wrap_untrusted(value, f"flow step {head}")
            return value

        text = PLACEHOLDER_RE.sub(replace, template)
        text = substitute_variables(text, self._variables)
        return _RenderResult(text=text, tainted=tainted)

    def render_value(self, value: Any, *, wrap: bool = False) -> tuple[Any, bool]:
        """Recursively render every string inside a config value."""
        if isinstance(value, str):
            result = self.render(value, wrap=wrap)
            return result.text, result.tainted
        if isinstance(value, list):
            items, tainted = [], False
            for item in value:
                rendered, item_tainted = self.render_value(item, wrap=wrap)
                items.append(rendered)
                tainted = tainted or item_tainted
            return items, tainted
        if isinstance(value, dict):
            out, tainted = {}, False
            for key, item in value.items():
                rendered, item_tainted = self.render_value(item, wrap=wrap)
                out[key] = rendered
                tainted = tainted or item_tainted
            return out, tainted
        return value, False


# ─── Graph helpers ───────────────────────────────────────────────────────────


def _memory_chain_handoff() -> dict | None:
    """The running burst's memory event chain, JSON-shaped for a run row.

    ``None`` when nothing is bound, so a trigger_context outside a flow burst
    stays exactly as it was.
    """
    from app.services.memory_flow_events import current_memory_chain

    chain = current_memory_chain()
    if chain is None:
        return None
    return {
        "flow_id": str(chain.flow_id) if chain.flow_id else None,
        "chain_depth": chain.chain_depth,
    }


def _nodes_by_id(graph: dict) -> dict[str, dict]:
    return {
        n["id"]: n
        for n in graph.get("nodes") or []
        if isinstance(n, dict) and n.get("id")
    }


def _edges_from(graph: dict, node_id: str, handle: str) -> list[dict]:
    return [
        e
        for e in graph.get("edges") or []
        if isinstance(e, dict)
        and e.get("source") == node_id
        and (e.get("source_handle") or "out") == handle
    ]


def _edges_into(graph: dict, node_id: str) -> list[dict]:
    return [
        e
        for e in graph.get("edges") or []
        if isinstance(e, dict) and e.get("target") == node_id
    ]


def wired_capabilities(
    graph: dict, node_id: str, nodes: dict[str, dict] | None = None
) -> dict[str, Any]:
    """Capability nodes wired into an agent node's bottom-edge sockets.

    What a run gives the agent and what a save checks (``flow_references``)
    read the graph through this one function. Id families
    (tools/integrations/skills/knowledge_bases) return lists — a node with
    several outgoing edges appears at every agent it feeds, which IS the
    sharing model. ``output_schema`` is singular: an agent has ONE schema, so
    the FIRST wired one wins.
    """
    nodes = _nodes_by_id(graph) if nodes is None else nodes
    wired: dict[str, Any] = {
        "tools": [],
        "integrations": [],
        "skills": [],
        "knowledge_bases": [],
        "output_schema": None,
        "memory": None,
        "guardrails": None,
    }
    # Each id-family node carries a LIST now (multi-select), so one node
    # can declare several tools/skills/etc. rather than one node per id.
    key_by_kind = {
        "capability.tool": ("tools", "tool_ids"),
        "capability.integration": ("integrations", "integration_ids"),
        "capability.skill": ("skills", "skill_ids"),
        "capability.knowledge_base": ("knowledge_bases", "knowledge_base_ids"),
    }
    # One identified socket per family on the agent's bottom edge; the
    # family itself is derived from the SOURCE node's kind — the port
    # matrix already guarantees the two agree.
    id_handles = frozenset(family for family, _ in key_by_kind.values())
    for edge in _edges_into(graph, node_id):
        handle = edge.get("target_handle") or ""
        source = nodes.get(edge.get("source") or "")
        if not isinstance(source, dict):
            continue
        source_kind = source.get("kind") or ""
        source_config = source.get("config") or {}
        # Singular settings — the FIRST wired one wins (validator warns on
        # a second), mirroring structured output.
        if handle == "output" and source_kind == "capability.structured_output":
            schema = source_config.get("schema")
            if isinstance(schema, dict) and wired["output_schema"] is None:
                wired["output_schema"] = schema
            continue
        if handle == "memory" and source_kind == "capability.memory":
            if wired["memory"] is None:
                wired["memory"] = {
                    flag: bool(source_config.get(flag))
                    for flag in ("short_term", "medium_term", "persistent", "user")
                }
            continue
        if handle == "guardrails" and source_kind == "capability.guardrails":
            text = source_config.get("guardrails")
            if isinstance(text, str) and text and wired["guardrails"] is None:
                wired["guardrails"] = text
            continue
        if handle not in id_handles:
            continue
        mapping = key_by_kind.get(source_kind)
        if mapping is None:
            continue
        family, field_name = mapping
        values = source_config.get(field_name)
        if isinstance(values, str):  # tolerate a legacy single value
            values = [values]
        if isinstance(values, list):
            wired[family].extend(v for v in values if isinstance(v, str) and v)
    return wired


def find_trigger_nodes(graph: dict, kind: str | None = None) -> list[dict]:
    """Trigger nodes in document order, optionally restricted to one kind."""
    out = []
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        node_kind = node.get("kind") or ""
        if not node_kind.startswith(_TRIGGER_PREFIX):
            continue
        if kind is not None and node_kind != kind:
            continue
        out.append(node)
    return out


def declared_parameters(config: dict) -> list[dict]:
    """A manual trigger's declared input parameters, normalised.

    Two formats coexist: the original KEY_VALUE dict (``{name: default}``)
    and the typed list (``[{name, type, required, default, description}]``)
    — the UE "function signature" upgrade.  Both normalise to the list shape
    so every consumer (run endpoint, ``flow.call``, the Run dialog) reads
    one contract.
    """
    raw = (config or {}).get("parameters")
    if isinstance(raw, dict):
        return [
            {"name": str(k).strip(), "type": "string", "required": False, "default": v}
            for k, v in raw.items()
            if str(k).strip()
        ]
    if isinstance(raw, list):
        out: list[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            out.append(
                {
                    "name": name,
                    "type": item.get("type") or "string",
                    "required": bool(item.get("required")),
                    "default": item.get("default"),
                    "description": item.get("description") or "",
                }
            )
        return out
    return []


def primitive_value(
    config: dict,
    variables: dict[str, str],
    object_types: dict[str, list] | None = None,
) -> Any:
    """A `util.primitive` node's constant.

    A picked platform variable wins; otherwise the literal is coerced to the
    declared type — best-effort, falling back to the raw text so a typo never
    crashes a run (the consumer's own validation is the real gate).

    Object constants (the UE struct pin): the declared type's fields are the
    schema, ``config.object_fields`` the per-field literals, and the value is
    the coerced dict — ``{{node.value.<field>}}`` downstream. A missing or
    deleted type yields ``{}`` rather than a crash.
    """
    variable_id = str(config.get("variable_id") or "").strip()
    if variable_id:
        return variables.get(variable_id, "")
    if (config.get("value_type") or "text") == "object":
        from app.services.flow_events import coerce_event_fields

        fields = (object_types or {}).get(
            str(config.get("object_type_id") or "").strip()
        )
        if fields is None:
            return {}
        raw_fields = config.get("object_fields") or {}
        rendered = {
            k: v
            for k, v in raw_fields.items()
            if isinstance(v, str) and v != ""
        }
        payload, _errors = coerce_event_fields(fields, rendered)
        return payload
    raw = config.get("value")
    text = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
    value_type = config.get("value_type") or "text"
    try:
        if value_type == "number":
            stripped = text.strip()
            return float(stripped) if "." in stripped else int(stripped)
        if value_type == "boolean":
            return text.strip().lower() in ("true", "1", "yes", "on")
        if value_type == "json":
            return json.loads(text)
    except (ValueError, json.JSONDecodeError):
        pass
    return text


def _truncate_for_journal(value: Any, limit: int = 1500) -> Any:
    """Bound a rendered config for the step journal — troubleshooting needs
    the values, not megabytes of them."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "… [truncated]"
    if isinstance(value, dict):
        return {k: _truncate_for_journal(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate_for_journal(v, limit) for v in value[:50]]
    return value


#: The most of a runner command's result a flow reads, as compact JSON: the
#: 1 MiB the result upload admitted before it was raised to
#: ``RUNNER_MAX_RESULT_META_BYTES``, so a flow sees what it always did. The
#: command's own row keeps the whole result.
FLOW_RUNNER_RESULT_MAX_BYTES = 1024 * 1024
#: Added to a result cut to fit: the size it had and the limit.
FLOW_RUNNER_RESULT_TRUNCATED_KEY = "flow_truncated"
#: Cuts tried before a result that still does not fit is reduced to its note.
_RUNNER_RESULT_MAX_CUTS = 64


def _json_bytes(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode()
    )


def _longest_prefix_within(text: str, max_bytes: int) -> int:
    """How many leading characters of ``text`` fit in ``max_bytes`` of JSON.

    Searched on the encoding itself: escaping makes a character one to six
    bytes, and the dense part of an output may be its head.
    """
    low, high = 0, max(0, min(len(text), max_bytes))
    while low < high:
        mid = (low + high + 1) // 2
        if _json_bytes(text[:mid]) <= max_bytes:
            low = mid
        else:
            high = mid - 1
    return low


def _largest_cuttable(
    value: Any, done: set[tuple[int, Any]]
) -> tuple[Any, Any, Any, int] | None:
    """The ``(container, key, value)`` of the largest text or list in ``value``,
    and the size of the next largest."""
    best: tuple[int, Any, Any, Any] | None = None
    runner_up = 0
    stack = [value]
    while stack:
        container = stack.pop()
        if isinstance(container, dict):
            items = [
                (k, v) for k, v in container.items() if k != FLOW_RUNNER_RESULT_TRUNCATED_KEY
            ]
        elif isinstance(container, list):
            items = list(enumerate(container))
        else:
            continue
        for key, child in items:
            if isinstance(child, (dict, list)):
                stack.append(child)
            if isinstance(child, (str, list)) and (id(container), key) not in done:
                size = _json_bytes(child)
                if best is None or size > best[0]:
                    runner_up = best[0] if best is not None else 0
                    best = (size, container, key, child)
                else:
                    runner_up = max(runner_up, size)
    return None if best is None else (best[1], best[2], best[3], runner_up)


def bound_runner_result(result: dict, limit: int = FLOW_RUNNER_RESULT_MAX_BYTES) -> dict:
    """A runner command's ``result`` as a flow reads it.

    Returned as it is within ``limit`` bytes of JSON. A larger one is copied
    and cut, largest part first: a text keeps its head followed by the marker
    a truncated tool result carries, a list its first items, and
    ``FLOW_RUNNER_RESULT_TRUNCATED_KEY`` says how large it was.
    """
    size = _json_bytes(result)
    if size <= limit:
        return result
    note = {"original_bytes": size, "limit_bytes": limit}
    marker = (
        f"\n\n... [TRUNCATED — runner result was {size:,} bytes, limit is "
        f"{limit:,} for a flow.] ..."
    )
    marker_bytes = len(marker.encode())
    bounded = copy.deepcopy(result)
    bounded[FLOW_RUNNER_RESULT_TRUNCATED_KEY] = note
    done: set[tuple[int, Any]] = set()
    for _ in range(_RUNNER_RESULT_MAX_CUTS):
        over = _json_bytes(bounded) - limit
        if over <= 0:
            return bounded
        slot = _largest_cuttable(bounded, done)
        if slot is None:
            break
        container, key, value, runner_up = slot
        # Never below the next largest part while more than this one must go
        # (nor below half), so two long streams keep a head each.
        floor = min(runner_up, _json_bytes(value) // 2)
        if isinstance(value, str):
            text = value.removesuffix(marker)
            written = _json_bytes(text)
            target = max(written - over - marker_bytes, floor - marker_bytes)
            keep = _longest_prefix_within(text, target)
            container[key] = text[:keep] + marker
        else:
            keep = len(value)
            freed = 0
            to_free = min(over, _json_bytes(value) - floor)
            while keep and freed < to_free:
                keep -= 1
                freed += _json_bytes(value[keep]) + 1
            container[key] = value[:keep]
        if keep == 0:
            done.add((id(container), key))
    return {FLOW_RUNNER_RESULT_TRUNCATED_KEY: note}


def coerce_tool_arguments(schema: dict, arguments: dict) -> dict:
    """Best-effort primitive coercion of rendered tool arguments.

    The Integration drawer's parameter form stores every value as a string
    (templates like ``{{node.output}}`` demand text inputs), but providers
    validate against the tool's JSON Schema — an int argument sent as "5"
    fails. Coerce number/integer/boolean per the schema after rendering;
    anything ambiguous stays untouched (the provider's own validation is
    the final word).
    """
    properties = (
        schema.get("properties") if isinstance(schema, dict) else None
    ) or {}
    out: dict = {}
    for key, value in (arguments or {}).items():
        spec = properties.get(key)
        expected = spec.get("type") if isinstance(spec, dict) else None
        if isinstance(value, str):
            text = value.strip()
            try:
                if expected == "integer":
                    out[key] = int(text)
                    continue
                if expected == "number":
                    out[key] = float(text) if "." in text else int(text)
                    continue
                if expected == "boolean" and text.lower() in (
                    "true", "false", "1", "0", "yes", "no",
                ):
                    out[key] = text.lower() in ("true", "1", "yes")
                    continue
                if expected in ("object", "array") and text[:1] in ("{", "["):
                    out[key] = json.loads(text)
                    continue
            except (ValueError, json.JSONDecodeError):
                pass  # leave as text; the provider reports the real error
        out[key] = value
    return out


def coerce_parameters(
    declared: list[dict], provided: dict
) -> tuple[dict, list[str]]:
    """Type-coerce run parameters against the declaration.

    Defaults fill the gaps first, then the caller's values win.  Values not
    covered by the declaration pass through untouched, so pre-declaration
    callers keep working.  A non-empty error list means the run (or the
    calling node) must refuse rather than start with a malformed payload.
    """
    from app.services.flow_events import coerce_event_fields

    def _as_text(value: Any) -> str:
        return value if isinstance(value, str) else json.dumps(value)

    merged: dict[str, str] = {}
    for param in declared:
        default = param.get("default")
        if default not in (None, ""):
            merged[param["name"]] = _as_text(default)
    for key, value in (provided or {}).items():
        if value is None:
            continue
        merged[str(key)] = _as_text(value)

    payload, errors = coerce_event_fields(declared, merged)
    declared_names = {param["name"] for param in declared}
    for key, value in (provided or {}).items():
        if key not in declared_names and value is not None:
            payload[key] = value
    return payload, errors


def _primary_output(node_kind: str, outputs: dict) -> str:
    """The first declared output variable's value — a node's 'result'."""
    kind_def = get_node_kind(node_kind)
    if kind_def is not None:
        for variable in kind_def.output_variables:
            if variable.name in outputs:
                return _to_text(outputs[variable.name])
    for value in outputs.values():
        return _to_text(value)
    return ""


async def final_step_output(db: AsyncSession, run_id: uuid.UUID) -> str:
    """A run's final output: its last completed non-trigger step's primary output, uncut.

    What ``result_summary`` stores (cut to 2,000 characters), what a
    ``flow.call`` parent reads, and what an evaluation judges (#8257).
    """
    row = (
        await db.execute(
            select(FlowRunStep)
            .where(
                FlowRunStep.flow_run_id == run_id,
                FlowRunStep.status == "completed",
                FlowRunStep.node_kind.not_like("trigger.%"),
            )
            .order_by(FlowRunStep.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return ""
    return _primary_output(row.node_kind, row.output or {})


class TriggerParametersError(ValueError):
    """Run parameters that do not match the entry trigger's declaration."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def manual_run_trigger(graph: dict, provided: dict) -> tuple[dict, str, dict]:
    """The entry trigger of a manual run and its ``(node, trigger_type, trigger_payload)``.

    The manual trigger first, then the chat trigger, then any. A chat trigger
    takes ``provided["message"]``; any other coerces *provided* against its
    declared parameters, plus ``provided["prompt"]``. One rule for the
    ``run_flow`` tool and an evaluation's flow subject (#8257). Raises
    ``LookupError`` with no trigger, ``TriggerParametersError`` on a mismatch.
    """
    triggers = (
        find_trigger_nodes(graph, "trigger.manual")
        or find_trigger_nodes(graph, "trigger.chat")
        or find_trigger_nodes(graph)
    )
    if not triggers:
        raise LookupError("no trigger node")
    entry = triggers[0]
    entry_kind = entry.get("kind") or "trigger.manual"
    config = entry.get("config") or {}
    trigger_type = entry_kind.removeprefix("trigger.") if entry_kind.startswith("trigger.") else "manual"
    if entry_kind == "trigger.chat":
        return entry, trigger_type, {"message": str(provided.get("message") or config.get("message") or "Run")}
    declared = declared_parameters(config)
    if declared:
        payload, errors = coerce_parameters(declared, provided)
        if errors:
            raise TriggerParametersError(errors)
    else:
        payload = provided
    return entry, trigger_type, {"payload": payload, "prompt": str(provided.get("prompt") or config.get("prompt") or "")}


# ─── Node execution result ──────────────────────────────────────────────────


@dataclass
class _NodeResult:
    outputs: dict = field(default_factory=dict)
    # Output handles whose edges the walk follows next.
    next_handles: list[str] = field(default_factory=lambda: ["out"])
    # For condition.switch: the matched case label (edges on the `case`
    # handle are filtered to the one whose label equals it).
    switch_case: str | None = None
    # For agents: the handover types that OCCURRED — edges on the `handover`
    # handle are filtered by label (unlabelled = every type;
    # `mandatory_transfer` labels match transfer occurrences).
    handover_types: frozenset[str] | None = None
    # Park request: the step stays running and the burst stops.
    park: dict | None = None
    error: str | None = None
    # The error fails the run even when the node's `error` port is wired: a
    # node that may not run at all (an imported step awaiting review) is not
    # a failure an error branch was drawn to handle.
    fatal: bool = False
    # A step that completed on an answer reading as an error: kept on the
    # journal row, never a failure.
    warning: str | None = None
    # `util.stop`: complete the run right here — everything still queued
    # (parallel branches, pending loop passes) is dropped.
    halt: bool = False


# ─── The walker ──────────────────────────────────────────────────────────────


class FlowWalker:
    """One burst of execution over a run's frozen graph.

    The instance is throwaway — all durable state lives in ``run.state`` and
    the ``flow_run_steps`` journal.  The walker commits ``self.db`` at every
    step transition (node started / finished / parked / run terminal) so the
    journal is live: the runs panel watches a node flip to ``running`` while
    it executes, and a crash mid-burst resumes at the last finished node
    instead of replaying the whole burst.  Each commit is followed by a
    best-effort ``flow_run_update`` frame on the SSE activity stream, which
    is the accelerator — polling stays the floor.
    """

    def __init__(
        self,
        db: AsyncSession,
        run: FlowRun,
        user: User,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        self.db = db
        self.run = run
        self.user = user
        # The reconciler's fast cancel signal (emergency stop, operator
        # cancel). The per-node status check catches a cancel BETWEEN nodes;
        # this reaches the agent loop INSIDE an agent node.
        self.cancel_event = cancel_event
        self.graph = run.graph or {}
        self.nodes = _nodes_by_id(self.graph)
        self.state: dict = dict(run.state or {})
        self.state.setdefault("seq", 0)
        self.state.setdefault("queue", [])
        self.state.setdefault("loops", {})
        self.state.setdefault("merges", {})
        self.state.setdefault("vars", {})
        self.state.setdefault("vars_tainted", [])
        self.state.setdefault("node_counters", {})
        self.state.setdefault("untrusted", [])
        # Run attachments (trigger_payload["files"]), loaded once per burst.
        self._trigger_files_cache: list | None = None
        self._renderer: _Renderer | None = None
        # Langfuse root trace of THIS burst (None when tracing is disabled).
        self._trace_ctx: dict | None = None
        # Rendered config of the node being executed (journal input).
        self._step_input: dict | None = None
        # The flow's display name (set at burst start).
        self._flow_name: str | None = None
        # Where the Call-tool nodes' tool names resolve (loaded on first use).
        self._tool_scope: CustomToolScope | None = None
        # Workset documents awaiting embedding. Collected during the burst and
        # queued by the CALLER after it commits: a worker that dequeued while
        # the insert was still invisible would find no row, treat the job as
        # stale and drop it, leaving the item pending forever.
        self.pending_workset_documents: list[uuid.UUID] = []
        # `auto` workset items no embedding job is coming for, given their
        # automatic pass after the same commit (see `AddOutcome`).
        self.pending_workset_auto_items: list[uuid.UUID] = []
        # Worksets sealed this burst. Clustering is queued after the commit for
        # the same reason the documents are: a job that dequeues before the
        # seal is visible would cluster a corpus it thinks is still open.
        self.pending_workset_clusters: list[uuid.UUID] = []
        # The sandbox session of a run of a sandbox copy (#8255), read at the
        # burst's first node (past the owner gate): its nodes and its agents'
        # tool calls are intercepted. ``_sandbox_base`` is the run's report
        # before this burst.
        self._sandbox = None
        self._sandbox_read = False
        self._sandbox_base: dict | None = None

    # ── public entrypoint ────────────────────────────────────────────────

    async def run_burst(self) -> BurstOutcome:
        run = self.run
        if run.status == FLOW_RUN_STATUS_CANCELLED:
            return BurstOutcome(kind="cancelled")

        # A run queued before its owner's account was deactivated ends here,
        # before any trace, step or charge. One already under way goes
        # on, like an assignment run in progress.
        if run.status == FLOW_RUN_STATUS_PENDING and not self.state.get("started"):
            from app.services.assignment_pause import (
                is_flow_paused_by_deactivated_owner,
            )

            if await is_flow_paused_by_deactivated_owner(self.db, run.flow_id):
                return await self._skip_paused_by_owner()

        # ── Observability: one root trace per burst ─────────────────
        # Every node execution opens a child span with its RENDERED inputs,
        # and agent steps' own agent-chat traces nest underneath through the
        # contextvar — mirroring the assignment engine's workflow trace.
        # Opened BEFORE any run mutation: a tracing failure (missing config
        # tables, transient DB error) aborts the transaction, so recovery is
        # a clean rollback with nothing of ours to lose.
        from app.services.langfuse_tracing import (
            end_trace,
            reset_current_trace_ctx,
            set_current_trace_ctx,
            trace_llm_call,
        )

        # The flow's display name — used by the trace label and the Notify
        # node's source line. A plain read, safe outside the savepoint.
        flow_name = (
            await self.db.execute(
                select(Flow.name).where(Flow.id == run.flow_id)
            )
        ).scalar_one_or_none() or str(run.flow_id)
        self._flow_name = flow_name

        try:
            # SAVEPOINT: a tracing failure (missing config tables, transient
            # DB error) must roll back only itself, never abort the burst's
            # transaction or expire the caller's loaded instances.
            async with self.db.begin_nested():
                self._trace_ctx = await trace_llm_call(
                    self.db,
                    name=f"flow-run:{flow_name}",
                    user_id=str(self.user.id),
                    source="flow",
                    input_data={
                        "flow_id": str(run.flow_id),
                        "flow_run_id": str(run.id),
                        "trigger_type": run.trigger_type,
                        "trigger_payload": run.trigger_payload,
                    },
                    metadata={
                        "flow_id": str(run.flow_id),
                        "flow_run_id": str(run.id),
                        "flow_version": run.flow_version_number,
                    },
                    run_id=str(run.id),
                    trigger_type=run.trigger_type,
                )
        except Exception:  # tracing must never block a run
            logger.debug(
                "Flow run %s: tracing unavailable, running untraced", run.id
            )
            self._trace_ctx = None
        _ctx_token = set_current_trace_ctx(self._trace_ctx)

        # Memory written under this burst — by a node, or by an agent node's
        # tool call, which inherits the contextvar — is attributed to this
        # flow and carries its event chain depth. That is what keeps a
        # memory-triggered flow that writes memory from re-triggering itself
        # and what makes an A → B → A cycle converge on EVENT_CHAIN_MAX_DEPTH
        # instead of running forever.
        from app.services.memory_flow_events import (
            reset_memory_chain_context,
            set_memory_chain_context,
        )

        _memory_token = set_memory_chain_context(
            run.flow_id,
            int((run.trigger_context or {}).get("chain_depth") or 0),
        )

        if run.status == FLOW_RUN_STATUS_PENDING:
            run.started_at = datetime.now(UTC)
        run.status = FLOW_RUN_STATUS_RUNNING

        await self._prepare_renderer()

        outcome_for_trace: BurstOutcome | None = None
        try:
            if not self.state.get("started"):
                refusal = await self._start()
                if refusal is not None:
                    outcome_for_trace = await self._fail(refusal)
                    return outcome_for_trace

            # A worker crash mid-node leaves its journal row `running` with
            # no park — `in_flight` names it. Fail the orphan honestly and
            # re-queue its node: re-executing one node is strictly better
            # than the pre-live-journal behaviour of replaying the burst.
            in_flight = self.state.get("in_flight")
            if in_flight and not self.state.get("waiting"):
                orphan = await self._load_parked_step(in_flight)
                if orphan is not None and orphan.status == "running":
                    orphan.status = "failed"
                    orphan.error = (
                        "Interrupted by a worker restart — the node was"
                        " queued to run again."
                    )
                    orphan.completed_at = datetime.now(UTC)
                node_id = in_flight.get("node")
                if node_id in self.nodes and node_id not in self.state["queue"]:
                    self.state["queue"] = [node_id, *self.state["queue"]]
                self.state["in_flight"] = None
                self._persist_state()

            # Resume a parked node first, if any.
            waiting = self.state.get("waiting")
            if waiting:
                outcome = await self._resume_waiting(waiting)
                if outcome is not None:
                    outcome_for_trace = outcome
                    return outcome

            while True:
                # Cooperative cancel: the API flips run.status directly.
                await self.db.refresh(run, ["status"])
                if run.status == FLOW_RUN_STATUS_CANCELLED:
                    run.completed_at = datetime.now(UTC)
                    self._persist_state()
                    await self.db.commit()
                    await publish_flow_run_update(run)
                    outcome_for_trace = BurstOutcome(kind="cancelled")
                    return outcome_for_trace

                run.status = FLOW_RUN_STATUS_RUNNING

                node_id = self._pop_next()
                if node_id is None:
                    outcome_for_trace = await self._complete()
                    return outcome_for_trace
                if self.state["seq"] >= MAX_STEPS_PER_RUN:
                    outcome_for_trace = await self._fail(
                        f"Run exceeded {MAX_STEPS_PER_RUN} steps — aborted as runaway."
                    )
                    return outcome_for_trace
                outcome = await self._execute_node(node_id)
                if outcome is not None:
                    outcome_for_trace = outcome
                    return outcome
        except Exception as exc:
            logger.exception("Flow run %s crashed", run.id)
            outcome_for_trace = await self._fail(format_exception_message(exc))
            return outcome_for_trace
        finally:
            try:
                if outcome_for_trace is not None and outcome_for_trace.kind == "failed":
                    end_trace(self._trace_ctx, error=outcome_for_trace.error or "failed")
                else:
                    end_trace(
                        self._trace_ctx,
                        output={
                            "outcome": outcome_for_trace.kind
                            if outcome_for_trace
                            else "unknown",
                            "result_summary": run.result_summary,
                        },
                    )
            except Exception:  # pragma: no cover
                logger.exception("Flow run %s: could not close the trace", run.id)
            reset_current_trace_ctx(_ctx_token)
            reset_memory_chain_context(_memory_token)
            self._trace_ctx = None

    # ── lifecycle helpers ────────────────────────────────────────────────

    async def _prepare_renderer(self) -> None:
        from app.api.v1.helpers import get_user_group_ids
        from app.services.variable_resolver import load_variables

        group_ids = await get_user_group_ids(self.db, self.user.id)
        variables = await load_variables(self.db, self.user.id, group_ids)
        outputs = await self._journal_outputs()
        # Constants are pre-evaluated, not executed: every `util.primitive`
        # publishes its value up-front so {{p.value}} resolves anywhere,
        # wired or not — the UE literal-pin semantics.
        primitives = [
            node
            for node in (self.graph.get("nodes") or [])
            if isinstance(node, dict) and node.get("kind") == "util.primitive"
        ]
        # Object constants conform to a declared type — fetch the referenced
        # signatures once so the coercion has its schemas.
        type_ids = {
            str((n.get("config") or {}).get("object_type_id") or "").strip()
            for n in primitives
            if ((n.get("config") or {}).get("value_type") or "text") == "object"
        }
        type_ids.discard("")
        object_types: dict[str, list] = {}
        if type_ids:
            from app.models.flow_event import FlowEventType

            rows = (
                (
                    await self.db.execute(
                        select(FlowEventType).where(
                            FlowEventType.id.in_(
                                [u for u in map(_coerce_uuid, type_ids) if u]
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
            object_types = {str(r.id): (r.fields or []) for r in rows}
        for node in primitives:
            node_id = str(node.get("id") or "")
            if node_id and node_id not in outputs:
                outputs[node_id] = {
                    "value": primitive_value(
                        node.get("config") or {}, variables, object_types
                    )
                }
        trigger_untrusted = self.run.trigger_type in UNTRUSTED_TRIGGER_TYPES or bool(
            (self.run.trigger_context or {}).get("untrusted")
        )
        self._renderer = _Renderer(
            node_outputs=outputs,
            untrusted_nodes=set(self.state.get("untrusted") or []),
            trigger_payload=self.run.trigger_payload or {},
            trigger_untrusted=trigger_untrusted,
            variables=variables,
            trigger_source=f"{self.run.trigger_type} trigger",
        )

    async def _journal_outputs(self) -> dict[str, dict]:
        """node_id → latest completed step output (loop passes overwrite)."""
        rows = (
            (
                await self.db.execute(
                    select(FlowRunStep)
                    .where(
                        FlowRunStep.flow_run_id == self.run.id,
                        FlowRunStep.status == "completed",
                    )
                    .order_by(FlowRunStep.seq)
                )
            )
            .scalars()
            .all()
        )
        return {row.node_id: row.output or {} for row in rows}

    async def _start(self) -> str | None:
        """Record the trigger step; the reason the run cannot start, if any."""
        trigger_node_id = (self.run.trigger_context or {}).get("trigger_node_id")
        node = self.nodes.get(trigger_node_id) if trigger_node_id else None
        if node is None:
            wanted = (
                "trigger.manual"
                if self.run.trigger_type == "manual"
                else _TRIGGER_PREFIX + self.run.trigger_type
            )
            candidates = find_trigger_nodes(self.graph, wanted) or find_trigger_nodes(
                self.graph
            )
            node = candidates[0] if candidates else None
        if node is None:
            return "The flow has no trigger node to start from."
        refusal = self._import_review_refusal(node)
        if refusal is not None:
            # A trigger never goes through _dispatch: an imported schedule
            # awaiting review would otherwise fire as converted.
            await self._record_step(
                node, status="failed", outputs={}, tainted=False, error=refusal
            )
            return refusal

        # The trigger step's outputs must be the kind's DECLARED variables —
        # the validator refuses any other ref at save time. Webhook and
        # integration events already arrive shaped ({payload, headers} /
        # {payload, event_type}); manual parameters and the schedule stamp
        # are wrapped under `payload`, so refs read `{{t1.payload.<key>}}`.
        payload = dict(self.run.trigger_payload or {})
        kind_def = get_node_kind(node.get("kind") or "")
        declared = {v.name for v in kind_def.output_variables} if kind_def else set()
        outputs = (
            payload
            if payload and set(payload.keys()) <= declared
            else {"payload": payload}
        )
        tainted = self.run.trigger_type in UNTRUSTED_TRIGGER_TYPES or bool(
            (self.run.trigger_context or {}).get("untrusted")
        )
        await self._record_step(
            node, status="completed", outputs=outputs, tainted=tainted
        )
        self._enqueue_targets(node["id"], ["out"], None)
        self.state["started"] = True
        self._persist_state()
        await self.db.commit()
        await publish_flow_run_update(
            self.run, node_id=node["id"], node_status="completed"
        )
        return None

    def _import_review_refusal(self, node: dict) -> str | None:
        """Why *node* may not run yet: its importer's review notes, or those
        of a capability node wired into it (a capability never executes, so
        the node it extends carries the refusal)."""
        pending = pending_import_review(node)
        if not pending:
            for edge in _edges_into(self.graph, node.get("id") or ""):
                source = self.nodes.get(edge.get("source") or "") or {}
                if str(source.get("kind") or "").startswith("capability."):
                    pending = pending_import_review(source)
                    if pending:
                        break
        if not pending:
            return None
        return (
            "This imported node has not been reviewed yet: "
            f"{pending[0]} Clear its review notes once it is fixed."
        )

    async def _complete(self) -> BurstOutcome:
        run = self.run
        run.status = FLOW_RUN_STATUS_COMPLETED
        run.completed_at = datetime.now(UTC)
        run.result_summary = await self._final_summary()
        self._persist_state()
        await self._record_sandbox()
        await self._settle_trigger_work_item(failed=False)
        await self._notify_parent()
        # Committed here (not by the adapter) so the SSE frame below never
        # points at an uncommitted terminal status. Safe: the adapter's
        # terminal-status guard maps a re-claim of an already-terminal run
        # to the matching engine transition without re-running it.
        await self.db.commit()
        await publish_flow_run_update(run)
        return BurstOutcome(kind="completed")

    async def _skip_paused_by_owner(self) -> BurstOutcome:
        """End a queued run of a flow whose owner is deactivated."""
        from app.services.assignment_pause import FLOW_OWNER_DEACTIVATED_RUN_ERROR

        run = self.run
        run.status = FLOW_RUN_STATUS_SKIPPED
        run.completed_at = datetime.now(UTC)
        run.result_summary = FLOW_OWNER_DEACTIVATED_RUN_ERROR
        await self._settle_trigger_work_item(failed=False, skipped=True)
        await self._notify_parent()
        await self.db.commit()
        await publish_flow_run_update(run)
        logger.info(
            "Flow run %s of flow %s skipped: the flow's owner is deactivated",
            run.id,
            run.flow_id,
        )
        return BurstOutcome(kind="skipped")

    async def _fail(self, error: str) -> BurstOutcome:
        run = self.run
        try:
            run.status = FLOW_RUN_STATUS_FAILED
            run.completed_at = datetime.now(UTC)
            run.error = error[:4000]
            self._persist_state()
            await self._record_sandbox()
            await self._settle_trigger_work_item(failed=True)
            await self._notify_parent()
            await self.db.commit()
        except Exception:
            # The very failure being recorded may have aborted the burst's
            # transaction (e.g. an IntegrityError inside a node) — start a
            # fresh one and re-apply the terminal verdict, or the run would
            # strand on `running` until the deadline sweep.
            logger.exception(
                "Flow run %s: transaction was aborted, re-applying the failure",
                run.id,
            )
            await self.db.rollback()
            # The rollback expired the instance — reload it explicitly, or
            # the attribute reads below (and the publisher's) would lazy-load
            # synchronously (MissingGreenlet) in this async context.
            await self.db.refresh(run)
            run.status = FLOW_RUN_STATUS_FAILED
            run.completed_at = datetime.now(UTC)
            run.error = error[:4000]
            run.state = dict(self.state)
            # The rollback also discarded any uncommitted step finalization
            # (e.g. _route_error closing the failing node's row), reverting
            # it to its committed `running` state — and the in-memory
            # `state.in_flight` was already cleared, so the next-burst
            # orphan sweep can't repair it on a terminal run. Close every
            # still-running row here: the run is terminal, nothing may stay
            # `running` in the journal.
            await close_running_steps(self.db, run.id, error=error)
            await self.db.commit()
        await publish_flow_run_update(run)
        return BurstOutcome(kind="failed", error=error)

    async def _settle_trigger_work_item(
        self, *, failed: bool, skipped: bool = False
    ) -> None:
        """Close the loop on a `trigger.work_item` run.

        The poller claimed the item before starting this run; the run's end
        is the item's outcome — the same contract as the assignment inbox:
        completed run → item `done` with the summary, failed run → the claim
        is released (bounded retries, then `blocked`), skipped run → the item
        goes back to the queue, no attempt counted. Best-effort: settling
        must never mask the run's own outcome.
        """
        if self.run.trigger_type != "work_item":
            return
        item_id = (self.run.trigger_context or {}).get("work_item_id")
        if not item_id:
            return
        from app.services.company_managed_writes import flow_writer

        # The run's snapshot of the flow's tier, the one its trigger claimed with.
        writer = flow_writer(is_company_managed=self.run.is_company_managed)
        try:
            if skipped:
                from app.services.work_item import release_work_item_claim

                await release_work_item_claim(
                    work_item_id=str(item_id),
                    user_id=str(self.user.id),
                    writer=writer,
                )
            elif failed:
                from app.services.work_item import release_work_item_claim

                await release_work_item_claim(
                    work_item_id=str(item_id),
                    user_id=str(self.user.id),
                    failed=True,
                    error=(self.run.error or "Flow run failed.")[:500],
                    writer=writer,
                )
            else:
                from app.services.work_item import complete_work_item

                await complete_work_item(
                    str(item_id),
                    user_id=str(self.user.id),
                    result=(self.run.result_summary or "Completed by flow run.")[
                        :4000
                    ],
                    status="done",
                    writer=writer,
                )
        except Exception:
            logger.exception(
                "Flow run %s could not settle work item %s", self.run.id, item_id
            )

    async def _final_summary(self) -> str:
        return (await final_step_output(self.db, self.run.id))[:2000]

    async def _notify_parent(self) -> None:
        """Wake the parent run's execution when a `flow.call` child ends."""
        if self.run.parent_run_id is None:
            return
        from app.models.orchestration import OrchestrationExecution
        from app.services import orchestration_engine as engine

        parent_execution = (
            await self.db.execute(
                select(OrchestrationExecution).where(
                    OrchestrationExecution.dedup_key
                    == f"flow_run:{self.run.parent_run_id}"
                )
            )
        ).scalar_one_or_none()
        if parent_execution is None:
            return
        await engine.emit_outbox_event(
            self.db,
            execution_id=parent_execution.id,
            event_type="resume",
            dedup_key=f"flow_child:{self.run.id}",
        )

    def _persist_state(self) -> None:
        # JSON column: reassign a fresh dict so SQLAlchemy sees the change.
        self.run.state = dict(self.state)

    # ── queue management ─────────────────────────────────────────────────

    def _pop_next(self) -> str | None:
        queue: list[str] = self.state["queue"]
        # A `wait_all` / `concat` merge re-queues itself while producers may
        # still arrive — and an unfinished loop counts as a producer. But the
        # drain rule below only advances a loop once the queue is EMPTY, so a
        # merge fed by (or sitting inside) a loop body keeps the queue
        # non-empty forever: the loop never advances, the merge never fires,
        # and the run burns MAX_STEPS_PER_RUN doing nothing. When the queue
        # holds nothing but waiting merges, the loop IS the remaining work —
        # advance it first.
        if queue and all(
            (self.nodes.get(q, {}).get("kind") or "") == "util.merge" for q in queue
        ):
            loop_id = self._innermost_unfinished_loop()
            if loop_id is not None:
                return loop_id
        while queue:
            node_id = queue.pop(0)
            if node_id in self.nodes:
                return node_id
        # Queue drained: advance the innermost unfinished loop, if any; this
        # re-executes the loop node → next pass or `done`.
        return self._innermost_unfinished_loop()

    def _enclosing_passes(self) -> dict[str, list[int]]:
        """The pass of every unfinished loop, keyed by loop id.

        Stored on a loop when it is armed: the loops unfinished at that
        moment are the ones it runs inside. ``armed`` is part of the key so
        an enclosing loop that was itself re-armed never looks unchanged.
        """
        return {
            loop_id: [int(ls.get("armed") or 0), int(ls.get("index") or 0)]
            for loop_id, ls in self.state["loops"].items()
            if not ls.get("done")
        }

    def _enclosing_pass_moved(self, loop_state: dict) -> bool:
        """True when a loop enclosing this (finished) one is on another pass."""
        loops = self.state["loops"]
        for loop_id, (armed, index) in (loop_state.get("enclosing") or {}).items():
            current = loops.get(loop_id)
            if current is None or [
                int(current.get("armed") or 0),
                int(current.get("index") or 0),
            ] != [armed, index]:
                return True
        return False

    def _innermost_unfinished_loop(self) -> str | None:
        """The unfinished loop armed last: the innermost one.

        A loop nested in another's body is armed during the outer pass, so
        it is always armed after its outer loop (a re-entry re-arms it). It
        must run all its passes before the outer loop advances, otherwise
        the outer loop steps ahead while the inner body is still due.
        ``armed`` is the step sequence at arming time: the JSON cursor does
        not promise key order. A legacy cursor without it falls back to
        dict order, latest first.
        """
        unfinished = [
            (int(loop_state.get("armed") or 0), position, loop_id)
            for position, (loop_id, loop_state) in enumerate(
                self.state["loops"].items()
            )
            if not loop_state.get("done")
        ]
        return max(unfinished)[2] if unfinished else None

    def _enqueue_targets(
        self,
        node_id: str,
        handles: list[str],
        switch_case: str | None,
        handover_types: frozenset[str] | None = None,
    ) -> None:
        queue: list[str] = self.state["queue"]
        for handle in handles:
            for edge in _edges_from(self.graph, node_id, handle):
                if (
                    handle == "case"
                    and switch_case is not None
                    and (edge.get("label") or "") != switch_case
                ):
                    continue
                if handle == "handover" and handover_types is not None:
                    label = (edge.get("label") or "").strip().lower()
                    if label and label not in handover_types:
                        continue
                target = edge.get("target")
                target_node = self.nodes.get(target)
                if target_node is None:
                    continue
                if (target_node.get("kind") or "") in _ANNOTATION_KINDS:
                    continue
                target_kind = target_node.get("kind") or ""
                if target_kind == "util.merge":
                    self._record_merge_arrival(node_id, target)
                if target_kind in ("util.loop", "util.while"):
                    target_state = self.state["loops"].get(target)
                    if target_state is not None and not target_state.get("done"):
                        # Back-edge into an armed loop: the drain rule
                        # advances it; enqueueing again would double-step
                        # the pass.
                        continue
                    if target_state is not None:
                        if not self._enclosing_pass_moved(target_state):
                            # Same pass of every enclosing loop: a merge-
                            # joined back-edge, a wire drawn from Done back
                            # in, a late second branch. The loop already ran
                            # for this pass.
                            continue
                        # An inner loop reached on its outer loop's next
                        # pass starts afresh: items re-read, passes from 0.
                        del self.state["loops"][target]
                if target not in queue:
                    queue.append(target)

    def _record_merge_arrival(self, source_id: str, merge_id: str) -> None:
        merges = self.state["merges"]
        entry = merges.setdefault(merge_id, {"arrivals": [], "fired": False})
        source_node = self.nodes.get(source_id) or {}
        value = ""
        # The arrival's value is the source's primary output at arrival time.
        outputs = self._renderer._outputs.get(source_id) if self._renderer else None
        if outputs is not None:
            value = _primary_output(source_node.get("kind") or "", outputs)
        entry["arrivals"].append([source_id, value])

    # ── node execution ───────────────────────────────────────────────────

    async def _execute_node(self, node_id: str) -> BurstOutcome | None:
        """Execute one node.  Returns a BurstOutcome to stop, None to go on."""
        node = self.nodes[node_id]
        kind = node.get("kind") or ""

        # A second trigger node reached via edges is a no-op passthrough.
        if kind.startswith(_TRIGGER_PREFIX):
            refusal = self._import_review_refusal(node)
            if refusal is not None:
                step = await self._begin_step(node)
                return await self._route_error(node, refusal, step, fatal=True)
            self._enqueue_targets(node_id, ["out"], None)
            return None

        # ── Debugger: breakpoints + step mode ────────────────────
        # Pause BEFORE the node executes: park with kind "breakpoint" and
        # wait for the debug endpoint to release (continue or step). The
        # release re-enters exactly here with the one-shot skip flag set.
        skip_bp = self.state.pop("skip_breakpoint_once", None)
        if skip_bp != node_id and (
            (node.get("config") or {}).get("breakpoint")
            or self.state.get("step_mode")
        ):
            self.state["queue"] = [node_id, *self.state["queue"]]
            self.state["waiting"] = {
                "kind": "breakpoint",
                "node": node_id,
                "released": False,
            }
            self.run.status = FLOW_RUN_STATUS_AWAITING
            self._persist_state()
            await self.db.commit()
            await publish_flow_run_update(self.run, node_id=node_id)
            wake = datetime.now(UTC) + timedelta(seconds=BREAKPOINT_POLL_SECONDS)
            return BurstOutcome(kind="park", wake_at=wake)

        # What the node actually computed with: its config after template
        # resolution — recorded on the step (troubleshooting) and on the
        # node's Langfuse span. Rendering here is read-only; executors keep
        # doing their own rendering with their own semantics.
        step_input: dict | None = None
        try:
            raw_config = {
                k: v
                for k, v in (node.get("config") or {}).items()
                if k != "shape"
            }
            if self._renderer is not None and raw_config:
                rendered, _tainted = self._renderer.render_value(raw_config)
                step_input = _truncate_for_journal(rendered)
            elif raw_config:
                step_input = _truncate_for_journal(raw_config)
        except Exception:  # pragma: no cover — recording must never fail a node
            step_input = None
        self._step_input = step_input

        from app.services.langfuse_tracing import end_span, start_flow_node_span

        node_span = start_flow_node_span(
            self._trace_ctx,
            node_id=node_id,
            node_kind=kind,
            label=node.get("label"),
            input_payload=step_input,
        )

        # Two-phase journaling: the step opens `running` BEFORE the node
        # executes and is committed right away, so the runs panel (and the
        # canvas overlay) show the node as in-flight while it works.
        step = await self._begin_step(node)

        try:
            result = await self._dispatch_or_intercept(node, kind, step_input)
        except WorkspaceScopeError as exc:
            logger.warning(
                "Flow run %s node %s: workspace scope refused (%s)",
                self.run.id,
                node_id,
                exc.reason,
            )
            result = _NodeResult(error=format_exception_message(exc))
        except Exception as exc:
            result = _NodeResult(error=format_exception_message(exc))

        try:
            if result.error is not None:
                end_span(node_span, error=result.error)
            elif result.park is not None:
                end_span(node_span, output={"parked": result.park.get("kind")})
            else:
                end_span(node_span, output=_truncate_for_journal(result.outputs))
        except Exception:  # pragma: no cover
            logger.exception("Flow node %s: could not close its span", node_id)

        if result.park is not None:
            # The step opened by _begin_step stays `running` — the park's
            # resume finishes it in place (one journal row per execution).
            self.state["waiting"] = {
                "node": node_id,
                "step_id": str(step.id),
                **result.park,
            }
            self.state["in_flight"] = None
            self.run.status = FLOW_RUN_STATUS_AWAITING
            self._persist_state()
            await self.db.commit()
            await publish_flow_run_update(
                self.run, node_id=node_id, node_status="running"
            )
            wake_at = result.park.get("wake_at")
            return BurstOutcome(
                kind="park",
                wake_at=datetime.fromisoformat(wake_at) if wake_at else None,
            )

        if result.error is not None:
            return await self._route_error(
                node, result.error, step, fatal=result.fatal
            )

        await self._finish_node(node, result, step)
        if result.halt:
            # Stop node: the run ends here, by design — whatever else was
            # queued (parallel branches, later loop passes) is dropped.
            self.state["queue"] = []
            self._persist_state()
            return await self._complete()
        return None

    async def _finish_node(
        self, node: dict, result: _NodeResult, step: FlowRunStep
    ) -> None:
        tainted = bool(result.outputs.pop("_tainted", False))
        self._finalize_step(
            step,
            status="completed",
            outputs=result.outputs,
            tainted=tainted,
            warning=result.warning,
        )
        self._enqueue_targets(
            node["id"], result.next_handles, result.switch_case, result.handover_types
        )
        self._persist_state()
        await self.db.commit()
        # AFTER the commit: the documents an Add-to-workset node wrote are now
        # durable, so a worker that picks the job up will find its row. Queuing
        # before this point is the race the tool path was fixed for.
        await self._flush_workset_queue()
        await publish_flow_run_update(
            self.run, node_id=node["id"], node_status="completed"
        )

    async def _flush_workset_queue(self) -> None:
        """Hand this burst's freshly written workset documents to the embedder.

        Fire-and-forget by design: an unreachable queue leaves the item
        un-embedded, which the workset's own progress read reports as
        outstanding work. Failing the node over it would throw away an
        ingestion that already succeeded.
        """
        if self.pending_workset_clusters:
            sealed = list(self.pending_workset_clusters)
            self.pending_workset_clusters.clear()
            from app.services.workset_clustering import enqueue_clustering

            for workset_id in sealed:
                await enqueue_clustering(workset_id)

        if not self.pending_workset_documents and not self.pending_workset_auto_items:
            return
        pending = list(self.pending_workset_documents)
        self.pending_workset_documents.clear()
        auto_items = list(self.pending_workset_auto_items)
        self.pending_workset_auto_items.clear()
        from app.services.workset import AddOutcome, queue_pending

        await queue_pending(
            AddOutcome(pending_documents=pending, auto_pass_items=auto_items)
        )

    async def _route_error(
        self, node: dict, error: str, step: FlowRunStep, *, fatal: bool = False
    ) -> BurstOutcome | None:
        """Follow the node's `error` port if wired, else fail the run."""
        node_id = node["id"]
        error_edges = [] if fatal else _edges_from(self.graph, node_id, "error")
        self._finalize_step(
            step,
            status="failed",
            outputs={"error": error[:2000]},
            tainted=False,
            error=error,
        )
        if not error_edges:
            # _fail commits — the step's terminal state rides that commit.
            return await self._fail(
                f"Step `{node.get('label') or node_id}` failed: {error}"
            )
        self._enqueue_targets(node_id, ["error"], None)
        self._persist_state()
        await self.db.commit()
        await publish_flow_run_update(self.run, node_id=node_id, node_status="failed")
        return None

    async def _begin_step(self, node: dict) -> FlowRunStep:
        """Open the node's journal row BEFORE it executes (two-phase).

        Committed immediately so the trace is live while the node works;
        ``state.in_flight`` names the row so a worker crash mid-node is
        detected on the next burst and the node re-queued instead of lost.
        """
        # The rendered config computed by _execute_node for THIS node — read
        # once so a later step never inherits it.
        step_input = self._step_input
        self._step_input = None
        seq = self.state["seq"]
        self.state["seq"] = seq + 1
        node_id = node["id"]
        loop_state = self.state["loops"].get(node_id)
        step = FlowRunStep(
            flow_run_id=self.run.id,
            seq=seq,
            node_id=node_id,
            node_kind=node.get("kind") or "",
            iteration=int(loop_state["index"]) if loop_state else 0,
            status="running",
            input=step_input,
        )
        self.db.add(step)
        await self.db.flush()
        self.state["in_flight"] = {"node": node_id, "step_id": str(step.id)}
        self._persist_state()
        await self.db.commit()
        await publish_flow_run_update(self.run, node_id=node_id, node_status="running")
        return step

    def _finalize_step(
        self,
        step: FlowRunStep,
        *,
        status: str,
        outputs: dict,
        tainted: bool,
        error: str | None = None,
        warning: str | None = None,
    ) -> None:
        """Close the row _begin_step opened; the caller persists + commits."""
        # A loop node advances its own index inside _dispatch, so the pass
        # number is only correct AFTER execution — re-stamp it here (the
        # begin-phase row briefly shows the previous pass, corrected now).
        loop_state = self.state["loops"].get(step.node_id)
        if loop_state:
            step.iteration = int(loop_state["index"])
        step.status = status
        step.output = outputs or None
        step.untrusted = tainted
        step.error = error[:4000] if error else None
        step.warning = warning[:4000] if warning else None
        step.completed_at = datetime.now(UTC)
        self.state["in_flight"] = None
        if status == "completed" and self._renderer is not None:
            self._renderer._outputs[step.node_id] = outputs or {}
            if tainted:
                self._renderer._untrusted.add(step.node_id)
                untrusted = set(self.state.get("untrusted") or [])
                untrusted.add(step.node_id)
                self.state["untrusted"] = sorted(untrusted)

    async def _record_step(
        self,
        node: dict,
        *,
        status: str,
        outputs: dict,
        tainted: bool,
        error: str | None = None,
    ) -> FlowRunStep:
        # One-shot insert, now only for the trigger step (executed nodes go
        # through _begin_step/_finalize_step). Consumes _step_input so a
        # later step never inherits it.
        step_input = getattr(self, "_step_input", None)
        self._step_input = None
        seq = self.state["seq"]
        self.state["seq"] = seq + 1
        node_id = node["id"]
        loop_state = self.state["loops"].get(node_id)
        step = FlowRunStep(
            flow_run_id=self.run.id,
            seq=seq,
            node_id=node_id,
            node_kind=node.get("kind") or "",
            iteration=int(loop_state["index"]) if loop_state else 0,
            status=status,
            input=step_input,
            output=outputs or None,
            untrusted=tainted,
            error=error[:4000] if error else None,
            completed_at=datetime.now(UTC) if status != "running" else None,
        )
        self.db.add(step)
        await self.db.flush()
        if status == "completed" and self._renderer is not None:
            self._renderer._outputs[node_id] = outputs or {}
            if tainted:
                self._renderer._untrusted.add(node_id)
                untrusted = set(self.state.get("untrusted") or [])
                untrusted.add(node_id)
                self.state["untrusted"] = sorted(untrusted)
        return step

    # ── dispatch table ───────────────────────────────────────────────────

    async def _dispatch_or_intercept(self, node: dict, kind: str, step_input: dict | None) -> _NodeResult:
        """Run the node, unless the run's sandbox session answers it (#8255)."""
        from app.services.health.sandbox.flows import STATE_KEY, flow_burst_session, intercept_flow_node
        from app.services.health.sandbox.session import sandbox_session

        if not self._sandbox_read:
            self._sandbox_read = True
            self._sandbox = await flow_burst_session(self.db, self.run, self.user)
            self._sandbox_base = self.state.get(STATE_KEY)
        if self._sandbox is None:
            return await self._dispatch(node, kind)
        with sandbox_session(self._sandbox):
            answered = await intercept_flow_node(
                self.db,
                self._sandbox,
                node=node,
                kind=kind,
                step_input=step_input,
                llm_wired=self._wired_llm(node["id"]) is not None,
            )
            if answered is None:
                result = await self._dispatch(node, kind)
            else:
                outputs, outcome = answered
                result = _NodeResult(outputs=outputs, warning=f"Sandbox: {outcome}, not executed.")
        await self._record_sandbox()
        return result

    def _sandbox_child_context(self) -> dict:
        """What a run this burst calls carries when the burst is a sandbox's:
        the mark that keeps it out of production listings and recordings."""
        if self._sandbox is None:
            return {}
        from app.services.health.sandbox.flows import SANDBOX_RUN_SOURCE

        return {"source": SANDBOX_RUN_SOURCE}

    async def _record_sandbox(self) -> None:
        """Carry the burst's sandbox counts onto the run and its report row."""
        if self._sandbox is None:
            return
        from app.services.health.sandbox.flows import STATE_KEY, burst_report, record_flow_report

        self.state[STATE_KEY] = burst_report(self._sandbox_base, self._sandbox)
        self._persist_state()
        await record_flow_report(self.db, self.run, self.state[STATE_KEY])

    async def _dispatch(self, node: dict, kind: str) -> _NodeResult:
        config = node.get("config") or {}
        refusal = self._import_review_refusal(node)
        if refusal is not None:
            return _NodeResult(error=refusal, fatal=True)
        if kind == "agent":
            return await self._exec_agent(node, config)
        if kind == "condition.if":
            return await self._exec_condition_if(node, config)
        if kind == "condition.switch":
            return await self._exec_condition_switch(node, config)
        if kind == "condition.classify":
            return await self._exec_condition_classify(config)
        if kind == "action.send_message":
            return await self._exec_send_message(config)
        if kind == "action.create_work_item":
            return await self._exec_create_work_item(config)
        if kind == "action.emit_event":
            return await self._exec_emit_event(config)
        if kind == "action.notify":
            return await self._exec_notify(config)
        if kind == "action.ask_user":
            return await self._exec_ask_user(node, config)
        if kind == "action.trigger_assignment":
            return await self._exec_trigger_assignment(node, config)
        if kind == "action.workset_add":
            return await self._exec_workset_add(node, config)
        if kind == "action.workset_search":
            return await self._exec_workset_search(node, config)
        if kind == "action.workset_digest":
            return await self._exec_workset_digest(node, config)
        if kind == "action.workset_seal":
            return await self._exec_workset_seal(node, config)
        if kind == "action.call_tool":
            return await self._exec_call_tool(node, config)
        if kind == "action.script":
            return await self._exec_script(config)
        if kind == "action.runner_command":
            return await self._exec_runner_command(config)
        if kind == "action.update_work_item":
            return await self._exec_update_work_item(config)
        if kind == "action.dispatch_to_fleet":
            return await self._exec_dispatch_to_fleet(config)
        if kind == "action.workspace_add_source":
            return await self._exec_workspace_add_source(config)
        if kind == "action.create_report":
            return await self._exec_create_report(config)
        if kind in ("action.export_report", "action.export_presentation"):
            return await self._exec_export_document(kind, config)
        if kind == "action.render_video":
            return await self._exec_render_video(config)
        if kind == "action.deep_investigation":
            return await self._exec_deep_investigation(config)
        if kind == "action.http_request":
            return await self._exec_http_request(config)
        if kind == "integration":
            return await self._exec_integration(config)
        if kind == "flow.call":
            return await self._exec_flow_call(node, config)
        if kind == "util.delay":
            return self._exec_delay(node, config)
        if kind == "util.wait_event":
            return await self._exec_wait_event(config)
        if kind == "util.loop":
            return self._exec_loop(node, config)
        if kind == "util.while":
            return self._exec_while(node, config)
        if kind == "util.expression":
            return self._exec_expression(config)
        if kind == "util.code":
            return await self._exec_code(config)
        if kind == "util.set_variable":
            return self._exec_set_variable(config)
        if kind == "util.get_variable":
            return await self._exec_get_variable(config)
        if kind == "util.do_once":
            return self._exec_do_once(node)
        if kind == "util.flip_flop":
            return self._exec_flip_flop(node)
        if kind == "util.gate":
            return self._exec_gate(config)
        if kind == "util.reroute":
            return self._exec_reroute(node)
        if kind == "util.merge":
            return self._exec_merge(node, config)
        if kind == "util.break":
            return self._exec_break(config)
        if kind == "util.cast":
            return self._exec_cast(config)
        if kind == "util.select":
            return self._exec_select(config)
        if kind == "util.sequence":
            return _NodeResult(next_handles=["then_1", "then_2", "then_3"])
        if kind == "util.stop":
            return self._exec_stop(config)
        if kind.startswith("capability."):
            # Defensive: capabilities are wiring, not steps — nothing should
            # ever enqueue one (their only edges leave their `provides` port).
            return _NodeResult(
                error="Capability nodes attach to agents; they never execute."
            )
        return _NodeResult(
            error=f"Node kind `{kind}` cannot be executed."
            + (" Re-map this imported node first." if kind == "unresolved" else "")
        )

    # ── executors ────────────────────────────────────────────────────────

    def _render(self, template: Any, *, wrap: bool = False) -> _RenderResult:
        if self._renderer is None:  # pragma: no cover — _prepare always ran
            return _RenderResult(text=_to_text(template))
        return self._renderer.render(_to_text(template), wrap=wrap)

    def _wired_capabilities(self, node_id: str) -> dict[str, Any]:
        """Capability nodes wired into this agent's bottom-edge sockets (``wired_capabilities``)."""
        return wired_capabilities(self.graph, node_id, self.nodes)

    async def _exec_agent(self, node: dict, config: dict) -> _NodeResult:
        from app.models.agent import Agent
        from app.services.agent_chat import run_agent_chat

        agent_id = config.get("agent_id")
        agent = (
            await self.db.execute(select(Agent).where(Agent.id == agent_id))
        ).scalar_one_or_none()
        if agent is None:
            return _NodeResult(error="The configured agent no longer exists.")

        # Wired capabilities EXTEND the agent's own. The session_* kwargs
        # REPLACE (chat-session semantics), so the union is built here; None
        # when nothing is wired keeps the agent's behaviour byte-identical.
        wired = self._wired_capabilities(node["id"])

        def _union(own: list | None, extra: list[str]) -> list[str] | None:
            if not extra:
                return None
            merged = [str(x) for x in (own or [])]
            merged.extend(x for x in extra if x not in merged)
            return merged

        # A Tool node offers built-ins AND custom tools in one select; the
        # `builtin:` prefix (set by the catalog source) is what tells them
        # apart, since built-in ids are names, not UUIDs.
        wired_builtin_tools = [
            v.removeprefix("builtin:")
            for v in wired["tools"]
            if v.startswith("builtin:")
        ]
        wired_custom_tools = [
            v for v in wired["tools"] if not v.startswith("builtin:")
        ]
        # Coordination nodes (shared state / work items) grant their tools and
        # carry the sharing contract: a common key namespace, teammate peers.
        (
            coord_tools,
            shared_state_namespace,
            work_item_peers,
            workspace_ref,
            work_item_workspace_ref,
        ) = self._wired_coordination(node["id"], str(agent.id))
        # A wired Workspace knowledge node: resolved and failing closed
        # like the coordination scopes - an agent wired to a workspace never
        # runs without what it was wired for.
        session_workspace_id = None
        if (knowledge_ref := self._wired_workspace_ref(node["id"])) is not None:
            session_workspace_id = (
                await resolve_workspace_scope(self.db, self.user, knowledge_ref)
            ).id
        work_item_workspace_id = None
        if work_item_workspace_ref is not None:
            # Resolved and failing closed exactly like the shared-state scope:
            # the fallback would be an unscoped queue, and quietly merging one
            # workspace's backlog into the global one is a leak.
            work_item_workspace_id = (
                await resolve_workspace_scope(
                    self.db, self.user, work_item_workspace_ref
                )
            ).id
        if workspace_ref is not None:
            # The workspace scope is the only one that needs the database, and
            # the only one that can FAIL. Resolution raises on missing,
            # deleted, invisible or archived; the exception becomes this
            # node's error, which is the whole point — the alternative a
            # fallback would reach for is the un-namespaced global namespace,
            # and collapsing an isolated namespace into the shared one is a
            # data-leak shape rather than a degradation.
            workspace = await resolve_workspace_scope(
                self.db, self.user, workspace_ref
            )
            shared_state_namespace = workspace_namespace(workspace.id)
        # The run budget for THIS agent, nearest layer first: a Budget
        # node wired to this agent node, then the budget the calling flow
        # handed down. MERGED per dimension rather than picked whole: a wired
        # node that pins only the cost must not drop the inherited token limit
        # — that would unbound a dimension the parent flow deliberately set,
        # which is the opposite of what a budget is for. Whatever neither pins
        # keeps resolving through the agent's own Budget tab (which BOUNDS this
        # layer) and then the platform, inside ``run_agent_chat``.
        budget_overrides = self._compose_budget(node["id"])
        # A wired workset grants its tool family and names the corpus.
        session_workset = await self._resolved_workset(node["id"])
        if session_workset is not None and "workset" not in coord_tools:
            coord_tools = [*coord_tools, "workset"]
        session_builtin_tool_ids = _union(
            agent.builtin_tool_ids, wired_builtin_tools + coord_tools
        )
        session_custom_tool_ids = _union(agent.custom_tool_ids, wired_custom_tools)
        session_skill_ids = _union(agent.skill_ids, wired["skills"])
        session_knowledge_base_ids = _union(
            agent.knowledge_base_ids, wired["knowledge_bases"]
        )
        # The agent's integrations live as {provider: [ids]} bindings; the
        # session override takes a flat id list, so flatten before the union.
        own_integration_ids = [
            str(item)
            for values in (agent.integration_bindings or {}).values()
            for item in (values if isinstance(values, (list, tuple)) else [values])
            if item
        ]
        session_integration_ids = _union(
            own_integration_ids, wired["integrations"]
        )

        prompt = self._render(config.get("prompt") or "", wrap=True)
        persona_override = (config.get("persona_override") or "").strip() or None
        # Per-node execution overrides — empty means "inherit the agent's own".
        # None is passed through to run_agent_chat, which then reads the agent.
        max_iterations = _as_int(config.get("max_iterations"))
        max_tokens = _as_int(config.get("max_tokens"))
        temperature = _as_float(config.get("temperature"))
        model = (config.get("model") or "").strip() or None
        reasoning_effort = (config.get("reasoning_effort") or "").strip() or None
        # Per-node toggle — the side-by-side bridge: default True keeps the
        # agent behaving exactly as it does in chat (it may consult/delegate
        # its declared peers inside the node); False isolates it.
        allow_handover = config.get("allow_handover", True)

        # Run attachments reach the agent exactly as chat attachments would:
        # inlined on the user message (files=) + listed in the system prompt
        # (file_context=). None/None when the run carries no files.
        file_refs = await self._trigger_file_refs()
        file_context = None
        if file_refs:
            from app.tools.base import FileContext

            file_context = FileContext.from_file_refs(file_refs)

        # The labels on this node's outgoing handover edges constrain what
        # the agent may do: an edge labelled `transfer` narrows the LLM's
        # peers to transfer-typed ones. Unlabelled edges leave chat behaviour
        # untouched (no constraint).
        _handover_labels = {
            (e.get("label") or "").strip().lower()
            for e in _edges_from(self.graph, node["id"], "handover")
        }
        _handover_labels.discard("")
        allowed_handover_types = (
            frozenset(_handover_labels) if _handover_labels else None
        )

        # "On delegate" attachments: capability nodes wired into the
        # agent's on_delegate socket. Each delegation the agent makes emits
        # their declared event — the flow-visible signal of background work.
        on_delegate_configs: list[dict] = []
        for cap_edge in _edges_into(self.graph, node["id"]):
            if (cap_edge.get("target_handle") or "") != "on_delegate":
                continue
            cap_source = self.nodes.get(cap_edge.get("source") or "")
            if (
                isinstance(cap_source, dict)
                and (cap_source.get("kind") or "") == "capability.on_delegate"
            ):
                on_delegate_configs.append(cap_source.get("config") or {})

        delegations: list[dict] = []
        handovers: list[dict] = []
        messages_out: list = []
        response, handover_agent_id = await run_agent_chat(
            self.db,
            str(self.user.id),
            agent,
            [],
            prompt.text,
            allow_handover=bool(allow_handover),
            allowed_handover_types=allowed_handover_types,
            delegations_out=delegations,
            handovers_out=handovers,
            persona_override=persona_override,
            max_iterations_override=max_iterations,
            max_tokens_override=max_tokens,
            temperature_override=temperature,
            reasoning_effort_override=reasoning_effort,
            model_id_override=model,
            messages_out=messages_out,
            session_builtin_tool_ids=session_builtin_tool_ids,
            session_custom_tool_ids=session_custom_tool_ids,
            session_integration_ids=session_integration_ids,
            session_skill_ids=session_skill_ids,
            session_knowledge_base_ids=session_knowledge_base_ids,
            session_output_schema=wired["output_schema"],
            memory_overrides=wired["memory"],
            guardrails_override=wired["guardrails"],
            shared_state_namespace=shared_state_namespace,
            session_work_item_owner_agent_ids=work_item_peers,
            session_work_item_workspace_id=work_item_workspace_id,
            session_workspace_id=session_workspace_id,
            flow_run_id=str(self.run.id),
            session_workset=session_workset,
            session_tools_from_flow=True,
            budget_overrides=budget_overrides,
            files=file_refs or None,
            file_context=file_context,
            cancel_event=self.cancel_event,
            # A sandbox run's agent node is an evaluation turn (#8255): no
            # memory, no profile, no metered execution of the real agent, so
            # nothing it writes leaves the interception through another door.
            evaluation_mode=self._sandbox is not None,
            # Nobody approves a sandbox call: the interceptor decides each one.
            evaluation_tool_approval=False if self._sandbox is not None else None,
        )
        outputs = {
            "output": response,
            "summary": response[:500],
            "_tainted": prompt.tainted,
        }
        # A JSON answer is read field by field, the Script node's contract:
        # `{{node.fields.KEY}}`. `output` stays the text, whose sub-paths
        # resolve to nothing.
        from app.services.output_format import effective_output_contract

        answer_format, _schema = effective_output_contract(
            agent.output_format, agent.output_schema, wired["output_schema"]
        )
        if answer_format == "json":
            fields = _json_object(response)
            if fields is not None:
                outputs["fields"] = fields
        if delegations:
            outputs["delegations"] = [
                {**d, "task": (d.get("task") or "")[:500]} for d in delegations
            ]
            # An attachment emits at spawn (task info, now), at completion
            # (the RESULT, later — registered on the task and fired by the
            # sub-agent completion path), or both.
            spawn_cfgs = [
                c for c in on_delegate_configs
                if (c.get("emit_on") or "spawn") in ("spawn", "both")
            ]
            completion_cfgs = [
                c for c in on_delegate_configs
                if (c.get("emit_on") or "spawn") in ("completion", "both")
            ]
            outputs["delegation_events"] = await self._emit_delegation_events(
                spawn_cfgs, delegations
            )
            if completion_cfgs:
                await self._register_completion_emissions(
                    completion_cfgs, delegations
                )

        # ── The unified handover contract ────────────────────────
        # EVERY handover the agent performed — consult, delegate, transfer —
        # is a typed occurrence, and the handover branch fires whenever one
        # exists. Edge labels route: a `consult` edge fires only when a
        # consult occurred, unlabelled edges fire for every type. What the
        # author draws is what runs.
        occurrences: list[dict] = list(handovers)
        # The chat layer's delegation tee already mirrors spawns into
        # `handovers` chronologically; this fallback only covers layers (or
        # test fakes) that fill delegations_out alone. Dedup by task_id.
        seen_tasks = {
            o.get("task_id") for o in occurrences if o.get("task_id")
        }
        for d in delegations:
            if d.get("task_id") and d.get("task_id") in seen_tasks:
                continue
            occurrences.append(
                {
                    "type": "delegate",
                    "target_agent_id": d.get("target_agent_id"),
                    "target_agent_name": d.get("target_agent_name"),
                    "task": (d.get("task") or "")[:500],
                    "task_id": d.get("task_id"),
                }
            )
        # Compatibility: an agent-chat layer (or a test fake) that only
        # returns handover_agent_id still yields a transfer occurrence.
        if handover_agent_id and not any(
            o.get("type") == "transfer" for o in occurrences
        ):
            occurrences.append(
                {
                    "type": "transfer",
                    "target_agent_id": handover_agent_id,
                    "target_agent_name": "",
                    "reason": "",
                }
            )
        handover_types = frozenset(
            str(o.get("type") or "") for o in occurrences
        ) | frozenset(
            "mandatory_transfer" for o in occurrences if o.get("mandatory")
        )
        if occurrences:
            # Journal-bounded: an agent looping on consults must not bloat
            # the step row (each occurrence's text is already truncated at
            # the source).
            outputs["handovers"] = occurrences[-50:]
            outputs["handover"] = occurrences[-1]
        handles = ["out"]
        if handover_agent_id:
            outputs["handover_agent_id"] = handover_agent_id
        if occurrences and _edges_from(self.graph, node["id"], "handover"):
            handles = ["handover", "out"]
        return _NodeResult(
            outputs=outputs,
            next_handles=handles,
            handover_types=handover_types if occurrences else None,
        )

    async def _trigger_file_refs(self) -> list:
        """The run's attachments as ``FileRef``s (trigger_payload["files"]).

        Loaded once per burst. Every agent node receives them — the run's
        files are its working set, exactly like a chat turn's attachments.
        """
        if self._trigger_files_cache is not None:
            return self._trigger_files_cache
        metas = (self.run.trigger_payload or {}).get("files") or []
        ids = [
            _coerce_uuid(m.get("file_id"))
            for m in metas
            if isinstance(m, dict) and m.get("file_id")
        ]
        ids = [i for i in ids if i is not None]
        refs: list = []
        if ids:
            from app.llm.base import FileRef
            from app.models.file_attachment import FileAttachment

            rows = (
                (
                    await self.db.execute(
                        select(FileAttachment).where(FileAttachment.id.in_(ids))
                    )
                )
                .scalars()
                .all()
            )
            refs = [FileRef.from_attachment(fa) for fa in rows]
        self._trigger_files_cache = refs
        return refs

    def _wired_coordination(
        self, node_id: str, own_agent_id: str | None
    ) -> tuple[list[str], str | None, list[str] | None, str | None, str | None]:
        """Coordination capabilities wired into an agent node.

        Returns ``(extra_builtin_tool_ids, shared_state_namespace, peers,
        workspace_ref)``:

        * A wired ``capability.shared_state`` grants the shared-state tools
          and scopes their keys — per run (default), per flow, per workspace,
          or global. One node feeding several agents = one namespace = shared
          blackboard.
        * A wired ``capability.work_items`` grants the work-item tools, and
          every OTHER agent wired to the same node becomes an assignable
          peer (plus any extra peers the node declares) — the canvas wiring
          IS the team roster.

        ``work_item_workspace_ref`` is separate from ``workspace_ref``: the
        work-items node names its own workspace and has no `scope` field at
        all, because a work item has no key to namespace and so `run` / `flow`
        would mean nothing for it. Two nodes, two independent referents.

        ``workspace_ref`` is the fourth element because a **workspace scope
        cannot be resolved here**. Run and flow take their referent from
        ``self.run``, which this method already holds; a workspace must be
        looked up, checked for visibility and checked for archival, and that
        needs the database. So this returns the id the config named and leaves
        ``namespace`` None, and the async caller resolves it — or fails the
        node. Splitting it that way is what makes the failure impossible to
        skip: a caller that ignores ``workspace_ref`` gets no namespace at
        all rather than a silently global one.
        """
        extra_tools: list[str] = []
        namespace: str | None = None
        peers: list[str] = []
        workspace_ref: str | None = None
        work_item_workspace_ref: str | None = None
        # `global` resolves to no namespace at all, so "already scoped" cannot
        # be read off `namespace` / `workspace_ref`: a later wired node would
        # override an explicit `global`.
        scope_resolved = False
        for edge in _edges_into(self.graph, node_id):
            handle = edge.get("target_handle") or ""
            source = self.nodes.get(edge.get("source") or "")
            if not isinstance(source, dict):
                continue
            kind = source.get("kind") or ""
            config = source.get("config") or {}
            if handle == "shared_state" and kind == "capability.shared_state":
                if "shared_state" not in extra_tools:
                    extra_tools.append("shared_state")
                # First wired node wins the scope, `global` included.
                if not scope_resolved:
                    scope_resolved = True
                    scope = (
                        config.get("scope") or COORDINATION_SCOPE_DEFAULT
                    ).strip()
                    if scope == COORDINATION_SCOPE_RUN:
                        namespace = f"flow-run:{self.run.id}"
                    elif scope == COORDINATION_SCOPE_FLOW:
                        namespace = f"flow:{self.run.flow_id}"
                    elif scope == COORDINATION_SCOPE_WORKSPACE:
                        # Resolved by the caller — see the docstring.
                        workspace_ref = str(
                            config.get("workspace_id") or ""
                        ).strip()
                    elif scope != COORDINATION_SCOPE_GLOBAL:
                        # FAIL CLOSED on a scope this code does not know.
                        #
                        # There was no `else` here, and its absence was a
                        # fail-OPEN: an unrecognised scope left `namespace`
                        # None, which means raw un-namespaced keys — the
                        # global blackboard. The graph validator rejects an
                        # out-of-range static select, so this was only
                        # reachable through hand-written JSON or an API
                        # caller; it stops being acceptable now that one of
                        # the scopes exists specifically to isolate.
                        raise WorkspaceScopeError(
                            "unknown_scope",
                            f"Unknown shared-state scope '{scope}'. Valid "
                            f"scopes: {', '.join(COORDINATION_SCOPES)}.",
                        )
            elif handle == "work_items" and kind == "capability.work_items":
                if "work_items" not in extra_tools:
                    extra_tools.append("work_items")
                # First wired node wins, as for the peers below; the validator
                # warns when a second is attached.
                if work_item_workspace_ref is None:
                    work_item_workspace_ref = (
                        str(config.get("workspace_id") or "").strip() or None
                    )
                declared = config.get("peer_agent_ids")
                if isinstance(declared, list):
                    peers.extend(str(p) for p in declared if p)
                # Teammates: the other agents plugged into this same node.
                for peer_edge in self.graph.get("edges") or []:
                    if (
                        peer_edge.get("source") != source.get("id")
                        or (peer_edge.get("target_handle") or "") != "work_items"
                    ):
                        continue
                    target = self.nodes.get(peer_edge.get("target") or "")
                    if not isinstance(target, dict):
                        continue
                    peer_agent = (target.get("config") or {}).get("agent_id")
                    if isinstance(peer_agent, str) and peer_agent:
                        peers.append(peer_agent)
        deduped: list[str] = []
        for p in peers:
            if p != own_agent_id and p not in deduped:
                deduped.append(p)
        return (
            extra_tools,
            namespace,
            deduped or None,
            workspace_ref,
            work_item_workspace_ref,
        )

    def _wired_workspace_ref(self, node_id: str) -> str | None:
        """The workspace a ``capability.workspace`` node wired to *node_id*
        names, the first one wired - or None when none is wired.

        An empty reference on a wired node is returned as ``""`` rather than
        None, so resolution refuses it instead of the node being skipped.
        """
        for edge in _edges_into(self.graph, node_id):
            if (edge.get("target_handle") or "") != "workspace":
                continue
            source = self.nodes.get(edge.get("source") or "")
            if isinstance(source, dict) and source.get("kind") == "capability.workspace":
                return str((source.get("config") or {}).get("workspace_id") or "").strip()
        return None

    async def _resolved_workset(self, node_id: str) -> dict | None:
        """:meth:`_wired_workset`, with its workspace scope resolved.

        Both consumers of a workset binding go through here so the two cannot
        drift: an agent node and an action node must fail the same way when
        their workspace is gone, and a resolution that lived at one call site
        would silently not apply at the other.

        Raises rather than degrading — the reasoning is the same as for
        shared state: the only fallback available is the un-namespaced global
        corpus, and quietly merging an isolated corpus into the shared one is
        a leak, not a lesser service.
        """
        binding = self._wired_workset(node_id)
        if binding is None:
            return None
        workspace_ref = binding.pop("workspace_ref", None)
        if workspace_ref is None:
            return binding
        workspace = await resolve_workspace_scope(
            self.db, self.user, workspace_ref
        )
        # The typed column, not `scope_ref`: `ck_worksets_workspace_scope`
        # requires exactly this pairing.
        binding["workspace_id"] = workspace.id
        return binding

    def _wired_workset(self, node_id: str) -> dict | None:
        """The `capability.workset` node wired into this node.

        Returns the binding an agent (or an action node) works through, or None.
        The corpus is named HERE, on the canvas, and passed down — no tool takes
        a workset id, so a prompt can neither point the work at another corpus
        nor lose track of which one it is on.

        First wired wins: the socket is `multiple` so ONE workset node can feed
        several agents, which is how they end up building one corpus together.
        """
        for edge in _edges_into(self.graph, node_id):
            # The dedicated socket only. Accepting `in` as well would let a
            # capability node bound to something else leak in through the
            # control-flow port.
            if (edge.get("target_handle") or "") != "workset":
                continue
            source = self.nodes.get(edge.get("source") or "")
            if not isinstance(source, dict):
                continue
            if source.get("kind") != "capability.workset":
                continue
            config = source.get("config") or {}
            scope = (
                str(config.get("scope") or COORDINATION_SCOPE_DEFAULT).strip()
                or COORDINATION_SCOPE_DEFAULT
            )
            # `run` and `flow` need the id they are scoped to; the service
            # refuses a non-global scope without one rather than letting every
            # run share a corpus.
            #
            # `workspace` is the exception: its referent goes in the typed
            # `workspace_id` column, NOT in `scope_ref`, and the model's CHECK
            # keeps the two in step. The binding therefore carries a
            # `workspace_ref` for the caller to resolve — like
            # `_wired_coordination`, because the lookup needs the database and
            # can fail.
            scope_ref = None
            workspace_ref = None
            if scope == COORDINATION_SCOPE_RUN:
                scope_ref = str(self.run.id)
            elif scope == COORDINATION_SCOPE_FLOW:
                scope_ref = str(self.run.flow_id)
            elif scope == COORDINATION_SCOPE_WORKSPACE:
                workspace_ref = str(config.get("workspace_id") or "").strip()
            elif scope != COORDINATION_SCOPE_GLOBAL:
                raise WorkspaceScopeError(
                    "unknown_scope",
                    f"Unknown workset scope '{scope}'. Valid scopes: "
                    f"{', '.join(COORDINATION_SCOPES)}.",
                )
            expires_at = None
            retention = _as_int(config.get("retention_hours"))
            if retention:
                expires_at = datetime.now(UTC) + timedelta(hours=retention)
            return {
                "key": str(config.get("key") or "reports").strip() or "reports",
                "scope": scope,
                "scope_ref": scope_ref,
                "workspace_ref": workspace_ref,
                "processing": str(config.get("processing") or "auto"),
                "processor_agent_id": config.get("processor_agent_id") or None,
                "workers": _as_int(config.get("workers")) or 1,
                "max_items": _as_int(config.get("max_items")),
                "max_content_chars": _as_int(config.get("max_content_chars")),
                "max_auto_calls": _as_int(config.get("max_auto_calls")),
                "expires_at": expires_at,
                # The LLM node wired into the WORKSET node (not the agent)
                # configures the automatic summary/priority pass.
                "llm_config": self._wired_llm(source.get("id") or "") or None,
            }
        return None

    def _inherited_budget(self) -> dict | None:
        """The run budget this run was STARTED with, if any.

        A ``flow.call`` node may wire a Budget into the flow it calls; the
        runner stamps it on the child run's ``trigger_context`` so it reaches
        every agent of that flow — and, because the child stamps it again on
        its own calls, every agent of the flows below it. One ceiling for a
        whole composed pipeline, wired once.
        """
        raw = (self.run.trigger_context or {}).get("budget_overrides")
        return raw if isinstance(raw, dict) and raw else None

    def _compose_budget(self, node_id: str) -> dict | None:
        """This node's effective budget layer: wired node over inherited.

        Merged PER DIMENSION (``budget_policy.merge_overrides``), not chosen
        whole. A wired Budget node that pins only the cost has to leave the
        inherited token limit standing — picking one dict over the other would
        silently unbound every dimension the nearer layer happens not to set,
        which is how a budget feature ends up not bounding anything.

        Returns ``None`` when neither layer pins anything, so the agent's own
        Budget tab still applies downstream instead of an empty dict shadowing
        it.
        """
        composed = merge_budget_overrides(
            parse_budget_overrides(self._wired_budget(node_id)),
            parse_budget_overrides(self._inherited_budget()),
        )
        return composed.to_dict() or None

    def _wired_budget(self, node_id: str) -> dict | None:
        """The `capability.budget` node wired into this node's `budget` socket.

        Returns its config — the partial override
        ``app.services.budget_policy`` resolves — or None when nothing is
        wired. Works for both sockets that accept one: an agent node (bounding
        that agent) and a `flow.call` node (bounding the called flow).

        First wired wins. The socket is `multiple` so ONE Budget node can fan
        out to every agent that should share the ceiling — the wiring IS the
        distribution — and the validator warns when a second is attached to
        the same agent (`agent.multiple_budgets`).
        """
        for edge in _edges_into(self.graph, node_id):
            # The dedicated socket only: a Budget node reaching a node through
            # the control-flow port would bound something nobody wired it to.
            if (edge.get("target_handle") or "") != "budget":
                continue
            source = self.nodes.get(edge.get("source") or "")
            if isinstance(source, dict) and source.get("kind") == "capability.budget":
                return source.get("config") or {}
        return None

    def _wired_llm(self, node_id: str) -> dict | None:
        """The `capability.llm` node wired into this node's `llm` socket.

        Returns its config (``{model, instructions}``) or None when the node is
        deterministic. First wired wins — the socket is `multiple` only so one
        LLM node can drive several conditions.
        """
        for edge in _edges_into(self.graph, node_id):
            if (edge.get("target_handle") or "") != "llm":
                continue
            source = self.nodes.get(edge.get("source") or "")
            if isinstance(source, dict) and source.get("kind") == "capability.llm":
                return source.get("config") or {}
        return None

    async def _exec_condition_if(self, node: dict, config: dict) -> _NodeResult:
        llm = self._wired_llm(node["id"])
        if llm is not None:
            from app.services.flow_condition_llm import evaluate_question

            question = self._render(config.get("question") or "")
            verdict, reason = await evaluate_question(
                self.db,
                question=question.text,
                llm_config=llm,
                user_id=str(self.user.id),
            )
            return _NodeResult(
                outputs={
                    "result": verdict,
                    "reason": reason,
                    "_tainted": question.tainted,
                },
                next_handles=["true" if verdict else "false"],
            )

        left = self._render(config.get("left") or "")
        right = self._render(config.get("right") or "")
        verdict = evaluate_operator(
            left.text, config.get("operator") or "equals", right.text
        )
        return _NodeResult(
            outputs={"result": verdict, "_tainted": left.tainted or right.tainted},
            next_handles=["true" if verdict else "false"],
        )

    async def _exec_condition_switch(self, node: dict, config: dict) -> _NodeResult:
        value = self._render(config.get("value") or "")
        llm = self._wired_llm(node["id"])

        if llm is not None:
            from app.services.flow_condition_llm import evaluate_cases

            # SAME list as deterministic mode — the entries' values are read
            # as descriptions here. Descriptions may reference upstream
            # outputs too.
            raw = config.get("cases") or {}
            cases = (
                {
                    str(name): self._render(_to_text(description)).text
                    for name, description in raw.items()
                }
                if isinstance(raw, dict)
                else {}
            )
            matched, reason = await evaluate_cases(
                self.db,
                value=value.text,
                cases=cases,
                llm_config=llm,
                user_id=str(self.user.id),
            )
            outputs = {
                "matched": matched,
                "reason": reason,
                "_tainted": value.tainted,
            }
            if matched is not None:
                return _NodeResult(
                    outputs=outputs,
                    # Both socket shapes: the per-case handle (one output per
                    # case, the Blueprint shape) and the legacy single `case`
                    # handle whose edges are filtered by label.
                    next_handles=[f"case:{matched}", "case"],
                    switch_case=matched,
                )
            return _NodeResult(outputs=outputs, next_handles=["default"])

        cases = config.get("cases") or {}
        matched = None
        if isinstance(cases, dict):
            for case_name, case_value in cases.items():
                rendered = self._render(_to_text(case_value))
                if value.text == rendered.text:
                    matched = case_name
                    break
        if matched is not None:
            return _NodeResult(
                outputs={"matched": matched, "_tainted": value.tainted},
                # Per-case handle first (the Blueprint shape), plus the legacy
                # single `case` handle whose edges are filtered by label.
                next_handles=[f"case:{matched}", "case"],
                switch_case=matched,
            )
        return _NodeResult(
            outputs={"matched": None, "_tainted": value.tainted},
            next_handles=["default"],
        )

    async def _exec_condition_classify(self, config: dict) -> _NodeResult:
        """Route on the label a decision model (CLM) picks (``flow_classifier``).

        Unlike an LLM-judged Switch, a call that brings no decision back fails
        the node down its error path: content this node filters must never be
        dropped because the provider was down.
        """
        from app.llm.decisions import MAX_CHOICES
        from app.services.flow_classifier import (
            MAX_INSTRUCTIONS_CHARS,
            MAX_LABEL_DESCRIPTION_CHARS,
            MAX_STATE_CHARS,
            ClassifierError,
            classify,
            parse_provider_id,
        )

        model = str(config.get("model") or "").strip()
        if not model:
            return _NodeResult(error="Pick the decision model this node asks.")
        raw = config.get("cases")
        labels = (
            {
                # Kept verbatim: the name IS the socket id (`case:<name>`).
                str(name): self._render(_to_text(description))
                for name, description in raw.items()
                if str(name)
            }
            if isinstance(raw, dict)
            else {}
        )
        if not labels:
            return _NodeResult(error="Declare at least one label to classify into.")
        if len(labels) > MAX_CHOICES:
            return _NodeResult(
                error=f"A decision model takes at most {MAX_CHOICES} labels."
            )
        content = self._render(config.get("input") or "")
        question = self._render(config.get("question") or "")
        if not content.text.strip():
            return _NodeResult(error="The content to classify is empty.")
        if not question.text.strip():
            return _NodeResult(error="The question to ask about the content is empty.")
        tainted = (
            content.tainted
            or question.tainted
            or any(r.tainted for r in labels.values())
        )
        state = content.text
        warning = None
        if len(state) > MAX_STATE_CHARS:
            state = state[:MAX_STATE_CHARS]
            warning = (
                f"The content was cut to its first {MAX_STATE_CHARS} characters "
                "before it was classified."
            )
        try:
            decision = await classify(
                self.db,
                provider_id=parse_provider_id(config.get("provider")),
                model=model,
                state=state,
                instructions=question.text[:MAX_INSTRUCTIONS_CHARS],
                choices={
                    name: r.text[:MAX_LABEL_DESCRIPTION_CHARS]
                    for name, r in labels.items()
                },
                user_id=str(self.user.id),
            )
        except ClassifierError as exc:
            return _NodeResult(error=str(exc))

        outputs = {
            "label": decision.choice,
            "confidence": decision.confidence,
            "probabilities": decision.probabilities,
            "_tainted": tainted,
        }
        if decision.confidence < _confidence_floor(config.get("min_confidence")):
            return _NodeResult(
                outputs=outputs, next_handles=["default"], warning=warning
            )
        return _NodeResult(
            outputs=outputs,
            # Per-label socket first (the Switch's Blueprint shape), plus the
            # single `case` handle whose edges are filtered by label.
            next_handles=[f"case:{decision.choice}", "case"],
            switch_case=decision.choice,
            warning=warning,
        )

    async def _exec_send_message(self, config: dict) -> _NodeResult:
        channel = (config.get("channel") or "").strip()
        target = self._render(config.get("target") or "")
        message = self._render(config.get("message") or "")
        tainted = target.tainted or message.tainted

        if channel == "email":
            from app.services.email import send_email

            ok = await send_email(
                to=target.text,
                subject="Flow notification",
                html=message.text.replace("\n", "<br>"),
                text=message.text,
            )
            if not ok:
                return _NodeResult(error="Email delivery failed or is not configured.")
            return _NodeResult(outputs={"result": "sent", "_tainted": tainted})

        if channel in ("slack", "teams"):
            from app.services.sub_agent import _send_proactive_channel_message

            await _send_proactive_channel_message(
                channel=channel,
                channel_thread_id=target.text,
                text=message.text,
                agent_name="Flow",
            )
            return _NodeResult(outputs={"result": "sent", "_tainted": tainted})

        return _NodeResult(
            error=(
                f"Channel `{channel}` is not wired for flow delivery yet — "
                "use email, Slack or Teams, or route through an agent node."
            )
        )

    async def _exec_create_work_item(self, config: dict) -> _NodeResult:
        from app.services.company_managed_writes import flow_writer
        from app.services.work_item import WorkItemOrigin, create_work_item, list_work_items

        title = self._render(config.get("title") or "")
        body = self._render(config.get("body") or "")
        correlation = self._render(config.get("correlation_key") or "")
        # Catalog vocabulary → model vocabulary.
        priority = {"normal": "medium"}.get(
            config.get("priority") or "normal", config.get("priority") or "medium"
        )

        # The correlation key IS the dedup contract the field promises: an
        # open item already carrying it is reused instead of minted again
        # (mirrors the Action Register materializer's idempotency). Probed
        # where the item would be created - unfiled - never in a workspace
        # the owner can merely read.
        item: dict | None = None
        if correlation.text.strip():
            existing = await list_work_items(
                user_id=str(self.user.id),
                correlation_key=correlation.text.strip(),
                open_only=True,
                limit=1,
                unfiled_only=True,
            )
            item = existing[0] if existing else None

        if item is None:
            item = await create_work_item(
                user_id=str(self.user.id),
                title=title.text,
                description=body.text or None,
                owner_agent_id=config.get("owner_agent_id") or None,
                priority=priority,
                correlation_key=correlation.text or None,
                source="flow",
                origin=WorkItemOrigin(flow_run_id=self.run.id),
                writer=flow_writer(is_company_managed=self.run.is_company_managed),
            )
        tainted = title.tainted or body.tainted

        # "Wait for completion": park until an agent finishes the item — the
        # same shape as waiting on an assignment run, polled on resume.
        if config.get("wait"):
            wake = datetime.now(UTC) + timedelta(seconds=ASSIGNMENT_POLL_SECONDS)
            return _NodeResult(
                park={
                    "kind": "work_item",
                    "work_item_id": str(item.get("id")),
                    "wake_at": wake.isoformat(),
                    "tainted": tainted,
                }
            )
        return _NodeResult(
            outputs={
                "work_item_id": item.get("id"),
                "_tainted": tainted,
            }
        )

    async def _exec_notify(self, config: dict) -> _NodeResult:
        """Ring the platform's notification bell.

        Durable (activity feed) + live SSE push, addressed to the run's user
        — a company-managed run broadcasts to every connected user, the same
        routing assignment notifications use.
        """
        from app.services.notify import notify_web_user

        title = self._render(config.get("title") or "")
        message = self._render(config.get("message") or "")
        if not title.text.strip():
            return _NodeResult(error="The notification needs a title.")
        try:
            await notify_web_user(
                self.db,
                str(self.user.id),
                title=title.text.strip(),
                summary=message.text.strip() or None,
                source_label=self._flow_name or "Flow",
                extra_data={
                    "notification_source": "flow",
                    "flow_id": str(self.run.flow_id),
                    "flow_run_id": str(self.run.id),
                },
                is_company=bool(self.run.is_company_managed),
            )
        except Exception as exc:
            return _NodeResult(error=format_exception_message(exc))
        return _NodeResult(
            outputs={
                "delivered": True,
                "_tainted": title.tainted or message.tainted,
            }
        )

    async def _exec_ask_user(self, node: dict, config: dict) -> _NodeResult:
        """Human-in-the-loop: ask, park, resume with the answer.

        The question rings the bell as an URGENT notification (source
        ``flow_hitl``) that deep-links to this run; the run parks until the
        respond endpoint stores the answer and releases it. The answer then
        becomes this node's output — ``{{ask.response}}`` downstream.

        Options, a recommendation and a deadline (#842, S6) make it a decision
        like any other: the park carries the normalised ask, the card shows
        the options, and the chosen key is ``{{ask.option}}`` downstream. A
        node without options asks for free text, as before.
        """
        from app.services.decision_ask import flow_ask
        from app.services.notify import notify_web_user

        title = self._render(config.get("title") or "")
        question = self._render(config.get("question") or "")
        if not title.text.strip():
            return _NodeResult(error="The question needs a title.")
        if not question.text.strip():
            return _NodeResult(error="The question text is required.")
        deadline = self._render(str(config.get("deadline") or "")).text.strip()
        try:
            ask = flow_ask(question.text.strip(), config.get("options"), config.get("recommendation"), deadline)
        except ValueError as exc:
            return _NodeResult(error=str(exc))
        try:
            await notify_web_user(
                self.db,
                str(self.user.id),
                title=title.text.strip(),
                summary=question.text.strip(),
                source_label=self._flow_name or "Flow",
                extra_data={
                    "notification_source": "flow_hitl",
                    "flow_id": str(self.run.flow_id),
                    "flow_run_id": str(self.run.id),
                },
                is_company=bool(self.run.is_company_managed),
            )
        except Exception as exc:
            return _NodeResult(error=format_exception_message(exc))
        now = datetime.now(UTC)
        wake = now + timedelta(seconds=HUMAN_POLL_SECONDS)
        return _NodeResult(
            park={
                "kind": "human",
                "node": node["id"],
                "question": question.text.strip(),
                **({"ask": ask} if ask is not None else {}),
                "released": False,
                "parked_at": now.isoformat(),
                "wake_at": wake.isoformat(),
            }
        )

    async def _emit_delegation_events(
        self, configs: list[dict], delegations: list[dict]
    ) -> int:
        """Emit each "On delegate" attachment's event once per delegation.

        Mapping templates render with a pseudo-node ``delegation`` exposing
        the entry's fields ({{delegation.task}}, {{delegation.task_id}}, …)
        on top of the run's normal reference context. Emission failures are
        logged and skipped — the agent's own work already succeeded, the
        signal must not fail the node.
        """
        from app.services.flow_events import (
            coerce_event_fields,
            emit_flow_event,
            get_event_type,
        )

        if not configs or not delegations:
            return 0
        emitted = 0
        chain_depth = (
            int((self.run.trigger_context or {}).get("chain_depth") or 0) + 1
        )
        for cfg in configs:
            event_type = await get_event_type(self.db, cfg.get("event_type_id"))
            if event_type is None:
                logger.warning(
                    "On-delegate attachment points at a missing event type (%s)",
                    cfg.get("event_type_id"),
                )
                continue
            mappings = cfg.get("mappings") or {}
            for delegation in delegations:
                if self._renderer is not None:
                    self._renderer._outputs["delegation"] = delegation
                try:
                    rendered: dict[str, str] = {}
                    for f in event_type.fields or []:
                        name = (f.get("name") or "").strip()
                        if not name or mappings.get(name) is None:
                            continue
                        rendered[name] = self._render(mappings.get(name)).text
                    payload, errors = coerce_event_fields(
                        event_type.fields or [], rendered
                    )
                    if errors:
                        logger.warning(
                            "On-delegate event payload mismatch: %s",
                            " ".join(errors),
                        )
                        continue
                    await emit_flow_event(
                        self.db,
                        event_type=event_type,
                        user_id=self.user.id,
                        flow_id=self.run.flow_id,
                        run_id=self.run.id,
                        scope=(cfg.get("scope") or "global").strip() or "global",
                        payload=payload,
                        chain_depth=chain_depth,
                    )
                    emitted += 1
                except Exception as exc:
                    logger.warning(
                        "On-delegate event emission failed: %s",
                        format_exception_message(exc),
                    )
                finally:
                    if self._renderer is not None:
                        self._renderer._outputs.pop("delegation", None)
        return emitted

    async def _register_completion_emissions(
        self, configs: list[dict], delegations: list[dict]
    ) -> None:
        """Stamp completion-emission specs onto each delegated task.

        A completion attachment fires when the BACKGROUND run ends — long
        after this step committed — so everything the emission needs is
        frozen here on ``SubAgentTask.metadata_json`` and replayed by the
        sub-agent completion path (``emit_delegation_completion_events``).
        Mappings stay RAW: only ``{{delegation.*}}`` renders at completion
        (with ``result`` and ``status`` finally known); the flow's other
        references are gone by then, by design.
        """
        from sqlalchemy.orm import attributes

        from app.models.sub_agent_task import SubAgentTask

        task_ids = []
        for d in delegations:
            try:
                task_ids.append(uuid.UUID(str(d.get("task_id"))))
            except (TypeError, ValueError):
                continue
        if not task_ids:
            return
        chain_depth = (
            int((self.run.trigger_context or {}).get("chain_depth") or 0) + 1
        )
        specs = [
            {
                "event_type_id": str(cfg.get("event_type_id") or ""),
                "mappings": cfg.get("mappings") or {},
                "scope": (cfg.get("scope") or "global").strip() or "global",
                "run_id": str(self.run.id),
                "flow_id": str(self.run.flow_id),
                "user_id": str(self.user.id),
                "chain_depth": chain_depth,
            }
            for cfg in configs
            if cfg.get("event_type_id")
        ]
        if not specs:
            return
        rows = (
            (
                await self.db.execute(
                    select(SubAgentTask).where(SubAgentTask.id.in_(task_ids))
                )
            )
            .scalars()
            .all()
        )
        for task in rows:
            meta = dict(task.metadata_json or {})
            existing = list(meta.get("flow_completion_emissions") or [])
            meta["flow_completion_emissions"] = existing + specs
            task.metadata_json = meta
            attributes.flag_modified(task, "metadata_json")

    async def _exec_emit_event(self, config: dict) -> _NodeResult:
        """Fire a declared event — the UE "Call Event Dispatcher".

        The payload is the node's per-field ``mappings`` rendered and coerced
        against the declaration's signature; a mismatch fails THIS node (the
        error path) instead of multicasting a malformed event.  Listener runs
        start inside this transaction — the emitting step's commit publishes
        both the emission and the runs atomically.
        """
        from app.services.flow_events import (
            coerce_event_fields,
            emit_flow_event,
            get_event_type,
        )

        event_type = await get_event_type(self.db, config.get("event_type_id"))
        if event_type is None:
            return _NodeResult(
                error=(
                    "The event type no longer exists — pick another one in "
                    "the node's configuration."
                )
            )
        mappings = config.get("mappings") or {}
        rendered: dict[str, str] = {}
        tainted = False
        for f in event_type.fields or []:
            name = (f.get("name") or "").strip()
            if not name or mappings.get(name) is None:
                continue
            r = self._render(mappings.get(name))
            rendered[name] = r.text
            tainted = tainted or r.tainted
        payload, errors = coerce_event_fields(event_type.fields or [], rendered)
        if errors:
            return _NodeResult(
                error="Event payload mismatch: " + " ".join(errors)
            )

        # Emit chains inherit depth: a run this event starts carries
        # chain_depth, and its own emits go one deeper.
        chain_depth = (
            int((self.run.trigger_context or {}).get("chain_depth") or 0) + 1
        )
        result = await emit_flow_event(
            self.db,
            event_type=event_type,
            user_id=self.user.id,
            flow_id=self.run.flow_id,
            run_id=self.run.id,
            scope=(config.get("scope") or "flow").strip() or "flow",
            payload=payload,
            chain_depth=chain_depth,
        )
        # Taint travels with the event: listener runs mark their trigger step
        # untrusted (see _start) so downstream guards keep applying.
        if tainted:
            for started_id in result.started_run_ids:
                child = (
                    await self.db.execute(
                        select(FlowRun).where(
                            FlowRun.id == _coerce_uuid(started_id)
                        )
                    )
                ).scalar_one_or_none()
                if child is not None:
                    context = dict(child.trigger_context or {})
                    context["untrusted"] = True
                    child.trigger_context = context
        return _NodeResult(
            outputs={
                "event_id": str(result.event.id),
                "listeners_started": len(result.started_run_ids),
                "_tainted": tainted,
            }
        )

    async def _exec_wait_event(self, config: dict) -> _NodeResult:
        """Park until a matching event is emitted — the latent bind.

        Polled like a work-item wait: the resume tick queries the events
        table for the first emission AFTER the park whose scope reaches this
        run.  The declaration's name is frozen into the wait state so the
        resumed outputs stay correct even if the type is renamed meanwhile.
        """
        from app.services.flow_events import get_event_type

        event_type = await get_event_type(self.db, config.get("event_type_id"))
        if event_type is None:
            return _NodeResult(
                error=(
                    "The event type no longer exists — pick another one in "
                    "the node's configuration."
                )
            )
        now = datetime.now(UTC)
        wake = now + timedelta(seconds=ASSIGNMENT_POLL_SECONDS)
        return _NodeResult(
            park={
                "kind": "event",
                "event_type_id": str(event_type.id),
                "event_name": event_type.name,
                "parked_at": now.isoformat(),
                "wake_at": wake.isoformat(),
            }
        )

    async def _exec_runner_command(self, config: dict) -> _NodeResult:
        """`action.runner_command` — run a command on an enrolled machine.

        Rides the runners transport untouched: same authorization
        (permission mode, allow-lists, approval gating), same audit row,
        same durable ``RunnerCommand`` queue as agent tools. Failure is a
        pin, not a run failure — a refusal or a failed command takes `fail`
        with the reason readable downstream.

        `wait` mode parks and the resume tick re-checks the row (the result
        upload nudges the reconciler, so joins are near-instant); `dispatch`
        mode continues immediately with the command id.
        """
        from app.core.config import settings as app_settings
        from app.models.runner import Runner
        from app.services.runners import (
            authorize_command,
            command_requires_approval,
            is_runner_online,
        )

        def _fail(reason: str, status: str = "failed") -> _NodeResult:
            return _NodeResult(
                outputs={
                    "command_id": "",
                    "status": status,
                    "result": {},
                    "file_id": "",
                    "error": reason,
                },
                next_handles=["fail"],
            )

        if not app_settings.runners_enabled:
            return _fail("Runners are disabled on this platform.")

        from app.api.v1.helpers import get_user_group_ids, user_visible_filter

        # Group ids matter here: the builder's runner picker offers
        # group-shared machines, so the executor must see them too or a
        # picked runner fails at run time.
        group_ids = await get_user_group_ids(self.db, self.user.id)
        runner = (
            await self.db.execute(
                select(Runner).where(
                    Runner.id == _coerce_uuid(config.get("runner_id")),
                    user_visible_filter(
                        Runner,
                        self.user.id,
                        group_ids,
                        is_admin=self.user.is_admin,
                    ),
                )
            )
        ).scalar_one_or_none()
        if runner is None:
            return _fail(
                "The configured runner no longer exists (or is not visible "
                "to the person who ran the flow)."
            )

        kind = str(config.get("command_kind") or "run_shell")
        payload, _tainted = (
            self._renderer.render_value(config.get("payload") or {}, wrap=True)
            if self._renderer
            else (config.get("payload") or {}, False)
        )
        if not isinstance(payload, dict):
            return _fail("The payload must be a JSON object.")

        refusal = authorize_command(runner, kind, payload)
        if refusal:
            return _fail(refusal, status="denied")
        if not is_runner_online(runner):
            return _fail(f"Runner '{runner.name}' is offline.", status="failed")

        from app.services.runner_transport import dispatch_command

        requires_approval = command_requires_approval(runner, kind, payload)
        # Under this run's row lock, held until the burst commits its park: a
        # cancel or a dead-letter (both lock the row) either ended the run
        # first and nothing is dispatched, or waits and then gives up the
        # command it finds by run.
        run_status = await self.db.scalar(
            select(FlowRun.status).where(FlowRun.id == self.run.id).with_for_update()
        )
        if run_status not in FLOW_RUN_ACTIVE_STATUSES:
            return _fail(
                "The flow run ended before the command was dispatched.",
                status="cancelled",
            )
        command_id, status = await dispatch_command(
            runner_id=runner.id,
            kind=kind,
            payload=payload,
            user_id=self.user.id,
            flow_run_id=self.run.id,
            requires_approval=requires_approval,
        )
        if status == "revoked":
            return _fail(f"Runner '{runner.name}' was revoked.")
        if status == "busy":
            return _fail(
                f"Runner '{runner.name}' is at its concurrent-command "
                "capacity — try again later."
            )

        if str(config.get("mode") or "wait") == "dispatch":
            return _NodeResult(
                outputs={
                    "command_id": command_id,
                    "status": status,
                    "result": {},
                    "file_id": "",
                    "error": "",
                },
                next_handles=["ok"],
            )

        now = datetime.now(UTC)
        wake = now + timedelta(seconds=ASSIGNMENT_POLL_SECONDS)
        return _NodeResult(
            park={
                "kind": "runner_command",
                "command_id": command_id,
                "runner_name": runner.name,
                "parked_at": now.isoformat(),
                "wake_at": wake.isoformat(),
            }
        )

    # ── Recent platform features (work items, fleets, workspaces, reports,
    # presentations, Video Studio, Deep Investigation) ───────────────────

    async def _exec_update_work_item(self, config: dict) -> _NodeResult:
        """`action.update_work_item` — note, re-status or complete an item.

        Through the work-item service and its writer gate, as the flow's
        owner: an item they may not write reads as not found.
        """
        from app.models.work_item import WORK_ITEM_STATUSES, WORK_ITEM_TERMINAL_STATUSES
        from app.services.company_managed_writes import (
            CompanyManagedWriteRefusedError,
            flow_writer,
        )
        from app.services.work_item import complete_work_item, update_work_item

        item_ref = self._render(config.get("work_item_id") or "")
        text = self._render(config.get("text") or "")
        tainted = item_ref.tainted or text.tainted
        work_item_id = item_ref.text.strip()
        if safe_uuid(work_item_id) is None:
            return _NodeResult(error="The work item id is not a work item id.")
        operation = (config.get("operation") or "note").strip()
        status = (config.get("status") or "").strip()
        writer = flow_writer(
            is_company_managed=bool(self.run.is_company_managed), flow_id=self.run.flow_id
        )
        user_id = str(self.user.id)
        try:
            if operation == "note":
                if not text.text.strip():
                    return _NodeResult(error="The note is empty.")
                item = await update_work_item(
                    work_item_id, user_id=user_id, note=text.text, writer=writer
                )
            elif operation == "status":
                if status not in WORK_ITEM_STATUSES:
                    return _NodeResult(
                        error=f"The status must be one of: {', '.join(WORK_ITEM_STATUSES)}."
                    )
                if status == "blocked":
                    # Blocked is black or white (#762): a text cannot be the blocker.
                    return _NodeResult(
                        error=(
                            "A flow cannot set a work item blocked: leave it open with a later "
                            "not_before, or create the wait with blocked_on. Nothing was written."
                        )
                    )
                # Ending an item is completing it: its holder is released and
                # its end is reported like any other completion.
                item = (
                    await complete_work_item(
                        work_item_id, user_id=user_id, status=status, writer=writer
                    )
                    if status in WORK_ITEM_TERMINAL_STATUSES
                    else await update_work_item(
                        work_item_id,
                        user_id=user_id,
                        status=status,
                        writer=writer,
                    )
                )
            elif operation == "complete":
                item = await complete_work_item(
                    work_item_id,
                    user_id=user_id,
                    result=text.text.strip() or None,
                    status=status if status in WORK_ITEM_TERMINAL_STATUSES else "done",
                    writer=writer,
                )
            else:
                return _NodeResult(error=f"Unknown operation `{operation}`.")
        except CompanyManagedWriteRefusedError as exc:
            return _NodeResult(error=str(exc))
        except ValueError as exc:
            return _NodeResult(error=format_exception_message(exc))
        if item is None:
            return _NodeResult(
                error="No work item the flow's owner may change has this id."
            )
        return _NodeResult(
            outputs={
                "work_item_id": str(item.get("id") or work_item_id),
                "status": item.get("status") or "",
                "_tainted": tainted,
            }
        )

    async def _exec_dispatch_to_fleet(self, config: dict) -> _NodeResult:
        """`action.dispatch_to_fleet` — a work item for a fleet's orchestrator.

        A fleet takes work through its orchestrator and its workspace (there is
        no fleet column on work): the item is filed in the fleet's workspace
        for the orchestrator resolved NOW, so a fleet whose orchestrator
        changed since the flow was saved gets the current one. Waiting parks
        on the item exactly like Create work item.
        """
        from app.api.v1.helpers import get_user_group_ids, user_visible_filter
        from app.models.fleet import Fleet
        from app.services.company_managed_writes import (
            CompanyManagedWriteRefusedError,
            flow_writer,
        )
        from app.services.shared_state import WorkspaceWriteRefusedError
        from app.services.work_item import WorkItemOrigin, create_work_item

        fleet_id = safe_uuid(str(config.get("fleet_id") or ""))
        group_ids = await get_user_group_ids(self.db, self.user.id)
        fleet = (
            (
                await self.db.execute(
                    select(Fleet).where(
                        Fleet.id == fleet_id,
                        user_visible_filter(
                            Fleet, self.user.id, group_ids, is_admin=self.user.is_admin
                        ),
                    )
                )
            ).scalar_one_or_none()
            if fleet_id is not None
            else None
        )
        if fleet is None or fleet.is_archived:
            return _NodeResult(
                error=(
                    "The fleet behind this node no longer exists, is archived "
                    "or is not visible to the flow's owner."
                )
            )
        from app.models.agent import Agent

        orchestrator = (
            (
                await self.db.execute(
                    select(Agent).where(Agent.id == fleet.orchestrator_agent_id)
                )
            ).scalar_one_or_none()
            if fleet.orchestrator_agent_id is not None
            else None
        )
        # A disabled orchestrator takes no work: an item filed for it would
        # never be dispatched, and a waiting run would wait for ever.
        if orchestrator is None or not orchestrator.enabled:
            return _NodeResult(
                error=(
                    f"The fleet '{fleet.name}' has no enabled orchestrator to "
                    "hand work to: set one on the fleet."
                )
            )
        title = self._render(config.get("title") or "")
        body = self._render(config.get("body") or "")
        correlation = self._render(config.get("correlation_key") or "")
        tainted = title.tainted or body.tainted or correlation.tainted
        if not title.text.strip():
            return _NodeResult(error="The work item needs a title.")
        priority = {"normal": "medium"}.get(
            config.get("priority") or "normal", config.get("priority") or "medium"
        )
        try:
            item = await create_work_item(
                user_id=str(self.user.id),
                title=title.text.strip(),
                description=body.text or None,
                owner_agent_id=str(fleet.orchestrator_agent_id),
                priority=priority,
                correlation_key=correlation.text.strip() or None,
                workspace_id=fleet.workspace_id,
                source="flow",
                origin=WorkItemOrigin(flow_run_id=self.run.id),
                dedupe=bool(correlation.text.strip()),
                writer=flow_writer(
                    is_company_managed=bool(self.run.is_company_managed), flow_id=self.run.flow_id
                ),
            )
        except (CompanyManagedWriteRefusedError, WorkspaceWriteRefusedError) as exc:
            return _NodeResult(error=str(exc))
        except ValueError as exc:
            return _NodeResult(error=format_exception_message(exc))
        if config.get("wait"):
            wake = datetime.now(UTC) + timedelta(seconds=ASSIGNMENT_POLL_SECONDS)
            return _NodeResult(
                park={
                    "kind": "work_item",
                    "work_item_id": str(item.get("id")),
                    "wake_at": wake.isoformat(),
                    "tainted": tainted,
                }
            )
        return _NodeResult(
            outputs={"work_item_id": str(item.get("id")), "_tainted": tainted}
        )

    async def _exec_workspace_add_source(self, config: dict) -> _NodeResult:
        """`action.workspace_add_source` — a text or a web page as a source.

        The REST route's rule: the workspace is one the flow's owner may edit
        (``can_manage``), its Sources knowledge base is created on first use,
        the document is audited and then queued for chunking and embedding. A
        URL goes through ``add_url_document`` (``web_fetch``'s SSRF guard).
        Written in a session of its own: the walker's session commits only
        step transitions.
        """
        from app.api.v1.helpers import can_manage, get_user_group_ids
        from app.core.database import async_session
        from app.services import kb_documents
        from app.services.audit import log_audit
        from app.services.kb_helpers import enqueue_document_processing
        from app.services.workspaces import (
            WORKSPACE_KB_SOURCES,
            ensure_workspace_kb,
            workspace_kb,
        )

        source_kind = (config.get("source_kind") or "text").strip()
        content = self._render(config.get("content") or "")
        title = self._render(config.get("title") or "")
        tainted = content.tainted or title.tainted
        if not content.text.strip():
            return _NodeResult(error="There is nothing to add: the text or URL is empty.")
        if source_kind == "url" and not re.match(r"^https?://", content.text.strip(), re.IGNORECASE):
            return _NodeResult(error="A web page source needs an http(s) address.")
        if source_kind not in ("text", "url"):
            return _NodeResult(error=f"Unknown source `{source_kind}`.")

        async with async_session() as db:
            group_ids = await get_user_group_ids(db, self.user.id)
            try:
                workspace = await resolve_workspace_scope(
                    db, self.user, config.get("workspace_id"), user_group_ids=group_ids
                )
            except WorkspaceScopeError as exc:
                return _NodeResult(error=str(exc))
            if not can_manage(self.user, workspace, group_ids):
                return _NodeResult(
                    error=(
                        "The flow's owner can read this workspace but not edit "
                        "its sources."
                    )
                )
            existed = await workspace_kb(db, workspace.id, WORKSPACE_KB_SOURCES) is not None
            kb = await ensure_workspace_kb(db, workspace, WORKSPACE_KB_SOURCES)
            if not existed:
                # Audited like the REST route: only the call that created it.
                await log_audit(
                    db,
                    user=self.user,
                    action="update",
                    entity_type="workspace",
                    entity_id=workspace.id,
                    entity_name=workspace.name,
                    summary=f"Created the sources of workspace '{workspace.name}' from a flow",
                    details={"knowledge_base_id": str(kb.id), "flow_run_id": str(self.run.id)},
                )
            # Committed before the document: ensuring the knowledge base locks
            # the workspace row, which a page fetch must not hold.
            await db.commit()
            try:
                if source_kind == "url":
                    doc = await kb_documents.add_url_document(db, kb, url=content.text.strip())
                else:
                    doc = await kb_documents.add_text_document(
                        db,
                        kb,
                        filename=(title.text.strip() or "Flow source")[:200],
                        content=content.text,
                        source_type="text",
                    )
            except kb_documents.DocumentRefusedError as exc:
                await db.rollback()
                return _NodeResult(error=str(exc))
            await log_audit(
                db,
                user=self.user,
                action="update",
                entity_type="workspace",
                entity_id=workspace.id,
                entity_name=workspace.name,
                summary=f"Added a source to workspace '{workspace.name}' from a flow",
                details={
                    "document_id": str(doc.id),
                    "flow_id": str(self.run.flow_id),
                    "flow_run_id": str(self.run.id),
                },
            )
            doc_id, kb_id = str(doc.id), str(kb.id)
            await db.commit()
        await enqueue_document_processing(doc_id)
        return _NodeResult(
            outputs={"document_id": doc_id, "knowledge_base_id": kb_id, "_tainted": tainted}
        )

    async def _run_document_tool(self, suite: str, tool: str, arguments: dict) -> Any:
        """Run one tool of the report or presentation suite as the flow's owner.

        The nodes ride the tools rather than re-spell them: the same
        visibility (a deck readable, a report owned), template resolution,
        render slot and stored file. Each tool opens its own sessions, and a
        run has no conversation or assignment run to file under.
        """
        from app.tools.base import ToolCall

        if suite == "report":
            from app.services.builtin_tools.report import build_report_tools

            _defs, executor = build_report_tools(None, str(self.user.id))  # type: ignore[arg-type]
        else:
            from app.services.builtin_tools.presentation import build_presentation_tools

            _defs, executor = build_presentation_tools(None, str(self.user.id))  # type: ignore[arg-type]
        return await executor.execute_tool(ToolCall(id="flow", name=tool, arguments=arguments))

    async def _exec_create_report(self, config: dict) -> _NodeResult:
        """`action.create_report` — a report from a spec, as ``generate_report``."""
        from app.services.email import platform_url

        title = self._render(config.get("title") or "")
        footer = self._render(config.get("footer") or "")
        report_ref = self._render(config.get("report_id") or "")
        header, header_tainted = self._render_json(config.get("header"))
        blocks, blocks_tainted = self._render_json(config.get("blocks"))
        tainted = (
            title.tainted or footer.tainted or report_ref.tainted or header_tainted or blocks_tainted
        )
        if not title.text.strip():
            return _NodeResult(error="The report needs a title.")
        arguments: dict[str, Any] = {
            "title": title.text.strip(),
            "header": header,
            "blocks": blocks,
            "theme": config.get("theme") or "light",
        }
        if footer.text.strip():
            arguments["footer"] = footer.text.strip()
        if config.get("template_id"):
            arguments["template_id"] = str(config["template_id"])
        if report_ref.text.strip():
            arguments["report_id"] = report_ref.text.strip()
        result = await self._run_document_tool("report", "generate_report", arguments)
        written = result.structured if isinstance(result.structured, dict) else None
        if result.is_error or written is None:
            return _NodeResult(error=_to_text(result.content)[:2000] or "The report was not written.")
        report_id = str(written["report_id"])
        return _NodeResult(
            outputs={
                "report_id": report_id,
                "revision": written.get("revision"),
                "url": f"{platform_url()}/reports/{report_id}",
                "_tainted": tainted,
            }
        )

    def _render_json(self, value: Any) -> tuple[Any, bool]:
        """A JSON config value with its ``{{...}}`` references rendered."""
        if self._renderer is None:  # pragma: no cover — _prepare always ran
            return value, False
        return self._renderer.render_value(value if value is not None else {})

    async def _exec_export_document(self, kind: str, config: dict) -> _NodeResult:
        """`action.export_report` / `action.export_presentation` — a stored file."""
        if kind == "action.export_report":
            ref = self._render(config.get("report_id") or "")
            suite, tool, arguments = "report", "export_report", {"report_id": ref.text.strip()}
        else:
            ref = self._render(config.get("presentation_id") or "")
            suite, tool = "presentation", "export_presentation"
            arguments = {
                "presentation_id": ref.text.strip(),
                "format": config.get("format") or "pdf",
            }
        if safe_uuid(ref.text.strip()) is None:
            return _NodeResult(error="The id to export is not a document id.")
        result = await self._run_document_tool(suite, tool, arguments)
        files = list(result.files or [])
        if result.is_error or not files:
            return _NodeResult(error=_to_text(result.content)[:2000] or "Nothing was exported.")
        return _NodeResult(
            outputs={
                "file_id": str(files[0].file_id),
                "filename": files[0].filename,
                "_tainted": ref.tainted,
            }
        )

    async def _exec_render_video(self, config: dict) -> _NodeResult:
        """`action.render_video` — a Video Studio render, waited for.

        ``projects.start_render`` is the one way a render starts (it gates,
        replaces renders in progress, audits); the node parks on the render
        row and the resume tick reads it. A refusal or a failed render takes
        the `fail` pin with its reason.
        """
        from app.core.database import async_session
        from app.core.feature_flags import is_feature_enabled
        from app.models.video_studio import JOB_TERMINAL_STATUSES
        from app.services.video_studio import projects
        from app.services.video_studio.render_checks import RenderGateError
        from app.services.video_studio.spec import QUALITY_PRESETS

        video_ref = self._render(config.get("video_id") or "")
        tainted = video_ref.tainted

        def refused(message: str) -> _NodeResult:
            return _NodeResult(
                outputs={
                    "render_id": "",
                    "status": "refused",
                    "file_id": "",
                    "url": "",
                    "error": message,
                    "_tainted": tainted,
                },
                next_handles=["fail"],
            )

        if not is_feature_enabled("VIDEO_STUDIO"):
            return refused("The Video Studio is turned off on this platform.")
        quality = (config.get("quality") or "high").strip()
        if quality not in QUALITY_PRESETS:
            return refused(f"The quality must be one of: {', '.join(QUALITY_PRESETS)}.")
        video_id = safe_uuid(video_ref.text.strip())
        if video_id is None:
            return refused("The video id is not a video id.")
        async with async_session() as db:
            project = await projects.load_visible_project(db, video_id, self.user)
            if project is None or not await projects.can_manage_project(db, project, self.user):
                return refused("No video the flow's owner may render has this id.")
            try:
                started = await projects.start_render(db, project, self.user, quality=quality)
            except RenderGateError as exc:
                await db.rollback()
                reasons = "; ".join(str(r) for r in (getattr(exc, "reasons", None) or [])) or str(exc)
                return refused(f"The render was refused: {reasons}")
            except projects.RenderLimitError as exc:
                await db.rollback()
                return refused(str(exc))
            render = started.render
            await db.commit()
            await projects.run_started(started)
        waiting = {
            "kind": "video_render",
            "render_id": str(render.id),
            "video_id": str(video_id),
            "tainted": tainted,
        }
        if render.status in JOB_TERMINAL_STATUSES:
            # Answered by a render that already covers this revision.
            return self._video_render_result(render, waiting)
        wake = datetime.now(UTC) + timedelta(seconds=ASSIGNMENT_POLL_SECONDS)
        return _NodeResult(park={**waiting, "wake_at": wake.isoformat()})

    @staticmethod
    def _video_render_result(render: Any, waiting: dict) -> _NodeResult:
        """The node's outcome for an ended render: `ok` only when ready."""
        from app.services.email import platform_url

        ready = render.status == "ready"
        return _NodeResult(
            outputs={
                "render_id": str(render.id),
                "status": render.status,
                # A failed render may still carry its MP4: published either way.
                "file_id": str(render.file_id) if render.file_id else "",
                "url": f"{platform_url()}/videos/{waiting.get('video_id')}",
                "error": "" if ready else (render.error or f"The render ended {render.status}."),
                "_tainted": bool(waiting.get("tainted")),
            },
            next_handles=["ok" if ready else "fail"],
        )

    async def _exec_deep_investigation(self, config: dict) -> _NodeResult:
        """`action.deep_investigation` — one investigation, through the metered turn.

        ``run_deep_investigation_turn`` owns the quota gate, the metering and
        a message for every failure; it commits after every step, so it runs
        in a session of its own, never the walker's. The report is built from
        what the sources returned: it is tainted.
        """
        from app.core.database import async_session
        from app.core.feature_flags import is_feature_enabled
        from app.services.deep_investigation_chat import run_deep_investigation_turn
        from app.services.investigation_events import build_investigation_marker

        if not is_feature_enabled("DEEP_INVESTIGATION"):
            return _NodeResult(error="Deep Investigation is turned off on this platform.")
        subject = self._render(config.get("subject") or "")
        if not subject.text.strip():
            return _NodeResult(error="The investigation needs a subject.")
        packs, _packs_tainted = self._render_json(config.get("source_packs") or [])
        if isinstance(packs, str):
            try:
                packs = json.loads(packs) if packs.strip() else []
            except ValueError:
                return _NodeResult(error="Source packs must be a JSON list of pack slugs.")
        if not isinstance(packs, list) or not all(isinstance(p, str) for p in packs):
            return _NodeResult(error="Source packs must be a JSON list of pack slugs.")
        results: list = []
        async with async_session() as db:
            text = await run_deep_investigation_turn(
                db,
                subject=subject.text.strip(),
                user_id=self.user.id,
                source_pack_slugs=[p.strip() for p in packs if p.strip()] or None,
                # A cancelled run or an emergency stop ends the investigation too.
                cancel_event=self.cancel_event,
                result_out=results,
            )
        if not results:
            # Refused before a run existed: quota, an unknown pack, a subject.
            return _NodeResult(error=_to_text(text)[:2000] or "The investigation did not start.")
        investigation = results[0].investigation
        if investigation.status != "completed":
            return _NodeResult(
                error=f"The investigation ended `{investigation.status}`: {_to_text(text)[:1500]}"
            )
        report = _to_text(text).replace(build_investigation_marker(str(investigation.id)), "").strip()
        return _NodeResult(
            outputs={
                "investigation_id": str(investigation.id),
                "report": report,
                "_tainted": True,
            }
        )

    async def _exec_trigger_assignment(
        self, node: dict, config: dict
    ) -> _NodeResult:
        from app.models.agent import Agent
        from app.models.assignment import Assignment
        from app.services.assignment_engine import trigger_assignment

        assignment = (
            await self.db.execute(
                select(Assignment).where(Assignment.id == config.get("assignment_id"))
            )
        ).scalar_one_or_none()
        if assignment is None:
            return _NodeResult(error="The configured assignment no longer exists.")
        agent = (
            await self.db.execute(
                select(Agent).where(Agent.id == assignment.agent_id)
            )
        ).scalar_one_or_none()
        if agent is None:
            return _NodeResult(error="The assignment's agent no longer exists.")

        payload, payload_tainted = (
            self._renderer.render_value(config.get("payload") or {}, wrap=True)
            if self._renderer
            else (config.get("payload") or {}, False)
        )
        # Caught rather than pre-checked: ``trigger_assignment`` raises
        # ``HTTPException`` when AI is off, which reached this node as an opaque
        # 403 (/code-review, 2026-08-28). Catching adds no read to the path and
        # nothing flushes the run's pending rows. The run itself still finishes
        # (FR-013) — what stops is starting *new* AI work from inside it.
        from fastapi import HTTPException

        from app.services.agentic_gate import (
            AI_DISABLED_CODE,
            refusal_code,
            refusal_is_personal,
            refusal_message,
        )

        try:
            run_id = await trigger_assignment(
                assignment,
                agent,
                payload=payload if isinstance(payload, dict) else {"input": payload},
                db=self.db,
                trigger_context={
                    "type": "flow",
                    "flow_run_id": str(self.run.id),
                    "flow_node_id": node["id"],
                    # Hand the memory event chain across the process boundary.
                    # The assignment executes in the worker, where this
                    # run's ContextVar does not exist, so without this its
                    # agent's memory writes would emit at depth 0 with no
                    # originating flow — and a flow that dispatches an
                    # assignment which writes memory would retrigger itself
                    # forever, never approaching EVENT_CHAIN_MAX_DEPTH. The
                    # assignment adapter rebinds it before executing. Same
                    # freeze-it-on-the-row trick as
                    # ``_register_completion_emissions``.
                    "memory_chain": _memory_chain_handoff(),
                },
                triggered_by_user_id=self.user.id,
            )
        except HTTPException as exc:
            from app.services.assignment_pause import (
                OWNER_DEACTIVATED_MESSAGE,
                AssignmentOwnerDeactivatedError,
            )

            if isinstance(exc, AssignmentOwnerDeactivatedError):
                return _NodeResult(
                    error=(
                        "This node cannot start the assignment. "
                        f"{OWNER_DEACTIVATED_MESSAGE}"
                    )
                )
            refusal = refusal_code(exc)
            if refusal == AI_DISABLED_CODE and refusal_is_personal(exc):
                return _NodeResult(
                    error=(
                        "AI is disabled for a group of the person this assignment "
                        "runs as, so this node cannot start it. An administrator "
                        "can switch it back on in the group's AI access card."
                    )
                )
            if refusal == AI_DISABLED_CODE:
                return _NodeResult(
                    error=(
                        "AI is disabled on this platform, so this node cannot "
                        "start an assignment. Enable AI in Settings > Platform."
                    )
                )
            if refusal is not None:
                # Not licensed - Community Edition.
                return _NodeResult(
                    error=(
                        "This deployment is not licensed for AI, so this node "
                        f"cannot start an assignment. {refusal_message(refusal)}"
                    )
                )
            raise
        if run_id is None:
            return _NodeResult(error="The assignment refused to start.")
        wake = datetime.now(UTC) + timedelta(seconds=ASSIGNMENT_POLL_SECONDS)
        return _NodeResult(
            park={
                "kind": "assignment",
                "assignment_run_id": str(run_id),
                "tainted": payload_tainted,
                "wake_at": wake.isoformat(),
            }
        )

    async def _exec_script(self, config: dict) -> _NodeResult:
        """`action.script` — run a Custom tool's script by id.

        Deliberately NOT a new execution engine: the node binds a platform
        CustomTool and rides the exact runtime agents use —
        ``load_custom_tools`` (enabled filter, {{var_id.*}} secrets,
        $output_files persistence) into ``execute_tool_auto`` (sandboxed JS /
        Python). Arguments render through the flow's taint-tracked renderer
        and coerce against the tool's LIVE input_schema — the DB row is the
        source of truth, not a snapshot frozen on the node.
        """
        from app.models.custom_tool import CustomTool
        from app.services.builtin_tools.loaders import load_custom_tools
        from app.tools.base import CompositeToolExecutor, ToolCall

        tool_id = str(config.get("custom_tool_id") or "").strip()
        if not tool_id:
            return _NodeResult(error="Pick the Custom tool this node runs.")
        tool = (
            await self.db.execute(
                select(CustomTool).where(CustomTool.id == tool_id)
            )
        ).scalar_one_or_none()
        if tool is None or not tool.enabled:
            return _NodeResult(
                error=(
                    "The Custom tool behind this node no longer exists or "
                    "is disabled — pick another one in the configuration."
                )
            )

        raw_arguments = config.get("arguments")
        arguments: dict[str, Any] = {}
        tainted = False
        if isinstance(raw_arguments, dict):
            for key, value in raw_arguments.items():
                if isinstance(value, str):
                    rendered = self._render(value)
                    arguments[key] = rendered.text
                    tainted = tainted or rendered.tainted
                else:
                    arguments[key] = value
        arguments = coerce_tool_arguments(tool.input_schema, arguments)

        composite = CompositeToolExecutor()
        # Platform variables ({{var_id.*}} secrets) must reach the script,
        # exactly as they do on the agent path — without them any tool that
        # binds a credential refuses to run inside a flow.
        from app.api.v1.helpers import get_user_group_ids
        from app.services.variable_resolver import load_variables

        variables = await load_variables(
            self.db, self.user.id, await get_user_group_ids(self.db, self.user.id)
        )
        await load_custom_tools(
            self.db,
            [str(tool.id)],
            composite,
            user_id=str(self.user.id),
            quota_user_id=str(self.user.id),
            variables=variables,
        )
        try:
            result = await composite.execute_tool(
                ToolCall(id="flow", name=tool.name, arguments=arguments)
            )
        except KeyError:
            return _NodeResult(
                error=(
                    "The Custom tool refused to load — check it is enabled "
                    "and its platform variables resolve."
                )
            )
        text = _to_text(result.content)
        if getattr(result, "is_error", False):
            return _NodeResult(error=text[:2000] or "Script failed.")
        outputs: dict[str, Any] = {"result": text, "_tainted": tainted}
        fields = _json_object(text)
        if fields is not None:
            outputs["fields"] = fields
        return _NodeResult(outputs=outputs)

    async def _flow_tool_scope(self) -> CustomToolScope:
        """Where this flow's tool names resolve (``flow_custom_tool_scope``)."""
        if self._tool_scope is None:
            self._tool_scope = await flow_custom_tool_scope(
                self.db, self.run.flow_id, self.user
            )
        return self._tool_scope

    async def _exec_call_tool(self, node: dict, config: dict) -> _NodeResult:
        llm = self._wired_llm(node["id"])
        if llm is not None:
            return await self._exec_call_tool_llm(config, llm)

        tool_name = (config.get("tool_name") or "").strip()
        arguments, tainted = (
            self._renderer.render_value(config.get("arguments") or {})
            if self._renderer
            else (config.get("arguments") or {}, False)
        )
        arguments = arguments if isinstance(arguments, dict) else {}
        # The drawer stores the picked tool's schema alongside — coerce the
        # string-typed form values back to what the tool declares.
        schema = config.get("arguments_schema")
        if isinstance(schema, dict) and schema:
            arguments = coerce_tool_arguments(schema, arguments)
        try:
            result_text = await _call_tool_by_name(
                self.db, self.user, tool_name, arguments,
                scope=await self._flow_tool_scope(),
                pin=tool_pin(config).get(tool_name),
            )
        except ToolResolutionError as exc:
            return _NodeResult(error=str(exc))
        if result_text is None:
            return _NodeResult(error=unavailable_tool_message(tool_name))
        # `arguments` is deliberately NOT published here. In LLM mode it is
        # explainability — the author did not write those values, so the run
        # must record what actually ran. In deterministic mode the author DID
        # write them, they are already on the node, and rendering them into
        # persisted step outputs would widen what run history holds (upstream
        # values can carry tokens or PII) for every flow that already exists.
        return _NodeResult(
            outputs={
                "result": result_text,
                "selected_tool": tool_name,
                "_tainted": tainted,
            }
        )

    async def _exec_call_tool_llm(
        self, config: dict, llm_config: dict
    ) -> _NodeResult:
        """LLM mode: the model picks the tool AND its arguments.

        The model only ever sees the candidates the author declared, each the
        tool picked for it (else the one its name designates) in the flow's
        reach, as in deterministic mode, and only if the runner can use it
        too. It never executes anything: the pick runs by its id
        (``run_offered_tool``), so the tool that runs is the one offered.
        """
        from app.services.flow_tool_selector import select_tool

        objective = self._render(config.get("objective") or "")
        hints = self._render(config.get("argument_hints") or "")
        candidates = config.get("candidate_tools")
        if isinstance(candidates, str):  # tolerate a single stored value
            candidates = [candidates]
        candidates = [c for c in (candidates or []) if isinstance(c, str) and c]

        scope = await self._flow_tool_scope()
        definitions, unresolved = await resolve_callable_definitions(
            self.db,
            self.user,
            candidates,
            scope=scope,
            pins=candidate_tool_pins(config),
        )
        if not definitions:
            missing = ", ".join(f"`{n}`" for n in unresolved) or "none configured"
            return _NodeResult(
                error=(
                    "None of this node's candidate tools are available to you "
                    f"({missing}). Pick tools you can call."
                )
            )

        selection = await select_tool(
            self.db,
            objective=objective.text,
            definitions=definitions,
            llm_config=llm_config,
            argument_hints=hints.text or None,
            # Explicit None check: `or 2` would turn a stored 0 into 2, and
            # "I set 0 and it retried twice" is a confusing bug report. The
            # selector clamps the range; this only supplies the default.
            max_attempts=(
                2 if _as_int(config.get("max_attempts")) is None
                else _as_int(config.get("max_attempts"))
            ),
            user_id=str(self.user.id),
        )
        if not selection.ok or selection.tool_name is None:
            return _NodeResult(error=selection.error or "No tool was chosen.")

        chosen = next(
            (d for d in definitions if d.name == selection.tool_name), None
        )
        arguments = selection.arguments
        if chosen is not None and isinstance(chosen.input_schema, dict):
            arguments = coerce_tool_arguments(chosen.input_schema, arguments)

        try:
            result_text = (
                await run_offered_tool(
                    self.db, self.user, chosen, arguments, scope=scope
                )
                if chosen is not None
                else None
            )
        except ToolResolutionError as exc:
            return _NodeResult(error=str(exc))
        if result_text is None:
            # The model chose from a list we resolved a moment ago, so this is
            # a genuine disappearance (a tool deleted mid-run), not a bad pick.
            return _NodeResult(
                error=(
                    f"`{selection.tool_name}` was chosen but is no longer "
                    "callable."
                )
            )

        reason = selection.reason
        if unresolved:
            # Never let a silently shrunken candidate list read as a clean run.
            reason = (
                f"{reason} (unavailable candidates skipped: "
                f"{', '.join(unresolved)})"
            ).strip()
        return _NodeResult(
            outputs={
                "result": result_text,
                "selected_tool": selection.tool_name,
                "arguments": arguments,
                "reason": reason,
                # The objective and hints are rendered from upstream outputs,
                # and the tool ran on what they said — taint follows.
                "_tainted": objective.tainted or hints.tainted,
            }
        )

    async def _workset_for_node(self, node: dict):
        """The Workset row this action node is wired to, or an error result.

        The binding comes from the wired capability node, never from a field —
        so a flow cannot address a corpus it did not put on the canvas.
        """
        from app.services.workset import WorksetSourceError, resolve_workset

        binding = await self._resolved_workset(node["id"])
        if binding is None:
            return None, _NodeResult(
                error=(
                    "No workset is wired into this node. Connect a Workset "
                    "node to its Workset socket — the corpus is named on the "
                    "canvas, not in a field."
                )
            )
        try:
            workset = await resolve_workset(
                self.db,
                self.user,
                key=binding["key"],
                scope=binding["scope"],
                scope_ref=binding["scope_ref"],
                workspace_id=binding.get("workspace_id"),
                processing=binding["processing"],
                workers=binding["workers"],
                llm_config=binding["llm_config"],
                expires_at=binding["expires_at"],
                **({"max_items": binding["max_items"]} if binding["max_items"] else {}),
                **(
                    {"max_content_chars": binding["max_content_chars"]}
                    if binding["max_content_chars"]
                    else {}
                ),
                **(
                    {"max_auto_calls": binding["max_auto_calls"]}
                    if binding.get("max_auto_calls") is not None
                    else {}
                ),
            )
        except WorksetSourceError as exc:
            return None, _NodeResult(error=str(exc))
        return workset, None

    async def _exec_workset_add(self, node: dict, config: dict) -> _NodeResult:
        from app.services.workset import add_item

        workset, failure = await self._workset_for_node(node)
        if failure is not None:
            return failure

        title = self._render(config.get("title") or "")
        content = self._render(config.get("content") or "")
        ref = self._render(config.get("external_ref") or "")
        occurred = self._render(config.get("occurred_at") or "")
        url = self._render(config.get("source_url") or "")
        priority = self._render(config.get("priority") or "")

        outcome = await add_item(
            self.db,
            workset,
            self.user,
            title=title.text,
            content=content.text or None,
            external_ref=ref.text or None,
            occurred_at=_parse_iso_datetime(occurred.text),
            source_url=url.text or None,
            priority=_as_int(priority.text),
            pinned=bool(config.get("pinned")),
        )
        if not outcome.ok:
            return _NodeResult(error=outcome.error)
        # Queued after the walker's own commit, for the same reason the tool
        # path does it: a worker that dequeued sooner would find no row.
        self.pending_workset_documents.extend(outcome.pending_documents)
        self.pending_workset_auto_items.extend(outcome.auto_pass_items)
        return _NodeResult(
            outputs={
                "item_id": str(outcome.item.id),
                "result": outcome.action,
                "evicted": len(outcome.evicted),
                "_tainted": (
                    title.tainted or content.tainted or ref.tainted
                    or occurred.tainted or url.tainted
                ),
            }
        )

    async def _exec_workset_search(self, node: dict, config: dict) -> _NodeResult:
        from app.services.workset_read import search

        workset, failure = await self._workset_for_node(node)
        if failure is not None:
            return failure

        query = self._render(config.get("query") or "")
        result = await search(
            self.db,
            workset,
            user=self.user,
            query=query.text,
            top_k=_as_int(config.get("limit")) or 10,
            min_priority=_as_int(config.get("min_priority")),
        )
        if result.error:
            return _NodeResult(error=result.error)
        return _NodeResult(
            outputs={
                "hits": [
                    {
                        "item_id": str(hit.item_id),
                        "title": hit.title,
                        "snippet": hit.snippet,
                        "chunk_index": hit.chunk_index,
                        "occurred_at": hit.occurred_at,
                        "priority_score": hit.priority_score,
                    }
                    for hit in result.hits
                ],
                "count": len(result.hits),
                "coverage": (
                    f"{result.note} {result.coverage.describe()}"
                    if result.note
                    else result.coverage.describe()
                ),
                # Hits are text this platform did not author.
                "_tainted": True,
            }
        )

    async def _exec_workset_digest(self, node: dict, config: dict) -> _NodeResult:
        from app.services.workset_read import digest

        workset, failure = await self._workset_for_node(node)
        if failure is not None:
            return failure

        result = await digest(
            self.db,
            workset,
            top_n=_as_int(config.get("top_n")) or 20,
            max_chars=_as_int(config.get("max_chars")) or 12000,
        )
        if config.get("require_ready") and not result.coverage.complete:
            # The author asked for the error path rather than a synthesis over
            # a half-processed corpus. The message says which half is missing.
            return _NodeResult(
                error=(
                    "This workset is not finished processing, and this node is "
                    f"set to fail rather than digest a partial corpus. "
                    f"{result.coverage.describe()}"
                )
            )
        return _NodeResult(
            outputs={
                "digest": result.render(),
                "included": result.included,
                "omitted": result.omitted,
                "coverage": result.coverage.describe(),
                "complete": result.coverage.complete,
                "_tainted": True,
            }
        )

    async def _exec_workset_seal(self, node: dict, config: dict) -> _NodeResult:
        from app.services.workset import seal
        from app.services.workset_read import coverage_of

        del config
        workset, failure = await self._workset_for_node(node)
        if failure is not None:
            return failure

        newly_sealed = workset.sealed_at is None
        await seal(self.db, workset)
        if newly_sealed:
            # Queued after this burst's commit — see `_flush_workset_queue`.
            self.pending_workset_clusters.append(workset.id)
        coverage = await coverage_of(self.db, workset)
        return _NodeResult(
            outputs={
                "coverage": coverage.describe(),
                "complete": coverage.complete,
            }
        )
    async def _exec_http_request(self, config: dict) -> _NodeResult:
        from app.services.web_fetch import validate_outbound_url_async

        url = self._render(config.get("url") or "")
        body = self._render(config.get("body") or "")
        method = (config.get("method") or "GET").upper()
        headers_cfg, _ = (
            self._renderer.render_value(config.get("headers") or {})
            if self._renderer
            else (config.get("headers") or {}, False)
        )
        route = None
        group_id = config.get("relay_group_id")
        if group_id:
            from app.services.relays import egress_route_for

            route = await egress_route_for(
                self.db, "flow", getattr(self.run, "flow_id", None), group_id
            )
            if route is None:
                # Never sent directly in place of a route it cannot read.
                return _NodeResult(
                    error="The relay group this request is routed through is not valid."
                )
        with egress_route(route):
            try:
                safe_url = await validate_outbound_url_async(url.text)
            except ValueError as exc:
                return _NodeResult(error=f"URL refused: {exc}")
            return await self._send_http_request(method, safe_url, headers_cfg, body, route)

    async def _send_http_request(
        self, method: str, safe_url: str, headers_cfg: Any, body: Any, route: Any
    ) -> _NodeResult:
        import httpx

        from app.services.web_fetch import guarded_client_options, validate_outbound_url_async

        # A direct request connects to an address checked when the connection
        # opens (DNS rebinding); a routed one leaves from the relay instead.
        pinned = guarded_client_options(allow_private=False) if route is None else {}
        try:
            async with egress_client(
                timeout=30.0, follow_redirects=False, route=route, **pinned
            ) as client:
                response = await client.request(
                    method,
                    safe_url,
                    headers=(
                        {str(k): str(v) for k, v in headers_cfg.items()}
                        if isinstance(headers_cfg, dict)
                        else None
                    ),
                    content=body.text.encode() if body.text else None,
                )
                # One manual redirect hop, re-validated — never follow blind.
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    safe_next = await validate_outbound_url_async(
                        str(httpx.URL(safe_url).join(location))
                    )
                    response = await client.request(method, safe_next)
        except httpx.HTTPError as exc:
            return _NodeResult(error=f"Request failed: {exc}")

        try:
            parsed: Any = response.json()
        except (json.JSONDecodeError, ValueError):
            parsed = response.text[:20000]
        outcome = _NodeResult(
            outputs={
                "status": response.status_code,
                "body": parsed,
                # An HTTP body is external input: always tainted.
                "_tainted": True,
            }
        )
        if response.status_code >= 400:
            outcome.error = f"HTTP {response.status_code}"
            outcome.outputs.pop("_tainted", None)
        return outcome

    async def _exec_integration(self, config: dict) -> _NodeResult:
        from app.api.v1.helpers import get_user_group_ids
        from app.services.integration_tools import (
            BindingBasis,
            ToolBindings,
            build_integration_tools,
            shadow_invisible_bindings,
        )
        from app.tools.base import ToolCall

        integration_id = config.get("integration_id")
        operation = (config.get("operation") or "").strip()
        parameters, tainted = (
            self._renderer.render_value(config.get("parameters") or {})
            if self._renderer
            else (config.get("parameters") or {}, False)
        )
        from app.models.integration import Integration

        integration = (
            await self.db.execute(
                select(Integration).where(Integration.id == integration_id)
            )
        ).scalar_one_or_none()
        if integration is None:
            return _NodeResult(error="The configured integration no longer exists.")

        # The run acts as its user: the node uses the integration only when
        # that person may, the rule of an agent's binding in chat (a flow
        # lends nothing).
        bindings = {integration.provider: str(integration.id)}
        await shadow_invisible_bindings(
            self.db,
            bindings,
            {},
            acting_user_id=str(self.user.id),
            user_group_ids=await get_user_group_ids(self.db, self.user.id),
        )
        if not bindings:
            return _NodeResult(
                error=(
                    "The run's user may not use the configured integration: it is not "
                    "shared with them, or its connected account is not theirs to use."
                )
            )

        definitions, executor = await build_integration_tools(
            self.db,
            ToolBindings(
                bindings,
                basis=BindingBasis.CALLER_CHECKED,
                reason="a flow Integration node, for the run's user (shadow_invisible_bindings)",
            ),
            context=flow_integration_context(self.user.id),
        )
        known = {d.name for d in definitions}
        if operation not in known:
            return _NodeResult(
                error=(
                    f"`{operation}` is not an operation of this integration. "
                    f"Available: {', '.join(sorted(known)) or 'none'}."
                )
            )
        arguments = parameters if isinstance(parameters, dict) else {}
        # The drawer's form leaves untouched optional fields as "" — sending
        # them would override provider defaults with empty strings.
        arguments = {
            k: v
            for k, v in arguments.items()
            if not (isinstance(v, str) and not v.strip())
        }
        definition = next(d for d in definitions if d.name == operation)
        arguments = coerce_tool_arguments(
            definition.input_schema or {}, arguments
        )
        result = await executor.execute_tool(
            ToolCall(
                id=f"flow-{uuid.uuid4().hex[:8]}",
                name=operation,
                arguments=arguments,
            )
        )
        text = _to_text(result.content)
        if result.is_error and (
            not result.error_returned
            or config.get("fail_on_error_result") in (True, "true")
        ):
            return _NodeResult(error=text)
        outputs = {"result": text, "_tainted": tainted}
        if result.is_error:
            return _NodeResult(outputs=outputs, warning=text[:2000])
        outputs.update(operation_data(result))
        return _NodeResult(outputs=outputs)

    def _flow_call_mode(self, node: dict) -> str:
        """`detached` or `wait` for this call, decided at the handover source."""
        for edge in _edges_into(self.graph, node["id"]):
            if (edge.get("source_handle") or "") != "handover":
                continue
            source = self.nodes.get(edge.get("source") or "")
            if not isinstance(source, dict):
                continue
            mode = ((source.get("config") or {}).get("handover_mode") or "detached")
            return "wait" if str(mode).strip() == "wait" else "detached"
        return "wait"

    async def _exec_flow_call(self, node: dict, config: dict) -> _NodeResult:
        if self.run.depth >= FLOW_CALL_MAX_DEPTH:
            return _NodeResult(
                error=(
                    f"Sub-flow depth limit ({FLOW_CALL_MAX_DEPTH}) reached — "
                    "the call chain loops back on itself too deeply."
                )
            )
        from app.api.v1.helpers import user_visible_filter

        flow = (
            await self.db.execute(
                select(Flow).where(
                    Flow.id == config.get("flow_id"),
                    user_visible_filter(
                        Flow, self.user.id, None, is_admin=self.user.is_admin
                    ),
                )
            )
        ).scalar_one_or_none()
        if flow is None:
            return _NodeResult(
                error="The called flow no longer exists, or you cannot access it."
            )
        from app.services.assignment_pause import (
            FLOW_OWNER_DEACTIVATED_MESSAGE,
            is_paused_by_deactivated_owner,
        )

        if await is_paused_by_deactivated_owner(self.db, flow):
            return _NodeResult(
                error="This node cannot start the flow. "
                + FLOW_OWNER_DEACTIVATED_MESSAGE
            )
        rendered_input = self._render(config.get("input") or "", wrap=False)
        # A child whose manual trigger declares typed parameters is called
        # like a UE function: one argument per declared parameter, rendered
        # here and coerced against the child's signature. A mismatch fails
        # THIS node — the child never starts with a malformed payload.
        child_payload: dict[str, Any] = {"input": rendered_input.text}
        args_tainted = False
        child_manual = find_trigger_nodes(flow.graph or {}, "trigger.manual")
        declared = (
            declared_parameters(child_manual[0].get("config") or {})
            if child_manual
            else []
        )
        if declared:
            arguments = config.get("arguments") or {}
            rendered_args: dict[str, str] = {}
            for param in declared:
                raw = arguments.get(param["name"])
                if raw is None:
                    continue
                rendered_arg = self._render(raw)
                rendered_args[param["name"]] = rendered_arg.text
                args_tainted = args_tainted or rendered_arg.tainted
            payload, errors = coerce_parameters(declared, rendered_args)
            if errors:
                return _NodeResult(
                    error="Sub-flow arguments mismatch: " + " ".join(errors)
                )
            # Shaped as the manual trigger's DECLARED outputs so the child's
            # trigger step passes it through verbatim ({{t.payload.<name>}}).
            child_payload = {"payload": payload, "prompt": rendered_input.text}
        # The budget this call imposes on the flow it starts: a Budget
        # node wired into THIS node's `budget` socket, merged over whatever
        # budget this run itself was started with — per dimension, for the same
        # reason as the agent node. Carrying the inherited one forward is what
        # makes a single wired ceiling reach a whole chain of sub-flows rather
        # than stopping one level down.
        child_budget = self._compose_budget(node["id"])
        from app.services.assignment_pause import FlowOwnerDeactivatedError

        try:
            child = await create_flow_run(
                self.db,
                flow,
                trigger_type="flow_call",
                trigger_payload=child_payload,
                trigger_context={
                    "parent_flow_run_id": str(self.run.id),
                    "untrusted": rendered_input.tainted
                    or args_tainted
                    or self.run.trigger_type in UNTRUSTED_TRIGGER_TYPES,
                    **({"budget_overrides": child_budget} if child_budget else {}),
                    **self._sandbox_child_context(),
                },
                user_id=self.user.id,
                parent_run_id=self.run.id,
                parent_node_id=node["id"],
                depth=self.run.depth + 1,
            )
        except FlowOwnerDeactivatedError:
            # Deactivated since the check above.
            return _NodeResult(
                error="This node cannot start the flow. "
                + FLOW_OWNER_DEACTIVATED_MESSAGE
            )
        # Detached: the relay lives its own life — the child run is
        # created (the reconciler picks it up like any pending run) and THIS
        # run continues immediately. No park, no `output`: join later with
        # Wait for event if the relay emits one.
        #
        # The mode belongs to the handover SOURCE: a call fed by an agent's
        # handover edge follows that agent's `handover_mode`, and the
        # transfer spirit makes DETACHED the default there (the relay takes
        # over, this flow keeps its own life). A normal call always waits.
        if self._flow_call_mode(node) == "detached":
            return _NodeResult(
                outputs={
                    "child_run_id": str(child.id),
                    "detached": True,
                    "_tainted": rendered_input.tainted or args_tainted,
                }
            )
        wake = datetime.now(UTC) + timedelta(seconds=FLOW_CALL_RECHECK_SECONDS)
        return _NodeResult(
            park={
                "kind": "flow_call",
                "child_run_id": str(child.id),
                "tainted": rendered_input.tainted,
                "wake_at": wake.isoformat(),
            }
        )

    def _exec_delay(self, node: dict, config: dict) -> _NodeResult:
        seconds = parse_delay(_to_text(config.get("duration")))
        if seconds <= 0:
            return _NodeResult(
                error=(
                    f"`{config.get('duration')}` is not a valid duration "
                    "(use 30s, 15m, 2h, 1d)."
                )
            )
        # Passthrough output: the first inbound node's primary output.
        passthrough = ""
        source_id = ""
        inbound = _edges_into(self.graph, node["id"])
        if inbound and self._renderer is not None:
            source_id = inbound[0].get("source") or ""
            source_outputs = self._renderer._outputs.get(source_id)
            if source_outputs is not None:
                source_node = self.nodes.get(source_id) or {}
                passthrough = _primary_output(
                    source_node.get("kind") or "", source_outputs
                )
        wake = datetime.now(UTC) + timedelta(seconds=seconds)
        return _NodeResult(
            park={
                "kind": "delay",
                "passthrough": passthrough,
                # Carried in the park state, not recomputed on wake: a delay
                # can outlive the burst, and the resume must not have to
                # re-derive who fed it.
                "passthrough_tainted": self._source_taint(source_id),
                "wake_at": wake.isoformat(),
            }
        )

    def _exec_loop(self, node: dict, config: dict) -> _NodeResult:
        node_id = node["id"]
        loops = self.state["loops"]
        loop_state = loops.get(node_id)

        if loop_state is None:
            rendered = self._render(config.get("items") or "")
            items = _parse_items(rendered.text)
            cap = int(config.get("max_iterations") or 100)
            items = items[: max(0, cap)]
            loop_state = {
                "items": items,
                "index": 0,
                "done": False,
                "armed": int(self.state["seq"]),
                "enclosing": self._enclosing_passes(),
            }
            loops[node_id] = loop_state
        else:
            loop_state["index"] = int(loop_state["index"]) + 1

        items = loop_state["items"]
        index = int(loop_state["index"])
        if index < len(items):
            return _NodeResult(
                outputs={"item": items[index], "index": index},
                next_handles=["each"],
            )
        loop_state["done"] = True
        return _NodeResult(
            outputs={"item": items[-1] if items else "", "index": max(0, len(items) - 1)},
            next_handles=["done"],
        )

    def _exec_while(self, node: dict, config: dict) -> _NodeResult:
        """UE While: re-evaluate the condition before EVERY pass.

        Rides the ForEach machinery: the node registers in ``state["loops"]``
        so the queue-drain rule re-executes it after each body pass. The
        condition reads templates fresh each time — a Set variable in the
        body is how the loop converges. ``max_iterations`` is the local
        runaway guard on top of MAX_STEPS_PER_RUN.
        """
        node_id = node["id"]
        loops = self.state["loops"]
        loop_state = loops.get(node_id)
        if loop_state is None:
            loop_state = {
                "index": 0,
                "done": False,
                "armed": int(self.state["seq"]),
                "enclosing": self._enclosing_passes(),
            }
            loops[node_id] = loop_state
        else:
            loop_state["index"] = int(loop_state["index"]) + 1
        passes = int(loop_state["index"])

        cap = int(config.get("max_iterations") or 25)
        left = self._render(config.get("left") or "")
        right = self._render(config.get("right") or "")
        holds = evaluate_operator(
            left.text, config.get("operator") or "equals", right.text
        )
        if holds and passes < cap:
            return _NodeResult(
                outputs={"passes": passes, "_tainted": left.tainted or right.tainted},
                next_handles=["body"],
            )
        loop_state["done"] = True
        return _NodeResult(
            outputs={"passes": passes, "_tainted": left.tainted or right.tainted},
            next_handles=["done"],
        )

    async def _exec_code(self, config: dict) -> _NodeResult:
        """Run Python through the PLATFORM sandbox.

        Reuses the code interpreter's exact execution path —
        ``execute_python`` picks OpenSandbox when configured/reachable and
        falls back to the local module-whitelisted subprocess wrapper — so
        flows inherit the same isolation, quotas and admin kill-switch.
        Rendered inputs travel as a JSON-decoded ``inputs`` dict rather than
        being spliced into the source, which kills the quoting/injection
        class entirely.
        """
        from app.services.code_interpreter import execute_python

        code = _to_text(config.get("code") or "")
        if not code.strip():
            return _NodeResult(error="The node has no code to run.")
        rendered_inputs, tainted = (
            self._renderer.render_value(config.get("inputs") or {})
            if self._renderer
            else (config.get("inputs") or {}, False)
        )
        # Refs inside the code body render too (documented), AFTER the
        # preamble is built from the clean inputs dict.
        rendered_code = self._render(code)
        preamble = (
            "import json as _json\n"
            f"inputs = _json.loads({json.dumps(json.dumps(rendered_inputs))})\n"
        )
        timeout = min(300, max(1, int(config.get("timeout") or 60)))
        try:
            result = await execute_python(
                preamble + rendered_code.text,
                timeout=timeout,
                user_id=self.user.id,
                quota_user_id=self.user.id,
                db=self.db,
            )
        except RuntimeError as exc:  # admin kill-switch, quota
            return _NodeResult(error=str(exc))
        if result.get("error"):
            return _NodeResult(error=str(result["error"])[:2000])
        stdout = str(result.get("stdout") or "").strip()
        outputs: dict = {
            "output": stdout,
            "_tainted": tainted or rendered_code.tainted,
        }
        fields = _json_object(stdout)
        if fields is not None:
            outputs["fields"] = fields
        return _NodeResult(outputs=outputs)

    def _exec_expression(self, config: dict) -> _NodeResult:
        """One sandboxed expression — the pure-function node.

        `{{...}}` references render BEFORE parsing (they arrive as literal
        values), then the AST-whitelisted evaluator does the rest. A refused
        or failing expression takes the error path with the evaluator's own
        message — no LLM anywhere near this.
        """
        from app.services.flow_expression import ExpressionError, evaluate_expression

        # wrap=False: untrusted markers would corrupt the expression source;
        # taint still propagates through the flag and the sandbox has no IO.
        rendered = self._render(config.get("expression") or "", wrap=False)
        try:
            value = evaluate_expression(rendered.text)
        except ExpressionError as exc:
            return _NodeResult(error=str(exc))
        outputs: dict = {"value": value, "_tainted": rendered.tainted}
        if isinstance(value, dict):
            outputs["fields"] = value
        return _NodeResult(outputs=outputs)

    def _exec_set_variable(self, config: dict) -> _NodeResult:
        name = str(config.get("name") or "").strip()
        if not name:
            return _NodeResult(error="The variable needs a name.")
        rendered = self._render(config.get("value") or "")
        variables: dict = self.state["vars"]
        mode = config.get("mode") or "set"
        if mode == "append":
            value = f"{variables.get(name, '')}{rendered.text}"
        elif mode == "increment":
            def _num(text: str) -> float | int:
                text = str(text).strip() or "0"
                try:
                    return float(text) if "." in text else int(text)
                except ValueError:
                    return 0
            total = _num(variables.get(name, 0)) + _num(rendered.text or "1")
            value = int(total) if float(total).is_integer() else total
        else:
            value = rendered.text
        variables[name] = value
        tainted = set(self.state["vars_tainted"])
        if rendered.tainted:
            tainted.add(name)
        else:
            tainted.discard(name)
        self.state["vars_tainted"] = sorted(tainted)
        return _NodeResult(
            outputs={"value": value, "_tainted": rendered.tainted}
        )

    # Hard ceiling on collection reads: run state (and every downstream
    # template) carries the list, so an unbounded fleet/library must not
    # balloon it. The Loop's own max_iterations still applies on top.
    _COLLECTION_CAP = 500

    async def _exec_get_variable(self, config: dict) -> _NodeResult:
        if str(config.get("source") or "flow") == "collection":
            return await self._exec_get_collection(config)
        name = str(config.get("name") or "").strip()
        if not name:
            return _NodeResult(error="The variable needs a name.")
        variables: dict = self.state["vars"]
        if name in variables:
            value = variables[name]
            tainted = name in set(self.state["vars_tainted"])
        else:
            rendered = self._render(config.get("default") or "")
            value, tainted = rendered.text, rendered.tainted
        count = len(value) if isinstance(value, list) else 0
        return _NodeResult(
            outputs={"value": value, "count": count, "_tainted": tainted}
        )

    async def _exec_get_collection(self, config: dict) -> _NodeResult:
        """Platform collections as iterable lists.

        Same visibility as the builder's pickers (own + group-shared +
        company-managed) so what the drawer offered is what the run sees.
        Items are compact plain dicts: `{{loop.item.name}}` downstream.
        """
        from app.api.v1.helpers import get_user_group_ids, user_visible_query

        collection = str(config.get("collection") or "runners")
        group_ids = await get_user_group_ids(self.db, self.user.id)

        async def _rows(model):
            return (
                (
                    await self.db.execute(
                        user_visible_query(
                            model,
                            self.user.id,
                            group_ids,
                            is_admin=self.user.is_admin,
                        ).limit(self._COLLECTION_CAP)
                    )
                )
                .scalars()
                .all()
            )

        items: list[dict[str, Any]]
        if collection == "runners":
            from app.core.config import settings as app_settings

            if not app_settings.runners_enabled:
                return _NodeResult(
                    error="Runners are disabled on this platform."
                )
            from app.models.runner import Runner
            from app.services.runners import is_runner_online

            items = [
                {
                    "id": str(r.id),
                    "name": r.name,
                    "platform": r.platform,
                    "status": "online" if is_runner_online(r) else "offline",
                    "online": is_runner_online(r),
                }
                for r in await _rows(Runner)
            ]
        elif collection == "agents":
            from app.models.agent import Agent

            items = [
                {"id": str(a.id), "name": a.name}
                for a in await _rows(Agent)
            ]
        elif collection == "flows":
            items = [
                {"id": str(f.id), "name": f.name, "status": f.status}
                for f in await _rows(Flow)
            ]
        elif collection == "custom_tools":
            from app.models.custom_tool import CustomTool

            items = [
                {"id": str(t.id), "name": t.name, "enabled": bool(t.enabled)}
                for t in await _rows(CustomTool)
            ]
        elif collection == "knowledge_bases":
            from app.models.knowledge_base import KnowledgeBase

            items = [
                {"id": str(kb.id), "name": kb.name}
                for kb in await _rows(KnowledgeBase)
            ]
        elif collection == "event_types":
            from app.models.flow_event import FlowEventType

            items = [
                {"id": str(et.id), "name": et.name}
                for et in await _rows(FlowEventType)
            ]
        else:
            return _NodeResult(
                error=f"Unknown collection '{collection}'."
            )
        return _NodeResult(outputs={"value": items, "count": len(items)})

    def _exec_do_once(self, node: dict) -> _NodeResult:
        counters: dict = self.state["node_counters"]
        count = int(counters.get(node["id"], 0)) + 1
        counters[node["id"]] = count
        return _NodeResult(
            outputs={"count": count},
            next_handles=["first" if count == 1 else "again"],
        )

    def _exec_flip_flop(self, node: dict) -> _NodeResult:
        counters: dict = self.state["node_counters"]
        count = int(counters.get(node["id"], 0)) + 1
        counters[node["id"]] = count
        is_a = count % 2 == 1
        return _NodeResult(
            outputs={"is_a": is_a},
            next_handles=["a" if is_a else "b"],
        )

    def _source_taint(self, *source_ids: str) -> bool:
        """True when any of *source_ids* is a node marked untrusted.

        Passthrough nodes (Reroute, Delay, Merge) copy another node's output
        verbatim instead of rendering a template, so they never pass through
        ``_render`` and never see its taint verdict. Without this, routing an
        untrusted value through a knot LAUNDERS it: the passthrough's own step
        is journaled clean, the node is left out of ``_untrusted``, and a
        downstream agent node rendering ``{{passthrough.value}}`` gets
        ``tainted=False`` — so ``wrap_untrusted`` never quarantines it and the
        prompt-injection defence is bypassed by an intermediate node that adds
        no data of its own. Taint has to survive the copy.

        Reading ``_untrusted`` at execution time is safe across bursts: the set
        only ever grows and is persisted in ``state["untrusted"]``, so a merge
        that parks and resumes still sees every arrival's verdict.
        """
        if self._renderer is None:
            return False
        return any(sid and sid in self._renderer._untrusted for sid in source_ids)

    def _exec_reroute(self, node: dict) -> _NodeResult:
        """A knot: pass the first inbound node's primary output on."""
        value = ""
        source_id = ""
        inbound = _edges_into(self.graph, node["id"])
        if inbound and self._renderer is not None:
            source_id = inbound[0].get("source") or ""
            source_outputs = self._renderer._outputs.get(source_id)
            if source_outputs is not None:
                source_node = self.nodes.get(source_id) or {}
                value = _primary_output(
                    source_node.get("kind") or "", source_outputs
                )
        return _NodeResult(
            outputs={"value": value, "_tainted": self._source_taint(source_id)}
        )

    def _exec_gate(self, config: dict) -> _NodeResult:
        name = str(config.get("variable") or "").strip()
        if not name:
            return _NodeResult(error="The gate needs a variable to watch.")
        variables: dict = self.state["vars"]
        if name in variables:
            raw = str(variables[name]).strip().lower()
            is_open = raw not in ("", "false", "0", "no", "off", "none")
        else:
            is_open = bool(config.get("default_open"))
        return _NodeResult(
            outputs={"open": is_open},
            next_handles=["out" if is_open else "blocked"],
        )

    def _exec_merge(self, node: dict, config: dict) -> _NodeResult:
        node_id = node["id"]
        merges = self.state["merges"]
        entry = merges.setdefault(node_id, {"arrivals": [], "fired": False})
        strategy = config.get("strategy") or "wait_all"

        if entry["fired"]:
            # first_wins already passed through; later arrivals are dropped.
            return _NodeResult(outputs={"merged": ""}, next_handles=[])

        arrivals = entry["arrivals"]
        if strategy == "first_wins":
            entry["fired"] = True
            value = arrivals[0][1] if arrivals else ""
            # Only the arrival that actually won carries its taint through.
            winner = arrivals[0][0] if arrivals else ""
            return _NodeResult(
                outputs={"merged": value, "_tainted": self._source_taint(winner)}
            )

        # wait_all / concat: hold while any non-merge work can still arrive.
        queue: list[str] = self.state["queue"]
        producers_left = any(
            (self.nodes.get(q, {}).get("kind") or "") != "util.merge" for q in queue
        ) or any(
            not ls.get("done") for ls in self.state["loops"].values()
        )
        if producers_left:
            queue.append(node_id)  # revisit once the queue drains to merges
            return _NodeResult(outputs={"merged": ""}, next_handles=[])

        entry["fired"] = True
        merged = "\n\n".join(value for _, value in arrivals if value)
        # The concatenation is tainted if ANY branch that contributed text was:
        # one untrusted arrival is enough to make the merged string untrusted.
        return _NodeResult(
            outputs={
                "merged": merged,
                "_tainted": self._source_taint(
                    *(source for source, value in arrivals if value)
                ),
            }
        )

    # ── Blueprint-style data utilities ───────────────────────────────────

    def _exec_break(self, config: dict) -> _NodeResult:
        """`util.break` — Break Struct: expose a JSON object's fields."""
        value = self._render(config.get("value") or "")
        raw = value.text.strip()
        if not raw:
            return _NodeResult(error="There is no value to break apart.")
        try:
            parsed = json.loads(raw)
        except ValueError:
            return _NodeResult(
                error="The value is not valid JSON, so it has no fields to break out."
            )
        if not isinstance(parsed, dict):
            return _NodeResult(
                error="Break needs a JSON object — this value is a "
                + ("list." if isinstance(parsed, list) else "plain value.")
            )
        return _NodeResult(
            outputs={"fields": parsed, "text": value.text, "_tainted": value.tainted}
        )

    def _exec_cast(self, config: dict) -> _NodeResult:
        """`util.cast` — typed conversion where failure is a pin, not a crash."""
        value = self._render(config.get("value") or "")
        target = (config.get("to") or "text").strip()
        raw = value.text.strip()

        converted: Any | None = None
        ok = True
        if target == "number":
            try:
                as_float = float(raw)
                converted = int(as_float) if as_float.is_integer() else as_float
            except ValueError:
                ok = False
        elif target == "boolean":
            lowered = raw.lower()
            truthy = {"true", "yes", "1", "on"}
            falsy = {"false", "no", "0", "off", ""}
            if lowered in truthy:
                converted = True
            elif lowered in falsy:
                converted = False
            else:
                ok = False
        elif target == "json":
            try:
                converted = json.loads(raw)
            except ValueError:
                ok = False
        else:  # text — everything already is one.
            converted = value.text

        if not ok:
            return _NodeResult(
                outputs={"value": value.text, "_tainted": value.tainted},
                next_handles=["fail"],
            )
        return _NodeResult(
            outputs={"value": converted, "_tainted": value.tainted},
            next_handles=["ok"],
        )

    def _exec_select(self, config: dict) -> _NodeResult:
        """`util.select` — pick a value from a mapping by key, with a default."""
        key = self._render(config.get("key") or "")
        options = config.get("options") or {}
        tainted = key.tainted
        if isinstance(options, dict) and key.text in options:
            chosen = self._render(_to_text(options[key.text]))
            return _NodeResult(
                outputs={"value": chosen.text, "_tainted": tainted or chosen.tainted}
            )
        default = self._render(config.get("default") or "")
        return _NodeResult(
            outputs={"value": default.text, "_tainted": tainted or default.tainted}
        )

    def _exec_stop(self, config: dict) -> _NodeResult:
        """`util.stop` — Return: complete the run at this node."""
        result = self._render(config.get("result") or "")
        return _NodeResult(
            outputs={"result": result.text, "_tainted": result.tainted},
            next_handles=[],
            halt=True,
        )

    # ── park resume ──────────────────────────────────────────────────────

    async def _resume_waiting(self, waiting: dict) -> BurstOutcome | None:
        """Complete (or re-park) the node the run parked on.

        Returns None when the walk should continue, or a BurstOutcome when
        the run must park again / stop.
        """
        node_id = waiting.get("node") or ""
        node = self.nodes.get(node_id)
        if node is None:
            self.state["waiting"] = None
            return None
        kind = waiting.get("kind")

        if kind == "delay":
            wake_at = waiting.get("wake_at")
            if wake_at and datetime.fromisoformat(wake_at) > datetime.now(UTC):
                # Woken early (deploy, reclaim): park again for the remainder.
                self.run.status = FLOW_RUN_STATUS_AWAITING
                return BurstOutcome(
                    kind="park", wake_at=datetime.fromisoformat(wake_at)
                )
            await self._finish_parked_node(
                node,
                waiting,
                _NodeResult(
                    outputs={
                        "input": waiting.get("passthrough") or "",
                        "_tainted": bool(waiting.get("passthrough_tainted")),
                    }
                ),
            )
            return None

        if kind == "assignment":
            from app.models.assignment import AssignmentRun

            run_row = (
                await self.db.execute(
                    select(AssignmentRun).where(
                        AssignmentRun.id == _coerce_uuid(waiting.get("assignment_run_id"))
                    )
                )
            ).scalar_one_or_none()
            if run_row is None:
                return await self._fail_parked_node(
                    node, waiting, "The assignment run vanished."
                )
            if run_row.status in ("completed", "failed", "cancelled", "dismissed"):
                if run_row.status != "completed":
                    return await self._fail_parked_node(
                        node, waiting, f"Assignment run ended `{run_row.status}`."
                    )
                await self._finish_parked_node(
                    node,
                    waiting,
                    _NodeResult(
                        outputs={
                            "result": run_row.result_summary or "",
                            "run_id": str(run_row.id),
                            "_tainted": bool(waiting.get("tainted")),
                        }
                    ),
                )
                return None
            # Still running: poll again.
            self.run.status = FLOW_RUN_STATUS_AWAITING
            wake = datetime.now(UTC) + timedelta(seconds=ASSIGNMENT_POLL_SECONDS)
            waiting["wake_at"] = wake.isoformat()
            self._persist_state()
            return BurstOutcome(kind="park", wake_at=wake)

        if kind == "work_item":
            from app.services.work_item import get_work_item

            item = await get_work_item(
                str(waiting.get("work_item_id") or ""),
                user_id=str(self.user.id),
            )
            if item is None:
                return await self._fail_parked_node(
                    node, waiting, "The work item vanished."
                )
            status = item.get("status") or ""
            if status == "done":
                await self._finish_parked_node(
                    node,
                    waiting,
                    _NodeResult(
                        outputs={
                            "work_item_id": item.get("id"),
                            "result": item.get("result") or "",
                            "_tainted": bool(waiting.get("tainted")),
                        }
                    ),
                )
                return None
            if status in ("cancelled", "blocked"):
                return await self._fail_parked_node(
                    node, waiting, f"The work item ended `{status}`."
                )
            # Still open / claimed / in progress: poll again.
            self.run.status = FLOW_RUN_STATUS_AWAITING
            wake = datetime.now(UTC) + timedelta(seconds=ASSIGNMENT_POLL_SECONDS)
            waiting["wake_at"] = wake.isoformat()
            self._persist_state()
            return BurstOutcome(kind="park", wake_at=wake)

        if kind == "breakpoint":
            node_id = waiting.get("node") or ""
            if waiting.get("released"):
                # Re-enter _execute_node on the paused node, skipping the
                # breakpoint check exactly once.
                self.state["skip_breakpoint_once"] = node_id
                self.state["waiting"] = None
                self._persist_state()
                return None
            # Still paused: poll for the debugger's release.
            self.run.status = FLOW_RUN_STATUS_AWAITING
            wake = datetime.now(UTC) + timedelta(seconds=BREAKPOINT_POLL_SECONDS)
            waiting["wake_at"] = wake.isoformat()
            self._persist_state()
            return BurstOutcome(kind="park", wake_at=wake)

        if kind == "human":
            if waiting.get("released"):
                await self._finish_parked_node(
                    node,
                    waiting,
                    _NodeResult(
                        outputs={
                            "response": waiting.get("response") or "",
                            "option": waiting.get("option") or "",
                            "responded_by": waiting.get("responded_by") or "",
                        }
                    ),
                )
                return None
            # Still unanswered: the respond endpoint wakes the engine, this
            # poll is only the safety net.
            self.run.status = FLOW_RUN_STATUS_AWAITING
            wake = datetime.now(UTC) + timedelta(seconds=HUMAN_POLL_SECONDS)
            waiting["wake_at"] = wake.isoformat()
            self._persist_state()
            return BurstOutcome(kind="park", wake_at=wake)

        if kind == "event":
            from app.services.flow_events import find_matching_event

            event = await find_matching_event(
                self.db,
                event_type_id=str(waiting.get("event_type_id") or ""),
                user_id=self.user.id,
                flow_id=self.run.flow_id,
                run_id=self.run.id,
                after_iso=waiting.get("parked_at"),
            )
            if event is not None:
                await self._finish_parked_node(
                    node,
                    waiting,
                    _NodeResult(
                        outputs={
                            "fields": event.payload or {},
                            "event": {
                                "id": str(event.id),
                                "name": waiting.get("event_name") or "",
                                "scope": event.scope,
                                "chain_depth": event.chain_depth,
                            },
                        }
                    ),
                )
                return None
            # Nothing heard yet: poll again.
            self.run.status = FLOW_RUN_STATUS_AWAITING
            wake = datetime.now(UTC) + timedelta(seconds=ASSIGNMENT_POLL_SECONDS)
            waiting["wake_at"] = wake.isoformat()
            self._persist_state()
            return BurstOutcome(kind="park", wake_at=wake)

        if kind == "runner_command":
            from app.models.runner import COMMAND_TERMINAL_STATUSES, RunnerCommand

            cmd = (
                await self.db.execute(
                    select(RunnerCommand).where(
                        RunnerCommand.id == _coerce_uuid(waiting.get("command_id"))
                    )
                )
            ).scalar_one_or_none()
            if cmd is None:
                # The row cascades away when the runner is deleted mid-wait.
                # Failure is a pin on this node, never a run failure — same
                # contract as denied/failed/timeout.
                await self._finish_parked_node(
                    node,
                    waiting,
                    _NodeResult(
                        outputs={
                            "command_id": str(waiting.get("command_id") or ""),
                            "status": "cancelled",
                            "result": {},
                            "file_id": "",
                            "error": (
                                "The runner command vanished — the machine "
                                "was likely un-enrolled while this node "
                                "waited."
                            ),
                        },
                        next_handles=["fail"],
                    ),
                )
                return None
            if cmd.status in COMMAND_TERMINAL_STATUSES:
                await self._finish_parked_node(
                    node,
                    waiting,
                    _NodeResult(
                        outputs={
                            "command_id": str(cmd.id),
                            "status": cmd.status,
                            # Measured and cut off the loop: up to 16 MiB of JSON.
                            "result": await asyncio.to_thread(
                                bound_runner_result, cmd.result or {}
                            ),
                            "file_id": (
                                str(cmd.result_file_id)
                                if cmd.result_file_id
                                else ""
                            ),
                            "error": cmd.error or "",
                        },
                        next_handles=[
                            "ok" if cmd.status == "completed" else "fail"
                        ],
                    ),
                )
                return None
            # Still running on the machine (or awaiting approval): the result
            # upload nudges the reconciler, this poll is only the safety net.
            self.run.status = FLOW_RUN_STATUS_AWAITING
            wake = datetime.now(UTC) + timedelta(seconds=ASSIGNMENT_POLL_SECONDS)
            waiting["wake_at"] = wake.isoformat()
            self._persist_state()
            return BurstOutcome(kind="park", wake_at=wake)

        if kind == "video_render":
            from app.models.video_studio import JOB_TERMINAL_STATUSES, VideoRender

            render = (
                await self.db.execute(
                    select(VideoRender).where(VideoRender.id == _coerce_uuid(waiting.get("render_id")))
                )
            ).scalar_one_or_none()
            if render is None:
                # Deleted while the node waited: a pin, like a vanished command.
                await self._finish_parked_node(
                    node,
                    waiting,
                    _NodeResult(
                        outputs={
                            "render_id": str(waiting.get("render_id") or ""),
                            "status": "cancelled",
                            "file_id": "",
                            "url": "",
                            "error": "The render was deleted while this node waited.",
                            "_tainted": bool(waiting.get("tainted")),
                        },
                        next_handles=["fail"],
                    ),
                )
                return None
            if render.status in JOB_TERMINAL_STATUSES:
                await self._finish_parked_node(
                    node, waiting, self._video_render_result(render, waiting)
                )
                return None
            # Still queued or rendering: renders take minutes, polled.
            self.run.status = FLOW_RUN_STATUS_AWAITING
            wake = datetime.now(UTC) + timedelta(seconds=ASSIGNMENT_POLL_SECONDS)
            waiting["wake_at"] = wake.isoformat()
            self._persist_state()
            return BurstOutcome(kind="park", wake_at=wake)

        if kind == "flow_call":
            child = (
                await self.db.execute(
                    select(FlowRun).where(FlowRun.id == _coerce_uuid(waiting.get("child_run_id")))
                )
            ).scalar_one_or_none()
            if child is None:
                return await self._fail_parked_node(
                    node, waiting, "The sub-flow run vanished."
                )
            if child.status in FLOW_RUN_ACTIVE_STATUSES:
                self.run.status = FLOW_RUN_STATUS_AWAITING
                wake = datetime.now(UTC) + timedelta(
                    seconds=FLOW_CALL_RECHECK_SECONDS
                )
                waiting["wake_at"] = wake.isoformat()
                self._persist_state()
                return BurstOutcome(kind="park", wake_at=wake)
            if child.status != FLOW_RUN_STATUS_COMPLETED:
                # A skipped child says why in its summary, never in `error`.
                detail = child.error or (
                    child.result_summary
                    if child.status == FLOW_RUN_STATUS_SKIPPED
                    else None
                )
                return await self._fail_parked_node(
                    node,
                    waiting,
                    f"Sub-flow ended `{child.status}`"
                    + (f": {detail}" if detail else "."),
                )
            child_tainted = bool(waiting.get("tainted")) or bool(
                (child.trigger_context or {}).get("untrusted")
            )
            await self._finish_parked_node(
                node,
                waiting,
                _NodeResult(
                    outputs={
                        "output": child.result_summary or "",
                        "child_run_id": str(child.id),
                        "detached": False,
                        "_tainted": child_tainted,
                    }
                ),
            )
            return None

        # Unknown park kind — fail loudly rather than spin.
        return await self._fail_parked_node(
            node, waiting, f"Unknown wait state `{kind}`."
        )

    async def _load_parked_step(self, waiting: dict) -> FlowRunStep | None:
        step_id = waiting.get("step_id")
        if not step_id:
            return None
        return (
            await self.db.execute(
                select(FlowRunStep).where(FlowRunStep.id == _coerce_uuid(step_id))
            )
        ).scalar_one_or_none()

    async def _finish_parked_node(
        self, node: dict, waiting: dict, result: _NodeResult
    ) -> None:
        """Complete the step that parked — ONE journal row per node execution.

        Closing the parked row empty and recording a second completed row
        (the first cut) made every waiting node appear twice in the timeline
        and left ``{{node.x}}`` resolving against the empty one.
        """
        tainted = bool(result.outputs.pop("_tainted", False))
        step = await self._load_parked_step(waiting)
        if step is not None:
            step.status = "completed"
            step.output = result.outputs or None
            step.untrusted = tainted
            step.completed_at = datetime.now(UTC)
        node_id = node["id"]
        if self._renderer is not None:
            self._renderer._outputs[node_id] = result.outputs or {}
            if tainted:
                self._renderer._untrusted.add(node_id)
                untrusted = set(self.state.get("untrusted") or [])
                untrusted.add(node_id)
                self.state["untrusted"] = sorted(untrusted)
        self.state["waiting"] = None
        self._enqueue_targets(
            node_id, result.next_handles, result.switch_case, result.handover_types
        )
        self._persist_state()
        await self.db.commit()
        await publish_flow_run_update(
            self.run, node_id=node_id, node_status="completed"
        )

    async def _fail_parked_node(
        self, node: dict, waiting: dict, error: str
    ) -> BurstOutcome | None:
        """Fail the parked step in place, then follow its error port if wired."""
        step = await self._load_parked_step(waiting)
        if step is not None:
            step.status = "failed"
            step.error = error[:4000]
            step.output = {"error": error[:2000]}
            step.completed_at = datetime.now(UTC)
        self.state["waiting"] = None
        node_id = node["id"]
        if not _edges_from(self.graph, node_id, "error"):
            return await self._fail(
                f"Step `{node.get('label') or node_id}` failed: {error}"
            )
        self._enqueue_targets(node_id, ["error"], None)
        self._persist_state()
        await self.db.commit()
        await publish_flow_run_update(self.run, node_id=node_id, node_status="failed")
        return None


def _confidence_floor(value: Any) -> float:
    """A Classify node's confidence floor, clamped to [0, 1]; 0 when unreadable."""
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return min(max(number, 0.0), 1.0)


def _parse_items(text: str) -> list[Any]:
    """A loop's items: JSON array if it parses, else non-empty lines."""
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass
    return [line.strip() for line in stripped.splitlines() if line.strip()]


# ─── Cross-source tool resolution (action.call_tool) ────────────────────────


def flow_builtin_tools() -> list[tuple[Any, Any]]:
    """Built-in tools executable OUTSIDE an agent loop.

    Only the self-contained builders qualify — they need no conversation,
    files or agent context. Code execution deliberately stays on the
    dedicated `Run code` node (same sandbox, richer contract), and
    KB/memory/media tools need agent context. Returns
    ``[(ToolDefinition, executor)]`` pairs, built fresh per call.
    """
    from app.services.builtin_tools.fimi_math import build_fimi_math_tool
    from app.services.builtin_tools.fimi_metrics import build_fimi_metrics_tool
    from app.services.builtin_tools.stix import (
        build_stix_bundle_validator_tool,
    )
    from app.services.builtin_tools.timeline_diff import build_timeline_diff_tool
    from app.tools.base import CallableToolExecutor, ToolDefinition

    pairs: list[tuple[Any, Any]] = []
    for build in (
        # A flow step reads the answer text, not a model: the bundle stays inline.
        lambda: build_stix_bundle_validator_tool(inline_bundle_max_chars=None),
        build_fimi_math_tool,
        build_fimi_metrics_tool,
        build_timeline_diff_tool,
    ):
        definitions, executor = build()
        pairs.extend((d, executor) for d in definitions)

    web_executor = CallableToolExecutor()

    async def _web_fetch(url: str, max_chars: int = 20000) -> str:
        from app.services.web_fetch import fetch_and_extract

        result = await fetch_and_extract(
            str(url), max_chars=min(max(int(max_chars), 1000), 80000)
        )
        return _to_text(getattr(result, "content", None) or result)

    web_executor.register("web_fetch", _web_fetch)
    pairs.append(
        (
            ToolDefinition(
                name="web_fetch",
                description=(
                    "Fetch a URL and extract its readable text content."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The http(s) URL to fetch.",
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": "Truncate the extracted text (default 20000).",
                        },
                    },
                    "required": ["url"],
                },
                source="builtin",
            ),
            web_executor,
        )
    )
    return pairs


@dataclass(frozen=True)
class CustomToolScope:
    """The custom tools a tool NAME may designate, and whose come first.

    ``user_id`` is ``None`` only with ``is_admin`` (a company-managed flow):
    the reach is then every tool and none of them is anyone's own.
    """

    user_id: uuid.UUID | None
    group_ids: tuple[str, ...]
    is_admin: bool


async def custom_tool_scope_of(db: AsyncSession, user: User) -> CustomToolScope:
    """*user*'s own reach: own, shared with them, company-managed (every tool
    for an admin, the rule of the builder's pickers)."""
    from app.api.v1.helpers import get_user_group_ids

    return CustomToolScope(
        user.id, tuple(await get_user_group_ids(db, user.id)), user.is_admin
    )


async def flow_custom_tool_scope(
    db: AsyncSession, flow_id: uuid.UUID, runner: User
) -> CustomToolScope:
    """Where the tool names a flow stores resolve, whoever runs it.

    A name is picked in the builder among its writer's tools, and what a flow
    stores runs as stored for everyone who runs it. So it resolves in
    the reach of the flow's writers: every tool for a company-managed flow,
    which only admins write (its creator, kept as ``user_id``, no longer
    does, so their tools do not come first), and its owner's reach for any
    other flow.
    """
    row = (
        await db.execute(
            select(Flow.user_id, Flow.is_company_managed).where(Flow.id == flow_id)
        )
    ).one_or_none()
    return await custom_tool_scope_for_flow(
        db,
        owner_id=row.user_id if row is not None else None,
        is_company_managed=bool(row is not None and row.is_company_managed),
        fallback=runner,
    )


async def custom_tool_scope_for_flow(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID | None,
    is_company_managed: bool,
    fallback: User,
) -> CustomToolScope:
    """``flow_custom_tool_scope`` for a flow described by its owner, which a
    write reads before the row exists (*fallback* stands for a missing
    owner: the creator of a new flow, the runner of an ownerless one)."""
    if is_company_managed:
        return CustomToolScope(None, (), True)
    owner = None
    if owner_id is not None and owner_id != fallback.id:
        owner = (
            await db.execute(select(User).where(User.id == owner_id))
        ).scalar_one_or_none()
    return await custom_tool_scope_of(db, owner or fallback)


class ToolResolutionError(Exception):
    """A Call-tool step's tool that cannot be run; the message is the step's
    error."""


class AmbiguousToolNameError(ToolResolutionError):
    """A tool name that designates more than one custom tool."""

    def __init__(self, name: str) -> None:
        super().__init__(ambiguous_tool_message(name))
        self.name = name


class CustomToolLoadError(ToolResolutionError):
    """The custom tool a name designates did not load (no code to run, or a
    loader failure), so neither it nor a same-named integration tool runs."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"The custom tool `{name}` could not be loaded; check that it has "
            "code to run."
        )
        self.name = name


def ambiguous_tool_message(name: str) -> str:
    return (
        f"More than one custom tool this flow can use is named `{name}`; "
        "give them distinct names so it designates one."
    )


def unavailable_tool_message(name: str) -> str:
    return f"No tool named `{name}` is available to this flow."


def _name_tier(
    tool: Any, scope: CustomToolScope, admin_owner_ids: set[uuid.UUID]
) -> int:
    """The tier a tool sits in: *scope*'s own, company-managed, an admin's,
    anyone else's.

    Only the owner fills the first tier and only admins the next two, so a
    name settled there cannot be taken over, or made ambiguous, by anyone
    else. Two tools of a name in the deciding tier are refused, never
    settled by an order: any user can create, rename or share a tool into
    the last one.
    """
    if scope.user_id is not None and tool.user_id == scope.user_id:
        return 0
    if tool.is_company_managed:
        return 1
    return 2 if tool.user_id in admin_owner_ids else 3


def _reach_filter(scope: CustomToolScope):
    """The custom tools *scope* reaches, as a filter clause."""
    from app.api.v1.helpers import user_visible_filter
    from app.models.custom_tool import CustomTool

    return user_visible_filter(
        CustomTool, scope.user_id, list(scope.group_ids), is_admin=scope.is_admin
    )


async def resolve_custom_tools_by_name(
    db: AsyncSession, names: list[str] | set[str], scope: CustomToolScope
) -> tuple[dict[str, Any], set[str]]:
    """The enabled custom tool each of *names* designates within *scope*.

    Returns ``(tools by name, ambiguous names)``. Nothing makes a tool name
    unique, so a name takes the tool of the first ``_name_tier`` holding it,
    and is ambiguous when that tier holds more than one. A name no tool in
    *scope* carries is in neither, exactly like a name no tool carries.
    """
    from app.models.custom_tool import CustomTool

    wanted = {n for n in names if isinstance(n, str) and n}
    if not wanted:
        return {}, set()
    rows = (
        (
            await db.execute(
                select(CustomTool).where(
                    CustomTool.name.in_(wanted),
                    CustomTool.enabled.is_(True),
                    _reach_filter(scope),
                )
            )
        )
        .scalars()
        .all()
    )
    other_owner_ids = {
        tool.user_id
        for tool in rows
        if tool.user_id is not None
        and tool.user_id != scope.user_id
        and not tool.is_company_managed
    }
    admin_owner_ids: set[uuid.UUID] = set()
    if other_owner_ids:
        admin_owner_ids = set(
            (
                await db.execute(
                    select(User.id).where(
                        User.id.in_(other_owner_ids), User.role == "admin"
                    )
                )
            )
            .scalars()
            .all()
        )
    tiers: dict[str, dict[int, list[Any]]] = {}
    for tool in rows:
        tier = _name_tier(tool, scope, admin_owner_ids)
        tiers.setdefault(tool.name, {}).setdefault(tier, []).append(tool)
    picked: dict[str, Any] = {}
    ambiguous: set[str] = set()
    for name, by_tier in tiers.items():
        deciding = by_tier[min(by_tier)]
        if len(deciding) == 1:
            picked[name] = deciding[0]
        else:
            ambiguous.add(name)
    return picked, ambiguous


async def resolve_callable_definitions(
    db: AsyncSession,
    user: User,
    names: list[str],
    *,
    scope: CustomToolScope | None = None,
    pins: dict[str, str] | None = None,
) -> tuple[list[Any], list[str]]:
    """The ``ToolDefinition``s an LLM-mode Call-tool node may offer for *names*.

    Returns ``(definitions, unresolved_names)``.

    This covers the same set the Call-tool drawer offers — flow-callable
    built-ins, then custom tools — and in the same
    precedence order ``_call_tool_by_name`` uses, so a built-in wins a name
    collision in both places. It is a strict SUBSET of what
    ``_call_tool_by_name`` can execute: that one also resolves integration
    tools, which have their own node and are not offered here. Subset is the
    safe direction — every tool the model may be offered is one the runner can
    invoke, never the reverse.

    A custom tool is the one *pins* names for it (``pinned_custom_tools``),
    else the one its name designates in *scope* (the caller's own by
    default, a flow's in a run: ``resolve_custom_tools_by_name``), and it is
    offered only when *user* can use it too: a flow's writer names a tool of
    the flow's reach, never one of whoever runs it. A pinned tool the user
    cannot use is not replaced by another of its name. The pick runs by its
    ``entity_id``, so the tool that runs is the one whose schema the model
    was offered. That is the security boundary of LLM mode and is covered
    by ``test_flow_runner_postgres.py``.

    A name that no longer resolves is reported rather than dropped: a
    candidate list that silently shrinks would change a node's behaviour
    without anyone being told.
    """
    wanted = [n for n in names if isinstance(n, str) and n]
    if not wanted:
        return [], []
    remaining = set(wanted)
    found: dict[str, Any] = {}

    for definition, _executor in flow_builtin_tools():
        if definition.name in remaining:
            found[definition.name] = definition
            remaining.discard(definition.name)

    if remaining:
        from app.tools.base import ToolDefinition

        own = await custom_tool_scope_of(db, user)
        reach = scope or own
        wanted_pins = {name: pin for name, pin in (pins or {}).items() if name in remaining}
        pinned = pinned_custom_tools(wanted_pins, await usable_custom_tools(db, reach, wanted_pins.values()))
        tools, _ambiguous = await resolve_custom_tools_by_name(db, remaining - set(pinned), reach)
        tools.update(pinned)
        if scope is not None and scope != own:
            usable = await usable_custom_tool_ids(db, own, [t.id for t in tools.values()])
            tools = {name: tool for name, tool in tools.items() if tool.id in usable}
        for name, tool in tools.items():
            found[name] = ToolDefinition(
                name=tool.name,
                description=tool.description or "",
                # Custom tools carry their JSON Schema directly; it is the same
                # schema `GET /flows/callable-tools` shows the drawer, so the
                # form an author sees and what the model is offered agree.
                input_schema=tool.input_schema
                or {"type": "object", "properties": {}},
                source="custom",
                entity_id=str(tool.id),
            )

    # Preserve the author's declared order — it is the order the model sees.
    definitions = [found[n] for n in wanted if n in found]
    unresolved = [n for n in wanted if n not in found]
    return definitions, unresolved


async def usable_custom_tools(db: AsyncSession, scope: CustomToolScope, ids: Iterable[Any]) -> dict[uuid.UUID, Any]:
    """Those of *ids* (UUIDs or their text) that are enabled custom tools
    *scope* reaches, by id."""
    from app.models.custom_tool import CustomTool

    wanted = {_tool_id(value) for value in ids} - {None}
    if not wanted:
        return {}
    rows = (
        (
            await db.execute(
                select(CustomTool).where(
                    CustomTool.id.in_(wanted),
                    CustomTool.enabled.is_(True),
                    _reach_filter(scope),
                )
            )
        )
        .scalars()
        .all()
    )
    return {tool.id: tool for tool in rows}


async def usable_custom_tool_ids(db: AsyncSession, scope: CustomToolScope, ids: Iterable[Any]) -> set[uuid.UUID]:
    """Those of *ids* that are enabled custom tools *scope* reaches."""
    return set(await usable_custom_tools(db, scope, ids))


def tool_pin(config: Any) -> dict[str, str]:
    """A deterministic Call-tool node's pin, ``{tool_name: tool_id}``:
    the custom tool picked for its name. Empty for a node written without one."""
    if not isinstance(config, dict):
        return {}
    name, pin = config.get("tool_name"), config.get("tool_id")
    if not isinstance(name, str) or not name.strip() or not isinstance(pin, str) or not pin:
        return {}
    return {name.strip(): pin}


def candidate_tool_pins(config: Any) -> dict[str, str]:
    """An LLM-mode Call-tool node's pins, candidate name -> the id of the
    custom tool picked for it (``candidate_tool_ids``)."""
    pins = config.get("candidate_tool_ids") if isinstance(config, dict) else None
    if not isinstance(pins, dict):
        return {}
    return {name: pin for name, pin in pins.items() if isinstance(name, str) and name and isinstance(pin, str) and pin}


def pinned_custom_tools(pins: dict[str, str], tools: dict[uuid.UUID, Any]) -> dict[str, Any]:
    """The tool each name of *pins* runs as: its pinned tool, while *tools*
    (``usable_custom_tools`` of the flow's reach) holds it under that name.

    A name missing here is resolved by name (``resolve_custom_tools_by_name``):
    a node written before pins existed, or one whose tool was deleted,
    disabled, renamed or left the reach. A pin never designates a tool of
    another name, so it cannot make a node run what its name does not say.
    """
    picked: dict[str, Any] = {}
    for name, pin in pins.items():
        tool = tools.get(_tool_id(pin))
        if tool is not None and tool.name == name:
            picked[name] = tool
    return picked


def _tool_id(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    return safe_uuid(value) if isinstance(value, str) else None


def unpinned_tool_message(name: str) -> str:
    return (
        f"The custom tool picked for `{name}` is no longer available to this "
        "flow; another tool of that name is used instead."
    )


async def _run_custom_tool(
    db: AsyncSession, user: User, tool: Any, arguments: dict
) -> str:
    """Run *tool* as *user*, with *user*'s variables; raises
    ``CustomToolLoadError`` when it does not load."""
    from app.api.v1.helpers import get_user_group_ids
    from app.services.builtin_tools.loaders import load_custom_tools
    from app.services.variable_resolver import load_variables
    from app.tools.base import CompositeToolExecutor, ToolCall

    # CompositeToolExecutor, not CallableToolExecutor: load_custom_tools
    # registers through add_tools, which only the composite carries — the
    # callable variant made this branch fail on its first real use.
    composite = CompositeToolExecutor()
    variables = await load_variables(db, user.id, await get_user_group_ids(db, user.id))
    await load_custom_tools(
        db, [str(tool.id)], composite, user_id=str(user.id),
        variables=variables,
    )
    # The composite answers an unregistered name with a "not found" result
    # rather than raising, which would read as this tool's output.
    if tool.name not in composite.tool_names:
        raise CustomToolLoadError(tool.name)
    result = await composite.execute_tool(
        ToolCall(id="flow", name=tool.name, arguments=arguments)
    )
    return _to_text(result.content)


async def run_offered_tool(
    db: AsyncSession,
    user: User,
    definition: Any,
    arguments: dict,
    *,
    scope: CustomToolScope,
) -> str | None:
    """Run the tool *definition* offered the model (``resolve_callable_definitions``).

    A custom tool runs by its id, not its name: nothing a concurrent create,
    rename or share does between the offer and the pick changes which tool
    that is. It must still be an enabled tool of that name that *scope* and
    *user* both reach, else None, like a tool deleted since the offer.
    """
    if definition.source != "custom":
        return await _call_tool_by_name(
            db, user, definition.name, arguments, scope=scope
        )
    from app.models.custom_tool import CustomTool

    tool_id = safe_uuid(definition.entity_id)
    if tool_id is None:
        return None
    tool = (
        await db.execute(
            select(CustomTool).where(
                CustomTool.id == tool_id,
                CustomTool.name == definition.name,
                CustomTool.enabled.is_(True),
                _reach_filter(scope),
                _reach_filter(await custom_tool_scope_of(db, user)),
            )
        )
    ).scalar_one_or_none()
    if tool is None:
        return None
    return await _run_custom_tool(db, user, tool, arguments)


async def _call_tool_by_name(
    db: AsyncSession,
    user: User,
    tool_name: str,
    arguments: dict,
    *,
    scope: CustomToolScope,
    pin: str | None = None,
) -> str | None:
    """Resolve *tool_name* across built-ins, custom tools and integrations.

    The custom tool is *pin* (the node's ``tool_id``) while it is an enabled
    tool of that name within *scope* (``pinned_custom_tools``), else the one
    the name designates within *scope* (``resolve_custom_tools_by_name``);
    it runs as *user*, with *user*'s variables. Returns the tool's text
    result, or None when nothing matches; raises a ``ToolResolutionError``
    for a name that designates several custom tools, or one that does not
    load. The custom tool wins over an integration tool of the same name
    (it is user-authored and explicit).
    """
    from app.tools.base import ToolCall

    # 0. Flow-callable built-ins by exact name.
    for definition, executor in flow_builtin_tools():
        if definition.name != tool_name:
            continue
        result = await executor.execute_tool(
            ToolCall(id="flow", name=tool_name, arguments=arguments)
        )
        return _to_text(result.content)

    # 1. The custom tool the node pinned, else the one the name designates,
    # within the scope.
    pins = {tool_name: pin} if pin else {}
    tool = pinned_custom_tools(pins, await usable_custom_tools(db, scope, pins.values())).get(tool_name)
    if tool is None:
        tools, ambiguous = await resolve_custom_tools_by_name(db, [tool_name], scope)
        if tool_name in ambiguous:
            raise AmbiguousToolNameError(tool_name)
        tool = tools.get(tool_name)
    if tool is not None:
        return await _run_custom_tool(db, user, tool, arguments)

    # 2. Integration tools across the caller's connected integrations.
    from app.models.integration import Integration

    integrations = (
        (
            await db.execute(
                select(Integration).where(Integration.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    if integrations:
        from app.services.integration_tools import BindingBasis, ToolBindings, build_integration_tools

        bindings = {i.provider: str(i.id) for i in integrations}
        definitions, executor = await build_integration_tools(
            db,
            ToolBindings(bindings, basis=BindingBasis.CALLER_CHECKED, reason="the caller's own integrations"),
            context=flow_integration_context(user.id),
        )
        if any(d.name == tool_name for d in definitions):
            result = await executor.execute_tool(
                ToolCall(id="flow", name=tool_name, arguments=arguments)
            )
            return _to_text(result.content)
    return None


# ─── Run creation ────────────────────────────────────────────────────────────


async def close_running_steps(
    db: AsyncSession, run_id: uuid.UUID, *, error: str
) -> None:
    """Close every still-`running` journal row of a run that won't burst again.

    The two-phase journal opens a row BEFORE its node executes, so an
    out-of-band termination — operator cancel while a node runs, a
    dead-lettered execution, a rolled-back terminal write — strands
    committed `running` rows on a terminal run that the next-burst orphan
    sweep can never reach. ``failed`` with an explicit error is the honest
    close (the step-status constraint has no ``cancelled``). The caller
    owns the transaction.
    """
    rows = (
        (
            await db.execute(
                select(FlowRunStep).where(
                    FlowRunStep.flow_run_id == run_id,
                    FlowRunStep.status == "running",
                )
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    for row in rows:
        row.status = "failed"
        row.error = error[:4000]
        row.completed_at = now


async def release_runner_commands(db: AsyncSession, run: FlowRun, *, reason: str) -> None:
    """Give up the runner commands of a run that won't burst again, and its render.

    Once the run is cancelled or dead-lettered nobody reads their results, so
    a command it queued must not run later - nor wait for an approval that
    would now run it for a run that no longer exists - and one a machine
    already took is not sent again. The caller owns the transaction.
    """
    from app.services.runner_transport import give_up_flow_run_commands

    await give_up_flow_run_commands(db, run.id, reason)
    await _release_waited_render(db, run, reason=reason)


async def _release_waited_render(db: AsyncSession, run: FlowRun, *, reason: str) -> None:
    """Cancel the Video Studio render a given-up run's Render video node waits on.

    The render would otherwise hold a worker, and count against its owner's
    render limit, for a result nobody reads. The caller owns the transaction.
    """
    waiting = (run.state or {}).get("waiting") if isinstance(run.state, dict) else None
    if not isinstance(waiting, dict) or waiting.get("kind") != "video_render":
        return
    from app.models.video_studio import VideoRender
    from app.services.video_studio import jobs

    render = await db.get(VideoRender, _coerce_uuid(waiting.get("render_id")))
    if render is not None:
        await jobs.cancel(db, "render", render, reason=reason)


async def stop_flow_run(
    db: AsyncSession,
    run: FlowRun,
    *,
    step_error: str = "The run was cancelled while this node was active.",
    command_reason: str = "The flow run that dispatched this command was cancelled.",
) -> bool:
    """Cancel an active *run*: the status the walker checks, its open steps, its
    runner commands and its executions. False when it had already ended. The
    caller commits."""
    if run.status not in FLOW_RUN_ACTIVE_STATUSES:
        return False
    from app.services.orchestration_engine import cancel_executions_for_runs

    run.status = FLOW_RUN_STATUS_CANCELLED
    run.completed_at = datetime.now(UTC)
    # A cancel can land while a node executes or while one is parked: either
    # leaves a committed `running` journal row no later burst will close.
    await close_running_steps(db, run.id, error=step_error)
    await release_runner_commands(db, run, reason=command_reason)
    await cancel_executions_for_runs(db, [run.id])
    return True


async def create_flow_run(
    db: AsyncSession,
    flow: Flow,
    *,
    trigger_type: str,
    trigger_payload: dict | None,
    trigger_context: dict | None,
    user_id: uuid.UUID | None,
    parent_run_id: uuid.UUID | None = None,
    parent_node_id: str | None = None,
    depth: int = 0,
    run_at: datetime | None = None,
) -> FlowRun:
    """Create a run (frozen graph) + its orchestration execution, unflushed.

    The caller owns the transaction and must ``commit`` then call
    :func:`app.services.orchestration_engine.notify_orchestration_wake`.

    Raises ``FlowOwnerDeactivatedError`` (409 ``owner_deactivated``) for a
    flow (not company-managed) whose owner's account is deactivated, before anything
    is added: the backstop of every trigger path, for an owner deactivated
    after its query picked the flow. An automatic caller catches it and undoes
    what it did for this run (stamp, seen event, claim, debounce window).
    """
    from app.core.config import settings
    from app.services import orchestration_engine as engine
    from app.services.assignment_pause import refuse_flow_if_owner_deactivated

    await refuse_flow_if_owner_deactivated(db, flow)

    run = FlowRun(
        flow_id=flow.id,
        flow_version_number=flow.version_number,
        graph=flow.graph or {},
        user_id=user_id,
        status=FLOW_RUN_STATUS_PENDING,
        trigger_type=trigger_type,
        trigger_payload=trigger_payload,
        trigger_context=trigger_context,
        depth=depth,
        parent_run_id=parent_run_id,
        parent_node_id=parent_node_id,
        is_company_managed=flow.is_company_managed,
        shared_group_ids=list(flow.shared_group_ids or []) or None,
        shared_user_ids=list(flow.shared_user_ids or []) or None,
        access_restricted=bool(flow.access_restricted),
    )
    db.add(run)
    await db.flush()

    deadline = datetime.now(UTC) + timedelta(
        seconds=settings.flow_run_timeout_seconds
    )
    await engine.create_execution(
        db,
        adapter="flow",
        payload={
            "run_id": str(run.id),
            "flow_id": str(flow.id),
            "deadline_at": deadline.isoformat(),
        },
        run_at=run_at,
        deadline_at=deadline,
        dedup_key=f"flow_run:{run.id}",
    )
    return run


async def active_run_count(db: AsyncSession, flow_id: uuid.UUID) -> int:
    """How many runs of *flow_id* are active, whatever started them.

    The admission count of a Webhook trigger that allows concurrent runs
    (``flow_triggers.webhook_run_admitted``)."""
    from sqlalchemy import func

    return int(
        (
            await db.execute(
                select(func.count(FlowRun.id)).where(
                    FlowRun.flow_id == flow_id,
                    FlowRun.status.in_(FLOW_RUN_ACTIVE_STATUSES),
                )
            )
        ).scalar_one()
        or 0
    )


async def has_active_run(db: AsyncSession, flow_id: uuid.UUID) -> bool:
    """Overlap guard for schedule and integration triggers (a webhook counts
    its flow's active runs against its own limit, ``active_run_count``).

    Deliberately NOT applied to flow-event dispatch: each emission is a
    distinct payload, so dropping one because a previous listener run is
    still active would lose data; the chain-depth cap bounds emit chains
    instead."""
    row = (
        await db.execute(
            select(FlowRun.id)
            .where(
                FlowRun.flow_id == flow_id,
                FlowRun.status.in_(FLOW_RUN_ACTIVE_STATUSES),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None
